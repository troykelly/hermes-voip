"""Tests for hermes_voip.media.audio — G.711 codec + resampling (ADR-0004/0005).

The media layer owns the wire codec (G.711 mu-law/a-law, 1 byte/sample) and
8<->16 kHz resampling so providers only ever see PCM16 at a declared rate. These
tests pin the structural contract (byte ratios, rate math), the near-lossless
round-trip (G.711 is ~12-bit accurate), streaming-state continuity, and the
PcmFrame integration.
"""

import struct

import pytest

from hermes_voip.media.audio import (
    Resampler,
    decode_alaw,
    decode_ulaw,
    encode_alaw,
    encode_ulaw,
    frame_to_ulaw,
    ulaw_to_frame,
)
from hermes_voip.providers.audio import PcmFrame


def _pcm16(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _samples(pcm: bytes) -> tuple[int, ...]:
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)


def test_ulaw_is_one_byte_per_sample() -> None:
    pcm = _pcm16(0, 1000, -1000, 16000, -16000)
    encoded = encode_ulaw(pcm)
    assert len(encoded) == 5  # one G.711 byte per 16-bit sample
    assert len(decode_ulaw(encoded)) == len(pcm)  # back to 2 bytes/sample


def test_alaw_is_one_byte_per_sample() -> None:
    pcm = _pcm16(0, 1000, -1000, 16000, -16000)
    assert len(encode_alaw(pcm)) == 5
    assert len(decode_alaw(encode_alaw(pcm))) == len(pcm)


def test_ulaw_round_trip_is_near_lossless_for_midrange() -> None:
    pcm = _pcm16(0, 2000, -2000, 8000, -8000, 100, -100)
    back = _samples(decode_ulaw(encode_ulaw(pcm)))
    for original, restored in zip(_samples(pcm), back, strict=True):
        # G.711 mu-law is ~12-bit accurate; quantisation error grows with level
        assert abs(original - restored) <= max(64, abs(original) // 16)


def test_resampler_8k_to_16k_roughly_doubles_samples() -> None:
    pcm = _pcm16(*([1000, -1000] * 80))  # 160 samples @ 8 kHz = 20 ms
    out = Resampler(8000, 16000).resample(pcm)
    out_samples = len(out) // 2
    assert 300 <= out_samples <= 340  # ~2x 160, allowing for filter edge


def test_resampler_16k_to_8k_roughly_halves_samples() -> None:
    pcm = _pcm16(*([500] * 320))  # 320 samples @ 16 kHz = 20 ms
    out = Resampler(16000, 8000).resample(pcm)
    assert 140 <= len(out) // 2 <= 180  # ~0.5x 320


def test_resampler_state_continuity_matches_single_pass() -> None:
    whole = _pcm16(*range(-200, 200))  # 400 samples
    half = len(whole) // 2

    streamed = Resampler(8000, 16000)
    out_a = streamed.resample(whole[:half])
    out_b = streamed.resample(whole[half:])

    single = Resampler(8000, 16000).resample(whole)
    # streaming in two frames yields the same total audio as one pass (no clicks)
    assert out_a + out_b == single


def test_resampler_reset_clears_state() -> None:
    r = Resampler(8000, 16000)
    first = r.resample(_pcm16(*range(160)))
    r.reset()
    after_reset = r.resample(_pcm16(*range(160)))
    assert (
        after_reset == first
    )  # identical input from a clean state => identical output


def test_frame_helpers_round_trip_through_ulaw() -> None:
    pcm = _pcm16(0, 4000, -4000, 1234)
    ulaw = encode_ulaw(pcm)
    frame = ulaw_to_frame(ulaw, sample_rate=8000, monotonic_ts_ns=42)
    assert isinstance(frame, PcmFrame)
    assert frame.sample_rate == 8000
    assert frame.monotonic_ts_ns == 42
    assert frame.sample_count == 4
    assert frame_to_ulaw(frame) == ulaw  # frame -> wire is the inverse of wire -> frame


def test_resampler_rejects_equal_rates() -> None:
    with pytest.raises(ValueError, match="differ"):
        Resampler(8000, 8000)
