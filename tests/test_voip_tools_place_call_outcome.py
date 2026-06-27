"""Tests for place_call outbound SIP failure classification + ring timeout (ADR-0086).

``place_call`` previously collapsed all outbound failures into a single generic
error, preventing the agent from branching on WHY a call failed.  This module
tests:

* A new ``PlaceCallOutcome`` enum (BUSY / NO_ANSWER / DECLINED / FAILED) that
  each distinct SIP failure maps to.
* The handler maps ``OutboundCallFailed`` status codes to the correct outcome:
  - 486 -> BUSY
  - 603 -> DECLINED
  - 487 / 408 -> NO_ANSWER  (peer-signalled no-answer codes)
  - anything else -> FAILED
* ``OutboundCallCancelled`` (a ring-timeout abort) -> NO_ANSWER
* The JSON tool result carries ``{"failure_outcome": <outcome.value>, ...}``
  so the model can branch without needing enum internals.
* A ring timeout env var (``HERMES_VOIP_RING_TIMEOUT_SECS``) wires a bounded
  ring timeout through the adapter: when set, ``place_call_with_objective``
  is called with the parsed ``ring_timeout_secs`` kwarg, which arms the
  ADR-0069 outbound CANCEL timer inside the adapter.
* No SIP host / extension / PII leaks into any error message.

All tests use fakes only (``pbx.example.test``, ext ``1000``); no real gateway.
"""

from __future__ import annotations

import json

import pytest

from hermes_voip.originate import (
    OutboundCallCancelled,
    OutboundCallFailed,
    OutboundCallNotAllowed,
)
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.voip_tools import (
    AttendedTransferOutcome,
    PlaceCallOutcome,
    TransferOutcome,
    place_call_handler,
    set_active_adapter,
)

# ---------------------------------------------------------------------------
# Fake host
# ---------------------------------------------------------------------------


class _FakeHost:
    """Fake ``VoipToolHost`` — injects exc from ``place_call_with_objective``."""

    def __init__(
        self,
        *,
        guard: GuardSessionState | None = None,
        exc: Exception | None = None,
        call_id: str = "call-out-1",
        ring_timeout_received: list[float | None] | None = None,
    ) -> None:
        self._guard = guard
        self._exc = exc
        self._call_id = call_id
        # Mutable list — populated by the fake on each call so tests can assert
        # what ring_timeout_secs was passed through.
        self._ring_timeout_received: list[float | None] = (
            ring_timeout_received if ring_timeout_received is not None else []
        )
        self.placed: list[tuple[str, str]] = []

    def guard_state_for(self, call_id: str) -> GuardSessionState | None:
        return self._guard

    async def place_call_with_objective(
        self,
        number: str,
        objective: str,
        *,
        ring_timeout_secs: float | None = None,
    ) -> str:
        self._ring_timeout_received.append(ring_timeout_secs)
        if self._exc is not None:
            raise self._exc
        self.placed.append((number, objective))
        return self._call_id

    # Remaining VoipToolHost members unused here — satisfy the Protocol.
    async def hang_up_call(self, call_id: str) -> bool:
        return True

    async def hold_call(self, call_id: str) -> bool:
        return True

    async def resume_call(self, call_id: str) -> bool:
        return True

    def list_registrations_text(self) -> str:
        return ""

    def record_call_result(self, call_id: str, summary: str) -> bool:
        return True

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_active_adapter() -> object:
    set_active_adapter(None)
    yield
    set_active_adapter(None)


def _set_chat(monkeypatch: pytest.MonkeyPatch, call_id: str | None) -> None:
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    monkeypatch.setattr(vt, "_current_call_id", lambda: call_id)


# ---------------------------------------------------------------------------
# PlaceCallOutcome enum: values are stable API surface for the agent
# ---------------------------------------------------------------------------


def test_place_call_outcome_members_exist() -> None:
    """PlaceCallOutcome has BUSY, NO_ANSWER, DECLINED, FAILED members."""
    assert PlaceCallOutcome.BUSY
    assert PlaceCallOutcome.NO_ANSWER
    assert PlaceCallOutcome.DECLINED
    assert PlaceCallOutcome.FAILED


