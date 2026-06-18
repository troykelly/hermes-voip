# ADR-0049: Outbound WebRTC origination over WSS + Opus on the SIP path

- **Date:** 2026-06-18
- **Status:** Accepted (lifts ADR-0032 §5 + ADR-0038 §4 deferrals)
- **Deciders:** agent session (outbound-WebRTC/Opus lane) — operator-directed

## Context

Two deferrals blocked a fully symmetric codec/transport surface:

1. **Outbound WebRTC origination over WSS was deferred** (ADR-0032 §5, ADR-0038
   §4). On a `HERMES_SIP_TRANSPORT=wss` gateway, `place_call` rejected the call
   loudly with `OutboundCallFailed(501, …)` *before* any INVITE — because the
   outbound UAC path only knew how to offer the SDES/G.711-G.722 menu with a
   `TLS` Via, which would be spec-incoherent SIP over a WebSocket. Inbound WebRTC
   (offer→answer as the UAS) was already wired end-to-end (ADR-0032); only the
   UAC-with-our-own-offer direction was missing.

2. **Opus on the SIP (SDES/TLS) path was deferred** (ADR-0032 alternatives).
   Opus was advertised *only* on the WebRTC m-line it was added for, even though
   the engine can carry Opus on either negotiation surface — so a SIP-over-TLS
   call (inbound or outbound) could not negotiate Opus.

The building blocks already existed and are transport-agnostic: the
`WebRtcMediaSession` orchestrator (ICE gather → DTLS-SRTP keying), the
`UDP/TLS/RTP/SAVPF` SDP body builder (`_build_webrtc_body`, used by
`build_webrtc_answer`), the `WssSipTransport` (RFC 7118 framing + `.invalid`
sent-by + `transport=ws` Contact), and `Codec.OPUS` in the engine. This lane wires
them into the outbound direction and widens the SIP codec menu.

## Decision

### 1. Outbound WebRTC origination over WSS (lifts ADR-0032 §5 / ADR-0038 §4)

`place_call` on a `wss` gateway no longer returns 501. It now drives a UAC flow
that carries **our own** DTLS/ICE/Opus offer:

- A new `build_webrtc_offer()` in `sdp.py` emits a `UDP/TLS/RTP/SAVPF` offer body
  (shared `_build_webrtc_body` with `build_webrtc_answer`) carrying our
  `a=fingerprint`, `a=setup`, `a=ice-ufrag`/`a=ice-pwd`/`a=candidate`,
  `a=rtcp-mux`, and our codec menu. The menu (`_webrtc_offer_codecs()`) mirrors the
  inbound answer menu (`_WEBRTC_SUPPORTED_ENCODINGS` = opus, PCMU, PCMA,
  telephone-event): the Opus rtpmap (`opus/48000/2` + `minptime=10;useinbandfec=1`)
  first, then the G.711 fallbacks, then `telephone-event` — so RFC 4733 DTMF can
  negotiate (an Opus-only offer makes the negotiated telephone-event PT structurally
  `None`, i.e. DTMF impossible) and a non-Opus gateway can still answer G.711. The
  2xx answer is bounded to exactly these offered encodings (RFC 3264 §6), so a
  gateway echoing a codec we never offered is rejected, not silently accepted.
- `WebRtcMediaSession.for_outbound_offer()` constructs the session as the
  **offerer**: `ice_controlling=True` (we are ICE-CONTROLLING on outbound, RFC
  8445 §6.1) and `a=setup:active` — we are the **DTLS CLIENT** that sends the
  ClientHello. Offering a concrete `active` (rather than `actpass`) is chosen
  deliberately: the `DtlsEndpoint`'s ephemeral certificate — and therefore the
  `a=fingerprint` we put in the offer — is fixed at construction, so the DTLS role
  must be known before the offer is sent. `active` is RFC 5763 §5 compliant for an
  offerer and needs no post-answer role switch (the answerer answers `passive`).
  The role-agnostic `run_handshake()` runs the same ICE-connect + DTLS-pump as the
  answerer path; being ICE-controlling and DTLS-client is the only difference.
- The INVITE goes out over the `WssSipTransport` with a `WSS` Via and the
  transport's `.invalid` sent-by + `transport=ws` Contact (already produced by
  `build_outbound_invite(transport="WSS", …)` + `WssSipTransport`), so the SIP on
  the WebSocket is RFC 7118 coherent. The 200 OK SDP answer is parsed, the peer's
  fingerprint/ICE creds/candidates feed `run_handshake()`, and the engine
  runs over the ICE pipe (the same `ice_transport` seam as inbound).
