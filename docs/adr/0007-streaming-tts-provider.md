# ADR-0007: Streaming TTS: self-host sherpa-onnx + Kokoro (default); Cartesia/Aura-2/ElevenLabs cloud fallbacks

- **Date:** 2026-06-14
- **Status:** Accepted (amended by ADR-0022)
- **Deciders:** agent session (VoIP architecture, post-research)

> **Amendment (ADR-0022, 2026-06-17):** the TTS output rate **follows the negotiated codec**.
> The single codec-gated rate hook introduced by the PR #82 choppiness fix
> (`ElevenLabsTTS(output_sample_rate=…)`) is generalised: the negotiated wire rate is threaded
> into the call loop and passed to the synthesiser per call via an optional
> `StreamingTTS.synthesize(..., sample_rate=…)` argument. ElevenLabs emits the negotiated rate
> natively (8 kHz for G.711 — preserving the no-resample choppiness fix — 16 kHz for G.722, so
> wideband is not downsampled away). Kokoro's intrinsic 24 kHz is downsampled by the engine to
> the wire rate (24→16 for G.722, 24→8 for G.711). See ADR-0022.

## Context

The cascaded media path (ADR-0003) ends in text-to-speech: the Hermes agent produces text,
and we must turn it into 8 kHz telephony audio the caller hears. The conversational latency
budget (ADR-0003) requires a TTS first-audio-byte target of 100–300 ms and a total
silence→first-caller-audio target under ~800 ms–1 s, so the synthesiser **must stream** —
emit audio chunks as the agent's text arrives, not after a whole utterance. It must also be
**cancellable mid-sentence** so the barge-in path (ADR-0008) can stop speaking the instant
the caller talks over the agent. Hermes' own `tts_provider.stream()` hook has no consumer and
its `send_voice`/`play_tts` path is whole-file batch (verified against
`NousResearch/hermes-agent@main`), so we own the streaming TTS implementation behind our
`StreamingTTS` interface (ADR-0004) rather than relying on the runtime's batch TTS.

Three constraints bind the choice:

- **Licensing for a PUBLIC repo and commercial operation.** A model whose *weights/voices*
  carry a non-commercial or copyleft-viral licence is disqualified for the default and for
  any committed config — the same trap as on the STT side (ADR-0006). The engine licence and
  the model/voice licence are independent and both must clear.
- **No new infra without operator approval (rules 40/41).** The default must run in-process
  in the Hermes runtime; any out-of-process or GPU media server is operator-gated and ADR-recorded.
- **Verify on the real target (rules 23/24/26).** All vendor "first-byte"/latency figures are
  model-only marketing numbers measured on the vendor's native sample rate; our path adds a
  24→8 kHz downsample and G.711 encode, so every number below is a *target to re-measure*, not
  an accepted fact.

A single hard-wired engine would be wrong: telephony deployments range from a CPU-only
box to a GPU host to "operator already pays a cloud TTS vendor", and the gateway is
vendor-agnostic. The provider must be swappable per deployment.

## Decision

The default `StreamingTTS` provider is **self-hosted**: the **sherpa-onnx** runtime driving
the **Kokoro-82M** voice model (both Apache-2.0), synthesising per sentence and delivering
audio through sherpa-onnx's streaming chunk callback — returning a non-zero/stop value from
that callback cancels synthesis at a chunk boundary, which is our native barge-in primitive
(ADR-0008). Two additional self-host tiers and three cloud fallbacks sit behind the same
interface, selected by env var; no provider is hard-wired.

**Self-host tiers (default path, in-process, no operator infra gate):**

- **Default (Kokoro):** sherpa-onnx + Kokoro-82M. Apache-2.0 engine *and* Apache-2.0 weights.
  Best quality-per-CPU of the unencumbered options; runs CPU-only, faster on GPU.
- **No-GPU / high-concurrency tier:** **Piper** with the **`en_US-libritts/high`** voice (MIT
  engine; this voice is trained from scratch and is **explicitly NOT** the
  `lessac`/Blizzard-derived voice, whose source corpus is non-commercial) — or **KittenTTS**
  (Apache-2.0). Chosen when many concurrent calls must share CPU.
- **Premium GPU tier:** **Kyutai TTS** (CC-BY-4.0 weights) restricted to **CC0/CC-BY voice
  packs only** — the Expresso voices are CC-BY-NC and are **disallowed** in committed config.

**Cloud fallbacks (behind the same `StreamingTTS` interface; each is operator-opt-in):**

