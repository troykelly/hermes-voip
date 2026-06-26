"""Multiple intercoms, each with NAMED openings (ADR-0045, issue #38).

ADR-0031 shipped a SINGLE intercom with a SINGLE actuation path (one DTMF code or
one relay). This module generalises that to **multiple intercom caller-IDs**, each
with a **named set of openings** (e.g. ``door`` / ``gate`` / ``garage``). Each
opening actuates via EITHER:

* **DTMF** — send a configured DTMF code on the live call (the door-phone's own open
  code); or
* **WEBHOOK** — issue an HTTP request (``GET`` / ``POST`` / ``PUT``) to an
  operator-configured URL with operator-settable headers and body (a smart-lock
  bridge / home-automation endpoint).

Config is a JSON document referenced by ``HERMES_VOIP_INTERCOM_CONFIG_FILE`` mapping
each intercom's caller-ID to its openings::

    {
      "intercoms": {
        "1000": {
          "openings": {
            "door": {"type": "dtmf", "dtmf_code": "9"},
            "gate": {"type": "webhook", "method": "POST",
                     "url": "https://lock.example.test/gate",
                     "headers": {"Authorization": "Bearer TOKEN"},
                     "body": "open=true"}
          }
        }
      }
    }

SECURITY POSTURE (ADR-0045):

* A webhook ``url`` / ``headers`` / ``body`` and the DTMF ``dtmf_code`` may carry
  secrets (a bearer token, a door code). They are ``repr=False`` on
  :class:`Opening` so they never reach a log line / repr; only the opening NAME and
  TYPE are loggable. Errors carry the failure class / HTTP status only — never the
  url/headers/body.
* Only the calling intercom's opening NAMES are surfaced to the agent (never the
  codes / urls). ``open_entry(name)`` is **scoped** to the calling intercom's set:
  the adapter rejects a name not in that set, and a non-intercom caller cannot open
  anything (ADR-0031's least-privilege spine carries over).
* Validation is **fail-loud** (rule 37): a typo'd type / a DTMF code that is not real
  DTMF / a non-https webhook URL / a bad method / an intercom with no openings all
  raise :class:`~hermes_voip.config.ConfigError` at load, never at door-open time.

The webhook call uses the standard library (``urllib``) off the event loop — a single
rare request needs no third-party HTTP dependency (AGENTS.md rule 40).

Back-compat: the ADR-0031 single-intercom env scheme (``HERMES_VOIP_INTERCOM_*``)
is UNCHANGED and still loaded by :mod:`hermes_voip.intercom`. This module is the
*additional*, opt-in multi-intercom path: when ``HERMES_VOIP_INTERCOM_CONFIG_FILE``
is unset the config is empty and the legacy single path applies.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import Message as HTTPMessage
from enum import Enum
from pathlib import Path
from typing import IO

from hermes_voip.config import ConfigError
from hermes_voip.dtmf import digit_to_event

__all__ = [
    "IntercomEntry",
    "MultiIntercomConfig",
    "Opening",
    "OpeningType",
    "WebhookError",
    "fire_webhook_opening",
    "load_multi_intercom_config",
]

_log = logging.getLogger(__name__)


# ASCII C0 control-char boundary: ord < _C0_LIMIT means a control character
# (CR=0x0D, LF=0x0A, NUL=0x00, …). http.client raises bare ValueError for these
# in header names/values, bypassing the typed exception chain.
_C0_LIMIT = 0x20


def _reject_control_chars(value: str, label: str) -> None:
    r"""Raise ConfigError if ``value`` contains any ASCII C0 control character.

    C0 controls (U+0000-U+001F) include CR (\r), LF (\n), NUL (\x00), and other
    bytes that cause HTTP header-injection or are rejected by ``http.client`` with a
    bare ``ValueError`` that bypasses the ``(HTTPError, URLError, TimeoutError,
    OSError)`` catch in ``_fire_webhook_blocking``.  Reject at load so the error is
    deterministic and the contract (WebhookError from ``fire_webhook_opening()``)
    holds.
    """
    for ch in value:
        if ord(ch) < _C0_LIMIT:
            msg = (
                f"{label} must not contain ASCII control characters "
                f"(found {ch!r} - rejecting to prevent HTTP header injection)"
            )
            raise ConfigError(msg)


#: The env key naming the JSON config file (opt-in; a gitignored path — the document
#: holds caller-IDs + possibly secrets, so the PATH is in env and the FILE is
#: gitignored, never a tracked file).
_CONFIG_FILE_KEY = "HERMES_VOIP_INTERCOM_CONFIG_FILE"

_DEFAULT_WEBHOOK_METHOD = "POST"
_DEFAULT_WEBHOOK_TIMEOUT_S = 5.0
_ALLOWED_WEBHOOK_METHODS = ("GET", "POST", "PUT")


class OpeningType(Enum):
    """How a named opening actuates (ADR-0045)."""

    DTMF = "dtmf"  # send a DTMF code on the live call
    WEBHOOK = "webhook"  # issue an HTTP request to an operator-configured endpoint


@dataclass(frozen=True, slots=True)
class Opening:
    """One named opening for an intercom (e.g. ``door`` / ``gate`` — ADR-0045).

    The NAME and TYPE are non-secret and loggable; the actuation details
    (``dtmf_code`` for DTMF; ``url`` / ``headers`` / ``body`` for WEBHOOK) may carry
    secrets and are therefore ``repr=False`` — they never appear in a repr / log line.

    Attributes:
        name: The opening's name (the agent-facing label, never secret).
        type: The actuation type (DTMF or WEBHOOK).
        dtmf_code: The DTMF code to send in DTMF mode (SECRET — a door code; empty for
            a webhook opening).
        method: The HTTP method for a webhook opening (``GET`` / ``POST`` / ``PUT``).
        url: The webhook endpoint URL (https only — it may carry a token; SECRET).
        headers: The webhook request headers (may carry an Authorization token;
            SECRET).
        body: The webhook request body (may carry a secret payload; SECRET).
        timeout_s: The webhook request timeout in seconds.
    """

    name: str
    type: OpeningType
    # ``repr=False`` on every actuation field: a door code / url / token / payload is
    # a secret and must never reach a repr or log line.
    dtmf_code: str = field(default="", repr=False)
    method: str = field(default=_DEFAULT_WEBHOOK_METHOD)
    url: str = field(default="", repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: str = field(default="", repr=False)
    timeout_s: float = _DEFAULT_WEBHOOK_TIMEOUT_S


@dataclass(frozen=True, slots=True)
class IntercomEntry:
    """One intercom caller-ID and its named opening set (ADR-0045).

    Attributes:
        caller_id: The intercom's caller-ID match value (exact or ``*``-suffixed
            literal prefix, mirroring the ADR-0020 caller-list match semantics). It is
            a forgeable SIP identifier — never an auth boundary; the per-opening
            secret + the scoping are the protection.
        openings: The named openings (``{name: Opening}``), at least one.
    """

    caller_id: str
    openings: Mapping[str, Opening]

    def opening_names(self) -> tuple[str, ...]:
        """The opening NAMES (non-secret) — the surface shown to the agent."""
        return tuple(self.openings)


@dataclass(frozen=True, slots=True)
class MultiIntercomConfig:
    """The loaded multi-intercom config: every intercom + its openings (ADR-0045).

    Empty (no ``HERMES_VOIP_INTERCOM_CONFIG_FILE``) means the multi-intercom feature
    is off and the legacy single-intercom path (ADR-0031) applies.
    """

    entries: tuple[IntercomEntry, ...]

    def match(self, caller_id: str) -> IntercomEntry | None:
        """Return the intercom entry whose caller-ID matches ``caller_id``, else None.

        First-match-wins over ``entries`` (declaration order). A ``*``-suffixed entry
        caller-ID is a literal prefix match (``startswith`` — no regex, no ReDoS); any
        other value is exact. Pure; runs once per call at setup, never on the media
        path. Caller-ID is forgeable — this is routing, never authentication.
        """
        candidate = caller_id.strip()
        for entry in self.entries:
            pattern = entry.caller_id
            if pattern.endswith("*"):
                if candidate.startswith(pattern[:-1]):
                    return entry
            elif candidate == pattern:
                return entry
        return None


class WebhookError(RuntimeError):
    """A webhook opening request failed (network error or a non-2xx response).

    Carries only the structural failure (HTTP status / failure class) — never the
    url / headers / body (which may embed a secret).
    """


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect handler that REFUSES every 3xx instead of following it.

    ``urllib.request.urlopen`` follows redirects by default. A webhook ``url`` is
    validated as ``https://`` at load, but a redirect ``Location`` (e.g. a 302 to an
    ``http://`` URL) would make urllib re-issue the request — Authorization header
    and body included — to the redirect target IN CLEARTEXT, silently defeating the
    https-only guarantee (ADR-0045). We refuse the redirect: the operator endpoint
    must answer the configured https URL directly, never bounce it elsewhere.
    """

    def redirect_request(  # noqa: PLR0913 — base-class signature; all args required
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,  # noqa: ARG002 — base-class signature; reason text is fixed here
        headers: HTTPMessage,
        newurl: str,  # noqa: ARG002 — base-class signature; the target is never used
    ) -> None:
        # Returning a request would follow the redirect; raising refuses it. The
        # error carries the status only — never the (secret-bearing) target URL.
        raise urllib.error.HTTPError(
            req.full_url, code, f"refusing to follow redirect ({code})", headers, fp
        )