- A `wss` gateway with no Opus runtime (`opuslib`/system `libopus`) is rejected
  cleanly **before** the INVITE (`OutboundCallFailed(488, …)` via the existing
  `ensure_opus_available` preflight): WebRTC mandates DTLS-SRTP + a real codec, and
  Opus is the WebRTC audio codec — never an answered-then-dead call.

### 2. Opus on the SIP path (lifts ADR-0032 alternatives)

Opus is added to the SDES/SIP offer + answer menus, **gated on runtime
availability**:

- `_outbound_offer_codecs()` (the outbound SIP INVITE menu) and the inbound SIP
  answer's supported list prepend the Opus rtpmap **only when
  `ensure_opus_available()` succeeds** (the `webrtc` extra + system `libopus` are
  present). A host without libopus advertises exactly the prior G.722/G.711 menu —
  so the #84 *advertise-without-carry* invariant holds: we never offer Opus we
  cannot encode.
- When Opus is the negotiated SIP codec, the outbound path preflights the Opus
  dependency before wiring the engine (defense-in-depth mirror of the WebRTC
  preflight), and `_to_engine_codec`/`codec_for_encoding` map `opus/48000` →
  `Codec.OPUS` exactly as on the WebRTC path. SDES keying + the engine pipeline are
  unchanged; only the codec menu widens.

### 3. The existing TLS-outbound and all inbound paths are unaffected

The `tls` outbound path keeps its `TLS` Via and SDES menu; inbound SDES and
inbound WebRTC are byte-unchanged except for the (availability-gated) Opus entry in
the SIP answer menu. The branch in `place_call` is `transport == "wss"` → outbound
WebRTC, else the existing SDES/TLS flow.

## Consequences

- A `wss` extension can now **originate** a WebRTC call (our DTLS/ICE/Opus offer),
  not only receive one — the outbound mirror of the inbound WebRTC path.
- Opus becomes negotiable on a SIP-over-TLS call when libopus is installed, at the
  48 kHz pipeline rate (STT/TTS follow the codec, as G.722 established). Without
  libopus, nothing changes.
- The outbound WebRTC path commits to `a=setup:active` (DTLS client). An
  *active-answerer* (a gateway that itself offers `passive`, forcing us to answer
  `active` on the inbound path) is a separate concern tracked as ADR-0050; this
  lane is offerer-only on the active side.
- `libopus` stays a `webrtc`-extra-only runtime dependency; the default install,
  licence gate, and base mypy gate remain free of opuslib/pyOpenSSL/aioice
  (all webrtc imports stay lazy).

## Scope / deferrals (rule 6 — named, not silent)

- **DTLS active-answerer (RFC 8842)** — answering a gateway's `passive` offer as
  the DTLS `active` party — stays deferred to ADR-0050. The outbound offerer here
  always offers `active`.
- **WebRTC video** stays deferred per ADR-0018/0044 (audio-only offer).
- **Trickle-receive (SIP-INFO, RFC 8840)** stays deferred per ADR-0034: the
  outbound offer, like the answer, carries a full candidate set + end-of-candidates
  (half-trickle, RFC 8838-interoperable).
- **Live validation** against the real gateway is the operator step (it needs the
  WSS endpoint + credential + a dual-stack UDP host per ADR-0043); this lane lands
  the wiring + in-process (loopback-ICE / in-memory-DTLS) evidence and does not
  touch the live gateway.

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| Offer `a=setup:actpass` on the outbound WebRTC offer | The `DtlsEndpoint` cert/fingerprint is fixed at construction; an actpass offer that resolves to `passive` would force a role switch that regenerates the cert and invalidates the already-sent `a=fingerprint`. A concrete `active` offer is RFC 5763 §5 compliant and needs no switch. |
| Advertise Opus on the SIP menu unconditionally | A host without `libopus` would offer a codec it cannot encode (the #84 advertise-without-carry defect → answered-but-dead audio). Gating on `ensure_opus_available()` keeps the invariant. |
| A separate outbound `WebRtcMediaSession` class | Would duplicate the ICE/DTLS-pump machinery; an offerer factory + an offerer handshake method on the one class is the narrow diff (the only deltas are `ice_controlling` and the DTLS role). |
| Keep the 501 reject and ship outbound WebRTC later | The deferral was explicitly a "real boundary, not a stub" (ADR-0038 §4) precisely so this lane could lift it; the blocker (WSS signalling) is wired (ADR-0038), so the boundary is now buildable. |
