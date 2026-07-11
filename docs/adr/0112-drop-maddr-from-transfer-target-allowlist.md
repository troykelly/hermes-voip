# ADR-0112: Drop `maddr` from the Refer-To transfer-target parameter allowlist

- **Date:** 2026-07-11
- **Status:** Accepted
- **Deciders:** agent session (gap-review finding + fix); operator owns the security posture
- **Relates to:** the outbound transfer-target injection guard (`refer.py`
  `_validate_transfer_target` / `_ALLOWED_URI_PARAMS`), the strict-ASCII transfer-target fix
  (#485), ADR-0009 (prompt-injection threat model — the caller can drive the agent).

## Context

`transfer_blind` / `transfer_attended` let a Hermes agent hand the call to an agent-supplied
target, interpolated into a `Refer-To:` header. Because the agent can be driven by a
prompt-injected caller (ADR-0009), that target is UNTRUSTED and gated by
`_validate_transfer_target`: it must be a bare dialable number, or a well-formed
`sip:`/`sips:` URI whose `;`-parameters are restricted to `_ALLOWED_URI_PARAMS`.

That allowlist included **`maddr`**. Per RFC 3261 §19.1.1 / §16.4, `maddr` **overrides the
network destination** a compliant proxy/UA routes the request to, while the URI `host` is
retained only for identity/comparison. So `sip:1000@trusted-gateway;maddr=<attacker>` passes
the guard (the host looks innocuous, `maddr` is allowlisted) yet routes the triggered INVITE
to the attacker — a **covert host-hijack** that defeats the exact protection the guard
exists to provide (its sibling `;Route=` / `?Replaces=` rejections target the same class).
The allowlist comment even asserted the params "carry no routing/dialog-seizing capability"
— false for `maddr` (rule 27).

## Decision

**Remove `maddr` from `_ALLOWED_URI_PARAMS`.** A Refer-To transfer target names WHERE to
transfer (the host); it has no legitimate need to re-aim routing away from that host, so a
`;maddr` on the target is now rejected outright (the whole target fails validation). The
allowlist comment is corrected to state the (now-true) no-destination-override property.

**Keep `transport`.** It selects only the wire protocol, not the destination host, and a
transfer legitimately may be `sip:x@host;transport=tls` (already tested/accepted). Its
residual risk is a *downgrade* — an attacker-supplied `;transport=udp` could push the
triggered leg to cleartext — but that is lower severity than a host-hijack, and blocking it
would reject legitimate TLS transfers; it stays allowlisted. `user`/`method`/`ttl`/`lr` are
benign and stay.

## Consequences

- A transfer target carrying `;maddr` is rejected (`ValueError`), closing the host-hijack.
  The existing accept-test that carried `;Maddr` is updated to drop it (a rule-19 behaviour
  change in its own commit), and a new reject-test pins the rejection.
- No legitimate transfer regresses (a real transfer names host + user, optionally
  `transport=tls`).
- Residual (noted, not fixed here): `transport` can still force a cleartext downgrade on the
  triggered leg — a follow-up may restrict it to secure transports (`tls`/`tcp`) if a
  deployment warrants it. Tracked as a lower-priority hardening, not part of this ADR.

## References

- RFC 3261 §19.1.1 (`maddr` in a SIP URI), §16.4 (proxy request targeting)
- `src/hermes_voip/refer.py` — `_validate_transfer_target`, `_ALLOWED_URI_PARAMS`
- PR #485 — the sibling strict-ASCII transfer-target fix from the same gap-review batch
