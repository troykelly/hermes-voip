# ADR-0094: G.722 hot-path CPU budget stays explicit and gated

- **Date:** 2026-06-28
- **Status:** Accepted
- **Deciders:** agent session (G.722 CPU-budget lane)

## Context

Backlog item bk1197 calls out a real efficiency risk: the SIP/SDES media menu still
prefers G.722 first (`src/hermes_voip/adapter.py`), but the codec implementation in
`src/hermes_voip/media/g722.py` is a fully-typed pure-Python port of the public-domain
reference (ADR-0022), not a C codec. That means the RTP hot path pays the codec cost on
**every 20 ms packet** in both directions:

- `G722Encoder.encode` on the outbound `RtpMediaTransport.send_audio` path.
- `G722Decoder.decode` on the inbound `RtpMediaTransport.inbound_audio` path.

ADR-0022 recorded the qualitative trade-off (“heavier than a C codec”) but did not pin a
concrete number or CI gate. Rule 22 requires the cost to be measured and documented, not
left as a hand-wave, and this repo must avoid flaky benchmark machinery or heavy new
benchmark dependencies.

The alternative backlog option was to stop preferring G.722 by default until a budget
existed. That is safer than flying blind, but it would knowingly throw away wideband on
any peer that can carry it, even if the measured cost is still well inside the 20 ms
packet deadline.

## Decision

We KEEP G.722 preferred by default on the SIP/SDES menu and add an explicit, CI-friendly
CPU budget gate instead of demoting it.

Concrete shape:

1. `src/hermes_voip/media/g722.py` records the measured per-frame hot-path costs in two
   module-level constants:
   - `_G722_ENCODE_MEASURED_US_PER_FRAME_16K = 3400.0`
   - `_G722_DECODE_MEASURED_US_PER_FRAME_16K = 3300.0`
2. `tests/test_media_g722_budget.py` gates the hot path with the same pattern already used
   by `tests/test_call_progress_microbench.py`:
   - deterministic existence/range checks for the documented constants;
   - a machine-checked assertion that encode + decode stays below one 20 ms frame period;
   - a **very generous** 15 ms wall-clock ceiling for encode and decode individually, used
     only as a catastrophic-regression safety net.
3. The measurement method is the project devcontainer on CPython 3.13.5, running repeated
   `G722Encoder().encode(frame)` / `G722Decoder().decode(frame)` loops on a 20 ms, 16 kHz,
   320-sample synthetic PCM frame (2000-3000 repetitions). The stateful continuous-stream
   path is the reference because production constructs one codec object per call direction
   and reuses it frame-by-frame.

Measured numbers on 2026-06-28:

- Encode: ~3.4 ms / 20 ms frame
- Decode: ~3.3 ms / 20 ms frame
- Combined: ~6.7 ms / frame (~33% of the 20 ms ptime)

Those numbers leave material headroom inside the packet deadline, so demoting G.722 would
be an unnecessary quality loss today.

## Consequences

- The SIP/SDES answerer preference remains wideband-first (`G722`, then G.711 fallback),
  so a peer that can carry G.722 still gets wideband audio by default.
- The CPU trade-off is no longer implicit. A future change that materially increases the
  codec cost must update the constants and will trip the budget test if it becomes
  catastrophic.
- The wall-clock gate is intentionally loose; it is NOT a precision benchmark. The exact
  number of record remains the documented constants plus this ADR. This avoids CI flake
  while still catching “something is badly wrong” regressions.
- We are now committed to revisiting this ADR if host measurements or production evidence
  show the pure-Python path approaching the 20 ms packet budget. The engine seam from
  ADR-0022 still allows a later switch to a compiled base dependency if CPU, not licence or
  portability, becomes the limiting factor.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Stop preferring G.722 by default until a budget exists | Once measured, the pure-Python cost is still comfortably within the 20 ms frame period (~6.7 ms combined encode+decode), so demoting G.722 would knowingly discard wideband quality without an actual budget breach. |
| Add a tight wall-clock benchmark threshold (for example 4 ms exact) | Too CI-flaky across hardware, scheduler load, and interpreter versions. Rule 22 needs an honest budget gate, not a noisy one. |
| Add a heavy benchmarking dependency (pytest-benchmark / perf harness) | Unnecessary for this repository’s needs; a small deterministic test plus recorded constants captures the safety goal without expanding the dependency surface. |
| Leave ADR-0022’s “heavier than C” statement as-is | Fails rule 22 because the cost stays qualitative and ungated; future regressions would have no concrete number to compare against. |
