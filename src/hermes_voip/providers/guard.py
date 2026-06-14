"""Prompt-injection guard seam — the single canonical contract (ADR-0004/0009).

The verdict is **graded** (not a binary benign/injection label) so the caller
can degrade behaviour proportionally — proceed, clarify, restrict the toolset,
or refuse — and ``degraded`` records a fail-open guard so policy can clamp the
action surface even when classification was unavailable. ADR-0009 imports these
types and does not redefine them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class GuardVerdict(Enum):
    """Graded screening outcome (ascending severity)."""

    ALLOW = "allow"  # benign turn; proceed normally
    CLARIFY = "clarify"  # ambiguous; ask a clarifying question, no tools this turn
    RESTRICT = "restrict"  # weak/medium signal; proceed least-privilege (read-only)
    REFUSE = "refuse"  # strong signal; refuse the instruction, flag, escalate


@dataclass(frozen=True, slots=True)
class GuardResult:
    """The graded outcome of screening one transcribed caller turn.

    Attributes:
        verdict: The graded screening outcome.
        normalized_text: Text after decode/normalise (base64/ROT13/homoglyph).
        reasons: Audit-log detail; never surfaced to the caller.
        degraded: True when the guard failed open (errored/unreachable).
        score: Raw detector probability in ``0.0..1.0``.
    """

    verdict: GuardVerdict
    normalized_text: str
    reasons: tuple[str, ...]
    degraded: bool
    score: float


@runtime_checkable
class InjectionGuard(Protocol):
    """Screens finalized caller turns for prompt injection (early-warning layer)."""

    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        """Screen one finalized, transcribed caller turn for prompt injection.

        ``call_id`` scopes per-session state (cumulative risk, rate of
        suspicious turns; ADR-0009). Returns a graded ``GuardResult``, never a
        raw string. This is an early-warning LAYER, not the defense: the caller
        decides policy on the typed verdict, and the enforceable control is the
        tool-policy gate (ADR-0004 ``policy``) — not the classifier alone.
        """
        ...
