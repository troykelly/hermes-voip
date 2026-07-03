"""RED tests for the VoipAdapter reconnect supervisor (RFC 5626 SIP flow resilience).

These tests require the optional ``hermes`` extra and skip cleanly without it;
the ``hermes-contract`` CI job installs the extra so they actually run there.

All fakes only; no real network, no real TLS.  Credentials are obvious fakes
(``pbx.example.test`` / ext ``1000`` / ``127.0.0.1``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Requires the optional hermes extra; skip if absent.
pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import (
    PlatformEntry,
    platform_registry,
)

from hermes_voip.adapter import VoipAdapter

if TYPE_CHECKING:
    pass

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fake env / helpers
# ---------------------------------------------------------------------------

_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
}


def _platform_config(extra: dict[str, str] | None = None) -> PlatformConfig:
    """A PlatformConfig carrying fake SIP credentials."""
    return PlatformConfig(enabled=True, extra=dict(extra or _FAKE_ENV))


@pytest.fixture(autouse=True)
def _register_voip_platform() -> None:
    """Register a throwaway 'voip' entry so Platform('voip') resolves."""
    if not platform_registry.is_registered("voip"):
        platform_registry.register(
            PlatformEntry(
                name="voip",
                label="VoIP",
                adapter_factory=lambda cfg: MagicMock(),
                check_fn=lambda: True,
                validate_config=lambda cfg: True,
                required_env=[],
                install_hint="",
                source="plugin",
            )
        )


# ---------------------------------------------------------------------------
# Fake transport with a connect-call counter
# ---------------------------------------------------------------------------


class _FakeReconnectTransport:
    """A fake transport that counts connect() calls and supports on_connection_lost."""

    def __init__(
        self,
        *,
        local_sent_by: str = "127.0.0.1:5061",
        fail_connect: bool = False,
    ) -> None:
        self._local_sent_by = local_sent_by
        self.connect_count: int = 0
        self.fail_connect = fail_connect
        self._calls: dict[str, object] = {}
        self._on_connection_lost_cb: object = None

    @property
    def local_sent_by(self) -> str:
        """Return fake local sent-by for tests."""
        return self._local_sent_by

    def contact_uri(self, extension: str) -> str:
        """Build a fake Contact URI."""
        return f"<sip:{extension}@{self._local_sent_by};transport=tls>"

    async def connect(self) -> None:
        """Count calls; optionally raise to simulate failure."""
        self.connect_count += 1
        if self.fail_connect:
            raise OSError("fake connection refused")

    async def aclose(self) -> None:
        """No-op close."""

    def bind_manager(self, manager: object) -> None:
        """No-op manager bind."""

    def add_call(self, call_id: str, sink: object) -> None:
        """Register a call sink."""
        self._calls[call_id] = sink

    def remove_call(self, call_id: str, sink: object | None = None) -> None:
        """Forget a call sink (identity-checked when ``sink`` is given)."""
        if sink is not None and self._calls.get(call_id) is not sink:
            return
        self._calls.pop(call_id, None)


class _FakeManager:
    """Minimal stand-in for RegistrationManager."""

    def __init__(self, *, is_up: bool = True) -> None:
        self._is_up = is_up
        self._calls: dict[tuple[str, str, str], object] = {}
        self.connected = False
        self.closed = False

    @property
    def is_up(self) -> bool:
        """Return registration status."""
        return self._is_up

    async def connect(self, *, timeout: float = 10.0) -> bool:
        """Fake connect; always returns is_up."""
        self.connected = True
        return self._is_up

    async def aclose(self) -> None:
        """Mark closed."""
        self.closed = True

    def add_call(self, dialog_id: tuple[str, str, str], consumer: object) -> None:
        """Register a dialog-level call consumer."""
        self._calls[dialog_id] = consumer

    def remove_call(self, dialog_id: tuple[str, str, str]) -> None:
        """Forget a dialog-level call consumer."""
        self._calls.pop(dialog_id, None)

    @property
    def registration_call_ids(self) -> frozenset[str]:
        """Return empty set (no live registrations in fakes)."""
        return frozenset()


async def _until(
    predicate: object,
    *,
    timeout: float = 3.0,
    step: float = 0.005,
) -> None:
    """Poll a callable predicate until True or timeout."""
    if not callable(predicate):
        msg = "_until requires a callable predicate"
        raise TypeError(msg)
    pred = predicate
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() >= deadline:
            msg = "condition not met within the timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(step)


# ---------------------------------------------------------------------------
# Shared patch context helpers
# ---------------------------------------------------------------------------


def _base_patches(transport: _FakeReconnectTransport) -> list[object]:
    """Return the standard set of patches for adapter tests."""
    manager = _FakeManager()
    return [
        patch(
            "hermes_voip.adapter.SipOverTlsTransport",
            side_effect=lambda **kw: _capture_cb(transport, kw),
        ),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.build_providers", return_value=MagicMock()),
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
    ]


def _capture_cb(
    transport: _FakeReconnectTransport, kw: dict[str, object]
) -> _FakeReconnectTransport:
    """Store on_connection_lost callback on the fake transport and return it."""
    transport._on_connection_lost_cb = kw.get("on_connection_lost")
    return transport


# ---------------------------------------------------------------------------
# Test 1: connection_lost triggers re-establish
# ---------------------------------------------------------------------------


async def test_connection_lost_triggers_reestablish() -> None:
    """_on_connection_lost on a connected adapter triggers transport.connect() again."""
    transport = _FakeReconnectTransport()
    manager = _FakeManager()

    with (
        patch(
            "hermes_voip.adapter.SipOverTlsTransport",
            side_effect=lambda **kw: _capture_cb(transport, kw),
        ),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.build_providers", return_value=MagicMock()),
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
    ):
        adapter = VoipAdapter(_platform_config())
        await adapter.connect()

        initial_count = transport.connect_count
        # Simulate a connection drop via the adapter's own callback.
        adapter._on_connection_lost(None)

        # The adapter must attempt a reconnect: connect_count must increase.
        await _until(lambda: transport.connect_count > initial_count, timeout=3.0)

    await adapter.disconnect()


# ---------------------------------------------------------------------------
# Test 2: failure then success reconnects
# ---------------------------------------------------------------------------


async def test_establish_failure_then_success_reconnects() -> None:
    """First reconnect attempt fails; second succeeds; adapter ends up connected."""
    call_count = 0
    transport_instances: list[_FakeReconnectTransport] = []

    def _factory(**kw: object) -> _FakeReconnectTransport:
        nonlocal call_count
        call_count += 1
        fail = call_count == 2  # second call (first reconnect) fails
        t = _FakeReconnectTransport(fail_connect=fail)
        transport_instances.append(t)
        if call_count == 1:
            t._on_connection_lost_cb = kw.get("on_connection_lost")
        return t

    manager = _FakeManager()

    with (
        patch("hermes_voip.adapter.SipOverTlsTransport", side_effect=_factory),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.build_providers", return_value=MagicMock()),
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
    ):
        adapter = VoipAdapter(_platform_config())
        await adapter.connect()

        # Trigger a reconnect.
        adapter._on_connection_lost(None)

        # Wait for 3 transports (initial + fail + success).
        await _until(lambda: call_count >= 3, timeout=5.0)

    await adapter.disconnect()


# ---------------------------------------------------------------------------
# Test 3: consecutive failures emit ALERT log
# ---------------------------------------------------------------------------


async def test_consecutive_failures_emit_error_alert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After ALERT_THRESHOLD failures, ERROR log with 'ALERT' or 'DOWN' is emitted."""
    call_count = 0

    def _factory(**kw: object) -> _FakeReconnectTransport:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            t = _FakeReconnectTransport()
            t._on_connection_lost_cb = kw.get("on_connection_lost")
            return t
        # All reconnect attempts fail.
        return _FakeReconnectTransport(fail_connect=True)

    manager = _FakeManager()

    with (
        patch("hermes_voip.adapter.SipOverTlsTransport", side_effect=_factory),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.build_providers", return_value=MagicMock()),
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
        # Use tiny backoff so 5 attempts complete well within the 5s timeout.
        patch("hermes_voip.adapter._RECONNECT_BACKOFF_INITIAL", 0.001),
        patch("hermes_voip.adapter._RECONNECT_BACKOFF_CAP", 0.001),
        caplog.at_level(logging.ERROR, logger="hermes_voip.adapter"),
    ):
        adapter = VoipAdapter(_platform_config())
        await adapter.connect()

        # Trigger connection loss.
        adapter._on_connection_lost(None)

        def _alert_logged() -> bool:
            return any(
                r.levelno >= logging.ERROR
                and ("ALERT" in r.message or "DOWN" in r.message)
                for r in caplog.records
            )

        await _until(_alert_logged, timeout=5.0)

    await adapter.disconnect()


