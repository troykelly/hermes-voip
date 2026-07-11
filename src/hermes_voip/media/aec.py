"""In-process acoustic echo canceller — numpy block-NLMS adaptive filter (ADR-0110).

The telephony gateway reflects the agent's own rendered TTS back on the inbound
leg (a delayed, attenuated, room/hybrid-filtered copy of audio we *already hold*
— the outbound PCM). Without cancellation the VAD/ASR transcribe that echo as the
caller and barge the agent in (ADR-0023's self-interruption loop); ADR-0023
worked around it with a sluggish 600 ms sustained-speech gate. This module
cancels the echo *before* the VAD/ASR see it, so the gate's threshold can drop
and barge-in becomes responsive.

It is a **reference-based** canceller: the echo is correlated with the known
outbound reference (it *is* the reference, delayed + filtered), so an adaptive
FIR filter learns the echo path and subtracts the estimate; the caller's speech
is *uncorrelated* with the reference, so it survives the subtraction (a genuine
barge-in still fires). The filter is **NLMS** (Normalised Least-Mean-Squares):
``w += (mu * e / (||x||^2 + eps)) * x`` -- the energy normalisation makes
convergence speed independent of the (varying) TTS level.

**Why block-NLMS + numpy (ADR-0110, supersedes ADR-0033's constraints).** The
shipped per-sample time-domain recursion cost ``O(filter_len)`` multiply-adds per
sample, twice per adapting sample — ~91 ms/frame at 16 kHz / 512 taps on the RX
media coroutine (ADR-0095), 2x+ the 20 ms packet period. This module vectorises
that with **numpy**: it forms the per-sample delayed-reference matrix once and runs
the estimate + tap update as small matmuls over an :data:`_ADAPT_BLOCK`-sample
sub-block, updating the taps once per sub-block instead of once per sample. The
per-frame cost collapses to a handful of small matmuls (well under 20 ms at both
rates — see the measured constants below and ``tests/test_media_aec_budget.py``).
Because a whole ``cancel`` frame is processed and returned in one call, there is
**no algorithmic delay** — :meth:`cancel` returns exactly the samples it is given.

The tap update is normalised by the **total sub-block reference energy**
(``sum_i ||x_i||^2``), which keeps the update unconditionally stable for
``mu`` in ``(0, 2)`` on correlated (tonal) and broadband references alike; a small
sub-block (``_ADAPT_BLOCK``) keeps convergence tracking the per-sample recursion
(at ``_ADAPT_BLOCK == 1`` the two are identical). numpy lives in the base
dependencies (AEC is on by default, and this module is type-checked by the
default mypy gate), so ``import hermes_voip.media.aec`` needs no optional extra.

Rates: the canceller runs at the inbound **analysis rate** (8 kHz G.711, 16 kHz
G.722/Opus). :meth:`push_reference` downsamples an off-analysis-rate reference
(Opus's 48 kHz wire) to the analysis rate so the two inputs align.

Time alignment (preserved from ADR-0033, non-negotiable): the far-end (reference)
and near-end are **sample-synchronous** streams — both advance one sample per
sample-period from the call start. The engine does NOT interleave a push 1:1 with
a cancel: on a real call ``push_reference`` (TX) runs ahead of ``cancel`` (RX) by
the system delay (the greeting plays before any inbound; the jitter buffer holds
the near-end; async scheduling batches). So the canceller consumes the far-end
through an **independent read cursor** that advances one far-end sample per
near-end sample (NOT anchored to the newest push), keeping the two time-locked;
the adaptive taps then span the echo round-trip delay forward from that cursor
(``bulk_delay`` skips a known dead delay first). Anchoring on the newest push
instead — wrong — would point the window at *future* TTS uncorrelated with the
current echo, diverging the filter and mis-firing the double-talk guard.
"""

from __future__ import annotations

from typing import Final

import numpy as np
import numpy.typing as npt

from hermes_voip.media.audio import Resampler
from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE

__all__ = ["EchoCanceller"]

_INT16_MIN = -32768
_INT16_MAX = 32767

# PCM16 little-endian sample dtype — matches the codebase's ``struct`` "<h" wire
# convention on every host, independent of the machine's native byte order.
_PCM16_LE: Final[str] = "<i2"

# NLMS step-size (mu) bounds: the open interval (0, 2). 0 never adapts; >= 2 diverges.
_MU_MIN = 0.0
_MU_MAX = 2.0

