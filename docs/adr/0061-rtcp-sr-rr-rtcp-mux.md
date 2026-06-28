# ADR-0061: RTCP (SR/RR/SDES/BYE), reception statistics, and rtcp-mux

- **Date:** 2026-06-19
- **Status:** Accepted
- **Deciders:** launch-readiness (RTCP promoted CORE) — agent session (RTCP lane)

## Context

The media plane (ADR-0005) carried RTP audio but had **no RTCP** (RFC 3550 §6) — the
control channel every RFC-compliant endpoint uses to report what it sent and what it
received (loss, jitter, round-trip time) and to name its source (SDES CNAME). Three
concrete gaps forced the decision:

1. **No quality signal.** SLO runbook `0014-voip-slo-metrics.md` lists RTP packet loss,
   jitter, packets sent/received, and one-way-audio detection as **NOT YET
   INSTRUMENTED** — there was no in-process source for any of them. ADR-0056 added
   packet-loss concealment that already tracks a per-call loss count and explicitly left
   "a future RTCP lane can read this loss count" as the seam. RTCP is that lane.
2. **Gateways expect RTCP.** Many gateways treat a total absence of RTCP as a dead/!
   one-way media path and tear the call down (or never report quality back). Sending SR/RR
   keeps the call healthy and gives the far end our reception view.
3. **rtcp-mux half-done.** `sdp.py` already *parsed* `a=rtcp-mux` (RFC 5761) into
   `AudioMedia.rtcp_mux`, and the WebRTC/video answer bodies already emit it
   unconditionally, but the SDES / plain-RTP (SIP-over-TLS) audio path neither emitted nor
   negotiated it — so a SIP/SDES call had no defined RTCP port behaviour at all.

