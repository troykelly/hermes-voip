"""Agent-facing call-control tools, gated by the ADR-0009 policy (ADR-0011 §3).

The agent drives a live call through five tools: ``hold_call`` / ``resume_call``
(``ELEVATED`` — reversible), ``transfer_blind`` / ``transfer_attended``
(**``IRREVERSIBLE``**), and ``list_registrations`` (``SAFE``). Each carries a
:class:`~hermes_voip.providers.policy.ToolRisk` and runs through
``gate_tool_call`` **verbatim** — no new policy code.

**Invariant 3** (the load-bearing control): an ``IRREVERSIBLE`` transfer requires
explicit caller confirmation (DTMF/human, ADR-0010 — sourced via
:class:`ConfirmationSource`) **and** is hard-blocked while the session is
``degraded`` — even if the injection guard returned ALLOW. A classifier miss
therefore still cannot reach a transfer. When a tool is blocked the underlying
:class:`~hermes_voip.call.CallSession` verb never runs.

This module owns no Hermes-registration concern: a tool is a method returning a
:class:`ToolResult`; the adapter (``register(ctx)``) wires them to the runtime and
applies :func:`gate_voip_tool` in its ``pre_tool_call`` hook.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hermes_voip.dialog import Dialog
from hermes_voip.manager import RegistrationStatus
from hermes_voip.providers.policy import GuardSessionState, ToolRisk, gate_tool_call

# A bound call action (PEP 695 alias; ControllableCall is defined below — the
# right-hand side is evaluated lazily).
type _CallAction = Callable[[ControllableCall], Awaitable[None]]

__all__ = [
    "TOOL_RISKS",
    "CallControlTools",
    "ConfirmationSource",
    "ControllableCall",
    "RegistrationsView",
    "ToolResult",
    "gate_voip_tool",
]

# Each tool's action risk class (ADR-0011 §3); the source of truth for the gate.
TOOL_RISKS: dict[str, ToolRisk] = {
    "hold_call": ToolRisk.ELEVATED,
    "resume_call": ToolRisk.ELEVATED,
    "transfer_blind": ToolRisk.IRREVERSIBLE,
    "transfer_attended": ToolRisk.IRREVERSIBLE,
    "list_registrations": ToolRisk.SAFE,
}


def gate_voip_tool(
    tool_name: str, state: GuardSessionState, *, confirmed: bool
) -> bool:
    """Map a tool name to its risk and apply ``gate_tool_call`` (pre_tool_call).

    An unknown tool name is **denied** (fail closed, rule 37): the gate never
    silently allows an unrecognised action.
    """
    risk = TOOL_RISKS.get(tool_name)
    if risk is None:
        return False
    return gate_tool_call(risk, state, confirmed=confirmed)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of a tool call: whether it ran, and a message for the agent."""

    allowed: bool
    message: str


@runtime_checkable
class ControllableCall(Protocol):
    """The live-call control surface the tools drive (``CallSession`` satisfies it)."""

    @property
    def guard(self) -> GuardSessionState:
        """The per-call guard state the gate reads ``degraded`` from."""
        ...

    async def hold(self) -> None:
        """Place the caller on hold."""
        ...

    async def unhold(self) -> None:
        """Resume the held caller."""
        ...

    async def transfer_blind(
        self, target_uri: str, *, referred_by: str | None = None
    ) -> None:
        """Blind-transfer the caller to ``target_uri``."""
        ...

    async def transfer_attended(
        self, consult: Dialog, *, referred_by: str | None = None
    ) -> None:
        """Attended-transfer the caller to the consultation peer."""
        ...


@runtime_checkable
class RegistrationsView(Protocol):
    """The registration snapshot source for ``list_registrations``."""

    def snapshot(self) -> tuple[RegistrationStatus, ...]:
        """Return the current per-registration status."""
        ...


@runtime_checkable
class ConfirmationSource(Protocol):
    """Obtains explicit caller confirmation for an irreversible action (ADR-0010)."""

    async def confirm(self) -> bool:
        """Prompt the caller and await confirmation; return whether confirmed."""
        ...


