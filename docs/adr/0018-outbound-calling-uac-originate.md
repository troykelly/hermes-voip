# ADR-0018: Outbound calling — UAC originate flow (place_call)

- **Date:** 2026-06-16
- **Status:** Accepted
- **Deciders:** agent session (outbound calling design)

## Context

The plugin currently answers inbound calls (UAS path: remote party sends INVITE, we
accept it, wire a `CallSession`, run the `CallLoop`). The agent must also be able to
**initiate** a call — the UAC path — so a Hermes operator or the Hermes agent itself
can say "call extension 1000" and the plugin places the INVITE. This is required for:

- Agent-driven enquiries and reservations (the "place\_call" use-case planned in
  ADR-0002 §Outbound and the memory entry from the initial design session).
- Operator/developer live validation of the full in-process stack without waiting for
  an external caller to ring in.

Several pieces needed for outbound already exist:

| Existing piece | Outbound use |
|---|---|
| `dialog.py::Dialog.from_invite_2xx` | Builds the UAC dialog after our INVITE is answered with a 2xx |
| `refer.py::build_triggered_invite` | Pattern for constructing an out-of-dialog INVITE (UAC style: new Call-ID, From-tag, CSeq 1) |
| `registration.py::DigestCredentials` + the 401/407 re-auth flow | Same challenge→re-send-with-Authorization pattern applies to an outbound INVITE challenged with 401/407 |
| `transport/connection.py::SipOverTlsTransport.send` + `InviteClientTransaction` | Tracks outbound INVITE client transactions, auto-ACKs non-2xx finals (RFC 3261 §17.1.1.3) |
| `media/engine.py::RtpMediaTransport`, `media/call_loop.py::CallLoop`, `call.py::CallSession` | Identical to inbound once the dialog is established |
| `adapter.py::_handle_inbound_invite` | The structural analogue — the outbound handler mirrors its post-2xx steps |
| `tools.py::CallControlTools` | The existing agent-facing tool surface for an active call |

The gap is two-fold:

1. **SDP OFFER builder** — `sdp.py` has `build_audio_answer` (builds a UAS SDP answer
   from an inbound offer) but no `build_audio_offer` (builds a UAC SDP offer from
   scratch). Outbound needs to generate the initial offer we put in our INVITE.
2. **Originate orchestrator** — no module yet wires: open RTP engine → build INVITE with
   the SDP offer → handle provisional 1xx + auth challenges → on 2xx, build dialog +
   send ACK + register `CallSession` + start `CallLoop`.

The per-call media isolation fix landing on branch `fix/voip-media-concurrency`
(isolating `RtpMediaTransport` per call, graceful `send_audio`) is an orthogonal
change; outbound composes with it because it opens its own engine per call in exactly
the same pattern as `_handle_inbound_invite`.

Constraints:

- **Public repo**: no real gateway host, extension, IP, or PII in any tracked file.
  All examples and tests use `pbx.example.test` and extension `1000`.
- **Fully typed, no escape hatches** (AGENTS.md rules 17, 39): every symbol annotated,
  clean under `mypy --strict`, no `Any`, no unjustified `# type: ignore`.
- **Gateway-agnostic** (CLAUDE.md invariant): no vendor-specific wire behaviour in the
  core. The same originate path works against any RFC-3261-compliant gateway.
- **Errors propagate** (rule 37): every SIP failure code, auth failure, and timeout
  reaches a typed outcome; no silent swallowing.

---

## Decision

### 1. End-to-end originate flow

`VoipAdapter.place_call(target_extension: str) -> str` (returns the new `Call-ID`)
drives the full outbound lifecycle as an `async` method. A thin
`_handle_outbound_invite` coroutine mirrors `_handle_inbound_invite`:

