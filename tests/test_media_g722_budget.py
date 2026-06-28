"""G.722 hot-path CPU-budget gate (backlog [high] efficiency, ADR-0094).

Rule 22 requires every hot-path module to have a concrete per-frame cost
measurement documented in source and gated in a test. The G.722 pure-Python
encode and decode paths run on every RTP packet at 50 pps (20 ms ptime):
G722Encoder.encode on the outbound send_audio path, G722Decoder.decode on the
inbound receive path. Because G.722 remains preferred by default, CI must enforce
the COMBINED encode+decode cost against the 20 ms packet-period budget.

Design:
  - media/g722.py records measured encode and decode baselines plus the combined
    packet-period budget. Those constants are not review-only: this test measures
    the hot path and verifies the measured values remain within a generous,
    documented tolerance of the constants.
  - The primary budget gate measures encode+decode together and asserts the
    combined per-frame cost stays below the 20 ms G.722 packet period. A
    regression to ~14 ms encode + ~14 ms decode fails even though each side would
    be below a separate 15 ms ceiling.
  - The benchmark is CI-friendly: no sleeps, no network, no benchmark dependency,
    warm-up before timing, several repeated batches, and the best batch is used to
    reduce scheduler-noise sensitivity. The budget is deliberately coarse (20 ms),
    not a precision performance score.

Measured numbers recorded 2026-06-28 on CPython 3.13.5 in the devcontainer:
  Encode: ~3 400 us / 20 ms frame (320 PCM16 samples -> 160 G.722 octets)
  Decode: ~3 300 us / 20 ms frame (160 G.722 octets -> 320 PCM16 samples)
  Combined: ~6 700 us / frame (~33 % of the 20 ms ptime)

No real gateway addresses, extension numbers, or device identifiers appear here.
"""

from __future__ import annotations

import math
import struct
import timeit
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import hermes_voip.media.g722 as _g722_mod
from hermes_voip.media.g722 import G722Decoder, G722Encoder

# ---------------------------------------------------------------------------
# Frame constants
# ---------------------------------------------------------------------------

_RATE: Final[int] = 16_000  # G.722 audio sample rate
_PTIME_MS: Final[int] = 20  # standard RTP packetisation
_FRAME_PERIOD_US: Final[float] = float(_PTIME_MS * 1000)  # 20 000 us
_SAMPLES_PER_FRAME: Final[int] = _RATE * _PTIME_MS // 1000  # 320

# ---------------------------------------------------------------------------
# Benchmark stability knobs (rule 22 / ADR-0094)
# ---------------------------------------------------------------------------

# Baseline constants are measured on one devcontainer host. CI hosts and transient
# scheduler load vary, so tolerate up to 3x the recorded per-side baseline while
# still enforcing the real combined packet-period budget below. A regression to
# ~14 ms encode or decode fails this tolerance; a combined regression over 20 ms
# fails regardless of the per-side split.
_MEASURED_CONSTANT_TOLERANCE_MULTIPLIER: Final[float] = 3.0

# Keep the test cheap while averaging over enough frames to avoid per-call timer
# noise. At the recorded cost, the whole benchmark file completes in a few seconds.
_BENCHMARK_REPEATS: Final[int] = 5
_BENCHMARK_FRAMES_PER_REPEAT: Final[int] = 20
_WARMUP_FRAMES: Final[int] = 20


@dataclass(frozen=True, slots=True)
class _MeasuredCost:
    """Measured per-20 ms-frame costs in microseconds."""

    encode_us: float
    decode_us: float
    combined_us: float


# ---------------------------------------------------------------------------
# Frame builders / timing helpers
# ---------------------------------------------------------------------------


def _pcm_frame(n_samples: int = _SAMPLES_PER_FRAME, freq_hz: float = 3000.0) -> bytes:
    """Return PCM16-LE mono @ 16 kHz with a tone at ``freq_hz`` Hz."""
    scale = 0.4 * 32767.0
    return struct.pack(
        f"<{n_samples}h",
        *[
            int(scale * math.sin(2.0 * math.pi * freq_hz * i / _RATE))
            for i in range(n_samples)
        ],
    )


def _warm_up(fn: Callable[[], object]) -> None:
    """Run ``fn`` enough times to settle interpreter/cache noise before timing."""
    for _ in range(_WARMUP_FRAMES):
        fn()


def _best_us_per_frame(fn: Callable[[], object]) -> float:
    """Return best repeated-batch time for one 20 ms frame in microseconds.

    The best of several batches is the least scheduler-contaminated estimate. We
    are enforcing a coarse 20 ms safety budget, not reporting a precision score.
    """
    _warm_up(fn)
    seconds = min(
        timeit.repeat(
            fn,
            repeat=_BENCHMARK_REPEATS,
            number=_BENCHMARK_FRAMES_PER_REPEAT,
        )
    )
    return seconds / _BENCHMARK_FRAMES_PER_REPEAT * 1_000_000.0


