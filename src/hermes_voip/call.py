"""The CallSession: the one IO-driving in-call orchestrator (ADR-0011 §2).

A :class:`CallSession` owns one established call's control plane — its
:class:`~hermes_voip.dialog.Dialog`, the signalling and media-control seams, the
per-call :class:`~hermes_voip.providers.policy.GuardSessionState`, and the local
media parameters — and exposes the agent-facing verbs ``hold`` / ``unhold`` /
``transfer_blind`` / ``transfer_attended``. It is also the manager's
``DialogConsumer``: :meth:`handle_request` answers inbound re-INVITE (mirrored
direction; **glare → 491**), NOTIFY (transfer progress), REFER (we are being
transferred), and BYE.

It drives the sans-IO ``dialog`` / ``incall`` / ``refer`` modules over two
injected seams — :class:`CallSignaling` (send wire text) and :class:`CallMedia`
(hold gating + teardown) — and correlates responses to its own requests by CSeq.
A re-INVITE/REFER that is challenged (401/407) is re-sent authenticated once.
Outbound verbs are serialised by a lock; while our offer is outstanding an
inbound re-INVITE is answered ``491 Request Pending`` (glare).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from hermes_voip._decimal import _parse_decimal
from hermes_voip.dialog import Dialog, InDialogRequest, build_in_dialog_request
from hermes_voip.digest import DigestChallenge, DigestCredentials, build_authorization
from hermes_voip.dtmf import DtmfSendMode
from hermes_voip.dtmf_sipinfo import (
    DTMF_RELAY_CONTENT_TYPE,
    build_dtmf_relay_body,
    parse_dtmf_info,
)
from hermes_voip.incall import (
    Glare,
    HoldConfirmed,
    LocalMediaSession,
    MediaUpdate,
    ReinviteRejected,
    UnsupportedReinviteOffer,
    build_hold_reinvite,
    classify_inbound_reinvite,
    handle_reinvite_response,
)
from hermes_voip.message import (
    SipRequest,
    SipResponse,
    build_request,
    build_response,
    new_branch,
)
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.refer import (
    NotifyProgress,
    ReferError,
    ReferRequest,
    TransferOutcomeReport,
    TransferUnknownReason,
    build_attended_refer,
    build_blind_refer,
    parse_notify_sipfrag,
    parse_refer,
)
from hermes_voip.sdp import (
    CryptoAttribute,
    SessionDescription,
    build_audio_offer,
    generate_answer_crypto,
)
from hermes_voip.session_timer import (
    RefreshOutcome,
    RefreshSucceeded,
    classify_refresh_failure,
)

__all__ = [
    "CallError",
    "CallMedia",
    "CallSession",
    "CallSignaling",
    "ReferHandler",
]

_log = logging.getLogger(__name__)

_PROVISIONAL_CEILING = 200  # a status below this is a 1xx provisional response
_UNAUTHORIZED = 401
_PROXY_AUTH_REQUIRED = 407
_FIRST_ERROR_STATUS = 300
_BAD_REQUEST = 400
_MAX_FORWARDS = "70"
_DEFAULT_RESPONSE_TIMEOUT = 32.0


class CallError(RuntimeError):
    """An in-call control verb could not complete (rejected, timed out, glare).

    ``status_code`` carries the SIP final-response status that failed the verb, or
    ``None`` when the verb timed out with no final response. It lets a caller that
    needs to branch on *why* a re-INVITE failed (the RFC 4028 refresh watchdog —
    491 glare vs 408/481 dead-dialog vs a transient 5xx) classify the failure
    instead of treating every non-2xx identically. Verbs that do not care (hold,
    transfer) simply ignore it.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Bind the human-readable ``message`` and the optional SIP ``status_code``."""
        super().__init__(message)
        self.status_code = status_code


@runtime_checkable
class CallSignaling(Protocol):
    """The call's SIP transaction-layer seam (ADR-0005 implements it).

    This is a **transaction** layer, not a bare socket: it owns request
    retransmission and the **ACK for a non-2xx final response** to an INVITE,
    which RFC 3261 §17.1.1.3 generates in the client transaction (same branch),
    not in the transaction user. :class:`CallSession` is the TU, so it emits only
    the §13.2.2.4 **2xx** ACK (a fresh transaction); it never ACKs a 401/407/491
    re-INVITE — that is this layer's job.
    """

    async def send(self, message: str) -> None:
        """Send one SIP message over the call's signalling transport."""
        ...


@runtime_checkable
class CallMedia(Protocol):
    """The call's media-control seam: hold gating, DTMF send, and teardown."""

    async def set_hold(self, on_hold: bool) -> None:
        """Gate (hold) or restore (resume) the RTP send and jitter buffer."""
        ...

    async def set_remote(self, address: str, port: int) -> None:
        """Re-point the outbound RTP target to a relocated peer media endpoint.

        Called on an in-dialog re-INVITE whose SDP offer moves the peer's audio to
        a new ``c=``/``m=audio`` endpoint (attended-transfer media re-anchor, MoH
        resume-elsewhere, SBC media relocation): re-point OUR outbound RTP to
        ``address``/``port`` and reset comedia latching so the transport re-learns
        the relocated peer's source — else agent→caller audio keeps flowing to the
        stale (latched) address (one-way audio). A no-op when the endpoint is
        unchanged, so an in-place hold/resume keeps its established latch. (Async
        to match the other seam methods; the implementation body may be sync work.)
        """
        ...

    async def send_dtmf(self, digits: str) -> None:
        """Send ``digits`` as RFC 4733 telephone-event RTP on the active call.

        Raises if the call negotiated no telephone-event payload type (ADR-0031) —
        DTMF is never silently dropped.
        """
        ...

    async def rekey_srtp(
        self,
        *,
        inbound: CryptoAttribute | None,
        outbound: CryptoAttribute | None,
    ) -> None:
        """Re-key the SRTP context from a re-offer's SDES ``a=crypto`` (RFC 4568 §6.1).

        Called on an in-dialog re-offer of a secured call so the media stays
        encrypted across hold/resume/re-INVITE (ADR-0053). ``outbound`` is OUR new
        key (encrypt + advertise), ``inbound`` is the peer's (decrypt); ``None``
        leaves that direction unchanged. A plain call passes both ``None``.
        """
        ...

    async def stop(self) -> None:
        """Tear down the media plane when the call ends; idempotent."""
        ...


