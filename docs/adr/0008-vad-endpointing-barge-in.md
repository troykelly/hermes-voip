# ADR-0008: VAD + endpointing with silero-vad (Phase 1, accepted); full-duplex barge-in deferred to a follow-up ADR

- **Date:** 2026-06-14
- **Status:** Accepted
- **Deciders:** agent session (VoIP architecture, post-research)

## Context

The cascaded media pipeline (ADR-0003) only works if something decides **when the caller has
started speaking** and **when their turn has ended** — telephony gives us a continuous 8 kHz
audio stream with no message boundaries. Three decisions are entangled here and are taken
together:

1. **Voice activity detection (VAD)** — distinguishing speech from silence/line-noise on the
   inbound stream, feeding the streaming STT (ADR-0006).
2. **Endpointing** — deciding the caller's turn is *finished* so the agent may respond. The
   research turn-taking budget is silence→first-audio under ~800 ms–1 s; endpointing latency
   alone is ~500 ms, which is why every downstream stage must stream (ADR-0006, ADR-0007).
3. **Barge-in** — letting the caller interrupt the agent mid-utterance, which requires
   listening to the inbound stream *while* TTS is playing out (full duplex).

Two hard constraints bound the answer. First, **Hermes' own voice mode is half-duplex** and
mic-deaf during TTS — there is no built-in barge-in to inherit, and the media seam is the
batch, file-path `BasePlatformAdapter`/`MessageEvent` model, so any duplex behaviour we want
lives in *our* in-process media engine (ADR-0005), not in Hermes. Second, rule 40 forbids
introducing an out-of-process media server or SaaS without an operator-approved ADR, so VAD,
endpointing and barge-in must all run **in-process** alongside the aiortc transport on the
shared event loop (ADR-0005).

A load-bearing **unknown** gates true barge-in: whether `AIAgent.run_conversation` can be
cancelled mid-generation. If it cannot, we can still stop *audible* output (cancel the
`TtsStream`, flush the playout buffer) but the agent keeps generating tokens into a discarded
turn. This is **unverified**. Per rule 6 (no claiming un-built design as done), full-duplex
barge-in is therefore **NOT accepted in this ADR**: the material below on barge-in/AEC is
**research and design context**, and the actual decision — cancellation mechanism and AEC
choice — is **deferred to a follow-up ADR after the cancellation spike** (rules 23, 26). What
this ADR accepts is Phase 1 only.

## Decision

