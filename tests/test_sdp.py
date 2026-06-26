"""Tests for hermes_voip.sdp — SDP offer/answer parse + build (RFC 4566/3264).

Fakes use the RFC 5737 documentation address range (``192.0.2.0/24``); no real
gateway address appears. Covers codec/rtpmap/fmtp parsing, media-security
detection (RTP/AVP vs RTP/SAVP + ``a=crypto`` SDES), codec negotiation, and a
build→parse round-trip.

Also covers the WebRTC SDP attributes (ADR-0016, PR-B):
- ``a=fingerprint:sha-256 <hex>`` and ``a=setup:actpass|active|passive``
  (DTLS-SRTP, RFC 5763/8122).
- ``a=ice-ufrag`` / ``a=ice-pwd`` and ``a=candidate`` (ICE, RFC 8839).
- ``a=rtcp-mux`` and the ``UDP/TLS/RTP/SAVPF`` media profile.
- ``build_webrtc_answer`` producing a SAVPF answer with our fingerprint/setup/ice.
- Mutual exclusion: a WebRTC (SAVPF/DTLS) m-line carries NO ``a=crypto`` and
  NO ``c=`` connection attribute (RFC 5763 §5).
- SDES/AVP path is fully unregressed (no cross-contamination).
"""

import base64

import pytest

from hermes_voip.sdp import (
    _SIP_DTLS_PROFILE,
    _SRTP_KEY_SALT_OCTETS,
    _SUPPORTED_CRYPTO_SUITES,
    AudioMedia,
    Codec,
    CryptoAttribute,
    Fingerprint,
    IceCandidate,
    MediaSecurity,
    SdpError,
    SessionDescription,
    SetupRole,
    _AudioAccumulator,
    _negotiate_answer_crypto,
    build_audio_answer,
    build_audio_offer,
    build_sip_dtls_answer,
    build_webrtc_answer,
    build_webrtc_offer,
    generate_answer_crypto,
    negotiate_audio,
    negotiate_media_security,
    negotiate_ptime,
    negotiate_rtcp_mux,
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


def test_parse_audio_direction_case_insensitively() -> None:
    offer = _OFFER_AVP.replace("a=sendrecv\r\n", "a=SENDONLY\r\n")

    sdp = SessionDescription.parse(offer)

    assert sdp.audio is not None
    assert sdp.audio.direction == "sendonly"


def test_parse_audio_maxptime_absent_is_none() -> None:
    """An offer without a=maxptime parses maxptime as None (ADR-0056 item 5)."""
    sdp = SessionDescription.parse(_OFFER_AVP)
    assert sdp.audio is not None
    assert sdp.audio.maxptime is None


def test_parse_audio_maxptime_when_present() -> None:
    """a=maxptime is parsed into AudioMedia.maxptime (ADR-0056 item 5)."""
    offer = _OFFER_AVP + "a=maxptime:40\r\n"
    sdp = SessionDescription.parse(offer)
    assert sdp.audio is not None
    assert sdp.audio.ptime == 20
    assert sdp.audio.maxptime == 40


def test_negotiate_ptime_honours_supported_offer_ptime() -> None:
    """The offer's ptime is used when the engine supports it (ADR-0056 item 5)."""
    assert negotiate_ptime(30, None, supported=(20, 30, 40), default=20) == 30


def test_negotiate_ptime_falls_back_to_default_for_unsupported() -> None:
    """An offered ptime the engine cannot frame falls back to the default."""
    # 25 ms is not a frame size the engine supports → default 20.
    assert negotiate_ptime(25, None, supported=(20, 30, 40), default=20) == 20


def test_negotiate_ptime_defaults_when_offer_omits_ptime() -> None:
    """No a=ptime in the offer → the default framing (RFC 3551's 20 ms)."""
    assert negotiate_ptime(None, None, supported=(20, 30, 40), default=20) == 20


def test_negotiate_ptime_respects_maxptime_ceiling() -> None:
    """An offered ptime above a=maxptime is not used (it would overrun the ceiling)."""
    # Offer asks 40 but caps at 30: 40 > 30, so fall back (default 20, ≤ 30 → ok).
    assert negotiate_ptime(40, 30, supported=(20, 30, 40), default=20) == 20


def test_negotiate_ptime_clamps_default_above_maxptime() -> None:
    """If even the default exceeds maxptime, pick the largest supported ≤ maxptime."""
    # maxptime 10 < default 20; the only supported value within the cap is 10.
    assert negotiate_ptime(None, 10, supported=(10, 20, 30), default=20) == 10


def test_negotiate_ptime_rejects_misconfiguration() -> None:
    """A misconfigured engine (bad supported/default) is a loud error, not a bad ptime.

    The docstring promises it never returns an unsupported/non-positive value, so a
    default the engine cannot frame — or an empty/non-positive supported set — must
    raise rather than silently emit an invalid ptime on the wire.
    """
    with pytest.raises(ValueError, match=r"non-empty|supported"):
        negotiate_ptime(20, None, supported=(), default=20)
    with pytest.raises(ValueError, match="positive"):
        negotiate_ptime(20, None, supported=(0, 20), default=20)
    with pytest.raises(ValueError, match="positive"):
        negotiate_ptime(None, None, supported=(20, 30), default=0)
    with pytest.raises(ValueError, match=r"supported|default"):
        negotiate_ptime(None, None, supported=(20, 30), default=25)


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


# ---------------------------------------------------------------------------
# G.722 wideband negotiation (ADR-0022): prefer G.722, fall back to G.711, and
# the engine must actually carry whatever was chosen (capability never drifts
# ahead of negotiation). This is the SUPPORTED menu the adapter advertises.
# ---------------------------------------------------------------------------

# The adapter's advertised order: G.722 first (wideband-preferred), then G.711,
# then DTMF. Kept here as a literal so the negotiation test is default-gate-safe
# (the adapter constant itself is exercised by the hermes-contract job).
_SUPPORTED = ("G722", "PCMU", "PCMA", "telephone-event")

_OFFER_G722_AND_G711 = (
    "v=0\r\n"
    "o=- 9 9 IN IP4 192.0.2.3\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.3\r\n"
    "t=0 0\r\n"
    "m=audio 40004 RTP/AVP 9 0 8 101\r\n"
    "a=rtpmap:9 G722/8000\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=sendrecv\r\n"
)

_OFFER_G711_ONLY = (
    "v=0\r\n"
    "o=- 9 9 IN IP4 192.0.2.3\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.3\r\n"
    "t=0 0\r\n"
    "m=audio 40006 RTP/AVP 0 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=sendrecv\r\n"
)


def test_g722_static_payload_9_parses_without_rtpmap() -> None:
    # RFC 3551 assigns G.722 the static payload type 9 at clock 8000; a peer may
    # offer it with no a=rtpmap. The parser's static table must resolve it.
    text = (
        "v=0\r\no=- 9 9 IN IP4 192.0.2.3\r\ns=-\r\nc=IN IP4 192.0.2.3\r\n"
        "t=0 0\r\nm=audio 40004 RTP/AVP 9\r\na=sendrecv\r\n"
    )
    audio = SessionDescription.parse(text).audio
    assert audio is not None
    by_pt = {c.payload_type: c for c in audio.codecs}
    assert by_pt[9].encoding == "G722"
    assert by_pt[9].clock_rate == 8000


def test_negotiate_prefers_g722_when_offered() -> None:
    # Offer [G722, PCMU, PCMA, DTMF] against our menu -> G722 wins (offer order
    # promotes it first), and the chosen voice codec is carriable by the engine.
    from hermes_voip.media.engine import Codec as EngineCodec  # noqa: PLC0415
    from hermes_voip.media.engine import codec_for_encoding  # noqa: PLC0415

    audio = SessionDescription.parse(_OFFER_G722_AND_G711).audio
    assert audio is not None
    chosen = negotiate_audio(audio, supported=_SUPPORTED)
    assert chosen[0].encoding == "G722"
    # The negotiated voice codec maps to a runnable engine codec.
    assert codec_for_encoding(chosen[0].encoding, chosen[0].clock_rate) is (
        EngineCodec.G722
    )


def test_negotiate_falls_back_to_g711_when_g722_absent() -> None:
    # Offer [PCMU, DTMF] only -> PCMU wins (G.722 not offered), engine carries it.
    from hermes_voip.media.engine import Codec as EngineCodec  # noqa: PLC0415
    from hermes_voip.media.engine import codec_for_encoding  # noqa: PLC0415

    audio = SessionDescription.parse(_OFFER_G711_ONLY).audio
    assert audio is not None
    chosen = negotiate_audio(audio, supported=_SUPPORTED)
    voice = [c for c in chosen if c.encoding.lower() != "telephone-event"]
    assert voice[0].encoding == "PCMU"
    assert codec_for_encoding(voice[0].encoding, voice[0].clock_rate) is (
        EngineCodec.PCMU
    )


# ---------------------------------------------------------------------------
# RFC 3264 §6.1 (ADR-0078): negotiate_audio orders the answer by OUR preference
# (the `supported` menu, first = most preferred), NOT by the offer's order. A
# gateway that lists a narrowband codec before our preferred wideband one must
# still be answered with the wideband codec we advertise. A stable sort by
# preference rank keeps already-preference-ordered offers byte-for-byte unchanged
# and keeps the relative offer order of two codecs sharing one rank.
# ---------------------------------------------------------------------------

_WEBRTC_MENU = ("opus", "PCMU", "PCMA", "telephone-event")

_OFFER_PCMU_BEFORE_OPUS = (
    "v=0\r\n"
    "o=- 9 9 IN IP4 192.0.2.3\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.3\r\n"
    "t=0 0\r\n"
    "m=audio 40008 RTP/AVP 0 111 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=sendrecv\r\n"
)

_OFFER_PCMU_BEFORE_G722 = (
    "v=0\r\n"
    "o=- 9 9 IN IP4 192.0.2.3\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.3\r\n"
    "t=0 0\r\n"
    "m=audio 40010 RTP/AVP 0 9 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:9 G722/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=sendrecv\r\n"
)


def test_negotiate_prefers_opus_when_offer_lists_pcmu_first() -> None:
    # Offer order is [PCMU, opus, DTMF] but OUR menu prefers opus first. The answer
    # must lead with opus (RFC 3264 §6.1: the answerer's preference wins), not the
    # narrowband PCMU the offer happened to list first.
    audio = SessionDescription.parse(_OFFER_PCMU_BEFORE_OPUS).audio
    assert audio is not None
    chosen = negotiate_audio(audio, supported=_WEBRTC_MENU)
    assert [c.encoding.lower() for c in chosen] == ["opus", "pcmu", "telephone-event"]


def test_negotiate_prefers_g722_when_offer_lists_pcmu_first() -> None:
    # Offer order is [PCMU, G722, DTMF] against the SDES menu (G722-preferred). The
    # answer must lead with G.722 (our preference), not the PCMU offered first —
    # otherwise a wideband-capable peer is silently answered narrowband.
    audio = SessionDescription.parse(_OFFER_PCMU_BEFORE_G722).audio
    assert audio is not None
    chosen = negotiate_audio(audio, supported=_SUPPORTED)
    assert [c.encoding.upper() for c in chosen] == ["G722", "PCMU", "TELEPHONE-EVENT"]


def test_negotiate_unchanged_when_offer_already_preference_ordered() -> None:
    # When the offer already matches our preference order the result is unchanged
    # (byte-for-byte: same Codec objects in the same order) — the reorder is a
    # stable no-op, so existing offer-order behaviour is preserved in that case.
    audio = SessionDescription.parse(_OFFER_G722_AND_G711).audio
    assert audio is not None
    chosen = negotiate_audio(audio, supported=_SUPPORTED)
    # Offer is [G722, PCMU, PCMA, DTMF]; our menu is (G722, PCMU, PCMA, DTMF) — the
    # reorder is a stable no-op, so the chosen Codec objects equal the offered ones
    # in the offered order (no copy, no shuffle).
    assert list(chosen) == list(audio.codecs)
    assert [c.encoding.upper() for c in chosen] == [
        "G722",
        "PCMU",
        "PCMA",
        "TELEPHONE-EVENT",
    ]


def test_negotiate_stable_within_same_preference_rank() -> None:
    # Two offered codecs that map to the SAME preference rank (here both PCMU,
    # offered under two payload types) keep their relative OFFER order — the sort
    # is stable, so the first-offered stays first.
    offer = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\nc=IN IP4 192.0.2.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 0 96 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\na=rtpmap:96 PCMU/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\na=sendrecv\r\n"
    )
    audio = offer.audio
    assert audio is not None
    chosen = negotiate_audio(audio, supported=("PCMU", "telephone-event"))
    # Both PCMU entries share rank 0; the first-offered (PT 0) precedes PT 96.
    pcmu = [c for c in chosen if c.encoding.upper() == "PCMU"]
    assert [c.payload_type for c in pcmu] == [0, 96]


