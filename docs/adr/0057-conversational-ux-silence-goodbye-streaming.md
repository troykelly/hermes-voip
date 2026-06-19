# ADR-0057: Conversational UX — caller-silence reprompt, spoken goodbye, reply-streaming feasibility

- **Date:** 2026-06-19
- **Status:** Accepted
- **Deciders:** agent session (VoIP voice-UX, launch-requirements conversational-UX cluster)
- **Builds on:** ADR-0003 (cascaded media / call loop), ADR-0023 (echo-robust barge-in),
  ADR-0028 (barge-in clean stop + flush), ADR-0026 (call-termination Hermes signal),
  ADR-0030 + ADR-0054 (dead-air comfort filler), ADR-0007 (streaming TTS), ADR-0027
  (model-conditional audio tags)

## Context

Three audit-confirmed conversational-UX gaps on a live call, all owned by `CallLoop`:

1. **A live-but-silent caller gets no handling.** When inbound RTP keeps flowing (silence /
   comfort-noise packets) but the caller never produces an end-of-turn, nothing is delivered
   to Hermes and the agent waits in dead air indefinitely. The RTP-inactivity watchdog in the
   engine (ADR-0026) only fires on **DEAD** media (no packets at all), not on silent-but-live
   media, so a caller who has stopped talking — set the phone down, walked away, a one-way
   audio fault — strands the line until the gateway's own session timer eventually reaps it.

2. **A normal end drops straight to BYE with no closing line.** When the call winds up, the
   caller hears the line cut with no goodbye. A human ends a call with a closing word; a
   silent cut reads as a fault. The post-hangup suppression (ADR-0026/PR#133) makes any reply
   *after* teardown a no-op (no media path), so a goodbye must be **pre-BYE**, while the media
   path is still live.

3. **First-audio latency on a reply (the ~10 s dead air seen live).** The operator reported
   long silence after the caller finishes before the agent's voice starts. The question: does
   the Hermes runtime deliver the reply **incrementally** (so the plugin can synthesise TTS
   sentence-by-sentence and start audio within one sentence), or only as a **complete string**?

Constraints that bound the answers (AGENTS.md): rule 6 (wire end-to-end, no stubs; name a
real blocker, don't fake it), rule 18 (TDD, deterministic seams), rule 23 (verify, don't
assume — and don't add a redundant optimisation), rule 37 (errors propagate / best-effort is
logged, never swallowed), and the in-flight scope split that assigns `adapter.py` / `config.py`
to other lanes (so this work lives in `call_loop.py` and is reachable via `CallLoop` defaults).

A read-only investigation of `adapter.py` established the seam: the adapter runs
`await CallLoop.run()` inside a `try/finally` and **classifies the end by whether `run()`
returns vs raises** (a raise ⇒ `PIPELINE_FAILURE` ⇒ `/stop`). It calls no
close/finish/goodbye method on the loop and speaks nothing itself on a normal end; it stops
the media engine only **after** `run()` returns. A caller-hangup and a clean inbound-EOS are
both `REMOTE_BYE` — the taxonomy does not distinguish "caller vanished" from "we ended
gracefully" on the clean-return path. Therefore a spoken goodbye and a silent-caller end must
both be produced **inside `run()`, before the TaskGroup completes**, and a loop-initiated end
must make `run()` return **cleanly**.

## Decision

Add three behaviours to `CallLoop`, all default-ON with built-in English phrases, all driven
off the existing injected `sleep` seam and the existing `speak()`/`_speak_text` path so they
inherit flushability (ADR-0028), echo-gate arming (ADR-0023) and model-conditional tags
(ADR-0027). The adapter constructs `CallLoop` with explicit kwargs, so these new defaulted
kwargs are **live on every call**; threading operator env knobs / language-selected phrase
sets through `MediaConfig` is a data-only follow-on in the `config.py` lane (exactly as the
comfort-filler phrases were threaded after ADR-0030/0054).

### 1. Caller-silence reprompt / no-input handling

A whole-call watchdog task `_no_input_gap`, armed at `run()` start (when enabled) **outside**
the `TaskGroup` and **cancelled + joined** in `run()`'s `finally` (no leak, like the comfort
filler). Each iteration clears a per-window activity flag, `await self._sleep(timeout)`, then:

- if the caller showed life during the window (`_caller_active_in_window`, set by a delivered
  turn in `_screen_and_deliver` **or** a barge-in in `barge_in()`), reset the reprompt count
  and re-arm — a caller who is talking is plainly still there;