def _measure_hot_path() -> _MeasuredCost:
    """Measure stateful encode, decode, and combined encode+decode hot paths."""
    pcm = _pcm_frame()
    decode_payload = G722Encoder().encode(pcm)

    encode_encoder = G722Encoder()
    decode_decoder = G722Decoder()
    combined_encoder = G722Encoder()
    combined_decoder = G722Decoder()

    def encode_one_frame() -> bytes:
        return encode_encoder.encode(pcm)

    def decode_one_frame() -> bytes:
        return decode_decoder.decode(decode_payload)

    def encode_then_decode_one_frame() -> bytes:
        return combined_decoder.decode(combined_encoder.encode(pcm))

    return _MeasuredCost(
        encode_us=_best_us_per_frame(encode_one_frame),
        decode_us=_best_us_per_frame(decode_one_frame),
        combined_us=_best_us_per_frame(encode_then_decode_one_frame),
    )


# ---------------------------------------------------------------------------
# Documented constants: existence/range checks (deterministic)
#
# This test now FAILS until g722.py defines the combined packet-period budget
# constant, proving the previous separate 15 ms thresholds are no longer enough.
# ---------------------------------------------------------------------------


def test_g722_cost_and_budget_constants_exist() -> None:
    """Measured G.722 costs and combined budget are documented in g722.py."""
    assert hasattr(_g722_mod, "_G722_ENCODE_MEASURED_US_PER_FRAME_16K"), (
        "G722Encoder.encode must document its measured per-frame cost in "
        "_G722_ENCODE_MEASURED_US_PER_FRAME_16K (rule 22 / ADR-0094)."
    )
    assert hasattr(_g722_mod, "_G722_DECODE_MEASURED_US_PER_FRAME_16K"), (
        "G722Decoder.decode must document its measured per-frame cost in "
        "_G722_DECODE_MEASURED_US_PER_FRAME_16K (rule 22 / ADR-0094)."
    )
    assert hasattr(_g722_mod, "_G722_COMBINED_BUDGET_US_PER_FRAME_16K"), (
        "G.722 must document the combined encode+decode per-frame CPU budget in "
        "_G722_COMBINED_BUDGET_US_PER_FRAME_16K so CI can enforce the 20 ms packet "
        "period while G.722 remains preferred by default."
    )

    encode_us: float = _g722_mod._G722_ENCODE_MEASURED_US_PER_FRAME_16K
    decode_us: float = _g722_mod._G722_DECODE_MEASURED_US_PER_FRAME_16K
    budget_us: float = _g722_mod._G722_COMBINED_BUDGET_US_PER_FRAME_16K

    assert 0 < encode_us < _FRAME_PERIOD_US
    assert 0 < decode_us < _FRAME_PERIOD_US
    assert 0 < encode_us + decode_us < budget_us <= _FRAME_PERIOD_US


# ---------------------------------------------------------------------------
# Real CI budget gate: measured constants are tied to measured benchmark output,
# and combined encode+decode must stay below the packet-period budget.
# ---------------------------------------------------------------------------


def test_g722_measured_hot_path_stays_within_combined_budget() -> None:
    """Measure encode+decode together and enforce the real 20 ms packet budget.

    This is the MUSTFIX gate: a regression to 14 ms encode + 14 ms decode fails
    because ``combined_us`` exceeds the single-frame budget. The per-side checks
    tie the documented measured constants to real benchmark output, so stale
    constants cannot hide a CPU regression.
    """
    measured = _measure_hot_path()

    documented_encode_us: float = _g722_mod._G722_ENCODE_MEASURED_US_PER_FRAME_16K
    documented_decode_us: float = _g722_mod._G722_DECODE_MEASURED_US_PER_FRAME_16K
    combined_budget_us: float = _g722_mod._G722_COMBINED_BUDGET_US_PER_FRAME_16K

    assert measured.encode_us <= (
        documented_encode_us * _MEASURED_CONSTANT_TOLERANCE_MULTIPLIER
    ), (
        f"Measured G.722 encode cost {measured.encode_us:.1f} us/frame exceeds "
        f"the documented {documented_encode_us:.1f} us baseline by more than "
        f"{_MEASURED_CONSTANT_TOLERANCE_MULTIPLIER:g}x. Update the implementation "
        "or ADR-0094/g722.py with a new measured budget."
    )
    assert measured.decode_us <= (
        documented_decode_us * _MEASURED_CONSTANT_TOLERANCE_MULTIPLIER
    ), (
        f"Measured G.722 decode cost {measured.decode_us:.1f} us/frame exceeds "
        f"the documented {documented_decode_us:.1f} us baseline by more than "
        f"{_MEASURED_CONSTANT_TOLERANCE_MULTIPLIER:g}x. Update the implementation "
        "or ADR-0094/g722.py with a new measured budget."
    )
    assert measured.combined_us < combined_budget_us, (
        f"Measured combined G.722 encode+decode cost {measured.combined_us:.1f} "
        f"us/frame exceeds the {combined_budget_us:.0f} us combined packet-period "
        "budget while G.722 remains preferred by default. Demote G.722 or reduce "
        "the codec cost before shipping."
    )
