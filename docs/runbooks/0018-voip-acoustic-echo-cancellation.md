# Runbook: VoIP in-process acoustic echo cancellation (`HERMES_VOIP_AEC_ENABLED`)

**What it is.** An in-process **acoustic echo canceller (AEC)** on the inbound media path. Some
gateways reflect the agent's own rendered TTS back on the inbound leg (a delayed, attenuated,
line/hybrid-filtered copy of audio the plugin already holds). Without cancellation the VAD/ASR
transcribe that echo as the caller and barge the agent in — a self-interruption loop (ADR-0023).
The canceller subtracts the **known outbound TTS reference** from each inbound frame *before* the
VAD/ASR see it, so the reflected echo cannot false-trigger barge-in. That is what lets the
barge-in sustained threshold (`HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS`) drop from 600 ms to a
responsive **200 ms** when AEC is on — **aggressive barge-in** without echo false-positives.

It is a **numpy block-NLMS** (Normalised Least-Mean-Squares) adaptive FIR filter. `numpy` is a
**base dependency** (ADR-0110): the estimate + tap update run as small matmuls over a short
sub-block of samples, so the per-frame cost is a handful of matmuls instead of the per-sample
`O(filter_len)` recursion the original stdlib filter used (which stalled the RX coroutine at ~2x
the packet period — ADR-0095). It still adds **no algorithmic delay** — each `cancel` processes and
returns its whole frame with no look-ahead buffering — so it does not slow the conversational path.

The WHY lives in **ADR-0033** (the original design + the deferral it closes, ADR-0008/0023/0028)
and **ADR-0110** (the block-NLMS CPU-budget refactor that supersedes ADR-0033's no-numpy /
no-latency constraints). This runbook is the operational HOW for the operator knobs.

> **Public repo.** No secrets here — these are a boolean, two integers, and a float.

## The knobs

| Env var | Type | Default | Read into |
| --- | --- | --- | --- |
| `HERMES_VOIP_AEC_ENABLED` | boolean (`true/1/yes/on` \| `false/0/no/off`) | `true` (**ON**) | `MediaConfig.aec_enabled` |
| `HERMES_VOIP_AEC_FILTER_MS` | integer **ms**, `> 0` | `64` | `MediaConfig.aec_filter_ms` |
| `HERMES_VOIP_AEC_BULK_DELAY_MS` | integer **ms**, `>= 0` | `0` | `MediaConfig.aec_bulk_delay_ms` |
| `HERMES_VOIP_AEC_MU` | float in the **open** `(0, 2)` | `0.30` | `MediaConfig.aec_mu` |

All four are read by `hermes_voip.config.load_media_config` and threaded by the adapter
(`_run_call_loop`) into `RtpMediaTransport(aec_enabled=…, aec_filter_ms=…, aec_bulk_delay_ms=…,
aec_mu=…)` for every inbound and outbound call. The filter length and bulk delay are configured in
**milliseconds** (rate-independent); the engine converts them to taps/samples at the call's live
analysis rate (8 kHz G.711, 16 kHz G.722/Opus) when it builds the canceller.

**The coupled default — `HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS`.** When AEC is **on** (the default)
and that key is **unset**, the barge-in sustained threshold defaults to **200 ms**; when AEC is
**off** it defaults to **600 ms** (the ADR-0023 echo-safety margin). An explicit
`HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS` always wins over the AEC-aware default. The `gated` mode +
tail (ADR-0023) stay as defense-in-depth even with AEC on, so a small residual leak below 200 ms
cannot self-interrupt.

Validation (fail-fast at startup, `MediaConfig.__post_init__` → `_validate_aec`):

- `aec_filter_ms <= 0` raises `ConfigError` (a zero-tap filter cancels nothing);
- `aec_bulk_delay_ms < 0` raises `ConfigError` (the env parser rejects a negative integer);
- `aec_mu` outside the open interval `(0, 2)` raises `ConfigError` — `0` never adapts, `>= 2`
  diverges.

## How it behaves (the guarantees)

- **Cancels the echo, not the caller.** The echo is *correlated* with the known reference (it is
  the reference, delayed + filtered), so the adaptive filter learns and subtracts it; the caller's
  speech is *uncorrelated*, so it survives the subtraction — a genuine barge-in still fires the
  VAD. A **double-talk hold** freezes adaptation when the near-end carries far more energy than the
  reference could produce as echo (the caller talking over the echo), so the caller can never pull
  the filter into cancelling them.
- **No added latency + within CPU budget (rule 22, ADR-0110).** `cancel` returns the same frame
  length it was given, with no look-ahead buffering. The per-frame CPU is a handful of small numpy
  matmuls (block-NLMS) — measured ~1.3 ms/frame at 8 kHz and ~4.2 ms/frame at 16 kHz for the
  worst-case 512-tap (`_AEC_MAX_TAPS`) filter while adapting, well under the 20 ms ptime budget
  (the superseded per-sample stdlib filter was ~35 ms / ~91 ms — 2x-4x over). The budget is gated
  in CI by `tests/test_media_aec_budget.py`, which also pins ≥ 30 dB convergence (ERLE) at both
  rates.
