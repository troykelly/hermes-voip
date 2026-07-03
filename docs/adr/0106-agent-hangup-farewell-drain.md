# ADR-0106: Bounded, cancellable agent-hangup farewell drain

- **Date:** 2026-07-03
- **Status:** Accepted
- **Deciders:** agent session (MEDIUM UX fix #1297; delicate async, one prior attempt shelved)
- **Relates to:** ADR-0026 (call termination / AGENT_HANGUP classification — amends),
  ADR-0057 (caller-silence reprompt + loop goodbye/drain — amends/symmetric),
  ADR-0028 (barge-in clean stop), ADR-0023 (echo gate), ADR-0059 (lifecycle drain knobs)

## Context

When the agent speaks a goodbye **and** calls the `hang_up` tool in the **same turn**, the
closing line was truncated or dropped. The path is `voip_tools.hang_up_call` →
`adapter.hang_up_call` (`_mark_agent_hangup` then `await session.hang_up()`, **no drain**) →
`CallSession.hang_up` which sends BYE **then** `await self._media.stop()`. `engine.stop()`
clears the TX buffer and turns `send_audio` into a silent no-op, so an in-flight
`CallLoop._play` loses its remaining frames. Two order-dependent wrong outcomes:

- **(A)** `hang_up` runs *before* the farewell `send()` → the adapter's post-hangup
  send-suppression drops the farewell entirely (caller hears silence).
- **(B)** `send`/`speak` mid-playout → `media.stop()` cuts it mid-word.

This is asymmetric with the **loop-initiated** goodbye (`_end_call_gracefully`, ADR-0057),
which *is* drained inline before `run()` returns. The two goodbye paths should be symmetric.

A **prior fix attempt was shelved for a non-correctness reason:** it introduced an
adapter-level `dict[str, asyncio.Event]` keyed by Call-ID; Events created under one
pytest function-loop and awaited/set under another produced a **cumulative, order-dependent
cross-test hang** (a pure inbound-INVITE test hung only when run after the full preceding
suite). The exact stuck await was never captured (pytest's per-test output capture swallows
the `faulthandler_timeout` dump for a never-finishing test). **The single overriding
constraint for this ADR is therefore: no test hang.** The drain must be strictly bounded,
cancellable, must not deadlock on `_playout_lock` or await a stream that never completes, and
must leave nothing that survives loop teardown.

## Decision

Add a **bounded, cancellable, leak-proof** drain the adapter awaits on an agent-initiated
hang_up **before** BYE + media stop, so the farewell reaches the wire while the media path is
still live — then BYE and `media.stop()` fire.

1. **State = a loop-local `int` counter `CallLoop._active_replies`, incremented only in
   `speak()`.** `speak()` is the agent-reply-only entry; the comfort filler and the loop
   goodbye call `_speak_text` **directly**, bypassing `speak()`. So the counter counts **only**
   agent farewells, touches exactly one method, and never stalls a hangup waiting on a
   greeting/tone/filler. `speak()` brackets its single `_speak_text` await with
   `self._active_replies += 1` / `finally: -= 1` (the increment is synchronous, so the count
   goes positive before `speak` yields — this is what closes the (A) dispatch race). A plain
   `int` mutated only on the event loop cannot tear and leaves no primitive behind.

2. **Wait = `CallLoop.drain_agent_speech(*, timeout, grace)` polling on `asyncio.sleep`
   only** — no `Event.wait()`, no `Lock`, no `Future`, no task, no async generator, no thread.
   `asyncio.sleep`'s wakeup is a per-loop `call_later` timer that cannot register a waiter on
   any object that outlives the loop; it is the one await primitive that is leak-proof *by
   construction*. Two phases under one deadline: **(1) arrival grace** — if nothing is
   speaking, wait up to `grace` for an imminent reply `speak` to begin (guards (A)); if none
   arrives, return `True` at once (a bare hangup must not stall teardown); **(2) completion** —
   wait for every in-flight reply to finish, bounded by the remaining `timeout`. Returns
   `True` when drained (or none came), `False` if the bound elapsed with a reply still in
   flight (the caller proceeds with BYE either way — rule 37). The drain **never** acquires
   `_playout_lock` (held by the in-flight `_play` — contending on it is the classic deadlock)
   and **never** awaits the `TtsStream`.

3. **`adapter.hang_up_call` awaits the drain between `_mark_agent_hangup` and
   `session.hang_up()`.** `_mark_agent_hangup` stays **first** so the end still classifies
   `AGENT_HANGUP` (ADR-0026) even if the drain hits its bound. Because the drain runs before
   `session.hang_up()`, `info["ended"]` (set only in `_teardown_call`) is `False` throughout
   the drain window: a farewell `send()` arriving during it passes the suppression, reaches
   `loop.speak()`, and increments the counter. **The (A) guard is the sequencing itself**
   (drain → BYE → stop → teardown-sets-`ended`); the send-suppression predicate is unchanged.

4. **Config: two new `MediaConfig` knobs** — `agent_hangup_drain_secs` (env
   `HERMES_VOIP_HANGUP_DRAIN_SECS`, default **5.0**) and `agent_hangup_grace_secs` (env
   `HERMES_VOIP_HANGUP_GRACE_SECS`, default **0.5**), validated positive-finite with the
   cross-field invariant `grace <= drain`. Distinct from `GatewayConfig.shutdown_drain_secs`
   (the all-calls SIP graceful-shutdown drain) so per-call farewell latency tunes
   independently. The grace increments at `speak()` **entry** (before synthesis), so it need
   only cover the sub-second same-turn dispatch race — not synth latency.

5. **Barge-in RELEASES but does not ABORT the hangup** (deliberate asymmetry vs
   `_end_call_gracefully`, which aborts the loop end when the caller barges in). A caller
   barge-in cancels the active stream → `_play` unwinds → `speak`'s `finally` decrements the
   counter → the drain's next poll observes 0 and returns promptly. But the `hang_up` tool is
   an explicit, already-committed decision, so barge-in only **shortens** the drain.

## Rejected alternatives

- **The `asyncio.Event` mechanism** (a loop-local `_playout_idle`/`_playout_busy` pair drained
  by `wait_for(event.wait())`). Safer than the shelved adapter-scope dict, but it still parks a
  waiter on a shared object — the primitive class implicated in the prior hang. The
  `asyncio.sleep`-only poll is a strictly larger leak-proof margin, which the overriding
  no-test-hang priority demands. A 20 ms poll granularity is imperceptible for a
  once-per-hangup wait and a `<=20 ms` barge-in-release delay.
- **Tracking `_active_tts_stream`** (via a `_set_active_stream` helper across its four write
  sites). It waits for **any** playout (greeting, tone, comfort filler), so a hangup during a
  filler would stall up to the bound; and the four sites sit on the hot echo-gate/barge-in/
  `_play`-finally path (larger blast radius). The counter counts **only** agent replies and
  touches **one** method.
- **Reusing `GatewayConfig.shutdown_drain_secs`.** Conflates whole-plugin shutdown with
  per-call farewell latency and prevents independent tuning.
- **An unconditional multi-second arrival grace.** Imposes dead air before BYE on **every**
  silent hangup (and on the common "farewell already finished, then hang_up" ordering). The
  grace is short (0.5 s) and separate from the total drain, skipped instantly when a reply is
  already in flight.
- **Aborting the hangup on barge-in** (symmetry with the loop goodbye). The agent committed to
  ending via the tool; barge-in shortens the drain but must not strand a call it chose to
  close.

## Consequences

- A same-turn "Goodbye!" + `hang_up` is now heard in full before BYE (the (B) mid-playout cut
  and the (A) dispatch-race drop are both closed).
- **Honest residual (A2):** a goodbye the model emits in a *genuinely separate later message*
  (after it has already seen the `hang_up` tool result, past the grace) arrives after
  `media.stop()` — no media path — and is correctly dropped (the tool contract promises no
  further audio after hangup). Closing it would require an unconditional multi-second
  pre-teardown window (dead air on every hangup), rejected as a worse regression.
- **No-test-hang discipline** (the crux): the product drain has no task/Event/Future/async-
  generator and only loop-local `int` + `asyncio.sleep`, so nothing survives a function-scope
  loop teardown. Tests drive concurrency with `asyncio.gather` (never a leaked `create_task`),
  use finite/gated/cancelled fake streams, and end with a pending-task assertion; the formerly-
  hanging inbound-INVITE victim test is run **after** the new drain/hangup tests under an
  OS-level `SIGABRT` timeout so any residual stall is a fast, stack-dumping failure.
- `call.py`, `engine.py`, `_active_tts_stream`, `_play`, and `barge_in` are **not** touched;
  the ordering is fixed entirely at the adapter seam plus a one-int counter in `speak()`.
- **Live validation** (rule 26) is required before "done": place a call, have the agent say
  goodbye + `hang_up` in one turn, and confirm on the recording that the full farewell is heard
  before the BYE. Pending the operator's redeploy.
