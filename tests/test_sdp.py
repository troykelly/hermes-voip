"""Tests for hermes_voip.sdp — SDP offer/answer parse + build (RFC 4566/3264).

Fakes use the RFC 5737 documentation address range (``192.0.2.0/24``); no real
gateway address appears. Covers codec/rtpmap/fmtp parsing, media-security
detection (RTP/AVP vs RTP/SAVP + ``a=crypto`` SDES), codec negotiation, and a
build→parse round-trip.
"""

import pytest

from hermes_voip.sdp import (
    AudioMedia,
    Codec,
    CryptoAttribute,
    SdpError,
    SessionDescription,
    build_audio_answer,
    build_audio_offer,
    negotiate_audio,
)

# Obvious fake SRTP master key||salt (RFC 4568 inline:). Never a real key.
# AES_CM_128_HMAC_SHA1_80 needs a 30-octet (16 key + 14 salt) key||salt, which
# base64-encodes to exactly 40 chars. These two decode to 30 octets.
_FAKE_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwd"  # bytes 0..29
_FAKE_ANSWER_KEY = "ZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXp7fH1+f4CB"  # bytes 100..129
_FAKE_CRYPTO = f"AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}"
_FAKE_ANSWER_CRYPTO = f"AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_ANSWER_KEY}"
# A key that decodes to 29 octets — one short for the suite (invalid).
_SHORT_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxw="

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
    f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}\r\n"
    "a=sendrecv\r\n"
)

# A SAVP offer whose only crypto line is malformed (key||salt does not decode to
# the suite's 30 octets). The lenient parser keeps it in the raw `crypto` tuple
# but excludes it from typed `crypto_attrs`; an answer to it must be rejected.
_OFFER_SAVP_BAD_CRYPTO = (
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


def test_build_audio_offer_distinct_session_id_and_version() -> None:
    # A re-INVITE keeps the o= session-id constant and bumps only the version
    # (ADR-0011 invariant 1); build_audio_offer takes them separately.
    codecs = (Codec(payload_type=0, encoding="PCMU", clock_rate=8000),)
    text = build_audio_offer(
        local_address="192.0.2.10",
        port=41000,
        codecs=codecs,
        session_id=5000,
        version=7,
    )
    assert "o=- 5000 7 IN IP4 192.0.2.10" in text


def test_build_audio_offer_version_defaults_to_session_id() -> None:
    codecs = (Codec(payload_type=0, encoding="PCMU", clock_rate=8000),)
    text = build_audio_offer(
        local_address="192.0.2.10", port=41000, codecs=codecs, session_id=42
    )
    assert "o=- 42 42 IN IP4 192.0.2.10" in text


def test_parse_without_audio_media_returns_none() -> None:
    video_only = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 192.0.2.3\r\n"
        "s=-\r\n"
        "t=0 0\r\n"
        "m=video 50000 RTP/AVP 96\r\n"
    )
    assert SessionDescription.parse(video_only).audio is None


# --- hardening per cross-vendor review (RFC 4566 scoping + robustness) ---


def _audio(sdp: SessionDescription) -> AudioMedia:
    assert sdp.audio is not None
    return sdp.audio


def test_media_level_c_overrides_session_c() -> None:
    sdp = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\nc=IN IP4 192.0.2.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 0\r\nc=IN IP4 192.0.2.5\r\na=rtpmap:0 PCMU/8000\r\n"
    )
    assert _audio(sdp).connection_address == "192.0.2.5"


def test_first_audio_wins_without_attribute_bleed() -> None:
    sdp = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        "m=audio 40002 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n"
    )
    audio = _audio(sdp)
    assert audio.port == 40000
    assert [c.encoding for c in audio.codecs] == ["PCMU"]  # second section ignored


def test_later_video_section_does_not_change_audio_address() -> None:
    sdp = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 0\r\nc=IN IP4 192.0.2.5\r\na=rtpmap:0 PCMU/8000\r\n"
        "m=video 50000 RTP/AVP 96\r\nc=IN IP4 192.0.2.9\r\n"
    )
    assert _audio(sdp).connection_address == "192.0.2.5"


