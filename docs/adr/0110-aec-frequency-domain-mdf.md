# ADR-0110: AEC durable fix — multi-delay block frequency-domain adaptive filter (MDF)

- **Date:** 2026-07-11
- **Status:** Accepted
- **Deciders:** operator (directed the best-practice implementation regardless of refactor
  scope, and that disabling is not acceptable — this is the rule-40 approval ADR-0095
  required); agent session selected the algorithm and implements + validates it
- **Supersedes:** ADR-0095 (`Proposed — Deferred` AEC real-time CPU budget) — its deferred
  option menu is now resolved: the operator picked the **durable fix**, ruling out the
  interim disable (option b) and the reach-trading filter trim (option a)
- **Relates to / re-litigates:** ADR-0033 (in-process time-domain NLMS AEC) — this ADR
  *deliberately reverses* two of ADR-0033's recorded constraints, with operator approval:
  its **no-numpy-in-media** constraint and its **no-added-algorithmic-latency** constraint.
  ADR-0094 (G.722 hot-path budget — the sibling `< 20 ms/frame` gate this AEC must now meet)

## Context

ADR-0095 measured the shipped time-domain NLMS canceller (`aec.py`) at **~39.8 ms/frame
while adapting at 16 kHz** (2× the 20 ms ptime) and **~21.2 ms at 8 kHz** (~6% over), on the
synchronous RX media coroutine (`engine.py` `_inbound_gen` → `_decode` → `_cancel_echo`,
inline, no offload). AEC is **on by default** (`_DEFAULT_AEC_ENABLED = True`). The stall is
real whenever the agent's TTS echo is returning — i.e. most of a call. ADR-0095 deferred the
fix as an explicitly operator-gated architecture decision.

The operator has now directed the **best-practice implementation**, accepting a complete
refactor and ruling out disabling. That resolves the deferral and authorizes the two
constraint reversals below.

**Why the time-domain filter cannot meet budget by tuning.** Its cost is `O(filter_len)`
multiply-accumulates per sample, twice per adapting sample (estimate pass + tap-update
pass). At 512 taps × the analysis rate this is intrinsically ~2× budget; shortening the
filter (ADR-0095 option a) trades echo-cancellation *reach* for CPU (ADR-0033's original
16 ms→64 ms regression), and thread-offload (option c) hides the stall from the event loop
without reducing the total CPU the host must find in wall-clock time.

## Decision

