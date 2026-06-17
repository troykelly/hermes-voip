# ADR-0025: Automatic TTS failover (cloud → self-host) + ElevenLabs `model_id` guard

- **Date:** 2026-06-17
- **Status:** Accepted (amends ADR-0007)
- **Deciders:** agent session (VoIP reliability), operator-directed

## Context

A live call failed with **no audio at all**. On a real G.722 call the ElevenLabs streaming
request returned **HTTP 400** during the greeting synthesis; the error rose out of the
`CallLoop` `TaskGroup`, cancelled the whole call, and the caller heard silence (no `rtp tx`).
Service was restored only by the operator manually switching `HERMES_VOIP_TTS_PROVIDER` to
the self-host `sherpa-kokoro` and redeploying.

Two distinct failures combined:

1. **A single TTS provider failure kills the entire call.** The opening greeting (ADR-0002
   NAT-latch) runs as a `TaskGroup` child and is *intentionally* fatal — the rationale being
   that if the opening cannot emit RTP, a NAT'd gateway never latches and the call is already
   dead (ADR-0007 / `call_loop.py`). But a *recoverable* TTS error (the cloud vendor 400s, a
   timeout, a dropped connection) is not the same as "RTP cannot be sent": the self-host
   synthesiser is right there and could have spoken instead. There was no fallback, so a
   transient cloud fault became a dead call.

2. **The ElevenLabs `model_id` 400 was a config foot-gun, not a bad request shape.** The
   400 root cause was reproduced live against the ElevenLabs API (key from 1Password, never
   logged): the *exact* request the plugin builds for a 16 kHz G.722 call — URL
   `…/{voice}/stream?output_format=pcm_16000`, `Accept: audio/pcm`, body
   `{"text":…,"model_id":"eleven_flash_v2_5","voice_settings":{"stability":0.35,
   "similarity_boost":0.75,"style":0.0,"use_speaker_boost":true}}`, River voice — returns
   **HTTP 200** with valid PCM. Empty/whitespace text and every segmenter-emitted segment
   also returned 200. The 400 only reproduces when `model_id` carries a **non-ElevenLabs
   value**:

   | `model_id` sent | Live response |
   | --- | --- |
   | `eleven_flash_v2_5` | `200 OK` (valid PCM) |
   | `kokoro-multi-lang-v1_0` | `400 invalid_uid` — "An invalid ID has been received for voice: '…'" |
   | `/opt/models/kokoro` | `400 invalid_uid` — same |
   | `""` (empty) | `400` — "No ID has been received for voice" |

   The mechanism is the **shared `HERMES_VOIP_TTS_MODEL` knob**: it is a model **directory
   path** for `sherpa-kokoro` but the model **id** for `elevenlabs`, and
   `_make_elevenlabs_tts` does `model_id = config.tts_model or FLASH_V2_5_MODEL_ID`. A `.env`
   that sets `HERMES_VOIP_TTS_MODEL` to a Kokoro directory (e.g. for a self-host A/B) and
   *also* selects `provider=elevenlabs` sends that directory string straight through as
   `model_id`, and ElevenLabs rejects it with the (confusingly voice-worded) `invalid_uid`
   400. The operator's hand-rolled `curl` passed because it hard-coded
   `model_id=eleven_flash_v2_5`.

Constraint: the repo is a vendor-agnostic plugin (rule 40). The fallback must be a generic
"primary → fallback" mechanism over the existing `StreamingTTS` seam (ADR-0004), not an
ElevenLabs-specific hack, and it must add **zero** latency on the happy path.

## Decision

**Two independent, complementary changes**, both behind the existing `StreamingTTS` seam.

### 1. Automatic TTS failover (the durable fix): `FailoverTTS`

`hermes_voip/tts/failover.py` adds `FailoverTTS(primary, fallback_factory)` — itself a
`StreamingTTS`. Its `synthesize()` returns a `TtsStream` that:

- **Buffers** each text chunk as it pulls from the agent's input iterator (the input iterator
  is single-use; the fallback needs its own copy to replay).
- Streams the **primary** provider first. On a primary failure **before any frame has been
  emitted for the utterance** (the 400 case — `urlopen` raises before audio; also timeouts /
  connection errors / any exception from the primary stream), it **logs at WARNING** (which
  provider failed, the exception, that it is falling back — the root error is logged, never
  swallowed, rule 37) and **replays the buffered + remaining text to the fallback**, yielding
  the fallback's frames. So the greeting still produces audio, via the self-host synthesiser.
- If the primary fails **after** already emitting ≥1 frame for the utterance, the partial
  audio is on the wire, so it does **not** replay that utterance (no double-speak) — it
  latches and lets subsequent utterances use the fallback.
- **Latches** per call: after the first primary failure, every later `synthesize()` on the
  wrapper goes **straight to the fallback** (no primary retry) — no mid-call flapping between
  the cloud and local voices.

**Per-call reset.** The providers are process-wide (`build_providers` is called once in the
adapter), so the latch is reset at the start of each call: `FailoverTTS` implements
`reset_failover()` (declared by a tiny `SupportsCallReset` `runtime_checkable` Protocol), and
`CallLoop.run()` calls `reset_failover_if_supported(self._tts)` at entry. A fresh call
therefore retries the primary; a provider with no such method is left untouched (no-op).

