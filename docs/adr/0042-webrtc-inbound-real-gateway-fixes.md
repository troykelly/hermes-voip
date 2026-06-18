# ADR-0042: Inbound WebRTC against a real Asterisk/UCM gateway — SDP + keepalive fixes

- **Date:** 2026-06-18
- **Status:** Accepted (amends ADR-0032 §SDP parsing, ADR-0038 §3; precedes a
  follow-up IPv6-first-ICE ADR)
- **Deciders:** agent session (WebRTC live-validation lane) — operator-directed

## Context

ADR-0032 wired the WebRTC media plane (ICE + DTLS-SRTP + Opus) and ADR-0038
selected the WSS signalling transport. Both passed their unit/e2e suites against
**our own fixtures**. The first **live** inbound WebRTC call from the test
gateway (a Grandstream UCM6304 whose WebRTC/WAVE edge is an embedded
**Asterisk**) exposed three gaps the fixtures did not model — the
unit-test-vs-real-gateway seam the operator had flagged. Each was captured on the
wire (secrets redacted) and fixed under TDD.

### 1. DTLS/ICE credentials arrive at the SDP **session** level (BUNDLE)

The real offer is a BUNDLE group (`a=group:BUNDLE 0 1 2`) with **one** audio
`m=` line (Opus-first: `opus G722 PCMU PCMA …`) plus two H.264 video `m=` lines,
all `UDP/TLS/RTP/SAVPF`. Crucially, `a=ice-ufrag` / `a=ice-pwd` /
`a=ice-options:trickle` / `a=fingerprint` / `a=setup:actpass` appear **before the
first `m=` line** (session level), shared across the bundled media — exactly what
RFC 8122 §5 (fingerprint) and RFC 8839 §4.2 (ICE credentials) permit.

`SessionDescription.parse` captured `a=` attributes **only inside the audio
`m=` block**, so it dropped every session-level credential →
`audio.fingerprint`/`ice_ufrag`/`ice_pwd` were `None` → `_setup_webrtc_call`
rejected a valid DTLS-SRTP offer with `488 "missing fingerprint/ICE"`. (Our
WebRTC fixtures put these at media level, so the suite never caught it.)

### 2. The WSS channel carries bare CRLF keepalive frames

Asterisk sends RFC 5626 §4.4 / RFC 7118 **CRLF keepalive** frames (a double-CRLF
ping, a single-CRLF pong) — and at least one empty text frame — over the WSS
signalling socket. The WSS reader fed **every** text frame straight to
`SipRequest.parse`, which raised `not a SIP request-line: ''` on the empty
request-line, ended the reader task, and dropped the registration. Inbound calls
then routed to **voicemail** until the next re-REGISTER. (The TLS transport never
hit this: its `SipMessageFramer._skip_keepalive_crlf` already drops inter-message
CRLFs; the per-frame WSS path had no equivalent.)

### 3. The WSS/WAVE edge authenticates with the **SIP** password, not a separate one

ADR-0038 §3 assumed the gateway's Secure-WebSocket edge uses a *different* digest
password than the SIP-TLS edge, and added an optional `HERMES_SIP_WS_PASSWORD`
override on that premise. A live RFC 7118 REGISTER credential matrix disproved it
for this gateway: the WSS edge (port 8090, path `/ws`, subprotocol `sip`, realm
`voip002`, MD5 `qop=auth`) returns **`200 OK`** with the **SIP-TLS digest
password** and **`401`** with the gateway's other ("portal/WAVE-app login")
password. So the WSS edge **shares** the SIP credential here, and the documented
fallback (`HERMES_SIP_WS_PASSWORD` unset → reuse `HERMES_SIP_PASSWORD`) is the
correct zero-config path.

## Decision

1. **Parse session-level DTLS/ICE attributes and inherit them onto the media**
   (media level overrides session — RFC 8122 §5 / RFC 8839 §4.2). `parse()` feeds
   the `a=` lines before the first `m=` into a dedicated accumulator;
   `_AudioAccumulator.build()` fills any unset `fingerprint`/`setup`/`ice_ufrag`/
   `ice_pwd`/`ice_options` from it. Candidates stay per-media (not inherited).
