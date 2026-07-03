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
import logging

import pytest

from hermes_voip.originate import OutboundCallNotAllowed
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.voip_tools import (
    OPEN_ENTRY_TOOL_NAME,
    PLACE_CALL_TOOL_NAME,
    PLACE_CALL_TOOL_SCHEMA,
    REPORT_RESULT_TOOL_NAME,
    REPORT_RESULT_TOOL_SCHEMA,
    SEND_DTMF_TOOL_NAME,
    TRANSFER_BLIND_TOOL_NAME,
    AttendedTransferOutcome,
    ProactiveDenyReason,
    TransferOutcome,
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

    async def place_call_with_objective(
        self,
        number: str,
        objective: str,
        *,
        ring_timeout_secs: float | None = None,
    ) -> str:
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

    async def open_entry(self, call_id: str, name: str | None = None) -> bool:
        return True

    async def transfer_blind_on_call(
        self, call_id: str, target: str
    ) -> TransferOutcome:
        return TransferOutcome.TRANSFERRED

    async def start_attended_consult(self, call_id: str, target: str) -> str:
        return ""

    async def complete_attended_transfer(self, call_id: str) -> AttendedTransferOutcome:
        return AttendedTransferOutcome.TRANSFERRED

    async def cancel_attended_transfer(self, call_id: str) -> bool:
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


# --- proactive place_call from a configured operator origin (issue #202) -----
#
# HERMES_VOIP_PROACTIVE_CALL_FROM lets a place_call originate from a NON-VoIP
# session (e.g. a Telegram chat turn) whose (platform, chat_id) matches a
# configured operator origin — and ONLY place_call, ONLY in the no-live-call
# branch. The static HERMES_VOIP_OUTBOUND_ALLOW allowlist (ADR-0029) still gates
# the dial target at the chokepoint regardless. Empty/unset => byte-identical to
# the fully fail-safe default. Public-repo: fake origins only (telegram:123).


def _set_origin(
    monkeypatch: pytest.MonkeyPatch, platform: str | None, chat_id: str | None
) -> None:
    """Install a fake ``gateway.session_context`` exposing the originating session.

    The proactive gate reads the originating ``(platform, chat_id)`` via a lazy
    ``from gateway.session_context import get_session_env``; the default test gate
    has no hermes runtime, so inject a minimal fake module whose ``get_session_env``
    returns the supplied originating values (and ``""`` for anything else, matching
    the real reader's empty-string-for-unset contract).
    """
    import sys  # noqa: PLC0415
    from types import ModuleType  # noqa: PLC0415

    module = ModuleType("gateway.session_context")
    values = {
        "HERMES_SESSION_PLATFORM": platform or "",
        "HERMES_SESSION_CHAT_ID": chat_id or "",
    }

    def _get_session_env(key: str) -> str:
        return values.get(key, "")

    # Attribute assignment on a fresh ModuleType — the gate does ``from
    # gateway.session_context import get_session_env``, so the name must resolve.
    # ``setattr`` (not ``module.x = ...``) keeps this mypy-strict clean without an
    # escape hatch: a ModuleType exposes no static stub for an injected attribute.
    setattr(module, "get_session_env", _get_session_env)  # noqa: B010
    monkeypatch.setitem(sys.modules, "gateway", ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)


def test_proactive_place_call_reaches_relaxation_on_real_telegram_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (#202 was unreachable): the REAL proactive flow must be ALLOWED.

    On a genuine Telegram turn the session ``chat_id`` (== ``_current_call_id()``) is a
    NON-None value, because the gate's Call-ID read and the proactive origin read both
    resolve the SAME ``HERMES_SESSION_CHAT_ID``. The relaxation must be reached whenever
    the guard ``state is None`` (no live SIP call), NOT only when ``call_id is None``
    (which never holds on this path). No ``_set_chat`` here — ``_set_origin`` drives
    both reads via the shared fake ``gateway.session_context``, exactly as at runtime.
    """
    monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:123")
    set_active_adapter(None)  # no live SIP call in scope
    _set_origin(monkeypatch, "telegram", "123")

    assert voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={}) is None


def test_voip_tools_gate_proactive_place_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching operator origin unblocks a proactive (no-live-call) place_call.

    With ``HERMES_VOIP_PROACTIVE_CALL_FROM`` set to the originating
    ``platform:chat_id`` and NO live SIP call in scope, ``place_call`` is ALLOWED
    (the gate returns ``None``). With no originating session context, it stays
    blocked — the fail-safe is only relaxed for a configured origin.
    """
    monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:123")
    set_active_adapter(None)  # no live SIP call / adapter in scope
    _set_chat(monkeypatch, None)  # the Telegram chat_id is NOT a SIP Call-ID
    _set_origin(monkeypatch, "telegram", "123")

    assert voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={}) is None

    # No originating session context at all => still blocked (nothing to match).
    _set_origin(monkeypatch, None, None)
    verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})
    assert verdict is not None
    assert verdict["action"] == "block"


