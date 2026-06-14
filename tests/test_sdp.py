"""Tests for hermes_voip.sdp — SDP offer/answer parse + build (RFC 4566/3264).

Fakes use the RFC 5737 documentation address range (``192.0.2.0/24``); no real
gateway address appears. Covers codec/rtpmap/fmtp parsing, media-security
detection (RTP/AVP vs RTP/SAVP + ``a=crypto`` SDES), codec negotiation, and a
build→parse round-trip.
"""

import pytest

from hermes_voip.sdp import (
    Codec,
    SessionDescription,
    build_audio_offer,
    negotiate_audio,
)

_OFFER_AVP = (
    "v=0\r\n"
    "o=- 8000 8000 IN IP4 192.0.2.1\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.1\r\n"
    "t=0 0\r\n"
    "m=audio 40000 RTP/AVP 0 8 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=fmtp:101 0-16\r\n"
    "a=ptime:20\r\n"
    "a=sendrecv\r\n"
)

_OFFER_SAVP = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 192.0.2.2\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.2\r\n"
    "t=0 0\r\n"
    "m=audio 40002 RTP/SAVP 0 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:WVNfX19zZW1jdGwgKecsfooBytestream\r\n"
    "a=sendrecv\r\n"
)


def test_parse_audio_media_line() -> None:
    sdp = SessionDescription.parse(_OFFER_AVP)
    assert sdp.audio is not None
    assert sdp.audio.port == 40000
    assert sdp.audio.protocol == "RTP/AVP"
    assert sdp.audio.connection_address == "192.0.2.1"
    assert sdp.audio.ptime == 20
    assert sdp.audio.direction == "sendrecv"


def test_parse_codecs_with_rtpmap_and_fmtp() -> None:
    sdp = SessionDescription.parse(_OFFER_AVP)
    assert sdp.audio is not None
    by_pt = {c.payload_type: c for c in sdp.audio.codecs}
    assert by_pt[0].encoding == "PCMU"
    assert by_pt[0].clock_rate == 8000
    assert by_pt[8].encoding == "PCMA"
    assert by_pt[101].encoding == "telephone-event"
    assert by_pt[101].fmtp == "0-16"


def test_is_srtp_detects_savp_and_crypto() -> None:
    avp = SessionDescription.parse(_OFFER_AVP)
    savp = SessionDescription.parse(_OFFER_SAVP)
    assert avp.audio is not None
    assert savp.audio is not None
    assert avp.audio.is_srtp is False
    assert savp.audio.is_srtp is True
    assert len(savp.audio.crypto) == 1
    assert savp.audio.crypto[0].startswith("1 AES_CM_128_HMAC_SHA1_80")


def test_negotiate_keeps_common_codecs_in_offer_order() -> None:
    sdp = SessionDescription.parse(_OFFER_AVP)
    assert sdp.audio is not None
    chosen = negotiate_audio(sdp.audio, supported=("PCMU", "telephone-event"))
    # PCMA is dropped (unsupported); order follows the offer (PCMU then 101)
    assert [c.encoding for c in chosen] == ["PCMU", "telephone-event"]


def test_negotiate_raises_when_no_common_audio_codec() -> None:
    sdp = SessionDescription.parse(_OFFER_AVP)
    assert sdp.audio is not None
    with pytest.raises(ValueError, match="no common audio codec"):
        negotiate_audio(sdp.audio, supported=("OPUS",))


def test_build_audio_offer_round_trips() -> None:
    codecs = (
        Codec(payload_type=0, encoding="PCMU", clock_rate=8000),
        Codec(
            payload_type=101, encoding="telephone-event", clock_rate=8000, fmtp="0-16"
        ),
    )
    text = build_audio_offer(
        local_address="192.0.2.10", port=41000, codecs=codecs, direction="sendrecv"
    )
    parsed = SessionDescription.parse(text)
    assert parsed.audio is not None
    assert parsed.audio.port == 41000
    assert parsed.audio.connection_address == "192.0.2.10"
    assert {c.payload_type for c in parsed.audio.codecs} == {0, 101}
    assert {c.encoding for c in parsed.audio.codecs} == {"PCMU", "telephone-event"}
    te = next(c for c in parsed.audio.codecs if c.payload_type == 101)
    assert te.fmtp == "0-16"


def test_parse_without_audio_media_returns_none() -> None:
    video_only = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 192.0.2.3\r\n"
        "s=-\r\n"
        "t=0 0\r\n"
        "m=video 50000 RTP/AVP 96\r\n"
    )
    assert SessionDescription.parse(video_only).audio is None
