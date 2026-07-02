# ADR-0029: Agent-triggered outbound calls — `place_call` tool, per-call objective brief, async cross-session result reporting

- **Date:** 2026-06-17
- **Status:** Accepted (supersedes ADR-0019 §4 + §8 Phase 2; amends ADR-0026: call-end injection now also targets a FOREIGN origin session)
- **Deciders:** agent session (agent-initiated outbound), operator-directed

## Context

ADR-0019 shipped the outbound UAC originate mechanism (`VoipAdapter.place_call`)
and deferred (§4 / §8 Phase 2) the agent-facing gated tool plus an allowlist. Today
the only trigger is the `HERMES_VOIP_CALL_ON_CONNECT` env var (a developer/test
trigger). The operator's goal: **an agent in one conversation (e.g. a Telegram
chat) can decide to place a phone call, have that call run as its own concurrent
conversation, and report the outcome back to the originating conversation** — for
example, "call the restaurant and book a table for two at 7", then tell the user
how it went.

Three facts bound the answer (all source-verified against hermes-agent 0.16.0;
see the `hermes-cross-session-messageevent-injection` / `outbound-trigger-concurrency`
project memory):

1. **Hermes is concurrent.** Each chat is its own session/agent/turn-loop; there
   is no cross-session lock. Each VoIP call is *already* a separate Hermes chat
   (`chat_id` == SIP `Call-ID`, ADR-0002), driven by its own background task. So an
   agent in chat A can trigger a call (chat B) that runs independently and reports
   back. `VoipAdapter.place_call` already returns the `Call-ID` immediately and runs
   the per-call `CallLoop` as a background task (adapter.py).

2. **There is no Hermes primitive for "a tool spawns a new conversation" and none
   for "deliver a result from one conversation into another."** Routing is by
   `event.source` only: `build_session_key(source)` =
   `agent:main:{source.platform.value}:{chat_type}:{chat_id}[…]` (gateway/session.py).
   A `MessageEvent` whose `source.platform`/`chat_id` name a *foreign* session lands
   in that session — `self.platform` is never consulted. This is the exact mechanism
   the gateway's own handoff path uses (`SessionSource(platform=target, …)` +
   `MessageEvent(internal=True)`), and the same mechanism ADR-0026 uses to signal
   call-end into the call's own session. We bridge the cross-conversation gap with it.

3. **There is no Hermes confirmation primitive** for an irreversible tool. The
   plugin's own `ToolRisk`/`gate_tool_call` (ADR-0009/0021) is the only gate, and its
   `IRREVERSIBLE` tier wants a spoof-resistant ADR-0010 DTMF confirmation that is not
   wired into the live adapter. `place_call` cannot depend on a model-set `confirmed`
   flag (a prompt injection would set it).

The operator's standing security directive (caller-modes) applies in both
directions: **the remote party on any call is untrusted, explicitly including the
callee on an outbound call** ("call the restaurant" — the restaurant is untrusted).
Least privilege is primary: an outbound call carries only the task, never operator
secrets, and runs with no privileged tools.

## Decision

Ship the agent-initiated outbound feature in three wired-together parts. It ships
**inert by default** (empty allowlist = no number may be dialled) — the safe ship;
the operator opts numbers in.

### 1. `place_call(number, objective)` agent tool + per-call objective brief

- A new tool `place_call` registered in `register_voip_tools` (mirrors `hang_up`),
  `is_async`, schema `{number: str, objective: str}`. The handler reads the live
  adapter (the existing `set_active_adapter` seam), calls a new
  `adapter.place_call_with_objective(number, objective)`, and returns
  `json.dumps({"call_id": cid})` **immediately** — it does NOT await the whole call.
  `place_call` already returns the `Call-ID` once the `CallLoop` is up and runs the
  loop in the background, so the originating agent's turn is not blocked for the call.

