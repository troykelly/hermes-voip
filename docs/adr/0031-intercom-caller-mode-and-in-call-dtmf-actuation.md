# ADR-0031: Intercom caller mode + in-call DTMF actuation

- **Date:** 2026-06-17
- **Status:** Accepted
- **Deciders:** operator (`troy@…`) + agent session

## Context

Two capabilities were missing from the live plugin, both turning on one mechanism —
transmitting DTMF on an established call:

1. **In-call DTMF send.** The RFC 4733 telephone-event GENERATOR exists and is
   unit-tested (`src/hermes_voip/dtmf.py`: `event_payloads`, `DtmfEvent`,
   `DtmfReceiver`), but nothing transmitted it on a live call. An agent could not
   navigate an IVR ("press 1 for…") or enter a code, and ADR-0010's designed
   `send_dtmf(digits)` path (ADR-0010 §"Generation") was never wired into
   `RtpMediaTransport`.

2. **An intercom (door / gate) caller mode.** The operator wants the agent to answer
   a door intercom, screen the visitor, and — for a legitimate expected visitor —
   open the entry. The actuation is gateway/site-specific: some door phones open on
   an in-band DTMF code ("press 9 to open"); others expose a network relay / smart
   lock. The operator's instruction was explicit: *"not sure, build both."*

The binding constraints: caller-ID is forgeable and is **not** an auth boundary
(ADR-0020/0021); opening a door is **physical access**, so a spoofed caller-ID
reaching the intercom must reach the entry action and **nothing else** — never an
operator tool or a secret; the repo is **PUBLIC** (no SIP host / number / token in a
tracked file); and the existing privilege gate (ADR-0009/0021) is the single
enforcement path — no parallel policy system.

ADR-0021 §1 deferred an optional per-group `allowed_tools` allowlist to "Phase 2"; the
intercom least-privilege requirement is exactly the case that needs it, so this ADR
promotes it to shipped.

## Decision

Ship three things; defer the fourth with a named blocker.

### 1. `allowed_tools` sub-ceiling (promotes ADR-0021 §1's deferred allowlist)

`CallerGroup` gains `allowed_tools: frozenset[str]` (default empty). It is carried onto
the per-call `GuardSessionState.allowed_tools` at INVITE time and read by
`gate_voip_tool(tool_name, state, *, confirmed)` (the single name-aware chokepoint that
both `voip_pre_tool_call` and `CallControlTools` flow through):

- **Empty** ⇒ no sub-ceiling: the privilege LEVEL alone gates (every existing decision
  is byte-for-byte unchanged; the ADR-0020/0021 privilege-clamp tests stay green).
- **Non-empty** ⇒ a tool not in the set is blocked **before** the level/risk check. The
  risk lookup runs first, so an UNKNOWN tool stays denied even if listed (fail closed,
  rule 37). The set can only **REMOVE** tools — a listed tool still has to pass the
  level/risk clamp, so the allowlist never grants a tool **above** the level.

The N-group JSON document parses an optional per-group `allowed_tools` array; a non-list
value is a fail-loud `ConfigError`.

### 2. `engine.send_dtmf` — RFC 4733 TX on the active call (wires ADR-0010 §Generation)

`RtpMediaTransport.send_dtmf(digits, *, tone_ms=100, gap_ms=70, volume=10)` emits the
generator's named-event payloads on the active call's RTP stream:

- the **negotiated** telephone-event payload type — a new
  `telephone_event_payload_type` engine field set from the SDP-agreed
  `telephone-event` codec (`_telephone_event_payload_type(agreed)` in the adapter,
  threaded at both inbound and outbound engine construction; outbound adopts the 2xx
  answer's PT). **Raises** when it was not negotiated — never a hardcoded `101`, never a
  silent no-op;
- **marker bit** on the first packet of each digit; a **constant RTP timestamp** across
  one digit's packets (duration grows in the payload, not the RTP clock); the digit's
  timestamp **advances** by the tone duration per digit so a repeated digit is a
  distinct event; **monotonic sequence** shared with the audio stream; the engine
  `_OUTBOUND_SSRC`; **SRTP protection** when active; the **three redundant end packets**
  (RFC 4733 §2.5.1.4, already produced by `event_payloads`); `ptime` pacing + an
  inter-digit gap;
- under a new engine **`_tx_lock`** (`asyncio.Lock`) that also wraps `send_audio`'s
  drain loop, so the two TX coroutines that await between per-packet sends never
  interleave packets nor race `_seq`/`_ts`/`_outbound_addr`. (The synchronous flushes —
  `stop()`/`_flush_tx_tail`, the barge-in `flush_outbound` — emit without awaiting
  between packets, so they are already atomic relative to these two and need no lock.)

The agent gets a gated `send_dtmf(digits)` tool (`TOOL_RISKS["send_dtmf"] = ELEVATED`):
reversible (a tone) but a mutating action an untrusted (level-0) caller must not invoke.
The handler reports the engine's not-negotiated / invalid-digit failures as clear tool
errors and never echoes the digits (a DTMF string can carry a PIN / card number — never
logged or returned).

### 3. Intercom caller mode + `open_entry`

- An **`intercom` persona** preamble (spotlighted, untrusted-data-fenced): screen the
  visitor, open the door **only** for a legitimate expected visitor, disclose nothing
  else; it names **only** `open_entry` + `hang_up` (rule 27).
- An **`open_entry` tool** (`TOOL_RISKS["open_entry"] = ELEVATED`) and an intercom
  caller group configured at **privilege_level 2** with
  **`allowed_tools = {"open_entry"}`** (or `{"send_dtmf"}` if the site prefers the raw
  tool). The least-privilege guarantee is by construction: a spoofed caller-ID landing
  in the intercom group gets ONLY the entry action — the level-2 + sub-ceiling gate
  removes `hold_call`, `list_registrations`, `send_dtmf`, `place_call`, everything else.
- **Both actuation paths**, chosen by `HERMES_VOIP_INTERCOM_OPEN_MODE`
  (`src/hermes_voip/intercom.py`):
  - **`dtmf`** → `open_entry` sends the configured open code (`HERMES_VOIP_INTERCOM_DTMF`,
    e.g. `9`) via `send_dtmf` on the live call;
  - **`relay`** → `open_entry` calls a dependency-free `IntercomRelayClient` that POSTs
    to `HERMES_VOIP_INTERCOM_RELAY_URL` (https-only, so the bearer token never travels
    cleartext) with `Authorization: Bearer <HERMES_VOIP_INTERCOM_RELAY_TOKEN>`, off the
    event loop (`asyncio.to_thread` over stdlib `urllib`). The token is read from
    env/1Password, never committed, `repr=False` on the config, and never logged; a
    non-2xx / network failure raises `IntercomRelayError` carrying only the status /
    reason class (no token, no URL).
  - **default `disabled`** ⇒ `open_entry` **raises** (rule 37 — opening a door is never a
    silent no-op, and a misconfiguration never silently opens one either).

### 4. DTMF-receive armed-confirmation resolver + transfer — DEFERRED (named blocker)

Wiring `DtmfReceiver` into the **inbound** path (an `on_dtmf` callback emitted from
`engine._inbound_gen` before the audio decode, since telephone-event packets ride a
different payload type than the decoded voice stream) plus an armed-confirmation state
machine that feeds `confirmed=True` to the ADR-0010 gate — and thereby registering the
deferred `transfer_blind` tool — is **not** shipped here. Two reasons:

1. it is a separate subsystem (inbound media-path change + a confirmation state machine)
   whose partial landing would violate rule 6; and
2. `transfer_attended` is **uncarriable** regardless — it needs a consultation `Dialog`
   the agent cannot produce (no consultation-leg origination path exists), per the
   ADR-0011 / PR #96 finding.

`transfer_blind` therefore stays deferred-not-registered (registering it with a
model-untrusted `confirmed=False` would be an always-blocked no-op). This is tracked as a
clean follow-up; the `send_dtmf` TX path shipped here is the prerequisite the resolver
will build on (the receive half + the gate-resolve are what remain).

## Consequences

- **In-call DTMF works end-to-end** for IVR navigation and keypad entry, on any gateway
  that negotiates `telephone-event` (the engine raises a clear error on one that does
  not, rather than silently dropping tones).
- **An intercom agent can open a door** for an expected visitor on either a DTMF or a
  relay site, and **cannot** do anything else from that call — the strongest
  spoof-resistance available given a forgeable caller-ID, by construction (level + the
  `allowed_tools` sub-ceiling), not by persona wording.
- **One enforcement path kept.** The sub-ceiling lives in the existing `gate_voip_tool`
  chokepoint; no parallel policy system, and the empty-default keeps every existing
  caller's gating identical.
- **New operational surface to maintain:** the intercom env knobs + the relay endpoint
  (a runbook + `.env.example` ship with this ADR). The relay bearer token is a managed
  secret (1Password) on the standard rotate-and-redeploy cadence (AGENTS.md rule 41).
- **A small RTP-stream contract:** DTMF and audio now share the engine's seq/ts under one
  mutex. A digit consumes an RTP timestamp block (the tone duration) so audio that
  follows stays monotonic.
- **No new third-party dependency** (the relay uses stdlib `urllib`), so the supply-chain
  / licence surface (rule 35) is unchanged.
- **Deferred debt is named, not hidden:** the inbound DTMF-confirmation resolver +
  `transfer_blind`. The blocker (`transfer_attended`'s missing consult leg) is recorded.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Hardcode the telephone-event PT to 101 | Gateways negotiate `telephone-event` at a dynamic PT that is not guaranteed to be 101 (the same dynamic-PT issue G.722 hit in ADR-0022/PR #85); sending 101 while the answer said e.g. 96 means the far end drops every digit. The engine resolves the negotiated PT and raises if absent. |
| Silently no-op `send_dtmf`/`open_entry` when DTMF is not negotiated / not configured | A door that silently fails to open (or DTMF that silently vanishes) is worse than a loud error — the agent cannot tell the visitor the truth, and rule 37 forbids swallowing the failure. Both raise / return a clear tool error. |
| Make `open_entry` IRREVERSIBLE (require ADR-0010 DTMF confirmation) | The spoof-resistant DTMF confirmation channel is not wired (see §4), so an IRREVERSIBLE `open_entry` would be an always-blocked no-op (rule 6). Opening a door for an expected visitor is a reversible courtesy, not a payment; ELEVATED + the `allowed_tools` sub-ceiling + the level-2 intercom group is the right, *shippable* posture. |
| Rely on the persona wording alone to keep the intercom from operator tools | A persona preamble is advisory (ADR-0009); a prompt injection in the visitor's speech could coax the model. The `allowed_tools` sub-ceiling is the enforced boundary — even a fully-subverted model cannot call a tool the gate removes. |
| A separate intercom policy / tool-registry | Duplicates the ADR-0009/0021 gate and risks divergence (the exact failure ADR-0021 warned against). The sub-ceiling reuses the one gate. |
| Add `httpx`/`aiohttp` for the relay POST | A new runtime dependency (licence/advisory/supply-chain surface, rule 35/40) for a single rare POST. Stdlib `urllib` in a worker thread is dependency-free and sufficient. |
| Inline the relay token in an env list / config file | The token is a secret; an inline value leaks into process listings / shell history / the public repo. It is a `*_TOKEN` env var read from 1Password, `repr=False`, never logged. |
| Allow an `http://` relay URL | The `Authorization: Bearer` header would travel in cleartext. `load_intercom_config` requires `https://`. |
| Ship the inbound DTMF-confirmation resolver + `transfer_blind` now | A separate subsystem (inbound media-path change + confirmation state machine) whose partial landing violates rule 6; and `transfer_attended` is uncarriable without a consult-leg origination path. Deferred with the blocker named (§4). |