Replace the time-domain NLMS with a **multi-delay block frequency-domain adaptive filter
(MDF / partitioned-block FDAF)** — the production standard for real-time AEC (SpeexDSP MDF,
WebRTC AEC3's linear stage). The echo path of length `L` taps is partitioned into `P`
blocks of `B` samples each (`L = P·B`); each block's convolution and NLMS-style adaptation
run in the frequency domain via FFT, using overlap-save. Per-frame cost drops from the
time-domain `O(L)`-per-sample to `O(P · B log B)` per block — the FFTs are `numpy.fft` C
routines, so the constant collapses well under the 20 ms/frame budget.

**Constraint reversals (operator-approved):**

1. **numpy enters the `media` extra.** MDF needs a real FFT; a pure-stdlib FFT would forfeit
   the speed that is the entire point. `numpy` (already pinned at `2.4.6` in the `ml` extra)
   is added to the `media` extra. Licence/advisory gating (rule 35) applies to the dep
   change; numpy's BSD-3 licence is already vetted for the `ml` extra.
2. **A bounded, minimized algorithmic latency is accepted.** A block filter processes `B`
   samples at a time, so `cancel()` incurs up to one block (`B` samples) of delay — NOT the
   whole filter length. `B` is chosen small (e.g. 128 samples = 8 ms at 16 kHz / 16 ms at
   8 kHz) so the added latency is a fraction of the echo round-trip the filter already
   spans, and well inside the conversational budget. ADR-0033's "no added delay" is replaced
   by "one small block of delay, `B` samples, explicitly budgeted."

**Preserved from ADR-0033 (non-negotiable — the correctness contract):**

- **Reference-based, sample-synchronous alignment.** The far-end reference (`push_reference`)
  and near-end (`cancel`) stay time-locked via the same independent read-cursor + `bulk_delay`
  discipline; the MDF taps span the echo round-trip forward from the cursor. The caller's
  speech is uncorrelated with the reference and survives (barge-in still fires).
- **The `EchoCanceller` public API is unchanged** (`__init__(sample_rate, filter_len,
  bulk_delay, mu)`, `push_reference`, `cancel(pcm16) -> bytes`) so the engine
  (`_cancel_echo`, `engine.py:1962`), config, and the barge-in integration are untouched;
  `filter_len` maps to `P·B` (rounded up to a whole block).
- **Double-talk hold.** Adaptation still freezes on detected double-talk (the caller talking
  over the agent), now a per-block frequency-domain step-size gate rather than a per-sample
  update skip.

## Validation bar (rule 22/24/25 — this ADR does not claim a fix it has not proven)

The implementation is not "done" until, with evidence:

1. **Convergence:** a deterministic test drives a known synthetic echo path (delay +
   attenuation + colouring) and asserts the residual echo energy converges to ≥ ~30 dB ERLE
   (echo-return-loss-enhancement) — i.e. the filter *actually cancels the echo*.
2. **Near-end survival:** an uncorrelated near-end (caller) signal passes through with its
   energy essentially intact (no cancellation of genuine barge-in).
3. **CPU budget:** a `< 20 ms/frame` per-frame benchmark at both 8 kHz and 16 kHz **while
   adapting** — the ADR-0094-pattern gate ADR-0095 said could not be added against the old
   default. This gate ships with the change.
4. **No behavioural regression:** the existing `test_media_aec.py` /
   `test_media_engine_aec.py` / `test_aec_barge_in_integration.py` contracts stay green
   (or, where a test pinned a now-changed internal like exact per-sample output, the test is
   re-expressed against the observable contract — ERLE + near-end survival — in its own
   justified commit, never weakened).

## Consequences

- The synchronous RX stall is eliminated: AEC stays **on by default** at both rates and fits
  the packet budget, so the ADR-0023 barge-in gate can stay tight (no sluggish fallback).
- `media` extra gains a numpy dependency (larger install; supply-chain surface widened but
  from an already-vetted, pinned package).
- One small block (`B` samples) of added RX latency, explicitly budgeted and tested.
- The AEC now has a permanent `< 20 ms/frame` CI budget gate (ADR-0094 parity), so a future
  regression that reinflates the constant fails CI instead of silently stalling calls.

## Alternatives considered

Per ADR-0095's menu, now resolved by the operator:

- **(a) shorten the filter** — rejected: trades echo reach for CPU (ADR-0033's regression).
- **(b) disable AEC at 16 kHz** — rejected by the operator ("disabling isn't an option").
- **(c) thread-offload** — rejected as *the* fix: moves CPU off the loop but does not reduce
  it; the host still needs the headroom, and it adds NLMS tap-state thread-safety and
  scheduler-jitter costs. (May still be layered later if a host is CPU-starved even after MDF.)
- **(e) numpy-vectorised *time-domain* NLMS** — rejected: still `O(L)` per sample (only a
  constant-factor win, and per-sample numpy call overhead erodes it), and the strict
  per-sample NLMS recursion does not block-vectorise without becoming block-LMS anyway. MDF
  is the algorithm that changes the complexity class, which is what the budget requires.

## References

- ADR-0095 (`0095-aec-realtime-cpu-budget.md`) — the deferred measurement + option menu
- ADR-0033 (`0033-in-process-aec-aggressive-barge-in.md`) — the time-domain NLMS + the
  no-numpy / no-latency constraints this ADR reverses, and the reference-alignment contract
  it preserves
- ADR-0094 (`0094-g722-hot-path-cpu-budget.md`) — the `< 20 ms/frame` gate pattern
- `src/hermes_voip/media/aec.py` — `EchoCanceller` (the API preserved, the internals replaced)
- `src/hermes_voip/media/engine.py:1962` — `_cancel_echo` call site (unchanged)
- SpeexDSP `mdf.c` / WebRTC AEC3 — the reference algorithm this mirrors
