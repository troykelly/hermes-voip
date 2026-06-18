# ADR-0043: IPv6-first ICE + default public STUN

- **Date:** 2026-06-18
- **Status:** Accepted (amends ADR-0032 §STUN and ADR-0034 §"The STUN knob")
- **Deciders:** agent session (IPv6-first ICE lane) — operator-directed

## Context

ADR-0042 closed the inbound-WebRTC signalling/parse gaps; a live call reached
**ICE connectivity** but media did not complete from the test devcontainer.
Diagnosis (verified, command output recorded in the lane) showed the cause is the
**environment**, plus two real plugin gaps:

1. **IPv4-only ICE gathering.** `WebRtcMediaSession`'s default ICE factory
   constructed `IceConnection` without `use_ipv6`, so `aioice` gathered **only
   IPv4** host/srflx candidates — even though `IceConnection` already supports
   `use_ipv6`. The operator's standing directive is **IPv6-first, IPv4-fallback**.
2. **No STUN by default.** ADR-0032/0034 defaulted `ice_stun_urls` to empty
   (host-only ICE), so a NAT'd deployment gathered no server-reflexive candidate
   and a gateway behind NAT could not reach it.

The devcontainer itself is double-NAT'd (Docker on the operator's Mac, Mac on the
office LAN): **UDP over IPv6 does not traverse Docker's NAT66** (TCP-IPv6 works,
UDP-IPv6 to three public STUN servers all time out) and **UDP-IPv4 only yields a
hairpin address** the gateway cannot route back to. So a live WebRTC **media** call
cannot complete from inside the container — an environment limit, not a plugin
defect. A real host with working dual-stack UDP (e.g. the operator's Mac, which
shares the gateway's IPv6 `/48`) reaches the gateway directly.

## Decision

1. **IPv6-first ICE address families.** Add `ice_use_ipv4` / `ice_use_ipv6`
   (`HERMES_VOIP_ICE_USE_IPV4` / `HERMES_VOIP_ICE_USE_IPV6`), both default **on**,
   threaded `MediaConfig → WebRtcMediaSession → ice_factory → IceConnection`. So
   the agent gathers IPv6 **and** IPv4 candidates. `ice_candidates` lists **IPv6
   before IPv4** (stable within family) so the SDP answer advertises the IPv6 path
   first. ICE *nomination* is still driven by RFC 8445 candidate priority on the
   controlling peer; the ordering makes our family preference explicit and
   deterministic. Either family can be disabled (IPv6-only / IPv4-only).
2. **Default public STUN servers (operator-directed).** When
   `HERMES_VOIP_ICE_STUN_URLS` is **unset**, default to
   `DEFAULT_ICE_STUN_URLS = (stun:stun.l.google.com:19302,
   stun:stun.cloudflare.com:3478)` — free, no-auth, widely-used, **dual-stack**
   (both publish AAAA), so a NAT'd deployment gathers a srflx (incl. an IPv6 srflx
   on an IPv6-capable host) out of the box. An **explicit empty** value disables
   STUN (host-only ICE). The config parser distinguishes *unset* (→ default) from
   *explicit empty* (→ disabled) by reading `env.get` directly.

This is a deliberate change to ADR-0034's "empty by default" posture, made on the
operator's explicit instruction (2026-06-18) and recorded here. It does **not**
introduce a paid or stateful SaaS dependency (rule 36): public STUN is a stateless
reflexive-address echo, contacted only until the operator supplies their own list
(or disables it). TURN (a relay that *does* carry media) remains operator-provided
and empty by default (ADR-0034 unchanged).

## Scope / deferred (rule 6)

- **The `o=` origin address** in the WebRTC answer keeps its existing form; for
  the DTLS-SRTP profile there is no `c=` line (RFC 5763 §5) and the real media
  address is conveyed by ICE candidates, so the origin line is not the IPv6-first
  surface. Untouched to keep the diff minimal.
- **Live media validation** must run where UDP actually reaches the gateway. The
  devcontainer cannot (NAT66 drops UDP; IPv4 is hairpin). The runbook documents
  running the validation on a real dual-stack host. This lane lands the code +
  unit/e2e evidence; the live two-way-audio leg is an operator step on a real host.
- **TURN-over-TCP from the container** (relay of last resort) is a possible future
  lane if container-local live validation is ever required; named, not built.

## Consequences

- A dual-stack or IPv6 deployment now gathers and prefers IPv6 ICE candidates and
  gets a srflx for free behind NAT — the operator's IPv6-first intent, in code.
- One behavioural change for existing installs: unset STUN now means *default
  public STUN*, not *host-only*. An install that wants host-only sets
  `HERMES_VOIP_ICE_STUN_URLS=` (empty). Documented in runbook 0009.
- No new dependency, no licence/advisory change (`aioice` already present;
  `use_ipv6` is an existing parameter).

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| Keep STUN empty-by-default; require operators to set it | Defeats "works out of the box behind NAT"; the operator directed a default. |
| IPv6-only (drop IPv4) | Loses the fallback on IPv4-only gateways/paths; IPv6-first ≠ IPv6-only. |
| Re-prioritise candidates by editing aioice priorities | Fragile and library-internal; SDP-order preference + gathering both families achieves the intent without forking aioice's RFC 8445 priority maths. |
| Add an IPv6 STUN only | The defaults are dual-stack already; a single list serves both families (aioice queries the family it gathers). |
| Bundle a TURN relay by default | TURN relays media (stateful, bandwidth, auth) — an operator-run resource (ADR-0034), not a safe default. |
