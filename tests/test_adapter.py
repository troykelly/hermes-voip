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

from hermes_voip.config import (
    ConfigError,
    ExtensionConfig,
    GatewayConfig,
    MediaConfig,
    load_media_config,
)
from hermes_voip.manager import InDialog, NewCall, RegistrationManager
from hermes_voip.message import SipRequest, SipResponse, new_call_id, new_tag
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.tts import TtsStream

if TYPE_CHECKING:
    from collections.abc import Callable

    from hermes_voip.adapter import VoipAdapter


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

    def __init__(self, *, is_up: bool = True) -> None:
        self._is_up = is_up
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
    def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
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
            ),
        ),
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
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
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
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
                local_port=20002,
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
                local_port=20002,
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
                local_port=20002,
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
        local_port=20002,
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
# Regression (live inbound-call failure, 2026-06-16): the 200 OK answering an
# inbound INVITE MUST carry our dialog To-tag, so the gateway's subsequent
# in-dialog ACK/BYE route back to the established CallSession instead of going
# out-of-dialog. The live UCM6304 call showed the caller "answered immediately"
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
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
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

    Reproduces the live UCM6304 failure: with no To-tag on the 200 OK, the
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
                    local_port=20002,
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
                local_port=20002,
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
                local_port=20002,
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
                local_port=20002,
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