```
place_call(target_extension)
  │
  ├─ 1. Choose a registered extension to originate from
  │     (RegistrationManager.active_registration() → a RegistrationStatus; fail fast
  │      if no extension is registered — cannot place a call without a registration)
  │
  ├─ 2. Open RtpMediaTransport (local_address="0.0.0.0", local_port=0)
  │     await engine.connect()          ← binds the UDP socket, learns local_port
  │
  ├─ 3. Build SDP offer
  │     build_audio_offer(
  │         local_address = _host_of(transport.local_sent_by),
  │         port          = engine.local_port,
  │         srtp          = media_cfg.srtp_enabled,   ← SDES if enabled, else plain
  │         session_id    = <monotonic int>,
  │     )
  │
  ├─ 4. Build INVITE (new Call-ID, From-tag, CSeq 1 INVITE)
  │     build_outbound_invite(
  │         target_uri    = f"sip:{target_extension}@{gateway_cfg.domain}",
  │         local_aor     = f"sip:{ext.extension}@{gateway_cfg.domain}",
  │         local_contact = transport.contact_uri(ext.extension),
  │         local_sent_by = transport.local_sent_by,
  │         transport     = "TLS",
  │         body          = sdp_offer,
  │     )
  │     (modelled on build_triggered_invite in refer.py — fresh Call-ID, From-tag,
  │      CSeq 1, no Replaces/Referred-By)
  │
  ├─ 5. transport.send(invite_text)
  │     → transport tracks InviteClientTransaction (Call-ID, CSeq=1)
  │
  ├─ 6. Await response via CallSession.on_response / a response channel
  │
  │   ┌── 1xx provisional (100 Trying, 180 Ringing) ──────────────────────────┐
  │   │   Absorbed (no action); 183 Session Progress with SDP = early media    │
  │   │   (early-media handling is deferred, see §8 Phasing below)             │
  │   └────────────────────────────────────────────────────────────────────────┘
  │
  │   ┌── 401 / 407 challenge ─────────────────────────────────────────────────┐
  │   │   Parse WWW-Authenticate / Proxy-Authenticate.                          │
  │   │   Build new INVITE: same To/From URIs, same Call-ID, same From-tag,    │
  │   │   NEW branch (new client transaction), CSeq incremented by 1,          │
  │   │   Authorization / Proxy-Authorization computed via digest.py.           │
  │   │   transport.send(re_invite). One re-auth attempt per INVITE; if the    │
  │   │   challenged re-send is also challenged → OutboundCallFailed(407).      │
  │   └────────────────────────────────────────────────────────────────────────┘
  │
  │   ┌── 2xx answer ──────────────────────────────────────────────────────────┐
  │   │   dialog = Dialog.from_invite_2xx(invite, response)                    │
  │   │   Build ACK (new branch, same Call-ID/From-tag, CSeq 1 ACK, no body)  │
  │   │   transport.send(ack_text)                                              │
  │   │   Parse peer's SDP answer → negotiate_audio → Codec                   │
  │   │   engine.set_remote(address, port, codec)   ← re-point the RTP engine  │
  │   │   Register CallSession in manager + transport                           │
  │   │   Start CallLoop (greeting="" for outbound — we speak first is wrong   │
  │   │   here; the agent's opening line comes via the Hermes turn)             │
  │   └────────────────────────────────────────────────────────────────────────┘
  │
  │   ┌── Non-2xx final (486 Busy Here / 603 Decline / 408 Timeout /           │
  │   │                   487 Request Terminated / 4xx/5xx/6xx)                │
  │   │   InviteClientTransaction auto-ACKs the final (RFC 3261 §17.1.1.3).   │
  │   │   engine.stop() — release the UDP socket.                               │
  │   │   Return / raise OutboundCallFailed(status, reason).                   │
  │   └────────────────────────────────────────────────────────────────────────┘
  │
  └─ Returns call_id to the caller (place_call resolves once the CallLoop
     is running; failures resolve as OutboundCallFailed)
```

**CANCEL** (caller/agent hangs up before the call is answered):

If the agent calls `cancel_call(call_id)` while the INVITE is pending (before a 2xx
or a non-2xx final), the adapter sends a `CANCEL` request using the same
`Call-ID` and `CSeq` number of the pending INVITE, on the same branch (RFC 3261
§9.1). The gateway responds `200 OK` to the CANCEL and then sends `487 Request
Terminated` to the INVITE; the `InviteClientTransaction` ACKs the 487. The engine
is stopped.

**BYE** (either side ends an established call):

Identical to the inbound path. `CallSession.hangup()` sends BYE in-dialog
(`build_in_dialog_request` via `dialog.py`). Inbound BYE from the peer is already
handled by the manager's in-dialog routing to `CallSession.handle_request`.

### 2. SDP OFFER builder

A new function `build_audio_offer` is added to `sdp.py` alongside the existing
`build_audio_answer`.

**What we offer:**

- **Codecs (offer order):** PCMU (PT 0), PCMA (PT 8), telephone-event (PT 101,
  fmtp 0-16). This is the same set `_SUPPORTED_ENCODINGS` in `adapter.py` and the
  same order as our answers. The gateway picks from our offer; we then negotiate
  (via the existing `negotiate_audio`) against its answer.
