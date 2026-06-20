"""Tests for VoipAdapter against the REAL hermes-agent BasePlatformAdapter.

``VoipAdapter`` subclasses ``gateway.platforms.base.BasePlatformAdapter`` at
runtime (so it inherits ``handle_message``/``build_source``/
``set_message_handler`` and is recognised by ``isinstance`` in the gateway).
These tests therefore require the optional ``hermes`` extra and skip cleanly
without it; the dedicated ``hermes-contract`` CI job installs the extra so they
actually run there (rule 26: validate against the real deployment target).

The SIP/RTP/provider collaborators are still fakes (no real network, no real
ML); credentials and hostnames are obvious fakes (``pbx.example.test`` / ext
``1000`` / ``127.0.0.1``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The adapter imports the real Hermes base at module top; skip the whole module
# when the optional runtime is absent (it runs in the hermes-contract CI job).
pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import (
    PlatformEntry,
    platform_registry,
)
from gateway.platforms.base import BasePlatformAdapter

from hermes_voip.call_end import CallEndReason
from hermes_voip.config import (
    ConfigError,
    ExtensionConfig,
    GatewayConfig,
    MediaConfig,
    load_media_config,
)
from hermes_voip.manager import (
    InDialog,
    NewCall,
    RegistrationManager,
    RegistrationStatus,
)
from hermes_voip.message import SipRequest, SipResponse, new_call_id, new_tag
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.providers.tts import TtsStream
from hermes_voip.sdp import Codec as SdpCodec

if TYPE_CHECKING:
    from collections.abc import Callable

    from hermes_voip.adapter import VoipAdapter
    from hermes_voip.caller_modes import CallerGroup


async def _until(
    predicate: Callable[[], bool], *, timeout: float = 3.0, step: float = 0.001
) -> None:
    """Poll ``predicate`` until true or the timeout elapses (no fixed sleeps)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            msg = "condition not met within the timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(step)


# ---------------------------------------------------------------------------
# Ensure the "voip" platform name resolves (Platform("voip") only works once
# the plugin has registered it in the module-singleton platform_registry).
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fake SIP transport
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Fake SipTransport + CallSignaling that captures sent messages."""

    def __init__(self, *, local_sent_by: str = "127.0.0.1:5061") -> None:
        self._local_sent_by = local_sent_by
        self.sent: list[str] = []
        self._calls: dict[str, object] = {}

    @property
    def local_sent_by(self) -> str:
        return self._local_sent_by

    def contact_uri(self, extension: str) -> str:
        return f"<sip:{extension}@{self._local_sent_by};transport=tls>"

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def connect(self) -> None:
        """No-op TLS connect for tests."""

    async def aclose(self) -> None:
        """No-op close for tests."""

    def bind_manager(self, manager: object) -> None:
        """No-op manager bind for tests."""

    def add_call(self, call_id: str, sink: object) -> None:
        self._calls[call_id] = sink

    def remove_call(self, call_id: str, sink: object | None = None) -> None:
        if sink is not None and self._calls.get(call_id) is not sink:
            return
        self._calls.pop(call_id, None)


class _ConnectOrderTransport(_FakeTransport):
    """Transport double enforcing the real ``SipOverTlsTransport`` contract.

    ``local_sent_by`` / ``contact_uri`` are unavailable until ``connect()`` has
    been awaited (the live transport learns the local socket address only then).
    Used to prove ``VoipAdapter.connect()`` connects the transport *before* it
    builds the ``RegistrationManager`` (whose ``__init__`` reads ``contact_uri``).
    """

    def __init__(self) -> None:
        super().__init__()
        self._connected = False

    @property
    def local_sent_by(self) -> str:
        if not self._connected:
            msg = "local_sent_by is unavailable before connect()"
            raise RuntimeError(msg)
        return self._local_sent_by

    def contact_uri(self, extension: str) -> str:
        # Reads local_sent_by, so it raises before connect() just like the real one.
        return f"<sip:{extension}@{self.local_sent_by};transport=tls>"

    async def connect(self) -> None:
        self._connected = True


# ---------------------------------------------------------------------------
# Fake manager
# ---------------------------------------------------------------------------


class _FakeManager:
    """Minimal stand-in for RegistrationManager."""

    def __init__(
        self, *, is_up: bool = True, snapshot: tuple[RegistrationStatus, ...] = ()
    ) -> None:
        self._is_up = is_up
        self._snapshot = snapshot
        self._calls: dict[tuple[str, str, str], object] = {}
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

    def add_call(self, dialog_id: tuple[str, str, str], consumer: object) -> None:
        self._calls[dialog_id] = consumer

    def remove_call(self, dialog_id: tuple[str, str, str]) -> None:
        self._calls.pop(dialog_id, None)

    def snapshot(self) -> tuple[RegistrationStatus, ...]:
        return self._snapshot


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


class _FakeTtsStream:
    def __init__(self, frames: list[PcmFrame] | None = None) -> None:
        self._frames = frames or []
        self._cancelled = False

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[PcmFrame]:
        for f in self._frames:
            if self._cancelled:
                return
            yield f

    async def cancel(self) -> None:
        self._cancelled = True


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
        """Drain audio frames and produce no transcripts (empty ASR stub)."""
        chunks: list[object] = []
        async for _ in audio:
            pass  # consume so pump does not stall
        for chunk in chunks:  # chunks is always empty — forces async-gen shape
            yield chunk


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


# ---------------------------------------------------------------------------
# Minimal fake env (never real credentials; pbx.example.test / ext 1000)
# ---------------------------------------------------------------------------

_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
}

_FAKE_MEDIA_ENV: dict[str, str] = {
    "HERMES_VOIP_STT_PROVIDER": "sherpa-onnx",
    "HERMES_VOIP_STT_MODEL_DIR": "/fake/stt",
    "HERMES_VOIP_TTS_PROVIDER": "sherpa-kokoro",
    "HERMES_VOIP_TTS_MODEL": "/fake/tts",
    "HERMES_VOIP_INJECTION_GUARD": "onnx",
    "HERMES_VOIP_INJECTION_GUARD_MODEL_DIR": "/fake/guard",
}


def _platform_config(env: dict[str, str] | None = None) -> PlatformConfig:
    """A real ``PlatformConfig`` carrying the fake SIP/media env in ``extra``."""
    return PlatformConfig(enabled=True, extra=dict(env or {}))


def _ext_config() -> ExtensionConfig:
    return ExtensionConfig(index=0, extension="1000", username="1000", password="fake")


# ---------------------------------------------------------------------------
# Helper to build a minimal inbound INVITE raw text
# ---------------------------------------------------------------------------

_FAKE_SDP_OFFER = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 20000 RTP/AVP 0 8\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=sendrecv\r\n"
)


def _make_invite(call_id: str | None = None, from_tag: str | None = None) -> str:
    cid = call_id or new_call_id()
    ftag = from_tag or new_tag()
    content_length = len(_FAKE_SDP_OFFER.encode("utf-8"))
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKfake\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:9999@pbx.example.test>;tag={ftag}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {cid}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:9999@127.0.0.1:60000;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{_FAKE_SDP_OFFER}"
    )


# ---------------------------------------------------------------------------
# Build a real VoipAdapter wired to fakes (connect() patched collaborators).
# ---------------------------------------------------------------------------


async def _build_adapter(
    transport: _FakeTransport,
    manager: _FakeManager,
    *,
    connect: bool = True,
) -> VoipAdapter:
    """Construct a real ``VoipAdapter`` wired to fakes, optionally calling connect()."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = _platform_config(_FAKE_ENV | _FAKE_MEDIA_ENV)

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
                # ADR-0059 lifecycle knobs — real ints/floats (not MagicMocks) so the
                # admission cap comparison + drain timeout work, not a MagicMock
                # TypeError.
                max_calls=8,
                shutdown_drain_secs=5.0,
            ),
        ),
        patch(
            "hermes_voip.adapter.load_media_config", return_value=load_media_config({})
        ),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch(
            "hermes_voip.adapter._make_tls_context",
            return_value=MagicMock(),
        ),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        if connect:
            await adapter.connect()
        return adapter


# ---------------------------------------------------------------------------
# (sub) VoipAdapter is a real BasePlatformAdapter subclass
# ---------------------------------------------------------------------------


def test_voip_adapter_subclasses_base() -> None:
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    assert issubclass(VoipAdapter, BasePlatformAdapter)


def test_voip_adapter_instance_is_base_and_has_inherited_methods() -> None:
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    adapter = VoipAdapter(_platform_config(_FAKE_ENV | _FAKE_MEDIA_ENV))
    assert isinstance(adapter, BasePlatformAdapter)
    # Inherited runtime services (NOT defined on VoipAdapter itself):
    for name in ("handle_message", "build_source", "set_message_handler"):
        assert callable(getattr(adapter, name)), f"missing inherited {name}"


def test_voip_adapter_declares_not_editable_to_force_complete_send() -> None:
    """A voice call must receive each reply as ONE complete ``send()`` (ADR-0057 §3).

    ``BasePlatformAdapter`` declares **no** ``SUPPORTS_MESSAGE_EDITING`` class
    attribute, and the gateway's streaming gate reads it with a ``True`` default
    (``getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True)`` in ``gateway/run.py``):
    so an adapter that does not override it is treated as **editable**. The base also
    ships a working default ``edit_message``. With Hermes streaming enabled, the
    gateway therefore feeds an editable adapter its reply as a partial-prefix
    ``send()`` followed by repeated ``edit_message()`` calls carrying *cumulative*
    growing text (the ``GatewayStreamConsumer`` edit-in-place renderer, flushed on a
    time/codepoint threshold — never on sentence boundaries). Our real-time audio
    pipeline (``send()`` → ``CallLoop.speak`` → sentence aggregation → RTP) consumes
    ONE complete reply string; cumulative-prefix edits would garble/duplicate audio.

    ``VoipAdapter`` therefore explicitly sets ``SUPPORTS_MESSAGE_EDITING = False``,
    which makes the gate take its ``if not SUPPORTS_MESSAGE_EDITING: skip streaming``
    branch (``gateway/run.py`` raises *"skip streaming for non-editable platform"*) and
    deliver the reply as a single complete ``send()`` — regardless of the operator's
    Hermes streaming config. This pins that contract.
    """
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    # The base declares no such attribute (the gateway defaults it to editable);
    # ours is an explicit, deliberate opt-out that forces the single complete send().
    assert not hasattr(BasePlatformAdapter, "SUPPORTS_MESSAGE_EDITING")
    assert VoipAdapter.SUPPORTS_MESSAGE_EDITING is False


# ---------------------------------------------------------------------------
# (a) connect() returns True when the fake manager is up; False when down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_returns_true_when_manager_up() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    _ = await _build_adapter(transport, manager)
    assert manager.connected


@pytest.mark.asyncio
async def test_connect_returns_false_when_manager_down() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=False)
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = _platform_config(_FAKE_ENV | _FAKE_MEDIA_ENV)

    with (
        patch("hermes_voip.adapter.load_gateway_config", return_value=MagicMock()),
        patch(
            "hermes_voip.adapter.load_media_config", return_value=load_media_config({})
        ),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        result = await adapter.connect()
    assert result is False


# ---------------------------------------------------------------------------
# (b) send() routes to CallLoop.speak(); unknown call_id → failure result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_unknown_call_id_returns_failure() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    result = await adapter.send("nonexistent-call-id", "hello")
    assert not result.success


@pytest.mark.asyncio
async def test_send_known_call_id_calls_loop_speak() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)

    call_id = new_call_id()
    spoken: list[str] = []

    class _FakeLoop:
        async def speak(self, text: AsyncIterator[str]) -> None:
            async for chunk in text:
                spoken.append(chunk)

    adapter._call_loops[call_id] = _FakeLoop()  # type: ignore[assignment]

    result = await adapter.send(call_id, "hello agent")
    assert result.success
    assert "hello agent" in spoken


# The exact home-channel onboarding notice the Hermes runtime delivers to a
# platform adapter's send() on the first turn of a session with no
# ``VOIP_HOME_CHANNEL`` (verified text of ``gateway.run._handle_message`` in
# hermes-agent 0.16.0). The runtime routes it through ``adapter.send`` exactly
# like a genuine reply, so without a guard it is synthesised as audio — the
# operator-reported "No home channel is set for voip" leak.
_HOME_CHANNEL_NOTICE = (
    "\U0001f4ec No home channel is set for Voip. "
    "A home channel is where Hermes delivers cron job results "
    "and cross-platform messages.\n\n"
    "Type /sethome to make this chat your home channel, "
    "or ignore to skip."
)


