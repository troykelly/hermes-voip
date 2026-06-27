"""Microbenchmark / operation-count guard for CallProgressDetector.on_audio_frame.

Rule 22 requires a concrete per-frame cost measurement documented and gated for every
hot-path module.  ``on_audio_frame`` runs on every decoded inbound frame (50 frames/s
at 20 ms packetisation) when call-progress detection is enabled.

Design: the primary regression gate is a deterministic Goertzel-pass count (not wall
clock) for the documented worst-case path: outbound call, machine already classified,
beep path active, frame carrying a pure 1 kHz tone that exercises all guard bins for
the beep check.  The wall-clock assertion uses a very generous 5 ms ceiling so it
catches only catastrophic regressions (e.g. an accidental O(n^2) or model load)
without being sensitive to CI scheduling jitter.

Worst-case frame:
  - ``outbound=True``, machine decided, ``_ready_emitted=False`` (beep check active)
  - A pure 1000 Hz (BEEP) tone at 16 kHz / 320 samples
  - Execution path: energy check passes, CNG fraction check fails (1 Goertzel call),
    CED fraction check fails (1 Goertzel call), BEEP fraction check passes then all
    4 guard bins evaluated (5 Goertzel calls) = 7 total Goertzel passes per frame.
    The 15-pass figure in the old comment assumed each of the three tones could
    simultaneously dominate a single frame; that is physically impossible for pure
    tones at distinct frequencies, so 7 is the correct tight upper bound.

Frame shape used in all tests:
  * 16 kHz / 20 ms -> 320 samples (resampled conversational path, ADR-0017)
"""

from __future__ import annotations

import math
import struct
import timeit
import tracemalloc
from typing import Final
from unittest.mock import patch

import hermes_voip.media.call_progress as _cp_mod
from hermes_voip.dtmf import _goertzel_power as _real_goertzel
from hermes_voip.media.call_progress import (
    _BEEP_HZ,
    _GUARD_OFFSETS_HZ,
    CallProgressDetector,
    _AmdState,
)
from hermes_voip.providers.audio import PcmFrame

# ---------------------------------------------------------------------------
# Frame constants
# ---------------------------------------------------------------------------

_RATE_16K: Final[int] = 16_000
_FRAME_MS: Final[int] = 20  # standard RTP packetisation

# 20 ms x sample rate -> sample count per frame
_SAMPLES_16K: Final[int] = _RATE_16K * _FRAME_MS // 1000  # 320

# ---------------------------------------------------------------------------
# Frame budget (ADR-0005)
# ---------------------------------------------------------------------------
# The 20 ms frame period is the outer wall.  call_progress.on_audio_frame shares
# the pump asyncio task with VAD, DTMF, and jitter-buffer work.  The per-component
# *budget* is not a fixed fraction; what rule 22 requires is a measured number and
# a documented ceiling.  We use 5 ms (25 % of the frame period) as the wall-clock
# guard ceiling -- generous enough that CI jitter cannot cause a false failure,
# tight enough to catch a catastrophic regression (e.g. an O(n^2) change or an
# accidental model load).  The ACTUAL measured cost (~131 us at 16 kHz worst-case)
# is far below this; the wall-clock assertion is a safety net, not a tight budget
# gate.  The primary, deterministic gate is the Goertzel pass count (Test 2 below).
# See the docstring of ``on_audio_frame`` for the concrete measured number.
_WALL_CLOCK_CEILING_US: Final[float] = 5_000.0  # 5 ms = 25 % of frame period

# ---------------------------------------------------------------------------
# Goertzel pass count bound
# ---------------------------------------------------------------------------
# On the documented worst-case path (outbound + machine decided + beep active,
# frame dominated by 1000 Hz):
#
#   CNG (1100 Hz):  1 target call, fraction fails, no guards  = 1 pass
#   CED (2100 Hz):  1 target call, fraction fails, no guards  = 1 pass
#   BEEP (1000 Hz): 1 target call, fraction passes, 4 guards  = 5 passes
#   Total:                                                     = 7 passes
#
# The ``_GUARD_OFFSETS_HZ`` tuple has 4 entries; all 4 are valid (in-band) at
# 1000 Hz for 16 kHz (Nyquist = 8000 Hz); none are clipped.  The 7-pass bound is
# exact and deterministic, not an approximation.  A regression that adds per-frame
# Goertzel work (e.g. a new guard bin, a new tone check) fails this test.

