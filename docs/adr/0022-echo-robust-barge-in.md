# ADR-0022: Echo-robust barge-in — sustained-speech gating during TTS playout (extends ADR-0008)

- **Date:** 2026-06-17
- **Status:** Accepted
- **Deciders:** agent session (VoIP media, post-live-evidence)
- **Extends:** ADR-0008 (VAD + endpointing; barge-in Phase 2 was deferred there)

## Context

On a live inbound call (session `20260617_033116`) the operator reported the agent
"interrupting current task" even when they were **not** talking. The live log is conclusive:
while the agent's own TTS reply was playing out (`tts playout: 11795 ms of audio synthesised`),
the VAD fired short ONSET/OFFSET bursts and the streaming STT finalised one- and two-word
fragments of **the agent's own answer** (`'IT'`, `'UPON'`, `'NO'`), each of which immediately
ended the agent's turn with `reason=interrupted_during_api_call`. A self-interruption loop.

**Echo source — diagnosed (rule 25), not assumed.** Two cases were possible:

- **(A) Self-loopback** — we receive our *own* transmitted RTP (e.g. a symmetric-RTP/comedia
  mislatch routing our TX into our RX). Ruled out by the log: the call advertised our local
  RTP as the Docker-bridge address, but we **both transmit to and receive from the gateway's
  media address** (`rtp tx: first packet -> <gw>` and `rtp rx: first packet <- <gw>` carry the
  same `<gw>:port`, not our own socket). There is no `rtp: latched` line, i.e. the SDP-
  advertised remote already matched the real source — no mislatch.
- **(B) External echo** — the gateway/PSTN reflects our rendered TTS back to us on the media
  path (a classic 2-wire hybrid / no-echo-cancellation gateway). This is what the evidence
  supports: the echoed audio arrives **from the gateway** (a different SSRC than our outbound
  `0xCAFEBABE`), transcribed as if the caller were speaking.

ADR-0008 anticipated exactly this: it noted that running VAD during TTS playout "would both
false-trigger barge-in and poison STT" and that a full Phase-2 design would need an in-process
acoustic echo canceller (AEC). A full AEC is a large, latency-sensitive DSP component. The
operator needs the self-interruption stopped now, and a far lighter mechanism suffices for the
observed failure mode.

**Key observation from the evidence.** Echo arrives as **short, broken** voiced runs: the
bursts are 2–15 VAD windows (~64–480 ms) and are repeatedly punctuated by OFFSET edges as the
agent's speech ebbs and the reflected energy dips below the exit threshold. A genuine human
interruption is **sustained**: a person who means to cut in keeps talking for several hundred
milliseconds continuously. That difference — *sustained continuous voicing* vs *short broken
blips* — is separable without an AEC, **provided the sustained threshold is set above the
longest observed echo burst** (≈15 windows ≈ 480 ms in the live log) with margin.

**Two echo routes, not one.** Echo can self-interrupt the agent by *two* independent paths,
and both must be closed: (1) a VAD speech ONSET cancels the agent's TTS (barge-in); and (2)
the echo is *transcribed* and the endpointer fires an end-of-turn on its trailing silence, so
the echoed fragment is **delivered to the agent as a caller turn** — which itself starts a new
agent turn (the live log's `asr: delivering turn 'NO'` immediately followed by
`reason=interrupted_during_api_call`). Gating only the barge-in (route 1) leaves route 2 open,
so the gate must *also* suppress the turn delivery for unauthorised echo.

## Decision

Add an **echo-robust barge-in gate** to the duplex call loop (`media/call_loop.py`), governed
by a new configurable **barge-in mode**:

- **`off`** — never barge in. The agent always finishes its turn; the caller cannot interrupt
  audibly. (Safest against echo; worst for interactivity. Provided for completeness.)
- **`gated`** (**default**) — barge-in is allowed, but **while the agent's TTS is actively
  playing (and for a short configurable tail afterwards)** an interruption only counts once it
  is a **sustained continuous voiced run** of at least a configurable minimum duration. A
  short echo blip (which OFFSETs before the threshold) never barges in; a genuine sustained
  interruption still does. When no TTS is playing (outside the tail), any ONSET barges in
  immediately (there is nothing to echo, so no gate is needed).
- **`full`** — the pre-existing behaviour: any speech ONSET barges in immediately, even during
  TTS playout. (Correct only on a gateway with its own echo cancellation; kept for such
  deployments and as the explicit opt-in to maximum interactivity.)

**How `gated` measures "sustained".** The VAD already stamps each edge with a monotonic
**window ordinal** (`VadEvent.frame_index`; one window = 256 samples = 32 ms at 8 kHz). The
gate, while armed (TTS playing or in the tail), records the window ordinal of a speech ONSET
and then, on each subsequent processed window, checks how many **consecutive** voiced windows
have elapsed since that onset. It fires the barge-in once that count reaches
`barge_in_min_voiced_windows` (derived by rounding **up** from
`HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS`, default **600 ms** → 19 windows at 8 kHz — above the
≈15-window longest observed echo burst, with margin). An OFFSET before the threshold disarms
the pending onset (the blip is dismissed). A new ONSET re-arms. The VAD exposes a read-only
`window_index` so the gate can measure the run live (mid-run), not only at the next edge — so
a continuous interruption fires promptly at the threshold rather than waiting for the caller to
pause.