# ---------------------------------------------------------------------------
# Test 4: disconnect stops supervisor
# ---------------------------------------------------------------------------


async def test_disconnect_stops_supervisor() -> None:
    """After disconnect(), _on_connection_lost must NOT trigger a reconnect."""
    transport = _FakeReconnectTransport()
    manager = _FakeManager()

    with (
        patch(
            "hermes_voip.adapter.SipOverTlsTransport",
            side_effect=lambda **kw: _capture_cb(transport, kw),
        ),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.build_providers", return_value=MagicMock()),
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
    ):
        adapter = VoipAdapter(_platform_config())
        await adapter.connect()

        await adapter.disconnect()
        count_after_disconnect = transport.connect_count

        # Fire connection-lost; supervisor must NOT reconnect after disconnect.
        adapter._on_connection_lost(None)

        # Give the loop a moment to run any stray task.
        await asyncio.sleep(0.05)

        assert transport.connect_count == count_after_disconnect, (
            "supervisor reconnected after disconnect() — it should have stopped"
        )


# ---------------------------------------------------------------------------
# Test 5: active call sink re-attached on reconnect
# ---------------------------------------------------------------------------


async def test_active_call_sink_reattached_on_reconnect() -> None:
    """A call sink registered before the drop is re-added to the new transport."""
    transports: list[_FakeReconnectTransport] = []
    call_count = 0

    def _factory(**kw: object) -> _FakeReconnectTransport:
        nonlocal call_count
        call_count += 1
        t = _FakeReconnectTransport()
        transports.append(t)
        if call_count == 1:
            t._on_connection_lost_cb = kw.get("on_connection_lost")
        return t

    manager = _FakeManager()

    fake_sink = MagicMock()
    fake_sink.dialog_id = ("call-id-1", "local-tag-1", "remote-tag-1")

    with (
        patch("hermes_voip.adapter.SipOverTlsTransport", side_effect=_factory),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.build_providers", return_value=MagicMock()),
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
    ):
        adapter = VoipAdapter(_platform_config())
        await adapter.connect()

        # Directly register a fake call session in the adapter's tracking dict.
        adapter._call_sessions["call-id-1"] = fake_sink

        # Trigger reconnect.
        adapter._on_connection_lost(None)

        # Wait for a second transport to be created.
        await _until(lambda: len(transports) >= 2, timeout=3.0)

        second_transport = transports[1]
        # The second transport must have the call-id registered in its _calls dict.
        await _until(
            lambda: "call-id-1" in second_transport._calls,
            timeout=3.0,
        )

    await adapter.disconnect()


