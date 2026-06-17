"""Opus codec (media/opus.py) — encode/decode at 48 kHz, 20 ms frames (ADR-0032).

TDD suite (AGENTS.md rule 18), red-first, deterministic. Opus rides the WebRTC
wire path: 48 kHz mono PCM16, one 20 ms frame (960 samples) per Opus packet, the
RTP timestamp advancing by 960 at the 48 kHz clock. ``opuslib`` (the BSD-3 ctypes
binding to the system ``libopus.so``) lives in the optional ``webrtc`` extra, so
the module imports without it but the codec classes raise ``ImportError`` at
construction when it (or libopus) is absent — the suite skips on the no-extra gate.
"""

from __future__ import annotations

import math
import struct

import pytest

# media/opus.py must be importable WITHOUT the webrtc extra (lazy import). The
# constants are plain ints, available with or without opuslib.
from hermes_voip.media.opus import (
    OPUS_DEFAULT_PAYLOAD_TYPE,
    OPUS_FRAME_SAMPLES,
    OPUS_RTP_CLOCK_RATE,
    OPUS_SAMPLE_RATE,
    OpusDecoder,
    OpusEncoder,
)

# Guard: the codec classes need opuslib + libopus; skip the round-trip suite when
# the webrtc extra is absent. The bare module import above must NOT skip.
pytest.importorskip("opuslib", reason="webrtc extra (opuslib) not installed")


_BYTES_PER_SAMPLE = 2


def _sine_pcm16(n_samples: int, freq_hz: float = 440.0, amp: int = 8000) -> bytes:
    """One channel of PCM16-LE sine at ``OPUS_SAMPLE_RATE``."""
    return struct.pack(
        f"<{n_samples}h",
        *[
            int(amp * math.sin(2 * math.pi * freq_hz * n / OPUS_SAMPLE_RATE))
            for n in range(n_samples)
        ],
    )


def test_constants_are_webrtc_opus() -> None:
    """Opus is 48 kHz, 20 ms = 960 samples, RTP clock == audio rate, default PT 111."""
    assert OPUS_SAMPLE_RATE == 48_000
    assert OPUS_RTP_CLOCK_RATE == 48_000  # unlike G.722, clock == audio rate
    assert OPUS_FRAME_SAMPLES == 960  # 20 ms @ 48 kHz
    assert OPUS_DEFAULT_PAYLOAD_TYPE == 111  # conventional dynamic PT for Opus


def test_encode_one_frame_produces_opus_packet() -> None:
    """A 20 ms 48 kHz frame encodes to a non-empty, compressed Opus packet."""
    enc = OpusEncoder()
    pcm = _sine_pcm16(OPUS_FRAME_SAMPLES)
    packet = enc.encode(pcm)
    assert isinstance(packet, bytes)
    assert 0 < len(packet) < len(pcm)  # Opus compresses


def test_round_trip_preserves_frame_length_and_energy() -> None:
    """encode→decode returns a 960-sample frame whose energy tracks the input."""
    enc = OpusEncoder()
    dec = OpusDecoder()
    pcm = _sine_pcm16(OPUS_FRAME_SAMPLES)
    out = dec.decode(enc.encode(pcm))
    assert len(out) == OPUS_FRAME_SAMPLES * _BYTES_PER_SAMPLE
    # The decoded RMS is within a generous tolerance of the input (Opus is lossy;
    # this is an energy-preservation sanity check, not bit-exactness).
    n = OPUS_FRAME_SAMPLES
    in_vals = struct.unpack(f"<{n}h", pcm)
    out_vals = struct.unpack(f"<{n}h", out)
    rms_in = math.sqrt(sum(v * v for v in in_vals) / n)
    rms_out = math.sqrt(sum(v * v for v in out_vals) / n)
    assert rms_out > 0.3 * rms_in  # signal survives, not silence


def test_round_trip_correlates_after_codec_delay() -> None:
    """A continuous tone survives encode/decode (cross-correlation is high).

    Opus has an algorithmic look-ahead, so the first frame's output lags the
    input; feeding several frames and correlating a steady-state frame against the
    input proves the codec carries the waveform (not just energy).
    """
    enc = OpusEncoder()
    dec = OpusDecoder()
    frames_in = [_sine_pcm16(OPUS_FRAME_SAMPLES) for _ in range(6)]
    decoded = [dec.decode(enc.encode(f)) for f in frames_in]
    # Compare a steady-state decoded frame to a pure tone of the same frequency.
    ref = struct.unpack(f"<{OPUS_FRAME_SAMPLES}h", _sine_pcm16(OPUS_FRAME_SAMPLES))
    last = struct.unpack(f"<{OPUS_FRAME_SAMPLES}h", decoded[-1])

    def _norm(xs: tuple[int, ...]) -> list[float]:
        mag = math.sqrt(sum(x * x for x in xs)) or 1.0
        return [x / mag for x in xs]

    a, b = _norm(ref), _norm(last)
    # Best cross-correlation over a small lag window (codec delay < 1 frame).
    best = max(
        abs(sum(a[i] * b[i - lag] for i in range(lag, len(a)))) for lag in range(0, 200)
    )
    assert best > 0.5  # strong correlation at the right lag


def test_decode_rejects_wrong_length_pcm() -> None:
    """Encode rejects a frame that is not exactly one 20 ms 48 kHz frame."""
    enc = OpusEncoder()
    with pytest.raises(ValueError, match=r"960|frame"):
        enc.encode(_sine_pcm16(OPUS_FRAME_SAMPLES - 1))


def test_encoder_and_decoder_are_independent_per_instance() -> None:
    """Two encoders produce identical bytes for identical input (no shared state).

    State is per-instance (one encoder/decoder per call direction), so a fresh
    encoder fed the same first frame yields the same packet.
    """
    pcm = _sine_pcm16(OPUS_FRAME_SAMPLES)
    p1 = OpusEncoder().encode(pcm)
    p2 = OpusEncoder().encode(pcm)
    assert p1 == p2
