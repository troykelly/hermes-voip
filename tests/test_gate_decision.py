"""Tests for the typed GateDecision / GateReason audit boundary.

``gate_tool_call`` returns a typed :class:`GateDecision` (``allowed`` +
``reason``) rather than a bare bool, so a hard-block carries the OPERATOR-visible
reason WHY it blocked (unconfirmed caller vs degraded mode vs not-privileged) —
the audit trace a bare bool destroyed. These tests pin the SPECIFIC reason on
each representative block path (rule 19), beyond ``is True/False``.

The allow/block DECISION is the same truth table asserted in
``test_providers_policy.py``; here we assert the ADDED structured reason.
"""

import logging

import pytest

from hermes_voip.media.call_loop import gate_voip_tool as loop_gate
from hermes_voip.providers.policy import (
    GateDecision,
    GateReason,
    GuardSessionState,
    ToolRisk,
    gate_tool_call,
)
from hermes_voip.tools import gate_voip_tool as tools_gate

# --- shape ---------------------------------------------------------------------


def test_gate_decision_is_immutable() -> None:
    # The decision is a frozen, fully-typed value (an audit record must not be
    # mutated after the gate produced it).
    decision = GateDecision(allowed=True, reason=GateReason.ALLOWED)
    with pytest.raises(AttributeError):
        decision.allowed = False  # type: ignore[misc]  # asserting frozen: this MUST raise


def test_gate_tool_call_returns_a_gate_decision() -> None:
    state = GuardSessionState(call_id="c1")
    decision = gate_tool_call(ToolRisk.SAFE, state, confirmed=False)
    assert isinstance(decision, GateDecision)


# --- the ALLOWED reason on every allow path -----------------------------------


def test_safe_is_allowed_with_allowed_reason() -> None:
    state = GuardSessionState(call_id="c1")
    decision = gate_tool_call(ToolRisk.SAFE, state, confirmed=False)
    assert decision.allowed is True
    assert decision.reason is GateReason.ALLOWED


def test_safe_is_allowed_even_when_degraded_and_unprivileged() -> None:
    # SAFE always runs; the reason is ALLOWED, never a block reason.
    state = GuardSessionState(call_id="c1", privileged=False, degraded=True)
    decision = gate_tool_call(ToolRisk.SAFE, state, confirmed=False)
    assert decision.allowed is True
    assert decision.reason is GateReason.ALLOWED


def test_elevated_allowed_for_privileged_clean_session() -> None:
    state = GuardSessionState(call_id="c1")  # default level 3, clean
    decision = gate_tool_call(ToolRisk.ELEVATED, state, confirmed=False)
    assert decision.allowed is True
    assert decision.reason is GateReason.ALLOWED


def test_irreversible_allowed_for_confirmed_privileged_clean_session() -> None:
    state = GuardSessionState(call_id="c1")  # default level 3, clean
    decision = gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=True)
    assert decision.allowed is True
    assert decision.reason is GateReason.ALLOWED


# --- INSUFFICIENT_PRIVILEGE block path ----------------------------------------


def test_elevated_block_for_unprivileged_clean_is_insufficient_privilege() -> None:
    # Level 0 (receptionist), clean session: blocked because the level is too low,
    # NOT because of degrade — the operator must see "not privileged".
    state = GuardSessionState(call_id="c1", privilege_level=0)
    assert state.degraded is False
    decision = gate_tool_call(ToolRisk.ELEVATED, state, confirmed=False)
    assert decision.allowed is False
    assert decision.reason is GateReason.INSUFFICIENT_PRIVILEGE


def test_irreversible_block_for_level2_confirmed_is_insufficient_privilege() -> None:
    # Level 2 (trusted) is below the IRREVERSIBLE ceiling (3); confirmed + clean, so
    # the ONLY block cause is the privilege level.
    state = GuardSessionState(call_id="c1", privilege_level=2)
    decision = gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=True)
    assert decision.allowed is False
    assert decision.reason is GateReason.INSUFFICIENT_PRIVILEGE


# --- UNCONFIRMED block path ----------------------------------------------------


def test_irreversible_blocked_when_unconfirmed_is_unconfirmed() -> None:
    # Operator level, clean session, but no confirmation: the block reason is the
    # missing confirmation, distinct from privilege/degrade.
    state = GuardSessionState(call_id="c1")  # level 3, clean
    decision = gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=False)
    assert decision.allowed is False
    assert decision.reason is GateReason.UNCONFIRMED


# --- DEGRADED block path (the load-bearing security signal) -------------------


def test_elevated_blocked_when_degraded_is_degraded() -> None:
    state = GuardSessionState(call_id="c1", degraded=True)  # level 3, degraded
    decision = gate_tool_call(ToolRisk.ELEVATED, state, confirmed=False)
    assert decision.allowed is False
    assert decision.reason is GateReason.DEGRADED