# ---------------------------------------------------------------------------
# Test 6 (bk1191): active call DIALOG re-attached to the registration manager
# on reconnect — distinct from Test 5's transport-sink re-attach.
# ---------------------------------------------------------------------------


async def test_active_call_dialog_reattached_to_manager_on_reconnect() -> None:
    """A dialog registered before the drop is re-added to the NEW manager.

    Test 5 only proves the new *transport* re-learns the call-id sink. RFC 3261
    dialog matching (in-dialog requests routed by Call-ID + tags) happens at the
    :class:`RegistrationManager` layer via ``add_call(dialog_id, consumer)`` — a
    reconnect that re-attaches the transport sink but forgets to re-attach the
    dialog on the new manager would silently misroute in-dialog requests
    (re-INVITE, BYE) for calls that survived the reconnect. A single shared fake
    manager instance (as Test 5 uses) cannot distinguish "already there from the
    first connect" from "re-attached on reconnect", so this test gives the
    manager its own per-connect-attempt factory, mirroring the transport
    factory pattern above.
    """
    transport = _FakeReconnectTransport()
    managers: list[_FakeManager] = []

    def _manager_factory(*args: object, **kwargs: object) -> _FakeManager:
        m = _FakeManager()
        managers.append(m)
        return m

    fake_sink = MagicMock()
    fake_sink.dialog_id = ("call-id-1", "local-tag-1", "remote-tag-1")

    with (
        patch(
            "hermes_voip.adapter.SipOverTlsTransport",
            side_effect=lambda **kw: _capture_cb(transport, kw),
        ),
        patch("hermes_voip.adapter.RegistrationManager", side_effect=_manager_factory),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.build_providers", return_value=MagicMock()),
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
    ):
        adapter = VoipAdapter(_platform_config())
        await adapter.connect()

        # Directly register a fake call session, keyed by call-id, in the
        # adapter's tracking dict (mirrors how a live inbound/outbound call
        # would have registered itself before the connection dropped).
        adapter._call_sessions["call-id-1"] = fake_sink

        # Trigger reconnect.
        adapter._on_connection_lost(None)

        # Wait for a second (fresh) manager to be constructed by _establish().
        await _until(lambda: len(managers) >= 2, timeout=3.0)

        second_manager = managers[1]
        # The dialog must be re-registered on the NEW manager, not just left
        # dangling on the old (torn-down) one.
        await _until(
            lambda: fake_sink.dialog_id in second_manager._calls,
            timeout=3.0,
        )
        assert second_manager._calls[fake_sink.dialog_id] is fake_sink, (
            "reconnect re-attached the wrong consumer for the dialog"
        )

    await adapter.disconnect()


