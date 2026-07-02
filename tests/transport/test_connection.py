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
import logging
import re
import ssl
import sys
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


async def test_remove_call_sink_mismatch_still_clears_outbound_cancel_tracking() -> (
    None
):
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


def _final_with_cseq(
    invite: SipRequest, status: int, reason: str, cseq_header: str | None
) -> str:
    """A final response to ``invite`` with a caller-supplied ``CSeq`` value.

    ``cseq_header`` is the raw header value to send (e.g. ``"INVITE"`` — a method
    with no sequence number — or ``"abc INVITE"`` — a non-numeric sequence
    number); ``None`` omits the ``CSeq`` header entirely. Used to regression-guard
    the loud-failure path when a final's CSeq cannot be parsed (bk1188).
    """
    cseq_line = f"CSeq: {cseq_header}\r\n" if cseq_header is not None else ""
    return (
        f"SIP/2.0 {status} {reason}\r\n"
        f"Via: {invite.header('Via')}\r\n"
        f"From: {invite.header('From')}\r\n"
        f"To: {invite.header('To')};tag=busy-srv\r\n"
        f"Call-ID: {invite.header('Call-ID')}\r\n"
        f"{cseq_line}"
        "Content-Length: 0\r\n\r\n"
    )


async def test_malformed_cseq_on_invite_final_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A final non-2xx with an unparseable CSeq is logged, not silently dropped.

    Before the fix, ``_auto_ack_non_2xx`` gives up via a bare ``return`` when the
    CSeq method isn't ``INVITE`` (``_method_of`` returns ``None`` for a missing or
    number-less CSeq) or when ``_txn_key`` can't parse the sequence number (a
    non-numeric one) — silently skipping both the mandatory ACK (RFC 3261
    §17.1.1.3) and the client-transaction cleanup, with NO log at all. This
    exercises all three malformed shapes (omitted CSeq, CSeq with no number, CSeq
    with a non-numeric number) and asserts each is logged as a WARNING naming the
    Call-ID.
    """
    call_id = new_call_id()

    async def respond(request: SipRequest) -> list[str]:
        if request.method != "INVITE":
            return []
        return []  # the malformed finals are pushed directly below

    server = LoopbackSipServer(respond)
    await server.start()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    with caplog.at_level(logging.WARNING, logger="hermes_voip.transport.connection"):
        try:
            await transport.connect()
            invite_text = _outbound_invite(new_branch(), call_id=call_id)
            await transport.send(invite_text)
            await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
            invite = SipRequest.parse(invite_text)

            for cseq_header in (None, "INVITE", "abc INVITE"):
                await server.push(
                    _final_with_cseq(invite, 486, "Busy Here", cseq_header)
                )

            await _until(
                lambda: (
                    len([r for r in caplog.records if r.levelno == logging.WARNING])
                    >= 3
                ),
                timeout=3.0,
            )
        finally:
            await transport.aclose()
            await server.stop()

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) >= 3, (
        f"expected one WARNING per malformed-CSeq shape (3), got {len(warning_records)}"
    )
    for record in warning_records:
        assert call_id in record.getMessage(), (
            f"WARNING must name the Call-ID, got: {record.getMessage()!r}"
        )


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
# A malformed message is skipped, NOT a connection-tearing event (bk646)
# --------------------------------------------------------------------------


def _malformed_but_framable() -> str:
    """A message that FRAMES cleanly (valid Content-Length head) but fails parse.

    The framer delimits it by ``Content-Length: 0`` and yields it as one complete
    message, but its first line is neither a SIP request-line nor a status-line, so
    :meth:`SipRequest.parse` raises ``ValueError``. This is a *post-framing* parse
    failure (bk646): the stream is still synchronised, so only this one message is
    dropped — the connection and its other calls survive.
    """
    return (
        "GARBAGE-NOT-A-SIP-START-LINE\r\n"
        "Via: SIP/2.0/TLS 203.0.113.5:5061;branch=z9hG4bKbad;rport\r\n"
        "Content-Length: 0\r\n\r\n"
    )


async def test_malformed_message_is_skipped_connection_survives_next_dispatches() -> (
    None
):
    # bk646: a single malformed (but framable) message must NOT tear the connection
    # down. The framer delimited it cleanly, so the stream is still synchronised — the
    # one bad message is skipped (surfaced via a WARNING, never swallowed silently) and
    # a SUBSEQUENT well-formed message on the SAME connection is still dispatched. A
    # bare parse() in _dispatch used to propagate out of the reader and fire
    # on_connection_lost, dropping ALL active calls because one peer sent one bad frame.
    new_calls: list[NewCall] = []
    lost: list[BaseException | None] = []
    lost_called = asyncio.Event()

    def on_lost(exc: BaseException | None) -> None:
        lost.append(exc)
        lost_called.set()

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
        on_connection_lost=on_lost,
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        # The peer sends ONE malformed message, then a well-formed INVITE.
        await server.push(_malformed_but_framable())
        await server.push(_inbound_invite(to_user="1000"))
        # The well-formed INVITE that FOLLOWED the bad message must still be dispatched.
        await _until(lambda: len(new_calls) == 1)
        assert new_calls[0].invite.method == "INVITE"
        # The reader must NOT have ended: on_connection_lost stays unfired. Give a
        # short grace so a (buggy) teardown would have had time to surface.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(lost_called.wait(), timeout=0.25)
        assert lost == [], "a malformed message must not fire on_connection_lost"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_active_call_survives_a_malformed_message_on_the_connection() -> None:
    # bk646 (the DoS angle): an ESTABLISHED call's response routing must survive a
    # malformed message arriving on the shared connection. A response sink is
    # registered for an active Call-ID; the peer interleaves a malformed message and
    # then a well-formed response for that call. The bad message is skipped and the
    # call's own response is still delivered to its sink — one malformed frame is not a
    # DoS against unrelated, already-active calls.
    sink = _RecordingSink()
    call_id = new_call_id()
    branch = new_branch()
    lost: list[BaseException | None] = []

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
        on_connection_lost=lost.append,
    )
    await transport.connect()
    transport.add_call(call_id, sink)
    try:
        invite_text = _outbound_invite(branch, call_id=call_id)
        await transport.send(invite_text)
        await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
        # A malformed message arrives on the connection, then the call's own 200 OK.
        await server.push(_malformed_but_framable())
        await server.push(_final_2xx_with_contact(SipRequest.parse(invite_text)))
        # The active call's response is still routed to its sink despite the bad frame.
        await _until(lambda: any(r.status_code == _OK_STATUS for r in sink.responses))
        assert lost == [], "the active call's connection must survive a bad message"
    finally:
        await transport.aclose()
        await server.stop()


# --------------------------------------------------------------------------
# ADR-0081 class, RESPONSE side (F1): a NON-DECIMAL CSeq number on an INVITE
# final must not tear the connection down via _txn_key's int().
# --------------------------------------------------------------------------


async def test_superscript_cseq_number_on_invite_final_survives_connection() -> None:
    # F1 (HIGH, ADR-0081): a crafted INVITE-final response with a non-decimal CSeq
    # NUMBER — the superscript "²": ``"²".isdigit()`` is True but
    # ``int("²")`` raises — reaches ``_txn_key`` (the method token is a valid
    # "INVITE", so ``_method_of`` passes the gate) and crashes in ``int()`` BEFORE
    # any transaction lookup. So NO correlated call is needed: a single response
    # escaped the reader and dropped every active call. Assert an UNRELATED active
    # call survives (its own 200 OK is still routed) and on_connection_lost never
    # fires.
    active_call_id = new_call_id()
    active_branch = new_branch()
    sink = _RecordingSink()
    lost: list[BaseException | None] = []
    lost_called = asyncio.Event()

    def on_lost(exc: BaseException | None) -> None:
        lost.append(exc)
        lost_called.set()

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
        on_connection_lost=on_lost,
    )
    await transport.connect()
    transport.add_call(active_call_id, sink)
    try:
        active_invite = _outbound_invite(active_branch, call_id=active_call_id)
        await transport.send(active_invite)
        await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
        # The crafted response carries its OWN (untracked) Call-ID; it correlates to
        # no transaction, and would crash in _txn_key before that even mattered.
        unrelated = SipRequest.parse(
            _outbound_invite(new_branch(), call_id=new_call_id())
        )
        await server.push(_final_with_cseq(unrelated, 486, "Busy Here", "² INVITE"))
        # RED before the fix: the reader crashes and fires on_connection_lost.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(lost_called.wait(), timeout=0.5)
        assert lost == [], "a non-decimal CSeq must not fire on_connection_lost"
        # The unrelated active call's own 200 OK is still routed to its sink.
        await server.push(_final_2xx_with_contact(SipRequest.parse(active_invite)))
        await _until(lambda: any(r.status_code == _OK_STATUS for r in sink.responses))
        # No ACK is attempted for the crafted response (it matches no transaction).
        assert not any(raw.startswith("ACK ") for raw in server.received)
    finally:
        await transport.aclose()
        await server.stop()


async def test_overlong_cseq_number_on_invite_final_survives_connection() -> None:
    # F1 residual (codex adversarial review): a CSeq number that is all-ASCII-decimal
    # but longer than CPython's int-from-string digit limit
    # (sys.get_int_max_str_digits(), default 4300) passes ``isascii() and
    # isdecimal()`` yet still makes ``int()`` raise ValueError ("Exceeds the limit").
    # So the isascii+isdecimal guard alone does NOT close the escape — a crafted
    # "CSeq: <thousands of digits> INVITE" response must be dropped like "²" is.
    overlong = "9" * (sys.get_int_max_str_digits() + 1)
    active_call_id = new_call_id()
    active_branch = new_branch()
    sink = _RecordingSink()
    lost: list[BaseException | None] = []
    lost_called = asyncio.Event()

    def on_lost(exc: BaseException | None) -> None:
        lost.append(exc)
        lost_called.set()

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
        on_connection_lost=on_lost,
    )
    await transport.connect()
    transport.add_call(active_call_id, sink)
    try:
        active_invite = _outbound_invite(active_branch, call_id=active_call_id)
        await transport.send(active_invite)
        await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
        unrelated = SipRequest.parse(
            _outbound_invite(new_branch(), call_id=new_call_id())
        )
        await server.push(
            _final_with_cseq(unrelated, 486, "Busy Here", f"{overlong} INVITE")
        )
        # RED before the try/except: the reader crashes and fires on_connection_lost.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(lost_called.wait(), timeout=0.5)
        assert lost == [], "an over-long CSeq number must not fire on_connection_lost"
        await server.push(_final_2xx_with_contact(SipRequest.parse(active_invite)))
        await _until(lambda: any(r.status_code == _OK_STATUS for r in sink.responses))
        assert not any(raw.startswith("ACK ") for raw in server.received)
    finally:
        await transport.aclose()
        await server.stop()


# --------------------------------------------------------------------------
# ADR-0081 class, RESPONSE side (F3): a CORRELATED non-2xx final whose auto-ACK
# cannot be built (missing To / control char) must not tear the connection down.
# --------------------------------------------------------------------------


def _final_missing_to(invite: SipRequest, status: int, reason: str) -> str:
    """A non-2xx final correlated to ``invite`` but carrying NO ``To`` header.

    Same Via/branch, Call-ID and CSeq number as ``invite`` (so it matches the
    outstanding INVITE client transaction), but no ``To``. parse() does not require
    ``To`` (it frames + parses cleanly), so this reaches _auto_ack_non_2xx and
    _build_ack's ``_require(response, "To")`` raises ValueError (RFC 3261 §17.1.1.3
    copies the response's To into the ACK).
    """
    return (
        f"SIP/2.0 {status} {reason}\r\n"
        f"Via: {invite.header('Via')}\r\n"
        f"From: {invite.header('From')}\r\n"
        f"Call-ID: {invite.header('Call-ID')}\r\n"
        f"CSeq: {invite.header('CSeq')}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


async def test_correlated_non_2xx_final_missing_to_survives_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # F3 (MEDIUM, ADR-0081): a non-2xx final that CORRELATES to an INVITE we sent
    # (so a client transaction IS found) but lacks a To header makes _build_ack's
    # _require(response, "To") raise ValueError. Unguarded it escaped the reader and
    # tore the connection down. Assert the connection survives, no ACK is sent (the
    # auto-ACK could not be built), and a non-PII WARNING is logged.
    branch = new_branch()
    call_id = new_call_id()
    lost: list[BaseException | None] = []
    lost_called = asyncio.Event()

    def on_lost(exc: BaseException | None) -> None:
        lost.append(exc)
        lost_called.set()

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
        on_connection_lost=on_lost,
    )
    with caplog.at_level(logging.WARNING, logger="hermes_voip.transport.connection"):
        try:
            await transport.connect()
            invite = _outbound_invite(branch, call_id=call_id)
            await transport.send(invite)
            await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
            await server.push(
                _final_missing_to(SipRequest.parse(invite), 486, "Busy Here")
            )
            # RED before the fix: the reader crashes and fires on_connection_lost.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(lost_called.wait(), timeout=0.5)
            assert lost == [], (
                "a To-less correlated final must not tear the reader down"
            )
            # The auto-ACK could not be built, so none was sent.
            assert not any(raw.startswith("ACK ") for raw in server.received)
        finally:
            await transport.aclose()
            await server.stop()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("auto-ACK" in r.getMessage() for r in warnings), (
        "dropping an unbuildable auto-ACK must be logged as a WARNING"
    )


# --------------------------------------------------------------------------
# ADR-0081 class, BUILD side: a header-incomplete inbound request that PARSES
# but whose auto-response cannot be built must not tear down the connection.
# --------------------------------------------------------------------------


def _cancel_missing_via() -> str:
    """A CANCEL that PARSES but carries NO Via — its 481 auto-response has none to echo.

    parse() does not require the echo headers, so this frames + parses cleanly. It
    routes to a Cancel (no To-tag, method CANCEL), matches no pending INVITE (no Via
    branch), and reaches _handle_cancel's build_response(cancel, 481, ...) — which
    RAISES ValueError (RFC 3261 §8.2.6 requires echoing the request's Via). That build
    runs INLINE in the reader task (OUTSIDE _dispatch's parse-only guard), so before the
    fix the escape ended the reader and fired on_connection_lost — a one-packet DoS
    against every active call. The distinctive Call-ID / From-tag are PII tripwires for
    the log-content assertion below.
    """
    return (
        "CANCEL sip:1000@pbx.example.test SIP/2.0\r\n"
        "From: <sip:2000@pbx.example.test>;tag=cancel-nohdr-ft\r\n"
        "To: <sip:1000@pbx.example.test>\r\n"
        "Call-ID: cancel-no-via-tripwire\r\n"
        "CSeq: 1 CANCEL\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _options_missing_via() -> str:
    """An out-of-dialog OPTIONS that PARSES but has NO Via — keepalive 200 has none.

    A gateway *qualify* OPTIONS (RFC 3261 §11) is out of dialog, so it routes Unroutable
    and is auto-answered by _answer_keepalive via build_options_ok → build_response —
    which RAISES ValueError on the absent Via. Same inline-escape hazard as the CANCEL.
    """
    return (
        "OPTIONS sip:1000@pbx.example.test SIP/2.0\r\n"
        "From: <sip:qualify@pbx.example.test>;tag=opt-nohdr-ft\r\n"
        "To: <sip:1000@pbx.example.test>\r\n"
        "Call-ID: options-no-via-tripwire\r\n"
        "CSeq: 1 OPTIONS\r\n"
        "Content-Length: 0\r\n\r\n"
    )


async def test_active_call_survives_a_header_incomplete_cancel() -> None:
    # ADR-0081 class (build side): a CANCEL that PARSES but is missing a mandatory echo
    # header (no Via) routes to _handle_cancel, whose build_response(481) raises
    # ValueError. That build runs INLINE in the reader task (OUTSIDE _dispatch's
    # parse-only try), so the escape used to end the reader and fire on_connection_lost,
    # dropping EVERY active call over one packet. The malformed CANCEL must be dropped
    # (logged) and an unrelated active call's own 200 OK still routed to its sink.
    sink = _RecordingSink()
    call_id = new_call_id()
    branch = new_branch()
    lost: list[BaseException | None] = []

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
        on_connection_lost=lost.append,
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)  # so route_request classifies the CANCEL
    transport.add_call(call_id, sink)
    try:
        invite_text = _outbound_invite(branch, call_id=call_id)
        await transport.send(invite_text)
        await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
        # The header-incomplete CANCEL arrives on the shared connection, then the active
        # call's own 200 OK.
        await server.push(_cancel_missing_via())
        await server.push(_final_2xx_with_contact(SipRequest.parse(invite_text)))
        await _until(lambda: any(r.status_code == _OK_STATUS for r in sink.responses))
        assert lost == [], (
            "a header-incomplete CANCEL must not tear down the shared connection"
        )
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_active_call_survives_a_header_incomplete_keepalive_options() -> None:
    # ADR-0081 class (build side), keepalive path: an out-of-dialog OPTIONS missing Via
    # is auto-answered by _answer_keepalive → build_options_ok → build_response, which
    # raises. The inline escape must not tear down the connection: an unrelated active
    # call's own 200 OK is still routed to its sink.
    sink = _RecordingSink()
    call_id = new_call_id()
    branch = new_branch()
    lost: list[BaseException | None] = []

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
        on_connection_lost=lost.append,
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)  # so the OPTIONS reaches _answer_keepalive
    transport.add_call(call_id, sink)
    try:
        invite_text = _outbound_invite(branch, call_id=call_id)
        await transport.send(invite_text)
        await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
        await server.push(_options_missing_via())
        await server.push(_final_2xx_with_contact(SipRequest.parse(invite_text)))
        await _until(lambda: any(r.status_code == _OK_STATUS for r in sink.responses))
        assert lost == [], (
            "a header-incomplete keepalive OPTIONS must not tear down the connection"
        )
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_header_incomplete_cancel_drop_is_logged_non_pii(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dropped header-incomplete CANCEL is logged as a non-PII WARNING (rules 37/34).

    Fail-closed is not silent (rule 37): dropping the un-answerable CANCEL emits a
    WARNING. It must carry ONLY the exception type name — never the wire content
    (Call-ID / From-tag / URIs are PII on a PUBLIC repo, rule 34). The fixture's
    distinctive Call-ID and From-tag must NOT appear in the log.
    """
    lost: list[BaseException | None] = []

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
        on_connection_lost=lost.append,
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    with caplog.at_level(logging.WARNING, logger="hermes_voip.transport.connection"):
        try:
            await server.push(_cancel_missing_via())
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 3.0
            while not caplog.records and loop.time() < deadline:
                await asyncio.sleep(0.01)
        finally:
            await manager.aclose()
            await transport.aclose()
            await server.stop()

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) >= 1, (
        "dropping an un-answerable CANCEL must emit a WARNING (surfaced, not swallowed)"
    )
    assert "ValueError" in warning_records[0].getMessage(), (
        f"log must carry type(exc).__name__, got: {warning_records[0].getMessage()!r}"
    )
    assert "cancel-no-via-tripwire" not in caplog.text, (
        "the CANCEL Call-ID must NOT appear in the log (rule 34 / PII guard)"
    )
    assert "cancel-nohdr-ft" not in caplog.text, (
        "the CANCEL From-tag must NOT appear in the log (rule 34 / PII guard)"
    )
    assert lost == [], "the header-incomplete CANCEL must not fire on_connection_lost"


def _matched_cancel_missing_cseq(*, branch: str, call_id: str) -> str:
    """A CANCEL that MATCHES a pending INVITE (branch + Call-ID) but omits CSeq.

    It carries the pending INVITE's top Via branch and its Call-ID, so ``_match_cancel``
    finds the transaction — but with no ``CSeq``, ``build_response(cancel, 200, ...)``
    raises, so the 200 OK to the CANCEL cannot be built. A CANCEL we cannot answer must
    have NO effect on the transaction (no 487, no ``on_cancel``).
    """
    return (
        "CANCEL sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 203.0.113.5:5061;branch={branch};rport\r\n"
        "From: <sip:2000@pbx.example.test>;tag=match-nocseq-ft\r\n"
        "To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


async def test_matched_header_incomplete_cancel_does_not_cancel_the_invite() -> None:
    # codex review follow-up: a CANCEL that MATCHES a pending INVITE by Via-branch +
    # Call-ID but is header-incomplete (here: no CSeq) cannot have its 200 OK built. A
    # CANCEL we cannot answer must have NO effect — it must not half-drive the
    # transaction (no 487 to the peer, no on_cancel abort). It is dropped whole, the
    # pending INVITE is left intact, and the connection survives.
    new_calls: list[NewCall] = []
    cancels: list[str] = []
    lost: list[BaseException | None] = []
    branch = new_branch()
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
        on_new_call=new_calls.append,
        on_cancel=cancels.append,
        on_connection_lost=lost.append,
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        # A well-formed inbound INVITE is tracked as a pending server transaction.
        await server.push(
            _inbound_invite_with(to_user="1000", call_id=call_id, branch=branch)
        )
        await _until(lambda: len(new_calls) == 1)
        # A matched-but-header-incomplete CANCEL: routes to _handle_cancel, matches the
        # pending INVITE, but its 200 OK cannot be built.
        await server.push(_matched_cancel_missing_cseq(branch=branch, call_id=call_id))
        # Let the reader process the CANCEL (in the buggy version this fired on_cancel).
        await asyncio.sleep(0.2)
        assert cancels == [], (
            "a header-incomplete CANCEL we cannot answer must not cancel the INVITE"
        )
        assert lost == [], "the connection must survive the header-incomplete CANCEL"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


def _nonutf8_body_but_framable() -> bytes:
    """A message that FRAMES cleanly but whose BODY is not valid UTF-8.

    The head is ASCII and declares ``Content-Length: 1``; the single body byte is
    ``0xFF`` — a lone continuation byte, invalid UTF-8 — as an inbound binary body
    (``application/ISUP``, ``octet-stream``, a Latin-1 payload) carries. The framer
    delimits the message by byte count and consumes it fully, so the stream stays
    synchronised at the next boundary; a strict ``utf-8`` decode of that framed
    message is what fails. Returned as RAW bytes because a non-UTF-8 body cannot be
    expressed as ``str``.
    """
    return (
        b"MESSAGE sip:1000@pbx.example.test SIP/2.0\r\n"
        b"Via: SIP/2.0/TLS 203.0.113.5:5061;branch=z9hG4bKnonutf8;rport\r\n"
        b"Call-ID: nonutf8-body-1\r\n"
        b"CSeq: 1 MESSAGE\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Length: 1\r\n\r\n"
        b"\xff"
    )


async def test_nonutf8_body_is_skipped_connection_survives_next_dispatches() -> None:
    # ADR-0081 decode variant: a message that FRAMES cleanly but whose BODY carries a
    # non-UTF-8 byte must NOT tear the connection down. The strict utf-8 decode used to
    # run at the FRAMING layer, raising UnicodeDecodeError OUTSIDE _dispatch's
    # recoverable `except ValueError` guard — ending the reader task and firing
    # on_connection_lost, dropping EVERY active call + bouncing registration over one
    # inbound binary body (attacker-reachable via INVITE/MESSAGE bodies through a
    # trusted gateway). The bad message must be SKIPPED (logged), and a SUBSEQUENT
    # well-formed INVITE on the SAME connection still dispatched — one malformed
    # message is not a DoS against unrelated calls.
    new_calls: list[NewCall] = []
    lost: list[BaseException | None] = []
    lost_called = asyncio.Event()

    def on_lost(exc: BaseException | None) -> None:
        lost.append(exc)
        lost_called.set()

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
        on_connection_lost=on_lost,
    )
    await transport.connect()
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        # The peer sends ONE non-UTF-8-body message, then a well-formed INVITE.
        await server.push_bytes(_nonutf8_body_but_framable())
        await server.push(_inbound_invite(to_user="1000"))
        # Wait until either the good INVITE dispatches or the connection is torn down.
        await _until(lambda: len(new_calls) == 1 or bool(lost))
        assert lost == [], (
            "a non-UTF-8-body message must NOT fire on_connection_lost — the strict "
            "utf-8 decode used to tear the shared signalling connection down (DoS "
            "against every unrelated active call)"
        )
        # The well-formed INVITE that FOLLOWED the bad-body message must still dispatch.
        assert new_calls[0].invite.method == "INVITE"
        # A short grace so a (buggy) late teardown would have had time to surface.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(lost_called.wait(), timeout=0.25)
        assert lost == [], "a non-UTF-8-body message must not fire on_connection_lost"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_framing_corruption_still_tears_down_the_reader() -> None:
    # bk646 boundary: only a POST-FRAMING parse failure is skipped. A genuine FRAMING
    # corruption (a complete head with NO Content-Length — the stream can no longer be
    # delimited) is unrecoverable and MUST still end the reader and fire
    # on_connection_lost (the pre-existing, correct behaviour preserved). The framer
    # raises FramingError from the read loop's iteration, which propagates — the
    # _dispatch parse fail-safe must NOT swallow it.
    lost: list[BaseException | None] = []
    lost_called = asyncio.Event()

    def on_lost(exc: BaseException | None) -> None:
        lost.append(exc)
        lost_called.set()

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
        on_connection_lost=on_lost,
    )
    await transport.connect()
    try:
        # A complete head with no Content-Length: the framer cannot delimit the stream.
        await server.push(
            "INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
            "Via: SIP/2.0/TLS 203.0.113.5:5061;branch=z9hG4bKnocl;rport\r\n\r\n"
        )
        await asyncio.wait_for(lost_called.wait(), timeout=3.0)
        assert len(lost) == 1
        assert lost[0] is not None, "a framing corruption must surface as a real error"
    finally:
        await transport.aclose()
        await server.stop()


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


