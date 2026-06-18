# ADR-0044: WebRTC video — pre-encoded H.264 file source, inbound discard

- **Date:** 2026-06-18
- **Status:** Accepted (supersedes the encoder/codec-library decision of ADR-0018
  §2 and §4, **and the `a=sendrecv` directionality of ADR-0018 §1d** — see §2a;
  ADR-0018's overall shape — additive `m=video`, pre-encode-and-loop, inbound
  discard, `m=video port 0` graceful decline — still holds)
- **Deciders:** agent session (WebRTC video lane) — operator direction

## Context

ADR-0018 designed video as an additive `m=video` track: pre-encode the outbound
source once, loop it on the wire, and discard inbound video (no decoder). That
design is sound. Its **encoder choice was not buildable**:

- The Python H.264 encoder bindings ADR-0018 named **do not exist on PyPI**:
  there is no installable `pyopenh264` and no `vp8codec` package that wraps the
  Cisco openh264 / libvpx encoders. The `video` extra ADR-0018 specified cannot be
  declared with real, pinned, lockfile-resolvable dependencies (rule 33/40).
- The remaining in-process route — a `ctypes` binding to the system
  `libopenh264.so` — **corrupts the process heap** (verified during this lane's
  investigation: in-process encode calls into the system openh264 produce heap
  corruption that crashes the interpreter). An in-process H.264 encoder is
  therefore not just unavailable but actively unsafe.

So the agent cannot encode H.264 in-process at all. But the operator requirement
— answer a video-capable call and present *something* to the caller — does not
require an in-process encoder, because the agent's outbound video is a **fixed,
pre-prepared source** (a static image or a short clip), not a live camera. That
source can be encoded **offline, ahead of time, by any tool the operator likes**
(ffmpeg, a phone, a hardware encoder) and delivered to the plugin as a plain
**H.264 Annex-B elementary-stream file**. The plugin then only has to
**packetise** (RFC 6184) and **send** it — no encoder in the process, ever.

## Decision

**Outbound WebRTC video is a pre-encoded H.264 Annex-B file**, read from
`HERMES_VOIP_VIDEO_SOURCE_PATH`, packetised per **RFC 6184** and sent over the
BUNDLE'd DTLS-SRTP video stream. **No H.264 encoder (and no codec library of any
kind) is imported into the process** — not at startup, not per call, not per
frame. **Inbound video is accepted in SDP but discarded** (no decode), exactly as
ADR-0018 §5a specified.

### 1. No in-process encoder — the hard constraint

The named encoder bindings do not exist and the system-library `ctypes` route
corrupts the heap (Context). This lane therefore introduces **zero codec
dependencies**: there is no `video` optional extra, no openh264, no libvpx, no
`av`/PyAV, no aiortc. `media/video_rtp.py` does pure byte manipulation
(NAL splitting, RTP framing) on bytes the operator supplied; a regression test
asserts the module imports **no** encoder/codec library, so a future change cannot
re-introduce the heap-corrupting path by accident.

### 2. Outbound source — `HERMES_VOIP_VIDEO_SOURCE_PATH`

- **Set** → the file is read at call setup, split into NAL units (Annex-B
  start-code framing, both 3- and 4-byte start codes), packetised per RFC 6184,
  and the resulting RTP packets are looped on the video SRTP stream (seq/ts
  rewritten per loop iteration; SPS/PPS + the first IDR re-sent at the start of
  each loop so a peer that joins mid-stream re-synchronises). The SDP answer's
  video m-line is **`a=sendonly`** — we contribute a track but do **not** solicit
  inbound video (see §2a).