def test_static_payload_without_rtpmap() -> None:
    sdp = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\nt=0 0\r\nm=audio 40000 RTP/AVP 0 8\r\n"
    )
    by_pt = {c.payload_type: c for c in _audio(sdp).codecs}
    assert by_pt[0].encoding == "PCMU"
    assert by_pt[8].encoding == "PCMA"


def test_dynamic_payload_without_rtpmap_is_dropped() -> None:
    sdp = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\nt=0 0\r\nm=audio 40000 RTP/AVP 0 96\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )
    assert [c.payload_type for c in _audio(sdp).codecs] == [0]


def test_ipv6_connection_address() -> None:
    sdp = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP6 2001:db8::1\r\nc=IN IP6 2001:db8::1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
    )
    assert _audio(sdp).connection_address == "2001:db8::1"


def test_rtpmap_channels_round_trip() -> None:
    codecs = (Codec(payload_type=111, encoding="OPUS", clock_rate=48000, channels=2),)
    text = build_audio_offer(local_address="192.0.2.10", port=41000, codecs=codecs)
    assert "a=rtpmap:111 OPUS/48000/2" in text
    parsed = _audio(SessionDescription.parse(text))
    assert parsed.codecs[0].channels == 2


def test_parse_rejects_non_int_port() -> None:
    with pytest.raises(SdpError, match="m=audio"):
        SessionDescription.parse("v=0\r\nt=0 0\r\nm=audio notaport RTP/AVP 0\r\n")


def test_parse_rejects_truncated_media_line() -> None:
    with pytest.raises(SdpError, match="m=audio"):
        SessionDescription.parse("v=0\r\nt=0 0\r\nm=audio\r\n")


def test_build_rejects_empty_codecs() -> None:
    with pytest.raises(ValueError, match="at least one codec"):
        build_audio_offer(local_address="192.0.2.10", port=41000, codecs=())


def test_build_rejects_bad_port() -> None:
    codecs = (Codec(0, "PCMU", 8000),)
    with pytest.raises(ValueError, match="port"):
        build_audio_offer(local_address="192.0.2.10", port=70000, codecs=codecs)


def test_build_rejects_non_positive_ptime() -> None:
    codecs = (Codec(0, "PCMU", 8000),)
    with pytest.raises(ValueError, match="ptime"):
        build_audio_offer(
            local_address="192.0.2.10", port=41000, codecs=codecs, ptime=0
        )


# --- W3: RTP/SAVP + a=crypto (SDES) offer building (RFC 4568) ---


def test_build_secure_offer_emits_savp_and_crypto_line() -> None:
    codecs = (Codec(0, "PCMU", 8000),)
    text = build_audio_offer(
        local_address="192.0.2.10",
        port=41000,
        codecs=codecs,
        crypto=_FAKE_CRYPTO,
    )
    # SAVP profile on the m= line; the crypto attribute carries tag 1.
    assert "m=audio 41000 RTP/SAVP 0" in text
    assert f"a=crypto:1 {_FAKE_CRYPTO}" in text
    # Negative control: the plaintext AVP profile must NOT appear.
    assert "RTP/AVP" not in text


def test_secure_offer_round_trips_to_srtp_media() -> None:
    codecs = (Codec(0, "PCMU", 8000),)
    text = build_audio_offer(
        local_address="192.0.2.10", port=41000, codecs=codecs, crypto=_FAKE_CRYPTO
    )
    parsed = _audio(SessionDescription.parse(text))
    assert parsed.protocol == "RTP/SAVP"
    assert parsed.is_srtp is True
    assert parsed.crypto == (f"1 {_FAKE_CRYPTO}",)


def test_plaintext_offer_emits_no_crypto_line() -> None:
    # Negative control: without crypto, output stays RTP/AVP with no a=crypto.
    codecs = (Codec(0, "PCMU", 8000),)
    text = build_audio_offer(local_address="192.0.2.10", port=41000, codecs=codecs)
    assert "RTP/AVP" in text
    assert "RTP/SAVP" not in text
    assert "a=crypto" not in text


# --- W3: Opus-first then G.711 codec ordering in the offer ---