type ReferHandler = Callable[[ReferRequest], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _ReanswerPlan:
    """A re-INVITE 200's SDES crypto, split into what to render vs. what to re-key.

    ``render`` is the ``a=crypto`` to advertise in the answer (or ``None`` for a plain
    answer). ``rekey`` is the ``(inbound, outbound)`` key pair to install in the engine
    once the answer is committed — set ONLY for a secured peer re-offer, ``None`` when
    nothing re-keys (offerless re-advertise, or a plain answer). Splitting the pure
    choice from the irreversible re-key lets :meth:`CallSession._answer_reinvite` build
    the whole 200 BEFORE touching the SRTP context, so a re-INVITE it cannot answer is
    dropped WHOLE (ADR-0081).
    """

    render: CryptoAttribute | None
    rekey: tuple[CryptoAttribute, CryptoAttribute] | None


class CallSession:
    """Drives one call's hold/transfer control and answers its in-dialog requests."""

    def __init__(  # noqa: PLR0913 — a call binds its dialog, two transport seams, guard state, media params and credentials; all keyword-only
        self,
        *,
        dialog: Dialog,
        signaling: CallSignaling,
        media: CallMedia,
        guard: GuardSessionState,
        local_media: LocalMediaSession,
        credentials: DigestCredentials,
        on_refer: ReferHandler | None = None,
        dtmf_send_mode: DtmfSendMode = DtmfSendMode.RFC4733,
        on_dtmf: Callable[[str], None] | None = None,
        response_timeout: float = _DEFAULT_RESPONSE_TIMEOUT,
    ) -> None:
        """Bind the call to its dialog, transport seams, and guard state."""
        self._dialog = dialog
        self._signaling = signaling
        self._media = media
        self._guard = guard
        self._local_media = local_media
        self._credentials = credentials
        self._refer_handler = on_refer
        # The resolved DTMF send backend (ADR-0036): in SIP_INFO mode :meth:`send_dtmf`
        # emits in-dialog INFO requests; otherwise it delegates to the media engine
        # (RFC 4733 / in-band). Settable so the adapter can resolve it after answer.
        self._dtmf_send_mode = dtmf_send_mode
        # Callback fired once per RECEIVED SIP-INFO DTMF digit (ADR-0036), or None to
        # ignore inbound INFO DTMF. The adapter wires this to ``CallLoop.feed_dtmf`` —
        # the SAME router the engine's RFC 4733 / in-band ``on_dtmf`` feeds, so digit
        # surfacing is uniform across all three receive backends. Settable after
        # construction (the loop is built after this session).
        self.on_dtmf: Callable[[str], None] | None = on_dtmf
        self._response_timeout = response_timeout
        self._pending: dict[int, asyncio.Queue[SipResponse]] = {}
        self._lock = asyncio.Lock()
        self._local_offer_pending = False
        self.on_hold = False
        self.ended = False
        self.transfer_progress: NotifyProgress | None = None
        # ADR-0109: the per-transfer terminal-outcome signal. Armed (a fresh Event)
        # by :meth:`_arm_transfer_outcome` BEFORE a REFER is sent, set by
        # :meth:`_on_notify` on a terminated transfer NOTIFY — and also on call-end
        # (BYE / hang_up) so an outcome wait wakes when the referrer leg is torn down.
        # ``None`` until the first transfer arms it.
        self._transfer_terminal: asyncio.Event | None = None
        # ADR-0109 P1: the CSeq NUMBER of the REFER whose transfer is currently
        # awaiting its outcome — the RFC 3515 implicit-subscription id a progress
        # NOTIFY carries as ``Event: refer;id=<CSeq>``. Set in :meth:`_refer` before the
        # REFER response is awaited (so a NOTIFY racing the 2xx still correlates),
        # cleared in :meth:`_disarm_transfer_outcome` when the wait ends. Lets
        # :meth:`_on_notify` reject a stale/late NOTIFY from a PRIOR transfer's still-
        # live subscription so it cannot wake or mis-classify a newer transfer. ``None``
        # when no transfer is awaiting an outcome.
        self._active_refer_cseq: int | None = None
        # ADR-0109 P1-b: the terminal transfer outcome LATCHED the instant the wait is
        # first woken — by :meth:`_on_notify` (a terminated NOTIFY → its progress) or
        # :meth:`_wake_transfer_on_end` (a BYE/hang_up → ``None``). Whichever fires
        # FIRST wins (``_transfer_outcome_latched`` guards the write), so a terminal
        # NOTIFY racing in AFTER a BYE cannot flip a torn-down leg's CALL_ENDED to
        # COMPLETED. The wait reads THIS, never the mutable :attr:`transfer_progress`,
        # so the decision is immune to a post-wake mutation.
        self._transfer_outcome_latched: bool = False
        self._transfer_outcome_progress: NotifyProgress | None = None
        # ADR-0109 (codex round-3): guards against a SECOND transfer arming while the
        # first still awaits its outcome (the wait runs OUTSIDE self._lock, so the lock
        # alone does not serialize it). One leg transfers to one place at a time; a
        # concurrent second transfer fails fast in :meth:`_arm_transfer_outcome` rather
        # than clobbering the first transfer's event / REFER CSeq / latch.
        self._transfer_in_progress: bool = False

    @property
    def dialog(self) -> Dialog:
        """The current dialog state (CSeq/SDP version advance as the call runs)."""
        return self._dialog

    @property
    def dialog_id(self) -> tuple[str, str, str]:
        """The demux key the manager routes this call's in-dialog requests by."""
        return self._dialog.dialog_id

    @property
    def guard(self) -> GuardSessionState:
        """The per-call guard state (``degraded`` gates the transfer tools)."""
        return self._guard

    # --- response correlation -----------------------------------------------

    async def on_response(self, response: SipResponse) -> None:
        """Deliver a response to the verb awaiting that CSeq (transport calls this)."""
        cseq = _cseq_number(response.header("CSeq"))
        if cseq is None:
            return
        queue = self._pending.get(cseq)
        if queue is not None:
            queue.put_nowait(response)

    async def _send_and_await_final(self, text: str, cseq: int) -> SipResponse:
        queue: asyncio.Queue[SipResponse] = asyncio.Queue()
        self._pending[cseq] = queue
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._response_timeout
        timeout_msg = (
            f"no final response to CSeq {cseq} within {self._response_timeout}s"
        )
        try:
            await self._signaling.send(text)
            while True:
                # An absolute deadline bounds the whole exchange, so a stream of
                # 1xx provisionals cannot keep the verb alive past the timeout.
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise CallError(timeout_msg)
                response = await asyncio.wait_for(queue.get(), remaining)
                if response.status_code < _PROVISIONAL_CEILING:
                    continue  # 1xx provisional; keep waiting for the final response
                return response
        except TimeoutError as exc:
            raise CallError(timeout_msg) from exc
        finally:
            self._pending.pop(cseq, None)

    # --- agent-facing verbs -------------------------------------------------

    async def hold(self) -> None:
        """Place the caller on hold (re-INVITE ``sendonly``) and gate the media."""
        async with self._lock:
            await self._reinvite("sendonly")
            self.on_hold = True
            await self._media.set_hold(True)

    async def unhold(self) -> None:
        """Resume the caller (re-INVITE ``sendrecv``) and un-gate the media."""
        async with self._lock:
            await self._reinvite("sendrecv")
            self.on_hold = False
            await self._media.set_hold(False)

    async def send_dtmf(self, digits: str) -> None:
        """Send ``digits`` as in-call DTMF via the resolved backend (ADR-0010/0034).

        In SIP-INFO send mode this emits one in-dialog ``INFO`` per digit
        (:meth:`send_dtmf_info`). Otherwise it delegates to the media engine, which
        emits RFC 4733 named-event RTP (or synthesises in-band tones) on the active
        call's stream under its own TX mutex (so DTMF never interleaves with audio). No
        re-INVITE and no hold gating — DTMF rides the established dialog/media path.
        Raises if the resolved backend cannot run (e.g. RFC 4733 with no negotiated
        telephone-event); DTMF is never silently dropped (rule 6/37).
        """
        if self._dtmf_send_mode is DtmfSendMode.SIP_INFO:
            await self.send_dtmf_info(digits)
            return
        await self._media.send_dtmf(digits)

    async def send_dtmf_info(self, digits: str, *, duration_ms: int = 160) -> None:
        """Send ``digits`` as SIP INFO DTMF: one in-dialog ``INFO`` each (ADR-0036).

        Builds an ``application/dtmf-relay`` body for each digit and sends it as an
        in-dialog ``INFO`` request (advancing the dialog CSeq — the ADR-0011 invariant),
        awaiting the FINAL response for each before the next so a rejection is not
        silently swallowed (cross-vendor review). A ``401``/``407`` challenge is met
        once with credentials (like re-INVITE/REFER); any ``>= 400`` final response
        raises :class:`CallError` (the digit was NOT accepted — never reported as sent).
        Under the call lock so it does not race a concurrent re-INVITE/REFER on the same
        dialog.

        Args:
            digits: The DTMF string to send (``0-9``, ``*``, ``#``, ``A``-``D``;
                case-insensitive). Empty ⇒ a no-op.
            duration_ms: The advisory ``Duration=`` value per digit (positive).

        Raises:
            ValueError: If ``digits`` contains a non-DTMF character (propagated from
                :func:`~hermes_voip.dtmf_sipinfo.build_dtmf_relay_body`), or
                ``duration_ms`` is not positive — raised BEFORE any INFO is sent so a
                bad digit never emits a partial burst.
            CallError: If the gateway rejects an ``INFO`` (a ``>= 400`` final response,
                including after an auth retry) — the digit was not accepted.
        """
        if not digits:
            return
        bodies = [build_dtmf_relay_body(d, duration_ms=duration_ms) for d in digits]
        async with self._lock:
            for body in bodies:
                await self._send_one_dtmf_info(body)

    async def _send_one_dtmf_info(self, body: str) -> None:
        """Send one DTMF ``INFO`` body, await its final response, raise on error.

        Caller holds :attr:`_lock`. Mirrors :meth:`_refer`'s build → await → re-auth →
        raise-on-error shape: a ``401``/``407`` is answered once with credentials, and
        any ``>= 400`` final response is a :class:`CallError`.
        """
        request = build_in_dialog_request(
            self._dialog,
            "INFO",
            extra_headers=(("Content-Type", DTMF_RELAY_CONTENT_TYPE),),
            body=body,
        )
        self._dialog = request.dialog
        response = await self._send_and_await_final(
            request.text, self._dialog.local_cseq
        )
        if response.status_code in (_UNAUTHORIZED, _PROXY_AUTH_REQUIRED):
            auth = self._authorization(response, "INFO")
            retry = build_in_dialog_request(
                self._dialog,
                "INFO",
                extra_headers=(
                    ("Content-Type", DTMF_RELAY_CONTENT_TYPE),
                    auth,
                ),
                body=body,
            )
            self._dialog = retry.dialog
            response = await self._send_and_await_final(
                retry.text, self._dialog.local_cseq
            )
        if response.status_code >= _FIRST_ERROR_STATUS:
            msg = f"DTMF INFO rejected: {response.status_code} {response.reason}"
            raise CallError(msg)

    async def hang_up(self) -> None:
        """End the call: send an in-dialog BYE, mark ended, stop media (ADR-0026).

        The UAC side of a BYE — the agent-initiated counterpart of :meth:`_on_bye`
        (which handles a BYE the *peer* sends). It builds an in-dialog BYE
        (advancing the dialog CSeq — ADR-0011 invariant 1), sends it on the
        signalling transport, flags the session ended, and stops the media engine.
        Stopping the media ends the conversational loop, so the call task's
        teardown classifies the end as AGENT_HANGUP (a SOFT, NORMAL end that keeps
        the Hermes session open for follow-up, never a hard ``/stop``).

        Idempotent: once the session has ended (a prior ``hang_up`` or an inbound
        BYE), this is a no-op — the dialog is gone, so a second BYE must not be
        sent. We do NOT wait for the BYE's 200 response: the call is over the
        moment we send BYE + stop media, and the gateway may never deliver a final
        response on a dropped media path (the same rationale as not blocking
        teardown on a network round-trip).

        The BYE send itself is best-effort for the same reason: a TLS/WS/transport
        fault there means the peer never receives the BYE, which is functionally
        the same lost-packet case its own dialog timers already handle. Local
        teardown (marking ended, stopping media) must complete regardless — an
        escaped send failure would otherwise leave the session marked ended but
        the media engine still running, wedging the conversational loop open on a
        call the session already considers over.
        """
        async with self._lock:
            if self.ended:
                return
            request = build_in_dialog_request(self._dialog, "BYE")
            self._dialog = request.dialog
            self.ended = True
            self._wake_transfer_on_end()
            try:
                await self._signaling.send(request.text)
            except Exception as exc:  # noqa: BLE001 — best-effort BYE; never strand teardown (rule 37: logged, not swallowed)
                _log.warning(
                    "hang_up: BYE send failed on call %s (peer may not observe "
                    "it; local teardown proceeds regardless): %s",
                    self._dialog.call_id,
                    exc,
                )
            await self._media.stop()

    async def transfer_blind(
        self,
        target_uri: str,
        *,
        referred_by: str | None = None,
        outcome_timeout: float = 0.0,
    ) -> TransferOutcomeReport:
        """Blind-transfer the caller to ``target_uri`` (REFER); return the outcome.

        Sends the RFC 3515 REFER, then waits up to ``outcome_timeout`` seconds for the
        terminal transfer-progress NOTIFY and returns a
        :class:`~hermes_voip.refer.TransferOutcomeReport` (ADR-0109 P2): a terminal
        NOTIFY yields the ``progress`` with no ``unknown_reason``; otherwise the report
        names why the outcome is unknown — ``SUBSCRIPTION_DECLINED`` when the peer
        declines the subscription (RFC 4488 ``Refer-Sub: false``), else ``TIMEOUT`` /
        ``CALL_ENDED`` / ``WAIT_DISABLED`` per :meth:`_await_transfer_outcome`.

        Raises:
            CallError: if the gateway rejects the REFER (a ``>= 300`` final response).
        """
        armed_event: asyncio.Event | None = None
        try:
            async with self._lock:
                armed_event = self._arm_transfer_outcome()
                response = await self._refer(
                    lambda auth: build_blind_refer(
                        self._dialog, target_uri, referred_by=referred_by, auth=auth
                    )
                )
                subscription_declined = _refer_subscription_declined(response)
            # The REFER is accepted and the lock released; await the terminal NOTIFY
            # OUTSIDE the lock so the bounded wait never blocks a concurrent
            # hold / refresh / DTMF on the same dialog.
            if subscription_declined:
                # A terminal NOTIFY may have raced the 2xx and already latched (the
                # event is armed before the REFER, and NOTIFY handling takes no call
                # lock); honour it. Else the peer declined the progress subscription
                # (RFC 4488), so no outcome will follow (codex round-6).
                latched = self._latched_transfer_outcome()
                if latched is not None:
                    return latched
                return TransferOutcomeReport(
                    progress=None,
                    unknown_reason=TransferUnknownReason.SUBSCRIPTION_DECLINED,
                )
            return await self._await_transfer_outcome(armed_event, outcome_timeout)
        finally:
            if armed_event is not None:
                self._disarm_transfer_outcome(armed_event)

    async def transfer_attended(
        self,
        consult: Dialog,
        *,
        referred_by: str | None = None,
        outcome_timeout: float = 0.0,
    ) -> TransferOutcomeReport:
        """Attended-transfer the caller to the ``consult`` peer (REFER + Replaces).

        Same outcome contract as :meth:`transfer_blind`: after the REFER 2xx it waits
        up to ``outcome_timeout`` seconds for the terminal NOTIFY and returns a
        :class:`~hermes_voip.refer.TransferOutcomeReport` — the terminal ``progress``,
        or the discriminated reason it is unknown (``SUBSCRIPTION_DECLINED`` /
        ``TIMEOUT`` / ``CALL_ENDED`` / ``WAIT_DISABLED``) (ADR-0109 P2).

        Raises:
            CallError: if the gateway rejects the REFER (a ``>= 300`` final response).
        """
        armed_event: asyncio.Event | None = None
        try:
            async with self._lock:
                armed_event = self._arm_transfer_outcome()
                response = await self._refer(
                    lambda auth: build_attended_refer(
                        self._dialog, consult, referred_by=referred_by, auth=auth
                    )
                )
                subscription_declined = _refer_subscription_declined(response)
            if subscription_declined:
                # A terminal NOTIFY may have raced the 2xx and already latched (the
                # event is armed before the REFER, and NOTIFY handling takes no call
                # lock); honour it. Else the peer declined the progress subscription
                # (RFC 4488), so no outcome will follow (codex round-6).
                latched = self._latched_transfer_outcome()
                if latched is not None:
                    return latched
                return TransferOutcomeReport(
                    progress=None,
                    unknown_reason=TransferUnknownReason.SUBSCRIPTION_DECLINED,
                )
            return await self._await_transfer_outcome(armed_event, outcome_timeout)
        finally:
            if armed_event is not None:
                self._disarm_transfer_outcome(armed_event)

    def _arm_transfer_outcome(self) -> asyncio.Event:
        """Reset transfer progress + arm the terminal-NOTIFY event BEFORE the REFER.

        Done under the call lock before the REFER is sent so a terminal NOTIFY that
        races the 2xx is never missed: :meth:`_on_notify` sets the freshly-armed event
        and the wait armed here observes it (ADR-0109). Returns the exact Event owned by
        this transfer so its ``finally`` cleanup cannot clear a newer transfer's state.

        Raises:
            CallError: if the call has already ended — a BYE that raced the adapter's
                ended-check (codex round-4): never REFER a dead dialog nor arm an event
                nothing will signal. Or if a transfer is already awaiting its outcome on
                this call — the outcome state is session-wide, so a concurrent second
                transfer would clobber the first's event / CSeq / latch (codex round-3).
                One leg transfers to one place at a time; the second fails fast.
        """
        if self.ended:
            msg = "the call has ended; cannot transfer"
            raise CallError(msg)
        if self._transfer_in_progress:
            msg = "a transfer is already awaiting its outcome on this call"
            raise CallError(msg)
        self._transfer_in_progress = True
        self.transfer_progress = None
        self._active_refer_cseq = None
        self._transfer_outcome_latched = False
        self._transfer_outcome_progress = None
        event = asyncio.Event()
        self._transfer_terminal = event
        return event

    def _disarm_transfer_outcome(self, event: asyncio.Event) -> None:
        """Clear correlation/wait state iff ``event`` is still the active transfer.

        A timed-out RFC 3515 subscription may keep sending NOTIFYs. Clearing its REFER
        CSeq and Event once the transfer method returns makes those stale reports unable
        to linger as the call's active outcome state. The identity check is defense in
        depth against a prior transfer's cleanup clobbering a newly-armed event.
        """
        if self._transfer_terminal is event:
            self._transfer_terminal = None
            self._active_refer_cseq = None
            self._transfer_outcome_latched = False
            self._transfer_outcome_progress = None
            self._transfer_in_progress = False

    def _latched_transfer_outcome(self) -> TransferOutcomeReport | None:
        """The already-latched transfer outcome, or ``None`` if nothing is latched.

        The cause is latched atomically when the wait is first woken (ADR-0109 P1-b): a
        terminal :attr:`transfer_progress` is the final outcome (COMPLETED / FAILED); a
        latched ``None`` means the leg ended first (CALL_ENDED). Callers consult this
        BEFORE reporting an opt-out / timeout / subscription-declined reason so a
        terminal NOTIFY that raced the REFER 2xx is never discarded (codex round-5/6).
        """
        if not self._transfer_outcome_latched:
            return None
        progress = self._transfer_outcome_progress
        if progress is not None:
            return TransferOutcomeReport(progress=progress, unknown_reason=None)
        return TransferOutcomeReport(
            progress=None, unknown_reason=TransferUnknownReason.CALL_ENDED
        )

    async def _await_transfer_outcome(
        self, event: asyncio.Event, timeout: float
    ) -> TransferOutcomeReport:
        """Wait up to ``timeout`` s for the terminal transfer NOTIFY (ADR-0109).

        Returns a :class:`~hermes_voip.refer.TransferOutcomeReport` naming the real
        outcome (ADR-0109 P2): a terminal :attr:`transfer_progress` (set by
        :meth:`_on_notify` on a terminated NOTIFY) yields ``(progress, unknown_reason
        =None)``; otherwise the report carries no progress and the discriminated reason
        the wait ended for:

        * ``timeout <= 0`` with nothing already latched — the wait is opted out:
          ``WAIT_DISABLED`` (no wait ran). A terminal outcome latched before the REFER
          2xx (it can race the arm) is still reported, never discarded.
        * the wait elapses with no terminal NOTIFY: ``TIMEOUT``.
        * the event fires with no terminal outcome latched — a BYE / hang_up woke it
          first (or only a non-terminal ``100 Trying`` arrived): ``CALL_ENDED``. The
          cause is latched when the wait is first woken, so a terminal NOTIFY racing in
          after the BYE cannot flip it (P1-b); we never infer success from a torn-down
          leg.
        """
        # The event is armed BEFORE the REFER, so a terminal NOTIFY (or a call-end) can
        # latch an outcome before we decide to wait (racing the 2xx) or right as the
        # deadline fires. Wait only when nothing is latched and the wait is enabled; a
        # TimeoutError falls THROUGH to the single latch re-read below (never returning
        # early), so an outcome latched at the deadline is honoured, never lost as a
        # TIMEOUT (codex round-5/6). ``_latched_transfer_outcome`` reads the LATCHED
        # cause (fixed atomically when the wait was first woken, ADR-0109 P1-b), never
        # the mutable transfer_progress.
        if not self._transfer_outcome_latched and timeout > 0:
            # A timeout falls through to the latch re-read below — a NOTIFY may have
            # latched an outcome right at the deadline (codex round-6).
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout)
        latched = self._latched_transfer_outcome()
        if latched is not None:
            return latched
        return TransferOutcomeReport(
            progress=None,
            unknown_reason=(
                TransferUnknownReason.TIMEOUT
                if timeout > 0
                else TransferUnknownReason.WAIT_DISABLED
            ),
        )

    def _wake_transfer_on_end(self) -> None:
        """Wake a pending transfer-outcome wait when the call ends (ADR-0109).

        A BYE (either side) tears the referrer leg down; a bounded outcome wait must
        return rather than block for the full timeout. If no terminal outcome has been
        latched yet, latch CALL_ENDED (``None``) — the FIRST waker wins (P1-b), so a
        terminal NOTIFY racing in after this cannot flip a torn-down leg to COMPLETED
        (we never infer success from a BYE) — then set the armed event. A no-op when no
        transfer is in flight.
        """
        if self._transfer_terminal is not None:
            if not self._transfer_outcome_latched:
                self._transfer_outcome_latched = True
                self._transfer_outcome_progress = None
            self._transfer_terminal.set()

    async def refresh_session(
        self, extra_headers: Sequence[tuple[str, str]]
    ) -> RefreshOutcome:
        """Send an RFC 4028 session-refresh re-INVITE; classify the outcome (ADR-0071).

        A session refresh is an in-dialog re-INVITE carrying the ``Session-Expires``
        (with the negotiated refresher) + ``Supported: timer`` headers so the session
        timer resets on both sides. It REUSES the existing re-INVITE machinery
        (:meth:`_reinvite`) — no new transaction type — with the session-timer headers
        threaded through ``extra_headers``.

        A refresh **re-asserts** the call's current state; it never changes it. So the
        offered direction mirrors the live media direction — ``sendonly`` while the
        call is on hold, else ``sendrecv`` — exactly like an offerless re-INVITE
        answer (:meth:`_answer_reinvite`). Refreshing a held call with ``sendrecv``
        would silently un-hold it at the SDP layer while ``on_hold`` and the engine
        hold-gate stayed set.

        Returns a discriminated :data:`RefreshOutcome` (RFC 4028 §10 / RFC 3261 §14.1)
        so the watchdog can act correctly per response class instead of tearing the
        call down on every non-2xx:

        * :class:`RefreshSucceeded` — the peer accepted (2xx); the timer is reset.
        * :class:`RefreshTeardown` — timeout / 408 / 481: the dialog is dead → BYE.
        * :class:`RefreshRetry` — 491 glare: retry after a randomized backoff.
        * :class:`RefreshContinue` — any other non-2xx (5xx/6xx/488…): keep the call
          up; the next refresh tick / the peer's deadline still guards liveness.
        """
        async with self._lock:
            # Read the live direction UNDER the lock — hold/unhold take the same lock,
            # so the refresh offers a direction consistent with the committed state.
            direction = "sendonly" if self.on_hold else "sendrecv"
            try:
                await self._reinvite(direction, extra_headers=extra_headers)
            except CallError as exc:
                # A failed refresh is NOT uniformly fatal — surface the SIP status
                # (carried on the error; None for a timeout) and let the pure
                # classifier decide BYE / retry / continue (RFC 4028 §10).
                return classify_refresh_failure(exc.status_code)
            return RefreshSucceeded()

    async def _reinvite(
        self, direction: str, *, extra_headers: Sequence[tuple[str, str]] = ()
    ) -> None:
        self._local_offer_pending = True
        try:
            # On a secured (SRTP) call mint a FRESH per-offer key echoing the call's
            # accepted tag+suite (RFC 4568 §6.1) so the re-offer stays RTP/SAVP +
            # a=crypto and never downgrades to cleartext RTP/AVP (ADR-0053). The new
            # key is ADVERTISED in the offer but only COMMITTED to the engine after
            # the peer accepts the re-INVITE (below) — a rejected/timed-out re-offer
            # must never leave outbound encrypted with a key the peer never agreed to.
            offer_crypto = self._reoffer_crypto()
            result = build_hold_reinvite(
                self._dialog,
                self._local_media,
                direction,
                crypto=offer_crypto,
                extra_headers=extra_headers,
            )
            self._dialog = result.dialog
            response = await self._send_and_await_final(
                result.text, self._dialog.local_cseq
            )
            if response.status_code in (_UNAUTHORIZED, _PROXY_AUTH_REQUIRED):
                auth = self._authorization(response, "INVITE")
                result = build_hold_reinvite(
                    self._dialog,
                    self._local_media,
                    direction,
                    auth=auth,
                    crypto=offer_crypto,
                    extra_headers=extra_headers,
                )
                self._dialog = result.dialog
                response = await self._send_and_await_final(
                    result.text, self._dialog.local_cseq
                )
            outcome = handle_reinvite_response(response)
            if isinstance(outcome, HoldConfirmed):
                # ACK the 2xx first (RFC 3261 §13.2.2.4) — the transaction completes
                # regardless of whether the negotiated media is acceptable. Then COMMIT
                # the SRTP re-key: RFC 4568 §6.1 — outbound uses OUR offered key,
                # inbound uses the peer's ANSWER key. A secured re-offer answered
                # without a usable a=crypto is a failed negotiation (or a downgrade
                # attempt) and raises — never silently confirmed as a media change.
                await self._send_ack(response)
                await self._commit_reoffer_keys(offer_crypto, outcome.answer)
                return
            if isinstance(outcome, ReinviteRejected):
                detail = (
                    "glare (491 Request Pending)"
                    if outcome.is_glare
                    else f"{outcome.status_code} {outcome.reason}"
                )
                msg = f"re-INVITE rejected: {detail}"
                # Carry the SIP status so a caller that must classify the failure
                # (the RFC 4028 refresh watchdog) can tell 491/408/481/5xx apart.
                raise CallError(msg, status_code=outcome.status_code)
            msg = "re-INVITE not confirmed after authentication"
            raise CallError(msg)
        finally:
            self._local_offer_pending = False

    # --- SDES SRTP continuity across in-dialog re-offers (ADR-0053) ----------

    def _reoffer_crypto(self) -> CryptoAttribute | None:
        """Mint a fresh per-offer SDES key for a re-offer WE send, or ``None``.

        On a secured call (``self._local_media.crypto`` set) this echoes the call's
        negotiated tag + suite with a fresh random key (RFC 4568 §6.1) so the
        re-INVITE offer stays ``RTP/SAVP`` + ``a=crypto``. ``None`` on a plain call
        keeps the re-offer plain ``RTP/AVP``.
        """
        accepted = self._local_media.crypto
        if accepted is None:
            return None
        return generate_answer_crypto(accepted)

    def _adopt_local_crypto(self, crypto: CryptoAttribute) -> None:
        """Persist the re-offer's crypto as the call's current SDES context.

        Keeps :attr:`_local_media.crypto` carrying a live tag + suite so the NEXT
        re-offer echoes a current negotiation identifier (the tag/suite are stable
        across a dialog; this also keeps an offerless re-INVITE answer secured).
        """
        self._local_media = replace(self._local_media, crypto=crypto)

    def _peer_reoffer_is_downgrade(self, offer: SessionDescription) -> bool:
        """``True`` if a peer re-offer would DOWNGRADE a secured call to cleartext.

        On an established SRTP call (``_local_media.crypto`` set) a re-INVITE offer
        that is plain ``RTP/AVP`` or carries no usable ``a=crypto`` cannot be answered
        securely — answering it plain would drop the media to cleartext mid-call. Such
        an offer is rejected (488) rather than honoured (ADR-0053 continuity).
        """
        if self._local_media.crypto is None:
            return False  # a plain call has nothing to downgrade
        audio = offer.audio
        return audio is None or not audio.is_srtp or not audio.crypto_attrs

    def _plan_reanswer_crypto(self, offer: SessionDescription | None) -> _ReanswerPlan:
        """Choose the SDES ``a=crypto`` for a re-INVITE answer/offer WE emit in a 200.

        Pure: mutates nothing (no re-key, no key adoption), so the answer can be built
        BEFORE any irreversible state and a re-INVITE we cannot answer is dropped WHOLE
        (ADR-0081); :meth:`_commit_reanswer_crypto` applies the planned re-key after.

        - Peer re-offer (``offer`` set) on a secured call: mint our fresh answer key
          echoing the OFFERED tag + suite; the plan re-keys the engine — inbound from
          the peer's key, outbound from ours (RFC 4568 §6.1). A plain-call peer offer is
          answered plain. (A secured call's downgrade re-offer never reaches here — it
          is rejected 488 upstream by :meth:`_peer_reoffer_is_downgrade`.)
        - Offerless re-INVITE (``offer`` is ``None``) on a secured call: WE offer, but
          re-advertise the call's ESTABLISHED key (no rotation, no re-key). The peer's
          answer rides the ACK, which this dialog does not parse for SDP; re-using the
          current key keeps BOTH directions on their agreed keys, so continuity holds
          without consuming the ACK answer. A plain call offers plain.
        """
        if offer is None:
            # Offerless: re-advertise the current key unchanged (no fresh mint, no
            # engine re-key) so there is no dependency on the peer's ACK-borne answer.
            return _ReanswerPlan(render=self._local_media.crypto, rekey=None)
        audio = offer.audio
        if self._local_media.crypto is None or audio is None or not audio.is_srtp:
            return _ReanswerPlan(render=None, rekey=None)
        if not audio.crypto_attrs:
            return _ReanswerPlan(render=None, rekey=None)
        peer_crypto = audio.crypto_attrs[0]
        answer_crypto = generate_answer_crypto(peer_crypto)
        return _ReanswerPlan(render=answer_crypto, rekey=(peer_crypto, answer_crypto))

    async def _commit_reanswer_crypto(self, plan: _ReanswerPlan) -> None:
        """Apply the re-key :meth:`_plan_reanswer_crypto` chose (secured peer re-offer).

        Called only once the 200 has been built and is about to be sent, so a re-INVITE
        we could not answer never touches the live SRTP key (ADR-0081 drop-whole).
        """
        if plan.rekey is None:
            return
        peer_crypto, answer_crypto = plan.rekey
        await self._media.rekey_srtp(inbound=peer_crypto, outbound=answer_crypto)
        self._adopt_local_crypto(answer_crypto)

    async def _commit_reoffer_keys(
        self, offer_crypto: CryptoAttribute | None, answer: SessionDescription
    ) -> None:
        """Commit a re-offer's SRTP keys after the peer ACCEPTED it (RFC 4568 §6.1).

        Called only on a confirmed (2xx) re-INVITE WE sent. When we offered SRTP
        (``offer_crypto`` set), the peer's answer MUST carry a usable ``a=crypto`` —
        that is the peer's key for OUR inbound decrypt path. We then re-key outbound
        with our offered key (now accepted) and inbound with the peer's, and adopt the
        new context. A plain re-offer (``offer_crypto`` ``None``) commits nothing.

        Raises:
            CallError: If we offered SRTP but the answer is not ``RTP/SAVP`` with a
                usable ``a=crypto`` (a downgrade), or the answer's crypto does not
                echo the offered ``tag``/``suite`` (RFC 4568 §6.1: the answer's tag
                identifies the accepted offered crypto). The media change is rejected,
                never silently accepted on a key the peer never selected.
        """
        if offer_crypto is None:
            return
        audio = answer.audio
        if audio is None or not audio.is_srtp or not audio.crypto_attrs:
            msg = "secured re-INVITE answered without a usable a=crypto (downgrade)"
            raise CallError(msg)
        peer_answer = audio.crypto_attrs[0]
        if (
            peer_answer.tag != offer_crypto.tag
            or peer_answer.suite != offer_crypto.suite
        ):
            # RFC 4568 §6.1: a compliant answer echoes the offered tag (and the suite
            # it selected). A mismatch is a non-compliant/forged answer — committing it
            # would key outbound to a tag the peer never accepted.
            msg = "secured re-INVITE answer crypto does not match the offered tag/suite"
            raise CallError(msg)
        await self._media.rekey_srtp(inbound=peer_answer, outbound=offer_crypto)
        self._adopt_local_crypto(offer_crypto)

    async def _refer(
        self, build: Callable[[tuple[str, str] | None], InDialogRequest]
    ) -> SipResponse:
        """Send a REFER (re-auth once on 401/407); return its 2xx, raise on >= 300.

        Returns the REFER's final 2xx response so the caller can inspect its
        ``Refer-Sub`` (RFC 4488) to decide whether a progress NOTIFY will follow
        (ADR-0109).
        """
        result = build(None)
        self._dialog = result.dialog
        # build_in_dialog_request stamps CSeq ``old local_cseq + 1`` into the REFER
        # and returns the dialog carrying THAT new value. Capture it before awaiting
        # the response so a NOTIFY racing the 2xx can correlate to the exact request.
        # If authentication retries, the rebuilt REFER advances CSeq again below and
        # replaces this with the accepted REFER's actual id.
        self._active_refer_cseq = self._dialog.local_cseq
        response = await self._send_and_await_final(
            result.text, self._dialog.local_cseq
        )
        if response.status_code in (_UNAUTHORIZED, _PROXY_AUTH_REQUIRED):
            auth = self._authorization(response, "REFER")
            result = build(auth)
            self._dialog = result.dialog
            self._active_refer_cseq = self._dialog.local_cseq
            response = await self._send_and_await_final(
                result.text, self._dialog.local_cseq
            )
        if response.status_code >= _FIRST_ERROR_STATUS:
            msg = f"REFER rejected: {response.status_code} {response.reason}"
            raise CallError(msg)
        return response

    def _authorization(self, response: SipResponse, method: str) -> tuple[str, str]:
        proxy = response.status_code == _PROXY_AUTH_REQUIRED
        challenge_header = "Proxy-Authenticate" if proxy else "WWW-Authenticate"
        header_name = "Proxy-Authorization" if proxy else "Authorization"
        challenge = DigestChallenge.parse(response.header(challenge_header) or "")
        value = build_authorization(
            challenge,
            self._credentials,
            method=method,
            uri=self._dialog.remote_target,
        )
        return (header_name, value)

    async def _send_ack(self, response: SipResponse) -> None:
        """ACK a 2xx to our re-INVITE (same CSeq number, method ACK, new branch)."""
        cseq = _cseq_number(response.header("CSeq"))
        if cseq is None:
            msg = "cannot ACK a 2xx with no CSeq"
            raise CallError(msg)
        dialog = self._dialog
        via = (
            f"SIP/2.0/{dialog.transport} {dialog.local_sent_by}"
            f";branch={new_branch()};rport"
        )
        headers: list[tuple[str, str]] = [("Via", via), ("Max-Forwards", _MAX_FORWARDS)]
        headers += [("Route", route) for route in dialog.route_set]
        headers += [
            ("From", f"<{dialog.local_uri}>;tag={dialog.local_tag}"),
            ("To", f"<{dialog.remote_uri}>;tag={dialog.remote_tag}"),
            ("Call-ID", dialog.call_id),
            ("CSeq", f"{cseq} ACK"),
            ("Contact", dialog.local_contact),
        ]
        await self._signaling.send(build_request("ACK", dialog.remote_target, headers))

    # --- inbound in-dialog requests (DialogConsumer) ------------------------

    def _build_or_drop(self, build: Callable[[], str], *, kind: str) -> str | None:
        """Build one in-dialog auto-response, or drop the request if it cannot be built.

        Every in-dialog answer echoes the request's Via, From, To, Call-ID and CSeq
        (RFC 3261 §8.2.6); :func:`build_response` raises :class:`ValueError` when the
        request lacks one to echo (or carries an un-echoable value). This runs INLINE
        in the transport reader task — :meth:`handle_request` is the manager's
        ``DialogConsumer`` and is awaited by the dispatcher OUTSIDE its parse-only
        guard (ADR-0081) — so an escaping ``ValueError`` would unwind the reader and
        fire ``on_connection_lost``, dropping every OTHER live call and the
        registration on the shared signalling connection over one packet (the ADR-0081
        DoS, response-build side). Fail closed: log a non-PII WARNING (the method
        ``kind`` and the exception TYPE only — never the wire content, rule 34) and
        return ``None`` so the caller drops just this request, keeping the reader and
        every other call alive.
        """
        try:
            return build()
        except ValueError as exc:
            _log.warning(
                "dropping a header-incomplete in-dialog %s we cannot answer (%s) —"
                " call and connection kept",
                kind,
                type(exc).__name__,
            )
            return None

    async def _answer_or_drop(self, build: Callable[[], str], *, kind: str) -> None:
        """Build via :meth:`_build_or_drop` and send the response if it built."""
        response = self._build_or_drop(build, kind=kind)
        if response is not None:
            await self._signaling.send(response)

    async def handle_request(self, request: SipRequest) -> None:
        """Answer an inbound in-dialog request (re-INVITE/REFER/NOTIFY/BYE/INFO/ACK).

        A request that PARSES and routes here (routing keys only on the To/From tags +
        Call-ID) but lacks a mandatory header to echo cannot be answered; each response
        is built via :meth:`_build_or_drop` / :meth:`_answer_or_drop`, so such a request
        is dropped fail-closed rather than escaping ``ValueError`` into the reader
        (ADR-0081). A request whose answer cannot be built has NO other effect: any
        response that gates call/dialog state is built BEFORE that state is mutated, so
        a matched-but-unanswerable request is dropped WHOLE.
        """
        method = request.method
        if method == "INVITE":
            await self._on_reinvite(request)
        elif method == "BYE":
            await self._on_bye(request)
        elif method == "NOTIFY":
            await self._on_notify(request)
        elif method == "REFER":
            await self._on_refer(request)
        elif method == "INFO":
            await self._on_info(request)
        elif method == "ACK":
            return  # confirms our 2xx answer to an inbound re-INVITE; no response
        else:
            await self._answer_or_drop(
                lambda: build_response(request, 501, "Not Implemented"), kind="request"
            )

    async def _on_info(self, request: SipRequest) -> None:
        """Answer an inbound ``INFO`` (SIP INFO DTMF receive, ADR-0036).

        Every in-dialog ``INFO`` is acknowledged ``200 OK`` (per RFC 6086 it must get a
        final response). When the body is a DTMF relay/simple body, the parsed digit is
        surfaced through :attr:`on_dtmf` (the same router the engine's RFC 4733 /
        in-band receive feeds) — so an inbound keypad press resolves an armed
        confirmation or
        joins a menu group exactly as the other backends do. A non-DTMF INFO (e.g. a
        media-control body) is acknowledged but surfaces nothing, and an INFO with no
        bound sink is acknowledged and dropped (never crashes the dialog). An INFO we
        cannot acknowledge (header-incomplete) is dropped WHOLE: the 200 is built first,
        so its digit is never surfaced when the ACK cannot be sent (ADR-0081).
        """
        response = self._build_or_drop(
            lambda: build_response(request, 200, "OK"), kind="INFO"
        )
        if response is None:
            return
        await self._signaling.send(response)
        digit = parse_dtmf_info(request.header("Content-Type") or "", request.body)
        if digit is not None and self.on_dtmf is not None:
            self.on_dtmf(digit)

    async def _on_reinvite(self, request: SipRequest) -> None:
        try:
            routing = classify_inbound_reinvite(
                request, pending_local_offer=self._local_offer_pending
            )
        except ValueError as exc:
            # A malformed re-INVITE SDP offer makes SessionDescription.parse raise
            # SdpError (a ValueError subclass — non-numeric m= port / rtpmap / fmtp /
            # ptime); an unclassifiable direction raises IncallError (also ValueError).
            # classify runs INLINE in the reader (handle_request is awaited bare), so an
            # escaping ValueError would unwind it and tear down the whole shared
            # signalling connection (ADR-0081, offer-parse side; the build_response
            # answer sites below are already guarded). Fail closed: reject 400 and drop
            # WHOLE — classify is the first statement, so no state changed — logging
            # non-PII (the exception TYPE only, never the offer body, rule 34).
            _log.warning(
                "rejecting an in-dialog re-INVITE with an unparseable offer (%s) —"
                " call and connection kept",
                type(exc).__name__,
            )
            await self._answer_or_drop(
                lambda: build_response(request, _BAD_REQUEST, "Bad Request"),
                kind="INVITE",
            )
            return
        if isinstance(routing, Glare):
            await self._answer_or_drop(
                lambda: build_response(request, 491, "Request Pending"), kind="INVITE"
            )
            return
        if isinstance(routing, UnsupportedReinviteOffer):
            await self._answer_or_drop(
                lambda: build_response(request, 488, "Not Acceptable Here"),
                kind="INVITE",
            )
            return
        if isinstance(routing, MediaUpdate):
            # Downgrade resistance (ADR-0053): a secured call never answers a plain
            # (no-a=crypto) re-offer with cleartext media — reject it 488 and leave
            # the established SRTP context untouched.
            if self._peer_reoffer_is_downgrade(routing.offer):
                await self._answer_or_drop(
                    lambda: build_response(request, 488, "Not Acceptable Here"),
                    kind="INVITE",
                )
                return
            # Flip hold state only once the re-INVITE is actually answered: an
            # unanswerable one is dropped WHOLE, so we must not act on a media change
            # the peer never saw us confirm (ADR-0081 drop-whole).
            if not await self._answer_reinvite(
                request, routing.answer_direction, offer=routing.offer
            ):
                return
            # Re-point outbound RTP when the peer RELOCATED its media endpoint while
            # still receiving our audio (not held): a re-INVITE that moves c=/m=audio
            # (attended-transfer re-anchor, MoH resume-elsewhere, SBC relocation)
            # otherwise leaves agent→caller RTP going to the stale address (one-way
            # audio). Done BEFORE the hold flip so a resume-and-relocate resumes onto
            # the new target. Skipped when held_by_peer — a hold/black-hole re-offer
            # names no live target and the peer is not receiving our media anyway;
            # the engine no-ops an unchanged endpoint, so an in-place resume keeps
            # its comedia latch.
            if not routing.held_by_peer:
                audio = routing.offer.audio
                new_addr = audio.connection_address if audio is not None else None
                if audio is not None and new_addr is not None and audio.port > 0:
                    await self._media.set_remote(new_addr, audio.port)
            self.on_hold = routing.held_by_peer
            await self._media.set_hold(routing.held_by_peer)
            return
        # OfferlessReinvite: re-offer our current media direction in the 2xx (drops
        # whole if unanswerable — no further state to gate).
        await self._answer_reinvite(
            request, "sendonly" if self.on_hold else "sendrecv", offer=None
        )

    async def _answer_reinvite(
        self, request: SipRequest, direction: str, *, offer: SessionDescription | None
    ) -> bool:
        """Answer a re-INVITE ``200 OK``; return whether it was actually answered.

        Answering re-keys the SRTP engine (secured peer re-offer) and bumps the SDP
        version — both irreversible. A re-INVITE we cannot answer (missing a header to
        echo) must have NO effect, so the WHOLE 200 (the actual response we send) is
        built via :meth:`_build_or_drop` BEFORE any of that. On failure the re-INVITE is
        dropped WHOLE — no re-key, no version bump, no send — and ``False`` is returned
        so the caller skips the media-state change too. Returns ``True`` once the 200 is
        sent.
        """
        # SDES continuity (ADR-0053): on a secured call the re-negotiated media stays
        # RTP/SAVP + a=crypto, never silently downgrading to cleartext RTP/AVP. Plan the
        # crypto and next dialog version PURELY (no re-key, no assignment) so the answer
        # is fully built before any irreversible state is touched.
        plan = self._plan_reanswer_crypto(offer)
        next_dialog = self._dialog.with_next_sdp_version()
        answer = build_audio_offer(
            local_address=self._local_media.local_address,
            port=self._local_media.port,
            codecs=self._local_media.codecs,
            direction=direction,
            ptime=self._local_media.ptime,
            session_id=self._local_media.session_id,
            version=next_dialog.sdp_version,
            crypto=plan.render,
        )
        # Fail closed (ADR-0081): build the actual 200 first. If it cannot be built the
        # re-INVITE is unanswerable, dropped WHOLE — no SRTP re-key, no version bump.
        response = self._build_or_drop(
            lambda: build_response(
                request,
                200,
                "OK",
                extra_headers=(
                    ("Contact", self._dialog.local_contact),
                    ("Content-Type", "application/sdp"),
                ),
                body=answer,
            ),
            kind="INVITE",
        )
        if response is None:
            return False
        # Commit only now that the 200 is built: re-key the engine, advance the dialog
        # version, then send. Ordering relative to the send is unchanged (re-key first).
        await self._commit_reanswer_crypto(plan)
        self._dialog = next_dialog
        await self._signaling.send(response)
        return True

    async def _on_bye(self, request: SipRequest) -> None:
        # Build the 200 first: a header-incomplete BYE we cannot answer is dropped
        # WHOLE — the call is NOT ended and media NOT stopped (ADR-0081 drop-whole).
        response = self._build_or_drop(
            lambda: build_response(request, 200, "OK"), kind="BYE"
        )
        if response is None:
            return
        await self._signaling.send(response)
        self.ended = True
        self._wake_transfer_on_end()
        await self._media.stop()

    async def _on_notify(self, request: SipRequest) -> None:
        """Dispatch an inbound NOTIFY by its Event package (RFC 6665 §8.2.1).

        ``_on_notify`` is the target for EVERY in-dialog NOTIFY, but only an
        ``Event: refer`` NOTIFY carries a ``message/sipfrag`` transfer-progress body
        (RFC 3515). A NOTIFY for any other Event package (dialog/presence, or a
        misrouted ``message-summary`` MWI) — or one with no ``Event`` header — is NOT
        a transfer-progress notification: it gets a plain ``200 OK`` and
        ``transfer_progress`` is left untouched. Feeding such a body to
        ``parse_notify_sipfrag`` would (mis)answer the peer's legitimate NOTIFY
        ``400 Bad Request`` when its body lacks a ``SIP/2.0`` status-line, or
        silently corrupt ``transfer_progress`` if the first line happened to look
        like one.

        The Event-type token is the part before any ``;`` parameters, compared
        case-insensitively (RFC 6665 §8.2.1: event-type is case-insensitive). For the
        ``refer`` package, RFC 3515 correlates an implicit subscription with
        ``Event: refer;id=<REFER-CSeq>``. While a transfer is armed, a present ``id``
        must match that transfer's REFER CSeq before progress/event state is updated; a
        stale subscription's non-matching NOTIFY is still a valid request and receives
        ``200 OK``. An omitted ``id`` is honoured best-effort for compatibility.

        Only the ``refer`` package reaches ``parse_notify_sipfrag``, which raises
        :class:`ReferError` on a NOTIFY missing ``Subscription-State`` or whose body
        has no ``SIP/2.0`` status-line. That is a malformed PEER message, not our
        bug — it is answered ``400 Bad Request`` and the error never propagates.
        Propagation would reach the transport read loop and tear down the ENTIRE
        signalling connection (and its registration), dropping every concurrent call
        on it: a one-message DoS (rule 37 nuance: a malformed inbound message is
        answered, not fatal).
        """
        event = request.header("Event")
        package = event.split(";", 1)[0].strip().lower() if event is not None else ""
        if event is None or package != "refer":
            await self._answer_or_drop(
                lambda: build_response(request, 200, "OK"), kind="NOTIFY"
            )
            return
        try:
            progress = parse_notify_sipfrag(request)
        except ReferError:
            await self._answer_or_drop(
                lambda: build_response(request, _BAD_REQUEST, "Bad Request"),
                kind="NOTIFY",
            )
            return
        # Build the 200 BEFORE recording the progress: a NOTIFY we cannot acknowledge
        # (header-incomplete) must not update transfer_progress (ADR-0081 drop-whole).
        response = self._build_or_drop(
            lambda: build_response(request, 200, "OK"), kind="NOTIFY"
        )
        if response is None:
            return
        active_cseq = self._active_refer_cseq
        event_id_present, event_id = _refer_event_id(event)
        # RFC 3515 §2.4.6: a refer subscription's Event ``id`` is the CSeq NUMBER
        # of the REFER that created it. If an outcome wait is armed, a present id must
        # match the active REFER before it can mutate/wake that transfer; a late NOTIFY
        # from a timed-out prior subscription is still valid and receives 200 below.
        # Some peers omit ``id`` entirely. With only one transfer armed, honour an
        # id-less NOTIFY best-effort; residual: an id-less stale report cannot be
        # distinguished from the active subscription and may still contaminate it.
        matches_active = (
            active_cseq is None or not event_id_present or event_id == active_cseq
        )
        if matches_active:
            self.transfer_progress = progress
            # ADR-0109: a terminated NOTIFY wakes the bounded wait ONLY when it carries
            # a DEFINITIVE final status (>= 200). A terminated subscription reporting a
            # non-final ``1xx`` (e.g. ``100 Trying``) is not a real transfer outcome
            # (codex round-4) and is never latched here; interim updates never wake.
            if (
                progress.terminated
                and progress.status_code >= _PROVISIONAL_CEILING
                and self._transfer_terminal is not None
            ):
                # P1-b: latch this outcome the instant we wake the wait (first waker
                # wins), so a BYE arriving right after cannot overwrite it.
                if not self._transfer_outcome_latched:
                    self._transfer_outcome_latched = True
                    self._transfer_outcome_progress = progress
                self._transfer_terminal.set()
        await self._signaling.send(response)

    async def _on_refer(self, request: SipRequest) -> None:
        """Answer an inbound REFER; 202 only once it parses, else a 4xx.

        ``parse_refer`` raises :class:`ReferError` when the REFER lacks exactly one
        ``Refer-To`` or its target fails the injection guard (foreign-host hijack,
        ``?``-header form, control char, …). A malformed/hostile REFER is answered
        ``400 Bad Request`` and the handler never runs — propagating the error would
        reach the transport read loop and drop the WHOLE signalling connection (a
        one-message DoS). Crucially, the ``202 Accepted`` is sent ONLY AFTER a
        successful parse: a REFER whose injection guard fails is rejected outright,
        never accepted-then-abandoned (rule 37 nuance: a malformed inbound message
        is answered, not fatal).
        """
        try:
            refer = parse_refer(request)
        except ReferError:
            await self._answer_or_drop(
                lambda: build_response(request, _BAD_REQUEST, "Bad Request"),
                kind="REFER",
            )
            return
        # Build the 202 BEFORE invoking the transfer handler: a REFER we cannot accept
        # (header-incomplete) must not trigger the transfer (ADR-0081 drop-whole).
        response = self._build_or_drop(
            lambda: build_response(request, 202, "Accepted"), kind="REFER"
        )
        if response is None:
            return
        await self._signaling.send(response)
        if self._refer_handler is not None:
            await self._refer_handler(refer)


