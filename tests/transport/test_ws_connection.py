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
import logging
import re
import sys
from collections.abc import Callable

import pytest

from hermes_voip.config import GatewayConfig, load_gateway_config
from hermes_voip.manager import NewCall, RegistrationManager
from hermes_voip.message import (
    SipRequest,
    SipResponse,
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

_OK_STATUS = 200  # a 2xx final response

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


def _outbound_invite(branch: str, *, call_id: str | None = None, cseq: int = 1) -> str:
    """A client-originated INVITE this transport sends (WSS Via, ``.invalid`` host)."""
    return build_request(
        "INVITE",
        "sip:2000@pbx.example.test",
        [
            ("Via", f"SIP/2.0/WSS abc123.invalid;branch={branch};rport"),
            ("Max-Forwards", "70"),
            ("From", f"<sip:1000@pbx.example.test>;tag={new_tag()}"),
            ("To", "<sip:2000@pbx.example.test>"),
            ("Call-ID", call_id if call_id is not None else new_call_id()),
            ("CSeq", f"{cseq} INVITE"),
            ("Contact", "<sip:1000@abc123.invalid;transport=ws>"),
        ],
    )


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


async def test_malformed_cseq_on_invite_final_logs_warning_wss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A final non-2xx with an unparseable CSeq is logged, not silently dropped.

    Mirrors the TLS-transport regression test: before the fix,
    ``_auto_ack_non_2xx`` gives up via a bare ``return`` when the CSeq method
    isn't ``INVITE`` or ``_txn_key`` can't parse the sequence number — silently
    skipping both the mandatory ACK (RFC 3261 §17.1.1.3) and the client-
    transaction cleanup, with NO log at all. This exercises all three malformed
    shapes (omitted CSeq, CSeq with no number, CSeq with a non-numeric number)
    and asserts each is logged as a WARNING naming the Call-ID.
    """
    call_id = new_call_id()

    def responder(_frame: str) -> list[str]:
        return []  # the malformed finals are pushed directly below

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = _make_transport(server.port)
    with caplog.at_level(logging.WARNING, logger="hermes_voip.transport.ws_connection"):
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


async def test_out_of_dialog_cancel_for_unknown_invite_is_481_wss() -> None:
    """Over WSS a CANCEL with no matching pending INVITE is answered 481.

    The WSS transport now implements RFC 3261 §9.2 server-transaction tracking
    (ported from SipOverTlsTransport).  A CANCEL that does not match any tracked
    pending inbound INVITE is answered ``481 Call/Transaction Does Not Exist``
    by the §9.2 handler; it is NOT routed to ``on_unroutable`` (the handler owns
    this code path).  This supersedes the previous "fall-through to unroutable"
    pinning test, which documented a known interim gap now closed.
    """
    unroutable: list[object] = []

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_cancel=lambda _cid: None,
        on_unroutable=unroutable.append,
    )
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await transport.connect()
        cancel = (
            "CANCEL sip:1000@pbx.example.test SIP/2.0\r\n"
            "Via: SIP/2.0/WSS gw.pbx.example.test;branch=z9hG4bKinv1;rport\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:caller@pbx.example.test>;tag=remote\r\n"
            "To: <sip:1000@pbx.example.test>\r\n"
            "Call-ID: ws-cancel-1\r\n"
            "CSeq: 1 CANCEL\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        await server.push(cancel)
        resp = await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 481"), timeout=3.0
        )
        assert "CSeq: 1 CANCEL" in resp
        # The §9.2 handler sends the 481; on_unroutable must NOT fire.
        await asyncio.sleep(0.1)
        assert unroutable == [], "an unmatched WSS CANCEL must not fire on_unroutable"
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


async def test_crlf_keepalive_ping_does_not_drop_connection() -> None:
    """An RFC 5626 §4.4 / RFC 7118 CRLF keepalive frame must not crash the reader.

    Regression: a real Asterisk/appliance-class WebRTC edge sends bare CRLF keepalive
    frames over the WSS signalling channel. The reader fed each text frame straight to
    ``SipRequest.parse``, which raised ``not a SIP request-line: ''`` on the empty
    request-line, ending the reader task and dropping the registration — inbound calls
    then went to voicemail. The keepalive frame must be absorbed; the connection (and
    thus the registration) survives, proven by a following OPTIONS still being answered.
    """

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
        # The gateway sends a double-CRLF keepalive PING, then a normal OPTIONS.
        await server.push("\r\n\r\n")
        await server.push(_inbound_options(to_user="1000"))
        # If the keepalive crashed the reader, the OPTIONS 200 OK never arrives.
        response_frame = await server.wait_for_received(
            lambda f: f.startswith("SIP/2.0 200 "), timeout=3.0
        )
        assert "200 OK" in response_frame
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_crlf_keepalive_ping_is_answered_with_crlf_pong() -> None:
    """A double-CRLF keepalive PING is answered with a single-CRLF PONG (RFC 5626)."""

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
        await server.push("\r\n\r\n")
        pong = await server.wait_for_received(lambda f: f == "\r\n", timeout=3.0)
        assert pong == "\r\n"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_empty_keepalive_frame_is_ignored() -> None:
    """An empty text frame (degenerate keepalive) is dropped, not parsed as SIP."""

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
        await server.push("")  # empty frame must not crash the reader
        await server.push(_inbound_options(to_user="1000"))
        response_frame = await server.wait_for_received(
            lambda f: f.startswith("SIP/2.0 200 "), timeout=3.0
        )
        assert "200 OK" in response_frame
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


def _malformed_but_nonempty_frame() -> str:
    """A non-empty text frame that is not a valid SIP message (fails parse).

    It is not whitespace-only (so it is not treated as a CRLF keepalive) but its
    first line is neither a request-line nor a status-line, so
    :meth:`SipRequest.parse` raises ``ValueError``. Over WSS each frame is one whole
    message (RFC 7118 §5), so this is a per-message parse failure (bk646): it must be
    skipped (surfaced via a WARNING) without ending the reader and dropping the
    registration + every active call on the connection.
    """
    return (
        "GARBAGE-NOT-A-SIP-START-LINE\r\n"
        "Via: SIP/2.0/WSS gw.pbx.example.test;branch=z9hG4bKbad;rport\r\n"
        "Content-Length: 0\r\n\r\n"
    )


async def test_malformed_frame_is_skipped_connection_survives_next_frames() -> None:
    # bk646: a single malformed WS frame must NOT tear the connection down. The bad
    # frame is skipped (surfaced via a WARNING, never swallowed silently) and a
    # SUBSEQUENT well-formed frame on the SAME connection is still dispatched — proven
    # by a following OPTIONS still being answered 200 OK (the registration, and thus
    # every active call, survives). A bare parse() in _dispatch used to raise out of
    # the reader, ending it and firing on_connection_lost (dropping all active calls).
    lost: list[BaseException | None] = []

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_connection_lost=lost.append,
    )
    gateway = _gateway()
    manager = RegistrationManager(gateway, transport)
    transport.bind_manager(manager)
    try:
        await transport.connect()
        # The gateway sends ONE malformed frame, then a normal OPTIONS.
        await server.push(_malformed_but_nonempty_frame())
        await server.push(_inbound_options(to_user="1000"))
        # If the bad frame crashed the reader, the OPTIONS 200 OK never arrives.
        response_frame = await server.wait_for_received(
            lambda f: f.startswith("SIP/2.0 200 "), timeout=3.0
        )
        assert "200 OK" in response_frame
        assert lost == [], "a malformed frame must not fire on_connection_lost"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


# --------------------------------------------------------------------------
# ADR-0081 class, BUILD side (WSS): a header-incomplete inbound request that
# PARSES but whose auto-response cannot be built must not tear down the reader.
# --------------------------------------------------------------------------


def _cancel_missing_via() -> str:
    """A CANCEL frame that PARSES but carries NO Via — its 481 auto-response has none.

    Routes to _handle_cancel (no To-tag, method CANCEL), matches no pending INVITE (no
    Via branch), and reaches build_response(cancel, 481, ...) — which RAISES ValueError
    on the absent Via (RFC 3261 §8.2.6). The build runs INLINE, so before the fix the
    escape ended the reader and dropped the registration + every active call.
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
    """An out-of-dialog OPTIONS frame that PARSES but has NO Via for its 200."""
    return (
        "OPTIONS sip:1000@pbx.example.test SIP/2.0\r\n"
        "From: <sip:qualify@pbx.example.test>;tag=opt-nohdr-ft\r\n"
        "To: <sip:1000@pbx.example.test>\r\n"
        "Call-ID: options-no-via-tripwire\r\n"
        "CSeq: 1 OPTIONS\r\n"
        "Content-Length: 0\r\n\r\n"
    )


async def test_header_incomplete_cancel_frame_does_not_tear_down_reader_wss() -> None:
    # ADR-0081 class (build side, WSS): a CANCEL that PARSES but lacks Via routes to
    # _handle_cancel, whose build_response(481) raises ValueError INLINE in the reader.
    # The escape must be contained: the bad CANCEL is dropped (logged) and a SUBSEQUENT
    # well-formed OPTIONS on the same connection is still answered 200 OK — the reader
    # (and thus the registration + every active call) survives.
    lost: list[BaseException | None] = []

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_connection_lost=lost.append,
    )
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await transport.connect()
        await server.push(_cancel_missing_via())
        await server.push(_inbound_options(to_user="1000"))
        response_frame = await server.wait_for_received(
            lambda f: f.startswith("SIP/2.0 200 "), timeout=3.0
        )
        assert "200 OK" in response_frame
        assert lost == [], (
            "a header-incomplete CANCEL frame must not fire on_connection_lost"
        )
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_header_incomplete_keepalive_options_does_not_tear_down_reader_wss() -> (
    None
):
    # ADR-0081 class (build side, WSS), keepalive: an out-of-dialog OPTIONS missing Via
    # is auto-answered via build_options_ok → build_response, which raises. The bad
    # OPTIONS is dropped and a SUBSEQUENT well-formed OPTIONS is still answered 200 OK.
    lost: list[BaseException | None] = []

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_connection_lost=lost.append,
    )
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await transport.connect()
        await server.push(_options_missing_via())
        await server.push(_inbound_options(to_user="1000"))
        response_frame = await server.wait_for_received(
            lambda f: f.startswith("SIP/2.0 200 "), timeout=3.0
        )
        assert "200 OK" in response_frame
        assert lost == [], (
            "a header-incomplete keepalive OPTIONS must not fire on_connection_lost"
        )
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_malformed_response_frame_is_skipped_active_call_survives() -> None:
    # bk646 (the DoS angle, WSS): an established call's response routing must survive a
    # malformed frame on the shared WS connection. A response sink is registered for an
    # active Call-ID; the peer interleaves a malformed frame and then a well-formed
    # response for that call. The bad frame is skipped and the call's own response is
    # still delivered to its sink — one malformed frame is not a DoS against unrelated,
    # already-active calls.
    call_id = new_call_id()
    branch = new_branch()
    responses: list[SipResponse] = []
    lost: list[BaseException | None] = []

    class _Sink:
        async def on_response(self, response: SipResponse) -> None:
            responses.append(response)

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_connection_lost=lost.append,
    )
    try:
        await transport.connect()
        transport.add_call(call_id, _Sink())
        # A well-formed in-dialog 200 OK (e.g. to a re-INVITE) for the active call.
        ok = (
            "SIP/2.0 200 OK\r\n"
            f"Via: SIP/2.0/WSS {transport.local_sent_by};branch={branch};rport\r\n"
            "From: <sip:1000@pbx.example.test>;tag=local\r\n"
            "To: <sip:2000@pbx.example.test>;tag=remote\r\n"
            f"Call-ID: {call_id}\r\n"
            "CSeq: 2 INVITE\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        await server.push(_malformed_but_nonempty_frame())
        await server.push(ok)
        await _until(lambda: len(responses) == 1, timeout=3.0)
        assert responses[0].status_code == _OK_STATUS  # the call's own 200 OK
        assert lost == [], "the active call's connection must survive a bad frame"
    finally:
        await transport.aclose()
        await server.stop()


async def test_superscript_cseq_number_on_invite_final_survives_connection() -> None:
    # F1 (HIGH, ADR-0081, WSS mirror): a crafted INVITE-final frame with a
    # non-decimal CSeq NUMBER — the superscript "²": ``"²".isdigit()`` is True but
    # ``int("²")`` raises — reaches ``_txn_key`` (method token is a valid "INVITE")
    # and crashes in ``int()`` BEFORE any transaction lookup, so no correlated call
    # is needed. Unguarded it escaped the reader and dropped every active call.
    # Assert an UNRELATED active call survives and on_connection_lost never fires.
    active_call_id = new_call_id()
    active_branch = new_branch()
    responses: list[SipResponse] = []
    lost: list[BaseException | None] = []
    lost_called = asyncio.Event()

    class _Sink:
        async def on_response(self, response: SipResponse) -> None:
            responses.append(response)

    def on_lost(exc: BaseException | None) -> None:
        lost.append(exc)
        lost_called.set()

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_connection_lost=on_lost,
    )
    try:
        await transport.connect()
        transport.add_call(active_call_id, _Sink())
        active_invite = _outbound_invite(active_branch, call_id=active_call_id)
        await transport.send(active_invite)
        await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
        # The crafted frame carries its OWN (untracked) Call-ID; it correlates to no
        # transaction, and would crash in _txn_key before that even mattered.
        unrelated = SipRequest.parse(
            _outbound_invite(new_branch(), call_id=new_call_id())
        )
        await server.push(_final_with_cseq(unrelated, 486, "Busy Here", "² INVITE"))
        # RED before the fix: the reader crashes and fires on_connection_lost.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(lost_called.wait(), timeout=0.5)
        assert lost == [], "a non-decimal CSeq must not fire on_connection_lost"
        # The unrelated active call's own 200 OK is still routed to its sink.
        parsed_active = SipRequest.parse(active_invite)
        cseq = parsed_active.header("CSeq")
        assert cseq is not None
        await server.push(_final_with_cseq(parsed_active, 200, "OK", cseq))
        await _until(lambda: any(r.status_code == _OK_STATUS for r in responses))
    finally:
        await transport.aclose()
        await server.stop()


