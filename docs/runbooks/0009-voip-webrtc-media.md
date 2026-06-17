# Runbook: VoIP WebRTC media plane (DTLS-SRTP + ICE + Opus)

**What it is.** The plugin answers an inbound WebRTC peer — a peer whose SDP offers the
`UDP/TLS/RTP/SAVPF` profile — as a co-equal to SIP-over-TLS: it negotiates **Opus** (48 kHz,
G.711 fallback), keys SRTP via a **DTLS-SRTP** handshake (RFC 5763/5764), runs **ICE** (RFC
8445) connectivity, and carries SRTP media over the ICE-selected pair. A plain `RTP/AVP` or
SDES `RTP/SAVP` offer is unaffected — it takes the SIP-over-TLS path exactly as before.

The WHY lives in **ADR-0032** (WebRTC media wiring + Opus) and **ADR-0016** (WebRTC transport
design). This runbook is the operational HOW.

> **Public repo.** No secrets here. Connection details (host/extension/password) live only in
> the gitignored `.env` / 1Password; the values below are fakes.

## What is wired vs deferred (read before relying on it)

| Capability | Status |
| --- | --- |
| Inbound WebRTC media (DTLS-SRTP + ICE + Opus) | **Wired** (ADR-0032) |
| Opus 48 kHz on the wire; G.711 fallback | **Wired** |
| ICE host candidates + STUN server-reflexive (srflx) | **Wired** |
| **Outbound** WebRTC origination (our own offer) | **Deferred** — outbound runs over SIP-over-TLS |
| SIP **signalling over Secure-WebSocket** (`HERMES_SIP_TRANSPORT=wss`) | **Deferred** — registration/INVITE run over TLS |
| **TURN** relay | **Deferred** (ADR-0016 §6); STUN srflx only |
| **Trickle** ICE | **Deferred** — non-trickle MVP (all candidates in the answer) |
| WebRTC **video** | **Deferred** (ADR-0018) |
| **Live** validation against a real WebRTC client | **Pending** the operator's redeploy |

## Prerequisites

1. **The `webrtc` extra** (`uv sync --extra webrtc` / `--all-extras`): `aioice` (ICE),
   `pyopenssl` (DTLS-SRTP), `opuslib` (Opus), `websockets` (WSS — roadmap).
2. **The system `libopus` shared library** — `opuslib` is a pure-Python ctypes wrapper that
   `dlopen`s `libopus` at runtime (it bundles no native code):

   ```
   sudo apt-get update && sudo apt-get install -y libopus0
   ```

   The devcontainer image already ships `libopus0`; the `webrtc` + `hermes-contract` CI jobs
   install it explicitly. **Without `libopus`**, a WebRTC/Opus call fails the call setup with
   a clear `ImportError` (the media engine never answers dead) — SIP-over-TLS (G.711/G.722)
   keeps working. Verify the library resolves:

   ```
   uv run python -c "import ctypes.util; print(ctypes.util.find_library('opus'))"
   # -> libopus.so.0
   ```

## The knob

| Item | Value |
| --- | --- |
| Env var | `HERMES_VOIP_ICE_STUN_URLS` |
| Type | comma-separated list of `stun:` URLs |
| Default | empty ⇒ **host-only** ICE |
| Read by | `hermes_voip.config.load_media_config` → `MediaConfig.ice_stun_urls` |
| Applied at | `WebRtcMediaSession(stun_urls=…)` → the ICE agent, per inbound WebRTC call |

- **Empty (default):** host-only ICE — works on a LAN, or wherever the WebRTC peer can reach
  one of our host candidates directly. No external service required.
- **Set:** each `stun:` URL gathers a server-reflexive (srflx) candidate, for a peer behind
  NAT that cannot reach our host candidate. Example (a value, no secret):

  ```
  HERMES_VOIP_ICE_STUN_URLS=stun:stun.example.test:3478,stun:stun2.example.test:3478
  ```

  A malformed URL fails loudly when the ICE agent is built (not silently at parse). TURN keys
  are reserved (ADR-0016 §6) but unused.

## What happens on an inbound WebRTC call (the flow)

