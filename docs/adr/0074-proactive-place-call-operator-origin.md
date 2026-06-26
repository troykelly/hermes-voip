# ADR-0074: Proactive `place_call` from a configured operator origin (opt-in, place_call-only)

- **Date:** 2026-06-26
- **Status:** Accepted
- **Deciders:** agent session (security lane). Extends ADR-0029 (agent-triggered outbound
  `place_call` + the `HERMES_VOIP_OUTBOUND_ALLOW` chokepoint) and composes with the
  ADR-0009/0011 tool-privilege gate. Resolves issue #202.

## Context

The `place_call` agent tool (ADR-0029) is `ToolRisk.IRREVERSIBLE`: the `pre_tool_call` gate
(`voip_tools.voip_pre_tool_call`) clamps it to an **operator** session (privilege level 3,
non-degraded). The gate resolves that privilege from the **live SIP call** in scope — it reads
the session `chat_id` (== SIP Call-ID, ADR-0002) and looks it up in the adapter's per-call
guard map. When there is **no live call** in scope, the gate falls back to a least-privilege
level-0 state, so an unknown/spoofed context can never reach a privileged tool.

That fail-safe makes `place_call` unreachable from any **non-VoIP** agent session. On a
Telegram-originated turn the session `chat_id` is the Telegram chat id, not a SIP Call-ID, so
the lookup misses and the state collapses to level 0 — and the operator's intended *proactive*
outbound ("call me with a brief", a cron-driven check-in call) is always blocked with
`The place_call tool is not permitted on this call.`

The rest of the proactive path already exists end-to-end: `adapter._capture_origin_session`
reads the originating `(platform, chat_id)` from `gateway.session_context`, and
`HERMES_VOIP_OUTBOUND_RESULT_CHANNEL` delivers the call outcome back. The only missing piece is
the gate recognising that originating chat as a trusted operator origin.

The inbound fail-safe is **correct and must stay**: a spoofed or prompt-injected *caller* must
never reach `place_call`. The relaxation must be surgical — proactive, operator-origin-only.

## Decision

Add one opt-in env gate, `HERMES_VOIP_PROACTIVE_CALL_FROM` — a comma-separated list of
`platform:chat_id` origins. In the **no-live-call** branch of `voip_pre_tool_call` **only**,
when the originating `(platform, chat_id)` (read from `gateway.session_context`) matches a
configured entry **and** the tool is **exactly** `place_call`, the gate resolves
`privilege_level=3` instead of 0; otherwise it resolves 0 as before. The decision lives in a
single pure helper, `voip_tools._proactive_place_call_allowed(tool_name) -> bool`.

Deliberate constraints, each a security property:

- **Opt-in, fail-safe default.** Empty/unset `HERMES_VOIP_PROACTIVE_CALL_FROM` (the shipped
  default) makes the helper return `False`, so behaviour is **byte-identical** to pre-#202 — no
  operator who has not opted in gains anything.
- **`place_call`-only.** `transfer_blind` / `send_dtmf` / `open_entry` are meaningless without a
  live call and stay blocked in the no-call branch; only `place_call` is relaxed.
- **Inbound fail-safe untouched.** The relaxation is reached only when `state is None` (no live
  call). A live call still resolves its real caller-group privilege above this branch, so a
  spoofed/injected caller is unaffected.
- **Scoped, not blanket.** Permission is tied to explicitly-configured `(platform, chat_id)`
  pairs (exact match after trimming), not "any chat on the platform".
- **Defense in depth preserved.** The static `HERMES_VOIP_OUTBOUND_ALLOW` allowlist (ADR-0029)
  still refuses any unlisted dial target at the chokepoint, so even a compromised/misconfigured
  operator chat can only reach pre-approved numbers.

The helper is read at the **gate** (not cached at `connect()`), so the seam is a single file
(`voip_tools.py`) with no new `VoipToolHost` member and no adapter state — the smallest change
that closes the gap. `HERMES_VOIP_PROACTIVE_CALL_FROM` is read from `os.environ`; the origin is
read via the same lazy `gateway.session_context` import the gate already uses for the Call-ID
(so the module stays hermes-free at import time, and the helper returns `False` if that runtime
is absent).

## Consequences

- An operator can drive a proactive outbound call from a configured chat: the gate grants
  operator privilege for that `place_call`, the allowlist gates the number, and the outcome is
  reported back via the originating session / `HERMES_VOIP_OUTBOUND_RESULT_CHANNEL`.
- With the env unset, nothing changes — the privilege spine and every existing inbound
  fail-safe test stay green unchanged.
- Two env vars must now align for proactive outbound: the **origin** (`…PROACTIVE_CALL_FROM`)
  and the **target** (`…OUTBOUND_ALLOW`). Both are required; the runbook documents the pairing.
- Operational HOW (set/verify/rotate/rollback) is in `docs/runbooks/0007-voip-outbound-calling.md`.

## Alternatives considered

- **Store the parsed origins on the adapter (parse at `connect()`).** Mirrors how
  `HERMES_VOIP_OUTBOUND_ALLOW` is handled and would let the gate read it via the `_ACTIVE_ADAPTER`
  seam. Rejected for this change: it adds adapter state and a protocol surface for a single
  boolean the gate can compute directly, and reading at the gate keeps the change to one file.
  Revisit if other gate decisions come to need adapter-cached config.
- **A blanket "trust any non-VoIP session" relaxation.** Rejected — it would let any platform
  chat that reaches the agent trigger an outbound call, defeating the operator-origin scoping.
- **Drop the privilege clamp for `place_call` entirely and rely on the allowlist.** Rejected —
  the privilege clamp is the spine that stops an untrusted *inbound* caller from dialling out;
  removing it would expose the inbound path. The allowlist is defense in depth, not a substitute.