**Accepted (Phase 1):** we use **`silero-vad`** (MIT) for speech detection and a **tunable
~500 ms silence endpointing timer** (with `Smart-Turn-v2` as an optional later upgrade) to
decide end-of-turn, running **half-duplex** turn-taking inside our own in-process media engine
(correct, and matching Hermes' own half-duplex model). The VAD choice and the half-duplex
endpointing path are the finished, accepted deliverable of this ADR.

**Deferred (Phase 2):** full-duplex barge-in (running VAD during TTS playout, cancelling the
in-flight turn, AEC). It is **not accepted here** and **not designed to completion**: it is
contingent on (a) a **verified mechanism to cancel an in-flight `AIAgent.run_conversation`**
(currently unverified — see the load-bearing unknown) and (b) an **AEC choice**, and is **to be
settled in a follow-up ADR after the cancellation spike** (rule 6). The Phase 2 material below
is recorded as research/design context for that follow-up, not as an accepted design.

**VAD — silero-vad, fed 16 kHz by choice (not by constraint).** silero-vad runs **natively at
either 8 kHz or 16 kHz**, so 8 kHz input is fully supported — it does **not** force a resample.
The media engine already up-samples the inbound G.711 8 kHz stream to 16 kHz for STT (ADR-0006),
and we feed VAD that **same** 16 kHz PCM so a single resample serves both consumers — a design
choice to share one stream, not a VAD requirement. The detector runs per frame on the inbound
stream and emits speech-onset / speech-offset events. Provider shape (the VAD lives in
`src/hermes_voip/media/vad.py`; it consumes ADR-0004's canonical `PcmFrame`/16 kHz convention):

```python
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, auto


class SpeechEdge(Enum):
    ONSET = auto()
    OFFSET = auto()


@dataclass(frozen=True, slots=True)
class VadEvent:
    edge: SpeechEdge
    frame_index: int  # 16 kHz frame ordinal, monotonic
    probability: float  # model speech probability in [0.0, 1.0]


class VoiceActivityDetector:
    """Per-frame speech detector over mono PCM (silero-vad supports 8 kHz or 16 kHz).

    silero-vad runs natively at either rate; we default to 16 kHz only to share
    one resampled stream with STT (ADR-0006), not because 8 kHz is unsupported.
    """

    def __init__(self, sample_rate_hz: int = 16_000, threshold: float = 0.5) -> None:
        """`sample_rate_hz` is 8000 or 16000 — both are native silero-vad rates."""
        ...

    def feed(self, pcm16_frame: bytes) -> Iterator[VadEvent]:
        """Push one fixed-size frame at `sample_rate_hz`; yield onset/offset edges."""
        ...

    def reset(self) -> None: ...
```

**Endpointing — a silence timer over VAD offsets.** End-of-turn fires when speech has been
absent for `HERMES_VOIP_ENDPOINT_SILENCE_MS` (default `500`). The threshold is config, not a
constant, because the 8 kHz telephony path and per-deployment line characteristics will shift
the optimum (rule 23 — re-measure on the real path). `Smart-Turn-v2` (a learned end-of-turn
classifier) is an **optional** later swap behind the same end-of-turn signal — adopting it is
its own decision, not this ADR's commitment. Config (read at runtime; see ADR-0004 for the
config pattern):

| Env var | Default | Meaning |
| ------- | ------- | ------- |
| `HERMES_VOIP_VAD_THRESHOLD` | `0.5` | silero-vad speech probability cutoff (Phase 1) |
| `HERMES_VOIP_ENDPOINT_SILENCE_MS` | `500` | trailing silence to declare end-of-turn (Phase 1) |
| `HERMES_VOIP_DUPLEX_MODE` | `half` | `half` (Phase 1, the only accepted mode); `full` is reserved for the deferred Phase 2 ADR |
| `HERMES_VOIP_AEC` | `off` | (Phase 2, deferred) acoustic echo cancellation: `off` \| `speex` \| `webrtc` |

The `full`/AEC settings are listed for continuity with the Phase 2 research below; they are
**not** part of the accepted decision and ship no behaviour until the follow-up ADR settles
cancellation + AEC.

**Phase 1 — half-duplex (accepted; complete on its own).** While the agent's TTS is
playing out, the inbound stream is **not** routed to STT; VAD/endpointing only gate caller
turns *between* agent utterances. This matches Hermes' native model exactly, is correct (no
self-transcription, no echo problem), and is the default (`HERMES_VOIP_DUPLEX_MODE=half`).
This is a real, finished deliverable — not a stub (rule 6).

