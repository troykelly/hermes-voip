# ADR-0059: Adapter production-safety lifecycle — failure BYE, drain, admission cap, log redaction

- Status: Accepted
- Date: 2026-06-19
- Deciders: agent session (engineering), operator (`troy@…`)
- Supersedes / relates to: ADR-0026 (call-end taxonomy + teardown chokepoint),
  ADR-0005 (SIP-over-TLS transport), ADR-0011 (multi-registration manager),
  ADR-0021 (caller-group classification), ADR-0053 (SDES SRTP), ADR-0055 (SIP
  signalling robustness)

## Context

A launch-readiness audit of the call lifecycle in `src/hermes_voip/adapter.py`
confirmed four production-safety gaps. None are exotic; each is reachable in
normal operation on a 24/7 line and each was verified against the current code.

1. **No SIP BYE on a mid-call failure.** The single call-end chokepoint
   `_teardown_call` (ADR-0026) cleaned up resources and signalled the Hermes
   session, but **never sent a SIP BYE on any path**. When the conversational
   pipeline failed mid-call (STT/TTS/LLM crash → the `CallLoop` raises →
   `PIPELINE_FAILURE`), the media engine stopped and Hermes got `/stop`, but the
   **SIP dialog stayed UP** on the gateway. The caller heard dead air until the
   gateway's own session timer (or the RTP-inactivity watchdog on a path we no
   longer serviced) eventually tore it down — a zombie dialog. Only the agent
   `hang_up` tool and a peer-initiated BYE produced a proper BYE.

2. **No graceful-shutdown drain.** `disconnect()` (the `aclose()`/SIGTERM path)
   cancelled in-flight call tasks and closed the transport, but sent **no BYE**
   to connected callers and did **not drain**. A restart hard-dropped every live
   call into a dangling dialog.

3. **No admission-control cap.** Every inbound INVITE spawned a full per-call
   pipeline (RTP socket + STT + TTS + AEC + VAD). There was **no ceiling on
   concurrent calls**, so a burst/flood was an unbounded resource (CPU/memory)
   amplifier — an OOM/CPU-starvation risk on a 24/7 line.

4. **Secret leak in the unroutable-request log.** `_on_unroutable` logged the raw
   request/response at DEBUG via `%s`, whose `repr` includes **every header** —
   the `Authorization`/`Proxy-Authorization` digest **response** (a credential
   derived from the SIP password) — and the **SDP body**, including `a=crypto`
   **inline SRTP key material**. This repo is PUBLIC and logs can be captured in
   CI (rule 34).

## Decision

All four land in `adapter.py` (plus two `GatewayConfig` fields in `config.py`),
each TDD'd. The BYE primitive already existed — `CallSession.hang_up()` (ADR-0026)
is idempotent (`if self.ended: return`), sends an in-dialog BYE, and stops media —
so items 1 and 2 reuse it rather than re-implementing BYE.

### 1 — BYE on a failure end (in the teardown chokepoint)

`_teardown_call` now, when **all** of these hold, awaits `session.hang_up()`
before stopping the engine:

- `reason.was_failure` — a `PIPELINE_FAILURE` / `MEDIA_TIMEOUT` / `SIP_ERROR` /
  `CONNECTION_LOST` / `REGISTRATION_LOST` end (the `CallEndReason.was_failure`
  family). A **normal** end (REMOTE_BYE / AGENT_HANGUP / EOS) already had its
  dialog closed by the peer or the agent tool, so it sends no second BYE.
- `session is not None and not session.ended` — `session.ended` is the
  discriminator: it is set `True` by both `_on_bye` (peer BYE) and `hang_up`
  (agent BYE), so an already-closed dialog is never BYE'd twice (RFC 3261: a BYE
  on a terminated dialog is wrong).
- the call is **ours** (`is_current`) — a superseded same-Call-ID task never BYEs
  the live task's dialog.

The BYE is **best-effort**: a send failure is logged and never strands the rest
of teardown (engine stop, route cleanup) — the error is surfaced, not swallowed
(rule 37). The Hermes `/stop` signal is unchanged; this only adds the missing SIP
dialog closure.

### 2 — Graceful-shutdown drain (`disconnect`)

`disconnect()` is reordered: (a) `_connected = False` first, so
`_handle_inbound_invite` declines a racing INVITE with **503 Service
Unavailable** (stop accepting new calls); (b) `_drain_active_calls()` BYEs every
live `CallSession` concurrently and waits up to `shutdown_drain_secs`
(`asyncio.wait_for` over a `return_exceptions=True` gather); (c) the existing
per-call-task cancellation runs as the backstop for any BYE that hung or any task
with no session yet; (d) the manager deregisters (`aclose` sends `Expires: 0`)
and the transport closes. The drain is **bounded** — a hung BYE cannot stall
shutdown past the timeout (its task is cancelled in step (c)), and the timeout is
surfaced at WARNING (rule 37).

