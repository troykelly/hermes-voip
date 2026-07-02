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
``on_unroutable`` (a normal network event — it does not tear the connection). A
single text frame that *parses* badly is the one exception that is recovered, not
fatal: it is logged loudly and skipped (ADR-0081), so one malformed frame is not a
DoS against the registration and every active call sharing the connection.

The ``websockets`` library (BSD-3-Clause) is **lazy-imported** inside
:meth:`connect` so that ``import hermes_voip`` is light when the ``webrtc``
optional extra is not installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hermes_voip.keepalive import build_keepalive_ok, build_options_ok
from hermes_voip.manager import (
    Cancel,
    InDialog,
    NewCall,
    RegistrationManager,
    Unroutable,
)
from hermes_voip.message import SipRequest, SipResponse, build_response, new_tag
from hermes_voip.transport.connection import CallResponseSink
from hermes_voip.transport.transaction import (
    InviteClientTransaction,
    InviteServerTransaction,
    TransactionState,
)

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

__all__ = ["WssSipTransport"]

_log = logging.getLogger(__name__)

_RESPONSE_PREFIX = "SIP/2.0 "
# RFC 5626 §4.4.1 / RFC 7118 CRLF keepalive: a double-CRLF ping is answered with a
# single-CRLF pong. Over WebSocket each arrives as one (whitespace-only) text frame.
_CRLF_KEEPALIVE_PING = "\r\n\r\n"
_CRLF_KEEPALIVE_PONG = "\r\n"
# A To/From header parameter carrying a dialog tag (after the name-addr's '>').
_TO_TAG_PARAM = re.compile(r";\s*tag=", re.IGNORECASE)
# The Via ``branch`` parameter (RFC 3261 §8.1.1.7) — the transaction identifier.
_VIA_BRANCH = re.compile(r";\s*branch=([^;,\s]+)", re.IGNORECASE)
_FINAL_STATUS = 200  # status >= 200 is a final response
_FIRST_FAILURE = 300  # status >= 300 is a non-2xx final


