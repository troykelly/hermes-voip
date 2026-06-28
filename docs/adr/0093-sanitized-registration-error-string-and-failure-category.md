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

2. **The raw reason is opt-in only — but the pre-existing public accessor is
   retained.** The reason is stored on a private `_reason` attribute (off `args`)
   and exposed via two equivalent read-only properties: `reason` — the
   **backwards-compatible** accessor, because original `main` exposed
   `RegistrationRejectedError.reason` as a public attribute (an external operator
   callback that reads `error.reason` keeps working unchanged) — and `raw_reason`,
   its explicit-intent alias. Both return the same untrusted text, both carry the
   same docstring contract (the value is registrar-controlled and a consumer that
   reads it owns validating/escaping it), and both are **excluded** from the
   sanitized default rendering (`str`/`repr`/`args`). Default logging cannot reach
   the reason; a consumer must deliberately ask for it via `reason`/`raw_reason`.

3. **`RegistrationFailureCategory`** (`REJECTED` | `TIMEOUT` | `TRANSPORT_FAILED`) is
   added as the safe discriminator. Every `RegistrationError` exposes a `category`
   property, and a module-level `registration_failure_category(error)` classifies any
   `BaseException` (non-`RegistrationError` → `TRANSPORT_FAILED`). Consumers branch on
   the enum instead of parsing the string. The enum values intentionally match the
   `sip_registration_failed` `outcome` string literals (`"rejected"` / `"timeout"`
   / `"transport_failed"`) defined in `manager.py`'s `_on_registration_failed`
   (introduced by PR #348), so the two surfaces stay consistent.

The public callback signature `(extension, error)` is **unchanged**, and the public
`RegistrationRejectedError.reason` accessor is **retained** — so this is a genuinely
non-breaking fix. No operator code breaks: code that was logging `str(error)` simply
stops leaking; code that reads `error.reason` keeps reading the same (now
documented-untrusted, opt-in) value; and `raw_reason` is available as an
explicit-intent alias for new code.

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
- A consumer that truly needs the registrar reason opts in via the retained public
  `reason` accessor (or its `raw_reason` alias) and owns sanitizing that untrusted
  text. Both are excluded from the sanitized default rendering.
- No change to the public callback signature, and the public `reason` accessor is
  retained, so no migration is required for existing operators (including any that
  read `error.reason` directly). The internal `last_error` flow state is unaffected
  (it is never rendered).

## Validation

- TDD: a RED test (`test_callback_error_str_omits_registrar_reason`) feeds a fake
  attacker-controlled reason via a rejected refresh and asserts it is absent from
  `str(error)`, `repr(error)`, and `error.args` handed to the callback, while the SIP
  status remains visible. It fails on the pre-fix code (the reason is present in
  `str(error)`) and passes after.
- Compat (codex #351 BLOCK follow-up): a second RED test
  (`test_registration_rejected_reason_attribute_retained_for_compat`) asserts the
  retained public `error.reason` accessor still returns the registrar reason verbatim
  while `str`/`repr`/`args` stay sanitized — it failed (`AttributeError`) on the
  interim code that dropped `.reason` and passes once the accessor is restored.
- Future-subclass guard: `test_registration_error_subclasses_never_leak_reason_in_default_form`
  enumerates every concrete `RegistrationError` subclass (and pins the cover set, so a
  new subclass must register) and asserts a passed-in sentinel never appears in
  `str`/`repr`/`args` — locking the invariant against a later subclass that forwards
  registrar/gateway text.
- `mypy --strict` clean (no `Any` / `# type: ignore` / `cast`); errors still propagate
  (rule 37 — nothing is swallowed).
