"""G.711 codec and sample-rate conversion (ADR-0004/0005).

The telephony wire is G.711 (mu-law/a-law, 8 kHz, one byte per sample); the
recognisers want 16 kHz and the synthesisers emit 24/16 kHz. This module owns
both conversions so every provider boundary speaks one currency: PCM16 frames
at a declared rate (ADR-0004).

It wraps ``audioop`` (the ``audioop-lts`` backport, since stdlib ``audioop`` is
removed in Python 3.13) — a typed, battle-tested C codec.
"""

from __future__ import annotations

import audioop

from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame

_MONO: int = 1

# audioop.ratecv's resumable conversion state (or None to start a fresh stream).
type _RateState = tuple[int, tuple[tuple[int, int], ...]] | None


def encode_ulaw(pcm16: bytes) -> bytes:
    """Encode PCM16-LE mono to G.711 mu-law (one byte per sample)."""
    return audioop.lin2ulaw(pcm16, PCM16_BYTES_PER_SAMPLE)


def decode_ulaw(ulaw: bytes) -> bytes:
    """Decode G.711 mu-law to PCM16-LE mono (two bytes per sample)."""
    return audioop.ulaw2lin(ulaw, PCM16_BYTES_PER_SAMPLE)


def encode_alaw(pcm16: bytes) -> bytes:
    """Encode PCM16-LE mono to G.711 a-law (one byte per sample)."""
    return audioop.lin2alaw(pcm16, PCM16_BYTES_PER_SAMPLE)


def decode_alaw(alaw: bytes) -> bytes:
    """Decode G.711 a-law to PCM16-LE mono (two bytes per sample)."""
    return audioop.alaw2lin(alaw, PCM16_BYTES_PER_SAMPLE)


def ulaw_to_frame(ulaw: bytes, *, sample_rate: int, monotonic_ts_ns: int) -> PcmFrame:
    """Decode a mu-law payload into a :class:`PcmFrame` at ``sample_rate``."""
    return PcmFrame(
        samples=decode_ulaw(ulaw),
        sample_rate=sample_rate,
        monotonic_ts_ns=monotonic_ts_ns,
    )


def frame_to_ulaw(frame: PcmFrame) -> bytes:
    """Encode a :class:`PcmFrame`'s PCM16 samples to a mu-law payload."""
    return encode_ulaw(frame.samples)


class Resampler:
    """Stateful PCM16 sample-rate converter for one continuous stream.

    The conversion state (sub-sample phase) is carried across calls so feeding a
    stream frame-by-frame produces exactly the same audio as one pass — without
    the clicks a stateless per-frame conversion would introduce at boundaries.
    One ``Resampler`` belongs to one direction of one call; ``reset()`` starts a
    fresh stream.
    """

    def __init__(self, from_rate: int, to_rate: int) -> None:
        """Create a converter from ``from_rate`` to ``to_rate`` (must differ)."""
        if from_rate == to_rate:
            msg = f"from_rate and to_rate must differ (both {from_rate})"
            raise ValueError(msg)
        self._from = from_rate
        self._to = to_rate
        self._state: _RateState = None

    def resample(self, pcm16: bytes) -> bytes:
        """Convert one chunk of PCM16-LE mono, carrying stream state forward."""
        converted, self._state = audioop.ratecv(
            pcm16, PCM16_BYTES_PER_SAMPLE, _MONO, self._from, self._to, self._state
        )
        return converted

    def reset(self) -> None:
        """Discard carried state so the next ``resample`` starts a fresh stream."""
        self._state = None
