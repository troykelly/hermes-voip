"""Tests for the agent-facing call-control tools and their gate (ADR-0011 PR9).

The five tools map to a :class:`ToolRisk` and run through ``gate_tool_call``
verbatim. The load-bearing property is **invariant 3**: an ``IRREVERSIBLE``
transfer is hard-blocked when unconfirmed or while the session is ``degraded`` —
even if the injection guard returned ALLOW — and the underlying verb never runs.

Fakes only (``pbx.example.test``, ext ``1000``/``2000``, ``198.51.100.x``).
"""

from __future__ import annotations

import pytest

from hermes_voip.dialog import Dialog
from hermes_voip.manager import RegistrationStatus
from hermes_voip.providers.policy import GuardSessionState, ToolRisk
from hermes_voip.tools import (
    TOOL_RISKS,
    CallControlTools,
    gate_voip_tool,
)


class _FakeCall:
    def __init__(self, *, degraded: bool = False) -> None:
        # This fixture models the PRIVILEGED (operator) call across this file —
        # the confirmed/degraded axis is what this module tests (see its
        # docstring); the privilege axis is `test_caller_privilege.py`'s job.
        # ADR-0097 made bare ``GuardSessionState`` construction level-0, so the
        # privileged default is now stated explicitly here rather than inherited.
        self.guard = GuardSessionState(
            call_id="call-1", privilege_level=3, degraded=degraded
        )
        self.held = 0
        self.resumed = 0
        self.hung_up = 0
        self.blind_targets: list[str] = []
        self.attended: list[Dialog] = []

    async def hold(self) -> None:
        self.held += 1

    async def unhold(self) -> None:
        self.resumed += 1

    async def hang_up(self) -> None:
        self.hung_up += 1

    async def transfer_blind(
        self, target_uri: str, *, referred_by: str | None = None
    ) -> None:
        self.blind_targets.append(target_uri)

    async def transfer_attended(
        self, consult: Dialog, *, referred_by: str | None = None
    ) -> None:
        self.attended.append(consult)


class _FakeManager:
    def __init__(self, statuses: tuple[RegistrationStatus, ...]) -> None:
        self._statuses = statuses

    def snapshot(self) -> tuple[RegistrationStatus, ...]:
        return self._statuses


class _FakeConfirmation:
    def __init__(self, *, confirmed: bool) -> None:
        self._confirmed = confirmed
        self.prompts = 0

    async def confirm(self) -> bool:
        self.prompts += 1
        return self._confirmed


def _manager() -> _FakeManager:
    return _FakeManager(
        (
            RegistrationStatus(extension="1000", index=1, registered=True, expires=300),
            RegistrationStatus(
                extension="1001", index=2, registered=False, expires=None
            ),
        )
    )


def _consult() -> Dialog:
    return Dialog(
        call_id="ac-call",
        local_uri="sip:1000@pbx.example.test",
        local_tag="a",
        remote_uri="sip:3000@pbx.example.test",
        remote_tag="c",
        remote_target="sip:3000@198.51.100.50:5061;transport=tls",
        route_set=(),
        local_contact="<sip:1000@198.51.100.7:5061;transport=tls>",
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
        local_cseq=1,
        sdp_version=0,
    )


def _tools(call: _FakeCall | None, *, confirmed: bool = True) -> CallControlTools:
    tools = CallControlTools(
        _manager(), confirmation=_FakeConfirmation(confirmed=confirmed)
    )
    if call is not None:
        tools.bind_call(call)
    return tools


# ---- risk map + gate hook --------------------------------------------------


