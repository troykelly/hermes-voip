# ADR-0107: Re-point media on a re-INVITE that relocates the peer RTP endpoint

- **Date:** 2026-07-04
- **Status:** Accepted
- **Deciders:** agent session (MEDIUM-severity one-way-audio fix)
- **Relates to:** ADR-0015 (symmetric-RTP comedia latching), ADR-0053 (SDES-SRTP
  in-dialog re-key), ADR-0081 (drop-whole an unanswerable in-dialog request)

## Context

An in-dialog re-INVITE can RELOCATE the peer's RTP endpoint — a new SDP
`c=`/`m=audio` address or port. This is normal telephony: an attended-transfer
media re-anchor to the transfer target, a music-on-hold server that holds on one
address then resumes the call elsewhere, or a session-border controller that moves
the media relay mid-call.

`CallSession._on_reinvite` answered such a re-INVITE `200 OK`, re-keyed SRTP
(ADR-0053), bumped the SDP version and flipped the hold state — but never re-pointed
the media engine's outbound target. `RtpMediaTransport` fixes its remote address at
construction, and the comedia latch (`_maybe_latch`, ADR-0015) latches ONCE onto the
peer's first genuine RTP source and never moves again; `set_hold` does not reset the
latch. So after a relocation the agent's outbound RTP kept flowing to the stale
(latched) address: the caller heard nothing while the call stayed up — ONE-WAY (dead)
audio, and the agent was misled into thinking it was still talking to the caller.
Resume did not self-heal because nothing reset the latch.

In-place hold/resume (a direction flip on the SAME `c=`/`m=` endpoint) was correct
and must stay correct: it must not disturb a working comedia latch (which may point
at a NAT-rewritten source that differs from the SDP address).

## Decision

`CallMedia` gains an `async set_remote(address, port)` seam. `RtpMediaTransport`
implements it: under the TX lock it sets `_remote_address`/`_remote_port` and the live
send target `_outbound_addr` to the new endpoint and resets `_latched = False`, so
`_maybe_latch` re-learns the relocated peer's real source. It validates the port range
`1..65535` like the SDP builders (port 0 is "no media", never a destination) and
NO-OPS when the endpoint already matches the negotiated remote — so an in-place
hold/resume keeps its established latch untouched. The mutation is under `_tx_lock`
because `_outbound_addr` is the tuple the send path aims at, so the re-point cannot
race a packet mid-send.

`CallSession._on_reinvite`'s `MediaUpdate` branch calls `set_remote` after the `200`
is built and the SRTP re-key is committed, and BEFORE the hold flip, so a
resume-and-relocate resumes onto the new target. It re-points only when the peer will
receive our media (`held_by_peer` is false) and the offer names a real endpoint (a
non-`None` connection address and a non-zero port). A held or black-hole (`c=0.0.0.0`)
re-offer names no live target and is skipped — the peer is not receiving our media
anyway, and the subsequent resume carries the real endpoint. All existing re-INVITE
behaviour is preserved: SRTP re-key (ADR-0053), SDP version bump, hold flip, and the
ADR-0081 drop-whole-on-unanswerable ordering (the re-point happens only on the
committed-answer path, never on a dropped re-INVITE).

## Consequences

- A relocating re-INVITE now restores two-way audio: attended-transfer media
  re-anchor, MoH resume-elsewhere and SBC media relocation all follow the peer.
- The comedia latch is re-armed exactly once per relocation, so the engine re-learns
  the new peer's real (possibly NAT'd) source on its first packet from the new path.
- In-place hold/resume is provably unaffected: the engine no-ops an unchanged
  endpoint, so a plain direction flip never resets a working latch.
- `CallMedia` implementers must provide `set_remote`; the real engine and the test
  doubles do. This is a small, additive seam, consistent with `set_hold`/`rekey_srtp`.
- The re-point trusts the answered offer's endpoint. That is the same trust boundary
  as the initial INVITE's `c=`/`m=` and is bounded by comedia anti-spoofing: the
  re-armed latch still only re-latches on a genuine RTP packet carrying the negotiated
  payload type (ADR-0015), so a forged off-path packet cannot steer the stream.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Always call `set_remote` on every `MediaUpdate` and let the engine decide | A black-hole (`c=0.0.0.0`) or port-0 hold re-offer would try to aim RTP at an impossible target and needlessly reset the latch; gating on `not held_by_peer` (which already subsumes black-hole holds) is precise and matches the bug (one-way audio only matters when the peer expects our media). |
| Track the current remote in `CallSession` and compare there | Duplicates state the engine already owns; the engine is the single source of truth for the live target, so the unchanged-endpoint no-op belongs there. |
| Reset the latch inside `set_hold` on resume | `set_hold` has no endpoint; it cannot distinguish a relocation from an in-place resume, so it would either miss relocations or destroy a working latch on every resume. |
| Re-point after the hold flip | On a resume-and-relocate that resumes sending before the target moves, one or more packets go to the stale address; re-pointing first resumes onto the correct endpoint. |
