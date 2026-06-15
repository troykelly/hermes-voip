"""The asyncio SIP-over-TLS transport: the live IO layer (ADR-0005).

:class:`SipOverTlsTransport` is the single signalling-IO hub. It owns one TLS
connection to the gateway, frames the inbound byte stream into whole SIP messages
(:class:`~hermes_voip.transport.framing.SipMessageFramer`), parses them, and
demuxes:

* a **response** routes by ``Call-ID`` — a registration Call-ID
  (:attr:`~hermes_voip.manager.RegistrationManager.registration_call_ids`) to
  :meth:`~hermes_voip.manager.RegistrationManager.on_response`, otherwise to the
  owning call's response sink (a ``CallSession``) registered via :meth:`add_call`;
* a **request** routes via
  :meth:`~hermes_voip.manager.RegistrationManager.route_request` —
  :class:`~hermes_voip.manager.NewCall` to the ``on_new_call`` callback,
  :class:`~hermes_voip.manager.InDialog` to the owning consumer's
  ``handle_request``, :class:`~hermes_voip.manager.Unroutable` to ``on_unroutable``.

It implements :class:`~hermes_voip.manager.SipTransport` (``send`` /
``local_sent_by`` / ``contact_uri``) and is each call's
:class:`~hermes_voip.call.CallSignaling` (``send``). It owns the transaction
concern those seams delegate here: when **we** send an INVITE, the transport
tracks its client transaction and **auto-ACKs a non-2xx final** (RFC 3261
§17.1.1.3) — the TU (``CallSession``) only ever ACKs the 2xx.

Errors propagate (rule 37): an unparseable / unframable stream fails the reader
task and is reported via ``on_connection_lost``; a stray response or unroutable
request is surfaced via ``on_unroutable`` (a normal network event — it does not
tear the connection down).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from hermes_voip.manager import (
    InDialog,
    NewCall,
    RegistrationManager,
    Unroutable,
)
from hermes_voip.message import SipRequest, SipResponse
from hermes_voip.transport.framing import SipMessageFramer
from hermes_voip.transport.transaction import (
    InviteClientTransaction,
    TransactionState,
)

__all__ = ["CallResponseSink", "SipOverTlsTransport"]

_RESPONSE_PREFIX = "SIP/2.0 "


@runtime_checkable
class CallResponseSink(Protocol):
    """A call's response consumer (``CallSession.on_response``) keyed by Call-ID."""

    async def on_response(self, response: SipResponse) -> None:
        """Deliver a response correlated to one of this call's own requests."""
        ...


