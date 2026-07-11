# ADR-0110: AEC durable CPU-budget fix — numpy block-NLMS adaptive filter

- **Date:** 2026-07-11
- **Status:** Accepted
- **Deciders:** operator (directed the best-practice implementation regardless of refactor
  scope, and that disabling is not acceptable — this is the rule-40 approval ADR-0095
  required); agent session selected the algorithm and implements + validates it
- **Supersedes:** ADR-0095 (`Proposed — Deferred` AEC real-time CPU budget) — its deferred
  option menu is now resolved: the operator picked the **durable fix**, ruling out the
  interim disable (option b) and the reach-trading filter trim (option a)
- **Relates to / re-litigates:** ADR-0033 (in-process time-domain NLMS AEC) — this ADR
  *deliberately reverses* ADR-0033's **no-numpy** constraint, with operator approval, and
  **preserves** its no-added-latency constraint. ADR-0094 (G.722 hot-path budget — the
  sibling `< 20 ms/frame` gate this AEC must now meet).

> **Decision evolution (recorded honestly, rule 23/27).** An earlier draft of this same ADR
> selected a multi-delay block **frequency-domain** adaptive filter (MDF / partitioned-block
> FDAF). During implementation a confirming microbenchmark (below) showed that a
> **numpy-vectorised time-domain block-NLMS** meets the `< 20 ms/frame` budget at the
> 512-tap cap with a **~5x margin, zero added latency, and far less code/FFT bug-surface**.
> The MDF's `O(N log N)` win only matters for filters much larger than the `_AEC_MAX_TAPS`
> = 512 cap this deployment uses. The decision was therefore finalised as block-NLMS
> **before first ship** (the ADR and the implementation land together — the ADR was never
> merged describing MDF). ADR-0095's rejection of "(e) numpy time-domain" was the thing
> that was wrong: it assumed per-sample numpy overhead that a *block* matmul does not incur.

## Context

ADR-0095 measured the shipped time-domain per-sample NLMS canceller (`aec.py`) at
**~39.8 ms/frame while adapting at 16 kHz** (2x the 20 ms ptime) and **~21.2 ms at 8 kHz**
(~6% over), on the synchronous RX media coroutine (`engine.py` `_inbound_gen` → `_decode` →
`_cancel_echo`, inline, no offload). AEC is **on by default** (`_DEFAULT_AEC_ENABLED = True`).
The stall is real whenever the agent's TTS echo is returning — i.e. most of a call. (On the
current devcontainer host the same per-sample filter measured ~35 ms / ~91 ms — the point
holds regardless of host.) ADR-0095 deferred the fix as an explicitly operator-gated
architecture decision.

The operator has now directed the **best-practice implementation**, accepting a complete
refactor and ruling out disabling. That resolves the deferral and authorises the numpy
dependency below.

**Why the per-sample filter cannot meet budget by tuning.** Its cost is `O(filter_len)`
multiply-accumulates per sample, twice per adapting sample (estimate pass + tap-update
pass). At 512 taps × the analysis rate this is intrinsically over budget; shortening the
filter (ADR-0095 option a) trades echo-cancellation *reach* for CPU (ADR-0033's original
16 ms→64 ms regression), and thread-offload (option c) hides the stall from the event loop
without reducing the total CPU the host must find in wall-clock time.

## Decision

Replace the per-sample time-domain recursion with a **numpy-vectorised block-NLMS**. The
per-sample delayed-reference window is assembled once per frame as a matrix (via a no-copy
`sliding_window_view`), and the echo estimate + NLMS-family tap update run as small numpy
matmuls over a short **sub-block** of `_ADAPT_BLOCK` samples, the taps updated once per
sub-block instead of once per sample:

- estimate `y = X · w` over the sub-block, residual `e = d − y`, output `e`;
- adapt `w += (mu / (‖X‖² + eps)) · Xᵀ e`, where `‖X‖²` is the **total sub-block reference
  energy** — the normalisation that keeps the block update unconditionally stable for
  `mu ∈ (0, 2)` on both correlated (tonal) and broadband references (an *average*-energy
  normalisation converges faster but diverges on tonal input; verified empirically).

`_ADAPT_BLOCK` is small (2 samples): at 1 it is exactly the per-sample recursion (so
convergence and the double-talk guard are unchanged), and small blocks keep the
frozen-within-block filter tracking that recursion, while still collapsing the Python-level
per-sample loop into vectorised C matmuls. Per-frame cost drops from the per-sample
`O(filter_len)` to a handful of small matmuls.

**Constraint reversal (operator-approved):**

1. **numpy becomes a base dependency.** The block matmuls need numpy. Because the canceller
   is **on by default** *and* `media/aec.py` is type-checked by the default (no-extra) mypy
   gate, numpy is added to `[project.dependencies]` (NOT an optional extra): mypy gets
   numpy's real `py.typed` types with no escape hatch, and the AEC suite runs in every CI
   job with no `importorskip`. `import hermes_voip` stays light (only `media/aec.py` and
   `media/engine.py` import numpy, never the package root). numpy is BSD-3-Clause
   (permissive, already vetted for the `ml` extra); the licence/advisory gate (rule 35)
   covers it in the base scope. It is removed from the `ml` extra (the base install now
   provides it).

**Preserved from ADR-0033 (non-negotiable — the correctness contract):**