- **Media security profile:** Controlled by `MediaConfig.srtp_enabled`
  (`HERMES_VOIP_SRTP_ENABLED`):
  - `True` → `RTP/SAVP` with two `a=crypto` lines (SDES, AES\_CM\_128\_HMAC\_SHA1\_80
    at tag 1, AES\_CM\_128\_HMAC\_SHA1\_32 at tag 2), keys generated via
    `secrets.token_bytes(30)` encoded as base64 inline — the same scheme `sdp.py`
    already validates. The peer must choose one; we key the engine from the answered
    tag (already covered by the inbound `_srtp_from_audio` logic).
  - `False` → `RTP/AVP`, no `a=crypto` lines. Matches a plain-RTP gateway.
- **Offer address / port:** The engine's bound local UDP address and port (same as
  the inbound answer path in `adapter.py::_handle_inbound_invite`).
- **Direction:** `sendrecv` (default; we both send and receive).
- **ptime:** `20` (20 ms — matches the inbound answer; standard for G.711).

The function signature (to be implemented):

```python
def build_audio_offer(
    *,
    local_address: str,
    port: int,
    srtp: bool,
    session_id: int,
    user: str = "-",
) -> str:
    ...
```

Returns a complete SDP body string (`application/sdp`) suitable for the INVITE body.

**Reuse:** `build_audio_offer` can share the session-level header builder and the
`a=rtpmap`/`a=fmtp` render logic already in `build_audio_answer`. It does **not**
parse an inbound offer and does not call `negotiate_audio` — it produces an initial
offer from scratch.

**NAT (ADR-0015 composability):** The offer advertises our private RTP address (same
as the inbound answer path). Comedia latching (ADR-0015) is already in
`RtpMediaTransport.send_audio`: on the first inbound RTP packet the engine latches
onto the real source address. This needs no change for outbound. The outbound greeting
in the `CallLoop` (or the first agent TTS turn) triggers the initial RTP send so the
gateway's comedia latch opens, exactly as for inbound.

### 3. Authentication challenge on the outbound INVITE

The pattern mirrors `registration.py::RegistrationFlow._reauthenticate`:

- 401 challenge → parse `WWW-Authenticate` → compute Authorization via
  `digest.py::build_authorization` with `method="INVITE"` and
  `uri=<request-URI of the INVITE>`.
- 407 challenge → parse `Proxy-Authenticate` → compute Proxy-Authorization.
- New INVITE: same `Call-ID` and `From-tag`, incremented `CSeq`, **new Via branch**
  (a fresh client transaction, per RFC 3261 §22.1). Credentials come from the
  originating registration's `DigestCredentials`.
- One re-auth per INVITE. A second challenge on the authenticated re-send → fail with
  `OutboundCallFailed(status=407, reason="Authentication failed")` (bad credentials
  or nonce replay — log at ERROR, do not retry in a loop).

The per-INVITE challenge state is held in a local dataclass
`_OutboundTransaction(call_id, from_tag, cseq, challenged: bool)` inside the
originate coroutine. No global mutable state; no shared structure with the REGISTER
flow's `_Transaction`.

### 4. Agent-facing `place_call` tool

**Why a tool?** ADR-0009 and ADR-0011 §3 establish that every action the agent can
take on the telephony plane is exposed as a typed tool, gated by `ToolRisk`, and
subject to the `pre_tool_call` hook. "Place an outbound call" is an irreversible,
outward-facing action: once the INVITE is sent, the far end rings.

**Tool surface added to `tools.py`:**

```python
TOOL_RISKS["place_call"] = ToolRisk.IRREVERSIBLE
```

`CallControlTools.place_call(target: str) -> ToolResult`:

- `target` is a SIP URI or bare extension number (`"1000"`, `"sip:1000@pbx.example.test"`).
  Bare numbers are resolved relative to the gateway domain at call time.
- Risk: `IRREVERSIBLE`. Requires explicit caller confirmation via `ConfirmationSource`
  **and** session not `degraded`, exactly like `transfer_blind`.
- On pass: calls `VoipAdapter.place_call(target)` (or a `PlaceCallCapable` Protocol
  the adapter implements — same pattern as `ControllableCall`).
- Returns `ToolResult(allowed=True, message=f"call to {target} placed, call_id={cid}")`
  or `ToolResult(allowed=False, message=<reason>)`.

**Allowlist / guardrails:**