# ---------------------------------------------------------------------------
# Test 7 (bk1192): degraded flow health clears after a successful reconnect
# ---------------------------------------------------------------------------


async def test_degraded_health_clears_after_successful_reconnect() -> None:
    """Degraded health clears after a later reconnect attempt succeeds.

    ``is_flow_healthy`` goes False while reconnecting fails, then True again
    once a reconnect attempt actually succeeds — the degraded state must not be
    sticky past recovery.
    """
    call_count = 0

    def _factory(**kw: object) -> _FakeReconnectTransport:
        nonlocal call_count
        call_count += 1
        # Only the SECOND transport (the first reconnect attempt) fails; the
        # initial connect and the third transport (second reconnect attempt)
        # both succeed.
        fail = call_count == 2
        t = _FakeReconnectTransport(fail_connect=fail)
        if call_count == 1:
            t._on_connection_lost_cb = kw.get("on_connection_lost")
        return t

    manager = _FakeManager()

    with (
        patch("hermes_voip.adapter.SipOverTlsTransport", side_effect=_factory),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.build_providers", return_value=MagicMock()),
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
        # A real (small but non-zero) backoff window so the "degraded between
        # attempts" state is actually observable by the poll below, rather
        # than the failed attempt and the successful retry both completing
        # within the same event-loop tick.
        patch("hermes_voip.adapter._RECONNECT_BACKOFF_INITIAL", 0.1),
        patch("hermes_voip.adapter._RECONNECT_BACKOFF_CAP", 0.1),
    ):
        adapter = VoipAdapter(_platform_config())
        await adapter.connect()
        assert adapter.is_flow_healthy is True, "must start out healthy"

        # Trigger connection loss; the first reconnect attempt is made to fail.
        adapter._on_connection_lost(None)

        # While the failed attempt is backing off before its retry, flow
        # health must report degraded (not healthy).
        await _until(lambda: not adapter.is_flow_healthy, timeout=3.0)

        # The retry succeeds; degraded health must clear back to healthy.
        await _until(lambda: adapter.is_flow_healthy, timeout=3.0)
        # Prove the clear was CAUSED by a genuine successful reconnect, not a
        # spurious flip: the failed attempt (transport #2) AND the succeeding
        # retry (transport #3) must both have been constructed by now, so the
        # health cleared strictly *after* a real reconnect completed.
        assert call_count >= 3, (
            f"expected a failed attempt + a successful retry (>=3 transports built), "
            f"got {call_count} — cannot conclude health cleared after a real reconnect"
        )
        assert adapter.is_flow_healthy is True, (
            "degraded health did not clear after a successful reconnect"
        )

    await adapter.disconnect()


# ---------------------------------------------------------------------------
# bk1321: structured SIP transport loss / retry / recovery SLO events.
# Deterministic — the lost event is driven by a direct _on_connection_lost call
# and the retry/recovery events by driving _reconnect_with_backoff() with a
# patched _establish (fail once, then succeed). No sleep-based supervisor race.
# ---------------------------------------------------------------------------

# A gateway-host-shaped sentinel. ADR-0084 / rule 34: the raw transport exception
# text (which can embed the gateway host) rides the EXISTING human WARNING prose,
# but MUST NEVER reach the structured sip_transport_* event records — the no-leak
# assertions prove that.
_HOST_SENTINEL = "sip-gw-sentinel.invalid:5061 connection reset"

_TRANSPORT_EVENTS: frozenset[str] = frozenset(
    {"sip_transport_lost", "sip_transport_retry", "sip_transport_recovered"}
)


