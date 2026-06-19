# ADR-0010: DTMF: RFC 4733 telephone-event primary, SIP INFO fallback, in-band last resort

- **Date:** 2026-06-14
- **Status:** Accepted (amended by ADR-0036 — ships the SIP INFO + in-band Goertzel
  mechanisms, send + receive, that this record deferred; `HERMES_SIP_DTMF_MODE` accepts
  all four values)
- **Deciders:** agent session (VoIP architecture, post-research)

## Context

A telephony agent must both **receive** and **send** DTMF (the 0-9, `*`, `#`, A-D
keypad tones). Receiving lets the caller drive an IVR-style menu, enter an account
number, and — most importantly — supply a spoof-resistant confirmation for the
high-risk human-in-the-loop step defined in ADR-0009. Sending lets the agent navigate
an upstream IVR or pass a code on a bridged leg. There is no native Hermes
`MessageType` for DTMF: the runtime models a turn as one discrete
`MessageEvent(text=...)` (cf. ADR-0003), so digits have to be surfaced into that model
ourselves.

The constraints that bind the choice:

- **Codecs destroy in-band tones.** Our media path (ADR-0005) runs Opus and G.711 over
  RTP/SRTP, and lossy/low-bitrate codecs mangle the dual-tone waveform — Goertzel
  detection on the decoded PCM is unreliable through anything but clean G.711. So
  in-band detection cannot be the primary mechanism.
- **Gateways differ, and we are gateway-agnostic** (CLAUDE.md project scope; rule 40).
  The first test gateway is only a test target. RFC 4733 telephone-event is the
  near-universal standard, but some gateways negotiate it badly or prefer SIP INFO, and
  a few legacy paths only pass in-band. We must support more than one and pick per call
  from what SDP/signalling actually offers — not hard-code one gateway's behaviour.
- **The transport choice is spike-resolved (ADR-0005).** The two candidate transports differ
  on DTMF: the `aiortc` candidate transports RTP and negotiates `m=audio` payload types but does
  **not** implement the RFC 4733 `telephone-event` payload codec, so we implement that codec
  ourselves; the first-class `pjsua2` candidate provides DTMF natively. The codec layer therefore
  sits behind a transport-agnostic seam so either transport works unchanged.
- **Confirmation must be spoof-resistant** (ADR-0009). A confirmation gate that accepts
  spoken "yes" is defeated by a caller who reads the agent's own injected instructions
  back to it, or by audio replay. A keypad press carried out-of-band on its own RTP
  payload type / signalling message is a materially harder channel to forge than
  recognised speech, which is why DTMF is the designated confirmation input.
- **Everything is in-process** (ADR-0002): detection and generation run on the same
  event loop as the adapter and agent, with no media server (rule 40) and no extra
  dependency for tone math beyond what the transport already needs.

## Decision

The plugin **handles DTMF via three mechanisms, tried in priority order and selected
per call from what signalling/SDP advertises: (1) RFC 4733 `telephone-event` over RTP
as the primary, (2) SIP INFO (`application/dtmf-relay` / `application/dtmf`) as the
fallback, and (3) in-band Goertzel tone detection on decoded G.711 PCM as the last
resort.** Received digits are normalised into a single internal stream and surfaced to
the call/turn controller (ADR-0003) as structured input it can route — never as raw
audio. DTMF is the input channel for the ADR-0009 high-risk confirmation step.

Concrete shape:

- **Negotiation.** The transport already parses the answer SDP. A small helper inspects
  it for `a=rtpmap:<pt> telephone-event/8000` and the associated `a=fmtp` digit range;
  if present, mode is `RFC4733` and we record the negotiated dynamic payload type
  (commonly 101, but **read from SDP, never assumed**). If absent but the gateway
  signalled SIP INFO capability, mode is `SIP_INFO`. Otherwise mode is `INBAND`. The
  mode is decided once per call and logged; nothing downstream cares which fired.

