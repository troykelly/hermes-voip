# Runbook: ElevenLabs dynamic-voice tuning

**What it is.** How to make the ElevenLabs telephony voice **more dynamic** (less "flat") and
how to A/B-test voice / model / `voice_settings` on **live calls without a code change**, all
via the `HERMES_VOIP_TTS_*` environment surface. The plugin streams ElevenLabs over the phone
path with low first-audio latency (Flash v2.5 + `pcm_8000`); this runbook tunes the
*expressiveness* of that stream.

This runbook is the operational HOW. The WHY lives in **ADR-0007** (TTS provider choice + the
ElevenLabs amendments) and **ADR-0022** (codec-gated output rate).

> **Public repo — secrets are NAMES only here.** No host, extension, token, or key value
> appears in this file. The ElevenLabs key lives only in the gitignored `.env` / 1Password
> (item `ElevenLabs API - Avery Hermes TTS`, vault `Claude API Access`, field `api_key`),
> read into `ELEVENLABS_API_KEY`. Never `echo`/`print`/log a fetched value.

## TL;DR — the fix for "flat"

The voice was flat because the provider sent **no `voice_settings`**, so ElevenLabs applied
its own `stability=0.5` default ("can result in a monotonous voice"). The shipped default is
now **dynamic-but-stable** (`stability=0.35`), which broadens the emotional range at **no
latency cost**. To go further or to tune live, set the env knobs below and have the operator
redeploy + call.

```
HERMES_VOIP_TTS_PROVIDER=elevenlabs        # cloud provider (needs ELEVENLABS_API_KEY)
HERMES_VOIP_TTS_VOICE=21m00Tcm4TlvDq8ikWAM # voice id (Rachel baseline; swap from the list below)
HERMES_VOIP_TTS_STABILITY=0.35             # main dynamism dial: LOWER = more expressive
HERMES_VOIP_TTS_STYLE=0.0                  # 0 on telephony; 0.10-0.15 adds drama (costs latency)
HERMES_VOIP_TTS_SIMILARITY=0.75            # clarity / similarity to the source voice
HERMES_VOIP_TTS_SPEAKER_BOOST=true         # subtle similarity boost
# HERMES_VOIP_TTS_MODEL=eleven_flash_v2_5  # keep Flash (see "Model choice")
# HERMES_VOIP_TTS_STREAMING_LATENCY=1      # deprecated; leave UNSET (see "Latency")
```

Every knob is optional: **unset => the dynamic default for that field**. A bare ElevenLabs
install is already livelier than the API default. Out-of-range values fail fast at startup
(`ConfigError`): floats must be in `[0.0, 1.0]`, `*_STREAMING_LATENCY` an int in `[0, 4]`.

## Model choice — keep Flash v2.5, tune the voice (do NOT swap to v3 or Turbo)

Verified against current ElevenLabs docs (2026-06-17):

| Model | Streams in real time? | Latency | Expressiveness | Verdict |
| --- | --- | --- | --- | --- |
| `eleven_flash_v2_5` (default) | Yes (HTTP `/stream` + WS) | ~75 ms model | Lowest of the lineup | **Keep.** Only model that is both real-time *and* ElevenLabs-recommended for voice agents. |
| `eleven_turbo_v2_5` | Yes | ~250-300 ms | Marginally above Flash | Superseded — ElevenLabs: "use the Flash models over Turbo in all use cases." |
| `eleven_multilingual_v2` | Yes | Several-hundred ms | High | Only viable *streaming* step-up, but a real first-audio regression on a phone call. Off-path A/B only. |
| `eleven_v3` | **No** (multi-context WS unavailable; "can't do real-time") | Higher | Most expressive | **Unusable on the phone path** — would break streaming. |

So the dynamism win on telephony comes from **`voice_settings`**, not a model swap.
`HERMES_VOIP_TTS_MODEL` (the model **id** for ElevenLabs — it is a model **directory** only for
the self-host `sherpa-kokoro` provider) stays at `eleven_flash_v2_5`; change it only for a
deliberate off-path experiment.

## voice_settings — what each knob does

Sent in the request **body** on every synthesis (ElevenLabs API ref). Floats are `0.0-1.0`.

- **`HERMES_VOIP_TTS_STABILITY`** — the primary dynamism dial. *Lower* = broader emotional
  range; *too low* = inconsistent / artefact-prone between generations. ElevenLabs default is
  `0.5` (flat); our default is `0.35`. Try `0.30-0.40`. Costs **no** latency.
- **`HERMES_VOIP_TTS_STYLE`** — style exaggeration. `0.0` is the telephony-safe default: any
  value above 0 makes the model *less stable* and **may add latency**. Raise to `0.10-0.15`
  only if `stability=0.35` alone is not lively enough, and re-check first-audio latency.
