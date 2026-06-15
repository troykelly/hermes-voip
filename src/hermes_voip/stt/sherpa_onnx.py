"""Self-hosted streaming zipformer recogniser (StreamingASR default, ADR-0006).

:class:`SherpaOnnxASR` is the default recogniser: a pinned Apache-2.0 sherpa-onnx
streaming zipformer running **in-process** (no cloud, no egress, rule 40). The
sherpa-onnx inference call is synchronous C++; the shared Hermes event loop must
never block on it, so the decode loop runs on a worker thread and its transcripts
are bridged back to the loop with :func:`hermes_voip.aio.stream_from_thread`.

Inbound :class:`PcmFrame` arrive at 16 kHz (the media layer resampled the 8 kHz
G.711 stream — see :mod:`hermes_voip.stt.resample`); each frame's PCM16 bytes are
converted to the normalised float32 sherpa-onnx requires before
``accept_waveform``. The engine reports stable text segments and finalises one on
its endpoint; **the turn boundary is not decided here** — ADR-0006 assigns
``Transcript.end_of_turn`` to ADR-0008's endpointer, so every transcript this
recogniser yields carries ``end_of_turn=False`` and the integration layer overlays
the endpointer signal.

The concrete sherpa surface is hidden behind the small :class:`_Recognizer`
Protocol, so the engine is constructed from a model directory in production yet
dependency-injected as a fake in tests (model-free CI). Structural conformance to
ADR-0004's ``StreamingASR`` is enforced by ``mypy`` and the ``runtime_checkable``
Protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import queue
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Final, Protocol

from hermes_voip.aio import stream_from_thread
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.onnx_compat import ensure_sherpa_loadable
from hermes_voip.stt.resample import (
    RECOGNISER_SAMPLE_RATE,
    FloatArray,
    pcm16_to_float32,
)

__all__ = ["SherpaOnnxASR"]

# sherpa's streaming zipformer does not expose a per-hypothesis score on this
# path, so a stable hypothesis is reported at full confidence; the value is
# informational (the turn/endpoint decision lives in ADR-0008, not here).
_FULL_CONFIDENCE: Final[float] = 1.0

# How many items may sit un-consumed before a producer blocks (back-pressure;
# bounds memory if one side stalls). Used for both the frame and transcript hops.
_BUFFER: Final[int] = 16


class _Recognizer[StreamT](Protocol):
    """The minimal, recogniser-centric streaming surface the decoder drives.

    Generic over the stream handle type ``StreamT`` (opaque to the decoder, which
    only threads it back into the recogniser's own methods): the real engine binds
    it to its sherpa stream handle and the test fake to its own stream, so each
    keeps a precise stream type without ``Any``. Nothing here imports sherpa
    types, so the core stays vendor-free and ``mypy --strict`` clean.
    """

    def create_stream(self) -> StreamT: ...

    def accept_waveform(
        self, stream: StreamT, sample_rate: int, samples: FloatArray
    ) -> None: ...

    def is_ready(self, stream: StreamT) -> bool: ...

    def decode_stream(self, stream: StreamT) -> None: ...

    def get_result(self, stream: StreamT) -> str: ...

    def is_endpoint(self, stream: StreamT) -> bool: ...

    def reset(self, stream: StreamT) -> None: ...

    def input_finished(self, stream: StreamT) -> None: ...


# Sentinel enqueued when the inbound audio iterator ends, so the worker decodes
# its tail and returns (distinct from None, which could mask a bug).
class _EndOfAudio:
    pass


_END_OF_AUDIO: Final[_EndOfAudio] = _EndOfAudio()

type _FrameItem = FloatArray | _EndOfAudio


# A decode run: consume frames from the queue, yield transcripts. The recogniser
# (and its stream type) is captured in the closure, so this signature is free of
# the generic ``StreamT`` — that is how ``SherpaOnnxASR`` stores it without ``Any``.
type _DecodeRun = Callable[[queue.Queue[_FrameItem]], Iterator[Transcript]]


class SherpaOnnxASR:
    """Streaming zipformer recogniser implementing ``StreamingASR`` (ADR-0006)."""

    _decode_run: _DecodeRun

    def __init__(self, model_dir: str) -> None:
        """Build the in-process sherpa-onnx recogniser from ``model_dir``.

        ``model_dir`` holds the pinned Apache-2.0 streaming-zipformer artifacts
        (``tokens.txt`` + ``encoder``/``decoder``/``joiner`` ONNX). The shared
        ``onnxruntime`` library is made loadable first
        (:func:`hermes_voip.providers.onnx_compat.ensure_sherpa_loadable`), then
        ``sherpa_onnx`` is imported and the recogniser constructed.
        """
        self._decode_run = _Decoder(_build_recognizer(model_dir)).run

    @classmethod
    def from_recognizer[StreamT](
        cls, recognizer: _Recognizer[StreamT]
    ) -> SherpaOnnxASR:
        """Construct from an already-built recogniser (DI for tests).

        Bypasses model loading so the streaming/turn state machine can be tested
        against a fake recogniser with no model and no native dependency. The
        recogniser's stream type ``StreamT`` is bound here and erased into the
        stored decode closure (kept precise inside :class:`_Decoder`).
        """
        instance = cls.__new__(cls)
        instance._decode_run = _Decoder(recognizer).run
        return instance

    @property
    def input_sample_rate(self) -> int:
        """16 kHz: the media layer resamples the 8 kHz G.711 stream to match."""
        return RECOGNISER_SAMPLE_RATE

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        """Drain ``audio`` until exhausted; yield interim and final transcripts.

        Frames are converted to float32 on the loop and handed to a worker thread
        that runs the blocking sherpa decode loop; the worker's transcripts are
        bridged back via :func:`stream_from_thread`. ``end_of_turn`` is always
        ``False`` here — ADR-0008's endpointer owns the turn boundary.

        The ``on_cancel`` callback passed to :func:`stream_from_thread` sets the
        stop flag and enqueues ``_END_OF_AUDIO`` via a daemon thread that performs
        a blocking ``put`` — this guarantees the sentinel always reaches the worker
        even when the frame queue is at capacity (a ``put_nowait`` would silently
        fail when the queue is full, leaving the worker parked in ``frames.get``
        and the ``stream_from_thread`` join timing out after 5 s).
        """

        async def _run() -> AsyncIterator[Transcript]:
            frames: queue.Queue[_FrameItem] = queue.Queue(maxsize=_BUFFER)
            stop = threading.Event()
            feeder = asyncio.ensure_future(_feed(audio, frames, stop))

            def _on_cancel() -> None:
                """Set stop and guarantee delivery of the end-of-audio sentinel.

                Called from the event-loop side by :func:`stream_from_thread` when
                the consumer closes the generator early.  Spawns a daemon thread to
                do a *blocking* ``frames.put`` so the sentinel is never lost even
                when the queue is at capacity.  The thread is daemon so it does not
                prevent interpreter exit if the process dies while the queue is full.
                """
                stop.set()
                threading.Thread(
                    target=frames.put,
                    args=(_END_OF_AUDIO,),
                    daemon=True,
                    name="hermes-voip-stt-cancel",
                ).start()

            try:
                async for transcript in stream_from_thread(
                    lambda: self._decode_run(frames),
                    max_buffer=_BUFFER,
                    on_cancel=_on_cancel,
                ):
                    yield transcript
            finally:
                stop.set()
                # Best-effort put in case _on_cancel was not called (normal path:
                # audio exhausted → feeder put sentinel → worker returned cleanly).
                with contextlib.suppress(queue.Full):
                    frames.put_nowait(_END_OF_AUDIO)
                feeder.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await feeder

        return _run()


async def _feed(
    audio: AsyncIterator[PcmFrame],
    frames: queue.Queue[_FrameItem],
    stop: threading.Event,
) -> None:
    """Pump float32 frames from the async inbound iterator into the sync queue.

    Runs as a loop task so the worker thread can ``queue.get`` blocking. On
    completion it enqueues the end-of-audio sentinel so the decoder flushes its
    tail and returns. ``queue.put`` runs in a thread so a full queue back-pressures
    without blocking the loop.
    """
    try:
        async for frame in audio:
            if stop.is_set():
                return
            samples = pcm16_to_float32(frame.samples)
            await asyncio.to_thread(frames.put, samples)
    finally:
        if not stop.is_set():
            await asyncio.to_thread(frames.put, _END_OF_AUDIO)


class _Decoder[StreamT]:
    """The blocking sherpa decode loop, run on a worker thread.

    Reads float32 frames from the queue, feeds the recogniser, and yields a
    ``Transcript`` whenever the stable hypothesis grows (interim) or the engine
    endpoints (final, then reset). On the end-of-audio sentinel it finishes input
    and drains a final hypothesis so trailing speech is never lost. Generic over
    the recogniser's stream handle type so it threads a precisely-typed stream.
    """

    def __init__(self, recognizer: _Recognizer[StreamT]) -> None:
        self._recognizer = recognizer

    def run(self, frames: queue.Queue[_FrameItem]) -> Iterator[Transcript]:
        recognizer = self._recognizer
        stream = recognizer.create_stream()
        last_emitted = ""
        while True:
            item = frames.get()
            if isinstance(item, _EndOfAudio):
                yield from self._flush(stream)
                return
            recognizer.accept_waveform(stream, RECOGNISER_SAMPLE_RATE, item)
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
            text = recognizer.get_result(stream)
            if recognizer.is_endpoint(stream):
                if text:
                    yield _transcript(text, is_final=True)
                recognizer.reset(stream)
                last_emitted = ""
            elif text and text != last_emitted:
                last_emitted = text
                yield _transcript(text, is_final=False)

    def _flush(self, stream: StreamT) -> Iterator[Transcript]:
        """Finish input and emit a final transcript for any pending hypothesis."""
        recognizer = self._recognizer
        recognizer.input_finished(stream)
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        text = recognizer.get_result(stream)
        if text:
            # Promote the trailing hypothesis to final so the caller does not lose
            # speech that never reached an engine endpoint (end-of-call flush).
            yield _transcript(text, is_final=True)


def _transcript(text: str, *, is_final: bool) -> Transcript:
    """A ``Transcript`` with the turn boundary deferred to ADR-0008 (always False)."""
    return Transcript(
        text=text,
        is_final=is_final,
        end_of_turn=False,
        confidence=_FULL_CONFIDENCE,
    )


def _build_recognizer(model_dir: str) -> _Recognizer[_SherpaOnlineStream]:
    """Construct the real sherpa-onnx streaming recogniser from ``model_dir``.

    ``sherpa_onnx`` (the optional ``ml`` extra) is loaded lazily via
    :func:`importlib.import_module` — bound to the :class:`_SherpaModule` Protocol
    that describes only the surface used — so the package imports without the extra
    and ``mypy --strict`` stays clean in both envs (no stub-bearing import, no
    ``# type: ignore``). It is loaded only after :func:`ensure_sherpa_loadable`
    has made the shared ``onnxruntime`` library resolvable. The returned adapter
    satisfies :class:`_Recognizer` structurally.
    """
    ensure_sherpa_loadable()
    sherpa: _SherpaModule = importlib.import_module("sherpa_onnx")

    base = Path(model_dir)
    recognizer = sherpa.OnlineRecognizer.from_transducer(
        tokens=str(base / "tokens.txt"),
        encoder=str(base / "encoder.onnx"),
        decoder=str(base / "decoder.onnx"),
        joiner=str(base / "joiner.onnx"),
        num_threads=1,
        sample_rate=RECOGNISER_SAMPLE_RATE,
        feature_dim=80,
        enable_endpoint_detection=True,
    )
    return _SherpaRecognizerAdapter(recognizer)


class _OnlineRecognizerFactory(Protocol):
    """Structural view of ``sherpa_onnx.OnlineRecognizer`` as a factory namespace.

    Only the ``from_transducer`` constructor we call is described; it returns the
    structural :class:`_SherpaOnlineRecognizer`. Modelled as an attribute Protocol
    (``OnlineRecognizer`` is a *class object* on the module) so the lazily-imported
    module binds to :class:`_SherpaModule` without importing sherpa's types.
    """

    def from_transducer(  # noqa: PLR0913 - mirrors sherpa_onnx.OnlineRecognizer.from_transducer arity exactly; all keyword-only
        self,
        *,
        tokens: str,
        encoder: str,
        decoder: str,
        joiner: str,
        num_threads: int,
        sample_rate: int,
        feature_dim: int,
        enable_endpoint_detection: bool,
    ) -> _SherpaOnlineRecognizer: ...


class _SherpaModule(Protocol):
    """The single ``sherpa_onnx`` module attribute this provider uses."""

    OnlineRecognizer: _OnlineRecognizerFactory


class _SherpaOnlineStream(Protocol):
    """Structural view of the sherpa ``OnlineStream`` methods the adapter calls."""

    def accept_waveform(self, sample_rate: int, samples: FloatArray) -> None: ...

    def input_finished(self) -> None: ...


class _SherpaOnlineRecognizer(Protocol):
    """Structural view of the ``sherpa_onnx.OnlineRecognizer`` parts we call."""

    def create_stream(self) -> _SherpaOnlineStream: ...

    def is_ready(self, stream: _SherpaOnlineStream) -> bool: ...

    def decode_stream(self, stream: _SherpaOnlineStream) -> None: ...

    def get_result(self, stream: _SherpaOnlineStream) -> str: ...

    def is_endpoint(self, stream: _SherpaOnlineStream) -> bool: ...

    def reset(self, stream: _SherpaOnlineStream) -> None: ...


class _SherpaRecognizerAdapter:
    """Adapt the real ``sherpa_onnx.OnlineRecognizer`` to ``_Recognizer``.

    ``accept_waveform`` / ``input_finished`` are methods on the sherpa *stream*,
    not the recogniser; this adapter normalises them onto the uniform
    recogniser-centric ``_Recognizer`` surface the decoder drives (and that the
    test fake mirrors). The native sherpa ``OnlineStream`` *is* the stream handle
    threaded through — no extra mapping — so the adapter satisfies
    ``_Recognizer[_SherpaOnlineStream]``.
    """

    def __init__(self, recognizer: _SherpaOnlineRecognizer) -> None:
        self._r = recognizer

    def create_stream(self) -> _SherpaOnlineStream:
        return self._r.create_stream()

    def accept_waveform(
        self, stream: _SherpaOnlineStream, sample_rate: int, samples: FloatArray
    ) -> None:
        stream.accept_waveform(sample_rate, samples)

    def is_ready(self, stream: _SherpaOnlineStream) -> bool:
        return self._r.is_ready(stream)

    def decode_stream(self, stream: _SherpaOnlineStream) -> None:
        self._r.decode_stream(stream)

    def get_result(self, stream: _SherpaOnlineStream) -> str:
        return self._r.get_result(stream)

    def is_endpoint(self, stream: _SherpaOnlineStream) -> bool:
        return self._r.is_endpoint(stream)

    def reset(self, stream: _SherpaOnlineStream) -> None:
        self._r.reset(stream)

    def input_finished(self, stream: _SherpaOnlineStream) -> None:
        stream.input_finished()
