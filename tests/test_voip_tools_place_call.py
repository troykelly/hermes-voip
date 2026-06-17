"""Tests for the agent-triggered outbound tools (ADR-0028): place_call + report.

These exercise :mod:`hermes_voip.voip_tools` against a fake ``VoipToolHost`` and a
monkeypatched session-context reader, so they run in the DEFAULT gate (no
hermes-agent runtime), exactly like ``tests/test_voip_tools.py``.

Coverage:

* ``place_call`` is registered, IRREVERSIBLE, and gated: a level-0 (untrusted)
  caller — or a level-2 / degraded session — is BLOCKED; only an operator
  (level-3, clean) session may invoke it (the same posture as transfer).
* ``place_call`` returns ``{"call_id": …}`` IMMEDIATELY (async — it does not await
  the whole call); a listed number dials, an unlisted number is rejected with a
  clear error and never dialled.
* ``report_call_result`` records the call's outcome (SAFE; the call agent records
  its OWN call's outcome, resolved from the session context).
"""

from __future__ import annotations

import json

import pytest

from hermes_voip.originate import OutboundCallNotAllowed
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.voip_tools import (
    PLACE_CALL_TOOL_NAME,
    PLACE_CALL_TOOL_SCHEMA,
    REPORT_RESULT_TOOL_NAME,
    REPORT_RESULT_TOOL_SCHEMA,
    place_call_handler,
    report_call_result_handler,
    set_active_adapter,
    voip_pre_tool_call,
)


class _FakeHost:
    """A fake ``VoipToolHost`` for the outbound tools: records calls + results."""

    def __init__(
        self,
        *,
        guard: GuardSessionState | None = None,
        allowed: bool = True,
        call_id: str = "call-out-1",
    ) -> None:
        self._guard = guard
        self._allowed = allowed
        self._call_id = call_id
        self.placed: list[tuple[str, str]] = []
        self.results: list[tuple[str, str]] = []

    def guard_state_for(self, call_id: str) -> GuardSessionState | None:
        return self._guard

    async def place_call_with_objective(self, number: str, objective: str) -> str:
        if not self._allowed:
            raise OutboundCallNotAllowed(number)
        self.placed.append((number, objective))
        return self._call_id

    def record_call_result(self, call_id: str, summary: str) -> bool:
        self.results.append((call_id, summary))
        return True

    # The remaining VoipToolHost members are unused by this module's tests; they
    # satisfy the protocol so set_active_adapter(host) type-checks.
    async def hang_up_call(self, call_id: str) -> bool:
        return True

    async def hold_call(self, call_id: str) -> bool:
        return True

    async def resume_call(self, call_id: str) -> bool:
        return True

    def list_registrations_text(self) -> str:
        return ""

    async def send_dtmf_on_call(self, call_id: str, digits: str) -> bool:
        return True

    async def open_entry(self, call_id: str) -> bool:
        return True


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


# --- schemas ----------------------------------------------------------------


def test_place_call_schema_names_the_tool_and_declares_params() -> None:
    """place_call's schema names the tool and declares number + objective params."""
    assert PLACE_CALL_TOOL_SCHEMA["name"] == PLACE_CALL_TOOL_NAME
    assert PLACE_CALL_TOOL_SCHEMA["description"]
    params = PLACE_CALL_TOOL_SCHEMA["parameters"]
    assert isinstance(params, dict)
    props = params["properties"]
    assert isinstance(props, dict)
    assert "number" in props
    assert "objective" in props
    # Both are required: a call with no objective would be a mute/aimless call.
    assert set(params.get("required", [])) == {"number", "objective"}


def test_report_result_schema_names_the_tool_and_declares_summary() -> None:
    assert REPORT_RESULT_TOOL_SCHEMA["name"] == REPORT_RESULT_TOOL_NAME
    assert REPORT_RESULT_TOOL_SCHEMA["description"]
    params = REPORT_RESULT_TOOL_SCHEMA["parameters"]
    assert isinstance(params, dict)
    props = params["properties"]
    assert isinstance(props, dict)
    assert "summary" in props


