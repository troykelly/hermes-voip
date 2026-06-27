"""HERMES_VOIP_DENY_MODE=decline polite-decline path (ADR-0020 §5/§6 Phase 2).

Phase 1 always rejects a declined-group caller with a hard ``603 Decline`` in the
pre-200-OK window. Phase 2 adds the ``decline`` variant: instead of the hard 603,
the call is ANSWERED (``200 OK``), one short TTS line is synthesised and played to
the caller over the (real, existing) media path, then the call is torn down with a
``BYE``. This trains a spammer less than a hard 603 (no "this number is blocked"
signal) while still keeping the agent off the call.

These drive the REAL ``VoipAdapter`` at its inbound INVITE seam, mirroring
``test_adapter_caller_modes.py`` (the ``hermes`` extra + the hermes-contract CI
job). Fakes only — ``pbx.example.test`` / ext ``1000`` (PUBLIC-repo invariant).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_voip.caller_modes import (
    CallerMode,
    CallerModeConfig,
    Normalization,
)
from hermes_voip.config import ExtensionConfig
from hermes_voip.manager import NewCall
from hermes_voip.message import SipRequest, new_call_id, new_tag
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from hermes_voip.adapter import VoipAdapter
    from hermes_voip.providers.tts import TtsStream


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


# --- fakes (mirror test_adapter_caller_modes.py, kept local) -----------------


class _FakeTransport:
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
        self._calls: dict[tuple[str, str, str], object] = {}

    @property
    def is_up(self) -> bool:
        return self._is_up

    async def connect(self, *, timeout: float = 10.0) -> bool:
        return self._is_up

    async def aclose(self) -> None: ...

    def add_call(self, dialog_id: tuple[str, str, str], consumer: object) -> None:
        self._calls[dialog_id] = consumer

    def remove_call(self, dialog_id: tuple[str, str, str]) -> None:
        self._calls.pop(dialog_id, None)


class _FakeTtsStream:
    """One PCM frame so the decline phrase actually plays out to ``send_audio``."""

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[PcmFrame]:
        yield PcmFrame(samples=b"\x00\x00", sample_rate=8000, monotonic_ts_ns=0)

    async def flush(self) -> None: ...

    async def cancel(self) -> None: ...

    async def aclose(self) -> None: ...


class _RecordingTTS:
    """Records the (joined) text passed to ``synthesize`` so the test can assert it."""

    def __init__(self) -> None:
        self.phrases: list[str] = []

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        async def _collect() -> None:
            parts: list[str] = []
            async for chunk in text:
                parts.append(chunk)
            self.phrases.append("".join(parts))

        # The synthesiser consumes the text iterator; do it eagerly on the loop so the
        # recorded phrase is available right after playout completes.
        asyncio.ensure_future(_collect())  # noqa: RUF006 — fire-and-forget recorder
        return _FakeTtsStream()  # type: ignore[return-value]


class _FakeASR:
    async def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[object]:
        async for _ in audio:
            pass
        empty: tuple[object, ...] = ()
        for chunk in empty:  # always empty — forces the async-gen shape
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


_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
}


def _ext_config() -> ExtensionConfig:
    return ExtensionConfig(index=0, extension="1000", username="1000", password="fake")


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


def _make_invite(*, caller: str, call_id: str) -> str:
    ftag = new_tag()
    content_length = len(_FAKE_SDP_OFFER.encode("utf-8"))
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKfake\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:{caller}@pbx.example.test>;tag={ftag}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:{caller}@127.0.0.1:60000;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{_FAKE_SDP_OFFER}"
    )


def _deny_caller(caller: str) -> CallerModeConfig:
    return CallerModeConfig(
        allow=(),
        deny=(caller,),
        grey=(),
        default_mode=CallerMode.GREY,
        normalization=Normalization.E164,
    )


async def _build_adapter(  # noqa: PLR0913 — keyword-only test wiring: transport + manager + caller_modes + tts + the two deny-mode knobs are all real dependencies
    transport: _FakeTransport,
    manager: _FakeManager,
    *,
    caller_modes: CallerModeConfig,
    tts: _RecordingTTS,
    deny_mode: str,
    decline_phrase: str,
) -> VoipAdapter:
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))
    providers = Providers(asr=_FakeASR(), tts=tts, guard=_FakeGuard())  # type: ignore[arg-type]
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
                deny_mode=deny_mode,
            ),
        ),
        patch(
            "hermes_voip.adapter.load_media_config",
            return_value=MagicMock(
                require_secure_media=False,
                session_expires=600,
                min_se=90,
                decline_phrase=decline_phrase,
            ),
        ),
        patch("hermes_voip.adapter.load_caller_modes", return_value=caller_modes),
        patch("hermes_voip.adapter.build_providers", return_value=providers),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()
        return adapter


@pytest.mark.asyncio
async def test_deny_mode_decline_answers_200_speaks_then_byes() -> None:
    """Decline mode: answer 200 (NOT 603), speak one phrase, then BYE — no agent.

    The caller "9999" is on the deny list, and ``deny_mode=decline``. Instead of the
    Phase-1 hard ``603 Decline``, the adapter answers the call (a ``200 OK`` reaches
    the transport), synthesises the configured decline phrase through the EXISTING TTS
    provider, plays it, then ends the dialog with a ``BYE``. No CallLoop is built (the
    agent never sees the caller).
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    tts = _RecordingTTS()
    phrase = "Sorry, I cannot take this call"
    adapter = await _build_adapter(
        transport,
        manager,
        caller_modes=_deny_caller("9999"),
        tts=tts,
        deny_mode="decline",
        decline_phrase=phrase,
    )

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(caller="9999", call_id=call_id))

    engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        start_rtcp=AsyncMock(return_value=None),
        send_audio=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=20002,
        inbound_sample_rate=8000,
    )

    async def _fake_hang_up() -> None:
        # The real CallSession.hang_up sends an in-dialog BYE + stops media; model
        # that here so the test asserts the BYE reached the transport.
        await transport.send("BYE sip:9999@pbx.example.test SIP/2.0\r\n")
        await engine.stop()

    session = MagicMock(
        dialog_id=("c", "l", "r"),
        ended=False,
        hang_up=AsyncMock(side_effect=_fake_hang_up),
    )

    with (
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
        patch("hermes_voip.adapter.CallSession", return_value=session),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        new_call = NewCall(registration=_ext_config(), invite=invite)
        adapter._on_inbound_invite(new_call)
        for _ in range(60):
            await asyncio.sleep(0)

    # A 200 OK was sent (the call was ANSWERED) and NO 603 Decline.
    assert any("200 OK" in m for m in transport.sent), transport.sent
    assert not any("603 Decline" in m for m in transport.sent), transport.sent
    # A BYE followed the answer (the call was torn down after the decline line).
    assert any(m.startswith("BYE") for m in transport.sent), transport.sent
    answer_idx = next(i for i, m in enumerate(transport.sent) if "200 OK" in m)
    bye_idx = next(i for i, m in enumerate(transport.sent) if m.startswith("BYE"))
    assert answer_idx < bye_idx, transport.sent
    # The decline phrase was synthesised through the real TTS provider (non-empty).
    assert tts.phrases, "the decline phrase was never synthesised"
    assert tts.phrases[0].strip()
    assert phrase in tts.phrases[0]
    # No CallLoop / CallSession-driven agent path was started for the declined caller.
    assert call_id not in adapter._call_loops
