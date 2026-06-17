# ADR-0018: Video call support — outbound looping clip, inbound discard, dual-transport

- **Date:** 2026-06-16
- **Status:** Accepted
- **Deciders:** agent session (video-adr design), operator direction

## Context

The plugin currently handles audio-only SIP calls over two transports: SIP-over-TLS with
SDES-SRTP (ADR-0013, live-validated) and WebRTC / SIP-over-WSS with DTLS-SRTP + ICE
(ADR-0016, designed). Gateway peers increasingly initiate calls that include a `m=video`
offer — they present a video track to the agent. Without a `m=video` answer the gateway
either rejects the call, terminates the call unexpectedly, or repeatedly re-INVITEs. The
operator requirement is therefore to accept and answer video-capable calls while presenting
**outbound video** (a static image or looping short clip) so the caller sees something. The
agent has **no camera** and no real-time video source; incoming video from the caller is
received at the SDP level but is **unused** (no decode dependency).

The same seam discipline that kept WebRTC transport-agnostic (ADR-0016) applies: video
support must compose with both the SIP-over-TLS (SDES-SRTP, `RTP/SAVP`) and the WebRTC
(DTLS-SRTP + ICE, `UDP/TLS/RTP/SAVPF`) transport paths without altering audio, the
adapter, the call loop, or the conversational plane (STT ↔ agent ↔ TTS). Audio-only calls
must continue to work unchanged — video is strictly additive.

Key constraints in force:

- **Rule 35 / zero-copyleft gate.** No GPL or LGPL-linked library may appear in the
  dependency tree. Full `aiortc` is already excluded (ADR-0016) because it pulls in
  `av`/PyAV → FFmpeg (LGPL-2.1+). Any video codec library must be audited to the same
  standard.
- **Rule 36 / no paid services.** The video pipeline must run in-process without calling
  external APIs.
- **Rule 40 / no undeclared dependencies.** The codec and encoding runtime choice is a
  decision this ADR must make and justify; it cannot be implied.
- **Rule 22 / efficiency.** The agent is not a video conferencing system; it presents a
  static or slow-looping source. The efficient design is to **pre-encode at startup** into
  a fixed set of RTP payloads, then loop them by rewriting only sequence numbers and
  timestamps — near-zero steady-state CPU, no real-time encoder on the hot path.
- **Rules 23/27.** This ADR records the design; no video sends are reported as done until
  implementation ships and a live call validates.

## Decision

**Add video as an additive `m=video` media track on top of both transports. Outbound video
is pre-encoded at startup (once) into a fixed RTP payload sequence that is looped on the
wire; inbound video RTP is acknowledged in SDP but dropped without decoding. Audio is
unchanged. Video is gracefully absent when the peer declines it (`m=video port 0` in the
answer).**

The sections below resolve each sub-decision.

### 1. SDP: the `m=video` line, codec offer, direction, and security profile

#### 1a. Transport profile per transport

The video media line uses the same transport profile as the audio media line — they share
the same security model within a single call:

| Call transport | Audio profile | Video profile |
| --- | --- | --- |
| SIP-over-TLS (SDES-SRTP, ADR-0013) | `RTP/SAVP` | `RTP/SAVP` |
| WebRTC (DTLS-SRTP + ICE, ADR-0016) | `UDP/TLS/RTP/SAVPF` | `UDP/TLS/RTP/SAVPF` |

