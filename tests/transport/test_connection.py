"""Integration tests for the SIP-over-TLS transport against a loopback server.

These exercise the real :class:`~hermes_voip.transport.connection.SipOverTlsTransport`
over a genuine asyncio TLS socket to a loopback SIP server (``_loopback``), with
the production framer and the merged sans-IO stack (RegistrationManager / route).
No live gateway — that is W16. Asserts:

* a REGISTER round-trips (401 → authenticated → 200) to ``registered`` via the
  :class:`~hermes_voip.manager.RegistrationManager`;
* an inbound INVITE is demuxed to a :class:`~hermes_voip.manager.NewCall`;
* a non-2xx final to an INVITE **we** sent is ACKed by this layer (same branch);
* coalesced/split TCP reads still frame correctly over the real socket.

Fakes only — ``pbx.example.test``, ext ``1000``, ``127.0.0.1``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from hermes_voip.config import GatewayConfig, load_gateway_config
from hermes_voip.manager import NewCall, RegistrationManager
from hermes_voip.message import (
    SipRequest,
    SipResponse,
    build_request,
    build_response,
    new_branch,
    new_call_id,
    new_tag,
)
from hermes_voip.transport.connection import SipOverTlsTransport

from ._loopback import LoopbackSipServer, Responder, client_ssl_context

pytestmark = pytest.mark.asyncio


async def _until(
    predicate: Callable[[], bool], *, timeout: float = 3.0, step: float = 0.01
) -> None:
    """Poll ``predicate`` until true or the timeout elapses (no fixed sleeps)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            msg = "condition not met within the timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(step)


def _gateway() -> GatewayConfig:
    return load_gateway_config(
        {
            "HERMES_SIP_HOST": "pbx.example.test",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "s3cr3t",
            "HERMES_SIP_EXPIRES": "120",
        }
    )


def _challenge(reg: SipRequest) -> str:
    return (
        "SIP/2.0 401 Unauthorized\r\n"
        f"Via: {reg.header('Via')}\r\n"
        f"From: {reg.header('From')}\r\n"
        f"To: {reg.header('To')};tag=reg-srv\r\n"
        f"Call-ID: {reg.header('Call-ID')}\r\n"
        f"CSeq: {reg.header('CSeq')}\r\n"
        'WWW-Authenticate: Digest realm="pbx.example.test", nonce="abc123", '
        'algorithm=MD5, qop="auth"\r\n'
        "Content-Length: 0\r\n\r\n"
    )


