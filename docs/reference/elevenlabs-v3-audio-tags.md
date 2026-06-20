# Reference: ElevenLabs Eleven v3 audio tags

What audio tags are, the bracket syntax, the **v3-only** model gate, the tag vocabulary, and
the caveats — the source material behind ADR-0027 (model-conditional preserve/strip) and
ADR-0068 (prompt the agent to use tags, gated on a v3 voice). This is a **reference** note
(what ElevenLabs documents), kept current with the linked sources; the *why* lives in the
ADRs and the operational *how* (selecting the model) in `docs/runbooks/0004-voip-tts-voice.md`.

> All facts here are from the ElevenLabs sources linked at the bottom. Where ElevenLabs
> describes v3 as an **alpha** that "requires more prompt engineering" and is "not suitable
> for real-time"; on **this plugin's HTTP `/stream` path** v3 is live-validated and usable
> (first-audio ~454 ms vs Flash ~310 ms, LLM-dominated) — see ADR-0027. The "not real-time"
> caveat is about ElevenLabs' multi-context **websocket**, which the plugin does not use.

## What an audio tag is

An **audio tag** is a word or short phrase wrapped in **square brackets** that the Eleven v3
model interprets as a **delivery cue** — emotion, reaction, pacing, accent, or a sound
event — rather than text to be read aloud. Tags shape *how* the voice performs the
surrounding words.

## Syntax

- Lowercase words inside square brackets: `[laughs]`, `[whispers]`, `[sighs]`.
- Placed **inline**, anywhere in the text, including mid-sentence — the cue affects the words
  that follow it.
- Tags can be **combined** for compound delivery.
- **No SSML break tags.** v3 does not support SSML `<break>`; use audio tags, punctuation
  (ellipses `…`), and sentence structure for pauses/pacing.

Example:

```
[reassuring] Of course — I can help with that. [laughs] No trouble at all.
```

## The model gate — v3 only

Only the **Eleven v3 family** interprets audio tags. Every other model
(`eleven_flash_v2_5`, `eleven_turbo_v2_5`, `eleven_multilingual_v2`, and self-host
`sherpa-kokoro`) has **no tag vocabulary** and would **speak the bracketed word literally**
(the caller hears "laughs" for `[laughs]`).

In this plugin that gate is one predicate,
`hermes_voip.tts.elevenlabs.model_supports_audio_tags(model_id)` — True for any id starting
`eleven_v3` (case-insensitive), False otherwise. It drives **both** sides of the design so
they can never disagree:

| Side | Where | On a v3 model | On a non-v3 model |
| ---- | ----- | ------------- | ----------------- |
| **Preserve / strip** (ADR-0027) | `ElevenLabsTTS.preserves_audio_tags` → `tts/_stream.py` → `spoken_text.strip_audio_tags` | tags **preserved** to synthesis (render) | whole `[tag]` token **stripped** per whole-sentence segment (never voiced) |
| **Prompt** (ADR-0068) | `VoipAdapter._deliver_turn` gate → `_spotlight_turn` `_AUDIO_TAG_PROMPT` | the agent is **encouraged** to use tags sparingly | **no** encouragement line added |

The adapter prompt gate additionally requires `tts_provider == "elevenlabs"`, so a self-host
provider configured with a `eleven_v3`-looking model name never receives the
ElevenLabs-only prompt.

## Tag vocabulary (by category)

ElevenLabs groups tags into the categories below. The lists are **representative, not
exhaustive** — v3 tolerates many free-form cues, and effectiveness depends on the chosen
voice. The plugin's `_AUDIO_TAG_PROMPT` deliberately suggests only a **small, voice-safe**
subset (reactions + a tone + a delivery cue), avoiding the instability-prone break family and
the highly voice-dependent accent/sound-effect tags.

| Category | Example tags | Notes |
| -------- | ------------ | ----- |
| **Human reactions** (non-verbal) | `[laughs]`, `[laughs harder]`, `[starts laughing]`, `[sighs]`, `[clears throat]`, `[wheezing]` | Natural, unscripted-sounding moments. The safest, most broadly-applicable tags. |
| **Emotions** | `[curious]`, `[excited]`, `[tired]`, `[sad]`, `[crying]`, `[mischievously]`, `[reassuring]` | Set the emotional tone of the following words. |
| **Delivery direction** | `[whispers]`, `[shouts]`, `[rushed]` | Volume / energy / performance. Bounded by the voice (a calm voice may not `[shout]`). |
| **Character performance** | `[pirate voice]`, `[sarcastically]` | Shift vocal identity mid-line without changing voice. Highly voice-dependent. |
| **Accents** | `[French accent]`, `[Australian accent]`, `[Southern US accent]` | Region/accent direction; quality varies by voice and training data. |
| **Sound effects / audio events** (mainly Text-to-Dialogue) | `[applause]`, `[leaves rustling]`, `[gentle footsteps]`, `[gunshot]`, `[explosion]` | Audible events, not delivery of words. Out of scope for the phone path. |
| **Pauses** (use with care) | `[pause]` and SSML-style breaks | **Discouraged** — too many in one generation cause instability (speed-ups, artifacts). Prefer ellipses `…`. |

## Caveats & best practices (ElevenLabs)

- **Match tags to the voice's character; use them intentionally.** A meditative voice
  shouldn't `[shout]`; a serious/professional voice may ignore playful tags like `[giggles]`.
  If the voice is shouting and you tag `[whispering]`, it likely won't work.
- **Use sparingly.** Overusing tags (especially pause/break tags) introduces instability and
  audio artifacts; some experimental tags are inconsistent across voices — test before
  production. This is why `_AUDIO_TAG_PROMPT` says "at most one or two per reply, only when
  they fit".
- **The chosen voice is the most important parameter** for v3; tag effectiveness follows from
  it.
- **Stability setting.** For maximum responsiveness to tags, ElevenLabs recommends the
  **Creative** or **Natural** stability settings; **Robust** reduces responsiveness to
  directional prompts. (In the plugin, dynamism comes from `voice_settings`; see ADR-0007's
  voice-settings amendment / runbook 0004.)
- **Prompt length.** Very short prompts give inconsistent output; ElevenLabs suggests inputs
  over ~250 characters for stability. (On the phone path, replies are typically longer than a
  word, so this is rarely a problem.)
- **Alpha status.** v3 is an alpha/research-preview tier requiring more prompt engineering
  than Flash/Turbo; output quality is high but reliability/latency vary.

## How the plugin uses this

- Select v3: `HERMES_VOIP_TTS_MODEL=eleven_v3` with `HERMES_VOIP_TTS_PROVIDER=elevenlabs`
  (runbook 0004). Default stays `eleven_flash_v2_5` for lowest latency.
- On v3, the agent is prompted (per turn) to use tags sparingly and its tags render.
- On any non-v3 model, no prompt is added and any stray `[tag]` is stripped at the TTS seam,
  so a bracketed cue is **never** spoken literally — including on a v3→Kokoro failover
  (ADR-0025), where the Kokoro provider strips per its own model.

## Sources

- ElevenLabs blog — *What are Eleven v3 Audio Tags — and why they matter*:
  <https://elevenlabs.io/blog/v3-audiotags>
- ElevenLabs docs — *Prompting Eleven v3 (alpha)*:
  <https://elevenlabs.io/docs/best-practices/prompting/eleven-v3>
- ElevenLabs help — *How do audio tags work with Eleven v3?*:
  <https://help.elevenlabs.io/hc/en-us/articles/35869142561297-How-do-audio-tags-work-with-Eleven-v3>