def test_answer_orders_codecs_by_our_preference_not_offer() -> None:
    # End-to-end through build_audio_answer: offer lists PCMA before PCMU; our menu
    # prefers PCMU first, so the answer's m= line must list PCMU (0) before PCMA (8)
    # — RFC 3264 §6.1, the answerer expresses ITS preference.
    offer = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\nc=IN IP4 192.0.2.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 8 0\r\na=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
    )
    text = build_audio_answer(
        offer, local_address="192.0.2.20", port=42000, supported=("PCMU", "PCMA")
    )
    assert "m=audio 42000 RTP/AVP 0 8" in text


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


def test_answer_orders_codecs_by_supported_not_offer_order() -> None:
    # Offer lists PCMA before PCMU; the answer must follow OUR `supported` order
    # (PCMU before PCMA), not the offer's (ADR-0078, RFC 3264 §6.1: the answer
    # expresses the ANSWERER's preference). This is the same end-to-end behaviour
    # asserted by test_answer_orders_codecs_by_our_preference_not_offer above; kept
    # here under the W3 build_audio_answer suite as the regression anchor for the
    # superseded offer-order contract.
    offer = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\nc=IN IP4 192.0.2.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 8 0\r\na=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
    )
    text = build_audio_answer(
        offer, local_address="192.0.2.20", port=42000, supported=("PCMU", "PCMA")
    )
    assert "m=audio 42000 RTP/AVP 0 8" in text


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


def test_audio_accumulator_repr_hides_crypto_key_material() -> None:
    # LOW (full-path sweep): the internal parse accumulator stores raw a=crypto
    # bodies (which carry inline:<key||salt>). It is never returned, but a debug
    # log or a traceback local would expose its repr — so its crypto field must
    # be repr-suppressed, like the public CryptoAttribute/AudioMedia fields.
    acc = _AudioAccumulator()
    acc.add_attribute(f"crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}")
    assert acc.crypto == [f"1 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}"]  # retained
    text = repr(acc)
    assert _FAKE_KEY not in text  # ...but the key never leaks into the repr.
    assert "inline:" not in text


# ---------------------------------------------------------------------------
# ADR-0016 PR-B — WebRTC SDP attributes (DTLS-SRTP + ICE)
# RFC 5763/8122 (fingerprint + setup), RFC 8839 (ICE candidates), RFC 5761
# (rtcp-mux), profile UDP/TLS/RTP/SAVPF.  Fakes only; no network.
# ---------------------------------------------------------------------------

# Fake SHA-256 fingerprint hex (32 bytes x 2 hex chars + 31 colons = 95 chars).
# Format: two-hex-per-octet, colon-separated, upper-case (RFC 4572 §5).
_FAKE_FINGERPRINT = (
    "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:"
    "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89"
)
_FAKE_FINGERPRINT_ANSWER = (
    "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:"
    "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00"
)
# Fake ICE credentials — RFC 8839 §5.4: ufrag 4+, pwd 22+ chars of ice-char.
_FAKE_UFRAG = "offerUfrag01"
_FAKE_PWD = "offerPassword0123456789"
_FAKE_ANSWER_UFRAG = "answerUfrag01"
_FAKE_ANSWER_PWD = "answerPassword01234567"

# A realistic WebRTC offer from a gateway: SAVPF + fingerprint + setup:actpass +
# ice-ufrag/pwd + one host candidate + one srflx candidate + rtcp-mux.
_OFFER_SAVPF = (
    "v=0\r\n"
    "o=- 2000 2000 IN IP4 192.0.2.10\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=audio 49000 UDP/TLS/RTP/SAVPF 0 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    f"a=fingerprint:sha-256 {_FAKE_FINGERPRINT}\r\n"
    "a=setup:actpass\r\n"
    f"a=ice-ufrag:{_FAKE_UFRAG}\r\n"
    f"a=ice-pwd:{_FAKE_PWD}\r\n"
    "a=ice-options:ice2\r\n"
    "a=candidate:1 1 UDP 2130706431 192.0.2.10 49000 typ host\r\n"
    "a=candidate:2 1 UDP 1694498815 203.0.113.1 49000"
    " typ srflx raddr 192.0.2.10 rport 49000\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
)


# A realistic WebRTC offer as sent by an Asterisk-based gateway (e.g. the
# Grandstream UCM's embedded Asterisk WebRTC edge): a BUNDLE group with the
# DTLS-SRTP + ICE credentials at the **session level** (before the first m=
# line), shared across all bundled m-lines. RFC 8122 §5 (fingerprint) and RFC
# 8839 §4.2 (ice-ufrag/ice-pwd) both permit session-level placement; a
# session-level attribute applies to every media section that does not override
# it. The audio m-line itself carries ONLY its candidates/codecs — no
# fingerprint/ice of its own. A second `a=bundle-only` video m-line is included
# so the fixture exercises the multi-m-line BUNDLE shape too. Fakes only.
_OFFER_SAVPF_SESSION_LEVEL = (
    "v=0\r\n"
    "o=- 1217827275 1217827275 IN IP6 2001:db8::1\r\n"
    "s=Asterisk\r\n"
    "c=IN IP6 2001:db8::1\r\n"
    "t=0 0\r\n"
    "a=msid-semantic:WMS *\r\n"
    "a=group:BUNDLE 0 1\r\n"
    f"a=ice-ufrag:{_FAKE_UFRAG}\r\n"
    f"a=ice-pwd:{_FAKE_PWD}\r\n"
    "a=ice-options:trickle\r\n"
    # Gateways send the algorithm token upper-case (SHA-256); the parser
    # lower-cases it (RFC 4572 §5) — the fixture keeps the on-the-wire form.
    f"a=fingerprint:SHA-256 {_FAKE_FINGERPRINT}\r\n"
    "a=setup:actpass\r\n"
    "m=audio 19710 UDP/TLS/RTP/SAVPF 123 0 101\r\n"
    "a=candidate:1 1 UDP 2130706431 2001:db8::1 19710 typ host\r\n"
    "a=rtpmap:123 opus/48000/2\r\n"
    "a=fmtp:123 useinbandfec=1;usedtx=1\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=fmtp:101 0-15\r\n"
    "a=ptime:20\r\n"
    "a=sendrecv\r\n"
    "a=rtcp-mux\r\n"
    "a=mid:0\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 99\r\n"
    "a=bundle-only\r\n"
    "a=rtpmap:99 H264/90000\r\n"
    "a=inactive\r\n"
    "a=rtcp-mux\r\n"
    "a=mid:1\r\n"
)


