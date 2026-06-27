# ADR-0090: Match our Contact binding by RFC 3261 §19.1.4 SIP-URI equality

- Status: Accepted
- Date: 2026-06-27
- Deciders: agent session (registration lane)

## Context

A REGISTER `200 OK` echoes EVERY binding the registrar holds for the AOR (RFC 3261
§10.3), each Contact carrying its own `expires`. `RegistrationFlow._granted_expires`
picks OUR binding out of that list to arm the refresh timer; failing a match it falls
back to the FIRST binding's `expires`, which on a shared AOR is a DIFFERENT device's
lifetime — arming the wrong timer and silently letting our own binding lapse.

The match was raw byte-for-byte string equality
(`_binding_uri(b) == self._contact_uri`). RFC 3261 §19.1.4 defines SIP-URI
equivalence more loosely than string equality: the scheme matches
case-insensitively, the userinfo case-sensitively, the host case-insensitively, and
a port that is omitted is equal to the scheme default. A perfectly compliant
registrar may echo our binding canonicalised — upper-casing the host
(`sip:1000@PBX.EXAMPLE.TEST`) and/or adding the explicit default port
(`…:5061`) — when our Contact omitted the port and lower-cased the host. Raw equality
misses it; the binding lapses.

## Decision

Add a module helper `_sip_uri_equal(a, b, *, default_port)` that compares the
`scheme + userinfo + host + port` prefix of two Contact addr-specs under §19.1.4:

- scheme — case-insensitive;
- userinfo — case-sensitive;
- host — case-insensitive;
- port — an omitted port equals an explicit `default_port`.

`_granted_expires` uses it instead of `==`, with a short-circuit on exact string
equality first (the common case — the registrar echoes us verbatim). A side helper
`_split_sip_uri` decomposes an addr-spec (dropping any `;uri-params` left attached by
`_binding_uri`, and handling a bracketed IPv6 host so the port colon is found
outside the brackets); on anything it cannot parse, the comparator falls back to
exact string equality so a malformed echo can never spuriously match.

### Default port is transport-derived, not purely scheme-derived

§19.1.4 keys the default port off the scheme (5060 `sip` / 5061 `sips`). But our
Contact uses a bare `sip:` addr-spec carrying `;transport=tls` (the param
`_binding_uri` strips), and on that secure leg the binding is genuinely reached over
5061 — so a registrar that echoes `…:5061` for our portless `sip:` Contact is binding
the SAME endpoint. We therefore elide the port against the ACTIVE Via transport's
signalling default (`_TRANSPORT_DEFAULT_PORT`: 5061 for TLS/WSS, 5060 for UDP/TCP),
not the bare-scheme default. `_sip_uri_equal` keeps a scheme-keyed `_DEFAULT_PORT`
fallback (`default_port=None`) for callers without a transport context. Both sides of
the comparison must already share a scheme (checked first), so a single elided
default is unambiguous.

## Consequences

- OUR binding is selected even when the registrar canonicalises host case or adds the
  explicit default port; the refresh timer is armed off our real lifetime.
- The change is local to `registration.py` and sans-IO — no new dependency, no I/O,
  no transport coupling beyond reading the already-validated `config.transport`.
- The comparison is a bounded string parse per binding in a 200 OK (a handful of
  bindings); no measurable cost on this cold path.
- Out of scope: full §19.1.4 (URI-parameter and header equivalence, `user`/`ttl`
  param rules). Only the prefix needed to identify our binding is compared; the rest
  is deliberately not implemented (the bindings are our own AOR's, not arbitrary
  URIs).
