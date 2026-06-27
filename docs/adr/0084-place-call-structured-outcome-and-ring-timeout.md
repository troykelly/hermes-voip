# ADR-0084: `place_call` structured outbound failure outcome + bounded ring timeout

- **Date:** 2026-06-27
- **Status:** Accepted
- **Deciders:** operator (direction) — agent session (feat/place-call-outbound-failure-classify-1)

## Context

`place_call` (ADR-0029) had two related gaps on the same tool surface:

### (A) Flat failure — no structured outcome

Every outbound SIP failure — 486 Busy Here, 603 Decline, 408/487 no-answer, 503
unavailable — was collapsed into a single `OutboundCallFailed` exception and surfaced to the
agent as a generic `{"error": "place_call failed: ..."}` string. The agent could not tell
WHY a call failed and could not branch its behaviour (e.g. tell the originating conversation
"the number was busy" vs "they declined" vs "no one answered"). The SIP reason phrase inside
`OutboundCallFailed.reason` was previously echoed verbatim, which risks leaking gateway host
names or internal routing details embedded in reason strings (public-repo rule 34).

### (B) Unbounded ring timeout — no cancel lever

`place_call_with_objective` had no ring timeout. An unanswered call blocked on the gateway's
own INVITE timeout (up to the hard 35 s `_OUTBOUND_INVITE_TIMEOUT` sink bound), with no way
for the operator to configure a shorter bound. ADR-0069 landed the SIP/TLS CANCEL machinery
(`OutboundCallCancelled`, `abort_call`, `place_call(..., ring_timeout_secs=...)`) but never
wired it to the `place_call` tool path.

## Decision

### (A) `PlaceCallOutcome` enum + structured tool result

A new `PlaceCallOutcome(Enum)` in `voip_tools.py` maps the distinct SIP failure classes to
typed outcome values the agent can branch on:

| Outcome | SIP status codes | Semantics |
|---------|-----------------|-----------|
| `BUSY` | 486, 600 | Callee is busy |
| `NO_ANSWER` | 487, 408, or `OutboundCallCancelled` | Not answered (peer-signalled or our own ring-timeout abort) |
| `DECLINED` | 603 | Callee explicitly rejected the call |
| `FAILED` | anything else | Any other non-2xx, or transport/init failure |

The `place_call_handler` catches `OutboundCallFailed`, `OutboundCallCancelled`, **and
`RuntimeError`** specifically (BEFORE the outer `except Exception` boundary) and returns:

```json
{"error": "outbound call failed: <outcome.value>", "failure_outcome": "<outcome.value>"}
```

The SIP `reason` phrase is **never** echoed in the agent-facing result (only the classified
category). A transport/media-initialisation `RuntimeError` (e.g. the RTP transport could not
be opened, or the WSS/WebRTC outbound path raises `NotImplementedError` — a `RuntimeError`
subclass) is caught here and mapped to `PlaceCallOutcome.FAILED`, so a non-SIP failure still
carries the structured `failure_outcome` contract instead of falling through to the generic
boundary handler. Its `str(exc)` is **redacted exactly like the SIP reason phrase** — the
message can embed gateway connection details (host:port), so only the classified `FAILED`
category is surfaced, never the raw exception text (rule 34). The failure is still logged for
the operator (rule 37), but with only the exception **type name** (no message, no traceback)
so a host/port in the message cannot leak into logs either.

The `OutboundCallNotAllowed` (allowlist refusal before any dial) stays as a plain
`{"error": ...}` with **no** `failure_outcome` key — this is a policy gate, not a SIP
failure.

The `failure_outcome` value is the stable JSON API surface for the agent; it matches
`PlaceCallOutcome.value` (lowercase string: `"busy"`, `"no_answer"`, `"declined"`,
`"failed"`).

### (B) Ring timeout via `HERMES_VOIP_RING_TIMEOUT_SECS`

A new env var `HERMES_VOIP_RING_TIMEOUT_SECS` (finite positive float, default unset) is read
by `place_call_handler` at call time via `_parse_ring_timeout()` and forwarded as
`ring_timeout_secs` to `VoipToolHost.place_call_with_objective`. The adapter wires it to
`place_call(..., ring_timeout_secs=...)` which arms the existing ADR-0069 timer.

`_parse_ring_timeout()` rejects (returns `None`, i.e. treats as absent) any value that is
unset, blank, non-numeric, non-positive, **or non-finite** — `math.isfinite()` guards
against `inf`/`nan`, because `float("inf") > 0` is `True` and a positive-infinity timeout is
an *unbounded* ring, which defeats the bounded-timeout policy this section establishes.

On expiry the adapter raises `OutboundCallCancelled`, which the handler maps to
`PlaceCallOutcome.NO_ANSWER` — "we stopped waiting, no one answered".

#### Why env var (not a tool arg)?

A tool arg for the timeout would be model-chosen; the timeout is an operator policy
constraint, not something the agent should vary per call. An env var keeps it operator-set
and consistent with the rest of the `HERMES_VOIP_*` configuration scheme.

#### Why through `place_call_with_objective` (not asyncio.timeout in the handler)?

The spec suggested `asyncio.timeout` at the `voip_tools` layer as the preferred path. We
chose to thread `ring_timeout_secs` through `place_call_with_objective` / adapter instead
because:

- The adapter already owns the full CANCEL machinery (ADR-0069): `abort_call`,
  `_ring_timeout`, glare suppression. An `asyncio.timeout` at the tool layer would cancel
  the `asyncio.Task` without sending a CANCEL to the gateway — abandoning the INVITE on the
  wire (exactly the pre-ADR-0069 defect).
- Threading the kwarg is a one-line addition to `place_call_with_objective` and is
  zero-risk (the `ring_timeout_secs` path in `place_call` is already tested).
- The `VoipToolHost` Protocol gains an optional kwarg (`ring_timeout_secs: float | None = None`)
  so existing fakes need a trivial signature update — no logic change.

The WSS/WebRTC outbound path does not yet support `ring_timeout_secs` (ADR-0069 scope;
the adapter raises `NotImplementedError` if attempted — preserved unchanged).

## Consequences

- The agent receives a structured `failure_outcome` for every failed outbound SIP call,
  enabling per-failure branching (e.g. "busy — try later" vs "declined — abort").
- The SIP reason phrase is suppressed in agent-facing results (rule 34 compliance).
- `HERMES_VOIP_RING_TIMEOUT_SECS` gives the operator a configurable ring bound on the
  SIP/TLS UAC; unset leaves the 35 s hard sink bound as before.
- `PlaceCallOutcome` and `_RING_TIMEOUT_ENV` are the new stable API additions in this
  module; both are exported in `__all__`.
- `VoipToolHost.place_call_with_objective` gains one keyword-only arg
  (`ring_timeout_secs: float | None = None`); existing fakes need a signature update
  but no logic change.

## Alternatives considered

| Alternative | Rejected because |
|-------------|-----------------|
| `asyncio.timeout` in `place_call_handler` | Would cancel the `asyncio.Task` without sending a SIP CANCEL — re-introduces the ADR-0069 wire-abandonment defect on the tool-call path. |
| Emit the SIP reason phrase in the error message | Leaks potential PII (gateway host, internal routing info embedded in reason strings) — violates rule 34 / public-repo invariant. |
| Tool arg for ring timeout | Makes timeout model-chosen; this is an operator policy, not per-agent-call preference. |
| A separate `OutboundCallCancelled` outcome | Merged into `NO_ANSWER` because, from the agent's perspective, "we stopped waiting" and "they never answered" are the same actionable state (call the objective failed; decide whether to retry). |
