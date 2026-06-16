# ADR-0016: WebRTC transport — SIP-over-WSS signalling + DTLS-SRTP media + ICE

- **Date:** 2026-06-16
- **Status:** Accepted
- **Deciders:** agent session (transport architecture), operator direction

## Context

The plugin ships SIP-over-TLS today: TLS signalling (ADR-0005), SDES-SRTP or plain
RTP media (ADR-0013), and symmetric-RTP (comedia) latching for NAT (ADR-0015). The
operator's mandate is **"any RFC-compliant SIP-over-TLS _or_ WebRTC voice gateway"**,
and CLAUDE.md names WebRTC as a co-equal transport — not a fallback. SIP here means
**SIP-over-TLS only** (no plaintext SIP/UDP/TCP, per the ADR-0005 encryption
invariant); the second transport this ADR adds is **WebRTC**: SIP signalling over a
**secure WebSocket** (WSS) plus **DTLS-SRTP** media keyed by a DTLS handshake on the
media path, with **ICE** for connectivity. This is the profile a WebRTC-capable SIP
gateway exposes on its secure-WebSocket endpoint, and a live probe against the test
gateway already confirmed a real RFC 7118 SIP-over-WebSocket endpoint exists there
(`Sec-WebSocket-Protocol: sip` → HTTP 101).

The design is bounded by the same forces as ADR-0005, plus the WebRTC RFCs:

- **The conversational plane must not move.** ADR-0002/0003 keep STT ↔ agent ↔ TTS in
  one process on the agent's event loop; ADR-0004 defines the `MediaTransport` /
  `CallSignaling` / `CallMedia` seams. The transport choice **must not leak above those
  seams** — the manager, dialog, `CallSession`, `CallLoop`, and adapter stay unchanged
  and pick the transport by config.
- **Maximise reuse, minimise new surface.** The SIP message/transaction/dialog
  plumbing, registration, digest, SDP core, the RFC 3711 SRTP transform, the jitter
  buffer, G.711 codecs, VAD/STT/guard/TTS, and the call loop are all transport-agnostic
  already. WebRTC is a **signalling-framing + media-keying** addition, not a rewrite —
  and the codebase anticipated it: `config.py` already parses `HERMES_SIP_TRANSPORT=wss`
  → Via `WSS`; `registration.py` and `manager.SipTransport` already document the
  `<token>.invalid` sent-by "for WebSocket per RFC 7118".
- **WebRTC security is non-negotiable (RFC 8827 §6.5).** On the WebRTC transport media
  **MUST** be DTLS-SRTP; **SDES `a=crypto` MUST NOT** be offered or accepted and
  **plain RTP MUST NOT** be sent. So the WSS transport cannot reuse the SDES (ADR-0013)
  or plain-RTP keying paths — only the RFC 3711 packet transform underneath them.
- **Rule 40/35 dependency gate.** DTLS and ICE need machinery `cryptography` does not
  provide (verified below), so this introduces **new runtime dependencies**. Each must
  be justified, licence-checked, permissive (no copyleft), and isolated in an optional
  extra — same discipline as the `ml` and `media` extras.
- **Rule 22 efficiency.** ICE connectivity checks and a DTLS handshake add per-call
  setup cost and a small native footprint; budgeted in Consequences.
- **Rules 23/26.** Every interop and latency claim here is RFC-grounded or
  library-documented; none is validated against the live gateway yet — the build plan
  ends in live validation, and until then the WebRTC path is **designed, not proven**.

## Decision

**Add WebRTC as a second transport behind the existing seams: a new `WssSipTransport`
(SIP-over-Secure-WebSocket, RFC 7118) implementing the same `SipTransport` /
`CallSignaling` Protocols as `SipOverTlsTransport`; DTLS-SRTP media keying (RFC
5763/5764) that feeds the _existing_ `SrtpSession` (only the key source changes from
SDES to a DTLS handshake); and full, non-trickle ICE (RFC 8445) via `aioice`. The
transport is selected by `HERMES_SIP_TRANSPORT` (`tls` | `wss`) — the manager, dialog,
`CallSession`, `CallLoop`, and adapter are otherwise unchanged.** This ADR is the WHY;
the implementing PRs and the live-validation runbook are the HOW.

