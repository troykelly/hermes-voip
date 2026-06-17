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

import asyncio
import logging
import struct
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from hermes_voip.providers.asr import StreamingASR
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.stt.resample import FloatArray
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

    fed: list[FloatArray] = field(default_factory=list)
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
        # Mirrors real sherpa: after reset() the current segment is cleared, so
        # get_result() is "" and is_endpoint() is False until the next decode.
        self._cleared = True
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
        samples: FloatArray,
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
            self._cleared = False

    def get_result(self, stream: _FakeStream) -> str:
        if self._cleared or self._index == 0:
            return ""
        return self._steps[self._index - 1].text

    def is_endpoint(self, stream: _FakeStream) -> bool:
        if self._cleared or self._index == 0:
            return False
        return self._steps[self._index - 1].endpoint

    def reset(self, stream: _FakeStream) -> None:
        self.resets += 1
        self._cleared = True  # the finalized segment is consumed

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
    pytest.importorskip("numpy")  # the decode loop converts PCM16 -> float32
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
    """After a final, decoding continues into a fresh segment (multi-utterance).

    The second utterance never reaches an engine endpoint, so it is emitted as an
    interim and then promoted to a final at end-of-stream (the flush; see
    ``test_sherpa_asr_flushes_tail_on_input_end``) — the consumer always gets a
    closing final per utterance.
    """
    pytest.importorskip("numpy")  # the decode loop converts PCM16 -> float32
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
        ("please", True),  # promoted to final by the end-of-stream flush
    ]
    assert recognizer.resets == 1


# --- inbound rate reconciliation (ADR-0017) -----------------------------------
#
# The transport delivers 8 kHz G.711 frames (inbound_sample_rate ==
# G711_SAMPLE_RATE). The zipformer wants 16 kHz. The ASR feed path must upsample
# 8 kHz -> 16 kHz BEFORE accept_waveform, so the recogniser is fed audio whose
# real rate matches the rate it is told (16 kHz). Previously the 8 kHz samples
# were handed to accept_waveform LABELLED 16 kHz (half-rate audio, wrong pitch).

_WIRE_RATE = 8_000  # G.711 narrowband wire rate the transport delivers


def _wire_frame(sample_count: int, *, ts: int = 0) -> PcmFrame:
    """An 8 kHz PcmFrame of ``sample_count`` non-silent samples (a ramp).

    Mirrors what the transport yields: PCM16 at the G.711 wire rate. Non-silent
    so the upsampled output is a real, longer array (not all-zero) and its sample
    count is observable.
    """
    samples = struct.pack(
        f"<{sample_count}h",
        *[((i % 32) - 16) * 8 for i in range(sample_count)],
    )
    return PcmFrame(samples=samples, sample_rate=_WIRE_RATE, monotonic_ts_ns=ts)


def _fed_sample_count(samples: FloatArray) -> int:
    """Number of float32 samples in a captured ``accept_waveform`` array."""
    return len(samples.tobytes()) // 4  # float32 = 4 bytes/sample


@pytest.mark.asyncio
async def test_sherpa_asr_upsamples_8k_inbound_to_16k_before_feeding() -> None:
    """8 kHz transport frames are upsampled to 16 kHz before accept_waveform.

    The recogniser must be fed at 16 kHz AND receive ~2x the input sample count
    (8 kHz -> 16 kHz doubles the samples). The previous code fed the raw 8 kHz
    samples while telling sherpa they were 16 kHz: half the samples, wrong rate.
    """
    pytest.importorskip("numpy")  # the decode loop / resample need numpy
    recognizer = _FakeRecognizer([_Script("hi", endpoint=True)])
    asr = SherpaOnnxASR.from_recognizer(recognizer)

    input_samples = 160  # 20 ms at 8 kHz
    _ = [t async for t in asr.stream(_frames(_wire_frame(input_samples)))]

    stream = recognizer.last_stream
    assert stream is not None
    assert stream.fed, "the recogniser must have been fed at least one waveform"
    # Every waveform must be presented at the recogniser's real rate (16 kHz),
    # never the 8 kHz wire rate.  The stream ends without an engine endpoint so
    # _flush() is called, which adds a silence-pad waveform at the recogniser rate
    # before input_finished — so fed_rates has at least two entries, all 16 kHz.
    assert all(r == _RATE for r in stream.fed_rates), (
        f"all fed rates must be {_RATE}, got {stream.fed_rates}"
    )
    # The FIRST fed waveform is the real audio (upsampled from 8 kHz -> 16 kHz).
    # It must carry the UPSAMPLED sample count (~2x): proof the 8 kHz frame was
    # actually converted to 16 kHz, not relabelled. audioop.ratecv produces
    # close to 2x the input samples (a couple of samples of edge slack).
    fed = _fed_sample_count(stream.fed[0])
    assert fed >= input_samples * 2 - 4, (
        f"expected ~{input_samples * 2} upsampled samples, got {fed}"
    )


