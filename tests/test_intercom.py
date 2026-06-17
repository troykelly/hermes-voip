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

from collections.abc import Mapping

import pytest

from hermes_voip.config import ConfigError
from hermes_voip.intercom import (
    IntercomConfig,
    IntercomOpenMode,
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