@dataclass(slots=True)
class _PendingInvite:
    """An inbound INVITE server transaction awaiting a final response.

    Tracked from the moment the INVITE is handed to ``on_new_call`` until we
    send its final response (or a CANCEL terminates it), so a CANCEL can be
    matched to it (RFC 3261 §9.2) and a 200 OK racing a CANCEL is suppressed.
    ``local_tag`` is the one stable To-tag for *every* response *we* generate
    for this CANCEL exchange — both the 487 to the INVITE and the 200 OK to the
    CANCEL — so they share a To-tag (§9.2) and a retransmitted 487/200 reuses it
    (§8.2.6.2).
    """

    invite: SipRequest
    call_id: str
    txn: InviteServerTransaction
    local_tag: str
    cancelled: bool = False


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
        on_cancel: Callable[[str], None] | None = None,
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
            on_cancel: Invoked with the Call-ID of a pending INVITE the peer has
                CANCELled (RFC 3261 §9.2), so the consumer aborts that call's
                half-built setup (the transport has already 487'd the INVITE).
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
        self._on_cancel = on_cancel
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
        # Inbound INVITE server transactions awaiting a final response, keyed by
        # Via branch (RFC 3261 §9.2 CANCEL matching).
        self._pending_invites: dict[str, _PendingInvite] = {}
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
        finals, and suppresses a 200 OK for an inbound INVITE the peer has
        already CANCELled (mirroring :class:`SipOverTlsTransport`).

        Args:
            message: The complete SIP message text.

        Raises:
            RuntimeError: if called before :meth:`connect`.
            websockets.exceptions.WebSocketException: if the connection is lost.
        """
        if self._ws is None:
            msg = "cannot send before connect()"
            raise RuntimeError(msg)
        is_response = message.startswith(_RESPONSE_PREFIX)
        if not is_response:
            self._register_if_invite(message)
        async with self._send_lock:
            if is_response and self._suppress_or_track_response(message):
                return  # 200 OK to a CANCELled inbound INVITE — dropped
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

    def _suppress_or_track_response(self, message: str) -> bool:
        """Handle an outbound INVITE response vs a pending CANCEL; return suppress.

        For a final response to an inbound INVITE we track (matched by Via branch,
        CSeq method ``INVITE``): a 200 OK for a transaction the peer CANCELled is
        suppressed (returns ``True``); otherwise the final is recorded on the
        server transaction and a non-CANCELled transaction's pending entry is
        cleared (it is complete). Responses to other methods are left untouched.
        """
        response = SipResponse.parse(message)
        if _method_of(response.header("CSeq")) != "INVITE":
            return False
        branch = _via_branch(response.header("Via"))
        if branch is None:
            return False
        pending = self._pending_invites.get(branch)
        if pending is None:
            return False
        status = response.status_code
        if status < _FINAL_STATUS:
            return False  # a provisional (1xx) keeps the transaction pending
        if pending.cancelled and status < _FIRST_FAILURE:
            # A 200 OK racing the CANCEL: the call is dead — drop it.
            _log.warning(
                "INVITE %s: suppressing a 200 OK for a CANCELled call",
                pending.call_id,
            )
            return True
        pending.txn.on_final_sent(status)
        if not pending.cancelled:
            del self._pending_invites[branch]
        return False

    # --- call registry (response routing) -----------------------------------

    def add_call(self, call_id: str, sink: CallResponseSink) -> None:
        """Register a call's response sink so its responses route to it by Call-ID."""
        self._calls[call_id] = sink

    def remove_call(self, call_id: str, sink: CallResponseSink | None = None) -> None:
        """Forget a call; drop any tracked client/server transactions for it.

        Mirrors :meth:`SipOverTlsTransport.remove_call`: when ``sink`` is given
        the registration is only removed if it is still that exact sink (an
        overlapping INVITE sharing a Call-ID may have overwritten the entry, so
        an earlier call's teardown must not evict the live later one). With
        ``sink=None`` the removal is unconditional.

        Any retained pending-INVITE server transaction for this Call-ID (a
        CANCELled call kept to keep suppressing a late 2xx) is also cleared here
        — the call is definitively gone once its teardown runs.
        """
        if sink is not None and self._calls.get(call_id) is not sink:
            return
        self._calls.pop(call_id, None)
        for key in [k for k in self._client_txns if k[0] == call_id]:
            del self._client_txns[key]
        for branch in [
            b for b, p in self._pending_invites.items() if p.call_id == call_id
        ]:
            del self._pending_invites[branch]

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
        """Parse one text frame (a whole message) and route it; skip if it won't parse.

        Over WebSocket each text frame is exactly one complete SIP message (RFC 7118
        §5), so there is no stream-framing problem here — but a frame can still fail
        to *parse*. A :class:`ValueError` from
        :meth:`~hermes_voip.message.SipResponse.parse` /
        :meth:`~hermes_voip.message.SipRequest.parse` is a per-message parse failure
        (ADR-0081): it is logged loudly (a WARNING with a non-PII summary — the repo
        is PUBLIC, rule 34) and the one bad frame is **skipped**, keeping the
        connection (and thus the registration and every active call) alive. One
        malformed frame must not be a DoS against unrelated calls — surfaced, not
        swallowed (rule 37). Whitespace-only keepalive frames never reach here (the
        read loop handles them before dispatch).
        """
        is_response = raw.startswith(_RESPONSE_PREFIX)
        try:
            message: SipResponse | SipRequest = (
                SipResponse.parse(raw) if is_response else SipRequest.parse(raw)
            )
        except ValueError as exc:
            # A non-PII summary only — never the raw frame (it may carry From/To/
            # Call-ID/SDP; the repo is PUBLIC, rule 34).
            _log.warning(
                "dropping an unparseable SIP frame (%s, len=%d) — connection kept",
                type(exc).__name__,
                len(raw),
            )
            return
        if isinstance(message, SipResponse):
            await self._dispatch_response(message)
        else:
            await self._dispatch_request(message)

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

    async def _handle_cancel(self, cancel: SipRequest) -> None:
        """Answer an inbound CANCEL and terminate the matching INVITE (RFC 3261 §9.2).

        A CANCEL shares the INVITE's top Via branch and (defensively) its Call-ID.
        When it matches a pending INVITE: the CANCEL is answered ``200 OK``, the
        INVITE is replied ``487 Request Terminated``, the entry is marked cancelled
        (so a 200 OK racing the answer task is suppressed in :meth:`send`), and the
        ``on_cancel`` hook fires once so the consumer tears down the half-built call.
        A **retransmitted** CANCEL (the entry is already cancelled) is absorbed —
        only the ``200 OK`` to the CANCEL is re-sent, with no second 487 or
        ``on_cancel`` (idempotent §9.2 handling). A CANCEL matching no transaction
        is answered ``481 Call/Transaction Does Not Exist``.
        """
        pending = self._match_cancel(cancel)
        if pending is None:
            await self.send(
                build_response(cancel, 481, "Call/Transaction Does Not Exist")
            )
            return
        already_cancelled = pending.cancelled
        pending.cancelled = True
        # 200 OK to the CANCEL itself. It carries the pending invite's stable
        # local_tag — the SAME To tag as the 487 below — per RFC 3261 §9.2: "The
        # To tag of the response to the CANCEL and the To tag in the response to
        # the original request SHOULD be the same." Reusing local_tag (never a
        # fresh new_tag()) also makes the 200 idempotent at the message level: a
        # retransmitted CANCEL is always re-answered with the identical To tag.
        await self.send(build_response(cancel, 200, "OK", to_tag=pending.local_tag))
        if already_cancelled:
            return  # the 487 + abort already happened on the first CANCEL
        # 487 the INVITE: a dialog-forming final carrying our stable local tag.
        await self.send(
            build_response(
                pending.invite, 487, "Request Terminated", to_tag=pending.local_tag
            )
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


def _via_branch(via: str | None) -> str | None:
    """The top Via's ``branch`` token (the transaction id, RFC 3261 §8.1.1.7)."""
    if via is None:
        return None
    match = _VIA_BRANCH.search(via)
    return match.group(1) if match is not None else None


def _txn_key(call_id: str | None, cseq: str | None) -> tuple[str, int] | None:
    if call_id is None or cseq is None:
        return None
    parts = cseq.split()
    if not parts or not parts[0].isdigit():
        return None
    return (call_id, int(parts[0]))
