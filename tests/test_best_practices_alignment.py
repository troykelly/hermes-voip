"""Tests for the ADR-0046 best-practices-alignment lane.

Five concerns, each TDD-first:

1. Every registered tool handler has an OUTERMOST guard: an unanticipated
   exception from a host call is logged and returned as ``{"error": ...}`` JSON
   (the Hermes tool-handler contract — never raise), not propagated.
2. ``register_voip_tools`` is fail-soft: a ``register_tool`` that raises on ONE
   spec still registers the others.
3. Both ``register_platform`` call sites (primary + channel aliases) carry a
   non-empty ``platform_hint``.
4. ``srtp._get_crypto`` / ``dtls._get_openssl`` build at most once and expose a
   reset for test isolation; the guarded ``lazy_singleton`` adoption keeps the
   stdlib fallback correct (the fallback is what runs in this test env).
5. The primary ``register_platform`` omits ``cron_deliver_env_var`` and declares
   an emoji.

These run in the DEFAULT gate (no hermes-agent runtime). The handlers are driven
through a fake ``VoipToolHost`` rigged to raise an unexpected exception.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence

import pytest

from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.voip_tools import (
    AttendedTransferOutcome,
    TransferOutcome,
    hang_up_handler,
    hold_call_handler,
    list_registrations_handler,
    open_entry_handler,
    place_call_handler,
    register_voip_tools,
    report_call_result_handler,
    resume_call_handler,
    send_dtmf_handler,
    set_active_adapter,
    transfer_blind_handler,
)

_BOOM = "unexpected host failure (synthetic)"

#: The DTMF/opening tools whose guard MUST return a FIXED generic message and must
#: NEVER echo the exception detail — an unanticipated ``exc`` can embed the secret
#: DTMF digits / opening secret (voip_tools.py documents "digits are NOT echoed").
_SECRET_DETAIL_HANDLER_NAMES = frozenset({"send_dtmf_handler", "open_entry_handler"})


class _SyntheticHostError(Exception):
    """An UNANTICIPATED host error no handler branch expects (only the guard)."""


class _ExplodingHost:
    """A ``VoipToolHost`` whose every method raises an UNANTICIPATED exception.

    The handlers anticipate ``OutboundCallNotAllowed`` / ``ValueError`` /
    ``RuntimeError``; this host raises :class:`_SyntheticHostError` (a plain
    ``Exception`` subclass) which no handler branch expects, so it exercises ONLY
    the outermost guard.
    """

    def guard_state_for(self, call_id: str) -> GuardSessionState | None:
        # Privileged so the gate (when consulted) never short-circuits before the
        # handler runs; the handler itself is what we exercise.
        return GuardSessionState(call_id=call_id, privilege_level=3)

    async def hang_up_call(self, call_id: str) -> bool:
        raise _SyntheticHostError(_BOOM)

    async def hold_call(self, call_id: str) -> bool:
        raise _SyntheticHostError(_BOOM)

    async def resume_call(self, call_id: str) -> bool:
        raise _SyntheticHostError(_BOOM)

    def list_registrations_text(self) -> str:
        raise _SyntheticHostError(_BOOM)

    async def place_call_with_objective(self, number: str, objective: str) -> str:
        raise _SyntheticHostError(_BOOM)

    def record_call_result(self, call_id: str, summary: str) -> bool:
        raise _SyntheticHostError(_BOOM)

    async def send_dtmf_on_call(self, call_id: str, digits: str) -> bool:
        raise _SyntheticHostError(_BOOM)

    async def open_entry(self, call_id: str) -> bool:
        raise _SyntheticHostError(_BOOM)

    async def transfer_blind_on_call(
        self, call_id: str, target: str
    ) -> TransferOutcome:
        raise _SyntheticHostError(_BOOM)

    # ADR-0048 VoipToolHost members (attended transfer): present so the host
    # satisfies the VoipToolHost protocol for set_active_adapter; they raise the
    # same synthetic error as every other method (the class's exploding contract).
    async def start_attended_consult(self, call_id: str, target: str) -> str:
        raise _SyntheticHostError(_BOOM)

    async def complete_attended_transfer(self, call_id: str) -> AttendedTransferOutcome:
        raise _SyntheticHostError(_BOOM)

    async def cancel_attended_transfer(self, call_id: str) -> bool:
        raise _SyntheticHostError(_BOOM)


@pytest.fixture(autouse=True)
def _reset_active_adapter() -> object:
    set_active_adapter(None)
    yield
    set_active_adapter(None)


def _set_chat(monkeypatch: pytest.MonkeyPatch, call_id: str | None) -> None:
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    monkeypatch.setattr(vt, "_current_call_id", lambda: call_id)


# A registered tool handler: takes the model's args mapping, returns the JSON result.
_Handler = Callable[[Mapping[str, object]], Awaitable[str]]

# Each entry: (handler, args) — args carry every required field so the handler
# reaches the host call (where the synthetic exception is raised).
_HANDLER_CASES: tuple[tuple[_Handler, dict[str, object]], ...] = (
    (hang_up_handler, {}),
    (hold_call_handler, {}),
    (resume_call_handler, {}),
    (list_registrations_handler, {}),
    (place_call_handler, {"number": "1000", "objective": "say hi"}),
    (report_call_result_handler, {"summary": "done"}),
    (send_dtmf_handler, {"digits": "1"}),
    (open_entry_handler, {}),
    (transfer_blind_handler, {"target": "sip:1000@pbx.example.test"}),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler", "args"), _HANDLER_CASES)
async def test_handler_returns_error_json_on_unanticipated_exception(
    handler: _Handler,
    args: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unanticipated host exception is returned as error JSON, never raised.

    The Hermes tool-handler contract is "never raise; return an error result".
    """
    set_active_adapter(_ExplodingHost())
    _set_chat(monkeypatch, "call-xyz")

    result = await handler(args)  # MUST NOT raise

    assert isinstance(result, str)
    payload = json.loads(result)
    assert "error" in payload, f"{handler.__name__} did not return an error result"
    if handler.__name__ in _SECRET_DETAIL_HANDLER_NAMES:
        # send_dtmf / open_entry MUST NOT echo the exception detail — an
        # unanticipated exc could embed the secret digits / opening secret. The
        # guard returns a FIXED generic message instead (asserted in detail by
        # test_secret_handlers_never_echo_exception_detail).
        assert _BOOM not in payload["error"]
    else:
        # The synthetic failure detail is surfaced to the model (and logged).
        assert _BOOM in payload["error"]


