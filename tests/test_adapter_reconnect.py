"""RED tests for the VoipAdapter reconnect supervisor (RFC 5626 SIP flow resilience).

These tests require the optional ``hermes`` extra and skip cleanly without it;
the ``hermes-contract`` CI job installs the extra so they actually run there.

All fakes only; no real network, no real TLS.  Credentials are obvious fakes
(``pbx.example.test`` / ext ``1000`` / ``127.0.0.1``).
"""

from __future__ import annotations

import asyncio
import logging
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

    def remove_call(self, call_id: str) -> None:
        """Forget a call sink."""
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
    transport._on_connection_lost_cb = kw.get(  # type: ignore[assignment]
        "on_connection_lost"
    )
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
        adapter._on_connection_lost(None)  # type: ignore[attr-defined]

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
            t._on_connection_lost_cb = kw.get(  # type: ignore[assignment]
                "on_connection_lost"
            )
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
        adapter._on_connection_lost(None)  # type: ignore[attr-defined]

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
            t._on_connection_lost_cb = kw.get(  # type: ignore[assignment]
                "on_connection_lost"
            )
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
        patch("asyncio.sleep", new_callable=AsyncMock),
        caplog.at_level(logging.ERROR, logger="hermes_voip.adapter"),
    ):
        adapter = VoipAdapter(_platform_config())
        await adapter.connect()

        # Trigger connection loss.
        adapter._on_connection_lost(None)  # type: ignore[attr-defined]

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
        adapter._on_connection_lost(None)  # type: ignore[attr-defined]

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
            t._on_connection_lost_cb = kw.get(  # type: ignore[assignment]
                "on_connection_lost"
            )
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
        adapter._call_sessions["call-id-1"] = fake_sink  # type: ignore[attr-defined]

        # Trigger reconnect.
        adapter._on_connection_lost(None)  # type: ignore[attr-defined]

        # Wait for a second transport to be created.
        await _until(lambda: len(transports) >= 2, timeout=3.0)

        second_transport = transports[1]
        # The second transport must have the call-id registered in its _calls dict.
        await _until(
            lambda: "call-id-1" in second_transport._calls,
            timeout=3.0,
        )

    await adapter.disconnect()