For `RTP/SAVP`, the video `m=` section carries its own `a=crypto` line with a **separate
SRTP master key||salt** (distinct from the audio stream's key). For `UDP/TLS/RTP/SAVPF`,
the video `m=` section includes the same `a=fingerprint`, `a=setup`, and ICE attributes as
audio (under BUNDLE, section 1c, they share a single DTLS session and ICE component).
Plain `RTP/AVP` is not offered for video (consistent with the audio encryption invariant —
no plaintext media).

#### 1b. Payload types and codec lines

The plugin offers, in preference order:

```
m=video <port> <profile> 97 98
a=rtpmap:97 H264/90000
a=fmtp:97 profile-level-id=42e01e;packetization-mode=1;level-asymmetry-allowed=1
a=rtpmap:98 VP8/90000
a=sendrecv
```

- **PT 97** — H.264 Constrained Baseline Profile (CBP), Level 3.0
  (`profile-level-id=42e01e`). Dynamic payload type 97 is the conventional dynamic range
  start for video when telephony PT 96 is avoided. `packetization-mode=1` enables
  non-interleaved FU-A fragmentation (required for frames larger than MTU); the answerer
  may signal `packetization-mode=0` and we accept that (single-NAL-unit only, forces
  smaller resolution/bitrate). `level-asymmetry-allowed=1` per RFC 6184 §8.1 allows the
  decoder-side to declare a lower level; we set it so gateways that decode at a lower level
  can accept our level 3.0 source.
- **PT 98** — VP8 (RFC 7741), offered as a WebRTC secondary. VP8 is mandatory-to-implement
  in WebRTC (RFC 7742 §5) so any WebRTC endpoint can accept it; SIP endpoints that do not
  support VP8 simply ignore PT 98 in their answer.

Both use a **90000 Hz RTP clock** (the standard video clock rate, RFC 3551 §4).

#### 1c. BUNDLE for WebRTC

On the WebRTC transport the offer includes `a=group:BUNDLE audio video` and a matching
`a=mid:` in each media section. ICE and the DTLS handshake run once on the audio component;
the video component is multiplexed onto the same 5-tuple. This is mandatory-to-implement
for WebRTC endpoints (RFC 8843) and eliminates a second ICE gathering + DTLS handshake for
video. SDES/SIP-over-TLS calls use separate ports per media section (no BUNDLE; BUNDLE is
a WebRTC-specific grouping).

#### 1d. Directionality — `a=sendrecv` vs `a=sendonly`

The agent sends video (the looping clip) and receives video (the caller's camera feed) at
the SDP level, but **the inbound video payload is immediately discarded** (section 5). The
correct direction attribute is therefore **`a=sendrecv`**, not `a=sendonly`. Rationale:

- `a=sendonly` tells the peer not to send video at all. Some gateways and SIP endpoints
  interpret this strictly and drop their video track; that is operationally fine, but it
  can also trigger `m=video port 0` rejection on peers that require a bidirectional video
  session to proceed.
- `a=sendrecv` is the standard for a video call where both parties contribute a track.
  It is exactly what a human endpoint would offer, maximises interoperability, and imposes
  no additional cost — the inbound UDP datagrams for the video `m=` section are received
  by the socket and immediately discarded before any decode step (section 5).
- The RTP clock and sequence infrastructure for the video send direction is required
  regardless of direction; the receive infrastructure is a UDP socket already in place.

The agent answers a peer offer's `a=sendonly` video section with `a=recvonly` (per RFC
3264 §6.1 direction mirroring), which tells the peer to keep sending video to us (we still
discard it) while we stop sending video in that direction. The agent answers a peer's
`a=sendrecv` offer with `a=sendrecv` (mirrored: sendrecv→sendrecv).

#### 1e. Graceful video-declined fallback

When the peer answers with `m=video 0` (port zero) — signalling it does not accept video —
the plugin treats the video track as inactive: no RTP is sent, no video socket is opened,
and the call proceeds as audio-only. `sdp.py` already parses port 0 as a rejected section
(it returns `None` for that media block); `build_video_answer` accepts a `declined: bool`
flag that emits `m=video 0 <profile> 0` when `True`. No restart or error: a port-0 answer
is a normal SDP negotiation outcome.

### 2. Codec selection and licence justification

#### Primary — H.264 Constrained Baseline Profile via openh264

The primary codec is **H.264 CBP**, encoded by **openh264** (Cisco Systems, BSD-2-Clause).
openh264 is the only production-grade H.264 encoder with a licence compatible with the
zero-copyleft gate:

| Library | H.264 licence | Verdict |
| --- | --- | --- |
| **libx264** | GPLv2+ | **BANNED** — GPL, copyleft |
| **openh264** | BSD-2-Clause (Cisco) | **Permitted** |
| **FFmpeg** (H.264 via libx264) | LGPL-2.1+ (with libx264: GPL) | **BANNED** |
| **PyAV / av** (FFmpeg wrapper) | BSD-3-Clause wrapper, but bundles FFmpeg | **BANNED** |

openh264 ships a pre-built binary (`.so`/`.dylib`/`.dll`) downloadable from Cisco's CDN
under the Cisco Binary Code Licence, which permits use in applications but is not
open-source itself. For an in-process Python runtime, the Python binding
**`pyopenh264`** (MIT, wraps the openh264 shared library via ctypes) is the integration
path. However, **for v1 (the pre-encode path, section 4), openh264 is needed only at
startup for a one-time encode step** — it is not on the per-RTP-packet hot path. It is
declared in a new **`video` optional extra** alongside the VP8 library, isolated from the
default install (same pattern as `media` and `webrtc`).

**Licence gate summary for openh264:**
- `pyopenh264`: MIT (Python binding).
- openh264 binary: Cisco BSD-2-Clause (permissive).
- The Cisco binary CDN licence grants use but is not OSI-certified open-source; this is
  documented here as the known limitation. The binary is downloaded at runtime (not
  committed to the repo) by `pyopenh264` on first use — no binary blob in git.

#### WebRTC secondary — VP8 via libvpx / vp8codec

**VP8** is the RFC 7742 mandatory-to-implement WebRTC video codec and carries no codec
licence encumbrance. The Python binding **`vp8codec`** (wraps `libvpx`, BSD-3-Clause) or
direct `ctypes` bindings to `libvpx` (BSD-3-Clause, maintained by the Alliance for Open
Media / Google) are the integration paths:

| Library | VP8 licence | Verdict |
| --- | --- | --- |
| **libvpx** | BSD-3-Clause | **Permitted** |
| **vp8codec** (Python binding) | BSD-3-Clause | **Permitted** |

Like openh264, libvpx is used only at startup for pre-encoding (section 4). It is bundled
in the `video` optional extra.

#### Why not a pure-Python encoder?

No production-grade pure-Python H.264 or VP8 encoder exists. The pre-encode approach
(section 4) means the encoder runs only once at startup — the runtime performance of a
pure-Python encoder is irrelevant — but correctness and standards compliance are
paramount for gateway interop. openh264 and libvpx are reference-class implementations of
their respective standards.

### 3. RTP video packetisation

Video packetisation is **separate from the existing audio engine**. A new
`src/hermes_voip/media/video_rtp.py` owns it.

#### 3a. H.264 — RFC 6184 (RTP Payload Format for H.264 Video)

H.264 video is delivered as a stream of NAL units (Network Abstraction Layer units). The
three relevant packetisation modes:

- **Single NAL unit (STT-A, mode 0):** one RTP packet = one NAL unit. Used when the NAL
  unit fits within MTU (≤ 1200 bytes). `packetization-mode=0` in `a=fmtp` restricts the
  sender to this mode.
- **STAP-A (Single-Time Aggregation Packet type A):** bundle multiple small NAL units into
  one RTP packet (SPS + PPS + small IDR slice fit together). Not required for pre-encoded
  clip playback but correct to produce for the SPS+PPS header.
- **FU-A (Fragmentation Unit type A):** fragment one large NAL unit across multiple RTP
  packets. Required for IDR frames larger than MTU (`packetization-mode=1`). Each FU-A
  packet header: `[FU indicator byte: F=0, NRI=<from NAL>, type=28] [FU header: S/E/R/type]`.
  The first fragment sets `S=1`; the last sets `E=1`. The RTP **marker bit** (`M=1`) is
  set on the last RTP packet of an access unit (a complete frame) — critical for receiver
  re-assembly.

**Timestamp clock:** 90000 Hz. For a 10 fps source, each frame advances the timestamp by
9000 ticks (90000 / 10 = 9000). For 15 fps: 6000 ticks. The timestamp is relative to the
start of the RTP session (not wall time).

**SPS/PPS refresh:** The SPS (Sequence Parameter Set) and PPS (Picture Parameter Set) NAL
units describe the video stream parameters that a decoder needs to begin decoding. For a
looping clip, the SPS and PPS are stable (they are the same for every loop iteration). The
plugin re-sends the SPS and PPS (followed immediately by the first IDR frame) at the start
of each loop iteration and additionally every 30 seconds — a forced IDR / SPS/PPS refresh
so a peer that joins mid-stream or experiences packet loss can re-synchronise. This is
standard practice for streaming H.264 over RTP.

#### 3b. VP8 — RFC 7741 (RTP Payload Format for VP8 Video)

VP8 RTP payload descriptor (RFC 7741 §4.2):

```
 0                   1
 0 1 2 3 4 5 6 7 0 1 2 3 4 5 6 7
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|X|R|N|S|R| PID |               |
+-+-+-+-+-+-+-+-+
```

- `S=1` on the first partition of each frame.
- `N=0` (reference frame).
- The **marker bit** is set on the last packet of each frame.
- **Timestamp** increments by `90000 / fps` per frame (same 90 kHz clock as H.264).
- Partition ID (`PID`) cycles 0–7 per frame.

VP8 frames are typically small enough that the looping frame set is a compact binary. The
pre-encode approach (section 4) applies identically.

### 4. Outbound video source pipeline — pre-encode strategy

#### Decision: pre-encode at startup, loop on the wire

The agent has no real-time video source. The outbound video is either:
- a **static image** (JPEG or PNG), configured via `HERMES_VOIP_VIDEO_SOURCE_PATH`; or
- a **short looping clip** (raw YUV frames or a container-less sequence), configured via
  the same env var pointing at a `.yuv` file or a directory of YUV frames.

**Both are pre-encoded at `RtpVideoTransport` construction time** into a fixed list of
`bytes` objects — one per RTP packet — ready to transmit. The send loop rewrites only the
**RTP sequence number** (modulo 2^16) and **RTP timestamp** (modulo 2^32, advancing by the
appropriate 90 kHz increment per frame) on each packet before transmission. SRTP protection
is applied after rewriting (the SRTP context authenticates the final wire bytes including
the updated header).

Why pre-encode?

- **Near-zero steady-state CPU.** Once the packet list is built, the hot path is:
  rewrite 8 bytes (seq + ts) in a `bytearray`, optionally SRTP-protect, UDP-sendto.
  No encoder is running, no YUV conversion, no NAL unit fragmentation, no frame queue.
  The encoder (openh264 / libvpx) runs once and is not imported at all after startup
  completes.
- **No real-time encoder dependency.** For v1, the `video` extra is only loaded at
  startup. The steady-state process holds no encoder state, no codec context, no GPU
  handle. An encoder crash or update cannot affect a running call.
- **Deterministic packet sizes.** Pre-encoded packets have fixed sizes; jitter is bounded
  and predictable.
- **Correctness by construction.** SPS/PPS and IDR NAL units are valid once and reused
  exactly; there is no risk of encoder state diverging between calls.

The pre-encoded packet list for a static image:
```
[SPS packet(s)] [PPS packet(s)] [IDR slice FU-A packets…]
```
repeated with the SPS/PPS refresh interval. For a short clip (N frames):
```
[SPS][PPS][IDR FU-A…][P-frame FU-A…] * N
```
The loop repeats from the SPS/PPS IDR on each iteration.

#### Encoder parameters (size and rate caps)

| Parameter | Default | Env var |
| --- | --- | --- |
| Resolution | QCIF (176×144) | `HERMES_VOIP_VIDEO_WIDTH` / `HERMES_VOIP_VIDEO_HEIGHT` |
| Frame rate | 10 fps | `HERMES_VOIP_VIDEO_FPS` |
| Max bitrate | 128 kbps | `HERMES_VOIP_VIDEO_BITRATE_KBPS` |
| Codec | h264 | `HERMES_VOIP_VIDEO_CODEC` (`h264` or `vp8`) |
| Source path | none (no video) | `HERMES_VOIP_VIDEO_SOURCE_PATH` |

QCIF at 10 fps at 128 kbps is the conservative interop floor: every SIP video endpoint
(softphone, IP phone, SBC, conferencing bridge) that supports H.264 at all supports H.264
CBP at QCIF. CIF (352×288) and 15 fps are available via the env vars for peers that
negotiate them; larger resolutions and higher frame rates are **not supported in v1** to
bound bandwidth and pre-encode time.

If `HERMES_VOIP_VIDEO_SOURCE_PATH` is unset, the plugin offers `m=video` in the SDP but
sends **comfort video only** — a single-colour black frame pre-encoded as a minimal IDR, to
satisfy the SDP contract without a real image. This keeps the `m=video` handshake working
while the operator has not yet configured a video source.

#### Bandwidth accounting

At QCIF 10 fps 128 kbps H.264 + ~20% RTP overhead ≈ **154 kbps** for video, plus the
audio stream (G.711 PCMU 64 kbps + overhead ≈ 77 kbps). Total ≈ 231 kbps, well within the
1 Mbps floor of any modern SIP trunk.

### 5. Inbound video

#### 5a. v1 scope — receive-and-discard

Inbound video RTP (the caller's camera feed) is, in v1, **received at the UDP socket level
and immediately discarded** — no decode, no buffer, no codec context. This is the v1-scope
behaviour and it keeps the critical path clean: no decoder runs while the core audio and
outbound video paths are brought up. The discard happens in the `RtpVideoTransport` receive
path before any payload inspection:

```python
# receive loop for the video socket / BUNDLE demux
async def _video_inbound_loop(self) -> None:
    while not self._stop.is_set():
        datagram, _ = await self._video_queue.get()
        # Inbound video is unused — payload is dropped after optional SRTP unprotect
        # (to maintain the SRTP replay window and prevent replay attacks).
        if self._srtp_video_in is not None:
            with contextlib.suppress(SrtpError):
                self._srtp_video_in.unprotect(datagram)
        # Payload never inspected beyond this point.
```

**Why unprotect inbound SRTP even though the payload is discarded?** The SRTP replay window
(RFC 3711 §3.3) must advance with received packets. If inbound SRTP packets are silently
dropped without unprotect, the replay window stagnates and the next genuine packet may be
rejected as a replay. Calling `unprotect` (and suppressing `SrtpError` for tampered
packets) correctly advances the window. On a WebRTC/DTLS-SRTP call the SRTP session for
video inbound is derived from the same DTLS handshake as audio (BUNDLE), so the session
already exists; calling `unprotect` is one function call per received packet with no
additional memory allocation.

**No decoder dependency.** In v1 this is the principal design benefit: the `video` extra
contains the encoder (openh264 / libvpx), used only at startup. No decoder library is
imported at any point. A rogue or corrupted video stream from the peer cannot crash a
decoder or exploit a decoder vulnerability.

#### 5b. Backlog / future phase — inbound video → agent vision snapshot

> **Status: Backlog / future phase — ships after core audio + outbound video are working.**
> This subsection records design intent only; it is not part of v1 and is not built. The
> detailed tracking is in backlog issue #64 ("Inbound video → agent vision (1 FPS
> snapshot)").

A later phase gives a **vision-capable Hermes agent the ability to "see" the call on
demand**. Motivating use case (operator-stated): the agent answers a video intercom and can
see that someone is standing at the door holding a package — it glances at the scene, it
does not watch a video stream.

**Design intent:**

- **Decode the inbound caller video, throttled to ~1 frame/second**, and write the latest
  decoded frame as a still image to a known, configurable location. The throttle is the
  core idea: the agent only needs to *glance*, not stream. Decoding one frame per second
  (dropping the other ~9–14 frames/s of a 10–15 fps stream without decoding them) keeps CPU
  negligible and **bounds the decoder CVE surface** — the decoder touches one keyframe-class
  frame per second rather than every packet of every frame. A decode error on any frame is
  logged and skipped (the previous snapshot stays); a corrupt stream degrades to "no fresh
  snapshot", never a crash.
- **Latest-frame snapshot, overwritten each cycle.** Only the most recent decoded frame is
  kept on disk — each ~1 Hz decode overwrites the single snapshot file. No frame history,
  no ring buffer, no recording.
- **Configurable, runtime-only path.** `HERMES_VOIP_VIDEO_SNAPSHOT_PATH`, defaulting to a
  generic per-process runtime directory (e.g. under the OS temp/runtime dir, resolved at
  start) — **never a tracked or public path**, never inside the repo. Tests use a fake
  temp path, exactly as the rest of the suite uses `pbx.example.test` / ext `1000`.

**Decoder — reuse the same zero-copyleft libraries.** The decode path adds **no new codec
licence surface**: H.264 decode via **openh264** (Cisco, BSD-2-Clause) and VP8 decode via
**libvpx** (BSD-3-Clause) — the very libraries the outbound encoder already brings in the
`video` extra. No GPL/LGPL decoder (no libx264, no FFmpeg/PyAV) is introduced.

**Snapshot still-image encode.** Writing the decoded frame as a still requires turning a
raw decoded YUV/RGB frame into an image file. Two options, decision deferred to the build:

- **Pillow** (PIL fork) — license **HPND** (permissive, BSD-style, zero copyleft; clears
  rule 35). Mature, handles YUV→RGB→JPEG/PNG cleanly, already a common dependency. The
  natural default for "decoded frame → `snapshot.jpg`".
- **Raw YUV→JPEG without Pillow** — write a baseline JPEG from the decoded plane directly
  (or emit a `.yuv`/`.ppm` and let the agent layer convert). Avoids a new dependency
  entirely but reimplements colour conversion + JPEG entropy coding, which is more code and
  more risk for marginal benefit.

The expected pick is **Pillow (HPND)** — permissive, correct, and far less code than a
hand-rolled JPEG encoder — declared in the `video` extra alongside the codecs when this
phase lands. (Final choice and pin happen in the implementing PR.)

**Agent-access seam (design, not built).** Two ways the agent reaches the snapshot, both
recorded here so the build can choose:

- **A gated "voip vision" tool** — a tool registered alongside the existing verbs in
  `tools.py` (subject to the same injection-guard gating, ADR-0009) that, when the agent
  calls it, reads the current snapshot file and returns the image to the model. This is the
  "glance on demand" shape: the agent decides when to look.
- **Attach the snapshot to the delivered turn** — include the latest snapshot with the
  finalized inbound turn handed to Hermes, so a vision-capable agent sees it inline without
  an explicit tool call.

Both are viable; the choice is left to the build (the tool form fits the "agent glances on
demand" intent more cleanly and matches the existing `tools.py` pattern, but the per-turn
attachment may suit some agent configurations). Either way, the media plane's job is only to
keep the single snapshot file fresh at ~1 Hz; how the agent consumes it is an upper-layer
seam.

**Privacy.** The snapshot is **runtime-only**, written to a **gitignored / non-public**
location, and **overwritten on every decode cycle** — there is no accumulation, no
recording, and nothing committed. The path is operator-configurable so it can point at an
ephemeral/tmpfs location.

**Efficiency (rule 22) for this future phase.** At ~1 decoded frame/second of a QCIF/CIF
keyframe-class frame, decode cost is a small fraction of one core (orders of magnitude below
a full-rate decode), plus one image-encode + file write per second. Memory: one decoded
frame buffer + the snapshot file; no history. This is why the throttle is intrinsic to the
design, not an afterthought — it is what keeps both CPU and CVE exposure negligible.

### 6. Integration seam: how video slots into the existing architecture

#### 6a. New module: `src/hermes_voip/media/video_rtp.py`

`RtpVideoTransport` is a new class parallel to `RtpMediaTransport` (audio). It is NOT a
subclass — the two have different packet clocks (audio 8000 Hz, video 90000 Hz), different
codec mechanics, different packetisation paths, and only one direction of useful payload.
Both satisfy a thin `VideoTransport` Protocol (analogous to `MediaTransport`):

```python
class VideoTransport(Protocol):
    async def connect(self) -> bool: ...
    async def stop(self) -> None: ...
    # No send_video / inbound_video in the Protocol:
    # video is internal to RtpVideoTransport (pre-encoded loop, discard).
```

The `VideoTransport` Protocol is intentionally minimal — callers only need to start and
stop it; the looping send task runs autonomously inside.

#### 6b. SDP changes: `sdp.py` gains `VideoMedia` and `build_video_answer`

A new `VideoMedia` dataclass (parallel to `AudioMedia`) holds the parsed `m=video` section:
port, protocol, offered codecs (H.264 / VP8 + their `fmtp` params), crypto attrs (SDES
path), and DTLS/ICE attrs (WebRTC path). `negotiate_video` selects the intersection of
offered and supported codecs in offer order (same discipline as `negotiate_audio`).
`build_video_answer` emits the `m=video` answer line with the chosen codec, the security
profile, and `a=sendrecv`. When the plugin declines video (no source configured and
`allow_comfort_video=False`), it emits `m=video 0 <profile> 0`.

The `SessionDescription` parser is extended to capture the first `m=video` section
alongside the existing `m=audio` capture. All existing audio parsing paths are unchanged.

#### 6c. `RtpMediaTransport` (audio engine) is unchanged

The audio engine does not know about video. Video sends and receives are entirely in
`RtpVideoTransport`. The hold logic (`on_hold`), SRTP sessions, comedia latch, and pacing
are independent per-media-type — this is the clean separation.

#### 6d. Adapter integration seam

`adapter.connect()` (the call setup path) currently builds `RtpMediaTransport` (audio) and
wires its SRTP sessions. Video extends this with:

1. Parse `m=video` from the offer (via `SessionDescription.video`).
2. If the offer includes `m=video` and the plugin has video support enabled:
   - Bind a video UDP port (or reuse the audio BUNDLE socket on WebRTC).
   - Build `RtpVideoTransport` with the pre-encoded packet list (loaded at process start).
   - For SDES path: derive a separate `SrtpSession` pair from the video `a=crypto` line.
   - For DTLS/BUNDLE path: reuse the DTLS-derived sessions from `media/dtls.py`, split
     by SSRC.
   - Include `m=video` in the SDP answer via `build_video_answer`.
3. If the offer has no `m=video`, or if the negotiation yields no common video codec: omit
   video from the answer (audio-only call proceeds unchanged).
4. `CallSession` holds an optional `RtpVideoTransport | None`; it is started with the audio
   transport and stopped with it. `CallLoop` does not reference video at all — the
   conversational plane is audio-only by design.

#### 6e. Testing strategy (implementation PR, not this ADR)

The test suite for video follows the TDD discipline of prior PRs:

- **Unit: `sdp.py` additions.** Round-trip parse+build of `m=video` lines for both SDES
  and WebRTC profiles; declined video (`port 0`); `negotiate_video` codec intersection;
  `fmtp` parameter extraction for `profile-level-id`/`packetization-mode`.
- **Unit: `video_rtp.py` packetiser.** Pre-encoded packet list structure; FU-A
  fragmentation of a synthetic IDR; marker bit placement; timestamp advance; STAP-A for
  SPS+PPS; seq/ts rewrite on loop iteration; SRTP protection applied post-rewrite.
- **Unit: `RtpVideoTransport`.** Inbound discard loop (no decoder called); pre-encode stub
  that generates a known packet list; stop signal; SRTP replay-window advance via
  `unprotect`.
- **Integration: adapter with video.** End-to-end INVITE → answer → video transport start
  → stop, with stubs for the pre-encoded payload and injected clocks (no wall-clock waits).
  Audio unchanged when `m=video` is absent; audio unchanged when `m=video port 0` in
  answer.
- **Live validation** (PR-D, see section 7): a real call with the test gateway at
  `pbx.example.test`, ext `1000`-style fake in tests; the live call uses the real gateway
  credentials from `.env` / 1Password.

### 7. Phasing — what ships in v1 vs later

#### v1 (first video PR sequence)

- `sdp.py` `VideoMedia` + `build_video_answer` + `negotiate_video` + `SessionDescription.video`.
- `video_rtp.py` H.264 CBP packetiser (FU-A + STAP-A for SPS/PPS; marker bit; 90 kHz
  clock; seq/ts loop-rewrite).
- Pre-encode pipeline: static image → openh264 → pre-encoded RTP packet list at startup.
- Inbound video discard loop (SRTP replay-window advance, no decoder).
- SDES-SRTP video keying (separate `a=crypto` per the video `m=` section).
- Audio-only graceful path: `m=video` absent in offer → no video transport opened.
- Declined-video graceful path: `m=video port 0` in answer → no video transport opened.
- Comfort video (black frame) when `HERMES_VOIP_VIDEO_SOURCE_PATH` is unset.
- `video` optional extra: `pyopenh264` (MIT / openh264 BSD-2-Clause).

**Not in v1:**
- VP8 (listed in the offer; supported in the packetiser design; but not wired to a VP8
  encoder in v1 — the answer downgrades to H.264 if both are offered, or accepts VP8 if
  H.264 is not in the offer. Encoding VP8 frames requires `vp8codec`/libvpx wired to the
  pre-encode step; this is deferred to PR-E).
- Looping clip (multi-frame source); v1 accepts only a single still image.
- WebRTC BUNDLE video (DTLS-SRTP shared session + BUNDLE demux for video); v1 supports
  video only on the SIP-over-TLS / SDES-SRTP path. WebRTC video is the next phase.
- RTCP for video (SR/RR); deferred until a gateway requires it.
- CIF or higher resolution; v1 caps at QCIF 176×144.
- Live looping clip; v1 is static image only.

#### Phase 2 (follow-up PRs, not blocking v1)

- VP8 encoder wired (`vp8codec`/libvpx added to `video` extra).
- WebRTC BUNDLE video (DTLS-SRTP shared session, first-byte demux extended to route video
  SSRC correctly).
- Looping short clip source (directory of YUV frames → multi-IDR pre-encoded sequence).
- RTCP Sender Report for video (maintains RTP/NTP timestamps for gateway sync).
- CIF resolution option (352×288) via env var.
- **Inbound video → agent vision snapshot** (§5b): decode inbound at ~1 fps → latest-frame
  still at a configurable runtime path → vision-capable agent reads it (gated tool or
  per-turn attachment). Decoder reuses openh264/libvpx (zero-copyleft); still-image encode
  via Pillow (HPND). Tracked in the backlog issue "Inbound video → agent vision (1 FPS
  snapshot)"; depends on core audio + outbound video landing first.

