"""Tests that the DTMF config keys drive real behaviour (ADR-0010, rule-27).

Before this lane the keys ``HERMES_SIP_DTMF_MODE`` / ``HERMES_SIP_DTMF_INBAND_ENABLED``
/ ``HERMES_SIP_DTMF_INTERDIGIT_MS`` were parsed into :class:`MediaConfig` but consumed
NOWHERE — the config advertised behaviour the code lacked (rule-27 drift). This suite
locks the reconciliation:

* RFC 4733 is the shipped receive path: ``auto`` and ``rfc4733`` load.
* ``sip_info`` and ``inband`` receive are NOT implemented, so they fail LOUD at config
  load (:class:`ConfigError`) — no key value silently does nothing.
* ``resolve_dtmf_receive_mode`` maps ``(dtmf_mode, dtmf_inband_enabled, PT)`` to a
  concrete :class:`DtmfReceiveMode`, so each key changes a real, observable outcome.
"""

from __future__ import annotations

import pytest

from hermes_voip.config import ConfigError, load_media_config
from hermes_voip.dtmf_config import DtmfReceiveMode, resolve_dtmf_receive_mode


def _media_env(**overrides: str) -> dict[str, str]:
    return dict(overrides)


def test_auto_mode_loads() -> None:
    cfg = load_media_config(_media_env(HERMES_SIP_DTMF_MODE="auto"))
    assert cfg.dtmf_mode == "auto"


def test_rfc4733_mode_loads() -> None:
    cfg = load_media_config(_media_env(HERMES_SIP_DTMF_MODE="rfc4733"))
    assert cfg.dtmf_mode == "rfc4733"


def test_sip_info_mode_rejected_at_load() -> None:
    """sip_info receive is not implemented — fail loud, not a silent no-op."""
    with pytest.raises(ConfigError, match="sip_info"):
        load_media_config(_media_env(HERMES_SIP_DTMF_MODE="sip_info"))


def test_inband_mode_rejected_at_load() -> None:
    """Inband receive is not implemented — fail loud, not a silent no-op."""
    with pytest.raises(ConfigError, match="inband"):
        load_media_config(_media_env(HERMES_SIP_DTMF_MODE="inband"))


def test_unknown_mode_still_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config(_media_env(HERMES_SIP_DTMF_MODE="carrier-pigeon"))


# --- resolve_dtmf_receive_mode: each key changes the resolved outcome ---


def test_resolve_auto_with_negotiated_pt_is_rfc4733() -> None:
    """Auto + a negotiated telephone-event PT => RFC 4733 receive active."""
    cfg = load_media_config(_media_env(HERMES_SIP_DTMF_MODE="auto"))
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=101)
        is DtmfReceiveMode.RFC4733
    )


def test_resolve_auto_without_pt_is_disabled() -> None:
    """Auto + NO negotiated PT + in-band disabled => DISABLED (nothing to receive)."""
    cfg = load_media_config(
        _media_env(HERMES_SIP_DTMF_MODE="auto", HERMES_SIP_DTMF_INBAND_ENABLED="false")
    )
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None)
        is DtmfReceiveMode.DISABLED
    )


def test_resolve_auto_without_pt_inband_enabled_is_unavailable() -> None:
    """Auto + NO PT + in-band ENABLED => UNAVAILABLE.

    The operator permits the in-band last resort (``dtmf_inband_enabled`` true, the
    default), but that detector is not built, and no telephone-event PT was negotiated
    — so DTMF receive is genuinely unavailable on this call. The flag CHANGES the
    resolved mode (DISABLED vs UNAVAILABLE), so it is not inert; UNAVAILABLE is the
    loud, operator-visible signal (logged WARNING) that the configured fallback cannot
    run, distinct from a clean DISABLED.
    """
    cfg = load_media_config(
        _media_env(HERMES_SIP_DTMF_MODE="auto", HERMES_SIP_DTMF_INBAND_ENABLED="true")
    )
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None)
        is DtmfReceiveMode.UNAVAILABLE
    )


def test_resolve_rfc4733_forced_with_pt() -> None:
    cfg = load_media_config(_media_env(HERMES_SIP_DTMF_MODE="rfc4733"))
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=96)
        is DtmfReceiveMode.RFC4733
    )


def test_resolve_rfc4733_forced_without_pt_is_unavailable() -> None:
    """rfc4733 forced but the gateway negotiated no telephone-event => UNAVAILABLE.

    The operator demanded RFC 4733 but the peer did not offer telephone-event, so
    receive cannot run — surfaced as UNAVAILABLE (loud), never a silent DISABLED.
    """
    cfg = load_media_config(_media_env(HERMES_SIP_DTMF_MODE="rfc4733"))
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None)
        is DtmfReceiveMode.UNAVAILABLE
    )


def test_inband_enabled_default_true() -> None:
    cfg = load_media_config(_media_env())
    assert cfg.dtmf_inband_enabled is True


def test_interdigit_ms_drives_default_when_unset() -> None:
    """dtmf_interdigit_ms None => the receiver primitive uses its built-in default."""
    cfg = load_media_config(_media_env())
    assert cfg.dtmf_interdigit_ms is None