@pytest.mark.asyncio
async def test_handler_guard_logs_the_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The outermost guard LOGS the exception (rule 37: surfaced, not swallowed)."""
    set_active_adapter(_ExplodingHost())
    _set_chat(monkeypatch, "call-xyz")

    with caplog.at_level("ERROR", logger="hermes_voip.voip_tools"):
        await hang_up_handler({})

    # The guard logs with full traceback context: the captured record's exc_info
    # must carry the synthetic error TYPE (an unconditional ``or rec.exc_info`` would
    # pass even if nothing about this failure was logged — exc_info is always set on
    # _log.exception). Asserting the exception type proves THIS failure was logged.
    assert any(
        rec.exc_info is not None and rec.exc_info[0] is _SyntheticHostError
        for rec in caplog.records
    ), "the guard must log the unanticipated exception with its traceback context"


# ---------------------------------------------------------------------------
# (1b) The DTMF/opening tools' guard NEVER echoes the exception detail (it could
#      embed the secret digits / opening secret). It returns a FIXED message and
#      logs WITHOUT the exception text.
# ---------------------------------------------------------------------------

#: A recognisable secret-digit string an unanticipated exception might embed.
_SECRET_DIGITS = "9182736450"


class _DigitLeakingHost(_ExplodingHost):
    """A host whose unanticipated error message embeds the secret DTMF digits.

    Models the worst case: a bug deep in the send path raises an exception whose
    ``str(exc)`` contains the very digits the caller asked to send (or the opening
    secret). The guard for the DTMF/opening tools must NOT propagate that text into
    the model-facing result OR the logs. Subclasses :class:`_ExplodingHost` so it
    still satisfies the full ``VoipToolHost`` surface; only the two secret-bearing
    methods are overridden to raise with the digits embedded.
    """

    async def send_dtmf_on_call(self, call_id: str, digits: str) -> bool:
        raise _SyntheticHostError(f"backend rejected sequence {_SECRET_DIGITS}")

    async def open_entry(self, call_id: str) -> bool:
        raise _SyntheticHostError(f"relay rejected secret {_SECRET_DIGITS}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "args"),
    [
        (send_dtmf_handler, {"digits": _SECRET_DIGITS}),
        (open_entry_handler, {}),
    ],
)
async def test_secret_handlers_never_echo_exception_detail(
    handler: _Handler,
    args: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """send_dtmf / open_entry return a FIXED message; digits never leak anywhere."""
    set_active_adapter(_DigitLeakingHost())
    _set_chat(monkeypatch, "call-xyz")

    with caplog.at_level("ERROR", logger="hermes_voip.voip_tools"):
        result = await handler(args)  # MUST NOT raise

    payload = json.loads(result)
    assert "error" in payload
    # The secret digits must not appear in the model-facing result...
    assert _SECRET_DIGITS not in result
    # ...and the fixed generic message names the tool + "internal error", no exc text.
    tool = handler.__name__.removesuffix("_handler")
    assert payload["error"] == f"{tool} failed (internal error)"
    # ...and the digits must not appear in any captured log line OR exc_info text.
    for rec in caplog.records:
        assert _SECRET_DIGITS not in rec.getMessage()
        if rec.exc_info is not None and rec.exc_info[1] is not None:
            assert _SECRET_DIGITS not in str(rec.exc_info[1])


# ---------------------------------------------------------------------------
# (2) register_voip_tools is fail-soft: one bad register_tool does not abort.
# ---------------------------------------------------------------------------


class _OneToolFailsCtx:
    """A ctx whose ``register_tool`` raises for ONE named tool, records the rest."""

    def __init__(self, failing_tool: str) -> None:
        self._failing_tool = failing_tool
        self.registered: list[str] = []
        self.hooks: list[str] = []

    def register_hook(self, hook_name: str, callback: object) -> None:
        self.hooks.append(hook_name)

    def register_tool(  # noqa: PLR0913 — mirrors hermes-agent register_tool arity
        self,
        name: str,
        toolset: str,
        schema: dict[str, object],
        handler: object,
        *,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
    ) -> None:
        if name == self._failing_tool:
            raise RuntimeError(f"tool {name!r} collides with another plugin")
        self.registered.append(name)


def test_register_voip_tools_is_fail_soft_on_one_colliding_tool() -> None:
    """A register_tool that raises on one spec still registers the others."""
    ctx = _OneToolFailsCtx(failing_tool="hold_call")
    register_voip_tools(ctx)
    # hold_call collided and was skipped...
    assert "hold_call" not in ctx.registered
    # ...but the other tools were still registered (fail-soft, not aborted).
    assert "hang_up" in ctx.registered
    assert "resume_call" in ctx.registered
    assert "send_dtmf" in ctx.registered
    assert "transfer_blind" in ctx.registered


# ---------------------------------------------------------------------------
# (3) platform_hint on BOTH register_platform call sites.
# ---------------------------------------------------------------------------


class _PlatformRecordingCtx:
    """Records every register_platform call (name + kwargs), ignores tools/hooks."""

    def __init__(self) -> None:
        self.platforms: list[dict[str, object]] = []

    def register_platform(  # noqa: PLR0913 — mirrors register_platform arity
        self,
        name: str,
        label: str,
        adapter_factory: Callable[[object], object],
        check_fn: Callable[[], bool],
        validate_config: Callable[[object], bool | None] | None = None,
        required_env: Sequence[str] | None = None,
        install_hint: str = "",
        **entry_kwargs: object,
    ) -> None:
        self.platforms.append({"name": name, **entry_kwargs})

    def register_tool(self, *args: object, **kwargs: object) -> None:
        return None

    def register_hook(self, *args: object, **kwargs: object) -> None:
        return None


def test_every_platform_registration_carries_a_platform_hint() -> None:
    """Both the primary platform and every channel alias declare a platform_hint."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _PlatformRecordingCtx()
    register(ctx)

    assert ctx.platforms, "no platforms registered"
    for entry in ctx.platforms:
        hint = entry.get("platform_hint")
        assert isinstance(hint, str), (
            f"platform {entry['name']!r} has no string platform_hint"
        )
        assert hint.strip(), f"platform {entry['name']!r} platform_hint is empty"