- **`HERMES_VOIP_TTS_SIMILARITY`** — clarity / similarity to the source voice. Default `0.75`;
  very high can over-enunciate or reproduce source artefacts.
- **`HERMES_VOIP_TTS_SPEAKER_BOOST`** — boosts similarity to the source speaker (subtle); small
  latency cost. Default `true`. Accepts `true/false/1/0/yes/no/on/off`.

## Latency — leave `optimize_streaming_latency` unset

`HERMES_VOIP_TTS_STREAMING_LATENCY` maps to ElevenLabs' `optimize_streaming_latency` **query**
param (int `0-4`). It is **deprecated** and **unset by default** — Flash + `pcm_8000` already
keep first-audio latency low. If a measured first-audio number is ever too high, `1` is the
safe step (keeps the text normaliser on). **Never `4`** on a phone agent: it disables the text
normaliser, so numbers/dates/extensions get mispronounced (bad for a receptionist).

## Starter voices (ElevenLabs) — swap with one env var

Set `HERMES_VOIP_TTS_VOICE` to a voice id. The plugin accepts **any** ElevenLabs `voice_id`,
so this list is only a **starting point** — you can point it at one of your own **custom or
cloned voices** the same way. The `HERMES_VOIP_TTS_*` dynamic settings (`STABILITY`, `STYLE`,
`SIMILARITY`, `SPEAKER_BOOST`) and the model choice apply to **whichever** voice is selected.

The ids below are ElevenLabs **public premade voices** (the default library, available to
standard accounts; **availability can vary by plan**). They are **ElevenLabs' ids and may
change** — the account's **TTS-scoped key cannot list voices** (`/v1/voices` returns
`missing_permissions: voices_read`) and premade names/ids drift over time (ElevenLabs default
voices were noted to expire **2026-12-31**), so **confirm any new voice by a live call** — a
successful synth proves access. To enumerate the account's real voices, mint a key with
`voices_read`, store it in 1Password, and `curl -s -H "xi-api-key: $KEY"
https://api.elevenlabs.io/v1/voices` (never echo the key or the header).

Each id below was **verified to synthesize (HTTP 200)** with this account's TTS key on
**2026-06-17** (a tiny `POST /v1/text-to-speech/{id}` with `eleven_flash_v2_5`). The spread
covers female / male / gender-neutral, different registers, and US / British / Australian
accents so there is a sensible default for most use cases:

| Name | voice_id | Character (gender / register / accent / style) |
| --- | --- | --- |
| River | `SAz9YHcvj6GT2YYXdXww` | Gender-neutral, calm, US — a good neutral default. |
| Rachel | `21m00Tcm4TlvDq8ikWAM` | Female, calm narration, US — the shipped baseline (the "flat" one before `stability=0.35`). |
| Sarah | `EXAVITQu4vr4xnSDxMaL` | Female, soft, conversational, US — strong livelier-default candidate. |
| Jessica | `cgSgspJ2msm6clMCkdW9` | Female, expressive / animated, US — most dynamic of the set. |
| Laura | `FGY2WhTYpPnrIDTdsKH5` | Female, bright, upbeat, US — sassy receptionist warmth. |
| Alice | `Xb7hH8MSUJpSbSDYk0k2` | Female, clear, **British**. |
| Liam | `TX3LPaxmHKxFdv7VOQHJ` | Male, younger, US. |
| Josh | `TxGEqnHWrfWFTfGW9XjX` | Male, younger, deep, US. |
| Bill | `pqHfZKP75CvOlQylNhV4` | Male, older, deep, trustworthy, US. |
| Brian | `nPczCjzI2devNBz1zQrb` | Male, deep, narration, US. |
| George | `JBFqnCBsd6RMkjVDRZzb` | Male, warm, **British**. |
| Daniel | `onwK4e9ZLuTAKqWW03F9` | Male, authoritative, **British**. |
| Charlie | `IKne3meq5aSn9XLyUdCD` | Male, casual, **Australian**. |
| Eric | `cjVigY5qzO86Huf0OWal` | Male, friendly, US. |

Note: dropping `stability` to `0.35` makes **even Rachel** noticeably less flat, so a voice
swap and the settings change are independent levers.

## A/B procedure (operator)

1. Set the knob(s) in `.env` (e.g. swap `HERMES_VOIP_TTS_VOICE`, or nudge
   `HERMES_VOIP_TTS_STABILITY`). One change at a time so the effect is attributable.
2. Redeploy the `hermes gateway` runtime (operator-owned step — the build agent never touches
   the gateway process).
3. Place a live call; judge dynamism + that numbers/dates are still pronounced cleanly.
4. Keep the winner in `.env`; the dynamic default (`stability=0.35`, Flash, Rachel) is the
   fallback if a setting regresses quality.

## Automatic failover: ElevenLabs → Kokoro (ADR-0025)

