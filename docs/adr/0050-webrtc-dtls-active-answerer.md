# ADR-0050: WebRTC DTLS active-answerer (RFC 8842)

- **Date:** 2026-06-18
- **Status:** Accepted (amends ADR-0032 §DTLS role selection)
- **Deciders:** agent session (WebRTC DTLS active-answerer lane)

## Context

When the plugin answers an inbound WebRTC (`UDP/TLS/RTP/SAVPF`) INVITE it must
choose its DTLS role from the offer's `a=setup` attribute and carry that role in the
SDP answer's own `a=setup`. The DTLS *client* sends the `ClientHello` and drives the
handshake; the *server* waits for it. If both ends pick the server role, neither
sends a `ClientHello` and the handshake **deadlocks** until the call setup times out.

ADR-0032 shipped `answer_setup_for_offer` following RFC 5763 §5's letter literally:
an `actpass` **or** `active` offer made us `passive` (the DTLS server); only a
`passive` offer made us `active`. That is safe against a strict offerer, but a real
Asterisk / UCM-class gateway offers `a=setup:actpass` while **behaving as the DTLS
server** — it expects the answerer to be the client. Our `passive` answer then leaves
**both** ends waiting (gateway-server + us-server), so the handshake never starts and
the live call fails to key SRTP.

RFC 8842 §5.3 resolves exactly this: for an `actpass` offer the **answerer SHOULD be
`active`** (the DTLS client). This is the modern, interoperable default and matches
how browsers and SIP gateways behave in practice.

## Decision

1. **Active answerer by default (RFC 8842 §5.3).** `answer_setup_for_offer` now maps
   an `actpass` offer (or a missing `a=setup`, which RFC 5763 §5 treats as `actpass`)
   to **`active`** — we become the DTLS client and send the `ClientHello`. The pinned
   cases are unchanged and still **override** any preference: an `active` offer is
   answered `passive`, a `passive` offer is answered `active` (RFC 5763 §5). Forcing
   can never create two clients or two servers.
2. **Operator knob `HERMES_VOIP_WEBRTC_DTLS_SETUP` ∈ {`auto`,`active`,`passive`}**,
   default `auto`. It is threaded `MediaConfig.webrtc_dtls_setup →
   WebRtcMediaSession(answer_setup=…) → answer_setup_for_offer(forced=…)`. `auto`
   and `active` both yield the RFC-8842 active answerer for an `actpass` offer;
   `passive` forces the **server** role for the rare gateway that insists on being the
   DTLS client. The knob applies **only** to an `actpass` offer — a pinned
   `active`/`passive` offer always dictates the complementary role regardless of the
   knob (the forced-vs-offer compatibility rule), so the knob can never deadlock a
   conformant peer. Unknown values are rejected at config load (rule 27 — no inert
   knob). The knob has no effect on the SIP-over-TLS (SDES) path.

The DTLS role drives `DtlsEndpoint(role=CLIENT|SERVER)` exactly as before; only the
`actpass` → role mapping changed, plus the new knob.

## Scope / deferred (rule 6)

- **SIP-over-TLS / SDES media** is unaffected — SDES has no DTLS role to negotiate.
- **Live two-way-audio validation** against the real gateway must run where UDP
  actually reaches it (ADR-0043: the devcontainer's NAT66 drops UDP). This lane lands
  the code + unit/e2e evidence (a real in-process DTLS handshake with the adapter as
  the active client); the live leg remains the operator step ADR-0043 already names.
- **Outbound WebRTC** (where the plugin is the *offerer* and would itself offer
  `actpass`) is a separate lane (ADR-0049); this ADR governs only the inbound
  answerer.

## Consequences

- Against an Asterisk/UCM-class gateway that offers `actpass` and acts as the DTLS
  server, the handshake now starts (we send the `ClientHello`) instead of
  dead-locking — the concrete live-gateway-confidence win this lane targets.
- The e2e fake gateway peer is now the DTLS **server** (it offers `actpass`, so the
  adapter answers `active`); the in-process handshake still completes both ways. This
  is a test-infra change, not a weakened assertion.
- The `test_answer_setup_for_offer_*` mapping tests changed to the new (correct)
  expectation — a justified test change (rule 19), committed red on its own.
- No new dependency; no licence/advisory change.

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| Keep the RFC 5763 literal `actpass → passive` mapping | Deadlocks against the common gateway that offers `actpass` but acts as the DTLS server — the live failure this lane fixes. |
| Always force `active`, no knob | A minority of gateways insist on being the client; an undefaultable forced role would strand them. The `auto`/`active`/`passive` knob keeps the safe default while leaving an escape hatch. |
| Let the knob override a pinned `active`/`passive` offer | Would create two clients or two servers and guarantee a deadlock; the knob must never override a peer that pinned its role. |
| Detect the gateway by vendor and branch | Vendor-specific quirks in the core are banned (CLAUDE.md gateway-agnostic invariant); RFC 8842's default is the standards-based fix. |