def test_tool_risk_map_is_correct() -> None:
    assert TOOL_RISKS["hold_call"] is ToolRisk.ELEVATED
    assert TOOL_RISKS["resume_call"] is ToolRisk.ELEVATED
    assert TOOL_RISKS["transfer_blind"] is ToolRisk.IRREVERSIBLE
    assert TOOL_RISKS["transfer_attended"] is ToolRisk.IRREVERSIBLE
    # ELEVATED (ADR-0020): list_registrations discloses internal extension
    # metadata, so an untrusted/unprivileged caller must not enumerate it.
    assert TOOL_RISKS["list_registrations"] is ToolRisk.ELEVATED
    # ELEVATED (ADR-0031): send_dtmf transmits in-call DTMF — reversible but a
    # mutating action a level-0 caller must not invoke. open_entry actuates the
    # intercom entry (physical access) and is likewise gated (ELEVATED), with the
    # intercom group's allowed_tools sub-ceiling restricting it to that group.
    assert TOOL_RISKS["send_dtmf"] is ToolRisk.ELEVATED
    assert TOOL_RISKS["open_entry"] is ToolRisk.ELEVATED


def test_gate_voip_tool_unknown_tool_denied() -> None:
    state = GuardSessionState(call_id="call-1")
    assert gate_voip_tool("delete_everything", state, confirmed=True) is False


def test_gate_voip_tool_maps_to_gate_tool_call() -> None:
    # ADR-0097: bare construction is level 0 now; both scenarios here are about
    # the confirmed/degraded axis on an already-privileged session.
    clean = GuardSessionState(call_id="call-1", privilege_level=3)
    degraded = GuardSessionState(call_id="call-1", privilege_level=3, degraded=True)
    # list_registrations is ELEVATED: allowed on a clean privileged call, blocked
    # while degraded (and — see test_caller_privilege — for an unprivileged call).
    assert gate_voip_tool("list_registrations", clean, confirmed=False) is True
    assert gate_voip_tool("list_registrations", degraded, confirmed=False) is False
    assert gate_voip_tool("hold_call", clean, confirmed=False) is True
    assert gate_voip_tool("hold_call", degraded, confirmed=False) is False
    assert gate_voip_tool("transfer_blind", clean, confirmed=True) is True
    assert gate_voip_tool("transfer_blind", clean, confirmed=False) is False
    assert gate_voip_tool("transfer_blind", degraded, confirmed=True) is False


# --- ADR-0031: allowed_tools sub-ceiling in gate_voip_tool --------------------
#
# When the session carries a non-empty allowed_tools set, gate_voip_tool blocks
# any tool NOT in that set BEFORE the risk/level check. This can only REMOVE
# tools; an empty set is the existing level-only behaviour.


def test_allowed_tools_empty_is_level_only_backcompat() -> None:
    # The default empty allow-list does not change any existing decision: an
    # ELEVATED tool on a clean operator session still runs. ADR-0097: bare
    # construction is level 0 now, so state the operator level explicitly —
    # the allow-list backcompat is what this test is actually about.
    clean = GuardSessionState(call_id="call-1", privilege_level=3)
    assert clean.allowed_tools == frozenset()
    assert gate_voip_tool("hold_call", clean, confirmed=False) is True
    assert gate_voip_tool("list_registrations", clean, confirmed=False) is True


def test_allowed_tools_blocks_a_tool_not_in_the_allowlist() -> None:
    # A session scoped to {open_entry} cannot reach hold_call even though its
    # level (3, default) would otherwise permit it — the sub-ceiling removes it.
    scoped = GuardSessionState(
        call_id="call-1", allowed_tools=frozenset({"open_entry"})
    )
    assert gate_voip_tool("hold_call", scoped, confirmed=False) is False
    assert gate_voip_tool("list_registrations", scoped, confirmed=False) is False


def test_allowed_tools_permits_a_tool_in_the_allowlist() -> None:
    # A same-class tool IN the allow-list still runs (subject to the level/risk
    # check, which it passes here at level 2 for an ELEVATED tool).
    scoped = GuardSessionState(
        call_id="call-1",
        privilege_level=2,
        allowed_tools=frozenset({"hold_call"}),
    )
    assert gate_voip_tool("hold_call", scoped, confirmed=False) is True