**Suppressing echo turn delivery (route 2).** While the gate is armed and the current speech
run is **not** an authorised barge-in, the call loop **withholds the inbound audio from the
ASR entirely** (it does not forward the frame onto the ASR queue, and it does not advance the
end-of-turn boundary). Because the recogniser never *sees* the echo, no echo transcript is
produced — closing the turn-delivery route on **both** end-of-turn paths: the endpointer's
trailing-silence boundary *and* a fused recogniser's own `end_of_turn` (e.g. Deepgram Flux sets
it natively; suppressing only the endpointer counter would leave that path open). The gate
tracks a per-run "authorised" flag set when a barge-in fires; it persists through the run's
trailing silence and is reset only on the *next* ONSET (so authorisation never leaks across
runs). An authorised sustained interruption (which also cancels the TTS) un-gates the audio
from that point on, so its transcript flows to the ASR and is delivered. The gate's
authorisation is driven on **every** inbound frame, not only while TTS is active, so a
sustained run that authorises *during the post-TTS tail* still delivers its turn.

The cost is that the first ~`barge_in_min_speech_ms` of a genuine interruption is withheld from
the ASR (it is transcribed from the authorisation point on); this start-clip is bounded by the
threshold, acceptable for an interruption, and avoided entirely on an echo-cancelled gateway by
setting `full`.

**Playout tail.** Echo can lag the TTS by tens to a few hundred ms (jitter buffer + network),
so the gate stays armed for `HERMES_VOIP_BARGE_IN_TAIL_MS` (default **250 ms**) of window
ordinals after the active TTS stream ends, then disarms. While disarmed, `gated` reverts to
immediate barge-in.

All thresholds are configurable with telephony-sensible defaults, parsed in `config.py` into
`MediaConfig` and threaded through the adapter into `CallLoop`:

| Env var | `MediaConfig` field | Default | Meaning |
| --- | --- | --- | --- |
| `HERMES_VOIP_BARGE_IN_MODE` | `barge_in_mode` | `gated` | `off` \| `gated` \| `full` |
| `HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS` | `barge_in_min_speech_ms` | `600` | Min sustained voiced run (ms) to barge in / deliver a turn during playout/tail |
| `HERMES_VOIP_BARGE_IN_TAIL_MS` | `barge_in_tail_ms` | `250` | How long after TTS ends the gate stays armed (ms) |

**Defense-in-depth — drop our own SSRC.** Independently of the echo gate, the media engine
(`media/engine.py`) now **drops any inbound RTP whose SSRC equals our outbound SSRC**
(`0xCAFEBABE`) before it reaches the jitter buffer/VAD/ASR. This is the correct root fix for a
*self-loopback* (case A): we must never process a packet we sent. It is cheap and always-on
(no config); it does **not** address case (B) external echo (the gateway re-originates the
audio under its own SSRC), which is why the `gated` barge-in is the primary fix here. The drop
is logged once per call at DEBUG with the offending SSRC.

## Consequences

- The self-interruption loop is removed on **both** routes: short echo blips during the agent's
  turn no longer cancel the TTS (route 1) **and** are no longer delivered to the agent as a
  caller turn (route 2). Verified by deterministic tests (`tests/test_call_loop.py`,
  `tests/test_barge_in_gate.py`, `tests/test_media_engine.py`): echo-shaped broken runs during
  playout do **not** barge in and deliver **no** turn; a sustained run **does** barge in and its
  turn **is** delivered; normal end-of-turn delivery during silence is unchanged.
- Genuine barge-in is preserved (a sustained interruption still stops the agent) — the
  capability ADR-0008 deferred is now delivered in its `gated` form, without an AEC.
- `gated` adds up to `barge_in_min_speech_ms` of latency to an *intentional* barge-in **during
  the agent's own speech only**; turn-taking during silence is unaffected. This is an
  acceptable, configurable trade (set `full` on an echo-cancelled gateway for zero added
  latency).
- The SSRC drop is a no-op on a well-behaved gateway (which re-originates under its own SSRC)
  and a correctness fix on any path that would loop our own packets back.
- A full in-process AEC (ADR-0008's Phase-2 sketch) remains a possible future enhancement for
  gateways with severe echo; it is **out of scope** here and explicitly not built (rule 6).

## Alternatives considered

- **Mute the VAD/ASR entirely during TTS (hard half-duplex).** Equivalent to `off` for
  interruption; rejected as the default because it removes barge-in, which the operator wants.
  `off` is available for operators who prefer it.
- **Energy-threshold-only gate.** Echo can be loud, so a level gate alone is unreliable across
  gateways; the *temporal* (sustained-run) signal is the robust discriminator and needs no
  per-gateway tuning. (A min-energy gate could be layered later; not required for the observed
  failure.)
- **Full AEC now.** Correct but heavyweight and latency-sensitive; disproportionate to the
  observed failure mode and a much larger change. Deferred (and unnecessary for the live bug).
