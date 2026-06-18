"""Tests for the agent-facing attended (consultative) transfer tool (ADR-0048).

These exercise :mod:`hermes_voip.voip_tools` against a fake ``VoipToolHost`` and a
monkeypatched session-context reader, so they run in the DEFAULT gate (no
hermes-agent runtime) — exactly like ``tests/test_voip_tools_place_call.py``.

``transfer_attended`` is a single tool driving a small state machine via its
``action`` argument:

* ``consult`` — originate the consultation leg to ``target`` (reusing the outbound
  origination path, gated by the SAME outbound allowlist as ``place_call``). Returns
  the consult call id; the agent then converses on it.
* ``complete`` — send the REFER+Replaces (RFC 3891) on the ORIGINAL call so the
  caller is bridged to the consultation target and our legs are released.
* ``cancel`` — abandon the consultation (hang up the consult leg) and keep the
  original caller.

The tool is IRREVERSIBLE: a level-0 / level-2 / degraded session is BLOCKED; only an
operator (level-3, clean) session may invoke it. The handler returns a JSON string on
success AND every error and never raises (the always-JSON contract).
"""

from __future__ import annotations

import json

import pytest

from hermes_voip.originate import OutboundCallFailed, OutboundCallNotAllowed
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.voip_tools import (
    TRANSFER_ATTENDED_TOOL_NAME,
    TRANSFER_ATTENDED_TOOL_SCHEMA,
    AttendedTransferOutcome,
    TransferOutcome,
    set_active_adapter,
    transfer_attended_handler,
    voip_pre_tool_call,
)


class _FakeHost:
    """A fake ``VoipToolHost`` for the attended-transfer tool: records each step."""

    def __init__(
        self,
        *,
        guard: GuardSessionState | None = None,
        allowed: bool = True,
        consult_id: str = "consult-1",
        complete_outcome: AttendedTransferOutcome = (
            AttendedTransferOutcome.TRANSFERRED
        ),
        cancelled: bool = True,
    ) -> None:
        self._guard = guard
        self._allowed = allowed
        self._consult_id = consult_id
        self._complete_outcome = complete_outcome
        self._cancelled = cancelled
        self.consulted: list[tuple[str, str]] = []
        self.completed: list[str] = []
        self.cancelledcalls: list[str] = []

    def guard_state_for(self, call_id: str) -> GuardSessionState | None:
        return self._guard

    async def start_attended_consult(self, call_id: str, target: str) -> str:
        if not self._allowed:
            raise OutboundCallNotAllowed(target)
        self.consulted.append((call_id, target))
        return self._consult_id

    async def complete_attended_transfer(self, call_id: str) -> AttendedTransferOutcome:
        self.completed.append(call_id)
        return self._complete_outcome

    async def cancel_attended_transfer(self, call_id: str) -> bool:
        self.cancelledcalls.append(call_id)
        return self._cancelled

    # The remaining VoipToolHost members are unused here; they satisfy the protocol
    # so set_active_adapter(host) type-checks.
    async def hang_up_call(self, call_id: str) -> bool:
        return True

    async def hold_call(self, call_id: str) -> bool:
        return True

    async def resume_call(self, call_id: str) -> bool:
        return True

    def list_registrations_text(self) -> str:
        return ""

    async def place_call_with_objective(self, number: str, objective: str) -> str:
        return ""

    def record_call_result(self, call_id: str, summary: str) -> bool:
        return True

    async def send_dtmf_on_call(self, call_id: str, digits: str) -> bool:
        return True

    async def open_entry(self, call_id: str) -> bool:
        return True

    async def transfer_blind_on_call(
        self, call_id: str, target: str
    ) -> TransferOutcome:
        return TransferOutcome.TRANSFERRED


@pytest.fixture(autouse=True)
def _reset_active_adapter() -> object:
    set_active_adapter(None)
    yield
    set_active_adapter(None)


def _set_chat(monkeypatch: pytest.MonkeyPatch, call_id: str | None) -> None:
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    monkeypatch.setattr(vt, "_current_call_id", lambda: call_id)


def _operator() -> GuardSessionState:
    return GuardSessionState(call_id="c", privilege_level=3)


def _receptionist() -> GuardSessionState:
    return GuardSessionState(call_id="c", privilege_level=0)


# --- schema -----------------------------------------------------------------


def test_transfer_attended_schema_names_the_tool_and_declares_params() -> None:
    """The schema names the tool and declares the action + target params."""
    assert TRANSFER_ATTENDED_TOOL_SCHEMA["name"] == TRANSFER_ATTENDED_TOOL_NAME
    assert TRANSFER_ATTENDED_TOOL_SCHEMA["description"]
    params = TRANSFER_ATTENDED_TOOL_SCHEMA["parameters"]
    assert isinstance(params, dict)
    props = params["properties"]
    assert isinstance(props, dict)
    assert "action" in props
    assert "target" in props
    # action is an enum over the three modes (no free-text actions).
    action = props["action"]
    assert isinstance(action, dict)
    assert set(action.get("enum", [])) == {"consult", "complete", "cancel"}
    assert params.get("required") == ["action"]


# --- consult ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_originates_the_leg_and_returns_consult_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """action=consult dials the target and returns the consult call id."""
    host = _FakeHost(consult_id="consult-42")
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "consult", "target": "1000"})

    assert host.consulted == [("orig-call", "1000")]
    payload = json.loads(result)
    assert payload.get("consult_call_id") == "consult-42"


