# ADR-0034: ICE TURN relay, trickle-ICE SDP primitives, and consent freshness

- **Date:** 2026-06-18
- **Status:** Accepted
- **Deciders:** agent session (WebRTC/ICE lane) — operator-directed
- **Amends:** ADR-0016 §3/§6 (ICE: "TURN deferred", "non-trickle MVP") and
  ADR-0032 §5 (the named TURN / trickle deferrals). Those deferrals are now
  partially closed; the residual boundary (in-dialog SIP-INFO trickle transport)
  is named below.

## Context

ADR-0016 shipped full, non-trickle ICE over `aioice` and **deferred** three
WebRTC connectivity features, each re-confirmed as deferred in ADR-0032 §5:

1. **TURN relay candidates** — needed when neither a host nor a server-reflexive
   path is usable (symmetric NAT, restrictive firewalls). `HERMES_VOIP_ICE_TURN_*`
   were parsed-but-reserved (actually: not parsed at all — see below), and
   `IceConnection` passed only `stun_server` to `aioice`.
2. **Trickle ICE** (RFC 8838) — incremental candidate exchange for faster call
   setup, vs gathering the full generation before the offer/answer.
3. **ICE consent freshness** (RFC 7675) — periodic STUN consent checks on
   established media so a call behind a NAT whose mapping silently expires is torn
   down deterministically rather than wedging (gap-analysis #6).

Three findings, **verified against the pinned `aioice==0.10.2` source**, bound the
decision and are the reason this ADR is mostly *wiring*, not *building*:

- **Consent freshness is already implemented inside `aioice`.** `Connection.connect()`
  arms a `query_consent` task that issues a STUN binding request on each nominated
  pair every ~5 s (`CONSENT_INTERVAL=5`, randomised ×0.8–1.2) and, after
  `CONSENT_FAILURES=6` consecutive failures, calls `self.close()` (RFC 7675). We
  **must not** reimplement it (rule 19/28: minimal in-scope diffs, no duplicate
  machinery).
- **`aioice` already supports TURN.** `Connection(turn_server=…, turn_username=…,
  turn_password=…, turn_ssl=…, turn_transport=…)` gathers a `relay` candidate via
  its bundled TURN client. The work is to thread operator-provided credentials
  through; the plugin **consumes** a TURN server, it does not run one (running a
  TURN server is external infrastructure, rule 40/41 — out of a plugin's scope).
- **`aioice`'s receive side already supports incremental remote candidates.**
  `add_remote_candidate` can be called after `connect()`; the connectivity-check
  loop stays alive while end-of-candidates has not been signalled
  (`check_periodic` returns truthy while `_remote_candidates_end is False`), and
  late candidates trigger checks. The missing trickle piece is therefore **not in
  the ICE agent** — it is in **SIP signalling**.

The bounding constraints: AGENTS.md rule 6 (end-to-end or not at all — no
half-wired "trickle" that claims an incremental channel that does not exist), rule
17/39 (no type escape hatches; webrtc-only imports stay behind the `webrtc` extra),
rule 34 (a TURN password is a secret: never logged, echoed, or committed; tests use
obvious fakes), rule 23/26 (live behaviour against a real TURN server is validation,
not assumption), and the lane territory (`media/ice.py`, `media/webrtc_session.py`,
`sdp.py`, with one minimal `adapter._setup_webrtc_call` construction-site touch for
TURN config; the in-dialog/inbound-INVITE adapter regions belong to other lanes).

## Decision

**Close the TURN and consent-freshness deferrals end-to-end, and ship the trickle
SDP/ICE primitives while naming the in-dialog SIP-INFO trickle transport as the one
residual boundary.** Concretely:

### 1. TURN relay (consume operator-provided credentials)

- **Config (`config.py`).** Three new keys parsed into `MediaConfig`, defaulted so
  non-WebRTC and host/STUN-only installs are unaffected:
  - `HERMES_VOIP_ICE_TURN_URLS` → `ice_turn_urls: tuple[str, ...]` (comma-separated
    `turn:`/`turns:` URLs; same trim-and-drop-blanks parsing as `ICE_STUN_URLS`).
  - `HERMES_VOIP_ICE_TURN_USERNAME` → `ice_turn_username: str | None`.
  - `HERMES_VOIP_ICE_TURN_PASSWORD` → `ice_turn_password: str | None` (a secret;
    suppressed from `repr` via `field(repr=False)` on `MediaConfig`, alongside the
    SDES-key suppression already there — it must never reach a log line/traceback).
  - **Validation at load (rule 27, loud-not-silent):** if `ice_turn_urls` is set
    but `ice_turn_username`/`ice_turn_password` is missing, raise `ConfigError`.
    TURN long-term credentials are mandatory (RFC 8656 §9.2); a credential-less
    `turn:` URL would silently gather no relay candidate, which rule 27 forbids.
- **ICE layer (`media/ice.py`).** `IceConnection.__init__` gains `turn_urls:
  tuple[str, ...] = ()`, `turn_username: str | None = None`, `turn_password:
  str | None = None`. It parses the **first** `turn:`/`turns:` URL (aioice accepts
  one TURN server) via a new `_parse_turn_url`, which returns
  `(host, port, ssl, transport)`:
  - `turns:` ⇒ `turn_ssl=True`, default port **5349**; `turn:` ⇒ `turn_ssl=False`,
    default port **3478** (RFC 8656 §5).
  - an optional `?transport=tcp|udp` query selects `turn_transport` (default `udp`).
  - the parsed values plus `turn_username`/`turn_password` are passed to
    `aioice.Connection(...)`, which then gathers a `relay` candidate. The narrow
    `_RawConnectionCtor` Protocol grows the five TURN keyword parameters so the call
    stays fully type-checked in both gate environments (no `Any`, no cast).
  - **Security:** `_parse_turn_url` raises on a malformed URL **without echoing the
    URL's userinfo**, and no TURN credential is ever logged. The existing
    `ice: nominated pair …` info log already prints only host/port (not creds).
- **Session + adapter.** `WebRtcMediaSession`, its `_IceFactory` Protocol, and
  `_default_ice_factory` thread `turn_urls`/`turn_username`/`turn_password` through
  to `IceConnection`. `adapter._setup_webrtc_call` passes
  `turn_urls=media_cfg.ice_turn_urls`, `turn_username=media_cfg.ice_turn_username`,
  `turn_password=media_cfg.ice_turn_password` at the one `WebRtcMediaSession(...)`
  construction site (a distinct region from the inbound-INVITE/turn-delivery code
  other lanes own).
- **Honest boundary (rule 6/26).** TURN candidate **gathering + signalling** is
  wired and unit-proven (the aioice constructor receives the right params; a fake
  TURN-shaped candidate round-trips through SDP). A **full live relay** — an actual
  allocation against a real TURN server and media over the relay — requires an
  operated TURN server the plugin is pointed at; that is an **operator validation
  step** (recorded in the runbook), exactly like the live-gateway validation
  pattern. It is not asserted as proven here.

### 2. Trickle-ICE SDP/ICE primitives (+ half-trickle answer)

The genuine trickle gap is the SIP signalling channel, not the ICE agent. This lane
ships the standards-conformant **primitives** so the plugin (a) advertises trickle
capability, (b) correctly interoperates with a *trickling* peer's SDP, and (c)
drives end-of-candidates deterministically — with **no half-wired incremental
channel**:

- **`a=ice-options` (RFC 8839 §5.6, RFC 8838 §4.1).** `sdp.py` parses
  `a=ice-options` into `AudioMedia.ice_options: tuple[str, ...]` (e.g. `ice2`,
  `trickle`). `is_trickle` (a convenience property) is `True` when `trickle` is
  present. The WebRTC answer builder emits **`a=ice-options:trickle ice2`** —
  declaring we support trickle and ICE2 (RFC 8445). Advertising trickle is always
  safe: a non-trickle answer that *also* lists all candidates and ends them is a
  valid degenerate ("half-trickle") case (RFC 8838 §4.1).
- **`a=end-of-candidates` (RFC 8838 §8.2).** `sdp.py` parses it into
  `AudioMedia.end_of_candidates: bool` and the answer builder emits it (we send our
  full candidate set in the answer, then mark the end — half-trickle). On the
  **receive** side, `WebRtcMediaSession.run_handshake` **always** signals
  end-of-candidates to `aioice` after the offer's candidates. The reason is the
  residual boundary below: with **no transport to *receive* a trickled candidate**,
  withholding the end marker for a trickling peer would leave `aioice`'s check loop
  open waiting for candidates that can never arrive — an ICE hang. So the plugin
  *advertises* trickle (it accepts trickle) but *acts on* the initial offer's
  candidate set and ends candidates — the safe half-trickle behaviour that
  interoperates with classic and trickling peers alike. (An earlier draft made
  end-of-candidates conditional on the parsed trickle markers; the cross-vendor
  review correctly flagged that as an ICE-hang risk given the missing receive
  transport, so the conditional was removed — we always end.)
- **The residual boundary, named (rule 6).** The *transport* that would deliver a
  trickling peer's later candidates — **in-dialog SIP INFO with
  `application/trickle-ice-sdpfrag` (RFC 8840)** — is **deferred**. The plugin has
  no INFO handler, and the in-dialog/INVITE adapter surface that would host one
  belongs to the SIP-signalling lanes (outbound-WSS #32, rich-payload #39), not this
  media lane. In practice WebRTC SIP gateways send a complete candidate set in the
  initial SDP (the same reason ADR-0016 chose a non-trickle MVP), so always ending
  candidates does not regress any working call. Wiring the SIP-INFO trickle transport
  (and only then making end-of-candidates conditional) is a real, named follow-up —
  mirroring how ADR-0032 deferred outbound WebRTC to the signalling plane.

### 3. ICE consent freshness (aioice-native; verified + locked, NOT reimplemented)

`aioice` runs RFC 7675 consent freshness internally: `Connection.connect()` arms a
`query_consent` task that issues a periodic STUN consent check on each nominated pair
(`CONSENT_INTERVAL=5` s, randomised ×0.8–1.2) and, after `CONSENT_FAILURES=6`
consecutive failures, calls `self.close()`. **The investigation found there is no
recv-hang defect to fix** (an earlier draft of this ADR claimed one and proposed a
"closed monitor" task — that claim was wrong and is withdrawn): `aioice`'s `close()`
calls each STUN protocol's `transport.close()`, which fires `connection_lost`, which
enqueues the `(None, …)` queue sentinel a blocked `recvfrom()` is waiting on, so a
parked `recv()` **raises `ConnectionError` rather than hanging** (verified against a
real aioice loopback pair). The engine's existing `_ice_recv_loop` **already**
converts that `recv()` exception into a transport-loss teardown
(`_media_timed_out=True; _stop_event.set()`).

So this lane **adds no consent code** (rule 28 — no duplicate machinery, no second
consent loop). It instead **verifies and locks** the existing end-to-end behaviour
with tests:

- the `aioice` `query_consent` task is live after `IceConnection.connect()` (proving
  consent freshness is active on every WebRTC call with no code of ours);
- a `recv()` blocked when the connection `close()`s raises `ConnectionError` (the
  consent-loss wake path);
- an ICE pipe that closes mid-call drives the engine to a transport-loss teardown
  (`media_timed_out`) — **independent of media flow**, so a held/quiet NAT'd call is
  torn down on consent loss even though the media-inactivity timeout alone would not
  fire (gap-analysis #6).

We keep `aioice`'s RFC-grounded `CONSENT_INTERVAL`/`CONSENT_FAILURES` defaults — there
is no timing or threshold of our own to second-guess the mechanism.

## Consequences

- **Easier:** a deployment behind symmetric NAT can now be given a TURN server with
  one config block (`HERMES_VOIP_ICE_TURN_URLS/_USERNAME/_PASSWORD`) and get relay
  candidates with no code change. Long-lived NAT'd WebRTC calls now drop
  deterministically on consent loss rather than wedging. The plugin advertises
  trickle/ice2 and correctly reads a trickling peer's SDP markers.
- **Harder / committed to maintain:** the `_RawConnectionCtor` Protocol now mirrors
  five more `aioice.Connection` keyword params (a maintenance point if aioice's TURN
  signature changes — pinned at `0.10.2`). `MediaConfig` now carries a secret
  (`ice_turn_password`), handled with the same `repr=False` discipline as the SDES
  keys. No new background tasks or consent machinery (consent is aioice's).
- **Efficiency (rule 22):** negligible. TURN gathering is one extra allocation
  round-trip at setup only (when configured). The consent task is aioice's, already
  present — this lane adds no per-call task and no per-packet cost. The steady-state
  SRTP/jitter/decode path is byte-for-byte unchanged. Trickle primitives are sans-IO
  SDP string handling.
- **Security:** the TURN password is a long-term credential; it is suppressed from
  `repr`, never logged, and `_parse_turn_url` errors do not echo userinfo. Relay
  media still rides DTLS-SRTP end-to-end (the TURN server relays ciphertext; it is
  not in the media-trust path). No new network listener; the plugin only *connects*
  to the operator's STUN/TURN.
- **Lock-in / cost:** none new. `aioice` already bundles the TURN client (BSD-3); no
  new dependency, no `uv.lock` change. A TURN server, if used, is OSS (`coturn`) or
  operator-contracted infrastructure pointed at by config — gated by rules 40/41 in
  its runbook, never click-created.
- **What is proven vs validated live:** TURN URL parsing (incl. userinfo rejection),
  the aioice TURN-param wiring, the trickle SDP round-trip + half-trickle answer +
  always-end-of-candidates, the consent task being armed, and the
  consent-close→recv-raise→engine-teardown chain are **unit-proven** (TDD red→green,
  deterministic in-memory fakes + a real aioice loopback pair). A **full live relay
  against a real TURN server** and **consent expiry against a real NAT** remain
  **operator validation steps** (runbook 0009), not asserted here (rule 23/26).

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Reimplement RFC 7675 consent freshness in our layer | `aioice` already implements it (verified: `query_consent`, 5 s/6-failure → `close()`), and its `close()` already wakes a blocked `recv()` so the engine tears the call down. Duplicating it would be redundant machinery (rule 28) and risk two consent loops fighting. We verify + lock the existing behaviour with tests instead. |
| Run/operate a TURN server inside the plugin | A plugin is not a service (rule 40); running TURN is external infrastructure (rule 41). The plugin *consumes* operator-provided TURN credentials only. |
| Ship "trickle ICE" as a full in-dialog incremental channel now | The transport is in-dialog SIP INFO (RFC 8840), which needs an INFO handler on the inbound/in-dialog adapter surface owned by other lanes (#32/#39). Building it here would either be a stub (rule 6) or contend on hot shared files (rule 32). We ship the SDP/ICE primitives + half-trickle and name the SIP-INFO transport as a real follow-up. |
| Pass *all* TURN URLs to aioice | `aioice.Connection` accepts a single TURN server. We parse the first `turn:`/`turns:` URL (same single-server shape as STUN today); multiple TURN servers are a non-MVP extension. |
| Detect consent loss via the media-inactivity timeout alone | The inactivity timeout only fires when media *was* flowing and stopped; it does not protect a held/quiet call whose NAT mapping expired. Consent freshness is the principled path-liveness check independent of media flow — surfacing it is the point of gap-analysis #6. |
| Accept a credential-less `turn:` URL (gather nothing) | Silent no-op (no relay candidate) violates rule 27; RFC 8656 §9.2 requires long-term credentials. We raise `ConfigError` at load. |
| Accept TURN URI userinfo (`turn:user:pass@host`) | A `turn:` URI carries no userinfo (RFC 7065 §3.1); parsing it would fold credentials into the host token and risk leaking them into a DNS/connect error. We reject `@` and take credentials only from the env vars. |
| Withhold end-of-candidates for a trickling peer (conditional on the SDP markers) | With no in-dialog SIP-INFO transport to *receive* trickled candidates, leaving aioice's check loop open would hang ICE waiting for candidates that can never arrive (cross-vendor review BLOCKING finding). We always end candidates and defer the conditional until the SIP-INFO transport exists. |
