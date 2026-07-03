# ADR-0105: Reach proactive `place_call` relaxation on `state is None`

- **Date:** 2026-07-03
- **Status:** Accepted
- **Deciders:** agent session (HIGH-severity proactive gate fix)
- **Relates to:** ADR-0074 (proactive `place_call` operator origin), ADR-0101
  (structured proactive deny reason), ADR-0029 (outbound target allowlist)

## Context

ADR-0074 defines the proactive `place_call` relaxation for an operator turn that is
not attached to a live SIP call: when `voip_pre_tool_call` has no live guard state
(`state is None`), the gate may grant operator level 3 only for `place_call` and only
when the originating `platform:chat_id` matches `HERMES_VOIP_PROACTIVE_CALL_FROM`.

The implementation contradicted that trigger. Inside the `state is None` branch it
consulted `_proactive_place_call_allowed` only when `call_id is None`; otherwise it
returned `LIVE_CALL_GUARD_MISSING` and kept the tool at level 0. On a real proactive
Telegram turn, `_current_call_id()` reads the same `HERMES_SESSION_CHAT_ID` as the
proactive origin reader, so `call_id == chat_id` and is non-`None`. Therefore the
ADR-0074/#202 feature was unreachable in production: the legitimate proactive turn was
always blocked before the allowlisted origin was considered.

The prior proactive tests masked this by monkeypatching `_current_call_id` directly via
`_set_chat(monkeypatch, None)` while `_set_origin` populated `HERMES_SESSION_CHAT_ID`.
That decoupled two values that are one runtime read from `gateway.session_context`.

## Decision

`voip_pre_tool_call` consults `_proactive_place_call_allowed(tool_name)`
unconditionally whenever the guard `state is None`. This restores ADR-0074's trigger:
the relaxation is reached for any no-live-guard-state turn, and the helper decides
whether to grant or deny.

The inbound fail-safe is platform-scoped, not Call-ID-presence-scoped. The helper builds
`needle = f"{HERMES_SESSION_PLATFORM}:{HERMES_SESSION_CHAT_ID}"`; an inbound SIP call's
platform is the VoIP transport, not the operator's configured non-VoIP platform (for
example `telegram`), and an unreadable or absent origin denies via `ORIGIN_UNAVAILABLE`.
A caller cannot forge the gateway-set session platform. The relaxation remains
place_call-only, and ADR-0029's `HERMES_VOIP_OUTBOUND_ALLOW` target allowlist still
gates the dial target at the outbound chokepoint.

This ADR amends ADR-0101 by removing `LIVE_CALL_GUARD_MISSING` from
`ProactiveDenyReason`. The branch that produced it was the bug, and the category
mislabelled a legitimate proactive turn as an inbound guard miss. A guard-missing inbound
call now denies through the same platform-scoped helper and is diagnosed as
`ORIGIN_NOT_ALLOWLISTED` when its `voip:<Call-ID>` origin matches no configured operator
entry.

The tests now model the runtime coupling: proactive tests use `_set_origin` to drive both
`_current_call_id()` and `_proactive_place_call_allowed`, while the inbound fail-safe test
uses a `voip` platform origin with a missing guard state and proves the tool remains
blocked.

## Consequences

- The ADR-0074/#202 proactive operator flow becomes reachable when the operator opts in
  with `HERMES_VOIP_PROACTIVE_CALL_FROM`.
- Inbound guard-missing calls remain fail-closed by platform-scoped origin matching plus
  `ORIGIN_UNAVAILABLE` on unresolved session context; no caller-controlled Call-ID value
  grants privilege.
- The structured diagnostic surface has one fewer category: `live_call_guard_missing` is
  historical only (ADR-0101), and current logs use `origin_not_allowlisted` for an inbound
  VoIP origin that is not an allowed operator origin.
- Test fixtures must not decouple `_current_call_id()` from the proactive origin
  `chat_id`; both values come from `HERMES_SESSION_CHAT_ID` at runtime.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Keep the `call_id is None` precondition and add a second allow path for Telegram | It preserves the bug's false discriminator. `call_id` is non-`None` in both the real proactive flow and inbound calls, so it cannot carry the security boundary. |
| Allow proactive relaxation only when no adapter is active | It blocks legitimate no-live-call sessions whenever an adapter is connected but has no guard state for the non-VoIP operator turn; ADR-0074's trigger is `state is None`, not adapter absence. |
| Keep `LIVE_CALL_GUARD_MISSING` as a current deny reason after consulting the helper | It would describe no reachable branch. The honest current diagnostic for a guard-missing inbound origin is `origin_not_allowlisted` (or `origin_unavailable`), depending on whether the platform/chat_id is readable. |
