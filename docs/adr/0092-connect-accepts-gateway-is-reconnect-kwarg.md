# ADR-0092: `VoipAdapter.connect` accepts the gateway `is_reconnect` keyword

- **Date:** 2026-06-28
- **Status:** Accepted
- **Deciders:** agent session

## Context

Issue #350 bumps the `hermes` extra from `hermes-agent==0.16.0` to `==0.17.0`.
The issue reports a live failure: the 0.17.0 gateway calling
`adapter.connect(is_reconnect=False)` against our `VoipAdapter.connect(self)`
(which took no such argument), raising `TypeError` on every connect so the VoIP
platform never came up.

Investigating the actually-installed `hermes-agent==0.17.0` runtime, every
adapter connect path routes through `gateway.run._connect_adapter_with_timeout`,
which calls `await adapter.connect()` with **no** `is_reconnect` argument (and
the abstract base `BasePlatformAdapter.connect` signature is still
`(self) -> bool`). The string `is_reconnect` appears nowhere in the installed
`hermes-agent==0.17.0` site-packages. So the precise call form named in #350 is
not exercised by this published build.

That does not make the fix unnecessary. Two facts stand:

1. The reported `TypeError` is real for any gateway build that *does* forward
   `is_reconnect` (a reconnect-aware variant, or a later point release). Our
   adapter must not break against it.
2. The corresponding fix must not break against the build that calls
   `connect()` with no argument — which is what 0.17.0 ships today.

The reconcilable contract is a **keyword-only parameter with a default**:
`connect(*, is_reconnect: bool = False)` is callable both ways.

## Decision

`VoipAdapter.connect` (and the mirrored `BasePlatformAdapterProtocol.connect`
in `hermes_surface.py`) take a keyword-only `is_reconnect: bool = False`. The
adapter **accepts-but-ignores** the flag: VoIP has no server-side message
backlog to replay on reconnect (SIP/RTP are live media with no durable queue),
and the adapter's own RFC 5626 reconnect supervisor
(`_supervise`/`_establish`) already restores registration and re-attaches
in-flight calls. The flag exists purely for gateway-base signature parity.

The `hermes` extra and `uv.lock` move to `hermes-agent==0.17.0`. The
`[project].version` is **not** bumped — release versioning is a separate
decision (see `docs/runbooks/0019-release-process.md`).

## Consequences

- `connect()` is a strict superset of the previous signature: existing callers
  (no-arg, as 0.17.0 ships) keep working, and a reconnect-forwarding gateway no
  longer raises `TypeError`. A parametrized contract test pins both call forms
  (`is_reconnect=False` and `True`) against the real `hermes-agent` runtime.
- The fix is defensive against a contract the published 0.17.0 build does not
  currently exercise. If a future hermes-agent release narrows or renames the
  reconnect signal, this ADR is the anchor to revisit — the accept-but-ignore
  body means no behavioural coupling was introduced, only signature tolerance.
- No new transport/provider/vendor lock-in; no infrastructure (rules 40–41).
