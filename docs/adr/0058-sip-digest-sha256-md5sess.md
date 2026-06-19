# ADR-0058 — SIP Digest Auth: SHA-256 and MD5-sess algorithm support

**Status:** Accepted  
**Date:** 2026-06-19  
**Scope:** `src/hermes_voip/digest.py`

## Context

`digest.py` shipped with `_SUPPORTED_ALGORITHMS = frozenset({"md5"})`. Any
challenge using `algorithm=SHA-256` (RFC 8760 / RFC 7616) or `algorithm=MD5-sess`
(RFC 2617 §3.2.2) caused `build_authorization` to raise, aborting REGISTER outright.

A launch-readiness audit identified this as a blocking gap: hardened PBX deployments
and carriers commonly challenge with SHA-256 or SHA-256-sess; the plugin must be
gateway-agnostic (CLAUDE.md invariant).

## Decision

Extend `digest.py` to support all four algorithm variants mandated or recommended
by RFC 2617, RFC 7616, and RFC 8760:

| Algorithm    | Standard        | HA1 construction                                  |
|--------------|-----------------|---------------------------------------------------|
| `MD5`        | RFC 2617 §3.2   | `MD5(user:realm:pass)`                            |
| `MD5-sess`   | RFC 2617 §3.2.2 | `MD5(MD5(user:realm:pass):nonce:cnonce)`          |
| `SHA-256`    | RFC 7616 / 8760 | `SHA-256(user:realm:pass)`                        |
| `SHA-256-sess` | RFC 7616      | `SHA-256(SHA-256(user:realm:pass):nonce:cnonce)`  |

`HA2 = H(method:uri)` and the final `response` use the same hash function as `HA1`.

**Algorithm preference** — `pick_best_challenge` selects the strongest available
algorithm from a list of challenges (RFC 8760 §3 order):
`SHA-256-sess > SHA-256 > MD5-sess > MD5`.

**Unsupported algorithms** (SHA-512, unknown tokens) still raise `ValueError` rather
than silently mis-signing — rule 37.

**Parser** — `DigestChallenge.parse` already accepted any token for the `algorithm`
field; the regex was tightened from `\w+` to `\w[\w-]*` to handle hyphenated tokens
(`SHA-256`, `MD5-sess`) as single captures. Case-insensitive matching throughout;
the token is echoed verbatim in the Authorization header per the RFC.

**No new dependencies** — `hashlib.sha256` is part of the Python standard library.

## Validation

Known-answer test vectors:

- **RFC 2617 §3.5 MD5** (pre-existing): `6629fae49393a05397450978507c4ef1` ✓
- **RFC 7616 §3.9.1 SHA-256**: `753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1` ✓
- **MD5-sess** (SIP-shaped, independently computed): `5755195a21c2ac0abb4cba6554ac7efe` ✓
- **MD5-sess** (RFC 7616 inputs): `e783283f46242139c486a698fec7211d` ✓
- **SHA-256-sess** (RFC 7616 inputs): `2fd51b3a77ad75bad6afad6003e818d767133c46d9e2749e7f5232ae1ea3efd7` ✓

## Consequences

- REGISTER now succeeds against any gateway that challenges with MD5, MD5-sess,
  SHA-256, or SHA-256-sess — covering the current Grandstream UCM (MD5) and
  hardened SBCs/carriers (SHA-256 preferred).
- `pick_best_challenge` is available for the registration state machine to prefer
  SHA-256 when the gateway offers a choice.
- `_compute_ha1` takes 6 arguments; justified `noqa: PLR0913` is in place (the
  inputs are irreducible HA1 parameters from the RFC).
- SHA-384 / SHA-512 remain out of scope (no deployed SIP gateway requires them;
  add via the same `_hash_hex` dispatch if needed).
