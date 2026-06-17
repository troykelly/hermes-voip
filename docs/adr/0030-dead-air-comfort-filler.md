# ADR-0030: Dead-air comfort filler — one short, flushable, model-aware filler on the turn gap

- **Date:** 2026-06-17
- **Status:** Accepted
- **Deciders:** agent session (VoIP voice-UX, operator media-UX report)
- **Builds on:** ADR-0023 (echo-robust barge-in), ADR-0028 (barge-in clean stop + flush), ADR-0027 (model-conditional audio tags), ADR-0007 (streaming TTS), ADR-0003 (cascaded media / call loop)

## Context

On a live call there is a gap between the caller finishing their turn and the agent's first
audio. The caller's end-of-turn is detected by the endpointer/ASR and delivered to Hermes
(`CallLoop._screen_and_deliver` → `deliver_turn` → `VoipAdapter.handle_message`); the agent
then runs an LLM turn and only later replies through `VoipAdapter.send` → `CallLoop.speak()`,
whose first frame reaches the wire via `_play`. The STT is already done by the gap; the gap is
the **LLM think time plus TTS first-audio latency**. On a slow turn (tool use, a long prompt,
a cold model) this is multiple seconds of complete silence. On a phone call — no visual
"typing" indicator — silence reads as a *dropped line*: the caller says "hello? are you
there?" or hangs up. A human on a call fills that gap involuntarily ("hmm", "let me see…", an
audible breath).

Constraints that bound the answer:

- **Must not fire on a fast reply.** A reply that comes back in a few hundred ms needs no
  filler; a filler that races a prompt reply would step on the agent's own opening word.
- **Must not collide with barge-in (ADR-0023/0028).** The filler is agent audio the gateway
  reflects back into our VAD/ASR; it must arm the echo gate exactly like any other agent audio
  (so it cannot self-interrupt the call), and it must be *flushable* — a real barge-in during
  the gap must cut the filler cleanly, not leave it playing.
- **Must be model-appropriate and clean (ADR-0027).** On an ElevenLabs v3 model a bracket tag
  like `[hesitates]` renders as a real hesitation; on Flash/Turbo/Kokoro a bracket tag would be
  *spoken literally* unless stripped. The filler text must read naturally on any model.
- **Opt-in, configurable, and a no-op when off** — `off` must be byte-for-byte today's
  behaviour (rule 6: no behaviour change for the default install).
- **At most once per gap** — never "hmm hmm hmm" while a very slow turn churns.

## Decision

Add an **optional, opt-in dead-air comfort filler** owned entirely by `CallLoop`, scheduled
around the turn hand-off and cancelled the instant the real reply (or a barge-in) arrives.

**Where it fires.** `CallLoop._screen_and_deliver` is the seam: it is the last point before
the turn leaves for the agent (`await self._deliver_turn(text)`), and the gap closes when the
agent's reply reaches `speak()` → `_play`'s first sent frame. When the filler is enabled and a
turn is being delivered, `_screen_and_deliver` launches a single one-shot **filler task** that:

1. waits `delay_ms` (default **900 ms**) on an **injected sleep seam** (`_sleep`, defaulting to
   `asyncio.sleep`; tests inject a controllable sleep for determinism);
2. after the wait, fires the filler **only if** no agent audio is already on the wire
   (`self._tts_audio_active is False` — set true by `_play` on the first real reply frame) **and**
   the gap has not been cancelled. Both checks are atomic relative to `speak()`/`barge_in()`
   because everything runs on one asyncio event loop with no preemption;
3. synthesises exactly **one** short filler utterance through the *same* `speak()` path the agent
   replies use, so the filler is registered as `_active_tts_stream`, plays under the playout lock,
   and arms the echo gate (`_tts_audio_active`) like any agent audio.

The task fills the gap **at most once** and then returns; a still-pending turn does not loop.

**When the filler stands down (no race).** The filler covers the caller-finish→reply
*processing* gap — STT/LLM think time, the operator's "while STT/LLM are processing" — and
stands down the instant the agent has a reply to speak:

- A real reply arriving **during the filler's delay wait** (the common case: the LLM finished
  before the threshold) cancels the pending filler in `speak()`, so it never starts. The agent
  has the floor and the reply is imminent.
- A real reply arriving **while the filler is playing** supersedes it via the existing
  `speak()`/`_speak_text` cancel-previous-stream logic *and* the `speak()` filler-cancel — the
  filler stops yielding and the reply plays under the single playout lock.
- A barge-in **during the gap** cancels the pending filler (nothing queued to flush if it never
  started) and, if the filler is already playing, flushes it through the ADR-0028 fade path.
- `run()`'s teardown cancels any lingering filler task on every exit path — including one
  **mid-playout** (its task handle is kept for its whole life, not dropped when it fires) — so a
  filler can never outlive the call.

A per-gap **latch** (`_gap_reply_audio_started`, set by the first *real* reply frame in `_play`,
reset when a new gap is armed) backs this up: the filler's post-delay check skips firing if reply
audio has begun during the gap, covering even a reply that started *and finished* within the
delay window (where the transient `_tts_audio_active` would already be back to false). Suppression
is keyed on reply **audio** for the completed-reply case, and on the `speak()` commit for the
pending case.

Because the filler routes through `_speak_text`/`_play`/`barge_in()`, *flushability and echo-gate
arming are inherited, not re-implemented*.