def test_place_call_outcome_values_are_strings() -> None:
    """PlaceCallOutcome values are lowercase string tokens (the JSON surface)."""
    assert isinstance(PlaceCallOutcome.BUSY.value, str)
    assert isinstance(PlaceCallOutcome.NO_ANSWER.value, str)
    assert isinstance(PlaceCallOutcome.DECLINED.value, str)
    assert isinstance(PlaceCallOutcome.FAILED.value, str)


def test_place_call_outcome_values_distinct() -> None:
    """Each PlaceCallOutcome value is distinct (no accidental aliasing)."""
    vals = {o.value for o in PlaceCallOutcome}
    assert len(vals) == len(PlaceCallOutcome)


# ---------------------------------------------------------------------------
# Failure classification: OutboundCallFailed -> PlaceCallOutcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_call_busy_486_maps_to_busy_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """486 Busy Here -> BUSY outcome in the tool result."""
    host = _FakeHost(exc=OutboundCallFailed(486, "Busy Here"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "1000", "objective": "book a table"})

    payload = json.loads(result)
    assert "error" in payload
    assert payload.get("failure_outcome") == PlaceCallOutcome.BUSY.value


@pytest.mark.asyncio
async def test_place_call_declined_603_maps_to_declined_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """603 Decline -> DECLINED outcome in the tool result."""
    host = _FakeHost(exc=OutboundCallFailed(603, "Decline"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "1000", "objective": "book a table"})

    payload = json.loads(result)
    assert "error" in payload
    assert payload.get("failure_outcome") == PlaceCallOutcome.DECLINED.value


@pytest.mark.asyncio
async def test_place_call_no_answer_487_maps_to_no_answer_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """487 Request Terminated (peer-signalled no-answer) -> NO_ANSWER outcome."""
    host = _FakeHost(exc=OutboundCallFailed(487, "Request Terminated"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "1000", "objective": "book a table"})

    payload = json.loads(result)
    assert "error" in payload
    assert payload.get("failure_outcome") == PlaceCallOutcome.NO_ANSWER.value


@pytest.mark.asyncio
async def test_place_call_no_answer_408_maps_to_no_answer_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """408 Request Timeout (no-answer) -> NO_ANSWER outcome."""
    host = _FakeHost(exc=OutboundCallFailed(408, "Request Timeout"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "1000", "objective": "book a table"})

    payload = json.loads(result)
    assert "error" in payload
    assert payload.get("failure_outcome") == PlaceCallOutcome.NO_ANSWER.value


@pytest.mark.asyncio
async def test_place_call_other_failure_503_maps_to_failed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 Service Unavailable (other) -> FAILED outcome."""
    host = _FakeHost(exc=OutboundCallFailed(503, "Service Unavailable"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "1000", "objective": "book a table"})

    payload = json.loads(result)
    assert "error" in payload
    assert payload.get("failure_outcome") == PlaceCallOutcome.FAILED.value


@pytest.mark.asyncio
async def test_place_call_cancelled_maps_to_no_answer_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OutboundCallCancelled (ring timeout abort) -> NO_ANSWER outcome."""
    host = _FakeHost(exc=OutboundCallCancelled("call-out-1", "ring timeout"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "1000", "objective": "book a table"})

    payload = json.loads(result)
    assert "error" in payload
    assert payload.get("failure_outcome") == PlaceCallOutcome.NO_ANSWER.value


# ---------------------------------------------------------------------------
# No PII leakage in failure messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_call_failure_message_does_not_leak_sip_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error message does not embed the SIP reason phrase (potential PII gateway).

    The error must tell the agent WHAT happened (the category) without leaking
    the exact gateway reason string that may embed internal host/extension info.
    """
    host = _FakeHost(exc=OutboundCallFailed(486, "Busy Here from pbx.internal.corp"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "1000", "objective": "book a table"})

    payload = json.loads(result)
    # The gateway-side reason phrase MUST NOT be echoed in the agent-facing result.
    assert "pbx.internal.corp" not in json.dumps(payload)
    assert "Busy Here from" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Successful call still returns call_id (no regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_call_success_still_returns_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful call still returns {call_id: ...} with no failure_outcome."""
    host = _FakeHost(call_id="call-success-99")
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "1000", "objective": "book a table"})

    payload = json.loads(result)
    assert payload == {"call_id": "call-success-99"}
    assert "failure_outcome" not in payload


