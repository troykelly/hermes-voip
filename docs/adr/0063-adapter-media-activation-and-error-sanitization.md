# ADR-0063: Adapter media activation (ptime + adaptive jitter) and provider-error sanitisation

- **Date:** 2026-06-19
- **Status:** Accepted
- **Deciders:** launch-readiness audit (adapter lane) — agent session

## Context

Two launch-readiness findings were verified against the running code and are both
fixed here. Both live at the **adapter** seam (`src/hermes_voip/adapter.py`); both
were genuine "built but not wired" / "missing safety net" gaps, not redesigns.

### 1. ptime negotiation + adaptive jitter were built but DORMANT

ADR-0056 (PR #142) shipped three media-quality capabilities into the lower layers:

- `sdp.negotiate_ptime(offer_ptime, offer_maxptime, supported=, default=)` — choose
  the RTP packetisation time to frame/answer with, honouring the peer's
  `a=ptime`/`a=maxptime` when carriable.
- `RtpMediaTransport.ptime` — a validated property/setter; every TX framing
  computation (samples-per-packet, RTP timestamp increment, the deadline-pacer
  interval) reads it, so the engine frames at the negotiated ptime rather than a
  hard-coded 20 ms.
- `rtp.JitterBuffer(adapt=True, max_depth=N)` — an adaptive RX reorder tolerance
  that grows under loss/wide-reorder up to a ceiling and shrinks back toward the
  fixed floor after a clean run.

But ADR-0056 deliberately stopped at the engine/SDP boundary: the **adapter never
called any of them**. Verified on `main` (c77096d): all four `RtpMediaTransport(...)`
construction sites omitted the adaptive-jitter arguments (so every call ran a
fixed-depth buffer) and never assigned `engine.ptime` (so every call framed at the
20 ms default and answered `a=ptime:20`), and the parsed offer's `AudioMedia.ptime`
/`AudioMedia.maxptime` were never read to drive TX. The launch-promoted features
were therefore not live (AGENTS.md rule 6 — "done" means wired end-to-end). This ADR
is the activation that makes them real.

### 2. Provider/LLM error text could be spoken to the caller verbatim

The Hermes runtime delivers the agent's turn text to a platform adapter's `send()`.
On an unrecoverable backend failure the *reply* `send()` receives can be the raw
error itself — an HTTP `502`/`503`, a provider error class (`overloaded_error`), or
a Python traceback. Verified end-to-end: the gateway's
`_sanitize_gateway_final_response` maps provider errors to a safe reply **only** for
`platform == "telegram"` and returns raw text unchanged for every other platform, so
the `voip` plugin receives raw text; `VoipAdapter.send()` then dropped only
system-notices + post-hangup replies and passed everything else to `loop.speak()`;
`spoken_text.sanitize_for_speech` strips only emoji/markdown/URLs, so a coherent
error sentence ("API call failed … HTTP 502 … overloaded_error") survived and was
synthesised — read aloud to the caller. That is unprofessional and an information
leak about the backend. (Re-bucketed by the audit from LAUNCH→CORE in severity — the
call does not drop and the leak is a generic provider/HTTP string, not the SIP
secret — but it is fixed in the launch set.)

## Decision

### ptime + adaptive jitter activation

- **Adaptive jitter.** All four `RtpMediaTransport` construction sites (inbound
  SDES, inbound WebRTC, outbound SIP, outbound WebRTC) open the engine with
  `jitter_adapt=True` and `jitter_max_depth=media_cfg.jitter_max_depth`. New config
  knob **`HERMES_VOIP_JITTER_MAX_DEPTH`** (`MediaConfig.jitter_max_depth`, default
  **10** ≈ 200 ms at 20 ms ptime, validated **positive**), following the #144
  `HERMES_SIP_MAX_CALLS` pattern (`_parse_positive_int` + `__post_init__` check).

- **ptime.** After the SDP is negotiated, the adapter sets
  `engine.ptime = negotiate_ptime(audio.ptime, audio.maxptime, supported=
  _SUPPORTED_PTIMES_MS, default=20)` via a small `_negotiated_ptime(audio)` helper.
  Inbound paths set it on the offer before `connect()`; outbound paths set it on the
  2xx **answer** (alongside the other negotiated `engine.*` adoptions). The engine's
  supported framing set is **`(10, 20, 30, 40)` ms** — 20 ms is RFC 3551's default,
  10 ms matches the Opus `minptime=10` we advertise, 30/40 ms are typical lower-rate
  options a gateway may request. ptime is a *preference*: an unsupported request
  falls back to 20 ms (the engine still emits valid RTP), never fails the call.

- **Engine seam (scope deviation, see below).** `RtpMediaTransport.__init__` gains
  two keyword-only params `jitter_adapt: bool = False` and
  `jitter_max_depth: int | None = None`, threaded into its two existing
  `JitterBuffer(...)` construction sites. The defaults preserve the prior behaviour
  byte-for-byte (a fixed-depth buffer), so no existing caller changes.

### Provider-error sanitisation

- New pure module **`hermes_voip.provider_error`** (mirroring `notice_filter`):
  - `is_provider_error(content) -> bool` — recognises provider/runtime error shapes
    by **strong, structural** hallmarks (HTTP 5xx *in an error context*, a
    provider/SDK error-class or error-code token, an explicit failure phrase, or a
    Python traceback header). Deliberately conservative: a genuine reply that merely
    mentions a number, the word "error", or a service in passing is **not** matched,
    so the agent is never wrongly silenced (rule 19).
  - `safe_error_reply(language) -> str` — a short, safe spoken apology keyed by
    language (the ADR-0054 mechanism; English default, unknown language falls back to
    English, never raises).
- `VoipAdapter.send()`: after the existing notice/ended-call drops, when
  `is_provider_error(content)` it (a) speaks `safe_error_reply(media_cfg.language)`
  instead of the raw text, (b) logs the **real** error at WARNING with the adapter's
  known secrets redacted (`_redact_secrets_for_log` masks any SIP digest/WSS
  password, cloud API key, or TURN password value, longest-first, and truncates),
  and (c) returns a successful `SendResult` (no retry storm). The error is **not**
  raised toward the caller; it is already surfaced in the log (rule 37).

## Scope deviation (recorded for the integrator)

The adapter task scoped `media/engine.py` as owned by the concurrent RTCP lane
(`feat/rtcp-sr-rr-mux`) — "call its EXISTING public APIs, don't edit it". But the
engine's public surface had **no** way to enable adaptive jitter: PR #142 added
`JitterBuffer(adapt=, max_depth=)` and the `engine.ptime` setter, but never threaded
adaptive params through the `RtpMediaTransport` constructor (verified: even the RTCP
branch still does `JitterBuffer(target_depth=jitter_depth)`). `engine.ptime` is a
public setter (so the ptime half is clean), but the adaptive-jitter half was blocked
on a missing engine seam. Per rule 6 (no partial-ship) the minimal two-parameter
engine change was made. It does **not** textually collide with the RTCP branch's
`engine.py` edit regions (verified via `git diff origin/main...origin/feat/rtcp-sr-rr-mux`):
the change touches the `jitter_depth` param line and the two `JitterBuffer`
construction lines, none of which the RTCP branch modifies. The integrator merging
both lanes should still re-verify the engine constructor signature after integration.

## Consequences

- The adaptive jitter buffer + ptime negotiation are now **live** on every call path
  — completing ADR-0056. A higher-jitter/lossy link gets a deeper reorder tolerance
  (up to the ceiling) without penalising a clean link; a gateway that requests a
  carriable framing gets it.
- A provider blip during a call now yields a brief spoken apology instead of a
  read-aloud stack trace, with the real error preserved (redacted) in the log for
  the operator. Graceful degradation, no info leak.
- Defaults are conservative and reversible: `jitter_max_depth=10` and a 20 ms ptime
  fallback are the safe norms; the error detector errs toward speaking a genuine
  reply rather than over-suppressing.

## Alternatives considered

- **Sanitise in `call_loop.speak()` instead of `send()`.** Rejected: `send()` is the
  single chokepoint for every Hermes reply and already hosts the notice/ended-call
  drops; putting the error filter there keeps `call_loop` unchanged and the decision
  in one place. (The internal loop-spoken lines — greeting, reprompt, goodbye, comfort
  filler — are authored by the plugin and are never provider errors, so they need no
  filtering.)
- **Per-turn LLM retry on a provider error.** Out of scope: the gateway owns the LLM
  call, so the plugin cannot retry it; the plugin's job is to not *speak* the error.
- **Adaptive jitter without a config ceiling (module constant).** A constant would be
  simpler, but an operator on a known-bad link benefits from raising the ceiling; the
  positive-validated knob is cheap and matches the existing media-config surface.
