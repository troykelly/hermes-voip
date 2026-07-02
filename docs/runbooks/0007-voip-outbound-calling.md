# Runbook: agent-triggered outbound calling (`HERMES_VOIP_OUTBOUND_ALLOW`)

**What it is.** The `place_call(number, objective)` agent tool lets a Hermes agent place an
outbound call to accomplish a task (e.g. "call the restaurant and book a table for two at 7").
The call runs as its own concurrent Hermes conversation that opens with the objective, and the
outcome is reported back to the conversation that requested it (ADR-0029). This runbook is the
operational HOW for the two operator knobs that govern it; the WHY is in **ADR-0029** (and the
amended **ADR-0019 §4/§8**, **ADR-0026**).

> **Public repo.** No secrets here. A dial target (an extension, or a real PSTN number if the
> gateway routes it) is potentially sensitive and lives ONLY in the gitignored `.env` — never
> in a tracked file. This runbook references the env-var keys, never a real value.

## The knobs

| Item | Value |
| --- | --- |
| Env var | `HERMES_VOIP_OUTBOUND_ALLOW` |
| Type | comma-separated list of dial targets (extensions and/or SIP URIs); exact by default. The ONLY wildcard is `x`/`X` (one digit) inside a simple extension mask, e.g. `10xx` = 1000–1099. `*` is a **literal** dial char (so `*67` is exact; `10**` is literal — use `10xx`); SIP URIs match verbatim. No `.*` glob is compiled (no host-swallow, no ReDoS). |
| Default | **empty** → no outbound call is permitted (the feature is **inert**) |
| Read by | `hermes_voip.outbound_allow.load_outbound_allowlist` (called at `connect()`) |
| Enforced at | `VoipAdapter.place_call_with_objective` — the dial chokepoint, BEFORE any INVITE |
| On an unlisted target | raises `OutboundCallNotAllowed`; the tool returns a clear error; nothing is dialled |

| Item | Value |
| --- | --- |
| Env var | `HERMES_VOIP_CALL_ON_CONNECT` (optional) |
| Type | extension number or SIP URI string |
| Default | unset → no dial-on-connect |
| Read by | `VoipAdapter._establish` (after first successful registration) |
| **Security** | **BYPASSES the `HERMES_VOIP_OUTBOUND_ALLOW` allowlist** — this is the operator's own explicit one-shot dial, so the allowlist gate is intentionally skipped. Only set this if you understand and accept the bypass. |
| Re-trigger | the flag is permanent once set; reconnects do NOT re-fire the dial. |

| Item | Value |
| --- | --- |
| Env var | `HERMES_VOIP_OUTBOUND_RESULT_CHANNEL` (optional) |
| Type | exact `platform:chat_id` fixed target, or a wildcard pattern like `telegram:*` that derives the destination from a matching origin |
| Default | unset → a no-origin call's outcome is **logged only** |
| Read by | `VoipAdapter._report_to_fallback_channel` (at call end) |
| Used when | the call had **no originating session** (the `HERMES_VOIP_CALL_ON_CONNECT` / cron path) |