- **Phase 1:** No allowlist. The guard is `ToolRisk.IRREVERSIBLE` requiring explicit
  confirmation — that is the enforceable control. An operator can configure a static
  extension allowlist via `HERMES_VOIP_OUTBOUND_ALLOW` (comma-separated extensions or
  SIP URI prefixes) in a Phase 2 hardening pass.
- **Who the agent may call:** At Phase 1, any extension reachable via the gateway
  (the gateway enforces its own dial plan). The agent cannot call arbitrary PSTN
  numbers (the gateway would need to permit PSTN routing; that is a gateway policy, not
  ours). The agent's prompt and the injection-guard's `DENY` verdict remain the first
  line of defense; `IRREVERSIBLE`-gating is the enforceable backstop.
- **Degraded-session block:** A session flagged `degraded` by the injection guard
  cannot place outbound calls (same invariant 3 as transfers).

**Context in which `place_call` is meaningful:** The tool is registered against the
Hermes session that owns the current `CallSession` (the inbound call the agent is
on). An agent can call `place_call` to escalate a caller's request ("I'll connect you
to the billing department") as an alternative to a transfer, or to place a new
call on behalf of the operator. Phase 1 does not support simultaneous inbound + outbound
on the same Hermes session; that is deferred (see §8).

### 5. Operator/developer test trigger

**Goal:** exercise the full in-process stack (plugin loaded, Hermes runtime active,
real TLS/SRTP, real RTP engine, real `CallLoop`) against the live gateway, placing one
outbound call to a given extension. This must not be a permanent hack, must not
require changes to production code paths, and must use the real Hermes `handle_message`
dispatch (not a stub).

**Options analysed:**

| Option | Assessment |
|---|---|
| A separate thin admin entrypoint (e.g. a Flask/FastAPI admin HTTP server running beside the adapter) | Requires a second process or a side-channel listener; adds an infra component; contradicts rule 40 (no hosting platform without ADR) |
| An env-var one-shot at adapter startup (`HERMES_VOIP_CALL_ON_CONNECT=1000`) | Simple; zero new code after the adapter is wired; fires once then clears; exercises the real stack from the first event loop tick after registration; clean |
| Driving the agent tool via a synthetic Hermes `MessageEvent` injected at startup | Requires constructing a fake inbound caller context and manufacturing a confirmation; exercises the guard/tool path but adds complexity for what is a dev/ops concern, not a user story |
| A `hermes_voip test-call` CLI subcommand (separate process) | Cannot share the adapter's in-process `asyncio` loop with the real Hermes runtime; exercises only a headless SIP stack, not the Hermes turn path |

**Decision: Option B — `HERMES_VOIP_CALL_ON_CONNECT`.**

When `HERMES_VOIP_CALL_ON_CONNECT=<extension>` is set and the adapter has
successfully registered at least one extension, it calls
`asyncio.create_task(self.place_call(extension))` exactly once (immediately after
`manager.connect()` returns `True` in `_establish`). The env var is read and cleared
(set to `""`) after the task is created so a reconnect does not re-trigger it.

This is not a production feature — it is a dev/ops escape hatch. It bypasses the
agent-tool gate and the `ConfirmationSource` (it is not coming from an agent; it is
coming from the operator who set the env var). The adapter logs
`"HERMES_VOIP_CALL_ON_CONNECT: placing test call to <extension>"` at WARNING level so
it is obvious in the output. It requires no code outside `adapter.py` and no permanent
new entrypoint; removing the env var disables it.

The call flows through `place_call` → `_handle_outbound_invite` → the real
`CallLoop` → `_deliver_turn` → `self.handle_message` → the Hermes runtime → the
agent → `send` → `CallLoop.speak`. This exercises the full in-process path.

### 6. Concurrency and limits

**Max concurrent outbound calls (Phase 1):** One. If `place_call` is called while an
outbound call from the same originating extension is already pending or established,
the second call is rejected immediately with `OutboundCallFailed(status=503,
reason="outbound call already in progress")`. This avoids race conditions in the
response-channel bookkeeping and matches the Phase-1 scope. Phase 2 can lift the limit
with per-extension tracking (one `_OutboundTransaction` per extension, map keyed by
extension).

**Interaction with inbound calls:** Outbound and inbound calls share the same
`_call_sessions` / `_call_loops` dicts (keyed by `Call-ID`). They are independent —
different `Call-ID`s, different `RtpMediaTransport` instances, different `CallSession`
rows. The manager's in-dialog routing is already keyed by `dialog_id` (a triple), so
there is no demux collision. The only shared resource is the TLS connection (one
connection, multiplexed by `Call-ID`), which is already the case for N simultaneous
registrations.

