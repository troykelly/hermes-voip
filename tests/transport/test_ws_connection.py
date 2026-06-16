"""Integration tests for the SIP-over-WSS transport against a loopback WS server.

These exercise the real :class:`~hermes_voip.transport.ws_connection.WssSipTransport`
over a genuine asyncio WebSocket connection to an in-process loopback SIP-over-WS
server, with the production routing stack (RegistrationManager / route).
No live gateway — fakes only: ``pbx.example.test``, ext ``1000``, ``127.0.0.1``.

Per RFC 7118 / ADR-0016 §1, asserts:

* REGISTER goes out as **one SIP message per WS frame** with a ``WSS`` Via and a
  ``<token>.invalid`` sent-by;
* the Contact carries ``transport=ws``, ``+sip.instance``, and ``reg-id=1``;
* a REGISTER round-trip (401 → authenticated → 200) reaches ``registered`` via
  :class:`~hermes_voip.manager.RegistrationManager`;
* an inbound INVITE is dispatched as a :class:`~hermes_voip.manager.NewCall`;
* an out-of-dialog ``OPTIONS`` ping is answered ``200 OK`` (keepalive);
* an in-dialog request routes to the registered
  :class:`~hermes_voip.manager.DialogConsumer`;
* ``send()`` writes exactly one WS frame per message.
"""

from __future__ import annotations

import asyncio
import re
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
from hermes_voip.transport.ws_connection import WssSipTransport

try:
    from websockets.asyncio.server import ServerConnection, serve
    from websockets.typing import Subprotocol
except ImportError:
    pytest.skip("websockets extra not installed", allow_module_level=True)


pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helper: poll until a predicate holds
# ---------------------------------------------------------------------------


async def _until(
    predicate: Callable[[], bool], *, timeout: float = 3.0, step: float = 0.01
) -> None:
    """Poll ``predicate`` until true or the timeout elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            msg = "condition not met within the timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(step)


# ---------------------------------------------------------------------------
# Loopback WS SIP server
# ---------------------------------------------------------------------------

type WsResponder = Callable[[str], list[str]]


class LoopbackWsSipServer:
    """An in-process WebSocket SIP server fixture (subprotocol ``sip``).

    Accepts one connection. Each text frame is a complete SIP message (RFC 7118
    §5 — one frame ↔ one message). The ``responder`` maps each received text
    to a list of text frames to send back (one per SIP reply). Received frames
    (complete SIP messages) are appended to :attr:`received`.
    """

    def __init__(self, responder: WsResponder) -> None:
        self._responder = responder
        self._server: object | None = None
        self._ws: ServerConnection | None = None
        self._connected = asyncio.Event()
        self.received: list[str] = []
        self._received_event = asyncio.Event()

    @property
    def port(self) -> int:
        """The ephemeral port the server bound to (valid after :meth:`start`)."""
        if self._server is None:
            msg = "server not started"
            raise RuntimeError(msg)
        # websockets serve() returns a Serve context; .sockets[0] has getsockname()
        sockets = getattr(self._server, "sockets", None)
        if not sockets:
            msg = "no sockets on server"
            raise RuntimeError(msg)
        return int(sockets[0].getsockname()[1])

    async def start(self) -> None:
        """Start the loopback WS server on an ephemeral port (plain WS — no TLS)."""
        self._server = await serve(
            self._handle,
            host="127.0.0.1",
            port=0,
            subprotocols=[Subprotocol("sip")],
        )

    async def _handle(self, ws: ServerConnection) -> None:
        self._ws = ws
        self._connected.set()
        try:
            async for frame in ws:
                if not isinstance(frame, str):
                    continue
                self.received.append(frame)
                self._received_event.set()
                replies = self._responder(frame)
                for reply in replies:
                    await ws.send(reply)
        except Exception:  # noqa: BLE001 — WS disconnect/close is a normal event in tests
            return

    async def push(self, message: str, *, timeout: float = 3.0) -> None:
        """Send an unsolicited SIP message to the connected client (one WS frame)."""
        await asyncio.wait_for(self._connected.wait(), timeout)
        if self._ws is None:  # pragma: no cover — guarded by the event
            msg = "no client connected"
            raise RuntimeError(msg)
        await self._ws.send(message)

    async def wait_for_received(
        self,
        predicate: Callable[[str], bool],
        *,
        timeout: float = 3.0,
    ) -> str:
        """Wait until a received frame matches ``predicate``; return it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for raw in self.received:
                if predicate(raw):
                    return raw
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = "no received frame matched within the timeout"
                raise TimeoutError(msg)
            self._received_event.clear()
            try:
                await asyncio.wait_for(self._received_event.wait(), remaining)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        """Close the WS server."""
        server = self._server
        if server is not None:
            self._server = None
            getattr(server, "close", lambda: None)()
            wait = getattr(server, "wait_closed", None)
            if wait is not None:
                await wait()