# --- place_call handler -----------------------------------------------------


@pytest.mark.asyncio
async def test_place_call_handler_dials_and_returns_call_id_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listed number dials and the handler returns {call_id} at once (async)."""
    host = _FakeHost(call_id="call-out-42")
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler(
        {"number": "1000", "objective": "book a table for two at 7pm"}
    )

    assert host.placed == [("1000", "book a table for two at 7pm")]
    assert json.loads(result) == {"call_id": "call-out-42"}


@pytest.mark.asyncio
async def test_place_call_handler_rejects_unlisted_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlisted number is rejected with a clear error and is NEVER dialled."""
    host = _FakeHost(allowed=False)
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "9999", "objective": "do a thing"})

    assert host.placed == []  # never dialled
    payload = json.loads(result)
    assert "error" in payload
    assert "9999" in payload["error"]


@pytest.mark.asyncio
async def test_place_call_handler_with_no_adapter_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_chat(monkeypatch, "origin-chat")  # adapter is None (fixture default)
    result = await place_call_handler({"number": "1000", "objective": "x"})
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_place_call_handler_requires_number_and_objective() -> None:
    """Missing/blank arguments produce a clear error, not a malformed dial."""
    host = _FakeHost()
    set_active_adapter(host)

    missing_obj = await place_call_handler({"number": "1000"})
    assert "error" in json.loads(missing_obj)
    missing_num = await place_call_handler({"objective": "x"})
    assert "error" in json.loads(missing_num)
    assert host.placed == []


# --- report_call_result handler ---------------------------------------------


@pytest.mark.asyncio
async def test_report_call_result_records_for_the_session_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """report_call_result records the outcome against the call's own session."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-out-1")

    result = await report_call_result_handler({"summary": "Booked for 7pm, 2 people."})

    assert host.results == [("call-out-1", "Booked for 7pm, 2 people.")]
    assert "result" in json.loads(result)


@pytest.mark.asyncio
async def test_report_call_result_with_no_session_call_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, None)

    result = await report_call_result_handler({"summary": "x"})

    assert host.results == []
    assert "error" in json.loads(result)


# --- the IRREVERSIBLE privilege gate on place_call --------------------------


def test_gate_owns_place_call() -> None:
    """The gate is responsible for place_call (judges it, never defers)."""
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    assert PLACE_CALL_TOOL_NAME in vt._voip_tool_names()


def test_gate_blocks_place_call_for_receptionist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A level-0 (untrusted) caller is BLOCKED from place_call (the security spine).

    An untrusted inbound caller must never be able to make the agent dial out, even
    via a prompt injection that coaxes the model into calling the tool.
    """
    host = _FakeHost(guard=_receptionist())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


def test_gate_blocks_place_call_for_trusted_level_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A level-2 (trusted) caller is still BLOCKED — place_call needs level-3."""
    host = _FakeHost(guard=GuardSessionState(call_id="c", privilege_level=2))
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


def test_gate_blocks_place_call_on_degraded_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded operator session is BLOCKED — a missed injection cannot dial out."""
    host = _FakeHost(
        guard=GuardSessionState(call_id="c", privilege_level=3, degraded=True)
    )
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


def test_gate_allows_place_call_for_clean_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator (level-3, clean) session is ALLOWED to invoke place_call."""
    host = _FakeHost(guard=_operator())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    assert voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={}) is None


def test_gate_fails_safe_for_place_call_when_call_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """place_call with no resolvable session state is BLOCKED (fail safe).

    The env-trigger / cron path has no session context, so the gate sees no call
    state. The agent-tool path is always inside a session; an unknown context here
    must never grant the IRREVERSIBLE dial.
    """
    set_active_adapter(None)
    _set_chat(monkeypatch, None)

    verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"