- **One detector seam, three backends.** A `DtmfDetector` protocol abstracts the source
  so the transport choice (the `aiortc` or `pjsua2` candidate, resolved by the ADR-0005 spike)
  and the mechanism are both invisible to callers. Each backend emits the same
  `DtmfDigit` events:

  ```python
  from __future__ import annotations

  from collections.abc import AsyncIterator
  from dataclasses import dataclass
  from enum import Enum
  from typing import Protocol


  class DtmfMode(Enum):
      RFC4733 = "rfc4733"
      SIP_INFO = "sip_info"
      INBAND = "inband"


  @dataclass(frozen=True, slots=True)
  class DtmfDigit:
      """One completed keypress, deduplicated across RTP event packets."""

      symbol: str            # one of "0123456789*#ABCD"
      duration_ms: int       # from the RFC 4733 duration field, or measured
      source: DtmfMode       # which mechanism produced it
      received_at: float     # event-loop monotonic timestamp


  class DtmfDetector(Protocol):
      # Synchronous factory returning an AsyncIterator, matching ADR-0004's
      # calling convention (StreamingASR.stream / MediaTransport.inbound_audio):
      # the caller iterates the returned async iterator, it does not await this.
      def digits(self) -> AsyncIterator[DtmfDigit]: ...
      def mode(self) -> DtmfMode: ...
  ```

- **RFC 4733 codec (we own it).** A pure-Python codec packs/unpacks the 4-byte
  telephone-event payload (event code, end bit `E`, volume, 16-bit duration) per
  RFC 4733 §2.5. The detector tracks the per-event state machine: a keypress spans
  multiple RTP packets sharing one RTP timestamp; we emit exactly one `DtmfDigit` on the
  packet with the end bit set (or on timestamp change as a safety net for a lost end
  packet), so repeats and packet duplication never double-fire a digit. Event codes
  0-15 map to `0123456789*#ABCD`.

- **SIP INFO fallback.** When the signalling layer (ADR-0005) is the SIP-over-TLS stack,
  inbound `INFO` requests carrying `application/dtmf-relay` (`Signal=` / `Duration=`)
  or `application/dtmf` (bare digit) bodies are parsed into the same `DtmfDigit` stream.
  Outbound generation in this mode emits the corresponding `INFO` body.

- **In-band last resort.** A Goertzel-filter detector runs over decoded **G.711** PCM
  only (it is explicitly not trusted through Opus). It scores the eight DTMF
  frequencies, requires the standard row/column pair plus a minimum sustained duration
  and inter-digit gap to debounce, and is the lowest-priority backend. The 8 kHz / G.711
  handling reuses the `audioop-lts` package already pinned for the media path
  (ADR-0005), so no new codec dependency is introduced.

