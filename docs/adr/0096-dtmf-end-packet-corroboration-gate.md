# ADR-0096: DTMF end-packet corroboration gate — two agreeing end packets required before a digit is trusted

- **Date:** 2026-07-02
- **Status:** Accepted
- **Deciders:** agent session (fix/dtmf-conflicting-event-substitution lane); re-verified
  independently by a second agent session (rule 32) before merge
- **Relates to:** ADR-0009 (DTMF is the spoof-resistant confirmation channel), ADR-0010
  (RFC 4733 primary / SIP INFO fallback / in-band last resort), ADR-0077 (the
  `DtmfPress | DtmfNoPress` result type and the single `_window` structure this ADR's
  `_PendingTimestamp` state replaces — ADR-0077 does **not** cover the corroboration gate
  decided here; several docstrings in the fix's first commit mis-cited ADR-0077 for that,
  corrected in the same commit that adds this record)

## Context

`DtmfReceiver.feed()` (`src/hermes_voip/dtmf.py`) collapses RFC 4733's three redundant
end-of-tone packets (§2.5.1.4, all sharing one RTP timestamp) into a single `DtmfPress`.
Prior to this decision, the receiver trusted the **first** end packet it ever saw for a
timestamp outright: it recorded the event code and returned `DtmfPress` immediately: later
packets at the same timestamp were deduped (`DUPLICATE_END` if the code matched) or, after
an earlier fix in this same PR (`f72d024`), flagged (`CONFLICTING_EVENT`) if the code
differed — but that flag was cosmetic. It relabelled the second packet; it did not change
what the first packet had already done.

DTMF is not incidental input. Per ADR-0009 it is **the spoof-resistant confirmation
channel** for `ToolRisk.IRREVERSIBLE` actions (transfers, payments, bookings) precisely
because, per ADR-0010, a keypress on its own RTP payload type is materially harder to
forge than recognised speech. That property only holds if the receiver itself cannot be
fooled by a forged packet on the media path. The media path here carries no
transport-layer authentication (SRTP is a separate, independent architectural decision,
out of scope for this fix and not assumed) — RTP telephone-event packets are unauthenticated
UDP payloads, and nothing stops an attacker who can reach the media path from injecting one
with an arbitrary event code and RTP timestamp.

**The concrete defect this ADR fixes.** An attacker who injects a single forged end packet
that reaches the receiver *before* the genuine end packet for the same RTP timestamp — a
one-packet arrival race, trivial on an unauthenticated UDP path — had their forged digit
accepted and acted on (including, in principle, resolving an ADR-0009 confirmation with a
digit the caller never pressed), because "first packet seen" and "genuine packet" were
treated as the same thing. This was caught by an independent rule-32 re-verification that
actually ran the forged-first ordering against the shipped fix and observed a `DtmfPress`
for the forged digit, contradicting the fix's own docstrings — a rule 27 violation this
ADR and its accompanying commit also correct.

## Decision

`DtmfReceiver.feed()` never emits a `DtmfPress` on the first end packet it sees for a
timestamp. It requires a **second, agreeing** end packet before trusting a digit — a
corroboration gate:

- **1st end packet for a new timestamp** → new `DtmfNoPress.AWAITING_CORROBORATION`
  variant. The event code is recorded (`_PendingTimestamp(event=..., emitted=False,
  poisoned=False)`); nothing is emitted yet.
- **2nd end packet, same event code** → now trusted: `state.emitted = True`, returns
  `DtmfPress`. Any further packet at this timestamp with the same code collapses to the
  existing `DUPLICATE_END`.
- **A disagreeing event code seen BEFORE a digit has been emitted** → `state.poisoned =
  True`, permanently. `CONFLICTING_EVENT` is returned now and for **every** later packet
  at this timestamp, even one whose code matches the code first recorded — because that
  first code might itself have been the forged one; once two packets for a timestamp have
  disagreed pre-emission there is no way to tell which sender was genuine, so the receiver
  **fails safe (no digit emitted) rather than fail open (the wrong digit accepted)**.