class CallControlTools:
    """The five agent tools over the active call and the registration manager."""

    def __init__(
        self, registrations: RegistrationsView, *, confirmation: ConfirmationSource
    ) -> None:
        """Bind the tools to the registration view and the confirmation source."""
        self._registrations = registrations
        self._confirmation = confirmation
        self._call: ControllableCall | None = None

    def bind_call(self, call: ControllableCall) -> None:
        """Set the active call the hold/resume/transfer tools operate on."""
        self._call = call

    def unbind_call(self) -> None:
        """Clear the active call (e.g. when it ends)."""
        self._call = None

    async def list_registrations(self) -> ToolResult:
        """List the gateway registrations and their status (``SAFE``)."""
        # SAFE: always permitted; gate kept explicit for symmetry/auditability.
        if not gate_voip_tool(
            "list_registrations", _ambient_state(self._call), confirmed=False
        ):  # pragma: no cover - SAFE never blocks
            return ToolResult(allowed=False, message="list_registrations blocked")
        lines = [
            f"{s.extension}: {'registered' if s.registered else 'down'}"
            for s in self._registrations.snapshot()
        ]
        return ToolResult(allowed=True, message="; ".join(lines))

    async def hold_call(self) -> ToolResult:
        """Place the caller on hold (``ELEVATED``)."""
        return await self._reversible("hold_call", lambda call: call.hold(), "held")

    async def resume_call(self) -> ToolResult:
        """Resume the held caller (``ELEVATED``)."""
        return await self._reversible(
            "resume_call", lambda call: call.unhold(), "resumed"
        )

    async def transfer_blind(
        self, target_uri: str, *, referred_by: str | None = None
    ) -> ToolResult:
        """Blind-transfer the caller to ``target_uri`` (``IRREVERSIBLE``)."""
        return await self._irreversible(
            "transfer_blind",
            lambda call: call.transfer_blind(target_uri, referred_by=referred_by),
            f"transfer to {target_uri} initiated",
        )

    async def transfer_attended(
        self, consult: Dialog, *, referred_by: str | None = None
    ) -> ToolResult:
        """Attended-transfer the caller to the consultation peer (``IRREVERSIBLE``)."""
        return await self._irreversible(
            "transfer_attended",
            lambda call: call.transfer_attended(consult, referred_by=referred_by),
            "attended transfer initiated",
        )

    async def _reversible(self, tool: str, run: _CallAction, done: str) -> ToolResult:
        call = self._call
        if call is None:
            return ToolResult(allowed=False, message=f"{tool}: no active call")
        if not gate_voip_tool(tool, call.guard, confirmed=False):
            return ToolResult(
                allowed=False, message=f"{tool} blocked: the session is degraded"
            )
        await run(call)
        return ToolResult(allowed=True, message=f"call {done}")

    async def _irreversible(self, tool: str, run: _CallAction, done: str) -> ToolResult:
        call = self._call
        if call is None:
            return ToolResult(allowed=False, message=f"{tool}: no active call")
        confirmed = await self._confirmation.confirm()
        # Re-validate after the await: the confirmation was sought for THIS call,
        # so if the active call was replaced while confirmation was pending the
        # transfer must not run on the stale call nor skip the new call's gate
        # (TOCTOU). Confirmation never carries across calls.
        if self._call is not call:
            return ToolResult(
                allowed=False,
                message=f"{tool} blocked: the active call changed during confirmation",
            )
        if not gate_voip_tool(tool, call.guard, confirmed=confirmed):
            reason = "degraded session" if call.guard.degraded else "not confirmed"
            return ToolResult(allowed=False, message=f"{tool} blocked: {reason}")
        await run(call)
        return ToolResult(allowed=True, message=done)


def _ambient_state(call: ControllableCall | None) -> GuardSessionState:
    """The guard state for a call-independent SAFE tool (clean when no call)."""
    return call.guard if call is not None else GuardSessionState(call_id="")
