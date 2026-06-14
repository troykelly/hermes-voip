# ADR-0006: Streaming STT: self-host sherpa-onnx (default) with Deepgram Flux as cloud fallback

- **Date:** 2026-06-14
- **Status:** Accepted
- **Deciders:** agent session (VoIP architecture, post-research)

## Context

The cascaded media path (ADR-0003) needs a speech-to-text stage that turns the inbound
caller audio into text for the Hermes agent. The telephony transport (ADR-0005) delivers
narrowband **8 kHz G.711** (mu-law / a-law) RTP, and the conversational latency budget is
tight: natural turn-taking wants silence→first-agent-audio under roughly 800 ms–1 s, of
which endpointing alone consumes ~500 ms (ADR-0008). That budget is only achievable if STT
is **streaming** — partial hypotheses emitted as audio arrives — not a whole-file batch
call. Hermes' own `transcription_provider` (whole-file `transcribe(file_path) -> dict`,
25 MB cap) is therefore the wrong shape for the live path and is not on this seam; it
remains available for non-real-time uses (e.g. voicemail transcription) but is not the
conversational recognizer.

Several rules bind the choice:

- **Rule 40/41** — the plugin is a package loaded by the Hermes runtime, with no hosting
  platform, cloud, or external SaaS assumed; introducing one needs explicit operator
  approval recorded in an ADR. A self-hosted, in-process recognizer is therefore the
  default that introduces no infrastructure and no egress; any cloud recognizer is an
  operator-gated, opt-in deviation.
- **Rule 35** — every dependency change is licence-gated. ASR engines and their model
  weights carry *separate* licences, and several otherwise-attractive streaming models ship
  under share-alike or non-commercial terms (the Kroko zipformer models are **CC-BY-SA**, a
  licence trap that would virally bind our use; Moonshine's weights are non-commercial for
  non-English). The default must be Apache-2.0 *engine and model*.
- **Rule 23/24/26** — verify on the real target. Every latency and accuracy number in the
  research is vendor/model-only and measured on wideband audio; our path is 8 kHz upsampled
  to 16 kHz, so the real bar is re-measured on that path, not asserted from a datasheet.
- **Rule 17/39** — fully-typed, no escape hatches. The recognizer hides behind a typed
  Protocol so the engine choice is swappable without leaking vendor types into the core.

This ADR resolves the STT-provider half of the conversational path deferred by ADR-0001.

## Decision

The default streaming recognizer is a **self-hosted, in-process sherpa-onnx streaming
zipformer** (Apache-2.0 engine) running a **pinned Apache-2.0 icefall model**
(`sherpa-onnx-streaming-zipformer-en-2023-06-26`), chosen for privacy, zero per-minute
cost, and no lock-in. **Deepgram Flux + Nova-3** (native `mulaw` @ 8 kHz, fused
turn-detection, a low per-minute fee — current rate tracked in the provider runbook, not
here) is a verified, pluggable **cloud fallback**
selected by configuration for accuracy- or multilingual-critical deployments, accepting the
recorded cloud-egress trade-off (rule 40 — operator-gated, opt-in, off by default). Both
implementations sit behind the `StreamingASR` provider Protocol defined in ADR-0004.

### Provider seam (behind ADR-0004's `StreamingASR`)