## Consequences

- **Easier:** video calls from peers that include `m=video` are answered gracefully instead
  of causing unexpected call termination or repeated re-INVITEs. The pre-encode approach
  means video has **no steady-state CPU cost** beyond the header rewrite and SRTP protect
  per packet (≤ 50 packets/s at 10 fps QCIF). The `video` optional extra keeps the default
  install lean (no codec library loaded unless video is enabled). The inbound-discard design
  eliminates all decoder CVE surface.
- **Harder / committed to maintain:** the `video` extra adds openh264 (via `pyopenh264` and
  the Cisco binary CDN fetch) and optionally libvpx as new dependencies — their licence,
  API stability, and binary availability must be reviewed on upgrade. The SDP parser grows
  a `VideoMedia` path and `build_video_answer`; these are additive but must be kept in sync
  with SDP standard evolution (new codec `fmtp` fields, new profiles). The pre-encoded
  packet list must be regenerated if the codec parameters change (env var change → restart).
- **Efficiency (rule 22):** Startup pre-encode for a QCIF still image takes < 200 ms on
  any modern CPU (openh264 is a fast software encoder; QCIF is 25 344 pixels). The
  resulting packet list is ≤ a few hundred packets (< 1 MB in memory). Steady-state: at
  10 fps QCIF, 50–80 packets/s for video RTP (150 kbps), each requiring an 8-byte header
  rewrite + SRTP protect (AES-CM, ≤ 1200 bytes/packet ≈ 96 kbps AES throughput demand).
  On any modern CPU capable of running the Python event loop, AES-CM at < 200 kbps is
  negligible (well under 0.5% of a single core). Memory: pre-encoded list + SRTP context
  per active call ≤ 5 MB/call.
