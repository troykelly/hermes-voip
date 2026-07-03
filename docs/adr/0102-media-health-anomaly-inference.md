# ADR-0102: media-health anomaly inference at call teardown

- Status: Accepted
- Date: 2026-07-03

## Context

`_teardown_call` already emits `rtcp_call_quality` — the raw loss / jitter / RTT numbers
from `CallQuality` (ADR-0061). Those are queryable but require an operator to interpret
them. Two operationally important conditions are not surfaced as named, queryable
signals (backlog item 1295):

- a **one-way** media path — the classic NAT / SDP-media-address failure where audio
  flows in only one direction; and
- a **degraded** call — sustained high loss and/or jitter.

We want both as ADR-0075 structured events, **without** adding a control decision (they
are diagnostic only) and **without** expanding the engine's media API with new packet
counters.

## The signal available at teardown

`CallQuality` carries two independent views:

- `local_*` — what WE received from the peer (`None` until any inbound RTP arrives).
- `remote_*` — what the peer reported it received from US (`None` until a peer RTCP
  report arrives).

Having BOTH views disambiguates a one-way failure from a call simply too short to
produce RTCP: `local is None` alone is ambiguous, but `local is None AND remote is
present` proves the peer received our stream while we received nothing.

## Decision

Add a pure `infer_media_anomalies(CallQuality) -> tuple[MediaAnomaly, ...]` in
`media/engine.py` (unit-tested in the default gate) and emit each returned anomaly from
`_teardown_call` as a structured event alongside `rtcp_call_quality`:

| event | reason | condition |
|---|---|---|
| `one_way_audio` | `no_inbound_rtp` | `local is None` AND `remote` present |
| `one_way_audio` | `peer_no_inbound` | `local` present AND `remote` present AND `remote >= 0.9` |
| `media_degraded` | `high_loss` | loss on a present view `> 0.05` (excluding the one-way `>= 0.9` outbound case) |
| `media_degraded` | `high_jitter` | jitter on a present view `> 30 ms` |

Thresholds are module-level `Final` constants: `_MEDIA_DEGRADED_LOSS = 0.05` (5%),
`_MEDIA_DEGRADED_JITTER_MS = 30.0`, `_ONE_WAY_PEER_LOSS = 0.9` (90%). The signals are
**independent** — a call may be both one-way and degraded. Each event carries `call_id`
+ the fixed `reason` + the numeric `CallQuality` metrics only — no address / caller
identity / PII (rule 34 / ADR-0084). Anomalies are emitted only when RTCP was active
(inside the existing `_rtcp_active` branch), so a call with no RTCP produces neither
these nor `rtcp_call_quality`.

## Consequences

- Operators query `event=one_way_audio` / `event=media_degraded` directly (runbook 0014)
  instead of eyeballing raw loss/jitter.
- Purely additive: no teardown control-flow or timing change; errors still propagate
  (rule 37).
- The thresholds are **diagnostic defaults** (reversible — tune from real-call data,
  rule 26); they are not a control decision, so a false positive/negative only
  mis-labels a log line, never affects a call.

## Alternatives considered

- **Expose packet counters on `CallQuality`** for a richer one-way signal — rejected:
  it expands the engine media API for marginal gain; the local/remote-view
  disambiguation is sufficient and needs no new plumbing.
- **Infer outbound-dead from `remote is None`** — rejected: `remote is None` is
  ambiguous (a peer that simply sends no RTCP is indistinguishable from a dead outbound
  path), so we require an explicit near-total-loss peer report (`>= 0.9`).
- **Make the events mutually exclusive** with `rtcp_call_quality` / each other —
  rejected: they answer different questions (raw numbers vs named condition; direction
  vs quality), so co-emission is simplest and most informative.

## Follow-ups

Threshold tuning from production call data (rule 26) once v0.3.0+ is live-tested; the
reverse-direction `remote is None` ambiguity could be revisited if the engine later
exposes an authoritative outbound-sent count.
