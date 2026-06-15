"""Behavioural tests for SherpaKokoroTTS streaming/flush/cancel (ADR-0007).

The real engine (sherpa-onnx + Kokoro) needs model weights absent from CI, so the
synthesiser backend is **dependency-injected**: a fake ``Synthesizer`` yields PCM16
chunks per sentence and honours the ``stop`` predicate. That lets these tests pin
the streaming contract — per-sentence synthesis order, ``flush()`` end-of-utterance
framing, ``cancel()``/barge-in stopping mid-utterance and yielding no more frames,
and the 24 kHz ``PcmFrame`` output type — with no model and no numpy. A separate
real-model test (``importorskip``) covers the actual engine when weights exist.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator

import pytest

from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.tts.sherpa_kokoro import SherpaKokoroTTS, Synthesizer

_OUTPUT_RATE = 24_000


def _pcm16(sample: int, count: int) -> bytes:
    """``count`` identical PCM16-LE samples carrying value ``sample``."""
    return (sample & 0xFFFF).to_bytes(PCM16_BYTES_PER_SAMPLE, "little") * count


class _FakeSynth:
    """A Synthesizer that emits a fixed number of PCM16 chunks per call.

    Records every text it is asked to synthesise (to assert per-sentence order)
    and respects ``stop()`` between chunks (to prove cancel reaches the backend).
    """

    def __init__(self, chunks_per_call: int = 3, chunk_samples: int = 4) -> None:
        self.requested: list[str] = []
        self.stopped_on: list[str] = []
        self._chunks_per_call = chunks_per_call
        self._chunk_samples = chunk_samples

    def synthesize(self, text: str, stop: Callable[[], bool]) -> Iterator[bytes]:
        self.requested.append(text)
        for index in range(self._chunks_per_call):
            if stop():
                self.stopped_on.append(text)
                return
            # A distinct sample value per chunk so ordering is observable.
            yield _pcm16(index + 1, self._chunk_samples)


class _BlockingSynth:
    """A Synthesizer whose chunks gate on an event, to test mid-stream cancel."""

    def __init__(self, gate: asyncio.Event, loop: asyncio.AbstractEventLoop) -> None:
        self.requested: list[str] = []
        self._gate = gate
        self._loop = loop

    def synthesize(self, text: str, stop: Callable[[], bool]) -> Iterator[bytes]:
        self.requested.append(text)
        index = 0
        while not stop():
            index += 1
            yield _pcm16(index, 4)
            # Block until the test opens the gate for the next chunk, so the
            # consumer can cancel between chunks deterministically.
            fut = asyncio.run_coroutine_threadsafe(self._gate.wait(), self._loop)
            fut.result()
            self._gate.clear()


async def _text(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


async def _drain(stream: TtsStream) -> list[PcmFrame]:
    return [frame async for frame in stream]


def _tts(synth: Synthesizer) -> SherpaKokoroTTS:
    """Build a provider whose backend is the given (fake) synthesiser."""
    return SherpaKokoroTTS(synthesizer_factory=lambda: synth)


# --- the provider conforms to the ADR-0004 seam ------------------------------


def test_fake_synth_satisfies_the_synthesizer_protocol() -> None:
    """The injected fake structurally matches the Synthesizer backend seam."""
    synth = _FakeSynth()
    conforms: Synthesizer = synth  # fails to type-check unless it matches the seam
    assert conforms is synth
    assert synth.requested == []  # exercise a concrete member so the bind is real


def test_sherpa_kokoro_is_a_streaming_tts() -> None:
    """SherpaKokoroTTS satisfies the StreamingTTS Protocol (structural + runtime)."""
    tts: StreamingTTS = _tts(_FakeSynth())
    assert isinstance(tts, StreamingTTS)
    assert tts.output_sample_rate == _OUTPUT_RATE


# --- streaming: frames flow, at 24 kHz, per sentence -------------------------


@pytest.mark.asyncio
async def test_streams_pcm16_frames_at_output_rate() -> None:
    """Every emitted frame is PCM16 at the declared 24 kHz output rate."""
    fake = _FakeSynth(chunks_per_call=2)
    tts = _tts(fake)
    frames = await _drain(tts.synthesize(_text("Hello there. "), voice="af"))
    assert frames  # at least one frame
    assert all(f.sample_rate == _OUTPUT_RATE for f in frames)
    assert all(len(f.samples) % PCM16_BYTES_PER_SAMPLE == 0 for f in frames)


@pytest.mark.asyncio
async def test_synthesises_each_sentence_separately_in_order() -> None:
    """The text stream is segmented and each sentence synthesised in order."""
    fake = _FakeSynth(chunks_per_call=1)
    tts = _tts(fake)
    await _drain(tts.synthesize(_text("First one. ", "Second one. "), voice="af"))
    assert fake.requested == ["First one.", "Second one."]


@pytest.mark.asyncio
async def test_flush_synthesises_the_trailing_unterminated_sentence() -> None:
    """A final sentence with no terminator is still synthesised (end of turn)."""
    fake = _FakeSynth(chunks_per_call=1)
    tts = _tts(fake)
    stream = tts.synthesize(_text("Complete. ", "dangling tail"), voice="af")
    frames = await _drain(stream)
    # Draining to exhaustion flushes the tail; both segments were synthesised.
    assert fake.requested == ["Complete.", "dangling tail"]
    assert len(frames) == 2


@pytest.mark.asyncio
async def test_explicit_flush_is_idempotent_and_safe_after_drain() -> None:
    """Calling flush() after the stream is exhausted does not error or re-emit."""
    fake = _FakeSynth(chunks_per_call=1)
    tts = _tts(fake)
    stream = tts.synthesize(_text("Only this. "), voice="af")
    frames = await _drain(stream)
    await stream.flush()  # no-op: nothing buffered, no extra synthesis
    assert fake.requested == ["Only this."]
    assert len(frames) == 1


# --- cancel / barge-in -------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_stops_yielding_further_frames() -> None:
    """After cancel(), the stream yields no more frames (barge-in)."""
    gate = asyncio.Event()
    loop = asyncio.get_running_loop()
    synth = _BlockingSynth(gate, loop)
    tts = _tts(synth)
    stream = tts.synthesize(_text("Keep talking forever. "), voice="af")

    first = await anext(aiter(stream))
    assert first.sample_rate == _OUTPUT_RATE

    await stream.cancel()
    gate.set()  # release the synth so it can observe stop() and exit

    # No further frames are produced after cancellation.
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_cancel_propagates_stop_to_the_backend() -> None:
    """cancel() flips the stop predicate the backend sees mid-utterance."""
    fake = _FakeSynth(chunks_per_call=100, chunk_samples=2)
    tts = _tts(fake)
    stream = tts.synthesize(_text("A very long sentence to interrupt. "), voice="af")

    # Pull one frame, then cancel; the backend must observe stop() and bail.
    await anext(aiter(stream))
    await stream.cancel()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    # The fake recorded that it was stopped (did not emit all 100 chunks).
    assert fake.stopped_on == ["A very long sentence to interrupt."]


@pytest.mark.asyncio
async def test_cancel_before_iteration_yields_nothing() -> None:
    """Cancelling before the first frame yields an immediately-empty stream."""
    fake = _FakeSynth(chunks_per_call=5)
    tts = _tts(fake)
    stream = tts.synthesize(_text("Never heard. "), voice="af")
    await stream.cancel()
    assert await _drain(stream) == []


@pytest.mark.asyncio
async def test_cancel_is_idempotent() -> None:
    """Calling cancel() twice is safe (second call is a no-op)."""
    fake = _FakeSynth()
    tts = _tts(fake)
    stream = tts.synthesize(_text("Whatever. "), voice="af")
    await stream.cancel()
    await stream.cancel()
    assert await _drain(stream) == []


# --- errors propagate (rule 37) ---------------------------------------------


@pytest.mark.asyncio
async def test_backend_error_propagates_to_the_consumer() -> None:
    """An exception raised by the synthesiser surfaces from the stream."""

    class _BoomSynth:
        def synthesize(self, text: str, stop: Callable[[], bool]) -> Iterator[bytes]:
            if text:  # always true for the test input; keeps the yield reachable
                raise RuntimeError("model exploded")
            yield b""  # pragma: no cover - empty-text branch makes this a generator

    tts = _tts(_BoomSynth())
    stream = tts.synthesize(_text("Trigger it. "), voice="af")
    with pytest.raises(RuntimeError, match="model exploded"):
        await _drain(stream)