async def test_overlong_cseq_number_on_invite_final_survives_connection() -> None:
    # F1 residual (codex adversarial review), WSS mirror: a CSeq number that is
    # all-ASCII-decimal but longer than CPython's int-from-string digit limit
    # (sys.get_int_max_str_digits(), default 4300) passes ``isascii() and
    # isdecimal()`` yet still makes ``int()`` raise ValueError. A crafted
    # "CSeq: <thousands of digits> INVITE" frame must be dropped like "²" is.
    overlong = "9" * (sys.get_int_max_str_digits() + 1)
    active_call_id = new_call_id()
    active_branch = new_branch()
    responses: list[SipResponse] = []
    lost: list[BaseException | None] = []
    lost_called = asyncio.Event()

    class _Sink:
        async def on_response(self, response: SipResponse) -> None:
            responses.append(response)

    def on_lost(exc: BaseException | None) -> None:
        lost.append(exc)
        lost_called.set()

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_connection_lost=on_lost,
    )
    try:
        await transport.connect()
        transport.add_call(active_call_id, _Sink())
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
        parsed_active = SipRequest.parse(active_invite)
        cseq = parsed_active.header("CSeq")
        assert cseq is not None
        await server.push(_final_with_cseq(parsed_active, 200, "OK", cseq))
        await _until(lambda: any(r.status_code == _OK_STATUS for r in responses))
    finally:
        await transport.aclose()
        await server.stop()


