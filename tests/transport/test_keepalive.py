"""Integration tests: the transport answers out-of-dialog OPTIONS/NOTIFY.

A registrar that gets no ``200 OK`` to its qualify ``OPTIONS`` marks the endpoint
UNREACHABLE and routes inbound calls to voicemail without ever sending an INVITE
(observed live against a real RFC-compliant SIP gateway). The
:class:`~hermes_voip.transport.connection.SipOverTlsTransport` therefore answers
an out-of-dialog ``OPTIONS`` with ``200 OK`` (RFC 3261 §11) and acknowledges an
unsolicited MWI ``NOTIFY`` with ``200 OK``, *before* falling through to the
unroutable path. A genuinely unroutable request (an in-dialog request for an
unknown dialog) still takes the unroutable path.

These run over a genuine asyncio TLS socket to the loopback SIP server. Fakes
only — ``pbx.example.test``, ext ``1000``, ``198.51.100.x`` / ``127.0.0.1``.
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_voip.config import GatewayConfig, load_gateway_config
from hermes_voip.manager import RegistrationManager, Unroutable
from hermes_voip.message import SipRequest, SipResponse
from hermes_voip.transport.connection import SipOverTlsTransport

from ._loopback import LoopbackSipServer, client_ssl_context

pytestmark = pytest.mark.asyncio


def _gateway() -> GatewayConfig:
    return load_gateway_config(
        {
            "HERMES_SIP_HOST": "pbx.example.test",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "s3cr3t",
            "HERMES_SIP_EXPIRES": "120",
        }
    )


def _options_ping(*, call_id: str = "opt-call-1") -> str:
    """A gateway-style out-of-dialog OPTIONS qualify ping (no To-tag)."""
    return (
        "OPTIONS sip:1000@127.0.0.1:5061;transport=tls SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bKopt1;rport\r\n"
        "Max-Forwards: 70\r\n"
        "From: <sip:pbx@pbx.example.test>;tag=qualify-1\r\n"
        "To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 OPTIONS\r\n"
        "Contact: <sip:pbx@198.51.100.7:5061;transport=tls>\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _mwi_notify(*, call_id: str = "mwi-call-1") -> str:
    """An unsolicited message-summary (MWI) NOTIFY, out of dialog (no To-tag)."""
    body = "Messages-Waiting: no\r\nVoice-Message: 0/0 (0/0)\r\n"
    return (
        "NOTIFY sip:1000@127.0.0.1:5061;transport=tls SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bKmwi1;rport\r\n"
        "Max-Forwards: 70\r\n"
        "From: <sip:1000@pbx.example.test>;tag=mwi-1\r\n"
        "To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 NOTIFY\r\n"
        "Event: message-summary\r\n"
        "Subscription-State: active\r\n"
        "Content-Type: application/simple-message-summary\r\n"
        f"Content-Length: {len(body.encode())}\r\n\r\n"
        f"{body}"
    )


async def _connected_transport(
    server: LoopbackSipServer,
    *,
    on_unroutable: object = None,
) -> SipOverTlsTransport:
    transport = SipOverTlsTransport(
        host="pbx.example.test",
        port=server.port,
        ssl_context=client_ssl_context(),
        server_hostname="pbx.example.test",
        connect_address="127.0.0.1",
        on_unroutable=on_unroutable,  # type: ignore[arg-type]
    )
    await transport.connect()
    return transport


# --------------------------------------------------------------------------
# Out-of-dialog OPTIONS → 200 OK with an Allow header
# --------------------------------------------------------------------------


async def test_out_of_dialog_options_is_answered_200_with_allow() -> None:
    unroutable: list[Unroutable | SipResponse] = []

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = await _connected_transport(server, on_unroutable=unroutable.append)
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await server.push(_options_ping(call_id="qualify-echo"))
        raw = await server.wait_for_received(
            lambda r: r.startswith("SIP/2.0 200"), timeout=3.0
        )
        response = SipResponse.parse(raw)
        assert response.status_code == 200
        allow = response.header("Allow")
        assert allow is not None
        methods = {m.strip() for m in allow.split(",")}
        assert "INVITE" in methods
        assert "OPTIONS" in methods
        # Echoes the qualify ping's Call-ID/CSeq and adds a To-tag (dialog id).
        assert response.header("Call-ID") == "qualify-echo"
        assert response.header("CSeq") == "1 OPTIONS"
        to = response.header("To")
        assert to is not None
        assert ";tag=" in to
        assert response.header("Content-Length") == "0"
        # A qualify ping is not an error: it must NOT hit the unroutable path.
        assert unroutable == []
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


# --------------------------------------------------------------------------
# Out-of-dialog (unsolicited) NOTIFY → 200 OK
# --------------------------------------------------------------------------


async def test_out_of_dialog_mwi_notify_is_answered_200() -> None:
    unroutable: list[Unroutable | SipResponse] = []

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = await _connected_transport(server, on_unroutable=unroutable.append)
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await server.push(_mwi_notify(call_id="mwi-echo"))
        raw = await server.wait_for_received(
            lambda r: r.startswith("SIP/2.0 200"), timeout=3.0
        )
        response = SipResponse.parse(raw)
        assert response.status_code == 200
        assert response.header("Call-ID") == "mwi-echo"
        assert response.header("CSeq") == "1 NOTIFY"
        to = response.header("To")
        assert to is not None
        assert ";tag=" in to
        assert unroutable == []
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


# --------------------------------------------------------------------------
# A genuinely unroutable request still takes the unroutable path
# --------------------------------------------------------------------------


async def test_in_dialog_request_for_unknown_dialog_is_unroutable() -> None:
    unroutable: list[Unroutable | SipResponse] = []

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = await _connected_transport(server, on_unroutable=unroutable.append)
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        # An in-dialog BYE (To carries a tag) for a dialog we never created.
        bye = (
            "BYE sip:1000@127.0.0.1:5061;transport=tls SIP/2.0\r\n"
            "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bKbye9;rport\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:caller@pbx.example.test>;tag=remote-x\r\n"
            "To: <sip:1000@pbx.example.test>;tag=unknown-local\r\n"
            "Call-ID: ghost-dialog\r\n"
            "CSeq: 2 BYE\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        await server.push(bye)
        # The unroutable observer fires; nothing is written back to the server.
        await _wait_until(lambda: len(unroutable) == 1)
        assert len(unroutable) == 1
        observed = unroutable[0]
        assert isinstance(observed, Unroutable)
        assert observed.request.method == "BYE"
        # No 200 was sent for this genuinely-unroutable request.
        await asyncio.sleep(0.1)
        assert not any(r.startswith("SIP/2.0 200") for r in server.received)
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


# --------------------------------------------------------------------------
# An OPTIONS that DOES carry a To-tag is in-dialog → unroutable, not answered.
# This pins the To-tag guard to OPTIONS specifically (a method-only fall-through
# test, e.g. BYE, would pass even with the guard removed).
# --------------------------------------------------------------------------


async def test_options_with_a_to_tag_is_unroutable_not_answered() -> None:
    unroutable: list[Unroutable | SipResponse] = []

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = await _connected_transport(server, on_unroutable=unroutable.append)
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        # An in-dialog OPTIONS (To carries a tag) for a dialog we never created:
        # it must NOT be auto-answered as a qualify ping — it takes the unroutable
        # path, exactly like any other in-dialog request for an unknown dialog.
        tagged_options = (
            "OPTIONS sip:1000@127.0.0.1:5061;transport=tls SIP/2.0\r\n"
            "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bKopt9;rport\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:caller@pbx.example.test>;tag=remote-y\r\n"
            "To: <sip:1000@pbx.example.test>;tag=unknown-local\r\n"
            "Call-ID: ghost-dialog-opt\r\n"
            "CSeq: 5 OPTIONS\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        await server.push(tagged_options)
        await _wait_until(lambda: len(unroutable) == 1)
        observed = unroutable[0]
        assert isinstance(observed, Unroutable)
        assert observed.request.method == "OPTIONS"
        await asyncio.sleep(0.1)
        assert not any(r.startswith("SIP/2.0 200") for r in server.received)
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


# --------------------------------------------------------------------------
# An in-dialog NOTIFY for a KNOWN dialog routes to the CallSession, NOT the
# keepalive responder — protects the ADR-0011 REFER-progress NOTIFY, which the
# call-control layer must consume (it must never be swallowed by a bare 200).
# --------------------------------------------------------------------------


async def test_in_dialog_notify_routes_to_consumer_not_keepalive() -> None:
    handled: list[SipRequest] = []
    unroutable: list[Unroutable | SipResponse] = []

    class _Consumer:
        async def handle_request(self, request: SipRequest) -> None:
            handled.append(request)

    async def respond(_request: SipRequest) -> list[str]:
        return []

    server = LoopbackSipServer(respond)
    await server.start()
    transport = await _connected_transport(server, on_unroutable=unroutable.append)
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    # Register a live dialog whose local tag is the NOTIFY's To-tag and whose
    # remote tag is its From-tag (the manager keys in-dialog routing this way).
    manager.add_call(("refer-call", "local-tag", "remote-tag"), _Consumer())
    try:
        in_dialog_notify = (
            "NOTIFY sip:1000@127.0.0.1:5061;transport=tls SIP/2.0\r\n"
            "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bKntf9;rport\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:caller@pbx.example.test>;tag=remote-tag\r\n"
            "To: <sip:1000@pbx.example.test>;tag=local-tag\r\n"
            "Call-ID: refer-call\r\n"
            "CSeq: 4 NOTIFY\r\n"
            "Event: refer\r\n"
            "Subscription-State: terminated;reason=noresource\r\n"
            "Content-Type: message/sipfrag;version=2.0\r\n"
            "Content-Length: 16\r\n\r\n"
            "SIP/2.0 200 OK\r\n"
        )
        await server.push(in_dialog_notify)
        await _wait_until(lambda: len(handled) == 1)
        assert handled[0].method == "NOTIFY"
        # The keepalive responder must NOT have fired: no auto 200, not unroutable.
        await asyncio.sleep(0.1)
        assert not any(r.startswith("SIP/2.0 200") for r in server.received)
        assert unroutable == []
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def _wait_until(
    predicate: object, *, timeout: float = 3.0, step: float = 0.01
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    check = predicate
    assert callable(check)
    while not check():
        if loop.time() >= deadline:
            msg = "condition not met within the timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(step)
