"""Tests for the intercom entry actuation config + relay client (ADR-0031).

The intercom caller mode opens an entry (door/gate) for a legitimate expected
visitor via ONE of two actuation paths, chosen by config:

* ``dtmf`` — send a configured DTMF string on the live call (e.g. the gateway's
  door-open code "9"); and
* ``relay`` — POST to an external relay/HTTP endpoint (URL + bearer token from the
  environment / 1Password — NEVER committed).

The default mode is ``disabled``: ``open_entry`` then fails LOUD (no accidental or
silent door opening). PUBLIC repo: fakes only (``relay.example.test``).
"""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Mapping
from unittest.mock import patch

import pytest

from hermes_voip.config import ConfigError
from hermes_voip.intercom import (
    IntercomConfig,
    IntercomOpenMode,
    IntercomRelayClient,
    IntercomRelayError,
    load_intercom_config,
)

_FAKE_RELAY_URL = "https://relay.example.test/open"
_FAKE_TOKEN = "fake-relay-token-0000"  # obvious fake for tests


def _env(**kw: str) -> Mapping[str, str]:
    return dict(kw)


# --- default: disabled --------------------------------------------------------


def test_default_is_disabled() -> None:
    cfg = load_intercom_config(_env())
    assert cfg.open_mode is IntercomOpenMode.DISABLED


def test_disabled_config_has_no_actuation() -> None:
    cfg = IntercomConfig(open_mode=IntercomOpenMode.DISABLED)
    assert cfg.open_mode is IntercomOpenMode.DISABLED
    assert cfg.dtmf_digits == ""
    assert cfg.relay_url == ""


# --- dtmf mode ----------------------------------------------------------------


def test_dtmf_mode_requires_digits() -> None:
    with pytest.raises(ConfigError, match="HERMES_VOIP_INTERCOM_DTMF"):
        load_intercom_config(_env(HERMES_VOIP_INTERCOM_OPEN_MODE="dtmf"))


def test_dtmf_mode_loads_digits() -> None:
    cfg = load_intercom_config(
        _env(
            HERMES_VOIP_INTERCOM_OPEN_MODE="dtmf",
            HERMES_VOIP_INTERCOM_DTMF="9",
        )
    )
    assert cfg.open_mode is IntercomOpenMode.DTMF
    assert cfg.dtmf_digits == "9"


def test_dtmf_mode_rejects_non_dtmf_digits() -> None:
    # The configured open code must be valid DTMF — a typo must fail loud, not send
    # garbage at door-open time.
    with pytest.raises(ConfigError):
        load_intercom_config(
            _env(
                HERMES_VOIP_INTERCOM_OPEN_MODE="dtmf",
                HERMES_VOIP_INTERCOM_DTMF="not-dtmf",
            )
        )


# --- relay mode ---------------------------------------------------------------


def test_relay_mode_requires_url() -> None:
    with pytest.raises(ConfigError, match="HERMES_VOIP_INTERCOM_RELAY_URL"):
        load_intercom_config(_env(HERMES_VOIP_INTERCOM_OPEN_MODE="relay"))


def test_relay_mode_loads_url_and_token() -> None:
    cfg = load_intercom_config(
        _env(
            HERMES_VOIP_INTERCOM_OPEN_MODE="relay",
            HERMES_VOIP_INTERCOM_RELAY_URL=_FAKE_RELAY_URL,
            HERMES_VOIP_INTERCOM_RELAY_TOKEN=_FAKE_TOKEN,
        )
    )
    assert cfg.open_mode is IntercomOpenMode.RELAY
    assert cfg.relay_url == _FAKE_RELAY_URL
    assert cfg.relay_token == _FAKE_TOKEN


def test_relay_mode_rejects_non_https_url() -> None:
    # The relay carries a bearer token; an http:// URL would leak it in cleartext.
    with pytest.raises(ConfigError, match="https"):
        load_intercom_config(
            _env(
                HERMES_VOIP_INTERCOM_OPEN_MODE="relay",
                HERMES_VOIP_INTERCOM_RELAY_URL="http://relay.example.test/open",
            )
        )


# --- bad mode token -----------------------------------------------------------


def test_unknown_mode_token_is_rejected() -> None:
    with pytest.raises(ConfigError, match="HERMES_VOIP_INTERCOM_OPEN_MODE"):
        load_intercom_config(_env(HERMES_VOIP_INTERCOM_OPEN_MODE="banana"))


# --- the token is never stringified into the repr (no accidental log leak) -----


def test_repr_does_not_leak_the_relay_token() -> None:
    cfg = IntercomConfig(
        open_mode=IntercomOpenMode.RELAY,
        relay_url=_FAKE_RELAY_URL,
        relay_token=_FAKE_TOKEN,
    )
    assert _FAKE_TOKEN not in repr(cfg)