def test_offer_orders_opus_before_g711() -> None:
    # Codecs are supplied G.711-first; the offer must reorder Opus to the front.
    codecs = (
        Codec(0, "PCMU", 8000),
        Codec(8, "PCMA", 8000),
        Codec(111, "opus", 48000, channels=2),
        Codec(101, "telephone-event", 8000, fmtp="0-16"),
    )
    text = build_audio_offer(local_address="192.0.2.10", port=41000, codecs=codecs)
    # m= payload order: opus(111), PCMU(0), PCMA(8), telephone-event(101).
    assert "m=audio 41000 RTP/AVP 111 0 8 101" in text
    # rtpmap lines follow the same order.
    rtpmap_pts = [
        int(line.split(":", 1)[1].split(" ", 1)[0])
        for line in text.split("\r\n")
        if line.startswith("a=rtpmap:")
    ]
    assert rtpmap_pts == [111, 0, 8, 101]


def test_offer_without_opus_preserves_given_order() -> None:
    # No Opus present -> ordering is left exactly as supplied (no reshuffle).
    codecs = (
        Codec(8, "PCMA", 8000),
        Codec(0, "PCMU", 8000),
        Codec(101, "telephone-event", 8000, fmtp="0-16"),
    )
    text = build_audio_offer(local_address="192.0.2.10", port=41000, codecs=codecs)
    assert "m=audio 41000 RTP/AVP 8 0 101" in text


# --- W3: build_audio_answer (RFC 3264 §6.1 direction mirroring + negotiation) ---


def test_answer_negotiates_codecs_and_mirrors_sendrecv() -> None:
    offer = SessionDescription.parse(_OFFER_AVP)  # PCMU, PCMA, telephone-event
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
    )
    parsed = _audio(SessionDescription.parse(text))
    # PCMA dropped (unsupported); PCMU + telephone-event kept in offer order.
    assert [c.encoding for c in parsed.codecs] == ["PCMU", "telephone-event"]
    assert parsed.port == 42000
    assert parsed.connection_address == "192.0.2.20"
    assert parsed.protocol == "RTP/AVP"
    # sendrecv offer -> sendrecv answer (RFC 3264 §6.1).
    assert "a=sendrecv" in text


@pytest.mark.parametrize(
    ("offer_dir", "answer_dir"),
    [
        ("sendrecv", "sendrecv"),
        ("sendonly", "recvonly"),
        ("recvonly", "sendonly"),
        ("inactive", "inactive"),
    ],
)
def test_answer_mirrors_direction(offer_dir: str, answer_dir: str) -> None:
    offer_text = (
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\nc=IN IP4 192.0.2.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        f"a={offer_dir}\r\n"
    )
    offer = SessionDescription.parse(offer_text)
    text = build_audio_answer(
        offer, local_address="192.0.2.20", port=42000, supported=("PCMU",)
    )
    assert f"a={answer_dir}\r\n" in text
    # Negative control: the offered direction is not blindly echoed when it
    # differs from the mirrored one.
    if offer_dir != answer_dir:
        assert f"a={offer_dir}\r\n" not in text


def test_answer_to_savp_offer_keys_crypto() -> None:
    offer = SessionDescription.parse(_OFFER_SAVP)  # PCMU + telephone-event, SAVP
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        crypto=_FAKE_ANSWER_CRYPTO,
    )
    parsed = _audio(SessionDescription.parse(text))
    assert parsed.protocol == "RTP/SAVP"
    # The answer echoes the ACCEPTED offer's tag (1) + suite but carries OUR key
    # material (RFC 4568: each party supplies its own key||salt).
    assert text.count("a=crypto:") == 1
    assert f"a=crypto:1 {_FAKE_ANSWER_CRYPTO}" in text
    # Negative control: the offerer's key material is NOT echoed back.
    assert _FAKE_KEY not in text


def test_answer_to_savp_offer_without_crypto_is_rejected() -> None:
    offer = SessionDescription.parse(_OFFER_SAVP)
    with pytest.raises(SdpError, match="crypto"):
        build_audio_answer(
            offer,
            local_address="192.0.2.20",
            port=42000,
            supported=("PCMU", "telephone-event"),
        )


