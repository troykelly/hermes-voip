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
import base64
import logging
import sys

import pytest

from hermes_voip.call import CallError, CallSession, _cseq_number
from hermes_voip.dialog import Dialog
from hermes_voip.digest import DigestCredentials
from hermes_voip.incall import LocalMediaSession
from hermes_voip.message import SipRequest, SipResponse, build_response
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.refer import ReferRequest
from hermes_voip.sdp import (
    Codec,
    CryptoAttribute,
    SessionDescription,
    build_audio_answer,
    build_audio_offer,
    generate_answer_crypto,
)
from hermes_voip.session_timer import RefreshSucceeded

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


class _RaisingSignaling(_FakeSignaling):
    """A signalling seam whose ``send`` always fails (a dropped transport)."""

    async def send(self, message: str) -> None:
        msg = "peer transport is gone"
        raise ConnectionError(msg)


class _FakeMedia:
    def __init__(self) -> None:
        self.holds: list[bool] = []
        self.stopped = False
        self.dtmf: list[str] = []
        # Records each SRTP re-key (ADR-0053 in-dialog re-offer keying): one
        # (inbound, outbound) tuple of the CryptoAttribute (or None) per call.
        self.rekeys: list[tuple[CryptoAttribute | None, CryptoAttribute | None]] = []

    async def set_hold(self, on_hold: bool) -> None:
        self.holds.append(on_hold)

    async def send_dtmf(self, digits: str) -> None:
        self.dtmf.append(digits)

    async def rekey_srtp(
        self,
        *,
        inbound: CryptoAttribute | None,
        outbound: CryptoAttribute | None,
    ) -> None:
        self.rekeys.append((inbound, outbound))

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


# ---- SDES SRTP continuity across re-offer (ADR-0053 in-dialog re-offer keying) ----


def _srtp_crypto(tag: int = 7) -> CryptoAttribute:
    """A supported SDES a=crypto with a runtime-computed fake key (no literal).

    The key is computed (never a literal) so the path-scoped gitleaks allowlist
    (which covers only ``tests/test_sdp.py``) need not extend to this file.
    """
    key = base64.b64encode(bytes(range(30))).decode("ascii")
    return CryptoAttribute(
        tag=tag, suite="AES_CM_128_HMAC_SHA1_80", key_params=f"inline:{key}"
    )


def _srtp_media() -> LocalMediaSession:
    return LocalMediaSession(
        local_address="198.51.100.7",
        port=40000,
        codecs=(_PCMU,),
        session_id=55555,
        crypto=_srtp_crypto(),
    )


def _srtp_answer_to(request: SipRequest, direction: str) -> str:
    """A 200-OK SRTP answer (RTP/SAVP + a=crypto echoing the offered tag/suite)."""
    offer = build_audio_offer(
        local_address="198.51.100.99",
        port=41000,
        codecs=(_PCMU,),
        direction="sendrecv",
        session_id=2,
        crypto=_srtp_crypto(),
    )
    answer_sdp = build_audio_answer(
        SessionDescription.parse(offer),
        local_address="198.51.100.99",
        port=41000,
        supported=["PCMU"],
        session_id=2,
        crypto=generate_answer_crypto(_srtp_crypto()),
    )
    # Mirror the requested direction so the classifier/assert reads cleanly.
    answer_sdp = answer_sdp.replace("a=sendrecv", f"a={direction}")
    return build_response(
        request,
        200,
        "OK",
        extra_headers=(("Content-Type", "application/sdp"),),
        body=answer_sdp,
    )


async def test_hold_on_srtp_call_reoffers_savp_and_rekeys() -> None:
    """Hold on an SRTP call: the re-INVITE stays RTP/SAVP + a=crypto, never plain.

    Regression for the ADR-0053 Stage 1 partial-ship — a hold/resume re-INVITE
    on a secured call MUST NOT downgrade media to cleartext RTP/AVP. The session
    must (a) emit a secured re-offer carrying a fresh per-offer key and (b) on
    acceptance COMMIT the re-key to the engine: outbound with our offered key,
    inbound from the 200's answer crypto (RFC 4568 §6.1 directionality). The
    commit happens only after the peer accepts (a rejected re-offer must not touch
    the live key — see test_rejected_srtp_reoffer_does_not_rekey_outbound).
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_srtp_media(),
        credentials=_CREDENTIALS,
        response_timeout=2.0,
    )
    task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")
    assert "m=audio 40000 RTP/SAVP 0" in reinvite.body
    assert "RTP/AVP" not in reinvite.body  # the secured stream is never downgraded
    assert "a=crypto:7 AES_CM_128_HMAC_SHA1_80 inline:" in reinvite.body
    # Not yet committed: the outbound key is only re-keyed once the peer accepts.
    assert media.rekeys == []
    await session.on_response(SipResponse.parse(_srtp_answer_to(reinvite, "recvonly")))
    await task
    assert session.on_hold is True
    # On acceptance BOTH directions are committed in one atomic re-key: outbound
    # with our offered key, inbound from the peer's answer crypto.
    assert len(media.rekeys) == 1
    inbound, outbound = media.rekeys[0]
    assert inbound is not None  # peer's answer key → our inbound decrypt
    assert outbound is not None  # our offered key → our outbound encrypt


async def test_inbound_reinvite_on_srtp_call_answers_savp_and_rekeys() -> None:
    """A peer re-INVITE (offer) on an SRTP call is answered RTP/SAVP + a=crypto.

    The ANSWER side of the same bug: when the peer re-offers SRTP, our 200 OK
    answer must stay secured and the engine must be re-keyed (inbound from the
    peer's offer key, outbound from our fresh answer key) — never a silent
    downgrade to RTP/AVP.
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_srtp_media(),
        credentials=_CREDENTIALS,
        response_timeout=2.0,
    )
    offer = build_audio_offer(
        local_address="198.51.100.99",
        port=42000,
        codecs=(_PCMU,),
        direction="sendonly",
        session_id=7,
        crypto=_srtp_crypto(),
    )
    await session.handle_request(_inbound("INVITE", body=offer))
    response = SipResponse.parse(signaling.sent[-1])
    assert response.status_code == 200
    assert "RTP/SAVP" in response.body
    assert "RTP/AVP" not in response.body
    assert "a=crypto:7 AES_CM_128_HMAC_SHA1_80 inline:" in response.body
    assert "a=recvonly" in response.body
    # Engine re-keyed both directions: inbound from the peer's offer, outbound
    # from our fresh answer key.
    assert media.rekeys, "engine SRTP was not re-keyed on the inbound re-offer"
    inbound, outbound = media.rekeys[-1]
    assert inbound is not None
    assert outbound is not None


