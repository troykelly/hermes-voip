"""Tests for the agent VoIP tool wiring (handlers + gate) — ADR-0026 / ADR-0011.

These exercise :mod:`hermes_voip.voip_tools` against a fake ``VoipToolHost`` and a
monkeypatched session-context reader, so they run in the DEFAULT gate (no
hermes-agent runtime). The plugin-registration side (``register(ctx)`` calling
``ctx.register_tool``) is covered by ``tests/test_register.py``.

Beyond ``hang_up`` (ADR-0026) this also covers the in-call control tools exposed
through the same mechanism (ADR-0011 §3): ``hold_call`` / ``resume_call`` and
``list_registrations`` (all ``ELEVATED``). The IRREVERSIBLE transfer tools
(``transfer_blind`` — ADR-0010; ``transfer_attended`` — ADR-0048) are exposed and
owned by the gate; the tests assert the gate blocks them for an unprivileged caller.
"""

from __future__ import annotations

import json

import pytest

from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.voip_tools import (
    HANG_UP_TOOL_NAME,
    HANG_UP_TOOL_SCHEMA,
    HOLD_TOOL_NAME,
    HOLD_TOOL_SCHEMA,
    LIST_REGISTRATIONS_TOOL_NAME,
    LIST_REGISTRATIONS_TOOL_SCHEMA,
    OPEN_ENTRY_TOOL_NAME,
    OPEN_ENTRY_TOOL_SCHEMA,
    RESUME_TOOL_NAME,
    RESUME_TOOL_SCHEMA,
    SEND_DTMF_TOOL_NAME,
    SEND_DTMF_TOOL_SCHEMA,
    TRANSFER_BLIND_TOOL_NAME,
    TRANSFER_BLIND_TOOL_SCHEMA,
    AttendedTransferOutcome,
    TransferOutcome,
    active_voip_adapter,
    hang_up_handler,
    hold_call_handler,
    list_registrations_handler,
    open_entry_handler,
    resume_call_handler,
    send_dtmf_handler,
    set_active_adapter,
    transfer_blind_handler,
    voip_pre_tool_call,
)


class _FakeHost:
    """A fake ``VoipToolHost``: records control calls and serves guard state."""

    def __init__(
        self,
        *,
        known: bool = True,
        already_ended: bool = False,
        guard: GuardSessionState | None = None,
        registrations: str = "1000: registered",
    ) -> None:
        self.known = known
        self.already_ended = already_ended
        self._guard = guard
        self._registrations = registrations
        self.hung_up: list[str] = []
        self.held: list[str] = []
        self.resumed: list[str] = []
        self.listed: int = 0
        self.placed: list[tuple[str, str]] = []
        self.results: list[tuple[str, str]] = []
        self.dtmf_sent: list[tuple[str, str]] = []
        self.entries_opened: list[str] = []
        self.transfers: list[tuple[str, str]] = []
        # When set, send_dtmf_on_call raises this instead of recording (e.g. the
        # telephone-event-not-negotiated RuntimeError).
        self.dtmf_raises: Exception | None = None
        # transfer_blind_on_call outcome (ADR-0010/0031): the host method returns a
        # tri-state so the handler can distinguish a fired transfer from an
        # unconfirmed one from an unknown/ended call. Override per-test.
        self.transfer_outcome: TransferOutcome = TransferOutcome.TRANSFERRED
        # When set, transfer_blind_on_call raises this (e.g. a REFER-rejected CallError
        # or the no-DTMF RuntimeError) so the handler can render a clear tool error.
        self.transfer_raises: Exception | None = None

    def guard_state_for(self, call_id: str) -> GuardSessionState | None:
        return self._guard if self.known else None

    async def hang_up_call(self, call_id: str) -> bool:
        self.hung_up.append(call_id)
        return self.known and not self.already_ended

    async def hold_call(self, call_id: str) -> bool:
        self.held.append(call_id)
        return self.known and not self.already_ended

    async def resume_call(self, call_id: str) -> bool:
        self.resumed.append(call_id)
        return self.known and not self.already_ended

    def list_registrations_text(self) -> str:
        self.listed += 1
        return self._registrations

    async def place_call_with_objective(self, number: str, objective: str) -> str:
        # ADR-0029 VoipToolHost member (unused by this module's tests; satisfies the
        # protocol so set_active_adapter type-checks).
        self.placed.append((number, objective))
        return "call-out"

    def record_call_result(self, call_id: str, summary: str) -> bool:
        # ADR-0029 VoipToolHost member (unused by this module's tests).
        self.results.append((call_id, summary))
        return self.known and not self.already_ended

    async def send_dtmf_on_call(self, call_id: str, digits: str) -> bool:
        # ADR-0031 VoipToolHost member: in-call DTMF send.
        if self.dtmf_raises is not None:
            raise self.dtmf_raises
        self.dtmf_sent.append((call_id, digits))
        return self.known and not self.already_ended

    async def open_entry(self, call_id: str) -> bool:
        # ADR-0031 VoipToolHost member: actuate the intercom entry.
        self.entries_opened.append(call_id)
        return self.known and not self.already_ended

    async def transfer_blind_on_call(
        self, call_id: str, target: str
    ) -> TransferOutcome:
        # ADR-0010/0031 VoipToolHost member: DTMF-confirmed blind transfer (REFER).
        if self.transfer_raises is not None:
            raise self.transfer_raises
        if not self.known or self.already_ended:
            return TransferOutcome.NO_CALL
        # Only record the (call, target) when the configured outcome is a fired
        # transfer — an unconfirmed outcome must leave no REFER trace.
        if self.transfer_outcome is TransferOutcome.TRANSFERRED:
            self.transfers.append((call_id, target))
        return self.transfer_outcome

    # ADR-0048 VoipToolHost members (attended transfer): unused by this module's
    # tests but present so set_active_adapter(host) type-checks against the protocol.
    async def start_attended_consult(self, call_id: str, target: str) -> str:
        return ""

    async def complete_attended_transfer(self, call_id: str) -> AttendedTransferOutcome:
        return AttendedTransferOutcome.TRANSFERRED

    async def cancel_attended_transfer(self, call_id: str) -> bool:
        return True


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