def _refer_event_id(event: str) -> tuple[bool, int | None]:
    """Parse RFC 3515 ``Event: refer;id=<REFER-CSeq>`` correlation.

    Returns ``(False, None)`` when ``id`` is omitted (the best-effort compatibility
    path), ``(True, n)`` for exactly one valid CSeq number, and ``(True, None)`` for a
    present but malformed/duplicate ``id``. The latter must not be laundered into the
    id-less fallback: it cannot match an active transfer, although the NOTIFY remains a
    valid request to acknowledge with 200.
    """
    raw_ids: list[str] = []
    for parameter in event.split(";")[1:]:
        name, separator, value = parameter.partition("=")
        if separator and name.strip().lower() == "id":
            raw_ids.append(value.strip())
    if not raw_ids:
        return (False, None)
    if len(raw_ids) != 1:
        return (True, None)
    return (True, _parse_decimal(raw_ids[0], max_exclusive=_MAX_CSEQ))


def _refer_subscription_declined(response: SipResponse) -> bool:
    """RFC 4488: ``True`` when a REFER 2xx carries ``Refer-Sub: false`` (ADR-0109).

    A referee that answers the REFER with ``Refer-Sub: false`` has declined the
    implicit subscription, so no progress NOTIFY will follow — the caller skips the
    outcome wait and reports OUTCOME_UNKNOWN immediately. The header token is the part
    before any ``;``-parameters, compared case-insensitively; an absent header (the
    common case — the subscription is active) is ``False``.
    """
    value = response.header("Refer-Sub")
    if value is None:
        return False
    return value.split(";", 1)[0].strip().lower() == "false"


# RFC 3261 §8.1.1.5: a CSeq sequence number is < 2**31. Leading zeros are valid
# (`1*DIGIT`), so the digit COUNT is not bounded — int() normalizes the value.
_MAX_CSEQ = 2**31


def _cseq_number(cseq: str | None) -> int | None:
    if cseq is None:
        return None
    parts = cseq.split()
    if not parts:
        return None
    token = parts[0]
    # Fail closed (ADR-0081): a valid SIP CSeq number is ASCII `1*DIGIT` < 2**31
    # (RFC 3261 §8.1.1.5). The shared parser rejects Unicode digits and catches
    # CPython's over-long-int ValueError while preserving leading-zero value
    # semantics. on_response/_send_ack run OUTSIDE the reader's parse-only
    # `except ValueError`, so an escape would tear down the whole connection — every
    # non-conformant token instead returns None (the uncorrelatable path a
    # non-numeric CSeq already takes).
    return _parse_decimal(token, max_exclusive=_MAX_CSEQ)
