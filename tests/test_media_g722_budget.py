"""G.722 hot-path CPU-budget gate (backlog [high] efficiency, ADR-0094).

Rule 22 requires every hot-path module to have a concrete per-frame cost
measurement documented in source and gated in a test.  The G.722 pure-Python
encode and decode paths run on every RTP packet at 50 pps (20 ms ptime):
G722Encoder.encode on the outbound send_audio path, G722Decoder.decode on the
inbound receive path.  This test gates both.

Design (mirrors test_call_progress_microbench.py):
  - The PRIMARY gate is that documented constants exist in media/g722.py and are
    within plausible bounds.  The constants commit the measured numbers to source
    so rule 22 is satisfied and a future refactor that doubles the cost is caught
    by a CI human reading the diff -- not just by wall-clock timing.
  - The SECONDARY gate is a wall-clock ceiling (15 ms = 75 % of the 20 ms frame
    period).  This is intentionally very generous so CI scheduling jitter cannot
    cause false failures; it catches only catastrophic regressions (e.g. an
    accidental O(n^2) path or importing a model on every call).
  - There is NO tight wall-clock budget gate: the actual cost varies across CPython
    versions and CI hardware.  The documented constant is the record; the ceiling
    guards against the extreme regression tail.

Measured numbers (2026-06-28, CPython 3.13.5, devcontainer):
  Encode: ~3 400 us / 20 ms frame (one 320-sample pair pass through QMF + ADPCM)
  Decode: ~3 300 us / 20 ms frame (inverse QMF + ADPCM reconstruction)
  Combined: ~6 700 us / frame (~33 % of the 20 ms ptime)

The 15 ms ceiling is 4-5x the observed cost -- wide enough for a heavily loaded
CI runner but tight enough to catch an accidental O(n^2) regression.

No networking, no wall-clock sleeps, no real gateway addresses.  Runs in the
DEFAULT gate (pure-Python G.722, no extras required).
"""

from __future__ import annotations

import math
import struct
import timeit
from typing import Final

import hermes_voip.media.g722 as _g722_mod
from hermes_voip.media.g722 import G722Decoder, G722Encoder

# ---------------------------------------------------------------------------
# Frame constants
# ---------------------------------------------------------------------------

_RATE: Final[int] = 16_000  # G.722 audio sample rate
_PTIME_MS: Final[int] = 20  # standard RTP packetisation
_SAMPLES_PER_FRAME: Final[int] = _RATE * _PTIME_MS // 1000  # 320
_BYTES_PER_FRAME: Final[int] = _SAMPLES_PER_FRAME // 2  # 160 G.722 octets

# ---------------------------------------------------------------------------
# Budget ceiling (rule 22 / ADR-0094)
# ---------------------------------------------------------------------------
# Primary gate: the documented constants in g722.py (see Test 1).
# Secondary gate: this very generous wall-clock ceiling.
# 15 ms = 75 % of the 20 ms frame period.  A pure-Python encode at ~3.4 ms
# has roughly 4x headroom here; only a catastrophic regression (accidental
# O(n^2), model load, full-frame copy loop) will breach the ceiling.
_WALL_CLOCK_CEILING_US: Final[float] = 15_000.0  # 15 ms


# ---------------------------------------------------------------------------
# Frame builders
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


def _g722_frame() -> bytes:
    """Return one encoded 20 ms G.722 frame (160 bytes) for decode tests."""
    return G722Encoder().encode(_pcm_frame())


# ---------------------------------------------------------------------------
# Test 1 -- documented cost constants must exist in g722.py (deterministic)
#
# This test FAILS before _G722_ENCODE_MEASURED_US_PER_FRAME_16K and
# _G722_DECODE_MEASURED_US_PER_FRAME_16K are added to media/g722.py.
# ---------------------------------------------------------------------------


def test_g722_encode_cost_constant_exists() -> None:
    """G722Encoder.encode must document its measured cost in a module constant.

    _G722_ENCODE_MEASURED_US_PER_FRAME_16K records the per-frame encode cost
    (us) at 16 kHz (320 samples -> 160 G.722 octets).  The constant satisfies
    rule 22 (efficiency is documented, not left implicit) and makes the ADR
    claim verifiable from source.  Any future change that significantly moves
    the cost must update the constant, making the regression visible in a diff.
    """
    assert hasattr(_g722_mod, "_G722_ENCODE_MEASURED_US_PER_FRAME_16K"), (
        "G722Encoder.encode must document its measured per-frame cost in a "
        "module-level constant _G722_ENCODE_MEASURED_US_PER_FRAME_16K (rule 22 "
        "/ ADR-0094).  Add it to src/hermes_voip/media/g722.py."
    )
    measured = _g722_mod._G722_ENCODE_MEASURED_US_PER_FRAME_16K
    assert isinstance(measured, (int, float)), (
        "_G722_ENCODE_MEASURED_US_PER_FRAME_16K must be a numeric constant (us)"
    )
    assert 0 < measured < 20_000, (
        f"_G722_ENCODE_MEASURED_US_PER_FRAME_16K={measured} us is out of the "
        "plausible (0, 20000) range; update the constant to the actual measurement"
    )