- **Unset** → the answer's video m-line is **`a=inactive`** (we keep the m-line so
  BUNDLE stays intact, but advertise no media flow) and **no video sender task is
  started**. RFC-correct for a BUNDLE offer: the m-line count and order are
  preserved (RFC 8843 requires the answer mirror the offer's m-lines), but the
  agent sends nothing.

A call **with no `m=video`** is answered **exactly as today** — no video m-line is
added, no video code runs. This is the audio-only regression invariant.

### 2a. Direction is `a=sendonly` (sourced) / `a=inactive` (not) — never `a=sendrecv`

When a source is configured we answer **`a=sendonly`**, not `a=sendrecv`, even
though we have a track to contribute. The reason is a **silent-inbound-audio-outage
hazard on the shared BUNDLE 5-tuple**:

- audio and video share one ICE 5-tuple and one DTLS handshake (§4), but each
  SRTP stream is bound to a single SSRC (one ROC + replay state);
- the **inbound audio** `SrtpSession` is created **without a pre-bound SSRC**
  (`media/dtls.py` `derive_srtp_sessions` calls `SrtpSession.from_raw_keys` with
  no `ssrc=`), so it binds to the **first** inbound SSRC it authenticates;
- answering `a=sendrecv` on video invites the gateway to **send inbound video** on
  that same 5-tuple. If an inbound video packet arrives before the first inbound
  audio packet, the audio session binds to the **video** SSRC, and every later
  inbound audio packet then fails the SSRC check and is **silently dropped** — a
  one-way-audio call with no error surfaced.

Because the plugin **discards all inbound video anyway** (§5), there is no reason
to solicit it. `a=sendonly` tells the peer not to send video at all, which removes
the race entirely without needing to pre-bind the audio SRTP SSRC. An unset source
answers `a=inactive` (no flow in either direction). We **never** emit `a=sendrecv`
for video.

### 3. RFC 6184 packetisation

`media/video_rtp.py` owns it (parallel to `rtp.py`, not coupled to the audio
engine — different 90 kHz clock, different framing):

- **Single NAL unit** packet when a NAL fits the MTU payload budget.
- **FU-A** (type 28) fragmentation for a NAL larger than the budget: the FU
  indicator copies `F`/`NRI` from the NAL header and sets type 28; the FU header
  carries `S` on the first fragment, `E` on the last, and the original NAL type in
  its low 5 bits. The RTP **marker bit** is set on the last RTP packet of an
  access unit (the frame boundary) per RFC 6184 §5.1.
- **STAP-A** (type 24) aggregation is supported for bundling the small parameter
  sets (SPS+PPS) into one packet when they fit; an oversized NAL never goes into a
  STAP-A (it is FU-A'd instead).
- 90 kHz timestamp clock (RFC 3551 §4); the per-frame increment is
  `90000 / fps`.

The packetiser is pure and deterministic, with unit tests covering the FU-A
boundary cases (NAL exactly at, one over, and well over the MTU budget; first/last
fragment flags; marker placement; NAL-header F/NRI propagation) and the
single-NAL/STAP-A paths.

**Codec selection requires `packetization-mode=1`.** Because the packetiser
FU-A-fragments any large NAL, it can only honour an H.264 mode that permits FU-A
— i.e. RFC 6184 **packetization-mode=1**. `negotiate_video_h264` therefore selects
**only** an offered H.264 codec whose `fmtp` declares `packetization-mode=1`; it
**declines** (returns `None` → `a=inactive`, no video) when the offer carries only
`packetization-mode=0` codecs, or H.264 with no `packetization-mode` at all (whose
RFC 6184 §8.1 default is mode 0). Emitting FU-A under a mode-0 contract would be a
spec violation, so we answer no video rather than send packets the peer's
single-NAL-only depacketiser may reject.

### 4. Transport — reuse the BUNDLE'd DTLS-SRTP + ICE pipe

WebRTC offers carry `a=group:BUNDLE` with all DTLS/ICE credentials at the session
level (ADR-0042); audio and video share one ICE 5-tuple and one DTLS handshake
(RFC 8843). The video sender therefore needs **no second ICE gather and no second
DTLS handshake**: it reuses the audio call's already-connected `IceConnection`
(the `send(bytes)` datagram pipe) and an SRTP session derived from the **same**
DTLS handshake. Each pre-packetised `RtpPacket` is SRTP-`protect`-ed (the existing
`SrtpSession.protect` seam) and written to the ICE pipe — the identical seam the
audio engine uses, so no new transport machinery.

The video stream uses its own SSRC and payload type (distinct from audio), so the
peer demultiplexes audio vs video by SSRC on the shared 5-tuple (RFC 8843 §9.2).
The video SSRC is randomised **excluding** the fixed outbound audio SSRC
(`engine.OUTBOUND_AUDIO_SSRC`, `0xCAFEBABE`): the generator redraws on the
(astronomically rare) collision, so audio and video never share an SSRC and the
peer's per-SSRC demux is never ambiguous.

### 5. Inbound video — discard (ADR-0018 §5a unchanged)

Inbound video RTP is **not solicited** (the video m-line is answered `a=sendonly`
or `a=inactive`, never `a=sendrecv` — §2a) and **not decoded**. There is no decoder
dependency and no video receive loop wired in this lane — the agent has no use for
the caller's pixels (the future "1 fps vision snapshot", ADR-0018 §5b, remains
backlog and is explicitly **not** built here). Because we answer `a=sendonly`, a
conformant peer sends no inbound video at all, so the inbound-SRTP-binding race
(§2a) cannot occur; the audio engine drains and SRTP-unprotects only the inbound
audio stream it expects.

## Scope / deferred (rule 6)

- **WebRTC path only.** This lane wires video into the DTLS-SRTP / BUNDLE
  (`UDP/TLS/RTP/SAVPF`) path. The SDES / SIP-over-TLS video path (ADR-0018 §1a,
  separate `a=crypto` per video m-line, separate port) is **not** built here —
  named, not done.
- **Static/Annex-B file only.** The source is whatever H.264 Annex-B bytes the
  operator supplies (a single-frame still or a multi-frame clip both work — the
  packetiser does not care). Producing that file is an operator/offline step; the
  plugin never encodes.
- **No inbound decode** (ADR-0018 §5b vision snapshot stays backlog).
- **VP8** is not offered or packetised (ADR-0018 listed it; this lane is H.264
  only, matching what the test gateway offers — ADR-0042 recorded the gateway's
  `m=video` lines are H.264 `42E016`/`42E01F`).
- **H.264 `packetization-mode=0`-only offers are declined** (§3): we answer no
  video (`a=inactive`) rather than emit FU-A under a mode-0 contract.
- **The answer mirrors only the audio + (optional) video m-lines.** The
  `build_webrtc_answer` body emits exactly one `m=audio` and, when video is
  negotiated, one BUNDLE'd `m=video`. **Any additional m-line the offer carries —
  e.g. a second `m=video` (slides), `m=application` (SCTP/data-channel), or a
  duplicate `m=audio` — is NOT mirrored in the answer.** This is a deliberate
  scope limit (rule 6): the plugin negotiates one audio + one video stream and no
  data channel. A strict RFC 8843 answerer mirrors every offered m-line (rejecting
  unsupported ones with `port 0`); the test gateway (ADR-0042) tolerates the
  reduced answer, so the fuller mirror is **named, not built** here. Revisit if a
  gateway requires every offered m-line to be echoed.
- **RTCP for video** (SR/RR) is deferred until a gateway requires it.

## Consequences

- A WebRTC video offer is answered with a real (`sendonly`, when sourced) or inert
  (`inactive`, when not) `m=video` line instead of being dropped — the call is no
  longer rejected/re-INVITE'd for lack of a video answer, and the `sendonly`
  direction removes the inbound-SRTP-binding hazard (§2a).
- **Zero new dependencies**, zero licence/advisory surface (rule 35): no codec
  library enters the tree. The heap-corruption foot-gun is closed by construction
  and guarded by an import-assertion test.
- The operator owns video quality/size by choosing the encode parameters offline;
  the plugin's steady-state cost is a header rewrite + SRTP protect per packet
  (rule 22) — no encoder CPU at all.
- Audio-only and no-video calls are byte-identical to before (regression-tested).

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| In-process openh264 via `ctypes` to the system `.so` | **Corrupts the process heap** (verified) — unsafe at any cost. |
| `pyopenh264` / `vp8codec` PyPI bindings (ADR-0018) | **Do not exist on PyPI** — un-pinnable, un-lockable (rule 33/40). |
| `av`/PyAV or full `aiortc` | FFmpeg LGPL/GPL surface (rule 35), already excluded in ADR-0016/0018. |
| A pure-Python H.264 encoder | None production-grade exists; and unnecessary — the source is fixed and can be encoded offline. |
| Decode inbound video | No decoder dependency is safe/available, and the pixels are unused (ADR-0018 §5b is backlog). |
| Omit the video m-line entirely from the answer | A BUNDLE answer must mirror the offer's m-lines (RFC 8843); dropping it can fail negotiation. `a=inactive` is the RFC-correct "present but no media" answer. |
