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
  receive side, the ICE driver now signals end-of-candidates to `aioice` **based on
  the parsed marker**: a peer offer carrying `a=end-of-candidates` (or no trickle
  option at all — a classic non-trickle peer) ⇒ end-of-candidates is signalled
  immediately after the offered candidates (today's behaviour); a **trickling** peer
  (offer has `ice-options:trickle` and **no** `end-of-candidates`) ⇒ we do **not**
  prematurely signal end, leaving `aioice`'s check loop open for the candidates the
  peer would trickle. `WebRtcMediaSession.run_handshake` gains a
  `peer_end_of_candidates: bool = True` parameter carrying this decision (default
  `True` preserves the non-trickle path verbatim).
- **The residual boundary, named (rule 6).** The *transport* that would deliver a
  trickling peer's later candidates — **in-dialog SIP INFO with
  `application/trickle-ice-sdpfrag` (RFC 8840)** — is **deferred**. The plugin has
  no INFO handler, and the in-dialog/INVITE adapter surface that would host one
  belongs to the SIP-signalling lanes (outbound-WSS #32, rich-payload #39), not this
  media lane. So a trickling peer that *withholds* candidates until after the answer
  is not yet fully served; in practice WebRTC SIP gateways send a complete candidate
  set in the initial SDP (the same reason ADR-0016 chose a non-trickle MVP), so this
  boundary does not regress any working call. This is a real, named follow-up
  (a SIP-INFO trickle task), not a stub — mirroring how ADR-0032 deferred outbound
  WebRTC to the signalling plane.

### 3. ICE consent freshness (surface aioice's built-in; do not duplicate)

`aioice` runs RFC 7675 consent internally and `close()`s the connection on consent
loss. The **defect** this lane fixes: `aioice`'s consent-driven `close()` does *not*
enqueue the queue sentinel that a blocked `recv()` waits on, so a media reader
already parked in `recv()` is **not woken** — the call can wedge instead of tearing
down (the precise gap-analysis #6 failure). The fix is at our layer, not aioice's:

- **`IceConnection` runs one "closed monitor" task** (started in `connect()`, after
  `aioice` has armed its consent task; cancelled in `close()`). It awaits
  `aioice`'s `get_event()` until it observes `ConnectionClosed` (or `get_event()`
  returns `None` because the connection is already closed) and then sets an internal
  `asyncio.Event`. We own the *only* `get_event()` consumer, so aioice's
  "one waiter at a time" constraint is satisfied.
- **`IceConnection.recv()` races the underlying `recv()` against that closed-event.**
  When consent is lost (or the connection otherwise closes), `recv()` raises
  `ConnectionError("ICE connection closed — consent lost or peer gone")` instead of
  hanging. The engine's existing `_ice_recv_loop` **already** converts a `recv()`
  exception into a transport-loss teardown (`_media_timed_out=True; _stop_event.set()`).
  Net: **consent loss → deterministic call teardown, independent of whether media is
  flowing** (so a held/quiet call is still protected, which the media-inactivity
  timeout alone does not guarantee).
- **No timing/threshold of our own.** We keep `aioice`'s `CONSENT_INTERVAL`/
  `CONSENT_FAILURES` (the RFC-grounded defaults) — we surface the outcome, we do not
  second-guess the mechanism. `selected_pair` introspection and `local_candidates`
  are unchanged.

## Consequences

- **Easier:** a deployment behind symmetric NAT can now be given a TURN server with
  one config block (`HERMES_VOIP_ICE_TURN_URLS/_USERNAME/_PASSWORD`) and get relay
  candidates with no code change. Long-lived NAT'd WebRTC calls now drop
  deterministically on consent loss rather than wedging. The plugin advertises
  trickle/ice2 and correctly reads a trickling peer's SDP markers.
- **Harder / committed to maintain:** the `_RawConnectionCtor` Protocol now mirrors
  five more `aioice.Connection` keyword params (a maintenance point if aioice's TURN
  signature changes — pinned at `0.10.2`). The closed-monitor adds one small
  background task per WebRTC call (bounded: it awaits a single event then exits;
  cancelled on close). `MediaConfig` now carries a secret (`ice_turn_password`),
  handled with the same `repr=False` discipline as the SDES keys.
- **Efficiency (rule 22):** negligible. TURN gathering is one extra allocation
  round-trip at setup only (when configured). The consent task is aioice's, already
  present; the closed-monitor is one idle `await get_event()` per call (no polling,
  no per-packet cost). The steady-state SRTP/jitter/decode path is byte-for-byte
  unchanged. Trickle primitives are sans-IO SDP string handling.
- **Security:** the TURN password is a long-term credential; it is suppressed from
  `repr`, never logged, and `_parse_turn_url` errors do not echo userinfo. Relay
  media still rides DTLS-SRTP end-to-end (the TURN server relays ciphertext; it is
  not in the media-trust path). No new network listener; the plugin only *connects*
  to the operator's STUN/TURN.
- **Lock-in / cost:** none new. `aioice` already bundles the TURN client (BSD-3); no
  new dependency, no `uv.lock` change. A TURN server, if used, is OSS (`coturn`) or
  operator-contracted infrastructure pointed at by config — gated by rules 40/41 in
  its runbook, never click-created.
- **What is proven vs validated live:** TURN URL parsing, the aioice TURN-param
  wiring, the trickle SDP round-trip + half-trickle answer, the end-of-candidates
  decision, and the consent-loss→teardown surfacing are **unit-proven** (TDD
  red→green, deterministic in-memory fakes). A **full live relay against a real TURN
  server** and **consent expiry against a real NAT** remain **operator validation
  steps** (runbook 0009), not asserted here (rule 23/26).

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Reimplement RFC 7675 consent freshness in our layer | `aioice` already implements it (verified: `query_consent`, 5 s/6-failure → `close()`). Duplicating it would be redundant machinery (rule 28) and risk two consent loops fighting. We surface its outcome instead. |
| Run/operate a TURN server inside the plugin | A plugin is not a service (rule 40); running TURN is external infrastructure (rule 41). The plugin *consumes* operator-provided TURN credentials only. |
| Ship "trickle ICE" as a full in-dialog incremental channel now | The transport is in-dialog SIP INFO (RFC 8840), which needs an INFO handler on the inbound/in-dialog adapter surface owned by other lanes (#32/#39). Building it here would either be a stub (rule 6) or contend on hot shared files (rule 32). We ship the SDP/ICE primitives + half-trickle and name the SIP-INFO transport as a real follow-up. |
| Pass *all* TURN URLs to aioice | `aioice.Connection` accepts a single TURN server. We parse the first `turn:`/`turns:` URL (same single-server shape as STUN today); multiple TURN servers are a non-MVP extension. |
| Detect consent loss via the media-inactivity timeout alone | The inactivity timeout only fires when media *was* flowing and stopped; it does not protect a held/quiet call whose NAT mapping expired. Consent freshness is the principled path-liveness check independent of media flow — surfacing it is the point of gap-analysis #6. |
| Accept a credential-less `turn:` URL (gather nothing) | Silent no-op (no relay candidate) violates rule 27; RFC 8656 §9.2 requires long-term credentials. We raise `ConfigError` at load. |
| Use `set_selected_pair` / poll `_nominated` to detect closure | Fragile (reaches into more aioice internals) and still would not wake a blocked `recv()`. Awaiting the public `ConnectionClosed` event is the supported, single-consumer-safe surface. |
