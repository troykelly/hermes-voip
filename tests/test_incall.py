"""Tests for the sans-IO in-call control layer (ADR-0011 PR4).

Covers hold/resume re-INVITE construction, the response classifier (incl. 491
glare), and the inbound re-INVITE classifier (mirrored answer direction, glare).
The load-bearing property is **invariant 1**: a hold re-INVITE bumps **both** the
dialog CSeq and the SDP ``o=`` version, with the SDP session-id held constant.

Fakes only (``pbx.example.test``, ext ``1000``/``2000``, ``198.51.100.x``).
"""

from __future__ import annotations

import pytest

from hermes_voip.dialog import Dialog
from hermes_voip.incall import (
    Glare,
    HoldConfirmed,
    IncallError,
    LocalMediaSession,
    MediaUpdate,
    OfferlessReinvite,
    ReinviteChallenged,
    ReinviteProgress,
    ReinviteRejected,
    build_hold_reinvite,
    classify_inbound_reinvite,
    handle_reinvite_response,
)
from hermes_voip.message import SipRequest, SipResponse
from hermes_voip.sdp import Codec, build_audio_offer

_PCMU = Codec(payload_type=0, encoding="PCMU", clock_rate=8000)
_MEDIA = LocalMediaSession(
    local_address="198.51.100.7",
    port=40000,
    codecs=(_PCMU,),
    session_id=88888,
)


def _dialog(*, local_cseq: int = 5, sdp_version: int = 2) -> Dialog:
    return Dialog(
        call_id="call-1",
        local_uri="sip:1000@pbx.example.test",
        local_tag="ours",
        remote_uri="sip:2000@pbx.example.test",
        remote_tag="theirs",
        remote_target="sip:2000@198.51.100.99:5061;transport=tls",
        route_set=(),
        local_contact="<sip:1000@198.51.100.7:5061;transport=tls>",
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
        local_cseq=local_cseq,
        sdp_version=sdp_version,
    )


def _response(
    code: int,
    reason: str,
    *,
    headers: tuple[tuple[str, str], ...] = (),
    body: str = "",
) -> SipResponse:
    return SipResponse(status_code=code, reason=reason, headers=headers, body=body)


def _offer_request(direction: str) -> SipRequest:
    body = build_audio_offer(
        local_address="198.51.100.99",
        port=42000,
        codecs=(_PCMU,),
        direction=direction,
        session_id=7,
    )
    return SipRequest(
        method="INVITE",
        request_uri="sip:1000@pbx.example.test",
        headers=(("Content-Type", "application/sdp"),),
        body=body,
    )


# ---- build_hold_reinvite: invariant 1 --------------------------------------


def test_build_hold_reinvite_sendonly_bumps_both_counters() -> None:
    d = _dialog(local_cseq=5, sdp_version=2)
    result = build_hold_reinvite(d, _MEDIA, "sendonly")
    req = SipRequest.parse(result.text)
    assert req.method == "INVITE"
    assert req.request_uri == "sip:2000@198.51.100.99:5061;transport=tls"
    assert req.header("CSeq") == "6 INVITE"
    assert req.header("Content-Type") == "application/sdp"
    assert "a=sendonly" in req.body
    # invariant 1: CSeq and the o= version both advance by one.
    assert result.dialog.local_cseq == 6
    assert result.dialog.sdp_version == 3
    # The o= line keeps the session-id (88888) and bumps only the version (2 -> 3).
    assert "o=- 88888 3 IN IP4 198.51.100.7" in req.body


def test_build_resume_reinvite_sendrecv() -> None:
    d = _dialog(local_cseq=6, sdp_version=3)
    result = build_hold_reinvite(d, _MEDIA, "sendrecv")
    req = SipRequest.parse(result.text)
    assert "a=sendrecv" in req.body
    assert result.dialog.local_cseq == 7
    assert result.dialog.sdp_version == 4
    assert "o=- 88888 4 IN IP4" in req.body


def test_build_hold_reinvite_rejects_bad_direction() -> None:
    with pytest.raises(IncallError):
        build_hold_reinvite(_dialog(), _MEDIA, "recvonly")


def test_session_id_constant_version_monotonic_across_reoffers() -> None:
    d = _dialog(local_cseq=1, sdp_version=0)
    first = build_hold_reinvite(d, _MEDIA, "sendonly")
    second = build_hold_reinvite(first.dialog, _MEDIA, "sendrecv")
    assert "o=- 88888 1 " in SipRequest.parse(first.text).body
    assert "o=- 88888 2 " in SipRequest.parse(second.text).body
    assert second.dialog.sdp_version == 2
    assert second.dialog.local_cseq == 3


# ---- handle_reinvite_response ----------------------------------------------


def test_handle_200_with_answer() -> None:
    answer = build_audio_offer(
        local_address="198.51.100.99",
        port=41000,
        codecs=(_PCMU,),
        direction="recvonly",
        session_id=2,
    )
    out = handle_reinvite_response(_response(200, "OK", body=answer))
    assert isinstance(out, HoldConfirmed)
    assert out.answer is not None
    assert out.answer.audio is not None
    assert out.answer.audio.direction == "recvonly"


