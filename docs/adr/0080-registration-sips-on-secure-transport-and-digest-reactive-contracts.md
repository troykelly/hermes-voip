# ADR-0080: Registration enforces SIPS on secure transports; digest nc/branch/qop-less/stale contracts are pinned, not extended

- **Date:** 2026-06-26
- **Status:** Accepted
- **Deciders:** agent session (registration launch-readiness lane). Composes with
  ADR-0005 (SIP-over-TLS/SIPS mandate), ADR-0058 (SIP digest SHA-256/MD5-sess),
  ADR-0011 (multi-registration / Call-ID demux), and the `config.py`
  transport-restriction policy (`_VIA_TRANSPORT = {tls, wss}`).

## Context

A launch-readiness audit of `src/hermes_voip/registration.py` (the sans-IO SIP
REGISTER state machine) surfaced five items (backlog `bk231/239/243/247/250`). One
demands a real behavioural change; four demand that already-correct behaviour be
**pinned** so it cannot silently drift under a future refactor. The pressure is the
public-repo, gateway-agnostic, fail-fast posture the rest of the codebase already
holds — plus rule 6 (no scaffolding: do not add state with no reachable consumer)
and rule 19 (no weakened tests). The items:

- **bk231 — AOR scheme not constrained to `sips` on a secure transport.** ADR-0005
  mandates SIP-over-TLS (SIPS, RFC 3261 §26.2) for the encrypted transports, and
  `config.py` already restricts the SIP transport to `{tls, wss}`. Yet
  `RegistrationConfig` accepted a cleartext `sip:` AOR on a TLS/WSS transport:
  `_AOR_SCHEMES = frozenset({"sip", "sips"})` accepted both unconditionally, so the
  registrar request-URI and the digest `uri` could advertise an insecure scheme
  over a secure leg **with no signal**. The canonical test fixture itself encoded
  this inconsistency (`sip:` AOR on TLS).
- **bk239 — `nc` is always `00000001` on the authed REGISTER.** `_reauthenticate`
  answers each challenge with a fresh `cnonce` and `nc=1`. A reader expecting a
  persistent monotonic nonce-count might call this a gap.
- **bk243 — the Via branch change across the re-authenticated REGISTER is untested.**
  `_build` calls `new_branch()` per send (RFC 3261 §8.1.1.7 requires a new branch
  per client transaction), but nothing pinned it.
- **bk247 — the qop-less RFC 2069 digest path and `opaque` echo are untested at the
  registration level.** `digest.py` implements both, but every registration fixture
  offered `qop="auth"` and no `opaque`, leaving the registration↔digest seam
  unguarded on those paths.
- **bk250 — a second 401/407 in a transaction is always `Failed`, even with
  `stale=true`.** The flow does not perform an in-transaction stale-nonce retry, and
  there is no `DigestChallenge.stale` field.

`bk235` (a `transport` `Literal` + `expires` validation) is a separate, **blocked**
item (it conflicts with `GatewayConfig.via_transport` dict typing and must reconcile
both files in one lane); it is explicitly out of scope here.

## Decision

**(1) bk231 — ENFORCE.** `RegistrationConfig.__post_init__` now rejects a `sip:` AOR
when the transport is secure, with `ValueError`, mirroring the existing fail-fast AOR
validation (bk226). The check is **transport-gated**:

```python
_SECURE_TRANSPORTS = frozenset({"tls", "wss"})  # lower-cased; case-insensitive match

def _require_secure_scheme(aor: str, transport: str) -> None:
    scheme, _ = _split_aor(aor)
    if scheme.lower() == "sip" and transport.lower() in _SECURE_TRANSPORTS:
        raise ValueError(...)  # "aor must use the sips: scheme on a secure transport"
```

- UDP/TCP leave the AOR scheme to the deployer (the SIPS mandate is an invariant
  **only** on the encrypted transports, where signalling carries credentials).
- A `sips:` AOR is accepted on **any** transport (upgrading the scheme is never the
  inconsistency this guards).
- The comparison is case-insensitive (`tls` and `TLS` are one transport), matching
  how `config.py` lower-cases the configured transport.
- The malformed-AOR check (`_split_aor`) runs **first**, so an empty/no-scheme AOR
  still raises its existing error rather than the scheme-mismatch one.

**(2) bk239 — PIN, do NOT add an nc counter.** Each REGISTER is a fresh transaction
answering the challenge it just received, with a fresh `cnonce` and `nc=00000001`.
This is **correct** by RFC 7616 §3.4 for a purely-reactive flow: the plugin only
authenticates in response to a 401/407, so there is no reused nonce to count
against. A persistent monotonic `nc` would add state with **no reachable consumer**
(rule 6). A test asserts the authed REGISTER carries `nc=00000001` so it cannot
silently drift.

