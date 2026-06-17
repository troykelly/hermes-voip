# ADR-0027: ElevenLabs v3 as a first-class expressive tier + model-conditional audio tags

- **Date:** 2026-06-17
- **Status:** Accepted (amends ADR-0007; relates to ADR-0025)
- **Deciders:** agent session (VoIP expressiveness), operator-directed (live-validated)

## Context

The agent (gpt-5.5) on the live gateway spontaneously emits ElevenLabs **audio tags** —
inline performance cues in square brackets like `[breath]`, `[hesitates]`, `[laughs]`,
`[sighs]`. With the gateway running the **`eleven_v3`** model, those tags **render** as the
intended vocal performance, and the operator confirms they are very effective. The operator's
ask: make v3 audio tags "a first-class capability — add to the palette and documentation".

Two things had to be reconciled:

1. **ADR-0007 (and runbook 0004) recorded that `eleven_v3` "can't do real-time" and is
   "unusable on the phone path".** That conclusion was drawn from ElevenLabs' statement about
   its multi-context **websocket** and is now **corrected by live validation**: `eleven_v3`
   works on **this plugin's HTTP `/stream` path** (the path the plugin actually uses — the
   websocket restriction does not apply to us). Measured first-audio on our HTTP `/stream`:
   **Flash ~310 ms, v3 ~454 ms** — both fine on the phone path, because Hermes' LLM
   round-trip dominates end-to-end latency either way. (`eleven_v3_conversational` is a
   different, Agents-platform-only model: it returns **401** with a standard TTS key and is
   out of scope — Hermes *is* the agent, so the Agents platform is not used.)

2. **Audio tags were a sharp edge.** The spoken-text sanitiser (ADR-0007 / PR #80,
   `spoken_text.py`) strips emoji/markdown/URLs but **passed a bare `[tag]` straight through
   to TTS for every model**. That is correct for v3 (it interprets the tag) but **wrong** for
   Flash / Turbo / Multilingual v2 / Kokoro, which have no tag vocabulary and would **speak
   the bracketed word literally** — the caller would hear the word "breath" for `[breath]`.
   Because the agent emits tags spontaneously, any non-v3 deployment (including the
   **`sherpa-kokoro` failover** under ADR-0025) would voice them.

## Decision

**Both ElevenLabs models are first-class, operator-selectable tiers via
`HERMES_VOIP_TTS_MODEL`:** `eleven_flash_v2_5` (low-latency ~310 ms, the default) and
`eleven_v3` (expressive, renders audio tags, ~454 ms first-audio). v3 is **not** rejected as
"unusable" — it is a supported expressive option on our HTTP streaming path.

**Audio-tag handling is MODEL-CONDITIONAL**, decided from the configured model id and applied
**inside the synthesising provider**, at the segment it synthesises:

- A model that **supports** audio tags (the **v3 family** — `eleven_v3` and any future
  `eleven_v3*`) **PRESERVES** tags through to synthesis so they render.
- A model that does **not** (`eleven_flash_v2_5`, `eleven_turbo_v2_5`,
  `eleven_multilingual_v2`, `sherpa-kokoro`) **STRIPS** the whole `[tag]` token (brackets and
  the word) so nothing is voiced literally.
- Emoji / markdown / URL stripping is **unchanged in both cases** (ADR-0007 / PR #80 is not
  regressed): that stays in the provider-agnostic call-loop layer; only the tag decision is
  per-model.

Concrete shape (`src/hermes_voip/`):

- `spoken_text.strip_audio_tags(text)` — removes audio-tag-*shaped* brackets only
  (`_RE_AUDIO_TAG`: opens with an ASCII letter, then letters/spaces/apostrophes/hyphens, ≤31
  inner chars), so a numeric footnote `[3]` or a long bracketed aside is left intact.
  `sanitize_for_speech` is untouched and still passes bare tags through.