def test_allowed_tools_never_grants_above_the_level() -> None:
    # The allow-list is a SUB-ceiling, never a grant: listing an IRREVERSIBLE
    # tool in a level-2 session's allow-list does NOT let it run (the level check
    # still applies after the sub-ceiling).
    scoped = GuardSessionState(
        call_id="call-1",
        privilege_level=2,
        allowed_tools=frozenset({"transfer_blind"}),
    )
    assert gate_voip_tool("transfer_blind", scoped, confirmed=True) is False


def test_allowed_tools_does_not_resurrect_unknown_tools() -> None:
    # An unknown tool is still denied even if it appears in allowed_tools — the
    # risk map has no entry for it, so it fails closed (rule 37). The sub-ceiling
    # only ever removes, it cannot register a tool the gate does not know.
    scoped = GuardSessionState(
        call_id="call-1", allowed_tools=frozenset({"delete_everything"})
    )
    assert gate_voip_tool("delete_everything", scoped, confirmed=True) is False


def test_safe_tools_bypass_the_allowed_tools_sub_ceiling() -> None:
    # A SAFE tool is EXEMPT from the allowed_tools sub-ceiling: the sub-ceiling is a
    # SUB-ceiling on ELEVATED/IRREVERSIBLE tools ONLY. hang_up / report_call_result are
    # SAFE (ADR-0026/0029, "SAFE always runs, never gated"), so a caller group scoped to
    # e.g. {open_entry} must STILL be able to END and RECORD its own call — otherwise an
    # intercom/known call agent could open the door but never hang up. The sub-ceiling
    # must keep clamping every NON-SAFE tool outside the grant.
    scoped = GuardSessionState(
        call_id="call-1",
        privilege_level=2,
        allowed_tools=frozenset({"open_entry"}),
    )
    # SAFE tools run even though they are NOT in the allow-list.
    assert gate_voip_tool("hang_up", scoped, confirmed=False) is True
    assert gate_voip_tool("report_call_result", scoped, confirmed=False) is True
    # The one granted (ELEVATED) tool still passes; every OTHER non-SAFE tool stays
    # clamped by the sub-ceiling even though the level (2) alone would permit the
    # ELEVATED ones — the sub-ceiling only ever removes, never grants above the level.
    assert gate_voip_tool("open_entry", scoped, confirmed=False) is True
    assert gate_voip_tool("hold_call", scoped, confirmed=False) is False
    assert gate_voip_tool("send_dtmf", scoped, confirmed=False) is False
    assert gate_voip_tool("transfer_blind", scoped, confirmed=True) is False
    assert gate_voip_tool("place_call", scoped, confirmed=True) is False
    # The SAFE exemption does not alter the EMPTY-allowed_tools path (no sub-ceiling):
    # a level-2 session still reaches its ELEVATED tools by level, SAFE stays allowed,
    # and IRREVERSIBLE stays blocked by the level — byte-identical to the prior gate.
    unscoped = GuardSessionState(call_id="call-1", privilege_level=2)
    assert unscoped.allowed_tools == frozenset()
    assert gate_voip_tool("hang_up", unscoped, confirmed=False) is True
    assert gate_voip_tool("send_dtmf", unscoped, confirmed=False) is True
    assert gate_voip_tool("hold_call", unscoped, confirmed=False) is True
    assert gate_voip_tool("transfer_blind", unscoped, confirmed=True) is False


# --- ADR-0031: grant-only tools (open_entry) require an EXPLICIT allow-list grant


def test_open_entry_blocked_without_an_explicit_grant_even_for_operator() -> None:
    # open_entry is a "grant-only" tool: physical access. An operator (level 3) with
    # NO allowed_tools (the common case — empty = no sub-ceiling for normal tools)
    # must NOT be able to open the door; only a group that EXPLICITLY lists open_entry
    # (the intercom group) can. This is stricter than the generic empty-set rule.
    operator = GuardSessionState(call_id="call-1", privilege_level=3)
    assert operator.allowed_tools == frozenset()
    assert gate_voip_tool("open_entry", operator, confirmed=True) is False