# ---------------------------------------------------------------------------
# Ring timeout: HERMES_VOIP_RING_TIMEOUT_SECS wires through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ring_timeout_env_is_passed_to_place_call_with_objective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HERMES_VOIP_RING_TIMEOUT_SECS is read and forwarded as ring_timeout_secs."""
    ring_log: list[float | None] = []
    host = _FakeHost(call_id="call-rt-1", ring_timeout_received=ring_log)
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")
    monkeypatch.setenv("HERMES_VOIP_RING_TIMEOUT_SECS", "20")

    result = await place_call_handler(
        {"number": "1000", "objective": "check availability"}
    )

    payload = json.loads(result)
    assert payload == {"call_id": "call-rt-1"}
    # The fake recorded the ring_timeout_secs kwarg received.
    assert ring_log == [20.0]


@pytest.mark.asyncio
async def test_ring_timeout_env_not_set_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When HERMES_VOIP_RING_TIMEOUT_SECS is absent, ring_timeout_secs=None passed."""
    ring_log: list[float | None] = []
    host = _FakeHost(call_id="call-rt-2", ring_timeout_received=ring_log)
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")
    monkeypatch.delenv("HERMES_VOIP_RING_TIMEOUT_SECS", raising=False)

    result = await place_call_handler(
        {"number": "1000", "objective": "check availability"}
    )

    assert json.loads(result) == {"call_id": "call-rt-2"}
    assert ring_log == [None]


@pytest.mark.asyncio
async def test_ring_timeout_env_invalid_value_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric HERMES_VOIP_RING_TIMEOUT_SECS is ignored (treated as absent)."""
    ring_log: list[float | None] = []
    host = _FakeHost(call_id="call-rt-3", ring_timeout_received=ring_log)
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")
    monkeypatch.setenv("HERMES_VOIP_RING_TIMEOUT_SECS", "not-a-number")

    result = await place_call_handler(
        {"number": "1000", "objective": "check availability"}
    )

    assert json.loads(result) == {"call_id": "call-rt-3"}
    # An unparseable value must not crash; ring_timeout_secs falls back to None.
    assert ring_log == [None]


@pytest.mark.asyncio
async def test_ring_timeout_expiry_fires_cancel_returns_no_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ring timeout expiry: OutboundCallCancelled raised by adapter -> NO_ANSWER.

    The adapter is responsible for arming the timer and raising
    OutboundCallCancelled when it fires.  The handler maps that to NO_ANSWER
    so the agent knows the call went unanswered (we chose to stop ringing),
    not that the peer rejected it.
    """
    host = _FakeHost(exc=OutboundCallCancelled("call-rt-4", "ring timeout"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")
    monkeypatch.setenv("HERMES_VOIP_RING_TIMEOUT_SECS", "15")

    result = await place_call_handler(
        {"number": "1000", "objective": "check availability"}
    )

    payload = json.loads(result)
    assert "error" in payload
    assert payload.get("failure_outcome") == PlaceCallOutcome.NO_ANSWER.value


# ---------------------------------------------------------------------------
# Unlisted number still produces a plain error (no failure_outcome)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_call_unlisted_number_no_failure_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OutboundCallNotAllowed (allowlist gate) produces a plain error with no outcome.

    This is a policy refusal before any dial, not a SIP failure — the agent
    should see a clear error but no failure_outcome (there was no call to fail).
    """
    host = _FakeHost(exc=OutboundCallNotAllowed("9999"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "9999", "objective": "do a thing"})

    payload = json.loads(result)
    assert "error" in payload
    assert "failure_outcome" not in payload