def test_answer_rejects_telephone_event_only_offer() -> None:
    te_only = (
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\nc=IN IP4 192.0.2.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 101\r\na=rtpmap:101 telephone-event/8000\r\n"
        "a=fmtp:101 0-16\r\na=sendrecv\r\n"
    )
    offer = SessionDescription.parse(te_only)
    with pytest.raises(SdpError, match="no common audio codec"):
        build_audio_answer(
            offer,
            local_address="192.0.2.20",
            port=42000,
            supported=("PCMU", "telephone-event"),
        )


def test_answer_requires_audio_in_offer() -> None:
    video_only = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.3\r\ns=-\r\nt=0 0\r\n"
        "m=video 50000 RTP/AVP 96\r\n"
    )
    with pytest.raises(SdpError, match="no audio media"):
        build_audio_answer(
            video_only, local_address="192.0.2.20", port=42000, supported=("PCMU",)
        )


def test_answer_preserves_offer_codec_order_not_supported_order() -> None:
    # Offer lists PCMA before PCMU; answer must follow the OFFER order, not the
    # order of `supported` (RFC 3264: answer uses the offerer's preference).
    offer = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\nc=IN IP4 192.0.2.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 8 0\r\na=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
    )
    text = build_audio_answer(
        offer, local_address="192.0.2.20", port=42000, supported=("PCMU", "PCMA")
    )
    assert "m=audio 42000 RTP/AVP 8 0" in text


# --- W3 (review): typed a=crypto parse + validation + negotiation (RFC 4568) ---


def test_crypto_attribute_parse_and_render_round_trip() -> None:
    attr = CryptoAttribute.parse(f"7 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}")
    assert attr.tag == 7
    assert attr.suite == "AES_CM_128_HMAC_SHA1_80"
    assert attr.key_params == f"inline:{_FAKE_KEY}"
    # render() reproduces the attribute body byte-for-byte.
    assert attr.render() == f"7 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}"


def test_crypto_attribute_parse_tolerates_lifetime_and_mki() -> None:
    # RFC 4568: inline key may carry optional |lifetime|MKI:length fields.
    body = f"1 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}|2^20|1:4"
    attr = CryptoAttribute.parse(body)
    assert attr.tag == 1
    assert attr.key_params == f"inline:{_FAKE_KEY}|2^20|1:4"


@pytest.mark.parametrize(
    "body",
    [
        f"0 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}",  # leading-zero tag
        f"x AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}",  # non-decimal tag
        f"1 AES_CM_256_HMAC_SHA1_80 inline:{_FAKE_KEY}",  # unsupported suite
        f"1 AES_CM_128_HMAC_SHA1_80 inline:{_SHORT_KEY}",  # wrong key length
        "1 AES_CM_128_HMAC_SHA1_80 inline:@@notbase64@@",  # non-base64 key
        "1 AES_CM_128_HMAC_SHA1_80 keymethodext:zzz",  # not an inline key
        "1 AES_CM_128_HMAC_SHA1_80",  # missing key params
    ],
)
def test_crypto_attribute_parse_rejects_malformed(body: str) -> None:
    with pytest.raises(SdpError):
        CryptoAttribute.parse(body)


def test_parser_exposes_typed_crypto_attrs() -> None:
    audio = _audio(SessionDescription.parse(_OFFER_SAVP))
    assert len(audio.crypto_attrs) == 1
    attr = audio.crypto_attrs[0]
    assert isinstance(attr, CryptoAttribute)
    assert attr.tag == 1
    assert attr.suite == "AES_CM_128_HMAC_SHA1_80"
    assert attr.key_params == f"inline:{_FAKE_KEY}"


def test_parser_is_lenient_on_malformed_crypto() -> None:
    # The raw line is preserved for diagnostics, but it is NOT promoted to a
    # typed crypto_attrs entry (it fails suite/key validation).
    audio = _audio(SessionDescription.parse(_OFFER_SAVP_BAD_CRYPTO))
    assert len(audio.crypto) == 1  # raw line kept
    assert audio.crypto_attrs == ()  # nothing well-formed


