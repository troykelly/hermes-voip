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
  ``handle_request``; otherwise the transport answers an out-of-dialog UA-liveness
  request itself (see below), and only a genuinely unroutable
  :class:`~hermes_voip.manager.Unroutable` reaches ``on_unroutable``.

It implements :class:`~hermes_voip.manager.SipTransport` (``send`` /
``local_sent_by`` / ``contact_uri``) and is each call's
:class:`~hermes_voip.call.CallSignaling` (``send``). It owns the transaction
concern those seams delegate here: when **we** send an INVITE, the transport
tracks its client transaction and **auto-ACKs a non-2xx final** (RFC 3261
§17.1.1.3) — the TU (``CallSession``) only ever ACKs the 2xx.

It also keeps the UA alive: an out-of-dialog ``OPTIONS`` qualify ping is answered
``200 OK`` with an ``Allow`` header (RFC 3261 §11) and an unsolicited ``NOTIFY``
(e.g. ``message-summary`` MWI) is acknowledged ``200 OK``
(:func:`~hermes_voip.keepalive.build_options_ok` /
:func:`~hermes_voip.keepalive.build_keepalive_ok`), so the registrar keeps the
contact *qualified* and rings the extension rather than diverting inbound calls to
voicemail — without one, a registrar marks the endpoint unreachable.

Framing failure vs message-parse failure are distinguished (ADR-0081, rule 37). A
**framing** failure — an unframable stream whose message boundaries can no longer
be delimited (a corrupt ``Content-Length`` / CRLF framing, raised as
:class:`~hermes_voip.transport.framing.FramingError` by the framer) — is
unrecoverable: it ends the reader task and is reported via ``on_connection_lost``.
But once framing **succeeds** and one complete message is extracted, a failure to
*decode or parse* that single message (a ``ValueError`` — including a
``UnicodeDecodeError`` from a body that is not valid UTF-8 — from the UTF-8 decode or
:meth:`~hermes_voip.message.SipRequest.parse`) is **not** fatal: the stream is
still synchronised, so the one bad message is logged loudly (a WARNING with a
non-PII summary — the repo is PUBLIC, rule 34) and **skipped**, keeping the
connection and every OTHER active call on it alive. This is surfaced, not swallowed
(rule 37) — one malformed message is not a DoS against unrelated calls. A stray
response or unroutable request is surfaced via ``on_unroutable`` (a normal network
event — it does not tear the connection down).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hermes_voip.keepalive import build_keepalive_ok, build_options_ok
from hermes_voip.manager import (
    Cancel,
    InDialog,
    NewCall,
    RegistrationManager,
    Unroutable,
)
from hermes_voip.message import (
    SipRequest,
    SipResponse,
    build_request,
    build_response,
    new_branch,
    new_tag,
)
from hermes_voip.transport.framing import SipMessageFramer
from hermes_voip.transport.transaction import (
    InviteClientTransaction,
    InviteServerTransaction,
    TransactionState,
)

__all__ = ["CallResponseSink", "SipOverTlsTransport"]

_log = logging.getLogger(__name__)

_RESPONSE_PREFIX = "SIP/2.0 "
# A To/From header parameter carrying a dialog tag (after the name-addr's '>').
_TO_TAG_PARAM = re.compile(r";\s*tag=", re.IGNORECASE)
# The Via ``branch`` parameter (RFC 3261 §8.1.1.7) that identifies a transaction.
_VIA_BRANCH = re.compile(r";\s*branch=([^;,\s]+)", re.IGNORECASE)
_FINAL_STATUS = 200  # status >= 200 is a final response (terminates the txn)
_FIRST_FAILURE = 300  # status >= 300 is a non-2xx final
_MAX_FORWARDS = "70"
# The addr-spec inside a name-addr ``<...>`` (a Contact/Record-Route URI).
_ANGLE_ADDR = re.compile(r"<([^>]*)>")


@dataclass(slots=True)
class _PendingInvite:
    """An inbound INVITE server transaction awaiting a final response.

    Tracked from the moment the INVITE is handed to ``on_new_call`` until we send
    its final response (or a CANCEL terminates it), so a CANCEL can be matched to
    it (RFC 3261 §9.2) and a 200 OK racing a CANCEL is suppressed. ``local_tag``
    is the To-tag for any response *we* generate for this transaction (the
    CANCEL-driven 487), stable so a retransmitted 487 reuses it (§8.2.6.2).
    """

    invite: SipRequest
    call_id: str
    txn: InviteServerTransaction
    local_tag: str
    cancelled: bool = False


@dataclass(slots=True)
class _OutboundInvite:
    """The fields of an INVITE **we** sent that a §9.1 CANCEL must echo.

    Captured alongside the :class:`InviteClientTransaction` when we originate an
    INVITE, keyed by ``(Call-ID, CSeq-num)`` (same key as ``_client_txns``). A
    CANCEL reuses the INVITE's Request-URI, top ``Via`` (same branch), ``From``,
    ``To`` (no tag added), ``Call-ID``, the CSeq number with method ``CANCEL``, and
    repeats the ``Route`` set (RFC 3261 §9.1). These same fields also build the
    ACK+BYE for a 2xx that races the CANCEL (the glare, §9.1).
    """

    request_uri: str
    via: str
    from_header: str
    to_header: str
    call_id: str
    cseq_number: int
    routes: tuple[str, ...]


