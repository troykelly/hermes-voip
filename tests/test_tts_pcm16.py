"""Full-scale float32 -> PCM16-LE conversion for the Kokoro backend (ADR-0007).

Kokoro (via sherpa-onnx) emits float32 samples in ``[-1.0, 1.0]``; the provider
must map them to signed 16-bit little-endian PCM at *full scale*. PCM16's range
is asymmetric — ``[-32768, 32767]`` — so ``-1.0`` must map to ``-32768`` (using
the negative full-scale code), not ``-32767``. These tests pin that mapping and
that out-of-range inputs saturate rather than overflow/wrap. They need numpy (the
``ml`` extra), so they skip in the no-ml base gate.
"""

from __future__ import annotations

import struct

import pytest

pytest.importorskip("numpy", reason="ml extra not installed")

# Imported after the importorskip (the test_tts_sherpa_kokoro_real idiom): the
# conversion resolves numpy lazily so importing the module is safe without numpy,
# but the test arrays need it, so the whole module skips in the no-ml base gate.
import numpy as np

from hermes_voip.tts.sherpa_kokoro import pcm16_from_float32


def _samples(pcm16: bytes) -> tuple[int, ...]:
    """Decode PCM16-LE bytes into signed-int samples."""
    count = len(pcm16) // 2
    return struct.unpack(f"<{count}h", pcm16)


def test_negative_full_scale_maps_to_int16_min() -> None:
    """-1.0 maps to PCM16 negative full scale (-32768), not -32767."""
    pcm16 = pcm16_from_float32(np.asarray([-1.0], dtype=np.float32))
    assert _samples(pcm16) == (-32768,)


def test_positive_full_scale_maps_to_int16_max() -> None:
    """+1.0 maps to PCM16 positive full scale (+32767)."""
    pcm16 = pcm16_from_float32(np.asarray([1.0], dtype=np.float32))
    assert _samples(pcm16) == (32767,)


def test_silence_maps_to_zero() -> None:
    """0.0 maps to the zero sample."""
    pcm16 = pcm16_from_float32(np.asarray([0.0], dtype=np.float32))
    assert _samples(pcm16) == (0,)


def test_out_of_range_saturates_without_overflow() -> None:
    """Values beyond [-1.0, 1.0] clip to the int16 endpoints (no wrap-around).

    Without a clamp, ``2.0 * 32768`` would overflow int16 and wrap to a positive
    value near zero (or raise); full-scale conversion must saturate to the range
    endpoints instead.
    """
    pcm16 = pcm16_from_float32(np.asarray([2.0, -2.0, 1.5, -1.5], dtype=np.float32))
    assert _samples(pcm16) == (32767, -32768, 32767, -32768)


def test_output_is_little_endian_int16_width() -> None:
    """The output is exactly 2 bytes per sample, little-endian."""
    pcm16 = pcm16_from_float32(np.asarray([-1.0, 0.0, 1.0], dtype=np.float32))
    assert len(pcm16) == 3 * 2
    # -32768 little-endian is 0x00 0x80; assert the first sample's byte order.
    assert pcm16[0:2] == b"\x00\x80"