- **Generation (sending).** A `send_dtmf(digits: str)` path mirrors the detector:
  RFC 4733 mode synthesises the event-packet train (start packets, repeats over the tone
  duration, three end-bit packets) on the outbound RTP stream; SIP INFO mode emits
  `INFO` bodies; in-band mode synthesises the dual-tone PCM. The agent reaches this
  through a registered tool (`ctx.register_tool`, ADR-0002/ADR-0004) so a model turn can
  request "press 1-2-3-4".

  > **Implementation status (ADR-0031, amended for RFC 4733 receive).** The
  > **RFC 4733 send path is SHIPPED**: `RtpMediaTransport.send_dtmf(digits, …)` emits the
  > named-event train on the active call at the *negotiated* telephone-event payload type
  > (raises if it was not negotiated — never a hardcoded 101), under the engine TX mutex
  > shared with `send_audio`; it is exposed as the gated `send_dtmf` agent tool (ELEVATED)
  > and is the actuation primitive for the intercom `open_entry` DTMF mode.
  >
  > The **RFC 4733 RECEIVE path is now also SHIPPED** (the feat/voip-dtmf-receive lane):
  > `RtpMediaTransport` demuxes inbound RTP at the negotiated telephone-event payload
  > type to a per-call `DtmfReceiver` (in `_inbound_gen`, BEFORE the jitter buffer, so a
  > named-event payload is never decoded as audio), collapses RFC 4733's redundant end
  > packets, and fires an `on_dtmf` callback once per key-press. The adapter wires
  > `on_dtmf → CallLoop.feed_dtmf` for every call (inbound and outbound) that negotiated
  > telephone-event. The **armed-confirmation resolver is SHIPPED** too: `ArmedConfirmation`
  > (`hermes_voip.dtmf_confirm`) is the spoof-resistant channel — it satisfies the existing
  > `ConfirmationSource` protocol (`async confirm() → bool`), so it is a drop-in for
  > `CallControlTools._irreversible`. **`transfer_blind` is now SHIPPED as a live agent
  > tool** (the feat/voip-transfer-blind-tool lane): `voip_tools.register_voip_tools`
  > registers `transfer_blind(target)` (IRREVERSIBLE, operator level 3 + non-degraded),
  > and the adapter's `transfer_blind_on_call` awaits the per-call `ArmedConfirmation`
  > and sends the RFC 3515 REFER (`CallSession.transfer_blind`, with a `Referred-By` AOR)
  > **only** when the person on the call presses the armed confirm digit — a wrong digit
  > or a timeout transfers nobody, and a call that negotiated no telephone-event refuses
  > LOUDLY (never a silent no-op). The `pre_tool_call` gate clamps privilege as a
  > fail-fast (an unprivileged/degraded caller is blocked before the confirm prompt is
  > ever spoken). The **SIP INFO** and **in-band** mechanisms (both send AND receive) are
  > now ALSO SHIPPED — see **ADR-0036**, which supersedes the "deferred" status here:
  > `HERMES_SIP_DTMF_MODE` accepts all four ADR-0010 values, a single per-call resolver
  > (`hermes_voip.dtmf_config`) picks the send + receive backend, the SIP-INFO codec lives
  > in `hermes_voip.dtmf_sipinfo` (handled in-dialog by `CallSession`), and the in-band
  > Goertzel detector + tone generator live in `hermes_voip.dtmf` (engine-wired, G.711
  > only). All three mechanisms feed the same `CallLoop.feed_dtmf` router.
  > **Attended transfer is now SHIPPED too** (ADR-0048, which lifts ADR-0031 §4's
  > "no agent-driven consult-leg Dialog origination" deferral): `transfer_attended` is a
  > registered IRREVERSIBLE agent tool driving a consult → complete/cancel state machine
  > (the consult leg is a new outbound leg, gated by the same `HERMES_VOIP_OUTBOUND_ALLOW`
  > allowlist as `place_call`).

- **Surfacing inbound digits — no fake transcript.** Because Hermes has no DTMF
  `MessageType`, the call/turn controller (ADR-0003) consumes the `DtmfDigit` stream
  directly and routes by context rather than laundering digits into a speech turn:
  - while an ADR-0009 confirmation is **armed**, digits are matched against the expected
    confirmation token and resolve the gate directly (a control action, not a message) —
    this keeps the spoof-resistant property, since the digits never pass through STT or
    the LLM as free text;
  - otherwise, a buffered digit group (terminated by `#`, an inter-digit timeout, or an
    expected length) is delivered to the agent as a synthetic
    `MessageEvent(text="[DTMF] 1234")` so a normal turn can act on menu input.
  Which path is active is owned by the controller's call state, not guessed per digit.

  > **As built (RFC 4733 receive lane).** The controller is `CallLoop`: the engine's
  > `on_dtmf` fires `CallLoop.feed_dtmf(digit)`, which routes by call state exactly as
  > above. While an `ArmedConfirmation` (bound via `CallLoop.bind_confirmation`) is
  > armed, the digit resolves it through `ArmedConfirmation.feed` and is NOT surfaced —
  > the spoof-resistant control path. Otherwise the digit joins a buffer delivered as
  > `deliver_turn("[DTMF] 1234")` on a `#` terminator or after
  > `HERMES_SIP_DTMF_INTERDIGIT_MS`. The "expected length" terminator is not yet wired
  > (only `#` and the inter-digit timeout) — a future menu-driven tool can add it.