- The `objective` is threaded into `place_call_with_objective` →
  `_handle_outbound_invite` → `_call_info[cid]["objective"]`. It surfaces twice:
  (a) `_deliver_turn` includes it in the OUTBOUND persona preamble (so every turn
  keeps the agent on task), and (b) it is injected as the call session's **first
  turn** (an `internal=True` `MessageEvent` into the call's own session, chat ==
  Call-ID) right after the loop starts — so the call agent *opens* with the goal
  instead of waiting mutely for the callee to speak. This closes the "no greeting /
  why am I calling" outbound gap.

### 2. Gate + allowlist (conservative, safe-by-default)

- `TOOL_RISKS["place_call"] = ToolRisk.IRREVERSIBLE`, and `place_call` is added to
  the VoIP gate's owned-tool set so `voip_pre_tool_call` clamps it. Only an
  operator / privilege-3, clean (non-degraded) session may invoke it — an untrusted
  caller (level 0/2) or a degraded session is blocked, exactly the posture transfer
  has. The privilege clamp uses the IRREVERSIBLE level (3) and the degraded
  hard-block; **the hard irreversibility gate is the allowlist, not DTMF** (a static,
  operator-curated allowlist is *more* spoof-resistant than an in-band DTMF
  confirmation a remote party shares the channel with). The gate therefore evaluates
  `place_call` at its IRREVERSIBLE level with the allowlist standing in for
  confirmation, and never consults a model-set flag.

- A new env var **`HERMES_VOIP_OUTBOUND_ALLOW`**: a comma-separated list of permitted
  dial targets (extensions and/or SIP URIs). **The default is empty = no outbound
  call is permitted** — the feature is inert until the operator opts numbers in. The
  handler rejects any `number` not on the allowlist with a clear error and never
  dials an unlisted target. Matching is **exact by default**; the ONLY wildcard is
  `x`/`X` inside a simple extension mask (an entry of digits, `+`, `#`, `*` and at
  least one `x`/`X`), where each `x`/`X` matches exactly one decimal digit
  (`10xx` = `1000`..`1099`). **`*` is a LITERAL dial character, never a wildcard** — so
  a star/service feature code like `*67` is an EXACT entry (matching only `*67`) and the
  `10**` spelling from issue #355 is a literal string, not a mask alias (use `10xx`).
  SIP URIs and any other non-mask entry are exact-only; no entry ever compiles to a
  `.*` glob (see §2a for why). Extensions-only is the assumed shape; a PSTN target is
  just an allowlist entry if the gateway routes it. The allowlist value lives only in
  the gitignored `.env` (a real number is potentially PII; extensions are not, but the
  rule is uniform).

- We deliberately do **not** rely on a `confirmed: bool` tool argument as a guard
  (a model under prompt injection would set it). The allowlist is the hard gate; the
  IRREVERSIBLE level-3 + non-degraded clamp and the empty default are the safeguards.

### 2a. `OUTBOUND_ALLOW` pattern semantics — `*` is literal; `x`/`X` is the sole mask wildcard (amended 2026-07-02)

Issue #355 added opt-in patterns to `OUTBOUND_ALLOW`. A first cut treated `*` as a
one-digit mask character (and as a `.*` glob in non-mask / URI entries). Cross-tier
review proved that over-matches the DIAL GATE — a security defect:

- `HERMES_VOIP_OUTBOUND_ALLOW="*67"` compiled to `^[0-9]67$`, so it **denied** the
  listed `*67` yet **authorised** `067`..`967` — ten targets the operator never listed
  (all valid dial strings that reach `place_call`). Star/service codes (`*67`, `*82`,
  `*98`) are exactly the digit-shaped entries this misfired on, silently.
- a URI entry like `sip:10*@host` compiled to `^sip:10.*@host$`, which a value such as
  `sip:10@evil.example@host` satisfies (host-swallow); and every `.*` is a ReDoS surface.

**Decision (adjudicated):** in `OUTBOUND_ALLOW`, `*` is a **literal** dial character and
`x`/`X` is the SOLE digit-wildcard, valid ONLY inside a simple extension mask. A mask
compiles to an anchored regex where `x`/`X` → one digit and every other character
(`*` included) is escaped; a non-mask entry (star code, SIP URI, label) is exact-only.
No compiled pattern contains `.*`, so the gate cannot over-match a target or a host and
has no ReDoS surface. The issue's primary `10xx` example is unchanged; the `10**` alias
is dropped (now literal — operators use `10xx`).