| Item | Value |
| --- | --- |
| Env var | `HERMES_VOIP_PROACTIVE_CALL_FROM` (optional, issue #202) |
| Type | comma-separated list of `platform:chat_id` operator origins; exact by default, `*` glob opt-in (for example `telegram:*`) |
| Default | **unset** → proactive `place_call` from a non-VoIP session is **blocked** (fully fail-safe) |
| Read by | `hermes_voip.voip_tools._proactive_place_call_allowed` (at the `pre_tool_call` gate) |
| Effect | a `place_call` (ONLY) from a listed origin, with **no live SIP call** in scope, resolves operator privilege (level 3) instead of the fail-safe level 0 |

| Item | Value |
| --- | --- |
| Env var | `HERMES_VOIP_RING_TIMEOUT_SECS` (optional, ADR-0086) |
| Type | positive float (seconds); max 3600 |
| Default | **unset** → no automatic ring-timeout (the adapter's hard sink timeout governs) |
| Read by | `hermes_voip.voip_tools._parse_ring_timeout` (at `place_call` invocation) |
| Effect | when set, arms `VoipAdapter._ring_timeout` which cancels an unanswered outbound INVITE after this many seconds; **TLS transport only** — raises `NotImplementedError` immediately on a WSS gateway (see §Aborting below) |

A call triggered by an **agent turn** always reports its outcome back to that originating
session (captured from `gateway.session_context` at trigger time) — the result channel is the
fallback ONLY for the env-trigger/cron path, because voip has no home channel of its own and a
proactive notification cannot be delivered into voip.

## The `place_call` structured result contract (ADR-0086)

`place_call`'s JSON tool result is a **stable contract** the calling agent branches on
(`hermes_voip.voip_tools.place_call_handler`, `PlaceCallOutcome`):

| Result shape | When |
| --- | --- |
| `{"call_id": …}` | Returned **immediately** on a successful dial. The call runs as its own background conversation — this does NOT wait for the call to finish. |
| `{"error": …}` (no `failure_outcome` key) | Refused **before** any INVITE was sent: no adapter in scope, a missing `number`/`objective` argument, or the target is not on `HERMES_VOIP_OUTBOUND_ALLOW` (`OutboundCallNotAllowed`). |
| `{"error": …, "failure_outcome": <value>}` | The INVITE was sent but the call did not connect. `<value>` is a `PlaceCallOutcome` string the agent can branch on. |

`failure_outcome` values (`hermes_voip.voip_tools.PlaceCallOutcome`):

| Value | Meaning | Trigger |
| --- | --- | --- |
| `busy` | Callee is busy | SIP `486 Busy Here` / `600 Busy Everywhere` |
| `no_answer` | Not answered | SIP `408` / `487` timeout, **or** our own `HERMES_VOIP_RING_TIMEOUT_SECS` CANCEL (ADR-0069, §Aborting above) — a self-inflicted ring timeout is reported the same as a peer no-answer |
| `declined` | Callee explicitly rejected | SIP `603 Decline` |
| `failed` | Any other final non-2xx response, or a transport/media-init error | e.g. unclassified `4xx`/`5xx`/`6xx`, or the RTP transport failed to open (`RuntimeError`) |

The gateway-controlled SIP reason phrase and any transport error message are **never** echoed
into the tool result or the agent-facing error string — only the typed `failure_outcome` value
and a generic message — so a registrar/gateway host, extension, or other PII embedded in a
reason phrase cannot reach the model or a downstream log the agent can read (rule 34). A
transport/media-init failure is still logged to the **operator's own** logs, but only the
exception's type name, never its message (`voip_tools.py` `place_call_handler`).

## Security model (why the allowlist is the gate)

- `place_call` is **`ToolRisk.IRREVERSIBLE`** and clamped by the `pre_tool_call` gate to an
  **operator** (privilege level 3), **non-degraded** session. An untrusted inbound caller
  (level 0/2) — or a session degraded by a fail-open injection screen — can never trigger an
  outbound call, even if a prompt injection coaxes the model into calling the tool.
- The **hard** gate is `HERMES_VOIP_OUTBOUND_ALLOW`. It stands in for the ADR-0010 DTMF
  confirmation an IRREVERSIBLE tool would otherwise require: a static, operator-curated
  allowlist is **more** spoof-resistant than an in-band DTMF tone the remote party shares the
  channel with. Matching is **exact by default** (after trimming) — a listed `1000` does NOT also permit
  `10000`. The ONLY wildcard is `x`/`X` inside a simple extension mask, **opt-in per
  entry**: each `x`/`X` matches exactly one digit, so `10xx` allows `1000`..`1099` and
  rejects `10`, `100`, `10000`, and `10ab`. `*` is a **literal** dial character, NOT a
  wildcard — so a star/service code like `*67` is an exact entry (it matches only `*67`,
  never `067`..`967`), and the `10**` spelling is a literal string, not a mask alias (use
  `10xx` for the range). SIP URIs and any other non-mask entry are exact-only; no entry
  ever compiles to a `.*` glob, so a URI entry cannot over-match a different host and there
  is no ReDoS surface. This keeps the ADR-0021 escalation lesson intact for exact entries
  while still allowing concise, deliberate `x`-masks (ADR-0029 §2a). NOTE: `*` is a glob
  ONLY in `PROACTIVE_CALL_FROM` / `OUTBOUND_RESULT_CHANNEL` (those use `fnmatch` — see
  below); in this dial allowlist `*` is always literal.
- The callee is **untrusted**: the resulting call runs unprivileged (the OUTBOUND persona,
  `privilege_level=0`), so the call agent cannot itself place a further call or transfer, and
  the **objective must not contain operator secrets** (it is pursued with the untrusted callee).
- **Proactive `place_call` from an operator chat** (`HERMES_VOIP_PROACTIVE_CALL_FROM`, issue
  #202): by default `place_call` is unreachable from a **non-VoIP** session (e.g. a Telegram
  turn), because with no live SIP call in scope the gate fails safe to level 0. This opt-in
  env relaxes that **only** for a place_call whose **originating** `(platform, chat_id)`
  matches an explicit list entry or wildcard pattern — and **only** `place_call`
  (transfer/dtmf/open_entry stay blocked, being
  meaningless without a live call). It does **not** touch the **inbound** fail-safe: a live
  call still resolves its real caller-group privilege, so a spoofed/prompt-injected caller is
  unaffected. The `HERMES_VOIP_OUTBOUND_ALLOW` allowlist above **still** gates the dial target
  at the chokepoint, so even a misconfigured operator origin can only reach pre-approved
  numbers (defense in depth).

## How to set it

Set the env vars where the rest of the `HERMES_VOIP_*` config lives (the gitignored `.env` the
Hermes runtime loads, or the process environment for `hermes gateway run`). Examples
(gitignored `.env`; fakes only — substitute the operator's real approved targets):

```
# Permit outbound calls to two approved extensions, any 10xx internal extension,
# and one SIP URI:
HERMES_VOIP_OUTBOUND_ALLOW=1000,1001,10xx,sip:reception@pbx.example.test

# (Optional) where env-trigger/cron call outcomes are reported:
# exact fixed destination:
HERMES_VOIP_OUTBOUND_RESULT_CHANNEL=telegram:123456789
# OR wildcard-derived destination from a matching origin:
# HERMES_VOIP_OUTBOUND_RESULT_CHANNEL=telegram:*
```

Then redeploy/restart the gateway so the plugin re-reads its config (the allowlist is read at
`connect()`). With `HERMES_VOIP_OUTBOUND_ALLOW` unset/empty the `place_call` tool is registered
but refuses every target — the safe default.

### Proactive outbound from an operator chat (issue #202)

To let the agent originate a call from a **non-VoIP** conversation ("call me with a brief")
you need all three knobs together — the dial allowlist, the trusted origin, and (so the
outcome lands back) the result channel (fakes only; substitute the operator's real values):

```
# The number(s) the proactive call may dial (the chokepoint gate):
HERMES_VOIP_OUTBOUND_ALLOW=1000,10xx,sip:reception@pbx.example.test

# The operator chat(s) allowed to TRIGGER a proactive place_call (platform:chat_id):
# exact:
HERMES_VOIP_PROACTIVE_CALL_FROM=telegram:123456789
# or wildcard-scope any Telegram chat explicitly:
# HERMES_VOIP_PROACTIVE_CALL_FROM=telegram:*

# Where the call outcome is reported when there is no direct origin capture:
# exact fixed destination:
HERMES_VOIP_OUTBOUND_RESULT_CHANNEL=telegram:123456789
# or wildcard-derived destination when the origin matches:
# HERMES_VOIP_OUTBOUND_RESULT_CHANNEL=telegram:*
```

`HERMES_VOIP_PROACTIVE_CALL_FROM` is the **only** new knob; it is read at the `pre_tool_call`
gate (not cached at `connect()`). Unset/empty → proactive `place_call` is blocked exactly as
before (the fully fail-safe default). It permits **only** `place_call` from a listed origin
and **only** when there is no live SIP call in scope; it never affects an inbound call.

## How to verify

1. **Allowlist parse (offline, deterministic):**

   ```
   uv run python -c "from hermes_voip.outbound_allow import load_outbound_allowlist, is_outbound_allowed; \
     a = load_outbound_allowlist({'HERMES_VOIP_OUTBOUND_ALLOW':'1000,10xx'}); \
     print(a == frozenset({'1000'}), is_outbound_allowed('1000', a), is_outbound_allowed('1055', a), is_outbound_allowed('1100', a))"
   ```

   Prints `False True True False` because the allowlist now contains one exact entry and one
   pattern entry (`10xx`, an `x`-digit mask; `*` is literal, so `10**` would be an exact
   literal entry — use `10xx` for the range). An empty/absent value still prints a
   falsey/empty allowlist and denies everything (inert, fail-closed).

2. **Gate + tool behaviour (covered by the test suite):**
   - `uv run pytest tests/test_outbound_allow.py tests/test_wildcard_config.py` — the exact
     allowlist parser, `x`-digit mask entries (`10xx`) and literal `*` entries (`*67`,
     `10**`), fixed vs wildcard result channel resolution, and fail-closed defaults.
   - `uv run pytest tests/test_voip_tools_place_call.py tests/test_wildcard_config.py` — the
     `place_call` / `report_call_result` tools, the IRREVERSIBLE gate (level-0/2/degraded
     blocked, operator level-3 allowed), the unlisted-number refusal, the immediate `{call_id}`
     return, AND the proactive-origin gate (matching origin allowed; wildcard `telegram:*`
     allowed; transfer/dtmf/open_entry still blocked; off-by-default; wrong-origin blocked).
   - `uv run --extra hermes pytest tests/test_adapter_caller_modes.py -k "objective or origin or first_turn or report"`
     — the objective in the outbound preamble + injected as the call's first turn, and the
     outcome reported into the ORIGIN session (success + failure-fallback; no-origin path).

3. **Live (pending operator redeploy + an allowlist entry):** from a trusted operator
   conversation, ask the agent to call an approved number with an objective. Confirm the
   operator log shows `agent place_call tool: dialling <number> (origin=present)`, the call
   connects and the agent opens with the objective, and at end the originating conversation
   receives `[Outbound call to '<number>' ended (…): <summary>]`. Dialling an **un-approved**
   number returns an error to the agent and sends no INVITE.

## Aborting a ringing outbound call (CANCEL — ADR-0069, RFC 3261 §9.1)

This outbound CANCEL path is currently implemented for the **TLS SIP UAC**. On the
**WSS/WebRTC UAC**, `send_cancel()` is a no-op and `ring_timeout_secs` is rejected up front with
`NotImplementedError`; see the WSS subsection below before assuming uniform behaviour.

On the TLS path, a ringing outbound call (INVITE sent, no 2xx yet) can be cancelled in two ways:
an explicit agent tool call (`abort_call`) or an automatic ring-timeout. Both paths aim to cancel
the unanswered INVITE; when the gateway returns `487 Request Terminated`, the caller sees
`OutboundCallCancelled`.

The implementation detail that matters for RFC 3261 §9.1 is this: the transport tracks outbound
INVITEs as soon as they are sent and `abort_call()`/`send_cancel()` do **not** wait for a logged
`100 Trying`/`180 Ringing`/other provisional response before attempting CANCEL. The code does skip
provisional responses while awaiting the INVITE's final response, but this runbook does **not**
claim a verified provisional-response guard before sending CANCEL.

### Explicit abort via `abort_call` (TLS path)

`VoipAdapter.abort_call(call_id, reason)` (adapter.py line 2387) looks up the pending INVITE
by Call-ID, marks it `cancel_requested`, and calls `transport.send_cancel(call_id)` on the TLS
connection (transport/connection.py line 418). The `reason` string is logged but not sent to the
peer.

Log lines to watch:

```
abort_call: CANCEL sent for ringing call <call-id> (<reason>)
```

If the call has already answered or finished (no ringing entry found):

```
abort_call: no ringing outbound call <call-id> — no-op
```

If `abort_call` is called twice concurrently on the same call:

```
abort_call: <call-id> already cancelling — no-op
```

### Automatic ring-timeout (TLS path)

When `place_call` is invoked with `ring_timeout_secs` set, `VoipAdapter._ring_timeout`
(adapter.py line 2443) fires after the specified interval and calls `abort_call` internally with
`reason="ring timeout"`.

Log line emitted by the timeout path before it calls `abort_call`:

```
outbound <call-id>: ring timeout (<N.1>s) — cancelling the unanswered call
```

This is then followed by the normal `abort_call` log line above.

The ring-timeout task is armed at INVITE send time and disarmed the instant a 2xx arrives, so a
call that answers just before the timer fires produces only the disarm (no CANCEL).

### WSS (WebRTC) transport — outbound CANCEL/ring-timeout are not supported

`ring_timeout_secs` is **not supported on a WSS gateway** (`HERMES_SIP_TRANSPORT=wss`). The WSS
transport's `send_cancel` (transport/ws_connection.py line 316) is a no-op that returns `False`
because the WebRTC origination path has no RFC 3261 client-transaction registry to build a §9.1
CANCEL from. Passing `ring_timeout_secs` on a WSS gateway raises `NotImplementedError` immediately
at `place_call` (adapter.py line 1634), before any INVITE is sent.

That means the outbound abort behaviours documented above are **TLS-only today**. On WSS/WebRTC:

- `abort_call()` can mark the call as cancelling, but `send_cancel()` itself does not emit a SIP
  CANCEL on the wire.
- automatic ring-timeout is rejected before dial, via `NotImplementedError`.

Do not set `ring_timeout_secs` on a WSS transport; use application-level timeout logic instead.

### RFC 3261 §9.1 note: provisional-response timing

This runbook cites RFC 3261 §9.1 because the feature is an outbound SIP CANCEL path, but the code
verification here found no explicit guard that waits for a received provisional response before
sending CANCEL. The TLS transport tracks outbound INVITEs when they are sent and can attempt
`send_cancel(call_id)` from that tracked state alone. So the verified statement is: the code
implements a CANCEL attempt for an unanswered outbound TLS INVITE, and the caller-side await loop
continues past provisional responses until it receives the INVITE's final response (for example the
`487 Request Terminated` after a successful cancel).

Do not read this section as a verified claim that the implementation enforces every RFC timing
precondition before transmitting CANCEL; that specific provisional-response guard was not present in
code.

## Transport: how the dial goes out (ADR-0049)

`place_call` picks the outbound media/signalling shape from the gateway transport
(`HERMES_SIP_TRANSPORT`), with no extra knob:

- **`tls`** (SIP-over-TLS): the existing SDES UAC — an INVITE with a `TLS` Via and an
  SDES/G.711-G.722 (+ Opus when `libopus` is loadable, ADR-0049) offer.
- **`wss`** (SIP-over-Secure-WebSocket): a **WebRTC** UAC — an RFC-7118 INVITE with a `WSS`
  Via carrying OUR DTLS/ICE/Opus offer (ICE-controlling, `a=setup:active` = DTLS client). It
  needs the `webrtc` extra + system `libopus` (a WebRTC call mandates Opus); without them the
  dial fails cleanly (`OutboundCallFailed(488)`) before any INVITE. This lifted the prior
  `501 "outbound not supported on WSS"` reject.

The allowlist + privilege gate above apply identically on both transports.

## Rotation / change

To add or remove an approved target, edit `HERMES_VOIP_OUTBOUND_ALLOW` in the gitignored `.env`
(or 1Password, if the operator keeps the list there) and redeploy. To change where env-trigger
outcomes go, edit `HERMES_VOIP_OUTBOUND_RESULT_CHANNEL` and redeploy. To add/remove an operator
chat allowed to trigger a proactive call, edit `HERMES_VOIP_PROACTIVE_CALL_FROM` (it is read at
the gate, so a restart that re-reads the environment suffices).

## Rollback (disable the feature)

Unset `HERMES_VOIP_OUTBOUND_ALLOW` (or set it empty) and redeploy. The `place_call` tool is
still registered but refuses every target — the feature is inert with no agent-initiated dial
possible, exactly the default-shipped state.

To disable **only** proactive outbound (keep in-call `place_call`), unset
`HERMES_VOIP_PROACTIVE_CALL_FROM`: a `place_call` from a non-VoIP session then falls back to
the fail-safe level-0 block, byte-identical to the pre-#202 behaviour.
