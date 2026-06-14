"""Behavioural async tests for the ADR-0004 provider seams.

The conformance suite (test_providers_conformance.py) proves fakes match the
Protocols structurally; this suite actually *drives* the async members — awaiting
``screen``, iterating ``inbound_audio``/``stream``/``synthesize``, and exercising
``TtsStream`` lifecycle — so the live call loop (ADR-0003) has a tested seam to
build on. Requires the async test runner (P0.3 of the implementation plan).
"""

from collections.abc import AsyncIterator

import pytest

from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardResult, GuardVerdict, InjectionGuard
from hermes_voip.providers.transport import MediaTransport
from hermes_voip.providers.tts import StreamingTTS, TtsStream


def _frame(ts: int) -> PcmFrame:
    return PcmFrame(samples=b"\x00\x00", sample_rate=8000, monotonic_ts_ns=ts)


class _ScriptedASR:
    """A StreamingASR that turns each inbound frame into a final Transcript."""

    @property
    def input_sample_rate(self) -> int:
        return 16000

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        async def _run() -> AsyncIterator[Transcript]:
            index = 0
            async for _frame_in in audio:
                index += 1
                yield Transcript(
                    text=f"word{index}",
                    is_final=True,
                    end_of_turn=False,
                    confidence=1.0,
                )

        return _run()


class _RecordingTtsStream:
    """A TtsStream that yields one frame, then records flush/cancel calls."""

    def __init__(self) -> None:
        self.flushed = False
        self.cancelled = False
        self._yielded = False

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        if self._yielded or self.cancelled:
            raise StopAsyncIteration
        self._yielded = True
        return _frame(0)

    async def flush(self) -> None:
        self.flushed = True

    async def cancel(self) -> None:
        self.cancelled = True


class _ScriptedTTS:
    def __init__(self) -> None:
        self.last_stream = _RecordingTtsStream()

    @property
    def output_sample_rate(self) -> int:
        return 24000

    def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
        self.last_stream = _RecordingTtsStream()
        return self.last_stream


class _AllowGuard:
    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            normalized_text=text,
            reasons=(),
            degraded=False,
            score=0.0,
        )


class _LoopbackTransport:
    """A MediaTransport that echoes sent frames back on inbound_audio."""

    def __init__(self) -> None:
        self.sent: list[PcmFrame] = []
        self.connected = False

    @property
    def inbound_sample_rate(self) -> int:
        return 8000

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def disconnect(self) -> None:
        self.connected = False

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        async def _run() -> AsyncIterator[PcmFrame]:
            for frame in self.sent:
                yield frame

        return _run()

    async def send_audio(self, frame: PcmFrame) -> None:
        self.sent.append(frame)


async def _drain_frames(frames: AsyncIterator[PcmFrame]) -> list[PcmFrame]:
    return [frame async for frame in frames]


async def _yield_frames(*frames: PcmFrame) -> AsyncIterator[PcmFrame]:
    for frame in frames:
        yield frame


@pytest.mark.asyncio
async def test_streaming_asr_yields_a_transcript_per_frame() -> None:
    asr: StreamingASR = _ScriptedASR()
    transcripts = [t async for t in asr.stream(_yield_frames(_frame(0), _frame(1)))]
    assert [t.text for t in transcripts] == ["word1", "word2"]
    assert all(t.is_final for t in transcripts)


@pytest.mark.asyncio
async def test_tts_stream_emits_then_flush_and_cancel_record() -> None:
    tts: StreamingTTS = _ScriptedTTS()

    async def _text() -> AsyncIterator[str]:
        yield "hello"

    stream = tts.synthesize(_text(), voice="default")
    frames = await _drain_frames(stream)
    assert len(frames) == 1
    await stream.flush()
    await stream.cancel()
    assert isinstance(tts, _ScriptedTTS)
    assert tts.last_stream.flushed is True
    assert tts.last_stream.cancelled is True


@pytest.mark.asyncio
async def test_injection_guard_screen_is_awaitable() -> None:
    guard: InjectionGuard = _AllowGuard()
    result = await guard.screen("book a table", call_id="call-1")
    assert result.verdict is GuardVerdict.ALLOW
    assert result.normalized_text == "book a table"


@pytest.mark.asyncio
async def test_media_transport_loopback_round_trips_frames() -> None:
    transport: MediaTransport = _LoopbackTransport()
    assert await transport.connect() is True
    await transport.send_audio(_frame(10))
    await transport.send_audio(_frame(20))
    inbound = await _drain_frames(transport.inbound_audio())
    assert [f.monotonic_ts_ns for f in inbound] == [10, 20]
    await transport.disconnect()
