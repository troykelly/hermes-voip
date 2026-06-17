# ADR-0028: Barge-in clean stop — flush queued audio + linear fade, no interrupt phrase (extends ADR-0023)

- **Date:** 2026-06-17
- **Status:** Accepted
- **Deciders:** agent session (VoIP media, operator media-UX report)
- **Extends:** ADR-0023 (echo-robust barge-in), ADR-0017/0022 (outbound send path), ADR-0005 (RTP engine)

## Context

ADR-0023 made a barge-in *decision* echo-robust (a sustained voiced run authorises an
interruption). But the operator reports the barge-in *action* is still wrong in two ways once it
fires:

1. **The stop is abrupt and delayed.** On an authorised barge-in the call loop calls
   `TtsStream.cancel()`, which only stops `_play()` *pulling* new frames from the synthesiser.
   The audio already handed to `RtpMediaTransport.send_audio` is re-framed into 20 ms packets and
   **deadline-paced out over real time** — a single Kokoro chunk is 700–2300 ms, i.e. dozens of
   packets still queued. `cancel()` does not touch that queue, so the caller keeps hearing the
   agent for the duration of the already-buffered audio *after* they interrupted. And when it does
   end, it ends on a full-amplitude packet boundary — a click/pop.

2. **An interruption acknowledgment is spoken.** The caller hears "Interrupting… I'll respond…"
   on barge-in. This is **not** hermes-voip text: it is the vendored Hermes gateway's *busy
   acknowledgment* — `gateway.run._handle_active_session_busy_message` builds
   `"⚡ Interrupting current task{detail}. I'll respond to your message shortly."` (and its
   Queued / Steered / Subagent siblings) and delivers it via `adapter._send_with_retry` →
   `adapter.send()`. On a text platform that renders as a chat line; on a live call our `send()`
   synthesises it as TTS and speaks it. (Verified by reading the gateway source; the env var
   `HERMES_GATEWAY_BUSY_ACK_ENABLED=false` would suppress it at the gateway, but that is a
   deploy-time lever on vendored config, not a robust in-plugin guarantee.)

The full fix for "interrupt more *aggressively*" (a lower-than-600 ms threshold without echo
false-positives) needs an acoustic echo canceller and is explicitly out of scope (see
*Consequences*). This ADR addresses only the operator's immediate ask: a **clean, click-free,
prompt** stop, and **never speaking** the acknowledgment.

## Decision

Three changes, all gateway-agnostic and behind telephony-sensible config:

**1. Flush the queued outbound audio on barge-in (`RtpMediaTransport.flush_outbound`).** A new
engine method drops the pending outbound audio the instant a barge-in is authorised:

- it discards the re-framing carry buffer (`_tx_buffer`) and any in-flight parked frame
  (`_inflight_wire`), so none of the superseded utterance's remaining audio reaches the wire;
- it bumps a **flush generation** counter. `send_audio`'s drain loop snapshots that counter and
  `_transmit_frame` re-checks it *after* its pacing sleep (the one yield point) and *before* the
  `sendto`: if a flush ran during the sleep, the pre-flush frame is superseded and dropped (sending
  it after the fade would place a full-amplitude packet past the ramp — a click). The drain loop
  then stops.

Net: the agent goes quiet within ~1 ptime instead of after the whole buffered chunk paces out.

**2. Linear fade-out on the final frames (`media/audio.linear_fade_out`, `HERMES_VOIP_BARGE_IN_FADE_MS`).**
Before dropping the buffer, `flush_outbound` takes the **front** of the pending buffer (the
immediate continuation of what is playing), ramps it linearly from full gain to zero over
`fade_ms`, re-frames it into whole ptime packets (zero-padding the final partial once), and sends
those inline (no pacing — the cut is now). The fade is computed in the **linear PCM16 domain
before the codec encode**, so G.711 and G.722 are both correct (G.722's stateful encoder simply
continues from the faded samples). Default **30 ms**; `0` is an instant hard cut for an operator
who prefers the abrupt stop. The fade brings the last sample to exactly 0 so there is no residual
step to click. **Total audio emitted after a barge-in is bounded by the fade window**
(`ceil(fade_ms / ptime)` packets ≈ 1–2 at 30 ms) — the parked in-flight frame is *dropped*, not
sent, so the agent never adds a full-amplitude packet past the cut. The dropped frame leaves a
single spent RTP sequence number (a benign 1-packet gap the receiver conceals like any lost
packet); for G.722 the decoder's adaptive sub-band predictor is briefly one frame behind but
re-converges within a few frames — inaudible because the fade is already ramping to silence (this
is a transient, **not** a permanent desync).

