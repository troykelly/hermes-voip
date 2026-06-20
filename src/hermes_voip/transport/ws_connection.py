"""The asyncio SIP-over-Secure-WebSocket transport (RFC 7118, ADR-0016 §1).

:class:`WssSipTransport` is the WebSocket counterpart to
:class:`~hermes_voip.transport.connection.SipOverTlsTransport`.  It connects
over a secure WebSocket (``wss://`` with subprotocol ``sip``, RFC 7118 §4.1),
and the two share the same upper-layer seams:

* it implements :class:`~hermes_voip.manager.SipTransport` (``send`` /
  ``local_sent_by`` / ``contact_uri``) so the
  :class:`~hermes_voip.manager.RegistrationManager` drives it unchanged;
* it is also each call's :class:`~hermes_voip.call.CallSignaling` (``send``);
* it keeps the same Call-ID demux, ``on_new_call`` / ``on_unroutable`` /
  ``on_connection_lost`` observers, auto-ACK of non-2xx INVITE finals
  (:class:`~hermes_voip.transport.transaction.InviteClientTransaction`), and
  out-of-dialog OPTIONS/NOTIFY keepalive as the TLS transport.

The three substantive deltas from :class:`SipOverTlsTransport` are:

**Framing** — Over WebSocket each SIP message is exactly one text frame
(RFC 7118 §5); there is no ``Content-Length`` stream-framing problem.  A
received text frame *is* a complete message, dispatched straight to
:class:`~hermes_voip.message.SipRequest.parse` /
:class:`~hermes_voip.message.SipResponse.parse`.
:class:`~hermes_voip.transport.framing.SipMessageFramer` is **not used** here.

**Via transport + sent-by** — The Via ``transport`` token is ``WSS`` (RFC
7118 §5.1).  The client has no routable address, so the Via ``sent-by`` is a
stable random token under the **``.invalid`` TLD** (RFC 2606), e.g.
``hq7sk2.invalid``.  :attr:`local_sent_by` returns that token.

**Contact + RFC 5626 Outbound** — :meth:`contact_uri` emits
``<sip:<ext>@<token>.invalid;transport=ws>;reg-id=1;+sip.instance="<urn:uuid:…>"``
— a per-registration persistent instance-id URN (RFC 5626 §4.1) and a
``reg-id`` (RFC 5626 §4.2).  The ``+sip.instance`` UUID is stable per
extension for the process lifetime of this transport object.

Errors propagate (rule 37): a failed ``recv`` or a closed connection reports
via ``on_connection_lost``; a stray response or unroutable request surfaces via
``on_unroutable`` (a normal network event — it does not tear the connection).

The ``websockets`` library (BSD-3-Clause) is **lazy-imported** inside
:meth:`connect` so that ``import hermes_voip`` is light when the ``webrtc``
optional extra is not installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import re
import secrets
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from hermes_voip.keepalive import build_keepalive_ok, build_options_ok
from hermes_voip.manager import (
    Cancel,
    InDialog,
    NewCall,
    RegistrationManager,
    Unroutable,
)
from hermes_voip.message import SipRequest, SipResponse, new_tag
from hermes_voip.transport.connection import CallResponseSink
from hermes_voip.transport.transaction import (
    InviteClientTransaction,
    TransactionState,
)

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

__all__ = ["WssSipTransport"]

_RESPONSE_PREFIX = "SIP/2.0 "
# RFC 5626 §4.4.1 / RFC 7118 CRLF keepalive: a double-CRLF ping is answered with a
# single-CRLF pong. Over WebSocket each arrives as one (whitespace-only) text frame.
_CRLF_KEEPALIVE_PING = "\r\n\r\n"
_CRLF_KEEPALIVE_PONG = "\r\n"
# A To/From header parameter carrying a dialog tag (after the name-addr's '>').
_TO_TAG_PARAM = re.compile(r";\s*tag=", re.IGNORECASE)


class WssSipTransport:
    """An asyncio SIP-over-Secure-WebSocket transport (RFC 7118, ADR-0016 §1).

    Implements :class:`~hermes_voip.manager.SipTransport` and
    :class:`~hermes_voip.call.CallSignaling` — the same seams as
    :class:`~hermes_voip.transport.connection.SipOverTlsTransport` —
    so the manager, dialog, CallSession, CallLoop, and adapter are unchanged.
    """

    def __init__(  # noqa: PLR0913 — connection identity plus observer callbacks; keyword-only
        self,
        *,
        host: str,
        port: int,
        ws_path: str = "/ws",
        connect_address: str | None = None,
        ssl_context: object | None = None,
        on_new_call: Callable[[NewCall], None] | None = None,
        on_unroutable: Callable[[Unroutable | SipResponse], None] | None = None,
        on_connection_lost: Callable[[BaseException | None], None] | None = None,
    ) -> None:
        """Configure the transport (no IO until :meth:`connect`).

        Args:
            host: The gateway hostname — used both as the SNI for TLS and as
                the ``wss://`` host in the WebSocket URI.
            port: The WebSocket port (default ``443`` for ``wss``).
            ws_path: The HTTP upgrade path for the WebSocket endpoint (e.g.
                ``/ws``). The runtime supplies this from ``HERMES_SIP_WS_PATH``
                via :attr:`~hermes_voip.config.GatewayConfig.ws_path` when
                ``HERMES_SIP_TRANSPORT=wss`` selects this transport in
                ``adapter._establish()`` (ADR-0038; default ``/ws``).
            connect_address: An alternative IP or hostname to dial (e.g.
                ``127.0.0.1`` in tests); the TLS SNI / hostname verification
                still uses ``host``.
            ssl_context: An :class:`ssl.SSLContext` for the TLS layer, or
                ``None`` to use an insecure plain-WS connection (tests only).
                Production always passes a context (the adapter enforces this).
            on_new_call: Invoked with each out-of-dialog INVITE the manager
                maps to a registration; the consumer builds the CallSession.
            on_unroutable: Invoked with an unroutable request or a response
                that matches no registration or active call (surfaced, not
                swallowed).
            on_connection_lost: Invoked when the reader task ends, with the
                exception that ended it (``None`` on a clean close).
        """
        self._host = host
        self._port = port
        self._ws_path = ws_path
        self._connect_address = connect_address if connect_address is not None else host
        self._ssl_context = ssl_context
        self._on_new_call = on_new_call
        self._on_unroutable = on_unroutable
        self._on_connection_lost = on_connection_lost
        self._ws: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        # The stable `<token>.invalid` sent-by for this transport instance.
        # Generated eagerly (not deferred to connect()) because the WSS sent-by
        # is a random token — not derived from a socket address — so it is stable
        # across reconnects and available to RegistrationManager before connect().
        self._local_sent_by: str = f"{secrets.token_hex(6)}.invalid"
        # Per-extension stable instance UUIDs for the Outbound Contact (RFC 5626).
        self._instance_ids: dict[str, uuid.UUID] = {}
        self._manager: RegistrationManager | None = None
        self._calls: dict[str, CallResponseSink] = {}
        self._client_txns: dict[tuple[str, int], InviteClientTransaction] = {}
        self._send_lock = asyncio.Lock()

    # --- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Open the WebSocket connection (subprotocol ``sip``) and start reading.

        Lazy-imports ``websockets`` (the ``webrtc`` optional extra) so that
        ``import hermes_voip`` is light when the extra is absent.

        Raises:
            ImportError: if the ``webrtc`` extra (``websockets``) is not installed.
            websockets.exceptions.WebSocketException: if the WS handshake fails.
        """
        # Lazy import so the default install does not require websockets.
        ws_client_mod = importlib.import_module("websockets.asyncio.client")
        ws_connect = ws_client_mod.connect

        # Build the WebSocket URI. Use the connect_address for the TCP dial but
        # pass the host for TLS SNI via ssl= and server_hostname= kwargs.
        scheme = "ws" if self._ssl_context is None else "wss"
        uri = f"{scheme}://{self._connect_address}:{self._port}{self._ws_path}"

        kwargs: dict[str, object] = {
            "subprotocols": ["sip"],
            # SIP messages can be multi-KiB (large SDP+ICE); 256 KiB matches
            # the TLS transport's defensive _MAX_MESSAGE cap.
            "max_size": 256 * 1024,
            # Disable websockets' built-in ping keepalive: SIP keepalive is
            # handled by the keepalive module (OPTIONS/NOTIFY), and WebSocket
            # Ping frames are handled separately. If ping_interval is None,
            # websockets does not send its own pings.
            "ping_interval": None,
        }
        if self._ssl_context is not None:
            kwargs["ssl"] = self._ssl_context
            # server_hostname ensures TLS SNI uses the real host name even when
            # connecting to a numeric IP (connect_address).
            kwargs["server_hostname"] = self._host

        self._ws = await ws_connect(uri, **kwargs)
        self._reader_task = asyncio.create_task(self._read_loop(self._ws))
        self._reader_task.add_done_callback(self._on_reader_done)

    async def aclose(self) -> None:
        """Stop the reader task and close the WebSocket connection (idempotent)."""
        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(OSError):
                await ws.close()

    # --- SipTransport seam --------------------------------------------------

    @property
    def local_sent_by(self) -> str:
        """The Via ``sent-by`` — a ``<token>.invalid`` host (RFC 2606 / RFC 7118 §5.1).

        Returns a stable random token under the ``.invalid`` TLD, e.g.
        ``hq7sk2.invalid``.  Unlike the TLS transport's socket-derived
        ``host:port``, the WSS sent-by is a random token generated at
        construction time (no socket address is meaningful for a WebSocket
        client behind NAT), so it is available before :meth:`connect`.
        """
        return self._local_sent_by

    def contact_uri(self, extension: str) -> str:
        """The ``Contact`` header value for ``extension`` on this WSS transport.

        Per RFC 7118 and RFC 5626 (Outbound) the Contact carries:

        * ``sip:<ext>@<token>.invalid`` — the SIP URI with the ``.invalid``
          host and no port (the WebSocket connection is addressed by URI, not
          by socket address);
        * ``;transport=ws`` — the WebSocket transport URI parameter (RFC 7118
          §5.3, lowercase ``ws`` even for WSS connections);
        * ``>;reg-id=1`` — the Outbound registration leg identifier (RFC 5626
          §4.2);
        * ``+sip.instance="<urn:uuid:…>"`` — a per-extension persistent
          instance URN (RFC 5626 §4.1), stable for the lifetime of this
          transport object.

        The instance UUID is derived deterministically per extension (not from a
        real device id — the repo is public).

        Args:
            extension: The SIP user-part / extension number.
        """
        sent_by = self.local_sent_by
        instance_id = self._instance_for(extension)
        return (
            f"<sip:{extension}@{sent_by};transport=ws>"
            f";reg-id=1"
            f';+sip.instance="<urn:uuid:{instance_id}>"'
        )

    def _instance_for(self, extension: str) -> uuid.UUID:
        """Return the stable per-extension RFC 5626 instance UUID."""
        if extension not in self._instance_ids:
            self._instance_ids[extension] = uuid.uuid4()
        return self._instance_ids[extension]

    async def send(self, message: str) -> None:
        """Send one SIP message as exactly one WebSocket text frame.

        Per RFC 7118 §5 each SIP message is one WebSocket message/frame.  The
        message must be a complete, well-formed SIP request or response (the
        ``Content-Length`` body is already present; we do not alter it).

        Also tracks outbound INVITE client transactions for auto-ACK of non-2xx
        finals (mirroring :class:`SipOverTlsTransport`).

        Args:
            message: The complete SIP message text.

        Raises:
            RuntimeError: if called before :meth:`connect`.
            websockets.exceptions.WebSocketException: if the connection is lost.
        """
        if self._ws is None:
            msg = "cannot send before connect()"
            raise RuntimeError(msg)
        self._register_if_invite(message)
        async with self._send_lock:
            await self._ws.send(message)

    def _register_if_invite(self, message: str) -> None:
        """Track an outbound INVITE in the client-transaction table."""
        if message.startswith(_RESPONSE_PREFIX):
            return  # a response we are sending, not a request
        request = SipRequest.parse(message)
        if request.method != "INVITE":
            return
        key = _txn_key(request.header("Call-ID"), request.header("CSeq"))
        if key is not None:
            self._client_txns[key] = InviteClientTransaction(message)

    # --- call registry (response routing) -----------------------------------

    def add_call(self, call_id: str, sink: CallResponseSink) -> None:
        """Register a call's response sink so its responses route to it by Call-ID."""
        self._calls[call_id] = sink

    def remove_call(self, call_id: str, sink: CallResponseSink | None = None) -> None:
        """Forget a call; drop any tracked client transactions for it.

        Mirrors :meth:`SipOverTlsTransport.remove_call`: when ``sink`` is given
        the registration is only removed if it is still that exact sink (an
        overlapping INVITE sharing a Call-ID may have overwritten the entry, so
        an earlier call's teardown must not evict the live later one). With
        ``sink=None`` the removal is unconditional.
        """
        if sink is not None and self._calls.get(call_id) is not sink:
            return
        self._calls.pop(call_id, None)
        for key in [k for k in self._client_txns if k[0] == call_id]:
            del self._client_txns[key]

    async def send_cancel(self, call_id: str) -> bool:
        """Outbound CANCEL is not supported on the WSS UAC yet (ADR-0069 scope).

        Provided so the :class:`~hermes_voip.adapter.SignalingTransport` union has a
        uniform ``send_cancel`` surface; the WSS WebRTC origination path frames a
        different exchange and has no Via client-transaction registry to build a §9.1
        CANCEL from, so this is a no-op returning ``False`` (nothing was CANCELled).
        The ``call_id`` is accepted for signature parity with the SIP/TLS transport.
        """
        _ = call_id  # accepted for parity; the WSS path has no CANCEL to send
        return False

    def bind_manager(self, manager: RegistrationManager) -> None:
        """Bind the registration manager the transport demuxes through."""
        self._manager = manager

    # --- inbound reader + dispatch ------------------------------------------

    async def _read_loop(self, ws: ClientConnection) -> None:
        """Receive text frames and dispatch until the connection closes.

        Each text frame is exactly one complete SIP message (RFC 7118 §5); no
        framing state is needed.  The loop ends cleanly on a normal WS close or
        raises on an error (reported via ``on_connection_lost``).
        """
        while True:
            frame = await ws.recv(decode=True)
            if isinstance(frame, str):
                # RFC 7118 / RFC 5626 §4.4 CRLF keepalive: the gateway may send a
                # bare double-CRLF ping ("\r\n\r\n"), a single-CRLF pong ("\r\n"),
                # or a degenerate empty text frame. None is a SIP message — feeding
                # one to SipRequest.parse would raise on the empty request-line and
                # end the reader (dropping the registration, so inbound calls route
                # to voicemail). Answer a double-CRLF ping with a single-CRLF pong
                # on THIS live connection (a send failure propagates and ends the
                # reader, surfacing as a connection loss — never swallowed); ignore
                # the empty/pong cases. The TLS transport gets this for free via
                # SipMessageFramer._skip_keepalive_crlf; the per-frame WSS path
                # needs it explicitly.
                if not frame.strip():
                    if frame == _CRLF_KEEPALIVE_PING:
                        async with self._send_lock:
                            await ws.send(_CRLF_KEEPALIVE_PONG)
                else:
                    await self._dispatch(frame)
            # Binary frames (STUN/DTLS — later PRs) are silently dropped here;
            # the media plane owns those on the media socket, not the signalling WS.

    async def _dispatch(self, raw: str) -> None:
        if raw.startswith(_RESPONSE_PREFIX):
            await self._dispatch_response(SipResponse.parse(raw))
        else:
            await self._dispatch_request(SipRequest.parse(raw))

    async def _dispatch_response(self, response: SipResponse) -> None:
        await self._auto_ack_non_2xx(response)
        call_id = response.header("Call-ID")
        manager = self._manager
        if manager is not None and call_id in manager.registration_call_ids:
            await manager.on_response(response)
            return
        sink = self._calls.get(call_id) if call_id is not None else None
        if sink is not None:
            await sink.on_response(response)
            return
        self._report_unroutable(response)

    async def _auto_ack_non_2xx(self, response: SipResponse) -> None:
        """ACK a non-2xx final to an INVITE we sent (RFC 3261 §17.1.1.3).

        Identical logic to the TLS transport: the client transaction generates
        the ACK (same branch), not the TU.  Over a reliable transport (WS) there
        are no retransmission timers, but the ACK absorbs retransmitted finals.
        """
        if _method_of(response.header("CSeq")) != "INVITE":
            return
        key = _txn_key(response.header("Call-ID"), response.header("CSeq"))
        txn = self._client_txns.get(key) if key is not None else None
        if txn is None or key is None:
            return
        ack = txn.ack_for_response(response)
        if ack is not None:
            await self.send(ack)
        if txn.state is TransactionState.TERMINATED:
            del self._client_txns[key]

    async def _dispatch_request(self, request: SipRequest) -> None:
        manager = self._manager
        if manager is None:
            self._report_unroutable(Unroutable(request, "no manager bound"))
            return
        routing = manager.route_request(request)
        if isinstance(routing, NewCall):
            if self._on_new_call is not None:
                self._on_new_call(routing)
            else:
                self._report_unroutable(
                    Unroutable(request, "inbound call with no on_new_call handler")
                )
        elif isinstance(routing, InDialog):
            await routing.consumer.handle_request(routing.request)
        elif isinstance(routing, Cancel):
            # CANCEL handling (RFC 3261 §9.2) is implemented on the TLS transport
            # only; over WSS a CANCEL falls through to the unroutable path as
            # before (a follow-up will port the server-transaction tracking here).
            self._report_unroutable(Unroutable(request, "out-of-dialog CANCEL"))
        elif await self._answer_keepalive(request):
            return
        else:
            self._report_unroutable(routing)

    async def _answer_keepalive(self, request: SipRequest) -> bool:
        """Answer an out-of-dialog ``OPTIONS``/``NOTIFY`` 200 OK; report success.

        Identical behaviour to the TLS transport: the gateway qualifies the
        registered contact with out-of-dialog OPTIONS pings (RFC 3261 §11); we
        respond 200 OK so the registrar does not mark the endpoint unreachable.
        Unsolicited NOTIFY (e.g. MWI) is acknowledged but not processed.

        Returns ``True`` when a response was sent (caller must not also report
        the request unroutable), ``False`` when this is not a keepalive request.
        """
        if _has_to_tag(request.header("To")):
            return False
        if request.method == "OPTIONS":
            await self.send(build_options_ok(request, to_tag=new_tag()))
            return True
        if request.method == "NOTIFY":
            await self.send(build_keepalive_ok(request, to_tag=new_tag()))
            return True
        return False

    def _report_unroutable(self, what: Unroutable | SipResponse) -> None:
        if self._on_unroutable is not None:
            self._on_unroutable(what)

    def _on_reader_done(self, task: asyncio.Task[None]) -> None:
        """Observe the reader task's end; surface any failure (never swallow)."""
        if self._reader_task is task:
            self._reader_task = None
        if task.cancelled():
            return
        error = task.exception()
        if self._on_connection_lost is not None:
            self._on_connection_lost(error)


# ---------------------------------------------------------------------------
# Module-level helpers (mirror the private helpers in connection.py)
# ---------------------------------------------------------------------------


def _has_to_tag(to_value: str | None) -> bool:
    """``True`` when a ``To`` header carries a dialog ``tag`` parameter."""
    if to_value is None:
        return False
    search_space = to_value.split(">", 1)[1] if ">" in to_value else to_value
    return _TO_TAG_PARAM.search(search_space) is not None


def _method_of(cseq: str | None) -> str | None:
    if cseq is None:
        return None
    parts = cseq.split()
    return parts[1] if len(parts) >= 2 else None  # noqa: PLR2004 — CSeq is "<num> <method>"


def _txn_key(call_id: str | None, cseq: str | None) -> tuple[str, int] | None:
    if call_id is None or cseq is None:
        return None
    parts = cseq.split()
    if not parts or not parts[0].isdigit():
        return None
    return (call_id, int(parts[0]))
