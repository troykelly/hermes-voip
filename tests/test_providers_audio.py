"""Tests for hermes_voip.providers.audio — the shared PCM16 audio currency."""

from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame


def test_sample_count_is_byte_length_over_two() -> None:
    frame = PcmFrame(
        samples=b"\x00\x00\x01\x01\x02\x02", sample_rate=8000, monotonic_ts_ns=0
    )
    assert frame.sample_count == 3
    assert PCM16_BYTES_PER_SAMPLE == 2


def test_empty_frame_has_zero_samples() -> None:
    assert PcmFrame(samples=b"", sample_rate=16000, monotonic_ts_ns=0).sample_count == 0


def test_frame_is_frozen_and_hashable() -> None:
    a = PcmFrame(samples=b"\x10\x20", sample_rate=8000, monotonic_ts_ns=42)
    b = PcmFrame(samples=b"\x10\x20", sample_rate=8000, monotonic_ts_ns=42)
    assert a == b
    assert hash(a) == hash(b)  # frozen + slots => hashable