2. **Absorb CRLF keepalive frames in the WSS reader.** `_dispatch` treats any
   whitespace-only frame as a keepalive — never SIP — and answers a double-CRLF
   ping with a single-CRLF pong (`_send_raw`, bypassing the SIP-parsing send
   path). The connection and registration survive.
3. **Correct the ADR-0038 credential premise.** `HERMES_SIP_WS_PASSWORD` stays as
   a valid **optional** override for gateways that genuinely differ, but it is
   **not** required for this gateway; the runbook documents that the WSS edge
   shares the SIP password and that the item's top-level password is the GDMS/WAVE
   portal login (not a SIP credential). Runbook 0002's "portal password (unused)"
   annotation is corrected accordingly.

All three are gateway-agnostic standards behaviour (RFC 8122/8839/5763 session
level; RFC 5626/7118 keepalive; RFC 7118 digest), so no vendor quirk enters core.

## Live evidence (this lane)

Proven against the real gateway, in order: WSS `REGISTER → 200 OK` (expires
~299 s) with the SIP password; inbound INVITE classified to the operator group;
**WebRTC SDP answer built** (`setup=passive`, Opus) — i.e. session-level
fingerprint/ICE now parsed; **`200 OK` sent**; **ICE connectivity check
SUCCEEDED**. The call reaches ICE; see the open item for why media does not yet
complete from the test environment.

## Open / deferred (rule 6 — named, not stubbed)

- **Inbound media does not yet complete from the devcontainer.** The container is
  double-NAT'd (Docker on the operator's Mac, Mac on the office LAN) with only a
  private IPv4 (`172.x`) and a **ULA** IPv6; a public-STUN srflx returns the
  office's hairpin-NAT IPv4 the gateway cannot reach, so the controlling gateway
  never nominates an ICE pair and DTLS never starts. This is an **environment**
  limit, not a plugin defect. The viable path is **IPv6-first** media: the
  container reaches the gateway's global IPv6 outbound (Docker NAT66) and the host
  shares the gateway's IPv6 `/48`. Per the operator's standing **IPv6-first,
  IPv4-fallback** directive, a follow-up ADR/lane will make ICE gather and
  prioritise IPv6 (and resolve STUN/TURN over IPv6) so the answer advertises a
  gateway-reachable address; TURN remains the fallback for IPv4-only paths.
- **Outbound WebRTC origination over WSS** stays deferred (ADR-0038 §4 / task #32).

## Consequences

- A real BUNDLE WebRTC offer is now accepted (no spurious 488); the WSS
  registration is stable under gateway keepalives (no voicemail flapping).
- `HERMES_SIP_WS_PASSWORD` is demoted from "expected" to "optional override".
- New seam discipline for fixtures: WebRTC SDP fixtures must include a
  session-level-BUNDLE variant so this class of bug cannot regress silently.
- No new dependency, no licence/advisory change.

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| Treat a SAVPF offer without media-level fingerprint as malformed (keep 488) | The offer is valid per RFC 8122/8839; the defect was ours. |
| Inherit session-level ICE **candidates** too | Candidates are per-media; a BUNDLE offer carries them on the first m-line, which we already parse. Inheriting would duplicate/confuse them. |
| Answer a double-CRLF ping by ignoring it (skip only, no pong) | Matches the TLS skip but risks the gateway marking the contact dead; a single-CRLF pong is the RFC 5626 §4.4 reply and costs nothing. |
| Drop `HERMES_SIP_WS_PASSWORD` entirely | Some gateways genuinely separate the WSS credential; the override stays as an optional, documented knob (just not required here). |
| Force the answerer DTLS role to `active` now | Not yet evidenced as the blocker — ICE never completed from the test env, so DTLS never ran. Deferred to the IPv6-first lane where the role can be observed end-to-end. |