def test_voip_tools_gate_proactive_blocked_when_live_call_in_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching origin does NOT unblock when a live Call-ID is in scope.

    ``state is None`` covers TWO cases: (a) no live call at all (true proactive —
    the only case the relaxation is for), and (b) a live/claimed Call-ID IS in
    scope but ``guard_state_for`` MISSED (the unknown/spoofed inbound-call fail-safe
    path, which MUST stay level 0). The proactive relaxation must require case (a)
    only: with a Call-ID present and its guard state missing, a configured origin
    must NOT grant level 3 — otherwise the inbound fail-safe is weakened.
    """
    monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:123")
    host = _FakeHost(guard=None)  # adapter present, guard_state_for() misses
    set_active_adapter(host)
    _set_chat(monkeypatch, "some-live-call-id")  # a Call-ID IS in scope
    _set_origin(monkeypatch, "telegram", "123")  # and the origin matches

    verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


@pytest.mark.parametrize(
    "tool_name",
    [TRANSFER_BLIND_TOOL_NAME, SEND_DTMF_TOOL_NAME, OPEN_ENTRY_TOOL_NAME],
)
def test_voip_tools_gate_proactive_not_place_call(
    monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    """A matching operator origin unblocks ONLY place_call — never the others.

    ``transfer_blind`` / ``send_dtmf`` / ``open_entry`` are meaningless without a
    live call and MUST stay blocked in the no-live-call branch even from a
    configured operator origin (the relaxation is place_call-scoped).
    """
    monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:123")
    set_active_adapter(None)
    _set_chat(monkeypatch, None)
    _set_origin(monkeypatch, "telegram", "123")

    verdict = voip_pre_tool_call(tool_name=tool_name, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


def test_voip_tools_gate_proactive_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset ``HERMES_VOIP_PROACTIVE_CALL_FROM`` => place_call blocked (byte-identical).

    With the opt-in env unset (the default), a place_call from a Telegram session
    is blocked exactly as today — the fail-safe is fully intact.
    """
    monkeypatch.delenv("HERMES_VOIP_PROACTIVE_CALL_FROM", raising=False)
    set_active_adapter(None)
    _set_chat(monkeypatch, None)
    _set_origin(monkeypatch, "telegram", "123")

    verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


def test_voip_tools_gate_proactive_wrong_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-matching origin is NOT unblocked (scoped to the configured pair).

    ``HERMES_VOIP_PROACTIVE_CALL_FROM="telegram:999"`` must not permit a
    ``telegram:123`` origin — permission is tied to the exact ``platform:chat_id``.
    """
    monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:999")
    set_active_adapter(None)
    _set_chat(monkeypatch, None)
    _set_origin(monkeypatch, "telegram", "123")

    verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


# --- proactive place_call deny-reason diagnostics (issue #414) ---------------
#
# The gate stays byte-identically fail-closed (deny in exactly the same cases),
# but each denial now emits ONE structured, machine-parseable log event keyed on
# ``event="proactive_place_call_gate"`` carrying only the NON-SENSITIVE deny-reason
# CATEGORY. The originating platform / chat_id / allowlist entries are secrets and
# MUST NEVER appear in the record — only the category token. Public-repo: fake
# origins only (telegram:123 / telegram:999).


def _proactive_gate_records(
    records: list[logging.LogRecord],
) -> list[logging.LogRecord]:
    """The structured proactive-gate deny events emitted during the call."""
    event = "proactive_place_call_gate"
    return [r for r in records if getattr(r, "event", None) == event]


def test_gate_proactive_deny_logs_allow_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Opt-in env unset => deny logs ``proactive_allow_unset`` (still blocked)."""
    monkeypatch.delenv("HERMES_VOIP_PROACTIVE_CALL_FROM", raising=False)
    set_active_adapter(None)
    _set_chat(monkeypatch, None)
    _set_origin(monkeypatch, "telegram", "123")

    with caplog.at_level(logging.WARNING, logger="hermes_voip.voip_tools"):
        verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"
    events = _proactive_gate_records(caplog.records)
    assert len(events) == 1
    assert (
        getattr(events[0], "reason", None)
        == ProactiveDenyReason.PROACTIVE_ALLOW_UNSET.value
    )


