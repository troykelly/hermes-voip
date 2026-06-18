# ADR-0035: SIP-over-WSS signalling wiring (select the transport by config)

- **Date:** 2026-06-18
- **Status:** Accepted (amends ADR-0016 §1/§6 and ADR-0032 §5)
- **Deciders:** agent session (WSS signalling lane) — operator-directed

## Context

ADR-0016 designed the WebRTC transport and ADR-0032 wired the WebRTC **media**
plane (ICE + DTLS-SRTP + Opus) end-to-end — but only for an inbound INVITE that
**already arrived over the SIP-over-TLS socket**. The signalling transport was
never switched: `transport/ws_connection.py::WssSipTransport` (RFC 7118 — the
WebSocket counterpart to `SipOverTlsTransport`, PR-A / #57) was fully built and
unit-tested against a loopback WS SIP server, but **completely unwired** — the
class was referenced only inside its own module and test. Concretely:

- `adapter._establish()` (the transport factory) **unconditionally** constructed
  `SipOverTlsTransport` and never read `gateway_cfg.transport`. So
  `HERMES_SIP_TRANSPORT=wss` selected a `WSS` Via token (`config.via_transport`)
  but the bytes still went over a TLS **stream** socket — the Via said `WSS`
  while the framing was `Content-Length`-delimited TLS, an incoherent mix that
  no gateway would accept.
- The inbound UAS `Dialog` (`adapter.py`) and the agent-facing call-context
  payload (`call_context`) hardcoded the literal Via transport `"TLS"`, so even
  a hypothetically WSS-arriving call would have advertised `TLS` in its dialog.
- There was **no separate WSS endpoint config**: ADR-0016 §6 reserved
  `HERMES_SIP_WS_PATH` but nothing read it, and the WebRTC/WSS edge of a real
  gateway is typically a **different port with a different digest password** than
  the SIP-TLS edge (verified against the test gateway: the SIP-TLS endpoint and
  the Secure-WebSocket endpoint authenticate against different credentials).

The consequence: there was **no way to register or receive a call over WSS**, so
the WebRTC media path ADR-0032 proved e2e could not actually be reached from a
WebRTC gateway. This ADR records the signalling-transport wiring that closes that
gap — the WebRTC-live-test enabler. It is **signalling wiring only**: once an
INVITE arrives over `WssSipTransport`, it flows through the identical
`_on_inbound_invite → is_webrtc → _setup_webrtc_call` path ADR-0032 already
shipped and the WebRTC inbound e2e harness (#107) already proves.

Constraints: AGENTS.md rule 6 (end-to-end, no half-wired transport — this lane
makes the *whole* WSS-signalling → WebRTC-media path reachable), rule 17/39 (no
escape hatches; `websockets` stays behind the `webrtc` extra, lazy-imported per
the ADR-0014 split), rule 34 (the separate WSS credential is read from an env var
by name — its value lives only in `.env`/1Password, never hardcoded or logged),
the repo is **public** (fakes only: `pbx.example.test`, ext `1000`).

## Decision

**Select the signalling transport class at `adapter._establish()` time by
`gateway_cfg.transport` (`tls` → `SipOverTlsTransport`, `wss` →
`WssSipTransport`), wiring the identical observer seams; make the inbound dialog
+ call-context Via derive from `gateway_cfg.via_transport` instead of a literal
`"TLS"`; and add a separate-WSS-endpoint config surface (`HERMES_SIP_WS_PATH` +
`HERMES_SIP_WS_PASSWORD`).** The manager, registration, dialog, `CallSession`,
`CallLoop`, the inbound INVITE handler, and the WebRTC media path are **otherwise
unchanged** — both transports satisfy the same `SipTransport` / `CallSignaling`
Protocols, which is exactly what ADR-0016 §5 promised the seam discipline would
buy. This ADR is the WHY; the implementing PR + the runbook are the HOW.

### 1. Transport selection in `_establish()`

`_establish()` branches on `gateway_cfg.transport`:

- `tls` → `SipOverTlsTransport(host, port, ssl_context, keepalive_interval, …)`
  — **unchanged** behaviour, byte-for-byte.
- `wss` → `WssSipTransport(host, port, ws_path, ssl_context, connect_address, …)`
  — connects over `wss://<host>:<port><ws_path>` with subprotocol `sip`.

Both are wired with the **same** `on_new_call=self._on_inbound_invite`,
`on_unroutable=self._on_unroutable`, `on_connection_lost=self._on_connection_lost`
observers, and the same `bind_manager` + `add_call` re-attach loop. So an INVITE
arriving over WSS reaches `_on_inbound_invite` exactly as a TLS INVITE does, and
the existing `offer.audio.is_webrtc` branch routes it into `_setup_webrtc_call`.

`WssSipTransport` takes the **same** `ssl_context` the TLS path uses
(`_make_tls_context(host)`) — `wss://` is WebSocket-over-TLS, so the gateway's
server certificate is verified identically; the SNI/hostname-verification host is
`gateway_cfg.host` even when `connect_address` dials a numeric IP.

`WssSipTransport` does **not** take a `keepalive_interval`: it has no
periodic-OPTIONS keepalive timer of its own (a WebSocket is a persistent
connection; the gateway qualifies the contact with inbound OPTIONS pings that the
transport already answers `200 OK`). This is a real, recorded difference in the
two transports' construction surface, not an oversight — `_establish()` builds
each with the kwargs that transport actually accepts.

### 2. Transport-aware inbound Via (the two real `"TLS"` literals)

The inbound INVITE handler minted the local UAS endpoint with a hardcoded Via
transport `"TLS"` in two places — the `Dialog` it builds and the agent-facing
`call_context` payload. Both now read `gateway_cfg.via_transport` (`TLS` | `WSS`),
so a call received over WSS advertises `WSS` in its dialog and reports the WSS
transport to the agent. The REGISTER Via and the Contact were **already**
transport-correct and needed no change:

- The REGISTER Via transport already comes from `gateway.via_transport`
  (`config.registration_config(... transport=self.via_transport)`), so it is
  already `WSS` on a wss gateway.
- The `Contact` is already built from `transport.contact_uri(ext)`, and
  `WssSipTransport.contact_uri` already emits the RFC 7118 / RFC 5626 Outbound
  Contact (`<sip:ext@<token>.invalid;transport=ws>;reg-id=1;+sip.instance="…"`).
  `SipOverTlsTransport.contact_uri` keeps emitting `;transport=tls` — that is the
  **correct** token for the TLS transport's own Contact, not a bug. (Per RFC 7118
  §5.3 the URI parameter is lowercase `ws` even over a `wss://` connection; the
  WSS *Via* token is upper-case `WSS`. Both are already produced correctly.)

The **outbound** UAC INVITE path keeps its `"TLS"` Via literal: outbound WebRTC
origination over WSS is explicitly **deferred** (§4), so the outbound path still
runs over SIP-over-TLS and `"TLS"` is the truthful token there.

### 3. Separate WSS endpoint configuration

A WebRTC/WSS gateway edge is commonly a **different port + different password**
than the SIP-TLS edge. Two additions to `GatewayConfig`, both defaulted so a
`tls` install is unaffected:

- **`ws_path`** — the WebSocket upgrade path, from `HERMES_SIP_WS_PATH`
  (default `/ws`). Threaded into `WssSipTransport(ws_path=…)`. Ignored on `tls`.
- **`ws_password`** — an **optional** digest password override from
  `HERMES_SIP_WS_PASSWORD`, **repr-suppressed** (a secret; never logged — same
  discipline as the TURN password, ADR-0034). When `transport == "wss"` **and**
  `ws_password` is set, `registration_config()` substitutes it for the
  per-extension SIP password used in the digest. When unset, the digest falls
  back to the existing per-extension `HERMES_SIP_PASSWORD[_n]` (documented
  fallback) — so an operator whose WSS edge shares the SIP password configures
  nothing extra.

The env-var **names** are defined here and in `config.py`; the **values** live
only in `.env` / 1Password and are supplied by the operator at deploy time. The
port is the existing `HERMES_SIP_PORT` (default `443` for `wss`); a deployment
that uses a non-standard WSS port (e.g. the test gateway's) sets `HERMES_SIP_PORT`
accordingly. We did **not** add a second simultaneous endpoint: `HERMES_SIP_TRANSPORT`
selects **one** transport per process (the operator runs the plugin as a WSS *or*
a TLS extension), so one port + one path + one (optional) password override is the
whole surface. Running both transports at once is a larger change (two managers,
two registration sets) and is out of scope — named, not stubbed.

### 4. Scope / deferrals (rule 6 — named, not silent)

- **Inbound over WSS is wired end-to-end** behind `HERMES_SIP_TRANSPORT=wss`:
  REGISTER + inbound INVITE + BYE + keepalive over `WssSipTransport`, feeding the
  ADR-0032 WebRTC media path. This lane makes the whole WSS→WebRTC path reachable.
- **Outbound WebRTC origination over WSS stays deferred** (ADR-0032 §5). The
  outbound UAC path (`originate.py` + `_place_call`) still offers SDES/G.711-G.722
  over TLS and keeps its `"TLS"` Via. Selecting WSS does not yet re-route the
  agent's `place_call` over the WebSocket; that is a separate, named follow-up
  (task #32). A real boundary, not a stub.
- **Opus-on-SIP (SDES/TLS) stays deferred** (ADR-0032 alternatives): Opus is
  advertised only on the WebRTC path it was added for.
- **Two simultaneous transports** (a TLS *and* a WSS registration in one process)
  is out of scope — `HERMES_SIP_TRANSPORT` selects one (§3).
- **Live WSS validation** against the real gateway is the operator step: it needs
  the operator's WSS-endpoint **port + `HERMES_SIP_WS_PASSWORD`** in `.env` and a
  plugin restart (runbook 0009). This lane lands the wiring + the in-process
  (loopback-WS REGISTER, faked-WSS-INVITE→`is_webrtc`) evidence; it does **not**
  touch the live gateway. Prior live probes recorded a 401 on the gateway's
  Secure-WebSocket REGISTER for the test extension; whether that endpoint accepts
  a pure-RFC-7118 SIP digest with the right port + credential is the open
  question the operator's live step answers — the plugin now emits the correct
  RFC 7118 REGISTER, so the remaining variable is the gateway-side endpoint +
  credential, not the client.

## Consequences

- **Easier:** a single config flag (`HERMES_SIP_TRANSPORT=wss` + the WSS endpoint
  port/path/password) now switches a deployment from a SIP-TLS extension to a
  WebRTC/WSS extension with the **entire** registration, dialog, call, media, and
  agent surface unchanged — the ADR-0004/0005/0016 seam discipline pays off: the
  diff is a transport-class selection plus two Via-token literals plus three
  config fields, not a parallel stack.
- **Type surface:** `adapter._transport` and the inbound-path helper signatures
  widen from `SipOverTlsTransport` to `SipOverTlsTransport | WssSipTransport` (a
  union both classes inhabit — they expose the identical method set:
  `send`/`local_sent_by`/`contact_uri`/`add_call`/`remove_call`/`bind_manager`/
  `connect`/`aclose`). No `Any`, no cast, no escape hatch — the union is exact and
  mypy `--strict` checks every call site against both members.
- **Harder / committed to maintain:** the construction surface now diverges by
  transport (`keepalive_interval` is TLS-only; `ws_path` is WSS-only), so
  `_establish()` carries a small branch. A new secret env var
  (`HERMES_SIP_WS_PASSWORD`) joins the never-log set.
- **Security:** `HERMES_SIP_WS_PASSWORD` is read by name, `repr`-suppressed on
  `GatewayConfig`, and never emitted to a log line (rule 34). The WSS TLS layer
  verifies the gateway certificate exactly as the SIP-TLS transport does (same
  `ssl_context`); there is no `verify=False` anywhere on the WSS path.
- **No new runtime dependency in this lane:** `websockets` (the WS client) was
  already added to the `webrtc` extra in PR-A (#57); this lane only *wires* it. No
  licence/advisory change.
- **Standards-only, gateway-agnostic:** the WSS REGISTER/INVITE/Via/Contact are
  RFC 7118 / RFC 5626; no vendor quirk enters core. Gateway specifics (host, WSS
  port, path, credential) stay in `HERMES_SIP_*` config.

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| Keep one transport class branching TLS vs WSS internally | ADR-0016 already rejected this: the framing (Content-Length stream vs one-message-per-frame) and address model (real socket sent-by vs `.invalid` token) differ fundamentally. `WssSipTransport` already exists as a peer `SipTransport`; `_establish()` just selects it. |
| Make `WssSipTransport` accept (and ignore) `keepalive_interval` for a uniform constructor | A constructor parameter the class ignores is an aspirational-API defect (rule 27). The two transports' construction surfaces genuinely differ; `_establish()` builds each with the kwargs it accepts. |
| Reuse the per-extension SIP password on WSS (no separate credential) | The test gateway authenticates its Secure-WebSocket edge against a different credential than its SIP-TLS edge. A single password would make a real WSS deployment impossible. `HERMES_SIP_WS_PASSWORD` overrides it; **falling back** to the SIP password keeps the common (shared-credential) case zero-config. |
| Add a second, simultaneous WSS endpoint alongside the TLS one | Out of scope: `HERMES_SIP_TRANSPORT` selects one transport per process; two simultaneous registration stacks is a larger, separately-justified change. Named as a non-goal, not stubbed. |
| Also re-route outbound `place_call` over WSS in this lane | Outbound WebRTC origination (our own DTLS/ICE offer over WSS) is a media-plane change deferred in ADR-0032 §5; this lane is signalling-transport selection only. Bundling it would exceed the WebRTC-live-test-enabler scope. |
| Change `connection.py` `contact_uri` to emit `transport=ws` for WSS | That method belongs to `SipOverTlsTransport`; `transport=tls` is the correct Contact token for the TLS transport. The WSS Contact comes from `WssSipTransport.contact_uri`, which already emits `transport=ws`. No change needed (the task brief's "fix connection.py:228" was a misread of which transport owns that method). |
