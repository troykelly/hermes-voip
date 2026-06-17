# ADR-0026: Call-termination → Hermes session lifecycle signal

- **Date:** 2026-06-17
- **Status:** Accepted (amends ADR-0002 termination semantics; amends ADR-0011 where BYE was SIP-only)
- **Deciders:** agent session (VoIP reliability), operator-directed (workflow wf_d9668c30-daa)

## Context

Two related gaps made the plugin behave badly at call end.

1. **The plugin signalled NOTHING to the Hermes session on call end — any path.** One call
   maps to one Hermes DM session (ADR-0002, `chat_id` ← SIP `Call-ID`), but `_teardown_call`
   only stopped the RTP engine and dropped the in-dialog routes; it never told Hermes the
   call had ended (no reason-carrying event, no stop). `CallSession._on_bye` 200-OKs an
   inbound BYE and stops media at the **SIP** layer only (ADR-0011). So after a hangup the
   Hermes session was left dangling — the agent could keep "thinking" about a caller who was
   gone, and nothing closed or informed the conversation.

2. **A silent media/network drop HUNG the call forever.** The conversational loop
   (`CallLoop.run`) ends only when `engine.inbound_audio()` terminates, which happened solely
   when `RtpMediaTransport.stop()` set the stop event — and `stop()` was called only from
   `_teardown_call` (after `run()` returned) or from `_on_bye` (a *received* BYE). There was
   **no RTP-inactivity timeout**: the inbound generator blocked indefinitely on the recv
   queue, and `connection_lost`/`error_received` on the UDP protocol were DEBUG-only no-ops.
   A dropped media path therefore wedged the call with no recovery.

3. **There was no agent-initiated hangup at all.** The agent had no tool to end a call — a
   concrete usability gap the operator hit on a live test call. The plugin's `register(ctx)`
   only registered the platform; it never called `ctx.register_tool`.

### Hermes API reality (verified against hermes-agent 0.16.0)

There is **no typed session-end / reason API**. Session lifecycle is driven entirely by
**inbound text** the gateway parses from a `MessageEvent`:

- `MessageEvent.is_command()` is `text.startswith("/")`; `get_command()` extracts the verb.
  `stop` is in `hermes_cli.commands.ACTIVE_SESSION_BYPASS_COMMANDS` (and `GATEWAY_KNOWN_COMMANDS`),
  so injecting `"/stop"` is a recognised **hard stop** that pre-empts an in-flight turn.
- An `internal=True` `MessageEvent` **bypasses user auth**, so the adapter can synthesise one.
- The gateway's *interrupt* path branches on the reason: a reason in
  `gateway.run._CONTROL_INTERRUPT_MESSAGES` (`'stop requested'`, `'session reset requested'`,
  `'execution timed out (inactivity)'`, …) is dropped with **no replay**; **any other**
  free-text reason is **replayed to the agent as its next turn**.

So the only lever is to inject a `MessageEvent`. A **control** command (`/stop`) is a hard
stop; a **content** note (plain free text, not a slash command and not a control-interrupt
string) is **replayed**, letting Hermes itself decide stop-vs-followup.

## Decision

### Mechanism — one chokepoint, one signal per call

From the single `_teardown_call` chokepoint (reached from every end path — inbound finally,
outbound background-task finally, outbound pre-session finally), inject **exactly one**
`internal=True` `MessageEvent` via the adapter's inherited `build_source` + `handle_message`:

- **FAILURE end** → text `"/stop"` (the gateway-recognised hard stop).
- **NORMAL end** (remote BYE, agent hangup, end-of-stream) → a plain-text internal note
  `"[The caller has hung up; the line is now disconnected.]"` — **not** a slash command and
  **not** a control-interrupt string, so the gateway **replays** it as the agent's next turn.
  The note states the line is **disconnected** so the model does not try to keep speaking to
  a dead media path.

The once-per-call guarantee rides the existing same-Call-ID `is_current` identity check
(plus an `already_ended` guard), so a superseded/duplicate teardown never re-injects.

### `CallEndReason` taxonomy (typed enum, `was_failure` / `can_followup`)

| Reason | Family | Signal | `can_followup` |
| --- | --- | --- | --- |
| `REMOTE_BYE` | normal | replayed note | yes |
| `AGENT_HANGUP` | normal | replayed note | yes |
| `EOS` | normal | replayed note | yes |
| `MEDIA_TIMEOUT` | failure | `/stop` | no |
| `PIPELINE_FAILURE` | failure | `/stop` | no |
| `SIP_ERROR` | failure | `/stop` | no |
| `CONNECTION_LOST` | failure | `/stop` | no |
| `REGISTRATION_LOST` | failure | `/stop` | no |

`injection_text_for_reason` is total over the enum (failure → `/stop`; every normal →
the note), branching only on the member's own `was_failure` — no third outcome, no silent
empty string (rule 37).

**Classification** (`_classify_end_reason`, fail-safe by construction):

1. the call loop **raised** (an `ExceptionGroup` from a failed ASR/TTS/guard/transport task,
   or any pre-`run()` error) → `PIPELINE_FAILURE`;