# ---------------------------------------------------------------------------
# In-call control tools: hold_call / resume_call / list_registrations (ADR-0011)
# ---------------------------------------------------------------------------


def _operator() -> GuardSessionState:
    """An operator (level-3, clean) call state — allows the ELEVATED control tools."""
    return GuardSessionState(call_id="c", privilege_level=3)


def _receptionist() -> GuardSessionState:
    """A receptionist (level-0, clean) call state — SAFE only."""
    return GuardSessionState(call_id="c", privilege_level=0)


def test_control_tool_schemas_name_the_tools_and_take_no_params() -> None:
    """Each control tool's schema names the tool, describes it, and takes no params.

    The call to act on is the session's own call (resolved from the session
    context), never a model-chosen target — so none of these tools expose a
    parameter the model could point at another call.
    """
    for name, schema in (
        (HOLD_TOOL_NAME, HOLD_TOOL_SCHEMA),
        (RESUME_TOOL_NAME, RESUME_TOOL_SCHEMA),
        (LIST_REGISTRATIONS_TOOL_NAME, LIST_REGISTRATIONS_TOOL_SCHEMA),
    ):
        assert schema["name"] == name
        assert schema["description"]
        params = schema["parameters"]
        assert isinstance(params, dict)
        assert params.get("properties") == {}


@pytest.mark.asyncio
async def test_hold_call_handler_holds_the_session_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hold_call resolves the session's call (chat_id == Call-ID) and holds it."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await hold_call_handler({})

    assert host.held == ["call-xyz"]
    assert "result" in json.loads(result)


@pytest.mark.asyncio
async def test_resume_call_handler_resumes_the_session_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resume_call resolves the session's call and resumes it."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await resume_call_handler({})

    assert host.resumed == ["call-xyz"]
    assert "result" in json.loads(result)


