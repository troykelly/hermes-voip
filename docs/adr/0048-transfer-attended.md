# ADR-0048: Attended (consultative) transfer — `transfer_attended` tool

- **Date:** 2026-06-18
- **Status:** Accepted (amends ADR-0031 §4 "transfer_attended deferred"; builds on
  ADR-0010 / ADR-0011 transfer, ADR-0019/0029 outbound origination)
- **Deciders:** agent session (attended-transfer lane) — operator "no deferrals" push

## Context

The plugin already ships **blind** transfer (`transfer_blind`, ADR-0010/0031): a REFER
(RFC 3515) on the live call that hands the caller to a destination, with a spoof-resistant
DTMF confirmation as the irreversibility safeguard. The sans-IO **attended**-transfer wire
layer was also already complete and unit-tested:

- `hermes_voip.refer.build_attended_refer(dialog, consult, …)` — a REFER on the primary
  call whose `Refer-To` embeds an escaped `Replaces` (RFC 3891) naming the consultation
  dialog (with the load-bearing RFC 3891 §3 tag orientation: `to-tag = consult.remote_tag`,
  `from-tag = consult.local_tag`); and
- `hermes_voip.call.CallSession.transfer_attended(consult: Dialog, …)` — sends that REFER on
  the primary dialog and reports progress via NOTIFY.

What was missing — and the reason ADR-0031 §4 kept the tool **deferred-not-registered** —
was an **agent-driven consultation leg**: an attended transfer first calls the target,
lets the agent converse, and only then completes. With ADR-0019/0029 the plugin now has a
full outbound origination path (`VoipAdapter.place_call`), so the consult leg can be
originated and the gap closes. Registering the tool is now wiring real behaviour, not a
lying stub (rule 6).

## Decision

Ship `transfer_attended` as **one agent tool driving a three-state machine** keyed by a
required `action` argument, backed by three host methods on `VoipAdapter`:

| `action`   | Host method                          | What it does |
| ---------- | ------------------------------------ | ------------ |
| `consult`  | `start_attended_consult(call_id, target)` | Originate the consultation leg to `target` via the existing outbound path; record the pairing `original → consult`. Returns the consult Call-ID. |
| `complete` | `complete_attended_transfer(call_id)`     | Send `build_attended_refer` on the ORIGINAL call naming the consult leg's `Dialog` (REFER+Replaces, RFC 3891); the gateway bridges the caller to the target and releases our legs. Clears the pairing. |
| `cancel`   | `cancel_attended_transfer(call_id)`       | Abandon the consultation: hang up the consult leg (BYE), keep the original caller, clear the pairing. |

States modelled cleanly: **consulting** (a pairing exists) → **completing** (REFER sent,
pairing cleared) or **cancel** (consult hung up, pairing cleared). The pairing lives in
`VoipAdapter._attended_consults: dict[str, str]` (original Call-ID → consult Call-ID),
keyed by the agent's own call (its `chat_id == Call-ID`), so the model can only act on the
call whose turn is being processed — concurrency-safe across simultaneous calls.
**One consultation per original call:** `start_attended_consult` refuses a second `consult`
while one is already paired for the call (reserving the pairing slot with a sentinel
**before** the dial `await`, rolled back if the dial fails), so the read→`await
place_call`→write window cannot let a second action overwrite the pairing and orphan the
first consult leg. A `RuntimeError` is raised (and rendered as a tool error); the operator
must `complete` or `cancel` the first consultation before starting another.

### Security posture

`transfer_attended` is **IRREVERSIBLE** — operator-only (privilege level 3) and
non-degraded, exactly the `transfer_blind` / `place_call` posture. It is already classified
`ToolRisk.IRREVERSIBLE` in `tools.py`, so the `pre_tool_call` gate clamps it. Two
additional hard gates, enforced **at the chokepoint** (defense in depth, mirroring
`transfer_blind_on_call`), not solely at the sync gate:

1. **Outbound allowlist (the consult-leg gate).** The consultation dials a **new outbound
   leg to an untrusted target**, exactly the `place_call` threat model — a prompt injection
   could otherwise make the agent dial an arbitrary number and bridge the caller to it. So
   `start_attended_consult` enforces the **same** `HERMES_VOIP_OUTBOUND_ALLOW` allowlist as
   `place_call` (raising `OutboundCallNotAllowed` before any INVITE). This is the key
   difference from `transfer_blind`, whose target is the live-call DTMF confirmation's
   authorization; an attended transfer's first action is an outbound dial, so it reuses the
   dial gate. (The completing REFER+Replaces only bridges the already-allowlisted consult
   leg, so the allowlist on `consult` covers the whole flow.)
2. **Privilege re-check at the chokepoint.** `start_attended_consult` and
   `complete_attended_transfer` re-run the operator-level + non-degraded clamp themselves
   (raising `PermissionError` / returning `BLOCKED`), so a session that lost privilege or
   went `degraded` cannot start or complete a transfer even if the sync gate were bypassed.

### Always-JSON, never-raise handler contract

`transfer_attended_handler` returns a JSON string on success AND every error and never
raises (the house tool contract; accepts `**kwargs`). Outcomes map to distinct messages so
the model can tell them apart: an unlisted target, a missing consult, a blocked privilege,
a completed transfer. The `consult` path additionally renders the consult dial's failure
modes as tool errors rather than letting them escape: `OutboundCallFailed` (a busy outbound
slot / no registered extension / the WSS transport, inherited from `place_call`) and the
`RuntimeError` paths (an un-initialised transport, or a second consultation refused while
one is in flight) all map to `{"error": …}`. The host's `complete_attended_transfer`
returns an `AttendedTransferOutcome` enum (`TRANSFERRED` / `NO_CONSULT` / `NO_CALL` /
`BLOCKED`); a gateway REFER rejection propagates as `CallError` and is caught
**specifically** (not as a bare `RuntimeError`, so an unrelated bug still propagates —
rule 37) and rendered as a clear tool error.

The tool is registered (`provides_tools` in `plugin.yaml`, guarded by the manifest
drift-guard test) and owned by the `pre_tool_call` gate (`_voip_tool_names`).

## Scope / deferred (rule 6)

- **The consult leg runs as its own background conversation** (the `place_call` model: one
  call = one Hermes session). The agent on the original call triggers `consult`/`complete`
  via the tool; full duplex three-way *mixing* (the agent simultaneously bridging audio
  between both legs in-process) is **not** in scope — the gateway bridges the two legs when
  the REFER+Replaces completes, which is the standard SIP attended-transfer outcome. The
  consult leg is dialled with a consultation objective brief.
- **No new env var, no new dependency.** The flow reuses the existing outbound path,
  allowlist, and sans-IO REFER/Replaces builder.
- **Live gateway validation** of the full consult → REFER+Replaces handshake against the
  test PBX is an operator step (the lane lands code + unit/adapter evidence), the same
  posture as outbound calling (ADR-0029) and WebRTC media (ADR-0043).

## Consequences

- The last deferred transfer ships: the agent can now place a consultation call, talk to
  the target, and complete a consultative bridge — or cancel and return to the caller.
- ADR-0031 §4's "transfer_attended deferred (no consult-leg origination)" is resolved; the
  manifest and `register()` now expose both transfer tools.
- The outbound allowlist gains a second consumer; an operator who has not opted any numbers
  into `HERMES_VOIP_OUTBOUND_ALLOW` cannot start an attended transfer (the feature is inert
  by default, exactly like `place_call`).

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| Two separate tools (`consult` + `complete_transfer`) | A single `action`-discriminated tool models the state machine more legibly and keeps the schema/gate surface to one tool. |
| Allow-list the *completing* REFER target instead of the consult dial | The untrusted new leg is created at `consult` time; gating there covers the whole flow and matches the `place_call` threat model. The completing REFER only bridges the already-allowlisted leg. |
| Reuse `transfer_blind`'s DTMF confirmation instead of the outbound allowlist | An attended transfer's first action is dialling a new outbound party (no human yet on that leg to press a key); the allowlist is the right per-target authorization, identical to `place_call`. |
| Keep it deferred until in-process 3-way audio mixing exists | The gateway performs the bridge on REFER+Replaces (standard SIP attended transfer); in-process mixing is not required for a correct consultative transfer. |
