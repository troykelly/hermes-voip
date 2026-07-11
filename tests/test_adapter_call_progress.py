"""Adapter surfacing of call-progress events to the agent (ADR-0064 wiring, #43).

The merged :class:`CallProgressDetector` is wired into the CallLoop pump, which
surfaces each :class:`CallProgressEvent` to the adapter via a callback. The
adapter's ``_handle_call_progress`` then ADVISES the agent and (for fax) acts:

* ``FaxCng`` / ``FaxCed`` => when ``amd_hang_up_on_fax`` is on, auto hang up (a fax
  cannot converse); the agent is also told.
* ``AnsweringMachine`` => inject an ``internal=True`` system turn into the call's
  OWN session telling the agent it reached voicemail, so the agent decides whether
  to hang up or wait and leave a message (operator intent).
* ``ReadyToLeaveMessage`` => inject a system cue that the agent may now speak its
  message (the record beep / post-greeting silence fired).
* ``LikelyHuman`` => advisory only; no injected turn, no hangup.

These drive ``_handle_call_progress`` directly against the REAL ``VoipAdapter``
base with fakes (``pbx.example.test`` / ext ``1000``); they run in the
hermes-contract CI job like the other adapter tests.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_voip.caller_modes import CallerGroup
from hermes_voip.media.call_progress import (
    AnsweringMachine,
    FaxCed,
    FaxCng,
    LikelyHuman,
    ReadyToLeaveMessage,
)
from hermes_voip.message import new_call_id
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from hermes_voip.adapter import VoipAdapter
    from hermes_voip.providers.audio import PcmFrame
    from hermes_voip.providers.tts import TtsStream


@pytest.fixture(autouse=True)
def _register_voip_platform() -> None:
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
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._calls: dict[str, object] = {}

    @property
    def local_sent_by(self) -> str:
        return "127.0.0.1:5061"

    def contact_uri(self, extension: str) -> str:
        return f"<sip:{extension}@127.0.0.1:5061;transport=tls>"

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def connect(self) -> None: ...

    async def aclose(self) -> None: ...

    def bind_manager(self, manager: object) -> None: ...

    def add_call(self, call_id: str, sink: object) -> None:
        self._calls[call_id] = sink

    def remove_call(self, call_id: str, sink: object | None = None) -> None:
        self._calls.pop(call_id, None)


class _FakeManager:
    def __init__(self, *, is_up: bool = True) -> None:
        self._is_up = is_up
        self.connected = False
        self.closed = False

    @property
    def is_up(self) -> bool:
        return self._is_up

    async def connect(self, *, timeout: float = 10.0) -> bool:
        self.connected = True
        return self._is_up

    async def aclose(self) -> None:
        self.closed = True

    def add_call(self, dialog_id: tuple[str, str, str], consumer: object) -> None: ...

    def remove_call(self, dialog_id: tuple[str, str, str]) -> None: ...


class _FakeTtsStream:
    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        async def _gen() -> AsyncIterator[PcmFrame]:
            return
            yield  # pragma: no cover

        return _gen()

    async def cancel(self) -> None: ...


class _FakeTTS:
    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        return _FakeTtsStream()  # type: ignore[return-value]


class _FakeASR:
    async def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[object]:
        finals: list[object] = []
        async for _ in audio:
            pass  # consume so the pump does not stall
        for transcript in finals:  # always empty — forces the async-gen shape
            yield transcript


class _FakeGuard:
    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            score=0.0,
            degraded=False,
            normalized_text=text,
            reasons=(),
        )


def _fake_providers() -> Providers:
    return Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())  # type: ignore[arg-type]


_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
}


async def _build_adapter(
    transport: _FakeTransport,
    manager: _FakeManager,
    *,
    media_cfg: object | None = None,
) -> VoipAdapter:
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))
    media = media_cfg if media_cfg is not None else MagicMock()
    with (
        patch(
            "hermes_voip.adapter.load_gateway_config",
            return_value=MagicMock(
                host="pbx.example.test",
                transport="tls",
                port=5061,
                extensions=(
                    MagicMock(
                        index=0, extension="1000", username="1000", password="fake"
                    ),
                ),
                default_extension=MagicMock(extension="1000"),
                via_transport="TLS",
                max_calls=8,
                shutdown_drain_secs=5.0,
                # ADR-0113: 0 disables the max-duration watchdog (no real timer here).
                max_call_duration_secs=0.0,
            ),
        ),
        patch("hermes_voip.adapter.load_media_config", return_value=media),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
    # connect() (which sets _media_cfg from load_media_config) is not called in these
    # unit tests, so wire the media config the fax-hangup gate reads directly.
    adapter._media_cfg = media  # type: ignore[assignment]  # test double / MagicMock
    return adapter


def _outbound_info() -> dict[str, object]:
    return {
        "name": "1000",
        "remote_uri": "sip:1000@pbx.example.test",
        "type": "dm",
        "ended": False,
        "group": CallerGroup(
            name="outbound",
            privilege_level=0,
            persona="outbound",
            declined_at_sip=False,
        ),
        "objective": "remind them about the meeting",
    }


async def _collect(captured: list[str]) -> None:
    for _ in range(50):
        if captured:
            return
        await asyncio.sleep(0.02)


# ===========================================================================
# Fax: auto hang-up (config-gated) + advisory
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [FaxCng(elapsed_s=0.5), FaxCed(elapsed_s=1.2)])
async def test_fax_event_hangs_up_when_enabled(event: FaxCng | FaxCed) -> None:
    """A fax tone auto-hangs-up the call when ``amd_hang_up_on_fax`` is on."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    media = MagicMock()
    media.amd_hang_up_on_fax = True
    adapter = await _build_adapter(transport, manager, media_cfg=media)

    call_id = new_call_id()
    adapter._call_info[call_id] = _outbound_info()

    hung_up = False

    class _Session:
        ended = False

        async def hang_up(self) -> None:
            nonlocal hung_up
            hung_up = True

    adapter._call_sessions[call_id] = _Session()  # type: ignore[assignment]
    await adapter._handle_call_progress(call_id, event)

    assert hung_up is True


