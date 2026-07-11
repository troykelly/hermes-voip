# ADR-0113: Per-call max-duration watchdog (active-call ceiling)

- **Date:** 2026-07-11
- **Status:** Accepted
- **Deciders:** agent session (autonomous orchestration)

## Context

An ACTIVE (answered) call had no server-side upper bound on its duration. The two
existing per-call watchdogs bound only NARROW failure modes:

- `media_timeout_secs` (ADR-0026, `HERMES_VOIP_RTP_TIMEOUT_SECS`, default 20 s) tears
  a call down only when RTP goes **silent** (inactivity). A caller streaming
  continuous RTP — comfort noise, hold music, a replayed loop — never trips it.
- The RFC 4028 session timer (ADR-0071) tears down only a **dead dialog** (no
  refresh). A peer that keeps refreshing keeps the dialog alive indefinitely.

So a caller — or a wedged / compromised / hostile peer — that keeps sending valid
RTP holds an admission slot (ADR-0059 `max_calls`, default 8) **forever**, running
the STT + LLM + TTS pipeline at unbounded operator cost. ~8 such calls permanently
`486 Busy Here`-reject every further inbound INVITE: a denial-of-service with no
server-side cap. This was the last outstanding gap-review `[high]`.

## Decision

Add a per-call **max-duration watchdog**: a configurable ceiling on the ACTIVE
(post-answer) phase, after which the gateway force-tears-down the call.

- **Config.** `max_call_duration_secs` on `GatewayConfig`, env
  `HERMES_VOIP_MAX_CALL_DURATION_SECS`, default **14400.0** (4 h). **`0` disables**
  the cap (operator opt-out). Validated non-negative + finite (`< 0` rejected via
  `_parse_non_negative_float`, ADR-0109) — the OPPOSITE polarity to the RTP watchdog,
  which rejects `0` because a silent-media *safety* watchdog must never be disabled.
  A duration ceiling is a *policy*, not a safety net, so opting out is legitimate.
- **Mechanism.** One watchdog task per call, armed in `_run_call_loop` (the single
  chokepoint every call path — inbound + both outbound legs — funnels through) and
  disarmed in `_teardown_call` (the single call-end chokepoint, ADR-0026), mirroring
  the RFC 4028 `_session_timers` lifecycle exactly. On expiry it flags the call and
  calls the idempotent `CallSession.hang_up()` (in-dialog BYE + media stop) — the
  SAME graceful path the shutdown drain uses. Stopping the media unblocks
  `call_loop.run()`, so the call's own teardown finally runs; there is NO cross-task
  cancellation of the running loop.
- **Reason.** A new `CallEndReason.MAX_CALL_DURATION` — a FAILURE end (`/stop`, no
  follow-up: the agent did not choose to end the call). Because the graceful hang-up
  gives `raised=False` / `media_timed_out=False`, `_classify_end_reason` consults a
  per-call flag FIRST so the end is tagged as a duration cap, not a normal REMOTE_BYE.

## Consequences

- A runaway / continuous-RTP call can no longer pin an admission slot + pipeline
  indefinitely: the slot is reclaimed at the cap. Closes the last gap-review `[high]`
  DoS (the ADR-0059 admission cap can no longer be permanently exhausted).
- The default 4 h is generous — no legitimate call is expected to run that long; a
  deployment that genuinely needs unbounded calls sets `0`.
- The cap is a HARD ceiling: at expiry the agent gets no chance to say goodbye (the
  media path is torn down), the same as MEDIA_TIMEOUT. The caller still receives a
  clean in-dialog BYE, and the Hermes session is hard-stopped (`/stop`).
- One extra `asyncio` task per active call while armed (a single sleep), cancelled at
  teardown — negligible cost.

## Alternatives considered

- **Cancel `call_loop.run()` via `asyncio.wait_for` at the cap.** Rejected: a
  cross-task cancellation of the running loop mid-iteration risks the
  `aclose(): asynchronous generator is already running` race (guarded by
  `test_teardown_mid_iteration_no_aclose_race`). The graceful `hang_up()` path avoids
  it entirely and reuses a proven mechanism.
- **Reuse MEDIA_TIMEOUT as the reason.** Rejected: it conflates two distinct causes
  (a silent-media drop vs a policy time cap) in logs and the ADR-0029 outbound
  outcome — an operator diagnosing why calls end must tell them apart. (A distinct
  member is only possible because ADR-adjacent PR — the CallEndReason enum-aliasing
  fix — made the reasons observably distinct.)
- **No cap / disabled by default.** Rejected: an uncapped active call is exactly the
  DoS; the safe default is a finite ceiling with an explicit opt-out (mirrors the
  ADR-0026 reasoning for the RTP-watchdog default).

## References

- ADR-0026 (call-termination signal + `media_timeout_secs`), ADR-0059 (`max_calls`
  admission cap), ADR-0071 (RFC 4028 session timers), ADR-0109
  (`_parse_non_negative_float`).
- Runbook 0020 (operator HOW for `HERMES_VOIP_MAX_CALL_DURATION_SECS`).
- `docs/backlog.md` gap-review batch 2026-07-11 (the `[high] robustness` item).