A live cloud-TTS fault must never drop the call. When the primary TTS raises during
synthesis — an HTTP 400 (see "The model_id 400 trap" below), a timeout, a connection reset,
or any other exception from the stream — the plugin **automatically falls back to the
self-host Kokoro synthesiser** so the caller hears the answer in the local voice instead of
silence. The primary error is **logged at WARNING** (which provider failed and why); it is
never swallowed.

- **`HERMES_VOIP_TTS_FALLBACK`** — the fallback provider token. **Default: `sherpa-kokoro`**
  when the primary is a cloud provider (`elevenlabs`/`cartesia`/`aura2`); set to `none` to
  disable failover; set to another TTS provider token to use that instead. Must differ from
  the primary (a same-provider fallback can't recover the same fault — rejected at startup).
- **`HERMES_VOIP_TTS_FALLBACK_MODEL`** — the **Kokoro fallback's own model directory**.
  **Required** when the fallback is `sherpa-kokoro` (rejected at startup otherwise). The
  shared `HERMES_VOIP_TTS_MODEL` is the ElevenLabs **model id** for the primary, *not* a
  Kokoro directory, so the fallback needs its own dir — point it at the local Kokoro model
  dir (the same one a Kokoro-primary deployment uses). The `ml` extra must be installed so
  Kokoro can load on demand.
- **Latch + retry.** After the first primary failure on a call, the rest of that call uses
  the fallback (no mid-call voice flapping). A **fresh call retries the primary**, so a brief
  cloud blip self-heals on the next call.
- **Zero happy-path cost.** The Kokoro fallback model is loaded **only on the first
  failover** (lazily) and cached — so a healthy ElevenLabs call never loads it. Any primary
  failure recovers: a streamed fault (HTTP 400 / timeout / dropped connection) *and* a
  synchronous one (e.g. an unsupported per-call rate).

```
HERMES_VOIP_TTS_PROVIDER=elevenlabs            # cloud primary
HERMES_VOIP_TTS_MODEL=eleven_flash_v2_5        # the PRIMARY's ElevenLabs model id (or unset)
HERMES_VOIP_TTS_FALLBACK=sherpa-kokoro         # default for a cloud primary; `none` disables
HERMES_VOIP_TTS_FALLBACK_MODEL=/path/to/kokoro # the FALLBACK's Kokoro model dir (ml extra)
```

### The model_id 400 trap (the live incident root cause)

The live "no audio" incident was an ElevenLabs **HTTP 400 `invalid_uid`** during the greeting
synth. Root cause: **`HERMES_VOIP_TTS_MODEL` is a model DIRECTORY for `sherpa-kokoro` but the
model ID for ElevenLabs.** If `.env` points it at a Kokoro directory (e.g. left over from a
self-host A/B) while `HERMES_VOIP_TTS_PROVIDER=elevenlabs`, the plugin sends that directory
string as ElevenLabs' `model_id`, which the API rejects with a 400 — and (before ADR-0025)
the error killed the call. The plugin now **rejects a path-shaped / blank `model_id` at
startup** with a clear `ConfigError` naming `HERMES_VOIP_TTS_MODEL` (fail loud, not a dead
call), and the failover above is the safety net for any other cloud fault.

> **Fix:** when `HERMES_VOIP_TTS_PROVIDER=elevenlabs`, set `HERMES_VOIP_TTS_MODEL` to an
> ElevenLabs **model id** (e.g. `eleven_flash_v2_5`) or leave it **unset** (the Flash default).
> Never point it at a filesystem path for the ElevenLabs provider.

## Verify the config parses (no call, no network)

```bash
uv run python -c "
from hermes_voip.config import load_media_config
c = load_media_config({
    'HERMES_VOIP_TTS_PROVIDER': 'elevenlabs', 'ELEVENLABS_API_KEY': 'x',
    'HERMES_VOIP_TTS_STABILITY': '0.35', 'HERMES_VOIP_TTS_STYLE': '0.1',
    'HERMES_VOIP_TTS_STREAMING_LATENCY': '1',
})
print(c.tts_stability, c.tts_style, c.tts_streaming_latency, c.tts_fallback)"
# expect: 0.35 0.1 1 sherpa-kokoro   (cloud primary -> Kokoro fallback by default)
```

## Roll back

Unset the `HERMES_VOIP_TTS_*` tuning knobs (or remove them from `.env`) and redeploy: the
provider reverts to the dynamic-but-stable default (Flash v2.5, `stability=0.35`,
`style=0.0`, `similarity=0.75`, `speaker_boost=true`, no `optimize_streaming_latency`). To
return fully to the pre-change behaviour, also reset `HERMES_VOIP_TTS_VOICE` to Rachel
(`21m00Tcm4TlvDq8ikWAM`).