# A WebRTC offer that carries ICE/DTLS at BOTH session and media level, with
# DIFFERENT values, so the parser's precedence rule (media overrides session,
# RFC 8839 §4.2) can be asserted unambiguously. Fakes only.
_OFFER_SAVPF_MEDIA_OVERRIDES_SESSION = (
    "v=0\r\n"
    "o=- 3000 3000 IN IP4 192.0.2.20\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    f"a=ice-ufrag:{_FAKE_UFRAG}\r\n"
    f"a=ice-pwd:{_FAKE_PWD}\r\n"
    f"a=fingerprint:sha-256 {_FAKE_FINGERPRINT}\r\n"
    "a=setup:actpass\r\n"
    "m=audio 49000 UDP/TLS/RTP/SAVPF 0 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    f"a=ice-ufrag:{_FAKE_ANSWER_UFRAG}\r\n"
    f"a=ice-pwd:{_FAKE_ANSWER_PWD}\r\n"
    f"a=fingerprint:sha-256 {_FAKE_FINGERPRINT_ANSWER}\r\n"
    "a=setup:active\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
)


# --- Fingerprint typed attribute ---


def test_fingerprint_parse_sha256() -> None:
    """Parse a valid sha-256 fingerprint attribute body."""
    fp = Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT}")
    assert fp.algorithm == "sha-256"
    assert fp.value == _FAKE_FINGERPRINT


def test_fingerprint_parse_rejects_missing_value() -> None:
    with pytest.raises(SdpError, match="fingerprint"):
        Fingerprint.parse("sha-256")


def test_fingerprint_parse_rejects_empty_body() -> None:
    with pytest.raises(SdpError, match="fingerprint"):
        Fingerprint.parse("")


def test_fingerprint_render_round_trip() -> None:
    fp = Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT}")
    assert fp.render() == f"sha-256 {_FAKE_FINGERPRINT}"


def test_fingerprint_algorithm_lowercased() -> None:
    # RFC 4572 allows any case; we normalise to lower for consistent matching.
    fp = Fingerprint.parse(f"SHA-256 {_FAKE_FINGERPRINT}")
    assert fp.algorithm == "sha-256"


# --- SetupRole typed attribute ---


def test_setup_role_all_values_accepted() -> None:
    for role_str in ("actpass", "active", "passive"):
        role = SetupRole.parse(role_str)
        assert role.value == role_str


def test_setup_role_rejects_unknown() -> None:
    with pytest.raises(SdpError, match="setup"):
        SetupRole.parse("holdconn")


def test_setup_role_rejects_empty() -> None:
    with pytest.raises(SdpError, match="setup"):
        SetupRole.parse("")


def test_setup_role_render() -> None:
    assert SetupRole.parse("actpass").render() == "actpass"
    assert SetupRole.parse("active").render() == "active"
    assert SetupRole.parse("passive").render() == "passive"


# --- IceCandidate typed attribute ---


def test_ice_candidate_parse_host() -> None:
    body = "1 1 UDP 2130706431 192.0.2.10 49000 typ host"
    cand = IceCandidate.parse(body)
    assert cand.foundation == "1"
    assert cand.component == 1
    assert cand.transport == "UDP"
    assert cand.priority == 2130706431
    assert cand.address == "192.0.2.10"
    assert cand.port == 49000
    assert cand.typ == "host"
    assert cand.raddr is None
    assert cand.rport is None


def test_ice_candidate_parse_srflx() -> None:
    body = "2 1 UDP 1694498815 203.0.113.1 49000 typ srflx raddr 192.0.2.10 rport 49000"
    cand = IceCandidate.parse(body)
    assert cand.foundation == "2"
    assert cand.typ == "srflx"
    assert cand.raddr == "192.0.2.10"
    assert cand.rport == 49000


def test_ice_candidate_parse_relay() -> None:
    # relay: candidate address 203.0.113.5:50000; base (raddr/rport) is the
    # server-reflexive addr at the TURN server: 203.0.113.1:49001 (RFC 8839 §5.1).
    body = "3 1 UDP 16777215 203.0.113.5 50000 typ relay raddr 203.0.113.1 rport 49001"
    cand = IceCandidate.parse(body)
    assert cand.typ == "relay"
    assert cand.address == "203.0.113.5"
    assert cand.port == 50000
    assert cand.raddr == "203.0.113.1"
    assert cand.rport == 49001


def test_ice_candidate_render_host_round_trip() -> None:
    body = "1 1 UDP 2130706431 192.0.2.10 49000 typ host"
    cand = IceCandidate.parse(body)
    assert cand.render() == body


def test_ice_candidate_render_srflx_round_trip() -> None:
    body = "2 1 UDP 1694498815 203.0.113.1 49000 typ srflx raddr 192.0.2.10 rport 49000"
    cand = IceCandidate.parse(body)
    assert cand.render() == body


def test_ice_candidate_rejects_truncated_body() -> None:
    with pytest.raises(SdpError, match="candidate"):
        IceCandidate.parse("1 1 UDP 2130706431 192.0.2.10")  # missing port + typ


def test_ice_candidate_rejects_bad_component() -> None:
    with pytest.raises(SdpError, match="candidate"):
        IceCandidate.parse("1 notint UDP 2130706431 192.0.2.10 49000 typ host")


def test_ice_candidate_rejects_bad_port() -> None:
    with pytest.raises(SdpError, match="candidate"):
        IceCandidate.parse("1 1 UDP 2130706431 192.0.2.10 99999 typ host")


# --- AudioMedia.is_webrtc property ---


def test_audio_media_is_webrtc_savpf_profile() -> None:
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert sdp.audio.is_webrtc is True


def test_audio_media_is_not_webrtc_for_savp() -> None:
    sdp = SessionDescription.parse(_OFFER_SAVP)
    assert sdp.audio is not None
    assert sdp.audio.is_webrtc is False


def test_audio_media_is_not_webrtc_for_avp() -> None:
    sdp = SessionDescription.parse(_OFFER_AVP)
    assert sdp.audio is not None
    assert sdp.audio.is_webrtc is False


# --- Full WebRTC offer parsing ---


def test_parse_webrtc_offer_fingerprint() -> None:
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert sdp.audio.fingerprint is not None
    assert sdp.audio.fingerprint.algorithm == "sha-256"
    assert sdp.audio.fingerprint.value == _FAKE_FINGERPRINT


def test_parse_webrtc_offer_setup_role() -> None:
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert sdp.audio.setup is not None
    assert sdp.audio.setup.value == "actpass"


def test_parse_webrtc_offer_ice_credentials() -> None:
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert sdp.audio.ice_ufrag == _FAKE_UFRAG
    assert sdp.audio.ice_pwd == _FAKE_PWD


def test_parse_webrtc_offer_candidates() -> None:
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert len(sdp.audio.ice_candidates) == 2
    host = sdp.audio.ice_candidates[0]
    srflx = sdp.audio.ice_candidates[1]
    assert host.typ == "host"
    assert host.address == "192.0.2.10"
    assert srflx.typ == "srflx"
    assert srflx.raddr == "192.0.2.10"


def test_parse_webrtc_offer_rtcp_mux() -> None:
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert sdp.audio.rtcp_mux is True


def test_parse_webrtc_offer_protocol() -> None:
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert sdp.audio.protocol == "UDP/TLS/RTP/SAVPF"


def test_parse_webrtc_offer_has_no_sdes_crypto() -> None:
    """A WebRTC SAVPF offer MUST NOT carry a=crypto (RFC 8827 §6.5 / ADR-0016)."""
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert sdp.audio.crypto == ()
    assert sdp.audio.crypto_attrs == ()


def test_parse_webrtc_session_level_fingerprint() -> None:
    """Session-level a=fingerprint applies to the bundled audio (RFC 8122 §5).

    Regression: an Asterisk/UCM BUNDLE offer puts the DTLS fingerprint at the
    session level; the parser must surface it on the audio media (it was being
    dropped, producing a spurious 488 'missing fingerprint' on the live gateway).
    """
    sdp = SessionDescription.parse(_OFFER_SAVPF_SESSION_LEVEL)
    assert sdp.audio is not None
    assert sdp.audio.is_webrtc is True
    assert sdp.audio.fingerprint is not None
    assert sdp.audio.fingerprint.algorithm == "sha-256"
    assert sdp.audio.fingerprint.value == _FAKE_FINGERPRINT


def test_parse_webrtc_session_level_ice_credentials() -> None:
    """Session-level a=ice-ufrag/a=ice-pwd apply to the audio (RFC 8839 §4.2)."""
    sdp = SessionDescription.parse(_OFFER_SAVPF_SESSION_LEVEL)
    assert sdp.audio is not None
    assert sdp.audio.ice_ufrag == _FAKE_UFRAG
    assert sdp.audio.ice_pwd == _FAKE_PWD


def test_parse_webrtc_session_level_setup_and_options() -> None:
    """Session-level a=setup and a=ice-options apply to the audio media."""
    sdp = SessionDescription.parse(_OFFER_SAVPF_SESSION_LEVEL)
    assert sdp.audio is not None
    assert sdp.audio.setup is not None
    assert sdp.audio.setup.value == "actpass"
    assert "trickle" in sdp.audio.ice_options