- else if agent audio is on the wire right now (`_tts_audio_active`), skip the window (do not
  speak over a reply/greeting/prior reprompt; the budget is not spent — no reprompt was heard);
- else, on genuine dead air: if the reprompt budget is not spent, speak ONE random reprompt
  (`_next_reprompt_phrase`, no immediate repeat — the comfort-filler discipline) and continue;
  once the budget (`no_input_max_reprompts`) is spent, **end the call gracefully** (§2).

A barge-in stands a pending reprompt down (the activity reset) and cancels a *playing*
reprompt (it is the active TTS stream). A reprompt synthesis/send failure is caught, logged,
and the loop continues (rule 37); `CancelledError` (teardown) is the normal stop.

### 2. Spoken goodbye before BYE, on a loop-initiated graceful end only

`_end_call_gracefully` speaks `goodbye_phrase` via `_speak_text` and lets it **fully flush**
(`_speak_text` returns only once the stream is drained) **before** setting a new
`asyncio.Event` `_end_call`. The pump checks `_end_call.is_set()` at the top of its inbound
`async for` and `break`s to its existing end-of-stream-marker emission, so the chain drains
and `run()` returns **cleanly** — a normal `REMOTE_BYE` end, never a raise. The goodbye is
therefore the last audio on the wire while the media path is still live (the adapter stops the
engine only after `run()` returns).

The goodbye fires **only** on this loop-initiated end (the no-input limit). A caller-hangup /
inbound-EOS / error end is driven from *outside* the loop (the transport closes / the pipeline
raises) — there is no live media path there, so no goodbye is spoken. This is the honest scope
of "normal/agent-initiated end with a media path": on the clean-return path the loop cannot
tell "caller vanished" from "we ended gracefully", so the only end where a goodbye provably
reaches the caller is the one the loop itself initiates.

On a real silent-but-live call inbound RTP keeps flowing, so the pump observes `_end_call`
within ~one frame; if inbound were truly dead the engine's RTP watchdog (ADR-0026) ends the
call instead, so the graceful end never relies on dead media.

### 3. Reply streaming — runtime-blocked for true streaming; the mitigation is already in place

**Verified against hermes-agent 0.16.0 and the plugin TTS layer (rule 23).** Two findings:

- **True plugin-side incremental delivery is RUNTIME-BLOCKED (rule 6 — named).** The runtime
  delivers the agent reply to a platform adapter's `send(chat_id, content: str)` as **one
  complete string, called once per turn** (`gateway/platforms/base.py`, final send
  `gateway/run.py`). The runtime *does* generate token-by-token internally
  (`agent/chat_completion_helpers.py` `_fire_stream_delta`), but that stream is bridged to a
  platform **only** through `gateway/stream_consumer.py` `GatewayStreamConsumer`, an
  edit-in-place renderer that (a) is gated off for non-editable platforms
  (`gateway/run.py` raises *"skip streaming for non-editable platform"* unless
  `SUPPORTS_MESSAGE_EDITING`) and (b) flushes **cumulative** text on a time/codepoint
  threshold via `edit_message`, never per-sentence to `send()`. The TTS delta hook
  (`run_conversation(stream_callback=…)`) is **never passed** by the gateway on the platform
  path. **The gap:** there is no plugin-registerable per-sentence/per-token TEXT callback
  wired through the gateway platform path. Closing it would require patching the runtime
  (forward `stream_delta_callback` to the voip adapter, or pass `stream_callback` into
  `run_conversation`), which is out of scope for a plugin.

- **The best available mitigation is ALREADY IMPLEMENTED in the plugin TTS layer.** Once the
  complete reply string reaches `CallLoop._speak_text → tts.synthesize()`, the TTS layer
  splits that single chunk into sentences (`tts/segment.py` `SentenceAggregator`, with a
  clause-split on the **first** segment ≤ `DEFAULT_FIRST_SEGMENT_MAX_CHARS` explicitly for
  first-audio latency) and `tts/_stream.py` `PcmFrameStream` opens + streams each segment's
  audio **before** synthesising the next (ElevenLabs / Kokoro make one streamed request per
  sentence). First-audio latency is therefore already ~**one short sentence**, not the whole
  reply. A call-loop sentence-splitter would **duplicate** this and is rejected (rule 23). The
  remaining pre-string wait (LLM think time) is covered by the comfort filler (ADR-0030/0054).

**No production change is made for §3.** A call-loop regression-guard test pins the
sentence-by-sentence pipelining end-to-end (first-sentence audio on the wire before the later
sentence is opened) so a future `call_loop.py` change cannot silently regress first-audio
latency back to whole-reply synthesis.