@pytest.mark.asyncio
async def test_consult_requires_a_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """action=consult with no target is a clear error and never dials."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "consult"})

    assert host.consulted == []
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_consult_rejects_unlisted_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consult target not on the outbound allowlist is rejected, never dialled."""
    host = _FakeHost(allowed=False)
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "consult", "target": "9999"})

    assert host.consulted == []
    payload = json.loads(result)
    assert "error" in payload
    assert "9999" in payload["error"]


class _RaisingConsultHost(_FakeHost):
    """A fake host whose ``start_attended_consult`` raises a chosen exception.

    Models the place_call failure paths that the consult leg inherits (ADR-0048): the
    dial gate (slot busy / no registered ext / WSS) raises ``OutboundCallFailed`` and
    the un-initialised transport raises ``RuntimeError`` — both escape the previous
    handler (it caught only OutboundCallNotAllowed / PermissionError / KeyError).
    """

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    async def start_attended_consult(self, call_id: str, target: str) -> str:
        raise self._exc


@pytest.mark.asyncio
async def test_consult_renders_outbound_call_failed_as_error_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consult whose dial fails (OutboundCallFailed) returns error JSON, never raises.

    The dial path raises ``OutboundCallFailed`` for a busy slot / no registered
    extension / the WSS transport; the always-JSON contract requires the handler to
    catch it and render ``{"error": …}`` rather than let it escape.
    """
    host = _RaisingConsultHost(OutboundCallFailed(503, "no registered extension"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "consult", "target": "1000"})

    payload = json.loads(result)
    assert "error" in payload


@pytest.mark.asyncio
async def test_consult_renders_uninitialised_transport_as_error_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consult before the transport is up (RuntimeError) returns error JSON.

    ``place_call`` raises ``RuntimeError`` when the transport/manager is not
    initialised; the handler must render it as ``{"error": …}`` (never raise).
    """
    host = _RaisingConsultHost(RuntimeError("transport is not initialised"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "consult", "target": "1000"})

    payload = json.loads(result)
    assert "error" in payload


# --- complete ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_sends_the_refer_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """action=complete bridges the caller to the target (REFER+Replaces)."""
    host = _FakeHost(complete_outcome=AttendedTransferOutcome.TRANSFERRED)
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "complete"})

    assert host.completed == ["orig-call"]
    assert "result" in json.loads(result)


@pytest.mark.asyncio
async def test_complete_without_a_consult_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completing with no consultation in flight is a clear, non-fatal error."""
    host = _FakeHost(complete_outcome=AttendedTransferOutcome.NO_CONSULT)
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "complete"})

    assert "error" in json.loads(result)


# --- cancel -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_abandons_the_consult(monkeypatch: pytest.MonkeyPatch) -> None:
    """action=cancel hangs up the consult leg and keeps the original caller."""
    host = _FakeHost(cancelled=True)
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "cancel"})

    assert host.cancelledcalls == ["orig-call"]
    assert "result" in json.loads(result)


@pytest.mark.asyncio
async def test_cancel_without_a_consult_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost(cancelled=False)
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "cancel"})

    assert "error" in json.loads(result)


# --- always-JSON / never-raise contract -------------------------------------


@pytest.mark.asyncio
async def test_unknown_action_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "frobnicate"})

    assert "error" in json.loads(result)
    assert host.consulted == []
    assert host.completed == []
    assert host.cancelledcalls == []


@pytest.mark.asyncio
async def test_handler_with_no_adapter_returns_error() -> None:
    """No active adapter -> a clear JSON error, never a crash."""
    result = await transfer_attended_handler({"action": "consult", "target": "1000"})
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_handler_with_no_session_call_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, None)

    result = await transfer_attended_handler({"action": "complete"})

    assert "error" in json.loads(result)


# --- the IRREVERSIBLE privilege gate ----------------------------------------


def test_gate_owns_transfer_attended() -> None:
    """The gate is responsible for transfer_attended (judges it, never defers)."""
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    assert TRANSFER_ATTENDED_TOOL_NAME in vt._voip_tool_names()


def test_gate_blocks_transfer_attended_for_receptionist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A level-0 caller is BLOCKED — a prompt injection cannot start a transfer."""
    host = _FakeHost(guard=_receptionist())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(tool_name=TRANSFER_ATTENDED_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


def test_gate_blocks_transfer_attended_for_trusted_level_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A level-2 caller is still BLOCKED — attended transfer needs level-3."""
    host = _FakeHost(guard=GuardSessionState(call_id="c", privilege_level=2))
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(tool_name=TRANSFER_ATTENDED_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


def test_gate_blocks_transfer_attended_on_degraded_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded operator session is BLOCKED — a missed injection cannot transfer."""
    host = _FakeHost(
        guard=GuardSessionState(call_id="c", privilege_level=3, degraded=True)
    )
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(tool_name=TRANSFER_ATTENDED_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


def test_gate_allows_transfer_attended_for_clean_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator (level-3, clean) session is ALLOWED to invoke transfer_attended."""
    host = _FakeHost(guard=_operator())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    assert voip_pre_tool_call(tool_name=TRANSFER_ATTENDED_TOOL_NAME, args={}) is None


def test_gate_fails_safe_for_transfer_attended_when_call_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No resolvable session state -> BLOCKED (fail safe)."""
    set_active_adapter(None)
    _set_chat(monkeypatch, None)

    verdict = voip_pre_tool_call(tool_name=TRANSFER_ATTENDED_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"