@pytest.mark.asyncio
async def test_sherpa_asr_feeds_g722_16k_inbound_natively_without_upsampling() -> None:
    """G.722 inbound frames (16 kHz) are fed to the recogniser AS-IS (ADR-0022).

    On a G.722 call the engine delivers native 16 kHz audio — already the
    recogniser's rate — so the STT feed must pass it straight through, NOT upsample
    it. The discriminating assertion: the fed sample count equals the input sample
    count (a wrongly-applied 8k->16k upsample would ~double it, and feeding it as
    8 kHz would mislabel native wideband). This is the STT half of the wideband
    accuracy win: the recogniser sees real 16 kHz, not 8 kHz upsampled to 16 kHz.
    """
    pytest.importorskip("numpy")  # the decode loop / resample need numpy
    recognizer = _FakeRecognizer([_Script("hi", endpoint=True)])
    asr = SherpaOnnxASR.from_recognizer(recognizer)

    input_samples = 320  # 20 ms at 16 kHz (the G.722 audio rate)
    native_16k = PcmFrame(
        samples=struct.pack(
            f"<{input_samples}h",
            *[((i % 32) - 16) * 8 for i in range(input_samples)],
        ),
        sample_rate=_RATE,  # 16 kHz — what the G.722 engine yields
        monotonic_ts_ns=0,
    )
    _ = [t async for t in asr.stream(_frames(native_16k))]

    stream = recognizer.last_stream
    assert stream is not None
    assert stream.fed, "the recogniser must have been fed at least one waveform"
    assert all(r == _RATE for r in stream.fed_rates), (
        f"all fed rates must be {_RATE}, got {stream.fed_rates}"
    )
    # The first fed waveform is the real audio at its NATIVE 16 kHz count — no
    # upsample doubling (which would be ~640) and no truncation.
    fed = _fed_sample_count(stream.fed[0])
    assert fed == input_samples, (
        f"native 16 kHz G.722 audio must be fed unchanged ({input_samples} "
        f"samples), got {fed} (it was wrongly resampled)"
    )


@pytest.mark.asyncio
async def test_sherpa_asr_suppresses_empty_hypotheses() -> None:
    """An empty decode result is not emitted as a transcript (no blank partials).

    The non-empty hypothesis is emitted once as interim and once, promoted, as the
    end-of-stream final; the blank result in between is never emitted.
    """
    pytest.importorskip("numpy")  # the decode loop converts PCM16 -> float32
    recognizer = _FakeRecognizer(
        [
            _Script("", endpoint=False),
            _Script("hi", endpoint=False),
        ]
    )
    asr = SherpaOnnxASR.from_recognizer(recognizer)
    out = [t async for t in asr.stream(_frames(_frame(1), _frame(2)))]
    assert [(t.text, t.is_final) for t in out] == [("hi", False), ("hi", True)]