def _register_ok(reg: SipRequest, *, expires: int = 120) -> str:
    return (
        "SIP/2.0 200 OK\r\n"
        f"Via: {reg.header('Via')}\r\n"
        f"From: {reg.header('From')}\r\n"
        f"To: {reg.header('To')};tag=reg-srv\r\n"
        f"Call-ID: {reg.header('Call-ID')}\r\n"
        f"CSeq: {reg.header('CSeq')}\r\n"
        f"Contact: {reg.header('Contact')};expires={expires}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


# --------------------------------------------------------------------------
# REGISTER round-trip through the RegistrationManager
# --------------------------------------------------------------------------


async def test_register_round_trips_to_registered() -> None:
    server = LoopbackSipServer(_register_responder())
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        up = await manager.connect(timeout=3.0)
        assert up is True
        snapshot = manager.snapshot()
        assert snapshot[0].registered is True
        assert snapshot[0].expires == 120
        # The server saw two REGISTERs: the first (challenged) and the authed one.
        registers = [r for r in server.received if r.startswith("REGISTER ")]
        assert len(registers) == 2
        assert "Authorization: Digest" in registers[1]
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


def _register_responder() -> Responder:
    state = {"seen": 0}

    async def respond(request: SipRequest) -> list[str]:
        if request.method != "REGISTER":
            return []
        state["seen"] += 1
        if state["seen"] == 1:
            return [_challenge(request)]
        return [_register_ok(request)]

    return respond


# --------------------------------------------------------------------------
# Inbound INVITE → NewCall routing
# --------------------------------------------------------------------------


async def test_inbound_invite_routes_to_new_call() -> None:
    new_calls: list[NewCall] = []

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
        on_new_call=new_calls.append,
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        # The server pushes an inbound INVITE down the established connection.
        await server.push(_inbound_invite(to_user="1000"))
        await _until(lambda: len(new_calls) == 1)
        assert len(new_calls) == 1
        assert new_calls[0].registration.extension == "1000"
        assert new_calls[0].invite.method == "INVITE"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


def _inbound_invite(*, to_user: str) -> str:
    return (
        f"INVITE sip:{to_user}@127.0.0.1:5061;transport=tls SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 203.0.113.5:5061;branch=z9hG4bKinv1;rport\r\n"
        "Max-Forwards: 70\r\n"
        "From: <sip:caller@pbx.example.test>;tag=remote\r\n"
        f"To: <sip:{to_user}@pbx.example.test>\r\n"
        f"Call-ID: {new_call_id()}\r\n"
        "CSeq: 1 INVITE\r\n"
        "Contact: <sip:caller@203.0.113.5:5061;transport=tls>\r\n"
        "Content-Type: application/sdp\r\n"
        "Content-Length: 0\r\n\r\n"
    )


# --------------------------------------------------------------------------
# Inbound CANCEL aborts the pending INVITE (RFC 3261 §9.2)
# --------------------------------------------------------------------------


def _inbound_invite_with(*, to_user: str, call_id: str, branch: str) -> str:
    return (
        f"INVITE sip:{to_user}@127.0.0.1:5061;transport=tls SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 203.0.113.5:5061;branch={branch};rport\r\n"
        "Max-Forwards: 70\r\n"
        "From: <sip:caller@pbx.example.test>;tag=remote\r\n"
        f"To: <sip:{to_user}@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 INVITE\r\n"
        "Contact: <sip:caller@203.0.113.5:5061;transport=tls>\r\n"
        "Content-Type: application/sdp\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _cancel_for(*, to_user: str, call_id: str, branch: str) -> str:
    # §9.1: same Request-URI/Call-ID/From/To and top Via branch as the INVITE,
    # CSeq number unchanged but method CANCEL.
    return (
        f"CANCEL sip:{to_user}@127.0.0.1:5061;transport=tls SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 203.0.113.5:5061;branch={branch};rport\r\n"
        "Max-Forwards: 70\r\n"
        "From: <sip:caller@pbx.example.test>;tag=remote\r\n"
        f"To: <sip:{to_user}@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 CANCEL\r\n"
        "Content-Length: 0\r\n\r\n"
    )


async def test_inbound_cancel_487s_invite_and_200s_cancel_and_aborts() -> None:
    # A caller who abandons setup sends CANCEL (RFC 3261 §9.2). The transport must
    # answer the CANCEL 200 OK, the pending INVITE 487 Request Terminated, and fire
    # the on_cancel hook (Call-ID) so the adapter tears down the half-built call.
    new_calls: list[NewCall] = []
    cancelled: list[str] = []
    call_id = new_call_id()
    branch = "z9hG4bKcancelme"

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
        on_new_call=new_calls.append,
        on_cancel=cancelled.append,
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await server.push(
            _inbound_invite_with(to_user="1000", call_id=call_id, branch=branch)
        )
        await _until(lambda: len(new_calls) == 1)
        # The caller now abandons: a CANCEL for the same transaction.
        await server.push(_cancel_for(to_user="1000", call_id=call_id, branch=branch))
        # The transport answers the CANCEL 200 OK ...
        ok = await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 200") and "CSeq: 1 CANCEL" in raw,
            timeout=3.0,
        )
        assert "CSeq: 1 CANCEL" in ok
        # ... and the pending INVITE 487 Request Terminated.
        terminated = await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 487"), timeout=3.0
        )
        assert "CSeq: 1 INVITE" in terminated
        # ... and the abort hook fired with this call's Call-ID.
        await _until(lambda: cancelled == [call_id])
        assert cancelled == [call_id]
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_cancel_suppresses_a_late_200_ok_for_the_dead_invite() -> None:
    # The core defect: even after CANCEL, the plugin could still 200-OK the dead
    # INVITE (the answer task may already be in flight). Once an INVITE has been
    # CANCELled, the transport MUST drop a late 200 OK for that INVITE — the caller
    # is gone, so answering it would strand a ghost call.
    new_calls: list[NewCall] = []
    call_id = new_call_id()
    branch = "z9hG4bKlate200"

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
        on_new_call=new_calls.append,
        on_cancel=lambda _cid: None,
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        invite = _inbound_invite_with(to_user="1000", call_id=call_id, branch=branch)
        await server.push(invite)
        await _until(lambda: len(new_calls) == 1)
        await server.push(_cancel_for(to_user="1000", call_id=call_id, branch=branch))
        await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 487"), timeout=3.0
        )
        # The adapter's answer task, unaware of the race, now tries to 200 OK the
        # INVITE. The transport must SUPPRESS it (the call is dead).
        late_200 = build_response(
            SipRequest.parse(invite),
            200,
            "OK",
            to_tag=new_tag(),
            extra_headers=(("Contact", "<sip:1000@127.0.0.1:5061;transport=tls>"),),
        )
        await transport.send(late_200)
        await asyncio.sleep(0.1)
        # No 200 OK to the INVITE reached the wire (only the 487 did).
        invite_2xx = [
            raw
            for raw in server.received
            if raw.startswith("SIP/2.0 200") and "CSeq: 1 INVITE" in raw
        ]
        assert invite_2xx == [], "a 200 OK for a CANCELled INVITE must be suppressed"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_cancel_for_unknown_invite_is_481() -> None:
    # RFC 3261 §9.2: a CANCEL that matches no existing INVITE server transaction is
    # answered 481 Call/Transaction Does Not Exist — never a 200 OK that would imply
    # a transaction was cancelled.
    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
        on_new_call=lambda _nc: None,
        on_cancel=lambda _cid: None,
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await server.push(
            _cancel_for(to_user="1000", call_id="never-seen", branch="z9hG4bKghost")
        )
        resp = await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 481"), timeout=3.0
        )
        assert "CSeq: 1 CANCEL" in resp
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_retransmitted_cancel_is_absorbed_no_second_487_or_abort() -> None:
    # A retransmitted CANCEL (same branch+Call-ID) must be absorbed: the 200 OK to
    # the CANCEL is re-sent, but the INVITE is NOT 487'd a second time and the
    # abort hook fires only once (idempotent RFC 3261 §9.2 — codex finding).
    new_calls: list[NewCall] = []
    cancelled: list[str] = []
    call_id = new_call_id()
    branch = "z9hG4bKretx"

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
        on_new_call=new_calls.append,
        on_cancel=cancelled.append,
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await server.push(
            _inbound_invite_with(to_user="1000", call_id=call_id, branch=branch)
        )
        await _until(lambda: len(new_calls) == 1)
        cancel = _cancel_for(to_user="1000", call_id=call_id, branch=branch)
        await server.push(cancel)
        await _until(lambda: cancelled == [call_id])
        await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 487"), timeout=3.0
        )
        # The peer retransmits the CANCEL.
        await server.push(cancel)
        await asyncio.sleep(0.1)
        # Exactly one 487 (the retransmit was absorbed) and one abort.
        terminated = [raw for raw in server.received if raw.startswith("SIP/2.0 487")]
        assert len(terminated) == 1, "a retransmitted CANCEL must not re-487 the INVITE"
        assert cancelled == [call_id], "the abort hook must fire only once"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


