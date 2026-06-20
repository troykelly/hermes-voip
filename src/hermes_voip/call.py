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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Protocol, runtime_checkable

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
    ReferRequest,
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

_PROVISIONAL_CEILING = 200  # a status below this is a 1xx provisional response
_UNAUTHORIZED = 401
_PROXY_AUTH_REQUIRED = 407
_FIRST_ERROR_STATUS = 300
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
        """
        async with self._lock:
            if self.ended:
                return
            request = build_in_dialog_request(self._dialog, "BYE")
            self._dialog = request.dialog
            self.ended = True
            await self._signaling.send(request.text)
            await self._media.stop()

    async def transfer_blind(
        self, target_uri: str, *, referred_by: str | None = None
    ) -> None:
        """Blind-transfer the caller to ``target_uri`` (REFER); progress via NOTIFY."""
        async with self._lock:
            await self._refer(
                lambda auth: build_blind_refer(
                    self._dialog, target_uri, referred_by=referred_by, auth=auth
                )
            )

    async def transfer_attended(
        self, consult: Dialog, *, referred_by: str | None = None
    ) -> None:
        """Attended-transfer the caller to the ``consult`` peer (REFER + Replaces)."""
        async with self._lock:
            await self._refer(
                lambda auth: build_attended_refer(
                    self._dialog, consult, referred_by=referred_by, auth=auth
                )
            )

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

    async def _reanswer_crypto(
        self, offer: SessionDescription | None
    ) -> CryptoAttribute | None:
        """Pick the SDES ``a=crypto`` for a re-INVITE answer/offer WE emit in a 200.

        - Peer re-offer (``offer`` set) on a secured call: mint our fresh answer key
          echoing the OFFERED tag + suite and re-key the engine — inbound from the
          peer's key, outbound from ours (RFC 4568 §6.1). A plain-call peer offer is
          answered plain. (A secured call's downgrade re-offer never reaches here — it
          is rejected 488 upstream by :meth:`_peer_reoffer_is_downgrade`.)
        - Offerless re-INVITE (``offer`` is ``None``) on a secured call: WE offer, but
          re-advertise the call's ESTABLISHED key (no rotation, no re-key). The peer's
          answer rides the ACK, which this dialog does not parse for SDP; re-using the
          current key keeps BOTH directions on their agreed keys, so continuity holds
          without consuming the ACK answer. A plain call offers plain.

        Returns the crypto to render, or ``None`` for a plain answer.
        """
        if offer is None:
            # Offerless: re-advertise the current key unchanged (no fresh mint, no
            # engine re-key) so there is no dependency on the peer's ACK-borne answer.
            return self._local_media.crypto
        audio = offer.audio
        if self._local_media.crypto is None or audio is None or not audio.is_srtp:
            return None
        if not audio.crypto_attrs:
            return None
        peer_crypto = audio.crypto_attrs[0]
        answer_crypto = generate_answer_crypto(peer_crypto)
        await self._media.rekey_srtp(inbound=peer_crypto, outbound=answer_crypto)
        self._adopt_local_crypto(answer_crypto)
        return answer_crypto

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
    ) -> None:
        result = build(None)
        self._dialog = result.dialog
        response = await self._send_and_await_final(
            result.text, self._dialog.local_cseq
        )
        if response.status_code in (_UNAUTHORIZED, _PROXY_AUTH_REQUIRED):
            auth = self._authorization(response, "REFER")
            result = build(auth)
            self._dialog = result.dialog
            response = await self._send_and_await_final(
                result.text, self._dialog.local_cseq
            )
        if response.status_code >= _FIRST_ERROR_STATUS:
            msg = f"REFER rejected: {response.status_code} {response.reason}"
            raise CallError(msg)

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

    async def handle_request(self, request: SipRequest) -> None:
        """Answer an inbound in-dialog request (re-INVITE/REFER/NOTIFY/BYE/INFO/ACK)."""
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
            await self._signaling.send(build_response(request, 501, "Not Implemented"))

    async def _on_info(self, request: SipRequest) -> None:
        """Answer an inbound ``INFO`` (SIP INFO DTMF receive, ADR-0036).

        Every in-dialog ``INFO`` is acknowledged ``200 OK`` (per RFC 6086 it must get a
        final response). When the body is a DTMF relay/simple body, the parsed digit is
        surfaced through :attr:`on_dtmf` (the same router the engine's RFC 4733 /
        in-band receive feeds) — so an inbound keypad press resolves an armed
        confirmation or
        joins a menu group exactly as the other backends do. A non-DTMF INFO (e.g. a
        media-control body) is acknowledged but surfaces nothing, and an INFO with no
        bound sink is acknowledged and dropped (never crashes the dialog).
        """
        await self._signaling.send(build_response(request, 200, "OK"))
        digit = parse_dtmf_info(request.header("Content-Type") or "", request.body)
        if digit is not None and self.on_dtmf is not None:
            self.on_dtmf(digit)

    async def _on_reinvite(self, request: SipRequest) -> None:
        routing = classify_inbound_reinvite(
            request, pending_local_offer=self._local_offer_pending
        )
        if isinstance(routing, Glare):
            await self._signaling.send(build_response(request, 491, "Request Pending"))
            return
        if isinstance(routing, MediaUpdate):
            # Downgrade resistance (ADR-0053): a secured call never answers a plain
            # (no-a=crypto) re-offer with cleartext media — reject it 488 and leave
            # the established SRTP context untouched.
            if self._peer_reoffer_is_downgrade(routing.offer):
                await self._signaling.send(
                    build_response(request, 488, "Not Acceptable Here")
                )
                return
            await self._answer_reinvite(
                request, routing.answer_direction, offer=routing.offer
            )
            self.on_hold = routing.held_by_peer
            await self._media.set_hold(routing.held_by_peer)
            return
        # OfferlessReinvite: re-offer our current media direction in the 2xx.
        await self._answer_reinvite(
            request, "sendonly" if self.on_hold else "sendrecv", offer=None
        )

    async def _answer_reinvite(
        self, request: SipRequest, direction: str, *, offer: SessionDescription | None
    ) -> None:
        # SDES continuity (ADR-0053): on a secured call the re-negotiated media stays
        # RTP/SAVP + a=crypto, never silently downgrading to cleartext RTP/AVP.
        answer_crypto = await self._reanswer_crypto(offer)
        self._dialog = self._dialog.with_next_sdp_version()
        answer = build_audio_offer(
            local_address=self._local_media.local_address,
            port=self._local_media.port,
            codecs=self._local_media.codecs,
            direction=direction,
            ptime=self._local_media.ptime,
            session_id=self._local_media.session_id,
            version=self._dialog.sdp_version,
            crypto=answer_crypto,
        )
        await self._signaling.send(
            build_response(
                request,
                200,
                "OK",
                extra_headers=(
                    ("Contact", self._dialog.local_contact),
                    ("Content-Type", "application/sdp"),
                ),
                body=answer,
            )
        )

    async def _on_bye(self, request: SipRequest) -> None:
        await self._signaling.send(build_response(request, 200, "OK"))
        self.ended = True
        await self._media.stop()

    async def _on_notify(self, request: SipRequest) -> None:
        self.transfer_progress = parse_notify_sipfrag(request)
        await self._signaling.send(build_response(request, 200, "OK"))

    async def _on_refer(self, request: SipRequest) -> None:
        refer = parse_refer(request)
        await self._signaling.send(build_response(request, 202, "Accepted"))
        if self._refer_handler is not None:
            await self._refer_handler(refer)


def _cseq_number(cseq: str | None) -> int | None:
    if cseq is None:
        return None
    parts = cseq.split()
    if not parts or not parts[0].isdigit():
        return None
    return int(parts[0])
