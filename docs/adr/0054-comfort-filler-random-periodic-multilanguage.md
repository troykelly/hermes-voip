# ADR-0054: Comfort filler — random phrasing, periodic fill for long waits, multi-language, default-on

- **Date:** 2026-06-19
- **Status:** Accepted (extends ADR-0030)
- **Deciders:** agent session (VoIP voice-UX, operator media-UX report)
- **Builds on:** ADR-0030 (dead-air comfort filler), ADR-0027 (model-conditional audio tags), ADR-0028 (barge-in clean stop + flush), ADR-0023 (echo-robust barge-in), ADR-0007 (streaming TTS), ADR-0003 (cascaded media / call loop)

## Context

ADR-0030 shipped an **opt-in, off-by-default, one-shot** dead-air comfort filler: on the gap
between the caller finishing and the agent's first audio, once the gap exceeds a delay,
`CallLoop` emits ONE short filler ("Hmm,") and then keeps waiting. Operator feedback from
running the agent:

- **A long processing wait still leaves dead air.** A ~10 s LLM turn (tool use, a long
  prompt) is not covered by a single ~1 s phrase: after the one filler the caller hears ~9 s
  of silence again — the very "is the line dead?" problem ADR-0030 set out to solve, just
  shifted a second later.
- **The phrasing is mechanical.** Round-robin selection means a multi-gap call cycles the
  same short list in the same order; the operator wants it to feel less robotic.
- **It must be structured for multiple languages** (English only for now, but adding a
  language later should be a data-only change).
- **The operator wants this behaviour live by default**, not behind an opt-in flag.

Constraints that bound the answer (unchanged from ADR-0030, and binding here):

- **Every ADR-0030 safety invariant is preserved.** The filler is agent audio: it must arm
  the ADR-0023 echo gate (so the gateway reflecting it cannot self-interrupt the call), be
  flushable via ADR-0028, never speak while real agent audio is on the wire, stand down the
  instant the real reply commits or a barge-in arrives, and never leak a task past the call.
- **Best-effort (AGENTS rule 37).** A filler synthesis/send failure is logged and never
  fatal to the call; the real reply still plays.
- **Deterministic, fast tests (no real waiting).** The loop's only wall-clock dependency
  stays the injected `sleep` seam.
- **No new dependency, no vendor lock-in, gateway-agnostic.** Random selection uses the
  standard-library `random` (variety only, never security).

## Decision

Extend the ADR-0030 filler — still owned entirely by `CallLoop`, still routed through the
same `speak()`/`_speak_text`/`_play`/`barge_in()` seams — with four changes:

**1. Random phrase selection, no immediate repeat.** `_next_comfort_phrase` draws uniformly
at random from the phrase set using an injected `random.Random` (`rng`, default a fresh
`random.Random()`; tests inject a seeded one), re-drawing while the draw equals the
immediately-previous phrase (`_last_comfort_phrase`). With ≥ 2 distinct phrases this
terminates in O(1) expected draws; a single-phrase set returns that phrase (the no-repeat
rule has no alternative, so no starvation). `random` is non-cryptographic and that is
correct here — the choice is for *variety*, not security (the one `# noqa: S311` is justified
inline).

**2. Periodic fill for long waits.** `_comfort_gap` is no longer one-shot. It waits the
dead-air delay, then loops: on genuine dead air it fires one random filler, then waits
`comfort_filler_repeat_ms` and re-checks, firing a *fresh* random phrase each interval, for
as long as the gap persists — until the real reply or a barge-in/teardown cancels the task.
So a 10 s wait is filled by several short phrases instead of one phrase plus 9 s of silence.

**3. The ADR-0030 dead-air guard is preserved PER ITERATION.** Each loop iteration re-checks
`self._tts_audio_active` and *skips* the fire while agent audio is on the wire right now (a
greeting or a prior reply still playing, or the real reply's own audio). This both keeps the
filler off live speech and — unlike the old one-shot check — *recovers*: if agent audio
overlaps the delay boundary and then ends while a slow reply is still pending, a later
iteration fills the new gap (the ADR-0030 "known one-shot limitation" is closed). All three
stand-down mechanisms remain:

- `speak()` cancels a pending/playing filler at the reply commit (the primary stand-down);
- `barge_in()` cancels a pending filler and flushes a playing one (ADR-0028 fade);
- `run()`'s teardown cancels **and joins** the task on every exit path (including
  mid-playout — the task keeps its handle for its whole life), so nothing leaks.

A fired phrase still routes through `_speak_text`, so sanitisation, ADR-0027 model-
conditional tag handling, echo-gate arming and flushability are inherited, not re-implemented.