def test_offer_builder_rejects_garbage_crypto_string() -> None:
    codecs = (Codec(0, "PCMU", 8000),)
    with pytest.raises(SdpError):
        build_audio_offer(
            local_address="192.0.2.10", port=41000, codecs=codecs, crypto="bogus"
        )


def test_offer_builder_rejects_bad_suite() -> None:
    codecs = (Codec(0, "PCMU", 8000),)
    with pytest.raises(SdpError, match="suite"):
        build_audio_offer(
            local_address="192.0.2.10",
            port=41000,
            codecs=codecs,
            crypto=f"1 AES_CM_256_HMAC_SHA1_80 inline:{_FAKE_KEY}",
        )


def test_offer_builder_rejects_bad_key_length() -> None:
    codecs = (Codec(0, "PCMU", 8000),)
    with pytest.raises(SdpError, match="key"):
        build_audio_offer(
            local_address="192.0.2.10",
            port=41000,
            codecs=codecs,
            crypto=f"1 AES_CM_128_HMAC_SHA1_80 inline:{_SHORT_KEY}",
        )


def test_offer_builder_accepts_crypto_attribute_object() -> None:
    codecs = (Codec(0, "PCMU", 8000),)
    attr = CryptoAttribute.parse(f"9 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}")
    text = build_audio_offer(
        local_address="192.0.2.10", port=41000, codecs=codecs, crypto=attr
    )
    assert "m=audio 41000 RTP/SAVP 0" in text
    assert f"a=crypto:9 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}" in text


def test_answer_echoes_accepted_offer_tag() -> None:
    # Offer presents the crypto under tag 7; the answer MUST reuse tag 7 (the
    # negotiated identifier, RFC 4568), with OUR key material.
    offer = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 40002 RTP/SAVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        f"a=crypto:7 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}\r\na=sendrecv\r\n"
    )
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU",),
        crypto=_FAKE_ANSWER_CRYPTO,
    )
    assert f"a=crypto:7 {_FAKE_ANSWER_CRYPTO}" in text
    # Negative control: tag 1 (a different tag) is not emitted.
    assert "a=crypto:1 " not in text


def test_answer_selects_supported_suite_among_offered() -> None:
    # First offered crypto is an unsupported suite; the second is supported. The
    # answer must select the supported one and echo ITS tag (2).
    offer = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 40002 RTP/SAVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        f"a=crypto:1 AES_CM_256_HMAC_SHA1_80 inline:{_FAKE_KEY}\r\n"
        f"a=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}\r\na=sendrecv\r\n"
    )
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU",),
        crypto=_FAKE_ANSWER_CRYPTO,
    )
    assert f"a=crypto:2 {_FAKE_ANSWER_CRYPTO}" in text


def test_answer_rejects_savp_offer_with_only_malformed_crypto() -> None:
    offer = SessionDescription.parse(_OFFER_SAVP_BAD_CRYPTO)
    with pytest.raises(SdpError, match="crypto"):
        build_audio_answer(
            offer,
            local_address="192.0.2.20",
            port=42000,
            supported=("PCMU", "telephone-event"),
            crypto=_FAKE_ANSWER_CRYPTO,
        )


def test_answer_rejects_savp_offer_with_no_supported_suite() -> None:
    offer = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 40002 RTP/SAVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        f"a=crypto:1 AES_CM_256_HMAC_SHA1_80 inline:{_FAKE_KEY}\r\na=sendrecv\r\n"
    )
    with pytest.raises(SdpError, match="crypto"):
        build_audio_answer(
            offer,
            local_address="192.0.2.20",
            port=42000,
            supported=("PCMU",),
            crypto=_FAKE_ANSWER_CRYPTO,
        )


def test_answer_to_savp_offer_with_bad_answer_key_is_rejected() -> None:
    # Our OWN supplied key must also be validated before emission.
    offer = SessionDescription.parse(_OFFER_SAVP)
    with pytest.raises(SdpError, match="key"):
        build_audio_answer(
            offer,
            local_address="192.0.2.20",
            port=42000,
            supported=("PCMU", "telephone-event"),
            crypto=f"1 AES_CM_128_HMAC_SHA1_80 inline:{_SHORT_KEY}",
        )