#: A shared opener that refuses redirects (see :class:`_NoRedirectHandler`). It is
#: stateless and thread-safe to reuse across the worker-thread webhook calls.
_WEBHOOK_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def load_multi_intercom_config(env: Mapping[str, str]) -> MultiIntercomConfig:
    """Parse the multi-intercom JSON document into a :class:`MultiIntercomConfig`.

    Unset ``HERMES_VOIP_INTERCOM_CONFIG_FILE`` ⇒ an empty config (the feature is off;
    the ADR-0031 single-intercom path applies). A configured-but-missing path raises
    (rule 37 — a typo must fail loudly, never silently disable the door openers).

    Args:
        env: The environment mapping to read the config-file path from.

    Returns:
        The parsed, validated :class:`MultiIntercomConfig`.

    Raises:
        ConfigError: missing/unreadable/invalid file, a non-object document, an
            intercom with no openings, an unknown opening type, a DTMF opening without
            a valid code, or a webhook opening without an https URL / with a bad
            method.
    """
    path_str = (env.get(_CONFIG_FILE_KEY) or "").strip()
    if not path_str:
        return MultiIntercomConfig(entries=())

    path = Path(path_str)
    if not path.exists():
        msg = (
            f"{_CONFIG_FILE_KEY}: intercom config file {path_str!r} is configured but "
            "does not exist (unset the variable to run without multi-intercom openings)"
        )
        raise ConfigError(msg)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"{_CONFIG_FILE_KEY}: cannot read intercom config file: {exc}"
        raise ConfigError(msg) from exc
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        msg = f"{_CONFIG_FILE_KEY}: intercom config file is not valid JSON: {exc}"
        raise ConfigError(msg) from exc

    return _parse_document(data)


