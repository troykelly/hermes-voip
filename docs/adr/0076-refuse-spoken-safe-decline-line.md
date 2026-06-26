# ADR-0076: Speak a safe-decline line on a guard REFUSE so a false-positived caller is not met with silence

- **Date:** 2026-06-26
- **Status:** Accepted
- **Deciders:** agent session (VoIP voice-UX lane). Extends ADR-0009/0011 (injection guard
  + tool-privilege gate), ADR-0030/0054 (dead-air comfort filler), ADR-0057 (caller-silence
  reprompt / spoken goodbye), ADR-0027 (model-conditional audio tags), ADR-0023/0028
  (echo-robust barge-in + clean-stop flush).

## Context

`CallLoop._screen_and_deliver` screens every finalised caller turn through the injection
guard (`InjectionGuard.screen`). On `GuardVerdict.REFUSE` the turn is **never** forwarded to
the agent — and, before this change, the method recorded the verdict and returned WITHOUT
calling `deliver_turn` AND WITHOUT arming the dead-air comfort filler (the filler is gated
behind the non-REFUSE branch, because a refused turn produces no agent gap to fill).

The guard is a probabilistic classifier: it has false positives. A **legitimate caller**
whose ordinary sentence trips the guard therefore heard **pure dead air** — no agent reply,
no filler, no acknowledgement. The caller, hearing nothing, naturally repeats themselves,
hits the same wall, is then no-input-reprompted by the ADR-0057 watchdog ("Are you still
there?" — which is wrong; they ARE there), and is eventually hung up on. A guard
false-positive thus degraded into a dropped call with zero conversational feedback. This is
the high-severity UX defect this ADR fixes.

Constraints that bound the answer:

- **The refused turn must STILL never reach the agent.** The decline is a spoken UX
  acknowledgement only; it must not weaken the guard (ADR-0009/0011). `deliver_turn` is not
  called on REFUSE — unchanged.
- **The decline line must not confirm or coach an injection.** A genuinely adversarial
  caller must not learn that "that" was detected as an injection, nor be told how to rephrase
  it. The line simply declines ("Sorry, I can't help with that. Is there anything else?").
- **It must reuse the existing spoken-phrase machinery**, not re-implement TTS: sanitisation,
  ADR-0027 model-tag handling, ADR-0023 echo-gate arming, ADR-0028 flushability, and
  best-effort error handling (rule 37) are all inherited via `_speak_phrase_best_effort` /
  `_speak_text` (the same path the reprompt and goodbye use).
- **Honour the established phrase conventions** (ADR-0054/0057): random selection with no
  immediate repeat, off the shared injected `rng`; a language-keyed built-in set so adding a
  language later is a data-only change.
- **Deterministic, fast tests** (no real waiting), and **no new dependency / no vendor
  lock-in / gateway-agnostic**.

## Decision

On `GuardVerdict.REFUSE`, `_screen_and_deliver` speaks **exactly ONE** short language-keyed
safe-decline line via `_speak_phrase_best_effort(phrase, what="decline")` and then returns —
still WITHOUT calling `deliver_turn` and WITHOUT arming the comfort filler.

1. **Selection.** `_next_refuse_decline_phrase` draws uniformly at random from
   `_refuse_decline_phrases` using the shared `_comfort_rng`, never repeating the
   immediately-previous decline (`_last_refuse_decline_phrase`) — the identical discipline as
   `_next_comfort_phrase` / `_next_reprompt_phrase`. A single-phrase or all-duplicate set
   returns that phrase (no starvation).

2. **Spoken path.** The phrase routes through `_speak_phrase_best_effort` → `_speak_text`,
   inheriting sanitisation, ADR-0027 tags, echo-gate arming, flushability, and best-effort
   semantics (a synthesis/send failure is logged and swallowed so the call survives a TTS
   hiccup; `CancelledError` propagates for clean teardown / barge-in). This is the same seam
   the reprompt and goodbye use, so it does not cancel any concurrent watchdog task.

3. **Empty set = opt-out.** An empty `refuse_decline_phrases` tuple speaks nothing — the
   prior pure-silence behaviour — for an operator who deliberately wants no decline line. The
   built-in default set is non-empty, and the config parser falls back to the language
   default on an unset/blank override, so the line is on by default (the fix is only useful
   on by default).

4. **Language-keyed config.** `_REFUSE_DECLINE_PHRASES_BY_LANGUAGE` (English default) mirrors
   the comfort-filler mechanism; `HERMES_VOIP_REFUSE_DECLINE_PHRASES` is a `|`-separated
   operator override that wins over the language default (a blank override falls back).
   `MediaConfig.refuse_decline_phrases` carries it; the adapter passes it to `CallLoop`. The
   English default in `config.py` byte-matches `_DEFAULT_REFUSE_DECLINE_PHRASES` in
   `call_loop.py` so behaviour is identical whether constructed directly or from env.

The two existing REFUSE-related concerns (caller-active flags for the no-input watchdog, and
mutation coverage) are untouched: `_caller_active_in_window` is still set on REFUSE (the
caller DID speak), and `deliver_turn` is still suppressed.

## Consequences

- A false-positived caller now gets immediate conversational feedback and can rephrase or
  ask for something else, instead of sitting in dead air until the no-input watchdog hangs up.
- One extra short TTS synthesis fires per REFUSE. REFUSE is rare (an injection-classifier
  positive), so the cost is negligible and bounded to one phrase per refused turn.
- The guard is unchanged: a real injection attempt is still refused and never reaches the
  agent; the decline line is deliberately non-informative so it does not aid an attacker.

## Alternatives considered

- **Deliver a canned "I can't help with that" to the agent instead of speaking directly.**
  Rejected: that routes attacker-influenced state through the agent turn path (the very thing
  REFUSE exists to prevent) and couples the UX line to agent latency.
- **Arm the comfort filler on REFUSE.** Rejected: the filler is for an agent-processing gap
  ("just a moment…"), which is the wrong message — there is no pending agent turn on a REFUSE;
  the caller needs a decline, not a please-wait.
- **A single fixed decline string.** Rejected: repeated refusals on one call would sound
  mechanical; the random no-immediate-repeat set matches the established convention and reads
  more naturally.