### 1. Signalling — SIP-over-Secure-WebSocket (RFC 7118)

A new `src/hermes_voip/transport/ws_connection.py::WssSipTransport`, structurally a
drop-in for `SipOverTlsTransport`: it implements `manager.SipTransport`
(`send` / `local_sent_by` / `contact_uri`) and is each call's `call.CallSignaling`
(`send`), and it keeps the same Call-ID demux, the `on_new_call` / `on_unroutable` /
`on_connection_lost` observers, the auto-ACK of non-2xx INVITE finals, and the
out-of-dialog OPTIONS/NOTIFY keepalive (`keepalive.py`, ADR's PR #50 fix) — all reused
verbatim because they are framing-independent. The deltas from the TLS transport:

- **Framing (the one substantive code change).** Over WebSocket **each SIP message is
  exactly one WebSocket message/frame, and a frame carries at most one SIP message**
  (RFC 7118 §5). The TLS path's `Content-Length` stream-framing
  (`transport/framing.py::SipMessageFramer`) is therefore **not used** on WSS: a
  received text frame _is_ a complete message, dispatched straight to
  `SipRequest.parse` / `SipResponse.parse`. (`Content-Length` stays semantically valid
  for the body but no longer delimits messages.) `framing.py` stays exactly as-is for
  the TLS transport; the WSS transport simply does not invoke it.
- **WebSocket handshake.** Connect with subprotocol token **`sip`** (`Sec-WebSocket-
  Protocol: sip`, RFC 7118 §4.1) over TLS (`wss://`). The HTTP-upgrade endpoint path
  (e.g. `/ws`) is gateway config, read from `HERMES_SIP_WS_PATH` (default `/ws`).
- **Via transport + sent-by.** Via uses the **`WSS`** transport token (RFC 7118 §5.1) —
  already what `config.GatewayConfig.via_transport` returns for `wss`. The client has no
  routable address, so the Via `sent-by` is a stable random token under the **`.invalid`
  TLD** (RFC 2606), e.g. `Via: SIP/2.0/WSS hq7sk2.invalid;branch=…`. `local_sent_by`
  returns that token (the `SipTransport` Protocol doc already allows "an `.invalid` for
  WSS"); `WssSipTransport` generates it once per connection rather than reading a socket
  address.
- **Contact + Outbound (RFC 5626).** The SIP URI scheme stays `sip:`; the WebSocket-ness
  is the **`transport=ws`** URI parameter (lowercase `ws`). RFC 7118's deployment model
  is SIP Outbound, so `contact_uri(extension)` emits
  `<sip:<ext>@<token>.invalid;transport=ws>;reg-id=1;+sip.instance="<urn:uuid:…>"` — a
  per-registration persistent instance-id URN (RFC 5626 §4.1) and a `reg-id` (§4.2). The
  `+sip.instance` UUID is stable per extension for the process lifetime (sourced
  deterministically, never a real device id — the repo is public). GRUU
  (`pub-gruu`/`temp-gruu` from the REGISTER 200, RFC 5627) is parsed and used as the
  dialog local contact when the registrar returns one, so REFER/transfer routing
  (ADR-0011) works through the WSS edge; absent a GRUU the Contact above is used.
- **Keep-alive.** WebSocket Ping frames (or the RFC 5626 §3.5.1 double-CRLF ping the TLS
  framer already tolerates). WebSocket is a reliable transport, so SIP transaction
  retransmit timers stay disarmed exactly as on TLS (RFC 7118 §5.1) — the existing
  `transaction.py` modelling holds unchanged.

Everything else on the wire — start-line, headers, REGISTER/INVITE/ACK/BYE/REFER, the
digest computation — is **byte-for-byte the same format as SIP/TLS**, so `message.py`,
`transaction.py`, `dialog.py`, `registration.py`, `digest.py`, `incall.py`, `refer.py`,
and `manager.py` are reused without change.

### 2. Media security — DTLS-SRTP (RFC 5763/5764), reusing the SRTP transform

WebRTC keys SRTP from a **DTLS handshake on the media path**, not from SDP. The crucial
reuse: the keys it produces are AES-CM-128 + HMAC-SHA1 master key (16 B) + salt (14 B) —
**the same suite `srtp.py` already implements** for SDES. So **`SrtpSession` (the RFC
3711 AES-CM/HMAC-SHA1 protect/unprotect, KATs, ROC, replay window, per-SSRC binding) is
reused verbatim; only the source of the key||salt changes.** A new
`src/hermes_voip/media/dtls.py` owns the keying:

- **SDP attributes (RFC 5763 §5).** The offer carries `a=fingerprint:sha-256 <hex>` (the
  hex SHA-256 of our self-signed DTLS cert, RFC 4572) and `a=setup:actpass`; the answer
  picks `a=setup:active`/`passive`. `a=setup` decides DTLS roles: the **`active`** party
  is the DTLS client and sends ClientHello, **`passive`** is the server (RFC 4145). We
  offer `actpass` and, as answerer to a gateway offer, prefer `active`. `a=connection`
  **MUST NOT** appear (RFC 5763 §5). The media profile token is **`UDP/TLS/RTP/SAVPF`**
  (vs SDES's `RTP/SAVP`).
- **DTLS handshake + keying.** After ICE nominates a candidate pair (§3), run a DTLS
  handshake over that UDP path using a memory-BIO DTLS endpoint (so DTLS rides the
  ICE-selected datagram flow, not a raw socket). On completion, verify the peer cert
  against the negotiated `a=fingerprint`, then export keying material with the **RFC 5705
  exporter, label `"EXTRACTOR-dtls_srtp"`, no context** (RFC 5764 §4.2). The exported
  block is `client_write_key ‖ server_write_key ‖ client_write_salt ‖ server_write_salt`
  (16/16/14/14 B for `SRTP_AES128_CM_HMAC_SHA1_80`). DTLS-client side uses the client
  key/salt outbound and server inbound; DTLS-server side mirrors it (RFC 5764 §4.2). Each
  direction's key||salt constructs a `SrtpSession` exactly as the SDES path does today
  (`CryptoAttribute`-shaped or a small direct constructor — see PR plan).
- **SRTP profile negotiation.** Offer `SRTP_AES128_CM_HMAC_SHA1_80` (and `_32`) in the
  DTLS `use_srtp` extension (RFC 5764 §4.1.1); the gateway returns one.
- **rtcp-mux + demux.** RTP, RTCP, DTLS, and STUN share one UDP 5-tuple (`a=rtcp-mux`,
  RFC 5761). The receive path demuxes by first byte (RFC 5764 §5.1.2): STUN 0–1, DTLS
  20–63, SRTP/SRTCP 128–191. This is the one new piece of receive-side dispatch the
  engine grows; below it, the SRTP branch is the existing `unprotect` path.
- **No SDES, no plain RTP on WSS (RFC 8827 §6.5).** The WSS transport never emits
  `a=crypto` and never accepts `RTP/AVP`. The SDES path (ADR-0013) and plain-RTP path
  remain **SIP-over-TLS only**.

### 3. ICE / STUN / TURN (RFC 8445, 8839)

A new `src/hermes_voip/media/ice.py` wrapping **`aioice`** (pure-Python asyncio ICE +
STUN + TURN agent) gathers candidates, performs connectivity checks, and yields the
nominated UDP path to the DTLS+SRTP engine.

- **Full ICE, not ICE-lite.** The client is behind NAT (ADR-0015's whole premise), and
  ICE-lite never initiates checks (valid only on a public address, RFC 8445 §A.2). So we
  run **full ICE**: gather, initiate, and respond to checks.
- **Non-trickle MVP (RFC 8838 §15).** Gather a full candidate generation, then send
  **all** candidates in the initial offer/answer — no trickling. This is fully
  interoperable with trickle-capable gateways. Trickle / half-trickle is a later
  optimisation, not MVP.
- **Candidate types, phased.** **MVP: host candidates always, server-reflexive (srflx)
  via a configured STUN server.** **TURN (relay) is deferred** — it needs an allocation +
  credentials + an operated/contracted TURN server (rule 40/41 infra), out of MVP scope;
  added only if a real call needs it. STUN server config: `HERMES_VOIP_ICE_STUN_URLS`
  (e.g. `stun:stun.example.test:3478`); empty → host-only ICE.
- **SDP attributes (RFC 8839).** Add `a=ice-ufrag`, `a=ice-pwd`, one `a=candidate` line
  per gathered candidate (`foundation component-id transport priority address port typ
  host|srflx [raddr … rport …]`), `a=rtcp-mux`, and `a=ice-options:ice2`. (Trickle tag
  and `a=end-of-candidates` are added with trickle later.)
- **ICE subsumes comedia (ADR-0015).** ICE connectivity checks are STUN binding requests
  on the media path — the principled superset of "send to the source of received media":
  ICE additionally _selects_ the working pair by priority and keeps the NAT binding alive
  with STUN consent freshness (RFC 7675). On the WSS/ICE transport, ICE owns the
  destination address; the ADR-0015 latch is **not** used there (it stays the mechanism
  for the plain SIP-over-TLS trunk-behind-NAT case). The two compose by transport, never
  both at once.

### 4. SDP — what `sdp.py` must add

The SDES path stays. A WebRTC offer/answer is a superset; `sdp.py` grows parse + build
support for the WebRTC attributes, gated to the WSS/DTLS path:

| SDP element | SDES path (today) | WebRTC path (new) |
| --- | --- | --- |
| `m=` profile | `RTP/SAVP` | `UDP/TLS/RTP/SAVPF` |
| Media keying | `a=crypto:` (SDES inline) | `a=fingerprint:sha-256 …` + `a=setup:actpass/active/passive` |
| ICE | none | `a=ice-ufrag`, `a=ice-pwd`, `a=candidate …`, `a=ice-options:ice2` |
| RTCP | implicit RTP+1 | `a=rtcp-mux` |
| Codecs | `a=rtpmap`/`a=fmtp` (unchanged) | `a=rtpmap`/`a=fmtp` (unchanged) |

The codec negotiation (`negotiate_audio`, Opus-then-G.711 ordering) and the
`AudioMedia`/`Codec`/`SessionDescription` parse core are reused as-is; the additions are
new typed attributes (a `Fingerprint`, a `SetupRole`, ICE `ufrag`/`pwd`/`Candidate`) and
a builder branch that emits `UDP/TLS/RTP/SAVPF` with fingerprint+setup+ICE instead of
`a=crypto`. `build_audio_offer`/`build_audio_answer` gain a keyword path for the WebRTC
shape; the SDES signature is untouched.

### 5. Reuse vs new

| Reused unchanged | New (this transport) |
| --- | --- |
| `message.py` (SIP build/parse) | `transport/ws_connection.py` — `WssSipTransport` (WS framing, `.invalid` Via, `transport=ws` Contact + Outbound) |
| `transaction.py` (INVITE client/server txn, non-2xx ACK) | `media/dtls.py` — DTLS handshake + RFC 5705 keying export → `SrtpSession` |
| `dialog.py`, `incall.py`, `refer.py` (dialog, hold, transfer) | `media/ice.py` — `aioice` ICE agent (gather/checks/nominate) + STUN |
| `registration.py` + `digest.py` (REGISTER, auth) | `sdp.py` **additions** — `Fingerprint`/`SetupRole`/ICE attrs + a WebRTC offer/answer branch |
| `manager.py` (`SipTransport`/demux/routing) | engine receive-side first-byte demux (STUN/DTLS/SRTP) |
| `sdp.py` core (parse, `negotiate_audio`, codec order) | new `webrtc` optional extra (`aioice`, `pyOpenSSL` [, `pylibsrtp`]) |
| `media/srtp.py` (`SrtpSession` RFC 3711 transform, KATs) | `+sip.instance` UUID + GRUU handling on the WSS Contact |
| `media/rtp.py` (`RtpPacket`, `JitterBuffer`) + `media/audio.py` (G.711, resample) | — |
| `media/engine.py` (RTP send/recv, pacing, hold) — keying swapped in, packet path same | — |
| `media/vad.py`, `stt/*`, `tts/*`, `guard/*`, `media/call_loop.py` (conversational plane) | — |
| `adapter.py`, `call.py`, `keepalive.py`, `tools.py` (control plane + agent verbs) | — |

**The adapter and call loop stay transport-agnostic.** `adapter.connect()` constructs
`SipOverTlsTransport` **or** `WssSipTransport` from `HERMES_SIP_TRANSPORT`; both satisfy
`SipTransport`, so `RegistrationManager` and `_on_inbound_invite` are unchanged. Two
adapter helpers branch by transport: the transport constructor (`_make_tls_context` +
the transport class) and the per-call keying (today `_srtp_from_audio` reads SDES
`crypto_attrs`; the WSS path instead drives `media/dtls.py` to produce the two
`SrtpSession`s, then constructs the **same** `RtpMediaTransport` with `srtp_inbound`/
`srtp_outbound` and `symmetric=False`). `RtpMediaTransport` itself is reused — its SRTP
seam is already a narrow Protocol (`_SrtpProtect`/`_SrtpUnprotect`), so DTLS-derived
sessions slot in with no engine change beyond the receive demux.

### 6. Configuration + scope

A deployment selects the transport with the **existing** `HERMES_SIP_TRANSPORT` env
(`tls` default | `wss`); `config.py` already parses both. New WebRTC-only keys (parsed
into a small `WebrtcConfig`, defaulted so non-WSS installs ignore them):

- `HERMES_SIP_WS_PATH` — WebSocket upgrade path (default `/ws`).
- `HERMES_VOIP_ICE_STUN_URLS` — comma-separated `stun:` URLs; empty → host-only ICE.
- `HERMES_VOIP_ICE_TURN_URLS` / `…_TURN_USERNAME` / `…_TURN_PASSWORD` — **reserved,
  deferred** (TURN is post-MVP); parsed but unused until the TURN PR lands, so the schema
  is stable.

Gateway endpoint: `wss://${HERMES_SIP_HOST}:${HERMES_SIP_PORT}${HERMES_SIP_WS_PATH}`
(port default `443` for `wss`, already in `config.py`). All connection facts stay in the
gitignored `.env` / 1Password; tests use `pbx.example.test` / ext `1000` /
`stun:stun.example.test:3478`.

**Honest MVP boundary.** Ships first: WSS signalling (REGISTER + inbound INVITE +
BYE + the keepalive answers), DTLS-SRTP with `AES_CM_128_HMAC_SHA1_80/32`, full
non-trickle ICE with host + STUN-srflx candidates, half-duplex media (ADR-0008 Phase 1,
same as TLS). **Deferred (named, not silently dropped):** TURN/relay candidates, trickle
& half-trickle ICE, SRTCP/`a=rtcp-mux` feedback messages beyond mux, multiple `m=`
lines/BUNDLE, and any DTLS-SRTP suite outside the two AES-CM-128 suites. Each is a
follow-up task, not part of "WebRTC works".

### 7. Build plan — ordered, independently-testable PRs

Per rule 6, every PR ships wired and green (TDD per rule 18; cross-vendor review per rule
21). The split is for review-ability, not deferral — the transport is not "done" until
PR-G validates live.

1. **PR-A — WSS framing + `WssSipTransport` REGISTER.** WebSocket client (subprotocol
   `sip`), one-message-per-frame dispatch, `.invalid` Via, `transport=ws` Contact +
   `+sip.instance`/`reg-id`, `local_sent_by`/`contact_uri`. Tested against a loopback
   asyncio WS echo/SIP server fixture (mirrors the existing TLS loopback fixture):
   REGISTER 401→200 via `RegistrationManager`. No new media. *(Decide the WS client lib
   here: stdlib has no WS client; evaluate a minimal permissive WS lib vs a hand-rolled
   RFC 6455 client over the existing asyncio TLS — recorded in PR-A, licence-gated.)*
2. **PR-B — SDP DTLS + ICE attributes.** `sdp.py` parse/build for `a=fingerprint`,
   `a=setup`, `a=ice-ufrag`/`-pwd`, `a=candidate`, `a=rtcp-mux`, `UDP/TLS/RTP/SAVPF`.
   Pure sans-IO; round-trip + RFC-8839 field-order tests. No network.
3. **PR-C — DTLS-SRTP keying (`media/dtls.py`).** `pyOpenSSL` memory-BIO DTLS handshake +
   cert-fingerprint verify + RFC 5705 export → two `SrtpSession`s. Deterministic test:
   drive both ends of a DTLS handshake in-process, assert the exported key||salt feeds
   `SrtpSession` and a protect→unprotect round-trips. Adds the `webrtc` extra.
4. **PR-D — ICE agent (`media/ice.py`).** `aioice` wrapper: gather host + (configured)
   srflx, run checks against a loopback/STUN fixture, nominate a pair; engine receive
   demux (first-byte STUN/DTLS/SRTP). Tested with a local STUN responder fixture.
5. **PR-E — Engine + adapter wiring.** `RtpMediaTransport` consumes the ICE-nominated
   path + DTLS sessions (`symmetric=False`); `adapter.connect()` selects the transport by
   `HERMES_SIP_TRANSPORT`; `WebrtcConfig` parsing. End-to-end test with all three new
   pieces stubbed deterministically (no real timing), proving the inbound-INVITE →
   answer → media-loop path is identical above the seam.
6. **PR-F — `.env.example` + runbook.** WebRTC config keys, the wss endpoint shape, and a
   `docs/runbooks/` entry for live WebRTC validation (rule 42).
7. **PR-G — Live validation.** Register + take one real inbound call over `wss://…/ws`
   with DTLS-SRTP + ICE against the test gateway; record measured ICE/DTLS setup time and
   first-audio latency (rules 23/26). Only here is the WebRTC transport reported done.

## Consequences

- **Easier:** a single config flag (`HERMES_SIP_TRANSPORT=wss`) switches a deployment to
  WebRTC with the **entire** conversational + control plane (manager, dialog, call,
  loop, adapter, providers) unchanged — the seam discipline from ADR-0004/0005 pays off
  directly. The hardest crypto (RFC 3711 SRTP) is already built and KAT-proven; WebRTC
  reuses it. ICE replaces the bespoke comedia latch with a standards path and brings
  consent-freshness keepalive "for free" on that transport.
- **Harder / committed to maintain:** three new surfaces — a WS SIP transport, a DTLS
  handshake/keying module, and an ICE integration — each with real interop edge cases
  (DTLS role/glare, ICE pair selection, first-byte demux) that need testing against a
  real gateway, not just mocks (which is why PR-G is part of "done"). The receive path
  gains a first-byte demux it did not have. We now maintain a `setup`/role state machine
  and an instance-id/GRUU lifecycle for Outbound.
- **Dependencies (rule 35) — the decisive finding:** the pinned `cryptography` (46.0.7,
  `media` extra) exposes **no DTLS handshake and no RFC 5705 exporter** — its hazmat
  surface is primitives only — and the stdlib `ssl` has no DTLS. **DTLS-SRTP therefore
  requires new dependencies; it cannot be done with the pinned lib alone.** A new
  **`webrtc` optional extra** (mirroring `media`/`ml` isolation, lazy-imported, absent
  from the default install/licence-gate/audit) carries:
  - **`aioice`** — ICE + STUN + TURN agent, **pure-Python**, asyncio-native — **BSD-3-Clause**.
  - **`pyOpenSSL`** — DTLS handshake + `export_keying_material` (RFC 5705) over a memory
    BIO — **Apache-2.0** (native OpenSSL 3.x, also Apache-2.0). **Pin `pyOpenSSL==25.3.0`**:
    it admits `cryptography` 46.x, whereas 26.3.0 requires `cryptography>=49` (would force
    a `cryptography` bump + re-running the RFC 3711 KATs + `pip-audit`). Confirm the exact
    `requires_dist` at lock time.
  - **`pylibsrtp`** (BSD-3-Clause; bundles libsrtp2, also BSD-3-Clause) is **optional and
    likely omitted** — our `srtp.py` already does the RFC 3711 transform, so DTLS-exported
    keys feed it directly. Pulled in only if validating against DTLS keys shows a reason to
    prefer the native libsrtp2 path.
  - Transitive: `dnspython` (ISC), `ifaddr` (MIT/BSD-class) — both permissive.
  - **All permissive; zero copyleft.** We explicitly **reject full `aiortc`**: it
    hard-depends on `av`/PyAV which bundles **FFmpeg (LGPL-2.1+)** — the only copyleft
    path — for codecs we already own (G.711, and Opus is negotiated not transcoded here),
    and it provides **no SIP** anyway (the SIP-over-WSS UA is ours to write either way).
  - **Bus-factor risk (recorded):** `aioice` and `pylibsrtp` share a single maintainer
    (the `aiortc` author); both are actively released (2025). Noted as a supply-chain risk
    in the licence/advisory review, not a blocker.
- **Efficiency (rule 22):** vs the TLS/SDES path, WebRTC adds per-call setup cost — ICE
  gathering + connectivity checks (one or more STUN round-trips) and a DTLS handshake
  (≥2 RTT) — before first audio; budgeted and **measured in PR-G**, not assumed. Steady-
  state media cost is unchanged: the same SRTP AES-CM/HMAC per packet and the same G.711
  transcode/jitter path as today. The native footprint is OpenSSL (already a transitive
  presence) plus `aioice`'s pure-Python agent; no FFmpeg, no large model.
- **Lock-in / cost:** no vendor or platform lock-in and no recurring cost — all deps are
  OSS pinned in `uv.lock`. A STUN server is needed for srflx candidates (a free public
  STUN or a self-run `coturn` in STUN-only mode); TURN, if ever required, is operated
  infrastructure gated by rules 40/41 in its own ADR.
- **Standards-only, gateway-agnostic:** the WebRTC path speaks RFC 7118 / 5763 / 5764 /
  8445 / 8839 / 8827 — no vendor quirk in core; gateway specifics stay in `HERMES_SIP_*`
  config. Both transports remain RFC-compliant, satisfying the "any RFC-compliant
  SIP-over-TLS or WebRTC gateway" mandate.

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| Adopt full **`aiortc`** for the WebRTC stack | Hard-depends on `av`/PyAV → **FFmpeg (LGPL-2.1+)** (the only copyleft path) for codecs we already own, adds a large native blob, **and provides no SIP** — the SIP-over-WSS UA is ours to write regardless. Reusing only its permissive sub-pieces (`aioice` + `pyOpenSSL`) gives ICE+DTLS with zero copyleft and a smaller footprint. |
| Key WSS media with **SDES** (reuse ADR-0013 as-is) | **RFC 8827 §6.5 forbids it** — WebRTC media MUST be DTLS-SRTP and MUST NOT offer/accept SDES. SDES stays the SIP-over-TLS path only. |
| Do DTLS with the already-pinned **`cryptography`** | Verified it cannot: `cryptography` exposes no DTLS handshake and no RFC 5705 keying-material exporter (primitives only), and stdlib `ssl` has no DTLS. A new dep (`pyOpenSSL`) is unavoidable for DTLS-SRTP. |
| **ICE-lite** instead of full ICE | ICE-lite never initiates connectivity checks and is valid only on a public address (RFC 8445 §A.2); the client is behind NAT (ADR-0015), so it must run full ICE to traverse. |
| **Trickle ICE** in the MVP | Non-trickle (all candidates in the initial offer/answer) is fully interoperable with trickle peers (RFC 8838 §15) and far simpler; the gateway returns its answer in one SDP, so there is little client-side to trickle. Trickle is a later optimisation. |
| **Replace** the SIP-over-TLS transport with WebRTC | The mandate is "SIP-over-TLS **or** WebRTC" — both are co-equal, and SIP-over-TLS works live today (W16). WebRTC is additive behind the same seams, selected by config, not a replacement. |
| Keep **comedia latching** (ADR-0015) on the WSS path | ICE is the standards superset (it latches *and* validates/selects/consent-refreshes). The ADR-0015 latch stays the mechanism for the plain SIP-over-TLS trunk-behind-NAT case; the two are chosen by transport, never run together. |
| One transport class handling both TLS and WSS | The framing differs fundamentally (TLS `Content-Length` stream vs one-message-per-WS-frame) and the address model differs (real socket sent-by vs `.invalid` token). A second `SipTransport` implementation is cleaner than branching one class; both satisfy the same Protocol, so the manager/call code does not care. |
| Reuse `framing.py`'s `Content-Length` framer over WebSocket | Unnecessary and wrong: RFC 7118 §5 guarantees one SIP message per WS frame, so a frame is already a complete message. The framer stays the TLS transport's; the WSS transport dispatches frames directly. |
