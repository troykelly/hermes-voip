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

import pytest

from hermes_voip.call import CallError, CallSession
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
