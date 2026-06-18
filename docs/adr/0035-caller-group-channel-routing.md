# ADR-0035: VoIP caller-group channel routing — one Hermes, many VoIP channels

- **Date:** 2026-06-18
- **Status:** Accepted
- **Deciders:** operator (requirement, 2026-06-18) + agent session (channel-routing design)
- **Extends:** ADR-0021 (caller groups) and ADR-0020 (caller modes). The N named caller
  groups are retained; what changes is how a group's identity reaches Hermes. Each group
  now also names a **channel** (a Hermes platform name), and an inbound call is delivered to
  the agent under that channel's platform identity. ADR-0020/0021's security spine
  (forgeable caller-ID, the spotlighted untrusted-data fence, PII-safe list files, the
  ADR-0009 tool gate) is retained verbatim — it is **not** re-litigated here.

## Context

The operator's model for this plugin is **voip caller-group channel routing**: our one
VoIP plugin presents **MULTIPLE** VoIP channels to **ONE** Hermes agent, where each channel
is a separate conversation with its own permitted tools (conceptually like a chat platform
with multiple channels under a single agent — an analogy only; there is no chat-platform
integration). Different kinds of caller should land in different channels — an unknown cold
caller, a known contact, the operator themselves, and a door/gate intercom are four
different conversations with the agent, not one. The agent
**always** handles every call (ADR-0021's correction: trust tiers must never gate the
plugin's core voice functionality); separation is per-channel **conversation + permitted
tools**, decided by the caller group.

Hermes routes an inbound `MessageEvent` to a session **solely by `event.source`**:
`build_session_key(source)` is `agent:main:{source.platform.value}:{chat_type}:{chat_id}[…]`
(`gateway/session.py`), and `self.platform` is never consulted for routing. The adapter's
inherited `build_source()` hard-codes the adapter's own platform (`voip`), so today every
call — unknown, operator, intercom — shares the single `voip` platform namespace. The
ADR-0029 outbound-result path already proves the escape hatch: it constructs a
`SessionSource(platform=Platform(<other>), …)` directly to land a turn in a foreign session
(`adapter.py` `_report_to_origin_session`). `Platform._missing_` (ADR-0002 §) resolves any
platform name without editing the core enum, so `Platform("voip-unknown")` /
`Platform("voip-operator")` are valid platform identities.

A separate, harder isolation — distinct **secrets/memory** per channel — is **not**
available within one Hermes process: persona/system-prompt is global (`SOUL.md`), plugin
tools register globally, and the only secret-isolating boundary is a separate Hermes
**profile/gateway process** (`hermes -p NAME`). That stronger isolation is option B,
deferred (see *Consequences* and ADR-0021).

## Decision

**Each caller group names a Hermes channel (platform name), and a call is delivered to the
agent under that channel's platform identity.** Concretely:

1. **`CallerGroup` gains a `channel: str` field** (`src/hermes_voip/caller_modes.py`).
   Empty (the default) resolves to `voip-<group-name>` via the pure helper
   `channel_for_group(group) -> str` (`return group.channel or f"voip-{group.name}"`) — the
   single chokepoint for the default so every construction site (positional or keyword) stays
   valid. The JSON groups document accepts an optional `"channel"` string per group; absent ⇒
   default. The default three groups synthesised from the ADR-0020 modes resolve to
   `voip-operator` / `voip-receptionist` / `voip-blocked`; the outbound group to
   `voip-outbound`. The operator's four canonical channels are
   `voip-unknown` / `voip-known` / `voip-operator` / `voip-intercom`.

2. **Each channel is registered as a first-class Hermes platform** in `plugin.register()`.
   The primary `voip` platform (the adapter factory + `check_fn` + `validate_config` +
   `required_env`) is unchanged; the per-channel platforms are registered as **lightweight
   aliases** of the same adapter factory (no own transport — they exist so the operator's
   per-platform `tools_config` / `agent.disabled_toolsets` can target a channel and so the
   channel is a first-class, discoverable platform). The channel-platform list is the
   canonical operator set (`voip-unknown`, `voip-known`, `voip-operator`, `voip-intercom`)
   so the names exist even before a groups file is loaded.

3. **Inbound INVITE handling + every own-session turn delivery build the `SessionSource`
   with `platform=Platform(channel)`** instead of the hard-coded `voip`. A new private
   helper `VoipAdapter._call_source(call_id, *, chat_name, user_id, user_name)` resolves the
   call's group → channel (via the call-info `"group"`/`"mode"` keys, the same resolution
   `_deliver_turn` already does, defaulting to the receptionist channel) and constructs the
   `SessionSource` directly (the ADR-0029 technique). **All four own-session injections** —
   the spotlighted caller-transcript turn (`_deliver_turn`), the objective first-turn seed
   (outbound), the rich call-context first-turn seed (ADR-0052), and the call-end Hermes
   signal (ADR-0026) — go through `_call_source`, so an entire call's conversation (seed →
   turns → end-signal) lives in **one** channel session namespace. The ADR-0029 cross-session
   report path is untouched (it already targets the *originating* foreign session, not the
   call's own channel).

4. **Per-channel permissions are the existing `CallerGroup.allowed_tools` sub-ceiling,
   reframed as the channel's permitted tool set** and threaded onto the call's
   `GuardSessionState` exactly as ADR-0031 already does (`adapter.py`,
   `GuardSessionState(allowed_tools=group.allowed_tools)`). This **is** the operator's
   "separate permissions" and remains the by-construction security mechanism — it is **not**
   trust-tier crippling of the plugin (the agent still handles the call and has the voice
   tools). Default per-channel tool sets (operator-overridable in the groups file):

   | Channel | Default permitted sensitive tools |
   |---|---|
   | `voip-unknown` | **none** — exposes no sensitive tool (no `place_call` / `transfer_blind` / `open_entry` / `hold`/`resume`); the agent converses only |
   | `voip-known` | limited — `hold_call` / `resume_call` (no `place_call` / `transfer_blind` / `open_entry`) |
   | `voip-operator` | all (no sub-ceiling) |
   | `voip-intercom` | `open_entry` only (the door/gate action) |

   The empty `allowed_tools` keeps meaning "no sub-ceiling" (level-only gating) so existing
   configs are unchanged; the new default sets above are applied to the **canonical default
   groups** the plugin synthesises, not retro-fitted onto a hand-written groups file.

`SOUL.md` persona stays global; the per-channel persona remains the spotlighted preamble the
plugin already injects per group (ADR-0009/0020). Caller-ID remains **non-authoritative**:
the channel a call routes to is derived from the forgeable caller-ID, so the **untrusted-data
fence + the `voip-unknown` no-sensitive-tools ceiling are what make a spoofed identity safe**,
never the channel name itself.

## Consequences

- **Easier:** the operator configures each kind of caller as its own conversation with its
  own permitted tools using Hermes's native per-platform `tools_config` /
  `disabled_toolsets`, exactly like configuring a chat platform. Channels are first-class
  and discoverable. An unknown caller and the operator no longer share one transcript/session.
- **Committed to maintaining:** the channel-name ↔ group mapping, the four canonical
  platform registrations, and `_call_source` as the single own-session routing chokepoint
  (all four injections must keep using it — a new injection that calls `build_source`
  directly would silently route to bare `voip`; covered by a test asserting the platform on
  every own-session event).
- **Accepted limitation (NOT solved here):** one Hermes process = **shared agent
  identity / memory / secrets** across channels. Per-channel separation is conversation +
  permitted tools, **not** secret isolation. A prompt-injection on the `voip-unknown` channel
  still runs inside the same process that holds the operator's secrets; the
  spotlight/untrusted-data fence and the no-sensitive-tools ceiling on `voip-unknown` are the
  mitigations. **Hard** secret isolation for the untrusted channel = run *that* channel as a
  separate Hermes profile/gateway (**option B**) — a later add-on if the operator wants it;
  not built now (ADR-0021).
- **Operational:** no new runtime dependency, no new transport, no hot-path cost (the
  channel is resolved once per turn from the in-memory call-info dict; the
  `SessionSource`/`MessageEvent` shape is unchanged). The registry gains a handful of alias
  platform entries (negligible).

## Alternatives considered

| Alternative | Rejected because |
|---|---|
| Keep one `voip` platform; encode the group only in the spotlight preamble (status quo) | Every caller shares one session/transcript and one per-platform tool config. The operator explicitly wants separate **conversations** per caller kind (voip channel routing); a preamble is not a separate conversation and cannot drive Hermes's per-platform `disabled_toolsets`. |
| A separate Hermes **profile/gateway per group** (option B) for full secret isolation | The principled answer for *secret* isolation, but it is multiple processes, multiple registrations, and operator-managed process lifecycle. The operator asked for the one-Hermes-many-channels model now; option B is recorded as the deferred path when hard secret isolation is required. |
| Re-introduce `privilege_level` tool-gating as the per-channel boundary | ADR-0021's operator correction forbids trust tiers gating the plugin's voice functionality. Channel routing decides the *conversation + permitted tools*; the agent always handles the call. (The `allowed_tools` sub-ceiling is retained as the permitted-tool mechanism, **not** as a "can the agent take the call" gate.) |
| Use the caller's channel as an **authentication** boundary (trust `voip-operator` to mean "is the operator") | Caller-ID is forgeable (ADR-0020 §0). The channel is derived from a spoofable identifier; it selects a conversation + a tool ceiling, never proof of identity. `voip-unknown`'s **no-sensitive-tools** ceiling is what bounds a spoofed `voip-operator` claim — there is no path from caller-ID to a sensitive action without an operator-assigned list match **and** ADR-0010 confirmation. |
| Add an explicit platform-override parameter to the gateway's `build_source` | A Hermes-core change for no extra benefit; constructing `SessionSource` directly is the already-proven, in-plugin technique (ADR-0029) and keeps the change inside this repo. |
| Register each channel with its **own** adapter instance | Each channel would open its own SIP/RTP transport and registration — wasteful and wrong: there is one telephony endpoint. The channels are routing identities over the **one** adapter, so they alias the same factory. |