class SipOverTlsTransport:
    """An asyncio TLS SIP transport (a ``SipTransport`` and ``CallSignaling``)."""

    def __init__(  # noqa: PLR0913 — connection identity (host/port/SNI/connect-address) plus three optional observer callbacks; all but host/port are keyword-only
        self,
        *,
        host: str,
        port: int,
        ssl_context: object,
        server_hostname: str | None = None,
        connect_address: str | None = None,
        on_new_call: Callable[[NewCall], None] | None = None,
        on_unroutable: Callable[[Unroutable | SipResponse], None] | None = None,
        on_connection_lost: Callable[[BaseException | None], None] | None = None,
    ) -> None:
        """Configure the transport (no IO until :meth:`connect`).

        Args:
            host: The gateway host — the TLS SNI / verification name and the
                default connect address.
            port: The signalling port.
            ssl_context: The client :class:`ssl.SSLContext` (typed ``object`` to
                keep ``ssl`` out of this module's public signature; it is passed
                straight to :func:`asyncio.open_connection`).
            server_hostname: The TLS server name for SNI/verification; defaults
                to ``host``.
            connect_address: The address to dial if it differs from ``host``
                (e.g. an IP under a hostname SNI, as on the loopback test).
            on_new_call: Invoked with each out-of-dialog INVITE the manager maps
                to a registration; the consumer builds the ``CallSession``.
            on_unroutable: Invoked with an unroutable request or a response that
                matches no registration or active call (observed, not swallowed).
            on_connection_lost: Invoked when the reader task ends, with the
                exception that ended it (``None`` on a clean EOF / close).
        """
        self._host = host
        self._port = port
        self._ssl_context = ssl_context
        self._server_hostname = server_hostname if server_hostname is not None else host
        self._connect_address = connect_address if connect_address is not None else host
        self._on_new_call = on_new_call
        self._on_unroutable = on_unroutable
        self._on_connection_lost = on_connection_lost
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._local_sent_by: str | None = None
        self._manager: RegistrationManager | None = None
        self._calls: dict[str, CallResponseSink] = {}
        self._client_txns: dict[tuple[str, int], InviteClientTransaction] = {}
        self._send_lock = asyncio.Lock()

    # --- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Open the TLS connection and start the inbound reader task."""
        self._reader, self._writer = await asyncio.open_connection(
            self._connect_address,
            self._port,
            ssl=self._ssl_context,
            server_hostname=self._server_hostname,
        )
        host, port = self._writer.get_extra_info("sockname")[:2]
        self._local_sent_by = _format_sent_by(host, port)
        self._reader_task = asyncio.create_task(self._read_loop(self._reader))
        self._reader_task.add_done_callback(self._on_reader_done)

    async def aclose(self) -> None:
        """Stop the reader task and close the connection (idempotent).

        The reader reports an operational failure via ``on_connection_lost`` and
        returns cleanly, so the only outcome to drain here is the cancellation we
        request (rule 37: real failures are surfaced, not swallowed in shutdown).
        """
        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._writer is not None:
            self._writer.close()
            # The peer may have already reset the connection; a close error on a
            # gone socket is not actionable during teardown.
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()
            self._writer = None
        self._reader = None

    # --- SipTransport seam --------------------------------------------------

    @property
    def local_sent_by(self) -> str:
        """The Via ``sent-by`` (the local socket's ``host:port``).

        Raises:
            RuntimeError: if accessed before :meth:`connect` (the socket's local
                address is not known until the connection is up).
        """
        if self._local_sent_by is None:
            msg = "local_sent_by is unavailable before connect()"
            raise RuntimeError(msg)
        return self._local_sent_by

    def contact_uri(self, extension: str) -> str:
        """The ``Contact`` header value for ``extension`` on this transport."""
        return f"<sip:{extension}@{self.local_sent_by};transport=tls>"

    async def send(self, message: str) -> None:
        """Send one SIP message; track an outbound INVITE's client transaction.

        Raises:
            RuntimeError: if called before :meth:`connect`.
        """
        if self._writer is None:
            msg = "cannot send before connect()"
            raise RuntimeError(msg)
        self._register_if_invite(message)
        async with self._send_lock:
            self._writer.write(message.encode("utf-8"))
            await self._writer.drain()

    def _register_if_invite(self, message: str) -> None:
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

    def remove_call(self, call_id: str) -> None:
        """Forget a call; drop any tracked client transactions for it."""
        self._calls.pop(call_id, None)
        for key in [k for k in self._client_txns if k[0] == call_id]:
            del self._client_txns[key]

    def bind_manager(self, manager: RegistrationManager) -> None:
        """Bind the registration manager the transport demuxes through."""
        self._manager = manager

    # --- inbound reader + dispatch ------------------------------------------

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Read, frame, and dispatch until EOF or a stream error.

        A framing error (an unparseable Content-Length), an ``OSError`` (the
        socket breaking), or a parse error all end the loop and propagate to the
        task, where :meth:`_on_reader_done` reports the cause via
        ``on_connection_lost`` (rule 37: surfaced, never swallowed). A clean EOF
        ends the loop with no error.
        """
        framer = SipMessageFramer()
        while True:
            data = await reader.read(4096)
            if not data:
                return  # clean EOF
            framer.feed(data)
            for raw in framer:
                await self._dispatch(raw)

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

        A 2xx terminates the client transaction (the TU owns the 2xx ACK), so the
        terminated transaction is unregistered here — a later non-2xx for the same
        branch then finds no transaction and produces no ACK (§17.1.1.2). The
        ``Completed`` non-2xx transaction is kept so a retransmitted non-2xx
        re-emits the same absorbing ACK; it is dropped with the call.
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
        else:
            self._report_unroutable(routing)

    def _report_unroutable(self, what: Unroutable | SipResponse) -> None:
        if self._on_unroutable is not None:
            self._on_unroutable(what)

    def _on_reader_done(self, task: asyncio.Task[None]) -> None:
        """Observe the reader task's end; report a failure (never swallow)."""
        if self._reader_task is task:
            self._reader_task = None
        if task.cancelled():
            return
        error = task.exception()
        if self._on_connection_lost is not None:
            self._on_connection_lost(error)


def _format_sent_by(host: str, port: int) -> str:
    """Format a Via ``sent-by`` from a socket address (IPv6 gets brackets)."""
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


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