def test_handle_200_without_answer_raises() -> None:
    # We always offer SDP in a hold/resume re-INVITE, so a 2xx with no usable
    # SDP answer is an RFC 3264 offer/answer violation — fail loudly, never
    # silently confirm media (codex HIGH).
    with pytest.raises(IncallError):
        handle_reinvite_response(_response(200, "OK"))


def test_handle_200_with_non_audio_sdp_raises() -> None:
    video_only = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 198.51.100.99\r\n"
        "s=-\r\n"
        "c=IN IP4 198.51.100.99\r\n"
        "t=0 0\r\n"
        "m=video 40000 RTP/AVP 96\r\n"
    )
    with pytest.raises(IncallError):
        handle_reinvite_response(_response(200, "OK", body=video_only))


def test_handle_401_challenged() -> None:
    out = handle_reinvite_response(
        _response(
            401,
            "Unauthorized",
            headers=(
                ("WWW-Authenticate", 'Digest realm="pbx.example.test", nonce="abc"'),
            ),
        )
    )
    assert isinstance(out, ReinviteChallenged)
    assert out.proxy is False
    assert out.challenge.realm == "pbx.example.test"


def test_handle_407_proxy_challenged() -> None:
    out = handle_reinvite_response(
        _response(
            407,
            "Proxy Authentication Required",
            headers=(
                (
                    "Proxy-Authenticate",
                    'Digest realm="pbx.example.test", nonce="abc"',
                ),
            ),
        )
    )
    assert isinstance(out, ReinviteChallenged)
    assert out.proxy is True


def test_handle_491_is_glare_rejection() -> None:
    out = handle_reinvite_response(_response(491, "Request Pending"))
    assert isinstance(out, ReinviteRejected)
    assert out.status_code == 491
    assert out.is_glare is True


def test_handle_488_rejected_not_glare() -> None:
    out = handle_reinvite_response(_response(488, "Not Acceptable Here"))
    assert isinstance(out, ReinviteRejected)
    assert out.status_code == 488
    assert out.is_glare is False


def test_handle_provisional_is_progress() -> None:
    out = handle_reinvite_response(_response(100, "Trying"))
    assert isinstance(out, ReinviteProgress)


# ---- classify_inbound_reinvite ---------------------------------------------


def test_classify_glare_when_local_offer_pending() -> None:
    # An inbound re-INVITE while our own re-INVITE is unanswered is glare -> 491.
    out = classify_inbound_reinvite(
        _offer_request("sendrecv"), pending_local_offer=True
    )
    assert isinstance(out, Glare)


def test_classify_hold_sendonly_answers_recvonly_held() -> None:
    out = classify_inbound_reinvite(
        _offer_request("sendonly"), pending_local_offer=False
    )
    assert isinstance(out, MediaUpdate)
    assert out.offer_direction == "sendonly"
    assert out.answer_direction == "recvonly"
    assert out.held_by_peer is True


def test_classify_sendrecv_answers_sendrecv_not_held() -> None:
    out = classify_inbound_reinvite(
        _offer_request("sendrecv"), pending_local_offer=False
    )
    assert isinstance(out, MediaUpdate)
    assert out.answer_direction == "sendrecv"
    assert out.held_by_peer is False


def test_classify_recvonly_answers_sendonly_not_held() -> None:
    out = classify_inbound_reinvite(
        _offer_request("recvonly"), pending_local_offer=False
    )
    assert isinstance(out, MediaUpdate)
    assert out.answer_direction == "sendonly"
    assert out.held_by_peer is False


def test_classify_inactive_answers_inactive_held() -> None:
    out = classify_inbound_reinvite(
        _offer_request("inactive"), pending_local_offer=False
    )
    assert isinstance(out, MediaUpdate)
    assert out.answer_direction == "inactive"
    assert out.held_by_peer is True


def test_classify_legacy_blackhole_hold_is_held() -> None:
    # Legacy RFC 2543 hold sets c= to 0.0.0.0 (often with sendrecv). ADR-0011
    # tolerates it on receive: classify it as held even though the direction is
    # not sendonly/inactive (codex MEDIUM).
    legacy = build_audio_offer(
        local_address="0.0.0.0",  # noqa: S104 — modelling a legacy black-hole hold offer
        port=42000,
        codecs=(_PCMU,),
        direction="sendrecv",
        session_id=9,
    )
    req = SipRequest(
        method="INVITE",
        request_uri="sip:1000@pbx.example.test",
        headers=(("Content-Type", "application/sdp"),),
        body=legacy,
    )
    out = classify_inbound_reinvite(req, pending_local_offer=False)
    assert isinstance(out, MediaUpdate)
    assert out.held_by_peer is True


def test_classify_offerless_reinvite() -> None:
    req = SipRequest(
        method="INVITE",
        request_uri="sip:1000@pbx.example.test",
        headers=(),
        body="",
    )
    out = classify_inbound_reinvite(req, pending_local_offer=False)
    assert isinstance(out, OfferlessReinvite)


def test_classify_glare_takes_priority_over_offer() -> None:
    # Even with a valid offer, a pending local offer means we must 491 first.
    out = classify_inbound_reinvite(
        _offer_request("sendonly"), pending_local_offer=True
    )
    assert isinstance(out, Glare)
