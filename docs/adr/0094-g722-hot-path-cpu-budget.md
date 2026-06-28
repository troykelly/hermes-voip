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

1. `src/hermes_voip/media/g722.py` records the measured per-frame hot-path costs and the
   combined budget in module-level constants:
   - `_G722_ENCODE_MEASURED_US_PER_FRAME_16K = 3400.0`
   - `_G722_DECODE_MEASURED_US_PER_FRAME_16K = 3300.0`
   - `_G722_COMBINED_BUDGET_US_PER_FRAME_16K = 20000.0`
2. `tests/test_media_g722_budget.py` gates the hot path directly:
   - deterministic existence/range checks for the documented constants;
   - a measured encode check that must stay within 3x the documented encode baseline;
   - a measured decode check that must stay within 3x the documented decode baseline;
   - a measured **combined encode+decode** check that must stay below the 20 ms packet
     period while G.722 remains preferred by default.
3. The benchmark avoids precision-performance flake while still being real CI evidence:
   no sleeps, no network, no benchmark dependency, warm-up before timing, five repeated
   batches of 20 frames, and the best batch is used to reduce scheduler-noise sensitivity.
   The budget is coarse (20 ms), so the test fails on meaningful CPU regressions rather
   than normal host variance.
4. The measurement method is the project devcontainer on CPython 3.13.5, running repeated
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
  codec cost cannot pass by leaving stale constants unchanged: CI measures encode and
  decode against the documented baselines with tolerance, and measures combined
  encode+decode against the 20 ms packet-period budget.
- The benchmark is intentionally coarse; it is NOT a precision performance score. The
  3x per-side tolerance plus best-of-repeated-batches design avoids CI flake while still
  failing meaningful regressions and any combined over-budget path.
- We are now committed to revisiting this ADR if host measurements or production evidence
  show the pure-Python path approaching the 20 ms packet budget. The engine seam from
  ADR-0022 still allows a later switch to a compiled base dependency if CPU, not licence or
  portability, becomes the limiting factor.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Stop preferring G.722 by default until a budget exists | Once measured, the pure-Python cost is still comfortably within the 20 ms frame period (~6.7 ms combined encode+decode), so demoting G.722 would knowingly discard wideband quality without an actual budget breach. |
| Keep separate 15 ms encode and decode ceilings | Rejected after review: 14 ms encode + 14 ms decode would pass separately while exceeding the single 20 ms packet-period budget with G.722 still preferred. |
| Add a tight wall-clock benchmark threshold (for example 4 ms exact) | Too CI-flaky across hardware, scheduler load, and interpreter versions. Rule 22 needs an honest budget gate, not a noisy one. |
| Add a heavy benchmarking dependency (pytest-benchmark / perf harness) | Unnecessary for this repository’s needs; a small real benchmark plus recorded constants captures the safety goal without expanding the dependency surface. |
| Leave ADR-0022’s “heavier than C” statement as-is | Fails rule 22 because the cost stays qualitative and ungated; future regressions would have no concrete number to compare against. |