- `tts/elevenlabs.py` — `V3_MODEL_ID = "eleven_v3"`, `model_supports_audio_tags()` (matches
  `eleven_v3*`, case-insensitive), and an `ElevenLabsTTS.preserves_audio_tags` property.
- `tts/sherpa_kokoro.py` — `preserves_audio_tags` is **always `False`**.
- `tts/_stream.PcmFrameStream` — a `preserve_audio_tags` flag (default `False`) that strips
  tags **per whole-sentence segment** (so a tag split across the agent's streamed chunks is
  reassembled by the segmenter *before* removal), skipping a segment that was only a tag.
- `tts/failover.FailoverTTS.preserves_audio_tags` mirrors the primary (duck-typed
  `SupportsAudioTags`).

**The capability signal flows from the configured model id to the synthesiser** and the
strip/keep decision is made **inside each provider** — never hard-coded and never made once
up-front in the call loop. This is what makes the **failover path correct for free**: each
provider strips/keeps per its *own* model, so when a `eleven_v3` primary fails over to the
`sherpa-kokoro` fallback (ADR-0025), Kokoro strips the tags on the **replayed** utterance —
the per-utterance sanitisation always matches the provider actually producing audio, even on
a mid-call provider switch.

## Consequences

- **Expressive voice is a real, documented option.** Setting `HERMES_VOIP_TTS_MODEL=eleven_v3`
  gives the agent's spontaneous `[breath]`/`[laughs]`/… real vocal performance; the default
  stays Flash for lowest latency. Documented in `README.md`, `docs/runbooks/0004-voip-tts-voice.md`,
  and `.env.example`.
- **No model ever speaks a tag literally.** On Flash/Turbo/Multilingual/Kokoro the tags are
  removed before synthesis, including on the Kokoro failover, so a v3-tuned agent prompt is
  safe on any model.
- **A new per-model capability to maintain.** Adding a future tag-capable model means
  extending `model_supports_audio_tags` (already forward-compatible with `eleven_v3*`). A
  future non-ElevenLabs provider gets the safe default (`preserves_audio_tags = False`) and
  strips tags until taught otherwise.
- **The tag matcher is intentionally narrow** (audio-tag-shaped brackets only). A genuinely
  exotic tag name with digits/punctuation, or a tag >31 chars, would not be stripped on a
  non-v3 model; the canonical ElevenLabs vocabulary is all covered, and widening the matcher
  risks eating legitimate bracketed prose.
- **ADR-0007's "v3 unusable" line is superseded** for the HTTP-streaming path: v3 is usable
  here. `eleven_v3_conversational` remains excluded (Agents-platform-only / 401).
- **Latency is per-deployment, LLM-dominated.** v3's ~454 ms first-audio vs Flash's ~310 ms
  is immaterial next to the Hermes LLM turn; both are well within the phone-path budget.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Keep passing tags through for all models (status quo) | Non-v3 models speak `[breath]` literally — the live defect this fixes. |
| Strip tags for **all** models (never render them) | Throws away v3's headline expressive feature the operator validated as effective. |
| Decide tag fate **once in the call loop** from a single capability flag, before `synthesize` | Wrong under ADR-0025 failover: the call loop sanitises before the provider, but a v3 primary can fail over to Kokoro mid-call, so the already-preserved tags would reach Kokoro and be spoken. The decision must live with the provider that actually synthesises. |
| Add `preserves_audio_tags` to the core `StreamingTTS` Protocol | Unnecessary — the call loop does not read it (the decision is internal to each provider); it would force every test fake and future provider to implement it for no functional gain. Kept as a duck-typed capability (`SupportsAudioTags`) instead. |
| Strip tags **per streamed chunk** (before the segmenter) | A tag split across two agent chunks (`…[bre` + `ath]…`) would leak. Stripping per whole-sentence segment reassembles it first. |
| Adopt `eleven_v3_conversational` for richer dialogue | 401 with a standard TTS key (Agents-platform only); Hermes is the agent, so the Agents platform is out of scope (rule 40 — no new platform without an ADR). |
