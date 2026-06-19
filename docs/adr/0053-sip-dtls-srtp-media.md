# ADR-0053: Opportunistic SRTP on the SIP-over-TLS media path — wire SDES (RFC 4568), add DTLS-SRTP (RFC 5763/5764, no ICE)

- **Date:** 2026-06-18 (Stage 2 built 2026-06-19)
- **Status:** Accepted — **Stage 1 SDES merged + live-validated (PR #132); Stage 2
  DTLS-SRTP capability built (this lane)**, pending its adapter-activation wave (see
  "Stage 2 build status" below)
- **Deciders:** operator direction ("require SRTP with real certs for our SIP over
  TLS"; clarified 2026-06-18 to mean cert-keyed **DTLS-SRTP** media, enforced
  **opportunistically**) — agent session (SIP DTLS-SRTP lane)

## Context

The SIP-over-TLS transport already secures **signalling** — its TLS verifies the
gateway certificate against real CAs (`ssl.create_default_context()` →
`CERT_REQUIRED` + `check_hostname=True`; ADR-0038) — and the codebase **does
support SRTP**: SDES (RFC 4568 `a=crypto`) is fully implemented in the SDP + SRTP
layer (`sdp._negotiate_answer_crypto` builds an SDES answer when given a key;
`media/srtp.SrtpSession` runs the RFC 3711 transform; ADR-0013) and is unit-tested.

The gap is **adapter wiring**, not capability. The inbound INVITE answer
(`_setup_sdes_call` → `build_audio_answer`) and the outbound INVITE offer both omit
the `crypto=` key, so today an inbound `RTP/SAVP` offer is rejected with **488**
(`_negotiate_answer_crypto` raises "cannot answer an RTP/SAVP offer without a
crypto key" — verified by repro) and the live + e2e SIP calls run **plain
RTP/AVP**. The `_setup_sdes_call` docstring claims an SDES answer "with our key"
the code never supplies — a rule-27 gap to close. So SRTP is *supported* but not
*negotiated end-to-end* on the SIP media path.

The operator requires SRTP "with real certs", and chose **DTLS-SRTP** (RFC
5763/5764) over keeping SDES or adding mutual-TLS client certs. DTLS-SRTP derives
the SRTP keys from a DTLS handshake authenticated by an X.509 certificate whose
SHA-256 fingerprint is bound into the (TLS-protected) SDP — the "real cert" is the
DTLS media certificate (RFC 5763 §3) — and is **stronger than SDES** because the
master key never travels inline in the SDP. This lane makes DTLS-SRTP the
**preferred** SIP media security and, so the *opportunistic* ladder is real rather
than a 488, also **wires the existing-but-dormant SDES** as the middle tier.

The WebRTC lane (ADR-0016) already built every cryptographic primitive this needs:
`media/dtls.py` `DtlsEndpoint` (transport-agnostic memory-BIO DTLS, RFC 5705
`EXTRACTOR-dtls_srtp` export, `a=fingerprint`, peer-fingerprint verification),
`media/srtp.py` `SrtpSession` (RFC 3711, reused for SDES and DTLS keying alike),
and `RtpMediaTransport` (which already carries SRTP over either a plain UDP socket
**or** a datagram pipe, with RFC 7983 first-byte demux). The only thing WebRTC
adds that the SIP path does **not** want is **ICE** — a SIP-over-TLS call has the
peer's RTP address directly in the SDP (plus the existing comedia latch), so the
DTLS handshake runs over a **plain UDP socket**, not an ICE pipe.

## Decision

**An inbound SIP-over-TLS call whose audio m-line offers `UDP/TLS/RTP/SAVP` with an
`a=fingerprint` is answered with DTLS-SRTP media** (RFC 5763/5764), keyed by a DTLS
handshake run over the RTP **UDP socket** — **no ICE, no SDES `a=crypto`**.
Negotiation is **opportunistic**: the answer mirrors the offered media profile and
never downgrades an encrypted offer to plaintext, but a plain `RTP/AVP` offer is
still answered in the clear (the operator chose interop over hard-fail).

### 1. SDP profile — `UDP/TLS/RTP/SAVP` (non-WebRTC DTLS-SRTP)

- New profile constant `_SIP_DTLS_PROFILE = "UDP/TLS/RTP/SAVP"` (RFC 5764 §8) —
  distinct from WebRTC's `UDP/TLS/RTP/SAVPF` (the `F` = AVPF feedback, RFC 5124,
  which the SIP path does not use) and from SDES `RTP/SAVP`.
- `AudioMedia.is_sip_dtls` ⟺ `protocol == "UDP/TLS/RTP/SAVP"` **and** a media- or
  session-level `a=fingerprint` is present. `is_srtp` (`"SAVP" in protocol`) stays
  true for it; `is_webrtc` (SAVPF) stays false, so the existing audio path keeps
  owning it — no ICE/BUNDLE machinery is engaged.
- The DTLS-SRTP answer carries `a=fingerprint:sha-256 …` (ours) + `a=setup:` (our
  negotiated role) + `a=rtcp-mux`; **no** `a=crypto`, **no** `a=ice-*`/`a=candidate`,
  **no** `c=` suppression (unlike WebRTC, the SIP path keeps its `c=`/port — the
  media address is real, not ICE-borne). `build_audio_answer` gains an optional
  `dtls=(Fingerprint, SetupRole)` parameter: when supplied (offer is SIP-DTLS) it
  emits the `SAVP`+fingerprint+setup answer; otherwise the existing SDES/plain
  behaviour is byte-identical (regression invariant).

### 2. DTLS role (RFC 5763 §5, reuse ADR-0050 rationale)

For an `a=setup:actpass` offer the answerer picks its role; we default to
**`active`** (DTLS client, sends the ClientHello) — the same choice ADR-0050 made
for WebRTC, for the same gateway-compatibility reason. An `a=setup:passive` offer
makes us `active`; `a=setup:active` makes us `passive`. The knob
`HERMES_VOIP_SIP_DTLS_SETUP` (`auto` (default→active) | `active` | `passive`)
mirrors `HERMES_VOIP_WEBRTC_DTLS_SETUP` but is independent (the two transports may
need different defaults against different gateways).

### 3. Handshake transport — `SipDtlsMediaSession` over plain UDP (no ICE)

A new `media/sip_dtls_session.py` `SipDtlsMediaSession` mirrors
`WebRtcMediaSession` but replaces ICE with a plain UDP datagram pipe:

- binds an `asyncio` UDP endpoint on the local RTP address/port;
- `run_handshake(*, peer_fingerprint, peer_address, peer_port)` pumps the DTLS
  state machine over that socket — `feed(inbound)` / `get_outbound_datagrams()` →
  `sendto(peer)` — with a timeout + bounded round count (as the WebRTC pump does),
  verifies the peer fingerprint (`verify_peer_fingerprint`, RFC 5763 §5), and
  returns the derived `(srtp_inbound, srtp_outbound)`;
- exposes the **same** datagram-pipe surface the engine already consumes for
  WebRTC, so `RtpMediaTransport` carries SRTP over it unchanged, with RFC 7983
  demux dropping any late DTLS retransmits (first byte 20–63) so only SRTP
  (128–191) reaches the jitter buffer.

The DTLS half of `DtlsEndpoint` is reused verbatim — it was already
transport-agnostic; only the byte pump changes (UDP `sendto`/`recvfrom` instead of
ICE `send`/`recv`).

### 4. Answer/handshake ordering

As on the WebRTC path: build the answer (advertising our fingerprint+setup) and
send the 200 OK **first** (the peer needs our fingerprint+role to start its half),
then run the handshake. A handshake failure (timeout, fingerprint mismatch) ends
the call cleanly (errors propagate, rule 37) — it does **not** silently fall back
to plaintext (that would defeat the security the peer asked for).

### 5. Opportunistic negotiation order

Per offered audio profile: `UDP/TLS/RTP/SAVP` (+fingerprint) → **DTLS-SRTP**;
`RTP/SAVP` (+usable `a=crypto`) → **SDES** — we generate our own answer key,
advertise it in the answer's `a=crypto`, key our **outbound** SRTP with it and our
**inbound** with the offerer's key (RFC 4568 §6.1), closing the dormant
inbound-answer wiring gap (see Scope); `RTP/AVP` → **plain**. We never answer a
*weaker* profile than the offer. `HERMES_VOIP_SIP_DTLS_SRTP` (default **on**) gates
whether we answer DTLS-SRTP (off ⇒ a SIP-DTLS offer falls through to SDES/plain
handling) — a rollback switch, not a downgrade path.

This is delivered in two complete, independently live-testable stages (rule 6 —
each is end-to-end, not a parked half): **Stage 1 wires SDES** (SRTP working over
SIP-over-TLS *now*, validated against the test gateway switched to TLS); **Stage 2
adds DTLS-SRTP** as the preferred, cert-keyed tier ("real certs"). Stage 1 ships
first so SRTP is testable immediately.

### 6. Stage 2 build status (capability vs. activation — built 2026-06-19)

Stage 2 follows the same **build-in-engine / activate-in-adapter** split that
shipped the engine-side of ADR-0061 (RTCP) and the #142 media work: the lane that
builds the media capability does **not** touch the adapter, and a subsequent,
explicitly-named adapter wave wires it onto the live INVITE/answer path.

- **Built in this lane (capability, no adapter wiring):**
  - `sdp.py` — `_SIP_DTLS_PROFILE = "UDP/TLS/RTP/SAVP"`, `AudioMedia.is_sip_dtls`
    (SAVP profile **and** an `a=fingerprint` present, media- or session-level),
    `build_sip_dtls_answer(...)` (the `UDP/TLS/RTP/SAVP` answer carrying
    `a=fingerprint`/`a=setup`/`a=rtcp-mux`, keeping `c=`/port, no `a=crypto`/ICE),
    and an opportunistic `negotiate_media_security(offer)` ranking function that
    returns the chosen tier (`dtls` > `sdes` > `plain`). The `dtls=` keyword on
    `build_audio_answer` (ADR §1) is realised as the standalone
    `build_sip_dtls_answer` so the plain/SDES `build_audio_answer` output stays
    **byte-identical** (regression invariant) and the DTLS shape is a separate,
    independently-tested code path rather than a branch inside the SDES builder.
  - `media/sip_dtls_session.py` — `SipDtlsMediaSession`: owns a bound `asyncio`
    UDP datagram pipe (`_UdpDatagramPipe`), reuses `DtlsEndpoint` + the WebRTC
    `answer_setup_for_offer` role logic verbatim, drives the same RFC-7983 DTLS
    handshake pump, verifies the peer fingerprint, and derives
    `(srtp_inbound, srtp_outbound)`. The pipe satisfies the engine's existing
    `ice_transport` seam (async `send`/`recv`/`close`) — so the engine carries
    SRTP over it with **no engine change**.
  - `media/srtp.py` already consumes DTLS-derived keys (`SrtpSession.from_raw_keys`,
    via `DtlsEndpoint.derive_srtp_sessions`) — unchanged.
- **The adapter-activation wave (separate, named — NOT built here, rule 6):** a
  `_setup_sip_dtls_call` path in `adapter.py` that, for an inbound `is_sip_dtls`
  offer (gated on `HERMES_VOIP_SIP_DTLS_SRTP`), constructs the session with the
  offered `a=setup` + the `HERMES_VOIP_SIP_DTLS_SETUP` knob, sends the
  `build_sip_dtls_answer` 200 OK advertising the session socket's port **first**,
  then runs the handshake and builds `RtpMediaTransport(ice_transport=<the
  session's pipe>, srtp_inbound=…, srtp_outbound=…)` — exactly mirroring
  `_setup_webrtc_call` (which hands `ice_transport=session.ice`). The two config
  knobs `HERMES_VOIP_SIP_DTLS_SRTP` / `HERMES_VOIP_SIP_DTLS_SETUP` (`config.py`
  `MediaConfig`, mirroring `webrtc_dtls_setup`) are added in that wave. Until then
  the capability is dormant: an `is_sip_dtls` offer still falls through to the
  existing SDES/plain handler (no behaviour change).

**Comedia on the plain-UDP DTLS path (implementation decision).** Unlike WebRTC
(where ICE establishes the 5-tuple) and like the existing SDES/plain SIP path, a
DTLS-SRTP SIP call may sit behind NAT, so the peer's real source `(host, port)` can
differ from the SDP `c=`/port. `_UdpDatagramPipe` therefore sends to the
SDP-advertised peer initially but **latches** its send destination to the source
address of the first inbound datagram it receives (the comedia / symmetric-RTP
latch, mirroring the engine's `symmetric` behaviour) — the latch happens during the
DTLS handshake (the ClientHello/HelloVerify exchange reveals the real source), so
SRTP media flows to the correct 5-tuple without ICE. The latch is one-shot
(first-source-wins) to avoid mid-call source-spoofing redirection.

## Scope / deferred (rule 6, rule 28)

- **Inbound answering only.** Both stages answer an offered SRTP SIP call
  end-to-end (SDES key exchange / DTLS handshake → SRTP → media). **Outbound SRTP
  *offering*** (the agent's `place_call`, ADR-0029, which today offers plain
  `RTP/AVP`) is a distinct scenario — it must offer the secure profile and then
  parse + act on the gateway's *answer* (with downgrade handling). That is a
  separate, named follow-on, not a parked half of this one.
- **SDES answering is wired here (Stage 1).** The pre-existing SDES support was
  dormant — the adapter never supplied our answer key, so an `RTP/SAVP` offer
  488'd (`_negotiate_answer_crypto` raises; verified by repro) and live/e2e SIP
  calls ran plain RTP. Stage 1 closes that gap (generate + advertise our key, key
  outbound with it) so SDES is a real opportunistic middle tier, and corrects the
  `_setup_sdes_call` docstring that claimed an SDES answer the code never produced
  (rule 27).
- **In-dialog re-offer keying is part of Stage 1 (RFC 4568 §6 continuity).** A
  secured call's SDES context survives any in-dialog re-offer — hold, resume, or a
  peer re-INVITE — so the media stays `RTP/SAVP` + `a=crypto` and never silently
  downgrades to cleartext `RTP/AVP` mid-call. `LocalMediaSession` carries the
  accepted crypto (tag + suite); each re-offer we send mints a **fresh per-offer
  master key** echoing that tag + suite (`generate_answer_crypto`, §6.1
  per-sender keying) and re-keys the engine's outbound SRTP before it is sent; the
  peer's answer (or, on the answer side, the peer's re-offer) re-keys our inbound.
  `RtpMediaTransport.rekey_srtp` swaps both per-direction `SrtpSession`s atomically
  (outbound under the TX lock, bound to the engine's outbound SSRC). A plain call's
  re-offer stays plain. This was a real partial-ship of the answer wiring above
  (the initial `200 OK` was secured but the re-offer paths were crypto-blind); it
  is closed here, not deferred (rule 6).
- **No mutual TLS.** The operator chose DTLS-SRTP media over a signalling client
  certificate; the SIP/WSS connection keeps server-cert-only verification.
- **No SRTCP/`a=rtcp` separate port** beyond `a=rtcp-mux` (single 5-tuple), as on
  the WebRTC path.

## Consequences

- Inbound SIP-over-TLS calls that offer DTLS-SRTP get **cert-keyed encrypted
  media** instead of a 488 — the strongest media-key secrecy available (the key
  never appears in the SDP, unlike SDES). The signalling TLS that carries the
  fingerprint is already real-CA-verified, closing the binding (RFC 5763 §3).
- **Zero new dependencies** — every primitive (`DtlsEndpoint`, `SrtpSession`,
  `RtpMediaTransport` datagram pipe) already exists from ADR-0016; no codec or
  crypto library is added (rule 35).
- Plain-RTP and SDES offers are **byte-identical** to today (regression-tested):
  the new path engages only for `UDP/TLS/RTP/SAVP`+fingerprint offers.
- Per-call cost: one DTLS handshake (a few RTT, RSA-2048 sign/verify) at setup,
  then the same per-packet SRTP protect/unprotect as the SDES path — no
  steady-state overhead beyond SRTP (rule 22).
- We now maintain a second DTLS-driving site (`SipDtlsMediaSession`) alongside
  `WebRtcMediaSession`; both delegate to the one `DtlsEndpoint`, so the crypto is
  not duplicated, only the byte pump.

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| Keep SDES, just fix the SDES answer | Weaker: the SRTP master key rides inline in the SDP. Operator explicitly chose DTLS-SRTP ("real certs"). |
| Mutual TLS (client cert on signalling) | Authenticates the *connection*, not the media; leaves media keying as SDES/plain. Operator chose media-layer DTLS-SRTP instead. |
| Full WebRTC stack (ICE + SAVPF) over SIP | ICE is unnecessary on a SIP-over-TLS call (the RTP address is in the SDP + comedia latch); SAVPF feedback is unused. Pure overhead and a larger attack/maintenance surface. |
| Hard-require SRTP (488 any plain offer) | Operator chose **opportunistic** (interop over hard-fail); a plain-only peer still completes in the clear. A future strict knob can layer on top. |
| Reuse `WebRtcMediaSession` directly | It hard-couples ICE gather/credentials; a plain-UDP pump is simpler and avoids dragging ICE state into the SIP path. The shared part (`DtlsEndpoint`) is reused. |
