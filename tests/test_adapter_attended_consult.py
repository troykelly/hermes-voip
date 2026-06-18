"""Adapter-level guards for the attended-transfer consultation leg (ADR-0048).

These exercise :meth:`hermes_voip.adapter.VoipAdapter.start_attended_consult`
directly with a lightweight fake session installed in ``_call_sessions`` and the
real outbound dial (``place_call``) stubbed, so they need only the ``hermes`` extra
(no transport, no live INVITE). They cover the concurrency guard against a SECOND
consultation for a call that already has one in flight — the first guard's
read->await place_call->write window would otherwise let a second action overwrite
``_attended_consults[call_id]`` and orphan the first leg.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from unittest.mock import MagicMock

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_voip.providers.policy import GuardSessionState

if TYPE_CHECKING:
    from hermes_voip.adapter import VoipAdapter


@pytest.fixture(autouse=True)
def _register_voip_platform() -> None:
    """Register a throwaway "voip" entry so ``Platform("voip")`` resolves."""
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


_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
    # Allow the consult targets used below so the dial is reached (the guard under
    # test sits AFTER the allowlist check).
    "HERMES_VOIP_OUTBOUND_ALLOW": "1000,2000",
}


class _FakeSession:
    """Minimal stand-in for a ``CallSession`` for the consult guard tests.

    ``start_attended_consult`` reads only ``ended`` and ``guard`` from the session
    before dialling, so an operator-level, non-degraded fake suffices.
    """

    def __init__(self) -> None:
        self.ended = False
        self._guard = GuardSessionState(call_id="orig", privilege_level=3)

    @property
    def guard(self) -> GuardSessionState:
        return self._guard


def _make_adapter() -> VoipAdapter:
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))
    adapter = VoipAdapter(config)
    # Mirror what connect() does for the two fields the consult path reads, without
    # standing up a transport: the outbound allowlist and a registered session.
    from hermes_voip.outbound_allow import load_outbound_allowlist  # noqa: PLC0415

    adapter._outbound_allow = load_outbound_allowlist(dict(_FAKE_ENV))
    # _FakeSession is a structural stand-in (start_attended_consult reads only
    # .ended/.guard); it is not a real CallSession, hence the narrow ignore.
    adapter._call_sessions["orig"] = _FakeSession()  # type: ignore[assignment]
    return adapter


@pytest.mark.asyncio
async def test_second_consult_for_same_call_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second consult while one is in flight for the call is refused, not silent.

    The first consult records ``_attended_consults[orig] = consult-1``. A second
    ``start_attended_consult`` for the same original call must NOT dial again and
    overwrite the pairing (which would orphan the first consult leg) — it raises a
    clear error and leaves the first pairing intact.
    """
    adapter = _make_adapter()
    dialed: list[str] = []

    async def _fake_place_call(extension: str, *, objective: str | None = None) -> str:
        dialed.append(extension)
        return f"consult-{len(dialed)}"

    monkeypatch.setattr(adapter, "place_call", _fake_place_call)

    first = await adapter.start_attended_consult("orig", "1000")
    assert first == "consult-1"
    assert adapter._attended_consults["orig"] == "consult-1"

    with pytest.raises(RuntimeError):
        await adapter.start_attended_consult("orig", "2000")

    # The first leg's pairing is intact and no second leg was dialled.
    assert adapter._attended_consults["orig"] == "consult-1"
    assert dialed == ["1000"]
