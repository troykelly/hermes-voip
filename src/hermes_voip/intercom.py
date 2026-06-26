"""Intercom entry actuation: config + relay client (ADR-0031).

The ``intercom`` caller mode screens a visitor and, for a legitimate expected
visitor, opens an entry (door / gate). There are TWO actuation paths, chosen by
config (the operator said "not sure, build both"):

* **DTMF** — send a configured DTMF string on the LIVE call (e.g. the gateway /
  door-phone's own open code ``"9"``). Uses the same RFC 4733 telephone-event TX
  path as the ``send_dtmf`` tool (ADR-0010/0031). No external dependency.
* **RELAY** — POST to an external relay / HTTP endpoint (a smart-lock bridge, a
  home-automation webhook). The URL and a bearer token come from the environment /
  1Password; the token is NEVER committed and never logged.

The default mode is **DISABLED**: :meth:`open_entry` then fails LOUD (rule 37 — no
accidental and no silent door opening). Security posture: the intercom caller group
runs at a non-operator privilege level with an ``allowed_tools`` sub-ceiling scoped
to ONLY the entry action (ADR-0031 §1), so even a spoofed caller-ID reaching the
group can reach nothing but this; and ``open_entry`` is ELEVATED, so a level-0
caller is blocked at the gate.

The relay HTTP call uses the standard library (``urllib``) wrapped in a worker
thread — a single rare POST needs no third-party HTTP dependency (AGENTS.md rule 40:
introduce no new dependency without an ADR).
"""

from __future__ import annotations

import asyncio
import logging
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from hermes_voip.config import ConfigError
from hermes_voip.dtmf import digit_to_event

__all__ = [
    "IntercomConfig",
    "IntercomOpenMode",
    "IntercomRelayClient",
    "IntercomRelayError",
    "load_intercom_config",
]

_log = logging.getLogger(__name__)


# Control-char boundaries. A character is a control character if its code point is
# a C0 control (U+0000-U+001F: CR=0x0D, LF=0x0A, NUL=0x00, …), DEL (U+007F), or a
# C1 control (U+0080-U+009F). urllib/http.client raises a bare ValueError for some
# of these in header values (and DEL/C1 are header-unsafe), bypassing the typed
# exception chain — so we reject the whole band at load.
_C0_LIMIT = 0x20
_DEL = 0x7F
_C1_LAST = 0x9F


def _is_control_char(ch: str) -> bool:
    """Return True if ``ch`` is a C0 (< 0x20), DEL (0x7f), or C1 (0x80-0x9f) control."""
    code = ord(ch)
    return code < _C0_LIMIT or _DEL <= code <= _C1_LAST


def _reject_control_chars(value: str, label: str) -> None:
    r"""Raise ConfigError if ``value`` contains any control character.

    Rejects C0 controls (U+0000-U+001F: CR (\r), LF (\n), NUL (\x00), …), DEL
    (U+007F), and C1 controls (U+0080-U+009F). These either cause HTTP header
    injection or are rejected by ``http.client`` with a bare ``ValueError`` that
    bypasses the ``(HTTPError, URLError, TimeoutError, OSError)`` catch in
    ``_open_blocking``.  Reject at load so the error is deterministic and the
    contract (IntercomRelayError from ``open()``) holds. The error reports only the
    offending character's code point (e.g. ``U+000A``), never the surrounding value
    (which may be a secret).
    """
    for ch in value:
        if _is_control_char(ch):
            msg = (
                f"{label} must not contain control characters "
                f"(found U+{ord(ch):04X} - rejecting to prevent HTTP header injection)"
            )
            raise ConfigError(msg)


# Env keys (PII-safe: the relay token is a secret, read from env/1Password only).
_OPEN_MODE_KEY = "HERMES_VOIP_INTERCOM_OPEN_MODE"
_DTMF_KEY = "HERMES_VOIP_INTERCOM_DTMF"
_RELAY_URL_KEY = "HERMES_VOIP_INTERCOM_RELAY_URL"
_RELAY_TOKEN_KEY = "HERMES_VOIP_INTERCOM_RELAY_TOKEN"  # noqa: S105 — env-var NAME, not a secret value
_RELAY_METHOD_KEY = "HERMES_VOIP_INTERCOM_RELAY_METHOD"
_RELAY_TIMEOUT_KEY = "HERMES_VOIP_INTERCOM_RELAY_TIMEOUT_S"