async def test_inbound_reinvite_on_plain_call_stays_plain_no_rekey() -> None:
    """A plain call's inbound re-INVITE answer stays RTP/AVP and never re-keys."""
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)  # _MEDIA has no crypto
    offer = build_audio_offer(
        local_address="198.51.100.99",
        port=42000,
        codecs=(_PCMU,),
        direction="sendonly",
        session_id=7,
    )
    await session.handle_request(_inbound("INVITE", body=offer))
    response = SipResponse.parse(signaling.sent[-1])
    assert "RTP/AVP" in response.body
    assert "RTP/SAVP" not in response.body
    assert "a=crypto" not in response.body
    assert media.rekeys == []  # a plain call never re-keys SRTP


async def test_inbound_plain_reinvite_on_srtp_call_is_rejected_488() -> None:
    """A peer that re-offers PLAIN RTP/AVP on a secured call is rejected 488.

    Downgrade resistance (codex finding): an established SRTP call must NEVER
    answer a plain (no-``a=crypto``) re-offer with a plain 200 OK — that would
    drop the media to cleartext mid-call. We reject with 488 Not Acceptable Here
    and leave the secured media untouched (no re-key, no hold state change).
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_srtp_media(),
        credentials=_CREDENTIALS,
        response_timeout=2.0,
    )
    plain_offer = build_audio_offer(
        local_address="198.51.100.99",
        port=42000,
        codecs=(_PCMU,),
        direction="sendrecv",
        session_id=7,
    )
    await session.handle_request(_inbound("INVITE", body=plain_offer))
    response = SipResponse.parse(signaling.sent[-1])
    assert response.status_code == 488
    assert media.rekeys == []  # the secured context is never disturbed
    assert media.holds == []


async def test_srtp_reoffer_with_plain_answer_raises_and_keeps_media() -> None:
    """If our SRTP re-offer gets a PLAIN answer, the re-INVITE fails loudly.

    Downgrade resistance (codex finding): when WE offered SRTP, a 200 OK that is
    not RTP/SAVP + a=crypto is a failed/non-compliant negotiation — never treated
    as a confirmed media change. We raise CallError and do NOT key inbound to a
    bogus/absent key.
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_srtp_media(),
        credentials=_CREDENTIALS,
        response_timeout=2.0,
    )
    task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")
    # The peer answers PLAIN RTP/AVP (a downgrade) to our SRTP offer.
    plain_answer = _answer_to(reinvite, "recvonly")
    await session.on_response(SipResponse.parse(plain_answer))
    with pytest.raises(CallError):
        await task
    # Inbound was never keyed from a plain answer (no inbound re-key recorded).
    assert all(inbound is None for inbound, _ in media.rekeys)


