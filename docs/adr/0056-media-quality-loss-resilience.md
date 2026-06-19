# ADR-0056: Media-quality loss resilience — adaptive jitter, PLC, stateful-codec concealment, Opus in-band FEC, and ptime negotiation

- **Date:** 2026-06-19
- **Status:** Accepted
- **Deciders:** launch-readiness audit (media-quality cluster) — agent session
  (media-quality lane)

## Context

The launch-readiness audit confirmed five real media-quality gaps in the RTP media
plane (`media/engine.py`, `media/opus.py`, `rtp.py`). All five degrade the audio a
caller hears (or that the agent's VAD/STT consume) once the network is anything but
perfect — and the open-internet WebRTC path is never perfect. Each was verified
against the running code, not inferred:

1. **Fixed jitter depth.** `rtp.JitterBuffer` declares loss once a fixed
   `target_depth` (default 2) of later packets pile up behind a gap. A 2-packet
   reorder tolerance is fine on a clean LAN but declares spurious loss on a
   higher-jitter link (where reordering routinely spans more packets), and the
   inverse — a larger fixed depth — adds needless latency on a clean link. The
   tolerance must follow the link.

2. **No packet-loss concealment.** On a `Lost(seq)` signal the inbound generator
   logged DEBUG and `continue`d, leaving a hole in the decoded stream. 1–5% loss
   then produced audible gaps AND fed discontinuous audio to the VAD/endpointer/STT
   (a dropout reads as a word boundary / end-of-speech to the endpointer).

3. **Stateful-codec desync after loss.** G.722 and Opus carry predictor state
   across packets. Skipping a lost frame and decoding the next packet against stale
   state makes wideband degrade *more* than G.711 under the same loss — exactly
   backwards from why wideband was chosen.

4. **Opus in-band FEC advertised but off.** ADR-0032/0049 put `useinbandfec=1` in
   the Opus fmtp the plugin sends, but `OpusEncoder` constructed `opuslib.Encoder`
   with no FEC / expected-loss / DTX set (so it never produced FEC redundancy), and
   `OpusDecoder.decode` never passed `decode_fec=True` (so it never recovered a lost
   frame). The loss-resilience feature Opus is chosen for on open-internet WebRTC
   was inert, contradicting the SDP the plugin sends (a rule-27 gap).

5. **ptime hard-coded to 20 ms.** The engine is fully parameterised on
   `self._ptime`, but the parsed offer's `a=ptime` was never read to drive it and
   `a=maxptime` was never parsed at all, so the engine always framed at 20 ms and
   always answered `a=ptime:20`. 20 ms is the RFC 3551 default and accepted
   everywhere, but a gateway that asks for a different framing (e.g. a constrained
   uplink offering `a=ptime:30 a=maxptime:40`) is ignored.

## Decision

### 1. Adaptive jitter buffer (`rtp.JitterBuffer`)

`JitterBuffer` gains an **optional** adaptive mode. `target_depth` becomes the
*initial* / *minimum* reorder tolerance; two new optional parameters
(`max_depth`, `adapt`) turn on adaptation. When `adapt=True` the buffer observes
the **reorder span** of every late-but-in-window packet (how many sequence numbers
behind the current anchor it arrived) and the **late-drop** events (a packet that
arrived after its slot had already been emitted), and steers the live depth between
`target_depth` (floor) and `max_depth` (ceiling):

- a late drop, or an in-window reorder whose span ≥ the current depth, grows the
  depth by one (we were too eager — wait longer before calling loss);
- a sustained run (`_SHRINK_AFTER` pops) with no such event shrinks the depth by
  one toward the floor (the link calmed down — stop adding latency).

The default (`adapt=False`, `max_depth is None`) is **byte-for-byte the old fixed
behaviour** — existing call sites and tests are unchanged. Adaptation is driven
**only** by the `push`/`pop` sequence stream (no wall clock), so it is fully
deterministic and unit-testable by injecting a crafted sequence of arrivals. Bounds
are sane: floor 2 (one reorder), a default ceiling of 10 (= 200 ms at 20 ms ptime),
both caller-overridable; the depth can never run away or drop below one reorder.

### 2. Packet-loss concealment + 3. stateful-codec concealment (`media/engine.py`)

A `Lost(seq)` no longer leaves a hole. The engine produces one **concealment
PcmFrame** at the analysis rate and runs it through the *same* downstream as a real
frame (AEC reference alignment, in-band DTMF detect, `yield`), so the VAD/endpointer
/STT see a continuous stream:

- **Opus** — native codec concealment. When the **next** in-order packet is already
  buffered (the jitter buffer only emits `Lost` once `depth` later packets have
  piled up, so it usually is), the decoder reconstructs the lost frame from that
  packet's **in-band FEC** (`decode_fec=True`). When no successor is available it
  falls back to Opus **PLC** (`opus_decode(dec, NULL, frame_size)`), which
  extrapolates from decoder state. Either path keeps the Opus decoder's internal
  predictor coherent — so wideband degrades *less* than G.711, not more (gap 3).
- **G.711 / G.722** — there is no in-codec concealment in the pure-Python G.722
  port, so the engine conceals with the **last good decoded analysis-rate frame,
  attenuated** (≈ −3 dB per consecutive lost frame, silenced after a short run so a
  long outage does not drone). This is identical for both, so G.722 loss handling is
  no worse than G.711's (gap 3). The concealment frame is held at the analysis rate
  (post-decode/-downsample), so it is codec- and rate-correct with no extra decode.

The concealment seam is a single `_conceal_frame()` helper plus a small
`_PlcState` holder, leaving `_inbound_gen`'s linear demux intact and leaving room
for the **separate, later RTCP lane** to read loss counts without restructuring.

### 4. Opus in-band FEC (`media/opus.py` + `media/engine.py`)

`OpusEncoder` enables **in-band FEC** and an **expected packet-loss percentage**
(default 10%) at construction, and disables DTX (DTX + telephony comfort-noise
interact badly and FEC needs every frame). Because `opuslib==3.0.1`'s
`Encoder.inband_fec` / `packet_loss_perc` *property setters are broken* (the lambda
omits the CTL value argument and raises `TypeError` — verified), the encoder sets
them via the low-level `opuslib.api.encoder.encoder_ctl(state, request, value)` CTL,
exactly the documented escape the binding leaves open. `OpusDecoder` gains
`decode_fec(packet)` (decode the *current* packet's FEC copy of the *previous* lost
frame) and `decode_plc()` (native NULL-packet concealment via the low-level
`opuslib.api.decoder.decode(state, None, 0, frame_size, False, channels=1)` — the
high-level wrapper crashes on `len(None)`). The encoder enablement is additive and
back-compatible (a normal `encode` still returns a normal packet; FEC redundancy
rides inside it when the encoder judges it worthwhile).

Expected-loss is exposed as an `OpusEncoder(expected_packet_loss_pct=…)` constructor
argument (default 10) so a future control path can raise it from observed loss; the
engine constructs the encoder with the default for now.

### 5. ptime / maxptime negotiation (`sdp.py` — ONLY this; `media/engine.py`)

`sdp.py` parses `a=maxptime` into `AudioMedia.maxptime` (alongside the
already-parsed `ptime`) and adds a pure `negotiate_ptime(offer_ptime, offer_maxptime,
supported, default)` helper that picks the framing to use: honour the offer's
`a=ptime` when it is one the engine supports and ≤ any `a=maxptime`, else fall back
to the default (20 ms). The engine gains a `ptime` **property + setter** (mirroring
the existing `payload_type` / `telephone_event_payload_type` post-construction
setters) and validates `ptime > 0`, so a caller (the adapter, owned by another lane)
can apply the negotiated framing after the offer is parsed without reconstructing the
engine. The engine no longer assumes 20 ms — every framing computation already reads
`self._ptime`; this makes that value the *negotiated* one.

**Out of scope (this lane):** RTCP (a separate later lane — the concealment seam is
structured so RTCP can read loss counts, but no RTCP is added here);
`adapter.py` / `manager.py` / `config.py` / `call_loop.py` wiring (owned by other
lanes — the engine exposes the seams they will call); any SDP change beyond ptime /
maxptime.

## Consequences

- **Loss resilience is real.** 1–5% loss no longer punches audible holes or feeds
  the endpointer false word boundaries; Opus recovers most single-frame losses
  exactly (FEC) or plausibly (PLC), and wideband no longer degrades worse than G.711.
- **Latency follows the link.** The adaptive buffer adds reorder tolerance only when
  the link reorders, and gives it back when the link is clean — no fixed
  latency tax on good networks.
- **The SDP stops lying.** `useinbandfec=1` on the wire is now backed by an encoder
  that produces FEC and a decoder that uses it (rule 27).
- **Negotiated framing is honoured** when the engine is told the negotiated ptime;
  the adapter wiring that tells it lands in a sibling lane (the engine seam is ready
  and unit-tested here).
- **Determinism preserved.** Every new behaviour is driven by injected
  sequence/loss/timing seams (the jitter `push`/`pop` stream, an injected loss in the
  inbound generator, a fake clock) — no test waits on a wall clock or a real network.
- **Cost.** Concealment is one extra small allocation + (for G.711/G.722) one
  `audioop.mul` per lost frame; Opus FEC/PLC is one extra `libopus` call per lost
  frame — both off the steady-state path (only on loss). The adaptive buffer adds a
  few integer comparisons per packet. No event-loop blocking is introduced (all work
  is synchronous CPU on already-decoded buffers).

## Alternatives considered

- **A fixed larger jitter depth** — rejected: taxes every clean call with latency to
  protect the rare jittery one; adaptation gives both.
- **Silence insertion for lost frames** — rejected: a hard mute is more audible than
  an attenuated repeat and reads as end-of-speech to the endpointer.
- **A full WebRTC-style adaptive playout (NetEq) buffer** — rejected as far out of
  scope and far heavier than the reorder-tolerance adaptation the current
  packet-counting buffer needs; revisit only if measurements demand it.
- **Fixing opuslib's broken FEC setters upstream** — out of scope; the low-level CTL
  is the binding's own supported path and needs no fork.
