"""AEC hot-path CPU-budget gate + convergence bar (ADR-0110, backlog 1322).

The in-process echo canceller runs on the **synchronous RX media coroutine**
(``engine.py`` ``_inbound_gen`` -> ``_decode`` -> ``_cancel_echo``), inline with
no offload, on every 20 ms inbound packet whenever the agent's TTS echo is
returning — i.e. most of a call. ADR-0095 measured the shipped time-domain
per-sample NLMS canceller at ~39.8 ms/frame while adapting at 16 kHz (2x the
20 ms ptime) and ~21.2 ms at 8 kHz — a real per-packet stall. ADR-0110 replaces
the per-sample O(filter_len) recursion with a numpy-vectorised **block-NLMS**
whose per-frame cost is a handful of small matmuls, and pins the durable bar:

* **CPU budget** — ``cancel`` (with the matching ``push_reference``) must cost
  **< 20 ms/frame while adapting** at BOTH 8 kHz and 16 kHz, at the 512-tap
  ``_AEC_MAX_TAPS`` default. This is the ADR-0094-pattern packet-period gate that
  ADR-0095 said could not be met by the old default; it ships with the change so a
  regression that reinflates the constant fails CI instead of silently stalling.
* **Convergence** — the canceller must actually cancel: a deterministic synthetic
  echo path (delay + colouring) is driven to **>= 30 dB ERLE** (echo-return-loss
  enhancement) on the converged tail, at both rates.

The benchmark is CI-friendly (ADR-0094 pattern): no sleeps, no network, warm-up
before timing, best-of-repeated-batches to shed scheduler noise, and a coarse
20 ms budget (not a precision score). All signals are synthesised PCM16 so the
energy assertions are exact and deterministic.

No real gateway addresses, extension numbers, or device identifiers appear here.
"""

from __future__ import annotations

import math
import random
import struct
import timeit
from collections.abc import Callable, Sequence
from typing import Final

from hermes_voip.media.aec import EchoCanceller

_G711_RATE: Final[int] = 8_000
_G722_RATE: Final[int] = 16_000
_PTIME_MS: Final[int] = 20
_PACKET_BUDGET_US: Final[float] = float(_PTIME_MS * 1000)  # 20 000 us packet period
_MAX_TAPS: Final[int] = 512  # engine.py _AEC_MAX_TAPS — the worst-case default

# Warm-up + best-of-batches keep the coarse 20 ms budget robust on noisy CI hosts.
_WARMUP_FRAMES: Final[int] = 20
_BENCH_REPEATS: Final[int] = 5
_BENCH_FRAMES_PER_REPEAT: Final[int] = 20


# ---------------------------------------------------------------------------
# Deterministic PCM16 signal helpers (pure stdlib; exact integer energy)
# ---------------------------------------------------------------------------


def _pack(samples: Sequence[int]) -> bytes:
    clamped = [max(-32768, min(32767, int(s))) for s in samples]
    return struct.pack(f"<{len(clamped)}h", *clamped)


def _rms(pcm16: bytes | bytearray) -> float:
    vals = struct.unpack(f"<{len(pcm16) // 2}h", pcm16)
    if not vals:
        return 0.0
    return math.sqrt(sum(v * v for v in vals) / len(vals))


def _noise(n: int, *, amplitude: float, seed: int) -> list[int]:
    rng = random.Random(seed)  # noqa: S311 — test signal, not cryptographic
    peak = amplitude * 32767.0
    return [int(rng.uniform(-peak, peak)) for _ in range(n)]


def _echo_of(
    reference: Sequence[int], *, delay: int, gain: float, taps: Sequence[float]
) -> list[int]:
    """A deterministic echo: reference convolved with ``taps``, delayed, attenuated."""
    out = [0] * len(reference)
    for n in range(len(reference)):
        acc = 0.0
        for k, c in enumerate(taps):
            j = n - delay - k
            if 0 <= j < len(reference):
                acc += c * reference[j]
        out[n] = int(gain * acc)
    return out