def test_parse_webrtc_session_level_keeps_media_candidates() -> None:
    """Media-level candidates are still captured alongside session-level creds."""
    sdp = SessionDescription.parse(_OFFER_SAVPF_SESSION_LEVEL)
    assert sdp.audio is not None
    assert len(sdp.audio.ice_candidates) == 1
    assert sdp.audio.ice_candidates[0].typ == "host"


def test_parse_webrtc_media_level_overrides_session_level() -> None:
    """A media-level attribute wins over a session-level one (RFC 8839 §4.2)."""
    sdp = SessionDescription.parse(_OFFER_SAVPF_MEDIA_OVERRIDES_SESSION)
    assert sdp.audio is not None
    assert sdp.audio.ice_ufrag == _FAKE_ANSWER_UFRAG
    assert sdp.audio.ice_pwd == _FAKE_ANSWER_PWD
    assert sdp.audio.fingerprint is not None
    assert sdp.audio.fingerprint.value == _FAKE_FINGERPRINT_ANSWER
    assert sdp.audio.setup is not None
    assert sdp.audio.setup.value == "active"


def test_parse_sdes_offer_has_no_webrtc_attrs() -> None:
    """An RTP/SAVP SDES offer MUST NOT expose fingerprint/setup/ice/rtcp-mux."""
    sdp = SessionDescription.parse(_OFFER_SAVP)
    assert sdp.audio is not None
    assert sdp.audio.fingerprint is None
    assert sdp.audio.setup is None
    assert sdp.audio.ice_ufrag is None
    assert sdp.audio.ice_pwd is None
    assert sdp.audio.ice_candidates == ()
    assert sdp.audio.rtcp_mux is False


# --- build_webrtc_answer ---


def _make_host_candidate(  # noqa: PLR0913 - ICE candidate fields are independent
    foundation: str = "1",
    component: int = 1,
    transport: str = "UDP",
    priority: int = 2130706431,
    address: str = "192.0.2.20",
    port: int = 42000,
) -> IceCandidate:
    return IceCandidate(
        foundation=foundation,
        component=component,
        transport=transport,
        priority=priority,
        address=address,
        port=port,
        typ="host",
        raddr=None,
        rport=None,
    )


_ANSWER_CANDIDATE = _make_host_candidate()


def test_build_webrtc_answer_profile() -> None:
    offer = SessionDescription.parse(_OFFER_SAVPF)
    text = build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )
    assert "UDP/TLS/RTP/SAVPF" in text


def test_build_webrtc_answer_fingerprint_present() -> None:
    offer = SessionDescription.parse(_OFFER_SAVPF)
    text = build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )
    assert f"a=fingerprint:sha-256 {_FAKE_FINGERPRINT_ANSWER}" in text


def test_build_webrtc_answer_setup_present() -> None:
    offer = SessionDescription.parse(_OFFER_SAVPF)
    text = build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )
    assert "a=setup:active" in text


def test_build_webrtc_answer_ice_credentials_present() -> None:
    offer = SessionDescription.parse(_OFFER_SAVPF)
    text = build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )
    assert f"a=ice-ufrag:{_FAKE_ANSWER_UFRAG}" in text
    assert f"a=ice-pwd:{_FAKE_ANSWER_PWD}" in text


def test_build_webrtc_answer_candidate_present() -> None:
    offer = SessionDescription.parse(_OFFER_SAVPF)
    text = build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )
    assert "a=candidate:1 1 UDP 2130706431 192.0.2.20 42000 typ host" in text


def test_build_webrtc_answer_rtcp_mux_present() -> None:
    offer = SessionDescription.parse(_OFFER_SAVPF)
    text = build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )
    assert "a=rtcp-mux" in text


# --- trickle-ICE SDP primitives (ADR-0034) ---


def _build_answer() -> str:
    """Build a WebRTC answer from the canonical SAVPF offer (test helper)."""
    offer = SessionDescription.parse(_OFFER_SAVPF)
    return build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )


def test_build_webrtc_answer_advertises_trickle_and_ice2() -> None:
    """ADR-0034: the answer declares a=ice-options:trickle ice2 (RFC 8838 §4.1)."""
    text = _build_answer()
    assert "a=ice-options:trickle ice2" in text


def test_build_webrtc_answer_emits_end_of_candidates() -> None:
    """ADR-0034: a full-candidate answer ends with a=end-of-candidates (RFC 8838 §8.2).

    We send our complete candidate set, then mark end-of-candidates — the
    half-trickle degenerate case that interoperates with both classic and
    trickling peers.
    """
    text = _build_answer()
    assert "a=end-of-candidates" in text


def test_build_webrtc_answer_end_of_candidates_after_candidates() -> None:
    """end-of-candidates appears AFTER the a=candidate lines it terminates."""
    text = _build_answer()
    cand_idx = text.index("a=candidate:")
    eoc_idx = text.index("a=end-of-candidates")
    assert cand_idx < eoc_idx


def test_parse_offer_ice_options_trickle() -> None:
    """ADR-0034: a peer offer's a=ice-options trickle/ice2 is parsed + exposed."""
    offer_with_trickle = _OFFER_SAVPF.replace(
        "a=ice-options:ice2\r\n", "a=ice-options:trickle ice2\r\n"
    )
    sdp = SessionDescription.parse(offer_with_trickle)
    assert sdp.audio is not None
    assert "trickle" in sdp.audio.ice_options
    assert "ice2" in sdp.audio.ice_options
    assert sdp.audio.is_trickle is True


def test_parse_offer_ice_options_non_trickle() -> None:
    """A plain ice2-only offer is parsed but is_trickle is False."""
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert sdp.audio.ice_options == ("ice2",)
    assert sdp.audio.is_trickle is False


def test_parse_offer_end_of_candidates_present() -> None:
    """ADR-0034: a peer offer's a=end-of-candidates is parsed into a flag."""
    offer_eoc = _OFFER_SAVPF.replace(
        "a=rtcp-mux\r\n", "a=rtcp-mux\r\na=end-of-candidates\r\n"
    )
    sdp = SessionDescription.parse(offer_eoc)
    assert sdp.audio is not None
    assert sdp.audio.end_of_candidates is True


def test_parse_offer_end_of_candidates_absent_defaults_false() -> None:
    """No a=end-of-candidates => the flag defaults False."""
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert sdp.audio.end_of_candidates is False


def test_parse_sdes_offer_has_no_trickle_attrs() -> None:
    """An SDES offer exposes no ice-options / end-of-candidates (defaults)."""
    sdp = SessionDescription.parse(_OFFER_SAVP)
    assert sdp.audio is not None
    assert sdp.audio.ice_options == ()
    assert sdp.audio.is_trickle is False
    assert sdp.audio.end_of_candidates is False


def test_build_webrtc_answer_no_sdes_crypto() -> None:
    """RFC 8827 §6.5 / ADR-0016: WebRTC answer MUST NOT carry a=crypto."""
    offer = SessionDescription.parse(_OFFER_SAVPF)
    text = build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )
    assert "a=crypto" not in text


def test_build_webrtc_answer_no_connection_attribute() -> None:
    """RFC 5763 §5: MUST NOT use the connection attribute on DTLS-SRTP m-lines."""
    offer = SessionDescription.parse(_OFFER_SAVPF)
    text = build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )
    # RFC 5763 §5 forbids the RFC 4145 a=connection attribute (not the c= line).
    assert "a=connection:" not in text


def test_build_webrtc_answer_no_session_c_line() -> None:
    """RFC 5763 §5: The c= connection-address MUST NOT be used on DTLS-SRTP m-lines.

    ADR-0016 records: 'The endpoint MUST NOT use the connection attribute defined
    in [RFC4145]'.  For WebRTC we also suppress the session-level c= line so that
    the SDP body contains no connection address at all (address is conveyed by ICE
    candidates instead).
    """
    offer = SessionDescription.parse(_OFFER_SAVPF)
    text = build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )
    assert "c=IN" not in text


def test_build_webrtc_answer_round_trip_parse() -> None:
    """A built WebRTC answer parses back to the expected typed fields."""
    offer = SessionDescription.parse(_OFFER_SAVPF)
    text = build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )
    parsed = SessionDescription.parse(text)
    assert parsed.audio is not None
    audio = parsed.audio
    assert audio.protocol == "UDP/TLS/RTP/SAVPF"
    assert audio.is_webrtc is True
    assert audio.fingerprint is not None
    assert audio.fingerprint.value == _FAKE_FINGERPRINT_ANSWER
    assert audio.setup is not None
    assert audio.setup.value == "active"
    assert audio.ice_ufrag == _FAKE_ANSWER_UFRAG
    assert audio.ice_pwd == _FAKE_ANSWER_PWD
    assert len(audio.ice_candidates) == 1
    assert audio.ice_candidates[0].typ == "host"
    assert audio.rtcp_mux is True
    assert audio.crypto == ()
    assert audio.crypto_attrs == ()


def test_build_webrtc_answer_rejects_non_savpf_offer() -> None:
    """build_webrtc_answer requires a UDP/TLS/RTP/SAVPF offer."""
    offer = SessionDescription.parse(_OFFER_SAVP)
    with pytest.raises(SdpError, match="WebRTC"):
        build_webrtc_answer(
            offer,
            local_address="192.0.2.20",
            port=42000,
            supported=("PCMU",),
            fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
            setup=SetupRole.parse("active"),
            ice_ufrag=_FAKE_ANSWER_UFRAG,
            ice_pwd=_FAKE_ANSWER_PWD,
            ice_candidates=(_ANSWER_CANDIDATE,),
        )


