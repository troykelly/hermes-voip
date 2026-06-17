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
    "DialogConsumer",
    "InDialog",
    "NewCall",
    "RegistrationManager",
    "RegistrationStatus",
    "RequestRouting",
    "SipTransport",
    "Unroutable",
]

_DEFAULT_REFRESH_FRACTION = 0.5
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
class Unroutable:
    """A request that matches no registration or active dialog."""

    request: SipRequest
    reason: str


type RequestRouting = NewCall | InDialog | Unroutable


@dataclass(slots=True)
class _FlowState:
    """One extension's flow plus its live registration status."""

    extension: ExtensionConfig
    flow: RegistrationFlow
    registered: bool = False
    expires: int | None = None
    refresh_task: asyncio.Task[None] | None = field(default=None)
    last_error: BaseException | None = None


class RegistrationManager:
    """Owns N registration flows over one transport and demuxes inbound SIP."""

    def __init__(
        self,
        gateway: GatewayConfig,
        transport: SipTransport,
        *,
        refresh_fraction: float = _DEFAULT_REFRESH_FRACTION,
        on_registration_error: Callable[[str, BaseException], None] | None = None,
    ) -> None:
        """Build one flow per configured extension (no IO until :meth:`start`).

        ``on_registration_error`` is invoked (extension, error) when a background
        refresh fails, so a flapping registration is observed, never swallowed.
        """
        self._gateway = gateway
        self._transport = transport
        self._refresh_fraction = refresh_fraction
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
        """Cancel every refresh task; the transport is closed by its owner.

        Each task's done callback (:meth:`_on_refresh_done`) has already observed
        any real failure; the ``gather`` here only drains the resulting
        cancellations.
        """
        tasks = [
            state.refresh_task
            for state in self._flows.values()
            if state.refresh_task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for state in self._flows.values():
            state.refresh_task = None

    # --- inbound responses --------------------------------------------------

    async def on_response(self, response: SipResponse) -> None:
        """Route a REGISTER response to its flow and act on the outcome.

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
        outcome = state.flow.handle(response)
        # Challenged (401/407) and Retry (423 Interval Too Brief) both carry a
        # ready-to-send follow-up request; the rest update registration state.
        if isinstance(outcome, Challenged | Retry):
            await self._transport.send(outcome.request)
        elif isinstance(outcome, Registered):
            was_registered = state.registered
            state.registered = True
            state.expires = outcome.expires
            self._up.set()
            self._schedule_refresh(state, outcome.expires)
            # Operator-facing "gateway login is up" signal (the runbooks point at
            # this line). Log the transition to registered at INFO; subsequent
            # refreshes of an already-up registration are DEBUG to avoid noise.
            # rule 34: NEVER log the SIP host/extension/username/password — only
            # the non-sensitive expiry. With N extensions a per-line identifier
            # would be the (PII) extension number, so it is deliberately omitted;
            # the count of these lines is how many extensions came up.
            if was_registered:
                _log.debug("SIP registration refreshed (expires %ss)", outcome.expires)
            else:
                _log.info("SIP registration established (expires %ss)", outcome.expires)
        elif isinstance(outcome, Failed):
            state.registered = False
        else:
            # Exhaustive over RegistrationOutcome: a future member fails mypy here
            # (and raises at runtime), never silently dropped (rule 37).
            assert_never(outcome)

    def _schedule_refresh(self, state: _FlowState, expires: int) -> None:
        if state.refresh_task is not None:
            state.refresh_task.cancel()
        delay = max(0.0, expires * self._refresh_fraction)
        task = asyncio.create_task(self._refresh_after(state, delay))
        state.refresh_task = task
        task.add_done_callback(lambda done: self._on_refresh_done(state, done))

    async def _refresh_after(self, state: _FlowState, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._transport.send(state.flow.start())

    def _on_refresh_done(self, state: _FlowState, task: asyncio.Task[None]) -> None:
        """Observe a finished refresh task; surface any failure (never swallow).

        A cancelled task (shutdown) is expected. Any other exception means the
        refresh could not be sent, so the registration can no longer be kept
        alive: mark it down, record the error, and report it.
        """
        if state.refresh_task is task:
            state.refresh_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        state.registered = False
        state.last_error = error
        if self._on_error is not None:
            self._on_error(state.extension.extension, error)

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
        """Demux an inbound request to a registration, a call, or nowhere."""
        to_tag = _tag(request.header("To"))
        if to_tag is not None:
            return self._route_in_dialog(request, to_tag)
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