def test_primary_platform_omits_cron_deliver_env_var_and_sets_emoji() -> None:
    """The primary voip platform omits cron_deliver_env_var (no home channel)."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _PlatformRecordingCtx()
    register(ctx)
    primary = next(e for e in ctx.platforms if e["name"] == "voip")
    # No persistent home channel -> cron delivery is intentionally not wired.
    assert "cron_deliver_env_var" not in primary
    # The phone emoji is declared on the primary platform.
    assert primary.get("emoji")


# ---------------------------------------------------------------------------
# (4) Media singletons: build-at-most-once + reset, with the stdlib fallback.
# ---------------------------------------------------------------------------


def test_srtp_get_crypto_is_a_singleton_with_reset() -> None:
    """_get_crypto returns the same instance until reset, then rebuilds."""
    # _get_crypto builds _CryptographyImpl, which needs the optional `media` extra;
    # the default (no-extra) gate would otherwise raise ImportError here.
    pytest.importorskip("cryptography")
    from hermes_voip.media import srtp  # noqa: PLC0415

    srtp._reset_crypto_singleton()
    first = srtp._get_crypto()
    second = srtp._get_crypto()
    assert first is second
    srtp._reset_crypto_singleton()
    third = srtp._get_crypto()
    assert third is not first


def test_dtls_get_openssl_is_a_singleton_with_reset() -> None:
    """_get_openssl returns the same instance until reset, then rebuilds."""
    pytest.importorskip("OpenSSL.SSL")
    from hermes_voip.media import dtls  # noqa: PLC0415

    dtls._reset_openssl_singleton()
    first = dtls._get_openssl()
    second = dtls._get_openssl()
    assert first is second
    dtls._reset_openssl_singleton()
    third = dtls._get_openssl()
    assert third is not first