@pytest.mark.asyncio
async def test_send_home_channel_notice_is_not_spoken() -> None:
    """A gateway home-channel/proactive notice must never reach the caller.

    The runtime delivers it via ``send()`` during a live call; the adapter
    drops it (logged, not synthesised) and reports success so the runtime does
    not treat the drop as a delivery failure and retry.
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)

    call_id = new_call_id()
    spoken: list[str] = []

    class _FakeLoop:
        async def speak(self, text: AsyncIterator[str]) -> None:
            async for chunk in text:
                spoken.append(chunk)

    adapter._call_loops[call_id] = _FakeLoop()  # type: ignore[assignment]

    result = await adapter.send(call_id, _HOME_CHANNEL_NOTICE)

    # Dropped, not spoken — and reported success (no retry storm).
    assert result.success
    assert spoken == []


@pytest.mark.asyncio
async def test_send_genuine_reply_still_spoken_alongside_notice_guard() -> None:
    """The notice guard must not over-filter genuine conversational replies."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)

    call_id = new_call_id()
    spoken: list[str] = []

    class _FakeLoop:
        async def speak(self, text: AsyncIterator[str]) -> None:
            async for chunk in text:
                spoken.append(chunk)

    adapter._call_loops[call_id] = _FakeLoop()  # type: ignore[assignment]

    reply = "Sure, I can set up that home channel in the lounge for you."
    result = await adapter.send(call_id, reply)
    assert result.success
    assert reply in spoken


# ---------------------------------------------------------------------------
# (c) get_chat_info returns {name, type} for live + ended + unknown calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_chat_info_live_call() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()
    adapter._call_info[call_id] = {
        "name": "9999",
        "remote_uri": "sip:9999@pbx.example.test",
        "type": "dm",
        "ended": False,
    }
    info = await adapter.get_chat_info(call_id)
    assert info["name"] == "9999"
    assert info["type"] == "dm"


@pytest.mark.asyncio
async def test_get_chat_info_ended_call() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()
    adapter._call_info[call_id] = {
        "name": "caller",
        "remote_uri": "sip:caller@pbx.example.test",
        "type": "dm",
        "ended": True,
    }
    info = await adapter.get_chat_info(call_id)
    assert info["type"] == "dm"


@pytest.mark.asyncio
async def test_get_chat_info_unknown_returns_fallback() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    info = await adapter.get_chat_info("unknown-call")
    assert "type" in info


# ---------------------------------------------------------------------------
# (d) validate_voip_config raises ConfigError on missing host (light module)
# ---------------------------------------------------------------------------


def test_validate_config_raises_on_missing_host() -> None:
    from hermes_voip.plugin import validate_voip_config  # noqa: PLC0415

    with pytest.raises(ConfigError):
        validate_voip_config(_platform_config({}))


def test_validate_config_truthy_with_valid_env() -> None:
    from hermes_voip.plugin import validate_voip_config  # noqa: PLC0415

    assert validate_voip_config(_platform_config(_FAKE_ENV)) is True


# ---------------------------------------------------------------------------
# (e) inbound INVITE creates a CallLoop per call (heavy IO patched out)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_invite_registers_call_loop() -> None:
    """An inbound INVITE should create a CallLoop in _call_loops."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)

    call_id = new_call_id()
    invite_raw = _make_invite(call_id=call_id)

    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                # RTCP activation (ADR-0061): the inbound plain-RTP path now starts
                # RTCP after connect(); the fake engine models the awaitable method +
                # the inert _rtcp_active flag so teardown's quality log is skipped.
                start_rtcp=AsyncMock(return_value=None),
                _rtcp_active=False,
                local_port=20002,
                inbound_sample_rate=8_000,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallSession",
            return_value=MagicMock(
                dialog_id=("call-id", "local-tag", "remote-tag"),
                ended=False,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        invite_req = SipRequest.parse(invite_raw)
        new_call = NewCall(registration=_ext_config(), invite=invite_req)
        adapter._on_inbound_invite(new_call)

        for _ in range(10):
            await asyncio.sleep(0)

        assert call_id in adapter._call_info


@pytest.mark.asyncio
async def test_inbound_invite_threads_greeting_into_call_loop() -> None:
    """The adapter must pass the media config's greeting into CallLoop.

    This is the on-answer NAT-latch wiring (ADR-0002): the configured opening
    line reaches the per-call loop, which speaks it the instant the call is
    answered so RTP flows out first.
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    # Replace the (mocked) media config with a real one carrying a concrete
    # greeting, so we assert the adapter threads THAT value into CallLoop.
    adapter._media_cfg = load_media_config(
        {"HERMES_VOIP_GREETING": "Hello from the gateway."}
    )

    call_id = new_call_id()
    invite_raw = _make_invite(call_id=call_id)
    call_loop_cls = MagicMock(return_value=MagicMock(run=AsyncMock(return_value=None)))

    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                # RTCP activation (ADR-0061): the inbound plain-RTP path now starts
                # RTCP after connect(); the fake engine models the awaitable method +
                # the inert _rtcp_active flag so teardown's quality log is skipped.
                start_rtcp=AsyncMock(return_value=None),
                _rtcp_active=False,
                local_port=20002,
                inbound_sample_rate=8_000,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallSession",
            return_value=MagicMock(
                dialog_id=("call-id", "local-tag", "remote-tag"),
                ended=False,
            ),
        ),
        patch("hermes_voip.adapter.CallLoop", call_loop_cls),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        invite_req = SipRequest.parse(invite_raw)
        new_call = NewCall(registration=_ext_config(), invite=invite_req)
        adapter._on_inbound_invite(new_call)

        for _ in range(10):
            await asyncio.sleep(0)

    call_loop_cls.assert_called_once()
    assert call_loop_cls.call_args.kwargs["greeting"] == "Hello from the gateway."


# ---------------------------------------------------------------------------
# (f) a finalized transcript reaches the agent via the REAL handle_message
#     (set_message_handler + inherited handle_message — no SimpleNamespace stub)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_turn_routes_to_message_handler() -> None:
    """deliver_turn must reach the agent's message handler via the real base."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)

    received: list[str] = []

    async def _handler(event: object) -> None:
        received.append(getattr(event, "text", ""))

    adapter.set_message_handler(_handler)

    call_id = new_call_id()
    adapter._call_info[call_id] = {
        "name": "9999",
        "remote_uri": "sip:9999@pbx.example.test",
        "type": "dm",
        "ended": False,
    }

    await adapter._deliver_turn(call_id, "hello from caller")

    # handle_message spawns a background task in the base; give it time.
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.02)

    # The delivered turn carries the spotlighted per-mode persona preamble
    # (ADR-0020) wrapping the caller's transcript as untrusted data, so it
    # contains the caller's words rather than equalling them verbatim.
    assert len(received) == 1
    assert "hello from caller" in received[0]
    assert "UNTRUSTED_CALLER_TRANSCRIPT" in received[0]


@pytest.mark.asyncio
async def test_deliver_turn_builds_voice_message_event() -> None:
    """The MessageEvent handed to the agent must be VOICE-typed with the text."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)

    from gateway.platforms.base import MessageType  # noqa: PLC0415

    captured: list[object] = []

    async def _handler(event: object) -> None:
        captured.append(event)

    adapter.set_message_handler(_handler)
    call_id = new_call_id()
    adapter._call_info[call_id] = {
        "name": "9999",
        "remote_uri": "sip:9999@pbx.example.test",
        "type": "dm",
        "ended": False,
    }
    await adapter._deliver_turn(call_id, "voice turn")
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.02)

    assert captured, "no event reached the handler"
    event = captured[0]
    # The text is the spotlighted persona preamble + the caller's transcript
    # (ADR-0020); assert the transcript is present rather than a bare equal.
    assert "voice turn" in getattr(event, "text", "")
    assert getattr(event, "message_type", None) == MessageType.VOICE


# ---------------------------------------------------------------------------
# ElevenLabs v3 audio-tag PROMPT encouragement (ADR-0068, extends ADR-0027).
#
# When the active TTS is an ElevenLabs v3-family model, the spotlighted turn
# gains a short preamble line that ENCOURAGES the agent to use audio tags
# sparingly (so its expressive voice sounds natural). The line is two-sided:
# emitted ONLY on a v3 model (the gate), and never on a non-v3 model — which is
# also where the existing TTS-seam ``strip_audio_tags`` scrubs any stray tag, so
# a non-v3 caller never voices a bracketed cue literally. ``_deliver_turn``
# computes the gate from ``self._media_cfg`` and threads the bool into
# ``_spotlight_turn``; both the adapter gate and the TTS-seam strip route through
# ``model_supports_audio_tags`` so they agree for every model id.
#
# A distinctive fragment of the approved preamble text used as the assertion
# anchor (one phrase that appears nowhere else in the spotlighted turn).
_AUDIO_TAG_PROMPT_ANCHOR = "expressive voice"


def _outbound_group() -> CallerGroup:
    """An outbound :class:`CallerGroup` (the persona that has framing to append).

    The outbound persona is the one that has trusted framing AFTER the persona
    preamble, so it exercises the audio-tag line's injection point fully.
    """
    from hermes_voip.caller_modes import CallerGroup  # noqa: PLC0415

    return CallerGroup(
        name="outbound",
        privilege_level=2,
        persona="outbound",
        declined_at_sip=False,
    )


def test_spotlight_turn_includes_audio_tag_preamble_only_when_v3() -> None:
    """``_spotlight_turn`` appends the audio-tag line iff ``v3_audio_tags=True``.

    Two-sided: the distinctive preamble fragment is present when the flag is True
    and absent when it is False, while the caller transcript is carried either way.
    """
    from hermes_voip.adapter import _spotlight_turn  # noqa: PLC0415

    group = _outbound_group()

    on = _spotlight_turn(group, "Jordan", "hello there", v3_audio_tags=True)
    off = _spotlight_turn(group, "Jordan", "hello there", v3_audio_tags=False)

    # The transcript survives in both (the line is additive, never a replacement).
    assert "hello there" in on
    assert "hello there" in off

    # The encouragement line appears ONLY on the v3 side.
    assert _AUDIO_TAG_PROMPT_ANCHOR in on
    assert "[laughs]" in on
    assert "sparingly" in on
    assert _AUDIO_TAG_PROMPT_ANCHOR not in off
    assert "[laughs]" not in off


def test_spotlight_turn_audio_tag_default_is_off() -> None:
    """Omitting ``v3_audio_tags`` defaults to OFF — no audio-tag line by default."""
    from hermes_voip.adapter import _spotlight_turn  # noqa: PLC0415

    out = _spotlight_turn(_outbound_group(), "Jordan", "hi")
    assert _AUDIO_TAG_PROMPT_ANCHOR not in out


def test_spotlight_turn_audio_tag_line_follows_persona_framing() -> None:
    """The audio-tag line sits AFTER the persona framing and BEFORE the untrusted block.

    The encouragement is trusted system framing about delivery, so it must never
    fall inside the untrusted-caller fence (where a malicious callee's text lives).
    """
    from hermes_voip.adapter import (  # noqa: PLC0415
        _UNTRUSTED_OPEN,
        _spotlight_turn,
    )

    out = _spotlight_turn(
        _outbound_group(), "Jordan", "hello there", v3_audio_tags=True
    )
    anchor_at = out.index(_AUDIO_TAG_PROMPT_ANCHOR)
    fence_at = out.index(_UNTRUSTED_OPEN)
    assert anchor_at < fence_at, "audio-tag line must precede the untrusted fence"


async def _spotlight_kwarg_for_media_env(env: dict[str, str]) -> bool:
    """Drive ``_deliver_turn`` under a media config and capture the gate kwarg.

    Patches the module-level ``_spotlight_turn`` to record the ``v3_audio_tags``
    keyword the adapter computes from ``self._media_cfg`` for the given TTS env,
    so the test asserts the GATE, not the prose.
    """
    from hermes_voip import adapter as adapter_mod  # noqa: PLC0415

    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    adapter._media_cfg = load_media_config(env)

    async def _noop_handler(event: object) -> None:
        return None

    adapter.set_message_handler(_noop_handler)

    captured: dict[str, bool] = {}
    real_spotlight = adapter_mod._spotlight_turn

    def _spy(
        group: CallerGroup,
        caller_name: str,
        text: str,
        *,
        objective: str | None = None,
        v3_audio_tags: bool = False,
    ) -> str:
        captured["v3_audio_tags"] = v3_audio_tags
        return real_spotlight(
            group,
            caller_name,
            text,
            objective=objective,
            v3_audio_tags=v3_audio_tags,
        )

    call_id = new_call_id()
    adapter._call_info[call_id] = {"name": "9999", "type": "dm", "ended": False}
    with patch.object(adapter_mod, "_spotlight_turn", _spy):
        await adapter._deliver_turn(call_id, "voice turn")
    assert "v3_audio_tags" in captured, "_deliver_turn did not call _spotlight_turn"
    return captured["v3_audio_tags"]


