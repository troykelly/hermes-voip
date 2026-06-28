# ADR-0093: Sanitized `RegistrationError` string form + `RegistrationFailureCategory`

- **Date:** 2026-06-28
- **Status:** Accepted
- **Deciders:** agent session

## Context

`RegistrationManager` reports a failed keep-alive REGISTER to an operator-supplied
callback: `on_registration_error: Callable[[str, BaseException], None]`. On a
rejection it hands over a `RegistrationRejectedError`, whose constructor baked the
registrar's free-text reason-phrase into the `Exception` message:

```python
super().__init__(f"registration refresh rejected: {status} {reason}")
```

The reason-phrase comes from the SIP final response (`Failed(status, response.reason)`
→ the error in `manager.py`). It is **registrar-controlled** — and a registrar (or
anyone who can influence its responses) can place arbitrary free text there. A very
common operator wiring is to point `on_registration_error` straight at a logger or a
telemetry sink and log `str(error)` / `repr(error)`. That forwards the attacker-
influenced text (alongside the sensitive extension) into the operator's log/telemetry
pipeline.

This path is **not** covered by the #348 structured-log secret-safety guard: that
guard sanitizes only the WARNING `sip_registration_failed` event the manager emits
itself; it cannot constrain what an operator's own callback does with the raw
exception. The gap was flagged by codex during the #351 review.

A stale earlier lane (`fix/on-error-callback-leak-contract-1`) proposed only adding a
classification helper and *documenting* that consumers must not call `str(error)`.
That leaves the dangerous default in place — the leak still fires the moment any
consumer logs the exception, which is the overwhelmingly likely thing to do.

## Decision

Make the default string form of the error **safe by construction** (preferred Option
A), and add a sanitized typed category for explicit classification:

1. **`RegistrationRejectedError` no longer carries the registrar reason in its
   message / `args`.** The `Exception` message is now
   `f"registration refresh rejected: {status} (rejected)"` — the SIP status code
   (safe, useful) plus the sanitized category value. So `str(error)`, `repr(error)`,
   and `error.args` are all free of registrar text. The SIP `status` stays public.

2. **The raw reason is opt-in only.** It is stored on a private `_reason` attribute
   (off `args`) and exposed solely via a documented `raw_reason` property, whose
   docstring states the value is untrusted and that a consumer reading it owns
   validating/escaping it. Default logging cannot reach it; a consumer must
   deliberately ask for it.

3. **`RegistrationFailureCategory`** (`REJECTED` | `TIMEOUT` | `TRANSPORT_FAILED`) is
   added as the safe discriminator. Every `RegistrationError` exposes a `category`
   property, and a module-level `registration_failure_category(error)` classifies any
   `BaseException` (non-`RegistrationError` → `TRANSPORT_FAILED`). Consumers branch on
   the enum instead of parsing the string. The enum values intentionally match the
   existing `sip_registration_failed` `outcome` strings (ADR-0075), so the two
   surfaces stay consistent.

The public callback signature `(extension, error)` is **unchanged** — this is a
non-breaking fix. No operator code breaks; code that was logging `str(error)` simply
stops leaking, and code that genuinely needs the reason migrates to `raw_reason`.

## Why not the alternatives

- **Option B (helper + docs only):** rejected as the primary fix. It relies on every
  consumer reading a docstring and not doing the obvious thing (logging the
  exception). Safety-by-documentation is not safety; the default must be safe. (We
  still ship the enum/helper from Option B — it is complementary, not a substitute.)
- **Changing the callback signature to pass a category instead of the error:** a
  gratuitous breaking change to a public API. The error object still carries useful
  typed data (`status`, `category`, opt-in `raw_reason`); sanitizing its *string* form
  achieves the safety goal without breaking anyone (rule: don't make breaking changes
  without recording why — here the breaking change buys nothing).

This mirrors the precedent in ADR-0086, where `OutboundCallFailed.reason` was likewise
stopped from being echoed verbatim because it risks leaking gateway-controlled text.

## Consequences

- An `on_registration_error` consumer that logs `str(error)` / `repr(error)` is now
  secret-safe by default; the registrar reason cannot leak through it.
- Consumers classify failures via `registration_failure_category(error)` /
  `error.category` (`RegistrationFailureCategory`) without touching free text.
- A consumer that truly needs the registrar reason opts in via `raw_reason` and owns
  sanitizing that untrusted text.
- No change to the public callback signature; no migration required for existing
  operators. The internal `last_error` flow state is unaffected (it is never rendered).

## Validation

- TDD: a RED test
  (`test_on_registration_error_callback_error_str_omits_registrar_reason`) feeds a
  fake attacker-controlled reason via a rejected refresh and asserts it is absent from
  `str(error)`, `repr(error)`, and `error.args` handed to the callback, while the SIP
  status remains visible. It fails on the pre-fix code (the reason is present in
  `str(error)`) and passes after.
- `mypy --strict` clean (no `Any` / `# type: ignore` / `cast`); errors still propagate
  (rule 37 — nothing is swallowed).
