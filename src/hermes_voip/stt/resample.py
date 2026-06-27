"""STT media glue: PCM16<->float32 conversion and 8 kHz->16 kHz upsampling.

The transport delivers narrowband 8 kHz G.711 (ADR-0005); the streaming
zipformer recogniser wants **16 kHz** float32 in ``-1..1`` (ADR-0006). The G.711
decode and the continuous, state-carrying resample already live canonically in
:mod:`hermes_voip.media.audio` (the ``Resampler`` and codec helpers) — this
module does **not** redefine them. It adds only the two pieces specific to the
recogniser seam:

* :func:`pcm16_to_float32` / :func:`float32_to_pcm16` — the normalised-float32
  conversion sherpa-onnx's ``accept_waveform`` requires (it wants float32, not
  PCM16 bytes), and its inverse for completeness;
* :class:`FrameUpsampler` — a thin :class:`PcmFrame`-aware wrapper over
  :class:`hermes_voip.media.audio.Resampler` that upsamples one continuous 8 kHz
  stream to 16 kHz, carrying resample state across frames so the streamed result
  is identical to a single pass (no per-frame boundary clicks).
"""

from __future__ import annotations

import importlib
from typing import Final, Protocol

from hermes_voip.media.audio import G711_SAMPLE_RATE, Resampler
from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame

__all__ = [
    "RECOGNISER_SAMPLE_RATE",
    "FloatArray",
    "FrameUpsampler",
    "float32_to_pcm16",
    "pcm16_to_float32",
]

#: The recogniser's required input rate; the media layer resamples 8 kHz to this.
RECOGNISER_SAMPLE_RATE: Final[int] = 16_000

# PCM16 full-scale: int16 spans -32768..32767, so dividing by 32768 maps every
# sample into [-1.0, 1.0) (and -32768 -> exactly -1.0). This is the convention
# sherpa-onnx (and most ONNX audio front-ends) expect for accept_waveform.
_PCM16_FULL_SCALE: Final[float] = 32_768.0
_INT16_MIN: Final[int] = -32_768
_INT16_MAX: Final[int] = 32_767


class FloatArray(Protocol):
    """Structural view of the 1-D numpy float32 buffer the recogniser is fed.

    ``numpy`` lives in the optional ``ml`` extra, so this module never imports it
    statically — it would break ``import hermes_voip.stt`` on the default install
    and ``mypy --strict`` in the no-ml gate. Typing the array structurally (only
    the few ndarray members used) keeps both clean with no ``Any`` and no
    ``# type: ignore``; the real value is a ``numpy.ndarray`` produced on the
    ml-present media path. The bridge to sherpa-onnx (``accept_waveform``) accepts
    this object directly.
    """

    def astype(self, dtype: object) -> FloatArray:
        """Return a copy cast to ``dtype`` (``float32`` or the ``<i2`` wire type)."""
        ...

    def tobytes(self) -> bytes:
        """Return the array's raw bytes (the PCM16-LE buffer for the wire)."""
        ...

    def __truediv__(self, other: float) -> FloatArray:
        """Elementwise division by a scalar (the PCM16 full-scale divisor)."""
        ...

    def __mul__(self, other: float) -> FloatArray:
        """Elementwise multiplication by a scalar (the PCM16 full-scale factor)."""
        ...

    def __len__(self) -> int:
        """Return the number of elements (samples) in the array.

        Exposes the length surface so callers can count samples with ``len(arr)``
        instead of ``len(arr.tobytes()) // 4``, avoiding a temporary bytes
        allocation per frame (~1280 bytes per 20 ms/16 kHz frame, ~64 KB/s/call).
        ``numpy.ndarray`` already implements ``__len__``, so no runtime shim is
        needed; the addition is a Protocol-level annotation only.
        """
        ...


class _NumpyModule(Protocol):
    """The ``numpy`` surface this module uses, bound to the lazily-imported module."""

    float32: object

    def frombuffer(self, buffer: bytes, dtype: str) -> FloatArray: ...

    def rint(self, x: FloatArray) -> FloatArray: ...

    def clip(self, a: FloatArray, a_min: int, a_max: int) -> FloatArray: ...