Constraints (AGENTS.md): fully-typed, `mypy --strict`, no escape hatches (17); TDD with
byte-level vectors (18); errors propagate (37); deterministic + unit-testable with no
threads/sockets/wall-clock (the engine's existing injectable-clock discipline); minimal
diffs (28); the live transport/socket wiring is an **adapter** concern, not the engine's
(ADR-0032/0056 boundary).

## Decision

Add a sans-IO RTCP packet layer, wire it into the media engine as a deterministic
send/receive capability, and complete rtcp-mux negotiation on the SDES path. The engine
**builds and runs** RTCP through injectable seams; **choosing the RTCP socket/mux and
starting the loop on the live transport is the adapter's job** (named below).

### 1. RTCP packet layer — `src/hermes_voip/rtcp.py` (new)

Sans-IO build + parse for Sender Report (SR), Receiver Report (RR), SDES (CNAME), and BYE,
plus compound packets (RFC 3550 §6.1):

- Validated, frozen dataclasses (`ReportBlock`, `SenderReport`, `ReceiverReport`,
  `SdesChunk`/`SourceDescription`, `Bye`) with range-checked fields (`cumulative_lost` as
  signed 24-bit two's complement, RC/SC/length computed and verified on parse). Parse
  errors raise `RtcpError` (rule 37).
- `build_compound` / `parse_compound`: the §6.1 sequence (lead with SR/RR, then SDES);
  `parse_compound` traverses by the length field and **skips unknown packet types**
  (e.g. APP=204) rather than choking, as a receiver must.
- Statistics maths: `ReceptionStats` (per-source sequence/cycle tracking A.1, fraction +
  cumulative loss over a report interval A.3, smoothed interarrival jitter in clock units
  A.8 — driven by injected `(seq, rtp_ts, arrival)`, no clock; a read-only `snapshot()`
  that does NOT roll the interval); `compute_rtcp_interval` (the §6.2 ~5% bandwidth rule
  with the 25/75 sender/receiver split, 5 s floor, §6.3.1 randomisation, injectable RNG);
  `rtt_from_report_block` + NTP helpers (`to_ntp`/`from_ntp`/`compact_ntp_now`).

### 2. rtcp-mux negotiation — `src/hermes_voip/sdp.py`

`negotiate_rtcp_mux(offer: AudioMedia) -> bool` (RFC 5761 §5.1.1: mux **iff** the offer
requested it). `_build_audio_body` gains an `rtcp_mux` flag; `build_audio_offer` offers
`a=rtcp-mux` **by default** (suppressible via `rtcp_mux=False`); `build_audio_answer`
**mirrors** the offer. The WebRTC/video paths are unchanged (they already mux
unconditionally per RFC 8829/8843).

### 3. Engine integration — `src/hermes_voip/media/engine.py`

`RtpMediaTransport` gains:

- New constructor params, all defaulted (existing call sites unaffected): `cname` (SDES
  CNAME, adapter sets a per-call value, NOT PII), `rtcp_send` (a `Callable[[bytes], None]`
  RTCP sink — `None` ⇒ **mux over the RTP transport**), `ntp_clock` (Unix-seconds wallclock
  for SR NTP + LSR/DLSR, default `time.time`, injectable), `rtcp_bandwidth`.
- `build_rtcp_report() -> bytes | None`: a compound SR (we have sent media) or RR
  (receive-only) + SDES; `None` before any media.
- `ingest_rtcp(data)`: parses an inbound compound; records a peer SR's LSR for our next
  block, derives RTT from a peer block about our SSRC, surfaces the far-end loss/jitter.
- A per-packet tap in `_inbound_gen` feeds `ReceptionStats`; `_transmit_frame` counts sent
  packets/payload-octets for the SR.
- `run_rtcp(...)`: a deterministic periodic sender on the §6.2 interval; muxes over the RTP
  transport by default, takes an injected sink for the separate-port case, flushes a BYE on
  stop. `stop()` cancels a registered loop task.
- `call_quality -> CallQuality`: local (what we received) + remote (what the peer reported
  about us) loss/jitter + RTT — the SLO numbers (runbook 0014).

### Adapter activation (the live wiring — NOT in this lane)

The engine ships the capability dormant. To turn RTCP live the **adapter** (a separate
lane) must, for each `RtpMediaTransport` it constructs:

1. **Choose the RTCP transport from the negotiated SDP.** Call
   `sdp.negotiate_rtcp_mux(offer.audio)` (SDES path) — or rely on the always-muxed WebRTC
   path. If muxed: leave `rtcp_send=None` (RTCP goes over the RTP socket/ICE pipe). If NOT
   muxed: open the RTCP socket on **RTP port + 1** (RFC 3550 §11) and pass
   `rtcp_send=lambda d: rtcp_sock.sendto(d, (remote_host, remote_rtcp_port))`.
2. **Pass a per-call CNAME** (e.g. a random opaque token — NOT the SIP host/extension):
   `RtpMediaTransport(..., cname=<token>)`.
3. **Start the loop after media is up**, keeping the task on the engine so `stop()` cancels
   it: `engine._rtcp_task = asyncio.create_task(engine.run_rtcp(send_bye_on_stop=True))`
   (the engine exposes `run_rtcp`; a thin public `start_rtcp()` setter can be added when the
   adapter lands if direct attribute set is undesirable).
4. **Feed inbound RTCP.** On the muxed path, demux inbound datagrams by the RFC 5761 §4
   second-byte rule (packet type 200–204 ⇒ RTCP) and call `engine.ingest_rtcp(datagram)`;
   on the separate-port path, pump the RTCP socket's datagrams into `ingest_rtcp`. (The RFC
   7983 first-byte demux on the WebRTC ICE pipe already lets SRTCP through; the second-byte
   split selects RTP vs RTCP after SRTP unprotect — to be added in the adapter's inbound
   reader.)
5. **Read quality** wherever the SLO/metrics consumer lives: `engine.call_quality`.

This mirrors ADR-0056's ptime boundary (`engine.ptime = negotiate_ptime(...)` is likewise
the adapter's call), so both land in the same launch push.

### Adapter activation as built (refinement, 2026-06-19)

The activation lane shipped the above with two refinements to what is written above:

1. **The engine owns the RTCP socket via `start_rtcp`, not the adapter.** Because the engine
   already owns the RTP socket, its OS-assigned port, the inbound reader, and `stop()`
   teardown, the live wiring is a single `await engine.start_rtcp(mux=…, remote_rtcp_addr=…)`
   the adapter calls after `connect()`. `start_rtcp` sets the inbound muxed-demux flag,
   registers the `run_rtcp` task on the engine (so `stop()` cancels + awaits it and flushes
   the BYE), and — on the non-muxed path — binds the sibling RTCP socket on RTP-port+1 and
   starts a reader that pumps it into `ingest_rtcp`. The muxed inbound demux (RFC 5761 §4
   second byte 200–204) lives in the engine's `_inbound_gen` (the only place raw datagrams
   are read), gated on the activation flag so a non-activated engine is byte-for-byte
   unchanged. `engine.call_quality` is logged at adapter teardown (runbook 0014).