def test_open_entry_allowed_only_with_explicit_grant() -> None:
    # The intercom group grants open_entry explicitly -> it is reachable (at level 2).
    intercom = GuardSessionState(
        call_id="call-1",
        privilege_level=2,
        allowed_tools=frozenset({"open_entry"}),
    )
    assert gate_voip_tool("open_entry", intercom, confirmed=False) is True


def test_grant_only_does_not_affect_normal_tools() -> None:
    # A normal ELEVATED tool (hold_call) is unaffected by the grant-only rule: an
    # operator with no sub-ceiling still gets it (the generic empty-set semantics).
    operator = GuardSessionState(call_id="call-1", privilege_level=3)
    assert gate_voip_tool("hold_call", operator, confirmed=False) is True


# ---- list_registrations (ELEVATED) -----------------------------------------


@pytest.mark.asyncio
async def test_list_registrations_allowed_on_privileged_call() -> None:
    # No active call => ambient clean+privileged state => the operator can list.
    tools = _tools(None)
    result = await tools.list_registrations()
    assert result.allowed is True
    assert "1000" in result.message
    assert "1001" in result.message


@pytest.mark.asyncio
async def test_list_registrations_blocked_on_unprivileged_call() -> None:
    # An untrusted (unprivileged) call cannot enumerate registrations (ADR-0020).
    # ADR-0021: `privileged` is read-only; construct with privilege_level=0.
    call = _FakeCall()
    call.guard = GuardSessionState(call_id="call-1", privilege_level=0)
    result = await _tools(call).list_registrations()
    assert result.allowed is False


# ---- hold / resume (ELEVATED) ----------------------------------------------


@pytest.mark.asyncio
async def test_hold_call_runs_when_not_degraded() -> None:
    call = _FakeCall()
    result = await _tools(call).hold_call()
    assert result.allowed is True
    assert call.held == 1


@pytest.mark.asyncio
async def test_hold_call_blocked_when_degraded() -> None:
    call = _FakeCall(degraded=True)
    result = await _tools(call).hold_call()
    assert result.allowed is False
    assert call.held == 0  # the verb never runs when blocked


@pytest.mark.asyncio
async def test_resume_call_runs_when_not_degraded() -> None:
    call = _FakeCall()
    result = await _tools(call).resume_call()
    assert result.allowed is True
    assert call.resumed == 1


# ---- hang up (SAFE) — ADR-0026 ---------------------------------------------


def test_hang_up_call_is_safe_risk() -> None:
    """``hang_up`` is SAFE: any caller's conversation may be concluded by the agent.

    Ending the call mutates no external state and is something even a level-0
    receptionist is told it may do ("end the call politely"). SAFE tools always
    run, so the agent can hang up regardless of privilege or degraded state.
    """
    assert TOOL_RISKS["hang_up"] is ToolRisk.SAFE


@pytest.mark.asyncio
async def test_hang_up_call_runs_and_ends_the_call() -> None:
    """``hang_up`` runs the call's hang_up verb (sends BYE, ends the call)."""
    call = _FakeCall()
    result = await _tools(call).hang_up()
    assert result.allowed is True
    assert call.hung_up == 1


@pytest.mark.asyncio
async def test_hang_up_call_runs_even_when_degraded() -> None:
    """A degraded session can still hang up (SAFE is never gated by degrade)."""
    call = _FakeCall(degraded=True)
    result = await _tools(call).hang_up()
    assert result.allowed is True
    assert call.hung_up == 1


@pytest.mark.asyncio
async def test_hang_up_call_with_no_active_call_is_a_no_op() -> None:
    """Hanging up with no bound call returns a not-allowed result, no crash."""
    result = await _tools(None).hang_up()
    assert result.allowed is False


def test_gate_voip_tool_allows_hang_up_for_unprivileged_caller() -> None:
    """The gate permits ``hang_up`` for a level-0 receptionist (SAFE always runs)."""
    receptionist = GuardSessionState(call_id="c", privilege_level=0)
    assert gate_voip_tool("hang_up", receptionist, confirmed=False) is True


# ---- transfer (IRREVERSIBLE) — invariant 3 ---------------------------------


