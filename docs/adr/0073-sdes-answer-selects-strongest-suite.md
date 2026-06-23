# ADR-0073: SDES answer selects the STRONGEST offered SRTP crypto suite (downgrade resistance)

- **Date:** 2026-06-23
- **Status:** Accepted
- **Deciders:** agent session (security lane). Refines ADR-0013 (SDES SRTP media
  encryption) and ADR-0053 (inbound SDES Stage 1); composes with ADR-0070 (secure-media
  mandate) and ADR-0067 (outbound SDES offering).

## Context

RFC 4568 SDES lets a peer offer several `a=crypto` lines and lets the **answerer** accept
any **one** of them. Our two supported suites share the same AES-CM-128 cipher and differ
only in the SRTP **auth-tag** length: `AES_CM_128_HMAC_SHA1_80` (80-bit integrity) versus
`AES_CM_128_HMAC_SHA1_32` (32-bit integrity).

`_negotiate_answer_crypto` (`sdp.py`) previously accepted `audio.crypto_attrs[0]` — the
**first** supported, well-formed crypto in offer order. A gateway, or a MITM able to
reorder the (signalling-encrypted but attacker-influenced) offer, that lists
`AES_CM_128_HMAC_SHA1_32` before `AES_CM_128_HMAC_SHA1_80` would pull our answer to the
weaker 32-bit auth tag — a within-SRTP **integrity downgrade** (media stays encrypted, but
forgery resistance drops from 80 to 32 bits). The strength ranking already existed in
`media/srtp.py` (`_AUTH_TAG_LEN`), unused by the answer path.

## Decision

Among the offer's supported + well-formed crypto attributes, the SDES answer selects the
one with the **strongest** SRTP auth tag (prefer `SHA1_80` over `SHA1_32`), not the
first-offered. On a strength tie or a single suite, the first-offered of the strongest
suites wins (prior behaviour, so the tag is preserved for existing single-suite offers).

The strength metric is the auth-tag length, exposed from `media/srtp.py` as a small public
helper `crypto_suite_strength(suite) -> int` (higher == stronger; unknown suite ranks `0`).
`sdp.py` imports it **lazily inside the function** because `media/srtp.py` imports
`CryptoAttribute` from `sdp.py` at module load — a module-level import would be circular.
The ranking is defined once (in `srtp.py`) and reused, never duplicated.

## Consequences

- An offer (or reordered offer) that lists the 32-bit suite first is now answered with the
  80-bit suite, closing the integrity downgrade. Both peers still negotiate a working SRTP
  context (we echo the chosen suite's tag with our own key material).
- No regression: a SHA1_80-first offer still answers SHA1_80; an offer carrying ONLY
  SHA1_32 still answers SHA1_32 (selecting the strongest among one suite is that suite).
- Future suites: adding a stronger suite to `_AUTH_TAG_LEN` automatically makes the
  answerer prefer it, with no change to `sdp.py`.

## Alternatives considered

- **Honour offer order (status quo).** Rejected: lets the peer/MITM dictate integrity
  strength — the downgrade this ADR fixes.
- **Duplicate the ranking in `sdp.py`.** Rejected: two sources of truth for suite strength
  drift apart; the ranking lives with the suites it describes (`srtp.py`).
