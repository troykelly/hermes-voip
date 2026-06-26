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