# NLMS regulariser added to the reference-window energy in the denominator, so the
# update step stays bounded when the reference is near-silent (no divide-by-zero, no
# huge step on a quiet frame). Small relative to typical speech-window energy.
_NLMS_EPS = 1.0

# Double-talk hold (the barge-in-preserving guard). When the near-end short-term
# energy exceeds the aligned reference short-term energy by more than this factor,
# the near-end contains the caller talking over the echo: FREEZE adaptation (keep
# subtracting the converged estimate, but do not let the uncorrelated caller speech
# pull the filter — which would both diverge it and partially cancel the caller).
# 2.0 (about +6 dB of unexplained near-end amplitude) is a Geigel-style ratio on RMS.
_DOUBLE_TALK_RATIO = 2.0

# A converged estimate must carry at least this mean-square energy before the
# double-talk ratio is trusted: when the agent is silent (no real echo estimate) the
# ratio is meaningless, so adaptation simply runs (there is no echo to mistake the
# caller for, and the filter stays near zero on uncorrelated input).
_MIN_ESTIMATE_MS_ENERGY = 1.0

# Adaptation sub-block: the echo estimate and one NLMS-family tap update are
# vectorised over this many near-end samples at a time (small matmuls), the taps
# updated once per sub-block. Small enough that the frozen-within-block filter
# tracks the per-sample recursion (at 1 they are identical, so convergence and
# double-talk behaviour match the old per-sample NLMS); large enough to amortise
# the numpy call overhead. Chosen from the ADR-0110 measured sweep: at 2 the
# broadband/tonal/double-talk survival + >= 30 dB ERLE bars all pass with margin at
# both rates and the per-frame cost stays far under the 20 ms budget (below) even at
# a 3x CI-host slowdown. Larger blocks converge slower (>= 30 dB ERLE at 8 kHz is
# missed by ~0.5 dB at 4 within a 2 s window); 1 recovers the per-sample recursion
# exactly but multiplies the numpy call count and erodes the CPU-budget margin.
_ADAPT_BLOCK: Final[int] = 2

# Measured push_reference+cancel cost for one *adapting* 512-tap (``_AEC_MAX_TAPS``)
# 20 ms frame on CPython 3.13 in the devcontainer (rule 22 / ADR-0094 pattern),
# gated against the 20 ms packet period by ``tests/test_media_aec_budget.py``.
# For contrast the superseded per-sample O(filter_len) NLMS measured ~35 ms at
# 8 kHz and ~91 ms at 16 kHz on the same host — 2x-4x over the packet budget. The
# block-NLMS figures are representative baselines (they vary with host/load); the
# gate asserts the coarse 20 ms packet period, not these exact values.
_AEC_CANCEL_MEASURED_US_PER_FRAME_8K: Final[float] = 1300.0
_AEC_CANCEL_MEASURED_US_PER_FRAME_16K: Final[float] = 4240.0
_AEC_CANCEL_BUDGET_US_PER_FRAME: Final[float] = 20_000.0  # the 20 ms RTP ptime


