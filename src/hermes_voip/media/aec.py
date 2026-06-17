"""In-process acoustic echo canceller — NLMS adaptive filter (ADR-0033).

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

**No new dependency, no numpy.** The canceller lives in the ``media``-extra engine
path (``RtpMediaTransport``), which must not pull the ``ml`` extra's numpy. The
filter math is :class:`array.array` (contiguous C doubles) over the Python stdlib
only. The per-sample cost is ``O(filter_len)`` multiply-accumulates; the filter is
short (a few tens of ms) and there is **no algorithmic delay** — :meth:`cancel`
returns exactly the samples it is given, with no look-ahead buffering (the
latency-sensitivity requirement, rule 22).

Rates: the canceller runs at the inbound **analysis rate** (8 kHz G.711, 16 kHz
G.722/Opus). :meth:`push_reference` downsamples an off-analysis-rate reference
(Opus's 48 kHz wire) to the analysis rate so the two inputs align.

Time alignment: the engine interleaves one :meth:`push_reference` (an outbound
ptime) with one :meth:`cancel` (an inbound ptime), so within a ``cancel`` frame of
``n`` samples the LAST near-end sample is aligned with the NEWEST reference sample
already pushed, and earlier near-end samples with proportionally older reference.
``bulk_delay`` shifts the whole alignment back by a fixed echo-return delay; the
adaptive taps absorb the residual fine delay + the room/hybrid impulse response.
"""

from __future__ import annotations

import struct
from array import array

from hermes_voip.media.audio import Resampler
from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE

__all__ = ["EchoCanceller"]

_INT16_MIN = -32768
_INT16_MAX = 32767

# NLMS step-size (mu) bounds: the open interval (0, 2). 0 never adapts; >= 2 diverges.
_MU_MIN = 0.0
_MU_MAX = 2.0

# NLMS regulariser added to the reference-window energy in the denominator, so the
# update step stays bounded when the reference is near-silent (no divide-by-zero, no
# huge step on a quiet frame). Small relative to typical speech-window energy.
_NLMS_EPS = 1.0

# Double-talk hold (the barge-in-preserving guard). When the near-end short-term
# energy exceeds the echo-estimate short-term energy by more than this factor, the
# near-end contains the caller talking over the echo: FREEZE adaptation (keep
# subtracting the converged estimate, but do not let the uncorrelated caller speech
# pull the filter — which would both diverge it and partially cancel the caller).
# 2.0 (about +6 dB of unexplained near-end amplitude) is a Geigel-style ratio on RMS.
_DOUBLE_TALK_RATIO = 2.0

# A converged estimate must carry at least this mean-square energy before the
# double-talk ratio is trusted: when the agent is silent (no real echo estimate) the
# ratio is meaningless, so adaptation simply runs (there is no echo to mistake the
# caller for, and the filter stays near zero on uncorrelated input).
_MIN_ESTIMATE_MS_ENERGY = 1.0