@pytest.mark.asyncio
async def test_deliver_turn_gate_true_for_elevenlabs_v3() -> None:
    """ElevenLabs + ``eleven_v3`` ⇒ the adapter passes ``v3_audio_tags=True``."""
    gate = await _spotlight_kwarg_for_media_env(
        {
            "HERMES_VOIP_TTS_PROVIDER": "elevenlabs",
            "HERMES_VOIP_TTS_MODEL": "eleven_v3",
        }
    )
    assert gate is True


@pytest.mark.asyncio
async def test_deliver_turn_gate_false_for_elevenlabs_flash() -> None:
    """ElevenLabs + ``eleven_flash_v2_5`` (non-v3) ⇒ ``v3_audio_tags=False``."""
    gate = await _spotlight_kwarg_for_media_env(
        {
            "HERMES_VOIP_TTS_PROVIDER": "elevenlabs",
            "HERMES_VOIP_TTS_MODEL": "eleven_flash_v2_5",
        }
    )
    assert gate is False


@pytest.mark.asyncio
async def test_deliver_turn_gate_false_for_non_elevenlabs_provider() -> None:
    """A non-ElevenLabs provider ⇒ ``v3_audio_tags=False`` even if the model looks v3.

    The gate requires BOTH provider == ``elevenlabs`` AND a v3 model id, so a
    self-host provider can never trip the ElevenLabs-only audio-tag prompt.
    """
    gate = await _spotlight_kwarg_for_media_env(
        {
            "HERMES_VOIP_TTS_PROVIDER": "sherpa-kokoro",
            "HERMES_VOIP_TTS_MODEL": "eleven_v3",
        }
    )
    assert gate is False


def test_audio_tag_gate_and_tts_seam_strip_agree_via_model_supports_audio_tags() -> (
    None
):
    """The adapter gate and the TTS-seam strip both route through one predicate.

    For representative model ids, ``model_supports_audio_tags`` decides BOTH (a)
    whether the adapter prompts for tags and (b) whether the TTS seam PRESERVES
    them (so a non-v3 id strips a stray ``[laughs]`` while a v3 id keeps it). This
    locks the two sides to a single source of truth — they can never disagree.
    """
    from hermes_voip.spoken_text import strip_audio_tags  # noqa: PLC0415
    from hermes_voip.tts.elevenlabs import (  # noqa: PLC0415
        model_supports_audio_tags,
    )

    sample = "Sure [laughs] right away."
    for model_id, expected in (
        ("eleven_v3", True),
        ("eleven_v3_preview", True),
        ("eleven_flash_v2_5", False),
        ("eleven_multilingual_v2", False),
        ("eleven_turbo_v2_5", False),
    ):
        supports = model_supports_audio_tags(model_id)
        assert supports is expected, model_id
        # The seam strips iff the model does NOT support tags; the kept/dropped
        # state of a stray tag must match the same predicate.
        seam_keeps_tag = "[laughs]" in (
            sample if supports else strip_audio_tags(sample)
        )
        assert seam_keeps_tag is expected, model_id


def test_stray_audio_tag_stripped_on_non_v3_preserved_on_v3() -> None:
    """Regression: a stray ``[laughs]`` is dropped on a non-v3 model, kept on v3.

    Guards both ``strip_audio_tags`` (the whole ``[tag]`` token is removed, brackets
    and word, with whitespace collapsed) and that a sentence around the tag is not
    mangled — the words on either side survive intact.
    """
    from hermes_voip.spoken_text import strip_audio_tags  # noqa: PLC0415

    text = "Of course [laughs] I can help with that."

    # Non-v3: the tag (brackets + word) is gone, the surrounding prose is intact,
    # and no double space is left where the tag stood.
    stripped = strip_audio_tags(text)
    assert "[laughs]" not in stripped
    assert "laughs" not in stripped
    assert "Of course" in stripped
    assert "I can help with that." in stripped
    assert "  " not in stripped

    # v3: the call loop passes the sentence through UNSTRIPPED (preserve path), so
    # the tag reaches the synthesiser verbatim. Model that path by NOT stripping.
    assert "[laughs]" in text


# ---------------------------------------------------------------------------
# Call-termination → Hermes session signal (ADR-0026). _teardown_call is the
# single chokepoint reached from every call-end path; it must signal the Hermes
# session EXACTLY ONCE per call with the right text: a FAILURE end injects /stop
# (a hard stop), a NORMAL end injects the replayed disconnected note. The signal
# is an internal=True MessageEvent (bypasses auth) via the real handle_message.
# ---------------------------------------------------------------------------


async def _captured_handler_adapter() -> tuple[VoipAdapter, list[object]]:
    """Build an adapter wired to a capturing message handler (real base path)."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    captured: list[object] = []

    async def _handler(event: object) -> None:
        captured.append(event)

    adapter.set_message_handler(_handler)
    return adapter, captured


async def _teardown(adapter: VoipAdapter, call_id: str, reason: CallEndReason) -> None:
    """Drive ``_teardown_call`` with a no-op engine + the adapter's fake transport.

    Keeps each test's call explicit (no ``**dict`` splat that would erase the
    keyword types under ``mypy --strict``).
    """
    transport = adapter._transport
    assert transport is not None
    engine = MagicMock(stop=AsyncMock(return_value=None))
    await adapter._teardown_call(
        call_id=call_id,
        engine=engine,
        transport=transport,
        dialog_id=(call_id, "lt", "rt"),
        session=None,
        reason=reason,
    )


@pytest.mark.asyncio
async def test_teardown_failure_injects_stop_command() -> None:
    """A FAILURE end (e.g. pipeline failure) injects ``/stop`` exactly once."""
    adapter, captured = await _captured_handler_adapter()
    call_id = new_call_id()

    await _teardown(adapter, call_id, CallEndReason.PIPELINE_FAILURE)
    await _until(lambda: len(captured) >= 1, timeout=3.0)

    assert len(captured) == 1, "the session must be signalled exactly once"
    event = captured[0]
    assert getattr(event, "text", None) == "/stop"
    # internal=True bypasses user auth so the adapter-injected event is accepted.
    assert getattr(event, "internal", False) is True


@pytest.mark.asyncio
async def test_teardown_normal_injects_the_replayed_note_not_a_command() -> None:
    """A NORMAL end (remote BYE) injects the disconnected content note, not /stop."""
    from hermes_voip.call_end import NORMAL_END_NOTE  # noqa: PLC0415

    adapter, captured = await _captured_handler_adapter()
    call_id = new_call_id()

    await _teardown(adapter, call_id, CallEndReason.REMOTE_BYE)
    await _until(lambda: len(captured) >= 1, timeout=3.0)

    assert len(captured) == 1
    event = captured[0]
    text = getattr(event, "text", "")
    assert text == NORMAL_END_NOTE
    assert not text.startswith("/"), "a normal end must NOT inject a slash command"
    assert getattr(event, "internal", False) is True


@pytest.mark.asyncio
async def test_teardown_agent_hangup_is_a_soft_normal_end() -> None:
    """An AGENT_HANGUP end is SOFT: it injects the replayed note, never /stop."""
    from hermes_voip.call_end import NORMAL_END_NOTE  # noqa: PLC0415

    adapter, captured = await _captured_handler_adapter()
    call_id = new_call_id()

    await _teardown(adapter, call_id, CallEndReason.AGENT_HANGUP)
    await _until(lambda: len(captured) >= 1, timeout=3.0)

    assert len(captured) == 1
    assert getattr(captured[0], "text", "") == NORMAL_END_NOTE


@pytest.mark.asyncio
async def test_teardown_signals_exactly_once_even_if_called_twice() -> None:
    """Teardown is idempotent on the signal: a double teardown signals only once.

    The same-Call-ID identity guard (``session``/``_call_sessions``) must also
    gate the call-end signal, so a retried/duplicate teardown never re-injects.
    """
    adapter, captured = await _captured_handler_adapter()
    call_id = new_call_id()

    await _teardown(adapter, call_id, CallEndReason.REMOTE_BYE)
    await _teardown(adapter, call_id, CallEndReason.REMOTE_BYE)
    await _until(lambda: len(captured) >= 1, timeout=3.0)
    # Give any erroneous second signal a chance to land before asserting.
    await asyncio.sleep(0.05)

    assert len(captured) == 1, "a duplicate teardown must not re-signal the session"


@pytest.mark.asyncio
async def test_send_after_the_call_has_ended_is_a_clean_no_op(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Post-hangup TTS is a CLEAN no-op: dropped, reported success, logged quietly.

    Once the chokepoint has signalled call-end (info["ended"]=True), a late agent
    reply (e.g. the replayed-note turn the agent answers) has NO media path — it
    must NOT be synthesised to the now-disconnected caller and must NOT touch the
    (stopped) call loop. The drop is EXPECTED, so send() returns a *successful*
    result (the gateway's ``_send_with_retry`` returns immediately on success and
    never logs a delivery failure / retries / plain-text fallback) and the drop is
    recorded at DEBUG/INFO only — never WARNING/ERROR.

    This is the bug fix for the live noise (2026-06-19): the prior contract
    returned ``success=False`` for this expected case, which drove the gateway to
    emit ``WARNING [Voip] Send failed … trying plain-text fallback`` and then
    ``ERROR [Voip] Fallback send also failed`` for a harmless late reply.
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()

    spoke = False

    async def _speak(_chunks: object) -> None:
        nonlocal spoke
        spoke = True

    # A live loop is registered, but the call is flagged ended (post-teardown).
    adapter._call_loops[call_id] = MagicMock(speak=_speak)
    adapter._call_info[call_id] = {"name": "9999", "type": "dm", "ended": True}

    with caplog.at_level(logging.DEBUG, logger="hermes_voip.adapter"):
        result = await adapter.send(call_id, "a late reply")

    assert spoke is False, "an ended call must not synthesise TTS to the caller"
    # Expected late reply → clean success so the gateway drops it quietly.
    assert getattr(result, "success", False) is True
    # And NOT a noisy failure: no WARNING/ERROR record for the expected drop.
    noisy = [
        r
        for r in caplog.records
        if r.name == "hermes_voip.adapter" and r.levelno >= logging.WARNING
    ]
    assert noisy == [], f"post-hangup drop must be quiet, got: {noisy!r}"


@pytest.mark.asyncio
async def test_send_mid_call_speak_failure_still_surfaces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A GENUINE mid-call send failure still surfaces (rule 37 — not swallowed).

    Only the *expected* post-hangup case is downgraded to a clean no-op. If the
    call is LIVE (not flagged ended) and ``CallLoop.speak`` raises — a real
    TTS/transport fault while the caller is still connected — ``send()`` must
    return a FAILURE result and log a WARNING, exactly as before. The ended-call
    fix must not blanket-swallow real errors.
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()

    async def _boom(_chunks: object) -> None:
        raise RuntimeError("rtp write failed mid-call")

    # Call is LIVE (ended is False); speak() raises a genuine fault.
    adapter._call_loops[call_id] = MagicMock(speak=_boom)
    adapter._call_info[call_id] = {"name": "9999", "type": "dm", "ended": False}

    with caplog.at_level(logging.WARNING, logger="hermes_voip.adapter"):
        result = await adapter.send(call_id, "a live reply")

    assert getattr(result, "success", True) is False
    assert "rtp write failed mid-call" in (getattr(result, "error", "") or "")
    warned = [
        r
        for r in caplog.records
        if r.name == "hermes_voip.adapter" and r.levelno >= logging.WARNING
    ]
    assert warned, "a genuine mid-call send failure must still be logged loudly"


@pytest.mark.asyncio
async def test_send_after_real_teardown_is_quiet_not_unknown_call_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End-to-end: a late send after the REAL teardown chokepoint is a clean no-op.

    Locks the teardown ORDERING invariant the clean-no-op fix depends on: the
    chokepoint sets ``_call_info[id]["ended"]=True`` (and KEEPS the ``_call_info``
    entry) while popping ``_call_loops``. So a reply that arrives after the real
    ``_teardown_call`` hits the ``ended`` branch (clean success, DEBUG) — NOT the
    ``unknown call_id`` failure branch, which would re-introduce the gateway's
    WARNING/ERROR fallback noise. If a future teardown ever dropped the
    ``_call_info`` entry, this test fails (it would become an unknown-call_id
    failure), catching the regression at its source.
    """
    adapter, _captured = await _captured_handler_adapter()
    call_id = new_call_id()

    spoke = False

    async def _speak(_chunks: object) -> None:
        nonlocal spoke
        spoke = True

    # Register a live call exactly as the inbound path would, then tear it down
    # through the real chokepoint (a NORMAL remote-BYE end).
    adapter._call_loops[call_id] = MagicMock(speak=_speak)
    adapter._call_info[call_id] = {"name": "9999", "type": "dm", "ended": False}

    await _teardown(adapter, call_id, CallEndReason.REMOTE_BYE)

    # The loop is gone but the call_info entry survives, flagged ended.
    assert call_id not in adapter._call_loops
    assert adapter._call_info.get(call_id, {}).get("ended") is True

    with caplog.at_level(logging.DEBUG, logger="hermes_voip.adapter"):
        result = await adapter.send(call_id, "a late reply after teardown")

    assert spoke is False, "no media path after teardown — must not synthesise"
    assert getattr(result, "success", False) is True, (
        "a late send after real teardown must be a clean success, not a failure"
    )
    error = getattr(result, "error", None)
    assert error is None or "unknown call_id" not in error, (
        "must hit the ended branch, never the unknown-call_id failure branch"
    )
    noisy = [
        r
        for r in caplog.records
        if r.name == "hermes_voip.adapter" and r.levelno >= logging.WARNING
    ]
    assert noisy == [], f"post-teardown drop must be quiet, got: {noisy!r}"


