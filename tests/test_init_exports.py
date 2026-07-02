"""Test that foundation modules export their public API via __all__."""

import hermes_voip
import hermes_voip.call_context
import hermes_voip.dtmf
import hermes_voip.message
import hermes_voip.providers.asr
import hermes_voip.providers.guard
import hermes_voip.providers.tts
import hermes_voip.registration
import hermes_voip.rtcp
import hermes_voip.rtp
import hermes_voip.sdp
import hermes_voip.sip
import hermes_voip.stt
import hermes_voip.transport
import hermes_voip.tts


def test_tts_package_all_excludes_internal_seams() -> None:
    # Not advertised in __all__ (trimmed from public surface)
    assert "HttpByteStream" not in hermes_voip.tts.__all__
    assert "HttpCancellation" not in hermes_voip.tts.__all__
    assert "ElevenLabsRequest" not in hermes_voip.tts.__all__
    # Still importable as package attributes (back-compat for existing consumers)
    assert hasattr(hermes_voip.tts, "HttpByteStream")
    assert hasattr(hermes_voip.tts, "HttpCancellation")
    assert hasattr(hermes_voip.tts, "ElevenLabsRequest")


def test_stt_package_all_excludes_internal_helpers() -> None:
    # Not advertised in __all__ (trimmed from public surface)
    assert "RECOGNISER_SAMPLE_RATE" not in hermes_voip.stt.__all__
    assert "FrameUpsampler" not in hermes_voip.stt.__all__
    assert "float32_to_pcm16" not in hermes_voip.stt.__all__
    assert "pcm16_to_float32" not in hermes_voip.stt.__all__
    # Still importable as package attributes (back-compat for existing consumers)
    assert hasattr(hermes_voip.stt, "RECOGNISER_SAMPLE_RATE")
    assert hasattr(hermes_voip.stt, "FrameUpsampler")
    assert hasattr(hermes_voip.stt, "float32_to_pcm16")
    assert hasattr(hermes_voip.stt, "pcm16_to_float32")


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


# ── (A) transport.__all__ gaps ────────────────────────────────────────────────


def test_transport_all_exports_wss_sip_transport() -> None:
    """WssSipTransport must appear in hermes_voip.transport.__all__."""
    assert "WssSipTransport" in hermes_voip.transport.__all__
    assert hasattr(hermes_voip.transport, "WssSipTransport")


def test_transport_all_exports_call_response_sink() -> None:
    """CallResponseSink must appear in hermes_voip.transport.__all__."""
    assert "CallResponseSink" in hermes_voip.transport.__all__
    assert hasattr(hermes_voip.transport, "CallResponseSink")


# ── (B) top-level __all__ gaps ────────────────────────────────────────────────


def test_top_level_exports_media_config() -> None:
    """MediaConfig must appear in hermes_voip.__all__ and be importable."""
    assert "MediaConfig" in hermes_voip.__all__
    assert hasattr(hermes_voip, "MediaConfig")


def test_top_level_exports_gateway_config() -> None:
    """GatewayConfig must appear in hermes_voip.__all__ and be importable."""
    assert "GatewayConfig" in hermes_voip.__all__
    assert hasattr(hermes_voip, "GatewayConfig")


def test_top_level_exports_config_error() -> None:
    """ConfigError must appear in hermes_voip.__all__ and be importable."""
    assert "ConfigError" in hermes_voip.__all__
    assert hasattr(hermes_voip, "ConfigError")


def test_top_level_exports_providers() -> None:
    """Providers must appear in hermes_voip.__all__ and be importable."""
    assert "Providers" in hermes_voip.__all__
    assert hasattr(hermes_voip, "Providers")


def test_top_level_exports_build_providers() -> None:
    """build_providers must appear in hermes_voip.__all__ and be importable."""
    assert "build_providers" in hermes_voip.__all__
    assert hasattr(hermes_voip, "build_providers")


def test_top_level_exports_pcm_frame() -> None:
    """PcmFrame must appear in hermes_voip.__all__ and be importable."""
    assert "PcmFrame" in hermes_voip.__all__
    assert hasattr(hermes_voip, "PcmFrame")


# ── (C) top-level provider-protocol export gaps (bk872) ──────────────────────
# StreamingASR / StreamingTTS / InjectionGuard are the canonical ADR-0004
# provider seams — already re-exported at hermes_voip.providers.__all__ — but
# #324 did not promote them to the hermes_voip top level alongside the other
# provider-wiring names (Providers, build_providers, PcmFrame).


def test_top_level_exports_streaming_asr() -> None:
    """StreamingASR must appear in hermes_voip.__all__ and be importable."""
    assert "StreamingASR" in hermes_voip.__all__
    assert hasattr(hermes_voip, "StreamingASR")
    assert hermes_voip.StreamingASR is hermes_voip.providers.asr.StreamingASR


def test_top_level_exports_streaming_tts() -> None:
    """StreamingTTS must appear in hermes_voip.__all__ and be importable."""
    assert "StreamingTTS" in hermes_voip.__all__
    assert hasattr(hermes_voip, "StreamingTTS")
    assert hermes_voip.StreamingTTS is hermes_voip.providers.tts.StreamingTTS


def test_top_level_exports_injection_guard() -> None:
    """InjectionGuard must appear in hermes_voip.__all__ and be importable."""
    assert "InjectionGuard" in hermes_voip.__all__
    assert hasattr(hermes_voip, "InjectionGuard")
    assert hermes_voip.InjectionGuard is hermes_voip.providers.guard.InjectionGuard


# ── (D) top-level submodule-attribute leak (bk872) ────────────────────────────
# #324's __all__ correctly excludes these internal submodules (so
# ``from hermes_voip import *`` already excludes them), but plain
# ``import hermes_voip`` still left them resolvable as ``hermes_voip.<name>``
# attributes — Python's import machinery binds every submodule pulled in,
# directly or transitively, by hermes_voip/__init__.py onto the package. The
# attribute surface must agree with __all__, not just the star-import surface.
#
# Only 4 of the 7 leaked names are covered here (not sip/registration/message):
# those three are load-bearing for existing tests ABOVE in this same file
# (test_sip_exports_sip_address_of_record, test_registration_exports_
# registration_flow, test_message_exports_sip_request all reach the module via
# hermes_voip.sip/.registration/.message attribute access) — deleting them
# would raise AttributeError there. caller_modes/config/digest/plugin have no
# such dependency anywhere in tests/ or src/, so they can be fully closed.


def test_top_level_does_not_leak_caller_modes_submodule() -> None:
    """hermes_voip.caller_modes must not resolve as a top-level attribute."""
    assert "caller_modes" not in hermes_voip.__all__
    assert not hasattr(hermes_voip, "caller_modes")


def test_top_level_does_not_leak_config_submodule() -> None:
    """hermes_voip.config must not resolve as a top-level attribute."""
    assert "config" not in hermes_voip.__all__
    assert not hasattr(hermes_voip, "config")


def test_top_level_does_not_leak_digest_submodule() -> None:
    """hermes_voip.digest must not resolve as a top-level attribute."""
    assert "digest" not in hermes_voip.__all__
    assert not hasattr(hermes_voip, "digest")


def test_top_level_does_not_leak_plugin_submodule() -> None:
    """hermes_voip.plugin must not resolve as a top-level attribute."""
    assert "plugin" not in hermes_voip.__all__
    assert not hasattr(hermes_voip, "plugin")