class EchoCanceller:
    """An NLMS adaptive-filter acoustic echo canceller for one call direction.

    Owned by :class:`~hermes_voip.media.engine.RtpMediaTransport`: the TX path taps
    every outbound wire-rate frame via :meth:`push_reference`, and the RX path runs
    every decoded inbound frame through :meth:`cancel` before the VAD/ASR see it.
    Both operate at the engine's inbound **analysis rate**.

    The filter estimates the echo as ``y[t] = sum_k w[k] * x[t - bulk_delay - k]``
    over the most recent ``filter_len`` reference samples (skipping ``bulk_delay``
    leading samples to cover a constant echo-return delay), subtracts it from the
    near-end ``d[t]``, and adapts ``w`` toward the residual by NLMS — except during
    double-talk, when adaptation is frozen so a real interruption is not cancelled.
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
                per sample. Must be ``>= 1``.
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
        # Adaptive tap weights, newest reference sample first (``w[0]`` multiplies
        # the most recent in-window reference sample). C-double storage for speed.
        self._w: array[float] = array("d", [0.0] * filter_len)
        # Far-end (reference) history, OLDEST first; the newest sample is the last
        # element. A ``cancel`` frame reads, for its oldest near-end sample, back to
        # ``bulk_delay + filter_len - 1`` samples before that sample's aligned
        # reference; the alignment maps the newest reference sample to the LAST
        # near-end sample of the frame. So the history must retain
        # ``bulk_delay + filter_len`` samples for steady state; we keep generous
        # headroom (a few ptimes) so a frame longer than one ptime still aligns.
        self._span = bulk_delay + filter_len
        self._max_history = self._span + sample_rate  # span + up to 1 s headroom
        self._x: array[float] = array("d")
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
        n = len(aligned) // PCM16_BYTES_PER_SAMPLE
        if n == 0:
            return
        self._x.extend(float(s) for s in struct.unpack(f"<{n}h", aligned))
        # Bound the history: keep only the most recent samples needed (+ headroom).
        excess = len(self._x) - self._max_history
        if excess > 0:
            del self._x[:excess]

    def cancel(self, pcm16: bytes) -> bytes:
        """Return ``pcm16`` with the estimated echo of the reference removed.

        Sample-by-sample NLMS over the aligned reference window: for each near-end
        sample form the echo estimate, subtract it, then (outside double-talk) adapt
        the taps. The newest reference sample aligns with the LAST near-end sample of
        the frame; earlier near-end samples step back through the history one sample
        each. Returns exactly the input length — **no buffering, no added latency**.
        An empty frame returns empty.

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
        n = len(pcm16) // PCM16_BYTES_PER_SAMPLE
        near = struct.unpack(f"<{n}h", pcm16)

        w = self._w
        x = self._x
        flen = self._filter_len
        bulk = self._bulk_delay
        mu = self._mu
        xlen = len(x)

        # Double-talk decision for the frame (the barge-in-preserving guard), made
        # ONCE on energies — see :meth:`_frame_adapts`. It compares the near-end
        # energy to the aligned REFERENCE energy (not to the current estimate, which
        # would chicken-and-egg freeze the filter before it ever converges): echo
        # never exceeds the reference (the gateway attenuates), so near-end energy
        # well above the reference is the caller talking over the echo → freeze.
        adapt = self._frame_adapts(near, head_last=xlen - 1 - bulk)

        out: list[int] = [0] * n
        int16_min = _INT16_MIN
        int16_max = _INT16_MAX
        eps = _NLMS_EPS
        for i in range(n):
            # Align near[i] with reference: the LAST near-end sample (i == n-1) maps
            # to the newest reference sample (index xlen-1), shifted back by
            # bulk_delay; earlier samples step back by (n-1-i). The window's newest
            # in-window reference sample is ``head``; w[k] multiplies x[head-k].
            head = xlen - 1 - bulk - (n - 1 - i)
            if head < 0:
                # No aligned reference yet (the agent has not spoken far enough back):
                # nothing to subtract, pass the near-end sample through unchanged.
                s = near[i]
                out[i] = (
                    int16_min if s < int16_min else int16_max if s > int16_max else s
                )
                continue
            # Slice the aligned window ONCE (oldest→newest): win[j] == x[start + j],
            # so win[-1] is the newest in-window sample. ``w`` is newest-first, so we
            # iterate it reversed to align w[0]↔win[-1]. Slicing + zip run the inner
            # multiply-accumulate at C speed instead of per-element bounds-checked
            # indexing — the hot-path cost (rule 22). ``flen`` taps unless the history
            # is shorter than the window (call start), then only what is available.
            start = max(head - flen + 1, 0)
            win = x[start : head + 1]  # oldest→newest, len == head - start + 1
            m = len(win)
            # Estimate y = sum_k w[k]*x[head-k] and the window energy ||x||^2. The
            # first ``m`` taps (newest-first) pair with the reversed window.
            est = 0.0
            energy = 0.0
            for wk, xv in zip(w[:m], reversed(win), strict=True):
                est += wk * xv
                energy += xv * xv
            s = near[i]
            e = s - est
            r = int(e)
            out[i] = int16_min if r < int16_min else int16_max if r > int16_max else r
            # NLMS update: w[k] += (mu*e/(||x||^2+eps)) * x[head-k], frozen during
            # double-talk so the uncorrelated caller cannot pull/diverge the filter.
            if adapt:
                step = mu * e / (energy + eps)
                wr = reversed(range(m))  # tap indices newest-first
                for k, xv in zip(wr, win, strict=True):
                    w[k] += step * xv

        return struct.pack(f"<{n}h", *out)

    def _frame_adapts(self, near: tuple[int, ...], *, head_last: int) -> bool:
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
        n = len(near)
        if n == 0 or head_last < 0:
            return True
        near_ms = sum(s * s for s in near) / n
        x = self._x
        first_head = head_last - (n - 1)
        ref_count = 0
        ref_ms_sum = 0.0
        for h in range(max(0, first_head), head_last + 1):
            xv = x[h]
            ref_ms_sum += xv * xv
            ref_count += 1
        if ref_count == 0:
            return True
        ref_ms = ref_ms_sum / ref_count
        if ref_ms < _MIN_ESTIMATE_MS_ENERGY:
            # Reference is silent in this frame: any near-end energy is NOT echo of
            # it, so adapting toward it would only fit noise — but with a silent
            # reference the NLMS step is ~0 anyway (numerator has no correlated x),
            # so adapting is harmless and keeps the converged taps. Freeze only when
            # the near-end is also loud (real caller during an outbound gap).
            return near_ms < _MIN_ESTIMATE_MS_ENERGY
        return near_ms <= (_DOUBLE_TALK_RATIO * _DOUBLE_TALK_RATIO) * ref_ms

    @staticmethod
    def _clamp(value: float) -> int:
        """Clamp a float sample to the int16 range and round to int."""
        if value <= _INT16_MIN:
            return _INT16_MIN
        if value >= _INT16_MAX:
            return _INT16_MAX
        return int(value)

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
        self._w = array("d", [0.0] * self._filter_len)
        self._x = array("d")
        self._ref_resampler = None