**Phase 2 — full-duplex barge-in (DEFERRED research context, not an accepted design).** The
following sketches the intended Phase 2 so the follow-up ADR starts informed; it is **not**
decided here. With a future `HERMES_VOIP_DUPLEX_MODE=full`, VAD would run on the inbound stream
**during** TTS playout. On a **confident caller-speech onset** (VAD onset sustained past a short
debounce so a cough/echo doesn't trigger it), the media engine would, in order:

1. cancels the in-flight `TtsStream` (ADR-0007 exposes `cancel()` — sherpa-onnx returns 0 from
   its chunk callback; cloud paths Cartesia/Deepgram Aura-2 send explicit cancel/flush),
2. flushes the outbound playout/jitter buffer so already-buffered agent audio stops *audibly*,
   and
3. **attempts** to abort the in-flight agent turn — gated by the cancellation unknown below.

```python
class DuplexController:
    """Phase 2 (deferred) sketch: would own the barge-in decision on the media
    event loop (ADR-0005). Not part of the accepted Phase 1 decision."""

    async def on_caller_onset(self, ev: VadEvent) -> None:
        if not self._agent_is_speaking:
            return
        await self._tts.cancel()           # ADR-0007: stop synthesis
        self._playout.flush()              # drop buffered outbound audio
        await self._try_abort_agent_turn()  # see UNKNOWN below
```

**Acoustic echo cancellation would be required for full duplex (Phase 2, deferred).** Without
AEC the agent hears its own TTS on the inbound leg and re-transcribes it as caller speech, which
would both false-trigger barge-in and poison STT. Phase 2 would therefore run an in-process AEC
stage on the inbound PCM — candidates are the `speexdsp` echo canceller (simpler) or the WebRTC
Audio Processing Module (`webrtc` APM, stronger). **Which AEC engine to adopt is an open part of
the deferred Phase 2 decision**, to be settled in the follow-up ADR alongside the cancellation
mechanism — not chosen here. AEC would run **before** VAD/STT in the inbound chain. AEC is
irrelevant to Phase 1 (mic-deaf during TTS means there is no echo to cancel).

**LOAD-BEARING UNKNOWN — the reason Phase 2 is deferred, not accepted (rules 6/23/26):** whether
`AIAgent.run_conversation` is cancellable mid-generation. The cancellation spike must establish,
from Hermes source plus a live test, one of: (a) a supported cancel/cooperative-abort path exists
→ true barge-in; or (b) it does not → the abort degrades to discarding the completed turn's
output (audio is already silenced; the orphan generation finishes and is dropped), which is
*acceptable degraded* full-duplex and must be documented as such, not claimed as clean
cancellation. Either way, the verified mechanism plus the AEC choice are the substance of the
**follow-up ADR**; until that spike lands there is no accepted full-duplex design. The
off-loop→asyncio bridge for any threaded media callback is
`agent.async_utils.safe_schedule_threadsafe` (signature **unverified** — confirm in the spike).

All of this (Phase 1 today, Phase 2 when settled) is in-process (ADR-0005); no media server, no
SaaS, no rule-40 trigger.

## Consequences

- **Streaming turn-taking becomes possible.** A real onset/offset/endpoint signal lets STT
  (ADR-0006) and TTS (ADR-0007) stream against the <~800 ms–1 s budget instead of waiting for
  whole-file batches; without this ADR the cascade cannot meet the latency bar.
- **Phase 1 ships a correct, shippable product** with no echo/self-transcription risk, matching
  Hermes' own UX. We are not blocked on the cancellation unknown to deliver voice calls.
- **We commit (Phase 1) to maintaining an in-process VAD/endpointing engine** — VAD frame loop,
  endpointing timer, playout/jitter buffer — on the shared event loop. This is real CPU on the
  media path: silero-vad runs per-frame and must fit the per-frame budget alongside resampling
  and codec work (rule 22 — measured, not assumed, on the 8 kHz path). A Phase 2 AEC stage is
  **not** a commitment of this ADR; it is scoped to the deferred follow-up.
- **Phase 2 (deferred) carries an unresolved cancellation risk.** Even with audio silenced
  instantly, if the agent turn cannot be aborted we pay wasted LLM tokens/latency on interrupted
  turns. That risk is precisely why barge-in is deferred to a follow-up ADR rather than accepted
  here; the barge-in *responsiveness* number (onset→silence) and the cancellation behaviour must
  be measured on our path before any acceptance (rules 6/23).
- **Config-driven, no lock-in.** Phase 1 thresholds are env vars; silero-vad (MIT) is permissive
  OSS, in-process — no per-minute cost, no vendor egress, no operator-gated infra. `Smart-Turn-v2`
  and any future AEC engine remain swappable behind stable seams.
- **Upgrade cadence:** silero-vad is pinned in `uv.lock` (rule 33); model/library bumps are
  deliberate, gated by a re-measure of endpointing latency.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| RMS / energy-threshold VAD (Hermes/Discord style) | Too blunt for telephony: line noise, comfort noise and varying levels make a fixed energy gate either trigger-happy or deaf. A learned model (silero-vad) is far more robust at the per-frame decision the endpointer depends on. |
| WebRTC VAD (`py-webrtcvad`) as the detector | Lighter and native-8 kHz, but materially weaker accuracy than silero-vad on noisy speech, with no probability output to tune a debounce/onset confidence against — barge-in needs that confidence signal. (We still use WebRTC's APM for *echo cancellation* in Phase 2, a different task.) |
| Permanently half-duplex (never build barge-in) | Acceptable as the Phase 1 deliverable (accepted here) and as Hermes' own ceiling. Interrupting an over-long agent answer is core to natural phone conversation, so a full-duplex Phase 2 is *intended* — but it is deferred to a follow-up ADR (gated on the cancellation spike + AEC choice), not committed in this ADR. Half-duplex is the verified default until then. |
| Server-side / gateway-provided VAD or endpointing | Vendor-specific behaviour in the core, violating the gateway-agnostic invariant (CLAUDE.md) — the first test gateway is only a test target. Also moves the decision off our event loop where we cannot tune it against the real 8 kHz path. |
| Fused speech-to-speech model owning turn-taking (OpenAI Realtime / Gemini Live) | Replaces Hermes as the brain and adds lock-in/egress (rejected for the core in ADR-0003). Turn-taking stays ours, in-process, model-agnostic. |

