"""The registration manager: N flows over one transport, plus inbound demux.

ADR-0011 §1. A single :class:`RegistrationManager` owns one
:class:`~hermes_voip.registration.RegistrationFlow` per configured extension,
sends their REGISTERs over a shared :class:`SipTransport`, refreshes each before
expiry, and is the **inbound demux hub** (the load-bearing routing):

* a REGISTER **response** routes to the owning flow by **Call-ID**;
* a new **INVITE** routes to the owning registration by **Request-URI user-part**
  (the registrar retargets to the registered Contact, so the user-part is the
  extension), falling back to the ``To`` AOR user-part, then the configured
  **default** registration;
* an **in-dialog** request routes to the owning call by dialog key
  ``(Call-ID, local-tag, remote-tag)``.

It owns no socket: signalling IO is the injected :class:`SipTransport`, and the
call media plane is each :class:`DialogConsumer` (a ``CallSession``). This keeps
the demux — invariant 2 (registration and call have independent Call-ID/CSeq
spaces) — testable against fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, assert_never, runtime_checkable

from hermes_voip.config import ExtensionConfig, GatewayConfig
from hermes_voip.message import SipRequest, SipResponse
from hermes_voip.registration import (
    Challenged,
    Failed,
    Registered,
    RegistrationFlow,
    Retry,
)

_log = logging.getLogger(__name__)

__all__ = [
    "Cancel",
    "DialogConsumer",
    "InDialog",
    "NewCall",
    "RegistrationError",
    "RegistrationManager",
    "RegistrationRejectedError",
    "RegistrationStatus",
    "RegistrationTimeoutError",
    "RequestRouting",
    "SipTransport",
    "Unroutable",
]


class RegistrationError(Exception):
    """Base class for a recoverable registration-keep-alive failure."""


class RegistrationRejectedError(RegistrationError):
    """A refresh REGISTER was rejected with a final 4xx/5xx/6xx status."""

    def __init__(self, status: int, reason: str) -> None:
        """Carry the rejecting status/reason (no SIP host/extension/secret)."""
        super().__init__(f"registration refresh rejected: {status} {reason}")
        self.status = status
        self.reason = reason


class RegistrationTimeoutError(RegistrationError):
    """A refresh REGISTER got no response within the Timer-F/B deadline."""

    def __init__(self) -> None:
        """A response-deadline timeout (the registrar never answered)."""
        super().__init__("registration refresh timed out with no response")


_DEFAULT_REFRESH_FRACTION = 0.5
# The smallest refresh delay we will ever arm. A registrar may grant a tiny (but
# positive) lifetime; ``expires * refresh_fraction`` could then be sub-second (or,
# for a 0 grant that slipped past the registration-flow guard, 0), arming a refresh
# task that re-REGISTERs almost immediately and hot-loops the gateway. Flooring the
# delay here guarantees no near-zero-delay refresh is ever scheduled (defence in
# depth alongside the flow-level non-positive-grant rejection).
_MIN_REFRESH_DELAY = 1.0
# RFC 3261 Timer-F/B analogue: how long to wait for a response to a REGISTER
# before treating it as failed. Over reliable TLS there are no retransmissions,
# so this is a single overall response deadline (the §17.1.1.2 32 s default).
_DEFAULT_REFRESH_TIMEOUT = 32.0
# Registration-recovery backoff (mirrors the transport reconnect supervisor):
# exponential from this initial delay, capped, with ±20% decorrelation jitter.
_DEFAULT_RETRY_BACKOFF = 1.0
_RETRY_BACKOFF_CAP = 30.0
_RETRY_JITTER = 0.2
_TAG_PARAM = re.compile(r";\s*tag=([^;,\s]+)", re.IGNORECASE)
_ANGLE_ADDR = re.compile(r"<([^>]*)>")


@runtime_checkable
class SipTransport(Protocol):
    """The SIP signalling seam the manager drives (ADR-0005 implements it)."""

    async def send(self, message: str) -> None:
        """Send one SIP message (request or response) over the wire."""
        ...

    @property
    def local_sent_by(self) -> str:
        """The Via ``sent-by`` (local host:port, or an ``.invalid`` for WSS)."""
        ...

    def contact_uri(self, extension: str) -> str:
        """The ``Contact`` header value for ``extension`` on this transport."""
        ...


@runtime_checkable
class DialogConsumer(Protocol):
    """A live call (``CallSession``) that consumes its in-dialog requests."""

    async def handle_request(self, request: SipRequest) -> None:
        """Handle one in-dialog request (re-INVITE / REFER / NOTIFY / BYE)."""
        ...


@dataclass(frozen=True, slots=True)
class RegistrationStatus:
    """A point-in-time view of one registration, for ``list_registrations``."""

    extension: str
    index: int
    registered: bool
    expires: int | None


@dataclass(frozen=True, slots=True)
class NewCall:
    """An out-of-dialog INVITE owned by ``registration``."""

    registration: ExtensionConfig
    invite: SipRequest


@dataclass(frozen=True, slots=True)
class InDialog:
    """An in-dialog request owned by an active call's ``consumer``."""

    consumer: DialogConsumer
    request: SipRequest


