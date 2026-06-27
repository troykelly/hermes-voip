# ADR-0085 — `gate_tool_call` returns a typed `GateDecision(allowed, reason)`

**Status:** Accepted
**Date:** 2026-06-27
**Supersedes (return type only):** the bare `bool` return of ADR-0009/ADR-0021 `gate_tool_call`

## Context

`gate_tool_call` (`src/hermes_voip/providers/policy.py`) is the enforceable
tool-policy gate (ADR-0009/0021): the load-bearing control that hard-blocks an
`ELEVATED`/`IRREVERSIBLE` tool when a session is under-privileged, unconfirmed, or
in the sticky fail-open `degraded` state — regardless of the injection
classifier's verdict.

It returned a bare `bool`. A hard-block therefore left **no audit trace of WHY**:
an operator inspecting a refused tool on a live call could not tell apart

- an unconfirmed caller (`IRREVERSIBLE` without DTMF/human confirmation),
- a degraded (fail-open) session (the ADR-0009 miss case — the security-critical
  one), and
- a not-allowlisted / under-privileged caller.

These are exactly the distinctions an operator needs at a security/audit boundary.

## Decision

`gate_tool_call` returns an immutable, fully-typed
`GateDecision(allowed: bool, reason: GateReason)`.

- `GateReason` is a discriminated `Enum` whose members are **derived from the
  existing gate logic** (not invented): `ALLOWED` plus the three distinct block
  causes the gate already computes — `INSUFFICIENT_PRIVILEGE`, `UNCONFIRMED`,
  `DEGRADED`.
- `GateDecision` is a frozen, slotted dataclass (an audit record must not be
  mutated after the gate produced it). Invariant: `allowed is True` iff
  `reason is GateReason.ALLOWED`.
- **Behaviour invariant:** the allow/block `allowed` field reproduces the prior
  bool **byte-for-byte** for every `(risk, privilege_level, degraded, confirmed)`
  — proven by an exhaustive truth-table test. Only the structured `reason` is
  ADDED.

### Block-reason precedence

When more than one block cause holds at once, the reason follows a fixed,
security-ordered precedence so the report is deterministic and surfaces the most
significant cause first:

1. `DEGRADED` — the sticky fail-open hard-block (the ADR-0009 missed-injection
   case); the most security-significant signal.
2. `INSUFFICIENT_PRIVILEGE` — a level no confirmation can lift.
3. `UNCONFIRMED` — the residual cause on an otherwise operator-level,
   non-degraded session.

Precedence governs only WHICH reason is reported on a block; it never changes
whether a tool is blocked.

### Callers

The two `gate_voip_tool` re-exports keep their `bool` return (the adapter REFER
chokepoints and the `pre_tool_call` hook branch on it), but now **branch on
`.allowed` and log the structured `.reason` on every block** so the operator-visible
WHY is captured in the audit log:

- `tools.gate_voip_tool` (name → risk → `gate_tool_call`) logs the
  `GateReason` value, plus the name-level block causes it owns
  (`unknown_tool`, `not_granted`, `not_allowlisted`).
- `media.call_loop.gate_voip_tool` (the thin risk-level re-export) logs the
  `GateReason` value.

The user-facing block *message* returned to the runtime is intentionally generic
(it must not leak the policy reason to an untrusted caller); the reason goes only
to the operator audit log.

`GateDecision` and `GateReason` are exported in `providers.policy.__all__`.

## Consequences

- Operators get an audit trace of WHY each tool was refused, without changing any
  allow/block decision.
- The discriminated enum + exhaustive `assert_never` over `ToolRisk` keep the gate
  total and escape-hatch-free under `mypy --strict` (no `Any`, no `cast`).
- Out-of-scope adapter callers are unaffected: the `bool` `gate_voip_tool`
  contract is preserved.