# ---------------------------------------------------------------------------
# Transport / media-init RuntimeError -> FAILED outcome (ADR-0086 Decision A)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_call_runtime_error_maps_to_failed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport/media-init RuntimeError -> FAILED outcome (structured contract).

    ADR-0086 classifies a non-SIP transport/media-init failure as the FAILED
    outcome.  Such a failure surfaces as a ``RuntimeError`` from
    ``place_call_with_objective`` (e.g. the RTP transport could not be opened).
    It MUST carry the structured ``failure_outcome`` key like the SIP failures
    do — not fall through to the generic boundary handler with no outcome.
    """
    host = _FakeHost(exc=RuntimeError("RTP transport init failed"))
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "1000", "objective": "book a table"})

    payload = json.loads(result)
    assert "error" in payload
    assert payload.get("failure_outcome") == PlaceCallOutcome.FAILED.value


@pytest.mark.asyncio
async def test_place_call_runtime_error_does_not_leak_gateway_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RuntimeError whose message embeds a (fake) gateway host MUST NOT leak it.

    SECURITY / public-repo: ADR-0086 requires that NO gateway detail (host,
    extension, port) leaks in the agent-facing result.  A transport/media-init
    ``RuntimeError`` can carry a connection string in its message; the handler
    must redact ``str(exc)`` exactly as the SIP path suppresses the reason
    phrase — returning only the classified category, never the raw message.
    """
    leaky = RuntimeError(
        "media transport to pbx.example.test:5061 failed: connection refused"
    )
    host = _FakeHost(exc=leaky)
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")

    result = await place_call_handler({"number": "1000", "objective": "book a table"})

    payload = json.loads(result)
    blob = json.dumps(payload)
    # The structured outcome is still present...
    assert payload.get("failure_outcome") == PlaceCallOutcome.FAILED.value
    # ...but the raw exception message (with the fake host:port) is NOT echoed.
    assert "pbx.example.test" not in blob
    assert "5061" not in blob
    assert "connection refused" not in blob


# ---------------------------------------------------------------------------
# Ring timeout: positive infinity must be rejected (bounded-timeout policy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ring_timeout_inf_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HERMES_VOIP_RING_TIMEOUT_SECS='inf' is rejected (defeats the bounded policy).

    ``float('inf') > 0`` is True, so a bare ``value > 0`` check would accept
    positive infinity — an unbounded timeout, which is exactly what the bounded
    ring-timeout policy forbids.  ``inf`` must be treated as absent (None), so
    the adapter's hard sink bound governs instead.
    """
    ring_log: list[float | None] = []
    host = _FakeHost(call_id="call-rt-inf", ring_timeout_received=ring_log)
    set_active_adapter(host)
    _set_chat(monkeypatch, "origin-chat")
    monkeypatch.setenv("HERMES_VOIP_RING_TIMEOUT_SECS", "inf")

    result = await place_call_handler(
        {"number": "1000", "objective": "check availability"}
    )

    assert json.loads(result) == {"call_id": "call-rt-inf"}
    # Positive infinity is NOT a valid bounded timeout — falls back to None.
    assert ring_log == [None]


def test_parse_ring_timeout_rejects_infinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_parse_ring_timeout returns None for 'inf' / 'infinity' (non-finite values)."""
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    for raw in ("inf", "Infinity", "+inf", "-inf", "nan"):
        monkeypatch.setenv("HERMES_VOIP_RING_TIMEOUT_SECS", raw)
        assert vt._parse_ring_timeout() is None, raw


# ---------------------------------------------------------------------------
# Stable API surface: ADR-0086 names PlaceCallOutcome + the ring-timeout env
# var as __all__ exports.
# ---------------------------------------------------------------------------


def test_ring_timeout_env_symbol_is_exported() -> None:
    """ADR-0086 Consequences: the ring-timeout env symbol is a stable __all__ export."""
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    assert "_RING_TIMEOUT_ENV" in vt.__all__
    assert vt._RING_TIMEOUT_ENV == "HERMES_VOIP_RING_TIMEOUT_SECS"


def test_place_call_outcome_is_exported() -> None:
    """PlaceCallOutcome is part of the module's public API (__all__)."""
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    assert "PlaceCallOutcome" in vt.__all__