# ---------------------------------------------------------------------------
# Fake SIP responses from the loopback server
# ---------------------------------------------------------------------------


def _sip_request_from_frame(frame: str) -> SipRequest | None:
    """Parse a SIP request frame; return ``None`` for responses."""
    if frame.startswith("SIP/2.0 "):
        return None
    return SipRequest.parse(frame)


def _challenge(req: SipRequest) -> str:
    return (
        "SIP/2.0 401 Unauthorized\r\n"
        f"Via: {req.header('Via')}\r\n"
        f"From: {req.header('From')}\r\n"
        f"To: {req.header('To')};tag=reg-srv\r\n"
        f"Call-ID: {req.header('Call-ID')}\r\n"
        f"CSeq: {req.header('CSeq')}\r\n"
        'WWW-Authenticate: Digest realm="pbx.example.test", nonce="nonce42", '
        'algorithm=MD5, qop="auth"\r\n'
        "Content-Length: 0\r\n\r\n"
    )


def _register_ok(req: SipRequest, *, expires: int = 120) -> str:
    return (
        "SIP/2.0 200 OK\r\n"
        f"Via: {req.header('Via')}\r\n"
        f"From: {req.header('From')}\r\n"
        f"To: {req.header('To')};tag=reg-srv\r\n"
        f"Call-ID: {req.header('Call-ID')}\r\n"
        f"CSeq: {req.header('CSeq')}\r\n"
        f"Contact: {req.header('Contact')};expires={expires}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _options_ok(req: SipRequest, to_tag: str) -> str:
    return (
        "SIP/2.0 200 OK\r\n"
        f"Via: {req.header('Via')}\r\n"
        f"From: {req.header('From')}\r\n"
        f"To: {req.header('To')};tag={to_tag}\r\n"
        f"Call-ID: {req.header('Call-ID')}\r\n"
        f"CSeq: {req.header('CSeq')}\r\n"
        "Allow: REGISTER, INVITE, ACK, BYE, OPTIONS, NOTIFY\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _inbound_invite(*, to_user: str) -> str:
    return (
        f"INVITE sip:{to_user}@pbx.example.test SIP/2.0\r\n"
        "Via: SIP/2.0/WSS gw.pbx.example.test;branch=z9hG4bKinv1;rport\r\n"
        "Max-Forwards: 70\r\n"
        "From: <sip:caller@pbx.example.test>;tag=remote\r\n"
        f"To: <sip:{to_user}@pbx.example.test>\r\n"
        f"Call-ID: {new_call_id()}\r\n"
        "CSeq: 1 INVITE\r\n"
        "Contact: <sip:caller@gw.pbx.example.test;transport=ws>\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _inbound_options(*, to_user: str) -> str:
    return (
        f"OPTIONS sip:{to_user}@pbx.example.test SIP/2.0\r\n"
        "Via: SIP/2.0/WSS gw.pbx.example.test;branch=z9hG4bKopt1;rport\r\n"
        "Max-Forwards: 70\r\n"
        "From: <sip:pbx@pbx.example.test>;tag=gw-tag\r\n"
        f"To: <sip:{to_user}@pbx.example.test>\r\n"
        f"Call-ID: {new_call_id()}\r\n"
        "CSeq: 1 OPTIONS\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _in_dialog_bye(*, call_id: str, local_tag: str, remote_tag: str) -> str:
    return (
        f"BYE sip:1000@pbx.example.test SIP/2.0\r\n"
        "Via: SIP/2.0/WSS gw.pbx.example.test;branch=z9hG4bKbye1;rport\r\n"
        "Max-Forwards: 70\r\n"
        f"From: <sip:caller@pbx.example.test>;tag={remote_tag}\r\n"
        f"To: <sip:1000@pbx.example.test>;tag={local_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 2 BYE\r\n"
        "Content-Length: 0\r\n\r\n"
    )


# ---------------------------------------------------------------------------
# Gateway config (fake credentials, no secrets)
# ---------------------------------------------------------------------------


def _gateway() -> GatewayConfig:
    return load_gateway_config(
        {
            "HERMES_SIP_HOST": "pbx.example.test",
            "HERMES_SIP_TRANSPORT": "wss",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "s3cr3t",
            "HERMES_SIP_EXPIRES": "120",
        }
    )


def _make_transport(
    port: int,
    *,
    on_new_call: Callable[[NewCall], None] | None = None,
) -> WssSipTransport:
    return WssSipTransport(
        host="pbx.example.test",
        port=port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_new_call=on_new_call,
    )


# ---------------------------------------------------------------------------
# Via / Contact shape assertions
# ---------------------------------------------------------------------------


def _assert_wss_via(frame: str) -> None:
    """The Via must use ``WSS`` transport and a ``<token>.invalid`` sent-by."""
    req = SipRequest.parse(frame)
    via = req.header("Via")
    assert via is not None, "REGISTER has no Via"
    assert via.startswith("SIP/2.0/WSS "), f"Via transport must be WSS, got: {via!r}"
    # The sent-by must be <token>.invalid (no real IP/port for WSS clients)
    sent_by = via.split()[1].split(";")[0]
    assert sent_by.endswith(".invalid"), (
        f"Via sent-by must end with .invalid, got: {sent_by!r}"
    )


def _assert_ws_contact(frame: str) -> None:
    """The Contact must carry ``transport=ws``, ``+sip.instance``, and ``reg-id=1``."""
    req = SipRequest.parse(frame)
    contact = req.header("Contact")
    assert contact is not None, "REGISTER has no Contact"
    assert "transport=ws" in contact, f"Contact must have transport=ws: {contact!r}"
    assert "+sip.instance" in contact, f"Contact must have +sip.instance: {contact!r}"
    assert "reg-id=1" in contact, f"Contact must have reg-id=1: {contact!r}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_register_frame_has_wss_via_and_invalid_sent_by() -> None:
    """REGISTER goes out as one WS frame with WSS Via and .invalid sent-by."""
    received_frames: list[str] = []

    def responder(frame: str) -> list[str]:
        received_frames.append(frame)
        req = _sip_request_from_frame(frame)
        if req is None or req.method != "REGISTER":
            return []
        return [_register_ok(req)]

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = _make_transport(server.port)
    try:
        await transport.connect()
        await transport.send(
            _make_plain_register(
                host="pbx.example.test",
                extension="1000",
                sent_by=transport.local_sent_by,
            )
        )
        await _until(lambda: any("REGISTER" in f for f in received_frames))
        register_frame = next(f for f in received_frames if "REGISTER" in f)
        _assert_wss_via(register_frame)
        # Each SIP message is one complete WS text frame (RFC 7118 §5).
        assert register_frame.startswith("REGISTER "), (
            f"Frame must be a complete SIP REGISTER, got: {register_frame[:50]!r}"
        )
    finally:
        await transport.aclose()
        await server.stop()


def _make_plain_register(*, host: str, extension: str, sent_by: str) -> str:
    via = f"SIP/2.0/WSS {sent_by};branch={new_branch()};rport"
    return build_request(
        "REGISTER",
        f"sip:{host}",
        [
            ("Via", via),
            ("Max-Forwards", "70"),
            ("From", f"<sip:{extension}@{host}>;tag={new_tag()}"),
            ("To", f"<sip:{extension}@{host}>"),
            ("Call-ID", new_call_id()),
            ("CSeq", "1 REGISTER"),
            ("Contact", f"<sip:{extension}@{sent_by};transport=ws>"),
            ("Expires", "120"),
        ],
    )


async def test_contact_uri_has_transport_ws_outbound_params() -> None:
    """contact_uri() returns a Contact with transport=ws, +sip.instance, reg-id=1."""
    received_frames: list[str] = []

    def responder(frame: str) -> list[str]:
        received_frames.append(frame)
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = _make_transport(server.port)
    try:
        await transport.connect()
        contact = transport.contact_uri("1000")
        assert "transport=ws" in contact, f"Expected transport=ws in {contact!r}"
        assert "+sip.instance" in contact, f"Expected +sip.instance in {contact!r}"
        assert "reg-id=1" in contact, f"Expected reg-id=1 in {contact!r}"
        assert ".invalid" in contact, f"Expected .invalid host in {contact!r}"
    finally:
        await transport.aclose()
        await server.stop()


async def test_local_sent_by_uses_invalid_tld() -> None:
    """local_sent_by returns a ``<token>.invalid`` host (no real address)."""

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = _make_transport(server.port)
    try:
        await transport.connect()
        sent_by = transport.local_sent_by
        assert sent_by.endswith(".invalid"), (
            f"local_sent_by must be a <token>.invalid host, got {sent_by!r}"
        )
        # Should not look like an IP:port
        assert ":" not in sent_by, (
            f"WSS sent-by must not contain a port, got {sent_by!r}"
        )
    finally:
        await transport.aclose()
        await server.stop()


async def test_register_round_trips_via_manager() -> None:
    """REGISTER 401 → authenticated → 200 delivers ``registered`` via the manager."""

    def responder(frame: str) -> list[str]:
        req = _sip_request_from_frame(frame)
        if req is None or req.method != "REGISTER":
            return []
        if req.header("Authorization") is None:
            return [_challenge(req)]
        return [_register_ok(req)]

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = _make_transport(server.port)
    gateway = _gateway()
    manager = RegistrationManager(gateway, transport)
    transport.bind_manager(manager)
    try:
        await transport.connect()
        up = await manager.connect(timeout=5.0)
        assert up is True
        snapshot = manager.snapshot()
        assert snapshot[0].registered is True
        assert snapshot[0].expires == 120
        # Verify REGISTER frames had WSS Via and ws Contact
        registers = [f for f in server.received if f.startswith("REGISTER ")]
        assert len(registers) == 2, (
            f"Expected 2 REGISTER frames (challenge + authed), got {len(registers)}"
        )
        _assert_wss_via(registers[0])
        _assert_ws_contact(registers[0])
        assert "Authorization" in registers[1]
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_inbound_invite_routes_to_new_call() -> None:
    """An inbound INVITE (one WS frame) fires the on_new_call callback."""
    new_calls: list[NewCall] = []

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = _make_transport(server.port, on_new_call=new_calls.append)
    gateway = _gateway()
    manager = RegistrationManager(gateway, transport)
    transport.bind_manager(manager)
    try:
        await transport.connect()
        # Push one INVITE frame (the server sends it as one WS text frame)
        await server.push(_inbound_invite(to_user="1000"))
        await _until(lambda: len(new_calls) == 1, timeout=3.0)
        assert new_calls[0].registration.extension == "1000"
        assert new_calls[0].invite.method == "INVITE"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_out_of_dialog_options_is_answered_200_ok() -> None:
    """An out-of-dialog OPTIONS ping is answered 200 OK (keepalive, RFC 3261 §11)."""

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = _make_transport(server.port)
    gateway = _gateway()
    manager = RegistrationManager(gateway, transport)
    transport.bind_manager(manager)
    try:
        await transport.connect()
        await server.push(_inbound_options(to_user="1000"))
        response_frame = await server.wait_for_received(
            lambda f: f.startswith("SIP/2.0 200 "), timeout=3.0
        )
        assert "200 OK" in response_frame
        # The response must be one complete WS frame
        assert "Content-Length" in response_frame
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_in_dialog_request_routes_to_consumer() -> None:
    """An in-dialog BYE (WS frame) routes to the registered DialogConsumer."""
    handled: list[SipRequest] = []
    call_id = new_call_id()
    local_tag = new_tag()
    remote_tag = new_tag()

    class _Consumer:
        async def handle_request(self, request: SipRequest) -> None:
            handled.append(request)

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = _make_transport(server.port)
    gateway = _gateway()
    manager = RegistrationManager(gateway, transport)
    transport.bind_manager(manager)
    consumer = _Consumer()
    manager.add_call((call_id, local_tag, remote_tag), consumer)
    try:
        await transport.connect()
        await server.push(
            _in_dialog_bye(call_id=call_id, local_tag=local_tag, remote_tag=remote_tag)
        )
        await _until(lambda: len(handled) == 1, timeout=3.0)
        assert handled[0].method == "BYE"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_send_writes_exactly_one_frame_per_message() -> None:
    """send() writes each SIP message as exactly one complete WS text frame."""
    frames: list[str] = []

    def responder(frame: str) -> list[str]:
        frames.append(frame)
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = _make_transport(server.port)
    try:
        await transport.connect()
        sent_by = transport.local_sent_by
        # Send three independent messages
        msgs = [
            _make_plain_register(
                host="pbx.example.test",
                extension="1000",
                sent_by=sent_by,
            )
            for _ in range(3)
        ]
        for msg in msgs:
            await transport.send(msg)
        await _until(lambda: len(frames) == 3, timeout=3.0)
        # Each received frame is a complete, standalone SIP message
        for frame in frames:
            assert frame.startswith("REGISTER "), (
                f"Frame is not a complete REGISTER: {frame[:60]!r}"
            )
            assert "Via:" in frame
            assert "Content-Length:" in frame
    finally:
        await transport.aclose()
        await server.stop()


async def test_local_sent_by_is_available_before_connect() -> None:
    """local_sent_by is available before connect() for WSS.

    Unlike the TLS transport (whose sent-by is a real socket address only known
    after connect), the WSS sent-by is a random ``.invalid`` token generated at
    construction time so that RegistrationManager can be built before connect().
    """
    # No server needed — we test the property before any IO.
    transport = WssSipTransport(host="pbx.example.test", port=443, ws_path="/ws")
    sent_by = transport.local_sent_by
    assert sent_by.endswith(".invalid"), (
        f"WSS local_sent_by must be a <token>.invalid host, got {sent_by!r}"
    )


async def test_send_raises_before_connect() -> None:
    """send() before connect() raises RuntimeError."""

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = _make_transport(server.port)
    try:
        fake_msg = build_request(
            "OPTIONS",
            "sip:pbx.example.test",
            [
                ("Via", f"SIP/2.0/WSS fake.invalid;branch={new_branch()}"),
                ("From", f"<sip:1000@pbx.example.test>;tag={new_tag()}"),
                ("To", "<sip:pbx.example.test>"),
                ("Call-ID", new_call_id()),
                ("CSeq", "1 OPTIONS"),
            ],
        )
        with pytest.raises(RuntimeError, match="before connect"):
            await transport.send(fake_msg)
    finally:
        await server.stop()


async def test_instance_id_is_stable_across_contact_uri_calls() -> None:
    """The +sip.instance URN is the same on every contact_uri() call for a transport."""

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = _make_transport(server.port)
    try:
        await transport.connect()
        contact_a = transport.contact_uri("1000")
        contact_b = transport.contact_uri("1000")
        contact_other = transport.contact_uri("1001")
        # The instance URN is the same for the same extension
        instance_re = re.compile(r"urn:uuid:[0-9a-f-]+", re.IGNORECASE)
        match_a = instance_re.search(contact_a)
        match_b = instance_re.search(contact_b)
        assert match_a is not None
        assert match_b is not None
        assert match_a.group(0) == match_b.group(0), (
            "instance URN must be stable for repeated calls"
        )
        # A different extension must get a different URN
        match_other = instance_re.search(contact_other)
        assert match_other is not None
        assert match_a.group(0) != match_other.group(0), (
            "Different extensions must get different instance URNs"
        )
    finally:
        await transport.aclose()
        await server.stop()