_DEFAULT_RELAY_METHOD = "POST"
_DEFAULT_RELAY_TIMEOUT_S = 5.0
_ALLOWED_RELAY_METHODS = ("POST", "GET", "PUT")


class IntercomOpenMode(Enum):
    """How the intercom opens the entry (ADR-0031).

    ``DISABLED`` (the default) means no actuation is configured: :meth:`open_entry`
    raises rather than silently doing nothing — opening a door is too important to
    fail quietly.
    """

    DISABLED = "disabled"
    DTMF = "dtmf"  # send a DTMF open code on the live call
    RELAY = "relay"  # POST to an external relay / HTTP endpoint


@dataclass(frozen=True, slots=True)
class IntercomConfig:
    """Immutable intercom actuation configuration (ADR-0031).

    Attributes:
        open_mode: The actuation path (DISABLED / DTMF / RELAY).
        dtmf_digits: The DTMF open code to send in DTMF mode (e.g. ``"9"``). Empty
            unless ``open_mode`` is ``DTMF``.
        relay_url: The relay endpoint URL in RELAY mode (https only — it carries a
            bearer token). Empty unless ``open_mode`` is ``RELAY``.
        relay_token: The bearer token for the relay (a SECRET — from env/1Password,
            never committed, never logged; excluded from :func:`repr`).
        relay_method: The HTTP method for the relay call (default ``POST``).
        relay_timeout_s: The relay request timeout in seconds.
    """

    open_mode: IntercomOpenMode
    dtmf_digits: str = ""
    relay_url: str = ""
    # ``repr=False``: the token is a secret — keep it out of any repr/log line.
    relay_token: str = field(default="", repr=False)
    relay_method: str = _DEFAULT_RELAY_METHOD
    relay_timeout_s: float = _DEFAULT_RELAY_TIMEOUT_S


class IntercomRelayError(RuntimeError):
    """The external relay call failed (network error or a non-2xx response).

    Carries only the structural failure (status / reason) for diagnostics — never
    the bearer token or the full URL query (which could embed a secret).
    """