# --- W3 (lane spec): also allow-list AES_CM_128_HMAC_SHA1_32 (RFC 4568 §6.2) ---
# The _32 suite uses the same AES_CM_128 cipher (16-octet master key + 14-octet
# master salt = 30-octet inline key||salt); it differs from _80 only in the SRTP
# auth-tag length (32 vs 80 bits). The lane requires both suites accepted.

_SUITE_32 = "AES_CM_128_HMAC_SHA1_32"


def test_crypto_attribute_accepts_sha1_32_suite() -> None:
    attr = CryptoAttribute.parse(f"3 {_SUITE_32} inline:{_FAKE_KEY}")
    assert attr.suite == _SUITE_32
    assert attr.tag == 3
    assert attr.key_params == f"inline:{_FAKE_KEY}"
    assert attr.render() == f"3 {_SUITE_32} inline:{_FAKE_KEY}"


def test_crypto_attribute_sha1_32_enforces_key_length() -> None:
    # The _32 suite shares the 30-octet key||salt requirement; a short key is
    # still rejected (proves _32 is validated, not blindly accepted).
    with pytest.raises(SdpError, match="key"):
        CryptoAttribute.parse(f"1 {_SUITE_32} inline:{_SHORT_KEY}")


def test_parser_promotes_sha1_32_offer_line() -> None:
    offer = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 40002 RTP/SAVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        f"a=crypto:1 {_SUITE_32} inline:{_FAKE_KEY}\r\na=sendrecv\r\n"
    )
    audio = _audio(offer)
    assert len(audio.crypto_attrs) == 1
    assert audio.crypto_attrs[0].suite == _SUITE_32


def test_offer_builder_emits_sha1_32_crypto_line() -> None:
    codecs = (Codec(0, "PCMU", 8000),)
    text = build_audio_offer(
        local_address="192.0.2.10",
        port=41000,
        codecs=codecs,
        crypto=f"{_SUITE_32} inline:{_FAKE_KEY}",
    )
    assert "m=audio 41000 RTP/SAVP 0" in text
    assert f"a=crypto:1 {_SUITE_32} inline:{_FAKE_KEY}" in text


def test_answer_echoes_sha1_32_accepted_suite() -> None:
    # An offer keyed with the _32 suite under tag 5 must be answered with the
    # _32 suite and tag 5, carrying OUR key material.
    offer = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
        "m=audio 40002 RTP/SAVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        f"a=crypto:5 {_SUITE_32} inline:{_FAKE_KEY}\r\na=sendrecv\r\n"
    )
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU",),
        crypto=f"{_SUITE_32} inline:{_FAKE_ANSWER_KEY}",
    )
    assert f"a=crypto:5 {_SUITE_32} inline:{_FAKE_ANSWER_KEY}" in text
    # Negative control: the offerer's key material is NOT echoed back.
    assert _FAKE_KEY not in text


# --- W3 (security review): SDES key material must never leak via repr/errors ---
# A `repr()` lands in logs and tracebacks; SDES master key||salt in a repr leaks
# the SRTP session key. Likewise SdpError messages can surface for our OWN crypto,
# so they must report STRUCTURAL facts only, never the key bytes/base64 string.


def test_crypto_attribute_repr_hides_key_material() -> None:
    # HIGH: the key||salt is the SRTP master key; it must not appear in repr().
    attr = CryptoAttribute.parse(f"1 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}")
    text = repr(attr)
    assert _FAKE_KEY not in text
    assert "inline:" not in text  # the whole key-params field is suppressed
    # The non-secret fields are still useful for diagnostics.
    assert "tag=1" in text
    assert "AES_CM_128_HMAC_SHA1_80" in text


def test_audio_media_repr_hides_crypto_key_material() -> None:
    # HIGH: AudioMedia.repr() must expose neither the raw a=crypto line (which
    # carries the inline key) nor the typed crypto_attrs.
    audio = _audio(SessionDescription.parse(_OFFER_SAVP))
    assert audio.crypto  # the raw line is retained on the object...
    assert audio.crypto_attrs  # ...as is the typed attribute...
    text = repr(audio)
    assert _FAKE_KEY not in text  # ...but neither leaks into its repr.
    assert "inline:" not in text