class EchoCanceller:
    """A numpy block-NLMS adaptive-filter echo canceller for one call direction.

    Owned by :class:`~hermes_voip.media.engine.RtpMediaTransport`: the TX path taps
    every outbound wire-rate frame via :meth:`push_reference`, and the RX path runs
    every decoded inbound frame through :meth:`cancel` before the VAD/ASR see it.
    Both operate at the engine's inbound **analysis rate**.

    The filter estimates the echo as ``y[t] = sum_k w[k] * x[t - bulk_delay - k]``
    over the most recent ``filter_len`` reference samples (skipping ``bulk_delay``
    leading samples to cover a constant echo-return delay), subtracts it from the
    near-end ``d[t]``, and adapts ``w`` toward the residual by block-NLMS — except
    during double-talk, when adaptation is frozen so a real interruption is not
    cancelled. The estimate + update are numpy matmuls over an
    :data:`_ADAPT_BLOCK`-sample window (see the module docstring).
    """

    def __init__(
        self, *, sample_rate: int, filter_len: int, bulk_delay: int, mu: float
    ) -> None:
        """Create a canceller.

        Args:
            sample_rate: The analysis rate (8000 or 16000 Hz) the canceller runs at
                — the rate of the inbound frames passed to :meth:`cancel` and of the
                reference after any downsample.
            filter_len: The adaptive FIR length in samples (taps). Spans the echo
                path's impulse response; longer models more reflections at more CPU
                per frame. Must be ``>= 1``.
            bulk_delay: A fixed number of leading reference samples to skip before
                the adaptive window, for a gateway with a large constant echo-return
                delay. ``0`` lets the adaptive taps cover the delay directly. ``>= 0``.
            mu: The NLMS step size in ``(0, 2)``. Higher converges faster with a
                higher steady-state residual.

        Raises:
            ValueError: If any parameter is out of range.
        """
        if sample_rate <= 0:
            msg = f"sample_rate must be positive, got {sample_rate}"
            raise ValueError(msg)
        if filter_len < 1:
            msg = f"filter_len must be >= 1, got {filter_len}"
            raise ValueError(msg)
        if bulk_delay < 0:
            msg = f"bulk_delay must be >= 0, got {bulk_delay}"
            raise ValueError(msg)
        if not _MU_MIN < mu < _MU_MAX:
            msg = f"mu must be in ({_MU_MIN}, {_MU_MAX}), got {mu}"
            raise ValueError(msg)
        self._rate = sample_rate
        self._filter_len = filter_len
        self._bulk_delay = bulk_delay
        self._mu = mu
        # Adaptive tap weights, newest in-window reference sample first (``w[0]``
        # multiplies the reference sample at the read cursor). C-double for speed.
        self._w: npt.NDArray[np.float64] = np.zeros(filter_len, dtype=np.float64)
        # Far-end (reference) FIFO, OLDEST first. The far-end and near-end are
        # SAMPLE-SYNCHRONOUS streams (both advance one sample per sample-period from
        # the call start), so the canceller cannot assume the engine interleaves a
        # push 1:1 with a cancel — on a real call ``push_reference`` (TX) runs ahead
        # of ``cancel`` (RX) by the system delay (the greeting plays before any
        # inbound, the jitter buffer holds the near-end, async scheduling batches).
        # ``cancel`` therefore consumes the far-end through an INDEPENDENT read
        # cursor that advances exactly one far-end sample per near-end sample (NOT
        # anchored to the newest push), so the two stay time-locked; the adaptive
        # taps span the echo round-trip delay forward from the cursor. ``_read`` is
        # the index (into ``_x``) of the far-end sample time-aligned with the NEXT
        # near-end sample; it starts at 0 (the first far-end sample lines up with the
        # first near-end sample — the echo's lead is then modelled by the taps).
        self._x: npt.NDArray[np.float64] = np.zeros(0, dtype=np.float64)
        self._read = 0
        # Retain enough trailing far-end past the read cursor for the filter window
        # (cursor back over bulk_delay + filter_len), plus generous headroom so a
        # burst of pushes ahead of the cursor is never trimmed away before its
        # matching near-end arrives (1 s).
        self._span = bulk_delay + filter_len
        self._keep_behind = self._span + sample_rate
        # Per-source-rate resampler for an off-analysis-rate reference (Opus 48 kHz
        # → 16 kHz). Created lazily on the first off-rate push; None while the
        # reference already arrives at the analysis rate.
        self._ref_resampler: Resampler | None = None

    def push_reference(self, pcm16: bytes, *, sample_rate: int) -> None:
        """Append one outbound frame to the far-end reference history.

        Called from the engine TX path the instant a frame goes on the wire. When
        ``sample_rate`` differs from the canceller's analysis rate (the Opus 48 kHz
        wire vs the 16 kHz analysis), the frame is downsampled to the analysis rate
        first (a state-carrying resampler, so the continuous reference is
        click-free) — so the canceller's far-end and near-end are always the same
        rate. An empty frame is a no-op.

        Args:
            pcm16: One outbound frame of PCM16-LE mono at ``sample_rate``.
            sample_rate: The frame's sample rate (the codec's wire rate).

        Raises:
            ValueError: If ``pcm16`` is not a whole number of 16-bit samples.
        """
        if len(pcm16) % PCM16_BYTES_PER_SAMPLE != 0:
            msg = (
                f"reference PCM16 must be whole 16-bit samples, got {len(pcm16)} bytes"
            )
            raise ValueError(msg)
        if not pcm16:
            return
        aligned = self._to_analysis_rate(pcm16, sample_rate)
        if not aligned:
            return
        samples = np.frombuffer(aligned, dtype=_PCM16_LE).astype(np.float64)
        if samples.size == 0:
            return
        # The FIFO is trimmed in cancel() relative to the read cursor (not here by
        # the newest sample), because push runs ahead of cancel and a far-end sample
        # must survive until its matching near-end has consumed it.
        self._x = np.concatenate((self._x, samples))

    def cancel(self, pcm16: bytes) -> bytes:
        """Return ``pcm16`` with the estimated echo of the reference removed.

        Block-NLMS over the aligned reference window: form the per-sample echo
        estimate, subtract it, then (outside double-talk) adapt the taps — the
        estimate and update run as numpy matmuls over :data:`_ADAPT_BLOCK`-sample
        sub-blocks. The far-end is consumed through the sample-synchronous read
        cursor (see the module docstring), so the window tracks the near-end
        timeline rather than the newest push. Returns exactly the input length —
        **no buffering, no added latency**. An empty frame returns empty.

        Args:
            pcm16: One inbound (near-end) frame of PCM16-LE mono at the analysis rate.

        Returns:
            The residual PCM16-LE mono frame, same length as the input.

        Raises:
            ValueError: If ``pcm16`` is not a whole number of 16-bit samples.
        """
        if len(pcm16) % PCM16_BYTES_PER_SAMPLE != 0:
            msg = f"near-end PCM16 must be whole 16-bit samples, got {len(pcm16)} bytes"
            raise ValueError(msg)
        if not pcm16:
            return b""
        near = np.frombuffer(pcm16, dtype=_PCM16_LE).astype(np.float64)
        n = near.size

        flen = self._filter_len
        bulk = self._bulk_delay
        mu = self._mu
        read = self._read
        x = self._x
        xlen = x.size
        eps = _NLMS_EPS

        # The residual defaults to the near-end (pass-through): any sample whose
        # aligned reference has not been pushed yet (the agent has not spoken far
        # enough — ``head >= xlen``) carries no echo and is returned unchanged.
        out = near.copy()

        # Far-end ECHO-SOURCE index for near[i] is ``read + i`` (the read cursor
        # advances one far-end sample per near-end sample — see __init__), shifted
        # forward by ``bulk``. Samples ``i`` with ``read + bulk + i < xlen`` have an
        # aligned reference window; the rest pass through.
        valid = max(0, min(n, xlen - read - bulk))
        if valid > 0:
            # Per-sample delayed-reference matrix, newest-first (so ``w[0]`` pairs
            # with ``x[head]``). Front-zero-padding by ``flen - 1`` makes a partial
            # window at the call start naturally zero-filled — identical to using
            # only the available taps, since a zero reference contributes nothing to
            # the estimate or the gradient. ``sliding_window_view`` is a no-copy view.
            xpad = np.concatenate((np.zeros(flen - 1, dtype=np.float64), x))
            windows = np.lib.stride_tricks.sliding_window_view(xpad, flen)
            t0 = read + bulk
            # windows[t] == xpad[t : t + flen] == x[t - flen + 1 .. t] (oldest→newest,
            # front-zero where the original index is < 0); reverse to newest-first.
            ref = windows[t0 : t0 + valid, ::-1]

            # Double-talk decision for the frame (the barge-in-preserving guard),
            # made ONCE on the energies over the aligned far-end window — see
            # :meth:`_frame_adapts`. Anchored at the LAST sample's head so the guard
            # reads the SAME reference the estimate uses (not the newest pushed
            # sample, which runs ahead of the echo and would mis-fire the guard).
            head_last = min(read + bulk + (n - 1), xlen - 1)
            adapt = self._frame_adapts(near, head_last=head_last)

            w = self._w
            block = _ADAPT_BLOCK
            for a in range(0, valid, block):
                b = min(a + block, valid)
                ref_b = ref[a:b]  # (m, flen) newest-first
                # Estimate y = ref_b @ w with the CURRENT taps, subtract, output.
                err = near[a:b] - ref_b @ w
                out[a:b] = err
                # Block-NLMS update: w += (mu / (||X||^2 + eps)) * X^T e, frozen
                # during double-talk so the uncorrelated caller cannot pull the
                # filter. ||X||^2 is the TOTAL sub-block reference energy, which keeps
                # the step stable for mu in (0, 2) on correlated + broadband refs.
                if adapt:
                    energy = float(np.multiply(ref_b, ref_b).sum()) + eps
                    w = w + (mu / energy) * (ref_b.T @ err)
            self._w = w

        # Round to nearest (floor(x + 0.5) rounds half up symmetrically for the small
        # sub-LSB residual, and leaves an already-integer pass-through sample exact),
        # clamp to int16, pack little-endian.
        clamped = np.clip(np.floor(out + 0.5), _INT16_MIN, _INT16_MAX)
        result: bytes = clamped.astype(_PCM16_LE).tobytes()

        # Advance the read cursor by this frame's near-end sample count (1:1 with the
        # far-end) — but NEVER past the available far-end (``len(x)``). On a real call
        # the inbound pump and the greeting run concurrently, so near-end (RTP /
        # comfort noise) can arrive BEFORE the first outbound TTS; those samples have
        # no echo (the agent has not spoken) and pass through, and the cursor must not
        # run off the end of the (still-empty/short) FIFO — otherwise when the
        # greeting's echo finally arrives the window would point PAST the FIFO and
        # never cancel (cross-vendor review: a 200 ms inbound pre-roll left the echo
        # uncancelled). Pinning the cursor at the FIFO end while the reference lags
        # keeps the window on the newest available reference; the NLMS taps (spanning
        # ``flen`` back) then find the true echo delay once both streams are live.
        self._read = min(read + n, xlen)
        # Trim FIFO samples no future window can reach (older than the next frame's
        # oldest window start), shifting the cursor by the trim count so the alignment
        # is preserved. Bounds the FIFO across a long call. ``.copy()`` releases the
        # trimmed prefix instead of retaining the base buffer through a view.
        trim = self._read - self._keep_behind
        if trim > 0:
            self._x = x[trim:].copy()
            self._read -= trim

        return result

    def _frame_adapts(self, near: npt.NDArray[np.float64], *, head_last: int) -> bool:
        """Whether to adapt over this frame (False during double-talk).

        Compares the near-end frame energy to the aligned far-end (reference) frame
        energy. The echo is an attenuated copy of the reference (gateway gain ≤ ~1),
        so when the near-end carries substantially MORE energy than the reference
        could produce as echo, the excess is the caller talking over the echo
        (double-talk): freeze adaptation so the caller does not pull/diverge the
        filter. Using the reference — not the current echo ESTIMATE — avoids the
        chicken-and-egg freeze where an unconverged (near-zero) estimate makes every
        echo sample look like double-talk and the filter never learns.

        With no aligned reference yet (the agent has not spoken — the history does
        not reach this frame), there is no echo to mistake the caller for, so adapt
        freely; the filter stays near zero on the uncorrelated caller-only input.
        """
        n = near.size
        if n == 0 or head_last < 0:
            return True
        near_ms = float(np.dot(near, near) / n)
        first_head = head_last - (n - 1)
        lo = first_head if first_head > 0 else 0
        seg = self._x[lo : head_last + 1]
        if seg.size == 0:
            return True
        ref_ms = float(np.dot(seg, seg) / seg.size)
        if ref_ms < _MIN_ESTIMATE_MS_ENERGY:
            # Reference is silent in this frame: any near-end energy is NOT echo of
            # it, so adapting toward it would only fit noise — but with a silent
            # reference the NLMS step is ~0 anyway (numerator has no correlated x),
            # so adapting is harmless and keeps the converged taps. Freeze only when
            # the near-end is also loud (real caller during an outbound gap).
            return near_ms < _MIN_ESTIMATE_MS_ENERGY
        return near_ms <= (_DOUBLE_TALK_RATIO * _DOUBLE_TALK_RATIO) * ref_ms

    def _to_analysis_rate(self, pcm16: bytes, sample_rate: int) -> bytes:
        """Downsample a reference frame to the analysis rate (no-op when equal)."""
        if sample_rate == self._rate:
            return pcm16
        if self._ref_resampler is None:
            self._ref_resampler = Resampler(sample_rate, self._rate)
        return self._ref_resampler.resample(pcm16)

    def reset(self) -> None:
        """Drop all per-call state: taps, reference history, resampler.

        Called at call start (engine ``connect``) so a reused engine begins with a
        zeroed filter and empty history — no stale echo path from a prior call.
        """
        self._w = np.zeros(self._filter_len, dtype=np.float64)
        self._x = np.zeros(0, dtype=np.float64)
        self._read = 0
        self._ref_resampler = None
