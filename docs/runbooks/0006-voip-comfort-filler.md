# Runbook: VoIP dead-air comfort filler (`HERMES_VOIP_TTS_COMFORT_FILLER`)

**What it is.** A voice-UX feature that fills the *dead air* on a slow turn — the gap of pure
silence between the caller finishing and the agent's first audio (LLM think time + TTS
first-audio latency). On a phone call that silence reads as a dropped line ("hello? are you
there?"). When enabled, the call loop emits a short, natural human filler ("One moment please.",
"Bear with me.") once the gap exceeds the delay, then **re-emits a fresh random phrase every
repeat interval** for as long as the gap lasts (so a long ~10 s LLM wait does not leave a long
silence), and is cancelled the instant the real reply audio or a barge-in arrives. **ON by
default** (the operator wants a slow turn to never sound dead); set
`HERMES_VOIP_TTS_COMFORT_FILLER=false` for exactly the pre-filler behaviour.

The WHY lives in **ADR-0030** (original one-shot filler) and **ADR-0054** (random phrasing,
periodic fill for long waits, multi-language phrase sets, default-on). This runbook is the
operational HOW for the operator knobs.

> **Public repo.** No secrets here — these are booleans, integers, a language code, and a
> `|`-separated phrase list of generic filler words.

## The knobs

| Env var | Type | Default | Read into |
| --- | --- | --- | --- |
| `HERMES_VOIP_TTS_COMFORT_FILLER` | boolean (`true/1/yes/on` \| `false/0/no/off`) | `true` (**ON**) | `MediaConfig.comfort_filler` |
| `HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS` | integer **ms**, `> 0` | `900` | `MediaConfig.comfort_filler_delay_ms` |
| `HERMES_VOIP_TTS_COMFORT_FILLER_REPEAT_MS` | integer **ms**, `> 0` | `900` | `MediaConfig.comfort_filler_repeat_ms` |
| `HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES` | `\|`-separated strings | the language's built-in set | `MediaConfig.comfort_filler_phrases` |
| `HERMES_VOIP_LANGUAGE` | language code (`en`) | `en` | `MediaConfig.language` |

All are read by `hermes_voip.config.load_media_config` and threaded by the adapter
(`_run_call_loop`) into `CallLoop(comfort_filler=…, comfort_filler_delay_ms=…,
comfort_filler_repeat_ms=…, comfort_filler_phrases=…)` for every inbound and outbound call.
`HERMES_VOIP_LANGUAGE` selects which built-in phrase set the parser supplies; the phrase set is
then passed to the loop (the loop is language-agnostic — it just plays the phrases it is given).

**Off = pre-filler behaviour exactly.** When `comfort_filler` is false the call loop never even
creates the filler task — the only added work on that path is one boolean check per delivered
turn.

**Default English set** (varied, so random/no-immediate-repeat selection does not sound cyclic):
`Just a moment.` · `One moment please.` · `Bear with me.` · `Let me check that for you.` ·
`Just a second.` · `Almost there.` · `Hold on a moment.` · `Let me look into that.` ·
`Give me just a second.` · `One moment.`

Validation (fail-fast at startup, `MediaConfig.__post_init__` → `_validate_comfort_filler`, and
`load_media_config` for the env layer):

- `comfort_filler_delay_ms <= 0` or `comfort_filler_repeat_ms <= 0` raises `ConfigError` (a
  non-positive dead-air / repeat interval is meaningless) — **not** silently clamped;
- `HERMES_VOIP_LANGUAGE` must be a language we have a phrase set for (currently `en`); an unknown
  code raises `ConfigError` at startup (a typo is caught, not silently defaulted);
- the phrase set must be non-empty and contain no blank phrase. A blank/unset
  `HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES` **falls back to the selected language's built-in
  set** (it never yields an empty set); `|`-separated members are trimmed and empty members
  (e.g. a trailing or doubled `|`) are dropped. An explicit set overrides the language default.

## How it behaves (the guarantees)

- **Never on a fast reply; stands down at the reply commit.** The filler is armed when a caller
  turn is delivered and waits the delay. It covers the STT/LLM *processing* gap and stands down
  the instant the agent commits a reply (calls `speak()`) — a pending/playing filler is cancelled
  then, so it never collides with the reply. Each fire also happens only if no agent audio is on
  the wire right now (it never supersedes a greeting or a prior reply still playing). Filling the
  sub-second TTS first-audio latency *after* a fast LLM stays a deliberate non-goal (ADR-0030/0054)
  — it degrades to silence (the prior behaviour), never a regression.
- **Periodic fill for long waits; random, non-repeating phrasing (ADR-0054).** On a sustained gap
  the filler re-fires a *fresh random phrase* every `HERMES_VOIP_TTS_COMFORT_FILLER_REPEAT_MS`
  until the reply audio starts, so a ~10 s wait is filled by several short phrases instead of one
  phrase plus a long silence. Phrases are chosen at random, never repeating the immediately-
  previous one, so a multi-fire / multi-gap call does not sound mechanically cyclic. Each periodic
  iteration re-checks for live agent audio and skips (then resumes when it clears), so the filler
  is never spoken over a greeting or the real reply.
- **Flushable + barge-in-safe (ADR-0023/0028).** The filler routes through the same
  `speak()`/TTS/`send_audio` path as a reply, so a barge-in during the gap cancels the filler, and
  a filler already playing is flushed (with the ADR-0028 fade) like any agent audio. Because it is
  agent audio it correctly arms the echo gate, so the gateway reflecting the filler back cannot
  self-interrupt the call. The filler task is cancelled and joined at call teardown — it never
  leaks past the call, even mid-playout.