def test_build_webrtc_answer_requires_audio_in_offer() -> None:
    video_only = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.3\r\ns=-\r\nt=0 0\r\n"
        "m=video 50000 RTP/AVP 96\r\n"
    )
    with pytest.raises(SdpError, match="audio"):
        build_webrtc_answer(
            video_only,
            local_address="192.0.2.20",
            port=42000,
            supported=("PCMU",),
            fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
            setup=SetupRole.parse("active"),
            ice_ufrag=_FAKE_ANSWER_UFRAG,
            ice_pwd=_FAKE_ANSWER_PWD,
            ice_candidates=(_ANSWER_CANDIDATE,),
        )


def test_build_webrtc_answer_rejects_actpass_setup() -> None:
    # RFC 5763: an answerer MUST NOT offer actpass; it must choose active or passive.
    """Build rejects setup=actpass in the answer (RFC 5763 §5)."""
    offer = SessionDescription.parse(_OFFER_SAVPF)
    with pytest.raises(SdpError, match="actpass"):
        build_webrtc_answer(
            offer,
            local_address="192.0.2.20",
            port=42000,
            supported=("PCMU",),
            fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
            setup=SetupRole.parse("actpass"),  # invalid for an answerer
            ice_ufrag=_FAKE_ANSWER_UFRAG,
            ice_pwd=_FAKE_ANSWER_PWD,
            ice_candidates=(_ANSWER_CANDIDATE,),
        )


def test_build_webrtc_answer_multiple_candidates() -> None:
    """Multiple candidates are all rendered in the answer."""
    host = _make_host_candidate()
    srflx = IceCandidate(
        foundation="2",
        component=1,
        transport="UDP",
        priority=1694498815,
        address="203.0.113.20",
        port=42000,
        typ="srflx",
        raddr="192.0.2.20",
        rport=42000,
    )
    offer = SessionDescription.parse(_OFFER_SAVPF)
    text = build_webrtc_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(host, srflx),
    )
    assert text.count("a=candidate:") == 2
    assert "typ host" in text
    assert "typ srflx" in text


# --- SDES / AVP path regression (no cross-contamination) ---


def test_sdes_path_unchanged_build_offer_avp() -> None:
    """The plain AVP offer builder carries no DTLS/ICE WebRTC attributes.

    rtcp-mux is now a deliberate SDES-path offer attribute (RFC 5761, ADR-0061) —
    offered by default — so it is NOT in this "no WebRTC leakage" exclusion list;
    its presence is asserted by ``test_build_audio_offer_includes_rtcp_mux_by_default``.
    """
    codecs = (Codec(0, "PCMU", 8000),)
    text = build_audio_offer(local_address="192.0.2.10", port=41000, codecs=codecs)
    assert "RTP/AVP" in text
    assert "fingerprint" not in text
    assert "ice-ufrag" not in text
    assert "a=setup" not in text


def test_sdes_path_unchanged_build_offer_savp() -> None:
    """The existing SAVP+SDES offer builder is unaffected by WebRTC additions."""
    codecs = (Codec(0, "PCMU", 8000),)
    text = build_audio_offer(
        local_address="192.0.2.10", port=41000, codecs=codecs, crypto=_FAKE_CRYPTO
    )
    assert "RTP/SAVP" in text
    assert "a=crypto:" in text
    assert "fingerprint" not in text
    assert "ice-ufrag" not in text
    assert "UDP/TLS" not in text


def test_sdes_path_unchanged_build_answer_savp() -> None:
    """The existing SAVP+SDES answer builder is unaffected by WebRTC additions."""
    offer = SessionDescription.parse(_OFFER_SAVP)
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        crypto=_FAKE_ANSWER_CRYPTO,
    )
    assert "RTP/SAVP" in text
    assert "a=crypto:" in text
    assert "fingerprint" not in text
    assert "UDP/TLS" not in text


# ---------------------------------------------------------------------------
# build_webrtc_offer (ADR-0049): the outbound WebRTC UAC offer body.
# Mirrors build_webrtc_answer's SAVPF/DTLS/ICE shape but is offerer-driven:
# our own codec menu (not the peer's), and a concrete a=setup we choose.
# ---------------------------------------------------------------------------

_OFFER_OPUS = Codec(payload_type=111, encoding="opus", clock_rate=48000, channels=2)
_OFFER_OPUS_FMTP = Codec(
    payload_type=111,
    encoding="opus",
    clock_rate=48000,
    channels=2,
    fmtp="minptime=10;useinbandfec=1",
)


def _build_offer(
    setup: str = "active", *, codecs: tuple[Codec, ...] | None = None
) -> str:
    """Build a WebRTC offer with our fingerprint/setup/ICE (test helper)."""
    return build_webrtc_offer(
        local_address="192.0.2.20",
        port=9,
        codecs=codecs if codecs is not None else (_OFFER_OPUS_FMTP,),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT}"),
        setup=SetupRole.parse(setup),
        ice_ufrag=_FAKE_ANSWER_UFRAG,
        ice_pwd=_FAKE_ANSWER_PWD,
        ice_candidates=(_ANSWER_CANDIDATE,),
    )


def test_build_webrtc_offer_profile_is_savpf() -> None:
    """The outbound WebRTC offer uses the UDP/TLS/RTP/SAVPF profile (RFC 8827)."""
    assert "m=audio 9 UDP/TLS/RTP/SAVPF 111" in _build_offer()


def test_build_webrtc_offer_carries_our_dtls_keying() -> None:
    """The offer carries OUR fingerprint + a concrete a=setup (RFC 5763 §5)."""
    text = _build_offer("active")
    assert f"a=fingerprint:sha-256 {_FAKE_FINGERPRINT}" in text
    assert "a=setup:active" in text


def test_build_webrtc_offer_carries_ice_creds_and_candidates() -> None:
    """The offer carries OUR ICE ufrag/pwd + candidates (RFC 8839)."""
    text = _build_offer()
    assert f"a=ice-ufrag:{_FAKE_ANSWER_UFRAG}" in text
    assert f"a=ice-pwd:{_FAKE_ANSWER_PWD}" in text
    assert "a=candidate:1 1 UDP 2130706431 192.0.2.20 42000 typ host" in text
    assert "a=rtcp-mux" in text


def test_build_webrtc_offer_advertises_opus() -> None:
    """An Opus codec appears in the WebRTC offer's rtpmap (the WebRTC audio codec)."""
    text = _build_offer()
    assert "a=rtpmap:111 opus/48000/2" in text
    assert "a=fmtp:111 minptime=10;useinbandfec=1" in text


def test_build_webrtc_offer_no_sdes_crypto_or_connection() -> None:
    """RFC 8827 §6.5 / RFC 5763 §5: no a=crypto and no c= on a WebRTC m-line."""
    text = _build_offer()
    assert "a=crypto" not in text
    assert "\r\nc=" not in text


def test_build_webrtc_offer_actpass_allowed() -> None:
    """An offerer MAY offer actpass (RFC 5763 §5) — unlike an answerer."""
    text = _build_offer("actpass")
    assert "a=setup:actpass" in text


def test_build_webrtc_offer_round_trips_through_the_parser() -> None:
    """The offer parses back as a WebRTC offer the answerer can consume."""
    parsed = SessionDescription.parse(_build_offer())
    assert parsed.audio is not None
    assert parsed.audio.is_webrtc
    assert parsed.audio.fingerprint is not None
    assert parsed.audio.setup is not None
    assert parsed.audio.setup.value == "active"
    assert parsed.audio.ice_ufrag == _FAKE_ANSWER_UFRAG
    assert parsed.audio.codecs[0].encoding.lower() == "opus"


# ---------------------------------------------------------------------------
# generate_answer_crypto (ADR-0053 Stage 1) — invariants that close a blind
# cross-vendor review's two MAJOR findings (codex reviewed diff-only; both
# premises are unreachable, proven here so they cannot silently regress).
# ---------------------------------------------------------------------------


def test_every_supported_crypto_suite_has_a_key_salt_length() -> None:
    """generate_answer_crypto looks up ``_SRTP_KEY_SALT_OCTETS[accepted.suite]``.

    A ``CryptoAttribute`` can only hold a suite in ``_SUPPORTED_CRYPTO_SUITES``
    (validated at construction), so every such suite MUST have a key||salt length
    entry — otherwise an accepted offer crypto could raise a raw ``KeyError``.
    This guards the two maps against drift (e.g. adding a suite to one only).
    """
    assert _SRTP_KEY_SALT_OCTETS.keys() >= _SUPPORTED_CRYPTO_SUITES


def test_crypto_attribute_rejects_unsupported_suite() -> None:
    """Unsupported suites cannot reach generate_answer_crypto.

    A CryptoAttribute validates its suite at construction, so no unsupported-suite
    attribute (and hence no KeyError lookup path) can ever exist.
    """
    with pytest.raises(SdpError, match="unsupported crypto suite"):
        CryptoAttribute(
            tag=1, suite="AES_CM_256_HMAC_SHA1_80", key_params=f"inline:{_FAKE_KEY}"
        )


