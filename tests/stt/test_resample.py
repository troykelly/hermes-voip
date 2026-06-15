"""STT media glue: int16<->float32 conversion + 8 kHz->16 kHz upsampling.

It proves the two things the recogniser layer owns (ADR-0006 §"Media glue we own"):

* the PCM16-bytes <-> normalised float32 conversion sherpa-onnx's
  ``accept_waveform`` requires (it wants float32 in ``-1..1``, not PCM16 bytes);
* a continuous, state-carrying 8 kHz -> 16 kHz upsample that reuses the canonical
  ``media.audio.Resampler`` so feeding a stream frame-by-frame yields exactly the
  same samples as one pass (no per-frame boundary clicks, ADR-0006).

The float32 conversion uses ``numpy`` (the optional ``ml`` extra), so those tests
``pytest.importorskip("numpy")`` and run in the ``providers`` (ml) gate; they skip
cleanly in the default no-ml gate. The ``FrameUpsampler`` rides on ``audioop-lts``
(a base dependency) and is numpy-free, so its tests run everywhere.
"""

from __future__ import annotations

import struct

import pytest

from hermes_voip.media.audio import G711_SAMPLE_RATE, Resampler
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.stt.resample import (
    FrameUpsampler,
    float32_to_pcm16,
    pcm16_to_float32,
)

_RECOGNISER_RATE = 16_000


def _pcm16(*samples: int) -> bytes:
    """Pack signed 16-bit little-endian samples into a PCM16 buffer."""
    return struct.pack(f"<{len(samples)}h", *samples)


def _frame(pcm16: bytes, *, rate: int = G711_SAMPLE_RATE, ts: int = 0) -> PcmFrame:
    return PcmFrame(samples=pcm16, sample_rate=rate, monotonic_ts_ns=ts)


# --- pcm16_to_float32: PCM16 bytes -> normalised float32 in -1..1 -------------


def test_pcm16_to_float32_normalises_by_32768() -> None:
    """Each sample is divided by 32768 so the range maps into ``-1..1``."""
    np = pytest.importorskip("numpy")
    out = np.asarray(pcm16_to_float32(_pcm16(0, 32767, -32768, 16384)))
    assert list(out) == [
        0.0,
        32767 / 32768,
        -32768 / 32768,  # exactly -1.0
        16384 / 32768,  # exactly 0.5
    ]


def test_pcm16_to_float32_is_float32_dtype() -> None:
    """The recogniser wants float32, not float64 — the dtype must be float32."""
    np = pytest.importorskip("numpy")
    out = np.asarray(pcm16_to_float32(_pcm16(1, -1)))
    assert out.dtype.name == "float32"


def test_pcm16_to_float32_empty_is_empty() -> None:
    """An empty buffer yields an empty array (a zero-length frame is valid)."""
    np = pytest.importorskip("numpy")
    out = np.asarray(pcm16_to_float32(b""))
    assert out.shape == (0,)


def test_pcm16_to_float32_rejects_odd_length() -> None:
    """A buffer that is not a whole number of 16-bit samples is malformed."""
    with pytest.raises(ValueError, match="16-bit"):
        pcm16_to_float32(b"\x00")


# --- float32_to_pcm16: inverse, clamped to the int16 range --------------------


def test_float32_to_pcm16_round_trips_within_quantisation() -> None:
    """float32 -> PCM16 inverts PCM16 -> float32 (modulo 1-LSB rounding)."""
    pytest.importorskip("numpy")
    original = _pcm16(0, 1000, -1000, 32767, -32768, 12345)
    back = float32_to_pcm16(pcm16_to_float32(original))
    restored = struct.unpack(f"<{len(original) // 2}h", back)
    for got, want in zip(restored, struct.unpack("<6h", original), strict=True):
        assert abs(got - want) <= 1


def test_float32_to_pcm16_clamps_out_of_range() -> None:
    """Values outside ``-1..1`` saturate at the int16 limits, never wrap."""
    np = pytest.importorskip("numpy")
    out = float32_to_pcm16(np.array([2.0, -2.0, 1.0, -1.0], dtype=np.float32))
    assert struct.unpack("<4h", out) == (32767, -32768, 32767, -32768)


# --- FrameUpsampler: continuous 8 kHz -> 16 kHz over a frame stream -----------


def test_frame_upsampler_matches_one_pass_resample() -> None:
    """Frame-by-frame upsampling equals a single continuous ``Resampler`` pass.

    This is the continuity guarantee: the upsampler must carry ``ratecv`` state
    across frames, so streaming N frames produces byte-identical output to
    resampling their concatenation in one go (no boundary discontinuities).
    """
    frames_pcm = [
        _pcm16(*range(-100, 100)),
        _pcm16(*range(100, -100, -1)),
        _pcm16(*([0, 5000, -5000] * 40)),
    ]
    reference = Resampler(G711_SAMPLE_RATE, _RECOGNISER_RATE)
    expected = b"".join(reference.resample(pcm) for pcm in frames_pcm)

    upsampler = FrameUpsampler()
    produced = b"".join(
        upsampler.upsample(_frame(pcm, ts=i)).samples
        for i, pcm in enumerate(frames_pcm)
    )
    assert produced == expected


def test_frame_upsampler_stamps_recogniser_rate_and_preserves_ts() -> None:
    """Output frames are stamped at 16 kHz and keep the source presentation ts."""
    upsampler = FrameUpsampler()
    out = upsampler.upsample(_frame(_pcm16(*range(80)), ts=4_242))
    assert out.sample_rate == _RECOGNISER_RATE
    assert out.monotonic_ts_ns == 4_242


def test_frame_upsampler_doubles_sample_count() -> None:
    """8 kHz -> 16 kHz roughly doubles the sample count (ratecv ratio 1:2)."""
    upsampler = FrameUpsampler()
    out = upsampler.upsample(_frame(_pcm16(*([0] * 160)), ts=0))
    # 160 input samples at 8k -> ~320 output samples at 16k (state-dependent +-1).
    assert abs(out.sample_count - 320) <= 1


def test_frame_upsampler_target_rate_is_16k() -> None:
    """The declared target equals the recogniser's required input rate."""
    assert FrameUpsampler().target_sample_rate == _RECOGNISER_RATE


def test_frame_upsampler_rejects_wrong_source_rate() -> None:
    """A frame already at 16 kHz (or any non-8 kHz rate) is a caller error.

    The upsampler is fixed at 8 kHz -> 16 kHz (the G.711 wire to the recogniser);
    feeding it a frame at another rate would silently change duration, so it
    raises rather than mis-resample.
    """
    upsampler = FrameUpsampler()
    with pytest.raises(ValueError, match="8000"):
        upsampler.upsample(_frame(_pcm16(0, 0), rate=_RECOGNISER_RATE))
