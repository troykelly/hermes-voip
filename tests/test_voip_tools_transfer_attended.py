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
from hermes_voip.refer import TransferUnknownReason
from hermes_voip.voip_tools import (
    TRANSFER_ATTENDED_TOOL_NAME,
    TRANSFER_ATTENDED_TOOL_SCHEMA,
    AttendedTransferOutcome,
    AttendedTransferResult,
    TransferOutcome,
    TransferResult,
    set_active_adapter,
    transfer_attended_handler,
    voip_pre_tool_call,
)


class _FakeHost:
    """A fake ``VoipToolHost`` for the attended-transfer tool: records each step."""

    def __init__(  # noqa: PLR0913 — a test fake wiring up several independent, keyword-only outcome knobs (guard/allowed/consult-id/complete-outcome + ADR-0109 notify status/reason/timeout/cancelled)
        self,
        *,
        guard: GuardSessionState | None = None,
        allowed: bool = True,
        consult_id: str = "consult-1",
        complete_outcome: AttendedTransferOutcome = (AttendedTransferOutcome.COMPLETED),
        complete_notify_status: int | None = None,
        complete_notify_reason: str | None = None,
        complete_timeout_secs: float | None = None,
        complete_unknown_reason: TransferUnknownReason | None = None,
        cancelled: bool = True,
    ) -> None:
        self._guard = guard
        self._allowed = allowed
        self._consult_id = consult_id
        self._complete_outcome = complete_outcome
        # ADR-0109: the terminal NOTIFY detail + bounded wait the result carries.
        self._complete_notify_status = complete_notify_status
        self._complete_notify_reason = complete_notify_reason
        self._complete_timeout_secs = complete_timeout_secs
        # ADR-0109 P2: the discriminated OUTCOME_UNKNOWN reason the message reflects.
        self._complete_unknown_reason = complete_unknown_reason
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

    async def complete_attended_transfer(self, call_id: str) -> AttendedTransferResult:
        self.completed.append(call_id)
        return AttendedTransferResult(
            outcome=self._complete_outcome,
            notify_status=self._complete_notify_status,
            notify_reason=self._complete_notify_reason,
            timeout_secs=self._complete_timeout_secs,
            unknown_reason=self._complete_unknown_reason,
        )

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

    async def place_call_with_objective(
        self,
        number: str,
        objective: str,
        *,
        ring_timeout_secs: float | None = None,
    ) -> str:
        return ""

    def record_call_result(self, call_id: str, summary: str) -> bool:
        return True

    async def send_dtmf_on_call(self, call_id: str, digits: str) -> bool:
        return True

    async def open_entry(self, call_id: str, name: str | None = None) -> bool:
        return True

    async def transfer_blind_on_call(self, call_id: str, target: str) -> TransferResult:
        return TransferResult(outcome=TransferOutcome.COMPLETED)


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
    host = _FakeHost(complete_outcome=AttendedTransferOutcome.COMPLETED)
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


# --- ADR-0109: the terminal attended-transfer outcome surfaced to the agent --


@pytest.mark.asyncio
async def test_complete_completed_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COMPLETED attended transfer (terminal 2xx NOTIFY) reports a success result."""
    host = _FakeHost(complete_outcome=AttendedTransferOutcome.COMPLETED)
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "complete"})

    assert json.loads(result) == {
        "result": "Caller connected to the consultation target."
    }


@pytest.mark.asyncio
async def test_complete_failed_reports_status_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FAILED attended transfer surfaces the SIP status + reason as a tool error."""
    host = _FakeHost(
        complete_outcome=AttendedTransferOutcome.FAILED,
        complete_notify_status=486,
        complete_notify_reason="Busy Here",
    )
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "complete"})

    assert json.loads(result) == {"error": "Transfer failed: 486 Busy Here."}


