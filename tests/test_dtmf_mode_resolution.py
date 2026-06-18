"""Tests for per-call DTMF send + receive mode resolution (ADR-0010/0034).

A single resolver maps ``(dtmf_mode, dtmf_inband_enabled)`` + the negotiated
telephone-event payload type + the negotiated audio codec to a concrete SEND
backend and a concrete RECEIVE backend, independently. ``auto`` prefers RFC 4733
when telephone-event was negotiated, else falls to in-band on a G.711 call.
In-band is trusted ONLY on G.711 (ADR-0005): on any other codec it resolves to
UNAVAILABLE, never a wrong-codec detector.
"""

from __future__ import annotations

from hermes_voip.config import MediaConfig, load_media_config
from hermes_voip.dtmf_config import (
    DtmfReceiveMode,
    DtmfSendMode,
    resolve_dtmf_receive_mode,
    resolve_dtmf_send_mode,
)


def _cfg(**overrides: str) -> MediaConfig:
    return load_media_config(dict(overrides))


# --- receive resolution -----------------------------------------------------


def test_auto_with_pt_receive_is_rfc4733() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="auto")
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=101, codec="PCMU")
        is DtmfReceiveMode.RFC4733
    )


def test_auto_no_pt_g711_inband_enabled_receive_is_inband() -> None:
    """Auto + no telephone-event + G.711 + in-band permitted => in-band receive."""
    cfg = _cfg(HERMES_SIP_DTMF_MODE="auto", HERMES_SIP_DTMF_INBAND_ENABLED="true")
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None, codec="PCMU")
        is DtmfReceiveMode.INBAND
    )
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None, codec="PCMA")
        is DtmfReceiveMode.INBAND
    )


def test_auto_no_pt_inband_disabled_receive_is_disabled() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="auto", HERMES_SIP_DTMF_INBAND_ENABLED="false")
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None, codec="PCMU")
        is DtmfReceiveMode.DISABLED
    )


def test_auto_no_pt_non_g711_receive_is_unavailable() -> None:
    """Auto + no telephone-event on a non-G.711 call: in-band cannot run => UNAVAILABLE."""  # noqa: E501 — one-line summary reads clearer unwrapped
    cfg = _cfg(HERMES_SIP_DTMF_MODE="auto", HERMES_SIP_DTMF_INBAND_ENABLED="true")
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None, codec="G722")
        is DtmfReceiveMode.UNAVAILABLE
    )
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None, codec="opus")
        is DtmfReceiveMode.UNAVAILABLE
    )


def test_forced_inband_g711_receive_is_inband() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="inband")
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=101, codec="PCMU")
        is DtmfReceiveMode.INBAND
    )


def test_forced_inband_non_g711_receive_is_unavailable() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="inband")
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None, codec="opus")
        is DtmfReceiveMode.UNAVAILABLE
    )


def test_forced_sip_info_receive_is_sip_info() -> None:
    """sip_info forced: SIP INFO is always available (it is in-dialog signalling)."""
    cfg = _cfg(HERMES_SIP_DTMF_MODE="sip_info")
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None, codec="opus")
        is DtmfReceiveMode.SIP_INFO
    )


def test_forced_rfc4733_no_pt_receive_is_unavailable() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="rfc4733")
    assert (
        resolve_dtmf_receive_mode(cfg, telephone_event_payload_type=None, codec="PCMU")
        is DtmfReceiveMode.UNAVAILABLE
    )


# --- send resolution --------------------------------------------------------


def test_auto_with_pt_send_is_rfc4733() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="auto")
    assert (
        resolve_dtmf_send_mode(cfg, telephone_event_payload_type=101, codec="PCMU")
        is DtmfSendMode.RFC4733
    )


def test_auto_no_pt_g711_send_is_inband() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="auto")
    assert (
        resolve_dtmf_send_mode(cfg, telephone_event_payload_type=None, codec="PCMU")
        is DtmfSendMode.INBAND
    )


def test_auto_no_pt_non_g711_send_is_unavailable() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="auto")
    assert (
        resolve_dtmf_send_mode(cfg, telephone_event_payload_type=None, codec="G722")
        is DtmfSendMode.UNAVAILABLE
    )


def test_forced_sip_info_send_is_sip_info() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="sip_info")
    assert (
        resolve_dtmf_send_mode(cfg, telephone_event_payload_type=101, codec="PCMU")
        is DtmfSendMode.SIP_INFO
    )


def test_forced_inband_g711_send_is_inband() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="inband")
    assert (
        resolve_dtmf_send_mode(cfg, telephone_event_payload_type=101, codec="PCMA")
        is DtmfSendMode.INBAND
    )


def test_forced_inband_non_g711_send_is_unavailable() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="inband")
    assert (
        resolve_dtmf_send_mode(cfg, telephone_event_payload_type=101, codec="opus")
        is DtmfSendMode.UNAVAILABLE
    )


def test_forced_rfc4733_no_pt_send_is_unavailable() -> None:
    cfg = _cfg(HERMES_SIP_DTMF_MODE="rfc4733")
    assert (
        resolve_dtmf_send_mode(cfg, telephone_event_payload_type=None, codec="PCMU")
        is DtmfSendMode.UNAVAILABLE
    )
