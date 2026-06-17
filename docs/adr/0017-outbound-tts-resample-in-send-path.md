# ADR-0017: Rate reconciliation at the media seam — outbound TTS→8 kHz in `send_audio`, inbound 8 kHz→16 kHz before the recogniser

- Status: Accepted
- Date: 2026-06-16
- Deciders: agent session (operator-directed live no-audio fix)
- Supersedes/relates: ADR-0004 (PcmFrame currency), ADR-0005 (media plane),
  ADR-0006 (STT), ADR-0007 (TTS)

## Context

A real inbound call produced **zero audio**. The greeting task in `CallLoop`
called `RtpMediaTransport.send_audio` with a `PcmFrame` at the TTS provider's
output rate (sherpa-Kokoro emits **24000 Hz**, ADR-0007). `send_audio` →
`frame_to_ulaw`/`frame_to_alaw` calls `_require_8k`, which raised
`ValueError: G.711 requires 8000 Hz, got 24000 Hz`. The greeting task is a
`TaskGroup` child, so the exception cancelled the whole `CallLoop`, the inbound
handler failed, no RTP was ever sent, and the caller heard silence.

Root cause: ADR-0004 declares `PcmFrame` carries a `sample_rate`, and providers
emit at their native rate, but **nothing reconciled the TTS output rate to the
G.711 wire rate (8 kHz)** on the outbound path. The codec helpers correctly
*refuse* to silently change a frame's duration by encoding a wide-band frame as
8 kHz — they raise. The missing piece is a resample step before the encode.

Investigating the inbound direction surfaced a second, quieter defect: the
sherpa-onnx ASR feed (`stt/sherpa_onnx.py::_feed`) converted each inbound
`PcmFrame` straight to float32 and called `accept_waveform(stream,
RECOGNISER_SAMPLE_RATE=16000, samples)` — but the transport delivers frames at
**8000 Hz** (`inbound_sample_rate == G711_SAMPLE_RATE`). The 8 kHz samples were
fed to the recogniser **labelled 16 kHz**: half-rate audio, wrong pitch and
timing, badly degraded recognition. `FrameUpsampler` (8 kHz→16 kHz, ADR-0006)
existed but was never wired into the pipeline. The existing unit tests masked it
by constructing frames already at 16 kHz.

## Decision

**Reconcile sample rates at the media seam, in both directions, by converting —
never by raising or dropping (rule 37: this is a conversion, not a swallow).**

### Outbound: resample inside `send_audio`

`RtpMediaTransport` owns a single fixed wire rate (`G711_SAMPLE_RATE`, 8 kHz).
`send_audio` now resamples any frame whose `sample_rate != 8000` down to 8 kHz
**before** `_encode`. The `PcmFrame` already self-describes its rate (ADR-0004),
so **no signature change** is needed — the engine reads `frame.sample_rate` and
converts. A frame already at 8 kHz passes through untouched (the existing fast
path and all existing send tests are unchanged).

The conversion reuses the canonical, state-carrying
`hermes_voip.media.audio.Resampler` (`audioop.ratecv`), not a stateless
per-frame convert: a continuous TTS stream resampled frame-by-frame must produce
the same audio as a single pass, with no boundary clicks. The engine keeps **one
`Resampler` per distinct source rate** (a small dict), created lazily on first
sight of that rate and reset in `connect()`/`stop()` so a reused engine starts a
fresh stream. Keying by source rate (rather than assuming 24 kHz) keeps the
engine gateway- and provider-agnostic: any TTS output rate (24000, 16000, …)
converts correctly; only 8 kHz bypasses resampling.

This is the cleanest, most local place:
- it makes `send_audio` total over input rates (it converts, never raises), so
  the live crash cannot recur for any provider rate;
- the engine is the one component that *knows* the wire rate, so the wire-rate
  reconciliation belongs there (symmetry with the inbound decode, which already
  produces 8 kHz frames there);
- `CallLoop.speak`/`_play` stay rate-agnostic — they forward provider frames
  unchanged, exactly as the TtsStream protocol yields them.

### Inbound: upsample 8 kHz→16 kHz before `accept_waveform`

`SherpaOnnxASR` now runs every inbound frame through a per-stream
`FrameUpsampler` (8 kHz→16 kHz) before converting to float32, so the recogniser
receives genuine 16 kHz samples that match the rate it is told. Frames that
already arrive at the recogniser rate (e.g. a cloud STT seam, or a fake in a
test) pass through unchanged — the upsampler only fires when the frame is at the
G.711 wire rate, and any other unexpected rate raises (a real config error, not
something to paper over). The Deepgram fallback already sends mu-law/8000 on the
wire and is unaffected.