### 3 — Admission-control cap (`_handle_inbound_invite`)

A new `_admitted_calls: set[str]` tracks Call-IDs that hold a concurrency slot.
`GatewayConfig.max_calls` (env `HERMES_SIP_MAX_CALLS`, **default 8**) is the cap.

- **Fast-path reject:** right after the transport/config guards, if we are
  already at the cap (and this Call-ID is not already admitted), reject with **486
  Busy Here** before the classification + SDP work — a flood does the least work.
- **Authoritative atomic reserve:** right before the per-call media engine +
  pipeline are built (the boundary where the protected resources are allocated),
  `_admit_inbound(call_id, max_calls)` re-checks and reserves **atomically** (no
  `await` between the check and the `set.add` on the single-threaded loop),
  closing the race where two INVITEs both passed the fast-path while the last slot
  was free. A retransmitted/forked same-Call-ID INVITE is idempotent (the `set`
  dedups; never double-counts).
- **Release:** in `_teardown_call` (gated on `is_current` so a superseded
  same-Call-ID task does not free the live call's slot) and on the two
  pre-session media-setup failure paths (`_MediaNegotiationRejected` return; an
  unexpected media-setup exception, which releases then re-raises). No leak on any
  path; `disconnect` also clears the set.

**Why 486, not 503-with-Retry-After.** 486 Busy Here (RFC 3261 §21.4.24) is the
semantically correct "the callee is busy" answer a gateway maps to a
busy/voicemail/queue treatment; the line is not *down*, it is *full*. 503 is the
right code for the shutdown-drain window (the endpoint is going away), which is
why item 2's racing-INVITE decline uses 503 — the two states are distinct.

### 4 — Redact secrets in the unroutable log

`_on_unroutable` now logs `_redact_sip_for_log(what)` — a pure helper that renders
the typed `Unroutable` request or `SipResponse` as a compact diagnostic string
(method/request-URI or status/reason + every header) with the secret-bearing
values masked: `Authorization` / `Proxy-Authorization` / `Authentication-Info`
(and the matching `*-Authenticate` challenges, for safety) → `<redacted>`, and
each SDP `a=crypto` line → `a=crypto:<redacted>`. An unknown type falls back to
`type(what).__name__` — never a `str()`/`repr()` that could embed a credential.
The header **names** and the routing **reason** are kept, so a routing problem
stays debuggable.

## Configuration

Two new `GatewayConfig` fields (parsed in `load_gateway_config`, validated in
`__post_init__` so a direct construction is self-validating):

| env var | field | default | bound |
| --- | --- | --- | --- |
| `HERMES_SIP_MAX_CALLS` | `max_calls: int` | `8` | strictly positive |
| `HERMES_SIP_SHUTDOWN_DRAIN_SECS` | `shutdown_drain_secs: float` | `5.0` | positive, finite |

The prompt authorised one new config field (the cap); the drain timeout is the
second because the same launch requirement mandates a *configurable* drain
timeout. Both are gateway-level SIP-lifecycle knobs, so they live on
`GatewayConfig` next to `expires`, following the existing `HERMES_SIP_*` pattern.

## Consequences

- A mid-call provider failure now ends as a clean BYE-closed dialog, not a zombie
  with the caller in dead air. A restart drains live calls with a BYE instead of
  hard-dropping them. A burst is capped at `max_calls` with a 486, protecting the
  line from resource exhaustion. The unroutable log can no longer leak the digest
  response or SRTP keys.
- **Slot accounting is the one correctness risk** (a leak would slowly wedge the
  line at the cap). It is covered by tests on every path: reserve at the media
  boundary, release in teardown (`is_current`-gated) and on both pre-session
  failure paths, and `disconnect` clears the set. The `set` keying makes
  retransmits idempotent and releases idempotent.
- **Efficiency:** all four are O(1) per call or O(live-calls) at shutdown; the cap
  is a single integer comparison on the hot inbound path; redaction runs only on
  the cold unroutable path. No new dependency, no new IO layer.
- The drain reuses the idempotent `hang_up`, so it composes safely with a
  concurrent peer BYE (whoever sets `ended` first wins; the other is a no-op).

## Operational note

The incident/triage runbook (`docs/runbooks/0013-*`, when present) should
cross-reference this ADR for the new shutdown-drain + admission-cap behaviour:
on shutdown, expect a `graceful shutdown: draining N live call(s)` log and BYEs
to active callers; under load, expect `REJECTED 486 Busy Here — at concurrent-call
cap` and tune `HERMES_SIP_MAX_CALLS` to the host's pipeline budget.
