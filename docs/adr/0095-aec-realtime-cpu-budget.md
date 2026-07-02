# ADR-0095: AEC hot-path CPU budget: the 16 kHz default is not real-time-safe

- **Date:** 2026-07-02
- **Status:** Proposed — Deferred (a genuine open architecture decision; adoption requires
  operator approval per AGENTS.md rule 40 — see ADR-0060 for the established precedent of
  recording a deferred decision on the record before an operator choice is made)
- **Deciders:** operator (the choice among the options below is not agent-defaultable);
  agent session (Wave-2 docs-reconcile lane) recorded the measurement and the option set
- **Relates to:** ADR-0033 (in-process AEC design, the original ~13.8 ms/16 kHz figure this
  ADR corrects the scope of), ADR-0094 (G.722 hot-path CPU budget — the sibling codec-cost
  ADR this mirrors in method, but not in outcome: G.722 measured under budget and shipped a
  gate; AEC measures over budget and cannot)

## Context

Backlog item bk1198 (`docs/backlog.md`, re-scoped in commit `87e3c5e`, Wave-11 gap-review)
called for "add and enforce an AEC hot-path CPU budget" — the same treatment ADR-0094 gave
G.722. Measuring first (rather than adding a budget constant blind) found the premise false:
**the AEC does not fit the packet budget at its shipped default, so a `< 20 ms` gate cannot
be added without a design change first.**

**The shipped default.** `_DEFAULT_AEC_FILTER_MS = 64` (`src/hermes_voip/config.py:278`) and
the engine's own standalone default `_AEC_DEFAULT_FILTER_MS = 64`
(`src/hermes_voip/media/engine.py:263`) both feed a hard tap ceiling
`_AEC_MAX_TAPS: Final[int] = 512` (`src/hermes_voip/media/engine.py:273`) — a 64 ms window at
16 kHz clamps to the 512-tap cap (≈ 32 ms of window), at 8 kHz it is the full 64 ms/512 taps.

**Measured cost (Wave-11 gap-review session, recorded in the backlog commit `87e3c5e`; not
re-derived here per the task instruction — cited as prior evidence):**

| Analysis rate | Filter state | Measured cost |
| --- | --- | --- |
| 16 kHz (G.722/Opus) | ADAPTING (the common case — whenever the agent is speaking and its own echo is returning) | **~39.8 ms/frame (37–43 ms)** — ~2x the 20 ms ptime |
| 16 kHz | FROZEN (double-talk hold, no NLMS update) | ~13.9 ms/frame |
| 8 kHz (G.711) | ADAPTING | **~21.2 ms/frame** — also over the 20 ms ptime, though only ~6% over |