def _parse_document(data: object) -> MultiIntercomConfig:
    """Validate the ``{"intercoms": {caller-id: {...}}}`` document (fail-loud)."""
    if not isinstance(data, dict):
        msg = f"{_CONFIG_FILE_KEY}: intercom config must be a JSON object"
        raise ConfigError(msg)
    intercoms = data.get("intercoms")
    if not isinstance(intercoms, dict):
        msg = (
            f"{_CONFIG_FILE_KEY}: the config object must have an 'intercoms' object "
            "mapping each intercom caller-ID to its openings"
        )
        raise ConfigError(msg)

    entries: list[IntercomEntry] = []
    for caller_id, raw_entry in intercoms.items():
        entries.append(_parse_entry(str(caller_id), raw_entry))

    # PII-safe summary: count of intercoms + total openings only, never caller-IDs /
    # opening details.
    total_openings = sum(len(e.openings) for e in entries)
    _log.info(
        "multi-intercom config loaded: %d intercom(s), %d opening(s) total",
        len(entries),
        total_openings,
    )
    return MultiIntercomConfig(entries=tuple(entries))


def _parse_entry(caller_id: str, raw_entry: object) -> IntercomEntry:
    """Validate one intercom entry: a non-empty named opening set (fail-loud)."""
    if not isinstance(raw_entry, dict):
        msg = (
            f"{_CONFIG_FILE_KEY}: each intercom entry must be a JSON object with an "
            "'openings' object"
        )
        raise ConfigError(msg)
    raw_openings = raw_entry.get("openings")
    if not isinstance(raw_openings, dict) or not raw_openings:
        msg = (
            f"{_CONFIG_FILE_KEY}: intercom must define a non-empty 'openings' object "
            "(at least one named opening)"
        )
        raise ConfigError(msg)
    openings: dict[str, Opening] = {}
    for name, raw_opening in raw_openings.items():
        openings[str(name)] = _parse_opening(str(name), raw_opening)
    return IntercomEntry(caller_id=caller_id.strip(), openings=openings)


def _parse_opening(name: str, raw_opening: object) -> Opening:
    """Validate one named opening (DTMF or webhook) — fail-loud (rule 37)."""
    if not isinstance(raw_opening, dict):
        msg = f"{_CONFIG_FILE_KEY}: opening {name!r} must be a JSON object"
        raise ConfigError(msg)
    type_token = str(raw_opening.get("type", "")).strip().lower()
    by_token = {t.value: t for t in OpeningType}
    opening_type = by_token.get(type_token)
    if opening_type is None:
        opts = ", ".join(sorted(by_token))
        msg = (
            f"{_CONFIG_FILE_KEY}: opening {name!r} has type {type_token!r}; must be "
            f"one of {{{opts}}}"
        )
        raise ConfigError(msg)
    if opening_type is OpeningType.DTMF:
        return _parse_dtmf_opening(name, raw_opening)
    return _parse_webhook_opening(name, raw_opening)


