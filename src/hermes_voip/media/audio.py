"""G.711 codec and sample-rate conversion (ADR-0004/0005).

The telephony wire is G.711 (mu-law/a-law, 8 kHz, one byte per sample); the
recognisers want 16 kHz and the synthesisers emit 24/16 kHz. This module owns
both conversions so every provider boundary speaks one currency: PCM16 frames
at a declared rate (ADR-0004).

It wraps ``audioop`` (the ``audioop-lts`` backport, since stdlib ``audioop`` is
removed in Python 3.13) — a typed, battle-tested C codec.

:func:`generate_tone_frames` generates a pure-sine diagnostic tone directly at
the G.711 wire rate (8 kHz), chunked into 20 ms :class:`PcmFrame` objects.
Because the frames are already at 8 kHz they bypass the TTS + resample path
entirely when fed to
:meth:`~hermes_voip.media.engine.RtpMediaTransport.send_audio`, letting an
operator isolate whether silence is in the encode/RTP layer or in the
TTS/resample layer (``HERMES_VOIP_TEST_TONE`` env var).
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator
from typing import Final

import audioop

from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame

_MONO: int = 1

#: The fixed sample rate of telephony G.711 (mu-law/a-law) narrowband audio.
G711_SAMPLE_RATE: Final[int] = 8000

# audioop.ratecv's resumable conversion state (or None to start a fresh stream).
type _RateState = tuple[int, tuple[tuple[int, int], ...]] | None


def _validate_pcm16(pcm16: bytes) -> None:
    """Raise if ``pcm16`` is not a whole number of 16-bit samples."""
    if len(pcm16) % PCM16_BYTES_PER_SAMPLE != 0:
        msg = f"PCM16 buffer must be whole 16-bit samples, got {len(pcm16)} bytes"
        raise ValueError(msg)


def encode_ulaw(pcm16: bytes) -> bytes:
    """Encode PCM16-LE mono to G.711 mu-law (one byte per sample)."""
    _validate_pcm16(pcm16)
    return audioop.lin2ulaw(pcm16, PCM16_BYTES_PER_SAMPLE)


def decode_ulaw(ulaw: bytes) -> bytes:
    """Decode G.711 mu-law to PCM16-LE mono (two bytes per sample)."""
    return audioop.ulaw2lin(ulaw, PCM16_BYTES_PER_SAMPLE)


def encode_alaw(pcm16: bytes) -> bytes:
    """Encode PCM16-LE mono to G.711 a-law (one byte per sample)."""
    _validate_pcm16(pcm16)
    return audioop.lin2alaw(pcm16, PCM16_BYTES_PER_SAMPLE)


def decode_alaw(alaw: bytes) -> bytes:
    """Decode G.711 a-law to PCM16-LE mono (two bytes per sample)."""
    return audioop.alaw2lin(alaw, PCM16_BYTES_PER_SAMPLE)


def ulaw_to_frame(ulaw: bytes, *, monotonic_ts_ns: int) -> PcmFrame:
    """Decode a mu-law payload into a :class:`PcmFrame`.

    G.711 is intrinsically 8 kHz, so the frame is always stamped at
    ``G711_SAMPLE_RATE``; resampling to the recogniser rate is a separate step.
    """
    return PcmFrame(
        samples=decode_ulaw(ulaw),
        sample_rate=G711_SAMPLE_RATE,
        monotonic_ts_ns=monotonic_ts_ns,
    )


def _require_8k(frame: PcmFrame) -> None:
    """Raise if ``frame`` is not at the G.711 wire rate.

    Encoding a wider-band frame to G.711 would silently change its duration, so
    the caller must resample to 8 kHz first.
    """
    if frame.sample_rate != G711_SAMPLE_RATE:
        msg = f"G.711 requires {G711_SAMPLE_RATE} Hz, got {frame.sample_rate} Hz"
        raise ValueError(msg)


def frame_to_ulaw(frame: PcmFrame) -> bytes:
    """Encode an 8 kHz :class:`PcmFrame` to a mu-law (PCMU) payload.

    Raises:
        ValueError: If ``frame.sample_rate`` is not ``G711_SAMPLE_RATE``.
    """
    _require_8k(frame)
    return encode_ulaw(frame.samples)


def alaw_to_frame(alaw: bytes, *, monotonic_ts_ns: int) -> PcmFrame:
    """Decode an a-law (PCMA) payload into a :class:`PcmFrame`.

    G.711 is intrinsically 8 kHz, so the frame is always stamped at
    ``G711_SAMPLE_RATE``; resampling to the recogniser rate is a separate step.
    """
    return PcmFrame(
        samples=decode_alaw(alaw),
        sample_rate=G711_SAMPLE_RATE,
        monotonic_ts_ns=monotonic_ts_ns,
    )


def frame_to_alaw(frame: PcmFrame) -> bytes:
    """Encode an 8 kHz :class:`PcmFrame` to an a-law (PCMA) payload.

    Raises:
        ValueError: If ``frame.sample_rate`` is not ``G711_SAMPLE_RATE``.
    """
    _require_8k(frame)
    return encode_alaw(frame.samples)


def linear_fade_out(pcm16: bytes, *, fade_samples: int) -> bytes:
    """Return ``pcm16`` with its final ``fade_samples`` linearly ramped to silence.

    Applies a linear gain ramp from full (``1.0``) down to ``0.0`` across the LAST
    ``fade_samples`` PCM16 samples, leaving everything before that window
    untouched. The ramp brings the very last sample to exactly ``0`` so a hard cut
    immediately after has no residual step to click/pop — this is the click-free
    barge-in tail (ADR-0028). It operates purely in the linear PCM16 domain, so it
    is codec-agnostic: callers fade BEFORE encoding to G.711 or G.722.

    The gain for the ``k``-th sample of an ``L``-sample fade window (``k`` counting
    ``0 .. L-1`` from the window start) is ``(L - 1 - k) / L`` — i.e. the first
    faded sample keeps ``(L-1)/L`` of its amplitude and the last keeps ``0``.

    Args:
        pcm16: PCM16-LE mono bytes (a whole number of 16-bit samples).
        fade_samples: The number of trailing samples to ramp down. ``0`` returns
            ``pcm16`` unchanged (no fade). A value larger than the sample count is
            clamped to fade the whole buffer.

    Returns:
        A new PCM16-LE buffer of the SAME length, with the trailing window faded.

    Raises:
        ValueError: If ``pcm16`` is not a whole number of 16-bit samples, or
            ``fade_samples`` is negative.
    """
    _validate_pcm16(pcm16)
    if fade_samples < 0:
        msg = f"fade_samples must be non-negative, got {fade_samples}"
        raise ValueError(msg)
    if fade_samples == 0 or not pcm16:
        return pcm16

    total = len(pcm16) // PCM16_BYTES_PER_SAMPLE
    window = min(fade_samples, total)
    samples = list(struct.unpack(f"<{total}h", pcm16))
    start = total - window
    for k in range(window):
        # Gain (L-1-k)/L as integer math: scale the sample by the numerator and
        # divide by L, so the last sample (k == window-1) is exactly zero. Round
        # toward zero (int() truncation) — the residual is <1 LSB, inaudible.
        numerator = window - 1 - k
        samples[start + k] = samples[start + k] * numerator // window
    return struct.pack(f"<{total}h", *samples)


class Resampler:
    """Stateful PCM16 sample-rate converter for one continuous stream.

    The conversion state (sub-sample phase) is carried across calls so feeding a
    stream frame-by-frame produces exactly the same audio as one pass — without
    the clicks a stateless per-frame conversion would introduce at boundaries.
    One ``Resampler`` belongs to one direction of one call; ``reset()`` starts a
    fresh stream.
    """

    def __init__(self, from_rate: int, to_rate: int) -> None:
        """Create a converter from ``from_rate`` to ``to_rate``.

        Both rates must be plain ``int`` values (not ``bool``) and must be
        positive and differ from each other.

        Validating type and sign here fails a config-derived bad rate at
        construction with ``ValueError`` rather than letting ``audioop.ratecv``
        raise a ``TypeError`` ('float' object cannot be interpreted as an integer)
        deep inside the C layer on the first ``resample`` call mid-stream. ``bool``
        is rejected explicitly because ``isinstance(True, int)`` is ``True`` in
        Python; a caller passing ``True``/``False`` as a rate is a programming
        error, not a valid 1 Hz or 0 Hz rate.

        Raises:
            ValueError: If either rate is a ``bool``, not an ``int``, not
                positive, or the rates are equal.
        """
        if isinstance(from_rate, bool) or not isinstance(from_rate, int):
            msg = (
                f"sample rates must be a plain integer, got from_rate={from_rate!r}"
                f" ({type(from_rate).__name__})"
            )
            raise ValueError(msg)  # noqa: TRY004 — module contract is ValueError throughout
        if isinstance(to_rate, bool) or not isinstance(to_rate, int):
            msg = (
                f"sample rates must be a plain integer, got to_rate={to_rate!r}"
                f" ({type(to_rate).__name__})"
            )
            raise ValueError(msg)  # noqa: TRY004 — module contract is ValueError throughout
        if from_rate <= 0 or to_rate <= 0:
            msg = f"sample rates must be positive, got {from_rate} -> {to_rate}"
            raise ValueError(msg)
        if from_rate == to_rate:
            msg = f"from_rate and to_rate must differ (both {from_rate})"
            raise ValueError(msg)
        self._from = from_rate
        self._to = to_rate
        self._state: _RateState = None

    def resample(self, pcm16: bytes) -> bytes:
        """Convert one chunk of PCM16-LE mono, carrying stream state forward."""
        _validate_pcm16(pcm16)
        converted, self._state = audioop.ratecv(
            pcm16, PCM16_BYTES_PER_SAMPLE, _MONO, self._from, self._to, self._state
        )
        return converted

    def reset(self) -> None:
        """Discard carried state so the next ``resample`` starts a fresh stream."""
        self._state = None


# ---------------------------------------------------------------------------
# Diagnostic tone generator
# ---------------------------------------------------------------------------

#: Packetisation time (ms) used by the RTP engine — one frame per 20 ms.
_PTIME_MS: Final[int] = 20

#: Default tone frequency (Hz) — 440 Hz (A4) is clearly audible on telephony.
_DEFAULT_TONE_FREQ_HZ: Final[float] = 440.0

#: Tone amplitude as a fraction of int16 full-scale. 45% gives a clearly
#: audible but non-clipping signal after G.711 encode/decode.
_TONE_AMPLITUDE: Final[float] = 0.45

#: Struct format for one 20 ms frame of 8 kHz PCM16-LE (160 samples).
_FRAME_FORMAT: Final[str] = f"<{G711_SAMPLE_RATE * _PTIME_MS // 1000}h"


def generate_tone_frames(
    *,
    duration_secs: float,
    freq_hz: float = _DEFAULT_TONE_FREQ_HZ,
) -> Iterator[PcmFrame]:
    """Generate a pure-sine tone at 8 kHz as a sequence of 20 ms PcmFrames.

    The frames are already at :data:`G711_SAMPLE_RATE`, so they bypass the
    TTS + resample path when fed to ``RtpMediaTransport.send_audio`` — the
    fast path encodes directly to G.711 with no conversion. This lets an
    operator confirm that the RTP transport and G.711 codec are working before
    implicating the TTS or resample layers.

    The tone is a continuous sine wave (phase carried across frames so there
    are no click artefacts at frame boundaries) at ``freq_hz`` Hz and
    :data:`_TONE_AMPLITUDE` (45% of full scale).

    Args:
        duration_secs: How many seconds of tone to generate.
        freq_hz: Tone frequency in Hz (default 440 Hz).

    Yields:
        20 ms :class:`PcmFrame` objects at :data:`G711_SAMPLE_RATE`.

    Raises:
        ValueError: If ``duration_secs`` is not positive, or ``freq_hz`` is
            not positive.
    """
    if duration_secs <= 0:
        msg = f"duration_secs must be positive, got {duration_secs}"
        raise ValueError(msg)
    if freq_hz <= 0:
        msg = f"freq_hz must be positive, got {freq_hz}"
        raise ValueError(msg)

    samples_per_frame = G711_SAMPLE_RATE * _PTIME_MS // 1000  # 160
    n_frames = int(duration_secs * 1000 / _PTIME_MS)
    peak = int(32767 * _TONE_AMPLITUDE)

    for frame_idx in range(n_frames):
        # Phase offset for continuous sine (no clicks between frames).
        base_sample = frame_idx * samples_per_frame
        two_pi_f_over_sr = 2.0 * math.pi * freq_hz / G711_SAMPLE_RATE
        pcm_samples = [
            int(peak * math.sin(two_pi_f_over_sr * (base_sample + i)))
            for i in range(samples_per_frame)
        ]
        raw = struct.pack(_FRAME_FORMAT, *pcm_samples)
        yield PcmFrame(
            samples=raw,
            sample_rate=G711_SAMPLE_RATE,
            monotonic_ts_ns=0,
        )
