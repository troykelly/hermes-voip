# ADR-0068: Prompt the agent to use ElevenLabs v3 audio tags (gated on the active v3 TTS)

- **Date:** 2026-06-20
- **Status:** Accepted (extends ADR-0027; relates to ADR-0020/ADR-0021 spotlight, ADR-0029)
- **Deciders:** agent session (VoIP expressiveness), operator-approved (ship as proposed)

## Context

ADR-0027 made audio-tag **handling** model-conditional: on an ElevenLabs **v3-family**
model the synthesiser **preserves** inline audio tags (`[laughs]`, `[sighs]`, `[whispers]`,
…) so they render as vocal performance; on every non-v3 model the TTS seam
(`spoken_text.strip_audio_tags`, applied per whole-sentence segment inside the provider)
**strips** the whole `[tag]` token so a bracketed cue is never voiced literally. That work
was purely defensive — it made tags *safe*, and relied on the agent **spontaneously**
emitting them.

Spontaneous emission is unreliable. On a v3 deployment the expressive headline feature is
left to chance: many turns carry no tags at all, so the voice sounds flat even though the
model can perform. ElevenLabs' own v3 prompting guidance is explicit that tags are the
intended control surface for delivery, that they should be **matched to the voice's
character** and **used intentionally** (a meditative voice shouldn't `[shout]`; overusing
break/`[pause]`-type tags causes instability/artifacts), and that v3 is the **only** tier
that interprets them. So the right move is to **ask** the agent — once, briefly, and only
when the active voice can actually render tags — to use them, sparingly.

This must not regress the safety ADR-0027 established, and must not leak onto a non-v3 voice
(where the same line would invite the agent to emit `[laughs]` that the strip then has to
remove — wasted tokens and a contradictory instruction).

## Decision

When the **active TTS is an ElevenLabs v3-family model**, append a short, fixed
**encouragement line** to the spotlighted turn preamble that tells the agent it has an
expressive voice and may use ElevenLabs-style audio tags **sparingly**. Gate it so it is
emitted **only** on v3; on every other configuration the line is omitted **and** the
existing strip still scrubs any stray tag — a deliberately **two-sided** design.

### The gate (computed per turn in `VoipAdapter._deliver_turn`)

```python
v3_audio_tags = (
    (mc := self._media_cfg) is not None
    and mc.tts_provider == "elevenlabs"
    and model_supports_audio_tags(mc.tts_model or "")
)
```

- `model_supports_audio_tags` is imported from `hermes_voip.tts.elevenlabs` — the **same
  predicate** the TTS seam uses to decide preserve-vs-strip
  (`ElevenLabsTTS.preserves_audio_tags`). Routing both sides through one function means the
  prompt and the strip **can never disagree**: if we prompt for tags, the seam preserves
  them; if we don't, the seam strips them.
- Both conjuncts are required. A self-host provider (`sherpa-kokoro`) whose
  `HERMES_VOIP_TTS_MODEL` happens to read `eleven_v3` still gates **False** (provider check
  fails first), so a non-ElevenLabs voice never gets the ElevenLabs-only prompt.
- `mc is None` (before media is configured) ⇒ False. Resolved from the **live** media
  config each turn, so an operator who swaps the model/provider gets the matching behaviour
  on the next turn without restart-coupling.

### The injection point (`_spotlight_turn`)

`_spotlight_turn` gains a keyword-only `v3_audio_tags: bool = False`. When True it appends
`_AUDIO_TAG_PROMPT` to the **trusted framing**, i.e. **after** the per-group persona
preamble (and the outbound objective / `report_call_result` framing) and **before** the
`<<<UNTRUSTED_CALLER_TRANSCRIPT>>>` fence. The line is fixed system text about *delivery*,
never caller-supplied content, so it rides **outside** the untrusted fence — it cannot be
forged by a malicious callee, and the spotlight boundary (ADR-0020/0021) is preserved. The
default `False` keeps every existing call site (and every non-v3 turn) byte-for-byte
unchanged.

### The approved preamble text (`adapter._AUDIO_TAG_PROMPT`)

> This call uses an expressive voice. To sound natural you may add ElevenLabs-style audio
> tags — short cues in square brackets such as `[laughs]`, `[sighs]`, `[whispers]`,
> `[reassuring]`, or `[clears throat]` — placed inline right before the words they affect.
> Use them sparingly (at most one or two per reply, only when they fit); they're spoken as
> delivery, not read aloud. Don't use any other bracketed markup.

The example set is deliberately **small and voice-safe** — reactions (`[laughs]`,
`[clears throat]`), non-verbals (`[sighs]`), a delivery cue (`[whispers]`), and a tone cue
(`[reassuring]`) — drawn from ElevenLabs' documented categories, avoiding the
instability-prone `[pause]`/break family and exotic accent/sound-effect tags that depend
heavily on the chosen voice. "Sparingly (at most one or two per reply)" implements
ElevenLabs' *use intentionally / match the voice* guidance and bounds the recurring token
cost. "Don't use any other bracketed markup" steers the agent away from emitting bracketed
tokens the seam would otherwise strip (footnotes, stage directions) on a v3 voice.

