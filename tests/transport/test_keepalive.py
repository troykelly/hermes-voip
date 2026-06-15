"""Integration tests: the transport answers out-of-dialog OPTIONS/NOTIFY.

A registrar that gets no ``200 OK`` to its qualify ``OPTIONS`` marks the endpoint
UNREACHABLE and routes inbound calls to voicemail without ever sending an INVITE
(observed live against a real Grandstream UCM). The
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
    """A UCM-style out-of-dialog OPTIONS qualify ping (no To-tag)."""
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
