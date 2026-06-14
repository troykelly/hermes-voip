"""Tests for the CallSession in-call orchestrator (ADR-0011 PR8).

Outbound verbs (hold/unhold/transfer) drive the sans-IO modules over fake
signalling + media seams; responses are fed back through ``on_response`` to
exercise the CSeq correlation, ACK, re-auth, and glare paths. Inbound
``handle_request`` answers re-INVITE (mirrored direction; glare → 491), NOTIFY,
REFER, and BYE.

Fakes only (``pbx.example.test``, ext ``1000``/``2000``, ``198.51.100.x``).
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_voip.call import CallError, CallSession
from hermes_voip.dialog import Dialog
from hermes_voip.digest import DigestCredentials
from hermes_voip.incall import LocalMediaSession
from hermes_voip.message import SipRequest, SipResponse, build_response
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.refer import ReferRequest
from hermes_voip.sdp import Codec, build_audio_offer

pytestmark = pytest.mark.asyncio

_PCMU = Codec(payload_type=0, encoding="PCMU", clock_rate=8000)
_MEDIA = LocalMediaSession(
    local_address="198.51.100.7", port=40000, codecs=(_PCMU,), session_id=55555
)
_CREDENTIALS = DigestCredentials("1000", "s3cr3t")


class _FakeSignaling:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


class _FakeMedia:
    def __init__(self) -> None:
        self.holds: list[bool] = []
        self.stopped = False

    async def set_hold(self, on_hold: bool) -> None:
        self.holds.append(on_hold)

    async def stop(self) -> None:
        self.stopped = True


def _dialog(*, local_cseq: int = 2, sdp_version: int = 0) -> Dialog:
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


def _session(signaling: _FakeSignaling, media: _FakeMedia, **kw: object) -> CallSession:
    return CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_MEDIA,
        credentials=_CREDENTIALS,
        response_timeout=2.0,
        **kw,  # type: ignore[arg-type]  # test-only extra kwargs (on_refer)
    )


def _last_request(signaling: _FakeSignaling, method: str) -> SipRequest:
    for text in reversed(signaling.sent):
        if text.startswith("SIP/2.0 "):
            continue  # a response we sent, not a request
        request = SipRequest.parse(text)
        if request.method == method:
            return request
    msg = f"no {method} was sent"
    raise AssertionError(msg)


def _answer_to(request: SipRequest, direction: str) -> str:
    sdp = build_audio_offer(
        local_address="198.51.100.99",
        port=41000,
        codecs=(_PCMU,),
        direction=direction,
        session_id=2,
    )
    return build_response(
        request,
        200,
        "OK",
        extra_headers=(("Content-Type", "application/sdp"),),
        body=sdp,
    )


# ---- outbound: hold / unhold -----------------------------------------------


async def test_hold_sends_reinvite_acks_and_gates_media() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")
    assert "a=sendonly" in reinvite.body
    await session.on_response(SipResponse.parse(_answer_to(reinvite, "recvonly")))
    await task
    assert session.on_hold is True
    assert media.holds == [True]
    ack = _last_request(signaling, "ACK")
    assert ack.header("CSeq") == reinvite.header("CSeq").split()[0] + " ACK"  # type: ignore[union-attr]


async def test_unhold_resumes_and_ungates_media() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    task = asyncio.create_task(session.unhold())
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")
    assert "a=sendrecv" in reinvite.body
    await session.on_response(SipResponse.parse(_answer_to(reinvite, "sendrecv")))
    await task
    assert session.on_hold is False
    assert media.holds == [False]


async def test_hold_skips_provisional_then_completes() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")
    trying = build_response(reinvite, 100, "Trying")
    await session.on_response(SipResponse.parse(trying))
    await asyncio.sleep(0)
    assert not task.done()  # a 1xx must not complete the verb
    await session.on_response(SipResponse.parse(_answer_to(reinvite, "recvonly")))
    await task
    assert session.on_hold is True


async def test_hold_reauthenticates_on_401() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)
    first = _last_request(signaling, "INVITE")
    challenge = build_response(
        first,
        401,
        "Unauthorized",
        extra_headers=(
            ("WWW-Authenticate", 'Digest realm="pbx.example.test", nonce="n1"'),
        ),
    )
    await session.on_response(SipResponse.parse(challenge))
    await asyncio.sleep(0)
    authed = _last_request(signaling, "INVITE")
    assert authed.header("Authorization") is not None
    assert authed.header("CSeq") != first.header("CSeq")  # a new transaction
    await session.on_response(SipResponse.parse(_answer_to(authed, "recvonly")))
    await task
    assert session.on_hold is True


async def test_hold_glare_491_raises() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")
    await session.on_response(
        SipResponse.parse(build_response(reinvite, 491, "Request Pending"))
    )
    with pytest.raises(CallError, match="glare"):
        await task
    assert media.holds == []  # media is never gated on a failed hold


async def test_hold_timeout_raises() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_MEDIA,
        credentials=_CREDENTIALS,
        response_timeout=0.05,
    )
    with pytest.raises(CallError, match="no final response"):
        await session.hold()


# ---- outbound: transfer ----------------------------------------------------


async def test_transfer_blind_sends_refer() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    task = asyncio.create_task(session.transfer_blind("sip:3000@pbx.example.test"))
    await asyncio.sleep(0)
    refer = _last_request(signaling, "REFER")
    assert refer.header("Refer-To") == "<sip:3000@pbx.example.test>"
    await session.on_response(SipResponse.parse(build_response(refer, 202, "Accepted")))
    await task


async def test_transfer_attended_embeds_replaces() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    consult = Dialog(
        call_id="ac-call",
        local_uri="sip:1000@pbx.example.test",
        local_tag="a-ctag",
        remote_uri="sip:3000@pbx.example.test",
        remote_tag="c-tag",
        remote_target="sip:3000@198.51.100.50:5061;transport=tls",
        route_set=(),
        local_contact="<sip:1000@198.51.100.7:5061;transport=tls>",
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
        local_cseq=5,
        sdp_version=0,
    )
    task = asyncio.create_task(session.transfer_attended(consult))
    await asyncio.sleep(0)
    refer = _last_request(signaling, "REFER")
    assert "Replaces=" in (refer.header("Refer-To") or "")
    await session.on_response(SipResponse.parse(build_response(refer, 202, "Accepted")))
    await task


async def test_transfer_rejected_raises() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    task = asyncio.create_task(session.transfer_blind("sip:3000@pbx.example.test"))
    await asyncio.sleep(0)
    refer = _last_request(signaling, "REFER")
    await session.on_response(SipResponse.parse(build_response(refer, 603, "Decline")))
    with pytest.raises(CallError, match="REFER rejected"):
        await task


# ---- inbound: handle_request -----------------------------------------------


def _inbound(method: str, *, body: str = "", cseq: int = 9) -> SipRequest:
    return SipRequest(
        method=method,
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Via", "SIP/2.0/TLS 198.51.100.99:5061;branch=z9hG4bK-in"),
            ("From", "<sip:2000@pbx.example.test>;tag=theirs"),
            ("To", "<sip:1000@pbx.example.test>;tag=ours"),
            ("Call-ID", "call-1"),
            ("CSeq", f"{cseq} {method}"),
            *((("Content-Type", "application/sdp"),) if body else ()),
        ),
        body=body,
    )


async def test_inbound_bye_answers_200_and_stops_media() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    await session.handle_request(_inbound("BYE"))
    response = SipResponse.parse(signaling.sent[-1])
    assert response.status_code == 200
    assert session.ended is True
    assert media.stopped is True


async def test_inbound_reinvite_hold_answers_recvonly_and_holds() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    offer = build_audio_offer(
        local_address="198.51.100.99",
        port=42000,
        codecs=(_PCMU,),
        direction="sendonly",
        session_id=7,
    )
    await session.handle_request(_inbound("INVITE", body=offer))
    response = SipResponse.parse(signaling.sent[-1])
    assert response.status_code == 200
    assert "a=recvonly" in response.body
    assert session.on_hold is True
    assert media.holds == [True]


async def test_inbound_reinvite_during_local_offer_is_491_glare() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    hold_task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)  # our re-INVITE is now outstanding (local offer pending)
    offer = build_audio_offer(
        local_address="198.51.100.99",
        port=42000,
        codecs=(_PCMU,),
        direction="sendrecv",
        session_id=7,
    )
    await session.handle_request(_inbound("INVITE", body=offer, cseq=11))
    glare = SipResponse.parse(signaling.sent[-1])
    assert glare.status_code == 491
    # let the hold complete so the task does not leak
    our_reinvite = _last_request(signaling, "INVITE")
    await session.on_response(SipResponse.parse(_answer_to(our_reinvite, "recvonly")))
    await hold_task


async def test_inbound_notify_records_progress_and_answers_200() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    notify = SipRequest(
        method="NOTIFY",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Via", "SIP/2.0/TLS 198.51.100.99:5061;branch=z9hG4bK-n"),
            ("From", "<sip:2000@pbx.example.test>;tag=theirs"),
            ("To", "<sip:1000@pbx.example.test>;tag=ours"),
            ("Call-ID", "call-1"),
            ("CSeq", "12 NOTIFY"),
            ("Subscription-State", "terminated;reason=noresource"),
            ("Content-Type", "message/sipfrag"),
        ),
        body="SIP/2.0 200 OK",
    )
    await session.handle_request(notify)
    assert SipResponse.parse(signaling.sent[-1]).status_code == 200
    assert session.transfer_progress is not None
    assert session.transfer_progress.status_code == 200
    assert session.transfer_progress.terminated is True


async def test_inbound_refer_accepts_and_invokes_handler() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    seen: list[ReferRequest] = []

    async def _handler(refer: ReferRequest) -> None:
        seen.append(refer)

    session = _session(signaling, media, on_refer=_handler)
    refer = SipRequest(
        method="REFER",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Via", "SIP/2.0/TLS 198.51.100.99:5061;branch=z9hG4bK-r"),
            ("From", "<sip:2000@pbx.example.test>;tag=theirs"),
            ("To", "<sip:1000@pbx.example.test>;tag=ours"),
            ("Call-ID", "call-1"),
            ("CSeq", "13 REFER"),
            ("Refer-To", "<sip:4000@pbx.example.test>"),
        ),
        body="",
    )
    await session.handle_request(refer)
    assert SipResponse.parse(signaling.sent[-1]).status_code == 202
    assert len(seen) == 1
    assert seen[0].refer_to == "sip:4000@pbx.example.test"


async def test_dialog_id_routes_inbound() -> None:
    session = _session(_FakeSignaling(), _FakeMedia())
    assert session.dialog_id == ("call-1", "ours", "theirs")