# --------------------------------------------------------------------------
# caplog regression: malformed-SIP skip log must be non-PII (TLS, bk646)
# --------------------------------------------------------------------------


async def test_malformed_message_caplog_non_pii(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WARNING log for a skipped malformed message is non-PII (TLS transport, bk646).

    The transport logs a WARNING when it drops a malformed (but framable) SIP
    message. This test regression-guards the log format: it must carry ONLY the
    exception type name + the byte length of the raw message — never the raw
    bytes themselves (which can contain From/To/Call-ID/SDP and therefore PII).

    A mutation that logs ``str(exc)`` or the raw message will include the first
    line of the malformed payload ("GARBAGE-NOT-A-SIP-START-LINE") in caplog and
    trigger the second assertion here.
    """
    malformed = _malformed_but_framable()

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
    with caplog.at_level(logging.WARNING, logger="hermes_voip.transport.connection"):
        try:
            await server.push(malformed)
            # Wait for the WARNING (reader processes asynchronously).
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 3.0
            while not caplog.records and loop.time() < deadline:
                await asyncio.sleep(0.01)
        finally:
            await transport.aclose()
            await server.stop()

    # (1) At least one WARNING record; it carries the exc type name and byte length.
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) >= 1, (
        "expected at least one WARNING for the malformed message"
    )
    record = warning_records[0]
    formatted = record.getMessage()
    # Must match "ValueError" (the exception type name) in the formatted message.
    assert "ValueError" in formatted, (
        f"log record must carry type(exc).__name__='ValueError', got: {formatted!r}"
    )
    # Must carry the byte length (len=NN shape).
    assert re.search(r"len=\d+", formatted) is not None, (
        f"log record must carry 'len=<N>' byte length, got: {formatted!r}"
    )
    # (2) No raw SIP content from the malformed message must appear in caplog.
    # "GARBAGE-NOT-A-SIP-START-LINE" would appear if str(exc) or raw bytes were logged.
    assert "GARBAGE-NOT-A-SIP-START-LINE" not in caplog.text, (
        "raw malformed-message content must NOT appear in the log (rule 34 / PII guard)"
    )
    # The branch parameter in the Via header of the malformed fixture must not leak.
    assert "z9hG4bKbad" not in caplog.text, (
        "Via branch from the malformed fixture must NOT appear in the log (PII guard)"
    )


# --------------------------------------------------------------------------
# RFC 3261 §9.2: 200-to-CANCEL To-tag must equal the 487 To-tag (TLS)
# --------------------------------------------------------------------------

_TO_TAG_RE = re.compile(r";\s*tag=([^;,\s]+)", re.IGNORECASE)


def _to_tag_of(frame: str) -> str | None:
    """The ``tag`` parameter of a response's ``To`` header (None if absent)."""
    to_value = SipResponse.parse(frame).header("To")
    if to_value is None:
        return None
    match = _TO_TAG_RE.search(to_value)
    return match.group(1) if match is not None else None


async def test_tls_cancel_200_totag_matches_487_and_is_stable_across_retransmit() -> (
    None
):
    # RFC 3261 §9.2: "The To tag of the response to the CANCEL and the To tag in
    # the response to the original request SHOULD be the same." So the To tag on
    # the 200 OK (to the CANCEL) MUST equal the To tag on the 487 (to the INVITE).
    # And because a retransmitted CANCEL re-sends its 200 OK (§9.2 idempotency),
    # every 200-to-CANCEL for the same transaction MUST carry that SAME stable To
    # tag — a fresh tag per receipt would make CANCEL responses differ at the
    # message level (non-idempotent). Both INVITE-487 and every CANCEL-200 carry
    # the pending invite's one stable local_tag.
    new_calls: list[NewCall] = []
    cancelled: list[str] = []
    call_id = new_call_id()
    branch = "z9hG4bKtlstag"

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
        await _until(lambda: len(new_calls) == 1, timeout=3.0)
        cancel = _cancel_for(to_user="1000", call_id=call_id, branch=branch)
        await server.push(cancel)
        # Collect the first 200-to-CANCEL and the 487-to-INVITE.
        first_ok = await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 200") and "CSeq: 1 CANCEL" in raw,
            timeout=3.0,
        )
        terminated = await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 487"), timeout=3.0
        )
        first_cancel_tag = _to_tag_of(first_ok)
        invite_tag = _to_tag_of(terminated)
        assert invite_tag is not None, "the 487 must carry a To tag"
        assert first_cancel_tag is not None, "the 200-to-CANCEL must carry a To tag"
        assert first_cancel_tag == invite_tag, (
            "RFC 3261 §9.2: the 200-to-CANCEL To tag must equal the 487 To tag, "
            f"got CANCEL-200={first_cancel_tag!r} vs INVITE-487={invite_tag!r}"
        )
        # The peer retransmits the CANCEL; its 200 OK must reuse the SAME To tag.
        await server.push(cancel)
        await _until(
            lambda: (
                sum(
                    1
                    for raw in server.received
                    if raw.startswith("SIP/2.0 200") and "CSeq: 1 CANCEL" in raw
                )
                >= 2
            ),
            timeout=3.0,
        )
        cancel_200_tags = {
            _to_tag_of(raw)
            for raw in server.received
            if raw.startswith("SIP/2.0 200") and "CSeq: 1 CANCEL" in raw
        }
        assert cancel_200_tags == {invite_tag}, (
            "every 200-to-CANCEL (incl. the retransmit) must carry the one stable "
            f"To tag {invite_tag!r}; got {cancel_200_tags!r}"
        )
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