- **Cartesia Sonic 3.5** — native `pcm_mulaw`/`alaw` output (no client-side codec needed),
  explicit `cancel`+`flush` control for clean barge-in.
- **Deepgram Aura-2** — native mulaw output, `Clear` message for the cleanest barge-in;
  pairs with Deepgram Flux (ADR-0006) as a **single-vendor** STT+TTS option.
- **ElevenLabs Flash v2.5** — an opt-in cloud fallback, selected at runtime via the
  `ELEVENLABS_API_KEY` env var (value lives only in the gitignored `.env` / 1Password, fetched
  via the `op` CLI — never committed; rules 34/41).

**Disqualified outright** (non-commercial / copyleft-viral *weights or voices* — never the
default, never committed config, fail the CI licence assertion): **Coqui XTTS-v2** (CPML),
**F5-TTS base** (CC-BY-NC), **Fish-Speech / OpenAudio S1** (CC-BY-NC-SA), **ChatTTS**
(AGPL + CC-BY-NC).

**Interface and configuration.** The provider conforms to `StreamingTTS` (ADR-0004) — the
canonical seam is **not** redefined here. `StreamingTTS`, `TtsStream`, and `PcmFrame` live in
ADR-0004 and are imported, not redeclared. The class name is `StreamingTTS`; `synthesize` is a
synchronous factory returning a `TtsStream`; `cancel()` and `flush()` live on the **`TtsStream`**
(not the provider); and the provider emits `PcmFrame`s at its declared `output_sample_rate`
(24 kHz for Kokoro). The 24→8 kHz downsample and G.711 encode are **media-layer** work
(ADR-0005), never the provider's output type — so there is no provider-side `pcm_8k`/`TtsChunk`.
The concrete implementation against ADR-0004's exact interface:

```python
from __future__ import annotations

from collections.abc import AsyncIterator

from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.tts import StreamingTTS, TtsStream


class SherpaKokoroTTS:
    """Self-hosted Kokoro-82M synthesiser (StreamingTTS, ADR-0004).

    Emits 24 kHz PcmFrames; barge-in cancellation maps to returning a stop value
    from the sherpa-onnx chunk callback. The 24->8 kHz downsample + G.711 encode
    are the media layer's job (ADR-0005), not this provider's.
    """

    def __init__(self, model_id: str) -> None: ...

    def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
        """Stream token/sentence text in, stream 24 kHz PcmFrames out.

        Returns a `TtsStream` (async iterator of `PcmFrame`) that also exposes
        `flush()` and `cancel()` for barge-in (ADR-0008); the engine begins
        emitting audio before `text` completes.
        """
        ...

    @property
    def output_sample_rate(self) -> int:
        """24 kHz: the media layer downsamples to 8 kHz for G.711 (ADR-0005)."""
        return 24_000


# Structural conformance is enforced by mypy and the runtime_checkable Protocol:
_: type[StreamingTTS] = SherpaKokoroTTS
```

Selection is by env var, defaulting to the self-host Kokoro path with **no cloud network
dependency**:

- `HERMES_VOIP_TTS_PROVIDER` — one of `sherpa-kokoro` (default), `piper`, `kittentts`,
  `kyutai`, `cartesia`, `aura2`, `elevenlabs`.
- `HERMES_VOIP_TTS_MODEL` / `HERMES_VOIP_TTS_VOICE` — model + voice pack id (e.g. the pinned
  Kokoro model id; for Kyutai a CC0/CC-BY voice id only).
- Cloud keys read at runtime, never committed: `ELEVENLABS_API_KEY`, plus
  `HERMES_VOIP_CARTESIA_API_KEY`, `HERMES_VOIP_DEEPGRAM_API_KEY` (the latter shared with
  ADR-0006 when Flux+Aura-2 run as one vendor).

**Media glue we own.** Self-host engines emit 24 kHz (Kokoro) PCM; we **downsample 24→8 kHz
and G.711 µ-law/A-law encode** with `audioop-lts` (`ratecv` + `lin2ulaw`/`lin2alaw`) before
the chunk reaches the transport (ADR-0005) — Python 3.13 removed stdlib `audioop`, so
`audioop-lts` is a pinned dependency. Native-mulaw cloud vendors (Cartesia, Aura-2,
ElevenLabs PCM/mulaw) skip the encode step. We keep **the first sentence(s) of each response
short** so that `cancel()` at a sentence boundary stays responsive — a long opening sentence
delays both first audio and barge-in.