def _numpy() -> _NumpyModule:
    """Load ``numpy`` lazily (optional ``ml`` extra) bound to :class:`_NumpyModule`.

    Imported via :func:`importlib.import_module` so the package imports without the
    ``ml`` extra and ``mypy --strict`` stays clean in both envs (no stub-bearing
    static import, no ``# type: ignore``). Callers are on the ml-present media path.
    """
    module: _NumpyModule = importlib.import_module("numpy")
    return module


def pcm16_to_float32(pcm16: bytes) -> FloatArray:
    """Decode PCM16-LE mono bytes to a normalised float32 array in ``-1..1``.

    Each signed 16-bit sample is divided by 32768, so the recogniser receives the
    float32 it expects rather than raw PCM16 bytes (ADR-0006).

    Raises:
        ValueError: If ``pcm16`` is not a whole number of 16-bit samples.
    """
    if len(pcm16) % PCM16_BYTES_PER_SAMPLE != 0:
        msg = f"PCM16 buffer must be whole 16-bit samples, got {len(pcm16)} bytes"
        raise ValueError(msg)
    np = _numpy()
    as_int16 = np.frombuffer(pcm16, dtype="<i2")
    return (as_int16.astype(np.float32) / _PCM16_FULL_SCALE).astype(np.float32)


def float32_to_pcm16(samples: FloatArray) -> bytes:
    """Encode a normalised float32 array back to PCM16-LE mono bytes.

    The inverse of :func:`pcm16_to_float32`. Values outside ``-1..1`` **saturate**
    at the int16 limits (they never wrap), so a synthesiser or gain stage that
    overshoots clips cleanly instead of producing a wrap-around artefact.
    """
    np = _numpy()
    scaled = np.rint(samples.astype(np.float32) * _PCM16_FULL_SCALE)
    clamped = np.clip(scaled, _INT16_MIN, _INT16_MAX)
    return clamped.astype("<i2").tobytes()


class FrameUpsampler:
    """Continuous 8 kHz -> 16 kHz upsampler over a stream of :class:`PcmFrame`.

    Wraps the canonical :class:`hermes_voip.media.audio.Resampler` (which carries
    ``audioop.ratecv`` state across calls) so feeding frames one at a time yields
    byte-identical output to resampling their concatenation in one pass. One
    upsampler belongs to one inbound direction of one call; construct a fresh one
    per call (or :meth:`reset`).
    """

    def __init__(self) -> None:
        """Create an upsampler fixed at the G.711 wire rate -> recogniser rate."""
        self._resampler = Resampler(G711_SAMPLE_RATE, RECOGNISER_SAMPLE_RATE)

    @property
    def target_sample_rate(self) -> int:
        """The output rate (16 kHz) — the recogniser's required input rate."""
        return RECOGNISER_SAMPLE_RATE

    def upsample(self, frame: PcmFrame) -> PcmFrame:
        """Upsample one 8 kHz frame to 16 kHz, carrying resample state forward.

        The output frame keeps the source's ``monotonic_ts_ns`` (the de-jittered
        presentation clock is preserved across the rate change) and is stamped at
        :data:`RECOGNISER_SAMPLE_RATE`.

        Raises:
            ValueError: If ``frame.sample_rate`` is not 8 kHz (the upsampler is
                fixed to the G.711 wire rate; another rate would mis-resample).
        """
        if frame.sample_rate != G711_SAMPLE_RATE:
            msg = (
                f"FrameUpsampler requires {G711_SAMPLE_RATE} Hz input, "
                f"got {frame.sample_rate} Hz"
            )
            raise ValueError(msg)
        return PcmFrame(
            samples=self._resampler.resample(frame.samples),
            sample_rate=RECOGNISER_SAMPLE_RATE,
            monotonic_ts_ns=frame.monotonic_ts_ns,
        )

    def reset(self) -> None:
        """Discard carried resample state so the next frame starts a fresh stream."""
        self._resampler.reset()