@runtime_checkable
class CallResponseSink(Protocol):
    """A call's response consumer (``CallSession.on_response``) keyed by Call-ID."""

    async def on_response(self, response: SipResponse) -> None:
        """Deliver a response correlated to one of this call's own requests."""
        ...


class SipOverTlsTransport:
    """An asyncio TLS SIP transport (a ``SipTransport`` and ``CallSignaling``)."""

    def __init__(  # noqa: PLR0913 — connection identity (host/port/SNI/connect-address) plus observer callbacks and keepalive knob; all but host/port are keyword-only
        self,
        *,
        host: str,
        port: int,
        ssl_context: object,
        server_hostname: str | None = None,
        connect_address: str | None = None,
        keepalive_interval: float = 30.0,
        on_new_call: Callable[[NewCall], None] | None = None,
        on_cancel: Callable[[str], None] | None = None,
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
            keepalive_interval: Seconds between RFC 5626 §5.4 double-CRLF
                keepalive pings.  Zero or negative disables pings entirely.
                Default 30 s matches the common NAT binding lifetime.
            on_new_call: Invoked with each out-of-dialog INVITE the manager maps
                to a registration; the consumer builds the ``CallSession``.
            on_cancel: Invoked with the Call-ID of a pending INVITE the peer has
                CANCELled (RFC 3261 §9.2), so the consumer aborts that call's
                half-built setup (the transport has already 487'd the INVITE).
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
        self._keepalive_interval = keepalive_interval
        self._on_new_call = on_new_call
        self._on_cancel = on_cancel
        self._on_unroutable = on_unroutable
        self._on_connection_lost = on_connection_lost
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._local_sent_by: str | None = None
        self._manager: RegistrationManager | None = None
        self._calls: dict[str, CallResponseSink] = {}
        self._client_txns: dict[tuple[str, int], InviteClientTransaction] = {}
        # Outbound INVITEs WE sent, keyed by (Call-ID, CSeq-num) — the fields a §9.1
        # CANCEL (and a glare 2xx's ACK+BYE) must echo (RFC 3261 §9.1). The re-auth
        # INVITE (CSeq 2) is a separate key, so send_cancel targets the latest.
        self._outbound_invites: dict[tuple[str, int], _OutboundInvite] = {}
        # Outbound Call-IDs we have CANCELled: a 2xx racing the CANCEL for one of
        # these is suppressed (not routed to the sink) and the remote dialog ACK+BYE'd.
        self._cancelled_outbound: set[str] = set()
        # Outbound Call-IDs whose glare 2xx we have already BYE'd. The UAS retransmits
        # its 2xx until the ACK arrives (RFC 3261 §13.3.1.4), so every retransmit is
        # ACKed again, but the in-dialog BYE is sent ONCE — a second BYE on an
        # already-closed dialog is spurious. Cleared with the call in remove_call.
        self._glare_byed: set[str] = set()
        # Inbound INVITE server transactions awaiting a final response, keyed by
        # Via branch (RFC 3261 §9.2 CANCEL matching).
        self._pending_invites: dict[str, _PendingInvite] = {}
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
        if self._keepalive_interval > 0:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def aclose(self) -> None:
        """Stop the reader task and close the connection (idempotent).

        The reader reports an operational failure via ``on_connection_lost`` and
        returns cleanly, so the only outcome to drain here is the cancellation we
        request (rule 37: real failures are surfaced, not swallowed in shutdown).
        """
        keepalive = self._keepalive_task
        self._keepalive_task = None
        if keepalive is not None:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
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

    async def _keepalive_loop(self) -> None:
        """Send RFC 5626 §5.4 double-CRLF keepalive pings at the configured interval.

        Runs as a background task from :meth:`connect`; cancelled by
        :meth:`aclose`.  On write failure the writer is closed and
        ``on_connection_lost`` is fired directly (the reader may not see EOF on
        a clean close of a fake or already-dead socket, so we surface the event
        here rather than waiting for the reader task to discover it).
        """
        while True:
            await asyncio.sleep(self._keepalive_interval)
            writer = self._writer
            if writer is None:
                return
            try:
                async with self._send_lock:
                    writer.write(b"\r\n\r\n")
                    await writer.drain()
            except (OSError, ConnectionError) as exc:
                # Close the writer so no further sends are attempted.
                if self._writer is not None:
                    self._writer.close()
                    self._writer = None
                # Surface the loss via on_connection_lost (the reader task may
                # not detect a write-side failure on its own).
                if self._on_connection_lost is not None:
                    self._on_connection_lost(exc)
                return

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
        """Send one SIP message; track transactions and honour CANCEL (§9.2).

        An outbound INVITE we originate is tracked as a client transaction. An
        outbound INVITE *response* updates the matching inbound server
        transaction: a final clears its pending entry, and a 200 OK for an INVITE
        the peer has already CANCELled is **suppressed** (the caller is gone, so
        answering would strand a ghost call — RFC 3261 §9.2 / §15).

        Raises:
            RuntimeError: if called before :meth:`connect`.
        """
        if self._writer is None:
            msg = "cannot send before connect()"
            raise RuntimeError(msg)
        is_response = message.startswith(_RESPONSE_PREFIX)
        if not is_response:
            self._register_if_invite(message)
        # The CANCEL-suppression decision and the wire write happen under the same
        # lock so a 200 OK racing an inbound CANCEL cannot pass the cancelled check
        # and then write after _handle_cancel marks the INVITE cancelled: the
        # re-check below sees the flag set under this lock (codex finding).
        async with self._send_lock:
            if is_response and self._suppress_or_track_response(message):
                return  # a 200 OK to a CANCELled INVITE — dropped, never sent
            self._writer.write(message.encode("utf-8"))
            await self._writer.drain()

    def _register_if_invite(self, message: str) -> None:
        request = SipRequest.parse(message)
        if request.method != "INVITE":
            return
        key = _txn_key(request.header("Call-ID"), request.header("CSeq"))
        if key is not None:
            self._client_txns[key] = InviteClientTransaction(message)
            self._track_outbound_invite(request, key)

    def _track_outbound_invite(self, invite: SipRequest, key: tuple[str, int]) -> None:
        """Record the §9.1 CANCEL-relevant fields of an INVITE we sent.

        A malformed INVITE missing a header a CANCEL must echo (Via/From/To/
        Call-ID/CSeq) is left untracked — it just cannot be CANCELled (the call
        would never establish either). The Route set is repeated verbatim.
        """
        via = invite.header("Via")
        from_header = invite.header("From")
        to_header = invite.header("To")
        call_id = invite.header("Call-ID")
        if via is None or from_header is None or to_header is None or call_id is None:
            return
        self._outbound_invites[key] = _OutboundInvite(
            request_uri=invite.request_uri,
            via=via,
            from_header=from_header,
            to_header=to_header,
            call_id=call_id,
            cseq_number=key[1],
            routes=invite.headers_all("Route"),
        )

    def _suppress_or_track_response(self, message: str) -> bool:
        """Handle an outbound INVITE response vs a pending CANCEL; return suppress.

        For a final response to an inbound INVITE we track (matched by Via branch,
        CSeq method ``INVITE``): a 200 OK for a transaction the peer CANCELled is
        suppressed (returns ``True``); otherwise the final is recorded on the
        server transaction and a non-CANCELled transaction's pending entry is
        cleared (it is complete). Responses to other methods (e.g. the 200 OK to
        the CANCEL itself, whose CSeq method is ``CANCEL``) are left untouched.
        """
        response = SipResponse.parse(message)
        match = self._pending_for_response(response)
        if match is None:
            return False
        branch, pending = match
        status = response.status_code
        if status < _FINAL_STATUS:
            return False  # a provisional (1xx) keeps the transaction pending
        if pending.cancelled and status < _FIRST_FAILURE:
            # A 200 OK racing the CANCEL: the call is dead — drop it. The entry is
            # KEPT (cleared in remove_call on teardown) so a retransmitted 2xx is
            # also suppressed.
            _log.warning(
                "INVITE %s: suppressing a 200 OK for a CANCELled call",
                pending.call_id,
            )
            return True
        # A final response completes the server transaction. A CANCELled entry is
        # KEPT (its 487 may be followed by a late 2xx to suppress); a normal
        # answer/reject clears it.
        pending.txn.on_final_sent(status)
        if not pending.cancelled:
            del self._pending_invites[branch]
        return False

    def _pending_for_response(
        self, response: SipResponse
    ) -> tuple[str, _PendingInvite] | None:
        """The (branch, pending INVITE) an outbound INVITE response answers, if any."""
        if _method_of(response.header("CSeq")) != "INVITE":
            return None
        branch = _via_branch(response.header("Via"))
        if branch is None:
            return None
        pending = self._pending_invites.get(branch)
        return (branch, pending) if pending is not None else None

    # --- outbound CANCEL (RFC 3261 §9.1) -------------------------------------

    async def send_cancel(self, call_id: str) -> bool:
        """CANCEL the latest in-flight INVITE **we** sent for ``call_id`` (§9.1).

        Builds and sends a CANCEL that echoes that INVITE's Request-URI, top
        ``Via`` (same branch), ``From``, ``To`` (no tag added), ``Call-ID`` and the
        CSeq number with method ``CANCEL``, repeating the ``Route`` set; the body is
        empty. The call is then recorded as cancelled, so a 2xx racing the CANCEL is
        suppressed and its dialog torn down (:meth:`_handle_glare_2xx`).

        When more than one INVITE is tracked for the Call-ID (a re-auth re-send), the
        one with the **highest CSeq** is targeted — it is the transaction still
        awaiting a final response.

        Returns ``True`` when an INVITE was tracked and a CANCEL was sent; ``False``
        when there is nothing to cancel (no in-flight INVITE for ``call_id`` — never
        sent, or its final response already arrived and cleared the entry). §9.1
        forbids CANCELling a transaction that has had its final response, so a
        ``False`` here means the abort is a no-op at the wire.

        Raises:
            RuntimeError: if called before :meth:`connect` (``send`` enforces this).
        """
        outbound = self._latest_outbound_invite(call_id)
        if outbound is None:
            return False
        # Mark cancelled BEFORE sending so a 2xx that arrives the instant the CANCEL
        # hits the wire is already classified for suppression (the read loop and this
        # send are on the same task, so there is no true concurrency, but the flag is
        # set first for clarity and to mirror the inbound §9.2 ordering).
        self._cancelled_outbound.add(call_id)
        await self.send(_build_cancel(outbound))
        _log.info("INVITE %s: sent CANCEL (RFC 3261 §9.1)", call_id)
        return True

    def _latest_outbound_invite(self, call_id: str) -> _OutboundInvite | None:
        """The tracked outbound INVITE with the highest CSeq for ``call_id``."""
        candidates = [
            inv for inv in self._outbound_invites.values() if inv.call_id == call_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda inv: inv.cseq_number)

    async def _handle_glare_2xx(self, response: SipResponse) -> bool:
        """Suppress + tear down a 2xx that races a CANCEL we sent (§9.1 glare).

        Returns ``True`` (the 2xx is consumed here, never routed to the sink) when
        the response is a 2xx to an INVITE for a Call-ID we CANCELled. A 2xx
        established the dialog on the callee, so we ACK it and then send an in-dialog
        BYE to close it — never leaving the remote stranded. This mirrors the inbound
        §9.2 late-200 suppression. Returns ``False`` for any other response (the
        normal routing path then applies).
        """
        if _method_of(response.header("CSeq")) != "INVITE":
            return False
        status = response.status_code
        if not _FINAL_STATUS <= status < _FIRST_FAILURE:
            return False  # provisional or non-2xx — not a racing answer
        call_id = response.header("Call-ID")
        if call_id is None or call_id not in self._cancelled_outbound:
            return False
        outbound = self._latest_outbound_invite(call_id)
        if outbound is None:
            return True  # cancelled, nothing tracked to ACK/BYE — still suppress
        _log.warning(
            "INVITE %s: 2xx raced our CANCEL — ACK+BYE to close the remote dialog",
            call_id,
        )
        await self._ack_and_bye_glare(outbound, response)
        return True

    async def _ack_and_bye_glare(
        self, outbound: _OutboundInvite, response: SipResponse
    ) -> None:
        """ACK a 2xx that raced our CANCEL, and BYE its dialog once (best-effort).

        Builds the ACK and in-dialog BYE from the tracked INVITE plus the 2xx's
        ``To`` (with its tag), ``Contact`` (the in-dialog request target) and
        reversed ``Record-Route`` route set (RFC 3261 §13.2.2.4 / §12.2). The UAS
        retransmits its 2xx until it sees the ACK (RFC 3261 §13.3.1.4), so EVERY
        retransmission is ACKed again — but the in-dialog BYE is sent only ONCE per
        Call-ID (tracked in ``_glare_byed``); a second BYE on the already-closed
        dialog is spurious. A send/parse failure is logged structurally (never the
        body — rule 34) and the callee's own session timer reaps the dialog if the BYE
        cannot be built.
        """
        try:
            to_header = response.header("To") or outbound.to_header
            remote_target = (
                _addr_spec(response.header("Contact")) or outbound.request_uri
            )
            route_set = tuple(reversed(response.headers_all("Record-Route")))
            transport_token = _via_transport(outbound.via)
            ack = _build_glare_ack(
                outbound, to_header, remote_target, route_set, transport_token
            )
            await self.send(ack)
            # BYE exactly once: the first glare 2xx closes the dialog; later 2xx
            # retransmits are ACKed (above) but not re-BYE'd.
            if outbound.call_id in self._glare_byed:
                return
            self._glare_byed.add(outbound.call_id)
            bye = _build_glare_bye(
                outbound, to_header, remote_target, route_set, transport_token
            )
            await self.send(bye)
        except (ValueError, RuntimeError) as exc:
            _log.warning(
                "INVITE %s: could not ACK/BYE the glare 2xx (%s) — "
                "the remote session timer will reap it",
                outbound.call_id,
                type(exc).__name__,
            )

    # --- call registry (response routing) -----------------------------------

    def add_call(self, call_id: str, sink: CallResponseSink) -> None:
        """Register a call's response sink so its responses route to it by Call-ID."""
        self._calls[call_id] = sink

    def remove_call(self, call_id: str, sink: CallResponseSink | None = None) -> None:
        """Forget a call; drop any tracked client/server transactions for it.

        When ``sink`` is given the registration is only removed if it is still
        that exact sink. Overlapping INVITEs can share a Call-ID (retransmission
        or fork); a later call's :meth:`add_call` overwrites the entry, so an
        earlier call's teardown must not evict the live later one — pass the
        earlier call's own sink and the identity check makes the removal a no-op
        when it no longer owns the entry. With ``sink=None`` the removal is
        unconditional (used where there is provably one call per Call-ID).

        Any retained pending-INVITE server transaction for this Call-ID (a
        CANCELled call kept to keep suppressing a late 2xx) is also cleared here —
        the call is definitively gone once its teardown runs.
        """
        # The outbound-INVITE CANCEL-tracking + cancelled flag are keyed by Call-ID
        # and are NOT sink-identity-sensitive: each outbound place_call mints a unique
        # Call-ID, so there is no later overlapping independent outbound call to
        # protect. Clear them UNCONDITIONALLY — before the sink-identity early-return —
        # so a sink-mismatch remove_call (an earlier same-Call-ID sink owner tearing
        # down) still clears the tracking instead of leaking it (the §9.1 records would
        # otherwise outlive the call and keep a stale Call-ID in _cancelled_outbound).
        for okey in [k for k in self._outbound_invites if k[0] == call_id]:
            del self._outbound_invites[okey]
        self._cancelled_outbound.discard(call_id)
        self._glare_byed.discard(call_id)
        # The sink/inbound state IS sink-identity-sensitive: a later overlapping
        # same-Call-ID INVITE may have overwritten _calls, so an earlier call's
        # teardown (passing its own sink) must not evict the live later one.
        if sink is not None and self._calls.get(call_id) is not sink:
            return
        self._calls.pop(call_id, None)
        for key in [k for k in self._client_txns if k[0] == call_id]:
            del self._client_txns[key]
        for branch in [
            b for b, p in self._pending_invites.items() if p.call_id == call_id
        ]:
            del self._pending_invites[branch]

    def bind_manager(self, manager: RegistrationManager) -> None:
        """Bind the registration manager the transport demuxes through."""
        self._manager = manager

    # --- inbound reader + dispatch ------------------------------------------

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Read, frame, and dispatch until EOF or an unrecoverable stream error.

        A **framing** error (a corrupt ``Content-Length`` — the stream can no
        longer be delimited, raised as ``FramingError`` from the framer iteration)
        or an ``OSError`` (the socket breaking) ends the loop and propagates to the
        task, where :meth:`_on_reader_done` reports the cause via
        ``on_connection_lost`` (rule 37: surfaced, never swallowed). A failure to
        *decode or parse* a single, successfully-framed message (the framer yields
        raw bytes; the UTF-8 decode and parse both run in :meth:`_dispatch`) is
        handled there (logged and skipped — ADR-0081), NOT here, so it does not end
        the loop. A clean EOF ends the loop with no error.
        """
        framer = SipMessageFramer()
        while True:
            data = await reader.read(4096)
            if not data:
                return  # clean EOF
            framer.feed(data)
            for raw in framer:
                await self._dispatch(raw)

    async def _dispatch(self, raw: bytes) -> None:
        """Decode, parse, and route one already-framed message; skip if it won't parse.

        ``raw`` is a single complete message the framer has already delimited, as raw
        bytes. Turning it into a parsed message here can raise a :class:`ValueError` —
        a :class:`UnicodeDecodeError` (a subclass of ``ValueError``) from a body that
        is not valid UTF-8, or a parse ``ValueError`` from
        :meth:`~hermes_voip.message.SipResponse.parse` /
        :meth:`~hermes_voip.message.SipRequest.parse`. Either is a *post-framing*
        failure of THIS one message — the stream is still synchronised — so it is
        logged loudly and skipped (ADR-0081), keeping the connection and every other
        active call alive: one malformed message must not be a DoS against unrelated
        calls. The UTF-8 decode runs HERE, inside the guard (not in the framer),
        precisely so a non-UTF-8 body is recoverable rather than fatal. A
        ``FramingError`` (an unframable stream) cannot reach this method — it is raised
        by the framer in :meth:`_read_loop` and propagates there (rule 37).
        """
        try:
            text = raw.decode("utf-8")
            message: SipResponse | SipRequest = (
                SipResponse.parse(text)
                if text.startswith(_RESPONSE_PREFIX)
                else SipRequest.parse(text)
            )
        except ValueError as exc:
            # A non-PII summary only — never the raw message (it may carry From/To/
            # Call-ID/SDP; the repo is PUBLIC, rule 34). type(exc).__name__ + length
            # is loud enough to alert without leaking wire content. UnicodeDecodeError
            # subclasses ValueError, so a non-UTF-8 body is caught here too.
            _log.warning(
                "dropping an unparseable SIP message (%s, len=%d) — connection kept",
                type(exc).__name__,
                len(raw),
            )
            return
        if isinstance(message, SipResponse):
            await self._dispatch_response(message)
        else:
            await self._dispatch_request(message)

    async def _dispatch_response(self, response: SipResponse) -> None:
        # A 2xx racing a CANCEL we sent (RFC 3261 §9.1 glare) is consumed here: the
        # remote dialog is ACK+BYE'd and the 2xx is NOT routed to the sink (the call
        # is cancelled, not answered). Checked before the auto-ACK / sink routing so a
        # suppressed answer never reaches place_call as success.
        if await self._handle_glare_2xx(response):
            return
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

        A response whose CSeq names a method other than ``INVITE`` (e.g. the 200
        OK to a BYE/REGISTER/OPTIONS) is the ordinary, high-volume case and is
        skipped without comment. A CSeq that cannot be parsed AT ALL — missing
        entirely, or present with no method token — is a *different*, malformed
        condition: :func:`_method_of` returns ``None`` for both, so it is
        distinguished here and logged loudly (never silently), because it means
        this layer gives up on the mandatory auto-ACK with no other signal.
        """
        cseq = response.header("CSeq")
        method = _method_of(cseq)
        if method != "INVITE":
            if method is None:
                _log.warning(
                    "INVITE-final response with an unparseable CSeq (Call-ID"
                    " %s) — cannot auto-ACK or clean up the transaction",
                    response.header("Call-ID"),
                )
            return
        key = _txn_key(response.header("Call-ID"), cseq)
        if key is None:
            _log.warning(
                "INVITE-final response with an unparseable CSeq (Call-ID %s) —"
                " cannot auto-ACK or clean up the transaction",
                response.header("Call-ID"),
            )
            return
        txn = self._client_txns.get(key)
        if txn is None:
            return
        # Building the §17.1.1.3 ACK copies the response's To (and echoes other
        # headers) into a fresh request, so a correlated non-2xx final that is missing
        # To — or carries a control char in an echoed header — makes the builder raise
        # ValueError; unguarded that escapes the reader and tears down the whole
        # connection (ADR-0081). Catch ONLY the builder's ValueError and drop the
        # auto-ACK: no ACK was built, so the transaction is untouched and is cleaned up
        # with the call. The send stays OUTSIDE the try so a genuine OSError from real
        # IO loss still propagates (rule 37).
        try:
            ack = txn.ack_for_response(response)
        except ValueError as exc:
            _log.warning(
                "dropping the auto-ACK for a non-2xx INVITE final that cannot build"
                " an ACK (%s) — connection kept",
                type(exc).__name__,
            )
            return
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
                self._track_pending_invite(routing.invite)
                self._on_new_call(routing)
            else:
                self._report_unroutable(
                    Unroutable(request, "inbound call with no on_new_call handler")
                )
        elif isinstance(routing, InDialog):
            await routing.consumer.handle_request(routing.request)
        elif isinstance(routing, Cancel):
            await self._handle_cancel(routing.request)
        elif await self._answer_keepalive(request):
            return
        else:
            self._report_unroutable(routing)

    def _track_pending_invite(self, invite: SipRequest) -> None:
        """Record an inbound INVITE server transaction for CANCEL matching.

        Keyed by the top Via branch (the CANCEL shares it, RFC 3261 §9.1). A
        malformed INVITE missing the branch or Call-ID is still dispatched; it
        just cannot be matched by a later CANCEL.
        """
        branch = _via_branch(invite.header("Via"))
        call_id = invite.header("Call-ID")
        if branch is None or call_id is None:
            return
        self._pending_invites[branch] = _PendingInvite(
            invite=invite,
            call_id=call_id,
            txn=InviteServerTransaction(),
            local_tag=new_tag(),
        )

    def _build_or_drop(self, build: Callable[[], str], *, kind: str) -> str | None:
        """Build one auto-response, or drop the request if it cannot be built.

        ``build_response`` (and the keepalive builders) raise ``ValueError`` when the
        inbound request lacks a mandatory header to echo — Via / From / To / Call-ID /
        CSeq (RFC 3261 §8.2.6). It PARSES but cannot be answered (no reliable Via /
        Call-ID / CSeq to route a response back). Building runs INLINE in the reader
        (from :meth:`_dispatch_request`, awaited OUTSIDE :meth:`_dispatch`'s parse-only
        ``try``), so an escaping ``ValueError`` would end the reader and fire
        ``on_connection_lost`` — dropping every other active call on the connection (the
        ADR-0081 DoS, response-build side). Fail closed: log a non-PII WARNING (the
        exception type only, never the wire content — rule 34) and return ``None`` so
        the caller skips it, keeping the connection and the other calls alive.
        """
        try:
            return build()
        except ValueError as exc:
            _log.warning(
                "dropping a header-incomplete inbound %s we cannot answer (%s) —"
                " connection kept",
                kind,
                type(exc).__name__,
            )
            return None

    async def _answer_or_drop(self, build: Callable[[], str], *, kind: str) -> None:
        """Build via :meth:`_build_or_drop` and send the response if it built."""
        response = self._build_or_drop(build, kind=kind)
        if response is not None:
            await self.send(response)

    async def _handle_cancel(self, cancel: SipRequest) -> None:
        """Answer an inbound CANCEL and terminate the matching INVITE (RFC 3261 §9.2).

        A CANCEL shares the INVITE's top Via branch and (defensively) its Call-ID.
        When it matches a pending INVITE: the CANCEL is answered ``200 OK``, the
        INVITE is replied ``487 Request Terminated``, the entry is marked cancelled
        (so a 200 OK racing the answer task is suppressed in :meth:`send`), and the
        ``on_cancel`` hook fires once so the consumer tears down the half-built
        call. A **retransmitted** CANCEL (the entry is already cancelled) is
        absorbed — only the ``200 OK`` to the CANCEL is re-sent, with no second
        487 or ``on_cancel`` (idempotent §9.2 handling). A CANCEL matching no
        transaction is answered ``481 Call/Transaction Does Not Exist``.

        Each response is built via :meth:`_build_or_drop` / :meth:`_answer_or_drop`: a
        request missing a mandatory header to echo cannot be answered and is dropped
        fail-closed rather than escaping ``ValueError`` into the reader (ADR-0081). A
        matched CANCEL whose ``200 OK`` cannot be built is un-answerable and is dropped
        WHOLE — it does not half-drive the transaction (no cancelled flag, no 487, no
        ``on_cancel``); the pending INVITE setup is left intact.
        """
        pending = self._match_cancel(cancel)
        if pending is None:
            await self._answer_or_drop(
                lambda: build_response(cancel, 481, "Call/Transaction Does Not Exist"),
                kind="CANCEL",
            )
            return
        # 200 OK to the CANCEL, built BEFORE any transaction state is mutated. A
        # header-incomplete CANCEL whose 200 cannot be built is un-answerable and
        # dropped WHOLE — no cancelled flag, no 487, no on_cancel — so a malformed
        # CANCEL cannot cancel a live INVITE. RFC 3261 §9.2: the 200-to-CANCEL and the
        # 487-to-INVITE share the pending invite's stable local_tag (never a fresh tag),
        # keeping a retransmitted CANCEL's 200 idempotent.
        ok = self._build_or_drop(
            lambda: build_response(cancel, 200, "OK", to_tag=pending.local_tag),
            kind="CANCEL",
        )
        if ok is None:
            return
        already_cancelled = pending.cancelled
        # Mark cancelled BEFORE the send-await, so a 200-to-INVITE racing on the answer
        # task is suppressed in :meth:`send`; the 200 is re-sent (retransmit-safe).
        pending.cancelled = True
        await self.send(ok)
        if already_cancelled:
            return  # the 487 + abort already happened on the first CANCEL
        # 487 the INVITE: a dialog-forming final carrying our stable local tag.
        # This records the final on the server transaction; the cancelled entry is
        # retained (cleared in remove_call) so a late 2xx stays suppressed.
        await self._answer_or_drop(
            lambda: build_response(
                pending.invite, 487, "Request Terminated", to_tag=pending.local_tag
            ),
            kind="INVITE",
        )
        _log.info(
            "INVITE %s CANCELled by peer — 487 Request Terminated, aborting setup",
            pending.call_id,
        )
        if self._on_cancel is not None:
            self._on_cancel(pending.call_id)

    def _match_cancel(self, cancel: SipRequest) -> _PendingInvite | None:
        """The pending INVITE a CANCEL targets (top Via branch + Call-ID), if any.

        RFC 3261 §9.2/§17.2.3: the branch identifies the transaction; the Call-ID
        is checked too as a cheap defence so a stray CANCEL that happens to reuse a
        branch cannot terminate the wrong INVITE.
        """
        branch = _via_branch(cancel.header("Via"))
        if branch is None:
            return None
        pending = self._pending_invites.get(branch)
        if pending is None:
            return None
        if cancel.header("Call-ID") != pending.invite.header("Call-ID"):
            return None
        return pending

    async def _answer_keepalive(self, request: SipRequest) -> bool:
        """Answer an out-of-dialog ``OPTIONS``/``NOTIFY`` 200 OK; report success.

        The gateway *qualifies* the registered contact with out-of-dialog
        ``OPTIONS`` pings and expects a ``200 OK``; without one it marks the
        endpoint UNREACHABLE and sends inbound calls to voicemail (RFC 3261 §11).
        It also sends unsolicited ``message-summary`` ``NOTIFY`` (MWI) that we
        acknowledge but do not process. This runs only on the
        :class:`~hermes_voip.manager.Unroutable` branch, so an in-dialog request
        (it carries a ``To``-tag, classified before us) never reaches here — and
        an out-of-dialog request that *does* carry a ``To``-tag is left to the
        unroutable path, never auto-answered.

        Returns ``True`` when this is a keepalive request we handled — either answered,
        or (if it is header-incomplete and cannot be answered) dropped fail-closed via
        :meth:`_answer_or_drop` so its builder ``ValueError`` never reaches the reader.
        The caller must not also report a handled request unroutable. Returns ``False``
        when this is not a keepalive request.

        Note: this assumes a *qualify* ``OPTIONS`` is out of dialog (no ``To``-tag),
        which holds for RFC 3261 §11 registrars; a tagged ``OPTIONS`` is treated as
        in-dialog and left to the unroutable path (answering it could 200 a genuine
        in-dialog request for an unknown dialog, which should be ``481``).
        """
        if _has_to_tag(request.header("To")):
            return False
        if request.method == "OPTIONS":
            await self._answer_or_drop(
                lambda: build_options_ok(request, to_tag=new_tag()), kind="OPTIONS"
            )
            return True
        if request.method == "NOTIFY":
            await self._answer_or_drop(
                lambda: build_keepalive_ok(request, to_tag=new_tag()), kind="NOTIFY"
            )
            return True
        return False

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


def _has_to_tag(to_value: str | None) -> bool:
    """``True`` when a ``To`` header carries a dialog ``tag`` parameter.

    The tag is a header parameter, so it lives after the closing ``>`` of a
    name-addr (a ``tag=`` inside the URI's user/host part would not be a dialog
    tag). A request with a ``To``-tag is in-dialog and is never auto-answered as a
    keepalive — it is left to the unroutable path.
    """
    if to_value is None:
        return False
    search_space = to_value.split(">", 1)[1] if ">" in to_value else to_value
    return _TO_TAG_PARAM.search(search_space) is not None


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


def _via_branch(via: str | None) -> str | None:
    """The top Via's ``branch`` token (the transaction id, RFC 3261 §8.1.1.7)."""
    if via is None:
        return None
    # ``headers_all('Via')`` may join multiple Vias with a comma; the transaction
    # is identified by the TOP Via, so match the first ``branch=`` occurrence.
    match = _VIA_BRANCH.search(via)
    return match.group(1) if match is not None else None


def _txn_key(call_id: str | None, cseq: str | None) -> tuple[str, int] | None:
    if call_id is None or cseq is None:
        return None
    parts = cseq.split()
    # Guard int() with ``isascii() and isdecimal()`` (the house pattern — see
    # framing.py / sdp.py / registration.py), NOT ``isdigit()``: ``str.isdigit()`` is
    # True for non-decimal digit characters such as the superscript "²" that ``int()``
    # cannot parse, so an inbound ``CSeq: ² INVITE`` would raise ValueError here and
    # escape the reader, tearing down the whole connection (ADR-0081). A non-decimal
    # CSeq number now yields None (no transaction match), so the caller drops the
    # auto-ACK rather than crashing.
    if not parts or not (parts[0].isascii() and parts[0].isdecimal()):
        return None
    return (call_id, int(parts[0]))


def _build_cancel(outbound: _OutboundInvite) -> str:
    """Build the §9.1 CANCEL for a tracked outbound INVITE.

    RFC 3261 §9.1: the CANCEL's Request-URI, ``Call-ID``, ``From`` (with tag), the
    single topmost ``Via`` (**same branch** as the INVITE) and the CSeq number all
    match the INVITE; the method is ``CANCEL``; the ``To`` is the INVITE's, with **no
    tag added** (a CANCEL is sent before the dialog-establishing 2xx); the ``Route``
    set is repeated; the body is empty.
    """
    headers: list[tuple[str, str]] = [
        ("Via", outbound.via),
        ("Max-Forwards", _MAX_FORWARDS),
    ]
    headers += [("Route", route) for route in outbound.routes]
    headers += [
        ("From", outbound.from_header),
        ("To", outbound.to_header),
        ("Call-ID", outbound.call_id),
        ("CSeq", f"{outbound.cseq_number} CANCEL"),
    ]
    return build_request("CANCEL", outbound.request_uri, headers)


def _build_glare_ack(
    outbound: _OutboundInvite,
    to_header: str,
    remote_target: str,
    route_set: tuple[str, ...],
    transport_token: str,
) -> str:
    """The ACK for a 2xx that raced our CANCEL (RFC 3261 §13.2.2.4, fresh branch)."""
    sent_by = _sent_by(outbound.via)
    headers: list[tuple[str, str]] = [
        ("Via", f"SIP/2.0/{transport_token} {sent_by};branch={new_branch()};rport"),
        ("Max-Forwards", _MAX_FORWARDS),
    ]
    headers += [("Route", route) for route in route_set]
    headers += [
        ("From", outbound.from_header),
        ("To", to_header),
        ("Call-ID", outbound.call_id),
        ("CSeq", f"{outbound.cseq_number} ACK"),
    ]
    return build_request("ACK", remote_target, headers)


def _build_glare_bye(
    outbound: _OutboundInvite,
    to_header: str,
    remote_target: str,
    route_set: tuple[str, ...],
    transport_token: str,
) -> str:
    """The in-dialog BYE that closes the dialog a glare 2xx established (§15)."""
    sent_by = _sent_by(outbound.via)
    headers: list[tuple[str, str]] = [
        ("Via", f"SIP/2.0/{transport_token} {sent_by};branch={new_branch()};rport"),
        ("Max-Forwards", _MAX_FORWARDS),
    ]
    headers += [("Route", route) for route in route_set]
    headers += [
        ("From", outbound.from_header),
        ("To", to_header),
        ("Call-ID", outbound.call_id),
        ("CSeq", f"{outbound.cseq_number + 1} BYE"),
    ]
    return build_request("BYE", remote_target, headers)


def _addr_spec(header_value: str | None) -> str | None:
    """The addr-spec from a name-addr/addr-spec header value (Contact), or None.

    Inside ``<...>`` if present (the in-dialog request target), else up to the first
    ``;`` (header params). An empty/absent value yields ``None`` (the caller falls
    back to the INVITE Request-URI).
    """
    if header_value is None:
        return None
    match = _ANGLE_ADDR.search(header_value)
    spec = (
        match.group(1).strip()
        if match is not None
        else header_value.split(";", 1)[0].strip()
    )
    return spec or None


def _via_transport(via: str) -> str:
    """The transport token of a Via value (``TLS``/``WSS``); defaults to ``TLS``."""
    match = re.match(r"\s*SIP/2\.0/(\S+)", via)
    return match.group(1) if match is not None else "TLS"


def _sent_by(via: str) -> str:
    """The ``sent-by`` (host:port) of the topmost Via value."""
    topmost = via.split(",", 1)[0].strip()
    match = re.match(r"\s*SIP/2\.0/\S+\s+([^\s;]+)", topmost)
    if match is None:
        msg = f"malformed Via, no sent-by: {via!r}"
        raise ValueError(msg)
    return match.group(1)
