# ADR-0015: Symmetric-RTP (comedia) latching in the media engine

- **Date:** 2026-06-16
- **Status:** Accepted
- **Deciders:** agent session (live-NAT-proven), operator direction

## Context

The runtime is behind NAT: it advertises a private RTP address in the SDP answer
(`adapter.py` derives the real local interface — ADR-0002 §NAT — but under NAT
that interface is still private), while the peer gateway's media originates from a
public address. For two-way audio when either side is behind NAT, sending to the
SDP `c=`/`m=` address is not enough — that address can be a private or
SBC-rewritten address the peer's media never actually comes from.

The on-answer greeting (PR #52) gave us the *send-first* half of comedia: we emit
RTP immediately so a comedia gateway latches onto **us** and the return path
opens. The missing half is **our** side of comedia: sending to wherever the
peer's media actually originates, learned from the wire — so a call works even
when the peer honours its own (private/wrong) SDP literally.

Constraints: the plugin must stay gateway-agnostic (CLAUDE.md — no vendor quirks
in the core); fully-typed, no escape hatches (AGENTS.md 17/39); errors propagate
(rule 37); no PII in logs beyond the gateway's media `ip:port` (which is
operational, not PII).

## Decision

`RtpMediaTransport` (`src/hermes_voip/media/engine.py`) keeps a mutable
`_outbound_addr` that `send_audio` targets. It is initialised to the
SDP-negotiated remote (so the greeting goes out immediately — the send-first path
must not break) and, on the **first valid inbound RTP packet**, latched onto that
packet's actual UDP source `(ip, port)`. Subsequent sends go to the latched
address. The engine receives and sends on **one** bound UDP socket, so the NAT
mapping the peer's comedia targets is the same hole we send from (symmetric).

The inbound datagram queue carries `(datagram, source-addr)` pairs (type
`_Datagram`) so the source tuple reaches the latch point. Latching happens in
`_maybe_latch`, called only after a datagram has been proven to be genuine RTP
(it parsed via `RtpPacket.parse`, or — under SRTP — authenticated via
`unprotect`).

**Anti-spoofing**, three guards before the target moves:

1. `HERMES_VOIP_RTP_SYMMETRIC` must be on (default true; `MediaConfig.rtp_symmetric`,
   threaded into the engine as `symmetric=`). When false the engine always
   honours the SDP address.
2. The packet must carry the **negotiated audio payload type** (`Codec.value`),
   so neither random noise that happens to set the RTP version bits nor an
   off-codec stray (a stray DTMF/CN event before any audio) triggers a latch.
   Garbage that does not parse as RTP never reaches `_maybe_latch` at all.
3. The latch fires **once per call** and then sticks: the first valid source
   wins; a later packet from a different tuple (a spoof, or a re-routed media
   path) cannot move it. A safe re-latch is explicitly out of scope (keep it
   simple: latch on first valid RTP, stick).

A latch logs one operational line — `rtp: latched to <ip>:<port>` — at INFO. The
latch state resets in `connect()` so a reused engine re-latches on its next call.

## Consequences

- Two-way audio now works for the live NAT'd inbound call (UCM6304-behind-NAT)
  even where the peer routes RTP by its own private/wrong SDP address — the engine
  follows the real media source. The greeting (send-first) and this latch
  (send-to-real-source) together cover the common comedia gateway end to end.
- The behaviour is vendor-neutral: it keys off the wire, not any gateway quirk.
  Gateways that route RTP strictly by the negotiated SDP address are served by
  `HERMES_VOIP_RTP_SYMMETRIC=false` (then a correct public SDP address via
  rport/STUN is the path — runbook 0002 §8 option 3).
- The recv queue element type changed from `bytes` to `(bytes, addr)`; this is an
  internal seam (not part of the `MediaTransport` Protocol), invisible to callers.
- Latch-once-and-stick means a mid-call media re-route (rare; e.g. a re-INVITE
  moving the media endpoint) is **not** followed. Accepted for now; a guarded
  re-latch can supersede this ADR if a real case appears.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Trust the SDP `c=`/`m=` address only | Fails the live NAT case — a public gateway cannot reach our private address, and the peer's media may come from an address its own SDP does not name. |
| Latch on the **first datagram of any kind** | An attacker (or stray non-audio packet) racing the first real audio packet could hijack our outbound media. Requiring a parseable RTP packet with the negotiated audio PT removes the trivial spoof. |
| Continuously re-latch to the latest source | More attack surface and instability (a single spoofed packet moves the stream); the once-and-stick policy is simpler and safe. Revisit only with a concrete mid-call re-route requirement. |
| Solve NAT only with the on-answer greeting (send-first) | Sufficient only when the gateway *itself* does comedia; a peer that honours its own (wrong) SDP address still never receives our media. This ADR adds our side. |
| ICE / STUN / TURN for full NAT traversal | Heavier machinery and dependency/transport surface (ADR-0005 is still in transport spike); comedia latching is the minimal, vendor-neutral fix for the SIP-trunk-behind-NAT case and composes with a future ICE transport. |
