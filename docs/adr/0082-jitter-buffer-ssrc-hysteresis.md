# ADR-0082 — JitterBuffer SSRC auto-reset hysteresis

| Field       | Value                        |
|-------------|------------------------------|
| Status      | Accepted                     |
| Date        | 2026-06-27                   |
| Supersedes  | —                            |
| Superseded by | —                          |

## Context

`JitterBuffer.push()` tracks the stream SSRC and resets sequence state when a packet
arrives with a different SSRC.  This handles re-INVITE / source-resync correctly — the new
source starts from a clean anchor rather than being compared against stale sequence numbers
from the old one.

Before this ADR the reset fired on the **first** foreign-SSRC packet.  Because SRTP
authentication (which sits above this module) rejects packets from unexpected sources,
a misrouted stray packet with a foreign SSRC that somehow passes SRTP auth would silently
flush all buffered audio — a defence-in-depth gap.

## Decision

Add `ssrc_hysteresis: int = 3` to the `JitterBuffer` constructor.  The reset fires only
after **N consecutive packets bearing the same foreign SSRC** have arrived.

State machine per `push()` call:

```
packet.ssrc == current home SSRC
    → clear candidate, count=0, enqueue normally

packet.ssrc != home SSRC, matches current candidate SSRC
    → count += 1
    → if count < N: drop packet silently (return early)
    → if count == N: reset(), adopt candidate as new home, enqueue packet

packet.ssrc != home SSRC, does NOT match current candidate
    → candidate = packet.ssrc, count = 1
    → count < N: drop packet silently

home SSRC packet arrives mid-run
    → candidate cleared, count = 0 (run must restart from scratch)
```

`reset()` also clears `_ssrc_candidate` and `_ssrc_candidate_count` so a manual reset
returns to a fully clean state.

### Default N = 3

Three consecutive packets is the smallest count that:

- Distinguishes a brief stray burst (one or two misrouted packets) from a genuine
  source change (a sustained new sender).
- Keeps the adoption latency under 60 ms at the standard 20 ms ptime — imperceptible
  to a listener — while covering the common "two back-to-back identical packets from a
  new source" case in session setup.
- Matches the empirical minimum for SSRC collision resolution noted in RFC 3550 §8.2
  (which uses a different metric but the same general principle of requiring confirmation
  before acting on an SSRC change).

`ssrc_hysteresis=1` restores the pre-ADR behaviour exactly: reset on the first foreign
packet.  Callers that sit above reliable SRTP auth and want the fastest possible resync
can opt in.

### Why not a time-based window?

The JitterBuffer operates entirely on the packet-arrival sequence, with no wall-clock
access.  Keeping hysteresis in packet counts preserves the fully-deterministic, clockless
property that makes the buffer straightforward to unit-test and reason about.

## Consequences

- A single stray / misrouted foreign-SSRC packet no longer flushes the buffer (defence in
  depth even when SRTP is not yet keyed or is bypassed in testing).
- Genuine source changes (re-INVITE, SSRC collision resolution) adopt the new SSRC after
  at most `ssrc_hysteresis * ptime` ms of held audio — 60 ms at the default of 3
  and 20 ms ptime.
- The existing `test_jitter_ssrc_change_auto_resets_sequence_anchor` test is updated to
  use `ssrc_hysteresis=1` to preserve coverage of the immediate-reset path.
- `JitterBuffer.ssrc_hysteresis` is exposed as a read-only property for introspection.