**(3) bk250 — PIN, do NOT implement stale-retry.** Any second 401/407 in a
transaction → `Failed` is kept as an **intentional, recorded limitation**. Recovery
is the `RegistrationManager`'s next refresh: a brand-new transaction that answers the
fresh nonce. A test pins that a second 401 carrying `stale=true` (with a fresh nonce)
is still `Failed`, and that the next refresh re-authenticates. The optional
in-transaction `stale=true` retry (parse `stale` + re-answer within the same
transaction) is left as a follow-up backlog note.

**(4) bk243 — TEST.** A test asserts the Via branch differs between the initial and
the authed REGISTER (both `z9hG4bK…`) while Call-ID and From-tag stay stable.

**(5) bk247 — TEST.** Registration-level tests cover the qop-less RFC 2069 MD5 path
(no `nc`/`cnonce`, 32-hex `response`, independently recomputed) and `opaque` echo
through the flow.

The canonical `_CONFIG` test fixture is corrected from a `sip:` AOR on TLS to a
`sips:` AOR (the scheme a real TLS deployment uses); dependent assertions move from
the `sip:` to the `sips:` registrar request-URI. No assertion was weakened (rule 19):
the fixture had encoded an invariant violation, and correcting it leaves every
assertion as strong, just on the mandated scheme.

## Consequences

- **One closed downgrade hole, fail-fast.** A `sip:`-on-TLS/WSS misconfiguration is
  now a loud `ValueError` at `RegistrationConfig` construction — never a silent
  insecure-scheme advertisement that surfaces later as a confusing gateway rejection.
  The blast radius: a deployment that genuinely wants cleartext signalling must use
  UDP/TCP (where `sip:` is still accepted) — consistent with the existing `config.py`
  `{tls, wss}` transport restriction, which already forbids cleartext SIP signalling
  for the gateway path.
- **No new state, no new dependency, no hot-path cost.** bk231 is one scheme check at
  construction; the four PIN items add only tests. Nothing runs per RTP packet or per
  REGISTER beyond the existing logic.
- **Recorded limitation.** Stale-nonce rotation is handled by the next refresh
  transaction, not in-transaction. If a future gateway is observed to rely on
  in-transaction `stale=true` recovery, the follow-up backlog item adds a
  `DigestChallenge.stale` field + a single in-transaction re-answer — a bounded,
  reversible change, deliberately deferred rather than scaffolded now.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Leave the AOR scheme entirely to the deployer (document, don't enforce) | The codebase is fail-fast everywhere else (bk226 AOR validation, `config.py` transport restriction). A silent `sip:`-on-TLS inconsistency is exactly the class of defect those guards exist to prevent; documentation alone does not stop a misconfiguration reaching the wire. |
| Enforce `sips:` on **all** transports | Over-broad: UDP/TCP are legitimately cleartext-signalling transports where `sip:` is the correct scheme. The SIPS mandate (ADR-0005) is scoped to the encrypted transports; forcing `sips:` on UDP/TCP would reject compliant cleartext deployments. |
| Cross-check the **Contact** scheme too | Out of scope for bk231 (AOR-vs-transport). The Contact scheme is a separate concern; bk235 (transport/expires validation) is the blocked item that would own broader config validation. Kept minimal (rule 28). |
| Thread a persistent monotonic `nc` counter (bk239) | Adds state with no reachable consumer in a purely-reactive flow (rule 6). RFC 7616 §3.4 makes `nc=00000001` correct when each REGISTER answers a freshly-received nonce; the counter would be dead scaffolding. |
| Implement in-transaction `stale=true` retry now (bk250) | Speculative: no observed gateway requires it, and recovery already works via the next refresh (a fresh transaction with the fresh nonce). Building it now is scaffolding for an unproven need (rule 6); it is a recorded follow-up instead. |

## References

- RFC 3261 §26.2 (SIPS), §8.1.1.7 (Via branch), §22 (digest).
- RFC 7616 §3.4 (nc/cnonce), §3.4.1 / RFC 8760 §2.6 (no qop-less form for SHA-256),
  RFC 2069 (legacy qop-less MD5).
- ADR-0005 (SIP-over-TLS/SIPS mandate), ADR-0058 (digest algorithms), ADR-0011
  (Call-ID demux / multi-registration).
- `src/hermes_voip/registration.py` (`_require_secure_scheme`,
  `RegistrationConfig.__post_init__`), `tests/test_registration.py`.
