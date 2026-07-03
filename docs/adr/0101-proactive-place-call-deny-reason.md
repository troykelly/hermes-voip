# ADR-0101: Structured deny-reason category for a refused proactive `place_call`

- **Date:** 2026-07-03
- **Status:** Accepted
- **Deciders:** agent session (diagnostics lane, issue #414)
- **Relates to:** ADR-0085 (typed `GateDecision(allowed, reason)`), ADR-0075
  (machine-parseable `extra=` log events), issues #202 / #355 (the proactive
  `place_call` relaxation this diagnoses).
- **Amended by:** ADR-0105 removes the `LIVE_CALL_GUARD_MISSING` current reason; the
  gate branch that produced it was a bug that made ADR-0074 unreachable.

## Context

When a proactive (no-live-call) `place_call` is attempted from a non-VoIP session,
`_proactive_place_call_allowed` correctly fails closed, but it returned a bare
`bool`: every distinct refusal cause collapsed to `False`. The gate then surfaced
the generic `The place_call tool is not permitted on this call.` and
`gate_voip_tool` logged only `reason=insufficient_privilege`. An operator wiring up
proactive outbound calling could not tell apart the actionable causes —
`HERMES_VOIP_PROACTIVE_CALL_FROM` unset, no readable origin, an origin that is not
allowlisted, a live Call-ID whose guard state is missing (the inbound fail-safe
deliberately bypassing proactive relaxation), or a non-`place_call` tool.

The origin values themselves (platform, chat_id, the allowlist contents) are
secrets in a PUBLIC repo (rule 34) and must never reach the logs. Only the
*category* of the refusal is non-sensitive.

## Decision

Introduce `ProactiveDenyReason` — a `StrEnum` whose members are **derived from the
existing fail-closed branches** (not invented): `ALLOWED`, `PROACTIVE_ALLOW_UNSET`,
`ORIGIN_UNAVAILABLE`, `ORIGIN_NOT_ALLOWLISTED`, and
`UNSUPPORTED_TOOL_FOR_PROACTIVE_ORIGIN`. `_proactive_place_call_allowed` now returns
a frozen, slotted `ProactiveDecision(allowed: bool, reason: ProactiveDenyReason)`
(mirroring ADR-0085's `GateDecision`); invariant `allowed is True` iff
`reason is ALLOWED`. The `allowed` field reproduces the prior `bool` byte-for-byte
for every input — the security boundary is unchanged; only the structured `reason`
is added. ADR-0105 removes the historical `LIVE_CALL_GUARD_MISSING` member because
the gate branch that produced it was the bug: it blocked the real proactive flow
before the platform-scoped helper could decide.

`voip_pre_tool_call` records the category from the no-live-guard fail-safe path
(the helper whenever `state is None`, per ADR-0105) and, iff the gate then actually
blocks the tool, emits **exactly one** ADR-0075-style structured log at WARNING:
`extra={"event": "proactive_place_call_gate", "reason": <category>, "tool": <name>}`.
The category token is the ONLY origin-derived data logged; the platform, chat_id and
allowlist entries never appear. The agent-facing block message stays generic
(unchanged) — the diagnostic is operator-log-only, so no policy detail leaks to an
untrusted caller.

## Consequences

- Operators get a countable, non-sensitive reason category for every refused
  proactive `place_call`, alongside the existing `gate_voip_tool` block warning —
  no new infrastructure, stdlib logging only (ADR-0075).
- No allow/block decision changes: the fail-closed semantics are byte-identical
  (proven by the pre-existing gate tests plus the new caplog tests). The log fires
  only when the tool is genuinely blocked, so SAFE tools in the no-live-call branch
  stay silent.
- One more public type (`ProactiveDenyReason` / `ProactiveDecision`) to maintain;
  new deny branches must map to a member (the `StrEnum` keeps it total under
  `mypy --strict`, no `Any`/`cast`). ADR-0105 removes `LIVE_CALL_GUARD_MISSING`
  from the current type; it remains only in this ADR's history.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Keep the bare `bool`, log the reason internally at each `False` branch inside `_proactive_place_call_allowed` | Historically, the `LIVE_CALL_GUARD_MISSING` case was decided in the gate, so the helper could not log it; and logging inside the helper fires even when the tool is not ultimately blocked (SAFE tools). ADR-0105 removes that current branch, but the gate remains the single place that knows the final block outcome. |
| Append the category to the agent-facing block message | Leaks policy internals to a potentially untrusted caller for no operator benefit; ADR-0085 already keeps the user-facing message generic. |
| Log the actual origin (`platform:chat_id`) to make diagnosis trivial | Violates rule 34 (secrets/PII in a public repo's logs). The category is sufficient to tell the operator which knob to fix. |
