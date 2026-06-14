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
        self.guard = GuardSessionState(call_id="call-1", degraded=degraded)
        self.held = 0
        self.resumed = 0
        self.blind_targets: list[str] = []
        self.attended: list[Dialog] = []

    async def hold(self) -> None:
        self.held += 1

    async def unhold(self) -> None:
        self.resumed += 1

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
    assert TOOL_RISKS["list_registrations"] is ToolRisk.SAFE


def test_gate_voip_tool_unknown_tool_denied() -> None:
    state = GuardSessionState(call_id="call-1")
    assert gate_voip_tool("delete_everything", state, confirmed=True) is False


def test_gate_voip_tool_maps_to_gate_tool_call() -> None:
    clean = GuardSessionState(call_id="call-1")
    degraded = GuardSessionState(call_id="call-1", degraded=True)
    assert gate_voip_tool("list_registrations", degraded, confirmed=False) is True
    assert gate_voip_tool("hold_call", clean, confirmed=False) is True
    assert gate_voip_tool("hold_call", degraded, confirmed=False) is False
    assert gate_voip_tool("transfer_blind", clean, confirmed=True) is True
    assert gate_voip_tool("transfer_blind", clean, confirmed=False) is False
    assert gate_voip_tool("transfer_blind", degraded, confirmed=True) is False


# ---- list_registrations (SAFE) ---------------------------------------------


@pytest.mark.asyncio
async def test_list_registrations_always_allowed() -> None:
    tools = _tools(None)
    result = await tools.list_registrations()
    assert result.allowed is True
    assert "1000" in result.message
    assert "1001" in result.message


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