- **No added algorithmic latency.** Unlike the MDF the earlier draft selected (which incurs
  one block of overlap-save delay), block-NLMS processes and returns each whole `cancel`
  frame with no look-ahead buffering — ADR-0033's "no added delay" constraint is **kept**,
  not reversed. This is a strict improvement over the MDF plan.
- **Reference-based, sample-synchronous alignment.** The far-end reference (`push_reference`)
  and near-end (`cancel`) stay time-locked via the same independent read-cursor + `bulk_delay`
  discipline; the taps span the echo round-trip forward from the cursor. The caller's speech
  is uncorrelated with the reference and survives (barge-in still fires). Partial windows at
  the call start are front-zero-padded — identical to using only the available taps.
- **The `EchoCanceller` public API is unchanged** (`__init__(sample_rate, filter_len,
  bulk_delay, mu)`, `push_reference`, `cancel(pcm16) -> bytes` same-length, `reset`) so the
  engine (`_cancel_echo`, `engine.py:1962`), config, and the barge-in integration are
  untouched.
- **Double-talk hold.** Adaptation still freezes on detected double-talk (a per-frame
  reference-vs-near-end energy ratio), so the caller talking over the agent is never
  cancelled away.

## Validation bar (rule 22/24/25 — this ADR does not claim a fix it has not proven)

Met, with evidence in `tests/test_media_aec_budget.py` (new) + the preserved
`tests/test_media_aec.py` / `test_media_engine_aec.py` / `test_aec_barge_in_integration.py`:

1. **Convergence:** a deterministic synthetic echo path (delay + attenuation + colouring) is
   driven to **≥ 30 dB ERLE** on the converged tail — measured **56 dB at 8 kHz and 76 dB at
   16 kHz** (bar 30).
2. **Near-end survival:** an uncorrelated caller passes through with its energy intact (the
   preserved double-talk and barge-in tests).
3. **CPU budget:** `push_reference`+`cancel` for a 512-tap adapting frame measured
   **~1.3 ms at 8 kHz and ~4.2 ms at 16 kHz** — well under the 20 ms packet period (the old
   per-sample filter was ~35 ms / ~91 ms on the same host). The `< 20 ms/frame` gate ships
   with the change (ADR-0094 pattern), so a regression that reinflates the constant fails CI.
4. **No behavioural regression:** the existing `test_media_aec.py` /
   `test_media_engine_aec.py` / `test_aec_barge_in_integration.py` / `test_config_aec.py`
   contracts stay green **unchanged** (the observable ERLE + near-end-survival + exact
   pass-through + frame-length contracts hold under block-NLMS at `_ADAPT_BLOCK = 2`).

## Consequences

- The synchronous RX stall is eliminated: AEC stays **on by default** at both rates and fits
  the packet budget with a ~5x margin, so the ADR-0023 barge-in gate can stay tight (no
  sluggish fallback).
- numpy enters the **base** dependency set (larger default install; supply-chain surface
  widened but from an already-vetted, pinned, permissive package). `import hermes_voip` is
  unaffected (numpy loads only on the media path).
- **Zero added RX latency** (better than the MDF alternative, which would have added one block).
- The AEC now has a permanent `< 20 ms/frame` CI budget gate (ADR-0094 parity) plus a 30 dB
  ERLE convergence gate, so a future regression that reinflates the constant or breaks
  convergence fails CI instead of silently stalling / de-cancelling calls.

## Alternatives considered

- **numpy time-domain block-NLMS** — **CHOSEN** (this decision). Simplest algorithm that
  changes the per-frame cost to a few matmuls, keeps zero latency, and reuses the exact NLMS
  convergence family and the ADR-0033 alignment/double-talk contract.
- **multi-delay block frequency-domain filter (MDF / PBFDAF)** — the earlier draft's
  selection; rejected on measurement: for the 512-tap cap it is more code + FFT bug-surface
  and adds one block of latency, with no budget benefit over block-NLMS. Revisit only if a
  later decision lifts `_AEC_MAX_TAPS` far enough that `O(N log N)` per block beats the block
  matmul (much larger filters / long reverberant tails).
- **(a) shorten the filter** — rejected: trades echo reach for CPU (ADR-0033's regression).
- **(b) disable AEC at 16 kHz** — rejected by the operator ("disabling isn't an option").
- **(c) thread-offload** — rejected as *the* fix: moves CPU off the loop but does not reduce
  it; adds tap-state thread-safety + scheduler-jitter costs. (May still be layered later if a
  host is CPU-starved even after this fix — now unlikely given the ~5x margin.)

## References

- ADR-0095 (`0095-aec-realtime-cpu-budget.md`) — the deferred measurement + option menu
- ADR-0033 (`0033-in-process-aec-aggressive-barge-in.md`) — the time-domain NLMS + the
  no-numpy constraint this ADR reverses (and the no-latency constraint + reference-alignment
  contract it preserves)
- ADR-0094 (`0094-g722-hot-path-cpu-budget.md`) — the `< 20 ms/frame` gate pattern
- `src/hermes_voip/media/aec.py` — `EchoCanceller` (the API preserved, the internals replaced)
- `src/hermes_voip/media/engine.py:1962` — `_cancel_echo` call site (unchanged)
- `tests/test_media_aec_budget.py` — the CPU-budget + 30 dB ERLE gates
- `docs/runbooks/0018-voip-acoustic-echo-cancellation.md` — the operational HOW