# --- control-char rejection at config load (CRLF/NUL hardening) ---------------


def test_relay_token_with_crlf_rejected_at_load() -> None:
    """A relay token containing CR+LF is rejected at config load (ConfigError).

    An embedded CRLF in an HTTP header value causes urllib to raise a bare
    ValueError (HTTP header injection) — which bypasses the existing
    (HTTPError, URLError, TimeoutError, OSError) catch in _open_blocking and
    violates the IntercomRelayError contract. Reject at load before it can reach
    the network call.
    """
    with pytest.raises(ConfigError, match="control"):
        load_intercom_config(
            _env(
                HERMES_VOIP_INTERCOM_OPEN_MODE="relay",
                HERMES_VOIP_INTERCOM_RELAY_URL=_FAKE_RELAY_URL,
                HERMES_VOIP_INTERCOM_RELAY_TOKEN="tok\r\nen:bad",
            )
        )


def test_relay_token_with_nul_rejected_at_load() -> None:
    """A relay token containing a NUL byte is rejected at config load."""
    with pytest.raises(ConfigError, match="control"):
        load_intercom_config(
            _env(
                HERMES_VOIP_INTERCOM_OPEN_MODE="relay",
                HERMES_VOIP_INTERCOM_RELAY_URL=_FAKE_RELAY_URL,
                HERMES_VOIP_INTERCOM_RELAY_TOKEN="tok\x00en",
            )
        )


def test_relay_token_with_lone_lf_rejected_at_load() -> None:
    """A relay token containing a lone LF is rejected at config load."""
    with pytest.raises(ConfigError, match="control"):
        load_intercom_config(
            _env(
                HERMES_VOIP_INTERCOM_OPEN_MODE="relay",
                HERMES_VOIP_INTERCOM_RELAY_URL=_FAKE_RELAY_URL,
                HERMES_VOIP_INTERCOM_RELAY_TOKEN="tok\nen",
            )
        )


# --- ValueError in _open_blocking surfaces as IntercomRelayError ---------------


def test_value_error_in_open_blocking_surfaces_as_relay_error() -> None:
    """A ValueError raised inside _open_blocking is re-raised as IntercomRelayError.

    urllib raises ValueError('Invalid header value') when a header contains
    control chars. If such a value slips through to the network call (defense-in-
    depth), the existing except chain (HTTPError, URLError, TimeoutError, OSError)
    does NOT catch ValueError — it propagates uncaught, violating the documented
    IntercomRelayError contract. The fix wraps ValueError in IntercomRelayError.
    """
    cfg = IntercomConfig(
        open_mode=IntercomOpenMode.RELAY,
        relay_url=_FAKE_RELAY_URL,
        relay_token=_FAKE_TOKEN,
    )
    client = IntercomRelayClient(cfg)

    def _raise_value_error() -> None:
        raise ValueError("Invalid header value")

    with (
        patch.object(client, "_open_blocking", side_effect=_raise_value_error),
        pytest.raises(IntercomRelayError),
    ):
        asyncio.run(client.open())


# --- the wrapped ValueError MUST NOT leak the offending header value -----------


def test_relay_error_from_value_error_does_not_leak_secret() -> None:
    """The IntercomRelayError wrapping a ValueError must NOT echo the secret.

    Python's http.client puts the OFFENDING HEADER VALUE into the ValueError text
    (e.g. ``Invalid header value b'Bearer <token>...'``). For the relay path that
    value is ``Authorization: Bearer <token>`` — interpolating the raw exception
    text into the user-facing IntercomRelayError message would leak the bearer
    token into logs/callers (PUBLIC-repo invariant violation). The wrapper must
    use a FIXED, generic message carrying NO interpolated exception text; and the
    secret-bearing ValueError cause MUST be suppressed (``from None``) so it cannot
    resurface in a printed traceback (``__cause__ is None``,
    ``__suppress_context__`` True).
    """
    secret = "SECRET-fake-bearer-token-0000"  # obvious fake
    cfg = IntercomConfig(
        open_mode=IntercomOpenMode.RELAY,
        relay_url=_FAKE_RELAY_URL,
        relay_token=secret,
    )
    client = IntercomRelayClient(cfg)

    def _raise_value_error_with_secret() -> None:
        # Mirrors http.client: the whole offending header value is in the message.
        raise ValueError(f"Invalid header value b'Bearer {secret}\\r\\nX-Evil: 1'")

    with (
        patch.object(
            client, "_open_blocking", side_effect=_raise_value_error_with_secret
        ),
        pytest.raises(IntercomRelayError) as excinfo,
    ):
        asyncio.run(client.open())

    assert secret not in str(excinfo.value), (
        "IntercomRelayError message leaked the bearer token from the ValueError text"
    )
    # The secret-bearing cause is SUPPRESSED (`from None`): chaining it as __cause__
    # would re-expose the token in any printed traceback / logging.exception, so the
    # wrapper drops it (the dedicated traceback test asserts the full traceback is
    # secret-free).
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