@dataclass(frozen=True, slots=True)
class Cancel:
    """An out-of-dialog CANCEL targeting a pending INVITE server transaction.

    RFC 3261 §9.2: a CANCEL abandons an in-progress INVITE. It is neither a new
    call nor an in-dialog request — the transport matches it to the pending
    INVITE by its top Via branch and answers the CANCEL ``200 OK`` while the
    INVITE is replied ``487 Request Terminated``.
    """

    request: SipRequest


@dataclass(frozen=True, slots=True)
class Unroutable:
    """A request that matches no registration or active dialog."""

    request: SipRequest
    reason: str


type RequestRouting = NewCall | InDialog | Cancel | Unroutable


@dataclass(slots=True)
class _FlowState:
    """One extension's flow plus its live registration status.

    ``established`` is sticky once the extension first registers: it stays True
    across a refresh failure and its recovery so the recovery loop (re-REGISTER
    with backoff) keeps running until the registrar comes back — distinct from a
    cold-start REGISTER failure, which is left to ``connect()``/the transport
    supervisor. ``deregistering`` suppresses recovery for a deliberate
    de-registration (a Failed there is an expected end, not an outage).
    """

    extension: ExtensionConfig
    flow: RegistrationFlow
    registered: bool = False
    established: bool = False
    deregistering: bool = False
    expires: int | None = None
    refresh_task: asyncio.Task[None] | None = field(default=None)
    response_timeout_task: asyncio.Task[None] | None = field(default=None)
    recovery_task: asyncio.Task[None] | None = field(default=None)
    recovery_attempt: int = 0
    last_error: BaseException | None = None