- **A disagreeing event code seen AFTER a digit has already been emitted** → flagged
  `CONFLICTING_EVENT` for visibility (logged at DEBUG in `_handle_inbound_dtmf`, matching
  the file's existing diagnostic conventions) but does **not** retroactively poison the
  timestamp: the digit was already safely corroborated by two packets that agreed *before*
  any conflict was seen, so it cannot be un-emitted, and further packets that agree with it
  keep collapsing to ordinary `DUPLICATE_END`.

Concrete evidence the fix closes the demonstrated race (`tests/test_dtmf.py`):

```
forged end(9) [1st packet for ts] -> AWAITING_CORROBORATION
genuine end(3) [2nd packet, ts]   -> CONFLICTING_EVENT
DtmfPress ever emitted for ts?    -> False
```

`test_receiver_never_accepts_forged_digit_that_arrives_first` pins exactly this ordering
(red before the fix, green after); `test_receiver_still_dedups_agreeing_duplicate_after_conflict_probe`
pins that a post-emission conflict does not disturb an already-decided genuine digit.

### The reliability cost this trades in, quantified

RFC 4733 §2.5.1.4 sends `_REDUNDANT_END_COUNT = 3` end packets per key-press. Before this
change, one surviving end packet was sufficient to emit a digit. After this change, a digit
needs **two of those three** to survive transit and agree. A tone whose end-of-tone burst
loses two of its three end packets — a short burst-loss event coinciding with key release,
rather than the independent per-packet loss RFC 4733's redundancy is designed to absorb —
now drops the digit entirely (the caller must press again) instead of emitting it from the
sole survivor. This is a **deliberate** trade, not an oversight: it applies to **all**
inbound RFC 4733 digit reception through this one shared `DtmfReceiver` — both the ADR-0009
confirmation path and ordinary IVR/menu-navigation digits (ADR-0010's `[DTMF]`-tagged
`MessageEvent` path) — favouring fail-safe on the confirmation channel over the module's
previous fail-open bias toward "never miss a digit." The failure mode is bounded: a dropped
digit is silence, never a substituted wrong one.

### Residual scope (honest limitation, not eliminated)

Corroboration requires two end packets to **agree**, not two packets from the **same**
sender. An attacker who wins the arrival race on **both** of the first two end packets for
a timestamp — two colluding forged copies that agree with each other, arriving before the
genuine pair — is indistinguishable from a genuine corroborated pair without transport-layer
authentication (SRTP); their forged digit is still accepted. This raises the bar from a
single-packet race to a same-outcome two-packet race. It does not eliminate spoofing on an
unauthenticated media path, and no receiver-side heuristic can without SRTP.
`test_residual_two_colluding_forged_packets_still_win` pins this as a real, currently-accepted
limitation — not prose that can silently drift out of sync with the code (rule 27).

## Consequences

- **Security:** closes the demonstrated single-packet substitution race against the
  ADR-0009 confirmation channel. The residual (two-packet collusion) is a strictly smaller
  attack surface than before this fix, not a full close — see above.
- **Reliability:** a legitimate digit now requires 2 of its 3 redundant end packets to
  survive and agree, on both the confirmation path and ordinary menu-navigation DTMF. Loss
  is bounded to "digit dropped, caller retries," never "wrong digit accepted."
- **Docs:** the enum/class/method docstrings in `dtmf.py` and the test docstrings in
  `test_dtmf.py` are corrected in the same commit that introduces this ADR to cite
  `ADR-0096` (this record) rather than the unrelated `ADR-0077`.
- **No new infrastructure, no API surface change beyond the new `AWAITING_CORROBORATION`
  variant** on the already-internal `DtmfNoPress` enum; `_handle_inbound_dtmf`
  (`media/engine.py`) already treated every non-`DtmfPress` result as "no digit yet"
  (`isinstance(result, DtmfPress)`), so the new variant needs no caller change.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| **Track the in-progress (`end=False`) update packets' event code and reject an end packet that mismatches it** (raised during review as a smaller, potentially no-reliability-cost fix) | Real advantage where genuine update packets already established the in-progress code: a single surviving end packet could be trusted immediately (no corroboration wait, no reliability cost) while still rejecting a mismatching forged end packet outright. But it needs its own fallback for a timestamp whose update packets never arrived or were themselves forged (an attacker able to inject arbitrary RTP telephone-event packets can inject forged update packets as readily as forged end packets), and that fallback would have to be this same two-end-packet corroboration gate anyway — so it is a possible *future* latency/reliability refinement layered on top of this gate, not a replacement for it. Deferred; the two-packet gate alone was independently re-verified (rule 32) to close the demonstrated repro, which is the bar this fix must clear. |
| **Do nothing beyond the existing label-only fix (`f72d024`)** | Proven, by the very re-verification that prompted this ADR, not to close the substitution: the forged-first ordering still yielded `DtmfPress` for the forged digit. Relabelling the second (already-harmless) packet gave zero protection against the first. |
| **Defer entirely to SRTP / transport-layer authentication** | SRTP is a separate, independent architectural decision that does not ship as part of this fix and cannot be assumed. A receiver-side corroboration gate is available today, regardless of SRTP's status, and remains a defense-in-depth layer even after SRTP ships. |
| **Require all 3 redundant end packets to agree before emitting (stronger corroboration)** | Over-fragile: drops a digit on any single lost or reordered end packet — a materially more common case than the 2-of-3 loss this design tolerates — for no real security gain, since an attacker able to forge 2 colluding packets can as easily forge 3. |