## Alternatives considered

1. **Resample in `CallLoop._play`/`speak`.** Rejected: the call loop would need
   to know the wire rate (8 kHz) and own resampler state, leaking media-layer
   detail into the orchestrator and duplicating the knowledge the engine already
   has. The engine is the single owner of the wire rate.
2. **Resample inside the TTS provider (emit 8 kHz directly).** Rejected: it
   re-introduces transport/wire-rate coupling into the provider (ADR-0004 keeps
   providers speaking their native rate; the media layer reconciles), and would
   make the provider gateway-specific.
3. **Keep `send_audio` raising; require callers to pre-resample.** Rejected:
   that is the status quo that produced the live crash. Making the seam total is
   the correct, defensive design — a frame at the wrong rate is converted, and
   the only way to "lose" audio is a genuinely unconvertible input.
4. **Thread the TTS `output_sample_rate` through `send_audio` as a parameter.**
   Unnecessary: `PcmFrame.sample_rate` already conveys the source rate per
   frame, which is strictly more correct (a provider could in principle change
   rate mid-stream) and needs no API change.

## Consequences

- `send_audio` is now total over input sample rates: the live `ValueError` crash
  cannot recur; a 24 kHz greeting frame is downsampled to 8 kHz and G.711-encoded
  and emitted as RTP.
- The inbound STT path feeds the recogniser correctly-rated audio, fixing a
  silent recognition-quality regression.
- Both directions reuse the one canonical `Resampler`/`FrameUpsampler`; no new
  resampling implementation is introduced.
- Efficiency: one extra `audioop.ratecv` pass per outbound frame when the source
  rate differs from 8 kHz (a C-level memmove-class operation on ≤ a few hundred
  samples per 20 ms frame — negligible against synthesis cost). Frames already at
  8 kHz incur zero overhead (fast-path bypass).
- The fix is gateway- and provider-agnostic (no vendor assumption about the
  source rate).

## Amendment (2026-06-17): re-frame the outbound stream with a carry buffer (the "very choppy" fix)

- Deciders: agent session (operator-directed live "very choppy" audio fix)

The original `send_audio` re-chunking padded each call's **sub-frame remainder**
(the `< 160`-sample tail left after slicing into 20 ms G.711 frames) up to a
whole frame with zeros, on **every call**. That was acoustically invisible for
sherpa-Kokoro (a few large chunks per utterance → a handful of pads) but became
the dominant defect for **ElevenLabs**, which streams audio as many small
chunked-HTTP reads (`_UrllibHttp._CHUNK_BYTES = 4096`). The call loop calls
`send_audio` **once per producer chunk**, so a stream that delivers ~12 chunks
per second of audio injected ~12 silence slugs per second — a click/gap at every
chunk boundary, i.e. the operator's "very choppy". Measured on a 1 s 24 kHz sine
fed as 4096-byte chunks: **180 ms of injected silence across 12 pad events** (the
per-chunk padding inflated 8000 → 9440 samples). The defect is independent of the
sample rate — at 8 kHz a 4096-byte chunk is still `4096 % 320 = 256 ≠ 0`, so it
pads every chunk too.

`send_audio` now keeps a per-call **outbound re-framing buffer** (`_tx_buffer`,
wire-rate PCM16 bytes): each call appends the resampled samples, emits only the
**whole** 160-sample frames, and **carries the sub-frame remainder forward** to
prepend to the next chunk — so the concatenated outbound stream is
sample-continuous with **no interior silence**. The residual tail is zero-padded
to one final frame **exactly once**, in `stop()` (flushed through the still-open
socket before it closes), so the last utterance's tail is delivered rather than
stranded. The buffer is reset in `connect()`/`stop()` alongside the existing
`_tx_resamplers`. The seq/timestamp counters advance only per emitted frame, so
the RTP wire stays RFC 3550-consistent.

This sits **after** the rate reconciliation above (it re-frames the already-8 kHz
wire-rate bytes), so it is orthogonal to and composes with the resampler: the
24 kHz resample path and a native-8 kHz path both produce one continuous stream.

Consequences: the per-chunk silence storm is gone (a streaming cloud TTS plays
smoothly); padding happens at most once per call, not per chunk; the change is
provider- and gateway-agnostic (it re-frames whatever wire-rate bytes arrive).
Within a single call, an utterance's final `< 20 ms` tail can carry into the next
utterance's first frame — real, sample-continuous audio (never silence), and
acoustically negligible versus the 180 ms-per-second of gaps it replaces.
