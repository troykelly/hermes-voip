"""RED test for reconnect-aware gateway ``connect(is_reconnect=...)`` calls.

Issue #350 reports a Hermes gateway path calling
``adapter.connect(is_reconnect=False)`` on first connect and
``adapter.connect(is_reconnect=True)`` on a supervised reconnect. Our
``VoipAdapter.connect()`` had no such argument, so that gateway call form raises
``TypeError`` and the VoIP platform never comes up.

These tests pin the keyword-only ``is_reconnect`` parameter into our adapter's
signature tolerance. They require the optional ``hermes`` extra and skip cleanly
without it; the ``hermes-contract`` CI job installs the extra so they actually
run.

All fakes only; no real network, no real TLS. Credentials are obvious fakes
(``pbx.example.test`` / ext ``1000`` / ``127.0.0.1``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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

pytestmark = pytest.mark.asyncio

_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
}


def _platform_config() -> PlatformConfig:
    """A PlatformConfig carrying fake SIP credentials."""
    return PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))


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


class _FakeTransport:
    """Minimal fake transport (no network)."""

    def __init__(self) -> None:
        self._on_connection_lost_cb: object = None

    @property
    def local_sent_by(self) -> str:
        """Return fake local sent-by for tests."""
        return "127.0.0.1:5061"

    def contact_uri(self, extension: str) -> str:
        """Build a fake Contact URI."""
        return f"<sip:{extension}@127.0.0.1:5061;transport=tls>"

    async def connect(self) -> None:
        """No-op connect."""

    async def aclose(self) -> None:
        """No-op close."""

    def bind_manager(self, manager: object) -> None:
        """No-op manager bind."""


class _FakeManager:
    """Minimal stand-in for RegistrationManager."""

    def __init__(self) -> None:
        self._is_up = True

    @property
    def is_up(self) -> bool:
        """Return registration status."""
        return self._is_up

    async def connect(self, *, timeout: float = 10.0) -> bool:
        """Fake connect; always returns is_up."""
        return self._is_up

    async def aclose(self) -> None:
        """No-op close."""

    @property
    def registration_call_ids(self) -> frozenset[str]:
        """Return empty set (no live registrations in fakes)."""
        return frozenset()


def _capture_cb(transport: _FakeTransport, kw: dict[str, object]) -> _FakeTransport:
    """Store on_connection_lost callback on the fake transport and return it."""
    transport._on_connection_lost_cb = kw.get("on_connection_lost")
    return transport


@pytest.mark.parametrize("is_reconnect", [False, True])
async def test_connect_accepts_is_reconnect_keyword(is_reconnect: bool) -> None:
    """connect(is_reconnect=...) must not raise TypeError and must bring up.

    A reconnect-aware gateway may call ``adapter.connect(is_reconnect=...)``
    on each connect path. Our adapter must accept the keyword-only flag and
    return ``True`` (degraded-up) without raising (issue #350).
    """
    transport = _FakeTransport()
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
        up = await adapter.connect(is_reconnect=is_reconnect)
        assert up is True
        await adapter.disconnect()