@pytest.mark.asyncio
async def test_fax_event_does_not_hang_up_when_disabled() -> None:
    """With ``amd_hang_up_on_fax`` off, a fax tone does NOT auto-hang-up."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    media = MagicMock()
    media.amd_hang_up_on_fax = False
    adapter = await _build_adapter(transport, manager, media_cfg=media)

    call_id = new_call_id()
    adapter._call_info[call_id] = _outbound_info()

    hung_up = False

    class _Session:
        ended = False

        async def hang_up(self) -> None:
            nonlocal hung_up
            hung_up = True

    adapter._call_sessions[call_id] = _Session()  # type: ignore[assignment]
    await adapter._handle_call_progress(call_id, FaxCng(elapsed_s=0.5))

    assert hung_up is False


# ===========================================================================
# AMD: inject a voicemail-context system turn so the agent decides
# ===========================================================================


@pytest.mark.asyncio
async def test_answering_machine_injects_voicemail_context_turn() -> None:
    """An AnsweringMachine verdict injects a system turn telling the agent voicemail."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)

    captured: list[str] = []

    async def _handler(event: object) -> None:
        captured.append(getattr(event, "text", ""))

    adapter.set_message_handler(_handler)

    call_id = new_call_id()
    adapter._call_info[call_id] = _outbound_info()
    await adapter._handle_call_progress(
        call_id,
        AnsweringMachine(elapsed_s=4.0, beep_at_s=None, why="continuous greeting"),
    )
    await _collect(captured)

    assert captured, "no system turn was injected for the answering machine"
    text = captured[0].lower()
    assert "voicemail" in text or "answering machine" in text
    # It is a system directive, not the callee's untrusted words.
    assert "[system" in text


# ===========================================================================
# ReadyToLeaveMessage: inject the record cue
# ===========================================================================


@pytest.mark.asyncio
async def test_ready_to_leave_message_injects_record_cue_turn() -> None:
    """The record cue injects a system turn that the agent may now speak its message."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)

    captured: list[str] = []

    async def _handler(event: object) -> None:
        captured.append(getattr(event, "text", ""))

    adapter.set_message_handler(_handler)

    call_id = new_call_id()
    adapter._call_info[call_id] = _outbound_info()
    await adapter._handle_call_progress(
        call_id, ReadyToLeaveMessage(elapsed_s=6.0, beep_at_s=None)
    )
    await _collect(captured)

    assert captured, "no record-cue turn was injected"
    text = captured[0].lower()
    assert "[system" in text
    assert "message" in text


# ===========================================================================
# LikelyHuman: advisory only — NO injected turn, NO hangup
# ===========================================================================


@pytest.mark.asyncio
async def test_likely_human_injects_no_turn_and_no_hangup() -> None:
    """A LikelyHuman verdict is advisory only: no system turn, no auto-hangup."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    media = MagicMock()
    media.amd_hang_up_on_fax = True
    adapter = await _build_adapter(transport, manager, media_cfg=media)

    captured: list[str] = []

    async def _handler(event: object) -> None:
        captured.append(getattr(event, "text", ""))

    adapter.set_message_handler(_handler)

    call_id = new_call_id()
    adapter._call_info[call_id] = _outbound_info()

    hung_up = False

    class _Session:
        ended = False

        async def hang_up(self) -> None:
            nonlocal hung_up
            hung_up = True

    adapter._call_sessions[call_id] = _Session()  # type: ignore[assignment]
    await adapter._handle_call_progress(
        call_id, LikelyHuman(elapsed_s=1.8, why="short greeting then pause")
    )
    # Give any (erroneous) injected turn a chance to land.
    await asyncio.sleep(0.05)

    assert captured == [], f"LikelyHuman wrongly injected a turn: {captured!r}"
    assert hung_up is False