def _frame_bytes(rate: int, *, seed: int) -> bytes:
    return _pack(_noise((rate * _PTIME_MS) // 1000, amplitude=0.4, seed=seed))


# ---------------------------------------------------------------------------
# Timing (ADR-0094 pattern)
# ---------------------------------------------------------------------------


def _best_us_per_frame(fn: Callable[[], object]) -> float:
    for _ in range(_WARMUP_FRAMES):
        fn()
    seconds = min(
        timeit.repeat(fn, repeat=_BENCH_REPEATS, number=_BENCH_FRAMES_PER_REPEAT)
    )
    return seconds / _BENCH_FRAMES_PER_REPEAT * 1_000_000.0


def _measure_cancel_us_per_frame(rate: int) -> float:
    """Best-of push_reference+cancel cost for one adapting 20 ms frame, in us.

    Drives the worst-case default: a full ``_AEC_MAX_TAPS`` filter, adapting (the
    near-end is an attenuated copy of the reference, so it is echo — not
    double-talk — and adaptation runs every frame).
    """
    aec = EchoCanceller(sample_rate=rate, filter_len=_MAX_TAPS, bulk_delay=0, mu=0.5)
    ref_frame = _frame_bytes(rate, seed=1)
    ref_vals = struct.unpack(f"<{len(ref_frame) // 2}h", ref_frame)
    echo_frame = _pack([int(s * 0.6) for s in ref_vals])

    def one_frame() -> bytes:
        aec.push_reference(ref_frame, sample_rate=rate)
        return aec.cancel(echo_frame)

    return _best_us_per_frame(one_frame)


# ---------------------------------------------------------------------------
# CPU-budget gate — the RED driver: the old per-sample NLMS is ~2x over at 16 kHz
# ---------------------------------------------------------------------------


def test_aec_cancel_within_packet_budget_16k() -> None:
    """Adapting cancel at 16 kHz / 512 taps stays under the 20 ms packet period."""
    measured_us = _measure_cancel_us_per_frame(_G722_RATE)
    assert measured_us < _PACKET_BUDGET_US, (
        f"AEC cancel cost {measured_us:.0f} us/frame at 16 kHz exceeds the "
        f"{_PACKET_BUDGET_US:.0f} us packet-period budget (ADR-0110): the "
        "synchronous RX coroutine stalls every packet while the echo returns."
    )


def test_aec_cancel_within_packet_budget_8k() -> None:
    """Adapting cancel at 8 kHz / 512 taps stays under the 20 ms packet period."""
    measured_us = _measure_cancel_us_per_frame(_G711_RATE)
    assert measured_us < _PACKET_BUDGET_US, (
        f"AEC cancel cost {measured_us:.0f} us/frame at 8 kHz exceeds the "
        f"{_PACKET_BUDGET_US:.0f} us packet-period budget (ADR-0110)."
    )


# ---------------------------------------------------------------------------
# Convergence bar — >= 30 dB ERLE on a clean synthetic echo at both rates
# ---------------------------------------------------------------------------


def _erle_db_on_converged_tail(rate: int) -> float:
    """Drive a clean synthetic echo and return the converged-tail ERLE in dB."""
    n = rate * 2  # 2 s — ample for a 512-tap filter to converge
    reference = _noise(n, amplitude=0.3, seed=99)
    echo = _echo_of(
        reference,
        delay=(rate * 8) // 1000,  # 8 ms bulk echo-return delay
        gain=0.5,
        taps=(1.0, 0.5, 0.25, -0.1),  # short room/hybrid colouring
    )
    aec = EchoCanceller(sample_rate=rate, filter_len=_MAX_TAPS, bulk_delay=0, mu=0.5)
    block = (rate * _PTIME_MS) // 1000
    residual = bytearray()
    for off in range(0, n, block):
        aec.push_reference(_pack(reference[off : off + block]), sample_rate=rate)
        residual += aec.cancel(_pack(echo[off : off + block]))

    tail = len(residual) // 5  # final 20 %, well after convergence
    echo_tail_rms = _rms(_pack(echo[len(echo) - tail // 2 :]))
    residual_tail_rms = _rms(residual[len(residual) - tail :])
    assert echo_tail_rms > 200.0, f"test echo too quiet: {echo_tail_rms}"
    assert residual_tail_rms > 0.0
    return 20.0 * math.log10(echo_tail_rms / residual_tail_rms)


def test_aec_converges_to_30db_erle_16k() -> None:
    """The canceller drives a known echo to >= 30 dB ERLE at 16 kHz."""
    erle = _erle_db_on_converged_tail(_G722_RATE)
    assert erle >= 30.0, f"ERLE {erle:.1f} dB < 30 dB at 16 kHz (echo not cancelled)"


def test_aec_converges_to_30db_erle_8k() -> None:
    """The canceller drives a known echo to >= 30 dB ERLE at 8 kHz."""
    erle = _erle_db_on_converged_tail(_G711_RATE)
    assert erle >= 30.0, f"ERLE {erle:.1f} dB < 30 dB at 8 kHz (echo not cancelled)"