# --------------------------------------------------------------------------
# ADR-0098 (amends ADR-0081): a SCOPED reader dispatch-boundary fail-safe
# backstop. The per-site ADR-0081 guards catch only the specific ``ValueError``
# each site was known to raise; the reader awaits ``_dispatch_response`` /
# ``_dispatch_request`` OUTSIDE ``_dispatch``'s parse-only ``try``, so ANY OTHER
# exception a downstream handler raises escapes the reader → ``_on_reader_done``
# → ``on_connection_lost``, tearing down every call + registration on the shared
# connection. These tests inject a SYNTHETIC unanticipated handler fault (a
# non-``ValueError``) — standing in for the next escape the per-site campaign has
# not yet found — and assert the scoped backstop keeps the connection alive,
# while still letting ``asyncio.CancelledError`` propagate (it must NOT swallow
# cancellation — ``except Exception``, not ``except BaseException``).
# --------------------------------------------------------------------------


class _RaisingSink:
    """A response sink whose ``on_response`` re-raises a caller-chosen exception.

    Used to synthesise an UNANTICIPATED fault at the reader's dispatch boundary
    (ADR-0098): a ``RuntimeError`` (a non-``ValueError`` the per-site ADR-0081
    guards do not catch) or an ``asyncio.CancelledError`` (which the backstop must
    let propagate). Routing to it is by Call-ID via :meth:`add_call`.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def on_response(self, response: SipResponse) -> None:
        raise self._exc


def _poison_response(call_id: str) -> str:
    """A well-formed non-2xx response routed to ``call_id`` (CSeq method ``BYE``).

    It parses cleanly and — because its CSeq method is not ``INVITE`` — bypasses
    the glare / auto-ACK / transaction path (``_handle_glare_2xx`` and
    ``_auto_ack_non_2xx`` both return early), so it is delivered straight to the
    registered response sink. A sink that raises then exercises the reader's
    dispatch boundary directly.
    """
    return (
        "SIP/2.0 480 Temporarily Unavailable\r\n"
        "Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKpoison;rport\r\n"
        "From: <sip:2000@pbx.example.test>;tag=remote\r\n"
        "To: <sip:1000@pbx.example.test>;tag=local\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 BYE\r\n"
        "Content-Length: 0\r\n\r\n"
    )


async def test_active_call_survives_a_handler_that_raises_a_non_valueerror(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # ADR-0098: a response routed to a call whose sink raises a NON-ValueError
    # (a synthetic RuntimeError — the shape of an unanticipated handler bug the
    # per-site ADR-0081 guards do not catch) is awaited by the reader OUTSIDE
    # _dispatch's parse-only try. Unguarded it ends the reader and fires
    # on_connection_lost — a one-message DoS on every OTHER call. The scoped
    # dispatch-boundary backstop must catch it, log a loud non-PII WARNING, drop
    # the one message, and keep the connection + every unrelated call alive.
    active_call_id = new_call_id()
    active_branch = new_branch()
    poison_call_id = new_call_id()
    healthy = _RecordingSink()
    lost: list[BaseException | None] = []
    lost_called = asyncio.Event()

    def on_lost(exc: BaseException | None) -> None:
        lost.append(exc)
        lost_called.set()

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
        on_connection_lost=on_lost,
    )
    with caplog.at_level(logging.WARNING, logger="hermes_voip.transport.connection"):
        await transport.connect()
        transport.add_call(active_call_id, healthy)
        transport.add_call(poison_call_id, _RaisingSink(RuntimeError("synthetic")))
        try:
            active_invite = _outbound_invite(active_branch, call_id=active_call_id)
            await transport.send(active_invite)
            await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
            # The poison response drives the sink's RuntimeError inside the reader.
            await server.push(_poison_response(poison_call_id))
            # RED before the backstop: the reader crashes and fires on_connection_lost.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(lost_called.wait(), timeout=0.5)
            assert lost == [], "a handler exception must not fire on_connection_lost"
            # The reader survived: the UNRELATED active call's own 200 OK is still
            # routed to its sink.
            await server.push(_final_2xx_with_contact(SipRequest.parse(active_invite)))
            await _until(
                lambda: any(r.status_code == _OK_STATUS for r in healthy.responses)
            )
        finally:
            await transport.aclose()
            await server.stop()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("RuntimeError" in r.getMessage() for r in warnings), (
        "the backstop must log a WARNING naming the exception type (RuntimeError)"
    )
    # rule 34: the WARNING must carry the type name ONLY — never wire content. The
    # poison message's Call-ID is routing detail/PII and must not appear in any log.
    for record in warnings:
        assert poison_call_id not in record.getMessage(), (
            f"the backstop WARNING must not leak wire content: {record.getMessage()!r}"
        )


async def test_a_handler_cancellederror_propagates_out_of_dispatch() -> None:
    # ADR-0098: the backstop catches ``Exception`` — NOT ``BaseException`` —
    # precisely so ``asyncio.CancelledError`` (cooperative cancellation, a
    # BaseException in 3.13) still propagates out of ``_dispatch`` and ends the
    # reader task (``_read_loop`` awaits ``_dispatch`` per message; rule 37). If the
    # backstop ever widened to ``except BaseException`` it would swallow
    # cancellation and the reader could not be stopped; this locks the boundary.
    # Driven directly at ``_dispatch`` — the exact site the backstop wraps — so no
    # socket/timing is involved. Passes both before AND after the backstop (it is a
    # guard against over-catching), and would FAIL if the catch were BaseException.
    call_id = new_call_id()
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=0,
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
    )
    transport.add_call(call_id, _RaisingSink(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await transport._dispatch(_poison_response(call_id).encode())