**Intentional cross-config divergence:** `PROACTIVE_CALL_FROM` and
`OUTBOUND_RESULT_CHANNEL` keep `fnmatch` glob semantics (`*` = any sequence), because
they select trigger origins / result routing over `platform:chat_id`, not dial targets.
`*` is therefore literal in the dial allowlist but a glob in the trigger/routing configs
— the gate that grants the right to *dial a target* must never over-match, while the
origin/routing configs remain bounded by that same dial gate at the chokepoint.

### 3. Async cross-session result reporting

- **Origin capture at trigger time.** `place_call_with_objective` reads the
  originating session identity from the task-local session context
  (`get_session_env("HERMES_SESSION_PLATFORM" / "HERMES_SESSION_CHAT_ID")`, the same
  API `hang_up` uses for the Call-ID) and stores it as
  `_call_info[cid]["origin"]` (a `(platform, chat_id)` pair). The `HERMES_VOIP_
  CALL_ON_CONNECT` / cron path has no origin (no session in scope) — captured as
  `None`. When a later no-origin fallback channel is configured as a wildcard pattern
  (for example `telegram:*`), delivery is derived from a **matching** origin only;
  with no origin in scope the fallback fails closed to log-only.

- **`report_call_result(summary: str)` tool** for the call agent (session B) to
  record the outcome before hangup → `_call_info[cid]["result"]`. SAFE (a session
  recording its own call's outcome needs no privilege); resolves the call from the
  session context (chat == Call-ID), so a call agent can only report *its own* call.

- **Delivery on call-end.** `_teardown_call` → `_signal_call_end` already injects the
  end signal into the call's OWN session (ADR-0026). This ADR adds: when
  `_call_info[cid]["origin"]` is set, also inject a *second* `MessageEvent` (a
  single-line `[Outbound call to '<callee>' ended (<reason>). Result …]`,
  `internal=True`, source = the origin `SessionSource`) into the ORIGIN session — so
  agent A tells the user how the call went. Built by constructing
  `SessionSource(platform=Platform(origin_platform), chat_id=origin_chat_id, …)`
  directly (`build_source` hard-codes `self.platform`, so it cannot target a foreign
  platform). Failure outcomes (busy / no-answer / declined / pipeline failure) report
  too — the summary defaults to the classified reason when the agent recorded none.

- **The result summary is UNTRUSTED and is sanitised before cross-session injection.**
  The `report_call_result` summary is recorded by the call agent on an untrusted-callee
  call, then injected `internal=True` into the *origin* session — so a hostile callee
  could try to make the summary forge a Hermes command (`/stop`), a control-interrupt
  string, system framing, or the untrusted-data fence, hijacking the origin session.
  `_outbound_result_text` therefore (a) **collapses all whitespace** (newlines/CR/tabs)
  to single spaces so no `\n/command` line can be smuggled, (b) **caps the length**
  (bounds a flooding payload), (c) **defangs the spotlight fence**, and (d) emits the
  whole report as a single `[…]`-bracketed line (so it never begins with `/` and is not
  a command) with the summary **fenced as untrusted DATA** between the
  `UNTRUSTED_CALLER_TRANSCRIPT` markers — exactly one legitimate fence pair the callee
  cannot break out of. The callee identity is sanitised the same way (it too is
  untrusted on an outbound call). This closes the cross-vendor review's one BLOCKING
  finding.

- **No-origin fallback.** When there is no origin (the env-trigger / cron path), VoIP
  has no home channel of its own, so a proactive notification *into* voip is impossible
  (it always fails "No home channel set for voip"). The fallback uses
  `HERMES_VOIP_OUTBOUND_RESULT_CHANNEL` when set, delivered by the SAME
  foreign-session injection the origin report uses (a `MessageEvent` whose `source`
  names the configured channel — the path the built-in `send_message` tool would also
  take). Exact entries (no wildcard) preserve the original behaviour: a fixed
  `platform:chat_id` destination. Wildcard entries (for example `telegram:*`) are
  matched against a captured origin `platform:chat_id`; on a match the destination is
  DERIVED from that origin (so the report lands back in the originating Telegram chat),
  and with no matching origin the fallback fails closed to log-only. The
  `tools.send_message_tool` symbol is *not* imported, because a sibling module in the
  hermes-agent `tools` package has a syntax error mypy cannot parse under
  `follow_untyped_imports`; reusing the `gateway.*`-only foreign-session injection keeps
  the type-check clean. With neither origin nor a configured channel the outcome is
  logged only.

### Hermes gaps recorded (so a future session does not re-derive them)

| Gap (hermes-agent 0.16.0) | Bridge |
| --- | --- |
| No tool→new-conversation primitive | A VoIP call is *already* a separate chat (Call-ID); place_call returns the Call-ID and the loop runs as a background task — the call IS the new conversation. |
| No cross-conversation result channel | Inject a foreign-`source` `MessageEvent(internal=True)` into the origin session (the handoff/ADR-0026 mechanism). |
| No irreversible-tool confirmation primitive | A static, operator-curated `HERMES_VOIP_OUTBOUND_ALLOW` allowlist (default empty) is the hard gate; the IRREVERSIBLE level-3 + non-degraded clamp is the privilege gate. No model-set `confirmed` flag. |

## Consequences

- The agent can place calls *autonomously* once the operator opts a number in — a new
  irreversible capability. It is contained by: (1) the empty-by-default allowlist (no
  dialling at all until configured), (2) exact-match semantics for every entry except a
  simple `x`/`X` extension mask — `*` is literal, so no entry ever over-matches a target
  or compiles to a `.*` glob (§2a), (3) the level-3 + non-degraded privilege clamp (an untrusted
  inbound caller can never trigger it), and (4) least privilege on the resulting call
  (the callee is untrusted; the call agent gets the OUTBOUND persona with
  `privilege_level=0`, so it cannot itself place a further call or transfer).

- **The objective brief must not contain secrets.** It is spoken to / pursued with an
  untrusted callee; the operator's prompt and the call agent's persona are framed so
  the objective is the task only (booking name/time), never operator credentials. This
  is the same least-privilege spine as the caller-modes design.

- We now maintain a second injection target in `_signal_call_end` (the origin
  session) in addition to the call's own session. Both are best-effort and never
  strand teardown (ADR-0026's contract is preserved).

- Live validation (an agent triggers a call, it runs concurrently, the result is
  reported back) requires the operator to redeploy and add an allowlist entry. The
  unit/contract suite proves every seam against fakes.

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| `delegate_task` sub-agent for the call | Hermes's `delegate_task` blocks the parent while children run and BLOCKS `send_message` in children (verified) — it cannot model a long-running phone call that reports back asynchronously. The call-as-its-own-chat model already gives true concurrency. |
| Synchronous `place_call` tool (await the whole call) | Would freeze the originating agent's turn for the entire call duration (could be minutes) — defeats concurrency and blocks the user. ASYNC return of the Call-ID is required. |
| DTMF `confirmed` flag as the irreversibility gate (ADR-0019 §4) | The DTMF confirmation channel is not wired into the live adapter, and a remote party shares the audio channel; a model-set flag is defeated by prompt injection. A static operator allowlist is both available now and more spoof-resistant. |
| Allowlist of file paths (like caller-modes lists) | Dial targets (extensions/SIP URIs) are short allowlist entries, not a PII corpus; a comma-separated inline value (in the gitignored `.env`) matches ADR-0019 §8's stated shape and keeps the config surface minimal. |
| Ship with a non-empty default allowlist | Any default that dials *something* is unsafe for a public, autonomous capability. Empty default = inert until the operator explicitly opts in. |
| Use the `send_message` tool to seed the origin turn | `send_message` only delivers outbound + mirrors a transcript line; it does NOT trigger an agent turn (verified, send_message_tool.py / mirror.py). It is correct only as the no-origin *fallback notification*, not as the result-into-origin mechanism. |