def _final_missing_to(invite: SipRequest, status: int, reason: str) -> str:
    """A non-2xx final correlated to ``invite`` but carrying NO ``To`` header.

    Same Via/branch, Call-ID and CSeq number as ``invite`` (so it matches the
    outstanding INVITE client transaction), but no ``To``. parse() does not require
    ``To`` (it frames + parses cleanly), so this reaches _auto_ack_non_2xx and
    _build_ack's ``_require(response, "To")`` raises ValueError.
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
    # F3 (MEDIUM, ADR-0081, WSS mirror): a non-2xx final that CORRELATES to an
    # INVITE we sent (so a client transaction IS found) but lacks a To header makes
    # _build_ack's _require(response, "To") raise ValueError. Unguarded it escaped
    # the reader and tore the connection down. Assert the connection survives, no
    # ACK is sent, and a non-PII WARNING is logged.
    branch = new_branch()
    call_id = new_call_id()
    lost: list[BaseException | None] = []
    lost_called = asyncio.Event()

    def on_lost(exc: BaseException | None) -> None:
        lost.append(exc)
        lost_called.set()

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_connection_lost=on_lost,
    )
    with caplog.at_level(logging.WARNING, logger="hermes_voip.transport.ws_connection"):
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


async def test_remove_call_sink_identity_guard_matches_tls() -> None:
    """remove_call(call_id, sink) mirrors the TLS transport's identity guard.

    The adapter tears a call down with ``remove_call(call_id, that_call's_sink)``;
    the WSS transport MUST accept the second arg (or a WSS call teardown raises
    TypeError) AND only evict the entry when the sink still owns it — so an
    overlapping INVITE sharing the Call-ID (a later add_call) is not evicted by
    the earlier call's teardown (RFC 3261 fork/retransmit). Pure in-memory; no IO.
    """

    class _Sink:
        async def on_response(self, response: SipResponse) -> None:
            """Unused — the registry only stores the sink by identity here."""

    transport = _make_transport(5061)
    first: _Sink = _Sink()
    second: _Sink = _Sink()

    # A later call overwrites the same Call-ID; the earlier call's teardown
    # (passing its OWN sink) must be a no-op, leaving the live later sink in place.
    transport.add_call("call-1", first)
    transport.add_call("call-1", second)
    transport.remove_call("call-1", first)  # the 2-arg form must not raise
    assert transport._calls.get("call-1") is second

    # Passing the owning sink removes it; sink=None removes unconditionally.
    transport.remove_call("call-1", second)
    assert "call-1" not in transport._calls
    transport.add_call("call-2", first)
    transport.remove_call("call-2")
    assert "call-2" not in transport._calls


# ---------------------------------------------------------------------------
# caplog regression: malformed-SIP skip log must be non-PII (WSS, bk646)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Inbound CANCEL handling (RFC 3261 §9.2) — WSS server-transaction tracking
# ---------------------------------------------------------------------------
# These tests cover the ported §9.2 behaviour from SipOverTlsTransport:
#   - a CANCEL matching a tracked pending inbound INVITE fires on_cancel, sends
#     200 OK (to CANCEL) + 487 Request Terminated (to INVITE)
#   - an unmatched CANCEL (no prior INVITE tracked) is answered 481
# ---------------------------------------------------------------------------


def _wss_inbound_invite_with(*, to_user: str, call_id: str, branch: str) -> str:
    """A WSS inbound INVITE with an explicit branch for CANCEL matching."""
    return (
        f"INVITE sip:{to_user}@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/WSS gw.pbx.example.test;branch={branch};rport\r\n"
        "Max-Forwards: 70\r\n"
        "From: <sip:caller@pbx.example.test>;tag=remote\r\n"
        f"To: <sip:{to_user}@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 INVITE\r\n"
        "Contact: <sip:caller@gw.pbx.example.test;transport=ws>\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _wss_cancel_for(*, to_user: str, call_id: str, branch: str) -> str:
    """A CANCEL matching the branch/Call-ID of a prior WSS inbound INVITE."""
    return (
        f"CANCEL sip:{to_user}@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/WSS gw.pbx.example.test;branch={branch};rport\r\n"
        "Max-Forwards: 70\r\n"
        "From: <sip:caller@pbx.example.test>;tag=remote\r\n"
        f"To: <sip:{to_user}@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 CANCEL\r\n"
        "Content-Length: 0\r\n\r\n"
    )


_TO_TAG_RE = re.compile(r";\s*tag=([^;,\s]+)", re.IGNORECASE)


def _to_tag_of(frame: str) -> str | None:
    """The ``tag`` parameter of a response's ``To`` header (None if absent)."""
    to_value = SipResponse.parse(frame).header("To")
    if to_value is None:
        return None
    match = _TO_TAG_RE.search(to_value)
    return match.group(1) if match is not None else None


async def test_wss_inbound_cancel_487s_invite_and_200s_cancel_and_fires_on_cancel() -> (
    None
):
    # RFC 3261 §9.2 on the WSS transport: a CANCEL matching a tracked pending
    # inbound INVITE must answer the CANCEL 200 OK, the INVITE 487 Request
    # Terminated, and fire the on_cancel hook (Call-ID) so the adapter tears
    # down the half-built call. Mirrors the TLS equivalent in test_connection.py.
    new_calls: list[NewCall] = []
    cancelled: list[str] = []
    call_id = new_call_id()
    branch = "z9hG4bKwscancel"

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_new_call=new_calls.append,
        on_cancel=cancelled.append,
    )
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await transport.connect()
        # Step 1: inbound INVITE arrives and is tracked.
        await server.push(
            _wss_inbound_invite_with(to_user="1000", call_id=call_id, branch=branch)
        )
        await _until(lambda: len(new_calls) == 1, timeout=3.0)
        # Step 2: caller abandons — a CANCEL for the same transaction.
        await server.push(
            _wss_cancel_for(to_user="1000", call_id=call_id, branch=branch)
        )
        # Transport must answer the CANCEL 200 OK ...
        ok = await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 200") and "CSeq: 1 CANCEL" in raw,
            timeout=3.0,
        )
        assert "CSeq: 1 CANCEL" in ok
        # ... the pending INVITE 487 Request Terminated ...
        terminated = await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 487"), timeout=3.0
        )
        assert "CSeq: 1 INVITE" in terminated
        # ... and the abort hook must fire with this call's Call-ID.
        await _until(lambda: cancelled == [call_id])
        assert cancelled == [call_id]
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_wss_cancel_for_unknown_invite_is_481() -> None:
    # RFC 3261 §9.2: a CANCEL matching no tracked INVITE server transaction
    # on the WSS transport is answered 481 Call/Transaction Does Not Exist
    # (not routed to on_unroutable — the §9.2 handler owns this code path).
    # Mirrors test_cancel_for_unknown_invite_is_481 on the TLS transport.
    unroutable: list[object] = []

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_new_call=lambda _nc: None,
        on_cancel=lambda _cid: None,
        on_unroutable=unroutable.append,
    )
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await transport.connect()
        await server.push(
            _wss_cancel_for(
                to_user="1000", call_id="wss-never-seen", branch="z9hG4bKwsghost"
            )
        )
        resp = await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 481"), timeout=3.0
        )
        assert "CSeq: 1 CANCEL" in resp
        # The 481 is sent by the §9.2 handler; on_unroutable must NOT fire.
        await asyncio.sleep(0.1)
        assert unroutable == [], "an unmatched WSS CANCEL must not fire on_unroutable"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_wss_retransmitted_cancel_is_absorbed_no_second_487_or_abort() -> None:
    # A retransmitted CANCEL on the WSS transport must be absorbed: the 200 OK
    # to the CANCEL is re-sent, but the INVITE is NOT 487'd a second time and
    # on_cancel fires only once (idempotent RFC 3261 §9.2).
    # Mirrors test_retransmitted_cancel_is_absorbed_no_second_487_or_abort (TLS).
    new_calls: list[NewCall] = []
    cancelled: list[str] = []
    call_id = new_call_id()
    branch = "z9hG4bKwsretx"

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_new_call=new_calls.append,
        on_cancel=cancelled.append,
    )
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await transport.connect()
        await server.push(
            _wss_inbound_invite_with(to_user="1000", call_id=call_id, branch=branch)
        )
        await _until(lambda: len(new_calls) == 1, timeout=3.0)
        cancel = _wss_cancel_for(to_user="1000", call_id=call_id, branch=branch)
        await server.push(cancel)
        await _until(lambda: cancelled == [call_id])
        await server.wait_for_received(
            lambda raw: raw.startswith("SIP/2.0 487"), timeout=3.0
        )
        # The peer retransmits the CANCEL.
        await server.push(cancel)
        await asyncio.sleep(0.1)
        # Exactly one 487 (the retransmit was absorbed).
        terminated = [raw for raw in server.received if raw.startswith("SIP/2.0 487")]
        assert len(terminated) == 1, (
            "a retransmitted WSS CANCEL must not re-487 the INVITE"
        )
        assert cancelled == [call_id], "on_cancel must fire only once for a WSS CANCEL"
    finally:
        await manager.aclose()
        await transport.aclose()
        await server.stop()