@pytest.mark.asyncio
async def test_run_call_loop_clean_return_then_media_timeout_classifies_timeout() -> (
    None
):
    """A clean loop return with the engine timed-out classifies as MEDIA_TIMEOUT.

    When ``run()`` returns but ``engine.media_timed_out`` is True (the RTP
    watchdog or a transport loss ended the call), the end is a FAILURE
    (MEDIA_TIMEOUT) → ``/stop``, NOT a clean REMOTE_BYE. This is the wiring that
    turns the silent-drop reliability fix into the right Hermes signal.
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()

    engine = MagicMock(media_timed_out=True)
    reason = adapter._classify_end_reason(call_id, engine, raised=False)
    assert reason is CallEndReason.MEDIA_TIMEOUT
    assert reason.was_failure is True


@pytest.mark.asyncio
async def test_classify_end_reason_clean_return_is_remote_bye() -> None:
    """A clean return with media still healthy is REMOTE_BYE (a normal end)."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()

    engine = MagicMock(media_timed_out=False)
    reason = adapter._classify_end_reason(call_id, engine, raised=False)
    assert reason is CallEndReason.REMOTE_BYE


@pytest.mark.asyncio
async def test_classify_end_reason_agent_hangup_flag_wins_for_clean_return() -> None:
    """When the agent hung up (flag set) a clean return classifies AGENT_HANGUP."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()
    adapter._mark_agent_hangup(call_id)

    engine = MagicMock(media_timed_out=False)
    reason = adapter._classify_end_reason(call_id, engine, raised=False)
    assert reason is CallEndReason.AGENT_HANGUP


@pytest.mark.asyncio
async def test_classify_end_reason_exception_is_pipeline_failure() -> None:
    """A raised end (``raised=True``) is a PIPELINE_FAILURE → ``/stop`` (fail-safe)."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()

    engine = MagicMock(media_timed_out=False)
    reason = adapter._classify_end_reason(call_id, engine, raised=True)
    assert reason is CallEndReason.PIPELINE_FAILURE
    assert reason.was_failure is True


@pytest.mark.asyncio
async def test_hang_up_call_drives_session_bye_and_marks_agent_hangup() -> None:
    """The VoipToolHost hang_up_call drives the session BYE + marks AGENT_HANGUP.

    ADR-0026: the agent hang_up tool calls this. It must call the live session's
    ``hang_up`` (sends BYE, stops media) and flag the call agent-initiated so the
    subsequent teardown classifies AGENT_HANGUP (a SOFT, NORMAL end), not REMOTE_BYE.
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()

    hung_up = False

    class _Session:
        ended = False
        guard = GuardSessionState(call_id=call_id, privilege_level=0)

        async def hang_up(self) -> None:
            nonlocal hung_up
            hung_up = True

    adapter._call_sessions[call_id] = _Session()  # type: ignore[assignment]  # test double

    ended = await adapter.hang_up_call(call_id)

    assert ended is True
    assert hung_up is True
    # The call is now flagged agent-initiated, so a clean end classifies AGENT_HANGUP.
    engine = MagicMock(media_timed_out=False)
    assert (
        adapter._classify_end_reason(call_id, engine, raised=False)
        is CallEndReason.AGENT_HANGUP
    )


@pytest.mark.asyncio
async def test_hang_up_call_unknown_call_returns_false() -> None:
    """Hanging up an unknown call id is a no-op returning False (clear tool result)."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    assert await adapter.hang_up_call("no-such-call") is False


@pytest.mark.asyncio
async def test_guard_state_for_returns_the_call_guard() -> None:
    """guard_state_for exposes the per-call guard state for the pre_tool_call gate."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()
    guard = GuardSessionState(call_id=call_id, privilege_level=2)

    class _Session:
        ended = False

        def __init__(self, g: GuardSessionState) -> None:
            self.guard = g

    adapter._call_sessions[call_id] = _Session(guard)  # type: ignore[assignment]  # test double

    assert adapter.guard_state_for(call_id) is guard
    assert adapter.guard_state_for("unknown") is None


@pytest.mark.asyncio
async def test_hold_call_drives_session_hold() -> None:
    """The VoipToolHost hold_call drives the live session's hold (re-INVITE sendonly).

    ADR-0011: the agent hold_call tool calls this. It must call the live session's
    ``hold`` and return True; an unknown/ended call returns False (clear tool result).
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()

    held = False

    class _Session:
        ended = False
        guard = GuardSessionState(call_id=call_id, privilege_level=2)

        async def hold(self) -> None:
            nonlocal held
            held = True

    adapter._call_sessions[call_id] = _Session()  # type: ignore[assignment]  # test double

    assert await adapter.hold_call(call_id) is True
    assert held is True
    # Unknown call → no-op False.
    assert await adapter.hold_call("no-such-call") is False


@pytest.mark.asyncio
async def test_resume_call_drives_session_unhold() -> None:
    """The VoipToolHost resume_call drives the session's unhold (re-INVITE sendrecv)."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()

    resumed = False

    class _Session:
        ended = False
        guard = GuardSessionState(call_id=call_id, privilege_level=2)

        async def unhold(self) -> None:
            nonlocal resumed
            resumed = True

    adapter._call_sessions[call_id] = _Session()  # type: ignore[assignment]  # test double

    assert await adapter.resume_call(call_id) is True
    assert resumed is True
    assert await adapter.resume_call("no-such-call") is False


@pytest.mark.asyncio
async def test_hold_call_on_ended_session_returns_false() -> None:
    """hold_call on an already-ended call is a no-op returning False (clear result)."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    call_id = new_call_id()

    class _Session:
        ended = True
        guard = GuardSessionState(call_id=call_id, privilege_level=2)

        async def hold(self) -> None:  # pragma: no cover — must not be reached
            raise AssertionError("hold must not run on an ended call")

    adapter._call_sessions[call_id] = _Session()  # type: ignore[assignment]  # test double

    assert await adapter.hold_call(call_id) is False


@pytest.mark.asyncio
async def test_list_registrations_text_reports_the_manager_snapshot() -> None:
    """VoipToolHost list_registrations_text formats the manager snapshot (ADR-0011)."""
    transport = _FakeTransport()
    manager = _FakeManager(
        is_up=True,
        snapshot=(
            RegistrationStatus(extension="1000", index=0, registered=True, expires=60),
            RegistrationStatus(
                extension="1001", index=1, registered=False, expires=None
            ),
        ),
    )
    adapter = await _build_adapter(transport, manager)

    text = adapter.list_registrations_text()

    assert text == "1000: registered; 1001: down"


@pytest.mark.asyncio
async def test_connect_registers_adapter_as_the_active_voip_tool_host() -> None:
    """connect() registers the adapter as the active VoIP-tool adapter (ADR-0026).

    Without this seam the agent hang_up tool handler cannot reach the live call.
    """
    from hermes_voip.voip_tools import active_voip_adapter  # noqa: PLC0415

    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    assert active_voip_adapter() is adapter
    await adapter.disconnect()
    assert active_voip_adapter() is None


# ---------------------------------------------------------------------------
# (g) disconnect() is idempotent and drains manager + transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_idempotent() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    await adapter.disconnect()
    await adapter.disconnect()  # second call must not raise
    assert manager.closed


# ---------------------------------------------------------------------------
# (h) guard state is per-call (distinct GuardSessionState per Call-ID)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_call_gets_distinct_guard_state() -> None:
    """Two calls must not share GuardSessionState (one per Call-ID)."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)

    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                # RTCP activation (ADR-0061): the inbound plain-RTP path now starts
                # RTCP after connect(); the fake engine models the awaitable method +
                # the inert _rtcp_active flag so teardown's quality log is skipped.
                start_rtcp=AsyncMock(return_value=None),
                _rtcp_active=False,
                local_port=20002,
                inbound_sample_rate=8_000,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallSession",
            return_value=MagicMock(dialog_id=("cid", "lt", "rt"), ended=False),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch(
            "hermes_voip.adapter.GuardSessionState",
            # The adapter constructs GuardSessionState(call_id, privileged=...)
            # since ADR-0020; accept the kwarg so the call handler does not error.
            side_effect=lambda call_id, *, privileged=True: MagicMock(
                call_id=call_id, privileged=privileged
            ),
        ) as mock_guard_state,
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        for n in range(2):
            cid = f"call-id-{n}"
            req = SipRequest.parse(_make_invite(call_id=cid))
            adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=req))
            for _ in range(10):
                await asyncio.sleep(0)

    assert mock_guard_state.call_count == 2
    call_ids_used = {c.args[0] for c in mock_guard_state.call_args_list}
    assert "call-id-0" in call_ids_used
    assert "call-id-1" in call_ids_used


# ---------------------------------------------------------------------------
# (i) LEAK GUARD: a failure during post-200-OK setup must release the RTP
#     engine and the manager/transport call routes, and mark the call ended.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_invite_failure_after_200ok_releases_resources() -> None:
    """If CallLoop construction fails after the 200 OK, nothing must leak.

    The engine must be stopped, the manager + transport call routes removed,
    and the call marked ended — otherwise an accepted call leaks an open RTP
    socket and a dangling in-dialog route for the life of the process.
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)

    engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        start_rtcp=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=20002,
        inbound_sample_rate=8_000,
    )
    call_id = new_call_id()
    dialog_id = ("cid", "lt", "rt")

    with (
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
        patch(
            "hermes_voip.adapter.CallSession",
            return_value=MagicMock(dialog_id=dialog_id, ended=False),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        # CallLoop construction blows up AFTER the 200 OK + add_call wiring.
        patch(
            "hermes_voip.adapter.CallLoop",
            side_effect=RuntimeError("boom building loop"),
        ),
    ):
        req = SipRequest.parse(_make_invite(call_id=call_id))
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=req))
        for _ in range(20):
            await asyncio.sleep(0)

    # The RTP engine must have been stopped (no leaked socket).
    engine.stop.assert_awaited()
    # The in-dialog call routes must have been removed from both sides.
    assert dialog_id not in manager._calls
    assert call_id not in transport._calls
    # The call must be marked ended, and no live CallLoop left behind.
    assert call_id not in adapter._call_loops
    info = await adapter.get_chat_info(call_id)
    assert info.get("ended") is True


