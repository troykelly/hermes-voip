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

Frame shape:
  * 8 kHz / 20 ms -> 160 samples (one RTP G.711 frame)
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

_RATE_8K: Final[int] = 8_000
_RATE_16K: Final[int] = 16_000
_FRAME_MS: Final[int] = 20  # standard RTP packetisation

# 20 ms x sample rate -> sample count per frame
_SAMPLES_8K: Final[int] = _RATE_8K * _FRAME_MS // 1000  # 160
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


def _silence_frame(rate: int, ts_ns: int = 0) -> PcmFrame:
    """Return a 20 ms all-zeros PCM16 frame at ``rate`` Hz."""
    n = rate * _FRAME_MS // 1000
    return PcmFrame(samples=b"\x00\x00" * n, sample_rate=rate, monotonic_ts_ns=ts_ns)


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
# that the count does not exceed _MAX_GOERTZEL_PASSES_WORST_CASE.  No wall
# clock; no OS jitter; fully deterministic.
# ---------------------------------------------------------------------------


def test_on_audio_frame_goertzel_pass_count_worst_case() -> None:
    """on_audio_frame makes <= _MAX_GOERTZEL_PASSES_WORST_CASE Goertzel calls.

    Exercises the documented worst-case path: outbound call, machine classified,
    beep check active, frame dominated by a pure 1000 Hz tone.  All 4 BEEP guard
    bins run; CNG and CED fraction-checks fail after 1 call each.  Total: 7 passes.

    This is the primary regression gate for on_audio_frame compute cost.  A change
    that adds a new tone check, a new guard bin, or restructures the early-exit
    logic will fail this assertion deterministically, with no dependence on timing.
    """
    det = _worst_case_detector()
    # 1000 Hz pure tone: triggers full BEEP guard-bin evaluation.
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

    # Reset state and run exactly one worst-case call under the counting proxy.
    det._ready_emitted = False
    det._cng_emitted = False
    det._ced_emitted = False

    with patch.object(_cp_mod, "_goertzel_power", side_effect=counting_goertzel):
        det.on_audio_frame(beep_frame)

    expected = 2 + 1 + len(_GUARD_OFFSETS_HZ)  # 1 CNG + 1 CED + 5 BEEP
    assert call_count <= _MAX_GOERTZEL_PASSES_WORST_CASE, (
        f"on_audio_frame made {call_count} Goertzel calls on the worst-case path "
        f"(budget <= {_MAX_GOERTZEL_PASSES_WORST_CASE}); "
        "check for a new tone check or guard-bin addition on this path. "
        f"Expected breakdown: 1 (CNG) + 1 (CED) + "
        f"{1 + len(_GUARD_OFFSETS_HZ)} (BEEP target+guards) = {expected} passes"
    )


# ---------------------------------------------------------------------------
# Test 3 -- Allocation growth (deterministic regression guard)
# ---------------------------------------------------------------------------


def test_on_audio_frame_allocation_growth_within_budget_16k() -> None:
    """on_audio_frame has bounded per-iteration heap-object growth on the worst path.

    Uses tracemalloc snapshot diff over N iterations to measure per-iteration
    heap-object growth in call_progress.py.  A pure allocation-free path grows by
    0 objects/call; the current implementation allocates a list[float] plus n float
    objects per call (the samples list comprehension), giving ~n+1 objects per call.
    The ceiling of 2*n+10 is generous; a regression (e.g. an extra list copy or a
    dict allocation per call) would exceed it without any timing dependency.

    At 16 kHz worst-case: n=320, ceiling = 2*320+10 = 650 objects per call.
    We run 10 iterations and assert total growth <= 10 * 650 = 6 500 objects.
    """
    det = _worst_case_detector()
    beep_frame = _sine_frame(float(_BEEP_HZ), _RATE_16K)

    # Warm up so Python caches and lazy state settle before we snapshot.
    for _ in range(50):
        det._ready_emitted = False
        det._cng_emitted = False
        det._ced_emitted = False
        det.on_audio_frame(beep_frame)

    n_iters = 10
    max_allocs_per_call = 2 * _SAMPLES_16K + 10  # 650 objects

    tracemalloc.start()
    try:
        snap_before = tracemalloc.take_snapshot()
        for _ in range(n_iters):
            det._ready_emitted = False
            det._cng_emitted = False
            det._ced_emitted = False
            det.on_audio_frame(beep_frame)
        snap_after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()

    # Diff the two snapshots: only count net-positive growth in call_progress.py.
    stats = snap_after.compare_to(snap_before, "lineno")
    cp_growth = sum(
        s.count_diff
        for s in stats
        if "call_progress" in s.traceback[0].filename and s.count_diff > 0
    )

    max_total = n_iters * max_allocs_per_call  # 6 500 objects across 10 calls
    assert cp_growth <= max_total, (
        f"on_audio_frame grew call_progress.py heap by {cp_growth} objects over "
        f"{n_iters} calls (budget <= {max_total}, i.e. {max_allocs_per_call}/call); "
        "check for accidental per-call list copies or extra per-sample allocations"
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
