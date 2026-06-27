"""Test that foundation modules export their public API via __all__."""

import hermes_voip
import hermes_voip.call_context
import hermes_voip.dtmf
import hermes_voip.message
import hermes_voip.registration
import hermes_voip.rtcp
import hermes_voip.rtp
import hermes_voip.sdp
import hermes_voip.sip


def test_rtcp_exports_rtcp_packet() -> None:
    """RtcpPacket should be exported from hermes_voip.rtcp.__all__."""
    assert "RtcpPacket" in hermes_voip.rtcp.__all__
    # Verify it's actually a type alias/class available
    assert hasattr(hermes_voip.rtcp, "RtcpPacket")


def test_rtp_exports_jitter_buffer() -> None:
    """JitterBuffer should be exported from hermes_voip.rtp.__all__."""
    assert "JitterBuffer" in hermes_voip.rtp.__all__
    assert hasattr(hermes_voip.rtp, "JitterBuffer")


def test_sdp_exports_session_description() -> None:
    """SessionDescription should be exported from hermes_voip.sdp.__all__."""
    assert "SessionDescription" in hermes_voip.sdp.__all__
    assert hasattr(hermes_voip.sdp, "SessionDescription")


def test_sip_exports_sip_address_of_record() -> None:
    """sip_address_of_record should be exported from hermes_voip.sip.__all__."""
    assert "sip_address_of_record" in hermes_voip.sip.__all__
    assert hasattr(hermes_voip.sip, "sip_address_of_record")


def test_registration_exports_registration_flow() -> None:
    """RegistrationFlow should be exported from hermes_voip.registration.__all__."""
    assert "RegistrationFlow" in hermes_voip.registration.__all__
    assert hasattr(hermes_voip.registration, "RegistrationFlow")


def test_dtmf_exports_dtmf_press() -> None:
    """DtmfPress should be exported from hermes_voip.dtmf.__all__."""
    assert "DtmfPress" in hermes_voip.dtmf.__all__
    assert hasattr(hermes_voip.dtmf, "DtmfPress")


def test_message_exports_sip_request() -> None:
    """SipRequest should be exported from hermes_voip.message.__all__."""
    assert "SipRequest" in hermes_voip.message.__all__
    assert hasattr(hermes_voip.message, "SipRequest")


def test_top_level_exports_inbound_call_context() -> None:
    """InboundCallContext should be exported from hermes_voip.__all__."""
    assert "InboundCallContext" in hermes_voip.__all__
    assert hasattr(hermes_voip, "InboundCallContext")
    assert hermes_voip.InboundCallContext is hermes_voip.call_context.InboundCallContext


def test_top_level_exports_extract_call_context() -> None:
    """extract_call_context should be exported from hermes_voip.__all__."""
    assert "extract_call_context" in hermes_voip.__all__
    assert hasattr(hermes_voip, "extract_call_context")
    assert (
        hermes_voip.extract_call_context
        is hermes_voip.call_context.extract_call_context
    )


def test_top_level_exports_diversion_hop() -> None:
    """DiversionHop should be exported from hermes_voip.__all__."""
    assert "DiversionHop" in hermes_voip.__all__
    assert hasattr(hermes_voip, "DiversionHop")
    assert hermes_voip.DiversionHop is hermes_voip.call_context.DiversionHop


def test_top_level_exports_history_info_entry() -> None:
    """HistoryInfoEntry should be exported from hermes_voip.__all__."""
    assert "HistoryInfoEntry" in hermes_voip.__all__
    assert hasattr(hermes_voip, "HistoryInfoEntry")
    assert hermes_voip.HistoryInfoEntry is hermes_voip.call_context.HistoryInfoEntry