async def test_srtp_reoffer_answer_with_wrong_tag_raises() -> None:
    """An SRTP answer that echoes the WRONG crypto tag is rejected (RFC 4568 §6.1).

    Robustness (codex round-2): the answerer MUST echo the offered tag (it
    identifies which offered crypto it accepted). An answer carrying a different
    tag/suite is non-compliant — committing it would key outbound to a tag the
    peer never selected. We offered tag 7; the peer answers tag 9 → CallError,
    and the engine outbound is never re-keyed.
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_srtp_media(),  # offers crypto tag 7
        credentials=_CREDENTIALS,
        response_timeout=2.0,
    )
    task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")
    # Build a secured answer whose a=crypto echoes a DIFFERENT tag (9, not 7).
    key = base64.b64encode(bytes(range(30))).decode("ascii")
    wrong = (
        "v=0\r\n"
        "o=- 2 2 IN IP4 198.51.100.99\r\n"
        "s=-\r\n"
        "c=IN IP4 198.51.100.99\r\n"
        "t=0 0\r\n"
        "m=audio 41000 RTP/SAVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        f"a=crypto:9 AES_CM_128_HMAC_SHA1_80 inline:{key}\r\n"
        "a=recvonly\r\n"
    )
    answer = build_response(
        reinvite,
        200,
        "OK",
        extra_headers=(("Content-Type", "application/sdp"),),
        body=wrong,
    )
    await session.on_response(SipResponse.parse(answer))
    with pytest.raises(CallError):
        await task
    assert media.rekeys == []  # never committed a mismatched key


async def test_rejected_srtp_reoffer_does_not_rekey_outbound() -> None:
    """A rejected SRTP re-INVITE must NOT leave outbound on an unaccepted key.

    Rollback/commit-timing (codex finding): the fresh outbound key is committed
    to the engine only AFTER the peer accepts the re-offer (HoldConfirmed). A
    491/timeout/4xx re-INVITE leaves the established outbound SRTP untouched, so
    media is never encrypted with a key the peer never agreed to.
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_srtp_media(),
        credentials=_CREDENTIALS,
        response_timeout=2.0,
    )
    task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")
    assert "RTP/SAVP" in reinvite.body  # the re-offer is still secured on the wire
    await session.on_response(
        SipResponse.parse(build_response(reinvite, 491, "Request Pending"))
    )
    with pytest.raises(CallError, match="glare"):
        await task
    # Rejected: the engine outbound SRTP was never re-keyed.
    assert media.rekeys == []


async def test_offerless_reinvite_on_srtp_reuses_current_key_no_rekey() -> None:
    """An offerless re-INVITE on an SRTP call re-advertises the CURRENT key.

    Continuity without ACK-answer plumbing (codex finding): for an offerless
    re-INVITE we offer in the 200 OK. We re-advertise the call's ESTABLISHED
    outbound key (not a fresh one) and do NOT re-key — so there is no dependency
    on parsing the peer's answer in the ACK, and inbound/outbound both stay on
    their agreed keys. The answer still carries RTP/SAVP + a=crypto (no downgrade).
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_srtp_media(),
        credentials=_CREDENTIALS,
        response_timeout=2.0,
    )
    # An offerless re-INVITE (no SDP body) → we offer in the 200 OK.
    await session.handle_request(_inbound("INVITE", body=""))
    response = SipResponse.parse(signaling.sent[-1])
    assert response.status_code == 200
    assert "RTP/SAVP" in response.body
    assert "RTP/AVP" not in response.body
    assert "a=crypto:7 AES_CM_128_HMAC_SHA1_80 inline:" in response.body
    # No re-key: the established keys are re-advertised, not rotated.
    assert media.rekeys == []


async def test_send_dtmf_delegates_to_media_without_reinvite() -> None:
    """CallSession.send_dtmf forwards the digits to media; no re-INVITE (ADR-0031)."""
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    await session.send_dtmf("12#")
    assert media.dtmf == ["12#"]
    # DTMF rides the established media path — no hold gating, no re-INVITE.
    assert media.holds == []
    with pytest.raises(AssertionError):  # no INVITE was ever sent
        _last_request(signaling, "INVITE")


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


async def test_provisional_stream_cannot_extend_past_deadline() -> None:
    # A peer dripping 1xx just under the per-response timeout must not keep the
    # verb alive forever — the wait is bounded by an absolute deadline (codex).
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_MEDIA,
        credentials=_CREDENTIALS,
        response_timeout=0.1,
    )
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")
    trying = SipResponse.parse(build_response(reinvite, 100, "Trying"))

    async def _drip() -> None:
        for _ in range(8):
            await asyncio.sleep(0.04)
            await session.on_response(trying)

    feeder = asyncio.create_task(_drip())
    start = loop.time()
    with pytest.raises(CallError, match="no final response"):
        await task
    elapsed = loop.time() - start
    feeder.cancel()
    assert elapsed < 0.25  # bounded by ~response_timeout, not 8 * 0.04 + timeout


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


async def test_hang_up_sends_bye_and_stops_media() -> None:
    """The agent hang-up tool path: send an in-dialog BYE, mark ended, stop media.

    ADR-0026 SOFT agent hangup: ``hang_up`` is the UAC side of a BYE — it sends a
    BYE request to the peer (advancing the dialog CSeq), flags the session ended,
    and stops the media engine (which ends the call loop so teardown classifies
    AGENT_HANGUP). Mirrors :meth:`_on_bye` but as the BYE *sender*.
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)

    await session.hang_up()

    bye = _last_request(signaling, "BYE")
    assert bye.method == "BYE"
    # In-dialog BYE: addressed to the peer's contact, carrying both dialog tags.
    assert "tag=ours" in (bye.header("From") or "")
    assert "tag=theirs" in (bye.header("To") or "")
    assert (bye.header("Call-ID") or "") == "call-1"
    # CSeq advanced past the dialog's local_cseq (2) — every request we send
    # uses a fresh, higher CSeq (ADR-0011 invariant 1).
    cseq = bye.header("CSeq") or ""
    assert cseq.endswith("BYE")
    assert int(cseq.split()[0]) == 3
    assert session.ended is True
    assert media.stopped is True


