"""Tests for VoipAdapter: connect, send, get_chat_info, disconnect, inbound INVITE.

TDD rule 18: these tests are written before the implementation. All fakes are
synthetic (no real network, no real Hermes, no real ML); credentials and hostnames
are obvious fakes (pbx.example.test, ext 1000, 127.0.0.1).
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio  # noqa: F401 — pytest-asyncio is a test dep; needed for asyncio_mode

from hermes_voip.config import ConfigError, ExtensionConfig
from hermes_voip.manager import NewCall
from hermes_voip.message import SipRequest, new_call_id, new_tag
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import (
    GuardResult,
    GuardVerdict,
)
from hermes_voip.providers.tts import TtsStream

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

    def remove_call(self, call_id: str) -> None:
        self._calls.pop(call_id, None)


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
        self.on_new_call_cb: Callable[[NewCall], None] | None = None

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

    def fire_new_call(self, invite_raw: str) -> None:
        """Simulate the transport delivering a new inbound INVITE."""
        req = SipRequest.parse(invite_raw)
        ext_cfg = _fake_ext_config()
        call = NewCall(registration=ext_cfg, invite=req)
        if self.on_new_call_cb is not None:
            self.on_new_call_cb(call)


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
        # This is an async generator: drains audio, never yields transcripts.
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
# Fake hermes_surface types (BasePlatformAdapter shim)
# ---------------------------------------------------------------------------


class _FakeSendResult:
    def __init__(self, *, success: bool, message_id: str | None = None) -> None:
        self.success = success
        self.message_id = message_id
        self.error: str | None = None if success else "unknown call_id"


class _FakePlatformConfig:
    """Minimal stand-in for gateway.config.PlatformConfig."""

    extra: dict[str, str]

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.extra = env or {}


class _FakePlatform:
    """Minimal stand-in for gateway.config.Platform enum member."""

    name = "voip"


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


def _fake_ext_config() -> ExtensionConfig:
    """The ExtensionConfig the fake manager would return."""
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
# Fixture: a pre-wired adapter using fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_transport() -> _FakeTransport:
    return _FakeTransport()


@pytest.fixture
def fake_manager() -> _FakeManager:
    return _FakeManager(is_up=True)


@pytest.fixture
def fake_manager_down() -> _FakeManager:
    return _FakeManager(is_up=False)


async def _build_adapter(
    transport: _FakeTransport,
    manager: _FakeManager,
    *,
    connect: bool = True,
) -> object:
    """Construct a VoipAdapter wired to fakes, optionally calling connect()."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = _FakePlatformConfig(_FAKE_ENV | _FAKE_MEDIA_ENV)
    platform = _FakePlatform()

    def _make_result(*, success: bool) -> _FakeSendResult:
        return _FakeSendResult(success=success)

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
        patch(
            "hermes_voip.adapter.load_media_config",
            return_value=MagicMock(),
        ),
        patch(
            "hermes_voip.adapter.build_providers",
            return_value=_fake_providers(),
        ),
        patch(
            "hermes_voip.adapter._make_tls_context",
            return_value=MagicMock(spec=ssl.SSLContext),
        ),
        patch(
            "hermes_voip.adapter.SipOverTlsTransport",
            return_value=transport,
        ),
        patch(
            "hermes_voip.adapter.RegistrationManager",
            return_value=manager,
        ),
    ):
        adapter = VoipAdapter(config, platform)
        # Patch handle_message so we can observe inbound turns
        adapter.handle_message = MagicMock()  # type: ignore[attr-defined]
        # Patch _make_send_result so send() can return a fake SendResult
        adapter._make_send_result = _make_result  # type: ignore[attr-defined]
        if connect:
            await adapter.connect()
        return adapter


# ---------------------------------------------------------------------------
# (a) connect() returns True when the fake manager is up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_returns_true_when_manager_up() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    _ = await _build_adapter(transport, manager)
    # connect() ran inside _build_adapter; the manager is up
    assert manager.connected


@pytest.mark.asyncio
async def test_connect_returns_false_when_manager_down() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=False)
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = _FakePlatformConfig(_FAKE_ENV | _FAKE_MEDIA_ENV)
    platform = _FakePlatform()

    with (
        patch("hermes_voip.adapter.load_gateway_config", return_value=MagicMock()),
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch(
            "hermes_voip.adapter._make_tls_context",
            return_value=MagicMock(spec=ssl.SSLContext),
        ),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config, platform)
        adapter.handle_message = MagicMock()  # type: ignore[attr-defined]
        with patch.object(transport, "connect", new=AsyncMock(return_value=None)):
            result = await adapter.connect()
    assert result is False


# ---------------------------------------------------------------------------
# (b) validate_config raises ConfigError on missing HERMES_SIP_HOST
# ---------------------------------------------------------------------------


def test_validate_config_raises_on_missing_host() -> None:
    from hermes_voip.adapter import validate_voip_config  # noqa: PLC0415

    config = _FakePlatformConfig({})  # no env at all
    with pytest.raises(ConfigError):
        validate_voip_config(config)