def test_g722_decode_cost_constant_exists() -> None:
    """G722Decoder.decode must document its measured cost in a module constant.

    _G722_DECODE_MEASURED_US_PER_FRAME_16K records the per-frame decode cost
    (us) at 16 kHz (160 G.722 octets -> 320 PCM16 samples).  Same rule 22
    rationale as the encode constant.
    """
    assert hasattr(_g722_mod, "_G722_DECODE_MEASURED_US_PER_FRAME_16K"), (
        "G722Decoder.decode must document its measured per-frame cost in a "
        "module-level constant _G722_DECODE_MEASURED_US_PER_FRAME_16K (rule 22 "
        "/ ADR-0094).  Add it to src/hermes_voip/media/g722.py."
    )
    measured = _g722_mod._G722_DECODE_MEASURED_US_PER_FRAME_16K
    assert isinstance(measured, (int, float)), (
        "_G722_DECODE_MEASURED_US_PER_FRAME_16K must be a numeric constant (us)"
    )
    assert 0 < measured < 20_000, (
        f"_G722_DECODE_MEASURED_US_PER_FRAME_16K={measured} us is out of the "
        "plausible (0, 20000) range; update the constant to the actual measurement"
    )


# ---------------------------------------------------------------------------
# Test 2 -- wall-clock ceiling for G.722 encode (secondary safety net)
#
# The ceiling is very generous (15 ms).  The primary gate is the documented
# constant in Test 1.  A tight wall-clock budget would be CI-flaky; this only
# catches catastrophic regressions.
# ---------------------------------------------------------------------------


def test_g722_encode_stays_within_wall_clock_ceiling() -> None:
    """G722Encoder.encode for one 20 ms frame must complete within 15 ms.

    This is a SECONDARY gate -- the ceiling is 4-5x the measured cost so CI
    scheduling jitter cannot produce false failures.  The PRIMARY gate is the
    documented constant (Test 1).  Only an O(n^2) regression, an accidental
    model load, or a catastrophic algorithmic change would breach 15 ms.

    Uses a stateful encoder (one instance per call direction) to match the
    production path: G722Encoder is constructed once per call and reused.
    """
    pcm = _pcm_frame()
    encoder = G722Encoder()
    # Warm up the encoder's Python state and JIT-equivalent caches.
    for _ in range(50):
        encoder.encode(pcm)

    # Re-construct to start from a clean state matching the ADR-0022 description
    # ("construct a fresh encoder per call"), but keep it stateful for the timing
    # loop (the real path feeds frames continuously into one encoder).
    encoder = G722Encoder()
    reps = 200
    elapsed_us = timeit.timeit(lambda: encoder.encode(pcm), number=reps) / reps * 1e6

    assert elapsed_us < _WALL_CLOCK_CEILING_US, (
        f"G722Encoder.encode for one 20 ms frame took {elapsed_us:.1f} us, "
        f"exceeding the {_WALL_CLOCK_CEILING_US:.0f} us ceiling (15 ms = 75 % "
        "of the 20 ms ptime).  This is a catastrophic regression -- check for an "
        "accidental O(n^2) path, a model load inside encode(), or a structural "
        "change to the ADPCM loop.  See ADR-0094."
    )


# ---------------------------------------------------------------------------
# Test 3 -- wall-clock ceiling for G.722 decode (secondary safety net)
# ---------------------------------------------------------------------------


def test_g722_decode_stays_within_wall_clock_ceiling() -> None:
    """G722Decoder.decode for one 20 ms frame must complete within 15 ms.

    Same rationale and ceiling as Test 2 for encode.
    """
    g722 = _g722_frame()
    decoder = G722Decoder()
    # Warm up.
    for _ in range(50):
        decoder.decode(g722)

    decoder = G722Decoder()
    reps = 200
    elapsed_us = timeit.timeit(lambda: decoder.decode(g722), number=reps) / reps * 1e6

    assert elapsed_us < _WALL_CLOCK_CEILING_US, (
        f"G722Decoder.decode for one 20 ms frame took {elapsed_us:.1f} us, "
        f"exceeding the {_WALL_CLOCK_CEILING_US:.0f} us ceiling (15 ms = 75 % "
        "of the 20 ms ptime).  See ADR-0094."
    )


# ---------------------------------------------------------------------------
# Test 4 -- combined encode + decode budget is documented
#
# Asserts that the two constants together produce a sane combined budget, so
# the ADR's claim "well within the 20 ms frame period" is machine-checked.
# ---------------------------------------------------------------------------


def test_g722_combined_budget_is_within_frame_period() -> None:
    """The documented encode + decode cost is less than one 20 ms frame.

    This catches a future update that raises one constant past 20 ms (which
    would make the ADR claim false).  The actual margin is large (~3 x),
    so the documented numbers would have to be wildly wrong to trigger this.
    """
    encode_us: float = _g722_mod._G722_ENCODE_MEASURED_US_PER_FRAME_16K
    decode_us: float = _g722_mod._G722_DECODE_MEASURED_US_PER_FRAME_16K
    frame_us = _PTIME_MS * 1000  # 20 000 us
    assert encode_us + decode_us < frame_us, (
        f"Documented G.722 encode ({encode_us} us) + decode ({decode_us} us) = "
        f"{encode_us + decode_us} us >= one 20 ms frame ({frame_us} us).  "
        "Update ADR-0094 and the constants to reflect the actual measured cost."
    )
