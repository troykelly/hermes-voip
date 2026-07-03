# ADR-0100: Categorise RTP transport-loss diagnostics (DNS vs generic) with operator-safe context

- **Date:** 2026-07-03
- **Status:** Accepted
- **Deciders:** agent session (media diagnostics lane, issue #413). Extends ADR-0075
  (machine-parseable `extra=` events) on the media plane; honours ADR-0084 (media/gateway
  connection detail is operator-sensitive); composes with ADR-0026 (transport-loss ends the
  call).

## Context

`_UdpReceiver.error_received()` (`media/engine.py`) ends the call on any fatal UDP/socket
error (ADR-0026) but logged every error identically:

```text
UDP error received — ending call as transport loss: [Errno 8] nodename nor servname provided, or not known
```

That `socket.gaierror` means the configured RTP destination host does **not resolve** from
the agent host — an SDP media-address problem — yet it is indistinguishable in the log from a
generic dead transport (an ICMP port-unreachable, a black-holed route). The operator saw a
call reach RTP transmit, then die as a "transport loss", with no signal that the root cause
was a name-resolution failure of the media address (#413). The fatal log carried neither the
error category nor any destination context.

## Decision

Categorise the error **before** ending the call and emit an ADR-0075 structured
`rtp_transport_lost` WARNING event alongside the (still human-readable) message:

- **Category.** `socket.gaierror` → `dns_resolution_failed`; anything else →
  `udp_transport_error`. `socket.gaierror` is a **subclass of `OSError`**, so it is tested
  **first** (`isinstance(exc, socket.gaierror)`), or every DNS failure would fall into the
  generic bucket — the central correctness trap of this fix.
- **Operator-safe destination context.** The event carries `remote_port` and
  `remote_host_kind` — `ip_literal` vs `hostname`, computed by trying
  `ipaddress.ip_address(host)` (a `ValueError` means it is a name). Only a `hostname` can
  fail to resolve, so this is the actionable discriminator. `_UdpReceiver` is given the
  configured (SDP-derived) `_remote_address`/`_remote_port` at construction to compute it.
- **Behaviour preserved.** The call is still ended in both cases (`on_lost(exc)` unchanged).

### Redaction level (the load-bearing choice)

The **raw remote host is never logged** — only its category token (`ip_literal`/`hostname`)
and the port. The existing operator RTP diagnostics (`rtp tx/rx: first packet -> <ip>:<port>`)
log the raw *latched peer IP*, which is an operational literal address discovered at runtime;
but the destination that fails here is the **configured SDP `c=` value**, which for the
failing case is precisely a **hostname** — the single most identifying media-connection
detail, and exactly what ADR-0084 marks operator-sensitive on a public repo. Logging the host
*kind* keeps every actionable bit (is it a name that could fail to resolve? which port?)
while leaking nothing. `str(exc)` is included in the human message because the resolver/errno
text ("nodename nor servname provided, or not known", "Connection refused") is a fixed
description and does **not** carry the destination host.

## Alternatives considered

- **Match the `rtp tx/rx` convention and log the raw host.** Rejected: those log a
  runtime-latched IP literal; here the sensitive value is a configured *hostname*. Logging it
  would leak media-connection PII into a public-repo-visible log for no diagnostic gain over
  the host *kind*.
- **Categorise inside `_on_transport_lost` (the callback) instead of `error_received`.**
  Rejected: `connection_lost` also routes there, the ICMP/`gaierror` distinction is only
  meaningful for `error_received`, and the receiver already owns the raw exception. Keeping
  the categorisation at the source is the minimal, clearest diff.
- **Log an `error_code`/`errno` structured field.** Deferred: not needed to distinguish the
  two operator actions (fix the media address vs investigate the transport); the errno is in
  the human message if required.

## Stretch (deferred, not shipped)

Proactively resolving/validating the RTP remote address when the transport starts (so a
resolution failure surfaces as a clear pre-media error rather than a late callback) is
**deliberately not done here**: a synchronous `getaddrinfo` on the event loop would risk a
blocking DNS call on the media hot path, and doing it correctly (async resolver, cached,
without changing connect() behaviour) widens the diff beyond this fix. Tracked as a follow-up;
this ADR ships the diagnostic categorisation only, wired end-to-end.

## Consequences

- Operators can now count `rtp_transport_lost` by `category` (runbook 0014) and immediately
  tell a bad/unresolvable SDP media address from a dead transport.
- One new construction argument pair on `_UdpReceiver`; no behaviour change to call
  teardown. The public repo carries no new host/IP/PII (redaction above).