async def test_hang_up_is_idempotent() -> None:
    """A second hang_up after the call already ended is a harmless no-op.

    Once ended (by a prior hang_up or an inbound BYE), hang_up must not send a
    second BYE — the dialog is gone. This keeps the agent calling the tool twice
    (or racing an inbound BYE) from emitting a spurious second BYE.
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)

    await session.hang_up()
    byes_after_first = sum(
        1
        for t in signaling.sent
        if not t.startswith("SIP/2.0 ") and SipRequest.parse(t).method == "BYE"
    )
    await session.hang_up()
    byes_after_second = sum(
        1
        for t in signaling.sent
        if not t.startswith("SIP/2.0 ") and SipRequest.parse(t).method == "BYE"
    )

    assert byes_after_first == 1
    assert byes_after_second == 1, "a second hang_up must not send another BYE"


async def test_hang_up_completes_teardown_despite_bye_send_failure() -> None:
    """A BYE-send failure (dropped transport) must not abort local teardown.

    ADR-0026: we do not wait for the BYE's response because the call is over the
    moment we send it — a peer that never receives the BYE is the normal
    lost-packet case its own dialog timers already handle. That same rationale
    extends to the send itself raising (a TLS/WS/transport fault): the local
    session must still be marked ended and the media engine stopped, or the
    conversational loop never ends and the call is left half-torn-down.
    """
    signaling, media = _RaisingSignaling(), _FakeMedia()
    session = _session(signaling, media)

    await session.hang_up()  # must not raise

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


_VIDEO_ONLY_OFFER = "\r\n".join(
    (
        "v=0",
        "o=- 7 1 IN IP4 198.51.100.99",
        "s=-",
        "c=IN IP4 198.51.100.99",
        "t=0 0",
        "m=video 42002 RTP/AVP 96",
        "a=rtpmap:96 H264/90000",
        "a=sendrecv",
        "",
    )
)


async def test_inbound_reinvite_video_only_offer_is_rejected_not_answered() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)

    await session.handle_request(_inbound("INVITE", body=_VIDEO_ONLY_OFFER))

    response = SipResponse.parse(signaling.sent[-1])
    assert response.status_code == 488
    assert response.body == ""
    assert session.on_hold is False
    assert media.holds == []


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
            # Event:refer is the only package that carries a sipfrag body (RFC 3515).
            ("Event", "refer"),
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


async def test_inbound_notify_non_refer_event_answers_200_leaves_progress() -> None:
    """A non-``Event:refer`` NOTIFY is a plain 200 OK and never touches progress.

    ``_on_notify`` is the dispatch target for EVERY in-dialog NOTIFY, but only an
    ``Event: refer`` NOTIFY carries a ``message/sipfrag`` transfer-progress body
    (RFC 6665/3515). A NOTIFY for any other Event package (here ``message-summary``
    MWI) must NOT be fed to ``parse_notify_sipfrag``: its body has no ``SIP/2.0``
    status-line, so the old unconditional dispatch answered the peer's legitimate
    NOTIFY ``400 Bad Request``. The correct behaviour is a plain ``200 OK`` with
    ``transfer_progress`` left untouched.
    """
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
            ("Event", "message-summary"),
            ("Subscription-State", "active"),
            ("Content-Type", "application/simple-message-summary"),
        ),
        # A real MWI body — no SIP/2.0 status-line. parse_notify_sipfrag would raise.
        body="Messages-Waiting: yes\r\nVoice-Message: 2/0 (0/0)",
    )

    await session.handle_request(notify)

    responses = [t for t in signaling.sent if t.startswith("SIP/2.0 ")]
    assert len(responses) == 1
    assert SipResponse.parse(responses[-1]).status_code == 200
    # The transfer-progress field is for Event:refer only — never touched here.
    assert session.transfer_progress is None
    assert session.ended is False


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


async def test_inbound_refer_no_refer_to_answers_4xx_does_not_propagate() -> None:
    """A malformed REFER (no Refer-To) is answered 4xx, not fatal to the connection.

    ``parse_refer`` raises ``ReferError`` (a ``ValueError``) on a REFER with no
    ``Refer-To``. If that propagated out of ``handle_request`` it would mark the
    transport reader task failed and tear down the ENTIRE TLS/WSS signalling
    connection — a one-message DoS dropping every concurrent call. The malformed
    peer message must be ANSWERED (4xx), never crash the dialog: ``handle_request``
    returns normally and emits a single 4xx, no 202 and no handler invocation.
    """
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
            # no Refer-To header — parse_refer raises ReferError
        ),
        body="",
    )

    # Must NOT raise — propagation would tear down the whole connection.
    await session.handle_request(refer)

    responses = [t for t in signaling.sent if t.startswith("SIP/2.0 ")]
    assert len(responses) == 1, "exactly one response to the malformed REFER"
    status = SipResponse.parse(responses[-1]).status_code
    assert 400 <= status < 500, f"a malformed REFER is answered 4xx, got {status}"
    # No 202 Accepted was sent and the handler was never invoked.
    assert SipResponse.parse(responses[-1]).status_code != 202
    assert seen == []
    # The session is untouched — the dialog stays alive for other calls.
    assert session.ended is False


async def test_inbound_refer_hostile_refer_to_answers_4xx_not_202() -> None:
    """A REFER whose Refer-To fails the injection guard gets a 4xx and NO 202.

    Response ordering security fix: a 202 Accepted must only be sent once the
    REFER parses (target injection guard passes). A control-char / hostile
    ``Refer-To`` makes ``parse_refer`` raise ``ReferError``, so the answer is a
    4xx — never an accepted-then-rejected sequence — and the handler never runs.
    """
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
            # CR injection vector in the target — rejected by the injection guard.
            ("Refer-To", "<sip:4000@pbx.example.test%0d%0aEvil: x>"),
        ),
        body="",
    )

    await session.handle_request(refer)

    responses = [t for t in signaling.sent if t.startswith("SIP/2.0 ")]
    statuses = [SipResponse.parse(r).status_code for r in responses]
    assert 202 not in statuses, "a rejected REFER must never be Accepted (202)"
    assert len(responses) == 1
    assert 400 <= statuses[-1] < 500, f"hostile REFER answered 4xx, got {statuses[-1]}"
    assert seen == []


async def test_inbound_notify_malformed_answers_400_does_not_propagate() -> None:
    """A malformed NOTIFY (no Subscription-State) is answered 400, not fatal.

    ``parse_notify_sipfrag`` raises ``ReferError`` on a NOTIFY missing
    ``Subscription-State`` (or with a non-``SIP/2.0`` body). If that propagated
    out of ``handle_request`` it would tear down the whole signalling connection.
    The malformed message must be ANSWERED 400 and the connection left alive.
    """
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
            # Event:refer routes to parse_notify_sipfrag; the malformed body (missing
            # Subscription-State) is what makes it raise ReferError → 400.
            ("Event", "refer"),
            ("Content-Type", "message/sipfrag"),
            # no Subscription-State header — parse_notify_sipfrag raises ReferError
        ),
        body="SIP/2.0 200 OK",
    )

    # Must NOT raise — propagation would tear down the whole connection.
    await session.handle_request(notify)

    responses = [t for t in signaling.sent if t.startswith("SIP/2.0 ")]
    assert len(responses) == 1
    assert SipResponse.parse(responses[-1]).status_code == 400
    # The progress field is left unchanged and the session stays alive.
    assert session.transfer_progress is None
    assert session.ended is False


async def test_dialog_id_routes_inbound() -> None:
    session = _session(_FakeSignaling(), _FakeMedia())
    assert session.dialog_id == ("call-1", "ours", "theirs")


# ---- outbound: refresh_session hold/unhold direction -------------------------


async def test_refresh_session_while_held_offers_sendonly() -> None:
    """refresh_session with on_hold=True must offer a=sendonly in the re-INVITE body.

    Killing the ``not self.on_hold`` mutant at call.py line 439: if the branch were
    inverted the refresh re-INVITE would offer ``sendrecv`` while the call is held,
    silently un-holding it at the SDP layer while ``on_hold`` and the media engine
    hold-gate stayed set (media/signalling split).
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    session.on_hold = True
    extra_headers = [
        ("Session-Expires", "600;refresher=uas"),
        ("Supported", "timer"),
    ]
    task = asyncio.create_task(session.refresh_session(extra_headers))
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")
    assert "a=sendonly" in reinvite.body
    assert "a=sendrecv" not in reinvite.body
    await session.on_response(SipResponse.parse(_answer_to(reinvite, "recvonly")))
    outcome = await task
    assert isinstance(outcome, RefreshSucceeded)