# ---------------------------------------------------------------------------
# Regression: connect() must connect the transport BEFORE building the manager.
#
# The live SipOverTlsTransport learns its local socket address only inside
# connect(); local_sent_by/contact_uri raise RuntimeError before then. The
# RegistrationManager __init__ reads transport.contact_uri() for every
# extension, so the adapter must await transport.connect() first. This test
# uses the REAL RegistrationManager + real load_gateway_config so the ordering
# is exercised against the genuine contract (only the transport, TLS, and
# providers are fakes). It fails (RuntimeError) on the buggy ordering and
# passes once connect() is reordered.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_brings_transport_up_before_building_manager() -> None:
    """connect() registers without raising; the strict transport proves ordering."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    transport = _ConnectOrderTransport()
    config = _platform_config(_FAKE_ENV | _FAKE_MEDIA_ENV)

    # Real RegistrationManager + real load_gateway_config: only fake the
    # transport (strict contract), the TLS context, and the provider build.
    with (
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
    ):
        adapter = VoipAdapter(config)
        # On the buggy ordering this raises RuntimeError("local_sent_by is
        # unavailable before connect()"); on the fixed ordering it returns.
        up = await adapter.connect()

    # The manager was built after the transport came up and sent its initial
    # REGISTER through the (now-connected) transport.
    assert any("REGISTER" in msg for msg in transport.sent), (
        "manager.connect() did not send a REGISTER — ordering/build is wrong"
    )
    # connect() returns the manager's degraded-up boolean (no real 200 OK here,
    # so it is False after the wait_for timeout — but it MUST NOT raise).
    assert up is False


# ---------------------------------------------------------------------------
# Regression (item 4): the production adapter MUST wire on_registration_error
# into the RegistrationManager. Without it, the manager's registration-recovery
# reporting (a rejected/timed-out refresh) has nowhere to surface — the failure
# is silently lost. The hook must log on the adapter logger and must NOT leak any
# HERMES_SIP_* value (rule 34: extension number / host / password are sensitive).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_wires_on_registration_error_into_the_manager(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_establish`` passes a non-None ``on_registration_error`` that logs."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    transport = _ConnectOrderTransport()
    config = _platform_config(_FAKE_ENV | _FAKE_MEDIA_ENV)

    captured: dict[str, object] = {}
    real_manager_cls = RegistrationManager

    def _capture_manager(*args: object, **kwargs: object) -> RegistrationManager:
        captured.update(kwargs)
        # Build the real manager so connect() proceeds normally.
        gateway = args[0]
        transport_arg = args[1]
        assert isinstance(gateway, GatewayConfig)
        return real_manager_cls(gateway, transport_arg, **kwargs)  # type: ignore[arg-type]  # test shim forwards the captured kwargs to the real ctor

    with (
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", side_effect=_capture_manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()

    hook = captured.get("on_registration_error")
    assert hook is not None, (
        "the adapter must pass on_registration_error so registration-failure "
        "recovery can be surfaced (item 4)"
    )
    assert callable(hook)
    # Invoking the hook logs the failure on the adapter logger (observed, not
    # swallowed) and never leaks the extension number or any secret.
    with caplog.at_level(logging.WARNING, logger="hermes_voip.adapter"):
        hook("1000", RuntimeError("registrar rejected refresh: 503"))
    records = [r for r in caplog.records if r.name == "hermes_voip.adapter"]
    assert records, "on_registration_error must log on the adapter logger"
    message = " ".join(r.getMessage() for r in records)
    # rule 34: the extension number is PII — it must not appear verbatim in the log.
    assert "1000" not in message, "registration-error log must not leak the extension"


# ---------------------------------------------------------------------------
# Regression (item 3, CANCEL teardown): the production adapter must wire
# on_cancel into the transport, and the handler must abort the half-built call
# (cancel its setup task) so a CANCELled INVITE leaks no media/CallLoop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_wires_on_cancel_and_it_aborts_the_call_task() -> None:
    """``_establish`` passes on_cancel; invoking it cancels the call's setup task."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    transport = _ConnectOrderTransport()
    config = _platform_config(_FAKE_ENV | _FAKE_MEDIA_ENV)

    captured: dict[str, object] = {}

    def _capture_transport(*_args: object, **kwargs: object) -> _ConnectOrderTransport:
        captured.update(kwargs)
        return transport

    with (
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch(
            "hermes_voip.adapter.SipOverTlsTransport", side_effect=_capture_transport
        ),
        patch("hermes_voip.adapter.RegistrationManager", return_value=_FakeManager()),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()

    on_cancel = captured.get("on_cancel")
    assert on_cancel is not None, (
        "the adapter must pass on_cancel so a CANCELled INVITE's setup is aborted"
    )
    assert callable(on_cancel)

    # A long-running fake setup task tracked under a Call-ID; on_cancel must cancel
    # it (the half-built media/CallLoop is torn down on the task's cancellation).
    call_id = "cancel-me-call"

    async def _never() -> None:
        await asyncio.Event().wait()  # blocks until cancelled

    task: asyncio.Task[None] = asyncio.ensure_future(_never())
    adapter._call_tasks.setdefault(call_id, set()).add(task)
    await asyncio.sleep(0)  # let the task start

    on_cancel(call_id)
    await asyncio.sleep(0)  # let the cancellation propagate
    assert task.cancelled() or task.cancelling() > 0, (
        "on_cancel must cancel the call's in-flight setup task"
    )
    # Drain the cancellation so the test leaves no pending task.
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Regression (live inbound-call failure, 2026-06-16): the 200 OK answering an
# inbound INVITE MUST carry our dialog To-tag, so the gateway's subsequent
# in-dialog ACK/BYE route back to the established CallSession instead of going
# out-of-dialog. The live gateway call showed the caller "answered immediately"
# but heard no audio, and the plugin logged the ACK and BYE as
# ``out-of-dialog ACK`` / ``out-of-dialog BYE`` — the dialog_id the adapter
# registered (local tag present) never matched the routed key (no tag on the
# wire). These tests drive the REAL _handle_inbound_invite with the REAL Dialog,
# CallSession, build_response, and RegistrationManager (only media/providers are
# fakes), so the on-the-wire 200 OK To-tag and the manager routing are exercised.
# ---------------------------------------------------------------------------


def _real_gateway_config() -> GatewayConfig:
    """A real GatewayConfig for ext 1000 (matches the fake transport's contact)."""
    return GatewayConfig(
        host="pbx.example.test",
        port=5061,
        transport="tls",
        expires=300,
        user_agent="hermes-voip/0",
        extensions=(
            ExtensionConfig(index=0, extension="1000", username="1000", password="x"),
        ),
        default_index=0,
    )


def _gateway_in_dialog_request(
    method: str, ok: SipResponse, *, call_id: str
) -> SipRequest:
    """A gateway in-dialog ACK/BYE: it echoes the dialog To/From from our 200 OK.

    Per RFC 3261 §12, the peer's in-dialog request carries OUR identity in ``To``
    (the 200 OK's ``To``, *with whatever tag we put there*) and the caller's
    identity in ``From``. We reproduce exactly that, so the test is faithful to
    what a real gateway sends after our answer.
    """
    to_value = ok.header("To") or ""
    from_value = ok.header("From") or ""
    raw = (
        f"{method} sip:1000@127.0.0.1:5061 SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 203.0.113.7:5061;branch=z9hG4bKdlg\r\n"
        "Max-Forwards: 70\r\n"
        f"From: {from_value}\r\n"
        f"To: {to_value}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 2 {method}\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    return SipRequest.parse(raw)


async def _build_adapter_with_real_manager(
    transport: _FakeTransport,
) -> tuple[VoipAdapter, RegistrationManager]:
    """A real VoipAdapter + a real RegistrationManager over the fake transport."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = _platform_config(_FAKE_ENV | _FAKE_MEDIA_ENV)
    manager = RegistrationManager(_real_gateway_config(), transport)
    with (
        patch(
            "hermes_voip.adapter.load_gateway_config",
            return_value=_real_gateway_config(),
        ),
        patch(
            "hermes_voip.adapter.load_media_config", return_value=load_media_config({})
        ),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()
    return adapter, manager


def _sent_200_ok(transport: _FakeTransport) -> SipResponse:
    """The single 2xx response the adapter sent (the INVITE answer)."""
    oks = [SipResponse.parse(m) for m in transport.sent if m.startswith("SIP/2.0 200")]
    assert oks, "the adapter did not send a 200 OK answer"
    return oks[-1]


@pytest.mark.asyncio
async def test_inbound_invite_ack_and_bye_route_in_dialog() -> None:
    """A call we answer must route its own ACK/BYE in-dialog (not out-of-dialog).

    Reproduces the live gateway failure: with no To-tag on the 200 OK, the
    gateway's ACK/BYE were ``Unroutable`` (``out-of-dialog``) and the call never
    established a routable dialog. The fix puts our dialog tag on the 200 OK's
    ``To`` so the manager routes the ACK/BYE to the CallSession.
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_real_manager(transport)

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(call_id=call_id))

    # Hold the call open: a real CallLoop.run() blocks for the call's lifetime, so
    # the in-dialog ACK/BYE arrive WHILE the call is active and registered (the
    # realistic ordering — and the live failure point). A run() that returned
    # immediately would tear the call down before the ACK/BYE, masking the bug.
    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    try:
        with (
            patch(
                "hermes_voip.adapter.RtpMediaTransport",
                return_value=MagicMock(
                    connect=AsyncMock(return_value=True),
                    stop=AsyncMock(return_value=None),
                    start_rtcp=AsyncMock(return_value=None),
                    _rtcp_active=False,
                    local_port=20002,
                    inbound_sample_rate=8_000,
                ),
            ),
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            # Real Dialog + real CallSession + real build_response — only media and
            # the loop body are faked, so the on-the-wire 200 OK and the registered
            # dialog_id are exactly what production produces.
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            # Let the handler run up to (and block at) call_loop.run().
            await _until(lambda: call_id in adapter._call_loops)

            ok = _sent_200_ok(transport)

            # The gateway now sends ACK + BYE in-dialog (echoing our 200 OK
            # To/From) while the call is live; both must route to the CallSession.
            for method in ("ACK", "BYE"):
                request = _gateway_in_dialog_request(method, ok, call_id=call_id)
                routing = manager.route_request(request)
                assert isinstance(routing, InDialog), (
                    f"{method} routed {type(routing).__name__} "
                    f"({getattr(routing, 'reason', '')!r}); To={request.header('To')!r}"
                )
    finally:
        in_call.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_inbound_invite_200ok_carries_dialog_to_tag() -> None:
    """The 200 OK answering an inbound INVITE carries our dialog To-tag.

    A dialog-forming 2xx without a To-tag is an RFC 3261 §12.1.1 violation and is
    the precise cause of the out-of-dialog ACK/BYE. The tag on the wire must be
    the same local tag the registered CallSession's dialog_id uses.
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_real_manager(transport)

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(call_id=call_id))

    captured: dict[str, tuple[str, str, str]] = {}
    real_add_call = manager.add_call

    def _spy_add_call(dialog_id: tuple[str, str, str], consumer: object) -> None:
        captured["dialog_id"] = dialog_id
        # The spy takes object (it only records the key); the value forwarded is
        # the real CallSession the adapter passed, which IS a DialogConsumer.
        real_add_call(dialog_id, consumer)  # type: ignore[arg-type]

    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                # RTCP activation (ADR-0061): the inbound plain-RTP path now starts
                # RTCP after connect(); the fake engine models the awaitable method +
                # the inert _rtcp_active flag so teardown's quality log is skipped.
                start_rtcp=AsyncMock(return_value=None),
                _rtcp_active=False,
                local_port=20002,
                inbound_sample_rate=8_000,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        patch.object(manager, "add_call", side_effect=_spy_add_call),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        for _ in range(20):
            await asyncio.sleep(0)

    ok = _sent_200_ok(transport)
    to_value = ok.header("To") or ""
    # The To header must carry a dialog tag (after the name-addr's '>').
    after_angle = to_value.split(">", 1)[1] if ">" in to_value else to_value
    assert ";tag=" in after_angle.lower(), (
        f"200 OK To header has no dialog tag: {to_value!r}"
    )

    # And that tag must equal the local tag of the registered dialog_id.
    dialog_id = captured["dialog_id"]
    local_tag = dialog_id[1]
    assert f"tag={local_tag}" in after_angle, (
        f"200 OK To-tag does not match registered dialog local tag {local_tag!r}: "
        f"{to_value!r}"
    )


# ---------------------------------------------------------------------------
# Regression (live no-audio failure): the SDP answer must advertise the runtime's
# REAL local RTP address (the transport's local interface), never the 127.0.0.1
# loopback placeholder. A gateway that receives c=IN IP4 127.0.0.1 sends RTP to
# its own loopback, so audio can never flow even once the dialog is routable.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_invite_sdp_answer_advertises_real_local_rtp_address() -> None:
    """The 200 OK SDP answer connection address is the transport's local host."""
    transport = _FakeTransport(local_sent_by="172.23.0.2:55728")
    adapter, _manager = await _build_adapter_with_real_manager(transport)

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(call_id=call_id))

    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                # RTCP activation (ADR-0061): the inbound plain-RTP path now starts
                # RTCP after connect(); the fake engine models the awaitable method +
                # the inert _rtcp_active flag so teardown's quality log is skipped.
                start_rtcp=AsyncMock(return_value=None),
                _rtcp_active=False,
                local_port=20002,
                inbound_sample_rate=8_000,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        for _ in range(20):
            await asyncio.sleep(0)

    ok = _sent_200_ok(transport)
    # The SDP answer (200 OK body) must carry the runtime's real RTP host, not
    # the loopback placeholder.
    assert "c=IN IP4 172.23.0.2" in ok.body, (
        f"SDP answer does not advertise the local interface address; body:\n{ok.body}"
    )
    assert "127.0.0.1" not in ok.body, (
        f"SDP answer still advertises the 127.0.0.1 loopback placeholder:\n{ok.body}"
    )


# ---------------------------------------------------------------------------
# ADR-0053 Stage 1: an inbound RTP/SAVP (SDES) offer is answered with SRTP — our
# OWN answer key in a=crypto — not rejected with 488. The fake SDES key is
# computed at runtime (sequential bytes 0..29) so no high-entropy key literal
# appears in this file (the gitleaks allowlist is path-scoped to test_sdp.py).
# ---------------------------------------------------------------------------

# The offerer's fake SDES master key||salt (30 octets for AES_CM_128_HMAC_SHA1_80),
# built at runtime so it is not a source literal the secret-scanner would flag.
_OFFER_SDES_KEY = base64.b64encode(bytes(range(30))).decode("ascii")
_FAKE_SDP_OFFER_SAVP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 20000 RTP/SAVP 0 8\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_OFFER_SDES_KEY}\r\n"
    "a=sendrecv\r\n"
)