# --------------------------------------------------------------------------
# Outbound CANCEL for an INVITE WE sent (RFC 3261 §9.1)
# --------------------------------------------------------------------------


_OK_STATUS = 200  # a 2xx final response to our INVITE


class _RecordingSink:
    """A :class:`CallResponseSink` that records every response routed to it."""

    def __init__(self) -> None:
        self.responses: list[SipResponse] = []

    async def on_response(self, response: SipResponse) -> None:
        self.responses.append(response)


async def test_send_cancel_builds_a_rfc9_1_cancel_for_our_invite() -> None:
    # RFC 3261 §9.1: a UAC that gives up on an INVITE for which it has had no final
    # response sends a CANCEL whose Request-URI/Call-ID/From/To match the INVITE,
    # whose top Via carries the SAME branch, and whose CSeq has the SAME number with
    # method CANCEL — and no body.
    branch = new_branch()
    call_id = new_call_id()

    async def respond(_request: SipRequest) -> list[str]:
        return []  # never answer — the call is "ringing"

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    try:
        invite_text = _outbound_invite(branch, call_id=call_id)
        await transport.send(invite_text)
        await server.wait_for_received(lambda raw: raw.startswith("INVITE "))

        sent = await transport.send_cancel(call_id)
        assert sent is True, "send_cancel must report it sent a CANCEL"

        cancel_raw = await server.wait_for_received(
            lambda raw: raw.startswith("CANCEL "), timeout=3.0
        )
        cancel = SipRequest.parse(cancel_raw)
        invite = SipRequest.parse(invite_text)

        # §9.1: Request-URI matches the INVITE.
        assert cancel.request_uri == invite.request_uri
        # Top Via carries the SAME branch as the INVITE.
        cancel_via = cancel.header("Via")
        assert cancel_via is not None
        assert f"branch={branch}" in cancel_via
        # CSeq: same number, method CANCEL.
        assert cancel.header("CSeq") == "1 CANCEL"
        # Call-ID / From / To echoed verbatim (To carries no added tag).
        assert cancel.header("Call-ID") == call_id
        assert cancel.header("From") == invite.header("From")
        assert cancel.header("To") == invite.header("To")
        # No body.
        assert cancel.body == ""
        assert cancel.header("Content-Length") == "0"
    finally:
        await transport.aclose()
        await server.stop()