def test_answer_selects_supported_crypto_when_unsupported_listed_first() -> None:
    """An unsupported-first a=crypto offer is answered with the SUPPORTED suite.

    RFC 4568 §6.1 directionality is by construction, not accident.
    The unsupported line is excluded from ``crypto_attrs`` during parse, so
    ``crypto_attrs[0]`` — used for the inbound key, the generated answer key, and
    the echoed tag/suite — is the validated accepted crypto. The answer therefore
    never carries the unsupported suite, and the answer key's suite matches.
    """
    offer_sdp = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 192.0.2.1\r\n"
        "s=-\r\n"
        "c=IN IP4 192.0.2.1\r\n"
        "t=0 0\r\n"
        "m=audio 40000 RTP/SAVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        f"a=crypto:1 AES_CM_256_HMAC_SHA1_80 inline:{_FAKE_KEY}\r\n"
        f"a=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:{_FAKE_KEY}\r\n"
        "a=sendrecv\r\n"
    )
    sdp = SessionDescription.parse(offer_sdp)
    assert sdp.audio is not None
    # The unsupported AES_CM_256 line (tag 1) is filtered; the accepted is tag 2.
    accepted = sdp.audio.crypto_attrs[0]
    assert accepted.suite == "AES_CM_128_HMAC_SHA1_80"
    assert accepted.tag == 2

    answer = generate_answer_crypto(accepted)
    assert answer.suite == "AES_CM_128_HMAC_SHA1_80"
    assert answer.tag == 2

    text = build_audio_answer(
        sdp,
        local_address="192.0.2.99",
        port=40002,
        supported=["PCMU"],
        session_id=1,
        crypto=answer,
    )
    assert "AES_CM_256" not in text
    assert "a=crypto:2 AES_CM_128_HMAC_SHA1_80 inline:" in text


def test_generate_answer_crypto_key_is_exactly_the_suite_length() -> None:
    """The minted answer key||salt decodes to exactly the suite's octet count.

    Security invariant (RFC 4568 §6.2): AES_CM_128_HMAC_SHA1_{80,32} need a
    16-octet master key + 14-octet master salt = 30 octets. A short key would
    leave salt/key bytes zero — a catastrophically weak SRTP context. This pins
    the generated length so a mutation of ``secrets.token_bytes(octets)`` to a
    wrong-length (or constant-length) value is caught, not silently accepted.
    """
    accepted = CryptoAttribute(
        tag=4, suite="AES_CM_128_HMAC_SHA1_80", key_params=f"inline:{_FAKE_KEY}"
    )
    answer = generate_answer_crypto(accepted)
    key_b64 = answer.key_params[len("inline:") :]
    decoded = base64.b64decode(key_b64, validate=True)
    assert len(decoded) == _SRTP_KEY_SALT_OCTETS[accepted.suite]
    assert len(decoded) == 30  # the literal RFC 4568 §6.2 key||salt length


def test_generate_answer_crypto_is_random_per_call() -> None:
    """Two calls mint DIFFERENT keys — the master key is per-call random.

    Security invariant: a static/reused master key would let an attacker who
    recovers one call's key decrypt every call (and breaks the SRTP keystream
    uniqueness assumption). This catches a mutation of ``secrets.token_bytes`` to
    a deterministic value (e.g. ``bytes(octets)``), which every other test passes.
    """
    accepted = CryptoAttribute(
        tag=1, suite="AES_CM_128_HMAC_SHA1_80", key_params=f"inline:{_FAKE_KEY}"
    )
    first = generate_answer_crypto(accepted)
    second = generate_answer_crypto(accepted)
    assert first.key_params != second.key_params


# --- RTCP-mux negotiation (RFC 5761) on the SDES/plain-RTP audio path ---


def _avp_offer_with(*, rtcp_mux: bool) -> str:
    """An RTP/AVP audio offer, optionally carrying ``a=rtcp-mux`` (RFC 5761)."""
    lines = [
        "v=0",
        "o=- 9000 9000 IN IP4 192.0.2.1",
        "s=-",
        "c=IN IP4 192.0.2.1",
        "t=0 0",
        "m=audio 40000 RTP/AVP 0 101",
        "a=rtpmap:0 PCMU/8000",
        "a=rtpmap:101 telephone-event/8000",
    ]
    if rtcp_mux:
        lines.append("a=rtcp-mux")
    lines.append("a=sendrecv")
    return "\r\n".join(lines) + "\r\n"


def test_parse_audio_offer_detects_rtcp_mux() -> None:
    """An RTP/AVP (non-WebRTC) audio offer's ``a=rtcp-mux`` is parsed (RFC 5761)."""
    offer = SessionDescription.parse(_avp_offer_with(rtcp_mux=True))
    assert offer.audio is not None
    assert offer.audio.rtcp_mux is True


def test_parse_audio_offer_without_rtcp_mux() -> None:
    """No ``a=rtcp-mux`` in the offer parses to ``rtcp_mux=False`` (default)."""
    offer = SessionDescription.parse(_avp_offer_with(rtcp_mux=False))
    assert offer.audio is not None
    assert offer.audio.rtcp_mux is False


def test_negotiate_rtcp_mux_true_when_offered() -> None:
    """RFC 5761 §5.1.1: we agree to mux iff the offer requested it."""
    offer = SessionDescription.parse(_avp_offer_with(rtcp_mux=True))
    assert offer.audio is not None
    assert negotiate_rtcp_mux(offer.audio) is True


def test_negotiate_rtcp_mux_false_when_not_offered() -> None:
    """An offer without ``a=rtcp-mux`` MUST NOT be muxed (RFC 5761 §5.1.1)."""
    offer = SessionDescription.parse(_avp_offer_with(rtcp_mux=False))
    assert offer.audio is not None
    assert negotiate_rtcp_mux(offer.audio) is False


def test_build_audio_answer_emits_rtcp_mux_when_offered() -> None:
    """The SDES answer carries ``a=rtcp-mux`` when the offer requested it."""
    offer = SessionDescription.parse(_avp_offer_with(rtcp_mux=True))
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
    )
    assert "a=rtcp-mux\r\n" in text
    # And the emitted answer re-parses with the flag set (round-trip).
    parsed = SessionDescription.parse(text)
    assert parsed.audio is not None
    assert parsed.audio.rtcp_mux is True


def test_build_audio_answer_omits_rtcp_mux_when_not_offered() -> None:
    """The SDES answer MUST NOT mux when the offer did not (RFC 5761 §5.1.1)."""
    offer = SessionDescription.parse(_avp_offer_with(rtcp_mux=False))
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
    )
    assert "a=rtcp-mux" not in text


def test_build_audio_offer_includes_rtcp_mux_by_default() -> None:
    """We OFFER rtcp-mux by default (RFC 5761 §5.1.1 — the offerer may request it)."""
    text = build_audio_offer(
        local_address="192.0.2.10",
        port=41000,
        codecs=(Codec(0, "PCMU", 8000),),
    )
    assert "a=rtcp-mux\r\n" in text
    parsed = SessionDescription.parse(text)
    assert parsed.audio is not None
    assert parsed.audio.rtcp_mux is True


def test_build_audio_offer_can_suppress_rtcp_mux() -> None:
    """``rtcp_mux=False`` builds an offer with no ``a=rtcp-mux`` line."""
    text = build_audio_offer(
        local_address="192.0.2.10",
        port=41000,
        codecs=(Codec(0, "PCMU", 8000),),
        rtcp_mux=False,
    )
    assert "a=rtcp-mux" not in text


def test_audio_answer_rtcp_mux_round_trips_with_crypto() -> None:
    """rtcp-mux and SDES crypto coexist in the answer (RFC 5761 + RFC 4568)."""
    secure_mux_offer = (
        "v=0\r\no=- 9 9 IN IP4 192.0.2.1\r\ns=-\r\nc=IN IP4 192.0.2.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/SAVP 0 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
        f"a=crypto:1 {_FAKE_CRYPTO}\r\na=rtcp-mux\r\na=sendrecv\r\n"
    )
    offer = SessionDescription.parse(secure_mux_offer)
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        crypto=_FAKE_ANSWER_CRYPTO,
    )
    parsed = SessionDescription.parse(text)
    assert parsed.audio is not None
    assert parsed.audio.rtcp_mux is True
    assert parsed.audio.protocol == "RTP/SAVP"
    assert text.count("a=crypto:") == 1


# ===========================================================================
# SIP DTLS-SRTP (UDP/TLS/RTP/SAVP, no ICE) — ADR-0053 Stage 2
#
# A non-WebRTC DTLS-SRTP offer: UDP/TLS/RTP/SAVP + a=fingerprint + a=setup +
# a=rtcp-mux, but UNLIKE WebRTC it KEEPS its c=/port and carries NO ICE and NO
# a=crypto. The fingerprint is bound by the (real-CA-verified) signalling TLS.
# ===========================================================================

_OFFER_SIP_DTLS = (
    "v=0\r\n"
    "o=- 3000 3000 IN IP4 192.0.2.30\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.30\r\n"
    "t=0 0\r\n"
    "m=audio 41000 UDP/TLS/RTP/SAVP 0 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    f"a=fingerprint:sha-256 {_FAKE_FINGERPRINT}\r\n"
    "a=setup:actpass\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
)

# A UDP/TLS/RTP/SAVP m-line that is missing the a=fingerprint: it MUST NOT be
# treated as a usable DTLS-SRTP offer (RFC 5763 §5 requires the fingerprint).
_OFFER_SIP_DTLS_NO_FP = (
    "v=0\r\n"
    "o=- 3000 3000 IN IP4 192.0.2.30\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.30\r\n"
    "t=0 0\r\n"
    "m=audio 41000 UDP/TLS/RTP/SAVP 0 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=setup:actpass\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
)


# --- AudioMedia.is_sip_dtls detection ---


def test_sip_dtls_profile_constant() -> None:
    """The SIP DTLS-SRTP profile is UDP/TLS/RTP/SAVP (RFC 5764 §8), no AVPF F."""
    assert _SIP_DTLS_PROFILE == "UDP/TLS/RTP/SAVP"