def _make_invite_savp(call_id: str) -> str:
    content_length = len(_FAKE_SDP_OFFER_SAVP.encode("utf-8"))
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bK{new_tag()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:caller@pbx.example.test>;tag={new_tag()}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:caller@127.0.0.1:5061;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{_FAKE_SDP_OFFER_SAVP}"
    )


@pytest.mark.asyncio
async def test_inbound_savp_offer_answered_with_sdes_srtp() -> None:
    """ADR-0053 Stage 1: an RTP/SAVP (SDES) offer is answered with SRTP, not 488.

    The answer must be RTP/SAVP carrying an ``a=crypto`` with OUR OWN key (RFC 4568
    §6.1 — each direction uses the sender's key), distinct from the offerer's key.
    Today the adapter omits the answer key, so this offer is rejected with 488 and
    no 200 OK is sent (the dormant-SDES wiring gap this stage closes).
    """
    transport = _FakeTransport(local_sent_by="172.23.0.2:55728")
    adapter, _manager = await _build_adapter_with_real_manager(transport)

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite_savp(call_id))

    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                # RTCP activation (ADR-0061): the inbound plain-RTP path now starts
                # RTCP after connect(); the fake engine models the awaitable method +
                # the inert _rtcp_active flag so teardown's quality log is skipped.
                start_rtcp=AsyncMock(return_value=None),
                _rtcp_active=False,
                local_port=20002,
                inbound_sample_rate=8_000,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        for _ in range(20):
            await asyncio.sleep(0)

    ok = _sent_200_ok(transport)
    # The answer is a secured RTP/SAVP stream (both offered codecs echoed) with
    # exactly one a=crypto line.
    assert "m=audio 20002 RTP/SAVP 0 8\r\n" in ok.body, (
        f"SDP answer is not RTP/SAVP (SDES); body:\n{ok.body}"
    )
    assert ok.body.count("a=crypto:") == 1, f"expected one a=crypto; body:\n{ok.body}"
    assert "AES_CM_128_HMAC_SHA1_80 inline:" in ok.body
    # Our answer key must be OUR OWN, never an echo of the offerer's key.
    assert f"inline:{_OFFER_SDES_KEY}" not in ok.body, (
        "answer echoes the offerer's SDES key instead of generating our own"
    )


# ---------------------------------------------------------------------------
# Regression (W10 review finding): an exception raised inside the fire-and-forget
# inbound-INVITE handler must be LOGGED WITH ITS TRACEBACK, never silently lost.
# On the live call there was zero log output from the handler — any failure (SDP
# parse, media setup, anything) was invisible. The done-callback must record the
# exception with full traceback (``exc_info``) so the next live call is
# diagnosable, not just ``str(exc)`` with no stack.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_handler_exception_is_logged_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure in the inbound handler is logged with its traceback, not swallowed."""
    import logging  # noqa: PLC0415

    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(call_id=call_id))

    boom = RuntimeError("media setup exploded")

    # Force a failure at an UNGUARDED point: the RTP engine's connect() (no local
    # try/except wraps it), so the exception escapes the handler body into the
    # fire-and-forget task. The safety net must log it WITH its traceback, not
    # lose it (the live call showed zero handler output on failure).
    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(side_effect=boom),
                stop=AsyncMock(return_value=None),
                # connect() raises, so start_rtcp is never reached; _rtcp_active is
                # inert so the teardown quality log is skipped (ADR-0061).
                _rtcp_active=False,
                local_port=20002,
                inbound_sample_rate=8_000,
            ),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        caplog.at_level(logging.ERROR, logger="hermes_voip.adapter"),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        for _ in range(20):
            await asyncio.sleep(0)

    # exc_info is (type, value, tb) | None; collect the values from ERROR records
    # that carry one — a record with exc_info proves the traceback was logged.
    logged_exceptions = [
        record.exc_info[1]
        for record in caplog.records
        if record.levelno >= logging.ERROR and record.exc_info is not None
    ]
    assert logged_exceptions, (
        "the inbound handler swallowed an exception (no ERROR log with exc_info)"
    )
    # The logged record must carry the actual exception (so the traceback is real).
    assert boom in logged_exceptions, (
        "the logged ERROR did not carry the handler's exception traceback"
    )


# ---------------------------------------------------------------------------
# LIVE BUG 1 — VAD + endpointer must be built at the engine's INBOUND sample
# rate (8 kHz G.711), not the silero default (16 kHz). The pump feeds the
# engine's 8 kHz frames straight into the VAD; a 16 kHz detector raises
# ``ValueError: frame rate 8000 != detector rate 16000`` on the very first frame,
# fails the CallLoop TaskGroup, and the caller hears silence. silero-vad runs
# natively at 8 kHz, so the inbound chain stays at the wire rate.
# ---------------------------------------------------------------------------


def _media_cfg_8k() -> MediaConfig:
    """A real MediaConfig with defaults (the rate is supplied separately)."""
    return load_media_config({})


def test_make_endpointer_built_at_engine_inbound_rate() -> None:
    """``_make_endpointer`` must build the endpointer at the supplied 8 kHz rate.

    Before the fix the endpointer was hard-built at silero's 16 kHz default, so
    its window clock (and silence-window count) did not match the 8 kHz VAD the
    pump actually drives. The endpointer must take the engine's inbound rate.
    """
    from hermes_voip.adapter import _make_endpointer  # noqa: PLC0415

    endpointer = _make_endpointer(_media_cfg_8k(), sample_rate_hz=8_000)

    # A 256-sample (32 ms) window at 8 kHz gives 16 windows per 500 ms — the same
    # 32 ms window duration as 16 kHz, so the silence-window count is rate-correct.
    assert endpointer.silence_windows == 16


def test_make_vad_built_at_engine_inbound_rate() -> None:
    """``_make_vad`` must load the model and build the VAD at the supplied rate.

    The silero model factory and the detector both take a sample rate; the
    adapter must pass the engine's 8 kHz inbound rate to BOTH so the detector
    accepts the engine's 8 kHz frames (256-sample windows) instead of rejecting
    them against a 16 kHz (512-sample) expectation.
    """
    from hermes_voip import adapter as adapter_mod  # noqa: PLC0415

    captured: dict[str, object] = {}

    def _fake_load(sample_rate_hz: int = 16_000, **_kw: object) -> object:
        captured["model_rate"] = sample_rate_hz

        def _model(window_pcm16: bytes, sample_rate: int) -> float:
            _ = window_pcm16, sample_rate
            return 0.0

        return _model

    with patch.object(adapter_mod, "load_silero_model", _fake_load):
        vad = adapter_mod._make_vad(_media_cfg_8k(), sample_rate_hz=8_000)

    # The model factory got the engine rate ...
    assert captured["model_rate"] == 8_000
    # ... and the detector accepts an 8 kHz frame without raising (proves the
    # detector itself was constructed at 8 kHz, i.e. one window == 256 samples).
    frame_8k = PcmFrame(samples=bytes(512), sample_rate=8_000, monotonic_ts_ns=0)
    events = list(vad.feed(frame_8k))  # no ValueError
    assert events == []


@pytest.mark.asyncio
async def test_run_call_loop_builds_detectors_at_engine_inbound_rate() -> None:
    """``_run_call_loop`` must build VAD + endpointer at ``engine.inbound_sample_rate``.

    This is the live wiring: the engine reports 8 kHz, so the detectors handed to
    the CallLoop must be 8 kHz. We capture the ``sample_rate_hz`` the adapter
    passes to ``_make_vad`` / ``_make_endpointer`` and assert it is the engine's
    inbound rate, not the silero 16 kHz default.
    """
    from hermes_voip import adapter as adapter_mod  # noqa: PLC0415

    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    adapter._media_cfg = load_media_config({})
    adapter._providers = MagicMock(asr=MagicMock(), tts=MagicMock(), guard=MagicMock())

    engine = MagicMock()
    engine.inbound_sample_rate = 8_000

    vad_rates: list[int] = []
    endpointer_rates: list[int] = []

    def _capture_vad(_cfg: MediaConfig, *, sample_rate_hz: int) -> object:
        vad_rates.append(sample_rate_hz)
        return MagicMock()

    def _capture_endpointer(_cfg: MediaConfig, *, sample_rate_hz: int) -> object:
        endpointer_rates.append(sample_rate_hz)
        return MagicMock()

    with (
        patch.object(adapter_mod, "_make_vad", _capture_vad),
        patch.object(adapter_mod, "_make_endpointer", _capture_endpointer),
        patch.object(
            adapter_mod,
            "CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
    ):
        await adapter._run_call_loop(
            call_id="call-rate-1",
            engine=engine,
            guard_state=MagicMock(),
        )

    assert vad_rates == [8_000]
    assert endpointer_rates == [8_000]


# ---------------------------------------------------------------------------
# Operator invariant: before answering, verify we support a codec+rate pair; if
# not, DON'T answer — reject with 488 and log a CLEAR error, never fail silently.
# An offer with NO supported voice codec (G.729 / PT 18 only) must be rejected
# with 488 Not Acceptable Here, MUST NOT receive a 200 OK, and MUST be logged
# loudly (ERROR), so a rejected call is never buried at WARNING.
# ---------------------------------------------------------------------------

# A G.729-only offer: the engine carries G.711 only, so this shares NO voice
# codec with us. ``telephone-event`` (DTMF) is included to prove a DTMF-only
# match is still not a usable call. Fakes only — synthetic PT numbers, no PII.
_FAKE_SDP_OFFER_NO_COMMON_CODEC = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 20000 RTP/AVP 18 101\r\n"
    "a=rtpmap:18 G729/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=fmtp:101 0-16\r\n"
    "a=sendrecv\r\n"
)


def _make_invite_no_common_codec(call_id: str) -> str:
    body = _FAKE_SDP_OFFER_NO_COMMON_CODEC
    content_length = len(body.encode("utf-8"))
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKfake\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:9999@pbx.example.test>;tag={new_tag()}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:9999@127.0.0.1:60000;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{body}"
    )


def _responses_with_status(transport: _FakeTransport, prefix: str) -> list[str]:
    """All SIP responses the adapter sent whose start-line begins with ``prefix``."""
    return [m for m in transport.sent if m.startswith(prefix)]


@pytest.mark.asyncio
async def test_inbound_invite_no_common_codec_rejects_488_no_200ok(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No common voice codec => 488 sent, NO 200 OK, and a CLEAR error logged.

    The operator invariant: we must not answer a call whose codec we cannot
    carry. An offer for G.729 only (the engine is G.711-only) shares no voice
    codec, so the handler must send ``488 Not Acceptable Here``, must NOT send a
    ``200 OK`` answer, and must surface the rejection at ERROR (not bury it at
    WARNING) so a refused call is unmistakable in the logs.
    """
    import logging  # noqa: PLC0415

    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport)

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite_no_common_codec(call_id))

    # If negotiation were (wrongly) to proceed, these media collaborators would be
    # exercised; they are faked so a leak past the reject is observable as a 200 OK.
    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                # RTCP activation (ADR-0061): the inbound plain-RTP path now starts
                # RTCP after connect(); the fake engine models the awaitable method +
                # the inert _rtcp_active flag so teardown's quality log is skipped.
                start_rtcp=AsyncMock(return_value=None),
                _rtcp_active=False,
                local_port=20002,
                inbound_sample_rate=8_000,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        caplog.at_level(logging.ERROR, logger="hermes_voip.adapter"),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        for _ in range(20):
            await asyncio.sleep(0)

    # A 488 Not Acceptable Here was sent ...
    rejections = _responses_with_status(transport, "SIP/2.0 488")
    assert rejections, (
        "the adapter did not reject the no-common-codec INVITE with 488; "
        f"sent: {transport.sent!r}"
    )
    # ... and crucially NO 200 OK answer (we must never answer a call we cannot
    # carry).
    answers = _responses_with_status(transport, "SIP/2.0 200")
    assert not answers, (
        "the adapter answered (200 OK) a call whose codec it cannot carry; "
        f"sent: {transport.sent!r}"
    )

    # The rejection is logged LOUDLY (ERROR), not buried at WARNING.
    error_records = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and record.name == "hermes_voip.adapter"
        and call_id in record.getMessage()
    ]
    assert error_records, (
        "the no-common-codec rejection was not logged at ERROR for this call_id; "
        f"records: {[(r.levelname, r.getMessage()) for r in caplog.records]!r}"
    )


@pytest.mark.asyncio
async def test_inbound_invite_pcmu_pcma_offer_still_answers_200ok() -> None:
    """Happy-path regression: a normal PCMU/PCMA offer still answers 200 OK.

    The capability guard must not break a carriable call: the standard G.711 offer
    (PCMU + PCMA) negotiates and is answered with a 200 OK exactly as before.
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport)

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(call_id=call_id))

    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                # RTCP activation (ADR-0061): the inbound plain-RTP path now starts
                # RTCP after connect(); the fake engine models the awaitable method +
                # the inert _rtcp_active flag so teardown's quality log is skipped.
                start_rtcp=AsyncMock(return_value=None),
                _rtcp_active=False,
                local_port=20002,
                inbound_sample_rate=8_000,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        for _ in range(20):
            await asyncio.sleep(0)

    ok = _sent_200_ok(transport)
    assert ok.status_code == 200
    # No 488 on the happy path.
    assert not _responses_with_status(transport, "SIP/2.0 488"), (
        f"a carriable PCMU/PCMA offer was wrongly rejected; sent: {transport.sent!r}"
    )