_MAX_GOERTZEL_PASSES_WORST_CASE: Final[int] = 7


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------


def _sine_frame(
    freq: float, rate: int, ts_ns: int = 0, amplitude: float = 0.5
) -> PcmFrame:
    """Return a 20 ms PCM16 frame containing a pure sine at ``freq`` Hz."""
    n = rate * _FRAME_MS // 1000
    scale = amplitude * 32767.0
    buf = bytearray()
    for k in range(n):
        v = int(scale * math.sin(2.0 * math.pi * freq * k / rate))
        buf += struct.pack("<h", max(-32768, min(32767, v)))
    return PcmFrame(samples=bytes(buf), sample_rate=rate, monotonic_ts_ns=ts_ns)


def _worst_case_detector() -> CallProgressDetector:
    """Return a ``CallProgressDetector`` in the documented worst-case state.

    outbound=True, machine already classified (``_AmdState.DECIDED``), beep check
    active (``_ready_emitted=False``), CNG/CED not yet emitted.  Feeding this
    detector a pure 1000 Hz frame exercises the maximum-Goertzel-pass path.
    """
    det = CallProgressDetector(sample_rate=_RATE_16K, outbound=True)
    # Drive the AMD state machine directly to DECIDED without emitting events so
    # the beep check runs on every frame.  These are private attributes accessed
    # here as test-only setup shortcuts — SLF001 is not in the project ruleset.
    det._amd._decided_machine = True
    det._amd._state = _AmdState.DECIDED
    return det


# ---------------------------------------------------------------------------
# Test 1 -- Module-level cost constant (deterministic documentation guard)
#
# This test FAILS before _ON_AUDIO_FRAME_MEASURED_US_PER_FRAME_16K exists.
# ---------------------------------------------------------------------------


def test_on_audio_frame_cost_constant_exists() -> None:
    """on_audio_frame must document its measured cost in a module-level constant.

    The constant _ON_AUDIO_FRAME_MEASURED_US_PER_FRAME_16K records the per-frame
    us cost at 16 kHz (320 samples) so rule 22 is satisfied: the concrete number
    is committed to source, not left implicit.  The wall-clock test below uses
    this constant as the authoritative reference.
    """
    assert hasattr(_cp_mod, "_ON_AUDIO_FRAME_MEASURED_US_PER_FRAME_16K"), (
        "on_audio_frame must document its measured per-frame cost in a module-level "
        "constant _ON_AUDIO_FRAME_MEASURED_US_PER_FRAME_16K (rule 22)"
    )
    measured = _cp_mod._ON_AUDIO_FRAME_MEASURED_US_PER_FRAME_16K
    assert isinstance(measured, (int, float)), (
        "_ON_AUDIO_FRAME_MEASURED_US_PER_FRAME_16K must be a numeric constant (us)"
    )
    # The constant must be positive and below the 20 ms frame period.
    assert 0 < measured < 20_000, (
        f"_ON_AUDIO_FRAME_MEASURED_US_PER_FRAME_16K={measured} us is out of range "
        "(0, 20000); update the constant to the actual measurement"
    )


# ---------------------------------------------------------------------------
# Test 2 -- Goertzel pass count (primary deterministic regression gate)
#
# This is the authoritative efficiency gate.  It counts the number of
# ``_goertzel_power`` calls made on the documented worst-case path and asserts
# the count == _MAX_GOERTZEL_PASSES_WORST_CASE (exact, not <=).  An exact
# assertion catches both regressions (new tone check or guard bin) AND silent
# setup errors that bypass the beep path and produce fewer calls.  No wall
# clock; no OS jitter; fully deterministic.
# ---------------------------------------------------------------------------


