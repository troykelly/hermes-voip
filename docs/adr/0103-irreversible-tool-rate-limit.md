# ADR-0103: rate-limiting repeated IRREVERSIBLE tool calls

- Status: **Proposed** (operator accepts before any throttle code is written — rule 40)
- Date: 2026-07-03
- Relates to: backlog item 1380; ADR-0029 (place_call), ADR-0048 (attended transfer),
  ADR-0021 (caller-group privilege), the injection-guard / `degraded` gate.

## Context

The IRREVERSIBLE agent tools — `place_call`, `transfer_blind`, `transfer_attended`,
`send_dtmf` — are gated on **privilege** (level-3, non-`degraded`; ADR-0021) and, for
outbound, the **static allow-list** (ADR-0029) plus the per-transfer DTMF-confirm gate.
Those are the right *authorisation* controls. They are **not rate controls**:

- `_outbound_extensions` (`adapter.py:1014`, checked `:1708`) only rejects a *concurrent*
  same-extension dial (a re-entrancy guard), cleared on completion (`:1725`).
- The attended-consult single-slot guard (`adapter.py:6050`) only blocks a *second
  concurrent* consult.

So once a session legitimately holds level-3/non-`degraded` privilege, a **confused
deputy** — a persistent prompt injection driving the model — can redial the allow-listed
targets, fire transfers, and DTMF-spam a callee IVR **sequentially, without bound on
attempt count or elapsed time**. The blast radius is capped only by the allow-list and
the DTMF-confirm gate, not by *how often*. That is a real abuse surface (nuisance
redials, IVR brute-forcing, transfer thrashing) even against a correctly-authorised
session.

## Decision drivers

1. **Bound sustained abuse** without breaking legitimate bursty use.
2. **MUST NOT throttle** (backlog 1380):
   - a legitimate rapid redial (e.g. immediately calling back a caller who just dropped);
   - a normal `transfer_attended` **consult → complete/cancel** sequence (several tool
     calls that constitute *one* logical, legitimate transfer).
3. Fail **closed** on the throttled action; never fail open.
4. Agent-visible behaviour, so the policy is decided on the record (rule 40) and the
   defaults are operator-tunable (env), not baked-in.

## Options considered

### A. Fixed per-tool cooldown (minimum interval between calls)
Simplest. **Rejected as the primary mechanism:** a fixed "≥ N s between dials" directly
violates driver 2 — it blocks an immediate legitimate callback, and a naïve version
mis-counts the consult→complete sequence as rapid-fire transfers.

### B. Per-session sliding-window budget (token bucket) — **recommended**
A rolling window per session per **action class**: at most `N` IRREVERSIBLE *actions* per
`T` seconds, with a small burst allowance `B`. A legitimate rapid callback fits inside the
burst; only *sustained* spam trips it. Counting granularity (the key detail):

- Count a *new* irreversible ACTION: a `place_call` dial, a `transfer_blind`, a
  `transfer_attended` **initiation**, and each `send_dtmf` *invocation*.
- Do **not** count the `transfer_attended` **complete/cancel** — they finish an already
  counted in-flight transfer (satisfies driver 2's consult-sequence carve-out).

### C. Per-target rate-limit (per dialled number / transfer target)
More granular, more state. **Rejected as primary:** the confused-deputy threat spams
regardless of target, and per-session B already covers it. Keep as an optional future
refinement, not v1.

## Recommendation

Adopt **Option B**, per-session sliding-window, evaluated in the existing
`voip_pre_tool_call` gate *after* the privilege check (so a throttle only ever *further*
restricts an already-authorised call — it can never grant one):

- **Proposed defaults** (operator-tunable via `HERMES_VOIP_IRREVERSIBLE_RATE_MAX` /
  `_WINDOW_S` / `_BURST`): `N = 6` actions per `T = 60 s`, burst `B = 3`. These let a
  double-dial-then-callback through while capping sustained spam at ~6/min.
- **On trip → REFUSE the one call, do NOT auto-`degrade`.** Rationale: `degrade` is a
  session-wide clamp on *all* elevated tools; auto-degrading on a rate trip would
  over-punish the legitimate rapid-redial edge the moment it crosses the line. The
  throttle already bounds the damage; a refusal is the proportionate, fail-closed
  response. The session may act again once the window clears.
- **Emit a structured `irreversible_tool_throttled` event** (ADR-0075: `event`, `tool`,
  `count`, `window_s` — no caller identity, rule 34) so an operator can *see* a spam
  pattern. Whether a *sustained* breach should escalate to `degrade` is deferred to a
  follow-up once we have real-traffic data (rule 26) — not decided here.
- **Key** the window on the session/call context already available to the gate; a
  no-context call (should not occur for these tools) fails closed.

## Consequences

- A fully prompt-compromised, correctly-authorised session is bounded to ~`N`/`T`
  irreversible actions — nuisance redials / IVR brute-forcing / transfer thrashing are
  capped, not merely allow-list-bounded.
- Legitimate bursty use (callback a dropped caller; consult→complete a transfer) is
  unaffected by construction (burst allowance + the complete/cancel counting carve-out).
- Adds a small per-session timestamp deque; O(1) amortised per gated call, no I/O
  (efficiency-safe, rule 22).
- The gate stays authorisation-first: the throttle is a *second* restriction, never a
  grant, so it cannot weaken the existing privilege/allow-list controls.

## Open questions for the operator (accept / adjust before implementation)

1. The `N`/`T`/`B` defaults above — acceptable, or tune (e.g. tighter for `send_dtmf`)?
2. Confirm **refuse-only** (no auto-`degrade`) for v1, with the throttle event feeding a
   later degrade-escalation decision.
3. Is a per-target refinement (Option C) wanted now, or deferred?

Once accepted, implementation is a bounded lane in `voip_tools.py` (the gate) with a
small per-session rate-tracker, TDD, and the `irreversible_tool_throttled` event —
strictly additive to the existing fail-closed gate.
