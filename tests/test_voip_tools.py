"""Tests for the agent VoIP tool wiring (hang_up handler + gate) — ADR-0026.

These exercise :mod:`hermes_voip.voip_tools` against a fake ``VoipToolHost`` and a
monkeypatched session-context reader, so they run in the DEFAULT gate (no
hermes-agent runtime). The plugin-registration side (``register(ctx)`` calling
``ctx.register_tool``) is covered by ``tests/test_register.py``.
"""

from __future__ import annotations

import json

import pytest

from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.voip_tools import (
    HANG_UP_TOOL_NAME,
    HANG_UP_TOOL_SCHEMA,
    active_voip_adapter,
    hang_up_handler,
    set_active_adapter,
    voip_pre_tool_call,
)


class _FakeHost:
    """A fake ``VoipToolHost``: records hang_up calls and serves guard state."""

    def __init__(
        self,
        *,
        known: bool = True,
        already_ended: bool = False,
        guard: GuardSessionState | None = None,
    ) -> None:
        self.known = known
        self.already_ended = already_ended
        self._guard = guard
        self.hung_up: list[str] = []

    def guard_state_for(self, call_id: str) -> GuardSessionState | None:
        return self._guard if self.known else None

    async def hang_up_call(self, call_id: str) -> bool:
        self.hung_up.append(call_id)
        return self.known and not self.already_ended


@pytest.fixture(autouse=True)
def _reset_active_adapter() -> object:
    """Isolate the process-wide active-adapter seam between tests."""
    set_active_adapter(None)
    yield
    set_active_adapter(None)


def _set_chat(monkeypatch: pytest.MonkeyPatch, call_id: str | None) -> None:
    """Make ``_current_call_id`` return ``call_id`` (None → no session)."""
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    monkeypatch.setattr(vt, "_current_call_id", lambda: call_id)


def test_set_and_get_active_adapter_roundtrip() -> None:
    host = _FakeHost()
    set_active_adapter(host)
    assert active_voip_adapter() is host
    set_active_adapter(None)
    assert active_voip_adapter() is None


def test_hang_up_schema_names_the_tool_and_describes_it() -> None:
    assert HANG_UP_TOOL_SCHEMA["name"] == HANG_UP_TOOL_NAME
    assert HANG_UP_TOOL_SCHEMA["description"]
    # No parameters — the call to end is the session's own call, not model-chosen.
    params = HANG_UP_TOOL_SCHEMA["parameters"]
    assert isinstance(params, dict)
    assert params.get("properties") == {}


@pytest.mark.asyncio
async def test_hang_up_handler_ends_the_session_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler ends exactly the current session's call (its chat_id == Call-ID)."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await hang_up_handler({})

    assert host.hung_up == ["call-xyz"]
    assert json.loads(result) == {"result": "Call ended."}


@pytest.mark.asyncio
async def test_hang_up_handler_with_no_adapter_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live adapter → a clear error result, never a crash."""
    _set_chat(monkeypatch, "call-xyz")  # adapter is None (fixture default)
    result = await hang_up_handler({})
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_hang_up_handler_with_no_session_call_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No call in scope (no chat_id) → an error result, no call ended."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, None)

    result = await hang_up_handler({})

    assert host.hung_up == []
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_hang_up_handler_already_ended_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the call already ended, the handler reports it (does not claim success)."""
    host = _FakeHost(already_ended=True)
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await hang_up_handler({})

    assert "error" in json.loads(result)


def test_pre_tool_call_ignores_non_voip_tools() -> None:
    """The gate defers (returns None) for a tool it does not own."""
    assert voip_pre_tool_call(tool_name="some_other_tool", args={}) is None


def test_pre_tool_call_allows_hang_up_for_receptionist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hang_up (SAFE) is allowed even for a level-0 receptionist call."""
    host = _FakeHost(guard=GuardSessionState(call_id="c", privilege_level=0))
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")
    assert voip_pre_tool_call(tool_name=HANG_UP_TOOL_NAME, args={}) is None


def test_pre_tool_call_fails_safe_when_call_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown call context still resolves hang_up via a least-privilege state.

    hang_up is SAFE so it is allowed even under the fail-safe receptionist state;
    the test pins that the gate does not CRASH when the call/adapter is out of
    scope (it builds a level-0 state rather than dereferencing None).
    """
    set_active_adapter(None)
    _set_chat(monkeypatch, None)
    # SAFE → allowed (None) even with no resolvable state; the key property is no
    # crash and no accidental privilege grant.
    assert voip_pre_tool_call(tool_name=HANG_UP_TOOL_NAME, args={}) is None
