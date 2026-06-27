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

1. **Flow level (primary).** `RegistrationFlow._handle_success` returns a
   `Failed(0, …)` outcome when the request was a registration
   (`requested_expires > 0`) but the granted lifetime for OUR binding is not a
   usable positive value:
   - a value parsed as `<= 0` (`"… a non-positive (0) expires for our binding; removed"`)
     — the registrar removed the binding; or
   - a **malformed** echo (`"… a malformed/negative expires for our binding; removed"`)
     — a negative or non-numeric `expires` token on our Contact. RFC 3261
     §10.2/§10.3 define Expires as a non-negative delta-seconds, so a value such as
     `expires=-1` or `expires=abc` is invalid. `_granted_expires` parses the whole
     `expires=` value token (via `_EXPIRES_TOKEN`) and returns `None` for a present
     but malformed value, so the flow **fails closed** instead of silently
     discarding the garbled token and falling back to the positive requested
     lifetime (which would mask a registrar that did not grant our binding — codex
     MUST-FIX 1). A binding with **no** `expires` parameter is not malformed: it
     still falls through to the `Expires` header / requested-lifetime fallbacks.

   Either case is surfaced as a failure outcome — never `Registered`. The synthetic
   status `0` is outside the SIP 1xx–6xx range, so it cannot collide with a real
   status and unambiguously marks this anomaly. A 2xx to a genuine
   **de-registration** (`requested_expires == 0`) keeps its existing meaning (a
   clean unbind; not registered) — a malformed expires there maps to `0` (there is
   no live lifetime to arm anyway).

2. **Manager level (defence in depth).** `_schedule_refresh` floors the delay at a
   positive minimum: `delay = max(self._min_refresh_delay, expires *
   refresh_fraction)`. `min_refresh_delay` is a keyword-only constructor knob
   (default `1.0` s) that **must be strictly positive**: a `0` or negative floor
   would defeat the very guard it provides, so `RegistrationManager.__init__`
   rejects `min_refresh_delay <= 0` with `ValueError` — the public knob can never be
   set to a guard-defeating value (codex MUST-FIX 2). Even if a tiny positive grant
   (or, defensively, a 0 that slipped past guard 1) reaches the scheduler, no
   near-zero-delay refresh is ever armed. Tests that deliberately drive an immediate
   refresh by hand reach past the public knob via the private `_min_refresh_delay`
   attribute (a test seam), never by passing a guard-defeating value to the
   constructor.

Because guard 1 routes a non-positive **or malformed** grant through
`_on_registration_failed`, an **established** extension whose registrar later
removes or garbles the binding is reported via `on_registration_error` and recovers
on the existing bounded-backoff re-REGISTER ramp (never the 0-delay hot loop); a
cold-start non-positive/malformed grant is reported and not retried (consistent with
the existing cold-start failure handling).

## Consequences

- A registrar that grants `expires=0`, a negative `expires`, or a non-numeric
  `expires` token on our binding can no longer drive a tight re-REGISTER loop (nor a
  silent positive fallback that masks the anomaly): the manager treats it as a
  failure, marks the extension down, and surfaces it (rule 37), instead of
  busy-looping.
- A tiny positive grant is clamped to a sane refresh cadence rather than a
  sub-second hot loop; the floor (`min_refresh_delay`) is hard-enforced positive at
  construction, so it cannot be disabled into a guard-defeating value through the
  public API.
- The change is behaviour-preserving for every healthy positive grant: the
  `Registered(expires=…)` path is unchanged for a normal lifetime, and the floor
  (1 s) is far below any realistic refresh window (`refresh_fraction` of a
  multi-second grant).
- `min_refresh_delay` is the only new public surface; all existing keyword knobs
  keep their defaults and meaning.