- **No-op when disabled or on an echo-cancelled gateway.** `HERMES_VOIP_AEC_ENABLED=false` builds
  no canceller (the RX/TX taps are no-ops) and restores the 600 ms threshold default. On a gateway
  with its own echo cancellation there is no echo to model and the inbound is uncorrelated with the
  reference, so the filter stays near zero and subtracts nothing.
- **Engine-internal.** The canceller is owned by `RtpMediaTransport`: the TX path taps every
  outbound wire-rate frame as the reference (`_push_aec_reference` after `sendto`), the RX path runs
  every decoded inbound frame through it (`_cancel_echo` before yield). The call-loop pump → VAD →
  ASR chain is unchanged; it simply receives already-cancelled frames.

## How to set it

Set the env vars wherever the rest of the `HERMES_VOIP_*` config lives (the gitignored `.env` the
Hermes runtime loads, or the process environment for `hermes gateway run`). Example (gitignored
`.env`, values only — no secret):

```
HERMES_VOIP_AEC_ENABLED=true
HERMES_VOIP_AEC_FILTER_MS=32
HERMES_VOIP_AEC_BULK_DELAY_MS=0
HERMES_VOIP_AEC_MU=0.30
# optional: override the AEC-aware barge-in default explicitly
# HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS=200
```

Then redeploy/restart the gateway so the plugin re-reads its config (the value is read at config
load; a running call keeps the settings it started with).

## How to verify

1. **Config parse (offline, deterministic):**

   ```
   uv run python -c "from hermes_voip.config import load_media_config; \
     c = load_media_config({}); \
     print(c.aec_enabled, c.aec_filter_ms, c.aec_mu, c.barge_in_min_speech_ms)"
   ```

   Prints `True 64 0.3 200` (AEC on → the 200 ms barge-in default). With
   `{'HERMES_VOIP_AEC_ENABLED':'false'}` it prints `False 64 0.3 600` (the 600 ms default
   restored). A bad `mu` fails loud:

   ```
   uv run python -c "from hermes_voip.config import load_media_config; \
     load_media_config({'HERMES_VOIP_AEC_MU':'2'})"
   ```

   exits non-zero with
   `ConfigError: HERMES_VOIP_AEC_MU must be a finite number in the open interval (0.0, 2.0), got '2'`.

2. **Behaviour (covered by the test suite, deterministic — synthesised PCM, no network):**
   `uv run pytest tests/test_media_aec.py` proves the canceller drives a known reflected reference
   toward silence, that an uncorrelated near-end (the caller) survives, that `cancel` adds no
   buffering, and that an Opus 48 kHz reference is downsampled to the 16 kHz analysis rate.
   `uv run pytest tests/test_media_engine_aec.py` proves the engine RX/TX taps cancel reflected
   audio before it is yielded (and pass it through when disabled).
   `uv run pytest tests/test_aec_barge_in_integration.py` proves the cancelled echo does not
   self-interrupt at the lowered 200 ms threshold while a real caller still barges in.
   `uv run pytest tests/test_config_aec.py` proves the parse + validation + the coupled default.
   `uv run pytest tests/test_media_aec_budget.py` proves `cancel` stays under the 20 ms packet
   budget at both rates while adapting and converges to ≥ 30 dB ERLE (ADR-0110).

3. **Live:** with AEC on, place a call on a gateway known to reflect TTS (the test gateway) and
   talk over the agent. The operator log should show the agent barging in promptly (~200 ms of your
   speech) and should **not** show the agent interrupting itself on its own playback — no
   `reason=interrupted_during_api_call` while only the agent is speaking. (Live validation is
   pending the operator's redeploy — do not touch the live gateway from a build lane.)

## Tuning guidance

- **`HERMES_VOIP_AEC_FILTER_MS`** — the window must span the echo-RETURN delay (round-trip), not
  just the impulse response, or a delayed broadband echo is left uncancelled. The 64 ms default
  gives a full 64 ms window at 8 kHz; the engine caps the tap count at 512 (`_AEC_MAX_TAPS`), so
  16 kHz is held to ~32 ms. With block-NLMS the per-frame cost at the 512-tap cap is a few ms
  (see above), so the cap is now about echo *reach*, not the CPU budget. Raising this past the cap
  has no effect (the clamp wins) unless you also accept the cap; lower it only if the echo delay is
  known-short. For a longer 16 kHz echo, prefer `HERMES_VOIP_AEC_BULK_DELAY_MS`.
- **`HERMES_VOIP_AEC_BULK_DELAY_MS`** — set this to the gateway's constant echo-return delay if it
  is large and known, so the adaptive taps model only the impulse response after it (a shorter, more
  responsive filter). `0` (the default) lets the taps cover the delay directly.
- **`HERMES_VOIP_AEC_MU`** — higher converges faster after the agent starts speaking but leaves a
  larger steady-state residual; lower is cleaner but slower to adapt. 0.30 is a brisk, stable
  middle.
- **`HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS`** — if a deployment still shows occasional echo-driven
  self-interruption at 200 ms (a very loud/long echo path), raise it (e.g. 300–400 ms) rather than
  disabling AEC; or raise the filter length first.

## Rollback

Set `HERMES_VOIP_AEC_ENABLED=false` and redeploy — no canceller is built, the inbound path is
untouched, and the barge-in threshold default reverts to the echo-safe 600 ms (ADR-0023 behaviour).
The other AEC knobs are then inert.
