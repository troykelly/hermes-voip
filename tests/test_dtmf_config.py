"""Tests that the DTMF config keys drive real behaviour (ADR-0010/0034, rule-27).

Before the DTMF-receive lane the keys ``HERMES_SIP_DTMF_MODE`` /
``HERMES_SIP_DTMF_INBAND_ENABLED`` / ``HERMES_SIP_DTMF_INTERDIGIT_MS`` were parsed into
:class:`MediaConfig` but consumed NOWHERE (rule-27 drift). ADR-0035 ships the last two
DTMF mechanisms, so all four ADR-0010 modes now load and drive a real backend:

* ``auto`` and ``rfc4733`` prefer RFC 4733 when telephone-event is negotiated.
* ``sip_info`` and ``inband`` are now IMPLEMENTED (ADR-0035) — they load and resolve to
  their backend (in-band only on a G.711 call).
* ``resolve_dtmf_receive_mode`` maps ``(dtmf_mode, dtmf_inband_enabled, PT, codec)``
  to a concrete :class:`DtmfReceiveMode`, so each key changes a real, observable
  outcome. The full send/receive resolution matrix lives in
  ``test_dtmf_mode_resolution.py``.
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


def test_sip_info_mode_loads() -> None:
    """sip_info is implemented (ADR-0035) — it now loads."""
    cfg = load_media_config(_media_env(HERMES_SIP_DTMF_MODE="sip_info"))
    assert cfg.dtmf_mode == "sip_info"


def test_inband_mode_loads() -> None:
    """In-band is implemented (ADR-0035) — it now loads."""
    cfg = load_media_config(_media_env(HERMES_SIP_DTMF_MODE="inband"))
    assert cfg.dtmf_mode == "inband"


def test_unknown_mode_still_rejected() -> None:
    with pytest.raises(ConfigError):
        load_media_config(_media_env(HERMES_SIP_DTMF_MODE="carrier-pigeon"))


# --- resolve_dtmf_receive_mode: each key changes the resolved outcome ---


def test_resolve_auto_with_negotiated_pt_is_rfc4733() -> None:
    """Auto + a negotiated telephone-event PT => RFC 4733 receive active."""
    cfg = load_media_config(_media_env(HERMES_SIP_DTMF_MODE="auto"))
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=101, codec="PCMU")
        is DtmfReceiveMode.RFC4733
    )


def test_resolve_auto_without_pt_is_disabled() -> None:
    """Auto + NO negotiated PT + in-band disabled => DISABLED (nothing to receive)."""
    cfg = load_media_config(
        _media_env(HERMES_SIP_DTMF_MODE="auto", HERMES_SIP_DTMF_INBAND_ENABLED="false")
    )
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None, codec="PCMU")
        is DtmfReceiveMode.DISABLED
    )


def test_resolve_auto_without_pt_inband_enabled_g711_is_inband() -> None:
    """Auto + NO PT + in-band ENABLED on a G.711 call => in-band receive (ADR-0035).

    The operator permits the in-band last resort (``dtmf_inband_enabled`` true, the
    default), no telephone-event was negotiated, and the codec is G.711 — so the
    in-band Goertzel detector is the live receive backend. The flag CHANGES the
    resolved mode (DISABLED vs INBAND), so it is not inert.
    """
    cfg = load_media_config(
        _media_env(HERMES_SIP_DTMF_MODE="auto", HERMES_SIP_DTMF_INBAND_ENABLED="true")
    )
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None, codec="PCMU")
        is DtmfReceiveMode.INBAND
    )


def test_resolve_rfc4733_forced_with_pt() -> None:
    cfg = load_media_config(_media_env(HERMES_SIP_DTMF_MODE="rfc4733"))
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=96, codec="PCMU")
        is DtmfReceiveMode.RFC4733
    )


def test_resolve_rfc4733_forced_without_pt_is_unavailable() -> None:
    """rfc4733 forced but the gateway negotiated no telephone-event => UNAVAILABLE.

    The operator demanded RFC 4733 but the peer did not offer telephone-event, so
    receive cannot run — surfaced as UNAVAILABLE (loud), never a silent DISABLED.
    """
    cfg = load_media_config(_media_env(HERMES_SIP_DTMF_MODE="rfc4733"))
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None, codec="PCMU")
        is DtmfReceiveMode.UNAVAILABLE
    )


def test_inband_enabled_default_true() -> None:
    cfg = load_media_config(_media_env())
    assert cfg.dtmf_inband_enabled is True


def test_interdigit_ms_drives_default_when_unset() -> None:
    """dtmf_interdigit_ms None => the receiver primitive uses its built-in default."""
    cfg = load_media_config(_media_env())
    assert cfg.dtmf_interdigit_ms is None