async def test_send_cancel_returns_false_when_no_invite_is_tracked() -> None:
    # An outbound call that has no in-flight INVITE for this Call-ID (never sent, or
    # already given its final response) cannot be CANCELled — send_cancel is a no-op
    # that reports False and puts nothing on the wire.
    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    try:
        sent = await transport.send_cancel("never-sent-this-call")
        assert sent is False
        await asyncio.sleep(0.05)
        assert not any(raw.startswith("CANCEL ") for raw in server.received)
    finally:
        await transport.aclose()
        await server.stop()


async def test_send_cancel_uses_the_re_auth_invite_branch_after_a_challenge() -> None:
    # After a 401/407 challenge the UAC re-sends the INVITE with a NEW branch (CSeq 2).
    # A CANCEL must target the LATEST in-flight transaction — the re-auth branch —
    # since that is the one still awaiting a final response (§9.1).
    branch1 = new_branch()
    branch2 = new_branch()
    call_id = new_call_id()

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    try:
        await transport.send(_outbound_invite(branch1, call_id=call_id))
        await transport.send(_outbound_invite(branch2, call_id=call_id, cseq=2))
        await server.wait_for_received(
            lambda raw: raw.startswith("INVITE ") and f"branch={branch2}" in raw
        )
        assert await transport.send_cancel(call_id) is True
        cancel_raw = await server.wait_for_received(
            lambda raw: raw.startswith("CANCEL "), timeout=3.0
        )
        cancel = SipRequest.parse(cancel_raw)
        via = cancel.header("Via")
        assert via is not None
        assert f"branch={branch2}" in via, "CANCEL must target the re-auth branch"
        assert cancel.header("CSeq") == "2 CANCEL"
    finally:
        await transport.aclose()
        await server.stop()


