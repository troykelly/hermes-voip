# ADR-0022: G.722 wideband — negotiate-best-available, vendored public-domain codec

- **Date:** 2026-06-17
- **Status:** Accepted (codec-order selection superseded in part by ADR-0078)
- **Deciders:** agent session (G.722 wideband lane)

> **Superseded in part (ADR-0078, 2026-06-26):** where this ADR states the offer/answer
> machinery "honours the offer's preference order", that no longer holds —
> `negotiate_audio` now orders the answer by OUR menu preference (the answerer's
> preference, RFC 3264 §6.1). The G.722-preferred menu and the G.711 fallback are
> unchanged; only the ordering of the negotiated result changed. See ADR-0078.

## Context

ADR-0005 mandates "negotiate by capability, prefer wideband", but the implemented codec
menu has always been G.711-only (`adapter._SUPPORTED_ENCODINGS = PCMU/PCMA/telephone-event`)
and the media engine (`media/engine.py`) could carry only the two 8 kHz G.711 variants. The
RFC 3264 offer/answer machinery in `sdp.py` already parses the peer offer, intersects with
our supported list, honours the offer's preference order, and rejects with 488 when no
common voice codec exists — but it never had a wideband codec to negotiate. ADR-0017 pinned
the wire at 8 kHz and ADR-0007 (PR #82) made the ElevenLabs TTS request rate a single
codec-gated hook (`output_sample_rate`), explicitly so a wideband lane could generalise it.

PR #84 established the capability invariant we must follow: the engine's exhaustive,
rate-aware `_ENGINE_CODEC_TABLE` is the single source of truth for what the engine can
carry, and a drift-guard test asserts every advertised voice encoding maps to a runnable
engine codec. The rule is: **extend the engine encode/decode + table FIRST, then widen the
advertised menu — never the reverse.**

G.722 (ITU-T) is the natural first wideband step on the SIP path: it is a 16 kHz wideband
codec at the same 64 kbit/s as G.711, widely supported by RFC-compliant gateways, and it
lifts BOTH directions — STT accuracy (the recogniser wants 16 kHz natively, no longer
upsampled from 8 kHz) and TTS quality (no downsample of the synthesiser's wideband output to
8 kHz). G.711 remains the universal fallback.

### The G.722 RTP framing quirk (RFC 3551)

G.722's RTP **clock rate is 8000 Hz even though the audio is sampled at 16 kHz** — RFC 3551
§4.5.2 notes the value "was erroneously assigned… and must remain unchanged for backward
compatibility." The codec emits **one octet per input sample-pair** at 64 kbit/s, so a 20 ms
frame is **320 input samples → 160 G.722 bytes**, and the **RTP timestamp advances by 160**
per 20 ms frame (the 8 kHz clock), not 320. The SDP rtpmap is `G722/8000`. Any engine that
matched a codec on encoding name + a single assumed rate would mis-handle G.722; the
capability table is keyed on the `(encoding, clock_rate)` pair and the engine derives the
audio sample rate, samples-per-frame, chunk size, and RTP-timestamp increment from a per-codec
descriptor (so the 8000-clock/16000-sample split is handled in one place).

## Decision

### 1. Codec implementation — vendored, fully-typed pure-Python port (no new dependency)

Python's `audioop`/`audioop-lts` has **no G.722** (only G.711 + IMA-ADPCM + `ratecv`),
confirmed by inspection. We surveyed PyPI and the wider ecosystem (four independent research
passes + direct PyPI-JSON verification):

- The only suitable PyPI package is **`G722` 1.2.7** (sippy/libg722): Public-Domain + BSD-2,
  ITU-bit-exact, ships cp313 manylinux x86_64 wheels + sdist. But it is a **C extension**,
  has **no musl/macOS wheels** and **no declared `requires_python`**, and `media/engine.py`
  must import and run in the **CI base gate, which installs no optional extras** — so it would
  have to be a compiled **base** dependency. That adds a build/portability burden (musl,
  macOS, sdist-only platforms) against this project's minimal-dependency, no-lock-in posture.
- **No pure-Python G.722 exists** on PyPI or as a maintained library.
- The ITU-T reference C and the openitu/STL mirror are under the **"ITU-T General Public
  License" (a modified GPL) = copyleft** — incompatible with this permissive, public repo
  (rule 35/40). aiortc/PyAV route the codec through ffmpeg `libavcodec` (LGPL); spandsp core
  is LGPL/GPL. All disqualified.

**Decision:** vendor a **self-contained, fully-typed pure-Python port** of the
**public-domain** G.722 reference (Steve Underwood's public-domain dedication + CMU-1993
"completely unrestricted" notice, as packaged permissively by `sippy/libg722`) into
`src/hermes_voip/media/g722.py`. We implement the standard **64 kbit/s mode (8 bits/sample,
unpacked, non-test-mode)** — exactly RFC 3551 RTP G.722. This adds **zero runtime
dependency**, works in **every** CI environment (base + all extras), carries **no copyleft and
no vendor lock-in**, and stays clean under `mypy --strict` (the original is fixed-point integer
DSP, which ports to typed Python with no `Any`/`cast`).

**Provenance & licence:** the module header records the public-domain/CMU/BSD-2 origin
verbatim. Only the algorithm and the public-domain reference are used; the ITU modified-GPL
STL code and its conformance vectors are **not** vendored.

**Correctness verification:** the port is validated **bit-exact** against known-answer
fixtures (`tests/fixtures/g722/`) — a documented, regenerable synthetic 16 kHz signal encoded
→ G.722 bytes → decoded, produced by the **public-domain C reference** used as a build-time
oracle (NOT the ITU vectors). Tests assert encode output is byte-identical and decode output
is sample-identical to the reference, plus a round-trip fidelity floor (high correlation,
bounded error) on independent signals. The fixtures and the exact regeneration procedure are
documented so a future session can reproduce them from the public-domain reference.

### 2. Codec menu — G.722 first, G.711 fallback

`media/engine.py` gains `("G722", 8000): Codec.G722` in `_ENGINE_CODEC_TABLE` (added FIRST,
before the menu widens, so the drift guard stays green), with G.722 encode/decode wired to the
new module. The adapter then advertises `_SUPPORTED_ENCODINGS = ("G722", "PCMU", "PCMA",
"telephone-event")` and lists `G722` **first** (wideband-preferred) in the outbound INVITE's
`offer_codecs`. Inbound answer and outbound 2xx handling already flow through
`negotiate_audio`, which honours the peer's offer order and falls back to G.711 when G.722 is
not offered; an offer we cannot carry still 488s (PR #84's belt-and-suspenders engine-table
check is unchanged).

### 3. Pipeline rate follows the negotiated codec (both directions)

The media engine's **wire/sample rate becomes codec-derived**: 8 kHz for G.711, 16 kHz for
G.722. `inbound_sample_rate` reports the codec's true audio rate, and `send_audio` resamples
any TTS frame to that rate (it already did this for the 24 kHz synthesiser → 8 kHz).

- **STT:** the inbound G.722 audio reaches the recogniser at its native 16 kHz (the engine
  yields 16 kHz frames; the STT seam already passes 16 kHz through and only upsamples the
  8 kHz G.711 case). VAD/endpointer already construct at `engine.inbound_sample_rate` (silero
  runs natively at 16 kHz). No upsampled-from-8k audio on the G.722 path.
- **TTS:** the negotiated wire rate is threaded into the call loop and passed to the
  synthesiser per call (generalising PR #82's single codec-gated rate hook). ElevenLabs emits
  the negotiated rate natively (8 kHz for G.711 — preserving the "very choppy" fix — and
  16 kHz for G.722, so wideband is not thrown away by a downsample). Kokoro's model rate is a
  fixed 24 kHz; the engine downsamples 24→16 for G.722 (wideband preserved) vs 24→8 for
  G.711. No negotiated wideband is downsampled away.

## Consequences

- We negotiate the best available codec on the SIP path and realise wideband end-to-end, with
  G.711 as the universal fallback — closing the long-standing ADR-0005 gap.
- Zero new dependency; the codec is a small, vendored, fully-typed pure-Python module that
  runs in every gate. The trade-off is CPU: per-sample fixed-point G.722 in Python is heavier
  than a C codec. At telephony scale (one 16 kHz mono stream, 50 packets/s) this is well within
  budget; if profiling ever shows it hot, the `(encoding, clock_rate)` seam lets a future
  session swap in the C `G722` wheel as a base dependency without touching callers (a separate,
  recorded decision).
- The amendments to ADR-0005 (menu/order), ADR-0007 (TTS rate follows codec), and ADR-0017
  (wire rate no longer unconditionally 8 kHz) are recorded in those ADRs.
- Live validation (a real G.722 call on the test extension) is performed by the operator after
  redeploy; this lane ships code + tests + gate only and does not touch the running gateway.

## Alternatives considered

- **Add the C `G722` wheel as a base dependency.** Rejected for now: a compiled base
  dependency with no musl/macOS wheels and no `requires_python` floor weakens portability and
  the minimal-dependency posture, and the codec must live in the base (no-extra) environment.
  The vendored pure-Python port has the same bit-exact behaviour with none of that surface. The
  swap remains available behind the engine seam if CPU ever demands it.
- **Opus first instead of G.722.** Deferred: Opus needs a heavier dependency and a
  dynamic-payload-type negotiation; G.722 is static PT 9, dependency-free as a vendored port,
  and the immediate wideband win on the SIP path. Opus stays a future lane (the SDP layer
  already promotes Opus ahead of G.711 when present).
- **Port the ITU/STL reference.** Rejected on licence (modified GPL / copyleft).