class RegistrationManager:
    """Owns N registration flows over one transport and demuxes inbound SIP."""

    def __init__(  # noqa: PLR0913 — gateway + transport plus three keyword-only refresh/recovery tuning knobs and one observer callback; all defaulted and keyword-only
        self,
        gateway: GatewayConfig,
        transport: SipTransport,
        *,
        refresh_fraction: float = _DEFAULT_REFRESH_FRACTION,
        refresh_timeout: float = _DEFAULT_REFRESH_TIMEOUT,
        retry_backoff: float = _DEFAULT_RETRY_BACKOFF,
        min_refresh_delay: float = _MIN_REFRESH_DELAY,
        on_registration_error: Callable[[str, BaseException], None] | None = None,
    ) -> None:
        """Build one flow per configured extension (no IO until :meth:`start`).

        ``on_registration_error`` is invoked (extension, error) when a background
        refresh fails, so a flapping registration is observed, never swallowed.

        ``refresh_timeout`` is the per-REGISTER response deadline (RFC 3261 Timer
        F/B analogue): a refresh that gets no response within it is treated as
        failed rather than left ``registered`` forever. ``retry_backoff`` is the
        initial recovery delay for a re-REGISTER after a failed refresh
        (exponential, capped at 30 s, with ±20% jitter).

        ``min_refresh_delay`` floors the scheduled refresh delay so a tiny granted
        lifetime cannot arm a near-zero-delay refresh that hot-loops the registrar
        (ADR-0088). It MUST be a positive number of seconds: a ``0`` or negative
        floor would defeat the very guard it exists to provide, so it is rejected at
        construction (codex MUST-FIX 2) — the public knob can never be set to a
        guard-defeating value. Tests that need an immediate hand-driven refresh
        reach past it via the private ``_min_refresh_delay`` attribute, never this
        knob.

        Raises:
            ValueError: If ``min_refresh_delay`` is not strictly positive. The check
                is ``not (min_refresh_delay > 0)``, which fails closed on ``NaN`` too
                (``nan > 0`` is False) — a NaN floor would otherwise slip a naive
                ``<= 0`` test and poison ``max(nan, …)`` / ``asyncio.sleep(nan)``.
        """
        # ``not (… > 0)`` rather than ``<= 0`` so the check fails closed on NaN:
        # ``nan <= 0`` is False (NaN would slip the floor) but ``nan > 0`` is also
        # False, so ``not (nan > 0)`` is True and NaN is rejected with 0/negatives.
        if not (min_refresh_delay > 0):
            msg = f"min_refresh_delay must be > 0 seconds, got {min_refresh_delay}"
            raise ValueError(msg)
        self._gateway = gateway
        self._transport = transport
        self._refresh_fraction = refresh_fraction
        self._refresh_timeout = refresh_timeout
        self._retry_backoff = retry_backoff
        self._min_refresh_delay = min_refresh_delay
        self._on_error = on_registration_error
        self._flows: dict[str, _FlowState] = {}
        self._by_extension: dict[str, _FlowState] = {}
        self._calls: dict[tuple[str, str, str], DialogConsumer] = {}
        self._up = asyncio.Event()
        for ext in gateway.extensions:
            config = gateway.registration_config(
                ext,
                contact=transport.contact_uri(ext.extension),
                local_sent_by=transport.local_sent_by,
            )
            flow = RegistrationFlow(config)
            state = _FlowState(extension=ext, flow=flow)
            self._flows[flow.call_id] = state
            self._by_extension[ext.extension] = state

    # --- lifecycle ----------------------------------------------------------

    @property
    def registration_call_ids(self) -> frozenset[str]:
        """The Call-IDs the transport routes REGISTER responses by."""
        return frozenset(self._flows)

    @property
    def is_up(self) -> bool:
        """``True`` while at least one extension is registered (degraded-up)."""
        return any(state.registered for state in self._flows.values())

    def snapshot(self) -> tuple[RegistrationStatus, ...]:
        """The current per-registration status, ordered by extension index."""
        return tuple(
            RegistrationStatus(
                extension=state.extension.extension,
                index=state.extension.index,
                registered=state.registered,
                expires=state.expires,
            )
            for state in sorted(self._flows.values(), key=lambda s: s.extension.index)
        )

    async def start(self) -> None:
        """Send the initial REGISTER for every flow."""
        for state in self._flows.values():
            await self._transport.send(state.flow.start())

    async def connect(self, *, timeout: float = 10.0) -> bool:
        """Register all extensions; return once at least one is up or timeout.

        Returns ``True`` if at least one extension registered within ``timeout``
        (degraded-but-up is up), ``False`` otherwise.
        """
        await self.start()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._up.wait(), timeout)
        return self.is_up

    async def aclose(self) -> None:
        """Cancel every background task; the transport is closed by its owner.

        Each task's done callback has already observed any real failure; the
        ``gather`` here only drains the resulting cancellations. The refresh, the
        Timer-F/B response-timeout, and the recovery re-REGISTER tasks are all
        cancelled so none fires after shutdown.
        """
        tasks = [
            task
            for state in self._flows.values()
            for task in (
                state.refresh_task,
                state.response_timeout_task,
                state.recovery_task,
            )
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for state in self._flows.values():
            state.refresh_task = None
            state.response_timeout_task = None
            state.recovery_task = None

    # --- inbound responses --------------------------------------------------

    async def on_response(self, response: SipResponse) -> None:
        """Route a REGISTER response to its flow and act on the outcome.

        A response for a flow disarms that flow's in-flight response timeout
        (RFC 3261 Timer F/B) — the registrar answered, so the deadline is moot.

        Raises:
            KeyError: if no flow owns the response's Call-ID (the transport must
                route only registration responses here; call responses go to the
                owning :class:`DialogConsumer`).
        """
        call_id = response.header("Call-ID")
        if call_id is None or call_id not in self._flows:
            msg = f"no registration flow for Call-ID {call_id!r}"
            raise KeyError(msg)
        state = self._flows[call_id]
        # The registrar responded: cancel the Timer-F/B deadline for this REGISTER.
        self._cancel_response_timeout(state)
        outcome = state.flow.handle(response)
        # Challenged (401/407) and Retry (423 Interval Too Brief) both carry a
        # ready-to-send follow-up REGISTER; the rest update registration state.
        if isinstance(outcome, Challenged | Retry):
            # The follow-up is itself a REGISTER awaiting a response — re-arm the
            # response deadline so a dropped re-auth/retry also recovers.
            await self._send_register(state, outcome.request)
        elif isinstance(outcome, Registered):
            was_registered = state.registered
            state.registered = True
            state.established = True
            state.expires = outcome.expires
            state.recovery_attempt = 0  # a success clears the backoff ramp
            self._up.set()
            self._schedule_refresh(state, outcome.expires)
            # Operator-facing "gateway login is up" signal (the runbooks point at
            # this line). Log the transition to registered at INFO; subsequent
            # refreshes of an already-up registration are DEBUG to avoid noise.
            # rule 34: NEVER log the SIP host/extension/username/password — only
            # the non-sensitive expiry. With N extensions a per-line identifier
            # would be the (PII) extension number, so it is deliberately omitted;
            # the count of these lines is how many extensions came up.
            extra = {
                "expires_s": outcome.expires,
            }
            if was_registered:
                _log.debug(
                    "SIP registration refreshed (expires %ss)",
                    outcome.expires,
                    extra={"event": "sip_registration_refreshed", **extra},
                )
            else:
                _log.info(
                    "SIP registration established (expires %ss)",
                    outcome.expires,
                    extra={"event": "sip_registration_established", **extra},
                )
        elif isinstance(outcome, Failed):
            self._on_registration_failed(
                state,
                RegistrationRejectedError(outcome.status, outcome.reason),
            )
        else:
            # Exhaustive over RegistrationOutcome: a future member fails mypy here
            # (and raises at runtime), never silently dropped (rule 37).
            assert_never(outcome)

    # --- refresh + recovery (RFC 3261 §10 keep-alive) -----------------------

    def _schedule_refresh(self, state: _FlowState, expires: int) -> None:
        if state.refresh_task is not None:
            state.refresh_task.cancel()
        # Floor the delay to a positive minimum so a tiny (or, defensively, a
        # non-positive) granted lifetime never arms a near-zero-delay refresh that
        # hot-loops the registrar (a non-positive grant is already rejected upstream
        # as a Failed outcome, so a refresh is normally only scheduled for a positive
        # grant; this guards the remaining tiny-positive case and any future caller).
        delay = max(self._min_refresh_delay, expires * self._refresh_fraction)
        task = asyncio.create_task(self._refresh_after(state, delay))
        state.refresh_task = task
        task.add_done_callback(lambda done: self._on_refresh_done(state, done))

    async def _refresh_after(self, state: _FlowState, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._send_register(state, state.flow.start())

    async def _send_register(self, state: _FlowState, request: str) -> None:
        """Send a REGISTER and arm its RFC 3261 Timer-F/B response deadline.

        The deadline fires only if no response for this flow arrives within
        ``refresh_timeout`` (:meth:`on_response` cancels it on any response), so a
        refresh that the registrar simply never answers is treated as a failure
        and recovered, never left ``registered`` forever.
        """
        await self._transport.send(request)
        self._arm_response_timeout(state)

    def _arm_response_timeout(self, state: _FlowState) -> None:
        self._cancel_response_timeout(state)
        if self._refresh_timeout <= 0:
            return  # disabled (e.g. tests that drive responses synchronously)
        task = asyncio.create_task(self._response_timeout_after(state))
        state.response_timeout_task = task
        task.add_done_callback(lambda done: self._on_background_task_done(state, done))

    def _cancel_response_timeout(self, state: _FlowState) -> None:
        task = state.response_timeout_task
        state.response_timeout_task = None
        if task is not None:
            task.cancel()

    async def _response_timeout_after(self, state: _FlowState) -> None:
        await asyncio.sleep(self._refresh_timeout)
        # No response cleared us in time: the binding may already be gone at the
        # registrar. Treat it as a failure and recover, rather than trusting a
        # 'registered' flag that no longer reflects reality (Timer F/B).
        state.response_timeout_task = None
        self._on_registration_failed(state, RegistrationTimeoutError())

    def _on_registration_failed(self, state: _FlowState, error: BaseException) -> None:
        """Mark a flow down on a failed REGISTER, report it, and recover.

        Recovery (a bounded-backoff re-REGISTER) runs only for an extension that
        was actually established — a refresh/keep-alive outage. A cold-start
        REGISTER that never succeeded, or a deliberate de-registration, is NOT
        retried here (those are ``connect()``'s / a clean teardown's concern).

        Emits a structured WARNING log event ``sip_registration_failed`` on every
        failure so operators can query registration rejects, timeouts, and
        transport send failures without parsing free-text logs. The event carries
        only non-sensitive fields: ``outcome`` (``"rejected"`` | ``"timeout"`` |
        ``"transport_failed"``), ``status_code`` (the SIP integer for a
        rejection, ``None`` otherwise), and ``attempt`` (the 1-based recovery
        attempt counter). NEVER logs the SIP host, realm, extension, username,
        or password (rule 34).
        """
        state.registered = False
        state.last_error = error
        # Classify the failure for the structured event before bumping the counter.
        outcome: str
        status_code: int | None
        if isinstance(error, RegistrationRejectedError):
            outcome = "rejected"
            status_code = error.status
        elif isinstance(error, RegistrationTimeoutError):
            outcome = "timeout"
            status_code = None
        else:
            # Transport or task-execution failures mean the REGISTER never made it
            # onto the wire or recovery could not keep running. Report only the
            # failure category, never transport/host detail.
            outcome = "transport_failed"
            status_code = None
        # recovery_attempt is incremented in _schedule_recovery (called below for
        # established flows), so it is 0 on the very first failure of a cold-start
        # flow. We add 1 here to produce a 1-based attempt count that is meaningful
        # whether or not recovery is scheduled.
        attempt = state.recovery_attempt + 1
        _log.warning(
            "SIP registration failed: %s (status=%s, attempt=%d)",
            outcome,
            status_code,
            attempt,
            extra={
                "event": "sip_registration_failed",
                "outcome": outcome,
                "status_code": status_code,
                "attempt": attempt,
            },
        )
        if self._on_error is not None:
            self._on_error(state.extension.extension, error)
        if state.established and not state.deregistering:
            self._schedule_recovery(state)

    def _schedule_recovery(self, state: _FlowState) -> None:
        """Schedule a re-REGISTER after exponential-with-jitter backoff."""
        if state.recovery_task is not None:
            state.recovery_task.cancel()
        delay = min(
            _RETRY_BACKOFF_CAP,
            self._retry_backoff * (2**state.recovery_attempt),
        )
        # ±20% decorrelation jitter so N extensions failing together don't
        # re-REGISTER in lockstep (a thundering herd at the registrar).
        jitter = 1.0 + random.uniform(-_RETRY_JITTER, _RETRY_JITTER)  # noqa: S311 — decorrelation jitter, not cryptographic
        state.recovery_attempt += 1
        task = asyncio.create_task(self._recover_after(state, max(0.0, delay * jitter)))
        state.recovery_task = task
        task.add_done_callback(lambda done: self._on_background_task_done(state, done))

    async def _recover_after(self, state: _FlowState, delay: float) -> None:
        await asyncio.sleep(delay)
        state.recovery_task = None
        # A fresh REGISTER transaction; its response drives on_response back to
        # Registered (recovered) or another Failed/timeout (backoff again).
        await self._send_register(state, state.flow.start())

    def _on_refresh_done(self, state: _FlowState, task: asyncio.Task[None]) -> None:
        """Observe a finished refresh task; surface any failure (never swallow).

        A cancelled task (shutdown) is expected. Any other exception means the
        refresh REGISTER could not even be sent (the transport write failed), so
        the registration can no longer be kept alive: fail it (which reports and,
        for an established extension, schedules recovery).
        """
        if state.refresh_task is task:
            state.refresh_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        self._on_registration_failed(state, error)

    def _on_background_task_done(
        self, state: _FlowState, task: asyncio.Task[None]
    ) -> None:
        """Surface a crash in a timeout/recovery task and keep recovery alive.

        A cancelled task (shutdown) is expected. Any other exception means the
        task could not complete — most often a transport ``send`` raising inside
        :meth:`_recover_after` (the registrar/socket is still down). Routing it
        through :meth:`_on_registration_failed` reports it AND reschedules the
        next bounded-backoff attempt for an established flow, so a send failure
        mid-recovery does not dead-end the registration (codex finding); it is
        never lost as an un-retrieved task exception (rule 37).
        """
        # Drop the finished task's handle so the reschedule below does not try to
        # cancel an already-completed task.
        if state.recovery_task is task:
            state.recovery_task = None
        if state.response_timeout_task is task:
            state.response_timeout_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        self._on_registration_failed(state, error)

    # --- inbound requests (demux) -------------------------------------------

    def add_call(
        self, dialog_id: tuple[str, str, str], consumer: DialogConsumer
    ) -> None:
        """Register an active call so its in-dialog requests route to it."""
        self._calls[dialog_id] = consumer

    def remove_call(self, dialog_id: tuple[str, str, str]) -> None:
        """Forget an ended call; its in-dialog requests become unroutable."""
        self._calls.pop(dialog_id, None)

    def route_request(self, request: SipRequest) -> RequestRouting:
        """Demux an inbound request to a registration, a call, a CANCEL, or nowhere.

        A CANCEL (RFC 3261 §9.2) targets a pending INVITE *transaction* and is
        matched by Via branch at the transport, not by dialog — so it is
        classified :class:`Cancel` here (no To-tag: it predates the dialog) and
        the transport resolves the matching INVITE and emits the 200/487.
        """
        to_tag = _tag(request.header("To"))
        if to_tag is not None:
            return self._route_in_dialog(request, to_tag)
        if request.method == "CANCEL":
            return Cancel(request)
        if request.method != "INVITE":
            return Unroutable(request, f"out-of-dialog {request.method}")
        return self._route_new_invite(request)

    def _route_in_dialog(self, request: SipRequest, to_tag: str) -> RequestRouting:
        call_id = request.header("Call-ID")
        from_tag = _tag(request.header("From"))
        if call_id is None or from_tag is None:
            return Unroutable(request, "in-dialog request missing Call-ID/From-tag")
        # Our local tag is the To-tag; the peer's remote tag is the From-tag.
        consumer = self._calls.get((call_id, to_tag, from_tag))
        if consumer is None:
            return Unroutable(request, "no matching dialog")
        return InDialog(consumer, request)

    def _route_new_invite(self, request: SipRequest) -> RequestRouting:
        candidates = (
            _uri_user(request.request_uri),
            _uri_user(_addr_spec(request.header("To") or "")),
            self._gateway.default_extension.extension,
        )
        for user in candidates:
            if user is not None and user in self._by_extension:
                return NewCall(self._by_extension[user].extension, request)
        # Unreachable: the default extension is always configured.
        return Unroutable(request, "no registration")


def _tag(header_value: str | None) -> str | None:
    if header_value is None:
        return None
    # The tag is a header parameter, after the closing '>' of a name-addr.
    if ">" in header_value:
        search_space = header_value.split(">", 1)[1]
    else:
        search_space = header_value
    match = _TAG_PARAM.search(search_space)
    return match.group(1) if match is not None else None


def _addr_spec(value: str) -> str:
    match = _ANGLE_ADDR.search(value)
    if match is not None:
        return match.group(1).strip()
    return value.split(";", 1)[0].strip()


def _uri_user(uri: str) -> str | None:
    if not uri:
        return None
    _scheme, sep, rest = uri.partition(":")
    if not sep:
        return None
    userhost = rest.split(";", 1)[0]
    user, at, _host = userhost.partition("@")
    return user if at and user else None