def test_parse_sip_dtls_offer_is_sip_dtls() -> None:
    """A UDP/TLS/RTP/SAVP m-line with a=fingerprint is recognised as SIP-DTLS."""
    sdp = SessionDescription.parse(_OFFER_SIP_DTLS)
    assert sdp.audio is not None
    assert sdp.audio.is_sip_dtls is True
    # It is SRTP-secured but NOT WebRTC (no SAVPF feedback profile, no ICE).
    assert sdp.audio.is_srtp is True
    assert sdp.audio.is_webrtc is False


def test_parse_sip_dtls_offer_keeps_connection_address() -> None:
    """Unlike WebRTC, a SIP-DTLS m-line keeps its c= connection address."""
    sdp = SessionDescription.parse(_OFFER_SIP_DTLS)
    assert sdp.audio is not None
    assert sdp.audio.connection_address == "192.0.2.30"


def test_sip_dtls_requires_fingerprint() -> None:
    """UDP/TLS/RTP/SAVP without a=fingerprint is NOT a usable DTLS-SRTP offer."""
    sdp = SessionDescription.parse(_OFFER_SIP_DTLS_NO_FP)
    assert sdp.audio is not None
    assert sdp.audio.fingerprint is None
    assert sdp.audio.is_sip_dtls is False


def test_webrtc_offer_is_not_sip_dtls() -> None:
    """A WebRTC SAVPF offer is is_webrtc, never is_sip_dtls (distinct profiles)."""
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert sdp.audio.is_webrtc is True
    assert sdp.audio.is_sip_dtls is False


def test_sdes_offer_is_not_sip_dtls() -> None:
    """An SDES RTP/SAVP offer is is_srtp but never is_sip_dtls (no fingerprint)."""
    sdp = SessionDescription.parse(_OFFER_SAVP)
    assert sdp.audio is not None
    assert sdp.audio.is_srtp is True
    assert sdp.audio.is_sip_dtls is False


# --- negotiate_media_security: opportunistic DTLS > SDES > plain ranking ---


def test_negotiate_media_security_dtls_for_sip_dtls_offer() -> None:
    """A UDP/TLS/RTP/SAVP+fingerprint offer ranks as DTLS (the strongest tier)."""
    sdp = SessionDescription.parse(_OFFER_SIP_DTLS)
    assert sdp.audio is not None
    assert negotiate_media_security(sdp.audio) is MediaSecurity.DTLS


def test_negotiate_media_security_sdes_for_savp_offer() -> None:
    """An RTP/SAVP offer with a usable a=crypto ranks as SDES (the middle tier)."""
    sdp = SessionDescription.parse(_OFFER_SAVP)
    assert sdp.audio is not None
    assert negotiate_media_security(sdp.audio) is MediaSecurity.SDES


def test_negotiate_media_security_plain_for_avp_offer() -> None:
    """A plain RTP/AVP offer ranks as PLAIN (the fallback tier)."""
    sdp = SessionDescription.parse(_OFFER_AVP)
    assert sdp.audio is not None
    assert negotiate_media_security(sdp.audio) is MediaSecurity.PLAIN


def test_negotiate_media_security_savp_without_crypto_is_unsupported() -> None:
    """An RTP/SAVP offer with no usable a=crypto is UNSUPPORTED — NOT PLAIN.

    Critical downgrade guard: a SAVP profile DEMANDED encrypted media. We cannot key
    it (its only crypto line is malformed), but it must NOT degrade to PLAIN — that
    would let the caller answer clear RTP to a peer that never offered clear RTP. The
    ranker returns UNSUPPORTED, a distinct non-plain tier the caller must reject (or
    re-offer), never silently answer in the clear.
    """
    sdp = SessionDescription.parse(_OFFER_SAVP_BAD_CRYPTO)
    assert sdp.audio is not None
    assert negotiate_media_security(sdp.audio) is MediaSecurity.UNSUPPORTED


def test_negotiate_media_security_dtls_profile_without_fingerprint_is_unsupported() -> (
    None
):
    """A UDP/TLS/RTP/SAVP offer missing a=fingerprint is UNSUPPORTED — not PLAIN.

    The profile demanded DTLS-SRTP but omitted the fingerprint (RFC 5763 §5), so we
    cannot key it. It must not downgrade to clear RTP — UNSUPPORTED, so the caller
    rejects rather than answering plaintext to an encrypted-media offer.
    """
    sdp = SessionDescription.parse(_OFFER_SIP_DTLS_NO_FP)
    assert sdp.audio is not None
    assert negotiate_media_security(sdp.audio) is MediaSecurity.UNSUPPORTED


def test_negotiate_media_security_webrtc_savpf_is_not_plain() -> None:
    """A WebRTC SAVPF offer is an encrypted profile — UNSUPPORTED here, never PLAIN.

    negotiate_media_security covers the SIP (non-WebRTC) path; a SAVPF offer is not
    answerable here (it needs the WebRTC/ICE path), and it must NOT be ranked PLAIN —
    that would invite a plaintext answer to an encrypted-transport offer.
    """
    sdp = SessionDescription.parse(_OFFER_SAVPF)
    assert sdp.audio is not None
    assert negotiate_media_security(sdp.audio) is MediaSecurity.UNSUPPORTED


def test_negotiate_media_security_dtls_outranks_sdes() -> None:
    """A UDP/TLS/RTP/SAVP offer that ALSO carried a=crypto still ranks DTLS first.

    Defensive: a non-conformant peer might attach SDES crypto to a DTLS profile.
    DTLS is strictly stronger (key never in SDP), so the ranker prefers it — we
    never downgrade to SDES when DTLS is on offer.
    """
    offer_both = (
        "v=0\r\n"
        "o=- 3000 3000 IN IP4 192.0.2.30\r\n"
        "s=-\r\n"
        "c=IN IP4 192.0.2.30\r\n"
        "t=0 0\r\n"
        "m=audio 41000 UDP/TLS/RTP/SAVP 0 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
        f"a=fingerprint:sha-256 {_FAKE_FINGERPRINT}\r\n"
        "a=setup:actpass\r\n"
        f"a=crypto:1 {_FAKE_CRYPTO}\r\n"
        "a=rtcp-mux\r\n"
        "a=sendrecv\r\n"
    )
    sdp = SessionDescription.parse(offer_both)
    assert sdp.audio is not None
    assert negotiate_media_security(sdp.audio) is MediaSecurity.DTLS


# --- build_sip_dtls_answer ---


def test_build_sip_dtls_answer_profile() -> None:
    """The answer uses the UDP/TLS/RTP/SAVP profile (no AVPF F)."""
    offer = SessionDescription.parse(_OFFER_SIP_DTLS)
    text = build_sip_dtls_answer(
        offer,
        local_address="192.0.2.40",
        port=43000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
    )
    assert "m=audio 43000 UDP/TLS/RTP/SAVP " in text


def test_build_sip_dtls_answer_fingerprint_present() -> None:
    """The answer advertises OUR DTLS certificate fingerprint."""
    offer = SessionDescription.parse(_OFFER_SIP_DTLS)
    text = build_sip_dtls_answer(
        offer,
        local_address="192.0.2.40",
        port=43000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
    )
    assert f"a=fingerprint:sha-256 {_FAKE_FINGERPRINT_ANSWER}" in text


def test_build_sip_dtls_answer_setup_present() -> None:
    """The answer carries our negotiated DTLS role (active/passive)."""
    offer = SessionDescription.parse(_OFFER_SIP_DTLS)
    text = build_sip_dtls_answer(
        offer,
        local_address="192.0.2.40",
        port=43000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
    )
    assert "a=setup:active" in text


def test_build_sip_dtls_answer_keeps_connection_line() -> None:
    """UNLIKE WebRTC, the SIP-DTLS answer KEEPS its c= line (real media address)."""
    offer = SessionDescription.parse(_OFFER_SIP_DTLS)
    text = build_sip_dtls_answer(
        offer,
        local_address="192.0.2.40",
        port=43000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
    )
    assert "c=IN IP4 192.0.2.40" in text


def test_build_sip_dtls_answer_rtcp_mux_mirrors_offer() -> None:
    """rtcp-mux is mirrored from the offer (RFC 5761 §5.1.1)."""
    offer = SessionDescription.parse(_OFFER_SIP_DTLS)
    text = build_sip_dtls_answer(
        offer,
        local_address="192.0.2.40",
        port=43000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
    )
    assert "a=rtcp-mux" in text


def test_build_sip_dtls_answer_no_sdes_crypto() -> None:
    """A DTLS-SRTP answer MUST NOT carry a=crypto (the key is derived, not inline)."""
    offer = SessionDescription.parse(_OFFER_SIP_DTLS)
    text = build_sip_dtls_answer(
        offer,
        local_address="192.0.2.40",
        port=43000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
    )
    assert "a=crypto" not in text


def test_build_sip_dtls_answer_no_ice() -> None:
    """A SIP-DTLS answer carries NO ICE attributes (no ICE on this path)."""
    offer = SessionDescription.parse(_OFFER_SIP_DTLS)
    text = build_sip_dtls_answer(
        offer,
        local_address="192.0.2.40",
        port=43000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
    )
    assert "a=ice-ufrag" not in text
    assert "a=ice-pwd" not in text
    assert "a=candidate" not in text


def test_build_sip_dtls_answer_round_trip_parse() -> None:
    """The built answer parses back as a UDP/TLS/RTP/SAVP DTLS-SRTP description."""
    offer = SessionDescription.parse(_OFFER_SIP_DTLS)
    text = build_sip_dtls_answer(
        offer,
        local_address="192.0.2.40",
        port=43000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
    )
    parsed = SessionDescription.parse(text)
    assert parsed.audio is not None
    assert parsed.audio.protocol == "UDP/TLS/RTP/SAVP"
    assert parsed.audio.is_sip_dtls is True
    assert parsed.audio.fingerprint is not None
    assert parsed.audio.fingerprint.value == _FAKE_FINGERPRINT_ANSWER
    assert parsed.audio.setup is not None
    assert parsed.audio.setup.value == "active"
    assert parsed.audio.connection_address == "192.0.2.40"