The sherpa-onnx engine implements the `StreamingASR` Protocol defined in ADR-0004
(`stream(audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]`). There is **no**
re-declared seam here: `StreamingASR`, `Transcript` (`text`, `is_final`, `end_of_turn`,
`confidence`), and `PcmFrame` (PCM16 at a declared sample rate) all live canonically in
ADR-0004 and are imported, not redefined. The recognizer is fed **16 kHz mono PCM16** frames
(the media layer's declared `input_sample_rate`) and emits partial and final `Transcript`s;
the core never sees vendor types.

The endpointing decision (when a turn is complete) is owned by ADR-0008's VAD/endpointing
layer, not the recognizer: the engine reports stable text segments, and each yielded
`Transcript`'s `end_of_turn` is set from the endpointer signal the media layer supplies
(ADR-0008). The concrete implementation against ADR-0004's exact interface:

```python
from __future__ import annotations

from collections.abc import AsyncIterator

from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame


class SherpaOnnxASR:
    """Self-hosted streaming zipformer recognizer (StreamingASR, ADR-0004).

    Runs in-process; consumes 16 kHz mono PCM16 frames decoded+upsampled from
    the 8 kHz G.711 path (ADR-0005). The inference call runs in a worker thread
    with results bridged back to the loop, so it never blocks the shared loop.
    """

    def __init__(self, model_dir: str) -> None: ...

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        """Drain `audio` until exhausted; yield interim + final `Transcript`s.

        Each `Transcript.end_of_turn` is set from the endpointer signal the media
        layer supplies (ADR-0008); the engine itself reports stable segments only.
        """
        ...

    @property
    def input_sample_rate(self) -> int:
        """16 kHz: the media layer resamples the 8 kHz G.711 stream to match."""
        return 16_000


# Structural conformance is enforced by mypy and the runtime_checkable Protocol:
_: type[StreamingASR] = SherpaOnnxASR
```

### Media glue we own

The transport delivers 8 kHz G.711; the recognizer requires 16 kHz (silero-vad, ADR-0008, runs
natively at 8 kHz or 16 kHz, so it does not force the resample — it just shares the recognizer's
16 kHz stream). The plugin owns the decode + resample, using **`audioop-lts`** (the maintained
replacement for the `audioop` stdlib module removed in Python 3.13):

- `audioop.ulaw2lin` / `audioop.alaw2lin` — G.711 mu-law/a-law → linear PCM16 @ 8 kHz.
- `audioop.ratecv` — 8 kHz → 16 kHz upsample (carrying the codec state tuple across frames
  so the resampler is continuous, not per-frame).

The same 16 kHz PCM stream fans out to both this recognizer and silero-vad (ADR-0008). The
recognizer's 16 kHz requirement is what fixes the canonical internal rate; silero-vad supports
both 8 kHz and 16 kHz natively and simply reuses this stream so one resample serves both.

### Configuration and selection

Provider choice is config-driven; the self-host default needs **no** credentials and runs
fully offline.

- `HERMES_VOIP_STT_PROVIDER` — `"sherpa-onnx"` (default) | `"deepgram"`.
- `HERMES_VOIP_STT_MODEL_DIR` — filesystem path to the pinned sherpa-onnx model directory
  (default points at the bundled/cached model).
- Deepgram fallback reads `DEEPGRAM_API_KEY` at runtime from the gitignored `.env` /
  1Password (via the `op` CLI per rule 41); the key value never enters a tracked file, log,
  or commit (rule 34). Selecting `"deepgram"` requires that env var to be present;
  validation fails fast and loudly if it is selected but unset (rule 37, errors propagate).

Plugin file paths: `src/hermes_voip/stt/base.py` (the Protocol + `Transcript`),
`src/hermes_voip/stt/sherpa_onnx.py` (default), `src/hermes_voip/stt/deepgram.py`
(fallback), `src/hermes_voip/stt/resample.py` (the G.711→PCM16→16 kHz glue).

### Licence gate (CI, rule 35)

Because the engine and the model carry independent licences and the Kroko zipformer trap is
specifically CC-BY-SA, a CI test asserts the **model's** declared licence is Apache-2.0
before the pinned model is accepted — the pin is verified, not trusted. The licence gate pins
the **exact** model artifact — the source repo, a **pinned revision**, the specific file names,
and their **checksums** — not the generic model name; the exact revision + checksums are
recorded at implementation. The test reads the recorded licence metadata for that pinned id and
fails the build on anything other than Apache-2.0. TDD per rule 18: the licence-assertion test
and the resample-continuity test are written red first.

## Consequences

- **Easier:** the default path is fully offline, zero per-minute cost, no egress, no vendor
  account, and no infrastructure to provision — it satisfies rule 40 by introducing
  nothing. Privacy is maximal: caller audio never leaves the process. The typed
  `StreamingASR` seam (ADR-0004) makes the engine swappable without touching the core.
- **Harder / committed to maintain:** we own the codec + resampling glue and its
  correctness (continuous `ratecv` state, frame alignment), the pinned-model artifact and
  its checksum/licence gate, and CPU-time of in-process inference. The recognizer shares the
  single adapter/agent event loop (per the verified Hermes in-process constraint), so the
  inference call must not block the loop — sherpa-onnx runs in a worker thread with results
  bridged back; the off-loop→asyncio bridge precedent is `agent.async_utils`
  (`safe_schedule_threadsafe`, signature unverified — to be confirmed in implementation, not
  assumed here).
- **Accuracy ceiling:** an 8 kHz telephony model on upsampled audio is the realistic floor,
  not wideband benchmark numbers; the multilingual/hard-audio escape hatch is the Deepgram
  fallback. Whether the self-host default's word-error-rate on our 8 kHz path is acceptable
  is a **measurement owed on the real target** (rule 26), not a claim made here.
- **Cloud fallback consequences (when enabled):** a low per-minute fee (current rate tracked
  in the provider runbook, not here), caller-audio egress to a third party (the reason it is
  opt-in and operator-gated under rule 40), and a network-availability dependency in the hot
  path. Flux's fused turn-detection partially overlaps ADR-0008's
  endpointing; when Deepgram is selected, the integration prefers Flux's turn signal and
  the local endpointer relaxes to avoid double-counting.
- **Upgrade cadence:** sherpa-onnx and `audioop-lts` are pinned in `uv.lock` (rule 33) and
  bumped deliberately; a model bump re-runs the licence gate and re-measures WER on the
  8 kHz path before it lands.
- **Pairing note:** if Deepgram is chosen for STT, Deepgram Aura-2 becomes the natural
  single-vendor TTS pairing (ADR-0007); the default keeps STT and TTS independently
  self-hosted.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| **AssemblyAI Universal-Streaming** (native `pcm_mulaw`) | Capable and native-8 kHz, but cloud-only (egress + rule 40 gating) and session-based billing complicates cost modelling; Deepgram Flux is the cleaner single fallback. Re-evaluable as a second cloud option later. |
| **Soniox** streaming | Preview-stage API; not yet stable enough to commit a fallback to. |
| **NVIDIA NeMo FastConformer** | Strong accuracy but requires a **GPU** (introduces infrastructure, rule 40) and the model is **CC-BY-4.0**, not Apache-2.0 — fails the default's licence bar. |
| **faster-whisper / Whisper** | **Batch**, not streaming — cannot emit partials, so it cannot meet the turn-taking latency budget. Suitable only for the non-live `transcription_provider` use, not this path. |
| **Vosk** | Apache-2.0 and self-hostable, but accuracy on **8 kHz** is materially weaker; fallback-tier only, not the default recognizer. |
| **Moonshine** | Non-English weights are **non-commercial** licensed — a licence trap for a general-purpose commercial deployment. |
| **Kroko streaming zipformer models** | The models are **CC-BY-SA** (share-alike) — would virally bind our use; the explicit reason the licence gate asserts Apache-2.0 on the pinned model. |
| **Cloud-only, or self-host-only, with no interface** | Hard-wiring one engine forfeits the privacy/cost default *or* the accuracy/multilingual escape hatch; both are needed for different deployments, so the choice lives behind the `StreamingASR` Protocol (ADR-0004) and is config-selected. |

