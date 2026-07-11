# Changelog

All notable changes to `hermes-voip` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The package version is **single-sourced** from `pyproject.toml [project].version`
(see [docs/runbooks/0019-release-process.md](docs/runbooks/0019-release-process.md));
`hermes_voip.__version__` and the `plugin.yaml` manifest version track it and are
pinned equal by the test suite.

## [Unreleased]

### Added

- **Cumulative packet-loss counters on the call-quality logs** — the `rtcp_call_quality`
  teardown event and the `media_anomaly` (`one_way_audio` / `media_degraded`) events now
  carry `local_cumulative_lost` / `remote_cumulative_lost`, so absolute per-call
  lost-packet totals are queryable as structured fields instead of only the loss
  fractions. (#446)
- **`inbound_secured_handshake_failed` observability event** — an inbound WebRTC
  (ICE/DTLS) or SIP secured-media call whose handshake fails *after* the `200 OK` now
  emits a structured event (`call_id`, transport, and a `fingerprint` / `ice` / `dtls` /
  `failed` category) instead of being silently counted as answered, correcting the
  call-setup-success SLO overcount; the exception is never stringified into the log. (#455)
- **Unrecognised `HERMES_SIP_*` / `HERMES_VOIP_*` env vars now log a warning** — a typo'd
  knob (e.g. `HERMES_VOIP_STT_PROIVDER`) was previously copied into config but read by no
  consumer, so the default was used silently with no signal; each prefixed env var is now
  cross-checked against the manifest's known-key registry (plus the indexed
  `HERMES_SIP_(EXTENSION|PASSWORD|USERNAME)_<n>` pattern) and anything unrecognised logs a
  `WARNING` naming the key only, never its value. Warn-only: the key still flows through
  unchanged. (#463)
- **Call transfers now report their real terminal outcome to the agent** — a blind or
  attended transfer previously returned "initiated" on the REFER `202 Accepted` (which
  only acknowledges receipt) and never consumed the terminal `message/sipfrag` NOTIFY, so
  the agent could not tell whether the callee answered, was busy, or was unreachable;
  `transfer_blind` and `transfer_attended` now bounded-wait for the terminal NOTIFY and
  surface the real outcome (COMPLETED / FAILED-with-SIP-status / OUTCOME_UNKNOWN) with an
  accurate per-cause message, correlated to the active transfer by REFER CSeq. New
  `HERMES_VOIP_TRANSFER_OUTCOME_TIMEOUT_S` knob (default 20 s; `0` opts out); see
  ADR-0109. (#469)
- **Outbound WebRTC/WSS call-lifecycle SLO events** — the WebRTC (WSS) outbound-call leg
  now emits the same structured `outbound_invite_sent` / `outbound_call_connected` /
  `outbound_call_failed` log events (tagged `transport="webrtc"`) that the SIP/TLS leg
  already did, so outbound call-setup SLO queries span both transports instead of going
  dark on the WebRTC path. (#471)
- **`first_audio_latency` SLO event** — each inbound call now emits a structured
  `first_audio_latency_ms`, the wall-clock time from INVITE receipt to the greeting's
  first outbound RTP frame, making the "time to first audio < 2 s" SLO countable from logs
  instead of eyeballed. (#474)
- **"Didn't catch that" reprompt on an untranscribable turn** — a caller who speaks but
  whose audio ASR returns as an empty transcript now hears a distinct reprompt asking them
  to repeat, instead of falling through to the "Are you still there?" silence prompt; the
  reprompt is scheduled off the ASR pump so it never stalls inbound audio, and its phrase
  set is configurable via `HERMES_VOIP_DIDNT_CATCH_PHRASES` (pipe-separated;
  explicit-empty opts out). (#476)
- **Per-call maximum-duration watchdog caps runaway active calls** — a new
  `HERMES_VOIP_MAX_CALL_DURATION_SECS` knob (default `14400`, 4h; `0` disables) arms a
  per-call watchdog that gracefully hangs up (in-dialog `BYE` + media stop) any call still
  active past the cap, so a caller streaming continuous RTP can no longer hold an
  admission slot and run the STT/LLM/TTS pipeline indefinitely — closing the
  slot-exhaustion path that `media_timeout` (silent RTP) and the RFC 4028 session timer
  (dead dialog) both miss; the end is tagged with a distinct `MAX_CALL_DURATION` reason.
  (#497)

### Changed

- **`HERMES_VOIP_DENY_MODE=decline` now speaks a language-keyed phrase** — the spoken
  decline line is selected from a built-in per-language set (en/fr/de/es/pt) keyed on
  `HERMES_VOIP_LANGUAGE` with English fallback, instead of always English; an explicit
  `HERMES_VOIP_DECLINE_PHRASE` override still wins. (#447)

### Fixed

- **Agent-initiated hang-up no longer truncates the spoken farewell** — before sending
  `BYE` and stopping media, the adapter now waits (bounded) for an in-flight reply to
  reach the wire and flushes the engine's buffered outbound tail, so a same-turn goodbye
  is delivered in full instead of being clipped or dropped on a gateway that stops media
  on `BYE`. Bounded by the new `HERMES_VOIP_HANGUP_DRAIN_SECS` /
  `HERMES_VOIP_HANGUP_GRACE_SECS` knobs. (#432)
- **Outbound `422 Session Interval Too Small` after an auth challenge now retries** — on a
  proxy-auth gateway the `INVITE → 407 → authenticated INVITE → 422` flow was mis-reported
  to the agent as a failed call; the raise-`Session-Expires`-and-retry remedy now runs
  whichever order the challenge and the `422` arrive in (each capped at one retry), and a
  stale-final flood is bounded so the outbound call can no longer hang. (#433)
- **A re-INVITE that relocates the peer's RTP endpoint no longer causes one-way audio** —
  a re-INVITE moving the peer to a new `c=` / `m=audio` endpoint (attended-transfer
  re-anchor, hold-then-resume-elsewhere, or SBC media relocation) now re-points the
  outbound media to the new address and ignores a stale in-flight packet from the old one,
  so agent-to-caller audio follows the relocation instead of continuing to the now-dead
  address. (#434)
- **Graceful shutdown de-registers each live binding** — `aclose()` now cancels the
  per-flow refresh tasks and *then* sends an `Expires: 0` REGISTER for every binding before
  teardown (bounded, best-effort), so a restart no longer leaves the gateway routing
  inbound calls to the just-closed contact — a silent inbound black-hole — until the
  binding expires. (#435)
- **A malformed `Min-SE` in an outbound `422` fails closed** — a `422 Session Interval Too
  Small` whose `Min-SE` is not a valid delta-seconds value now surfaces as a typed
  `place_call` failure (exactly like a `422` carrying no `Min-SE`) instead of raising an
  unhandled error out of the outbound-call coroutine. (#436)
- **Inbound INVITE with an unsupported `Require` is rejected `420 Bad Extension`** — an
  INVITE demanding an option-tag the plugin cannot honour (e.g. `100rel`, `precondition`)
  is now rejected per RFC 3261 §8.2.2.3 with an `Unsupported` header listing the tags,
  before any dialog / media / agent surface, rather than being silently answered `200`;
  the `420` takes precedence over the at-capacity `486`, and `Require: timer` is still
  honoured. (#437)
- **Guard `RESTRICT` / `CLARIFY` verdicts now clamp the toolset for that turn** — the
  tool-policy gate previously ignored a guard verdict below the `REFUSE` threshold, so an
  injected caller turn screened as `RESTRICT` or `CLARIFY` still reached the agent with its
  tools unchanged; a per-turn read-only clamp now blocks every non-SAFE tool on such a turn
  (SAFE call-control such as `hang_up` still runs), and is reset each turn including on the
  unscreened DTMF path. (#438)
- **Bracketed text in a quoted display-name no longer desyncs SIP identity parsing** — a
  name-addr whose quoted display-name legitimately contains `<…>` or `;tag=` (RFC 3261
  §25.1) is now parsed with a quote-aware angle-addr locator in the dialog and REFER
  layers, so the real addr-spec and dialog tag are extracted instead of the bracketed
  display text — a valid inbound INVITE/2xx is no longer rejected and blind/attended
  transfer targets are no longer corrupted. (#439)
- **Outbound calls no longer play the inbound greeting** — an agent-placed call previously
  spoke the canned on-answer greeting to the party it called the instant they answered; per
  ADR-0019 the agent's first turn now opens an outbound conversation, while the inbound
  on-answer greeting path is unchanged. (#443)
- **A caller display-name can no longer forge a line in the agent's context block** — a
  bare `LF` / `CR` embedded in a `From` display-name (which SIP header parsing does not
  treat as a line boundary) previously survived verbatim into the untrusted caller-context
  block; identity defanging now collapses every interior whitespace run to a single space,
  keeping each rendered field on one line. (#445)
- **Quote-aware angle-addr parsing extended to the registration and routing sites** — the
  quote-safe locator from #439 now also backs To-user routing, Contact-binding matching,
  and Record-Route / in-dialog classification, so a quoted display-name containing `<…>` or
  `;tag=` can no longer corrupt the extracted URI or hide the real tag on those paths.
  (#448)
- **All SIP identity-header parsing consolidated onto one quote-safe primitive** — the
  remaining naive `;`-parameter / `;tag=` / angle-addr scans (including the WSS in-dialog
  classifier and the caller-context display/URI extraction) now share the quote-aware
  parser, closing residual gaps where a forged `;tag=` inside a quoted parameter value, or a
  quoted display-name, could inject or suppress a dialog tag; an empty `;tag=` is treated as
  absent and backslash escapes are honoured. (#450)
- **`;tag=` values are validated against the strict RFC 3261 token grammar** — the shared
  tag parser now treats a quoted, empty, or otherwise non-`token` tag value as absent
  (fail-closed) and strips only RFC 3261 LWS (SP/HTAB) around the value, so a malformed tag
  can no longer be accepted as a dialog identifier. (#456)
- **Held calls are no longer torn down by the no-input watchdog** — when the agent
  (`hold_call`) or the peer/PBX (a hold re-INVITE) puts a call on hold the media plane is
  suspended both ways, but the watchdog kept counting silent windows, reprompting into
  dead media and hanging up the live caller after ~30 s; it is now hold-aware, skipping a
  held silence window (and resetting the reprompt budget), and the graceful-end path
  re-checks hold immediately before the irreversible teardown so a call held during the
  goodbye is not dropped. (#460)
- **A caller who repeatedly trips the injection guard is no longer declined forever** —
  because a guard `REFUSE` marked the caller active each turn it kept resetting the
  no-input watchdog, so a legitimate caller whose accent/phrasing kept tripping the guard
  heard the safe-decline line indefinitely, never reaching the agent and never closing;
  consecutive REFUSEs are now counted and the call is ended gracefully once they reach
  `HERMES_VOIP_MAX_CONSECUTIVE_REFUSE` (default 3 → two declines then a graceful close;
  `0` disables the bound), with any delivered turn resetting the count. (#465)
- **Deny-mode decline phrase is sanitised before it is spoken** — a custom
  `HERMES_VOIP_DECLINE_PHRASE` containing markdown, a bare URL, or emoji is now run
  through the same speech sanitiser as agent replies before being voiced to a denied
  caller, instead of verbatim; if sanitisation empties the phrase the raw value is still
  spoken, so the one-line decline is never dead air. (#480)
- **Acoustic echo canceller no longer stalls the inbound media path** — the on-by-default
  AEC previously ran a per-sample time-domain NLMS costing ~35 ms/frame at 8 kHz and ~91
  ms/frame at 16 kHz (2–4× the 20 ms RTP packet period), stalling the inbound leg whenever
  the agent's TTS echo was returning; it is re-implemented as a numpy-vectorised
  block-NLMS running ~1.3–4.2 ms/frame (≥5× under budget) with unchanged cancellation
  quality and zero added latency. `numpy` is now a base dependency. (#481)
- **A bad character in a DTMF string no longer emits a partial burst** — the RFC 4733
  `send_dtmf` path streamed digits to the media engine and raised on the first non-DTMF
  character only *after* a valid prefix had already reached the wire, so an agent sending
  e.g. a space-grouped code (`4111 1111 1111 1111`) corrupted the far-end IVR entry while
  the tool still reported "nothing sent"; the whole burst is now validated before any
  transmission, so a bad digit raises before anything is sent. (#491)
- **Call-end reasons are no longer silently collapsed in logs and caller outcome text** —
  six of the eight `CallEndReason` values shared identical enum data and so became silent
  aliases, making the outcome phrase relayed to the originating session (ADR-0029) and the
  `reason.name` in logs correct for only two reasons (every failure reported as
  `MEDIA_TIMEOUT`, agent hang-up / end-of-stream as `REMOTE_BYE`); each reason now carries
  a distinct value, so the spoken outcome and the structured logs label the real cause.
  (#496)

### Security

- **The inbound-call context block injected into the agent is now length-capped** — most
  of the block's fields are caller-controlled (From display name, Diversion / History-Info
  chains, User-Agent, Subject, Organization), so an oversized INVITE could balloon it (a
  flooded INVITE rendered 15488 chars) to inflate the agent's context window and dilute
  the trusted framing header behind attacker-controlled text; the rendered block is now
  hard-capped at 600 chars with tail truncation — mirroring the existing outbound-summary
  cap — trimming only the caller-controlled tail and never the untrusted-data / spoofable
  warning. (#466)
- **Strict ASCII / anchored parsing of SIP numeric headers** — `Session-Expires`,
  `Min-SE`, and NOTIFY sipfrag status codes now reject non-ASCII digits (e.g.
  Arabic-Indic, fullwidth) instead of silently folding them through `int()`, and reject
  trailing garbage after the value, so attacker-influenced headers are parsed as strictly
  as the message parser already was. (#479)
- **Transfer-target URIs reject fullwidth / non-ASCII digits** — the REFER transfer-target
  guard parsed IPv4 octets and ports with a Unicode-aware regex, so a prompt-injected
  `transfer_blind` / `transfer_attended` target could fold fullwidth / Arabic-Indic
  "digits" into a seemingly-valid host or port and smuggle non-ASCII characters past the
  injection allowlist onto the UTF-8 `Refer-To` wire; both regexes are now strict ASCII
  (`[0-9]`), as RFC 3261 requires. (#485)
- **`;maddr` dropped from the transfer-target parameter allowlist** — a prompt-injected
  `transfer_blind` / `transfer_attended` target such as
  `sip:1000@trusted;maddr=<attacker>` passed the URI guard with an innocuous host yet, per
  RFC 3261 §19.1.1, `maddr` overrides the routed destination and covertly re-aimed the
  triggered INVITE at an attacker (host-hijack); `maddr` is no longer allowlisted, while
  `transport` is retained for legitimate TLS transfers. Decision recorded in ADR-0112.
  (#486)
- **Strict-ASCII CSeq parsing in the dialog layer** — the `CSeq` parser previously used a
  Unicode-aware regex whose fullwidth / Arabic-Indic digits `int()` would fold, a
  parser-differential against an RFC 3261-strict proxy; it now requires ASCII digits.
  Defense-in-depth hardening in the same strict-ASCII series as the SIP numeric-header
  fixes (the value parsed here is currently self-built outbound, with the
  attacker-reachable peer-response CSeq siblings tracked separately). (#488)
- **One binary WebSocket frame can no longer tear down a WSS signaling connection** — the
  WSS reader force-UTF-8-decoded inbound frames, so a single BINARY frame carrying
  non-UTF-8 bytes raised inside `recv()` and escaped the read-loop guards, killing SIP
  registration and every active call on that connection; the reader now takes frames
  undecoded (TEXT dispatched, BINARY dropped) so no binary frame can crash it. (#489)
- **A Unicode-digit SIP header param can no longer DoS inbound call setup** — `Diversion`
  / `History-Info` parsing used `str.isdigit()` + `int()`, so a crafted `;index=` /
  `;cause=` / `;counter=` value carrying a Unicode digit such as `²` (accepted by
  `isdigit()`, rejected by `int()`) raised after the INVITE was answered and skipped
  teardown, permanently leaking the admission slot, RTP socket and in-dialog routes;
  repeating it `max_calls` times drove every further inbound INVITE to `486 Busy Here`.
  The three sites now parse through the fail-closed decimal helper. (#490)
- **Linear RFC 3261 header unfolding closes a quadratic-parse event-loop DoS** — folded
  (obs-fold continuation) headers were reassembled by rebuilding the growing value on
  every fold, making one crafted header O(N²); because parsing runs synchronously on the
  shared asyncio loop, a single message folded into many tiny continuation lines stalled
  every concurrent call's media and timers (measured ~3.3 s for 300k folds, worst on the
  un-capped WSS path). Header fragments are now joined once, yielding the identical value
  in linear time. (#492)
- **RTCP Sender-Report SSRC flood can no longer exhaust media-process memory** — on a
  plain-RTP call (no SRTCP, a common SIP-trunk config) the unauthenticated RTCP ingest
  path stored a permanent entry for every distinct sender SSRC, so a sustained low-rate
  stream of forged Sender Reports grew an unbounded map until the shared media process ran
  out of memory, taking down every concurrent call; the map is now capped in lockstep with
  the already-bounded reception table (≤ 31 tracked sources). (#494)

## [0.3.1] - 2026-07-03

A maintenance / hardening patch release bundling five fixes for operator testing: SIP/SDP
parser hardening, an ADR-0081 fail-closed completion on the transport, proactive and
live-call VoIP-tool gate fixes, and a developer-only hermes-contract CI regression fix. No
breaking changes.

### Fixed

- **ICE candidate `rport` is range-validated** — an out-of-range `rport` value on an ICE
  candidate line is now rejected with the same bounds check already applied to the
  candidate port itself, closing an SDP parser-hardening gap. (#426)
- **Over-long `Content-Length` fails closed as a `FramingError`** — a declared body length
  beyond the transport's accepted maximum now raises the typed `FramingError` (the single
  malformed message is dropped and the connection survives) instead of a bare `ValueError`
  that could unwind the signalling reader, completing the ADR-0081 fail-closed contract for
  this path. (#427)
- **Proactive `place_call` gate reachability (ADR-0105)** — the proactive-origin relaxation
  is now actually reached when the guard session state is absent (the branch was previously
  unreachable), and a code-enforced deny guarantees a VoIP-owned platform origin can never
  be treated as a proactive operator origin — a defence-in-depth boundary raised by
  cross-vendor review. (#428)
- **hermes-contract CI regression fixed (developer-only)** — the media-engine teardown
  tests now model faithful RTCP state and a `voip_tools` mypy reachability annotation was
  widened; both are test / type-check-only changes with no runtime behaviour change. (#429)
- **Scoped caller groups can still use SAFE tools** — the caller-group `allowed_tools`
  sub-ceiling now clamps only ELEVATED / IRREVERSIBLE tools, so scoped groups such as
  intercom and known callers can still `hang_up` and `report_call_result` while sensitive
  tools remain allow-listed. A guardrail test locks the SAFE tool set to those two tools.
  (#431)

## [0.3.0] - 2026-07-03

An observability + operability release. Structured ADR-0075 SLO log events now span the
**outbound-call lifecycle**, the **SIP-over-TLS transport** (loss / retry / recovery),
and **ICE** nomination, so call-setup, uptime, and flap metrics are queryable from logs
without regex. The plugin-enable gate validates the provider wiring up front, and media
destination-resolution failures and proactive-call gate denials are now diagnosable. All
changes are additive — no breaking changes.

### Added

- **Outbound call lifecycle SLO events** — the outbound `place_call` path emits structured
  `outbound_invite_sent` / `outbound_call_connected` / `outbound_call_failed` log events
  (ADR-0075), correlated by Call-ID and carrying only non-sensitive context (transport,
  codec, and the ADR-0086 failure category), so outbound attempt / answer / failure rates
  are queryable. (#419)
- **SIP transport loss / retry / recovery SLO events** — the reconnect supervisor emits
  structured `sip_transport_lost` / `sip_transport_retry` / `sip_transport_recovered`
  events so uptime and flap windows are queryable without regex. (#420)
- **Structured `ice_pair_nominated` log event** — the ICE nominated-pair line now carries
  a structured `event` name, `call_id`, and `candidate_type`. (#412)
- **Diagnosable proactive `place_call` gate denials** — a blocked proactive call now logs a
  non-sensitive deny-reason category (`proactive_allow_unset` / `origin_unavailable` /
  `origin_not_allowlisted` / `live_call_guard_missing` /
  `unsupported_tool_for_proactive_origin`), preserving the fail-closed boundary and never
  logging the origin platform / chat-id / allow-list. (#418)
- **Provider-wiring preflight at the enable gate** — `validate_voip_config` now rejects an
  unwired provider token or a missing / mis-pathed self-host model directory at
  plugin-enable time (naming the offending token / env var, never the SIP password),
  instead of surfacing the failure later inside `connect()`. (#415)

### Fixed

- **RTP media-destination resolution failures are now diagnosable** — a DNS / `gaierror`
  failure of the SDP media destination is categorised `dns_resolution_failed` (vs
  `udp_transport_error`) with operator-safe context (host-kind + port, never the raw
  host), instead of appearing as a generic dead RTP transport. (#417, closes #413)

## [0.2.0] - 2026-07-02

A security-hardening release. The centrepiece is the **complete closure of the
ADR-0081 class** — the guarantee that "one malformed inbound SIP message must never
be a denial of service against unrelated calls". Every reader-reachable path where a
crafted-but-parseable inbound message could raise an uncaught exception and unwind the
signalling reader — tearing down the whole TLS/WSS connection along with every active
call and the registration on it — now **fails closed**: the one bad message is dropped
and the connection survives. A scoped reader dispatch-boundary backstop (ADR-0098) adds
defence-in-depth on top of the per-site fixes. Also included: SDES/SRTP suite-keying
correctness (HIGH), caller-identity injection defence, PII redaction in logs, and a
least-privilege session default.

### Added

- **Wildcard / pattern matching for outbound allow-lists** — `HERMES_VOIP_OUTBOUND_ALLOW`,
  `HERMES_VOIP_PROACTIVE_CALL_FROM`, and `HERMES_VOIP_OUTBOUND_RESULT_CHANNEL` now accept
  shell-style glob patterns, not only exact values. (#355)
- **Provider protocols promoted to top-level exports** — the STT / TTS / media provider
  protocol types are importable from `hermes_voip` directly. (#367)

### Changed

- **`GuardSessionState` defaults to least privilege** — the per-session privilege level
  now defaults to `0` (unprivileged); elevation must be granted explicitly rather than
  assumed. (#389)
- ⚠ **`HERMES_VOIP_DUPLEX_MODE=full` is rejected at config load** — the value was
  previously accepted but silently inert. A deployment that set it must remove it (or use
  a supported mode) before upgrading, or config load fails fast. (#363)
- **G.722 enforces a combined hot-path CPU budget** — the wideband codec's encode/decode
  hot path is held to an explicit per-frame CPU budget (rule 22).

### Security

- **ADR-0081 class fully closed — malformed inbound SIP can no longer DoS a whole
  connection.** Each reader-reachable handler now fails closed instead of unwinding the
  reader task:
  - non-decimal / out-of-range CSeq at every parse site — registration `_check_cseq`
    (#390), transaction key `_txn_key` (#392), and the call `on_response` sink
    `_cseq_number` (#393); a malformed final-INVITE-response CSeq now warns instead of
    silently skipping ACK/cleanup (#358);
  - header-incomplete inbound requests whose inline auto-response build raises — the
    dispatch-level CANCEL / OPTIONS / answered-guard responses (#388) and the in-dialog
    request path (#391);
  - an in-dialog re-INVITE carrying an unparseable SDP offer (#395);
  - a non-UTF-8 SIP body (#375), a zero-SSRC RTCP BYE (#370), and SDES/BYE trailing
    garbage, with an implausible-RTT clamp (#383).
  - A **scoped reader dispatch-boundary fail-safe backstop** (ADR-0098) drops any single
    message whose handler raises and keeps the connection — defence-in-depth over the
    per-site fixes, without a broad read-loop catch-all. (#394)
- **SDES/SRTP suite-keying consistency (HIGH)** — the SRTP crypto suite advertised in the
  SDP answer can no longer diverge from the key material actually installed; unicode-digit
  crypto-tag and non-numeric `rport` `int()` escapes are guarded. (#387)
- **Typed media-layer failures** — SRTP raises `SrtpError` (not a bare `ValueError`) on
  malformed decrypted RTP (#376), and a corrupt Opus payload is concealed as packet loss
  rather than crashing the inbound media generator (#377).
- **Caller identity is defanged before it reaches the agent** — a forgeable `From` /
  display name can no longer inject content into the agent-visible user / chat / id fields
  (prompt-injection seam). (#378)
- **Registration fails closed on an unanswerable challenge**, ignores a stale REGISTER
  final response (HIGH DoS), and never surfaces the registrar's raw reason string
  (safe-by-default rendering). (#374)
- **Tool-surface hardening** — `voip_tools` handlers honour a never-raise contract and cap
  `send_dtmf` length, closing a tool-abuse surface. (#382)
- **PII redaction** — raw inbound DTMF digits are redacted from logs (rule 34). (#384)
- **DTMF digit-substitution resistance** — a same-timestamp telephone-event carrying a
  conflicting event code is rejected (ADR-0096 corroboration). (#372)
- **Message ingress hardening** — ASCII-only status text, numeric range checks, and
  redacted parse-error logging. (#386)
- **Best-effort hang-up BYE** — a send error while ending a call can no longer escape and
  disturb teardown. (#380)

### Fixed

- **RTP field type-guards and coalesced loss count** — inbound `RtpPacket` fields are
  type-guarded, and `peek()` reports a coalesced `Lost(count)` for far-ahead bursts. (#373)

## [0.1.3] - 2026-06-28

### Fixed

- **`VoipAdapter.connect` now accepts the keyword-only `is_reconnect` param the
  Hermes 0.17.0 gateway may pass** (`connect(*, is_reconnect: bool = False)`),
  so a gateway build that forwards the reconnect flag no longer hits
  `TypeError` on every connect (the VoIP platform never coming up). VoIP has no
  server-side message backlog to replay, so the flag is accepted-but-ignored;
  the adapter's own RFC 5626 reconnect supervisor already restores
  registration. `hermes-agent` pin moved `0.16.0` → `0.17.0`. (#350)

## [0.1.2] - 2026-06-28

### Added

- **`HERMES_VOIP_DENY_MODE=decline`** — new deny mode that answers with 200 OK,
  delivers a spoken decline message, then sends BYE; per ADR-0020 Phase 2. (#332)
- **Model-file sha256 verification** — the provider build step verifies pinned
  model-file checksums with a path-traversal guard, so a tampered or corrupted
  model file is rejected at load time rather than at inference. (#326)
- **`WssSipTransport` / `CallResponseSink` in top-level `__all__`** — and all
  top-level `config` / `provider` types, so consumers can import them without
  reaching into private sub-modules. (#324)
- **Outbound ring-timeout config knob** — `HERMES_VOIP_RING_TIMEOUT_SECS` with
  validation and documentation in `voip_tools`. (#308)
- **Preflight VoIP env validation** — plugin validates the full set of required
  environment variables at startup and fails fast with a secret-safe error. (#306)
- **BCP-47 language-tag acceptance** — `HERMES_VOIP_LANGUAGE` now accepts any
  well-formed BCP-47 tag, decoupled from comfort-filler availability. (#257)
- **`HERMES_VOIP_CALL_ON_CONNECT` and `KEEPALIVE_INTERVAL` config** — documented
  and validated in the config layer. (#267)
- **Structured extra fields on observability logs** — `call-progress`, TTS
  failover, and SIP registration log events carry typed `extra={}` dicts for
  structured log consumers. (#276, #275, #274)
- **`ProviderRegistry` introspection** — `__contains__` and `__all__` + fresh-
  instance identity pin so callers can test registry membership and iterate
  registered providers. (#296)
- **`__all__` exports** across DTLS/SRTP/SRTCP crypto modules, audio codec
  module, and the foundation RTP/RTCP/SDP/SIP/registration modules. (#285, #279,
  #268, #281, #271)
- **`InboundCallContext` and helpers promoted** to top-level `hermes_voip`
  exports. (#278)
- **CI: pinned third-party workflow action SHAs** with an enforcement test. (#246)
- **JitterBuffer SSRC auto-reset hysteresis** — N-consecutive-confirmation before
  accepting an SSRC change (ADR-0082). (#248)
- **`HERMES_VOIP_CARTESIA_API_KEY` credential** — Cartesia API-key declared in the
  plugin manifest so the Hermes config wizard surfaces it. (#342)

### Changed

- **`GuardVerdict` / `ToolRisk` are `IntEnum`** — enables documented severity
  comparison semantics and documented `__all__`. (#294)
- **Control-character guard extracted** to shared `_chars.py` used by the
  message, digest, and refer layers. (#277, #286)
- **`py.typed` marker and `Typing::Typed` classifier** shipped in the built
  wheel so downstream type-checkers pick up inline type information. (#307)
- **Dependency extras relaxed** to compatible ranges for Hermes/plugin
  coexistence without pinned upper bounds causing install conflicts. (#254)
- **REGISTER expires validation tightened** — non-positive or malformed granted
  expires is a hard failure, not a 0-second refresh loop (ADR-0087). (#305)
- **`place_call` outbound-failure outcomes structured** — failures now carry
  typed outcome codes and bounded ring timeout. (#259, #308)
- **`provider_error` apology configurable** — localized error apology text and
  structured provider-error logs. (#263)
- **`GateDecision` reasons typed** — `tool-policy` gate returns typed
  `GateDecision` reasons rather than bare strings. (#261)
- **`JitterBuffer` packet-loss coalesced** — far-ahead packet bursts emit one
  `Lost(count)` event instead of per-packet events. (#260)
- **Media engine per-datagram task overhead removed** — asyncio task churn per
  datagram dropped; TX path uses a `bytearray` buffer. (#310)
- **STT sample-count via `FloatArray.__len__`** — drops the per-frame
  `.tobytes()` copy. (#295)
- **Engine TX amplitude log via `audioop.max`** — ~51x faster than previous
  path. (#252)
- **Plugin-list version wording reconciled** — `plugin.yaml` version expectations
  aligned with the pinned test suite; a docs-drift guard prevents future divergence.
  (#341)

### Fixed

- **IPv6 black-hole hold detection** — `c=IN IP6 ::` in a re-INVITE SDP is
  now correctly classified as a held call. (#328)
- **Duplicate `Content-Length` header rejected** — fail-closed; the first value
  is not silently used. (#329)
- **`NOTIFY` dispatched by `Event` package** — in-dialog NOTIFY is routed by
  the `Event:` header value; only `Event: refer` carries sipfrag body. (#323)
- **SDP duplicate `rtpmap` rejected** — per payload type, fail-closed, for both
  audio and video. (#320)
- **TLS 1.2 floor on WSS/TLS client context** (ADR-0089). (#318)
- **Registration `isascii()+isdecimal()` guards** — a Unicode-digit `expires`
  field can no longer crash REGISTER handling. (#316)
- **Contact-binding canonicalisation** — pragmatic canonicalisation for
  registrar echoes (ADR-0090). (#331)
- **RTCP SDES CNAME UTF-8 end-to-end** — a valid non-ASCII CNAME no longer
  aborts compound RTCP parse. (#321)
- **SDP `telephone-event` clock-rate validated** per RFC 4733. (#311)
- **API `__all__` trimmed** — leaked `tts`/`stt` seams removed; still importable
  for back-compat. (#313)
- **SDP `addrtype` derived from address family** — IPv6 addresses now produce
  `IN IP6` SDP lines. (#291)
- **CSeq overflow guard** — `build_in_dialog_request` rejects CSeq >= 2**31
  per RFC 3261. (#250)
- **Registration `accept 2xx`** — any 2xx (not just 200) is treated as success;
  CSeq method validated. (#288)
- **Digest `cnonce` / `nc` validation** — empty cnonce rejected; `nc` validated
  within 32-bit range; unquoted `algorithm`/`qop`/`nc` render pinned. (#287)
- **Digest `realm` / `nonce` validation** — raise on missing or empty realm,
  symmetric with nonce. (#293)
- **Inbound CANCEL on `WssSipTransport`** — end-to-end handling per RFC 3261
  §9.2; TLS CANCEL-200 reuses the stable `To`-tag. (#300, #302)
- **Malformed REFER/NOTIFY answered 4xx** instead of dropping the SIP
  connection. (#301)
- **Guard `config.json` corruption** — corrupt or invalid `config.json` is
  wrapped cleanly and does not leak path information. (#289)
- **SRTP ROC increment masked** to 32-bit modulus per RFC 3711. (#304)
- **DTLS fingerprint hash algorithm validated** at SDP answer time (fail-
  closed). (#303)
- **DTMF counts as no-input watchdog activity** — inbound DTMF digits reset
  the caller-silence reprompt timer. (#327)
- **VAD silero v5 dynamic-batch state shape** accepted; pinned v5 model
  download. (#255)
- **`Refer-To` target injection guard** — inbound `Refer-To` URI validated in
  `parse_refer`. (#266)
- **Empty/whitespace-only ASR finals dropped** — no phantom agent turns from
  silent utterances. (#269)
- **Runbook-0013 doc drift fixed** — false WSS-unwired claim corrected;
  `sherpa_kokoro` → `sherpa-kokoro` package token updated, with drift tests.
  (#325)
- **`audioop.error` wrapped** in the media-layer exception contract. (#262)
- **Registration negative `expires` rejected** — Literal transport type
  enforced. (#282)
- **`call_loop` empty ASR final guard** — drops whitespace-only transcript
  finals before routing to the agent. (#269)
- **Malformed (non-UTF-8) RTCP BYE reason strings rejected** — non-decodable
  BYE reason payloads are now refused rather than silently discarded or
  mis-decoded. (#338)

### Security

- **Removed operator-specific gateway identifiers from tracked files** — a CI
  guard scanning the whole tracked tree now prevents reintroduction. (#343)
- **Supply-chain audit gates declared optional extras** — a banned-SPDX optional
  dependency now fails CI; closes the licence-gate gap for optional dependency
  groups (ADR-0091). (#339)

## [0.1.1] - 2026-06-27

### Added

- **Automated publish on tag** — pushing a `vX.Y.Z` tag (including pre-releases
  like `v1.2.3-rc1`) runs `.github/workflows/publish.yml`: a `build` job guards the
  tag against `pyproject.toml [project].version`, builds + wheel-smokes the wheel +
  sdist, and uploads them; a `github-release` job attaches them (plus `SHA256SUMS`)
  to a GitHub Release; and an independent `pypi-publish` job publishes them to PyPI
  via OIDC Trusted Publishing with PEP 740 attestations and no stored token. See
  [docs/runbooks/0019-release-process.md](docs/runbooks/0019-release-process.md).
  (#235)
- **JitterBuffer accessors and SSRC-aware auto-reset** — `__len__`, `peek`, `flush`,
  and `reset` methods on `JitterBuffer`; automatic per-SSRC state reset on SSRC
  change so a re-invited call gets a clean buffer. (#221)
- **Plugin manifest admission knobs** — `HERMES_SIP_MAX_CALLS` and
  `HERMES_SIP_SHUTDOWN_DRAIN_SECS` declared in `plugin.yaml` `optional_env` so the
  config wizard surfaces them. (#222)
- **Manifest platform label and per-env prompt fields** — `label` and `prompt` fields
  on all `requires_env` / `optional_env` entries, aligned with the `hermes config`
  setup-wizard platform injector (ADR-0037). (#228)
- **`resample_frame` preserves `monotonic_ts_ns`** — `resample_frame(PcmFrame)`
  returns a `PcmFrame` with the original monotonic timestamp intact rather than
  silently zeroing it. (#233)
- **DTMF enum-tagged `feed()` result** — `DtmfDetector.feed()` now returns a typed
  `DtmfPress | DtmfNoPress` discriminated union instead of an untagged optional,
  collapsing the `_order` / `_seen` internal state into a single cleaner structure.
  (#232)
- **Provider input validation** — `PcmFrame`, `Transcript`, and `GuardResult`
  constructors validate their fields and reject malformed input; odd-length PCM
  chunks in the TTS send path are realigned before codec processing. (#225)
- **Scheduled supply-chain audit** — `.github/workflows/supply-chain.yml` gains a
  daily cron trigger and `workflow_dispatch` so the advisory audit runs automatically.
  (#224)

### Changed

- ⚠ **Registration enforces `sips:` AOR on TLS/WSS transports** — a `sip:` AOR
  supplied with a TLS or WSS transport now **raises `ConfigurationError` at config
  load** (it previously silently accepted a potentially downgrade-vulnerable AOR).
  The default AOR scheme is `sips:`. Deployers using an explicit `sip:` AOR on a
  TLS/WSS transport must update their configuration to use `sips:` before upgrading.
  Digest `nc`/`stale`/`qop` contract constraints are also pinned and tested.
  (#230)
- **SDP answerer-preference codec ordering** — the SDP answerer now preserves the
  peer's codec order on received 2xx answers, while still applying local preference
  when generating offers. (#234)

### Fixed

- **Non-audio re-INVITE returns 488** — a re-INVITE carrying a non-audio SDP (e.g.
  video-only) is now correctly answered 488 Not Acceptable Here instead of being
  misclassified as an offerless re-INVITE. (#229)
- **Transport skips malformed SIP message** — a message that cannot be parsed (bad
  framing, missing mandatory headers, etc.) is now skipped and logged; the transport
  connection and all active calls continue rather than the connection being dropped.
  (#231)
- **Intercom control-character rejection** — control characters in intercom
  configuration are rejected at config load; wrapped relay/webhook errors are
  sanitised before surfacing. (#226)
- **Docs reconciled** — stale `IMPLEMENTATION-PLAN` / `MULTIREG` plan documents
  updated; outbound CANCEL runbook coverage added. (#227)
- **DTMF mutation-hardening vectors** — encode() end-bit and volume edge cases, and
  bounded-window eviction, are now covered by deterministic mutation tests. (#223)
- **`Record-Route` header comma-splitting** — comma-combined `Record-Route` headers
  are now split per RFC 3261 §7.3.1 so multi-hop dialog routing works correctly.
  (#218)
- **Content-Length line-folding** — line-folded `Content-Length` values in the SIP
  stream framer are now unfolded before parsing. (#216)
- **RTP padding rejection** — malformed RTP padding is rejected; jitter-buffer depth
  semantics are pinned by tests. (#215)
- **Digest nc range + control-char validation** — `nc` is validated within the
  32-bit range and per-field control-character rejection is enforced. (#214)

## [0.1.0] - 2026-06-23

First tagged release of the `hermes-voip` Hermes plugin: two-way voice over
telephony for a Hermes agent on any RFC-compliant SIP-over-TLS or WebRTC voice
gateway.

### Added

- **SIP-over-TLS registration** as one or many extensions, with digest
  authentication across SHA-256, MD5, and the `-sess` variants (RFC 7616 / 8760)
  and automatic re-authentication on challenge.
- **Inbound calls** — answer incoming `INVITE`s with full SDP offer/answer
  (RFC 3261 / 3550 / 4566).
- **Outbound calls** — the agent places calls itself via the UAC originate flow
  (the `place_call` tool), with a per-call objective brief and asynchronous,
  cross-session result reporting (`report_call_result`).
- **Media security** — SDES-SRTP on the SIP-over-TLS path and DTLS-SRTP on the
  WebRTC path (RFC 3711); the SDES answer selects the strongest offered crypto
  suite for downgrade resistance.
- **RTP with media-quality resilience** — adaptive jitter buffer, packet-loss
  concealment, and stateful-codec concealment.
- **RTCP** — sender/receiver reports, SDES, and BYE, with reception statistics
  and `rtcp-mux`.
- **Best-available audio codecs, negotiated per call** — G.722 wideband first
  with G.711 fallback on the SIP path, and Opus on the WebRTC path.
- **DTMF** — RFC 4733 telephone-event as primary, SIP INFO as fallback, and
  in-band tone detection as last resort.
- **Two-way conversational bridge** — streaming speech-to-text → the Hermes agent
  → sentence-streamed text-to-speech. Each reply is delivered as one complete
  string and sentence-streamed into audio for fast first-audio; spoken output is
  cleaned up for the phone.
- **Conversational providers** — local, offline-by-default speech-to-text
  (sherpa-onnx) and text-to-speech (sherpa-onnx Kokoro-82M), with optional cloud
  providers (Deepgram speech-to-text, ElevenLabs text-to-speech, including the v3
  expressive tier and model-conditional audio tags) selectable by configuration.
- **Barge-in** — in-process acoustic echo cancellation (NLMS) and voice-activity
  endpointing let a caller interrupt the agent mid-sentence.
- **Offline prompt-injection guard** — an on-device model screens every caller
  utterance before it reaches the agent.
- **Caller recognition** — caller modes (allow / deny / grey classification) and
  caller groups (named trust tiers with per-group privilege, persona, and tool
  allowance).
- **In-call control and transfer** — hold/resume, blind transfer, and attended
  (consultative) transfer via REFER + Replaces.
- **Intercom mode + in-call DTMF actuation** — screen a door/gate visitor and
  buzz them in, locked down to only that action (`open_entry`, `send_dtmf`).
- **Call-progress detection** — fax-tone (CNG/CED) and answering-machine
  detection, with a leave-message protocol.
- **RFC 4028 session timers** — `Session-Expires` / `Min-SE` keep-alive refresh.
- **Production-safety lifecycle** — failure BYE, connection draining, an
  admission cap (concurrent-call limit), graceful shutdown, and log redaction.
- **Conversational UX** — an instant greeting on connect, dead-air comfort
  fillers, a caller-silence reprompt, and a spoken goodbye.
- **Automatic resilience** — registration reconnect, a watchdog that cleanly ends
  a silently-dropped call, and automatic text-to-speech failover from a cloud
  voice to the local voice mid-call.
- **The Hermes tool surface** — registers the `voip` platform plus **10 tools and
  1 hook**: `hang_up`, `hold_call`, `resume_call`, `list_registrations`,
  `place_call`, `report_call_result`, `send_dtmf`, `open_entry`, `transfer_blind`,
  and `transfer_attended`, all governed by a per-call `pre_tool_call`
  privilege-clamp hook. Bundled call-scenario skills ship as importable package
  data.
- **WebRTC support (experimental — live ICE not yet validated).** A WebRTC client
  is a first-class inbound caller with DTLS-SRTP media, ICE connectivity, and
  Opus audio (needs the `webrtc` extra + `libopus`); SIP-over-Secure-WebSocket
  signalling, outbound WebRTC origination over WSS, and a pre-encoded outbound
  WebRTC video stream are wired. These paths still want full live validation
  against a real gateway and client.
- **Apache-2.0 licensed**, with third-party attribution in
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and a [`NOTICE`](NOTICE) file.
- Release-process runbook
  ([docs/runbooks/0019-release-process.md](docs/runbooks/0019-release-process.md)):
  the exact, verified steps to cut a release — bump the version, run the
  version-sync tests, update this changelog, tag, `uv build`, and verify the wheel
  installs and ships the plugin manifest.
- This `CHANGELOG.md`.

### Changed

- Version is now single-sourced. `hermes_voip.__version__` derives from the
  installed distribution metadata (`importlib.metadata.version("hermes-voip")`,
  populated from `pyproject.toml [project].version`) instead of a hand-maintained
  literal. The test suite pins `pyproject.toml`, `__version__`, and the
  `plugin.yaml` manifest version equal, so a release is a single edit in
  `pyproject.toml`.

[Unreleased]: https://github.com/troykelly/hermes-voip/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/troykelly/hermes-voip/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/troykelly/hermes-voip/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/troykelly/hermes-voip/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/troykelly/hermes-voip/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/troykelly/hermes-voip/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/troykelly/hermes-voip/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/troykelly/hermes-voip/releases/tag/v0.1.0