def test_build_sip_dtls_answer_mirrors_direction() -> None:
    """The answer mirrors the offered direction (sendonly -> recvonly, RFC 3264)."""
    sendonly = _OFFER_SIP_DTLS.replace("a=sendrecv", "a=sendonly")
    offer = SessionDescription.parse(sendonly)
    text = build_sip_dtls_answer(
        offer,
        local_address="192.0.2.40",
        port=43000,
        supported=("PCMU", "telephone-event"),
        fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
        setup=SetupRole.parse("active"),
    )
    assert "a=recvonly" in text


def test_build_sip_dtls_answer_rejects_actpass_setup() -> None:
    """RFC 5763 §5: the answerer MUST NOT answer actpass — only active/passive."""
    offer = SessionDescription.parse(_OFFER_SIP_DTLS)
    with pytest.raises(SdpError, match="actpass"):
        build_sip_dtls_answer(
            offer,
            local_address="192.0.2.40",
            port=43000,
            supported=("PCMU", "telephone-event"),
            fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
            setup=SetupRole.parse("actpass"),
        )


def test_build_sip_dtls_answer_rejects_non_dtls_offer() -> None:
    """build_sip_dtls_answer requires a UDP/TLS/RTP/SAVP+fingerprint offer."""
    offer = SessionDescription.parse(_OFFER_SAVP)  # SDES, not DTLS
    with pytest.raises(SdpError, match="UDP/TLS/RTP/SAVP"):
        build_sip_dtls_answer(
            offer,
            local_address="192.0.2.40",
            port=43000,
            supported=("PCMU", "telephone-event"),
            fingerprint=Fingerprint.parse(f"sha-256 {_FAKE_FINGERPRINT_ANSWER}"),
            setup=SetupRole.parse("active"),
        )


def test_build_sip_dtls_answer_does_not_regress_sdes() -> None:
    """build_audio_answer (SDES/plain) is unchanged: a SAVP offer still answers SDES.

    Cross-contamination guard — the new DTLS path is a SEPARATE builder; the SDES
    answer remains byte-for-byte the SDES shape (RTP/SAVP + a=crypto, no fingerprint).
    """
    offer = SessionDescription.parse(_OFFER_SAVP)
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU", "telephone-event"),
        crypto=_FAKE_ANSWER_CRYPTO,
    )
    assert "RTP/SAVP" in text
    assert "UDP/TLS/RTP/SAVP" not in text
    assert "a=crypto:" in text
    assert "a=fingerprint" not in text


# ---------------------------------------------------------------------------
# Parse-path port and ptime validation (hostile inbound, robustness).
# RFC 4566 §5.7: port 0 is VALID (signals a disabled/rejected media stream);
# only NEGATIVE and out-of-range (>65535) ports are invalid.
# ---------------------------------------------------------------------------

_SDP_NEGATIVE_PORT = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 192.0.2.3\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.3\r\n"
    "t=0 0\r\n"
    "m=audio -5 RTP/AVP 0\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=sendrecv\r\n"
)

_SDP_PORT_TOO_LARGE = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 192.0.2.3\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.3\r\n"
    "t=0 0\r\n"
    "m=audio 65536 RTP/AVP 0\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=sendrecv\r\n"
)

_SDP_PORT_ZERO = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 192.0.2.3\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.3\r\n"
    "t=0 0\r\n"
    "m=audio 0 RTP/AVP 0\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=sendrecv\r\n"
)

_SDP_PTIME_NEGATIVE = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 192.0.2.3\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.3\r\n"
    "t=0 0\r\n"
    "m=audio 40000 RTP/AVP 0\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=ptime:-3\r\n"
    "a=sendrecv\r\n"
)

_SDP_PTIME_ZERO = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 192.0.2.3\r\n"
    "s=-\r\n"
    "c=IN IP4 192.0.2.3\r\n"
    "t=0 0\r\n"
    "m=audio 40000 RTP/AVP 0\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=ptime:0\r\n"
    "a=sendrecv\r\n"
)


def test_parse_rejects_negative_port() -> None:
    """The parser MUST reject a negative media port with SdpError (hostile inbound)."""
    with pytest.raises(SdpError, match=r"port"):
        SessionDescription.parse(_SDP_NEGATIVE_PORT)


def test_parse_rejects_port_above_65535() -> None:
    """The parser MUST reject a port > 65535 with SdpError (hostile inbound)."""
    with pytest.raises(SdpError, match=r"port"):
        SessionDescription.parse(_SDP_PORT_TOO_LARGE)


def test_parse_accepts_port_zero() -> None:
    """Port 0 is VALID per RFC 4566 §5.7 (disabled/rejected stream); must not raise."""
    sdp = SessionDescription.parse(_SDP_PORT_ZERO)
    assert sdp.audio is not None
    assert sdp.audio.port == 0


def test_parse_rejects_negative_ptime() -> None:
    """The parser MUST reject a negative ptime with SdpError (hostile inbound)."""
    with pytest.raises(SdpError, match=r"ptime"):
        SessionDescription.parse(_SDP_PTIME_NEGATIVE)


def test_parse_rejects_ptime_zero() -> None:
    """The parser MUST reject ptime=0 with SdpError (non-positive ptime is invalid)."""
    with pytest.raises(SdpError, match=r"ptime"):
        SessionDescription.parse(_SDP_PTIME_ZERO)


# --- W3 (security): SDES answer selects the STRONGEST offered suite, not the
# first-offered (within-SRTP downgrade fix). RFC 4568 lets the answerer pick any
# ONE offered crypto; honouring offer ORDER lets a gateway (or a MITM that can
# reorder the offer) put AES_CM_128_HMAC_SHA1_32 (32-bit auth tag) first and pull
# the answer to the weaker integrity tag while both stay encrypted. The answerer
# MUST select the offered+supported suite with the strongest auth tag (SHA1_80
# over SHA1_32), falling back to the single/only suite when there is just one.

_SUITE_80 = "AES_CM_128_HMAC_SHA1_80"


def _savp_offer_two_crypto(first: str, second: str) -> str:
    """An RTP/SAVP audio offer carrying two a=crypto lines (tag 1 then tag 2)."""
    return (
        "v=0\r\n"
        "o=- 1 1 IN IP4 192.0.2.2\r\n"
        "s=-\r\n"
        "c=IN IP4 192.0.2.2\r\n"
        "t=0 0\r\n"
        "m=audio 40002 RTP/SAVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        f"a=crypto:1 {first} inline:{_FAKE_KEY}\r\n"
        f"a=crypto:2 {second} inline:{_FAKE_KEY}\r\n"
        "a=sendrecv\r\n"
    )


def test_answer_selects_sha1_80_when_sha1_32_offered_first() -> None:
    # DOWNGRADE FIX: SHA1_32 (tag 1) is listed BEFORE SHA1_80 (tag 2). The answer
    # MUST select the STRONGER SHA1_80 (tag 2), NOT the first-offered SHA1_32.
    offer = SessionDescription.parse(_savp_offer_two_crypto(_SUITE_32, _SUITE_80))
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU",),
        crypto=f"{_SUITE_80} inline:{_FAKE_ANSWER_KEY}",
    )
    # The answer echoes the STRONGER suite under its tag (2), with OUR key.
    assert f"a=crypto:2 {_SUITE_80} inline:{_FAKE_ANSWER_KEY}" in text
    # The weaker first-offered suite is NOT selected.
    assert f"a=crypto:1 {_SUITE_32}" not in text
    assert _SUITE_32 not in text
    # Negative control: the offerer's key material is never echoed back.
    assert _FAKE_KEY not in text


def test_answer_selects_sha1_80_when_sha1_80_offered_first() -> None:
    # No regression: SHA1_80 first already wins; tag is preserved.
    offer = SessionDescription.parse(_savp_offer_two_crypto(_SUITE_80, _SUITE_32))
    text = build_audio_answer(
        offer,
        local_address="192.0.2.20",
        port=42000,
        supported=("PCMU",),
        crypto=f"{_SUITE_80} inline:{_FAKE_ANSWER_KEY}",
    )
    assert f"a=crypto:1 {_SUITE_80} inline:{_FAKE_ANSWER_KEY}" in text
    assert _SUITE_32 not in text


def test_answer_keeps_sha1_32_when_only_weak_suite_offered() -> None:
    # No regression: when SHA1_32 is the ONLY offered suite, it is still accepted
    # (selecting the strongest among one suite is that suite).
    offer = SessionDescription.parse(
        "v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=-\r\nc=IN IP4 192.0.2.2\r\nt=0 0\r\n"
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


def test_negotiate_answer_crypto_picks_strongest_directly() -> None:
    # Direct (sans-IO) check of _negotiate_answer_crypto: SHA1_32 first, SHA1_80
    # second -> the returned attribute echoes the SHA1_80 suite + its tag.
    offer = SessionDescription.parse(_savp_offer_two_crypto(_SUITE_32, _SUITE_80))
    assert offer.audio is not None
    accepted = _negotiate_answer_crypto(
        offer.audio, f"{_SUITE_80} inline:{_FAKE_ANSWER_KEY}"
    )
    assert accepted.suite == _SUITE_80
    assert accepted.tag == 2