async def test_refresh_session_while_not_held_offers_sendrecv() -> None:
    """refresh_session with on_hold=False must offer a=sendrecv in the re-INVITE body.

    Companion to test_refresh_session_while_held_offers_sendonly: verifies the
    non-held branch picks ``sendrecv``.  A ``always sendonly`` mutant at line 439
    would fail this test.
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    assert session.on_hold is False
    extra_headers = [
        ("Session-Expires", "600;refresher=uas"),
        ("Supported", "timer"),
    ]
    task = asyncio.create_task(session.refresh_session(extra_headers))
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")
    assert "a=sendrecv" in reinvite.body
    assert "a=sendonly" not in reinvite.body
    await session.on_response(SipResponse.parse(_answer_to(reinvite, "sendrecv")))
    outcome = await task
    assert isinstance(outcome, RefreshSucceeded)


# ---- fail-closed: a header-incomplete in-dialog request must not tear down the
# ---- whole signalling connection (ADR-0081 class, response-build side) ------


def _header_incomplete(
    method: str, *, omit: str, body: str = "", cseq: int = 9
) -> SipRequest:
    """An in-dialog request routed to this call but missing one mandatory echo header.

    ``manager._route_in_dialog`` keys an in-dialog request on its To-tag, From-tag and
    Call-ID ONLY — never Via or CSeq. So a request carrying those three (here the
    dialog's ``ours``/``theirs`` tags + ``call-1``) that ``omit``s a header used only to
    ECHO into the response — Via or CSeq — still routes ``InDialog`` to the established
    :class:`CallSession` and reaches an inline ``build_response``, which raises
    :class:`ValueError` because it cannot echo the missing header (RFC 3261 §8.2.6). The
    tests omit Via / CSeq for exactly that reason; omitting To / From / Call-ID would
    instead fail routing upstream and never reach ``build_response``.
    """
    headers = [
        ("Via", "SIP/2.0/TLS 198.51.100.99:5061;branch=z9hG4bK-in"),
        ("From", "<sip:2000@pbx.example.test>;tag=theirs"),
        ("To", "<sip:1000@pbx.example.test>;tag=ours"),
        ("Call-ID", "call-1"),
        ("CSeq", f"{cseq} {method}"),
    ]
    if body:
        headers.append(("Content-Type", "application/sdp"))
    return SipRequest(
        method=method,
        request_uri="sip:1000@198.51.100.7:5061",
        headers=tuple((name, value) for name, value in headers if name != omit),
        body=body,
    )


async def test_established_call_survives_a_header_incomplete_bye() -> None:
    """A header-incomplete in-dialog BYE is dropped WHOLE, not a connection teardown.

    An established call answers an inbound BYE ``200 OK`` inline in the transport
    reader task (``handle_request`` -> ``_on_bye`` -> ``build_response``). A BYE that
    routes here (To/From tags + Call-ID) but omits its ``Via`` cannot be answered:
    ``build_response`` raises :class:`ValueError`. Unguarded, that ``ValueError``
    escapes ``handle_request``, unwinds the reader (``_dispatch_request`` ->
    ``_read_loop``) and fires ``on_connection_lost`` — dropping every OTHER live call
    and the registration on the shared signalling connection over one packet (the
    ADR-0081 DoS class, response-build side). Fail closed: ``handle_request`` must DROP
    the malformed BYE whole — send no ``200`` and (a request we cannot answer must have
    no effect) leave the call NOT ended and its media NOT stopped — and must NOT raise,
    so the reader and every other call on the connection stay alive.
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    # Must not raise: a ``ValueError`` propagating out of here IS the reader unwind.
    await session.handle_request(_header_incomplete("BYE", omit="Via"))
    assert signaling.sent == []  # unanswerable -> no 200 built or sent
    assert session.ended is False  # drop-whole: the malformed BYE did not end the call
    assert media.stopped is False
    # The session is not wedged: a subsequent well-formed BYE is still answered 200
    # (the reader-survival analogue — the dispatcher keeps serving in-dialog requests;
    # the 200 proves handle_request still functions after the malformed drop). The
    # ended/stopped transition itself is covered by test_inbound_bye_answers_200_...
    await session.handle_request(_inbound("BYE"))
    assert SipResponse.parse(signaling.sent[-1]).status_code == 200


async def test_established_srtp_call_survives_header_incomplete_reinvite_no_rekey() -> (
    None
):
    """A header-incomplete secured re-INVITE is dropped WHOLE — no partial SRTP re-key.

    Answering a peer re-INVITE on an SRTP call re-keys the media engine (via
    ``_answer_reinvite`` -> ``_reanswer_crypto``) and bumps the SDP version BEFORE the
    ``200`` is built. A re-INVITE carrying a usable ``a=crypto`` (so it reaches the
    re-key path) but omitting its ``Via`` cannot be answered: ``build_response`` raises.
    Unguarded, the engine is left re-keyed to a key the peer never saw us confirm, AND
    the ``ValueError`` unwinds the reader. Fail closed: answerability is checked BEFORE
    the irreversible re-key, so an unanswerable re-INVITE re-keys NOTHING, sends no
    ``200``, and does not raise.
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_srtp_media(),
        credentials=_CREDENTIALS,
        response_timeout=2.0,
    )
    offer = build_audio_offer(
        local_address="198.51.100.99",
        port=42000,
        codecs=(_PCMU,),
        direction="sendonly",
        session_id=7,
        crypto=_srtp_crypto(),
    )
    await session.handle_request(_header_incomplete("INVITE", omit="Via", body=offer))
    assert signaling.sent == []  # unanswerable -> no 200
    assert media.rekeys == []  # drop-whole: the SRTP context was NOT re-keyed
    # Not wedged: a subsequent well-formed secured re-INVITE is still answered + rekeys.
    await session.handle_request(_inbound("INVITE", body=offer))
    assert SipResponse.parse(signaling.sent[-1]).status_code == 200
    assert media.rekeys, "a well-formed secured re-INVITE must still re-key the engine"


async def test_header_incomplete_in_dialog_drop_is_logged_non_pii(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fail-closed drop emits a WARNING carrying no wire content (rule 34).

    The dropped request is logged at WARNING with only the method kind and the
    exception TYPE name — never the request's From/To/Call-ID or any host (the repo is
    PUBLIC). Before the fix no WARNING is emitted at all (the ``ValueError`` escapes
    instead of being caught and logged).
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    with caplog.at_level(logging.WARNING, logger="hermes_voip.call"):
        await session.handle_request(_header_incomplete("BYE", omit="CSeq"))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a header-incomplete in-dialog drop must log a WARNING"
    blob = " ".join(r.getMessage() for r in warnings)
    assert "ValueError" in blob  # the exception type is surfaced for diagnosis
    # No PII / wire content: none of the request's tags, Call-ID or host leaks.
    assert "theirs" not in blob
    assert "call-1" not in blob
    assert "pbx.example.test" not in blob


# ---- fail-closed: an inbound RESPONSE whose CSeq number is a unicode "digit"
# ---- must not tear down the whole signalling connection (ADR-0081 class,
# ---- response-correlation side; sibling of the handle_request fix above) -----


def _response_with_cseq(cseq: str) -> SipResponse:
    """A ``200 OK`` routed to this call by Call-ID, carrying an arbitrary ``CSeq``.

    The transport routes a response to a :class:`CallSession` by Call-ID (``call-1``)
    and calls ``on_response``, whose ONLY use of the message is to parse the CSeq
    NUMBER (via ``_cseq_number``) to correlate the response to the outstanding verb.
    A raw ``cseq`` string forges a number ``int()`` cannot parse without tripping the
    parser — ``SipResponse.parse`` stores the ``CSeq`` header verbatim.
    """
    raw = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bK-resp\r\n"
        "From: <sip:2000@pbx.example.test>;tag=theirs\r\n"
        "To: <sip:1000@pbx.example.test>;tag=ours\r\n"
        "Call-ID: call-1\r\n"
        f"CSeq: {cseq}\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )
    return SipResponse.parse(raw)


async def test_established_call_survives_a_unicode_digit_cseq_response() -> None:
    """An inbound response with a unicode-digit CSeq is ignored, not a teardown.

    The transport routes a response to a :class:`CallSession` by Call-ID and calls
    ``on_response``, which parses the CSeq number via ``_cseq_number`` to correlate
    it to the outstanding verb. ``_cseq_number`` guarded ``int(parts[0])`` with
    ``str.isdigit()`` — ``True`` for the superscript ``²`` (U+00B2) — yet
    ``int("²")`` raises a bare :class:`ValueError`. ``on_response`` runs in the
    transport reader task OUTSIDE its parse-only ``except ValueError``, so an inbound
    ``CSeq: ² BYE`` response whose Call-ID matches an active call escaped
    ``on_response`` -> ``_read_loop`` -> ``on_connection_lost``, tearing down every
    call and the registration on the shared SIP-over-TLS/WSS connection over one
    packet (the ADR-0081 DoS class, response-correlation side — the sibling of the
    ``handle_request`` fix above).

    Fail closed: a non-decimal CSeq number takes the SAME path as any other
    uncorrelatable CSeq — ``_cseq_number`` returns ``None`` and ``on_response`` drops
    the response — so ``int()`` is never reached and the reader survives. Proven by
    keeping a legitimate re-INVITE outstanding (the "unrelated" traffic on the shared
    connection): the ``²`` response must neither raise nor disturb it, and the
    well-formed answer that follows must still correlate and complete the hold (no
    wedge, no over-drop).
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    # A real verb is outstanding on this session: hold() sends a re-INVITE and awaits
    # its final response (registering the re-INVITE's CSeq in _pending).
    task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")

    # The forged response arrives on the same connection. Must NOT raise: a
    # ValueError propagating out of on_response IS the reader unwind (a whole-
    # connection teardown). ² = U+00B2 — isdigit()-True but int()-unparseable.
    await session.on_response(_response_with_cseq("² BYE"))

    # Not wedged / no over-drop: the outstanding re-INVITE's correlation is intact,
    # so its well-formed 200 answer still completes the hold.
    await session.on_response(SipResponse.parse(_answer_to(reinvite, "recvonly")))
    await task
    assert session.on_hold is True
    assert media.holds == [True]


async def test_established_call_survives_an_oversized_ascii_cseq_response() -> None:
    """An inbound response with an over-long ASCII-decimal CSeq is dropped, not fatal.

    Gating ``int()`` on ``isascii()+isdecimal()`` closes the non-decimal unicode-digit
    escape but is NOT sufficient on its own: an all-ASCII-decimal CSeq number longer
    than Python's (configurable, >= 640) integer-string-conversion limit still makes
    ``int(parts[0])`` raise a bare :class:`ValueError` (``Exceeds the limit ... for
    integer string conversion``) — the SAME escape, via the SAME ``on_response`` ->
    reader path that tears down the shared SIP-over-TLS/WSS connection.

    ``_cseq_number`` calls ``int()`` on the ASCII-decimal token inside a
    ``try``/``except ValueError`` and classifies the digit-limit overflow as
    uncorrelatable (``None``) instead of letting it escape. Proven, as for the unicode
    case, with a legitimate re-INVITE kept outstanding: the oversized response must
    neither raise nor disturb it, and the well-formed answer that follows still
    completes the hold.
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    task = asyncio.create_task(session.hold())
    await asyncio.sleep(0)
    reinvite = _last_request(signaling, "INVITE")

    # A CSeq number far longer than CPython's integer-string conversion limit (default
    # 4300; 0 disables it), yet every character is isascii()+isdecimal(). Pre-fix this
    # made int() raise and escape; now int() runs inside try/except so the overflow (or,
    # if the limit is disabled, the resulting out-of-range value) maps to None.
    limit = sys.get_int_max_str_digits()
    oversized = "9" * (limit + 1 if limit else 5000)
    await session.on_response(_response_with_cseq(f"{oversized} BYE"))  # must NOT raise

    await session.on_response(SipResponse.parse(_answer_to(reinvite, "recvonly")))
    await task
    assert session.on_hold is True
    assert media.holds == [True]


async def test_cseq_number_rejects_a_number_at_or_above_2_31() -> None:
    """_cseq_number drops a CSeq value outside the valid SIP range (RFC 3261 §8.1.1.5).

    A SIP CSeq sequence number is ``< 2**31``. The largest valid value (``2**31 - 1``)
    still parses; ``2**31`` and any larger value (e.g. ``9999999999``) is out of range
    and returns ``None`` — dropped as uncorrelatable, never mistaken for a real sequence
    number. Range enforcement is not observable through ``on_response`` (whose
    ``_pending`` only ever holds our own small CSeqs), so the boundary is pinned here
    directly.
    """
    assert _cseq_number("2147483647 BYE") == 2**31 - 1  # largest valid CSeq
    assert _cseq_number("2147483648 BYE") is None  # 2**31: out of range
    assert _cseq_number("9999999999 BYE") is None  # 10 digits but >= 2**31


async def test_cseq_number_accepts_a_leading_zero_padded_cseq() -> None:
    """Leading zeros are valid in a SIP CSeq number; its VALUE is what counts.

    RFC 3261's CSeq sequence-number grammar is `1*DIGIT`, which permits leading
    zeros, so ``00000000001`` is a valid encoding of sequence number 1 and MUST
    correlate as 1 — never dropped for its digit count. ``_cseq_number`` normalizes
    via ``int()`` (``int("00000000001") == 1``) and range-checks the VALUE, not the
    length; a bounded-length guard would wrongly drop a legitimately padded CSeq an
    RFC-compliant peer may send (a real over-drop, not a fail-closed non-numeric case).
    """
    assert _cseq_number("00000000001 BYE") == 1  # 11 digits, value 1: not over-dropped
    # A heavily padded largest-valid CSeq still normalizes and is accepted here.
    assert _cseq_number("0000000000002147483647 INVITE") == 2**31 - 1


# ---- fail-closed: a malformed in-dialog re-INVITE SDP OFFER must not tear down the
# ---- whole signalling connection (ADR-0081 class, offer-parse side; uncovered by #391
# ---- which guarded only the build_response/answer sites) --------------------


async def test_established_call_survives_a_malformed_sdp_reinvite() -> None:
    """A re-INVITE with an unparseable SDP offer is rejected 400, not a teardown.

    An established call classifies an inbound re-INVITE's offer INLINE in the transport
    reader task (handle_request -> _on_reinvite -> classify_inbound_reinvite ->
    SessionDescription.parse). A non-empty but malformed offer body — here a non-numeric
    ``m=`` port — makes SessionDescription.parse raise ``SdpError`` (a ``ValueError``
    subclass). Unguarded, that SdpError escapes _on_reinvite -> handle_request and
    unwinds the reader -> on_connection_lost, dropping every OTHER live call and the
    registration on the shared connection over one packet. This is the ADR-0081 class on
    the offer-PARSE side (#391 guarded only the build_response answer sites, not this
    classify/parse site). Fail closed: _on_reinvite REJECTS the malformed re-INVITE
    ``400`` and does NOT raise (reader + other calls survive); classify runs first, so
    no call/dialog state is mutated.
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    # Must not raise: an SdpError propagating out of here IS the reader unwind.
    await session.handle_request(_inbound("INVITE", body="m=audio x RTP/AVP 0"))
    assert SipResponse.parse(signaling.sent[-1]).status_code == 400  # offer rejected
    # Drop-whole: the malformed re-INVITE flipped no hold/media state.
    assert session.on_hold is False
    assert media.holds == []
    # Not wedged: a subsequent WELL-FORMED re-INVITE is still answered 200.
    good_offer = build_audio_offer(
        local_address="198.51.100.99",
        port=42000,
        codecs=(_PCMU,),
        direction="sendonly",
        session_id=7,
    )
    await session.handle_request(_inbound("INVITE", body=good_offer))
    assert SipResponse.parse(signaling.sent[-1]).status_code == 200


async def test_malformed_sdp_reinvite_reject_is_logged_non_pii(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rejecting a malformed re-INVITE offer logs a WARNING with no wire content.

    The log carries only the exception TYPE — never the offer body or any dialog
    identifier (the repo is PUBLIC, rule 34). Before the fix no WARNING is emitted (the
    SdpError escapes instead of being caught, rejected 400, and logged).
    """
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    with caplog.at_level(logging.WARNING, logger="hermes_voip.call"):
        await session.handle_request(_inbound("INVITE", body="m=audio x RTP/AVP 0"))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a rejected malformed re-INVITE offer must log a WARNING"
    blob = " ".join(r.getMessage() for r in warnings)
    assert "SdpError" in blob  # the exception type is surfaced for diagnosis
    # No wire content / PII: neither the offer body nor any dialog identifier leaks.
    assert "m=audio" not in blob
    assert "theirs" not in blob
    assert "call-1" not in blob
    assert "pbx.example.test" not in blob