# ---------------------------------------------------------------------------
# _to_engine_codec: the adapter's SDP->engine bridge must delegate to the
# engine's exhaustive capability map — a negotiated voice codec the engine
# cannot carry RAISES (the old ``else -> PCMA`` silently mis-mapped it), and the
# advertised ``_SUPPORTED_ENCODINGS`` menu can never drift ahead of the engine.
# ---------------------------------------------------------------------------


def _sdp_codec(encoding: str, payload_type: int, clock_rate: int = 8000) -> SdpCodec:
    return SdpCodec(payload_type=payload_type, encoding=encoding, clock_rate=clock_rate)


def test_to_engine_codec_maps_pcmu_and_pcma() -> None:
    from hermes_voip.adapter import _to_engine_codec  # noqa: PLC0415
    from hermes_voip.media.engine import Codec as EngineCodec  # noqa: PLC0415

    assert _to_engine_codec(_sdp_codec("PCMU", 0)) is EngineCodec.PCMU
    assert _to_engine_codec(_sdp_codec("PCMA", 8)) is EngineCodec.PCMA


def test_to_engine_codec_unsupported_raises_not_silent_pcma() -> None:
    # THE BUG: the old ``else -> PCMA`` returned PCMA for ANY non-PCMU encoding,
    # so a G.729 offer would be answered as a PCMA call the engine cannot carry ->
    # dead/wrong audio. It must RAISE the capability error instead.
    from hermes_voip.adapter import _to_engine_codec  # noqa: PLC0415
    from hermes_voip.media.engine import UnsupportedCodecError  # noqa: PLC0415

    with pytest.raises(UnsupportedCodecError):
        _to_engine_codec(_sdp_codec("G729", 18))


def test_to_engine_codec_rejects_wrong_clock_rate() -> None:
    # CODEC AND RATE: a PCMU rtpmap at 16 kHz is not carriable by the 8 kHz engine.
    from hermes_voip.adapter import _to_engine_codec  # noqa: PLC0415
    from hermes_voip.media.engine import UnsupportedCodecError  # noqa: PLC0415

    with pytest.raises(UnsupportedCodecError):
        _to_engine_codec(_sdp_codec("PCMU", 0, clock_rate=16000))


def test_supported_voice_encodings_never_drift_ahead_of_engine() -> None:
    """DRIFT GUARD: every voice entry in ``_SUPPORTED_ENCODINGS`` is carriable.

    ``telephone-event`` (DTMF, RFC 4733) is excluded — it is named-event RTP, not
    routed through the engine's PCM encode/decode path. Every *voice* encoding the
    adapter advertises in its offer allow-list MUST map to a real engine ``Codec``
    without raising; otherwise the menu is advertising a capability the engine
    does not have. (This is what lets a future codec be added safely: add it to
    the engine first, then the menu.)
    """
    from hermes_voip.adapter import _SUPPORTED_ENCODINGS  # noqa: PLC0415
    from hermes_voip.media.engine import (  # noqa: PLC0415
        Codec as EngineCodec,
    )
    from hermes_voip.media.engine import (  # noqa: PLC0415
        codec_for_encoding,
    )

    voice = [e for e in _SUPPORTED_ENCODINGS if e.lower() != "telephone-event"]
    assert voice, "expected at least one voice encoding in the supported menu"
    for encoding in voice:
        result = codec_for_encoding(encoding, 8000)
        assert isinstance(result, EngineCodec), (
            f"supported menu encoding {encoding!r} does not map to a runnable "
            f"engine codec — the advertised menu drifted ahead of the engine"
        )


# ---------------------------------------------------------------------------
# G.722 wideband menu + offer order + negotiation (ADR-0022). The advertised menu
# now leads with G.722 (wideband), and an offered G.722 is answered with G.722;
# a G.711-only offer still answers G.711 (fallback).
# ---------------------------------------------------------------------------


def test_supported_menu_leads_with_g722_then_g711() -> None:
    from hermes_voip.adapter import _SUPPORTED_ENCODINGS  # noqa: PLC0415

    voice = [e for e in _SUPPORTED_ENCODINGS if e.lower() != "telephone-event"]
    # G.722 is advertised FIRST (wideband-preferred), then G.711 PCMU/PCMA.
    assert voice[0] == "G722"
    assert "PCMU" in voice
    assert "PCMA" in voice


def test_outbound_offer_lists_g722_first() -> None:
    # The outbound INVITE offer must put G.722 (PT 9) ahead of the G.711 payloads
    # so a wideband-capable peer picks it; G.711 stays as the fallback. (When Opus
    # is available it is promoted ahead of G.722 by the offer builder; this test
    # forces Opus UNavailable so the floor menu is asserted.)
    from unittest.mock import patch  # noqa: PLC0415

    from hermes_voip.adapter import _outbound_offer_codecs  # noqa: PLC0415

    with patch("hermes_voip.adapter._opus_sip_available", return_value=False):
        codecs = _outbound_offer_codecs()
    voice = [c for c in codecs if c.encoding.lower() != "telephone-event"]
    assert voice[0].encoding == "G722"
    assert voice[0].payload_type == 9
    assert {c.encoding for c in voice} >= {"G722", "PCMU", "PCMA"}
    assert not any(c.encoding.lower() == "opus" for c in voice)


def test_outbound_offer_includes_opus_when_available() -> None:
    # ADR-0049: Opus on the SIP path — when libopus is available, the outbound SIP
    # offer menu carries Opus (opus/48000) so a SIP call can negotiate it.
    from unittest.mock import patch  # noqa: PLC0415

    from hermes_voip.adapter import _outbound_offer_codecs  # noqa: PLC0415

    with patch("hermes_voip.adapter._opus_sip_available", return_value=True):
        codecs = _outbound_offer_codecs()
    opus = [c for c in codecs if c.encoding.lower() == "opus"]
    assert opus, "Opus must appear in the SIP offer menu when libopus is available"
    assert opus[0].clock_rate == 48000


def test_supported_menu_includes_opus_when_available() -> None:
    # ADR-0049: the inbound SIP answer's supported list also offers Opus when
    # libopus is available (so an inbound SIP call can negotiate Opus).
    from unittest.mock import patch  # noqa: PLC0415

    from hermes_voip.adapter import _sip_supported_encodings  # noqa: PLC0415

    with patch("hermes_voip.adapter._opus_sip_available", return_value=True):
        supported = _sip_supported_encodings()
    assert any(e.lower() == "opus" for e in supported)

    with patch("hermes_voip.adapter._opus_sip_available", return_value=False):
        supported_floor = _sip_supported_encodings()
    assert not any(e.lower() == "opus" for e in supported_floor)
    assert "G722" in supported_floor


_FAKE_SDP_OFFER_G722 = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 20000 RTP/AVP 9 0 8\r\n"
    "a=rtpmap:9 G722/8000\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=sendrecv\r\n"
)


def _make_invite_g722(call_id: str) -> str:
    content_length = len(_FAKE_SDP_OFFER_G722.encode("utf-8"))
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKg722\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:9999@pbx.example.test>;tag={new_tag()}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:9999@127.0.0.1:5061;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{_FAKE_SDP_OFFER_G722}"
    )


