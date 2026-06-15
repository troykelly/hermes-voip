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
    build_request,
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


def _outbound_invite(branch: str) -> str:
    return build_request(
        "INVITE",
        "sip:2000@127.0.0.1:5061;transport=tls",
        [
            ("Via", f"SIP/2.0/TLS 127.0.0.1:5061;branch={branch};rport"),
            ("Max-Forwards", "70"),
            ("From", f"<sip:1000@pbx.example.test>;tag={new_tag()}"),
            ("To", "<sip:2000@pbx.example.test>"),
            ("Call-ID", new_call_id()),
            ("CSeq", "1 INVITE"),
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
