# ADR-0104: DTMF privilege for the outbound (agent-initiated) persona

- Status: **Proposed** (operator accepts before code — rule 40; agent-visible privilege policy)
- Date: 2026-07-03
- Relates to: backlog 1284; ADR-0031 (`send_dtmf`/`open_entry` are ELEVATED), ADR-0029
  (agent-triggered outbound calls), ADR-0021 (caller-group privilege levels),
  **ADR-0103** (rate-limiting IRREVERSIBLE tools — a hard dependency, see below).

## Context

`send_dtmf` is classified **ELEVATED** (ADR-0031): the agent transmits in-call DTMF,
which can navigate IVR menus, enter PINs, and — via `open_entry` — actuate **physical**
intercom entry. Elevated tools require a level-3 / non-`degraded` session.

The **outbound persona** (a call the *agent* placed via `place_call`, ADR-0029) runs at
**privilege level 0** by construction. So an outbound agent **cannot** send DTMF — yet
navigating a callee's IVR ("press 1 for sales", "enter your account number") is the
*normal, intended* reason an agent places an outbound call to a business line. This is a
real functional gap (backlog 1284).

Naively raising the outbound persona's privilege is unsafe: it would also unlock
`open_entry` (a door), transfers, and the rest of the ELEVATED class, and it would let a
prompt-injected outbound agent DTMF-**brute-force** a callee IVR (PIN guessing, menu
abuse).

## Decision drivers

1. Let a legitimate outbound agent navigate the callee IVR it was dialled to reach.
2. Do **NOT** grant physical-access (`open_entry`) or call-control (transfer) tools to the
   outbound persona.
3. **Bound abuse**: an outbound `send_dtmf` capability multiplies the IVR-brute-force
   surface, so it MUST be paired with a rate limit (ADR-0103).
4. **Fail closed on INBOUND**: an inbound caller's persona must not inherit any
   outbound-only DTMF allowance.

## Options considered

- **A. Raise the outbound persona to ELEVATED (level 2/3).** Rejected — over-grants
  (`open_entry`, transfers), violates driver 2.
- **B. Per-tool allowance: grant *only* `send_dtmf` to the outbound persona, keyed on
  call direction — recommended.** The outbound persona gets `send_dtmf` (and nothing else
  new); `open_entry` and transfers stay gated. Inbound is unaffected (driver 4).
- **C. Objective-scoped grant (only when the `place_call` objective implies IVR use).**
  Rejected — "does the objective imply IVR navigation" is a fuzzy, model-derived
  predicate; a static direction-keyed grant is auditable and predictable.

## Recommendation

Adopt **Option B**: a per-tool `send_dtmf` allowance for the **outbound (agent-initiated)**
persona, evaluated in the existing tool gate:

- Grant `send_dtmf` to the outbound persona **only**; `open_entry` and every other ELEVATED
  tool remain denied at level 0 (the grant is a single-tool exception, not a level bump).
- **Keyed on direction**: applies to `place_call`-originated calls; inbound personas are
  unchanged and fail closed.
- **Hard dependency on ADR-0103**: this grant SHOULD NOT ship until the IRREVERSIBLE-tool
  rate limit (which covers `send_dtmf`) is accepted and implemented — otherwise an outbound
  agent gains *unbounded* DTMF-spam capability against the callee. Sequence 1380 → 1284.
- Emit the existing gate-decision log so an operator can audit outbound DTMF use.

## Consequences

- Outbound agents can navigate callee IVRs (the intended use of `place_call`), while the
  physical-access and call-control surface stays closed at level 0.
- The IVR-brute-force risk is bounded by ADR-0103's rate limit (the coupling is explicit,
  not incidental).
- Inbound behaviour is unchanged; the change is a narrow, direction-scoped, single-tool
  exception — auditable and reversible.

## Open questions for the operator (accept / adjust before implementation)

1. Accept the **per-tool** `send_dtmf` grant for the outbound persona (Option B), rather
   than a broader privilege bump?
2. Confirm the **ADR-0103 rate limit as a precondition** (sequence 1380 before 1284)?
3. Any additional scoping wanted — e.g. an allow-list of outbound targets permitted DTMF,
   or a per-call DTMF cap distinct from the general rate limit?

Once accepted, implementation is a bounded lane in `caller_modes.py` / the tool gate (the
direction-keyed single-tool allowance), TDD, with the inbound-fail-closed and
open-entry-still-denied cases pinned.