def _parse_dtmf_opening(name: str, raw_opening: Mapping[str, object]) -> Opening:
    """Build a DTMF opening; validate the code is real DTMF (without echoing it)."""
    code = str(raw_opening.get("dtmf_code", "")).strip()
    if not code:
        msg = (
            f"{_CONFIG_FILE_KEY}: DTMF opening {name!r} requires a 'dtmf_code' (the "
            "open code to send)"
        )
        raise ConfigError(msg)
    # Validate every character is real DTMF — a typo must fail at load, not send
    # garbage at door-open time. The code is SENSITIVE (a door code), so the error
    # reports only the offending position, never the code itself.
    for i, ch in enumerate(code):
        try:
            digit_to_event(ch)
        except ValueError as exc:
            msg = (
                f"{_CONFIG_FILE_KEY}: DTMF opening {name!r} has an invalid code: the "
                f"character at position {i} is not a DTMF digit (allowed: 0-9, *, #, "
                "A-D). (The code itself is not shown — it is sensitive.)"
            )
            raise ConfigError(msg) from exc
    return Opening(name=name, type=OpeningType.DTMF, dtmf_code=code)


def _parse_webhook_opening(name: str, raw_opening: Mapping[str, object]) -> Opening:
    """Build a webhook opening; require an https URL + a valid method (fail-loud)."""
    url = str(raw_opening.get("url", "")).strip()
    if not url:
        msg = (
            f"{_CONFIG_FILE_KEY}: webhook opening {name!r} requires a 'url' (the "
            "endpoint to call)"
        )
        raise ConfigError(msg)
    if not url.lower().startswith("https://"):
        # A webhook may carry a bearer token / secret payload; an http:// URL would
        # leak it in cleartext on the wire. Require https.
        msg = (
            f"{_CONFIG_FILE_KEY}: webhook opening {name!r} 'url' must be an https:// "
            "URL (it may carry a token that must not travel in cleartext)"
        )
        raise ConfigError(msg)
    method = (
        str(raw_opening.get("method", "")).strip().upper() or _DEFAULT_WEBHOOK_METHOD
    )
    if method not in _ALLOWED_WEBHOOK_METHODS:
        opts = ", ".join(_ALLOWED_WEBHOOK_METHODS)
        msg = (
            f"{_CONFIG_FILE_KEY}: webhook opening {name!r} 'method' must be one of "
            f"{{{opts}}}, got {method!r}"
        )
        raise ConfigError(msg)
    headers = _parse_headers(name, raw_opening.get("headers"))
    body = _parse_body(name, raw_opening.get("body"))
    if method == "GET" and body:
        # A GET carries no body on the wire. Silently dropping a configured body
        # would mask an operator misconfiguration, so reject it loud at load
        # (rule 37) rather than discarding it at door-open time.
        msg = (
            f"{_CONFIG_FILE_KEY}: webhook opening {name!r} uses method GET but has a "
            "'body'; a GET sends no body — drop the body or use POST/PUT"
        )
        raise ConfigError(msg)
    timeout = _parse_timeout(name, raw_opening.get("timeout_s"))
    return Opening(
        name=name,
        type=OpeningType.WEBHOOK,
        method=method,
        url=url,
        headers=headers,
        body=body,
        timeout_s=timeout,
    )


def _parse_headers(name: str, raw: object) -> Mapping[str, str]:
    """Validate the optional ``headers`` object (a string→string map).

    Rejects any header name or value that contains an ASCII C0 control character
    (CR, LF, NUL, …). ``http.client`` raises a bare ``ValueError`` for such values
    at request-send time; that ``ValueError`` bypasses the existing except chain in
    ``_fire_webhook_blocking`` and violates the ``WebhookError`` contract. Reject at
    load so the failure is deterministic.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        msg = (
            f"{_CONFIG_FILE_KEY}: webhook opening {name!r} 'headers' must be an object"
        )
        raise ConfigError(msg)
    headers: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            msg = (
                f"{_CONFIG_FILE_KEY}: webhook opening {name!r} header {key!r} must "
                "be a string value"
            )
            raise ConfigError(msg)
        # Reject control chars in both name and value.
        _reject_control_chars(
            str(key),
            f"{_CONFIG_FILE_KEY}: webhook opening {name!r} header name {key!r}",
        )
        _reject_control_chars(
            value,
            f"{_CONFIG_FILE_KEY}: webhook opening {name!r} header {key!r} value",
        )
        headers[str(key)] = value
    return headers


def _parse_body(name: str, raw: object) -> str:
    """Validate the optional ``body`` (a string)."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        msg = (
            f"{_CONFIG_FILE_KEY}: webhook opening {name!r} 'body' must be a string "
            "(serialise structured payloads to a JSON string)"
        )
        raise ConfigError(msg)
    return raw