def test_audio_media_repr_hides_directly_constructed_key() -> None:
    # The suppression is a property of the dataclass fields, not of the parser:
    # a directly-constructed AudioMedia hides the key too.
    attr = CryptoAttribute.parse(f"1 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}")
    audio = AudioMedia(
        port=40000,
        protocol="RTP/SAVP",
        codecs=(Codec(0, "PCMU", 8000),),
        crypto=(f"1 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}",),
        ptime=20,
        direction="sendrecv",
        connection_address="192.0.2.2",
        crypto_attrs=(attr,),
    )
    text = repr(audio)
    assert _FAKE_KEY not in text
    assert "inline:" not in text


def test_crypto_error_non_inline_key_does_not_leak_key() -> None:
    # MEDIUM: a non-inline key-params string must not be echoed into the error.
    secret = "keymethodext:SECRETKEYMATERIAL0123456789"
    with pytest.raises(SdpError) as exc_info:
        CryptoAttribute.parse(f"1 AES_CM_128_HMAC_SHA1_80 {secret}")
    msg = str(exc_info.value)
    assert "SECRETKEYMATERIAL0123456789" not in msg
    assert "keymethodext:" not in msg
    assert "inline" in msg  # structural fact: it must be an inline key


def test_crypto_error_bad_base64_does_not_leak_token() -> None:
    # MEDIUM: the invalid base64 token can BE the (corrupt) key; never echo it.
    with pytest.raises(SdpError) as exc_info:
        CryptoAttribute.parse("1 AES_CM_128_HMAC_SHA1_80 inline:@@notbase64@@")
    msg = str(exc_info.value)
    assert "@@notbase64@@" not in msg
    assert "base64" in msg  # structural fact: not valid base64


def test_crypto_error_malformed_body_does_not_leak_key() -> None:
    # MEDIUM: a truncated crypto body (here the tag is missing, so only two
    # whitespace tokens remain) still carries the inline key in the last token;
    # the "malformed" error must not echo the whole body.
    body = f"AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}"  # missing tag => 2 fields
    with pytest.raises(SdpError) as exc_info:
        CryptoAttribute.parse(body)
    msg = str(exc_info.value)
    assert _FAKE_KEY not in msg
    assert "inline:" not in msg


def test_crypto_error_wrong_length_reports_structure_only() -> None:
    # The key-length error is already structural (octet counts + suite). Confirm
    # it states the lengths and suite but never the base64 key itself.
    with pytest.raises(SdpError) as exc_info:
        CryptoAttribute.parse(f"1 AES_CM_128_HMAC_SHA1_80 inline:{_SHORT_KEY}")
    msg = str(exc_info.value)
    assert _SHORT_KEY not in msg
    # Structural facts are retained.
    assert "29" in msg  # decoded octet count
    assert "30" in msg  # expected octet count
    assert "AES_CM_128_HMAC_SHA1_80" in msg


def test_crypto_error_unsupported_suite_does_not_leak_key() -> None:
    # MEDIUM (re-review): the suite token sits in field[1]. A body that puts the
    # key there (e.g. a doubled inline: with no real suite) makes the
    # "unsupported suite" error leak the key — so it must be structural only.
    body = f"1 inline:{_FAKE_KEY} inline:{_FAKE_KEY}"  # field[1] == the key
    with pytest.raises(SdpError) as exc_info:
        CryptoAttribute.parse(body)
    msg = str(exc_info.value)
    assert _FAKE_KEY not in msg
    assert "inline:" not in msg
    assert "suite" in msg  # structural fact: the suite is unsupported


def test_crypto_error_non_decimal_tag_does_not_leak_key() -> None:
    # MEDIUM (re-review): the tag token sits in field[0]. A body that puts the
    # key there (key, then a real suite, then the key) makes the "tag is not
    # decimal" error leak the key — so it must be structural only.
    body = f"inline:{_FAKE_KEY} AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}"
    with pytest.raises(SdpError) as exc_info:
        CryptoAttribute.parse(body)
    msg = str(exc_info.value)
    assert _FAKE_KEY not in msg
    assert "inline:" not in msg
    assert "tag" in msg  # structural fact: the tag is not decimal