The existing `engine.py:267` comment — "512 taps measures ~6.9 ms/frame at 8 kHz and
~13.8 ms at 16 kHz — both safely under the 20 ms ptime" — and ADR-0033's matching
"Consequences" figure are **not wrong as measurements, but incomplete as a budget claim**:
both measured the FROZEN/estimate-only cost and did not account for the NLMS update loop.
Verified directly against `EchoCanceller.cancel()`
(`src/hermes_voip/media/aec.py:250-288`): every sample pays one `O(filter_len)`
estimate/energy pass (lines 268-274) unconditionally, and — only on a frame where
double-talk is **not** detected, i.e. exactly the frames where adaptation runs
(`aec.py:284`) — a **second** `O(filter_len)` tap-update pass (lines 284-288). This is the
structural reason an adapting frame costs materially more than a frozen one: the frozen case
in the engine.py comment measured roughly half the work an adapting frame actually does.
"Frozen" is not the common case in a live call — it only holds during double-talk (the
caller talking over the agent); whenever the agent is speaking into a quiet line (most of a
call's TTS playout), the filter is adapting and pays the full cost.

**The RX path is synchronous, so the stall is real, not theoretical.** `_inbound_gen`
(`src/hermes_voip/media/engine.py:1582`) calls `_decode`
(`src/hermes_voip/media/engine.py:1774`) then `_cancel_echo`
(`src/hermes_voip/media/engine.py:1785`) inline, in the same async media coroutine, with no
thread/executor offload. G.722 decode measures ~3.3 ms/frame at 16 kHz
(`_G722_DECODE_MEASURED_US_PER_FRAME_16K = 3_300.0`, `src/hermes_voip/media/g722.py:76`,
ADR-0094), so combined 16 kHz RX cost while adapting is **decode (~3.3 ms) + cancel
(~39.8 ms) ≈ 43 ms/frame** against a 20 ms ptime budget — the loop falls behind without
bound for as long as the agent's TTS is playing and its echo is returning.

**Consequence.** A `< 20 ms` per-frame CI budget gate — the ADR-0094 pattern bk1198 asked
for — cannot be added today without first changing the design: gating on the measured
numbers as shipped would either fail CI permanently or force AEC off as a side effect of a
docs/test task, neither of which is a decision this ADR is entitled to make silently
(AGENTS.md rule 40: architecture that is genuinely undecided is deferred on the record, never
defaulted from whatever happens to be installed).

## Decision

**No option is adopted by this ADR.** Consistent with the ADR-0060 precedent for a
rule-40 deferred decision, this record's "decision" is procedural: state the measured
problem honestly, enumerate the real options with their actual trade-offs (below), and give
a recommendation — the operator makes the final call, at which point this ADR is either
flipped to **Accepted** naming the operator as decider (if implemented as recorded) or
**Superseded by** a follow-up ADR (if the adopted shape differs materially from what is
sketched here), per `docs/adr/CLAUDE.md`.

### Recommendation

As an **interim, low-risk default-safety step**: disable AEC by default at 16 kHz
(option **b** below) — this is the rate where the synchronous-loop stall is most severe
(~2x ptime) and where AEC also delivers the least differentiated value today, since the
512-tap cap already limits the 16 kHz window to ~32 ms (never the full 64 ms the 8 kHz path
gets). This stops the unbounded stall risk immediately with a one-line default change and no
new design. It is **not a complete fix**: 8 kHz measures ~21.2 ms adapting, itself ~6% over
budget, so an interim disable scoped to 16 kHz only leaves a smaller residual risk at 8 kHz
that should get either the same treatment or a smaller filter-length trim (option **a**'s
territory) in the same change — this ADR does not pick between those two for 8 kHz and
leaves it to the operator alongside the main 16 kHz call.

The **durable fix** is a real design project, not a constant tweak, and should land as its
own ADR once the operator picks a direction: either **(c)** thread-offloading
`_cancel_echo` off the event-loop coroutine, or **(d)** a latency-budgeted block/partitioned
AEC that deliberately re-litigates ADR-0033's no-added-latency requirement (rather than
silently reversing it). **(e)** — accepting numpy in the `media`-extra path — is not
recommended as a first move: it reverses ADR-0033's explicit no-numpy constraint for the
*entire* media-extra path to fix *one* component's constant, a broad architectural and
supply-chain (rule 35) cost for a narrow gain, and is worth revisiting only if (c) and (d)
both prove insufficient.

## Consequences

- **No code ships with this ADR.** AEC remains ON by default at both 8 kHz and 16 kHz,
  exactly as ADR-0033 shipped it, including the still-over-budget synchronous RX path — the
  stall risk is **unchanged** until the operator picks an option and it is implemented. This
  is a design record, not a mitigation (rule 27 — this ADR does not claim a fix it has not
  built).
- **bk1198 stays open** with this ADR as its design reference (cross-linked in
  `docs/backlog.md`) instead of a bare measurement — a future session has a concrete,
  trade-off-scored option set instead of re-deriving one.