def _parse_timeout(name: str, raw: object) -> float:
    """Validate the optional ``timeout_s`` (a positive number); default 5.0s."""
    if raw is None:
        return _DEFAULT_WEBHOOK_TIMEOUT_S
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        msg = (
            f"{_CONFIG_FILE_KEY}: webhook opening {name!r} 'timeout_s' must be a number"
        )
        raise ConfigError(msg)
    value = float(raw)
    if value <= 0:
        msg = (
            f"{_CONFIG_FILE_KEY}: webhook opening {name!r} 'timeout_s' must be "
            f"positive, got {value}"
        )
        raise ConfigError(msg)
    return value


async def fire_webhook_opening(opening: Opening) -> None:
    """Actuate a WEBHOOK opening by issuing its configured HTTP request (ADR-0045).

    Runs the blocking ``urllib`` request off the event loop (``asyncio.to_thread``) so
    it never stalls the media/asyncio loop. A non-2xx response or a network error
    raises :class:`WebhookError` (so ``open_entry`` reports a clear failure — the entry
    was NOT opened). The url / headers / body are never logged (they may carry
    secrets); only the opening name + HTTP status / failure class are.

    Raises:
        WebhookError: on a network error, a non-2xx HTTP response, or an
            invalid-value error (e.g. a control char in a header that bypasses
            the load-time rejection).
        ValueError: if ``opening`` is not a WEBHOOK opening (a programming error).
    """
    import asyncio  # noqa: PLC0415 — local import keeps the module import-light

    if opening.type is not OpeningType.WEBHOOK:
        msg = f"fire_webhook_opening called for a non-webhook opening {opening.name!r}"
        raise ValueError(msg)
    try:
        await asyncio.to_thread(_fire_webhook_blocking, opening)
    except WebhookError:
        raise
    except ValueError as exc:
        # Defense-in-depth: http.client raises ValueError for invalid header names
        # or values (e.g. HTTP header injection via a control char that slips past
        # load-time validation). Wrap so the documented WebhookError contract holds
        # from the caller's perspective.
        msg = f"webhook opening {opening.name!r} request failed: invalid value ({exc})"
        raise WebhookError(msg) from exc


def _fire_webhook_blocking(opening: Opening) -> None:
    """The blocking webhook request (run in a worker thread)."""
    # A GET carries no body; POST/PUT send the configured body bytes.
    data = (
        opening.body.encode("utf-8")
        if opening.body and opening.method != "GET"
        else None
    )
    request = urllib.request.Request(  # noqa: S310 — URL is operator-configured + https-validated at load
        opening.url,
        data=data,
        headers=dict(opening.headers),
        method=opening.method,
    )
    try:
        # Use the no-redirect opener: a 3xx (e.g. an https->http downgrade) is
        # REFUSED, never followed (it would re-send the token/body in cleartext —
        # ADR-0045). The url is operator-configured + https-validated at load.
        with _WEBHOOK_OPENER.open(request, timeout=opening.timeout_s) as response:
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        # A non-2xx response (including a refused redirect): report the status only
        # (no url, no headers, no body).
        msg = f"webhook opening {opening.name!r} returned HTTP {exc.code}"
        raise WebhookError(msg) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Network failure: report the reason class, never the url/headers/body.
        msg = f"webhook opening {opening.name!r} request failed: {type(exc).__name__}"
        raise WebhookError(msg) from exc
    except ValueError as exc:
        # Defense-in-depth: http.client raises ValueError for invalid header names
        # or values (e.g. HTTP header injection via a control char). This bypasses
        # the except chain above; wrap it so the documented WebhookError contract
        # always holds, even if a bad value reaches the network call despite the
        # load-time rejection.
        msg = f"webhook opening {opening.name!r} request failed: invalid value ({exc})"
        raise WebhookError(msg) from exc
    if not 200 <= status < 300:  # noqa: PLR2004 — HTTP 2xx success band
        msg = f"webhook opening {opening.name!r} returned HTTP {status}"
        raise WebhookError(msg)
    _log.info("webhook opening %r: entry opened (HTTP %d)", opening.name, status)