2. **At first, RTCP was activated ONLY on the cleartext plain-RTP path; secured paths were
   NOT** (because `media/srtp.py` was SRTP-only and the engine emitted/parsed CLEARTEXT RTCP,
   which on an RTP/SAVP or SAVPF 5-tuple would violate the profile and leak SSRC/CNAME/timing
   in cleartext — worse than no RTCP). This was an explicitly-named, bounded limitation: RTCP
   stayed dormant on encrypted calls (including the SDES-SRTP live test gateway) "until SRTCP
   lands". **See the secured-activation refinement below — SRTCP has now landed.**

### Secured-path RTCP activation via SRTCP (refinement, 2026-06-20, ADR-0066)

The dormancy of point 2 is **resolved**: SRTCP (RFC 3711 §3.4) shipped as `media/srtcp.py`
(ADR-0066, PR #152), and this lane wires it into the engine + adapter so RTCP **activates**
on every secured path (SDES, SIP-DTLS, WebRTC) instead of staying dormant.

- **Engine seam.** `RtpMediaTransport` gained narrow `_SrtcpProtect`/`_SrtcpUnprotect`
  Protocols and `srtcp_inbound`/`srtcp_outbound` fields. `_emit_rtcp` wraps every outbound
  compound RTCP in `_srtcp_out.protect`; `_ingest_rtcp_datagram` unwraps every inbound one
  with `_srtcp_in.unprotect` (an `SrtcpError` — auth/replay/format — drops the datagram, the
  call continues). The secured-transport guard in `start_rtcp` **flips**: a secured engine
  now activates RTCP when SRTCP is wired (`_has_srtcp` = both sessions set) and stays dormant
  only when secured **without** SRTCP. The inbound muxed demux (RFC 5761 §4 second byte
  200–204, in the clear even under SRTCP per §3.4) now recognises secured RTCP.
- **Keying.** SDES SRTCP is keyed from the SAME negotiated `a=crypto` master key||salt as
  SRTP (offerer's key inbound, our answer key outbound) — only the §4.3.2 KDF labels differ
  (0x03/0x04/0x05 vs 0x00/0x01/0x02), so the keystreams never collide. DTLS/WebRTC SRTCP is
  derived from the SAME RFC 5764 export as SRTP via a new `DtlsEndpoint.derive_srtcp_sessions`
  (proxied by both session wrappers).
- **Adapter.** `_setup_sdes_call` builds the SRTCP pair and activates RTCP via a new
  `_plan_secured_rtcp_activation` (mux mirrors the offer, kill-switch only, no profile gate;
  SDES can use the non-muxed sibling port because the engine owns a UDP socket).
  `_setup_webrtc_call` and `_setup_sip_dtls_call` derive SRTCP from the handshake and activate
  RTCP **muxed** (a single ICE/UDP pipe has no second socket) via `_activate_muxed_srtcp_rtcp`.
- The operator kill-switch is still `HERMES_VOIP_RTCP_ENABLED` (default on); it now suppresses
  RTCP on secured calls too.

### Secured-path RTCP is OPT-IN, default off (live finding, 2026-06-21)

The "activate on every secured path" posture of the 2026-06-20 refinement broke a **real
call**. On a live inbound SDES (RTP/SAVP) call to a UCM-class gateway that did **not** negotiate
`a=rtcp-mux`, `_plan_secured_rtcp_activation` (then gated by the kill-switch only) activated
RTCP, so the engine opened the sibling SRTCP socket on RTP-port+1 and emitted SRTCP on the
wire. The gateway **muted the media session** in response — no two-way audio. Setting
`HERMES_VOIP_RTCP_ENABLED=false` restored audio, confirming the activation as the cause.
(Engine-isolation probes showed the RTP datapath itself survives RTCP activation on loopback;
the break is the wire interaction with a strict real gateway, which localhost cannot
reproduce.)

**Decision:** secured-path RTCP is now **opt-in, default off**, mirroring the cleartext
planner's fail-closed posture. A new `MediaConfig.secured_rtcp_enabled`
(env `HERMES_VOIP_SECURED_RTCP_ENABLED`, default `False`) gates
`_plan_secured_rtcp_activation`: it returns `None` (RTCP dormant, no sibling socket, no SRTCP
on the wire) unless the flag is true **and** the master `rtcp_enabled` kill-switch is true.
So by default a secured call has exactly the pre-#160 behaviour (audio works); the SRTCP
capability is **retained behind the flag** for a gateway-validated rollout. This corrects the
earlier "kill-switch only, no profile gate" description of the SDES secured planner. The
muxed DTLS/WebRTC path (`_activate_muxed_srtcp_rtcp`, single ICE/UDP pipe, no sibling socket)
is unchanged and still governed by the master kill-switch only — the non-mux sibling-socket
hazard does not arise there. **Follow-up (integration-seam gap):** there is no end-to-end
real-socket test of a secured inbound call (the e2e fake gateway sends only cleartext RTP),
so the opt-in path's wire behaviour against a strict gateway remains validated only by hand.

## Consequences

- The SLO catalogue's packet-loss / jitter / RTT / packets-sent-received signals now have a
  real in-process source (runbook 0014 updated). One-way-audio is inferable (we send RTCP
  but the peer's reports show zero received, or vice-versa).
- We are now an RFC 3550 §6 RTCP participant: a gateway that gated on RTCP presence is
  satisfied, and the far end gets our reception view (and a prompt BYE on hangup).
- The engine carries a small amount of per-call RTCP state (one `ReceptionStats` per remote
  source, a few timestamps). Cost is negligible: one compound packet every ~5 s (the §6.2
  floor for a 2-party call), built from O(sources) report blocks. No hot-path change — the
  inbound tap is one dict lookup + counter update per packet (rule 22).
- **Maintenance commitment:** the byte-level KAT vectors pin the wire format; any future
  RTCP profile addition (XR, feedback RFC 4585) extends `parse_compound`'s skip-unknown
  traversal without breaking it.
- **The live path is wired in the adapter** (named above): RTCP is activated on the cleartext
  plain-RTP path, and — since the SRTCP refinement (ADR-0066) — on every secured path (SDES,
  SIP-DTLS, WebRTC) too, so the SLO signals are populated on encrypted calls (including the
  SDES-SRTP live test gateway), not only cleartext ones.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Use a library (e.g. `aiortc`'s RTCP) | Adds a heavy dependency for ~400 lines of well-specified packing; the engine is deliberately sans-IO + injectable for determinism, which a library's own I/O/loop fights. ADR-0032 already declares the no-unneeded-dependency posture. |
| Skip SR/RR, send only SDES/BYE | Loses the entire point — loss/jitter/RTT come from SR/RR report blocks; SDES/BYE alone give no quality signal. |
| Start `run_rtcp` automatically in `connect()` | Violates the adapter boundary (ADR-0032/0056): the engine cannot choose the RTCP socket/mux (that needs the negotiated SDP the adapter holds), and auto-starting would send RTCP to the wrong port on a non-muxed call. The adapter starts it with the right sink. |
| Always mux (ignore the offer on the SDES path) | RFC 5761 §5.1.1 forbids assuming mux the peer did not offer — our RTCP would hit a port it is not listening on. We offer mux and mirror the answer. |
| Compute jitter in milliseconds | RFC 3550 §6.4.1 fixes the report-block jitter unit as the source RTP clock; converting to ms only at the `call_quality` boundary keeps the wire correct and the SLO view human-readable. |
