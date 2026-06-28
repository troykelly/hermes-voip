# ADR-0090: Pragmatic Contact-binding canonicalisation for REGISTER echoes

- Status: Accepted
- Date: 2026-06-27
- Deciders: agent session (registration lane)

## Context

A REGISTER `200 OK` echoes EVERY binding the registrar holds for the AOR (RFC 3261
§10.3), each Contact carrying its own `expires`. `RegistrationFlow._granted_expires`
picks OUR binding out of that list to arm the refresh timer; failing a match it falls
back to the FIRST binding's `expires`, which on a shared AOR is a DIFFERENT device's
lifetime — arming the wrong timer and silently letting our own binding lapse.

The previous match was raw byte-for-byte string equality
(`_binding_uri(b) == self._contact_uri`). That misses a registrar that canonicalises
OUR echoed Contact by lower/upper-casing the host or by adding the explicit signalling
port to a portless Contact, for example echoing `sip:1000@PBX.EXAMPLE.TEST:5061` for
our `sip:1000@pbx.example.test;transport=tls` Contact.

Strict RFC 3261 §19.1.4 SIP-URI equality is not the behavior we need here: it treats
an omitted component as different from an explicitly present default component, and it
also compares URI parameters/headers and defines percent-encoding equivalence. A strict
§19.1.4 comparison would therefore NOT recognise the registrar canonical echo that
caused this bug.

## Decision

Use a deliberately bounded helper, `_contact_binding_matches(a, b, *, default_port)`,
only for matching the registrar's echoed Contact binding against OUR configured Contact
inside `_granted_expires`. The helper parses a Contact addr-spec into
`scheme + userinfo + host + port` and applies pragmatic canonicalisation:

- scheme — case-insensitive;
- userinfo — case-sensitive;
- host — case-insensitive;
- port — an omitted port is normalised to the active transport's signalling default
  before comparing.

This is a documented deviation/superset relative to strict RFC 3261 §19.1.4 for this
specific REGISTER Contact echo problem, not generic SIP-URI equality. The safety bound is
that `_granted_expires` only chooses among bindings echoed for our own AOR; a false match
can select the wrong `expires` among our own canonical forms, not another AOR's device.
That is safer than failing to recognise OUR binding and falling back to the first echoed
binding on a shared AOR.

`_split_contact_binding_uri` decomposes an addr-spec and strips any remaining URI
parameters only from the hostport portion. It splits userinfo first with `rpartition("@")`
so semicolons inside userinfo, such as `sip:alice;day=tuesday@host`, are preserved. On
anything it cannot parse, the matcher returns `False` so a malformed echo can never
spuriously match.

### Default port is transport-derived, not purely scheme-derived

Our Contact can use a bare `sip:` addr-spec carrying `;transport=tls` (the param
`_binding_uri` strips), and on that secure leg the binding is genuinely reached over
5061 — so a registrar that echoes `…:5061` for our portless `sip:` Contact is binding the
same endpoint. We therefore elide the port against the ACTIVE Via transport's signalling
default (`_TRANSPORT_DEFAULT_PORT`: 5061 for TLS/WSS, 5060 for UDP/TCP), not by claiming
that RFC 3261 §19.1.4 equates omitted and explicit default ports. `_contact_binding_matches`
keeps a scheme-keyed `_DEFAULT_PORT` fallback (`default_port=None`) for callers without a
transport context, but `_granted_expires` always supplies the transport-derived default.

### Parameters and percent-encoding are intentionally out of scope

`_binding_uri` already strips parameters from bare Contact forms, and
`_split_contact_binding_uri` strips remaining name-addr URI parameters from the hostport
portion before this OUR-AOR-scoped comparison. Full parameter equivalence is deliberately
out of scope: strict §19.1.4 has one-sided `user`, `ttl`, `method`, and `maddr` rules, but
this helper does not compare arbitrary SIP URIs.

Percent-encoding equivalence is also out of scope. Our minted Contact contains only the
plain extension/host forms produced by `RegistrationConfig` and no percent-encoding, so
adding a decoder would expand behavior without addressing the registrar canonical echo
bug this ADR records.

## Consequences

- OUR binding is selected when the registrar canonicalises host case or adds the active
  transport's explicit default port; the refresh timer is armed off our real lifetime.
- The helper name and documentation no longer present this behavior as strict RFC 3261
  §19.1.4 URI equality.
- URI parameters, headers, and percent-encoding are not generic-equivalence features here;
  they are scoped out because the comparison is only for our own REGISTER Contact echo.
- The change is local to `registration.py` and sans-IO — no new dependency, no I/O, no
  transport coupling beyond reading the already-validated `config.transport`.
- The comparison is a bounded string parse per binding in a 200 OK (a handful of bindings);
  no measurable cost on this cold path.
