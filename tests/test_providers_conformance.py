"""Structural conformance of fakes to the ADR-0004 provider Protocols.

The Protocols are ``runtime_checkable``, and the call loop is tested against
fakes (ADR-0004 consequences), so both the runtime ``isinstance`` check and the
static assignment (which ``mypy --strict`` verifies) must hold for a minimal fake.
"""

from collections.abc import AsyncIterator

from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardResult, GuardVerdict, InjectionGuard
from hermes_voip.providers.transport import MediaTransport
from hermes_voip.providers.tts import StreamingTTS, TtsStream


async def _no_transcripts() -> AsyncIterator[Transcript]:
    return
    yield  # pragma: no cover - makes this an async generator that yields nothing


async def _no_frames() -> AsyncIterator[PcmFrame]:
    return
    yield  # pragma: no cover


class FakeASR:
    @property
    def input_sample_rate(self) -> int:
        return 16000

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        return _no_transcripts()


class FakeTtsStream:
    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        raise StopAsyncIteration

    async def flush(self) -> None: ...

    async def cancel(self) -> None: ...

    async def aclose(self) -> None: ...


class FakeTTS:
    @property
    def output_sample_rate(self) -> int:
        return 24000

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        return FakeTtsStream()


class FakeGuard:
    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            normalized_text=text,
            reasons=(),
            degraded=False,
            score=0.0,
        )


class FakeTransport:
    @property
    def inbound_sample_rate(self) -> int:
        return 8000

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None: ...

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        return _no_frames()

    async def send_audio(self, frame: PcmFrame) -> None: ...

    async def flush_outbound(self, *, fade_ms: int) -> None: ...


def test_fakes_satisfy_protocols_statically() -> None:
    # The assignments are what `mypy --strict` verifies; the body exercises the
    # declared members so the runtime assertions are meaningful (not tautological).
    asr: StreamingASR = FakeASR()
    tts: StreamingTTS = FakeTTS()
    stream: TtsStream = FakeTtsStream()
    guard: InjectionGuard = FakeGuard()
    transport: MediaTransport = FakeTransport()
    assert asr.input_sample_rate == 16000
    assert tts.output_sample_rate == 24000
    assert transport.inbound_sample_rate == 8000
    assert isinstance(stream, TtsStream)
    assert isinstance(guard, InjectionGuard)


def test_fakes_satisfy_protocols_at_runtime() -> None:
    assert isinstance(FakeASR(), StreamingASR)
    assert isinstance(FakeTTS(), StreamingTTS)
    assert isinstance(FakeTtsStream(), TtsStream)
    assert isinstance(FakeGuard(), InjectionGuard)
    assert isinstance(FakeTransport(), MediaTransport)
