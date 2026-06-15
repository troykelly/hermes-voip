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

from typing import Final

import numpy as np

from hermes_voip.media.audio import G711_SAMPLE_RATE, Resampler
from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame

__all__ = [
    "RECOGNISER_SAMPLE_RATE",
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

# A 1-D float32 audio buffer (mono samples in -1..1) and its int16 counterpart.
type _Float32Array = np.ndarray[tuple[int], np.dtype[np.float32]]
type _Int16Array = np.ndarray[tuple[int], np.dtype[np.int16]]


def pcm16_to_float32(pcm16: bytes) -> _Float32Array:
    """Decode PCM16-LE mono bytes to a normalised float32 array in ``-1..1``.

    Each signed 16-bit sample is divided by 32768, so the recogniser receives the
    float32 it expects rather than raw PCM16 bytes (ADR-0006).

    Raises:
        ValueError: If ``pcm16`` is not a whole number of 16-bit samples.
    """
    if len(pcm16) % PCM16_BYTES_PER_SAMPLE != 0:
        msg = f"PCM16 buffer must be whole 16-bit samples, got {len(pcm16)} bytes"
        raise ValueError(msg)
    as_int16: _Int16Array = np.frombuffer(pcm16, dtype="<i2")
    normalised: _Float32Array = (
        as_int16.astype(np.float32) / _PCM16_FULL_SCALE
    ).astype(np.float32)
    return normalised


def float32_to_pcm16(samples: _Float32Array) -> bytes:
    """Encode a normalised float32 array back to PCM16-LE mono bytes.

    The inverse of :func:`pcm16_to_float32`. Values outside ``-1..1`` **saturate**
    at the int16 limits (they never wrap), so a synthesiser or gain stage that
    overshoots clips cleanly instead of producing a wrap-around artefact.
    """
    scaled = np.rint(samples.astype(np.float32) * _PCM16_FULL_SCALE)
    clamped = np.clip(scaled, _INT16_MIN, _INT16_MAX)
    as_int16: _Int16Array = clamped.astype("<i2")
    return as_int16.tobytes()


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