class IntercomRelayClient:
    """Opens the entry by calling an external relay endpoint (ADR-0031).

    A thin, dependency-free HTTP client: it issues one request (default ``POST``)
    to :attr:`IntercomConfig.relay_url` with a ``Authorization: Bearer <token>``
    header when a token is configured, off the event loop (``asyncio.to_thread``)
    so the blocking ``urllib`` call never stalls the media/asyncio loop. A non-2xx
    response or a network error raises :class:`IntercomRelayError` (so the
    ``open_entry`` tool reports a clear failure — the door was NOT opened).
    """

    def __init__(self, config: IntercomConfig) -> None:
        """Bind the client to its relay configuration."""
        self._config = config

    async def open(self) -> None:
        """Call the relay to open the entry; raise on failure (never silent).

        Raises:
            IntercomRelayError: On a network error, a non-2xx HTTP response, or
                an invalid-value error (e.g. a control char in a header that
                bypasses the load-time rejection).
        """
        try:
            await asyncio.to_thread(self._open_blocking)
        except IntercomRelayError:
            raise
        except ValueError as exc:
            # Defense-in-depth: urllib/http.client raises ValueError for invalid
            # header values (e.g. HTTP header injection via a control char that
            # slips past load-time validation). The ValueError's text embeds the
            # OFFENDING HEADER VALUE — for the relay path that is "Bearer <token>",
            # so it MUST NOT be interpolated into the message (it would leak the
            # bearer token into logs/callers). Use a fixed message; the original is
            # chained via `from exc` so the cause survives in the traceback only.
            msg = "intercom relay request failed: invalid request value"
            raise IntercomRelayError(msg) from exc

    def _open_blocking(self) -> None:
        """The blocking relay request (run in a worker thread)."""
        cfg = self._config
        headers = {"Content-Type": "application/json"}
        if cfg.relay_token:
            headers["Authorization"] = f"Bearer {cfg.relay_token}"
        # A small JSON body identifies the action without leaking PII.
        body = b'{"action":"open"}'
        request = urllib.request.Request(  # noqa: S310 — URL is operator-configured + https-validated at load
            cfg.relay_url,
            data=body,
            headers=headers,
            method=cfg.relay_method,
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 — see above; https enforced by load_intercom_config
                request, timeout=cfg.relay_timeout_s
            ) as response:
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            # A non-2xx response: report the status only (no token, no body).
            msg = f"intercom relay returned HTTP {exc.code}"
            raise IntercomRelayError(msg) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Network failure: report the reason class, never the URL/token.
            msg = f"intercom relay request failed: {type(exc).__name__}"
            raise IntercomRelayError(msg) from exc
        except ValueError as exc:
            # Defense-in-depth: urllib/http.client raises ValueError for invalid
            # header values (e.g. HTTP header injection via a control char). This
            # bypasses the except chain above; wrap it so the documented
            # IntercomRelayError contract always holds, even if a bad value reaches
            # the network call despite the load-time rejection. The ValueError's
            # text embeds the OFFENDING HEADER VALUE (the "Bearer <token>" header),
            # so it MUST NOT be interpolated — that would leak the token. Use a
            # fixed message; the cause is preserved via `from exc` for the traceback.
            msg = "intercom relay request failed: invalid request value"
            raise IntercomRelayError(msg) from exc
        if not 200 <= status < 300:  # noqa: PLR2004 — HTTP 2xx success band
            msg = f"intercom relay returned HTTP {status}"
            raise IntercomRelayError(msg)
        _log.info("intercom relay: entry opened (HTTP %d)", status)


def load_intercom_config(env: Mapping[str, str]) -> IntercomConfig:
    """Parse the intercom env scheme into an immutable :class:`IntercomConfig`.

    Default (no ``HERMES_VOIP_INTERCOM_OPEN_MODE``) ⇒ ``DISABLED``: the entry
    cannot be opened until the operator explicitly configures an actuation path
    (fail-safe — a misconfiguration never silently opens or no-ops a door).

    Args:
        env: The environment mapping to read the intercom knobs from.

    Returns:
        The parsed, validated :class:`IntercomConfig`.

    Raises:
        ConfigError: Unknown mode token; DTMF mode without (or with invalid)
            ``HERMES_VOIP_INTERCOM_DTMF``; RELAY mode without
            ``HERMES_VOIP_INTERCOM_RELAY_URL`` or with a non-https URL.
    """
    token = (env.get(_OPEN_MODE_KEY) or "").strip().lower()
    if not token:
        return IntercomConfig(open_mode=IntercomOpenMode.DISABLED)

    by_token = {m.value: m for m in IntercomOpenMode}
    mode = by_token.get(token)
    if mode is None:
        opts = ", ".join(sorted(by_token))
        msg = f"{_OPEN_MODE_KEY} must be one of {{{opts}}}, got {token!r}"
        raise ConfigError(msg)

    if mode is IntercomOpenMode.DISABLED:
        return IntercomConfig(open_mode=IntercomOpenMode.DISABLED)

    if mode is IntercomOpenMode.DTMF:
        return _load_dtmf_config(env)

    return _load_relay_config(env)