async def test_late_200_to_invite_after_cancel_is_suppressed_and_acked_byed() -> None:
    # RFC 3261 §9.1 glare: a 2xx can race the CANCEL. The transport must NOT surface
    # that 2xx to the call's response sink (the call is cancelled, not answered), and
    # because the 2xx established a dialog on the callee it must ACK then BYE it so the
    # remote is not stranded. Mirrors the inbound §9.2 late-200 suppression.
    branch = new_branch()
    call_id = new_call_id()
    sink = _RecordingSink()

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    transport.add_call(call_id, sink)
    try:
        invite_text = _outbound_invite(branch, call_id=call_id)
        await transport.send(invite_text)
        await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
        assert await transport.send_cancel(call_id) is True
        await server.wait_for_received(lambda raw: raw.startswith("CANCEL "))

        # The gateway, racing our CANCEL, answers the INVITE 200 OK.
        late_200 = _final_2xx_with_contact(SipRequest.parse(invite_text))
        await server.push(late_200)

        # The transport must ACK then BYE the racing 2xx (clean up the remote dialog).
        ack = await server.wait_for_received(
            lambda raw: raw.startswith("ACK ") and f"Call-ID: {call_id}" in raw,
            timeout=3.0,
        )
        assert "CSeq: 1 ACK" in ack
        bye = await server.wait_for_received(
            lambda raw: raw.startswith("BYE ") and f"Call-ID: {call_id}" in raw,
            timeout=3.0,
        )
        assert bye.startswith("BYE ")

        # The suppressed 2xx must NOT have been delivered to the call's sink as a
        # successful answer.
        await asyncio.sleep(0.1)
        invite_2xx = [r for r in sink.responses if r.status_code == _OK_STATUS]
        assert invite_2xx == [], (
            "a 200 OK racing the CANCEL must be suppressed at the sink"
        )
    finally:
        await transport.aclose()
        await server.stop()


def _final_2xx_with_contact(invite: SipRequest) -> str:
    """A 200 OK to our outbound INVITE with a To-tag and a Contact (a real answer)."""
    return (
        "SIP/2.0 200 OK\r\n"
        f"Via: {invite.header('Via')}\r\n"
        f"From: {invite.header('From')}\r\n"
        f"To: {invite.header('To')};tag=answered-srv\r\n"
        f"Call-ID: {invite.header('Call-ID')}\r\n"
        f"CSeq: {invite.header('CSeq')}\r\n"
        "Contact: <sip:2000@127.0.0.1:5061;transport=tls>\r\n"
        "Content-Length: 0\r\n\r\n"
    )


async def test_remove_call_sink_mismatch_still_clears_outbound_cancel_tracking() -> None:
    # Fix (a): remove_call's sink-identity early-return (an earlier call's teardown
    # must not evict a later same-Call-ID sink) used to sit ABOVE the outbound
    # CANCEL-tracking cleanup, so a sink-mismatch remove_call returned before clearing
    # _outbound_invites / _cancelled_outbound — leaking the CANCEL tracking for a call
    # that is being torn down. The outbound-tracking cleanup must run regardless of the
    # sink identity. Observable: after a CANCEL + a MISMATCHED remove_call, a second
    # send_cancel for the same Call-ID has nothing tracked and returns False.
    branch = new_branch()
    call_id = new_call_id()
    owner_sink = _RecordingSink()
    other_sink = _RecordingSink()

    async def respond(_request: SipRequest) -> list[str]:
        return []  # never answer — the call is "ringing"

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    transport.add_call(call_id, owner_sink)
    try:
        await transport.send(_outbound_invite(branch, call_id=call_id))
        await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
        assert await transport.send_cancel(call_id) is True

        # A DIFFERENT sink than the one registered for this Call-ID: the identity
        # guard makes the _calls removal a no-op, but the outbound CANCEL tracking
        # must still be cleared (the call is being torn down).
        transport.remove_call(call_id, other_sink)

        # The outbound INVITE record is gone, so there is nothing left to CANCEL.
        assert await transport.send_cancel(call_id) is False, (
            "a sink-mismatch remove_call must still clear the outbound CANCEL "
            "tracking — a second send_cancel must find nothing in-flight"
        )
    finally:
        await transport.aclose()
        await server.stop()