- **Configuration / env.** Behaviour is env-driven and gateway-agnostic, read at runtime
  (the test gateway's real connection details stay only in the gitignored `.env` /
  1Password per CLAUDE.md and rule 34):
  `HERMES_SIP_DTMF_MODE` (`auto` | `rfc4733` | `sip_info` | `inband`; default `auto` =
  negotiate-from-SDP as above), `HERMES_SIP_DTMF_INTERDIGIT_MS` (digit-group timeout),
  and `HERMES_SIP_DTMF_INBAND_ENABLED` (default true; lets an operator forbid the
  unreliable in-band last resort outright). Tests and examples use the obvious fakes
  (host `pbx.example.test`, extension `1000`).

  > **As built (no inert key — rule 27; ADR-0036).** All three mechanisms are now
  > implemented, so `HERMES_SIP_DTMF_MODE` accepts all four ADR-0010 values (`auto` |
  > `rfc4733` | `sip_info` | `inband`) and each drives a real backend (the interim
  > fail-loud rejection of `sip_info`/`inband` is gone). The resolvers
  > `resolve_dtmf_send_mode` / `resolve_dtmf_receive_mode(config, *,
  > telephone_event_payload_type, codec)` (`hermes_voip.dtmf_config`) map the mode +
  > `HERMES_SIP_DTMF_INBAND_ENABLED` + the negotiated PT + the negotiated codec to a
  > concrete backend. `auto` prefers RFC 4733, else the in-band last resort on a G.711
  > call; a forced `sip_info` always resolves (in-dialog signalling); a forced `inband`
  > resolves on G.711 else `UNAVAILABLE`. The adapter wires the resolved receive backend
  > (RFC 4733 / SIP INFO / in-band) to `CallLoop.feed_dtmf` and binds the
  > armed-confirmation resolver for each; `UNAVAILABLE` logs a WARNING and `DISABLED`
  > stays silent. `HERMES_SIP_DTMF_INBAND_ENABLED` now genuinely permits/forbids the
  > in-band backend (it IS built). `HERMES_SIP_DTMF_INTERDIGIT_MS` is the inbound
  > menu-group delivery timeout (default 2000 ms when unset). In-band is trusted ONLY on
  > G.711 (a lossy/wideband codec distorts the dual-tone waveform).

- **File paths.** `src/hermes_voip/dtmf/` holds `detector.py` (protocol + mode
  negotiation), `rfc4733.py` (event-payload codec + RTP state machine), `sip_info.py`,
  `inband.py` (Goertzel), and `generate.py` (outbound). Tests live under
  `tests/dtmf/`; per rule 18 each codec lands red-first with vector fixtures (e.g.
  a captured RFC 4733 packet train) before implementation.

  > **As built.** The single shipped mechanism (RFC 4733) made the planned package
  > split unnecessary. Actual layout: `src/hermes_voip/dtmf.py` (the 4-byte
  > telephone-event payload codec, digit↔event map, outbound `event_payloads`, and the
  > `DtmfReceiver` redundant-end collapse), `src/hermes_voip/dtmf_confirm.py`
  > (`ArmedConfirmation`), and `src/hermes_voip/dtmf_config.py` (`DtmfReceiveMode` +
  > `resolve_dtmf_receive_mode`). Send/receive RTP framing lives on
  > `RtpMediaTransport` (`media/engine.py`); surfacing lives on `CallLoop`
  > (`media/call_loop.py`). Tests: `tests/test_dtmf.py`, `tests/test_dtmf_receive.py`,
  > `tests/test_dtmf_confirm.py`, `tests/test_dtmf_config.py`,
  > `tests/test_call_loop_dtmf.py`. The `sip_info.py` / `inband.py` / `detector.py`
  > backends arrive only if/when those deferred mechanisms are built.