**Deliberate non-goal: filling TTS first-audio latency after a *fast* LLM.** Once the agent calls
`speak()`, the reply's `_play` holds the single playout lock through its own TTS first-audio
latency (~300–450 ms in practice; below the 900 ms default). The filler therefore covers the
think-time gap and **stands down at the `speak()` commit**, rather than playing a filler that
would race the imminent reply and contend on the lock. Covering that sub-second post-commit
latency would require restructuring the playout-lock model (a real risk to the echo-gate /
barge-in invariants) for marginal benefit, so it is explicitly out of scope.

**Model-aware, clean text (ADR-0027).** The filler text is synthesised through
`self._tts.synthesize(...)`, so the per-segment `strip_audio_tags` in `tts/_stream.py` already
applies: a bracket tag survives on a v3 model and is stripped (and the empty-only segment
skipped) on every other model. The default phrase set is chosen to read naturally on **any**
model without a tag — a plain spoken `"Hmm,"` / `"Let me see,"` / `"One moment,"` — so the
default never depends on tag rendering. An operator may set a phrase that *includes* a v3 tag
(e.g. `[hesitates] hmm`) and it will render on v3 and strip cleanly on Flash/Kokoro. The
phrases are also passed through `sanitize_for_speech` by `speak()` (emoji/markdown/URL strip),
exactly as agent replies are. The single phrase per gap is chosen by rotating a per-call index
so a multi-gap call does not repeat the same word every time.

### Config surface

| Env var | `MediaConfig` field | Default | Meaning |
| --- | --- | --- | --- |
| `HERMES_VOIP_TTS_COMFORT_FILLER` | `comfort_filler` | `false` (**OFF**) | Opt-in master switch. Off = today's behaviour exactly (no filler task is even created). |
| `HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS` | `comfort_filler_delay_ms` | `900` | Dead-air threshold (ms): how long the gap must last before one filler fires. Must be `> 0` (validated, fail-fast). |
| `HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES` | `comfort_filler_phrases` | `("Hmm,", "Let me see,", "One moment,")` | `|`-separated phrase set; one is chosen per gap (round-robin per call). Empty/unset → the built-in default set. Each phrase is non-empty after trim. |

Parsed in `config.py` into the frozen `MediaConfig`, threaded through `adapter._run_call_loop`
into `CallLoop` (`comfort_filler`, `comfort_filler_delay_ms`, `comfort_filler_phrases`). When
`comfort_filler` is false the `CallLoop` never schedules a filler task — the off path adds one
boolean check per delivered turn and nothing else.

## Consequences

- The default install is **unchanged** (filler off): the only added work on the default path is a
  single `if self._comfort_filler:` guard in `_screen_and_deliver`. Pinned by a test asserting no
  extra synthesis when off.
- When on, a slow turn now emits one short, natural human filler on dead air and then continues
  waiting for the real reply, so the caller does not think the line dropped. Verified by
  deterministic tests using an injected sleep seam: the filler fires after the delay when no reply
  has started; does **not** fire if the reply audio starts before the delay; fires **at most once**
  per gap; is cancelled by `barge_in()`; and is superseded by a real reply mid-play.
- The filler is agent audio, so it correctly arms the ADR-0023 echo gate (the gateway reflecting
  the filler back cannot self-interrupt the call) and is flushable via ADR-0028. No new coupling to
  the engine is introduced — the filler reuses `speak()`/`_play`/`barge_in()` unchanged.
- We commit to maintaining one more per-call task lifecycle. It is bounded (one task per delivered
  turn, fires once, cancelled deterministically by reply/barge-in/teardown), supervised by the same
  delivery coroutine, and never blocks the pipeline (it runs concurrently with the agent turn).
- The filler adds a small TTS synthesis cost only when it actually fires (one short utterance on a
  genuinely slow turn); a fast turn pays nothing because the task is cancelled in its sleep before
  any synthesis.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Fire the filler from the adapter (`send()` / `handle_message`) instead of `CallLoop` | The gap's two ends — turn hand-off and first reply frame — both live inside `CallLoop`, as do `speak()`'s supersede and `barge_in()`'s flush. Owning it in the adapter would duplicate the stream-supersede/flush coordination and re-cross the loop boundary; `CallLoop` already has every seam. |
| Pre-synthesise/cache the filler audio and inject raw PCM straight into the engine | Bypasses `speak()`/`_play`, so it would not arm the echo gate or be flushable, and would not pick up model-conditional tag handling or sanitisation — re-implementing three existing behaviours. Routing through `synthesize()` inherits them. |
| A real wall-clock timestamp at turn delivery, polled against `time.monotonic` | `CallLoop` has no injected clock and tests are synchronous; a polled timestamp needs a clock seam *and* a polling loop. A single `await self._sleep(delay)` with an injected `_sleep` is simpler, deterministic in tests, and cancels cleanly. |
| Loop the filler ("hmm" every N ms until the reply lands) | The operator explicitly forbids "hmm hmm hmm"; one filler per gap is enough to signal liveness, and repeated agent audio increases echo-gate load and the chance of stepping on the reply. |
| Always-on (no opt-in), or default-on | Rule 6 + operator ask: off must be exactly today's behaviour. A filler changes what the caller hears, so it is opt-in and defaults off. |
| Emit a bracket tag (`[breath]`) as the default filler | A non-v3 model would speak it literally unless stripped; once stripped on Flash/Kokoro the segment is empty and nothing is heard — a silent "filler" defeats the purpose. The default is plain spoken text that works on every model; a tag is available to operators who run v3. |