def test_irreversible_blocked_when_degraded_even_if_confirmed_is_degraded() -> None:
    # The ADR-0009 miss case: a degraded session hard-blocks IRREVERSIBLE even with a
    # (possibly spoofed) confirmation. The reason must say DEGRADED so the operator
    # sees the fail-open, not a misleading "unconfirmed".
    state = GuardSessionState(call_id="c1", degraded=True)
    decision = gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=True)
    assert decision.allowed is False
    assert decision.reason is GateReason.DEGRADED


def test_degraded_precedence_over_privilege_and_confirmation() -> None:
    # A session that is degraded AND unprivileged AND unconfirmed reports DEGRADED:
    # the sticky fail-open is the most security-significant cause and takes
    # precedence so the operator sees the guard degraded first.
    state = GuardSessionState(call_id="c1", privilege_level=0, degraded=True)
    decision = gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=False)
    assert decision.allowed is False
    assert decision.reason is GateReason.DEGRADED


def test_privilege_precedence_over_confirmation_on_clean_session() -> None:
    # Clean (non-degraded) but unprivileged AND unconfirmed: the level is the
    # binding cause (a confirmation could never lift a too-low level), so the reason
    # is INSUFFICIENT_PRIVILEGE, not UNCONFIRMED.
    state = GuardSessionState(call_id="c1", privilege_level=2)
    decision = gate_tool_call(ToolRisk.IRREVERSIBLE, state, confirmed=False)
    assert decision.allowed is False
    assert decision.reason is GateReason.INSUFFICIENT_PRIVILEGE


# --- the decision matches the prior bool truth table exactly -------------------


def test_allowed_field_reproduces_the_bool_truth_table() -> None:
    # Behaviour invariant: only the reason is ADDED; the allow/block decision is
    # byte-for-byte the prior bool for every (risk, level, degraded, confirmed).
    for level in (0, 2, 3):
        for degraded in (False, True):
            for confirmed in (False, True):
                safe = GuardSessionState(
                    call_id="c1", privilege_level=level, degraded=degraded
                )
                assert (
                    gate_tool_call(ToolRisk.SAFE, safe, confirmed=confirmed).allowed
                    is True
                )
                elevated = GuardSessionState(
                    call_id="c1", privilege_level=level, degraded=degraded
                )
                assert gate_tool_call(
                    ToolRisk.ELEVATED, elevated, confirmed=confirmed
                ).allowed is (level >= 2 and not degraded)
                irreversible = GuardSessionState(
                    call_id="c1", privilege_level=level, degraded=degraded
                )
                assert gate_tool_call(
                    ToolRisk.IRREVERSIBLE, irreversible, confirmed=confirmed
                ).allowed is (level >= 3 and confirmed and not degraded)


# --- the gate callers AUDIT the reason on a block -----------------------------
#
# A bare bool destroyed the audit trace; the callers that turn the decision back
# into a block must LOG the operator-visible reason so an operator can see WHY a
# tool was refused on a live call. ``tools.gate_voip_tool`` keeps its ``bool``
# return (the adapter REFER chokepoints branch on it) but now logs the structured
# reason on every block.


def test_tools_gate_voip_tool_returns_bool_and_logs_reason_on_block(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unpriv = GuardSessionState(call_id="c1", privilege_level=0)
    with caplog.at_level(logging.WARNING, logger="hermes_voip.tools"):
        allowed = tools_gate("hold_call", unpriv, confirmed=False)
    # The bool contract is preserved for the out-of-scope adapter callers.
    assert allowed is False
    # The operator-visible reason is in the audit log (the WHY a bare bool lost).
    assert any(
        GateReason.INSUFFICIENT_PRIVILEGE.value in record.getMessage()
        for record in caplog.records
    ), caplog.text


def test_tools_gate_voip_tool_logs_degraded_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    degraded = GuardSessionState(call_id="c1", degraded=True)
    with caplog.at_level(logging.WARNING, logger="hermes_voip.tools"):
        allowed = tools_gate("transfer_blind", degraded, confirmed=True)
    assert allowed is False
    assert any(
        GateReason.DEGRADED.value in record.getMessage() for record in caplog.records
    ), caplog.text


def test_tools_gate_voip_tool_does_not_log_on_allow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clean = GuardSessionState(call_id="c1")  # level 3, clean
    with caplog.at_level(logging.WARNING, logger="hermes_voip.tools"):
        allowed = tools_gate("hold_call", clean, confirmed=False)
    assert allowed is True
    assert caplog.records == []


def test_call_loop_gate_voip_tool_returns_bool() -> None:
    # The media/call_loop re-export keeps the bool contract its callers use.
    clean = GuardSessionState(call_id="c1")
    degraded = GuardSessionState(call_id="c1", degraded=True)
    assert loop_gate(ToolRisk.SAFE, clean, confirmed=False) is True
    assert loop_gate(ToolRisk.IRREVERSIBLE, degraded, confirmed=True) is False