**CI licence assertion.** A test asserts the configured `HERMES_VOIP_TTS_MODEL` /
`HERMES_VOIP_TTS_VOICE` resolves to an allow-listed licence (Apache-2.0, MIT, CC0, CC-BY-4.0),
and that the disqualified set above can never be the committed default — mirroring the STT
licence gate (ADR-0006) and the project's licence-gating rule (rule 35). The gate pins the
**exact** model/voice artifact — the source repo (e.g. `hexgrad/Kokoro-82M` for the default
weights, the Piper voices repo for `en_US-libritts/high`), a **pinned revision**, the specific
model/voice file names, and their **checksums** — not the generic names "Kokoro-82M" or
"en_US-libritts/high"; the exact revisions + checksums are recorded at implementation so CI
verifies those artifacts, not a name. Provider work is TDD (rule 18): a failing test for
streaming order, `TtsStream.flush()` end-of-utterance framing, and mid-stream
`TtsStream.cancel()` lands before implementation.

## Consequences

- **Default ships with zero cloud dependency, zero per-minute cost, and no egress of caller
  audio** — fully self-contained in the Hermes runtime, no operator infra gate (rules 40/41).
  Barge-in is a first-class engine feature (sherpa-onnx chunk callback), not a bolted-on hack.
- **We own and must maintain the media glue**: sentence segmentation of the agent's text
  stream, the 24→8 kHz + G.711 encode (`audioop-lts`), and the cancel-at-boundary plumbing.
  This is the cost of streaming + barge-in that the Hermes batch TTS does not give us.
- **CPU cost is real on the default path.** Kokoro CPU synthesis competes with sherpa-onnx STT
  (ADR-0006) and the in-process SIP/RTP stack (ADR-0005) on one event loop; high concurrency
  pushes deployments to the Piper/KittenTTS tier or to a GPU host (operator-gated infra) or to
  a cloud fallback. Concurrency headroom must be measured on the real 8 kHz path (rule 23).
- **All latency targets are unverified until measured on our path** (rule 26): the 100–300 ms
  first-byte and <800 ms–1 s end-to-end figures are model-only vendor claims; the downsample +
  encode adds real time we must benchmark and report as a number, not "done".
- **Licence discipline is permanent**: any new voice/model must clear the CI assertion; the
  disqualified non-commercial set stays banned from committed config even if quality tempts.
- **Cloud fallbacks trade money + egress + lock-in for quality/latency and lower CPU.**
  Choosing Deepgram Flux (ADR-0006) + Aura-2 collapses STT+TTS to one vendor and one key,
  simplifying ops at the cost of single-vendor dependence. ElevenLabs Flash v2.5 is an opt-in
  cloud fallback selected at runtime via `ELEVENLABS_API_KEY`, but it is a paid external SaaS
  and is not the default.
- **Upgrade cadence**: pinned sherpa-onnx runtime + pinned Kokoro/Piper/KittenTTS model ids in
  the lockfile (rule 33); model bumps are deliberate, licence-re-checked commits.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| ElevenLabs Flash v2.5 as the **default** | Paid external SaaS with per-character cost, vendor lock-in, and caller-audio egress; violates the no-default-cloud posture (rules 40/41). Kept as an opt-in cloud fallback, selected at runtime via `ELEVENLABS_API_KEY`. |
| Cartesia Sonic 3.5 as the **default** | Excellent (native mulaw, clean cancel/flush) but still a paid cloud vendor with egress + lock-in; same objection as ElevenLabs. Retained as a strong opt-in fallback. |
| Deepgram Aura-2 as the **default** | Native mulaw and cleanest `Clear` barge-in, and pairs with Flux as a single vendor — but cloud, paid, egress. Kept as the opt-in single-vendor STT+TTS pairing. |
| Coqui XTTS-v2 (CPML) | Non-commercial weight licence — disqualified for a commercial, public-repo project; would fail the CI licence assertion. |
| F5-TTS base (CC-BY-NC) | CC-BY-NC weights — non-commercial; disqualified. |
| Fish-Speech / OpenAudio S1 (CC-BY-NC-SA) | CC-BY-NC-SA weights — non-commercial + share-alike viral; disqualified. |
| ChatTTS (AGPL + CC-BY-NC) | AGPL code plus CC-BY-NC weights — copyleft-viral *and* non-commercial; disqualified. |
| Kyutai Expresso voices | The Expresso voice packs are CC-BY-NC — banned from committed config; Kyutai is allowed only with CC0/CC-BY voice ids. |
| Piper `en_US-lessac` voice | Derived from the Blizzard/lessac corpus whose terms are non-commercial; we use the from-scratch `en_US-libritts/high` (MIT) voice instead. |
| A single hard-wired engine | Deployments span CPU-only, GPU, and "operator already pays a cloud vendor"; one fixed engine cannot serve all and contradicts the swappable-provider seam (ADR-0004). |