# --- validate-before-strip: a trailing/leading CR/LF must be REJECTED ----------


def test_relay_token_with_trailing_lf_rejected_at_load() -> None:
    r"""A relay token with a TRAILING LF is rejected, not silently stripped.

    The token must be validated for control characters on its RAW value BEFORE any
    trimming. A trailing ``\n`` that ``.strip()`` would remove must still be
    rejected (an operator who pasted a token with a stray newline has a malformed
    secret — fail loud, do not silently accept a mangled token).
    """
    with pytest.raises(ConfigError, match="control"):
        load_intercom_config(
            _env(
                HERMES_VOIP_INTERCOM_OPEN_MODE="relay",
                HERMES_VOIP_INTERCOM_RELAY_URL=_FAKE_RELAY_URL,
                HERMES_VOIP_INTERCOM_RELAY_TOKEN=f"{_FAKE_TOKEN}\n",
            )
        )


def test_relay_token_with_leading_cr_rejected_at_load() -> None:
    """A relay token with a LEADING CR is rejected, not silently stripped."""
    with pytest.raises(ConfigError, match="control"):
        load_intercom_config(
            _env(
                HERMES_VOIP_INTERCOM_OPEN_MODE="relay",
                HERMES_VOIP_INTERCOM_RELAY_URL=_FAKE_RELAY_URL,
                HERMES_VOIP_INTERCOM_RELAY_TOKEN=f"\r{_FAKE_TOKEN}",
            )
        )


# --- broaden control-char rejection to DEL (0x7f) and C1 (0x80-0x9f) -----------


def test_relay_token_with_del_rejected_at_load() -> None:
    """A relay token containing DEL (0x7f) is rejected at config load.

    DEL and the C1 controls (0x80-0x9f) are not below 0x20, so a check that only
    rejects ``ord < 0x20`` lets them through. They are still header-unsafe, so the
    rejection band must include 0x7f-0x9f.
    """
    with pytest.raises(ConfigError, match="control"):
        load_intercom_config(
            _env(
                HERMES_VOIP_INTERCOM_OPEN_MODE="relay",
                HERMES_VOIP_INTERCOM_RELAY_URL=_FAKE_RELAY_URL,
                HERMES_VOIP_INTERCOM_RELAY_TOKEN="tok\x7fen",
            )
        )


def test_relay_token_with_c1_control_rejected_at_load() -> None:
    """A relay token containing a C1 control (0x85 NEL) is rejected at load."""
    with pytest.raises(ConfigError, match="control"):
        load_intercom_config(
            _env(
                HERMES_VOIP_INTERCOM_OPEN_MODE="relay",
                HERMES_VOIP_INTERCOM_RELAY_URL=_FAKE_RELAY_URL,
                HERMES_VOIP_INTERCOM_RELAY_TOKEN="tok\x85en",
            )
        )


# --- the secret must not leak via the EXCEPTION CHAIN (traceback), not just str ---


def test_relay_error_traceback_does_not_leak_secret_via_cause() -> None:
    """The full traceback of the wrapped error must NOT contain the bearer token.

    ``str(IntercomRelayError)`` is already secret-free, but chaining the original
    ValueError as ``__cause__`` (``raise ... from exc``) means a printed traceback /
    ``logging.exception`` / ``exc_info=True`` re-exposes the token, because the cause
    ValueError's text is ``Invalid header value b'Bearer <token>...'``. The fix
    suppresses the cause (``raise ... from None``) while still propagating the typed
    error. This drives the REAL http.client path so the genuine secret-bearing
    ValueError is produced as the (now-suppressed) cause.
    """
    secret = "SECRET-fake-bearer-token-0000"  # obvious fake
    # Construct a config carrying a CRLF-injected token directly (as if a bad value
    # slipped past load-time validation), so the REAL urllib/http.client raises the
    # secret-bearing ValueError inside _open_blocking.
    cfg = IntercomConfig(
        open_mode=IntercomOpenMode.RELAY,
        relay_url=_FAKE_RELAY_URL,
        relay_token=f"{secret}\r\nX-Evil: 1",
    )
    client = IntercomRelayClient(cfg)

    with pytest.raises(IntercomRelayError) as excinfo:
        asyncio.run(client.open())

    err = excinfo.value
    formatted = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    assert secret not in formatted, (
        "the bearer token leaked into the wrapped error's traceback via the "
        "exception cause chain"
    )
    # The message itself stays clean too.
    assert secret not in str(err)