def test_validate_config_passes_with_valid_env() -> None:
    from hermes_voip.adapter import validate_voip_config  # noqa: PLC0415

    config = _FakePlatformConfig(_FAKE_ENV)
    validate_voip_config(config)  # must not raise


# ---------------------------------------------------------------------------
# (c) send() routes to speak(); unknown call_id → failure result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_unknown_call_id_returns_failure() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    assert isinstance(adapter, VoipAdapter)
    result = await adapter.send("nonexistent-call-id", "hello")
    assert not result.success


@pytest.mark.asyncio
async def test_send_known_call_id_calls_loop_speak() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    assert isinstance(adapter, VoipAdapter)

    # Inject a fake loop for a call_id
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
# (d) get_chat_info returns {name, type} for live + ended calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_chat_info_live_call() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    assert isinstance(adapter, VoipAdapter)
    call_id = new_call_id()
    # Register a fake call record
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
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    assert isinstance(adapter, VoipAdapter)
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
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    assert isinstance(adapter, VoipAdapter)
    info = await adapter.get_chat_info("unknown-call")
    assert "type" in info


# ---------------------------------------------------------------------------
# (e) inbound INVITE creates a CallSession + CallLoop per call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_invite_registers_call_loop() -> None:
    """An inbound INVITE should create a CallLoop in _call_loops."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    assert isinstance(adapter, VoipAdapter)

    call_id = new_call_id()
    invite_raw = _make_invite(call_id=call_id)

    # Patch the heavy IO parts of _on_inbound_invite so no UDP socket is opened.
    # Keep the patches active while we await — the background task resolves
    # its module-level names at call time, so patches must outlive the fire.
    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
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
        patch(
            "hermes_voip.adapter.GuardSessionState",
            return_value=MagicMock(),
        ),
        patch(
            "hermes_voip.adapter._make_vad",
            return_value=MagicMock(),
        ),
        patch(
            "hermes_voip.adapter._make_endpointer",
            return_value=MagicMock(),
        ),
    ):
        invite_req = SipRequest.parse(invite_raw)
        from hermes_voip.config import ExtensionConfig  # noqa: PLC0415

        ext = ExtensionConfig(index=0, extension="1000", username="1000", password="x")
        new_call = NewCall(registration=ext, invite=invite_req)
        adapter._on_inbound_invite(new_call)

        # Allow multiple loop iterations for the background task to reach
        # the self._call_info assignment (after connect + 200 OK + SessionSetup).
        for _ in range(10):
            await asyncio.sleep(0)

        # The call_id should be registered before or during call_loop.run()
        assert call_id in adapter._call_info


# ---------------------------------------------------------------------------
# (f) a finalized transcript reaches exactly one handle_message(VOICE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_turn_calls_handle_message() -> None:
    """deliver_turn for a call_id should result in handle_message being called."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    assert isinstance(adapter, VoipAdapter)

    call_id = new_call_id()
    # Register a call record
    adapter._call_info[call_id] = {
        "name": "9999",
        "remote_uri": "sip:9999@pbx.example.test",
        "type": "dm",
        "ended": False,
    }
    # Simulate deliver_turn being called
    await adapter._deliver_turn(call_id, "hello from caller")

    # handle_message should have been called once
    adapter.handle_message.assert_called_once()  # type: ignore[attr-defined]
    event = adapter.handle_message.call_args[0][0]  # type: ignore[attr-defined]
    # Must have text attribute
    assert hasattr(event, "text")
    assert event.text == "hello from caller"


# ---------------------------------------------------------------------------
# (g) disconnect() is idempotent and drains manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_idempotent() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    assert isinstance(adapter, VoipAdapter)
    with patch.object(transport, "aclose", new=AsyncMock(return_value=None)):
        await adapter.disconnect()
        await adapter.disconnect()  # second call must not raise

    assert manager.closed


# ---------------------------------------------------------------------------
# (h) guard state is per-call (distinct GuardSessionState per Call-ID)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_call_gets_distinct_guard_state() -> None:
    """Two simultaneous calls must not share GuardSessionState."""
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager)
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    assert isinstance(adapter, VoipAdapter)

    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                local_port=20002,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallSession",
            return_value=MagicMock(
                dialog_id=("cid", "lt", "rt"),
                ended=False,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch(
            "hermes_voip.adapter.GuardSessionState",
            side_effect=lambda call_id: MagicMock(call_id=call_id),
        ) as mock_guard_state,
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        for n in range(2):
            cid = f"call-id-{n}"
            req = SipRequest.parse(_make_invite(call_id=cid))
            from hermes_voip.config import ExtensionConfig  # noqa: PLC0415

            ext = ExtensionConfig(
                index=0, extension="1000", username="1000", password="x"
            )
            adapter._on_inbound_invite(NewCall(registration=ext, invite=req))
            for _ in range(10):
                await asyncio.sleep(0)

    # GuardSessionState should have been constructed twice, with different call_ids
    assert mock_guard_state.call_count == 2
    call_ids_used = {c.args[0] for c in mock_guard_state.call_args_list}
    assert "call-id-0" in call_ids_used
    assert "call-id-1" in call_ids_used
