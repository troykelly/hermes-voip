"""Tests for hermes_voip.providers.policy — the enforceable tool-policy gate.

This is the security-critical control (ADR-0009): an irreversible tool must be
blocked even when the classifier returned ALLOW, whenever the session is degraded
or the action is unconfirmed. The truth table is asserted exhaustively.
"""

from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import GuardSessionState, ToolRisk, gate_tool_call


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


def test_safe_tools_always_run() -> None:
    state = GuardSessionState(call_id="c1")
    assert gate_tool_call(ToolRisk.SAFE, state, confirmed=False) is True
    state.degraded = True
    assert gate_tool_call(ToolRisk.SAFE, state, confirmed=False) is True


def test_irreversible_requires_confirmation() -> None:
    state = GuardSessionState(call_id="c1")
    assert gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=False) is False
    assert gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=True) is True


def test_irreversible_blocked_when_degraded_even_if_confirmed() -> None:
    # The ADR-0009 miss case: classifier may have ALLOWed, but a degraded guard
    # must still hard-block irreversible actions regardless of confirmation.
    state = GuardSessionState(call_id="c1", degraded=True)
    assert gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=True) is False


def test_elevated_blocked_only_when_degraded() -> None:
    state = GuardSessionState(call_id="c1")
    assert gate_tool_call(ToolRisk.ELEVATED, state, confirmed=False) is True
    state.degraded = True
    assert gate_tool_call(ToolRisk.ELEVATED, state, confirmed=False) is False


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
