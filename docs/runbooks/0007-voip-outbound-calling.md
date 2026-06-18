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
| Type | comma-separated list of dial targets (extensions and/or SIP URIs) |
| Default | **empty** → no outbound call is permitted (the feature is **inert**) |
| Read by | `hermes_voip.outbound_allow.load_outbound_allowlist` (called at `connect()`) |
| Enforced at | `VoipAdapter.place_call_with_objective` — the dial chokepoint, BEFORE any INVITE |
| On an unlisted target | raises `OutboundCallNotAllowed`; the tool returns a clear error; nothing is dialled |

| Item | Value |
| --- | --- |
| Env var | `HERMES_VOIP_OUTBOUND_RESULT_CHANNEL` (optional) |
| Type | a single `platform:chat_id` target (split on the FIRST `:`) |
| Default | unset → a no-origin call's outcome is **logged only** |
| Read by | `VoipAdapter._report_to_fallback_channel` (at call end) |
| Used when | the call had **no originating session** (the `HERMES_VOIP_CALL_ON_CONNECT` / cron path) |

A call triggered by an **agent turn** always reports its outcome back to that originating
session (captured from `gateway.session_context` at trigger time) — the result channel is the
fallback ONLY for the env-trigger/cron path, because voip has no home channel of its own and a
proactive notification cannot be delivered into voip.

## Security model (why the allowlist is the gate)

- `place_call` is **`ToolRisk.IRREVERSIBLE`** and clamped by the `pre_tool_call` gate to an
  **operator** (privilege level 3), **non-degraded** session. An untrusted inbound caller
  (level 0/2) — or a session degraded by a fail-open injection screen — can never trigger an
  outbound call, even if a prompt injection coaxes the model into calling the tool.
- The **hard** gate is `HERMES_VOIP_OUTBOUND_ALLOW`. It stands in for the ADR-0010 DTMF
  confirmation an IRREVERSIBLE tool would otherwise require: a static, operator-curated
  allowlist is **more** spoof-resistant than an in-band DTMF tone the remote party shares the
  channel with. Matching is **exact** (after trimming) — a listed `1000` does NOT also permit
  `10000`; there are no prefix wildcards on this trust-granting list (ADR-0021 lesson).
- The callee is **untrusted**: the resulting call runs unprivileged (the OUTBOUND persona,
  `privilege_level=0`), so the call agent cannot itself place a further call or transfer, and
  the **objective must not contain operator secrets** (it is pursued with the untrusted callee).

## How to set it

Set the env vars where the rest of the `HERMES_VOIP_*` config lives (the gitignored `.env` the
Hermes runtime loads, or the process environment for `hermes gateway run`). Examples
(gitignored `.env`; fakes only — substitute the operator's real approved targets):

```
# Permit outbound calls to two approved extensions and one SIP URI:
HERMES_VOIP_OUTBOUND_ALLOW=1000,1001,sip:reception@pbx.example.test

# (Optional) where env-trigger/cron call outcomes are reported:
HERMES_VOIP_OUTBOUND_RESULT_CHANNEL=telegram:123456789
```

Then redeploy/restart the gateway so the plugin re-reads its config (the allowlist is read at
`connect()`). With `HERMES_VOIP_OUTBOUND_ALLOW` unset/empty the `place_call` tool is registered
but refuses every target — the safe default.

## How to verify

1. **Allowlist parse (offline, deterministic):**

   ```
   uv run python -c "from hermes_voip.outbound_allow import load_outbound_allowlist, is_outbound_allowed; \
     a = load_outbound_allowlist({'HERMES_VOIP_OUTBOUND_ALLOW':'1000, 1001'}); \
     print(sorted(a), is_outbound_allowed('1000', a), is_outbound_allowed('9999', a))"
   ```

   Prints `['1000', '1001'] True False`. An empty/absent value prints `[] False False` (inert).

2. **Gate + tool behaviour (covered by the test suite):**
   - `uv run pytest tests/test_outbound_allow.py` — the allowlist parser + default-empty.
   - `uv run pytest tests/test_voip_tools_place_call.py` — the `place_call` / `report_call_result`
     tools, the IRREVERSIBLE gate (level-0/2/degraded blocked, operator level-3 allowed), the
     unlisted-number refusal, and the immediate `{call_id}` return.
   - `uv run --extra hermes pytest tests/test_adapter_caller_modes.py -k "objective or origin or first_turn or report"`
     — the objective in the outbound preamble + injected as the call's first turn, and the
     outcome reported into the ORIGIN session (success + failure-fallback; no-origin path).

3. **Live (pending operator redeploy + an allowlist entry):** from a trusted operator
   conversation, ask the agent to call an approved number with an objective. Confirm the
   operator log shows `agent place_call tool: dialling <number> (origin=present)`, the call
   connects and the agent opens with the objective, and at end the originating conversation
   receives `[Outbound call to '<number>' ended (…): <summary>]`. Dialling an **un-approved**
   number returns an error to the agent and sends no INVITE.

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
outcomes go, edit `HERMES_VOIP_OUTBOUND_RESULT_CHANNEL` and redeploy.

## Rollback (disable the feature)

Unset `HERMES_VOIP_OUTBOUND_ALLOW` (or set it empty) and redeploy. The `place_call` tool is
still registered but refuses every target — the feature is inert with no agent-initiated dial
possible, exactly the default-shipped state.
