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
    G711_SAMPLE_RATE,
    Resampler,
    alaw_to_frame,
    decode_alaw,
    decode_ulaw,
    encode_alaw,
    encode_ulaw,
    frame_to_alaw,
    frame_to_ulaw,
    linear_fade_out,
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


# ---------------------------------------------------------------------------
# linear_fade_out — click-free ramp on the final frames of a barge-in cut
# ---------------------------------------------------------------------------


def test_linear_fade_out_ramps_last_samples_to_zero() -> None:
    """The final ``fade_samples`` ramp linearly from full gain down to ~0.

    A constant full-scale signal is the worst case for a hard cut (max click). The
    fade must leave the head untouched and bring the tail monotonically to near
    silence, with the very last sample at (or essentially at) zero.
    """
    const = 10_000
    pcm = _pcm16(*([const] * 100))
    faded = _samples(linear_fade_out(pcm, fade_samples=40))

    # The 60 samples BEFORE the fade window are untouched (full amplitude).
    assert all(s == const for s in faded[:60])
    # The fade window ramps DOWN monotonically (each sample <= the previous).
    tail = faded[60:]
    assert all(tail[i] <= tail[i - 1] for i in range(1, len(tail)))
    # It starts near full and ends at (essentially) zero.
    assert tail[0] >= const - const // 20  # ~first faded sample still near full
    assert tail[-1] == 0  # last sample is silence — no residual step to click


def test_linear_fade_out_is_symmetric_for_negative_signal() -> None:
    """A full-scale NEGATIVE constant ramps up toward zero (magnitude shrinks)."""
    const = -12_000
    pcm = _pcm16(*([const] * 60))
    faded = _samples(linear_fade_out(pcm, fade_samples=30))
    tail = faded[30:]
    # Magnitude shrinks monotonically toward zero (samples rise toward 0).
    assert all(abs(tail[i]) <= abs(tail[i - 1]) for i in range(1, len(tail)))
    assert tail[-1] == 0


def test_linear_fade_out_clamps_fade_to_buffer_length() -> None:
    """A fade longer than the buffer fades the WHOLE buffer (no overrun/error)."""
    pcm = _pcm16(*([8000] * 10))
    faded = _samples(linear_fade_out(pcm, fade_samples=100))
    assert len(faded) == 10
    assert all(faded[i] <= faded[i - 1] for i in range(1, len(faded)))
    assert faded[-1] == 0


def test_linear_fade_out_zero_fade_returns_input_unchanged() -> None:
    """fade_samples=0 is a no-op (returns the bytes unchanged)."""
    pcm = _pcm16(1, 2, 3, 4)
    assert linear_fade_out(pcm, fade_samples=0) == pcm


def test_linear_fade_out_preserves_byte_length() -> None:
    """The faded buffer has the same number of PCM16 samples as the input."""
    pcm = _pcm16(*([5000] * 50))
    assert len(linear_fade_out(pcm, fade_samples=20)) == len(pcm)


@pytest.mark.parametrize(("from_rate", "to_rate"), [(8000, 16000), (16000, 8000)])
def test_resampler_state_continuity_matches_single_pass(
    from_rate: int, to_rate: int
) -> None:
    # both directions: streaming repeated 20 ms chunks must equal a single pass
    chunk = _pcm16(*range(-160, 160))  # 320 samples
    streamed = Resampler(from_rate, to_rate)
    streamed_out = b"".join(streamed.resample(chunk) for _ in range(4))
    single = Resampler(from_rate, to_rate).resample(chunk * 4)
    assert streamed_out == single


def test_resampler_rejects_odd_length_pcm() -> None:
    with pytest.raises(ValueError, match="whole 16-bit samples"):
        Resampler(8000, 16000).resample(b"\x00\x01\x02")  # 3 bytes


def test_encoders_reject_odd_length_pcm() -> None:
    with pytest.raises(ValueError, match="whole 16-bit samples"):
        encode_ulaw(b"\x00")
    with pytest.raises(ValueError, match="whole 16-bit samples"):
        encode_alaw(b"\x00\x01\x02")


def test_resampler_reset_clears_state() -> None:
    r = Resampler(8000, 16000)
    first = r.resample(_pcm16(*range(160)))
    r.reset()
    after_reset = r.resample(_pcm16(*range(160)))
    assert (
        after_reset == first
    )  # identical input from a clean state => identical output


def test_frame_helpers_round_trip_through_ulaw() -> None:
    # G.711 is intrinsically 8 kHz: ulaw_to_frame always stamps the wire rate.
    pcm = _pcm16(0, 4000, -4000, 1234)
    ulaw = encode_ulaw(pcm)
    frame = ulaw_to_frame(ulaw, monotonic_ts_ns=42)
    assert isinstance(frame, PcmFrame)
    assert frame.sample_rate == G711_SAMPLE_RATE == 8000
    assert frame.monotonic_ts_ns == 42
    assert frame.sample_count == 4
    assert frame_to_ulaw(frame) == ulaw  # frame -> wire is the inverse of wire -> frame


def test_frame_to_ulaw_rejects_non_8k_frame() -> None:
    # encoding a 16 kHz frame to G.711 would silently halve its duration
    frame = PcmFrame(samples=_pcm16(0, 1, 2), sample_rate=16000, monotonic_ts_ns=0)
    with pytest.raises(ValueError, match="8000 Hz"):
        frame_to_ulaw(frame)


def test_resampler_rejects_equal_rates() -> None:
    with pytest.raises(ValueError, match="differ"):
        Resampler(8000, 8000)


@pytest.mark.parametrize("bad_rate", [0, -8000])
def test_resampler_rejects_non_positive_from_rate(bad_rate: int) -> None:
    # A config-derived rate of 0/negative must fail fast at construction with a
    # ValueError, not lie dormant until ratecv raises audioop.error mid-call.
    with pytest.raises(ValueError, match="positive"):
        Resampler(bad_rate, 16000)


@pytest.mark.parametrize("bad_rate", [0, -16000])
def test_resampler_rejects_non_positive_to_rate(bad_rate: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        Resampler(8000, bad_rate)


def test_alaw_is_one_byte_per_sample_via_frame_bridge() -> None:
    pcm = _pcm16(0, 4000, -4000, 1234)
    alaw = encode_alaw(pcm)
    frame = alaw_to_frame(alaw, monotonic_ts_ns=7)
    assert isinstance(frame, PcmFrame)
    assert frame.sample_rate == G711_SAMPLE_RATE == 8000
    assert frame.monotonic_ts_ns == 7
    assert frame.sample_count == 4
    # frame -> wire is the exact inverse of wire -> frame
    assert frame_to_alaw(frame) == alaw


def test_frame_to_alaw_rejects_non_8k_frame() -> None:
    frame = PcmFrame(samples=_pcm16(0, 1, 2), sample_rate=16000, monotonic_ts_ns=0)
    with pytest.raises(ValueError, match="8000 Hz"):
        frame_to_alaw(frame)


def test_alaw_frame_bridge_is_distinct_from_ulaw() -> None:
    # PCMA and PCMU are different codecs: the a-law wire bytes for a non-trivial
    # frame must differ from the mu-law bytes (guards a copy-paste mu/a swap).
    frame = PcmFrame(
        samples=_pcm16(1000, -1000, 8000), sample_rate=8000, monotonic_ts_ns=0
    )
    assert frame_to_alaw(frame) != frame_to_ulaw(frame)