## Amendment (2026-06-17): ElevenLabs requests the telephony wire rate (codec-gated; `pcm_8000` for G.711), not `pcm_24000`

- Deciders: agent session (operator-directed live "very choppy" audio fix)

A live call on the ElevenLabs fallback was "very choppy". Two things compounded:
the per-chunk re-framing padding (fixed in ADR-0017's 2026-06-17 amendment), and
the fact that the provider was requesting **`output_format=pcm_24000`** (24 kHz
PCM16). Per the original decision above, the canonical provider seam is PCM16 and
"the 24→8 kHz downsample and G.711 encode are media-layer work", so ElevenLabs
asked for 24 kHz and the media layer downsampled 3:1 per streamed chunk.

For a chunked-HTTP cloud provider that can natively emit the telephony rate, that
24 kHz request is pure cost: 3× the bytes egressed from ElevenLabs to the box, a
lossy 3:1 `audioop.ratecv` pass over every small streamed chunk, and extra
first-audio latency — with **no quality benefit** on an 8 kHz G.711 wire (the
narrowband codec discards everything above ~3.4 kHz regardless). ElevenLabs
supports `pcm_8000` directly.

**Decision:** the ElevenLabs provider requests PCM16 at the **telephony wire
rate** — `output_format=pcm_8000`, `output_sample_rate = 8000` **by default**.
The frames it emits are then already at the G.711 wire rate, so
`RtpMediaTransport._to_wire_rate` takes its byte-exact fast-path (no resampler is
constructed for the stream) and the media layer only G.711-encodes. We keep
**PCM16** (not the also-native `ulaw_8000`) because the codec is the media layer's
job (ADR-0004) and `PcmFrame` is the PCM16 currency — requesting raw µ-law would
break that contract and force a passthrough path through the whole TTS→media seam
for marginal gain (one `lin2ulaw` call).

**Codec-gated, not a hardcoded narrowband pin.** The requested rate is the
**G.711 case of a codec→rate mapping**, not an unconditional `8000`. It is 8 kHz
because the SDP codec menu this plugin advertises today (`adapter._SUPPORTED_
ENCODINGS` = PCMU/PCMA/telephone-event) is G.711-only, so the negotiated wire is
always 8 kHz (RFC 3551 PCMU/PCMA are defined at 8000 Hz). The provider takes the
rate as a constructor argument (`output_sample_rate`, default
`G711_NARROWBAND_RATE`); `_make_elevenlabs_tts` passes the G.711 narrowband rate
explicitly, with that build site documented as the single place to derive the
rate from the negotiated codec. A `pcm_<rate>` format helper
(`elevenlabs_pcm_format`) maps the rate to ElevenLabs' supported PCM formats
(8000/16000/22050/24000/44100) and **raises** for an unsupported rate (fail fast
at construction, never a silent lossy fallback). This matters because **ADR-0005
mandates wideband** ("prefer Opus, negotiate by capability"): when that lane lands
and the menu advertises G.722/Opus, the TTS rate must FOLLOW the negotiated codec
(G.722→16 kHz `pcm_16000`; Opus→48 kHz, resampled) — burying an unconditional
8 kHz request here would instead throw negotiated wideband away by downsampling
TTS to 8 kHz. The mapping makes that generalization a one-line extension at the
build site, not a rewrite.

This is a deliberate, **narrow** refinement of ADR-0017 alternative #2 ("resample
inside the provider — rejected, keeps providers gateway-specific"). Asking a cloud
TTS for the *negotiated wire rate* is **not** gateway-specific — it couples the
provider to the telephony wire in general (whatever codec was negotiated), not to
any one gateway. The self-host engines (Kokoro 24 kHz) are unchanged — they have
no native telephony-rate mode and the media layer still reconciles their rate
(ADR-0017). Only the cloud providers that natively offer the wire rate (ElevenLabs
`pcm_8000`/`pcm_16000`/…; Cartesia/Aura-2 native mulaw, already noted above)
request it.

Consequences: zero outbound resample on the ElevenLabs path at the G.711 rate,
~3× less provider egress bandwidth, lower first-audio latency, and one fewer lossy
DSP stage — all while the `StreamingTTS`/`PcmFrame` seam (ADR-0004) is unchanged
(only the provider's declared `output_sample_rate` and the requested
`output_format` differ). Tests pin the default `output_format == "pcm_8000"` /
`output_sample_rate == 8000` so a regression to 24 kHz fails the suite, AND assert
a non-default rate (16 kHz) requests `pcm_16000` so the rate is provably codec-
driven rather than a constant.

## Amendment (2026-06-17): ElevenLabs voice is **dynamic by default** + tunable (`voice_settings`), not a flat Flash default

- Deciders: agent session (operator-directed "the voice is flat" feedback)

The operator found the live ElevenLabs voice **flat**. Root cause: the provider's
synthesis request body carried only `{text, model_id}` and **no `voice_settings`**,
so ElevenLabs applied its own default `stability=0.5` — which its docs describe as
possibly producing "a monotonous voice". We were never sending the dynamism
controls at all.

**Research finding (verified against current ElevenLabs docs, 2026-06-17) — the fix
is settings, not a model swap.** The genuinely more *expressive* models do not fit
the real-time phone path: `eleven_v3` (the expressive tier) **cannot stream in real
time** (its multi-context websocket is unavailable; ElevenLabs explicitly states it
"can't do real-time"), and `eleven_turbo_v2_5` is **superseded by Flash**
("we recommend using the Flash models over Turbo in all use cases"; Turbo's only
edge is marginal prosody at ~250–300 ms vs Flash ~75 ms). `eleven_multilingual_v2`
streams but at several-hundred-ms first-audio — a real regression on a call. So
`eleven_flash_v2_5` stays the default model (the only one that is both real-time and
ElevenLabs-recommended for voice agents), and the dynamism comes from
`voice_settings` — chiefly **lowering `stability`**, which broadens emotional range
at *no latency cost*.

**Decision.** The ElevenLabs provider now sends a `voice_settings` object on every
request, defaulting to a **dynamic-but-stable** set: `stability=0.35` (below the flat
0.5), `similarity_boost=0.75`, `style=0.0` (kept 0 to protect telephony first-audio
latency + stability), `use_speaker_boost=true`. The model id, every `voice_settings`
field, and the (deprecated) `optimize_streaming_latency` query param are
**operator-tunable via env without a code change** — the surface the operator A/B's
on live calls:

- `HERMES_VOIP_TTS_MODEL` — ElevenLabs model **id** (default `eleven_flash_v2_5`).
  (For the self-host `sherpa-kokoro` provider this same key is a model *directory* —
  the meaning is provider-specific.)
- `HERMES_VOIP_TTS_STABILITY` / `_STYLE` / `_SIMILARITY` — floats in `[0.0, 1.0]`.
- `HERMES_VOIP_TTS_SPEAKER_BOOST` — boolean.
- `HERMES_VOIP_TTS_STREAMING_LATENCY` — int in `[0, 4]`; **unset by default**
  (deprecated param; `4` disables number/date normalisation, wrong for a
  receptionist). Each tuning knob defaults to **None** at the config layer, meaning
  "the provider supplies its dynamic default for that field", so a bare ElevenLabs
  install is already livelier than the API default rather than flat.

`voice_settings` and `model_id` are **body** fields; `output_format` and
`optimize_streaming_latency` are **query** params (ElevenLabs API reference). The
streaming websocket-style architecture and the `pcm_8000` no-resample choppiness fix
are unchanged. A voice swap (`HERMES_VOIP_TTS_VOICE`) is an independent lever; a
premade shortlist (Rachel/Sarah/Jessica/Laura/Bill) is documented in
`docs/runbooks/0004-voip-tts-voice.md`. Note the account's TTS-scoped key cannot
enumerate voices (`/v1/voices` → `missing_permissions: voices_read`), so a new voice
id is confirmed by a successful live synth, and the **default voice stays Rachel**
(verified working live) — the dynamism default applies to whatever voice is set.

Consequences: the default ElevenLabs voice is materially more dynamic out of the box
(a lower-stability delivery on the same model/voice) at no latency cost, and the
operator can tune dynamism per deployment from env alone. Validation is the
operator's live A/B after redeploy (rule 26). Tests pin that the body now carries
`voice_settings`, that the default is the dynamic set (a regression to the flat
no-settings default fails the suite), that the knobs validate ranges fail-fast, and
that `_make_elevenlabs_tts` threads them in with per-field fallback to the dynamic
default.
