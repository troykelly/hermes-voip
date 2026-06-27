# ADR-0089 — The client TLS context floors the negotiated version at TLS 1.2

**Status:** Accepted
**Date:** 2026-06-27

## Context

`_make_tls_context` (`src/hermes_voip/adapter.py`) builds the **sole** client TLS
context, and the same context secures **both** signalling legs: SIP-over-TLS and
WSS. Those legs carry secret material — the SIP digest password (the
`Authorization` HA1/response) and the SDES `a=crypto` inline SRTP master key and
salt (ADR-0070 secure-media mandate, ADR-0073 SDES selection).

The context was built by a bare `ssl.create_default_context()`. That helper sets
`check_hostname = True` and `verify_mode = CERT_REQUIRED` (correctly inherited),
but it leaves `minimum_version` at `ssl.TLSVersion.MINIMUM_SUPPORTED`. On Python
3.13 that defers the floor entirely to the host OpenSSL policy, which on a permissive
build can still negotiate **TLS 1.0 / 1.1** — protocols deprecated by RFC 8996.
A downgrade on these legs exposes the digest password and the SDES key/salt to the
weaknesses of obsolete TLS, undermining ADR-0005's SIP-over-TLS mandate.

## Decision

Set `ctx.minimum_version = ssl.TLSVersion.TLSv1_2` in `_make_tls_context`.

This is **downgrade-hardening, not a verification change**: `check_hostname`
(`True`) and `verify_mode` (`CERT_REQUIRED`) remain inherited from
`create_default_context()`, and no certificate pinning is introduced (the plugin
stays gateway-agnostic per CLAUDE.md). The floor is TLS 1.2 rather than 1.3 so the
plugin keeps interoperating with RFC-compliant gateways that have not yet enabled
1.3, while still excluding every deprecated protocol version.

The change is local and infra-free: it edits one stdlib `ssl.SSLContext` property
on the in-process client and commissions no new resource, so no runbook applies.

## Consequences

- A gateway that only offers TLS 1.0/1.1 now fails the handshake instead of
  silently downgrading the secret-bearing legs. This is the intended posture; such
  a gateway is non-compliant with current TLS guidance.
- The guarantee is verified by `tests/test_adapter_tls_context.py`, which calls the
  real `_make_tls_context` and pins `minimum_version == TLSv1_2`,
  `check_hostname is True`, and `verify_mode == CERT_REQUIRED` so a future
  weakening of any of the three is caught. It asserts context **properties** only —
  no live connection, no real host or certificate (public repo).
