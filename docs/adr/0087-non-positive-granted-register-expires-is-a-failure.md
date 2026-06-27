# ADR-0087 — A non-positive granted REGISTER expiry is a registration failure, and the refresh delay is floored

**Status:** Accepted
**Date:** 2026-06-27

## Context

`RegistrationFlow.handle` (`src/hermes_voip/registration.py`) treated any 2xx
response to a REGISTER as success and returned `Registered(expires=…)` with the
lifetime read from our echoed `Contact` binding. `RegistrationManager.on_response`
(`src/hermes_voip/manager.py`) then called `_schedule_refresh(state,
outcome.expires)`, which computed `delay = max(0.0, expires * refresh_fraction)`
with **no positive floor**.

RFC 3261 §10.3 permits a registrar to grant a **shorter** lifetime than requested,
including **0** — which means it removed the binding (a de-registration the client
did not request). Because `_granted_expires` returns the echoed value verbatim, a
200 OK whose `Contact` (our binding) carries `expires=0` produced
`Registered(expires=0)`, and `_schedule_refresh` then armed a **0-delay** refresh
task: `sleep(0)` → immediate re-REGISTER → the registrar again grants 0 → a tight
re-REGISTER loop that floods the gateway while the binding never stays up. A tiny
positive grant (e.g. 1–2 s) yields the same pathology at a sub-second cadence.

## Decision

Two independent guards, defence in depth:

1. **Flow level (primary).** `RegistrationFlow._handle_success` returns
   `Failed(0, "registrar granted non-positive expires (…); binding removed")`
   when the request was a registration (`requested_expires > 0`) but the granted
   lifetime is `<= 0`. A non-positive grant is a binding removal, not a live
   registration, so it is surfaced as a failure outcome — never `Registered`. The
   synthetic status `0` is outside the SIP 1xx–6xx range, so it cannot collide
   with a real status and unambiguously marks this anomaly. A 2xx to a genuine
   **de-registration** (`requested_expires == 0`) keeps its existing meaning (a
   clean unbind; not registered).

2. **Manager level (defence in depth).** `_schedule_refresh` floors the delay at a
   positive minimum: `delay = max(self._min_refresh_delay, expires *
   refresh_fraction)`. `min_refresh_delay` is a keyword-only constructor knob
   (default `1.0` s). Even if a tiny positive grant (or, defensively, a 0 that
   slipped past guard 1) reaches the scheduler, no near-zero-delay refresh is ever
   armed. Tests that deliberately drive an immediate refresh by hand pass
   `min_refresh_delay=0.0`.

Because guard 1 routes a non-positive grant through `_on_registration_failed`, an
**established** extension whose registrar later removes the binding is reported via
`on_registration_error` and recovers on the existing bounded-backoff re-REGISTER
ramp (never the 0-delay hot loop); a cold-start non-positive grant is reported and
not retried (consistent with the existing cold-start failure handling).

## Consequences

- A registrar that grants `expires=0` (or negative) can no longer drive a tight
  re-REGISTER loop: the manager treats it as a failure, marks the extension down,
  and surfaces the anomaly (rule 37), instead of busy-looping.
- A tiny positive grant is clamped to a sane refresh cadence rather than a
  sub-second hot loop.
- The change is behaviour-preserving for every healthy positive grant: the
  `Registered(expires=…)` path is unchanged for a normal lifetime, and the floor
  (1 s) is far below any realistic refresh window (`refresh_fraction` of a
  multi-second grant).
- `min_refresh_delay` is the only new public surface; all existing keyword knobs
  keep their defaults and meaning.
