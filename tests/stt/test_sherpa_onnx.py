"""Streaming/turn logic of ``SherpaOnnxASR`` against a FAKE recogniser.

The real sherpa-onnx engine (the ``ml`` extra) is heavy and model-bound, so the
streaming contract is tested with a dependency-injected fake recogniser that
mimics the sherpa ``OnlineRecognizer`` surface (``create_stream`` /
``accept_waveform`` / ``is_ready`` / ``decode_stream`` / ``get_result`` /
``is_endpoint`` / ``reset``). This is model-free and always runs in CI.

The contract under test (ADR-0006):

* PCM16 frames in -> interim + final ``Transcript`` values out, drained until the
  inbound iterator is exhausted (caller closes it on hang-up);
* the engine is fed normalised **float32** samples at **16 kHz** (the frames are
  already at ``input_sample_rate``; the media layer resampled them);
* a sherpa *endpoint* finalises the current segment: the recogniser emits a
  ``is_final=True`` transcript and resets, then starts a fresh segment;
* ``end_of_turn`` is **not** decided by the recogniser — ADR-0006 assigns the
  turn boundary to ADR-0008's endpointer, so every transcript this engine emits
  carries ``end_of_turn=False``;
* the synchronous inference runs off the event loop (via ``aio.stream_from_thread``)
  so it never blocks the shared loop; closing the consumer stops the worker.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import numpy as np
import pytest

from hermes_voip.providers.asr import StreamingASR
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.stt.sherpa_onnx import SherpaOnnxASR

_RATE = 16_000


def _frame(*samples: int, ts: int = 0) -> PcmFrame:
    return PcmFrame(
        samples=struct.pack(f"<{len(samples)}h", *samples),
        sample_rate=_RATE,
        monotonic_ts_ns=ts,
    )


async def _frames(*items: PcmFrame) -> AsyncIterator[PcmFrame]:
    for item in items:
        yield item


# --- a fake sherpa OnlineRecognizer + stream ---------------------------------


@dataclass
class _FakeStream:
    """Stands in for a sherpa ``OnlineStream``; records fed waveforms."""

    fed: list[np.ndarray[tuple[int], np.dtype[np.float32]]] = field(
        default_factory=list
    )
    fed_rates: list[int] = field(default_factory=list)
    finished: bool = False


@dataclass
class _Script:
    """One scripted decode step: the cumulative text and whether it endpoints."""

    text: str
    endpoint: bool


class _FakeRecognizer:
    """A scripted fake of the sherpa ``OnlineRecognizer`` surface.

    Each call to :meth:`decode_stream` advances through ``steps``; ``get_result``
    returns the current step's text and ``is_endpoint`` its endpoint flag. The
    fake is ready exactly once per fed waveform (one decode per frame), which is
    enough to exercise the interim/final/reset state machine deterministically.
    """

    def __init__(self, steps: list[_Script]) -> None:
        self._steps = steps
        self._index = 0
        self._ready = False
        self.resets = 0
        self.created_streams = 0
        self.last_stream: _FakeStream | None = None

    def create_stream(self) -> _FakeStream:
        self.created_streams += 1
        self.last_stream = _FakeStream()
        return self.last_stream

    def accept_waveform(
        self,
        stream: _FakeStream,
        sample_rate: int,
        samples: np.ndarray[tuple[int], np.dtype[np.float32]],
    ) -> None:
        stream.fed.append(samples)
        stream.fed_rates.append(sample_rate)
        self._ready = True

    def is_ready(self, stream: _FakeStream) -> bool:
        ready = self._ready
        self._ready = False  # one decode per fed waveform
        return ready

    def decode_stream(self, stream: _FakeStream) -> None:
        if self._index < len(self._steps):
            self._index += 1

    def get_result(self, stream: _FakeStream) -> str:
        if self._index == 0:
            return ""
        return self._steps[self._index - 1].text

    def is_endpoint(self, stream: _FakeStream) -> bool:
        if self._index == 0:
            return False
        return self._steps[self._index - 1].endpoint

    def reset(self, stream: _FakeStream) -> None:
        self.resets += 1

    def input_finished(self, stream: _FakeStream) -> None:
        stream.finished = True


# --- the contract -------------------------------------------------------------


def test_sherpa_asr_is_a_streaming_asr() -> None:
    """It conforms to the ADR-0004 ``StreamingASR`` Protocol (static + runtime)."""
    asr: StreamingASR = SherpaOnnxASR.from_recognizer(_FakeRecognizer([]))
    assert isinstance(asr, StreamingASR)


def test_sherpa_asr_declares_16k_input_rate() -> None:
    """The recogniser declares the 16 kHz rate the media layer resamples to."""
    asr = SherpaOnnxASR.from_recognizer(_FakeRecognizer([]))
    assert asr.input_sample_rate == _RATE


@pytest.mark.asyncio
async def test_sherpa_asr_emits_interim_then_final_on_endpoint() -> None:
    """Growing partials, then a final transcript when the engine endpoints."""
    recognizer = _FakeRecognizer(
        [
            _Script("book", endpoint=False),
            _Script("book a", endpoint=False),
            _Script("book a table", endpoint=True),
        ]
    )
    asr = SherpaOnnxASR.from_recognizer(recognizer)
    out = [t async for t in asr.stream(_frames(_frame(1), _frame(2), _frame(3)))]

    assert [(t.text, t.is_final) for t in out] == [
        ("book", False),
        ("book a", False),
        ("book a table", True),
    ]
    # The engine never decides the turn boundary (ADR-0006 -> ADR-0008 owns it).
    assert all(t.end_of_turn is False for t in out)
    # Exactly one reset, on the single endpoint.
    assert recognizer.resets == 1


@pytest.mark.asyncio
async def test_sherpa_asr_starts_a_new_segment_after_endpoint() -> None:
    """After a final, decoding continues into a fresh segment (multi-utterance)."""
    recognizer = _FakeRecognizer(
        [
            _Script("yes", endpoint=True),
            _Script("please", endpoint=False),
        ]
    )
    asr = SherpaOnnxASR.from_recognizer(recognizer)
    out = [t async for t in asr.stream(_frames(_frame(1), _frame(2)))]

    assert [(t.text, t.is_final) for t in out] == [
        ("yes", True),
        ("please", False),
    ]
    assert recognizer.resets == 1


@pytest.mark.asyncio
async def test_sherpa_asr_suppresses_empty_hypotheses() -> None:
    """An empty decode result is not emitted as a transcript (no blank partials)."""
    recognizer = _FakeRecognizer(
        [
            _Script("", endpoint=False),
            _Script("hi", endpoint=False),
        ]
    )
    asr = SherpaOnnxASR.from_recognizer(recognizer)
    out = [t async for t in asr.stream(_frames(_frame(1), _frame(2)))]
    assert [t.text for t in out] == ["hi"]


@pytest.mark.asyncio
async def test_sherpa_asr_feeds_float32_at_16k() -> None:
    """The engine receives normalised float32 at 16 kHz, not PCM16 bytes."""
    recognizer = _FakeRecognizer([_Script("x", endpoint=False)])
    asr = SherpaOnnxASR.from_recognizer(recognizer)
    # max-positive and max-negative PCM16 -> ~+1.0 / -1.0 float32.
    [_ async for _ in asr.stream(_frames(_frame(32767, -32768)))]

    assert recognizer.created_streams == 1
    stream = recognizer.last_stream
    assert stream is not None
    assert stream.fed_rates == [_RATE]
    fed = stream.fed[0]
    assert fed.dtype.name == "float32"
    assert fed[0] == pytest.approx(32767 / 32768, abs=1e-6)
    assert fed[1] == pytest.approx(-1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_sherpa_asr_flushes_tail_on_input_end() -> None:
    """When inbound audio ends, the engine input is finished so a tail flushes.

    ADR-0006 / sherpa: at end-of-input the recogniser is told ``input_finished``
    (with trailing-silence padding) so a final partial is decoded rather than
    being stranded mid-buffer. We assert a final transcript is still emitted for
    the last segment after the inbound iterator is exhausted.
    """
    recognizer = _FakeRecognizer(
        [
            _Script("almost", endpoint=False),
            _Script("almost done", endpoint=False),
        ]
    )
    asr = SherpaOnnxASR.from_recognizer(recognizer)
    out = [t async for t in asr.stream(_frames(_frame(1), _frame(2)))]
    # The last hypothesis is promoted to final at end-of-stream even with no
    # engine endpoint, so the caller never loses trailing speech.
    assert out[-1].text == "almost done"
    assert out[-1].is_final is True


@pytest.mark.asyncio
async def test_sherpa_asr_propagates_engine_errors() -> None:
    """An exception from the engine surfaces to the consumer (rule 37)."""

    class _Boom(_FakeRecognizer):
        def decode_stream(self, stream: _FakeStream) -> None:
            msg = "engine exploded"
            raise RuntimeError(msg)

    asr = SherpaOnnxASR.from_recognizer(_Boom([_Script("x", endpoint=False)]))
    with pytest.raises(RuntimeError, match="engine exploded"):
        [_ async for _ in asr.stream(_frames(_frame(1)))]