- **Best-effort (AGENTS rule 37).** A filler synthesis/send failure is logged at warning
  (`comfort filler task failed (call continues)`) and is never fatal — the call survives a failed
  filler and still plays the real reply.
- **Model-appropriate, clean text (ADR-0027).** The default phrases are plain spoken words that
  read naturally on every TTS model. The per-segment audio-tag strip applies: a phrase containing
  a v3 tag (e.g. `[hesitates] hmm`) renders on `eleven_v3` and is stripped (the empty-only segment
  skipped) on Flash/Turbo/Kokoro. Emoji/markdown/URLs are stripped by `sanitize_for_speech` like a
  reply. **Do not** set a bare-bracket-tag-only phrase as a *default* for a non-v3 deployment — it
  would strip to empty and you would hear nothing.

## How to set it

It is on by default — set the env vars only to tune or disable it. Put them wherever the rest of
the `HERMES_VOIP_*` config lives (the gitignored `.env` the Hermes runtime loads, or the process
environment for `hermes gateway run`). Example (gitignored `.env`, values only — no secret):

```
# Tune (these are the defaults — shown for illustration):
HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS=900
HERMES_VOIP_TTS_COMFORT_FILLER_REPEAT_MS=900
HERMES_VOIP_LANGUAGE=en
# Custom phrase set (overrides the language default):
HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES=One moment please.|Bear with me.|Just a second.
# To turn it OFF entirely (pre-filler behaviour):
# HERMES_VOIP_TTS_COMFORT_FILLER=false
```

Then redeploy/restart the gateway so the plugin re-reads its config (the value is read at config
load; a running call keeps the settings it started with).

## How to verify

1. **Config parse (offline, deterministic):**

   ```
   uv run python -c "from hermes_voip.config import load_media_config; \
     c = load_media_config({'HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS':'1200', \
       'HERMES_VOIP_TTS_COMFORT_FILLER_REPEAT_MS':'2000', \
       'HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES':'uh,|hold on,'}); \
     print(c.comfort_filler, c.language, c.comfort_filler_delay_ms, \
       c.comfort_filler_repeat_ms, c.comfort_filler_phrases)"
   ```

   Prints `True en 1200 2000 ('uh,', 'hold on,')` (on by default; explicit phrases override the
   language set). A non-positive delay/repeat, or an unknown language, raises `ConfigError`:

   ```
   uv run python -c "from hermes_voip.config import load_media_config; \
     load_media_config({'HERMES_VOIP_TTS_COMFORT_FILLER_REPEAT_MS':'0'})"
   uv run python -c "from hermes_voip.config import load_media_config; \
     load_media_config({'HERMES_VOIP_LANGUAGE':'zz'})"
   ```

   exit non-zero with `ConfigError: HERMES_VOIP_TTS_COMFORT_FILLER_REPEAT_MS must be positive,
   got 0` and `ConfigError: HERMES_VOIP_LANGUAGE must be one of {en}, got 'zz'` respectively (a
   direct `MediaConfig(comfort_filler_repeat_ms=0)` raises the field-named message from
   `__post_init__`).

2. **Behaviour (covered by the test suite, deterministic — injected sleep seam, no real waiting):**
   `uv run pytest tests/test_call_loop.py -k comfort` proves the filler fires after the delay when
   no reply has started, **re-fires periodically** on a sustained gap, selects phrases at random
   with no immediate repeat, does **not** fire if the reply starts first or while agent audio is
   active, is cancelled by a barge-in, is joined (no leak) at teardown, and emits nothing while
   off. `uv run pytest tests/test_config.py -k "comfort or language"` proves the parse + validation
   (default-on, repeat interval, language selection + rejection).

3. **Live:** with the agent running (on by default), place a call and force a slow turn (a
   tool-using or long-prompt request). After ~900 ms of dead air the operator log shows
   `comfort filler: emitting 'One moment please.' on dead air` (a random phrase), and roughly every
   further 900 ms another such line with a *different* phrase, until the real reply lands. A brisk
   reply shows no such line. (Live validation is pending the operator's redeploy.)

## Tuning guidance

- **Lower delay** (e.g. 600 ms) signals liveness sooner but risks the first filler stepping on a
  reply that was about to arrive. **Higher** (e.g. 1500 ms) only fires on genuinely slow turns.
  The default 900 ms is above a brisk reply's latency and below the silence threshold at which a
  caller starts to think the line dropped.
- **Repeat interval:** how often a *fresh* phrase fills a long wait. The default equals the delay
  (one cadence). A longer repeat leaves more silence between phrases on a long wait; a shorter one
  is chattier. Each fired phrase is independently interruptible (barge-in flushes it).
- **Phrases:** keep each ~0.3–0.8 s spoken; a *varied* set (the default has ten) keeps the random
  selection from sounding cyclic. On a v3 deployment a tag phrase can sound more natural; on any
  other model keep them plain.
- **Language:** `HERMES_VOIP_LANGUAGE` selects the built-in set (only `en` today). Adding a
  language is a code change (a new entry in `_COMFORT_FILLER_PHRASES_BY_LANGUAGE`), not an operator
  knob; until then, use `HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES` to supply your own phrases.

## Rollback

Set `HERMES_VOIP_TTS_COMFORT_FILLER=false` and redeploy — the loop returns to the pre-filler
behaviour (no filler task created). The delay/repeat/phrase/language knobs are then inert.