## Consequences

- **Robustness across gateways.** Supporting all three mechanisms, chosen from real
  negotiation, means a new gateway works without a code change in the common case;
  in-band remains available for the legacy long tail. This is the explicit cost of
  staying gateway-agnostic: three backends to maintain and test, not one.
- **We own the RFC 4733 codec.** Because the `aiortc` candidate does not provide
  telephone-event (ADR-0005), the event-payload codec and its packet state machine are ours to
  keep correct (duration/end-bit handling, lost-end-packet recovery, dedup) on that path. This is
  a small, well-specified, fully-testable surface with golden packet fixtures — acceptable. The
  `pjsua2` candidate transport supplies DTMF natively, so the `DtmfDetector` seam lets us use its
  events without touching callers if the spike selects it.
- **A real spoof-resistant confirmation channel.** DTMF gives ADR-0009 a confirmation
  input that does not pass through STT or the LLM and is not satisfiable by recognised
  speech or audio replay, materially strengthening the human-in-the-loop gate. We commit
  to keeping confirmation digits on the control path, never round-tripped as text.
- **No native Hermes seam, so we adapt.** Digits reach the agent either as a control
  action (confirmation) or a clearly-tagged synthetic `MessageEvent` (menu input); we
  accept a thin controller-owned layer rather than a fake transcript, and we commit to
  the `[DTMF]` tagging convention so a model turn can never confuse a keypress for
  spoken words.
- **Latency / cost.** All detection and generation are in-process integer/PCM math on
  the existing event loop — no provider, no network hop, no per-event cost, negligible
  CPU. No vendor lock-in and no infrastructure (rule 40). Per rules 23/24/26 the in-band
  detector's reliability and the RFC 4733 round-trip are to be **re-measured on the real
  8 kHz / G.711 test-gateway path**, not assumed from the spec.
- **Upgrade cadence.** RFC 4733/4734 are stable standards; the codec rarely changes. The
  only moving dependency is the shared `audioop-lts` package (ADR-0005), already pinned
  in `uv.lock` (rule 33).

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| In-band Goertzel only | Lossy/low-bitrate codecs (Opus, and even G.711 over a poor path, ADR-0005) distort the dual-tone waveform, so in-band detection is unreliable as a primary; it survives only as the last resort behind RFC 4733 and SIP INFO. |
| SIP INFO only | Not universally supported and body formats vary by gateway (`application/dtmf-relay` vs `application/dtmf`); RFC 4733 is the near-universal media-path standard, so SIP INFO is the fallback, not the primary. |
| RFC 4733 only (drop the others) | Some gateways negotiate telephone-event poorly or not at all, or only pass in-band; a single-mechanism plugin would silently lose digits on those gateways and break the ADR-0009 confirmation channel — incompatible with the gateway-agnostic scope. |
| Ignore DTMF entirely | Loses IVR navigation, keypad data entry, and — critically — the spoof-resistant input for the ADR-0009 high-risk confirmation gate, which spoken "yes" cannot safely replace. |
| Synthesise a fake STT transcript for inbound digits | Laundering keypresses into a speech turn destroys the spoof-resistance the confirmation step depends on and risks the LLM mis-parsing "[DTMF] 1234"; the controller instead routes digits as a control action or a clearly-tagged synthetic event. |
| Use spoken confirmation instead of DTMF for ADR-0009 | Recognised speech is defeatable by replay and by a caller reading the agent's own injected text back; an out-of-band keypress on its own payload type is materially harder to forge. |

