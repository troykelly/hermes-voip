"""Tests for hermes_voip.providers.policy — the enforceable tool-policy gate.

This is the security-critical control (ADR-0009): an irreversible tool must be
blocked even when the classifier returned ALLOW, whenever the session is degraded
or the action is unconfirmed. The truth table is asserted exhaustively.
"""

from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import (
    GateDecision,
    GuardSessionState,
    ToolRisk,
    gate_tool_call,
)


def _result(
    *, degraded: bool, verdict: GuardVerdict = GuardVerdict.ALLOW
) -> GuardResult:
    return GuardResult(
        verdict=verdict,
        normalized_text="",
        reasons=(),
        degraded=degraded,
        score=0.0,
    )


def _flagged(verdict: GuardVerdict) -> GuardResult:
    """A non-degraded screen carrying a graded (possibly flagged) verdict."""
    return GuardResult(
        verdict=verdict,
        normalized_text="",
        reasons=("audit",),
        degraded=False,
        score=0.5,
    )


def test_safe_tools_always_run() -> None:
    state = GuardSessionState(call_id="c1")
    assert gate_tool_call(ToolRisk.SAFE, state, confirmed=False).allowed is True
    state.degraded = True
    assert gate_tool_call(ToolRisk.SAFE, state, confirmed=False).allowed is True


def test_gate_is_total_over_tool_risk() -> None:
    # Every ToolRisk member yields a GateDecision (ADR-0085: the gate returns a
    # typed decision, not a bare bool); none falls through to assert_never.
    state = GuardSessionState(call_id="c1")
    for risk in ToolRisk:
        assert isinstance(gate_tool_call(risk, state, confirmed=True), GateDecision)


def test_irreversible_requires_confirmation() -> None:
    state = GuardSessionState(call_id="c1")
    assert (
        gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=False).allowed is False
    )
    assert gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=True).allowed is True


def test_irreversible_blocked_when_degraded_even_if_confirmed() -> None:
    # The ADR-0009 miss case: classifier may have ALLOWed, but a degraded guard
    # must still hard-block irreversible actions regardless of confirmation.
    state = GuardSessionState(call_id="c1", degraded=True)
    assert gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=True).allowed is False


def test_elevated_blocked_only_when_degraded() -> None:
    state = GuardSessionState(call_id="c1")
    assert gate_tool_call(ToolRisk.ELEVATED, state, confirmed=False).allowed is True
    state.degraded = True
    assert gate_tool_call(ToolRisk.ELEVATED, state, confirmed=False).allowed is False


def test_degraded_is_sticky_via_record() -> None:
    state = GuardSessionState(call_id="c1")
    state.record(_result(degraded=True))
    assert state.degraded is True
    # a later clean screen must NOT un-degrade the session within a call
    state.record(_result(degraded=False))
    assert state.degraded is True


def test_record_keeps_clean_session_clean() -> None:
    state = GuardSessionState(call_id="c1")
    state.record(_result(degraded=False))
    assert state.degraded is False


# --- record() populates flagged_turns and honours the graded verdict ----------


def test_record_does_not_flag_an_allow_turn() -> None:
    # A benign ALLOW is not an audit-worthy event: flagged_turns stays empty.
    state = GuardSessionState(call_id="c1")
    state.record(_flagged(GuardVerdict.ALLOW))
    assert state.flagged_turns == ()


def test_record_flags_each_non_allow_verdict() -> None:
    # CLARIFY, RESTRICT, REFUSE are all flagged for audit; each adds a turn id.
    state = GuardSessionState(call_id="c1")
    state.record(_flagged(GuardVerdict.CLARIFY))
    state.record(_flagged(GuardVerdict.RESTRICT))
    state.record(_flagged(GuardVerdict.REFUSE))
    assert len(state.flagged_turns) == 3
    # The flagged turn ids are distinct (one per screened turn).
    assert len(set(state.flagged_turns)) == 3


def test_record_flags_a_degraded_turn_even_if_verdict_is_allow() -> None:
    # A fail-open turn (degraded) is audit-worthy regardless of its verdict.
    state = GuardSessionState(call_id="c1")
    state.record(_result(degraded=True, verdict=GuardVerdict.ALLOW))
    assert len(state.flagged_turns) == 1
    assert state.degraded is True


def test_flagged_turns_accumulate_across_a_call() -> None:
    state = GuardSessionState(call_id="c1")
    state.record(_flagged(GuardVerdict.ALLOW))  # not flagged
    state.record(_flagged(GuardVerdict.REFUSE))  # flagged
    state.record(_flagged(GuardVerdict.ALLOW))  # not flagged
    state.record(_flagged(GuardVerdict.RESTRICT))  # flagged
    assert len(state.flagged_turns) == 2


# --- ADR-0031: allowed_tools sub-ceiling on GuardSessionState -----------------
#
# A caller group may carry an explicit allow-list of tool names (the intercom
# group is scoped to ONLY the entry action). The list is a SUB-ceiling: it can
# only REMOVE tools, never grant a tool above the privilege level. It lives on
# the per-session guard state as ``allowed_tools`` (an empty frozenset = no
# allow-list = level-only behaviour, the existing default).


def test_allowed_tools_defaults_to_empty_frozenset() -> None:
    # Back-compat: a session constructed without allowed_tools has an empty
    # frozenset, which the gate treats as "no sub-ceiling" (level-only).
    state = GuardSessionState(call_id="c1")
    assert state.allowed_tools == frozenset()


def test_allowed_tools_is_stored_as_given() -> None:
    state = GuardSessionState(
        call_id="c1", privilege_level=2, allowed_tools=frozenset({"open_entry"})
    )
    assert state.allowed_tools == frozenset({"open_entry"})
    # It does not perturb the other state.
    assert state.privilege_level == 2