**4. Multi-language phrase sets, and default-on.** Built-in phrase sets live in a dict keyed
by language code (`_COMFORT_FILLER_PHRASES_BY_LANGUAGE`), with a rich English set as the
default. A new `HERMES_VOIP_LANGUAGE` env var (default `en`, validated against the known set)
selects the active set; adding a language later is a data-only edit to that dict.
`HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES` remains an operator override that wins over the
language default (a blank override falls back to the selected language's set). The master
switch `HERMES_VOIP_TTS_COMFORT_FILLER` now **defaults to `true`**; setting it `false`
restores exactly the pre-filler behaviour (no filler task is created).

### Config surface

| Env var | `MediaConfig` field | Default | Meaning |
| --- | --- | --- | --- |
| `HERMES_VOIP_TTS_COMFORT_FILLER` | `comfort_filler` | `true` (**ON**) | Master switch. `false` = pre-filler behaviour exactly (no filler task created). |
| `HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS` | `comfort_filler_delay_ms` | `900` | Dead-air threshold (ms) before the FIRST filler. Must be `> 0`. |
| `HERMES_VOIP_TTS_COMFORT_FILLER_REPEAT_MS` | `comfort_filler_repeat_ms` | `900` | Periodic interval (ms) between subsequent fillers on a sustained gap. Must be `> 0`. |
| `HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES` | `comfort_filler_phrases` | language default set | `\|`-separated override; one chosen at RANDOM per fire, no immediate repeat. Blank/unset → the selected language's built-in set. |
| `HERMES_VOIP_LANGUAGE` | `language` | `en` | Active language; selects the built-in phrase set. Validated against the known set. |

Parsed in `config.py` into the frozen `MediaConfig` (delay/repeat validated `> 0`; language
validated; phrases non-empty and no blank member), threaded through `adapter._run_call_loop`
into `CallLoop`. `CallLoop`'s `rng` defaults to a fresh `random.Random()` in production (no
adapter seam needed); tests inject a seeded one.

### Deliberate non-goals (unchanged from ADR-0030)

Filling the sub-second TTS first-audio latency *after* a fast LLM stays out of scope: once
`speak()` commits, the reply's `_play` holds the single playout lock through its own TTS
first-audio latency, and the filler stands down at that commit rather than racing it.

### Testing note

The periodic loop suspends on the injected `sleep` between fires; production `asyncio.sleep`
(with `repeat_ms > 0`) always suspends, so a reply/barge-in/teardown cancellation is always
delivered. Tests therefore use a *stepped* sleep seam (`_SteppedSleep`) that blocks each wait
until released one step at a time — a fake that returns from every wait synchronously would
let the loop free-run with no suspension point and is not representative of `asyncio.sleep`.

## Consequences

- **The default install now speaks.** A slow turn emits short, varied, natural fillers for
  the whole wait instead of going silent — the operator's intent. An operator who wants the
  old silence sets `HERMES_VOIP_TTS_COMFORT_FILLER=false`.
- **Cost is bounded and pay-as-you-go.** A fast turn pays nothing (the task is cancelled in
  its first sleep before any synthesis). A genuinely slow turn pays one short TTS synthesis
  per repeat interval — proportional to how long the agent keeps the caller waiting.
- **One more per-call task lifecycle**, as in ADR-0030, but now a loop rather than a one-shot:
  still bounded (one task per delivered turn), still cancelled deterministically by
  reply/barge-in/teardown, still supervised by the delivery coroutine, still never blocking
  the pipeline. The teardown cancel+join is unchanged.
- **Adding a language is data-only** (one dict entry). The structure does not commit us to
  any translation pipeline or runtime.
- **ADR-0030's "known one-shot limitation" is closed** by the per-iteration re-check.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Keep one-shot; just lengthen the single phrase | A long single utterance is unnatural, harder to barge in over cleanly, and still cannot match an arbitrary multi-second wait; periodic short phrases track the wait and stay interruptible. |
| Keep round-robin selection | Operator explicitly wants it to feel less mechanical; random with a no-immediate-repeat guard removes the audible cycle while still never stuttering the same phrase twice. |
| Cryptographic RNG (`secrets`) for selection | Pointless: the choice is cosmetic variety, not a security boundary. `random` with a seeded test injection is simpler and deterministic in tests. |
| A separate "stop after N fillers" cap | An arbitrary cap would re-introduce silence on a wait longer than N intervals; the natural stop is "until the reply audio starts", which the `_tts_audio_active` check + `speak()` cancel already give. |
| Re-check by polling a clock instead of looping on the sleep seam | `CallLoop` has no injected clock and the tests are synchronous; looping on the existing injected `sleep` is deterministic, cancels cleanly, and adds no new seam. |
| Keep it opt-in / off by default | Operator ask: this behaviour should be live by default. The off path is preserved verbatim for anyone who wants the prior silence. |
| Per-language separate ADRs / a translation service | Over-engineered for "English now, structure for more later"; a dict keyed by language code is the minimal data-only structure and introduces no provider. |