def test_gate_proactive_deny_logs_origin_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Opt-in set but no readable origin => reason ``origin_unavailable``."""
    monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:123")
    set_active_adapter(None)
    _set_chat(monkeypatch, None)
    _set_origin(monkeypatch, None, None)  # no originating platform/chat_id

    with caplog.at_level(logging.WARNING, logger="hermes_voip.voip_tools"):
        verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"
    events = _proactive_gate_records(caplog.records)
    assert len(events) == 1
    assert (
        getattr(events[0], "reason", None)
        == ProactiveDenyReason.ORIGIN_UNAVAILABLE.value
    )


def test_gate_proactive_deny_logs_origin_not_allowlisted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Opt-in set, origin readable but not listed => ``origin_not_allowlisted``."""
    monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:999")
    set_active_adapter(None)
    _set_chat(monkeypatch, None)
    _set_origin(monkeypatch, "telegram", "123")

    with caplog.at_level(logging.WARNING, logger="hermes_voip.voip_tools"):
        verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"
    events = _proactive_gate_records(caplog.records)
    assert len(events) == 1
    assert (
        getattr(events[0], "reason", None)
        == ProactiveDenyReason.ORIGIN_NOT_ALLOWLISTED.value
    )


def test_gate_proactive_deny_logs_live_call_guard_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Live Call-ID present but guard state missing => ``live_call_guard_missing``.

    The inbound fail-safe deliberately bypasses proactive-origin relaxation here;
    the diagnostic distinguishes this from a genuine proactive attempt.
    """
    monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:123")
    host = _FakeHost(guard=None)  # adapter present, guard_state_for() misses
    set_active_adapter(host)
    _set_chat(monkeypatch, "some-live-call-id")  # a Call-ID IS in scope
    _set_origin(monkeypatch, "telegram", "123")

    with caplog.at_level(logging.WARNING, logger="hermes_voip.voip_tools"):
        verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"
    events = _proactive_gate_records(caplog.records)
    assert len(events) == 1
    assert (
        getattr(events[0], "reason", None)
        == ProactiveDenyReason.LIVE_CALL_GUARD_MISSING.value
    )


def test_gate_proactive_deny_logs_unsupported_tool(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-place_call tool in the no-live-call branch => ``unsupported_tool_...``."""
    monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:123")
    set_active_adapter(None)
    _set_chat(monkeypatch, None)
    _set_origin(monkeypatch, "telegram", "123")  # a matching origin

    with caplog.at_level(logging.WARNING, logger="hermes_voip.voip_tools"):
        verdict = voip_pre_tool_call(tool_name=TRANSFER_BLIND_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"
    events = _proactive_gate_records(caplog.records)
    assert len(events) == 1
    assert (
        getattr(events[0], "reason", None)
        == ProactiveDenyReason.UNSUPPORTED_TOOL_FOR_PROACTIVE_ORIGIN.value
    )


def test_gate_proactive_allow_emits_no_deny_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A matching origin still ALLOWS place_call and emits NO deny event."""
    monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:123")
    set_active_adapter(None)
    _set_chat(monkeypatch, None)
    _set_origin(monkeypatch, "telegram", "123")

    with caplog.at_level(logging.WARNING, logger="hermes_voip.voip_tools"):
        verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    assert verdict is None  # unaffected: proactive call still permitted
    assert _proactive_gate_records(caplog.records) == []


def test_gate_proactive_deny_log_never_leaks_origin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The deny event carries the category ONLY — never a sensitive origin value.

    The originating platform, chat_id, and ``HERMES_VOIP_PROACTIVE_CALL_FROM``
    allowlist entries are secrets: none may appear in the proactive-gate event
    surface (message + structured fields the event owns).
    """
    monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:999")
    set_active_adapter(None)
    _set_chat(monkeypatch, None)
    _set_origin(monkeypatch, "telegram", "123")

    with caplog.at_level(logging.WARNING, logger="hermes_voip.voip_tools"):
        voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})

    sensitive = ("telegram", "123", "999", "telegram:999")
    events = _proactive_gate_records(caplog.records)
    assert len(events) == 1
    # Scan only the log surface controlled by the proactive-gate event: rendered
    # message + structured extras. Do NOT stringify the whole LogRecord dict — stdlib
    # metadata such as relativeCreated/process/thread can coincidentally contain a
    # short numeric sentinel (CI hit `999` in a timestamp), causing a false redaction
    # failure even when the production event is clean.
    for record in events:
        surface = [record.getMessage()]
        for field in ("event", "reason", "tool"):
            value = getattr(record, field, None)
            if value is not None:
                surface.append(str(value))
        blob = " | ".join(surface)
        for secret in sensitive:
            assert secret not in blob