async def test_retransmitted_glare_2xx_is_acked_each_time_but_byed_only_once() -> None:
    # Fix (b): RFC 3261 §13.3.1.4 — a UAS retransmits its 2xx until the ACK arrives.
    # For a 2xx that raced our CANCEL, the transport must ACK EVERY retransmission
    # (so the UAS stops retransmitting) but send the in-dialog BYE only ONCE (a second
    # BYE on an already-BYE'd dialog is spurious). The bug ACK+BYE'd on every 2xx.
    branch = new_branch()
    call_id = new_call_id()
    sink = _RecordingSink()

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    transport.add_call(call_id, sink)
    try:
        invite_text = _outbound_invite(branch, call_id=call_id)
        await transport.send(invite_text)
        await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
        assert await transport.send_cancel(call_id) is True
        await server.wait_for_received(lambda raw: raw.startswith("CANCEL "))

        late_200 = _final_2xx_with_contact(SipRequest.parse(invite_text))

        # First 2xx: ACK + BYE the racing answer.
        await server.push(late_200)
        await server.wait_for_received(
            lambda raw: raw.startswith("BYE ") and f"Call-ID: {call_id}" in raw,
            timeout=3.0,
        )

        def _ack_count() -> int:
            return sum(
                1
                for raw in server.received
                if raw.startswith("ACK ") and f"Call-ID: {call_id}" in raw
            )

        def _bye_count() -> int:
            return sum(
                1
                for raw in server.received
                if raw.startswith("BYE ") and f"Call-ID: {call_id}" in raw
            )

        assert _ack_count() == 1
        assert _bye_count() == 1

        # The UAS retransmits the SAME 2xx (it has not yet "seen" our ACK).
        await server.push(late_200)
        # The retransmit must be ACKed again (so the UAS stops retransmitting).
        await _until(lambda: _ack_count() == 2, timeout=3.0)
        # But the dialog was already BYE'd — no second BYE.
        await asyncio.sleep(0.15)
        assert _bye_count() == 1, (
            "a retransmitted glare 2xx must be ACKed again but BYE'd only once "
            "(RFC 3261 §13.3.1.4)"
        )
    finally:
        await transport.aclose()
        await server.stop()


# --------------------------------------------------------------------------
# Non-2xx final to an INVITE we sent is ACKed by this layer
# --------------------------------------------------------------------------


async def test_non_2xx_invite_response_is_acked_by_the_transport() -> None:
    branch = new_branch()

    async def respond(request: SipRequest) -> list[str]:
        if request.method != "INVITE":
            return []
        return [
            _provisional(request, 100, "Trying"),
            _final(request, 486, "Busy Here"),
        ]

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    try:
        await transport.send(_outbound_invite(branch))
        ack = await server.wait_for_received(
            lambda raw: raw.startswith("ACK "), timeout=3.0
        )
        parsed = SipRequest.parse(ack)
        via = parsed.header("Via")
        assert via is not None
        assert f"branch={branch}" in via  # the non-2xx ACK reuses the INVITE branch
        assert parsed.header("CSeq") == "1 ACK"
        to = parsed.header("To")
        assert to is not None
        assert "tag=busy-srv" in to  # To-tag copied from the 486
    finally:
        await transport.aclose()
        await server.stop()