@pytest.mark.asyncio
async def test_list_registrations_handler_returns_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_registrations reads the adapter's registration snapshot (no call needed).

    It is a process-wide read (the manager's registrations), not a per-call action,
    so it does not depend on a current chat_id.
    """
    host = _FakeHost(registrations="1000: registered; 1001: down")
    set_active_adapter(host)
    _set_chat(monkeypatch, None)

    result = await list_registrations_handler({})

    assert host.listed == 1
    payload = json.loads(result)
    assert payload.get("result") == "1000: registered; 1001: down"


@pytest.mark.asyncio
async def test_hold_call_handler_with_no_adapter_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live adapter → a clear error result, never a crash."""
    _set_chat(monkeypatch, "call-xyz")  # adapter is None (fixture default)
    result = await hold_call_handler({})
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_hold_call_handler_with_no_session_call_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No call in scope (no chat_id) → an error result, no call held."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, None)

    result = await hold_call_handler({})

    assert host.held == []
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_resume_call_handler_already_ended_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call that already ended → an error result (does not claim success)."""
    host = _FakeHost(already_ended=True)
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await resume_call_handler({})

    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_list_registrations_handler_with_no_adapter_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live adapter → a clear error result for the read-only tool too."""
    _set_chat(monkeypatch, None)
    result = await list_registrations_handler({})
    assert "error" in json.loads(result)


# --- the pre_tool_call gate now OWNS the control tools (privilege clamp) ----


def test_gate_owns_the_control_tools() -> None:
    """The gate is responsible for hold/resume/list (returns a verdict, not None).

    A tool the gate does not own returns None (defer); these must NOT defer — the
    privilege clamp judges them.
    """
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    owned = vt._voip_tool_names()
    assert HOLD_TOOL_NAME in owned
    assert RESUME_TOOL_NAME in owned
    assert LIST_REGISTRATIONS_TOOL_NAME in owned


@pytest.mark.parametrize(
    "tool_name",
    [HOLD_TOOL_NAME, RESUME_TOOL_NAME, LIST_REGISTRATIONS_TOOL_NAME],
)
def test_gate_blocks_elevated_tools_for_receptionist(
    monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    """A level-0 (untrusted) caller is BLOCKED from every ELEVATED control tool.

    This is the security spine: an unprivileged/receptionist caller cannot hold,
    resume, or enumerate the operator's registrations, even if a prompt injection
    coaxes the model into calling the tool.
    """
    host = _FakeHost(guard=_receptionist())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(tool_name=tool_name, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


@pytest.mark.parametrize(
    "tool_name",
    [HOLD_TOOL_NAME, RESUME_TOOL_NAME, LIST_REGISTRATIONS_TOOL_NAME],
)
def test_gate_allows_elevated_tools_for_operator(
    monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    """An operator (level-3, clean) call is ALLOWED every ELEVATED control tool."""
    host = _FakeHost(guard=_operator())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    assert voip_pre_tool_call(tool_name=tool_name, args={}) is None


@pytest.mark.parametrize(
    "tool_name",
    [HOLD_TOOL_NAME, RESUME_TOOL_NAME, LIST_REGISTRATIONS_TOOL_NAME],
)
def test_gate_blocks_elevated_tools_on_degraded_session(
    monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    """A degraded (fail-open screened) operator call is BLOCKED from ELEVATED tools.

    The ``degraded`` hard-block applies even at operator level: a missed injection
    that flips the session degraded cannot then reach hold/resume/list.
    """
    host = _FakeHost(
        guard=GuardSessionState(call_id="c", privilege_level=3, degraded=True)
    )
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(tool_name=tool_name, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


def test_gate_fails_safe_for_elevated_tool_when_call_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ELEVATED tool with no resolvable call state is BLOCKED (fail safe).

    When the adapter/call is out of scope the gate falls back to a level-0 state,
    so an unknown context cannot accidentally grant an ELEVATED action.
    """
    set_active_adapter(None)
    _set_chat(monkeypatch, None)

    verdict = voip_pre_tool_call(tool_name=HOLD_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


# ---------------------------------------------------------------------------
# send_dtmf (ELEVATED) + open_entry (intercom entry actuation) — ADR-0031
# ---------------------------------------------------------------------------


def _trusted() -> GuardSessionState:
    """A level-2 trusted call state — allows ELEVATED tools (e.g. send_dtmf)."""
    return GuardSessionState(call_id="c", privilege_level=2)


def test_send_dtmf_schema_takes_a_digits_string() -> None:
    assert SEND_DTMF_TOOL_SCHEMA["name"] == SEND_DTMF_TOOL_NAME
    assert SEND_DTMF_TOOL_SCHEMA["description"]
    params = SEND_DTMF_TOOL_SCHEMA["parameters"]
    assert isinstance(params, dict)
    props = params.get("properties")
    assert isinstance(props, dict)
    assert "digits" in props
    assert params.get("required") == ["digits"]


@pytest.mark.asyncio
async def test_send_dtmf_handler_sends_on_the_session_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_dtmf resolves the session's call and sends the requested digits."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await send_dtmf_handler({"digits": "123"})

    assert host.dtmf_sent == [("call-xyz", "123")]
    assert "result" in json.loads(result)


@pytest.mark.asyncio
async def test_send_dtmf_handler_requires_digits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing/blank digits argument yields an error, sends nothing."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await send_dtmf_handler({})

    assert host.dtmf_sent == []
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_send_dtmf_handler_reports_unnegotiated_telephone_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the engine raises (no telephone-event PT), the tool reports a clear error.

    The engine raises rather than silently dropping DTMF; the handler converts that
    into a tool error result so the model learns it could not send — never a crash.
    """
    host = _FakeHost()
    host.dtmf_raises = RuntimeError("no telephone-event payload type negotiated")
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await send_dtmf_handler({"digits": "1"})

    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_send_dtmf_handler_no_call_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No call in scope → an error, nothing sent."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, None)

    result = await send_dtmf_handler({"digits": "1"})

    assert host.dtmf_sent == []
    assert "error" in json.loads(result)


def test_send_dtmf_is_elevated_blocked_for_receptionist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_dtmf is ELEVATED: a level-0 receptionist call is BLOCKED by the gate."""
    host = _FakeHost(guard=_receptionist())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(tool_name=SEND_DTMF_TOOL_NAME, args={"digits": "1"})

    assert verdict is not None
    assert verdict["action"] == "block"


def test_send_dtmf_allowed_for_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_dtmf (ELEVATED) is allowed for a level-2 trusted call."""
    host = _FakeHost(guard=_trusted())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    assert (
        voip_pre_tool_call(tool_name=SEND_DTMF_TOOL_NAME, args={"digits": "1"}) is None
    )


# --- open_entry: the intercom entry action ------------------------------------


def test_open_entry_schema_takes_no_params() -> None:
    assert OPEN_ENTRY_TOOL_SCHEMA["name"] == OPEN_ENTRY_TOOL_NAME
    assert OPEN_ENTRY_TOOL_SCHEMA["description"]
    params = OPEN_ENTRY_TOOL_SCHEMA["parameters"]
    assert isinstance(params, dict)
    # No parameters: opening the door is a fixed action, never a model-chosen target.
    assert params.get("properties") == {}


@pytest.mark.asyncio
async def test_open_entry_handler_actuates_on_the_session_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open_entry resolves the session's call and actuates the entry."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-door")

    result = await open_entry_handler({})

    assert host.entries_opened == ["call-door"]
    assert "result" in json.loads(result)


def test_open_entry_is_gated_blocked_for_receptionist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open_entry is gated (ELEVATED): a level-0 receptionist call is BLOCKED.

    The intercom group runs at level 2 with an allowed_tools sub-ceiling, so it
    passes; a level-0 caller (the fail-safe default for an unknown context) does not.
    """
    host = _FakeHost(guard=_receptionist())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(tool_name=OPEN_ENTRY_TOOL_NAME, args={})

    assert verdict is not None
    assert verdict["action"] == "block"


def test_open_entry_scoped_by_allowed_tools_blocks_other_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An intercom session (allowed_tools={open_entry}) cannot reach hold_call.

    This is the intercom least-privilege guarantee at the gate: even though the
    group is level 2 (which would otherwise permit hold_call), the allowed_tools
    sub-ceiling removes every tool but open_entry — so a spoofed caller-ID reaching
    the intercom group gets ONLY the entry action.
    """
    intercom_state = GuardSessionState(
        call_id="c",
        privilege_level=2,
        allowed_tools=frozenset({OPEN_ENTRY_TOOL_NAME}),
    )
    host = _FakeHost(guard=intercom_state)
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    # open_entry is permitted...
    assert voip_pre_tool_call(tool_name=OPEN_ENTRY_TOOL_NAME, args={}) is None
    # ...but every other tool (hold_call, list_registrations, send_dtmf) is blocked.
    for blocked in (HOLD_TOOL_NAME, LIST_REGISTRATIONS_TOOL_NAME, SEND_DTMF_TOOL_NAME):
        verdict = voip_pre_tool_call(tool_name=blocked, args={})
        assert verdict is not None, f"{blocked} should be blocked by the sub-ceiling"
        assert verdict["action"] == "block"


# ---------------------------------------------------------------------------
# transfer_blind (IRREVERSIBLE) — DTMF-confirmed blind transfer (ADR-0010/0031)
# ---------------------------------------------------------------------------
#
# The spoof-resistant safeguard is the ArmedConfirmation DTMF channel, driven by
# the host method ``transfer_blind_on_call`` (NOT the sync gate). The gate's role
# is the privilege clamp ONLY (operator level 3 + non-degraded) — a fail-fast so an
# unprivileged/degraded caller is blocked BEFORE the confirm prompt is ever spoken.
# The handler then asks the host to confirm-and-REFER, and the REFER fires ONLY when
# the host reports TRANSFERRED (i.e. the caller pressed the armed digit).


def test_transfer_blind_schema_takes_a_target_string() -> None:
    """The schema names the tool and takes a single required ``target`` string."""
    assert TRANSFER_BLIND_TOOL_SCHEMA["name"] == TRANSFER_BLIND_TOOL_NAME
    assert TRANSFER_BLIND_TOOL_SCHEMA["description"]
    params = TRANSFER_BLIND_TOOL_SCHEMA["parameters"]
    assert isinstance(params, dict)
    props = params.get("properties")
    assert isinstance(props, dict)
    assert "target" in props
    assert params.get("required") == ["target"]


@pytest.mark.asyncio
async def test_transfer_blind_handler_fires_on_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed transfer resolves the session call and fires the REFER to target.

    The host method returns TRANSFERRED only after the ArmedConfirmation resolves
    True; the handler reports success and the REFER reached exactly the session's
    own call with the requested target.
    """
    host = _FakeHost()
    host.transfer_outcome = TransferOutcome.TRANSFERRED
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await transfer_blind_handler({"target": "sip:1001@pbx.example.test"})

    assert host.transfers == [("call-xyz", "sip:1001@pbx.example.test")]
    assert "result" in json.loads(result)


@pytest.mark.asyncio
async def test_transfer_blind_handler_unconfirmed_does_not_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong-digit / timeout (UNCONFIRMED) → no transfer, a clear error result.

    This is the load-bearing safety property: even an operator-level call does NOT
    transfer unless the caller presses the armed confirm digit. The handler reports
    that nothing was transferred and the host recorded no REFER.
    """
    host = _FakeHost()
    host.transfer_outcome = TransferOutcome.UNCONFIRMED
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await transfer_blind_handler({"target": "sip:1001@pbx.example.test"})

    assert host.transfers == []
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_transfer_blind_handler_blocked_does_not_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BLOCKED outcome (chokepoint privilege re-check) → no transfer, clear error.

    The host method's own privilege clamp (defense in depth) can refuse the transfer
    even after the gate passed — e.g. the session went degraded during the confirm
    window. The handler reports that and the host recorded no REFER.
    """
    host = _FakeHost()
    host.transfer_outcome = TransferOutcome.BLOCKED
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await transfer_blind_handler({"target": "sip:1001@pbx.example.test"})

    assert host.transfers == []
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_transfer_blind_handler_requires_a_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing/blank target yields an error and nothing is transferred."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await transfer_blind_handler({})

    assert host.transfers == []
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_transfer_blind_handler_no_call_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No call in scope → an error, nothing transferred (the REFER is never built)."""
    host = _FakeHost()
    set_active_adapter(host)
    _set_chat(monkeypatch, None)

    result = await transfer_blind_handler({"target": "sip:1001@pbx.example.test"})

    assert host.transfers == []
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_transfer_blind_handler_no_adapter_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live adapter → a clear error, never a crash."""
    _set_chat(monkeypatch, "call-xyz")  # adapter is None (fixture default)
    result = await transfer_blind_handler({"target": "sip:1001@pbx.example.test"})
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_transfer_blind_handler_unavailable_reports_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DTMF confirmation is not available the handler reports a clear error.

    The host raises RuntimeError (no ArmedConfirmation bound — the call negotiated no
    telephone-event, so it cannot obtain a spoof-resistant confirmation). The handler
    surfaces it as a tool error; the transfer is NOT performed (rule 37 — never a
    silent no-op).
    """
    host = _FakeHost()
    host.transfer_raises = RuntimeError("inbound DTMF confirmation is not available")
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await transfer_blind_handler({"target": "sip:1001@pbx.example.test"})

    assert host.transfers == []
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_transfer_blind_handler_refer_rejected_reports_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REFER rejected by the gateway (CallError) is surfaced as a tool error."""
    from hermes_voip.call import CallError  # noqa: PLC0415

    host = _FakeHost()
    host.transfer_raises = CallError("REFER rejected: 603 Declined")
    set_active_adapter(host)
    _set_chat(monkeypatch, "call-xyz")

    result = await transfer_blind_handler({"target": "sip:1001@pbx.example.test"})

    assert host.transfers == []
    assert "error" in json.loads(result)


# --- the gate OWNS transfer_blind and clamps it to operator level 3 ----------


def test_gate_owns_transfer_blind() -> None:
    """The gate is responsible for transfer_blind (returns a verdict, not None)."""
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    assert TRANSFER_BLIND_TOOL_NAME in vt._voip_tool_names()


def test_gate_blocks_transfer_blind_for_receptionist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A level-0 (untrusted) caller is BLOCKED from transfer_blind (no prompt spoken).

    The gate fail-fasts BEFORE the handler runs, so an unprivileged caller never even
    hears the confirm prompt — the credit-card-attack class cannot reach the REFER.
    """
    host = _FakeHost(guard=_receptionist())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(
        tool_name=TRANSFER_BLIND_TOOL_NAME, args={"target": "sip:1001@pbx.example.test"}
    )

    assert verdict is not None
    assert verdict["action"] == "block"


def test_gate_blocks_transfer_blind_for_trusted_level_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A level-2 trusted (colleague) call is BLOCKED from transfer_blind.

    transfer_blind is IRREVERSIBLE (requires level 3); a trusted-but-not-operator
    caller can hold/resume/send DTMF but cannot initiate a transfer — matching the
    colleague persona, which states it may NOT initiate transfers.
    """
    host = _FakeHost(guard=_trusted())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(
        tool_name=TRANSFER_BLIND_TOOL_NAME, args={"target": "sip:1001@pbx.example.test"}
    )

    assert verdict is not None
    assert verdict["action"] == "block"


def test_gate_blocks_transfer_blind_on_degraded_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded (fail-open screened) operator call is BLOCKED from transfer_blind.

    The degraded hard-block applies even at operator level: a missed injection that
    flips the session degraded cannot then reach a transfer.
    """
    host = _FakeHost(
        guard=GuardSessionState(call_id="c", privilege_level=3, degraded=True)
    )
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    verdict = voip_pre_tool_call(
        tool_name=TRANSFER_BLIND_TOOL_NAME, args={"target": "sip:1001@pbx.example.test"}
    )

    assert verdict is not None
    assert verdict["action"] == "block"


def test_gate_allows_transfer_blind_for_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator (level-3, clean) call PASSES the gate (reaches the handler).

    The gate clamps privilege only; the DTMF confirmation is then enforced by the
    handler/host before the REFER fires (covered by the handler tests above).
    """
    host = _FakeHost(guard=_operator())
    set_active_adapter(host)
    _set_chat(monkeypatch, "c")

    assert (
        voip_pre_tool_call(
            tool_name=TRANSFER_BLIND_TOOL_NAME,
            args={"target": "sip:1001@pbx.example.test"},
        )
        is None
    )


def test_gate_fails_safe_for_transfer_blind_when_call_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """transfer_blind with no resolvable call state is BLOCKED (fail safe)."""
    set_active_adapter(None)
    _set_chat(monkeypatch, None)

    verdict = voip_pre_tool_call(
        tool_name=TRANSFER_BLIND_TOOL_NAME, args={"target": "sip:1001@pbx.example.test"}
    )

    assert verdict is not None
    assert verdict["action"] == "block"


def test_both_transfer_tools_are_exposed_and_gated() -> None:
    """Both transfer tools are exposed and owned by the gate (ADR-0010/0048).

    The agent-driven consult-leg origination landed (ADR-0019/0029), so the attended
    transfer is no longer a deferred stub (rule 6 satisfied): it has a name, schema, and
    handler in the public API, and the gate owns it (so its IRREVERSIBLE clamp applies).
    """
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    # Both transfers are exposed and owned by the gate...
    assert TRANSFER_BLIND_TOOL_NAME in vt._voip_tool_names()
    assert "transfer_attended" in vt._voip_tool_names()
    # ...with a name, schema, and handler in the public API.
    assert hasattr(vt, "TRANSFER_ATTENDED_TOOL_NAME")
    assert hasattr(vt, "TRANSFER_ATTENDED_TOOL_SCHEMA")
    assert hasattr(vt, "transfer_attended_handler")
