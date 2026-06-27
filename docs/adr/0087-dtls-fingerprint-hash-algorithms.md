# ADR-0087: DTLS fingerprint hash algorithms fail closed

- **Date:** 2026-06-27
- **Status:** Accepted
- **Deciders:** agent session

## Context

DTLS-SRTP authenticates the DTLS peer certificate to the SIP/WebRTC signalling identity by
comparing the peer certificate fingerprint with the SDP `a=fingerprint:<hash-func>
<fingerprint>` attribute (RFC 8122). The existing `DtlsEndpoint.verify_peer_fingerprint`
path treated the SDP value as a SHA-256 fingerprint string and ignored the advertised
`hash-func` token. That is fail-open parsing: a weak, unsupported, or mismatched algorithm
could be misinterpreted as SHA-256 instead of being rejected as invalid signalling.

The repo is public and gateway-agnostic, so the policy must not encode a gateway quirk or
secret deployment detail. The Python code remains fully typed under `mypy --strict` and the
media path keeps pyOpenSSL behind the existing lazy optional-extra boundary.

## Decision

`DtlsEndpoint.verify_peer_fingerprint` parses the SDP hash-function token and computes the
peer certificate fingerprint with the same advertised algorithm only when that algorithm is
in the supported strong set: `sha-256`, `sha-384`, or `sha-512`. The endpoint rejects
`sha-1`, `md5`, malformed algorithm tokens, and unknown algorithms with `ValueError` before
any SRTP/SRTCP key derivation flag is set.

The local certificate fingerprint we advertise remains `sha-256` for interoperability and
minimal SDP size. Peer verification additionally accepts SHA-384 and SHA-512 when a peer
advertises those stronger algorithms.

## Consequences

The DTLS-SRTP setup now fails closed on weak or unsupported SDP fingerprint algorithms
instead of silently treating the value as SHA-256. Interoperability is maintained with
normal SHA-256 WebRTC/SIP-DTLS peers and with peers that advertise SHA-384 or SHA-512.
Legacy peers that advertise SHA-1 or MD5 fingerprints are rejected and must be upgraded or
reconfigured to a strong fingerprint algorithm.

The supported-algorithm set is explicit code policy and must be revisited through a
superseding ADR if future RFC guidance or interoperability testing requires a different
policy.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Continue assuming SHA-256 | Ignores the RFC 8122 `hash-func` token and can mis-bind or reject the wrong value without identifying the signalling error. |
| Support SHA-1 for legacy peers | SHA-1 is a weak fingerprint algorithm; accepting it weakens the DTLS peer-to-SDP binding for compatibility that is not required by this project. |
| Reject every non-SHA-256 algorithm | Secure, but unnecessarily rejects peers that advertise stronger SHA-384 or SHA-512 fingerprints that Python `hashlib` supports without new dependencies. |
| Trust `hashlib.new()` for any algorithm name | Would admit weak or surprising algorithms such as MD5/SHA-1 when available in the runtime and would make the security policy implicit. |
