# ADR-0054: SIP signalling robustness — registration-refresh recovery and inbound CANCEL

- Status: Accepted
- Date: 2026-06-19
- Deciders: agent session (engineering), operator (`troy@…`)
- Supersedes / relates to: ADR-0005 (SIP-over-TLS transport), ADR-0011
  (multi-registration manager), ADR-0010 (signalling), ADR-0038 (WSS signalling)

## Context

A code audit (RFC 3261) found five real robustness gaps in the SIP signalling
plane. None are exotic; all are reachable in normal operation against a
compliant gateway:

1. **A failed REGISTER refresh was a terminal silent dead-end.** When a periodic
   refresh REGISTER got a `Failed` (4xx/5xx/6xx) response, `RegistrationManager`
   only set `registered = False` — no retry, no backoff, no alert. The extension
   silently de-registered (inbound calls divert to voicemail) until process
   restart.
2. **The refresh had no Timer-F/B equivalent.** A refresh REGISTER that got *no
   response at all* left the binding marked `registered` forever (stale) — the
   binding may already have lapsed at the registrar, but the plugin kept
   believing it was up.
3. **Inbound CANCEL (RFC 3261 §9.2) was never handled.** A caller who abandoned
   setup sent CANCEL; `route_request` classified it `Unroutable` and the
   transport dropped it. The in-flight answer task could then still `200 OK` the
   abandoned INVITE — a ghost-answered call.
4. **`on_registration_error` was inert in production.** The manager's
   registration-failure reporting hook was never wired by the production adapter,
   so the recovery in (1)/(2) had nowhere to surface.
5. **Multi-binding granted-expiry read the wrong Contact.** `_granted_expires`
   read the *first* Contact of a multi-binding 200 OK (RFC 3261 §10.3) — it could
   arm the refresh timer off *another device's* lifetime and let our own binding
   lapse.

## Decision

### 1+2 — Registration refresh recovery (manager-owned, sans-IO timers)

`RegistrationManager` gains two reactive recovery mechanisms, both driven by the
existing sans-IO `RegistrationFlow` outcomes and asyncio timer tasks (no new IO
layer):

- **Response deadline (Timer F/B analogue).** Every REGISTER the manager sends
  (initial, refresh, re-auth, retry, recovery) arms a per-flow response-timeout
  task (`refresh_timeout`, default **32 s** — the RFC 3261 §17.1.1.2 Timer-F/B
  value). `on_response` cancels it on *any* response for that flow. If it fires,
  the flow is marked down, reported, and recovered. This means a refresh the
  registrar never answers can no longer leave a stale `registered` flag.
- **Bounded-backoff re-REGISTER.** On a `Failed` refresh response, a send
  failure, or a response timeout, an *established* flow schedules a recovery
  re-REGISTER with exponential backoff (initial **1 s**, cap **30 s**, ±20%
  decorrelation jitter — mirroring the transport reconnect supervisor in
  ADR-0005/the adapter). A success resets the backoff ramp.

**Recoverable vs deliberate.** Recovery runs only for a flow that was actually
`established` (a refresh/keep-alive outage) and is **not** `deregistering`. A
cold-start REGISTER that never succeeded is left to `connect()` / the transport
reconnect supervisor (re-registering it in a tight loop would hammer the
registrar on bad credentials); a deliberate de-registration's `Failed` is an
expected end, not an outage. The failure is surfaced as a typed
`RegistrationRejectedError` (carries only status/reason — never host/extension/
secret) or `RegistrationTimeoutError`.

### 3 — Inbound CANCEL (transport-owned server transaction)

CANCEL matching is a **transaction-layer** concern (by top Via branch, RFC 3261
§9.1), so it is owned by the TLS transport, not the dialog demux:

- The manager classifies a CANCEL as a new `Cancel` routing variant (it has no
  To-tag — it predates the dialog).
- The TLS transport records a pending INVITE *server transaction* (keyed by Via
  branch) the moment an inbound INVITE is handed to `on_new_call`. On a matching
  CANCEL it answers the **CANCEL `200 OK`**, the **INVITE `487 Request
  Terminated`**, marks the entry cancelled, and fires an `on_cancel(call_id)`
  hook. An unmatched CANCEL is answered **`481`**.
- **Race-safe 200 suppression.** The `cancelled` flag is set before any send, so
  `send()` **suppresses a `2xx` for a CANCELled INVITE** regardless of whether
  the answer task wins the race — closing the "still 200-OK a dead INVITE" gap
  even if the abort lands after the 200 OK was produced. The cancelled entry is
  retained (so a retransmitted 2xx is also suppressed) and cleared on
  `remove_call` at teardown.
- The `on_cancel` hook (wired by the adapter) cancels the call's setup task,
  tearing down any half-built media/CallLoop.

**Scope: TLS only.** The WSS transport (ADR-0038, a separate module) does not yet
implement the §9.2 server-transaction handling; over WSS a CANCEL continues to
surface via `on_unroutable` exactly as before (now via the explicit `Cancel`
branch). Porting CANCEL to WSS is a tracked follow-up — it is genuinely a
different transport implementation, not a silent omission.

### 4 — Wire `on_registration_error`

The production adapter passes `on_registration_error=self._on_registration_error`
at the single `RegistrationManager` construction site. The handler logs at
WARNING on the adapter logger and redacts the extension to its last two digits
(rule 34 — the extension number is sensitive); the manager's error message
carries only a status/reason.

### 5 — Select our Contact's expiry

`_granted_expires` flattens *all* `Contact` headers of the 200 OK into individual
bindings (handling both repeated headers and comma-separated values, tracking
angle-bracket / quote depth so a comma inside `<...>` is not a separator) and
reads the `expires` of the binding whose addr-spec matches our own Contact URI.
It falls back to the first binding, then the `Expires` header, then our requested
lifetime.

## Consequences

- A flapping or registrar-rejected registration now self-heals with bounded
  backoff and is observable in the operator log, instead of silently dead-ending
  until restart.
- An abandoned inbound call is cleanly terminated (200/487) and never
  ghost-answered; its half-built media is torn down.
- New manager knobs (`refresh_timeout`, `retry_backoff`) are keyword-only with
  RFC-aligned defaults; tests drive them to small values for determinism.
- All five fixes are sans-IO/transport-local and fully unit-tested against fakes
  and the loopback TLS server; no new dependency, no new transport/provider
  lock-in.

### Out of scope (named, not silently deferred)

- WSS inbound CANCEL handling (the server-transaction tracking is TLS-only).
- Registration recovery for a *cold-start* REGISTER that never succeeded (owned
  by `connect()` / the reconnect supervisor).
