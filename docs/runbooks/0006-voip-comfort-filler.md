# Runbook: VoIP dead-air comfort filler (`HERMES_VOIP_TTS_COMFORT_FILLER`)

**What it is.** An opt-in voice-UX feature that fills the *dead air* on a slow turn — the gap
of pure silence between the caller finishing and the agent's first audio (LLM think time + TTS
first-audio latency). On a phone call that silence reads as a dropped line ("hello? are you
there?"). When enabled, the call loop emits ONE short, natural human filler ("Hmm,", "Let me
see,") on the gap if it lasts longer than the configured delay, then keeps waiting for the real
reply. It fires **at most once per gap** and is cancelled the instant the real reply audio or a
barge-in arrives.

The WHY lives in **ADR-0030** (dead-air comfort filler). This runbook is the operational HOW for
the operator knobs.

> **Public repo.** No secrets here — these are a boolean, an integer, and a `|`-separated phrase
> list of generic filler words.

## The knobs

| Env var | Type | Default | Read into |
| --- | --- | --- | --- |
| `HERMES_VOIP_TTS_COMFORT_FILLER` | boolean (`true/1/yes/on` \| `false/0/no/off`) | `false` (**OFF**) | `MediaConfig.comfort_filler` |
| `HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS` | integer **ms**, `> 0` | `900` | `MediaConfig.comfort_filler_delay_ms` |
| `HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES` | `\|`-separated strings | `Hmm,\|Let me see,\|One moment,` | `MediaConfig.comfort_filler_phrases` |

All three are read by `hermes_voip.config.load_media_config` and threaded by the adapter
(`_run_call_loop`) into `CallLoop(comfort_filler=…, comfort_filler_delay_ms=…,
comfort_filler_phrases=…)` for every inbound and outbound call.

**Off = today's behaviour exactly.** When `comfort_filler` is false the call loop never even
creates the filler task — the only added work on the default path is one boolean check per
delivered turn.

Validation (fail-fast at startup, `MediaConfig.__post_init__` → `_validate_comfort_filler`):

- `comfort_filler_delay_ms <= 0` raises `ConfigError` (a non-positive dead-air threshold is
  meaningless) — **not** silently clamped;
- the phrase set must be non-empty and contain no blank phrase. A blank/unset
  `HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES` **falls back to the built-in default set** (it never
  yields an empty set); `|`-separated members are trimmed and empty members (e.g. a trailing or
  doubled `|`) are dropped.

## How it behaves (the guarantees)

- **Never on a fast reply; stands down at the reply commit.** The filler is armed when a caller
  turn is delivered and waits the delay. It covers the STT/LLM *processing* gap and stands down
  the instant the agent commits a reply (calls `speak()`) — a pending filler is cancelled then, so
  it never collides with the imminent reply. (A reply that started and finished within the delay
  is also caught by a per-gap audio latch.) Filling the sub-second TTS first-audio latency *after*
  a fast LLM is a deliberate non-goal (ADR-0030) — the reply already owns the playout lock by then.
- **At most once per gap.** The filler task fires once and returns; a still-pending slow turn
  does not loop ("hmm hmm hmm").
- **Flushable + barge-in-safe (ADR-0023/0028).** The filler routes through the same
  `speak()`/TTS/`send_audio` path as a reply, so a barge-in during the gap cancels a pending
  filler, and a filler already playing is flushed (with the ADR-0028 fade) like any agent audio.
  Because it is agent audio it correctly arms the echo gate, so the gateway reflecting the filler
  back cannot self-interrupt the call.
- **Model-appropriate, clean text (ADR-0027).** The default phrases are plain spoken words that
  read naturally on every TTS model. The per-segment audio-tag strip applies: a phrase containing
  a v3 tag (e.g. `[hesitates] hmm`) renders on `eleven_v3` and is stripped (the empty-only segment
  skipped) on Flash/Turbo/Kokoro. Emoji/markdown/URLs are stripped by `sanitize_for_speech` like a
  reply. **Do not** set a bare-bracket-tag-only phrase as a *default* for a non-v3 deployment — it
  would strip to empty and you would hear nothing.

## How to set it

Set the env vars wherever the rest of the `HERMES_VOIP_*` config lives (the gitignored `.env` the
Hermes runtime loads, or the process environment for `hermes gateway run`). Example (gitignored
`.env`, values only — no secret):

```
HERMES_VOIP_TTS_COMFORT_FILLER=true
HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS=900
HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES=Hmm,|Let me see,|One moment,
```

Then redeploy/restart the gateway so the plugin re-reads its config (the value is read at config
load; a running call keeps the settings it started with).

## How to verify

1. **Config parse (offline, deterministic):**

   ```
   uv run python -c "from hermes_voip.config import load_media_config; \
     c = load_media_config({'HERMES_VOIP_TTS_COMFORT_FILLER':'on', \
       'HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS':'1200', \
       'HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES':'uh,|hold on,'}); \
     print(c.comfort_filler, c.comfort_filler_delay_ms, c.comfort_filler_phrases)"
   ```

   Prints `True 1200 ('uh,', 'hold on,')`. A non-positive delay raises `ConfigError`:

   ```
   uv run python -c "from hermes_voip.config import load_media_config; \
     load_media_config({'HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS':'0'})"
   ```

   exits non-zero with
   `ConfigError: HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS must be positive, got 0` (the env
   parser rejects it before the dataclass; a direct `MediaConfig(comfort_filler_delay_ms=0)`
   raises the field-named `comfort_filler_delay_ms must be positive, got 0` from
   `__post_init__`).

2. **Behaviour (covered by the test suite, deterministic — injected sleep seam, no real waiting):**
   `uv run pytest tests/test_call_loop.py -k comfort_filler` proves the filler fires after the
   delay when no reply has started, does **not** fire if the reply starts first, fires at most
   once per gap, is cancelled by a barge-in, and emits nothing while off.
   `uv run pytest tests/test_config.py -k comfort_filler` proves the parse + validation.

3. **Live:** with the knob on, place a call and force a slow turn (a tool-using or long-prompt
   request). After ~900 ms of dead air the operator log shows
   `comfort filler: emitting 'Hmm,' on dead air` and the caller hears one short filler, then the
   real reply. A brisk reply shows no such line. (Live validation is pending the operator's
   redeploy + enabling the knob.)

## Tuning guidance

- **Lower delay** (e.g. 600 ms) signals liveness sooner but risks the filler stepping on a reply
  that was about to arrive. **Higher** (e.g. 1500 ms) only fires on genuinely slow turns. The
  default 900 ms is above a brisk reply's latency and below the silence threshold at which a caller
  starts to think the line dropped.
- **Phrases:** keep each ~0.3–0.8 s spoken. Fewer, neutral phrases ("Hmm,", "One moment,") wear
  better across a call than a long or distinctive line. On a v3 deployment a tag phrase can sound
  more natural; on any other model keep them plain.

## Rollback

Unset `HERMES_VOIP_TTS_COMFORT_FILLER` (or set it to `false`) and redeploy — the loop returns to
exactly today's behaviour (no filler task created). The delay/phrase knobs are then inert.