def _events_named(
    caplog: pytest.LogCaptureFixture, name: str
) -> list[logging.LogRecord]:
    """Captured records carrying structured ``event == name``."""
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def _assert_no_transport_leak(records: list[logging.LogRecord]) -> None:
    """No sip_transport_* event record carries the host/exception sentinel."""
    for record in records:
        blob = f"{record.getMessage()} {record.__dict__!r}"
        assert _HOST_SENTINEL not in blob, (
            f"event {getattr(record, 'event', None)!r} leaked host/exc: {blob!r}"
        )
        assert "sip-gw-sentinel" not in blob, (
            f"event {getattr(record, 'event', None)!r} leaked a host fragment"
        )


async def test_on_connection_lost_emits_transport_lost_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception-bearing drop emits sip_transport_lost reason='error' (bk1321).

    The raw exception (host-shaped sentinel) rides the existing WARNING prose but
    NOT the structured event, which carries only the reason category (ADR-0084).
    """
    adapter = VoipAdapter(_platform_config())
    adapter._connected = True  # else _on_connection_lost returns at its guard
    adapter._lost_event = asyncio.Event()

    with caplog.at_level(logging.INFO, logger="hermes_voip.adapter"):
        adapter._on_connection_lost(OSError(_HOST_SENTINEL))

    lost = _events_named(caplog, "sip_transport_lost")
    assert len(lost) == 1, f"expected one sip_transport_lost, got {len(lost)}"
    assert lost[0].reason == "error", (
        f"exception-bearing drop must be reason='error', got {lost[0].reason!r}"
    )
    _assert_no_transport_leak(lost)


async def test_on_connection_lost_emits_transport_lost_clean(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A graceful (exception-free) close emits sip_transport_lost reason='clean'."""
    adapter = VoipAdapter(_platform_config())
    adapter._connected = True
    adapter._lost_event = asyncio.Event()

    with caplog.at_level(logging.INFO, logger="hermes_voip.adapter"):
        adapter._on_connection_lost(None)

    lost = _events_named(caplog, "sip_transport_lost")
    assert len(lost) == 1
    assert lost[0].reason == "clean"


async def test_reconnect_backoff_emits_retry_then_recovered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed-then-successful reconnect emits sip_transport_retry + _recovered.

    Drives ``_reconnect_with_backoff()`` directly with ``_establish`` patched to
    fail once (raising the host sentinel) then succeed, backoff constants patched
    to ~0. Asserts the retry event (attempt=1, consecutive_failures=1, backoff_s
    float) and the recovery event (attempts=2, downtime_s float), and that neither
    leaks the host/exception text (ADR-0084).
    """
    adapter = VoipAdapter(_platform_config())
    adapter._connected = True
    adapter._consecutive_failures = 0
    adapter._transport = None
    adapter._manager = None
    adapter._lost_at = time.monotonic()  # a prior loss, for the downtime figure

    establish_calls = 0

    def _establish_fail_once() -> None:
        nonlocal establish_calls
        establish_calls += 1
        if establish_calls == 1:
            raise OSError(_HOST_SENTINEL)  # first attempt fails

    with (
        patch.object(
            adapter, "_establish", new=AsyncMock(side_effect=_establish_fail_once)
        ),
        patch("hermes_voip.adapter._RECONNECT_BACKOFF_INITIAL", 0.001),
        patch("hermes_voip.adapter._RECONNECT_BACKOFF_CAP", 0.001),
        caplog.at_level(logging.INFO, logger="hermes_voip.adapter"),
    ):
        await adapter._reconnect_with_backoff()

    assert establish_calls == 2, f"expected fail+success, got {establish_calls}"

    retries = _events_named(caplog, "sip_transport_retry")
    assert len(retries) == 1, f"expected one retry event, got {len(retries)}"
    assert retries[0].attempt == 1
    assert retries[0].consecutive_failures == 1
    assert isinstance(retries[0].backoff_s, float)
    assert retries[0].backoff_s >= 0.0

    recovered = _events_named(caplog, "sip_transport_recovered")
    assert len(recovered) == 1, f"expected one recovered event, got {len(recovered)}"
    assert recovered[0].attempts == 2
    assert isinstance(recovered[0].downtime_s, float)
    assert recovered[0].downtime_s >= 0.0

    _assert_no_transport_leak(retries + recovered)