### Config surface (CallLoop kwargs; `MediaConfig`/env plumbing is a config.py follow-on)

| CallLoop kwarg | Default | Meaning |
| --- | --- | --- |
| `no_input_reprompt` | `True` | Caller-silence watchdog master switch. Off ⇒ no watchdog task is created (the prior behaviour). |
| `no_input_timeout_ms` | `10000` | Silence window (ms) of no end-of-turn before a reprompt. Must be `> 0`. |
| `no_input_max_reprompts` | `2` | Unanswered reprompts before the graceful end. `>= 0` (`0` ⇒ straight to goodbye + end on the first silent window). |
| `no_input_reprompt_phrases` | `("Are you still there?", …)` | Reprompt set; one chosen at random per fire (no immediate repeat). Multi-language-ready like the comfort-filler phrases. |
| `goodbye` | `True` | Speak a closing line on the loop-initiated graceful end. Off ⇒ the end still happens, silently. |
| `goodbye_phrase` | `"Goodbye."` | The closing line. |

The window and phrases reuse the comfort-filler RNG (`_comfort_rng`) and the shared `sleep`
seam; tests inject a seeded RNG and a stepped/gated sleep for determinism.

## Consequences

- A silent caller is noticed and handled: one or more spoken reprompts, then a graceful,
  goodbye-capped wind-up instead of an indefinite dead-air hang. The reprompt/goodbye are real
  agent audio, so they arm the echo gate and are flushable — a caller who answers a reprompt
  barges in and is heard, exactly like a reply.
- The default install changes what the caller hears (default-ON): a silent line now gets a
  reprompt and a goodbye. This is the intended launch behaviour; an operator can turn either
  off. (The comfort filler is likewise default-ON since ADR-0054.)
- One more per-call task lifecycle to maintain (the watchdog). It is bounded (one task per
  call, armed once, cancelled + joined at teardown), best-effort (a failure is logged, never
  fatal), and never blocks the pipeline (it runs concurrently).
- The graceful-end termination depends on inbound RTP flowing (the pump observing `_end_call`).
  This is sound: a silent-but-live caller has continuous RTP, and a truly dead line is ended
  by the engine's RTP watchdog instead. The watchdog never raises on a normal end, so the
  adapter's `REMOTE_BYE` classification is preserved (no spurious `/stop`).
- Reply-streaming is recorded as runtime-blocked with the specific API gap. If a future
  hermes-agent release exposes a per-sentence text callback to platform plugins, the plugin can
  consume it without touching the already-correct TTS pipeline; until then, the per-sentence
  TTS pipeline + comfort filler is the ceiling and is in place.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Add a call-loop sentence-splitter that calls `synthesize` once per sentence | The TTS layer already splits a single chunk into sentences and pipelines per-segment audio (verified); a splitter would duplicate it for zero first-audio benefit (rule 23). |
| Patch the hermes-agent runtime to forward token deltas / pass `stream_callback` to the voip adapter | Out of scope for a plugin and a vendor-lock risk (rule 40); recorded as the named blocker (rule 6) instead. |
| Speak the goodbye on every normal end (incl. caller-hangup / EOS) | There is no live media path once the caller is gone (post-BYE / closed inbound) — the goodbye would play to nothing. Only the loop-initiated end has a guaranteed media path. |
| End the call by cancelling the pump task / the TaskGroup | A cancelled TaskGroup raises `CancelledError` from `run()`, which the adapter classifies as `PIPELINE_FAILURE` ⇒ `/stop` — the opposite of a graceful normal end. A pump-observed `_end_call` Event lets the existing end-of-stream markers flow so `run()` returns cleanly. |
| Make the pump race the inbound `__anext__` against `_end_call` | Re-introduces the cross-task generator-`aclose` hazard the module docstring carefully avoids (ADR-0023 §barge-in). A top-of-loop check on the existing `async for` is safe and minimal; inbound cadence on a live call makes it prompt, and dead media is the RTP watchdog's job. |
| Drive the no-input timer from a wall clock polled against `time.monotonic` | The loop has no injected clock and tests are deterministic on the `sleep` seam; a single `await self._sleep(window)` per iteration with the injected seam is simpler and cancels cleanly (same rationale as ADR-0030). |
| Reset the silence window on raw VAD onset rather than barge-in / delivered-turn | `barge_in()` and `_screen_and_deliver` are the existing "caller took the floor" / "caller finished a turn" signals and cover both silence and over-the-agent speech; hooking raw VAD would duplicate the barge-in gate's echo-vs-speech discrimination (ADR-0023). |