@pytest.mark.asyncio
async def test_inbound_invite_g722_offer_answers_200_with_g722() -> None:
    """A G.722-first offer is answered 200 OK with G.722 in the answer SDP.

    The engine is mocked (no socket), but the answer SDP is built from the REAL
    offer via build_audio_answer + the real _SUPPORTED_ENCODINGS, so this proves
    the wideband codec is negotiated and advertised back when offered.
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport)

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite_g722(call_id=call_id))

    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                start_rtcp=AsyncMock(return_value=None),
                _rtcp_active=False,
                local_port=20004,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        for _ in range(20):
            await asyncio.sleep(0)

    ok = _sent_200_ok(transport)
    assert ok.status_code == 200
    assert not _responses_with_status(transport, "SIP/2.0 488"), (
        f"a carriable G.722 offer was wrongly rejected; sent: {transport.sent!r}"
    )
    # The answer SDP advertises G.722 (PT 9) — the wideband codec was negotiated.
    assert "a=rtpmap:9 G722/8000" in ok.body, (
        f"answer SDP did not select G.722; body:\n{ok.body}"
    )


@pytest.mark.asyncio
async def test_inbound_invite_g722_engine_opened_with_g722_codec() -> None:
    """The RtpMediaTransport is constructed with the G.722 engine codec.

    Proves the negotiated G.722 actually reaches the media engine (codec=G722),
    not just the SDP answer — so the wire really carries G.722.
    """
    from hermes_voip.media.engine import Codec as EngineCodec  # noqa: PLC0415

    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport)

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite_g722(call_id=call_id))

    captured: dict[str, object] = {}

    def _capture_engine(**kwargs: object) -> MagicMock:
        captured.update(kwargs)
        return MagicMock(
            connect=AsyncMock(return_value=True),
            stop=AsyncMock(return_value=None),
            start_rtcp=AsyncMock(return_value=None),
            _rtcp_active=False,
            local_port=20006,
        )

    with (
        patch("hermes_voip.adapter.RtpMediaTransport", side_effect=_capture_engine),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        for _ in range(20):
            await asyncio.sleep(0)

    assert captured.get("codec") is EngineCodec.G722, (
        f"engine was not opened with the G.722 codec; got {captured.get('codec')!r}"
    )
    # The negotiated RTP payload type (static 9 here) is passed to the engine so
    # outbound packets + the comedia latch use the wire PT, not just the codec kind.
    assert captured.get("payload_type") == 9, (
        f"engine not opened with the negotiated PT; "
        f"got {captured.get('payload_type')!r}"
    )


_FAKE_SDP_OFFER_G722_DYNAMIC_PT = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 20000 RTP/AVP 109 0\r\n"
    "a=rtpmap:109 G722/8000\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=sendrecv\r\n"
)


@pytest.mark.asyncio
async def test_inbound_invite_g722_dynamic_pt_opens_engine_with_that_pt() -> None:
    """A G.722 offer at a DYNAMIC payload type opens the engine with THAT PT.

    Cross-vendor review finding: RFC 3551 reserves G.722's static PT 9, but
    gateways do offer it at a dynamic PT, and the answer mirrors the offer's PT. If
    the engine sent the static 9 while we advertised 109, the gateway would drop our
    media and the comedia latch would reject inbound PT-109 packets (no audio). The
    engine must therefore be opened with the negotiated PT 109, not 9.
    """
    from hermes_voip.media.engine import Codec as EngineCodec  # noqa: PLC0415

    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport)

    call_id = new_call_id()
    content_length = len(_FAKE_SDP_OFFER_G722_DYNAMIC_PT.encode("utf-8"))
    invite_raw = (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKg722dyn\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:9999@pbx.example.test>;tag={new_tag()}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:9999@127.0.0.1:5061;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{_FAKE_SDP_OFFER_G722_DYNAMIC_PT}"
    )
    invite = SipRequest.parse(invite_raw)

    captured: dict[str, object] = {}

    def _capture_engine(**kwargs: object) -> MagicMock:
        captured.update(kwargs)
        return MagicMock(
            connect=AsyncMock(return_value=True),
            stop=AsyncMock(return_value=None),
            start_rtcp=AsyncMock(return_value=None),
            _rtcp_active=False,
            local_port=20008,
        )

    with (
        patch("hermes_voip.adapter.RtpMediaTransport", side_effect=_capture_engine),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        for _ in range(20):
            await asyncio.sleep(0)

    ok = _sent_200_ok(transport)
    assert ok.status_code == 200
    # Answer echoes the dynamic PT 109 (RFC 3264).
    assert "a=rtpmap:109 G722/8000" in ok.body
    assert captured.get("codec") is EngineCodec.G722
    assert captured.get("payload_type") == 109, (
        f"engine not opened with the negotiated dynamic PT 109; "
        f"got {captured.get('payload_type')!r} (sending static 9 is the bug)"
    )


# ===========================================================================
# ptime negotiation + adaptive jitter ACTIVATION (ADR-0063, completes ADR-0056)
# ===========================================================================
#
# PR #142 built negotiate_ptime() + the engine ptime setter + the adaptive
# JitterBuffer, but they were DORMANT (the adapter never called them). These
# tests prove the adapter now activates both on the inbound answer path.

# A G.722 offer that explicitly requests a 40 ms packetisation time (a value in
# our supported framing set, != the 20 ms default) so the test proves the
# NEGOTIATED ptime reaches the engine, not the hard-coded 20.
_FAKE_SDP_OFFER_G722_PTIME40 = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 20000 RTP/AVP 9 0\r\n"
    "a=rtpmap:9 G722/8000\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=ptime:40\r\n"
    "a=sendrecv\r\n"
)


def _make_invite_g722_ptime40(call_id: str) -> str:
    content_length = len(_FAKE_SDP_OFFER_G722_PTIME40.encode("utf-8"))
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKpt40\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:9999@pbx.example.test>;tag={new_tag()}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:9999@127.0.0.1:5061;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{_FAKE_SDP_OFFER_G722_PTIME40}"
    )


@pytest.mark.asyncio
async def test_inbound_invite_activates_negotiated_ptime_and_adaptive_jitter() -> None:
    """Inbound answer: the negotiated ptime + adaptive jitter reach the engine.

    The offer requests ``a=ptime:40``; the adapter must set ``engine.ptime`` to
    the negotiated 40 (not the 20 ms default) AND construct the engine with
    ``jitter_adapt=True`` + the configured ``jitter_max_depth`` ceiling — the
    activation that makes ADR-0056's launch-promoted features live.
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport)
    # Default media config → jitter_max_depth == 10.
    adapter._media_cfg = load_media_config({})

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite_g722_ptime40(call_id=call_id))

    captured: dict[str, object] = {}
    engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        start_rtcp=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=20010,
    )

    def _capture_engine(**kwargs: object) -> MagicMock:
        captured.update(kwargs)
        return engine

    with (
        patch("hermes_voip.adapter.RtpMediaTransport", side_effect=_capture_engine),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        for _ in range(20):
            await asyncio.sleep(0)

    # Adaptive jitter activated at construction with the configured ceiling.
    assert captured.get("jitter_adapt") is True, (
        f"engine not opened with adaptive jitter; got "
        f"jitter_adapt={captured.get('jitter_adapt')!r} (the dormant ADR-0056 path)"
    )
    assert captured.get("jitter_max_depth") == 10, (
        f"engine not opened with the configured jitter ceiling; "
        f"got jitter_max_depth={captured.get('jitter_max_depth')!r}"
    )
    # The NEGOTIATED ptime (40) was applied to the engine, not the 20 ms default.
    assert engine.ptime == 40, (
        f"engine.ptime was not set to the negotiated 40 ms; got {engine.ptime!r} "
        f"(the adapter is still leaving the engine on its hard-coded default)"
    )


def test_negotiated_ptime_is_codec_aware_opus_pinned_to_20() -> None:
    """Codec-aware ptime negotiation pins Opus to 20 ms (ADR-0063).

    Codex review BLOCKING: the engine's Opus encoder frames EXACTLY one 20 ms packet
    (960 samples @ 48 kHz) and raises on any other size, so negotiating a non-20 ms
    ptime for an Opus call would crash send_audio. G.711/G.722 encode per-sample and
    DO support the other framings. The negotiator must therefore honour a 40 ms offer
    for G.722 but pin Opus to 20 ms regardless.
    """
    from hermes_voip.adapter import _negotiated_ptime  # noqa: PLC0415
    from hermes_voip.media.engine import Codec as EngineCodec  # noqa: PLC0415
    from hermes_voip.sdp import AudioMedia  # noqa: PLC0415
    from hermes_voip.sdp import Codec as SdpCodecForTest  # noqa: PLC0415

    def _audio_with_ptime(ms: int) -> AudioMedia:
        return AudioMedia(
            port=20000,
            protocol="RTP/AVP",
            codecs=(SdpCodecForTest(payload_type=0, encoding="PCMU", clock_rate=8000),),
            crypto=(),
            ptime=ms,
            direction="sendrecv",
            connection_address="127.0.0.1",
            maxptime=None,
        )

    # G.722 (per-sample encode) honours a carriable 40 ms request …
    assert _negotiated_ptime(_audio_with_ptime(40), EngineCodec.G722) == 40
    # … but Opus is pinned to 20 ms even when the peer asks for 40 (engine can only
    # frame 960-sample/20 ms Opus packets).
    assert _negotiated_ptime(_audio_with_ptime(40), EngineCodec.OPUS) == 20
    # G.711 honours 40 too.
    assert _negotiated_ptime(_audio_with_ptime(40), EngineCodec.PCMU) == 40


# ===========================================================================
# Provider/LLM error sanitisation spoken to the caller (ADR-0063, LAUNCH #4)
# ===========================================================================
#
# An unrecoverable backend error (a raw 502 / provider message / stack trace)
# arrives at send() as the "reply" text; today it is read aloud verbatim — an
# info leak + unprofessional. The adapter must speak a short safe line instead
# and log the real error (redacted), never raising toward the caller.

# A realistic provider error string the Hermes gateway can hand to send() as the
# turn text on an unrecoverable LLM failure (HTTP status + provider error class).
_PROVIDER_ERROR_REPLY = (
    "API call failed: HTTP 502 Bad Gateway (overloaded_error: Overloaded)"
)


@pytest.mark.asyncio
async def test_send_provider_error_speaks_safe_line_not_raw_error() -> None:
    """A provider-error reply is replaced with a short safe spoken line.

    The raw error text must never reach the TTS sink; the safe apology is spoken
    instead, and the call still reports a successful send (no retry storm).
    """
    from hermes_voip.provider_error import safe_error_reply  # noqa: PLC0415

    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    adapter._media_cfg = load_media_config({})  # language defaults to "en"

    call_id = new_call_id()
    spoken: list[str] = []

    class _FakeLoop:
        async def speak(self, text: AsyncIterator[str]) -> None:
            async for chunk in text:
                spoken.append(chunk)

    adapter._call_loops[call_id] = _FakeLoop()  # type: ignore[assignment]

    result = await adapter.send(call_id, _PROVIDER_ERROR_REPLY)

    assert result.success
    # The safe line was spoken …
    assert spoken == [safe_error_reply("en")]
    # … and the raw provider error reached the TTS sink NOWHERE.
    joined = " ".join(spoken)
    assert "502" not in joined
    assert "overloaded_error" not in joined
    assert "API call failed" not in joined


@pytest.mark.asyncio
async def test_send_provider_error_logs_real_error_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real provider error is logged at WARNING (so it is not lost)."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    adapter._media_cfg = load_media_config({})

    call_id = new_call_id()

    class _FakeLoop:
        async def speak(self, text: AsyncIterator[str]) -> None:
            async for _chunk in text:
                pass

    adapter._call_loops[call_id] = _FakeLoop()  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="hermes_voip.adapter"):
        result = await adapter.send(call_id, _PROVIDER_ERROR_REPLY)

    assert result.success
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    # Some WARNING record references the backend error (so an operator sees it).
    assert any(
        "502" in r.getMessage() or "provider" in r.getMessage().lower()
        for r in warnings
    ), (
        f"no WARNING logged the real provider error; "
        f"records={[r.getMessage() for r in warnings]!r}"
    )


@pytest.mark.asyncio
async def test_send_provider_error_log_redacts_credential_shapes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The WARNING log masks credential-SHAPED tokens, not just exact secrets.

    Codex review MINOR: a leaked error could carry a bearer token / api_key= /
    password= value that is NOT the plugin's own configured secret; the WARNING log
    must still not leak it. Built at runtime (never a literal — gitleaks all-refs).
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    adapter._media_cfg = load_media_config({})

    call_id = new_call_id()

    class _FakeLoop:
        async def speak(self, text: AsyncIterator[str]) -> None:
            async for _chunk in text:
                pass

    # Minimal speak()-only double for the typed call-loop slot (rule 20: a full
    # CallLoop is unnecessary for this log-redaction assertion).
    adapter._call_loops[call_id] = _FakeLoop()  # type: ignore[assignment]

    secret_token = base64.b64encode(bytes(range(33, 63))).decode()
    err = (
        f"API call failed: HTTP 502; Authorization: Bearer {secret_token}; "
        f"api_key={secret_token}"
    )

    with caplog.at_level(logging.WARNING, logger="hermes_voip.adapter"):
        result = await adapter.send(call_id, err)

    assert result.success
    blob = "\n".join(r.getMessage() for r in caplog.records)
    # The diagnostic status survives …
    assert "502" in blob
    # … but the credential-shaped value does not.
    assert secret_token not in blob


@pytest.mark.asyncio
async def test_send_provider_error_log_redacts_json_credential_shapes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The WARNING log masks JSON-quoted credential shapes, not just header style.

    Codex review BLOCKING: a backend error can embed a JSON body — e.g.
    ``{"Authorization":"Bearer X","api_key":"X"}`` — where a closing quote sits
    between the key and the ``:`` separator, so a header-only regex misses it and
    the secret leaks (rule 34: the repo is PUBLIC). Built at runtime (never a
    literal — gitleaks all-refs).
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    adapter._media_cfg = load_media_config({})

    call_id = new_call_id()

    class _FakeLoop:
        async def speak(self, text: AsyncIterator[str]) -> None:
            async for _chunk in text:
                pass

    # Minimal speak()-only double for the typed call-loop slot (rule 20: a full
    # CallLoop is unnecessary for this log-redaction assertion).
    adapter._call_loops[call_id] = _FakeLoop()  # type: ignore[assignment]

    secret_token = base64.b64encode(bytes(range(64, 94))).decode()
    # A JSON body with a non-bearer Authorization scheme + an api_key field: a closing
    # quote sits between each key and its ``:``, so a header-only regex misses both.
    # (No "Bearer " here — otherwise a greedy bearer-token match would mask the rest
    # of the compact JSON by accident and hide the real leak.)
    err = (
        "API call failed: HTTP 502; "
        '{"Authorization": "Basic ' + secret_token + '", '
        '"api_key": "' + secret_token + '"}'
    )

    with caplog.at_level(logging.WARNING, logger="hermes_voip.adapter"):
        result = await adapter.send(call_id, err)

    assert result.success
    blob = "\n".join(r.getMessage() for r in caplog.records)
    # The diagnostic status survives …
    assert "502" in blob
    # … but the JSON-embedded credential does not.
    assert secret_token not in blob


@pytest.mark.asyncio
async def test_send_provider_error_log_redacts_json_value_with_escaped_quote(
    caplog: pytest.LogCaptureFixture,
) -> None:
    r"""A JSON credential value containing an escaped quote is fully masked.

    Codex review BLOCKING: a value like ``{"api_key":"HEAD\"TAIL"}`` must not leak
    the post-escaped-quote tail — the redactor treats ``\"`` as an escaped char
    INSIDE the quoted value, not as the closing quote (rule 34). Built at runtime.
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    adapter._media_cfg = load_media_config({})

    call_id = new_call_id()

    class _FakeLoop:
        async def speak(self, text: AsyncIterator[str]) -> None:
            async for _chunk in text:
                pass

    # Minimal speak()-only double for the typed call-loop slot (rule 20).
    adapter._call_loops[call_id] = _FakeLoop()  # type: ignore[assignment]

    head = base64.b64encode(bytes(range(94, 109))).decode()
    tail = base64.b64encode(bytes(range(109, 124))).decode()
    # The api_key value is head + an ESCAPED quote + tail; the ``\"`` must not be read
    # as the closing quote (which would leave ``tail`` visible in the log).
    err = 'API call failed: HTTP 502; {"api_key": "' + head + '\\"' + tail + '"}'

    with caplog.at_level(logging.WARNING, logger="hermes_voip.adapter"):
        result = await adapter.send(call_id, err)

    assert result.success
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "502" in blob
    assert head not in blob
    # The tail AFTER the escaped quote must not leak either.
    assert tail not in blob


@pytest.mark.asyncio
async def test_send_genuine_reply_not_treated_as_provider_error() -> None:
    """A genuine reply mentioning an error/number is still spoken verbatim."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    adapter._media_cfg = load_media_config({})

    call_id = new_call_id()
    spoken: list[str] = []

    class _FakeLoop:
        async def speak(self, text: AsyncIterator[str]) -> None:
            async for chunk in text:
                spoken.append(chunk)

    adapter._call_loops[call_id] = _FakeLoop()  # type: ignore[assignment]

    reply = "Your reference number is 502 and there was no error with the booking."
    result = await adapter.send(call_id, reply)
    assert result.success
    assert reply in spoken
