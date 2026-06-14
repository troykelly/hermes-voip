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

from hermes_voip.providers.guard import GuardResult


class ToolRisk(Enum):
    """Action risk class for a registered tool (ascending)."""

    SAFE = "safe"  # read-only / no side effects
    ELEVATED = "elevated"  # mutating but reversible / low blast radius
    IRREVERSIBLE = "irreversible"  # payments, bookings, transfers, account mutation


@dataclass(slots=True)
class GuardSessionState:
    """Per-session guard state; lives for the call (ADR-0009).

    Attributes:
        call_id: The call/session this state belongs to.
        degraded: True once any fail-open screen occurred; never un-sets in-call.
        flagged_turns: Identifiers of turns flagged for audit during the call.
    """

    call_id: str
    degraded: bool = False
    flagged_turns: tuple[str, ...] = field(default_factory=tuple)

    def record(self, result: GuardResult) -> None:
        """Fold one screen into session state; ``degraded`` never un-sets in-call."""
        self.degraded = self.degraded or result.degraded


def gate_tool_call(
    risk: ToolRisk, state: GuardSessionState, *, confirmed: bool
) -> bool:
    """Decide whether a tool may run (``pre_tool_call`` policy).

    An ``IRREVERSIBLE`` tool requires explicit confirmation (human/DTMF,
    ADR-0010) and is hard-blocked while the session is ``degraded`` — even if
    the classifier returned ALLOW (the miss case ADR-0009 tests for). An
    ``ELEVATED`` tool is blocked while ``degraded``. ``SAFE`` tools always run.
    This never silently allows (rule 37): the decision is total over ``ToolRisk``.

    Args:
        risk: The action risk class of the tool.
        state: The per-session guard state.
        confirmed: Whether the caller explicitly confirmed the action.

    Returns:
        True if the tool may run, else False.
    """
    if risk is ToolRisk.IRREVERSIBLE:
        return confirmed and not state.degraded
    if risk is ToolRisk.ELEVATED:
        return not state.degraded
    if risk is ToolRisk.SAFE:
        return True
    assert_never(
        risk
    )  # exhaustive: a new ToolRisk member fails mypy here, not silently allows
