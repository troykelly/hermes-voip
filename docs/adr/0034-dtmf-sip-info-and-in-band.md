# ADR-0034: Ship the deferred DTMF mechanisms — SIP INFO and in-band (Goertzel), send + receive

- **Date:** 2026-06-18
- **Status:** Accepted
- **Deciders:** agent session (DTMF lane; amends ADR-0010)
- **Amends:** ADR-0010 (DTMF: RFC 4733 primary, SIP INFO fallback, in-band last resort)

## Context

ADR-0010 decided the plugin handles DTMF via **three** mechanisms, tried in priority
order and chosen per call from what signalling/SDP advertises: (1) RFC 4733
`telephone-event` over RTP, (2) SIP INFO (`application/dtmf-relay` / `application/dtmf`),
and (3) in-band Goertzel on decoded G.711 PCM. Only mechanism (1) was built (send: PR
#100; receive + the armed-confirmation resolver: PR #104). Mechanisms (2) and (3) were
DEFERRED, and — correctly per rule 27 — `HERMES_SIP_DTMF_MODE=sip_info` and
`HERMES_SIP_DTMF_MODE=inband` were **rejected at config load** with a loud `ConfigError`
rather than parsed into a value that did nothing.

That fail-loud is the right interim state but it is not the decision: ADR-0010 commits us
to a gateway-agnostic DTMF surface, and a gateway that negotiates telephone-event badly
(or only passes in-band, or prefers SIP INFO) currently loses every digit — and with it
the ADR-0009 spoof-resistant confirmation channel. This ADR ships the two deferred
mechanisms so the `auto` selection actually has a fallback, and so an operator can force
either mode for a known gateway.

Constraints that bind the build:

- **In-band is trusted only on clean G.711** (ADR-0010, ADR-0005). A lossy/low-bitrate
  codec (Opus, G.722) mangles the dual-tone waveform, so Goertzel detection runs **only**
  when the negotiated audio codec is G.711 (PCMU/PCMA, 8 kHz). On any other codec the
  in-band receive backend is unavailable (loud, not silent).
- **The in-band RX hook composes with AEC** (ADR-0033). The inbound generator already
  runs an acoustic-echo canceller on each decoded frame; in-band detection must run on the
  **AEC-cleaned** signal (after `_cancel_echo`), not the raw decode, so the agent's own
  reflected tones are removed before detection and the hot-path order is RX → AEC → DTMF
  detect → VAD/ASR. Detection adds bounded O(frame) integer/float math per frame (rule 22).
- **Goertzel must reject speech and noise** — a false keypress can resolve an ADR-0009
  confirmation or inject a bogus menu turn. Detection requires the standard row/column
  pair, an energy floor, a forward/reverse twist bound, a second-harmonic ratio test (the
  classic speech rejecter), and a minimum sustained duration plus an inter-digit gap to
  debounce, emitting one digit per press.
- **SIP INFO is in-dialog signalling, not media.** Inbound `INFO` requests reach the
  `CallSession` (the `DialogConsumer`), not the media engine; the send path builds an
  in-dialog `INFO`. Both formats are supported: `application/dtmf-relay` (`Signal=`,
  `Duration=`) and bare `application/dtmf`.
- **Everything stays in-process, no new dependency** (rule 40): the Goertzel math and tone
  synthesis are stdlib (`math`, `struct`); G.711 framing reuses the pinned `audioop-lts`
  already on the media path. No vendor/transport lock-in.

## Decision

Ship **SIP INFO** and **in-band Goertzel** DTMF, both **send and receive**, behind the
existing `HERMES_SIP_DTMF_MODE` selector. `HERMES_SIP_DTMF_MODE` now accepts all four
ADR-0010 values (`auto` | `rfc4733` | `sip_info` | `inband`); the previously-rejected
`sip_info` / `inband` are real. A single per-call resolver maps config + negotiation to a
**send backend** and a **receive backend** independently. Received digits from every
backend converge on the same `CallLoop.feed_dtmf` router (the ADR-0010 surfacing
contract), so the confirmation/menu routing downstream is mechanism-blind.

Concrete shape:

- **Mode resolution (`hermes_voip.dtmf_config`).** `resolve_dtmf_send_mode(config, *,
  telephone_event_payload_type, codec)` → `DtmfSendMode` (`RFC4733` | `SIP_INFO` |
  `INBAND` | `UNAVAILABLE`); `resolve_dtmf_receive_mode(config, *,
  telephone_event_payload_type, codec)` → `DtmfReceiveMode` (`RFC4733` | `SIP_INFO` |
  `INBAND` | `DISABLED` | `UNAVAILABLE`). `auto` prefers RFC 4733 when telephone-event was
  negotiated, else falls to in-band when the codec is G.711 and `dtmf_inband_enabled`,
  else `UNAVAILABLE` (loud) / `DISABLED` (in-band explicitly forbidden, clean). A forced
  `sip_info` always resolves to SIP INFO (signalling is always present). A forced `inband`
  resolves to in-band on G.711, else `UNAVAILABLE`.

- **In-band detector (`hermes_voip.dtmf.InbandDtmfDetector`).** A Goertzel state machine
  over PCM16 frames at a fixed analysis rate (8 kHz). `feed(pcm16) -> str | None` returns
  a digit at the **start** of a validated, debounced press and suppresses the rest of that
  press until an inter-digit gap of silence resets it. Validation: per-frequency Goertzel
  power for the four low + four high DTMF tones, the strongest low and strongest high must
  each clear an absolute energy floor and dominate their group, forward/reverse twist
  within bound, and a second-harmonic-energy ratio below threshold (rejects voiced
  speech whose formants drift across a DTMF pair). A press must persist for a minimum
  number of consecutive validated frames before it emits (debounce).

- **In-band generator (`hermes_voip.dtmf.inband_tone_pcm`).** Synthesises one digit's
  dual-tone PCM16 at a given sample rate, duration, and amplitude (sum of the row + column
  sines, scaled to avoid clipping). The engine sends it through the normal audio TX path
  (encode + 20 ms framing + pace + SRTP), so an in-band digit is just audio on the wire.

- **SIP INFO codec (`hermes_voip.dtmf_sipinfo`).** `parse_dtmf_info(content_type, body) ->
  str | None` parses both body formats to a keypad digit (or `None` when the request is
  not a DTMF INFO). `build_dtmf_relay_body(digit, *, duration_ms) -> str` builds the
  `application/dtmf-relay` body; `DTMF_RELAY_CONTENT_TYPE` is the content type.

- **Engine (`media/engine.py`).** Two new construction params:
  `dtmf_send_mode: DtmfSendMode` (default `RFC4733`, the shipped behaviour) and
  `inband_dtmf_rx_enabled: bool` (default `False`). `send_dtmf` dispatches on the send
  mode: `RFC4733` emits the named-event train (unchanged), `INBAND` synthesises and sends
  the dual-tone audio; `SIP_INFO` is NOT a media-engine concern (the `CallSession` owns
  it) so the engine never resolves to it. The inbound generator, after `_cancel_echo`,
  feeds the AEC-cleaned analysis-rate frame to a per-call `InbandDtmfDetector` when
  `inband_dtmf_rx_enabled` (which the adapter sets only on a G.711 call), firing `on_dtmf`
  exactly as the RFC 4733 demux does.

- **SIP INFO transport (`call.py` `CallSession`).** `handle_request` gains an `INFO`
  branch → `_on_info`: parse the body, answer `200 OK`, and fire a settable per-call
  `on_dtmf` callback with the digit (the adapter wires it to `CallLoop.feed_dtmf`). A new
  `send_dtmf_info(digits, *, duration_ms)` builds one in-dialog `INFO` per digit (advancing
  the dialog CSeq) and sends it. `send_dtmf(digits)` is now send-mode-aware: in SIP-INFO
  send mode it routes to `send_dtmf_info`, otherwise it delegates to the media engine
  (RFC 4733 / in-band) as before. A call that resolves to no send backend (`UNAVAILABLE`)
  raises rather than silently dropping (rule 6/37).

- **Adapter wiring (`adapter.py`).** The send + receive modes are resolved per call from
  the media config + the negotiated telephone-event PT + the negotiated codec, threaded
  into the engine (send mode + in-band RX flag) and the `CallSession` (SIP-INFO send mode
  + the `on_dtmf` callback). `_wire_dtmf_receive` now wires whichever receive backend
  resolved: RFC 4733 (engine `on_dtmf` → loop), SIP INFO (`CallSession.on_dtmf` → loop),
  or in-band (engine in-band RX → loop). The armed-confirmation resolver binds for **every**
  active receive backend, so the spoof-resistant channel works on all three.

- **Config (`config.py`).** `_DTMF_RECEIVE_SUPPORTED_MODES` is removed (all four modes are
  supported); `HERMES_SIP_DTMF_MODE` accepts the full ADR-0010 vocabulary again.
  `HERMES_SIP_DTMF_INBAND_ENABLED` keeps its meaning (forbid the in-band last resort).

## Consequences

- **The `auto` fallback is real.** A gateway that negotiates no telephone-event but runs
  G.711 now gets in-band DTMF instead of `UNAVAILABLE`; an operator can force `sip_info`
  for a gateway that prefers it, or `inband` for a legacy path. The gateway-agnostic DTMF
  promise of ADR-0010 is met end-to-end.
- **Three backends to keep correct.** Per ADR-0010 this is the accepted cost of staying
  gateway-agnostic. The Goertzel detector and tone synthesis are a small, fully-testable
  surface with golden fixtures; the SIP-INFO codec is two body formats.
- **In-band is best-effort by nature.** Detection reliability on a real lossy G.711 path is
  to be **re-measured live** (rules 23/26), not assumed from synthetic tones; the unit
  tests prove tone-in/tone-out and speech rejection on clean PCM. In-band is the **last**
  resort precisely because of this.
- **Spoof resistance.** SIP INFO carries the digit out-of-band on its own signalling
  request (harder to forge than recognised speech); in-band is the weakest channel (a
  caller can play tones), so in-band remains the lowest-priority backend and the ADR-0009
  confirmation still prefers RFC 4733 / SIP INFO when available. The confirmation resolver
  binds on every backend so the human-in-the-loop gate keeps working when only in-band is
  available, with this known weaker property documented.
- **Hot-path cost.** In-band RX adds bounded Goertzel math per inbound frame (eight tones x
  frame samples) only when in-band RX is armed (a G.711 call with no telephone-event); it
  is zero on the common RFC 4733 path. Runs after AEC, before VAD/ASR.
- **No new dependency, no infrastructure** (rule 40). Stdlib tone math + the already-pinned
  `audioop-lts`.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Keep SIP INFO / in-band deferred (status quo) | ADR-0010 committed to all three; a gateway with no telephone-event silently loses every digit and the ADR-0009 confirmation. The operator's "no deferrals" direction is to ship them. |
| In-band RX on the raw decode (before AEC) | The gateway reflects the agent's own audio; running detection before `_cancel_echo` would let the agent's reflected tones (and echo energy) false-trigger. Detection must see the AEC-cleaned near-end signal (ADR-0033). |
| Allow in-band on any codec | Opus/G.722 distort the dual-tone waveform (ADR-0005); detection would be unreliable and produce false digits on a security-relevant channel. Gate strictly on G.711. |
| A bare energy/row-column detector (no harmonic/twist tests) | Voiced speech routinely lights two DTMF frequencies; without the second-harmonic ratio + twist + duration debounce it false-fires on speech, which can resolve a confirmation. The speech-rejection tests are load-bearing. |
| Route SIP-INFO digits through the engine | Inbound `INFO` is in-dialog signalling delivered to the `CallSession`, not RTP to the engine; forcing it through the media path would invert the layering. The `CallSession` fires `on_dtmf` into the same `CallLoop.feed_dtmf` the engine uses, so surfacing stays uniform. |
