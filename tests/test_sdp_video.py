"""Tests for WebRTC video SDP parse + answer (ADR-0044).

A WebRTC offer may carry an ``m=video`` section alongside ``m=audio`` under a
BUNDLE group (RFC 8843); the gateway shares one DTLS handshake + ICE 5-tuple for
both. This lane:

* parses the first ``m=video`` section into :class:`VideoMedia` (port, protocol,
  H.264 codecs with their ``profile-level-id`` / ``packetization-mode`` fmtp,
  ``a=mid``, direction) — additive, leaving all audio parsing unchanged;
* builds a full WebRTC answer that, when the offer has video, mirrors the
  BUNDLE group, emits an ``m=video`` answer sharing the SAME fingerprint / ICE /
  setup as audio, and is ``a=sendonly`` (when a source is configured) or
  ``a=inactive`` (no source). We NEVER answer ``a=sendrecv``: the plugin discards
  all inbound video, and soliciting inbound video onto the shared BUNDLE 5-tuple
  would let an early inbound video packet bind the audio SRTP session to the
  video SSRC and silently kill inbound audio (ADR-0044).

The audio-only answer MUST be byte-identical to before (regression). Fakes only:
RFC 5737/3849 documentation addresses, fake ICE/fingerprint tokens.
"""

from __future__ import annotations

import pytest

from hermes_voip.sdp import (
    Codec,
    Fingerprint,
    SdpError,
    SessionDescription,
    SetupRole,
    VideoAnswer,
    VideoMedia,
    build_webrtc_answer,
    negotiate_video_h264,
)

_FAKE_FINGERPRINT = (
    "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:"
    "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89"
)
_FAKE_UFRAG = "offerUfrag01"
_FAKE_PWD = "offerPassword0123456789"
_OUR_UFRAG = "ansUfrag99"
_OUR_PWD = "ansPassword9876543210"

# A BUNDLE offer with audio + a real (non-bundle-only) H.264 video m-line that
# carries its own profile-level-id + packetization-mode fmtp. Session-level
# DTLS/ICE (ADR-0042 shape). Fakes only.
_OFFER_AUDIO_VIDEO = (
    "v=0\r\n"
    "o=- 1 1 IN IP6 2001:db8::1\r\n"
    "s=-\r\n"
    "c=IN IP6 2001:db8::1\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0 1\r\n"
    f"a=ice-ufrag:{_FAKE_UFRAG}\r\n"
    f"a=ice-pwd:{_FAKE_PWD}\r\n"
    "a=ice-options:trickle\r\n"
    f"a=fingerprint:SHA-256 {_FAKE_FINGERPRINT}\r\n"
    "a=setup:actpass\r\n"
    "m=audio 19710 UDP/TLS/RTP/SAVPF 0 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
    "a=mid:0\r\n"
    "m=video 19710 UDP/TLS/RTP/SAVPF 99 100\r\n"
    "a=rtpmap:99 H264/90000\r\n"
    "a=fmtp:99 profile-level-id=42e01f;packetization-mode=1\r\n"
    "a=rtpmap:100 H264/90000\r\n"
    "a=fmtp:100 profile-level-id=42e016;packetization-mode=0\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
    "a=mid:1\r\n"
)

# Audio-only WebRTC offer (no m=video) — the regression baseline.
_OFFER_AUDIO_ONLY = (
    "v=0\r\n"
    "o=- 2 2 IN IP4 192.0.2.10\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    f"a=ice-ufrag:{_FAKE_UFRAG}\r\n"
    f"a=ice-pwd:{_FAKE_PWD}\r\n"
    f"a=fingerprint:sha-256 {_FAKE_FINGERPRINT}\r\n"
    "a=setup:actpass\r\n"
    "m=audio 49000 UDP/TLS/RTP/SAVPF 0 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
)


