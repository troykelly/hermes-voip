"""The enforceable tool-policy gate (ADR-0004/0009).

The classifier has false negatives by construction, so the *enforceable* control
is this typed gate. Every registered tool carries a ``ToolRisk``; per-session
guard state carries the ``degraded`` flag (it follows the session, set by any
fail-open ``GuardResult``); and a ``pre_tool_call`` hook MUST gate every
``IRREVERSIBLE`` tool — requiring explicit human/DTMF confirmation (ADR-0010)
and hard-blocking while ``degraded`` — regardless of the classifier outcome. A
missed injection therefore still cannot reach an irreversible action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import assert_never

from hermes_voip.providers.guard import GuardResult, GuardVerdict


class ToolRisk(Enum):
    """Action risk class for a registered tool (ascending)."""

    SAFE = "safe"  # read-only / no side effects
    ELEVATED = "elevated"  # mutating but reversible / low blast radius
    IRREVERSIBLE = "irreversible"  # payments, bookings, transfers, account mutation


@dataclass(slots=True)
class GuardSessionState:
    """Per-session guard state; lives for the call (ADR-0009 / ADR-0020).

    Attributes:
        call_id: The call/session this state belongs to.
        degraded: True once any fail-open screen occurred; never un-sets in-call.
        privileged: Whether this session may use ``ELEVATED``/``IRREVERSIBLE``
            tools at all (ADR-0020 caller modes). ``True`` is the default
            (assistant / trusted ALLOW call), preserving the ADR-0009 behaviour.
            The adapter sets it to ``False`` for an **untrusted** remote party —
            the GREY receptionist and the OUTBOUND untrusted callee — and
            :func:`gate_tool_call` then hard-blocks every non-``SAFE`` tool. This
            is *least privilege*: an untrusted party's agent cannot invoke a tool
            that could reach an operator secret or mutate state, regardless of the
            persona prompt, the injection classifier verdict, or any confirmation.
        flagged_turns: Identifiers of turns flagged for audit during the call —
            one per screened turn whose verdict was not ``ALLOW`` or which failed
            open (``degraded``). A benign ``ALLOW`` adds nothing.
    """

    call_id: str
    degraded: bool = False
    privileged: bool = True
    flagged_turns: tuple[str, ...] = field(default_factory=tuple)
    _turns_seen: int = 0

    def record(self, result: GuardResult) -> None:
        """Fold one screen into session state (ADR-0009 audit + degrade tracking).

        ``degraded`` is sticky — once any fail-open turn sets it, it never un-sets
        for the rest of the call. A turn is *flagged for audit* (appended to
        ``flagged_turns`` with a per-call turn id) when it carries a non-``ALLOW``
        verdict OR it failed open; a clean ``ALLOW`` turn is not audit-worthy and
        flags nothing. The verdict is therefore honoured, not just the degrade bit.

        Args:
            result: The graded outcome of screening one caller turn.
        """
        self._turns_seen += 1
        self.degraded = self.degraded or result.degraded
        if result.verdict is not GuardVerdict.ALLOW or result.degraded:
            turn_id = f"{self.call_id}#{self._turns_seen}"
            self.flagged_turns = (*self.flagged_turns, turn_id)


def gate_tool_call(
    risk: ToolRisk, state: GuardSessionState, *, confirmed: bool
) -> bool:
    """Decide whether a tool may run (``pre_tool_call`` policy).

    An **unprivileged** session (ADR-0020: an untrusted remote party — the GREY
    receptionist or the OUTBOUND untrusted callee) is hard-blocked from every
    ``ELEVATED``/``IRREVERSIBLE`` tool, structurally and unconditionally — before
    confirmation or the ``degraded`` flag is even considered. This is *least
    privilege* as the primary defense: the agent for an untrusted party cannot
    invoke a tool that could reach an operator secret or mutate state, no matter
    what the persona prompt, the injection classifier, or a (spoofable)
    confirmation say. ``SAFE`` (read-only) tools still run so the caller is never
    dropped.

    For a **privileged** session the ADR-0009 rules apply unchanged: an
    ``IRREVERSIBLE`` tool requires explicit confirmation (human/DTMF, ADR-0010)
    and is hard-blocked while ``degraded`` — even if the classifier returned
    ALLOW (the miss case ADR-0009 tests for); an ``ELEVATED`` tool is blocked
    while ``degraded``.

    This never silently allows (rule 37): the decision is total over ``ToolRisk``.

    Args:
        risk: The action risk class of the tool.
        state: The per-session guard state.
        confirmed: Whether the caller explicitly confirmed the action.

    Returns:
        True if the tool may run, else False.
    """
    if risk is ToolRisk.SAFE:
        # Read-only tools always run, even for an untrusted/unprivileged session:
        # the caller is screened, never dropped.
        return True
    # Every non-SAFE (mutating) tool requires a privileged session (ADR-0020). An
    # untrusted remote party (receptionist / outbound callee) is clamped here
    # before confirmation or degraded are consulted — least privilege first.
    if not state.privileged:
        return False
    if risk is ToolRisk.IRREVERSIBLE:
        return confirmed and not state.degraded
    if risk is ToolRisk.ELEVATED:
        return not state.degraded
    assert_never(
        risk
    )  # exhaustive: a new ToolRisk member fails mypy here, not silently allows