def test_on_audio_frame_goertzel_pass_count_worst_case() -> None:
    """on_audio_frame makes exactly _MAX_GOERTZEL_PASSES_WORST_CASE Goertzel calls.

    Exercises the documented worst-case path: outbound call, machine classified,
    beep check active, frame dominated by a pure 1000 Hz tone.  All 4 BEEP guard
    bins run; CNG and CED fraction-checks fail after 1 target call each.
    Exact count: 1 (CNG) + 1 (CED) + 5 (BEEP target + 4 guards) = 7 passes.

    The assertion is == (exact), not <=, so a setup error that silently bypasses
    the beep path and produces fewer calls also fails loudly.  A pre-flight check
    confirms the detector is genuinely in the beep-path-active state before
    counting begins.

    This is the primary regression gate for on_audio_frame compute cost.  Any
    change that adds a tone check, a guard bin, or restructures early-exit logic
    will fail this test deterministically with no timing dependency.
    """
    det = _worst_case_detector()
    # 1000 Hz pure tone: CNG/CED fraction-checks fail (1 call each); BEEP
    # fraction passes then all 4 guard bins run = 5 calls.  Total: 7.
    beep_frame = _sine_frame(float(_BEEP_HZ), _RATE_16K)

    # Warm up so all Python caches and JIT-equivalent state settle.
    for _ in range(20):
        det._ready_emitted = False
        det._cng_emitted = False
        det._ced_emitted = False
        det.on_audio_frame(beep_frame)

    call_count = 0

    def counting_goertzel(samples: list[float], freq: int, rate: int) -> float:
        """Proxy that counts _goertzel_power calls without altering results."""
        nonlocal call_count
        call_count += 1
        return _real_goertzel(samples, freq, rate)

    # Reset to the exact worst-case state: outbound, machine decided, beep active.
    det._ready_emitted = False
    det._cng_emitted = False
    det._ced_emitted = False

    # Pre-flight: verify the detector is in the beep-path-active state so that a
    # broken _worst_case_detector() that omits the machine classification cannot
    # silently produce 2 passes instead of 7 and still pass a <= guard.
    assert det._amd._decided_machine, (
        "pre-flight: detector must have AMD in decided_machine=True state "
        "for the worst-case beep path to be active; check _worst_case_detector()"
    )
    assert not det._ready_emitted, (
        "pre-flight: _ready_emitted must be False so the beep check runs"
    )

    with patch.object(_cp_mod, "_goertzel_power", side_effect=counting_goertzel):
        det.on_audio_frame(beep_frame)

    expected = 2 + 1 + len(_GUARD_OFFSETS_HZ)  # 1 CNG + 1 CED + 5 BEEP
    assert call_count == _MAX_GOERTZEL_PASSES_WORST_CASE, (
        f"on_audio_frame made {call_count} Goertzel calls on the worst-case path "
        f"(expected exactly {_MAX_GOERTZEL_PASSES_WORST_CASE}). "
        f"Breakdown must be: 1 (CNG target) + 1 (CED target) + "
        f"{1 + len(_GUARD_OFFSETS_HZ)} (BEEP target+{len(_GUARD_OFFSETS_HZ)} guards)"
        f" = {expected}. "
        "If count < expected, the beep path was not reached (check setup). "
        "If count > expected, a new tone check or guard bin was added."
    )


# ---------------------------------------------------------------------------
# Test 3 -- Transient allocation peak (deterministic regression guard)
#
# Justification for this approach (rule 19): the previous version used
# tracemalloc snapshot-diff (compare_to), which only sees LIVE blocks at
# snapshot time.  The per-call samples list and float objects are freed before
# the snapshot, so count_diff was ~0 regardless of transient regressions --
# a non-gating assertion that cannot fail is worse than none (rule 19).
#
# tracemalloc.get_traced_memory() returns (current, peak) where peak is the
# high-water mark DURING the tracing window, capturing the maximum live-set
# even after those allocations are freed.  Sequential worst-case calls each
# produce the same transient heap footprint (list + n floats + misc); they do
# not accumulate across calls, so peak ≈ one call's transient high-water.
# Measured on this machine: ~22 KB peak for 320 float samples.  The 50 KB
# ceiling is ~2.3x the measured peak, tight enough to catch a regression (e.g.
# an extra full list copy per call would roughly double the peak to ~44 KB) yet
# loose enough that Python version differences in object overhead cannot flip it.
# ---------------------------------------------------------------------------

# Measured transient peak on this machine for a single worst-case call at 16 kHz:
# ~22 KB (list[float] of 320 floats + misc per-call temporaries).
# 50 KB ≈ 2.3x the measured value; catches an extra full-list-copy regression.
_ALLOC_PEAK_BYTES_CEILING: Final[int] = 50_000  # 50 KB