**Per-call isolation (composability with `fix/voip-media-concurrency`):** Outbound
opens its own `RtpMediaTransport` in the same way `_handle_inbound_invite` does.
Whatever per-call isolation the media fix introduces (e.g. isolating the `asyncio`
task, bounding `send_audio` gracefully) applies to outbound identically — the fix
is at the engine level, not in the call-acceptance path.

### 7. Failure and edge-case handling

| Scenario | Handling |
|---|---|
| No extension registered when `place_call` is called | Fail immediately: `OutboundCallFailed(status=503, "no registered extension")`. Do not open the RTP engine. |
| INVITE times out (no response after T1=500 ms × 7 retransmits ≈ 32 s on UDP; on TLS/reliable, one send, then 64×T1 timer) | `InviteClientTransaction` state machine fires Timer B; deliver timeout outcome → `OutboundCallFailed(408)`. Engine stopped. |
| 486 Busy Here / 603 Decline | Non-2xx final; auto-ACKed by `InviteClientTransaction`; `OutboundCallFailed(486/603)`. |
| 487 Request Terminated (response to CANCEL) | Same path: auto-ACKed; engine stopped. |
| 2xx arrives after CANCEL was sent | The `InviteClientTransaction` is in `Completed` state; the stray 2xx is absorbed; a BYE is sent to tear down the half-open dialog (RFC 3261 §15.1.1). |
| Transport lost mid-INVITE (TLS FIN before 2xx) | The reconnect supervisor rebuilds the transport and re-registers; the pending outbound transaction has no way to resume on the new flow. The originate coroutine's response channel will never fire. The `place_call` caller must impose a timeout (e.g. `asyncio.wait_for` with 35 s, covering Timer B). |
| Peer's 2xx SDP answer has no audio | `negotiate_audio` raises `ValueError`; send BYE immediately (RFC 3261 §13.2.2.4 — a 2xx must be ACKed before BYE; ACK first, then BYE); clean up engine. |
| RTP engine fails to bind (port exhaustion) | `engine.connect()` raises `OSError`; send 500 Internal Error to the ... wait, we are the UAC — there is no 500 to send. The originate coroutine fails with `OutboundCallFailed(500, "media engine failed to bind")`. |

**Efficiency (rule 22):**

- The RTP engine is opened **before** the INVITE is sent. If the INVITE is
  challenged or rejected, the engine is stopped promptly (immediately on non-2xx
  final, after the re-send on auth challenge). The UDP socket lifetime is at most
  the INVITE transaction time (≤ 35 s Timer B). On a successful call the engine
  lives for the call duration, same as inbound.
- The 2xx ACK is sent synchronously inside the `on_response` handler path, before the
  `CallLoop` starts, so the ACK reaches the gateway before any RTP flows. This matches
  the RFC 3261 §13.2.2.4 requirement and prevents the gateway from retransmitting the
  2xx indefinitely.
- The response channel between `_handle_outbound_invite` and the transport's response
  dispatch is a single-item `asyncio.Queue[SipResponse]`; the coroutine `await`s on
  it. No polling, no busy loop.
- Memory: one outbound call adds one entry to each of `_call_sessions`,
  `_call_loops`, `_call_tasks`, `_call_info` — identical footprint to one inbound
  call.

### 8. Phasing

**Phase 1 — first outbound PR (minimum shippable):**

1. `sdp.py::build_audio_offer` — the SDP offer builder, with full SDES-SRTP support.
2. `originate.py` (new pure sans-IO module) — `build_outbound_invite` (INVITE builder,
   modelled on `build_triggered_invite`) and `OutboundCallFailed` (typed failure).
3. `adapter.py::place_call` + `_handle_outbound_invite` — the async originate
   orchestrator, including 401/407 re-auth, 2xx ACK, `CallSession` registration, and
   `CallLoop` start. No greeting on outbound (the agent's first turn opens the
   conversation). Max 1 concurrent outbound.
4. `adapter.py::HERMES_VOIP_CALL_ON_CONNECT` — env-var test trigger (WARNING-logged,
   gate-bypassing, fires once).
5. Tests: a loopback TLS + fake gateway test (using the existing loopback TLS fixture
   from `tests/transport/`) exercising: INVITE → 407 challenge → re-send → 200 OK →
   ACK emitted → CallLoop wired; INVITE → 486 → engine stopped; CANCEL path. Pure
   unit tests for `build_outbound_invite` and `build_audio_offer`.

