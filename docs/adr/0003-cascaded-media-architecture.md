# ADR-0003: Cascaded STT → Hermes → TTS, keeping Hermes as the brain (reject fused speech-to-speech for the core)

- **Date:** 2026-06-14
- **Status:** Accepted
- **Deciders:** agent session (VoIP architecture, post-research)

## Context

ADR-0002 establishes the plugin shape: a `kind: platform` adapter holds the SIP/RTP
registration, and inbound caller turns surface to the Hermes agent as discrete
`MessageEvent` text turns while replies go back out as audio. That seam forces a prior,
deeper decision: **what produces the text the agent reasons over, and what turns the
agent's reply back into speech?** Two families answer this:

1. **Cascaded** — three replaceable stages: streaming speech-to-text (STT), the Hermes
   agent (`AIAgent.run_conversation` over text turns), and streaming text-to-speech (TTS).
   Hermes remains the reasoning brain; audio is a peripheral on each side.
2. **Fused speech-to-speech (S2S)** — one multimodal model (OpenAI Realtime, Google
   Gemini Live, Kyutai Moshi, Ultravox) ingests caller audio and emits reply audio
   directly, fusing transcription, reasoning, and synthesis into a single network call.

The bound constraints:

- **Hermes is the product.** The whole point of this plugin is to give *a Hermes agent* a
  voice — its tools, hooks (`pre_tool_call`, `pre_gateway_dispatch`, `on_session_start`),
  memory, system prompt, and `ctx.llm`-driven reasoning. A fused S2S model **replaces**
  `run_conversation` with its own opaque reasoning; the Hermes agent would no longer be
  the thing thinking. That is not a media decision, it is an "is this still Hermes"
  decision.
- **Gateway- and vendor-agnostic core (rule 40).** Every credible production-grade S2S
  model today is a hosted cloud API (OpenAI, Google) or a research weight set with a
  heavy GPU footprint (Moshi, Ultravox). Wiring one into the *core* path introduces both
  cloud lock-in and continuous **caller-audio egress** to a third party — neither may sit
  in the core without explicit operator approval recorded in an ADR (rule 40).
- **Honest latency (rules 23/24/26).** S2S's headline appeal is latency: vendors claim
  ~200 ms audio-to-audio. A cascade pays for three stages and a text round-trip — research
  puts a naive cascade at several hundred ms up to ~1 s. The natural-turn-taking target is
  silence→first-audio under ~800 ms–1 s (ADR-0008). The cascade can hit that target *only
  if every stage streams*; this must be designed in, not assumed, and re-measured on our
  real 8 kHz telephony path (rule 26) — vendor numbers are model-only.
- **Replaceability and on-the-record deferral (rule 40).** The STT and TTS providers are
  genuinely undecided per-deployment (self-host vs cloud, English vs multilingual,
  CPU vs GPU). A cascade lets each stage be a swappable provider (ADR-0004) decided in its
  own ADR (ADR-0006 STT, ADR-0007 TTS); a fused model collapses all three choices into one
  irreversible vendor pick.

## Decision

**The conversational media path is cascaded: a streaming STT provider produces text, the
Hermes agent reasons over that text via `AIAgent.run_conversation`, and a streaming TTS
provider speaks the reply. Fused speech-to-speech models are NOT the core path** — they
fuse reasoning into the audio model and would replace Hermes, while adding cloud lock-in
and caller-audio egress. S2S remains available **only opt-in, behind the provider
interface (ADR-0004)**, and any S2S deployment that egresses caller audio to a cloud is
infra-gated under rule 40.

Concrete shape of the cascade:

```text
                 caller audio (G.711/Opus @ 8 kHz, RTP)
                              │
                  ┌───────────▼────────────┐
                  │  VAD + endpointing      │  ADR-0008 (barge-in, eager EOT)
                  └───────────┬────────────┘
              streaming PCM   │  turn boundary
                  ┌───────────▼────────────┐
                  │  StreamingASR           │  ADR-0006  → partial + final text
                  └───────────┬────────────┘
                final transcript (str)
                  ┌───────────▼────────────┐
                  │  Hermes AIAgent         │  run_conversation over text turns
                  │  (the brain — ctx.llm,  │  tools, hooks, memory unchanged
                  │   tools, hooks)         │
                  └───────────┬────────────┘
                reply text, streamed by sentence
                  ┌───────────▼────────────┐
                  │  StreamingTTS           │  ADR-0007 → PCM frames, cancellable
                  └───────────┬────────────┘
                              ▼
                 reply audio (RTP back to caller)
```

- **The agent boundary stays text.** The cascade plugs into the existing `MessageEvent`
  text seam (ADR-0002): the adapter calls `self.handle_message(event)` with the STT
  **final transcript** as `event.text`, and the agent's reply text drives the TTS stage.
  No change to `run_conversation` or Hermes's reasoning is required or made.
- **Both edges stream, by interface contract.** STT and TTS are *streaming* providers
  (ADR-0006, ADR-0007), not Hermes's built-in whole-file batch STT/TTS
  (`transcribe(file_path)` / `synthesize(text, output_path)`), which are too coarse for
  conversational latency. The provider interface (ADR-0004) is defined in terms of async
  iterators of partial results, not files. Sketch (fully annotated, no `Any`):

  ```python
  from collections.abc import AsyncIterator

  from hermes_voip.providers.asr import StreamingASR, Transcript
  from hermes_voip.providers.audio import PcmFrame
  from hermes_voip.providers.tts import StreamingTTS, TtsStream

  # Canonical shapes (defined in ADR-0004, imported not redefined):
  #   StreamingASR.stream(audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]
  #   StreamingTTS.synthesize(text: AsyncIterator[str], voice: str) -> TtsStream
  # `Transcript` carries text/is_final/end_of_turn/confidence; a `TtsStream` is an
  # AsyncIterator[PcmFrame] with flush()/cancel() for barge-in (ADR-0007/0008).
  ```

  (The `Protocol` definitions and their exact signatures live in ADR-0004; referenced here
  only to show the cascade's shape — the WHY. The concrete provider list and config land in
  ADR-0006/0007.)

- **Latency is engineered, not hoped for.** The cascade meets the sub-second target
  through four mitigations, each owned by a sibling ADR:
  1. **Streaming partials** — STT emits running hypotheses so the next stage can begin
     before the caller stops speaking (ADR-0006).
  2. **Sentence-chunked TTS** — the first sentence of the agent's reply is synthesised and
     played the moment it is complete, not after the full reply, so first-audio is gated by
     one sentence, not the whole turn (ADR-0007).
  3. **Eager end-of-turn** — VAD/endpointing closes the caller's turn on a tuned silence
     timer (~500 ms) rather than waiting for a hard pause (ADR-0008).
  4. **Tuned endpointing + optional fused turn-detection** — providers with native
     turn-detection (an STT option in ADR-0006) collapse endpointing latency into the STT
     stage.
- **Provider selection is deferred and per-stage (rule 40).** Which STT and which TTS are
  decided in ADR-0006 and ADR-0007 respectively; this ADR commits only to the *cascade
  topology* and the *text agent boundary*, not to any vendor. Self-host defaults keep the
  core free of cloud egress; cloud providers are configured per-deployment via env vars
  (e.g. `ELEVENLABS_API_KEY` read at runtime — never committed; see
  CLAUDE.md secrecy invariant and rule 34).
- **S2S as opt-in only.** A fused model may be registered as an alternative provider
  through the same ADR-0004 interface, but selecting it for a deployment that sends caller
  audio off-box requires explicit operator approval recorded as its own ADR (rule 40); it
  is never the default and never the core. Using an S2S model *merely as the STT stage*
  (audio-in, text-out, discarding its synthesis) is permitted as one possible
  `StreamingASR` implementation under ADR-0006 — it does not replace the brain — but
  is still cloud-egress and so still per-deployment, infra-gated.