def test_on_audio_frame_transient_alloc_peak_worst_case_16k() -> None:
    """on_audio_frame transient heap peak is < 50 KB on the worst-case path.

    Uses tracemalloc.get_traced_memory() peak (high-water mark during the window)
    to capture transient per-call allocations even though they are freed between
    calls.  The snapshot-diff approach is NOT used here because compare_to() only
    sees live blocks at snapshot time; freed transients are invisible, making it
    non-gating.

    Measured peak on this machine: ~22 KB (320 floats + list + misc).  The 50 KB
    ceiling catches an extra-list-copy regression (~44 KB) without depending on
    timing or precise object sizes.
    """
    det = _worst_case_detector()
    beep_frame = _sine_frame(float(_BEEP_HZ), _RATE_16K)

    # Warm up so Python caches and lazy state settle before measuring.
    for _ in range(100):
        det._ready_emitted = False
        det._cng_emitted = False
        det._ced_emitted = False
        det.on_audio_frame(beep_frame)

    tracemalloc.start()
    try:
        # reset_peak so the high-water mark reflects only the calls below.
        tracemalloc.reset_peak()
        for _ in range(10):
            det._ready_emitted = False
            det._cng_emitted = False
            det._ced_emitted = False
            det.on_audio_frame(beep_frame)
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < _ALLOC_PEAK_BYTES_CEILING, (
        f"on_audio_frame transient heap peak was {peak} bytes on worst-case path "
        f"(ceiling {_ALLOC_PEAK_BYTES_CEILING} bytes / 50 KB); "
        "check for an extra per-call list copy or large temporary allocation. "
        f"Measured baseline on this machine: ~22 KB "
        f"(320 floats + list + misc per-call temporaries)."
    )


# ---------------------------------------------------------------------------
# Test 4 -- Wall-clock ceiling (generous safety net, NOT the primary gate)
#
# This tests the WORST-CASE path (outbound + machine classified + beep active,
# pure 1000 Hz frame), NOT the silence early-exit path.  The primary gate is
# Test 2 (Goertzel count); this is a coarse safety net for catastrophic
# regressions that the Goertzel count alone would miss (e.g. an accidental
# blocking call or FFT import added upstream of the Goertzel check).
# ---------------------------------------------------------------------------


def test_on_audio_frame_wall_clock_within_5ms_ceiling_16k() -> None:
    """on_audio_frame worst-case path completes in < 5 ms per frame at 16 kHz.

    Uses the documented worst-case frame: outbound + machine classified + beep
    check active, 1000 Hz pure tone at 16 kHz / 320 samples.  All 7 Goertzel
    passes run (1 CNG + 1 CED + 5 BEEP).

    The MEASURED cost on this machine is ~131 us/frame (see
    ``_ON_AUDIO_FRAME_MEASURED_US_PER_FRAME_16K``).  The 5 ms ceiling is ~38 x
    the measured cost, so CI scheduling jitter cannot cause a false failure.

    This assertion is NOT the primary regression gate -- the Goertzel pass count
    (test_on_audio_frame_goertzel_pass_count_worst_case) is.  This test catches
    only catastrophic regressions (>38 x the measured cost).
    """
    det = _worst_case_detector()
    beep_frame = _sine_frame(float(_BEEP_HZ), _RATE_16K)

    # Warm up so Python caches settle.
    for _ in range(200):
        det._ready_emitted = False
        det._cng_emitted = False
        det._ced_emitted = False
        det.on_audio_frame(beep_frame)

    def _call() -> None:
        det._ready_emitted = False
        det._cng_emitted = False
        det._ced_emitted = False
        det.on_audio_frame(beep_frame)

    n_iters = 1_000
    elapsed_s = timeit.timeit(_call, number=n_iters)
    us_per_frame = elapsed_s / n_iters * 1_000_000

    # Document the measured number -- this is what rule 22 requires.
    measured = _cp_mod._ON_AUDIO_FRAME_MEASURED_US_PER_FRAME_16K

    # Coarse safety-net assertion: CI must not regress beyond the generous ceiling.
    # NOT the authoritative regression signal -- see the Goertzel-count test above.
    assert us_per_frame < _WALL_CLOCK_CEILING_US, (
        f"on_audio_frame took {us_per_frame:.1f} us/frame (worst-case path); "
        f"ceiling is {_WALL_CLOCK_CEILING_US:.0f} us (25 % of the 20 ms period). "
        f"Documented baseline: {measured} us/frame. "
        "If the median has genuinely risen, update the constant and docstring. "
        "Also run the Goertzel-count test to find the root cause."
    )