**3. Never synthesise the interruption acknowledgment (`notice_filter.is_interruption_ack`).** The
existing `send()`-boundary guard `is_internal_system_notice` — which already drops the home-channel
notice family — is extended to also recognise the gateway busy-ack family by its distinctive
announcement openings ("Interrupting current task …", "Queued for the next turn …", "Steered into
current run …", "Subagent working … your message is queued …"). The emoji glyphs and the optional
" (N min elapsed, iteration X/Y, running: <tool>)" status detail are tolerated, not required. The
voip adapter therefore drops the ack instead of speaking it: after a barge-in the agent goes
silent and processes the caller's input, it does not announce the interruption. The matcher is
conservative — a genuine reply that merely *mentions* interrupting/queuing/steering as ordinary
words ("Sorry to interrupt, your taxi's here.") is not an ack and is still spoken.

### Config surface

| Env var | `MediaConfig` field | Default | Meaning |
| --- | --- | --- | --- |
| `HERMES_VOIP_BARGE_IN_FADE_MS` | `barge_in_fade_ms` | `30` | Linear fade-out (ms) on the final frames of a barge-in flush; `0` = instant hard cut; `>= 0` |

Parsed in `config.py` into `MediaConfig`, threaded through the adapter into `CallLoop`
(`barge_in_fade_ms`), and passed per-call to `RtpMediaTransport.flush_outbound(fade_ms=…)`.
`call_loop.barge_in()` now does two steps: `stream.cancel()` then
`transport.flush_outbound(fade_ms=…)` — and only when a stream is/was active (nothing queued to
flush otherwise).

## Consequences

- A barge-in now stops the agent within ~1 RTP packet with a click-free ramp, not after the
  buffered TTS drains — the abruptness/delay is gone. Verified by deterministic tests
  (`tests/test_media_engine_bargein.py` decodes the flushed G.711 and G.722 tails and asserts the
  envelope collapses to silence; `tests/test_media_audio.py` pins the pure ramp;
  `tests/test_call_loop.py` proves `barge_in()` flushes with the configured fade and that normal
  speech / idle never flush).
- The "Interrupting… I'll respond…" artifact (and its siblings) is never spoken;
  `tests/test_notice_filter.py` pins the family and that genuine replies pass.
- The flush is a barge-in-only action: a normally-completing utterance still delivers its tail via
  `stop()`/`_flush_tx_tail` (the flush would truncate it). The flush-generation check is the only
  new coupling between `flush_outbound` and the `send_audio` drain; it is single-event-loop, lock-free
  (asyncio co-operative scheduling), and the existing stop-race contract is unchanged.
- **Out of scope (explicitly not built, rule 6): a full in-process acoustic echo canceller (AEC).**
  To let barge-in trigger *more aggressively* — a threshold below the current 600 ms sustained run
  — without echo false-positives needs AEC so the gateway's reflected TTS is cancelled before the
  VAD/ASR see it. That is a large, latency-sensitive DSP component and a separate follow-up. This
  lane deliberately does **not** lower `HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS` (that needs AEC first).

## Alternatives considered

- **Suppress the ack at the gateway via `HERMES_GATEWAY_BUSY_ACK_ENABLED=false`.** A real lever, but
  it is deploy-time config on vendored code; the in-plugin `send()`-boundary filter is robust
  regardless of gateway configuration and matches how the home-channel notice is already handled.
- **Fade the *back* of the pending buffer.** Rejected: the audio currently playing is the front of
  the queue, so a continuous ramp-to-silence must start from the front; fading the back would leave
  a full-amplitude gap before the ramp.
- **Send the parked in-flight frame first (for G.722 continuity), then fade.** Considered and
  rejected (cross-vendor review): it adds a full-amplitude packet *after* the barge-in, extending
  the agent's speech past the cut — exactly what the operator asked to stop. The in-flight frame's
  PCM is already gone (only its encoded bytes survive) so it cannot be faded; dropping it caps the
  post-barge-in audio to the fade window, and the resulting 1-packet gap / brief G.722 predictor
  transient is benign (concealed; masked by the ramp to silence).
- **Mute via `set_hold(True)`.** `set_hold` drops the buffer but emits no fade and is a
  call-control state (hold), not a one-shot cut; a dedicated `flush_outbound` keeps the semantics
  clear and adds the fade.
- **Cancel inside `send_audio` by checking a flag every frame with no generation counter.** A bare
  boolean cannot distinguish "this drain's flush" from a later one on a re-used path; the monotonic
  generation snapshot is unambiguous and lock-free.