@pytest.mark.asyncio
async def test_sherpa_asr_feeds_float32_at_16k() -> None:
    """The engine receives normalised float32 at 16 kHz, not PCM16 bytes."""
    np = pytest.importorskip("numpy")
    recognizer = _FakeRecognizer([_Script("x", endpoint=False)])
    asr = SherpaOnnxASR.from_recognizer(recognizer)
    # max-positive and max-negative PCM16 -> ~+1.0 / -1.0 float32.
    [_ async for _ in asr.stream(_frames(_frame(32767, -32768)))]

    assert recognizer.created_streams == 1
    stream = recognizer.last_stream
    assert stream is not None
    # _flush() adds a silence-pad waveform before input_finished so there are at
    # least two fed calls; all must be at the recogniser rate (16 kHz).
    assert all(r == _RATE for r in stream.fed_rates), (
        f"all fed rates must be {_RATE}, got {stream.fed_rates}"
    )
    # The FIRST fed waveform is the real audio — check dtype and sample values.
    fed = np.asarray(stream.fed[0])
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
    pytest.importorskip("numpy")  # the decode loop converts PCM16 -> float32
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
async def test_sherpa_asr_flush_feeds_silence_before_input_finished() -> None:
    """_flush() pads silence before input_finished so the last word decodes.

    The streaming zipformer needs lookahead context (trailing silence) to commit
    its final hypothesis.  Without a pre-flush silence pad, the trailing word of
    a phrase that ends right at the audio boundary is stranded in the model's
    lookahead buffer and never emitted.

    This test asserts that when inbound audio ends mid-utterance the recogniser
    receives at least one silence frame (all-zero waveform) via ``accept_waveform``
    BEFORE ``input_finished`` is called.  The silence frame must be at the
    recogniser rate (16 kHz) and must contain at least one sample.

    The ``_FakeRecognizer.accept_waveform`` records every waveform fed.  We
    observe the sequence: real audio frames, then at least one all-zero frame,
    then ``input_finished()``.
    """
    np = pytest.importorskip("numpy")

    # A recogniser whose flush can only succeed if silence is fed first:
    # the script has two steps; the second is only reached when a second waveform
    # (the silence pad) is accepted after the single real audio frame.
    recognizer = _FakeRecognizer(
        [
            _Script("tail", endpoint=False),  # produced by the real audio frame
            _Script("tail word", endpoint=False),  # produced by the silence pad
        ]
    )
    asr = SherpaOnnxASR.from_recognizer(recognizer)

    # Feed exactly one real (non-zero) audio frame, then let the stream end.
    out = [t async for t in asr.stream(_frames(_frame(32767)))]

    stream = recognizer.last_stream
    assert stream is not None, "recogniser was never given a stream"
    assert stream.finished, "input_finished() must be called on stream end"

    # Find the waveforms that were fed: there must be at least two.
    fed = stream.fed
    assert len(fed) >= 2, (
        f"expected real audio frame + at least one silence pad, "
        f"got {len(fed)} fed calls"
    )

    # The last fed waveform before input_finished must be all-zero (the silence pad).
    last_fed = np.asarray(fed[-1])
    assert len(last_fed) > 0, "silence pad must be non-empty"
    assert float(np.max(np.abs(last_fed))) == pytest.approx(0.0), (
        "the pre-flush padding frame must be all zeros (silence)"
    )

    # The final transcript must still be the promoted flush result.
    assert out[-1].is_final is True