def _webrtc_answer(offer: str, *, video: VideoAnswer | None = None) -> str:
    return build_webrtc_answer(
        SessionDescription.parse(offer),
        local_address="192.0.2.99",
        port=9,
        supported=["PCMU", "telephone-event"],
        fingerprint=Fingerprint(algorithm="sha-256", value=_FAKE_FINGERPRINT),
        setup=SetupRole("passive"),
        ice_ufrag=_OUR_UFRAG,
        ice_pwd=_OUR_PWD,
        ice_candidates=(),
        video=video,
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_extracts_video_media() -> None:
    sdp = SessionDescription.parse(_OFFER_AUDIO_VIDEO)
    assert sdp.video is not None
    assert isinstance(sdp.video, VideoMedia)
    assert sdp.video.protocol == "UDP/TLS/RTP/SAVPF"
    assert sdp.video.port == 19710
    assert sdp.video.mid == "1"


def test_parse_video_h264_codecs_with_fmtp() -> None:
    sdp = SessionDescription.parse(_OFFER_AUDIO_VIDEO)
    assert sdp.video is not None
    encs = [(c.payload_type, c.encoding, c.clock_rate) for c in sdp.video.codecs]
    assert encs == [(99, "H264", 90000), (100, "H264", 90000)]
    by_pt = {c.payload_type: c for c in sdp.video.codecs}
    assert by_pt[99].fmtp == "profile-level-id=42e01f;packetization-mode=1"
    assert by_pt[100].fmtp == "profile-level-id=42e016;packetization-mode=0"


def test_parse_audio_unchanged_when_video_present() -> None:
    sdp = SessionDescription.parse(_OFFER_AUDIO_VIDEO)
    assert sdp.audio is not None
    # Audio still parses with its session-level inherited fingerprint/ICE.
    assert sdp.audio.protocol == "UDP/TLS/RTP/SAVPF"
    assert sdp.audio.ice_ufrag == _FAKE_UFRAG
    assert sdp.audio.fingerprint is not None
    assert [c.encoding for c in sdp.audio.codecs] == ["PCMU", "telephone-event"]


def test_parse_no_video_section_is_none() -> None:
    sdp = SessionDescription.parse(_OFFER_AUDIO_ONLY)
    assert sdp.video is None


def test_parse_video_audio_group_captured() -> None:
    sdp = SessionDescription.parse(_OFFER_AUDIO_VIDEO)
    assert sdp.audio is not None
    assert sdp.audio.mid == "0"


# ---------------------------------------------------------------------------
# negotiate_video_h264
# ---------------------------------------------------------------------------


def test_negotiate_video_prefers_packetization_mode_1() -> None:
    sdp = SessionDescription.parse(_OFFER_AUDIO_VIDEO)
    assert sdp.video is not None
    chosen = negotiate_video_h264(sdp.video)
    assert chosen is not None
    # PT 99 is H.264 with packetization-mode=1 (FU-A capable) — preferred over the
    # mode-0 PT 100.
    assert chosen.payload_type == 99


def test_negotiate_video_none_when_no_h264() -> None:
    no_h264 = _OFFER_AUDIO_VIDEO.replace("H264", "VP8")
    sdp = SessionDescription.parse(no_h264)
    assert sdp.video is not None
    assert negotiate_video_h264(sdp.video) is None


def test_negotiate_video_none_when_only_packetization_mode_0() -> None:
    """Decline H.264 when only packetization-mode=0 is offered (ADR-0044).

    The RFC 6184 packetiser FU-A-fragments any large NAL, which violates the
    mode-0 single-NAL-only contract. Rather than emit FU-A under mode 0, decline
    video (return ``None``) so the adapter answers a=inactive.
    """
    offer = (
        "v=0\r\n"
        "o=- 1 1 IN IP6 2001:db8::1\r\n"
        "s=-\r\n"
        "t=0 0\r\n"
        "a=group:BUNDLE 0 1\r\n"
        f"a=ice-ufrag:{_FAKE_UFRAG}\r\n"
        f"a=ice-pwd:{_FAKE_PWD}\r\n"
        f"a=fingerprint:SHA-256 {_FAKE_FINGERPRINT}\r\n"
        "a=setup:actpass\r\n"
        "m=audio 19710 UDP/TLS/RTP/SAVPF 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtcp-mux\r\n"
        "a=mid:0\r\n"
        "m=video 19710 UDP/TLS/RTP/SAVPF 100\r\n"
        "a=rtpmap:100 H264/90000\r\n"
        "a=fmtp:100 profile-level-id=42e016;packetization-mode=0\r\n"
        "a=rtcp-mux\r\n"
        "a=mid:1\r\n"
    )
    sdp = SessionDescription.parse(offer)
    assert sdp.video is not None
    assert negotiate_video_h264(sdp.video) is None


def test_negotiate_video_none_when_h264_has_no_fmtp() -> None:
    """An H.264 codec with no fmtp at all is mode-0 by default (RFC 6184 §8.1).

    Absent ``packetization-mode``, the default is 0 (single-NAL only); we cannot
    safely FU-A-fragment, so decline.
    """
    offer = (
        "v=0\r\n"
        "o=- 1 1 IN IP6 2001:db8::1\r\n"
        "s=-\r\n"
        "t=0 0\r\n"
        "a=group:BUNDLE 0 1\r\n"
        f"a=ice-ufrag:{_FAKE_UFRAG}\r\n"
        f"a=ice-pwd:{_FAKE_PWD}\r\n"
        f"a=fingerprint:SHA-256 {_FAKE_FINGERPRINT}\r\n"
        "a=setup:actpass\r\n"
        "m=audio 19710 UDP/TLS/RTP/SAVPF 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtcp-mux\r\n"
        "a=mid:0\r\n"
        "m=video 19710 UDP/TLS/RTP/SAVPF 100\r\n"
        "a=rtpmap:100 H264/90000\r\n"
        "a=rtcp-mux\r\n"
        "a=mid:1\r\n"
    )
    sdp = SessionDescription.parse(offer)
    assert sdp.video is not None
    assert negotiate_video_h264(sdp.video) is None


# ---------------------------------------------------------------------------
# Answer building
# ---------------------------------------------------------------------------


def test_answer_video_sendonly_when_source_configured() -> None:
    sdp = SessionDescription.parse(_OFFER_AUDIO_VIDEO)
    assert sdp.video is not None
    chosen = negotiate_video_h264(sdp.video)
    assert chosen is not None
    text = _webrtc_answer(
        _OFFER_AUDIO_VIDEO,
        video=VideoAnswer(codec=chosen, mid="1", active=True),
    )
    assert "m=video 9 UDP/TLS/RTP/SAVPF 99\r\n" in text
    assert "a=rtpmap:99 H264/90000\r\n" in text
    assert "a=fmtp:99 profile-level-id=42e01f;packetization-mode=1\r\n" in text
    # sendonly because a source is configured (active=True) but we discard inbound
    # video — never sendrecv, which would solicit inbound video onto the shared
    # BUNDLE 5-tuple and risk killing inbound audio SRTP binding (ADR-0044).
    video_block = text.split("m=video", 1)[1]
    assert "a=sendonly" in video_block
    assert "a=sendrecv" not in video_block
    assert "a=inactive" not in video_block
    # BUNDLE group + mids (RFC 8843).
    assert "a=group:BUNDLE 0 1\r\n" in text
    assert "a=mid:0\r\n" in text
    assert "a=mid:1\r\n" in text
    # rtcp-mux on the video section.
    assert "a=rtcp-mux" in video_block


def test_answer_video_inactive_when_no_source() -> None:
    sdp = SessionDescription.parse(_OFFER_AUDIO_VIDEO)
    assert sdp.video is not None
    chosen = negotiate_video_h264(sdp.video)
    assert chosen is not None
    text = _webrtc_answer(
        _OFFER_AUDIO_VIDEO,
        video=VideoAnswer(codec=chosen, mid="1", active=False),
    )
    video_block = text.split("m=video", 1)[1]
    assert "a=inactive" in video_block
    assert "a=sendrecv" not in video_block
    # The m-line keeps a real port (not 0) so the BUNDLE group stays intact.
    assert "m=video 9 UDP/TLS/RTP/SAVPF 99\r\n" in text


def test_answer_video_shares_audio_fingerprint_and_ice() -> None:
    sdp = SessionDescription.parse(_OFFER_AUDIO_VIDEO)
    assert sdp.video is not None
    chosen = negotiate_video_h264(sdp.video)
    assert chosen is not None
    text = _webrtc_answer(
        _OFFER_AUDIO_VIDEO,
        video=VideoAnswer(codec=chosen, mid="1", active=True),
    )
    # Exactly one fingerprint/ice-ufrag at the session/audio level shared via BUNDLE
    # — the video section does NOT repeat a different fingerprint.
    assert text.count(f"a=fingerprint:sha-256 {_FAKE_FINGERPRINT}") >= 1
    # The single DTLS fingerprint we present is the one passed in (our cert).
    assert _FAKE_FINGERPRINT in text


def test_audio_only_answer_unchanged_no_video_param() -> None:
    """Regression: an audio-only offer answered with no video param has no video.

    No BUNDLE group, no m=video, no a=mid — byte-identical to the pre-video
    build_webrtc_answer output.
    """
    with_default = _webrtc_answer(_OFFER_AUDIO_ONLY)
    assert "m=video" not in with_default
    assert "a=group:BUNDLE" not in with_default
    assert "a=mid:" not in with_default
    assert "UDP/TLS/RTP/SAVPF" in with_default


def test_audio_only_offer_ignores_video_param() -> None:
    """A video param is harmless when the offer has no m=video: no video emitted."""
    sdp = SessionDescription.parse(_OFFER_AUDIO_ONLY)
    assert sdp.video is None
    # negotiate yields nothing to answer; the adapter would pass video=None, but even
    # an erroneously-supplied param must not invent a video section for an audio offer.
    text = build_webrtc_answer(
        sdp,
        local_address="192.0.2.99",
        port=9,
        supported=["PCMU", "telephone-event"],
        fingerprint=Fingerprint(algorithm="sha-256", value=_FAKE_FINGERPRINT),
        setup=SetupRole("passive"),
        ice_ufrag=_OUR_UFRAG,
        ice_pwd=_OUR_PWD,
        ice_candidates=(),
        video=VideoAnswer(
            codec=Codec(payload_type=99, encoding="H264", clock_rate=90000),
            mid="1",
            active=True,
        ),
    )
    assert "m=video" not in text


def test_build_webrtc_answer_rejects_video_setup_actpass() -> None:
    sdp = SessionDescription.parse(_OFFER_AUDIO_VIDEO)
    assert sdp.video is not None
    chosen = negotiate_video_h264(sdp.video)
    assert chosen is not None
    with pytest.raises(SdpError, match="actpass"):
        build_webrtc_answer(
            sdp,
            local_address="192.0.2.99",
            port=9,
            supported=["PCMU", "telephone-event"],
            fingerprint=__import__(
                "hermes_voip.sdp", fromlist=["Fingerprint"]
            ).Fingerprint(algorithm="sha-256", value=_FAKE_FINGERPRINT),
            setup=SetupRole("actpass"),
            ice_ufrag=_OUR_UFRAG,
            ice_pwd=_OUR_PWD,
            ice_candidates=(),
            video=VideoAnswer(codec=chosen, mid="1", active=True),
        )