async def test_wss_cancel_200_totag_matches_487_and_is_stable_across_retransmit() -> (
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
    branch = "z9hG4bKwstag"

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_new_call=new_calls.append,
        on_cancel=cancelled.append,
    )
    manager = RegistrationManager(_gateway(), transport)
    transport.bind_manager(manager)
    try:
        await transport.connect()
        await server.push(
            _wss_inbound_invite_with(to_user="1000", call_id=call_id, branch=branch)
        )
        await _until(lambda: len(new_calls) == 1, timeout=3.0)
        cancel = _wss_cancel_for(to_user="1000", call_id=call_id, branch=branch)
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


async def test_malformed_frame_caplog_non_pii(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WARNING log for a skipped malformed frame is non-PII (WSS transport, bk646).

    Over WebSocket each frame is one whole SIP message (RFC 7118 §5), so a frame
    that fails to parse is logged as a WARNING and skipped. This test
    regression-guards the log format: it must carry ONLY the exception type name
    + the byte length of the raw frame — never the raw bytes themselves (which can
    contain From/To/Call-ID/SDP and therefore PII).

    A mutation that logs ``str(exc)`` or the raw frame will include the first line
    of the malformed payload ("GARBAGE-NOT-A-SIP-START-LINE") in caplog and
    trigger the second assertion here.
    """
    malformed = _malformed_but_nonempty_frame()

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
    )
    await transport.connect()
    with caplog.at_level(logging.WARNING, logger="hermes_voip.transport.ws_connection"):
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
        "expected at least one WARNING for the malformed frame"
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
    # (2) No raw SIP content from the malformed frame must appear in caplog.
    # "GARBAGE-NOT-A-SIP-START-LINE" would appear if str(exc) or raw bytes were logged.
    assert "GARBAGE-NOT-A-SIP-START-LINE" not in caplog.text, (
        "raw malformed-frame content must NOT appear in the log (rule 34 / PII guard)"
    )
    # The branch parameter in the Via header of the malformed fixture must not leak.
    assert "z9hG4bKbad" not in caplog.text, (
        "Via branch from the malformed fixture must NOT appear in the log (PII guard)"
    )


# --------------------------------------------------------------------------
# ADR-0098 (amends ADR-0081), WSS mirror: a SCOPED reader dispatch-boundary
# fail-safe backstop. The reader awaits ``_dispatch_response`` /
# ``_dispatch_request`` OUTSIDE ``_dispatch``'s parse-only ``try``, so ANY
# non-``ValueError`` a downstream handler raises escapes the reader →
# ``_on_reader_done`` → ``on_connection_lost``, dropping the registration and
# every active call. These inject a SYNTHETIC unanticipated handler fault and
# assert the scoped backstop keeps the connection alive, while still letting
# ``asyncio.CancelledError`` propagate (``except Exception``, not
# ``except BaseException``).
# --------------------------------------------------------------------------


class _RaisingSink:
    """A response sink whose ``on_response`` re-raises a caller-chosen exception.

    Synthesises an UNANTICIPATED fault at the reader's dispatch boundary
    (ADR-0098): a ``RuntimeError`` (a non-``ValueError`` the per-site ADR-0081
    guards do not catch) or an ``asyncio.CancelledError`` (which the backstop must
    let propagate). Routing to it is by Call-ID via :meth:`add_call`.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def on_response(self, response: SipResponse) -> None:
        raise self._exc


def _poison_response(call_id: str) -> str:
    """A well-formed non-2xx frame routed to ``call_id`` (CSeq method ``BYE``).

    It parses cleanly and — because its CSeq method is not ``INVITE`` — bypasses
    the auto-ACK / transaction path, so it is delivered straight to the registered
    response sink. A sink that raises then exercises the reader's dispatch boundary
    directly.
    """
    return (
        "SIP/2.0 480 Temporarily Unavailable\r\n"
        "Via: SIP/2.0/WSS client.invalid;branch=z9hG4bKpoison;rport\r\n"
        "From: <sip:2000@pbx.example.test>;tag=remote\r\n"
        "To: <sip:1000@pbx.example.test>;tag=local\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 BYE\r\n"
        "Content-Length: 0\r\n\r\n"
    )


async def test_active_call_survives_a_handler_that_raises_a_non_valueerror(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # ADR-0098 (WSS mirror): a response frame routed to a call whose sink raises a
    # NON-ValueError (a synthetic RuntimeError — an unanticipated handler bug the
    # per-site ADR-0081 guards do not catch) is awaited by the reader OUTSIDE
    # _dispatch's parse-only try. Unguarded it ends the reader and fires
    # on_connection_lost, dropping the registration + every other call. The scoped
    # dispatch-boundary backstop must catch it, log a loud non-PII WARNING, drop
    # the one frame, and keep the connection + every unrelated call alive.
    active_call_id = new_call_id()
    active_branch = new_branch()
    poison_call_id = new_call_id()
    responses: list[SipResponse] = []
    lost: list[BaseException | None] = []
    lost_called = asyncio.Event()

    class _Sink:
        async def on_response(self, response: SipResponse) -> None:
            responses.append(response)

    def on_lost(exc: BaseException | None) -> None:
        lost.append(exc)
        lost_called.set()

    def responder(_frame: str) -> list[str]:
        return []

    server = LoopbackWsSipServer(responder)
    await server.start()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=server.port,
        ws_path="/ws",
        connect_address="127.0.0.1",
        on_connection_lost=on_lost,
    )
    with caplog.at_level(logging.WARNING, logger="hermes_voip.transport.ws_connection"):
        await transport.connect()
        transport.add_call(active_call_id, _Sink())
        transport.add_call(poison_call_id, _RaisingSink(RuntimeError("synthetic")))
        try:
            active_invite = _outbound_invite(active_branch, call_id=active_call_id)
            await transport.send(active_invite)
            await server.wait_for_received(lambda raw: raw.startswith("INVITE "))
            # The poison frame drives the sink's RuntimeError inside the reader.
            await server.push(_poison_response(poison_call_id))
            # RED before the backstop: the reader crashes and fires on_connection_lost.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(lost_called.wait(), timeout=0.5)
            assert lost == [], "a handler exception must not fire on_connection_lost"
            # The reader survived: the UNRELATED active call's own 200 OK still routes.
            parsed_active = SipRequest.parse(active_invite)
            cseq = parsed_active.header("CSeq")
            assert cseq is not None
            await server.push(_final_with_cseq(parsed_active, 200, "OK", cseq))
            await _until(lambda: any(r.status_code == _OK_STATUS for r in responses))
        finally:
            await transport.aclose()
            await server.stop()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("RuntimeError" in r.getMessage() for r in warnings), (
        "the backstop must log a WARNING naming the exception type (RuntimeError)"
    )
    # rule 34: the WARNING must carry the type name ONLY — never wire content. The
    # poison frame's Call-ID is routing detail/PII and must not appear in any log.
    for record in warnings:
        assert poison_call_id not in record.getMessage(), (
            f"the backstop WARNING must not leak wire content: {record.getMessage()!r}"
        )


async def test_a_handler_cancellederror_propagates_out_of_dispatch() -> None:
    # ADR-0098 (WSS mirror): the backstop catches ``Exception`` — NOT
    # ``BaseException`` — so ``asyncio.CancelledError`` (a BaseException in 3.13)
    # still propagates out of ``_dispatch`` and ends the reader task
    # (``_read_loop`` awaits ``_dispatch`` per frame; rule 37). Driven directly at
    # ``_dispatch`` — the exact site the backstop wraps. Passes both before AND
    # after the backstop (a guard against over-catching); FAILS if the catch were
    # ``BaseException``.
    call_id = new_call_id()
    transport = WssSipTransport(
        host="pbx.example.test",
        port=0,
        ws_path="/ws",
        connect_address="127.0.0.1",
    )
    transport.add_call(call_id, _RaisingSink(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await transport._dispatch(_poison_response(call_id))