1. The INVITE's SDP offer carries `m=audio … UDP/TLS/RTP/SAVPF …` (+ `a=fingerprint`,
   `a=setup`, `a=ice-ufrag`/`a=ice-pwd`/`a=candidate`). `adapter._handle_inbound_invite`
   detects `offer.audio.is_webrtc` and takes the WebRTC branch (`_setup_webrtc_call`).
2. **Codec:** negotiated against the Opus-first WebRTC menu (`_WEBRTC_SUPPORTED_ENCODINGS =
   opus, PCMU, PCMA, telephone-event`), then clamped against the engine's capability table.
3. **ICE gather + answer:** `WebRtcMediaSession.prepare()` gathers ICE candidates and exposes
   our DTLS fingerprint, `a=setup` role (RFC 5763: `actpass`/`active` offer → we are
   `passive`; `passive` offer → we are `active`), and ICE creds/candidates. The SAVPF answer
   (`build_webrtc_answer`) carries them — **no `a=crypto`, no `c=`** (RFC 5763 §5) — and the
   `200 OK` is sent.
4. **ICE + DTLS handshake:** `run_handshake()` applies the peer's ICE creds/candidates, runs
   ICE, pumps the DTLS handshake over the ICE pipe (RFC 7983 first-byte demux), **verifies the
   peer's certificate fingerprint against the offered `a=fingerprint`** (RFC 5763 §5 — a
   mismatch aborts the call), and derives the inbound/outbound `SrtpSession` pair.
5. **Media:** `RtpMediaTransport` carries SRTP over the ICE pipe (no bound UDP socket). Opus
   is decoded at 48 kHz and **downsampled to 16 kHz** for the VAD/STT pipeline (Silero VAD
   accepts only 8/16 kHz); outbound TTS is resampled up to 48 kHz to encode.

## How to verify

- **Unit / handshake evidence (no live gateway):** `uv sync --extra webrtc --extra media` then

  ```
  uv run pytest tests/test_media_opus.py tests/test_media_engine_opus.py \
                tests/test_media_engine_ice.py tests/test_media_webrtc_session.py
  ```

  `test_media_webrtc_session.py` runs a **real DTLS-SRTP handshake** over an in-memory ICE
  pipe and asserts the role-mirrored SRTP cross-decrypts. The adapter branch (the full
  is_webrtc path) is exercised by `tests/test_adapter_webrtc.py` in the `hermes-contract` CI
  job (which installs hermes + webrtc + media + libopus).

- **Live (pending the operator's redeploy + a WebRTC client):** point a WebRTC client at the
  extension; in the operator log expect `WebRTC SDP answer built — setup=…`, `webrtc: DTLS-SRTP
  keyed (setup=…)`, `WebRTC media engine connected over ICE`, then `rtp tx/rx` lines. Two-way
  audio confirms the path.

## Security notes

- No DTLS private key, certificate, or SRTP key material is ever logged, `repr`'d, or raised
  in exception text (`media/dtls.py` / `media/webrtc_session.py`). The peer fingerprint is
  verified **before** any SRTP key is derived (`derive_srtp_sessions` enforces this).
- The DTLS certificate is **ephemeral, generated per endpoint at construction** — nothing is
  written to disk or committed.
- A failure at any step (no common codec, ICE failure, DTLS handshake failure, fingerprint
  mismatch) sends `488`/`500` and the call is **never half-answered** (rule 6).

## Rollback / disable

- **Disable WebRTC entirely:** there is no separate enable flag — WebRTC is driven by the
  offer profile. A peer that does not offer `UDP/TLS/RTP/SAVPF` never touches this path. To
  refuse WebRTC, do not point a WebRTC client at the extension (or omit the `webrtc` extra: a
  WebRTC offer then fails the call cleanly with a 488/ImportError, never dead audio).
- **STUN:** unset `HERMES_VOIP_ICE_STUN_URLS` to fall back to host-only ICE.
- This is a Python plugin (no provisioned infrastructure to tear down). STUN/TURN servers, if
  any, are external services the operator runs separately and are out of this plugin's scope.