@pytest.mark.asyncio
async def test_transfer_blind_runs_when_confirmed_and_clean() -> None:
    call = _FakeCall()
    result = await _tools(call, confirmed=True).transfer_blind(
        "sip:3000@pbx.example.test"
    )
    assert result.allowed is True
    assert call.blind_targets == ["sip:3000@pbx.example.test"]


@pytest.mark.asyncio
async def test_transfer_blind_blocked_when_unconfirmed() -> None:
    call = _FakeCall()
    result = await _tools(call, confirmed=False).transfer_blind(
        "sip:3000@pbx.example.test"
    )
    assert result.allowed is False
    assert call.blind_targets == []  # invariant 3: never transfers unconfirmed


@pytest.mark.asyncio
async def test_transfer_blind_blocked_while_degraded_even_if_confirmed() -> None:
    # invariant 3: a degraded session hard-blocks an IRREVERSIBLE transfer even
    # when confirmed and even if the injection guard returned ALLOW.
    call = _FakeCall(degraded=True)
    result = await _tools(call, confirmed=True).transfer_blind(
        "sip:3000@pbx.example.test"
    )
    assert result.allowed is False
    assert call.blind_targets == []


@pytest.mark.asyncio
async def test_transfer_attended_runs_when_confirmed_and_clean() -> None:
    call = _FakeCall()
    consult = _consult()
    result = await _tools(call, confirmed=True).transfer_attended(consult)
    assert result.allowed is True
    assert call.attended == [consult]


@pytest.mark.asyncio
async def test_transfer_attended_blocked_when_unconfirmed() -> None:
    call = _FakeCall()
    result = await _tools(call, confirmed=False).transfer_attended(_consult())
    assert result.allowed is False
    assert call.attended == []


@pytest.mark.asyncio
async def test_transfer_confirmation_is_actively_requested() -> None:
    call = _FakeCall()
    confirmation = _FakeConfirmation(confirmed=True)
    tools = CallControlTools(_manager(), confirmation=confirmation)
    tools.bind_call(call)
    await tools.transfer_blind("sip:3000@pbx.example.test")
    assert confirmation.prompts == 1  # the caller is asked to confirm


@pytest.mark.asyncio
async def test_hold_does_not_request_confirmation() -> None:
    call = _FakeCall()
    confirmation = _FakeConfirmation(confirmed=False)
    tools = CallControlTools(_manager(), confirmation=confirmation)
    tools.bind_call(call)
    await tools.hold_call()
    assert confirmation.prompts == 0  # ELEVATED tools need no confirmation


# ---- no active call --------------------------------------------------------


@pytest.mark.asyncio
async def test_hold_without_active_call_is_blocked() -> None:
    result = await _tools(None).hold_call()
    assert result.allowed is False


@pytest.mark.asyncio
async def test_transfer_without_active_call_is_blocked() -> None:
    result = await _tools(None).transfer_blind("sip:3000@pbx.example.test")
    assert result.allowed is False


class _SwappingConfirmation:
    """Rebinds the active call mid-confirmation (a TOCTOU attempt)."""

    def __init__(self, replacement: _FakeCall) -> None:
        self.replacement = replacement
        self.tools: CallControlTools | None = None

    async def confirm(self) -> bool:
        if self.tools is not None:
            self.tools.bind_call(self.replacement)
        return True


@pytest.mark.asyncio
async def test_transfer_blocked_if_active_call_changes_during_confirmation() -> None:
    # invariant 3 / TOCTOU: if the active call is replaced while confirmation is
    # pending, the transfer must not run on the stale call nor skip the new
    # call's gate (codex HIGH).
    original = _FakeCall()
    replacement = _FakeCall(degraded=True)
    confirmation = _SwappingConfirmation(replacement)
    tools = CallControlTools(_manager(), confirmation=confirmation)
    confirmation.tools = tools
    tools.bind_call(original)
    result = await tools.transfer_blind("sip:3000@pbx.example.test")
    assert result.allowed is False
    assert original.blind_targets == []  # never runs on the captured (stale) call
    assert replacement.blind_targets == []