def _load_dtmf_config(env: Mapping[str, str]) -> IntercomConfig:
    """Build a DTMF-mode config; validate the open code is real DTMF (fail loud)."""
    digits = (env.get(_DTMF_KEY) or "").strip()
    if not digits:
        msg = (
            f"{_OPEN_MODE_KEY}=dtmf requires {_DTMF_KEY} (the DTMF open code to send, "
            'e.g. "9")'
        )
        raise ConfigError(msg)
    # Validate every character is a real DTMF digit — a typo must fail at load, not
    # send garbage at door-open time. The open code is SENSITIVE (a door code), so the
    # error must NOT echo it: config errors are commonly logged, and the value would
    # leak. Report only the offending character POSITION, never the code.
    for i, ch in enumerate(digits):
        try:
            digit_to_event(ch)
        except ValueError as exc:
            msg = (
                f"{_DTMF_KEY} is not a valid DTMF code: character at position {i} is "
                "not a DTMF digit (allowed characters: 0-9, *, #, A-D). "
                "(The code itself is not shown — it is sensitive.)"
            )
            raise ConfigError(msg) from exc
    return IntercomConfig(open_mode=IntercomOpenMode.DTMF, dtmf_digits=digits)


def _load_relay_config(env: Mapping[str, str]) -> IntercomConfig:
    """Build a RELAY-mode config; require an https URL (the token rides on it)."""
    url = (env.get(_RELAY_URL_KEY) or "").strip()
    if not url:
        msg = (
            f"{_OPEN_MODE_KEY}=relay requires {_RELAY_URL_KEY} (the relay endpoint "
            "to POST to)"
        )
        raise ConfigError(msg)
    if not url.lower().startswith("https://"):
        # The relay carries a bearer token; an http:// URL would leak it in
        # cleartext on the wire. Require https.
        msg = (
            f"{_RELAY_URL_KEY} must be an https:// URL (it carries a bearer token "
            "that must not travel in cleartext)"
        )
        raise ConfigError(msg)
    raw_token = env.get(_RELAY_TOKEN_KEY) or ""
    # Validate the RAW value for control characters BEFORE trimming. ``.strip()``
    # removes leading/trailing CR/LF, so stripping first would silently accept a
    # token with a stray newline (a malformed secret) instead of rejecting it. An
    # embedded control char causes urllib to raise a bare ValueError (HTTP header
    # injection) that bypasses the except chain in _open_blocking, violating the
    # IntercomRelayError contract — so reject at load, on the untrimmed value.
    _reject_control_chars(raw_token, _RELAY_TOKEN_KEY)
    token = raw_token.strip()
    if not token:
        # Physical-access actuation with NO bearer token: the relay is unauthenticated
        # unless the endpoint enforces auth another way (mTLS / IP allowlist / a secret
        # in the path). We do NOT hard-fail (a deliberately network-isolated relay is
        # legitimate), but warn LOUDLY so an accidentally open door-opener is visible.
        _log.warning(
            "%s is unset: the intercom relay will be called WITHOUT an Authorization "
            "bearer token. Ensure the relay endpoint enforces authentication another "
            "way (mTLS / IP allowlist), or set %s (from 1Password).",
            _RELAY_TOKEN_KEY,
            _RELAY_TOKEN_KEY,
        )
    method = (env.get(_RELAY_METHOD_KEY) or "").strip().upper() or _DEFAULT_RELAY_METHOD
    if method not in _ALLOWED_RELAY_METHODS:
        opts = ", ".join(_ALLOWED_RELAY_METHODS)
        msg = f"{_RELAY_METHOD_KEY} must be one of {{{opts}}}, got {method!r}"
        raise ConfigError(msg)
    timeout = _parse_timeout(env)
    return IntercomConfig(
        open_mode=IntercomOpenMode.RELAY,
        relay_url=url,
        relay_token=token,
        relay_method=method,
        relay_timeout_s=timeout,
    )


def _parse_timeout(env: Mapping[str, str]) -> float:
    """Parse ``HERMES_VOIP_INTERCOM_RELAY_TIMEOUT_S``; default 5.0s, must be > 0."""
    raw = (env.get(_RELAY_TIMEOUT_KEY) or "").strip()
    if not raw:
        return _DEFAULT_RELAY_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{_RELAY_TIMEOUT_KEY} must be a number, got {raw!r}"
        raise ConfigError(msg) from exc
    if value <= 0:
        msg = f"{_RELAY_TIMEOUT_KEY} must be positive, got {value}"
        raise ConfigError(msg)
    return value
