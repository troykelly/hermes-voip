"""The enforceable tool-policy gate (ADR-0004/0009 / ADR-0021).

The classifier has false negatives by construction, so the *enforceable* control
is this typed gate. Every registered tool carries a ``ToolRisk``; per-session
guard state carries the ``degraded`` flag (it follows the session, set by any
fail-open ``GuardResult``); and a ``pre_tool_call`` hook MUST gate every
``IRREVERSIBLE`` tool — requiring explicit human/DTMF confirmation (ADR-0010)
and hard-blocking while ``degraded`` — regardless of the classifier outcome. A
missed injection therefore still cannot reach an irreversible action.

**ADR-0021 change:** ``GuardSessionState.privileged: bool`` is replaced by
``privilege_level: int`` (0/2/3) with a backward-compat ``privileged`` property
(``level >= 3``).  The gate now uses a level comparison instead of a bool check;
levels 0/3 reproduce the ADR-0020 bool behaviour exactly so all existing callers
work unchanged.  Old code that still constructs with ``privileged=True/False``
maps to level 3/0 via the constructor keyword.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import assert_never

from hermes_voip.providers.guard import GuardResult, GuardVerdict

__all__ = [
    "GateDecision",
    "GateReason",
    "GuardSessionState",
    "ToolRisk",
    "gate_tool_call",
]

# Minimum privilege level required to reach each non-SAFE risk class (ADR-0021).
_MIN_LEVEL_ELEVATED = 2
_MIN_LEVEL_IRREVERSIBLE = 3


class GateReason(Enum):
    """WHY :func:`gate_tool_call` allowed or blocked a tool (ADR-0085).

    The discriminant of :class:`GateDecision`: a bare bool recorded only THAT a
    tool was blocked, never WHY — exactly the audit trace an operator needs to
    tell an unconfirmed caller apart from a degraded (fail-open) session apart
    from a not-allowlisted/under-privileged one. The members are DERIVED from the
    existing gate logic (no invented reasons): one allow reason plus the three
    distinct block causes the gate already computes.

    Members:
        ALLOWED: The tool may run (the only non-block reason).
        INSUFFICIENT_PRIVILEGE: The session's ``privilege_level`` is below the
            tool's risk class (a confirmation can never lift a too-low level).
        UNCONFIRMED: An ``IRREVERSIBLE`` tool lacked explicit caller confirmation
            on an otherwise operator-level, non-degraded session.
        DEGRADED: The session is in the sticky fail-open ``degraded`` state — the
            ADR-0009 hard-block that fires regardless of privilege/confirmation.
        RESTRICTED: The CURRENT caller turn was screened ``RESTRICT`` or ``CLARIFY``
            (ADR-0009), so this turn is clamped read-only (no non-SAFE tool) — a
            PER-TURN block that a later ``ALLOW`` turn clears (unlike sticky degraded).
    """

    ALLOWED = "allowed"
    INSUFFICIENT_PRIVILEGE = "insufficient_privilege"
    UNCONFIRMED = "unconfirmed"
    DEGRADED = "degraded"
    RESTRICTED = "restricted"


@dataclass(frozen=True, slots=True)
class GateDecision:
    """An immutable, fully-typed gate outcome: the decision PLUS its reason.

    Replaces ``gate_tool_call``'s bare ``bool`` so every block carries the
    operator-visible :class:`GateReason` (the WHY a bool destroyed). The
    allow/block ``allowed`` field reproduces the prior bool byte-for-byte; the
    ``reason`` is purely ADDITIVE audit metadata.

    Invariant: ``allowed`` is ``True`` iff ``reason is GateReason.ALLOWED`` — the
    two fields are constructed together by :func:`gate_tool_call` and never drift.

    Attributes:
        allowed: Whether the tool may run (the prior bool decision).
        reason: WHY — :attr:`GateReason.ALLOWED` on an allow, else the specific
            block cause.
    """

    allowed: bool
    reason: GateReason

    def __post_init__(self) -> None:
        """Reject inconsistent allow/block + reason pairs at construction."""
        if self.allowed is not (self.reason is GateReason.ALLOWED):
            msg = (
                "GateDecision.allowed must be allowed iff reason is GateReason.ALLOWED"
            )
            raise ValueError(msg)


class ToolRisk(IntEnum):
    """Action risk class for a registered tool (ascending severity).

    The integer values encode severity order: the least-severe class is 0 and
    the most-severe is 2, enabling direct ``>``/``<`` comparisons such as
    ``risk >= ToolRisk.ELEVATED``.  The documented ascending order is
    ``SAFE < ELEVATED < IRREVERSIBLE``.
    """

    SAFE = 0  # read-only / no side effects
    ELEVATED = 1  # mutating but reversible / low blast radius
    IRREVERSIBLE = 2  # payments, bookings, transfers, account mutation


class GuardSessionState:
    """Per-session guard state; lives for the call (ADR-0009 / ADR-0020 / ADR-0021).

    **ADR-0021** replaces the single ``privileged: bool`` with an integer
    ``privilege_level`` (0 = receptionist/SAFE-only, 2 = trusted/+ELEVATED,
    3 = operator/+IRREVERSIBLE) so the gate can express the three tiers the
    operator asked for.  Levels 0 and 3 reproduce ADR-0020's ``privileged=False``
    / ``True`` **exactly**.  The backward-compat ``privileged`` property
    (``level >= 3``) keeps every existing ``state.privileged`` reader working
    without changes.

    Construction (ADR-0097 — least-privilege by default):
    - ``GuardSessionState(call_id, privilege_level=N)`` — new callers.
    - ``GuardSessionState(call_id, privileged=True/False)`` — old callers: maps to
      level 3 (True) or level 0 (False).  If ``privileged`` is supplied it silently
      takes precedence over ``privilege_level`` (no error) — no current call site
      supplies both, so this precedence is a documented fallback, not a load-bearing
      behaviour.
    - ``GuardSessionState(call_id)`` — default, level 0 (receptionist/SAFE-only).
      **ADR-0097** flipped this from the pre-existing level-3 (assistant) default:
      a caller-classification path that forgets to pass a level now fails closed
      instead of silently granting operator trust — the same least-privilege
      principle ADR-0021 §0.3 already applies to an unmatched caller group
      (``CallerGroupConfig`` rejects a privileged ``default_group``). Every
      production construction site either already passed an explicit level, or
      (the one exception, the no-call ``list_registrations`` ambient state) now
      does so explicitly rather than relying on the constructor default.

    Attributes:
        call_id: The call/session this state belongs to.
        degraded: True once any fail-open screen occurred; never un-sets in-call.
        turn_restricted: True when the CURRENT turn was screened RESTRICT/CLARIFY
            (ADR-0009), clamping it read-only (no non-SAFE tool). PER-TURN — set by
            :meth:`record` each screen, cleared by a clean ALLOW turn (unlike the
            sticky ``degraded``).
        privilege_level: The tool-risk ceiling for this session (0/2/3).
            Set from the caller group at INVITE time (ADR-0021).
        allowed_tools: An optional per-session tool allow-list — a SUB-ceiling
            below ``privilege_level`` (ADR-0031). EMPTY (the default) means "no
            sub-ceiling": the level alone gates, reproducing every existing
            decision. A NON-EMPTY set scopes the session's SENSITIVE surface to ONLY
            those tool names: :func:`~hermes_voip.tools.gate_voip_tool` blocks any
            NON-SAFE (ELEVATED/IRREVERSIBLE) tool not listed before the level/risk
            check, so the set can only REMOVE tools, never grant one above the level.
            SAFE tools (``hang_up`` / ``report_call_result``) are EXEMPT and always
            run, so a scoped group never loses the ability to end/record its own call.
            Set from the caller group at INVITE time (the intercom group is scoped to
            just its entry action).
        flagged_turns: Identifiers of turns flagged for audit during the call.
    """

    __slots__ = (
        "_turns_seen",
        "allowed_tools",
        "call_id",
        "degraded",
        "flagged_turns",
        "privilege_level",
        "turn_restricted",
    )

    def __init__(  # noqa: PLR0913 — each arg is an independent session-state field; the back-compat ``privileged`` kwarg + the ADR-0031 ``allowed_tools`` sub-ceiling both have to live alongside the four existing fields
        self,
        call_id: str,
        degraded: bool = False,
        privilege_level: int = 0,
        flagged_turns: tuple[str, ...] = (),
        *,
        privileged: bool | None = None,
        allowed_tools: frozenset[str] = frozenset(),
    ) -> None:
        """Construct session state.

        Args:
            call_id: The call identifier.
            degraded: Whether the session is already in degraded state.
            privilege_level: Tool-risk ceiling (0/2/3).  Default 0 = least-privilege
                (receptionist/SAFE-only) — ADR-0097. Ignored if ``privileged`` is
                supplied. A caller-classification path that wants operator/trusted
                trust must state the level explicitly; an omitted level fails
                closed rather than silently granting operator trust.
            flagged_turns: Initial set of flagged turn IDs (typically empty).
            privileged: Backward-compat kwarg.  If supplied (not ``None``), silently
                takes precedence over ``privilege_level``: ``True`` → 3,
                ``False`` → 0. No current call site supplies both.
            allowed_tools: Optional tool-name allow-list (ADR-0031). EMPTY (the
                default) = no sub-ceiling (level-only gating, the existing
                behaviour). A NON-EMPTY set scopes the session's SENSITIVE surface to
                ONLY those tools (a sub-ceiling that can only remove non-SAFE tools,
                never grant above the level); SAFE tools are always allowed.
        """
        self.call_id = call_id
        self.degraded = degraded
        self.flagged_turns = flagged_turns
        self.allowed_tools = allowed_tools
        self._turns_seen = 0
        # Per-turn read-only clamp (ADR-0009): set by ``record`` when the CURRENT
        # turn's verdict is not ALLOW (RESTRICT/CLARIFY/REFUSE). Distinct from the
        # sticky, whole-call ``degraded`` flag — a later ALLOW turn clears it.
        self.turn_restricted = False
        if privileged is not None:
            # Backward-compat: map old bool kwarg → level.
            # True = operator (3); False = receptionist (0).
            self.privilege_level = 3 if privileged else 0
        else:
            self.privilege_level = privilege_level

    @property
    def privileged(self) -> bool:
        """True iff this session has operator-level (IRREVERSIBLE) privilege.

        Backward-compat property: ``state.privileged`` reads as
        ``state.privilege_level >= 3``, preserving every existing reader
        (adapter.py, tools.py, tests) without changes.
        """
        return self.privilege_level >= _MIN_LEVEL_IRREVERSIBLE

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
        # Per-turn read-only clamp (ADR-0009): a non-ALLOW verdict (RESTRICT/CLARIFY,
        # or the RESTRICT a fail-open returns) clamps THIS turn to SAFE-only; a clean
        # ALLOW turn clears it. Overwritten every screen, so it tracks the current
        # turn — unlike the sticky ``degraded`` bit above.
        self.turn_restricted = result.verdict is not GuardVerdict.ALLOW
        if result.verdict is not GuardVerdict.ALLOW or result.degraded:
            turn_id = f"{self.call_id}#{self._turns_seen}"
            self.flagged_turns = (*self.flagged_turns, turn_id)

    def note_trusted_turn(self) -> None:
        """Clear the per-turn read-only clamp for a NON-classifier-screened turn.

        A delivery path that carries no natural-language injection surface — DTMF
        keypad input (constrained to ``0-9*#ABCD``, gated by privilege + explicit
        confirmation, never the classifier) — must still set ``turn_restricted`` for
        ITS turn, or the flag stays stale from the previous screened turn (a
        stale-``True`` over-blocks the legitimate keypad turn, e.g. an ADR-0010
        DTMF-confirmed transfer; a stale-``False`` would silently carry a prior
        clamp's assumptions). A trusted turn is not clamped, so this clears it. The
        sticky, whole-call ``degraded`` state is deliberately left untouched — a
        fail-open still hard-blocks non-SAFE tools on every turn, keypad included.
        """
        self.turn_restricted = False

    def __repr__(self) -> str:
        """Return a developer-friendly representation."""
        return (
            f"GuardSessionState(call_id={self.call_id!r},"
            f" degraded={self.degraded!r},"
            f" privilege_level={self.privilege_level!r},"
            f" allowed_tools={sorted(self.allowed_tools)!r},"
            f" flagged_turns={self.flagged_turns!r})"
        )


# Operator-friendly allow record, shared by every SAFE/allow path (no per-call alloc).
_ALLOW = GateDecision(allowed=True, reason=GateReason.ALLOWED)


def gate_tool_call(
    risk: ToolRisk, state: GuardSessionState, *, confirmed: bool
) -> GateDecision:
    """Decide whether a tool may run (``pre_tool_call`` policy).

    **ADR-0021:** the gate now uses ``state.privilege_level`` (0/2/3) instead of
    ``state.privileged`` (bool), enabling the three-tier model:

    - privilege_level=0 (receptionist): SAFE only; ELEVATED+IRREVERSIBLE blocked.
    - privilege_level=2 (trusted): SAFE + ELEVATED (if not degraded);
      IRREVERSIBLE blocked.
    - privilege_level=3 (operator): SAFE + ELEVATED (if not degraded)
      + IRREVERSIBLE (if confirmed AND not degraded).

    Levels 0/3 reproduce ADR-0020's ``privileged=False``/``True`` **exactly**:
    level-0 blocks all non-``SAFE``; level-3 applies the existing
    ``degraded``/``confirmed`` checks unchanged.  Level 2 adds the new middle
    tier (hold/resume but not transfer).

    A missed injection with a spoofed confirmation still cannot reach a
    privileged action: the level check fires before ``confirmed`` is consulted
    for IRREVERSIBLE, and the ``degraded`` hard-block applies at every level >= 2
    for both ELEVATED and IRREVERSIBLE.

    This never silently allows (rule 37): the decision is total over ``ToolRisk``.

    **ADR-0085 — typed decision.** Returns a :class:`GateDecision` (``allowed`` +
    :class:`GateReason`) instead of a bare bool. The ``allowed`` field is the prior
    bool byte-for-byte (the truth table is unchanged); the ``reason`` is the ADDED
    audit WHY. When MULTIPLE block causes hold at once the reason follows a fixed,
    security-ordered precedence — ``DEGRADED`` (the sticky fail-open, the most
    security-significant signal) first, then ``INSUFFICIENT_PRIVILEGE`` (a level a
    confirmation can never lift), then ``UNCONFIRMED`` (the residual cause on an
    otherwise-eligible operator session). The precedence governs only WHICH reason
    is reported on a block; it never changes whether the tool is blocked.

    Args:
        risk: The action risk class of the tool.
        state: The per-session guard state.
        confirmed: Whether the caller explicitly confirmed the action.

    Returns:
        A :class:`GateDecision`: ``allowed`` (the prior bool) plus the
        :class:`GateReason` explaining the outcome.
    """
    if risk is ToolRisk.SAFE:
        # Read-only tools always run, even for an untrusted/unprivileged session:
        # the caller is screened, never dropped.
        return _ALLOW
    if risk is ToolRisk.ELEVATED:
        # SAFE+ELEVATED ceiling is level >= 2; no per-action confirmation needed, so
        # confirmation is treated as satisfied (it is never consulted for ELEVATED).
        return _gate_non_safe(
            state, min_level=_MIN_LEVEL_ELEVATED, confirmation_ok=True
        )
    if risk is ToolRisk.IRREVERSIBLE:
        # Operator ceiling (level >= 3) AND explicit confirmation.
        return _gate_non_safe(
            state, min_level=_MIN_LEVEL_IRREVERSIBLE, confirmation_ok=confirmed
        )
    assert_never(
        risk
    )  # exhaustive: a new ToolRisk member fails mypy here, not silently allows


def _gate_non_safe(
    state: GuardSessionState, *, min_level: int, confirmation_ok: bool
) -> GateDecision:
    """The ELEVATED/IRREVERSIBLE clamp, with the block-reason precedence (ADR-0085).

    ``confirmation_ok`` is the *already-evaluated* confirmation status: ``True`` ⇒
    confirmation satisfied or not required (ELEVATED never needs it); ``False`` ⇒
    an IRREVERSIBLE tool still awaits its explicit caller confirmation. The
    block-reason precedence is fixed: a ``degraded`` session (the load-bearing
    fail-open hard-block) is reported first, then an insufficient ``privilege_level``
    (a level no confirmation can lift), then the residual ``UNCONFIRMED`` cause.
    Precedence governs only WHICH reason is reported; the allow/block decision is the
    prior bool byte-for-byte.
    """
    if state.degraded:
        return GateDecision(allowed=False, reason=GateReason.DEGRADED)
    if state.turn_restricted:
        # The current turn was screened RESTRICT/CLARIFY (ADR-0009): clamp read-only,
        # no non-SAFE tool this turn, regardless of the session's privilege_level.
        # Per-turn (a later ALLOW screen clears it), so this reports before the
        # structural privilege/confirmation causes.
        return GateDecision(allowed=False, reason=GateReason.RESTRICTED)
    if state.privilege_level < min_level:
        return GateDecision(allowed=False, reason=GateReason.INSUFFICIENT_PRIVILEGE)
    if not confirmation_ok:
        return GateDecision(allowed=False, reason=GateReason.UNCONFIRMED)
    return _ALLOW