async def test_2xx_to_our_invite_unregisters_txn_and_no_late_ack() -> None:
    # A 2xx terminates the client transaction (the TU owns the 2xx ACK), so the
    # transport unregisters it; a late non-2xx for the same branch then produces
    # NO ACK (the terminated transaction is gone, RFC 3261 §17.1.1.2).
    branch = new_branch()
    call_id = new_call_id()

    async def respond(request: SipRequest) -> list[str]:
        if request.method != "INVITE":
            return []
        return [_final(request, 200, "OK")]  # a 2xx — the transport must NOT ACK

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    try:
        invite = _outbound_invite(branch, call_id=call_id)
        await transport.send(invite)
        await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
        # Once the 200 is processed the txn must be unregistered (the review
        # finding): poll the transport's client-txn registry directly.
        await _until(lambda: not transport._client_txns)
        # A late non-2xx for the same transaction must NOT be ACKed.
        await server.push(_final(SipRequest.parse(invite), 486, "Busy Here"))
        await asyncio.sleep(0.1)
        assert not any(raw.startswith("ACK ") for raw in server.received)
    finally:
        await transport.aclose()
        await server.stop()


def _outbound_invite(branch: str, *, call_id: str | None = None, cseq: int = 1) -> str:
    return build_request(
        "INVITE",
        "sip:2000@127.0.0.1:5061;transport=tls",
        [
            ("Via", f"SIP/2.0/TLS 127.0.0.1:5061;branch={branch};rport"),
            ("Max-Forwards", "70"),
            ("From", f"<sip:1000@pbx.example.test>;tag={new_tag()}"),
            ("To", "<sip:2000@pbx.example.test>"),
            ("Call-ID", call_id if call_id is not None else new_call_id()),
            ("CSeq", f"{cseq} INVITE"),
            ("Contact", "<sip:1000@127.0.0.1:5061;transport=tls>"),
        ],
    )


def _provisional(invite: SipRequest, status: int, reason: str) -> str:
    return (
        f"SIP/2.0 {status} {reason}\r\n"
        f"Via: {invite.header('Via')}\r\n"
        f"From: {invite.header('From')}\r\n"
        f"To: {invite.header('To')}\r\n"
        f"Call-ID: {invite.header('Call-ID')}\r\n"
        f"CSeq: {invite.header('CSeq')}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _final(invite: SipRequest, status: int, reason: str) -> str:
    return (
        f"SIP/2.0 {status} {reason}\r\n"
        f"Via: {invite.header('Via')}\r\n"
        f"From: {invite.header('From')}\r\n"
        f"To: {invite.header('To')};tag=busy-srv\r\n"
        f"Call-ID: {invite.header('Call-ID')}\r\n"
        f"CSeq: {invite.header('CSeq')}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


# --------------------------------------------------------------------------
# local_sent_by / contact_uri reflect the live socket
# --------------------------------------------------------------------------


async def test_local_sent_by_and_contact_uri_reflect_socket() -> None:
    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    try:
        sent_by = transport.local_sent_by
        assert sent_by.startswith("127.0.0.1:")
        contact = transport.contact_uri("1000")
        assert contact.startswith("<sip:1000@127.0.0.1:")
        assert "transport=tls>" in contact
    finally:
        await transport.aclose()
        await server.stop()


# --------------------------------------------------------------------------
# In-dialog request demux to the owning CallSession's handle_request
# --------------------------------------------------------------------------


async def test_in_dialog_request_routes_to_registered_consumer() -> None:
    handled: list[SipRequest] = []

    class _Consumer:
        async def handle_request(self, request: SipRequest) -> None:
            handled.append(request)

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    consumer = _Consumer()
    dialog_id = ("indlg-call", "local-tag", "remote-tag")
    manager.add_call(dialog_id, consumer)
    try:
        bye = (
            "BYE sip:1000@127.0.0.1:5061;transport=tls SIP/2.0\r\n"
            "Via: SIP/2.0/TLS 203.0.113.5:5061;branch=z9hG4bKbye;rport\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:caller@pbx.example.test>;tag=remote-tag\r\n"
            "To: <sip:1000@pbx.example.test>;tag=local-tag\r\n"
            "Call-ID: indlg-call\r\n"
            "CSeq: 2 BYE\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        await server.push(bye)
        await _until(lambda: len(handled) == 1)
        assert len(handled) == 1
        assert handled[0].method == "BYE"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()