## Consequences

- **A v3 voice now actually sounds expressive**, on purpose, not by luck — the agent is
  told it has an expressive voice every turn it runs on v3.
- **No behaviour change off v3.** Flash/Turbo/Multilingual/Kokoro turns are unchanged: no
  extra line, and the ADR-0027 strip still removes any stray tag. The two-sided gate means
  the only place the prompt appears is exactly the place the seam preserves tags.
- **Single source of truth.** The adapter gate and the TTS-seam strip both call
  `model_supports_audio_tags`; a regression test asserts they agree for representative ids
  (`eleven_v3`, `eleven_v3_preview` → prompt+preserve; `eleven_flash_v2_5`,
  `eleven_multilingual_v2`, `eleven_turbo_v2_5` → no-prompt+strip), so they cannot drift.
- **Recurring token cost.** The line (~70 words) is prepended to every v3 turn. Accepted:
  it is small relative to the persona preamble + untrusted transcript already in each turn,
  and it only ships on v3 (the expressive deployment that opted into the cost). A
  send-once-per-call variant was considered and rejected (below).
- **Failover interaction is safe.** If a v3 primary fails over to Kokoro mid-call
  (ADR-0025), the *prompt* may already have ridden in earlier turns, but the Kokoro seam
  **strips** the tags the agent emits — so the fallback caller still never hears a literal
  `[laughs]`. The prompt is advisory; the strip is the guarantee. (Subsequent turns after
  the latched failover are still computed from `tts_provider`/`tts_model`, which name the
  *primary*; this is the documented, accepted limitation of the prompt side — the strip side
  remains correct because each provider strips per its own model.)
- **Advisory, not enforced.** The agent may ignore the line, over-tag, or emit a tag the
  voice renders poorly. The downside is bounded: a poorly-matched tag is an odd delivery,
  not a literal spoken word; "sparingly" plus the small example set minimise it. This is the
  same advisory posture as the rest of the spotlight preamble (the enforced boundary is the
  `privileged` clamp, not the prose).

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Prompt for tags on **all** models (unconditional) | On non-v3 it invites tags the seam must then strip — wasted tokens, a self-contradictory instruction, and zero benefit (the voice can't render them). |
| Keep ADR-0027 as-is (preserve/strip only; never prompt) | Leaves the expressive feature to the agent's whim; many v3 turns carry no tags and sound flat. The operator asked to actively encourage tags. |
| Drop the strip now that we prompt (rely on the prompt alone) | A prompt is advisory; the agent (or a Hermes proactive notice, or a failover to a non-tag model) can still emit a bracketed token. The strip is the only *guarantee* a non-v3 voice never speaks one. Keep both — prompt-gate **and** strip-fallback. |
| Compute the gate from a standalone capability flag instead of `model_supports_audio_tags` | Would create a second source of truth that could disagree with the seam's preserve/strip decision. Reusing the one predicate makes drift impossible. |
| Inject the line **inside** the untrusted fence (with the transcript) | It is trusted delivery guidance; inside the fence the agent is told to treat it as untrusted data, and a callee could appear to "issue" it. Trusted framing belongs outside the fence. |
| Send the encouragement **once per call** (e.g. only in the context seed) instead of every turn | Cheaper in tokens, but the spotlight re-establishes the persona **every** turn precisely because per-turn context is what steers the model; a once-only delivery hint fades. Chose per-turn for reliability; the cost is bounded and v3-only. Revisit if token cost becomes a measured problem. |
| A larger / richer example set (accents, sound effects, `[pause]`) | ElevenLabs notes break/`[pause]` tags cause instability and that accent/SFX tags are highly voice-dependent; a small reaction/tone set is the safe, broadly-applicable subset. |

## References

- ADR-0027 — model-conditional audio-tag preserve/strip (the safety foundation this extends).
- `docs/reference/elevenlabs-v3-audio-tags.md` — the v3 audio-tag vocabulary, syntax, the
  v3-only model gate, and caveats, with ElevenLabs source URLs.
- ElevenLabs: *What are Eleven v3 Audio Tags* — <https://elevenlabs.io/blog/v3-audiotags>
- ElevenLabs docs: *Prompting Eleven v3 (alpha)* —
  <https://elevenlabs.io/docs/best-practices/prompting/eleven-v3>
- ElevenLabs help: *How do audio tags work with Eleven v3?* —
  <https://help.elevenlabs.io/hc/en-us/articles/35869142561297-How-do-audio-tags-work-with-Eleven-v3>
