# ADR-0075: Structured per-call lifecycle + RTCP call-quality log events (machine-parseable `extra=`)

- **Date:** 2026-06-26
- **Status:** Accepted
- **Deciders:** agent session (observability lane). Instruments the signals defined in
  runbook 0014; composes with ADR-0059 (admission control) and ADR-0061 (RTCP call quality).

## Context

`adapter.py` already logs the per-call lifecycle (INVITE received, rejections, the `200 OK`
answer, the CallLoop start) and the RTCP teardown call-quality snapshot (ADR-0061) as plain
`printf`-style human messages. Runbook 0014 marked the call-setup-success, packet-loss/jitter/RTT,
and concurrency signals as **NOT YET INSTRUMENTED** because a log pipeline could only extract
them by fragile prose-grepping, and the RTCP-active teardown branch had no test (every adapter
test set `_rtcp_active=False`, so the quality log path was never exercised).

We want these signals countable from logs **now**, with no new infrastructure, no external
metrics dependency, and no change to the existing human-readable messages (so existing log
consumers and tests keep working).

## Decision

Attach a machine-parseable `extra={}` dict to the existing lifecycle and RTCP log calls,
keying every record on a stable `event` discriminator plus the call's `call_id`:

- `invite_received` — inbound INVITE arrives (`+extension`).
- `call_rejected` — any pre-200-OK reject; `outcome="rejected"`, `sip_code`, and a stable
  `reason` token (e.g. `at_capacity`, `caller_declined`, `no_common_codec`). A shared
  `_rejected_extra(call_id, sip_code, reason)` helper builds the dict at every reject site
  (486/603/488/422) so the field set never drifts.
- `call_answered` — the inbound `200 OK` is sent (`outcome="answered"`, `sip_code=200`),
  emitted from the shared `_send_answer_200` seam (inbound-only — the three inbound media
  setup helpers are its only callers).
- `call_loop_started` — the conversational loop goes live (`direction` = `inbound`/`outbound`).
- `call_released` — the admission slot is freed at teardown; carries `duration_s`
  (release − admit, from a new `_admission_start` monotonic stamp recorded in `_admit_inbound`)
  and `active_calls` (the live concurrency gauge = slots still held after this release).
- `rtcp_call_quality` — the teardown RTCP snapshot (gated on `_rtcp_active`), carrying the
  five `CallQuality` numeric fields.

The human message text is unchanged at every site; the structured fields ride alongside via
the stdlib logger's `extra=` kwarg. This is **LOCAL-ONLY stdlib logging** — no metrics sink,
no network, no new dependency. Mapping these events to StatsD/Prometheus gauges is deferred
(runbook 0014 roadmap); introducing such a sink is an infra decision requiring its own ADR.

## Alternatives considered

- **A dedicated metrics/telemetry sink (StatsD/Prometheus/OpenTelemetry) now.** Rejected: it
  is new infrastructure/dependency (rule 40 — requires explicit operator approval) and is not
  needed to make the signals countable. Structured stdlib logs are the zero-infra floor; a sink
  can consume these same events later.
- **A new structured-event emitter abstraction.** Rejected as over-engineering for five call
  sites; `extra=` on the existing `_log` calls is the minimal change and keeps each event next
  to the behaviour it describes.
- **Replacing the human messages with pure structured records.** Rejected: it would break
  existing plain-text log consumers and the runbook's `grep` recipes; the additive `extra=`
  preserves both.

## Consequences

- Runbook 0014's call-setup-success, RTP-quality, and concurrency signals are now countable
  from JSON-formatted logs (`jq 'select(.event==…)'`) without prose-grepping; the runbook is
  updated in the same change with the field tables and `jq` recipes.
- **PUBLIC-repo safe (rule 34):** every field is a Call-ID, a SIP code, a stable reason token,
  a numeric metric, or the registration extension index — never caller PII, never a host/IP/
  secret. The 603 caller-declined path keeps its existing redacted-number message and adds only
  the non-PII `reason="caller_declined"`.
- The RTCP-active teardown branch is now covered by a test (a fake engine with
  `_rtcp_active=True`), closing the prior coverage gap.
- Negligible cost: one `dict` literal per logged event on paths that already log; the
  `_admission_start` map is pruned in lockstep with `_admitted_calls` (and cleared on
  `disconnect`), so it never outlives the slot it measures.
