"""Self-hosted Kokoro-82M streaming TTS (default provider, ADR-0007).

``SherpaKokoroTTS`` is the default ``StreamingTTS`` (ADR-0004): it drives the
sherpa-onnx runtime with the Apache-2.0 Kokoro-82M voice model, synthesising the
agent's text **per sentence** and emitting 24 kHz ``PcmFrame``s. The 24->8 kHz
downsample and G.711 encode are the media layer's job (ADR-0005), never this
provider's output type.

Barge-in (ADR-0008) maps to sherpa-onnx's streaming chunk callback: returning a
stop value from it cancels synthesis at a chunk boundary. The blocking, native
synthesis loop runs on a worker thread bridged to the event loop
(:func:`hermes_voip.aio.stream_from_thread`), so the loop is never blocked.

The synthesiser backend is behind the :class:`Synthesizer` seam and is
**dependency-injected**, so the streaming/segmentation/cancel machinery is tested
with a fake (no model, no numpy); the real sherpa-onnx backend is built from a
model directory and exercised by a separate, weight-gated smoke test.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Protocol, runtime_checkable

from hermes_voip.aio import stream_from_thread
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.tts._stream import PcmFrameStream, SegmentSource

__all__ = [
    "KOKORO_SAMPLE_RATE",
    "SherpaKokoroTTS",
    "Synthesizer",
    "pcm16_from_float32",
]

#: Kokoro-82M emits 24 kHz mono audio; the media layer downsamples to 8 kHz for
#: G.711 (ADR-0005/0007). This is the provider's declared ``output_sample_rate``.
KOKORO_SAMPLE_RATE = 24_000


@runtime_checkable
class Synthesizer(Protocol):
    """The blocking, per-sentence synthesis backend behind ``SherpaKokoroTTS``.

    One call synthesises one sentence, yielding PCM16-LE mono chunks at the
    provider's output rate. It must poll ``stop`` cooperatively and return
    promptly once it is true — that is the barge-in primitive (for the real
    backend, ``stop`` drives the sherpa-onnx chunk callback's return value).
    """

    def synthesize(self, text: str, stop: Callable[[], bool]) -> Iterator[bytes]:
        """Yield PCM16-LE chunks for ``text``; stop early when ``stop()`` is true."""
        ...


class SherpaKokoroTTS:
    """Default streaming TTS: sherpa-onnx + Kokoro-82M (StreamingTTS, ADR-0004).

    Emits 24 kHz ``PcmFrame``s; ``cancel()`` on the returned stream stops
    synthesis mid-utterance for barge-in. Construct with a ``model_dir`` for the
    real engine, or inject a ``synthesizer_factory`` (used by tests).
    """

    def __init__(
        self,
        *,
        model_dir: str | None = None,
        synthesizer_factory: Callable[[], Synthesizer] | None = None,
        voice: str = "",
        speed: float = 1.0,
    ) -> None:
        """Create the provider from a model directory or an injected backend.

        Exactly one of ``model_dir`` or ``synthesizer_factory`` must be given.

        Args:
            model_dir: Directory with the pinned Kokoro artifacts (``model.onnx``,
                ``voices.bin``, ``tokens.txt`` and its espeak data). Builds the
                real sherpa-onnx backend.
            synthesizer_factory: A zero-arg factory returning a :class:`Synthesizer`
                (dependency injection for tests / alternate backends).
            voice: Default voice when ``synthesize`` is called with an empty
                ``voice``. Kokoro selects a speaker by integer id, so a numeric
                voice (e.g. ``"0"``) is the speaker id and a non-numeric name
                falls back to speaker ``0`` (real backend; the injected backend
                ignores voice).
            speed: Synthesis speed multiplier for the real backend.

        Raises:
            ValueError: If neither or both of ``model_dir`` / ``synthesizer_factory``
                are provided.
        """
        if (model_dir is None) == (synthesizer_factory is None):
            msg = "provide exactly one of model_dir or synthesizer_factory"
            raise ValueError(msg)
        self._default_voice = voice
        self._injected_factory = synthesizer_factory
        self._model_dir = model_dir
        self._speed = speed

    @property
    def output_sample_rate(self) -> int:
        """24 kHz: the media layer downsamples to 8 kHz for G.711 (ADR-0005)."""
        return KOKORO_SAMPLE_RATE

    def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
        """Stream agent text in, stream 24 kHz ``PcmFrame``s out (ADR-0004).

        Returns a ``TtsStream``: an async iterator of frames that also exposes
        ``flush()`` and ``cancel()``. The engine begins emitting audio before
        ``text`` completes (synthesis starts at the first completed sentence).
        ``voice`` overrides the construction default when non-empty.
        """
        synthesizer = self._backend(voice or self._default_voice)
        stop = threading.Event()

        def _open(sentence: str) -> SegmentSource:
            # The backend polls ``stop`` directly (the barge-in predicate) and
            # returns promptly once it is set, so the segment needs no forcible
            # ``abort`` — synthesis is never parked in an uninterruptible read.
            chunks = stream_from_thread(
                lambda: synthesizer.synthesize(sentence, stop.is_set)
            )
            return SegmentSource(chunks=chunks)

        return PcmFrameStream(
            text=text,
            open_segment=_open,
            sample_rate=KOKORO_SAMPLE_RATE,
            stop=stop,
        )

    def _backend(self, voice: str) -> Synthesizer:
        """Build the synthesis backend for ``voice`` (the injected one ignores it).

        Branches on ``model_dir`` (not the factory) so the real-backend path also
        narrows ``model_dir`` to non-``None`` for the type checker without an
        ``assert``; ``__init__``'s XOR guarantees the factory is set otherwise.
        """
        if self._model_dir is not None:
            return _SherpaSynthesizer(self._model_dir, self._speed, _voice_sid(voice))
        if self._injected_factory is None:  # unreachable: __init__ enforces the XOR
            msg = "no synthesis backend configured"
            raise RuntimeError(msg)
        return self._injected_factory()


def _voice_sid(voice: str) -> int:
    """Resolve a voice id to a Kokoro speaker id; non-numeric falls back to 0."""
    stripped = voice.strip()
    return int(stripped) if stripped.isdigit() else 0


# --- narrow typed surfaces over the optional ml stack -----------------------
#
# ``numpy`` and ``sherpa_onnx`` are in the optional ``ml`` extra, absent in the
# default install (and its ``mypy`` gate). Rather than a static ``import`` (which
# trips ``import-not-found`` in the no-ml gate) plus ``# type: ignore`` on every
# dynamic call, these Protocols describe the *exact* members this backend uses;
# the lazy resolvers below bind ``importlib.import_module(...)`` to a
# Protocol-typed handle. mypy stays clean under both the no-ml gate and the ml
# env — no ``Any``, no ``cast``, no ``# type: ignore`` (AGENTS.md rules 17/39).


class _NdArray(Protocol):
    """The numpy ``ndarray`` surface used by the PCM16 conversion."""

    def __mul__(self, other: float) -> _NdArray: ...
    def astype(self, dtype: str) -> _NdArray: ...
    def tobytes(self) -> bytes: ...


class _Numpy(Protocol):
    """The numpy module surface used by the PCM16 conversion."""

    @property
    def float32(self) -> object: ...
    def asarray(self, a: object, dtype: object) -> _NdArray: ...
    def clip(self, a: _NdArray, a_min: float, a_max: float) -> _NdArray: ...


class _OfflineTts(Protocol):
    """The sherpa-onnx ``OfflineTts`` instance surface used here."""

    def generate(
        self,
        text: str,
        sid: int,
        speed: float,
        callback: Callable[[object, float], int],
    ) -> object: ...


class _SherpaOnnx(Protocol):
    """The sherpa-onnx module surface used to build the Kokoro offline-TTS."""

    def OfflineTts(self, config: object) -> _OfflineTts: ...  # noqa: N802 - sherpa name
    def OfflineTtsConfig(  # noqa: N802 - mirrors the sherpa-onnx class name
        self, *, model: object, max_num_sentences: int
    ) -> object: ...
    def OfflineTtsModelConfig(  # noqa: N802 - mirrors the sherpa-onnx class name
        self, *, kokoro: object, num_threads: int
    ) -> object: ...
    def OfflineTtsKokoroModelConfig(  # noqa: N802 - mirrors the sherpa-onnx name
        self, *, model: str, voices: str, tokens: str, data_dir: str
    ) -> object: ...


def _load_numpy() -> _Numpy:
    """Import numpy lazily, typed to the narrow surface this backend uses."""
    module: _Numpy = importlib.import_module("numpy")
    return module


def _load_sherpa_onnx() -> _SherpaOnnx:
    """Import sherpa-onnx lazily, typed to the narrow surface used here."""
    module: _SherpaOnnx = importlib.import_module("sherpa_onnx")
    return module


#: Negative full-scale PCM16: int16's range is asymmetric (-32768..32767), so a
#: float ``-1.0`` maps to ``-32768`` using the negative full-scale code. Scaling
#: by 32768 (not 32767) maps both signs to full scale; the clamp below keeps the
#: top end at ``32767`` and saturates anything past either rail (no int16 wrap).
_FLOAT_TO_INT16_SCALE = 32_768.0
_INT16_MIN = -32_768.0
_INT16_MAX = 32_767.0


def pcm16_from_float32(samples: object) -> bytes:
    """Convert float32 audio in ``[-1.0, 1.0]`` to full-scale PCM16-LE bytes.

    The model emits float32 samples (sherpa-onnx hands them in as a numpy array);
    this maps them to signed 16-bit little-endian PCM at full scale. ``-1.0`` maps
    to ``-32768`` (PCM16 negative full scale) and ``+1.0`` to ``+32767``. Values
    outside the range **saturate** to the int16 endpoints rather than wrapping:
    the scale-then-clamp order means e.g. ``2.0`` becomes ``32767``, never an
    overflowed near-zero value. numpy is resolved lazily (the ``ml`` extra), so
    importing this module never requires numpy.

    Args:
        samples: A float32 numpy array (or array-like) of samples in ``[-1, 1]``.

    Returns:
        The samples as signed 16-bit little-endian PCM bytes (2 bytes/sample).
    """
    np = _load_numpy()
    scaled = np.asarray(samples, dtype=np.float32) * _FLOAT_TO_INT16_SCALE
    clamped = np.clip(scaled, _INT16_MIN, _INT16_MAX)
    return clamped.astype("<i2").tobytes()


class _SherpaSynthesizer:
    """The real sherpa-onnx + Kokoro backend (built only when weights exist).

    Imports ``sherpa_onnx``/``numpy`` lazily via the narrow Protocol resolvers —
    the default install lacks the ``ml`` extra, so importing this module must not
    require them. Construction loads the offline-TTS model once; each
    :meth:`synthesize` call runs one sentence through ``generate`` with a
    streaming chunk callback whose return value (``1`` keep / ``0`` stop) is
    driven by the ``stop`` predicate.
    """

    def __init__(self, model_dir: str, speed: float, sid: int) -> None:
        from pathlib import Path  # noqa: PLC0415 - lazy, real-backend path only

        from hermes_voip.providers.onnx_compat import (  # noqa: PLC0415 - lazy import
            ensure_sherpa_loadable,
        )

        ensure_sherpa_loadable()
        sherpa_onnx = _load_sherpa_onnx()

        base = Path(model_dir)
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(base / "model.onnx"),
                    voices=str(base / "voices.bin"),
                    tokens=str(base / "tokens.txt"),
                    data_dir=str(base / "espeak-ng-data"),
                ),
                num_threads=1,
            ),
            max_num_sentences=1,
        )
        self._tts = sherpa_onnx.OfflineTts(config)
        self._speed = speed
        self._sid = sid

    def synthesize(self, text: str, stop: Callable[[], bool]) -> Iterator[bytes]:
        """Synthesise ``text``, yielding PCM16-LE chunks until done or stopped."""
        import queue  # noqa: PLC0415 - lazy import, real-backend path only

        chunks: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()

        def _on_chunk(samples: object, _progress: float) -> int:
            if stop():
                return 0  # sherpa-onnx stop: cancel synthesis at this boundary
            chunks.put(pcm16_from_float32(samples))
            return 1  # keep generating

        # generate() blocks until synthesis finishes, invoking _on_chunk inline;
        # it returns on the worker thread this Synthesizer runs on, so draining
        # the queue afterwards yields every chunk produced before a stop.
        self._tts.generate(text, sid=self._sid, speed=self._speed, callback=_on_chunk)
        chunks.put(None)
        while (item := chunks.get()) is not None:
            yield item


# Structural conformance to the ADR-0004 seam, enforced by mypy and the
# runtime_checkable Protocol (mirrors the assertion in ADR-0007). The real
# backend's conformance to the injected Synthesizer seam is asserted too.
_: type[StreamingTTS] = SherpaKokoroTTS
_backend: type[Synthesizer] = _SherpaSynthesizer