- **Bandwidth:** QCIF 10 fps H.264 CBP 128 kbps + 20% RTP overhead ≈ 154 kbps per active
  call. Audio adds ≈ 77 kbps (G.711 PCMU). Total per-call ≈ 231 kbps — within the budget
  of any standard SIP trunk.
- **Lock-in / cost:** no vendor API, no cloud, no paid codec licence. openh264 is Cisco
  BSD-2-Clause (the Cisco binary CDN is free to use per the Binary Code Licence); libvpx
  is BSD-3-Clause. No recurring cost.

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| **libx264** for H.264 encoding | GPL-2.0+ — directly violates rule 35 (zero-copyleft gate). |
| **FFmpeg / PyAV** for encoding | FFmpeg is LGPL-2.1+; when linked with libx264, GPL-2.0+. Already excluded in ADR-0016. |
| **aiortc** full video stack | Hard-depends on `av`/PyAV → FFmpeg (LGPL-2.1+). Already excluded in ADR-0016. |
| **Real-time H.264 encoder on hot path** | Runs openh264 per frame per call — CPU scales with concurrent calls and frame rate. The pre-encode approach achieves near-zero steady-state cost for the same output (the looping static/clip source does not change between calls). Only justified when a live dynamic video source is required (not the agent use case). |
| **`a=sendonly` direction** | Prevents peers that require `sendrecv` video from connecting; adds no cost saving (the inbound discard is a one-line drop, not decoding). `a=sendrecv` maximises interoperability (section 1d). |
| **Offer `m=video` with port 0 always** (disabled from the start) | Defeats the purpose: a peer that sees `m=video 0` immediately knows the endpoint offers no video and may render a grey box or fall back to audio-only display, degrading UX. Offering video with a real port makes the agent appear as a normal video endpoint. |
| **Decode inbound video (even to /dev/null)** | Requires a decoder library (libx264 for H.264 decode = GPL; libvpx for VP8 decode = BSD-3-Clause, but still a runtime dependency). Decoding adds CPU, memory, and CVE surface with zero benefit — the pixels are never used. The SRTP unprotect call (already required for the replay window) is the only correct operation on inbound video data. |
| **VP8 as primary codec (H.264 as secondary)** | VP8 is mandatory-to-implement for WebRTC peers (RFC 7742) but optional for SIP endpoints and SIP/TLS-only gateways. H.264 CBP has broader SIP interop (supported by virtually every SIP video endpoint made after 2010). Offering H.264 first and VP8 second gives SIP peers their preferred codec and WebRTC peers their mandatory codec. |
| **Separate ICE component for video (no BUNDLE on WebRTC)** | Requires a second ICE gathering pass, a second DTLS handshake, and a second UDP port — all for a one-way static stream. BUNDLE is mandatory-to-implement for WebRTC (RFC 8843) and eliminates this overhead entirely. Keeping video in the BUNDLE group is correct and efficient. |
| **Commit a pre-encoded binary asset to the repo** | Binary assets in git are not human-reviewable, conflict with rule 27 (no aspirational content), and embed a specific resolution/codec version permanently. The correct model is encode-at-startup from a committed source image (PNG/JPEG). |