**Phase 2 — agent tool + hardening (follow-on PR):**

6. `tools.py::TOOL_RISKS["place_call"]` + `CallControlTools.place_call` — the gated
   agent tool with `IRREVERSIBLE` risk and `ConfirmationSource`.
7. `PlaceCallCapable` Protocol in `tools.py` (mirrors `ControllableCall`); `VoipAdapter`
   implements it.
8. `HERMES_VOIP_OUTBOUND_ALLOW` allowlist (comma-separated) in `config.py` — extension
   and SIP-URI-prefix validation at call time.
9. Early-media (183 with SDP): decode and apply the remote SDP in `on_response` for
   183, connect the engine early so the caller hears ringback/IVR before the call
   answers.
10. Concurrent outbound limit lifted to N per extension, with per-extension tracking.

---

## Consequences

**Easier:**

- The Hermes agent can initiate calls to extensions — the "have the agent call me"
  use-case and operator-driven enquiries are unblocked.
- Live end-to-end testing no longer requires an external caller; the operator sets one
  env var, restarts, and observes the full stack in the log.
- The outbound path reuses almost every existing building block: no new crypto, no
  new transport, no new media engine, no new provider interface.

**Harder / new commitments:**

- `sdp.py` gains a new public function (`build_audio_offer`) with its own SDES key
  generation; it must stay in sync with the `build_audio_answer` render conventions.
- Auth challenge handling is now needed in two places (REGISTER flow in
  `registration.py` and the outbound INVITE flow in the new `originate.py`). The
  shared logic (`digest.py::build_authorization`) is already factored out correctly;
  care is needed not to duplicate the challenge-state machine.
- The response-channel dispatch in `SipOverTlsTransport` must route 1xx / 2xx / 4xx
  responses for an outbound INVITE to the originate coroutine's queue, not to an
  existing `CallSession.on_response` (the `CallSession` is not registered until after
  the 2xx). The transport's `_calls` dict is keyed by `Call-ID`; during the INVITE
  transaction phase a temporary `CallResponseSink` (a thin wrapper around the
  `asyncio.Queue`) is registered under the outbound `Call-ID`, then replaced by the
  real `CallSession` on 2xx. This is a small extension to the existing dispatch.
- CANCEL semantics require the originate coroutine to know the branch of the pending
  INVITE transaction and to send CANCEL on the same branch. The `_OutboundTransaction`
  local state carries the branch. This must be a separate code path from the existing
  in-dialog BYE.

**Not changed:**

- Inbound call handling is unchanged.
- The reconnect supervisor is unchanged.
- Provider, codec, SRTP, and RTP engine interfaces are unchanged.
- The `tools.py` gate architecture (AGENTS.md invariant 3 / ADR-0009) is extended,
  not modified: `place_call` is one more entry in `TOOL_RISKS`.
- The WebRTC path (ADR-0016) is not affected; outbound via WebRTC will follow
  ADR-0016's transport seam when that transport ships.

---

## Alternatives considered

| Alternative | Rejected because |
|---|---|
| Place outbound call via an attended-transfer REFER to the target (use the agent's own extension as the consultation leg) | Requires an established call already in progress; useless for initiating from idle. REFER is for transferring, not originating. |
| Open a second TLS connection per outbound call | Wasteful; the gateway expects the single registered connection to carry all dialogs for the extension. Against the existing transport design. |
| Use a separate `asyncio.Queue` per response type (1xx queue, final queue) | Over-engineering. A single `Queue[SipResponse]` with the coroutine dispatching on `response.status_code` is simpler and sufficient — the coroutine is the sole consumer of its own outbound call's responses. |
| Generate the SDP offer inside `originate.py` rather than adding to `sdp.py` | `sdp.py` owns all SDP construction/negotiation (ADR-0005 boundary). Adding `build_audio_offer` there is consistent with `build_audio_answer` and avoids a new dependency cycle. |
| Allowlist gating from day one (Phase 1) | The `IRREVERSIBLE` tool risk + `ConfirmationSource` already requires an explicit caller confirmation gate. An allowlist adds defence-in-depth but is not required for correctness. Deferring it to Phase 2 keeps Phase 1 focused on the mechanism. |
| CLI subcommand for the test trigger | A separate process cannot share the Hermes runtime's `asyncio` event loop. The call loop, `handle_message`, and `send` all require the live adapter instance; a subprocess cannot reach it without IPC. The env-var trigger is a direct call inside the running process. |