- **The ADR-0094-style `< 20 ms` CI budget gate for AEC is blocked** until an option is
  chosen and implemented; adding it against the current default would either fail CI
  permanently or silently force a behaviour change no ADR yet authorises.
- **Whichever option is chosen still has to be measured and gated** the same way ADR-0094
  gated G.722, once it exists — this ADR does not relax that bar, it only defers picking
  which design gets measured.

## Alternatives considered

Unlike a normal Accepted ADR, none of these is rejected — this is the open menu the operator
chooses from. The table below records each option's real trade-off, not a rejection reason.

| Option | Trade-off |
| --- | --- |
| **(a)** Shorten the default filter so cancel+decode fits under ptime (e.g. back toward a ~16 ms window) | ADR-0033's own design history (commit `ff73953`) found a 16 ms window leaves a ~40 ms-delayed broadband echo essentially uncancelled — this directly trades echo-cancellation reach for CPU, the exact regression ADR-0033 fixed by lengthening the window in the first place. |
| **(b)** Disable AEC by default at 16 kHz (recommended interim step, see above) | Immediately stops the worst-case stall with a one-line default change and no new design, but reintroduces ADR-0023's sluggish 600 ms sustained-speech barge-in gate at 16 kHz by default, and does not by itself address the smaller ~6%-over-budget residual at 8 kHz. |
| **(c)** Offload `_cancel_echo` to a worker thread | Moves the O(taps) work off the asyncio event-loop thread so the media coroutine does not stall, but does not reduce total CPU cost — the host still needs real headroom to keep up in wall-clock time or the executor queue backs up; introduces thread-safety questions for the canceller's mutable NLMS tap state and a scheduling-jitter hop the current design does not have. A real design project (threading model + safety), not a constant tweak. |
| **(d)** Block/partitioned frequency-domain AEC (FDAF/PBFDAF) | ADR-0033 explicitly rejected FDAF for adding a block of algorithmic latency (it processes a block at a time), violating the recorded no-added-latency requirement. Adopting it now means *deliberately* re-litigating that requirement with an explicit new latency budget — a legitimate design choice, but a reversal of a previously-recorded constraint, not a free win. |
| **(e)** Accept a numpy dependency in the `media`-extra engine path | `aec.py`'s module docstring and ADR-0033 both state the canceller "must not pull the `ml` extra's numpy" into the media-extra path. A vectorised implementation could plausibly cut the per-frame constant substantially, but this reverses ADR-0033's explicit no-numpy design constraint and widens the lean media extra's supply-chain/licence surface (rule 35) — a real rule 22 (efficiency) vs. rule 35/architecture-boundary trade-off, not a narrow fix. |

## References

- `docs/backlog.md` bk1198 (AEC hot-path CPU budget item, re-scoped in commit `87e3c5e`)
- ADR-0033 (`docs/adr/0033-in-process-aec-aggressive-barge-in.md`) — the AEC design, the
  16 ms→64 ms window history (commit `ff73953`), and the FDAF/numpy constraints this ADR's
  options (d)/(e) would reverse
- ADR-0094 (`docs/adr/0094-g722-hot-path-cpu-budget.md`) — the sibling hot-path CPU-budget
  ADR and the measured G.722 decode figure this ADR's combined-RX arithmetic uses
- ADR-0060 (`docs/adr/0060-s3-tables-iceberg-call-events-recordings.md`) — the precedent for
  a `Proposed — Deferred` ADR status under AGENTS.md rule 40
- `src/hermes_voip/media/engine.py:255-273` (filter-length/tap-cap constants and the
  frozen-only comment this ADR corrects the scope of), `:1582-1794` (`_inbound_gen`'s
  synchronous decode → cancel chain)
- `src/hermes_voip/media/aec.py:196-296` (`EchoCanceller.cancel` — the estimate-always /
  update-only-when-adapting loop structure)
- `src/hermes_voip/media/g722.py:74-77` (measured G.722 decode constant)