**Lazy fallback.** The fallback is built from a zero-arg `fallback_factory` invoked **only on
the first failover** and cached, so the self-host Kokoro model is **not** loaded unless and
until the primary actually fails — zero added cost (no model load, no socket, no allocation)
on the happy path.

**Wiring + config.** `HERMES_VOIP_TTS_FALLBACK` (`MediaConfig.tts_fallback`) selects the
fallback provider token; it **defaults to `sherpa-kokoro` when the primary is a cloud
provider** (`elevenlabs` / `cartesia` / `aura2`) and to `none` (disabled) otherwise. `none`
disables failover. `build_providers` wraps the primary TTS in `FailoverTTS` when a fallback
is configured, passing a `fallback_factory` that builds the fallback provider via the same
factory map (so the Kokoro licence gate still runs, lazily).

### 2. ElevenLabs `model_id` guard (fail loud, not a runtime 400)

`ElevenLabsTTS.__init__` now **rejects a `model_id` that is empty/blank or looks like a
filesystem path** (contains `/` or a backslash) with a `ValueError` naming
`HERMES_VOIP_TTS_MODEL` — turning the Kokoro-directory foot-gun into a **startup**
`ConfigError` (fail-fast, ADR-0007 surface) instead of a per-call HTTP 400 that kills the
call. The body the plugin builds for a 16 kHz G.722 call is thereby guaranteed well-formed
(valid `model_id`). A legitimate ElevenLabs model id (`eleven_flash_v2_5`,
`eleven_multilingual_v2`, …) has no slash and is unaffected.

The failover (1) is the safety net for *any* primary failure; the guard (2) eliminates the
*specific* known foot-gun early and loudly. Both ship together.

## Consequences

- **A transient cloud-TTS fault no longer drops the call.** The caller hears the self-host
  voice instead of silence; the call survives. This is the operator's core ask.
- **Latching means at most one primary attempt per call.** After a failure the call finishes
  in the fallback voice (consistent), and the next call retries the cloud — so a brief cloud
  outage degrades gracefully without per-utterance voice flapping.
- **Zero happy-path cost.** When the primary succeeds, the fallback is never constructed
  (lazy factory) and the buffer is a bounded list of the utterance's text chunks; no model
  load, no extra socket, no extra synthesis. The only overhead is retaining the (small) text
  of the in-flight utterance.
- **The shared-knob foot-gun fails at startup, not on a live call.** A `.env` mixing a
  Kokoro `HERMES_VOIP_TTS_MODEL` with `provider=elevenlabs` now raises a clear `ConfigError`
  at provider build, before any call.
- **New maintenance surface:** `FailoverTTS`, `SupportsCallReset`, the `tts_fallback` config
  field, and the `reset_failover_if_supported` call in `CallLoop.run()`. The failover wrapper
  is generic over `StreamingTTS`, so it is not ElevenLabs-specific and works for any future
  primary/fallback pair.
- **Mid-utterance failover is deliberately partial.** If the primary dies after some frames,
  that one utterance is truncated (the rest is dropped) rather than double-spoken; the call
  still recovers for subsequent utterances. Replaying mid-utterance audio without
  double-speak would require frame-accurate resumption the providers do not offer; truncate-
  then-recover is the honest, simple choice and the incident case (greeting, zero frames) is
  fully recovered.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Only fix the `model_id` 400 (no failover) | Fixes one foot-gun but leaves the call fragile to *any* cloud fault (rate-limit 429, 5xx, TLS reset, timeout). The operator's ask is "never silence on a primary failure" — that needs a fallback, not a single-bug patch. |
| Make the greeting non-fatal (swallow the synth error) | Violates rule 37 (errors swallowed) and the ADR-0002 NAT-latch rationale (a real "cannot emit RTP" must still fail). Failover *recovers with audio* instead of silently degrading to a greeting-less call. |
| Retry the same primary on 400 | A 400 `invalid_uid` is deterministic — retrying the same bad `model_id` 400s again. Retry helps only transient 5xx/timeouts and still ends in silence if the cloud stays down; the fallback covers all cases. |
| Validate `model_id` against a hard-coded allow-list of ElevenLabs model ids | ElevenLabs model ids drift (new models ship; `HERMES_VOIP_TTS_MODEL` is explicitly an operator A/B knob). An allow-list would reject valid future models. The path-shaped/empty check catches the actual foot-gun (a directory leaking in) without freezing the model catalogue. |
| Per-call `FailoverTTS` instance (build providers per call) | `build_providers` is called once in the adapter; reworking it to per-call construction would reload models / re-run licence gates every call. A process-wide wrapper with an explicit `reset_failover()` at call start is cheaper and localised. |
| Eagerly construct the fallback at startup | Loads the Kokoro model into every process even when the cloud primary never fails — wasted memory/CPU on the happy path. Lazy construction on first failover keeps the happy path free while still guaranteeing the fallback *can* load on demand. |