@pytest.mark.asyncio
async def test_sherpa_asr_logs_segment_at_final(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A final transcript logs INFO with its text and sample count.

    The logging provides operators with observability into the STT confidence
    and segment length so they can tune the endpointer silence threshold and
    identify transcription quality problems (per the task requirement).
    """
    pytest.importorskip("numpy")  # the decode loop converts PCM16 -> float32
    recognizer = _FakeRecognizer(
        [
            _Script("hello world", endpoint=True),
        ]
    )
    asr = SherpaOnnxASR.from_recognizer(recognizer)
    with caplog.at_level(logging.INFO, logger="hermes_voip.stt.sherpa_onnx"):
        _out = [t async for t in asr.stream(_frames(_frame(1)))]

    # At least one INFO record must mention the transcript text.
    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("hello world" in m.lower() for m in info_msgs), (
        f"no INFO log mentioning the final transcript text; got: {info_msgs}"
    )


@pytest.mark.asyncio
async def test_sherpa_asr_propagates_engine_errors() -> None:
    """An exception from the engine surfaces to the consumer (rule 37)."""
    pytest.importorskip("numpy")  # the decode loop converts PCM16 -> float32

    class _Boom(_FakeRecognizer):
        def decode_stream(self, stream: _FakeStream) -> None:
            msg = "engine exploded"
            raise RuntimeError(msg)

    asr = SherpaOnnxASR.from_recognizer(_Boom([_Script("x", endpoint=False)]))
    with pytest.raises(RuntimeError, match="engine exploded"):
        [_ async for _ in asr.stream(_frames(_frame(1)))]


@pytest.mark.asyncio
async def test_sherpa_asr_early_consumer_break_releases_worker_without_timeout() -> (
    None
):
    """Breaking from ``stream()`` early releases the worker without a hang.

    The bug: the ``_END_OF_AUDIO`` sentinel is enqueued with ``put_nowait``
    in ``_run()``'s finally.  When the frame queue is full at that moment the
    put is silently dropped (``contextlib.suppress(queue.Full)``), and the
    worker thread parks in ``frames.get()`` indefinitely.  The asyncgen
    finalizer cleanup raises ``RuntimeError("worker did not terminate")``
    after 5 s — swallowed as an unhandled task exception, but the thread
    is leaked for 5 s.

    The fix: pass an ``on_cancel`` to ``stream_from_thread`` that sets the stop
    flag AND enqueues ``_END_OF_AUDIO`` via a blocking put so the sentinel is
    guaranteed to arrive even when the queue is full.

    To reproduce the bug deterministically we block the worker inside
    ``accept_waveform`` (the frame-processing step) until the feeder has
    filled the entire frame queue.  At that moment we let the consumer ``break``
    early.  With the bug the ``put_nowait`` is dropped; with the fix the
    ``on_cancel`` delivers the sentinel reliably.

    We detect the leak by checking whether the worker thread is still alive
    shortly after the break (it should not be).
    """
    pytest.importorskip("numpy")  # the decode loop converts PCM16 -> float32

    # Frame queue capacity mirrors sherpa_onnx._BUFFER (16).
    frame_queue_cap = 16
    release = threading.Event()
    first_accept_done = threading.Event()

    # Track the worker thread so we can check it after the break.
    worker_threads: list[threading.Thread] = []
    orig_thread_init = threading.Thread.__init__

    def _tracking_init(  # noqa: PLR0913  # mirrors Thread.__init__ signature exactly
        self: threading.Thread,
        group: None = None,
        target: object = None,
        name: str | None = None,
        args: object = (),
        kwargs: object = None,
        *,
        daemon: bool | None = None,
    ) -> None:
        # Forward all arguments to the real __init__; add name-based tracking.
        orig_thread_init(
            self,
            group=group,
            target=target,  # type: ignore[arg-type]  # target is Callable|None
            name=name,
            args=args,  # type: ignore[arg-type]  # args is Iterable
            kwargs=kwargs,  # type: ignore[arg-type]  # kwargs is Mapping|None
            daemon=daemon,
        )
        if getattr(self, "name", "").startswith("hermes-voip-stream"):
            worker_threads.append(self)

    threading.Thread.__init__ = _tracking_init  # type: ignore[method-assign]

    class _SlowFirstAccept(_FakeRecognizer):
        """Blocks on the first ``accept_waveform`` so frames fill the queue."""

        _blocked_once: bool = False

        def accept_waveform(
            self,
            stream: _FakeStream,
            sample_rate: int,
            samples: FloatArray,
        ) -> None:
            if not self._blocked_once:
                self._blocked_once = True
                first_accept_done.set()
                release.wait()  # hold the worker; feeder fills the queue
            super().accept_waveform(stream, sample_rate, samples)

    recognizer = _SlowFirstAccept(
        [_Script("word", endpoint=False)] * (frame_queue_cap + 2)
    )
    asr = SherpaOnnxASR.from_recognizer(recognizer)

    async def _many_frames() -> AsyncIterator[PcmFrame]:
        for _ in range(frame_queue_cap + 2):
            yield _frame(1)

    collected: list[str] = []

    async def _fill_then_release() -> None:
        # Wait until the worker is blocked inside accept_waveform.
        await asyncio.to_thread(first_accept_done.wait)
        # Give the feeder time to fill the frame queue while the worker is stuck.
        await asyncio.sleep(0.05)
        # Unblock the worker so it produces the first transcript then loops back
        # to frames.get() — at which point the queue is full and we break.
        release.set()

    fill_task = asyncio.create_task(_fill_then_release())
    try:
        async for t in asr.stream(_many_frames()):
            collected.append(t.text)
            break  # early exit — frame queue is full at this moment
    finally:
        threading.Thread.__init__ = orig_thread_init  # type: ignore[method-assign]

    await fill_task
    # Give the worker a brief window to terminate (on_cancel must deliver the
    # sentinel before this check; the old best-effort put_nowait would have
    # dropped it and the thread would still be alive here).
    await asyncio.sleep(0.1)

    assert collected, "no transcript was received"
    leaked = [t for t in worker_threads if t.is_alive()]
    assert not leaked, (
        f"{len(leaked)} worker thread(s) still alive after early break — "
        "on_cancel not wired; sentinel dropped when frame queue is full"
    )
