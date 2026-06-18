# ADR-0032: WebRTC media wiring (ICE + DTLS-SRTP) and the Opus wire codec

- **Date:** 2026-06-17
- **Status:** Accepted (§5 TURN + trickle deferrals closed by ADR-0034)
- **Deciders:** agent session (WebRTC lane) — operator-directed

## Context

ADR-0016 chose SIP-over-Secure-WebSocket signalling plus a DTLS-SRTP / ICE media
plane for the WebRTC transport, and its build-plan PRs landed the **primitives**:

- `sdp.py::build_webrtc_answer` (a `UDP/TLS/RTP/SAVPF` answer builder with
  `a=fingerprint` / `a=setup` / ICE attributes / `a=rtcp-mux`), plus the
  `Fingerprint` / `SetupRole` / `IceCandidate` typed attributes and the
  `AudioMedia.is_webrtc` discriminator (PR-B, #56).
- `media/ice.py::IceConnection` over `aioice` (gather → checks → a `send`/`recv`
  datagram pipe over the nominated pair; PR-D, #59).
- `media/dtls.py::DtlsEndpoint` (memory-BIO DTLS, ephemeral self-signed cert,
  RFC 5705 `EXTRACTOR-dtls_srtp` export → `SrtpSession.from_raw_keys`; PR-C, #58).
- `transport/ws_connection.py::WssSipTransport` (PR-A, #57).

But **none of it was wired**. The inbound INVITE handler in `adapter.py`
unconditionally called the SDES `build_audio_answer`; there were **no `src/` call
sites** for `IceConnection`, `DtlsEndpoint`, or `build_webrtc_answer`; and Opus —
the de-facto WebRTC audio codec — was recognised only by `sdp._order_opus_first`
(offer-ordering) and was **absent from the media engine** (`Codec`,
`_ENGINE_CODEC_TABLE`, encode/decode). A WebRTC (`SAVPF` / Opus / DTLS-SRTP) offer
therefore did not connect — it fell through the SDES path and either mis-keyed or
got rejected. This ADR records wiring the WebRTC media path end-to-end and adding
Opus so the plugin serves a WebRTC peer as a co-equal to SIP-over-TLS (rule 6: no
offer-only / half-wired transport).

Constraints: AGENTS.md rule 6 (end-to-end or not at all), rule 17/39 (no escape
hatches; webrtc-only imports stay behind the `webrtc` extra per the ADR-0014 lazy
split), rule 35 (licence/advisory gating on the new dep), the repo is **public**
(fakes only). The recorded ADR-0016 socket seam — "aioice owns the nominated
socket; the engine takes it rather than binding its own" — bounds the wiring.

## Decision

### 1. Opus codec in the engine (the ADR-0022 pattern, reused)

- New `media/opus.py` with `OpusEncoder` / `OpusDecoder`: stateful per-call (like
  `media/g722.py`), **48 kHz mono, 20 ms frames** (960 samples → one Opus packet;
  RTP timestamp advances by 960 at the 48 kHz clock). `application=VOIP`,
  `useinbandfec=1`. Both lazy-import `opuslib`.
- `Codec.OPUS` is added to `media/engine.py`. Because Opus is a **dynamic** payload
  type (no static RTP PT, RFC 3551), `Codec.OPUS.value` is the **conventional
  default 111** — but the wire PT is always the negotiated one (the engine already
  threads `payload_type` separately from the codec kind, as G.722 needs).
- `_ENGINE_CODEC_TABLE` gains `("OPUS", 48000): Codec.OPUS`; `_CODEC_DESCRIPTORS`
  gains `Codec.OPUS → _CodecDescriptor(wire_sample_rate=48000, rtp_clock_rate=48000)`.
  Unlike G.722, Opus's RTP clock **equals** its audio rate (both 48 kHz). The
  engine's existing rate-follows-codec machinery (`inbound_sample_rate`,
  `_to_wire_rate` resampling, the 20 ms re-framer, the deadline pacer) then carries
  Opus with no special-casing: TTS frames resample to 48 kHz, STT receives native
  48 kHz, packetisation is the standard 20 ms.
- **Dep:** `opuslib==3.0.1` (BSD-3-Clause, OSI-approved) in the `webrtc` extra. It
  is a pure-Python ctypes wrapper carrying **no compiled extension**; it dlopen's
  the **system `libopus.so`** (`ctypes.util.find_library('opus')`). libopus
  (BSD-3-Clause) is a runtime system dependency the host provides — the
  devcontainer ships `libopus0`; the `webrtc` + `supply-chain` CI jobs
  `apt-get install -y libopus0`. We do **not** vendor a heavy C build, and we do
  not pull in FFmpeg/PyAV (the only LGPL path).

### 2. `is_webrtc` branch in the inbound INVITE handler

`adapter._handle_inbound_invite` branches on `offer.audio.is_webrtc` (the
`UDP/TLS/RTP/SAVPF` profile discriminator). The SDES/G.711-G.722 path is unchanged.
The WebRTC path:

1. **Negotiates** codecs against a WebRTC-specific advertised menu (Opus first,
   then G.711 fallback) and clamps against the engine table (#84 belt-and-suspenders).
2. **Gathers** ICE candidates (`IceConnection(ice_controlling=False, …)` — the SIP
   UAS / answerer is ICE-**controlled**), reading STUN servers from
   `HERMES_VOIP_ICE_STUN_URLS` (empty ⇒ host-only; TURN deferred, ADR-0016 §6).
3. Constructs a `DtlsEndpoint` whose role derives from the offered `a=setup`
   (offer `actpass`/`active` ⇒ we are `passive`/SERVER; offer `passive` ⇒ we are
   `active`/CLIENT), and emits **our** fingerprint + our chosen setup in the answer.
4. **Builds + sends the 200 OK** answer (`build_webrtc_answer`) carrying our
   fingerprint, setup, ICE ufrag/pwd, and all gathered candidates (non-trickle,
   RFC 8838 §3) — *before* the DTLS handshake, since DTLS rides the media path that
   only opens once the peer has our answer.
5. **Runs ICE → DTLS** (`media/webrtc_session.py::run_webrtc_handshake`): apply the
   peer's ICE creds + candidates, `ice.connect()`, then pump DTLS records over
   `ice.send`/`ice.recv` (RFC 7983 first-byte demux: bytes 20–63 are DTLS) until the
   handshake completes; verify the peer fingerprint (RFC 5763 §5); derive the
   inbound/outbound `SrtpSession` pair.
6. Feeds the derived SRTP sessions + the connected `IceConnection` into
   `RtpMediaTransport`, which then carries SRTP media over the ICE pipe.

**Validation ordering (no answer-then-fail).** The mandatory WebRTC SDP attributes
(peer `a=fingerprint` + `a=ice-ufrag`/`a=ice-pwd`) and the Opus codec runtime
dependency (`opuslib` + the system `libopus`) are validated/preflighted **before**
the 200 OK: a malformed offer or a missing libopus is a clean **488 pre-answer
reject**, never an answered-but-dead call. The DTLS handshake itself is necessarily
**after** the 200 OK (the peer needs our answer to start it), so a handshake/ICE/
fingerprint-mismatch failure there tears the **answered** call down (close ICE +
abort setup → the inbound handler's `finally` releases it) rather than masquerading
as a 488. Either way the call never proceeds to the conversation loop on unkeyed
media — the no-answer-on-failure invariant (ADR-0005) holds identically to the SDES
path (cross-vendor review BLOCKING findings, 2026-06-17).

### 3. ICE socket seam in `RtpMediaTransport`

`RtpMediaTransport` gains an optional `ice_transport` seam (a narrow
`_IceDatagramPipe` Protocol: `send`/`recv`/`close`). When supplied, the engine does
**not** bind a UDP socket: `connect()` starts a background reader task that
`await ice.recv()`s, applies the RFC 7983 demux (only SRTP — first byte 128–191 —
reaches the engine; the handshake is already done, residual DTLS/STUN is dropped),
and enqueues onto the existing inbound queue; `send` writes via `ice.send`. The
entire SRTP / jitter / decode / pacing / DTMF machinery is reused verbatim — only
the datagram I/O is swapped (the seam the ADR-0016 review made explicit). The TLS
path (no `ice_transport`) is byte-for-byte unchanged.

### 4. Offer ordering

The WebRTC advertised menu is Opus-first then G.711 (`_order_opus_first` already
promotes Opus). Answers preserve the peer's offer order (`negotiate_audio`), as
before.

### 5. Scope / deferrals (rule 6 — named, not silent)

- **Inbound WebRTC is wired end-to-end.** Outbound WebRTC origination
  (UAC over WSS with our own offer) is **deferred**: the outbound path
  (`originate.py` + `_place_call`) keeps offering the SDES/G.711-G.722 menu over
  TLS. Blocker: outbound needs the WSS *signalling* transport selected end-to-end
  (`HERMES_SIP_TRANSPORT=wss` driving registration + INVITE over `WssSipTransport`),
  which is a signalling-plane concern beyond this media lane; tracked as a
  follow-up. This is a real, named boundary, not a stub.
- **Video stays deferred per ADR-0018** (the WebRTC answer is audio-only; an
  `m=video` line in the offer is declined). Forward-compat unaffected.
- **TURN relay** was deferred per ADR-0016 §6; **now wired in ADR-0034**
  (`HERMES_VOIP_ICE_TURN_*` are consumed; aioice gathers a relay candidate). This lane
  remained host + srflx only.
- **Trickle ICE** was non-trickle MVP here; **ADR-0034 adds the trickle SDP/ICE
  primitives** (`a=ice-options:trickle`, `a=end-of-candidates`, half-trickle answer +
  end-of-candidates control). The in-dialog SIP-INFO trickle transport (RFC 8840)
  remains a named follow-up. A full candidate set in the initial answer still
  interoperates with trickle peers per RFC 8838.
- **Live validation** against a real WebRTC client + the redeployed gateway is
  pending the operator's redeploy; this lane lands the wiring + the in-process
  (loopback ICE, in-memory DTLS pump, Opus round-trip) evidence.

## Consequences

- A WebRTC peer offering `SAVPF` / Opus / DTLS-SRTP now connects: SAVPF answer with
  Opus advertised first, a live ICE + DTLS-SRTP handshake, and SRTP media over the
  ICE-selected pair. The SDES path is untouched (regression-guarded).
- libopus becomes a **runtime system dependency** for the `webrtc` extra (documented
  in the README + runbook): a host without `libopus.so` gets a clear ImportError
  from `media/opus.py`, never silent dead audio.
- Opus is advertised **only** because the engine can now carry it (the #84
  advertise-without-carry drift guard extends to cover `("OPUS", 48000)`).
- The engine's pipeline now runs at 48 kHz on a WebRTC/Opus call (STT/TTS rate
  follows the codec, as G.722 established) — higher CPU per frame than 8 kHz G.711,
  bounded by the 20 ms deadline pacer (Opus encode is fast C, far cheaper than the
  pure-Python G.722 path already shipped).
- We are committed to the `opuslib` + system-libopus pairing and to keeping the
  webrtc-only imports lazy (the default install, licence gate, and base mypy gate
  stay free of pyOpenSSL / aioice / opuslib).

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Vendor a pure-Python or bundled-C Opus build | No clean self-contained wheel exists; opuslib/opuslib_next are ctypes wrappers over system libopus, PyOgg bundles libopus on Windows only. Vendoring a heavy C build was explicitly out of scope without an operator decision. The system-`libopus.so` path is permissive, builds with no C toolchain, and is already present in the image. |
| Use `aiortc` for the whole media stack | Hard-depends on `av`/PyAV → FFmpeg (LGPL-2.1+, the one copyleft path) for codecs we already own, and does no SIP — rejected in ADR-0016. |
| Make `RtpMediaTransport` open a UDP socket and have ICE hand it the fd | aioice owns the nominated socket and runs STUN consent on it; reusing that socket fd out from under aioice fights the library. The `send`/`recv` datagram pipe is aioice's intended seam (ADR-0016 socket-handoff decision). |
| A separate `WebRtcMediaTransport` class | Would duplicate the entire SRTP / jitter / pacing / DTMF / barge-in machinery (~1.7 kloc) that is transport-agnostic; the only WebRTC difference is datagram I/O. A narrow injected `ice_transport` seam keeps one engine, one set of tests. |
| Advertise Opus on the SDES/TLS path too | ADR-0005 prefers Opus generally, but SDES-Opus is a separate negotiation surface (dynamic PT over `RTP/SAVP`) with its own live-validation risk; this lane scopes Opus to the WebRTC path it was added for. The engine *can* carry it on either path now, so a later ADR can widen the SDES menu without engine work. |