@pytest.mark.asyncio
async def test_complete_outcome_unknown_reports_initiated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OUTCOME_UNKNOWN attended transfer keeps the honest 'initiated' wording."""
    host = _FakeHost(
        complete_outcome=AttendedTransferOutcome.OUTCOME_UNKNOWN,
        complete_timeout_secs=20.0,
    )
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "complete"})

    assert json.loads(result) == {
        "result": "Transfer initiated; outcome not confirmed within 20s."
    }


@pytest.mark.asyncio
async def test_complete_unknown_timeout_reports_within(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OUTCOME_UNKNOWN TIMEOUT names the bounded wait (ADR-0109 P2)."""
    host = _FakeHost(
        complete_outcome=AttendedTransferOutcome.OUTCOME_UNKNOWN,
        complete_unknown_reason=TransferUnknownReason.TIMEOUT,
        complete_timeout_secs=20.0,
    )
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "complete"})

    assert json.loads(result) == {
        "result": "Transfer initiated; outcome not confirmed within 20s."
    }


@pytest.mark.asyncio
async def test_complete_unknown_declined_reports_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declined RFC 4488 subscription reports the peer declined it (ADR-0109 P2)."""
    host = _FakeHost(
        complete_outcome=AttendedTransferOutcome.OUTCOME_UNKNOWN,
        complete_unknown_reason=TransferUnknownReason.SUBSCRIPTION_DECLINED,
        complete_timeout_secs=20.0,
    )
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "complete"})

    expected = (
        "Transfer initiated; the peer declined the transfer-progress "
        "subscription, so the final outcome was not reported."
    )
    assert json.loads(result) == {"result": expected}


@pytest.mark.asyncio
async def test_complete_unknown_call_ended_reports_call_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leg torn down before the outcome reports the call ended (ADR-0109 P2)."""
    host = _FakeHost(
        complete_outcome=AttendedTransferOutcome.OUTCOME_UNKNOWN,
        complete_unknown_reason=TransferUnknownReason.CALL_ENDED,
        complete_timeout_secs=20.0,
    )
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "complete"})

    expected = (
        "Transfer initiated; the call ended before the transfer outcome was reported."
    )
    assert json.loads(result) == {"result": expected}


@pytest.mark.asyncio
async def test_complete_unknown_wait_disabled_reports_initiated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled outcome confirmation (timeout 0) reports only 'initiated' (P2)."""
    host = _FakeHost(
        complete_outcome=AttendedTransferOutcome.OUTCOME_UNKNOWN,
        complete_unknown_reason=TransferUnknownReason.WAIT_DISABLED,
        complete_timeout_secs=0.0,
    )
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "complete"})

    assert json.loads(result) == {"result": "Transfer initiated."}


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


# --- never-raise on a non-CallError transport fault (VT-1) -------------------
#
# ADR-0048 §"Always-JSON, never-raise handler contract" guarantees the handler
# returns JSON on EVERY error and never raises. Each action helper anticipates only
# SOME exceptions (consult: OutboundCall*/PermissionError/KeyError/RuntimeError;
# complete: CallError; cancel: none), so a non-CallError transport fault from the
# dial/REFER (OSError/ConnectionError/TimeoutError) escaped the tool call before the
# outer guard. These pin the never-raise contract for all three actions.


class _RaisingCompleteHost(_FakeHost):
    """A fake host whose ``complete_attended_transfer`` raises a chosen exception."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    async def complete_attended_transfer(self, call_id: str) -> AttendedTransferResult:
        raise self._exc


class _RaisingCancelHost(_FakeHost):
    """A fake host whose ``cancel_attended_transfer`` raises a chosen exception."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    async def cancel_attended_transfer(self, call_id: str) -> bool:
        raise self._exc


@pytest.mark.asyncio
async def test_consult_transport_fault_is_rendered_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-CallError consult fault renders as error JSON, never raises (VT-1)."""
    host = _RaisingConsultHost(OSError("tls broke"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "consult", "target": "1000"})

    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_complete_transport_fault_is_rendered_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-CallError complete fault renders as error JSON, never raises (VT-1)."""
    host = _RaisingCompleteHost(OSError("tls broke"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "complete"})

    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_cancel_transport_fault_is_rendered_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-CallError cancel fault renders as error JSON, never raises (VT-1)."""
    host = _RaisingCancelHost(OSError("tls broke"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "orig-call")

    result = await transfer_attended_handler({"action": "cancel"})

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