## Consequences

- **Hermes stays the brain.** Tools, hooks, memory, system prompt, and `ctx.llm`-driven
  reasoning all keep working unchanged; voice is a peripheral, not a fork of the agent.
  Anything Hermes can do in text, it can now do on a phone call.
- **Each stage is independently swappable and testable.** STT, agent, and TTS have clean
  text boundaries, so each is unit-testable in isolation with fakes (a fake STT yields
  fixed transcripts; a fake TTS records text) and any one can be replaced without touching
  the others — exactly what TDD (rule 18) and the per-stage deferral (rule 40) need.
- **We own the latency budget, and it is harder.** A cascade has more moving parts than a
  single S2S call and *will* be slower if any stage batches. We commit to maintaining the
  streaming contract end-to-end and to re-measuring silence→first-audio on the real 8 kHz
  path (rule 26); the number is reported, not assumed (rule 24). The honest trade is: we
  accept a few-hundred-ms-to-~1 s budget (vs S2S's vendor-claimed ~200 ms) in exchange for
  keeping Hermes as the reasoner and the core vendor-free.
- **No mandatory cloud egress in the core.** With self-host STT/TTS defaults, caller audio
  never leaves the box; there is no per-minute API cost floor and no third-party
  dependency for a basic deployment. Cloud providers add cost and egress only when a
  deployment opts in.
- **More surface to maintain.** Three provider integrations plus the streaming glue, VAD,
  and barge-in, versus one API. This is the cost of replaceability and of not ceding the
  brain; it is deliberate.
- **Barge-in is our responsibility.** Because synthesis is a stage we own, we must
  implement cancellation (stop TTS the moment the caller speaks over it — ADR-0008) rather
  than inheriting an S2S model's built-in `cancel_response()`. The streaming TTS interface
  is defined to be cancellable for exactly this reason.
- **Upgrade cadence is per-stage and decoupled.** An STT model bump or a TTS voice change
  is a provider-local change behind a stable text interface; it does not ripple into the
  agent or the transport.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| **OpenAI Realtime API as the core brain** | Fuses reasoning into a hosted audio model — replaces Hermes's `run_conversation`, so the agent (its tools/hooks/memory/prompt) is no longer the thing thinking. Adds cloud lock-in and continuous caller-audio egress; both are infra-gated by rule 40 and inappropriate for a vendor-agnostic core. Available opt-in via ADR-0004. |
| **Google Gemini Live as the core brain** | Same defect: a fused multimodal model supplants Hermes as the reasoner and egresses caller audio to a single cloud vendor. Lock-in + rule 40 gating; opt-in only. |
| **Kyutai Moshi as the core brain** | Full-duplex S2S research model that *is* the dialogue model — it would replace Hermes entirely, and self-hosting it is a heavy GPU footprint (rule 40 infra gate). Its strengths (duplex, low latency) don't outweigh losing the Hermes brain. Opt-in only. |
| **Ultravox as the core brain** | An audio-conditioned LLM: even where text-out is possible, adopting it as the conversational core means *its* weights reason, not Hermes via `ctx.llm`. GPU-heavy self-host; replaces the brain. Opt-in only. |
| **Use an S2S model merely as the STT stage** (audio→text, discard synthesis) | Permissible as one `StreamingASR` under ADR-0006 — it does *not* replace the brain — but it is still cloud egress of caller audio, so it stays per-deployment and infra-gated (rule 40); not a core default. Wasteful too: paying for a full S2S model to use only its transcription. |
| **Hermes's built-in batch STT/TTS** (`transcribe(file_path)` / `synthesize(text, output_path)`) | Whole-file, non-streaming: would add seconds of latency per turn and blow the sub-second turn-taking budget (ADR-0008). The cascade therefore uses *streaming* providers (ADR-0006/0007), not the batch path. |