2. else `engine.media_timed_out` (the RTP watchdog fired or the UDP transport was lost) →
   `MEDIA_TIMEOUT`;
3. else a clean return → `AGENT_HANGUP` when the call's hang-up marker is set, else
   `REMOTE_BYE`.

### Fail-safe: unknown / ambiguous end → failure → stop

`_teardown_call`'s `reason` defaults to `PIPELINE_FAILURE`, and `CallEndReason.fail_safe()`
is `PIPELINE_FAILURE`. An end we cannot otherwise explain **hard-stops** the session rather
than leaving it dangling or replaying an ambiguous content note.

### Operator-approved specific decisions

- **Agent hangup is SOFT.** A `hang_up` tool ends the call (sends BYE) but routes through the
  chokepoint as a **NORMAL** end (`AGENT_HANGUP`), keeping the Hermes session open for
  follow-up — like a caller hangup — **not** a hard `/stop`.
- **RTP-inactivity timeout: 20 s default, configurable up to a 300 s max.** A no-media
  deadline in the inbound generator (re-armed on every datagram) fires `MEDIA_TIMEOUT`.
  `HERMES_VOIP_RTP_TIMEOUT_SECS` validates/caps to `[1, 300]`. (The engine accepts `0` as
  "disabled", but the operator knob does **not** expose disabling the safety watchdog.)
- **Registration loss → failure → stop** (`REGISTRATION_LOST`).
- **Post-hangup TTS is suppressed.** Once a call is flagged ended, `send()` to it returns a
  **failed** send (no media path) and never synthesises — notably the turn the agent produces
  in reply to the replayed note is not spoken to the disconnected caller. (`send()` to an
  ended call already failed; this enforces it as a deliberate, logged drop.)

### Reason rides as TEXT; follow-up has no media path

The reason is carried as `MessageEvent.text` — there is **no structured reason/metadata
channel** on `MessageEvent`/`SendResult` (the API limit). And once a call ends, **there is no
media path to the caller**: "follow-up" (`can_followup`) means the session stays open for
**background agent work**, a **new outbound callback** (ADR-0019), or a notification on
**another** Hermes channel — **never** speaking to the now-disconnected caller.

### Agent hang-up tool wiring

`register(ctx)` now also registers the agent `hang_up` tool (`ctx.register_tool`, async, with
a JSON schema) and a `pre_tool_call` gate (`ctx.register_hook`) that applies the ADR-0009/0011
`gate_voip_tool` policy. `hang_up` is `ToolRisk.SAFE` — ending a call mutates no external
state and is something even a level-0 receptionist may do — so it is never gated, but routing
it through the gate means a future higher-risk VoIP tool is already covered. The tool handler
resolves the call from the Hermes session's `chat_id` (= SIP Call-ID) via
`gateway.session_context` (lazy import), reaching the live call through a process-wide
"active adapter" seam the adapter sets on connect. Every persona preamble (`caller_modes.py`)
names the `hang_up` tool and when to use it, so the model actually invokes it.

## Consequences

- **Hermes is always told a call ended, with the right disposition.** A normal end hands the
  agent a turn it can react to (e.g. log the call, schedule follow-up) and decide whether to
  stop; a failure hard-stops the session. No more dangling sessions.
- **A silent media drop self-heals.** The watchdog ends a wedged call within the configured
  window (default 20 s) instead of hanging forever — the real reliability fix. A live call's
  continuous media re-arms the deadline, so it is never false-killed.
- **The agent can end calls.** Closes the live usability gap; the hangup keeps the session
  open for follow-up (SOFT).
- **The plugin now registers tools + a hook**, not just the platform. `register(ctx)` is
  resilient to a `ctx` that predates `register_tool`/`register_hook` (the platform still
  registers).
- **No structured reason channel.** Downstream consumers parse the injected text; if Hermes
  later adds a typed lifecycle API, the FAILURE/NORMAL split maps onto it directly.

## Alternatives considered

- **Route call-end through `interrupt_session_activity` / the media barge-in path.** Rejected:
  `interrupt_session_activity` is turn-scoped and reason-blind (it stops the in-flight turn,
  not the session), and the media barge-in is TTS-only. Neither ends or informs the session.
- **Always inject `/stop` (a hard stop) on every end.** Rejected: it removes Hermes's ability
  to do follow-up on a normal hangup. The control-vs-content branch is the operator's "let
  Hermes decide" for normal ends, with `/stop` reserved for failures.
- **A hard agent hangup (`/stop`).** Rejected by the operator: a hangup should keep the
  session open for follow-up, like a caller hangup.
- **Disable the RTP watchdog by default / no cap.** Rejected: a disabled watchdog reintroduces
  the infinite-hang bug, and an uncapped window lets a wedged call persist arbitrarily long.

## References

- ADR-0002 (platform adapter; one call = one DM session) — termination semantics added here.
- ADR-0011 (multi-registration + call control; BYE was SIP-only) — session-end signal added here.
- ADR-0019 (outbound calling) — the callback channel a NORMAL-end follow-up can use.
- `docs/runbooks/0005-voip-rtp-inactivity-timeout.md` — the operator knob for the watchdog.
