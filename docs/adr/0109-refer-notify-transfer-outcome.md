# ADR-0109: Surface the terminal REFER/NOTIFY transfer outcome to the agent

- **Date:** 2026-07-10
- **Status:** Accepted
- **Deciders:** agent session (medium-severity transfer-correctness gap, backlog 1330)
- **Relates to:** ADR-0011 (multi-registration & call control — documents the
  REFER + `message/sipfrag` NOTIFY progress mechanism), the REFER `Refer-To`
  injection guard (`_validate_transfer_target`), RFC 3515 (REFER + implicit
  subscription), RFC 4488 (`Refer-Sub` / suppressing the subscription), RFC 3891
  (attended transfer / `Replaces`)

## Context

A blind or attended transfer sends a `REFER`. The referee answers `202 Accepted`
— which acknowledges only that the REFER was **received**, NOT that the transfer
succeeded. Per RFC 3515 the REFER creates an implicit subscription and the referee
reports real progress via `NOTIFY` requests carrying a `message/sipfrag` body: a
non-terminal `SIP/2.0 100 Trying` (subscription `active`), then a terminal
`SIP/2.0 200 OK` (success) or `SIP/2.0 4xx/5xx/6xx` (failure) with
`Subscription-State: terminated`.

Today the transfer tools return success on the `202`, never consuming the terminal
NOTIFY:

- `CallSession._refer` (`call.py:680`) awaits the REFER's final response and raises
  only on status `>= 400`; a `202` returns `None` — no NOTIFY is awaited.
- `VoipAdapter.transfer_blind_on_call` (`adapter.py:6391`) does
  `await session.transfer_blind(target)` then `return TransferOutcome.TRANSFERRED`
  with nothing in between; `complete_attended_transfer` (`adapter.py:6584`) is
  identical.
- The handler maps `TRANSFERRED` → `"Transfer to {target} initiated."`
  (`voip_tools.py:1512`) — the word "initiated", not "completed", is the honest
  tell.

The inbound half IS built: `CallSession._on_notify` (`call.py:963`) parses the
sipfrag via `parse_notify_sipfrag` and stores `self.transfer_progress:
NotifyProgress | None` (`call.py:1011`; `NotifyProgress` = `{status_code: int,
reason: str, terminated: bool}`). But `transfer_progress` is **write-only in
production** — read nowhere in `src/`. There is no async primitive tying transfer
completion to the terminal NOTIFY, and `TransferOutcome` has no member for a
NOTIFY-reported outcome.

Net effect: an agent that transfers a caller is told the transfer "initiated" even
when the callee was busy / rejected / unreachable. The agent cannot recover (offer
to take a message, try another target) because it never learns the transfer failed.
We send no `Refer-Sub: false`, so the implicit subscription is active and a
compliant referee WILL send the NOTIFYs — the machinery to observe them just is not
wired to the tool result.

## Decision

Make the transfer tools **wait, bounded, for the terminal transfer-progress NOTIFY
after the REFER is accepted, and return the real outcome.** Applies to BOTH blind
and attended transfer.

### 1. Synchronous bounded-wait (not async surfacing)

The agent drives transfers through a tool call; the natural contract is
one-call → real-outcome. After the REFER 2xx, the adapter awaits the terminal
NOTIFY up to a bounded timeout, then returns. Rejected alternative: returning
"initiated" immediately and surfacing the outcome later via an out-of-band agent
message — there is no such agent-notification channel in the tool model, and adding
one is new infrastructure (rule 40) far larger than this gap warrants.

### 2. Async mechanism (`CallSession`)

- A per-transfer `asyncio.Event` (`_transfer_terminal`) is created and
  `transfer_progress` reset to `None` **before** the REFER is sent, so a terminal
  NOTIFY racing the `202` is never missed.
- `_on_notify` sets the event when it stores a `NotifyProgress` with
  `terminated is True` (a non-terminal `100 Trying` updates `transfer_progress` but
  does not set the event).
- A new `await _await_transfer_outcome(timeout)` waits for the terminal event OR
  call-end (the leg being BYE'd), whichever comes first, and returns the terminal
  `NotifyProgress` or `None`.

### 3. Outcome classification (pure helper)

- terminal NOTIFY `200–299` → **`COMPLETED`**.
- terminal NOTIFY `300–699` → **`FAILED`**, carrying `status_code` + `reason`.
- no terminal NOTIFY within the timeout → **`OUTCOME_UNKNOWN`** (`reason=timeout`).
- leg BYE'd before any terminal NOTIFY → **`OUTCOME_UNKNOWN`** (`reason=call ended`).
  We do **not** infer success from a BYE: a proxy may tear the referrer leg down on
  either success or failure, so inferring would lie.

### 4. Outcome vocabulary + tool contract

`TransferOutcome` gains `COMPLETED`, `FAILED`, `OUTCOME_UNKNOWN`
(`AttendedTransferOutcome` likewise); the adapter returns the outcome plus the
terminal `NotifyProgress | None` (a small frozen result record, so `FAILED` carries
the SIP status). `TransferOutcome.TRANSFERRED` becomes the internal
REFER-accepted intermediate, no longer the tool's terminal return. Agent-facing
messages:

- `COMPLETED` → `"Transfer to {target} completed."`
- `FAILED` → `"Transfer to {target} failed: {status} {reason}."`
- `OUTCOME_UNKNOWN` → `"Transfer to {target} initiated; outcome not confirmed
  within {N}s."` (keeps the honest "initiated" wording for the genuinely-unknown
  case).

`NO_CALL` / `REFUSED` (DTMF) / `BLOCKED` are unchanged.

### 5. Timeout knob

New `HERMES_VOIP_TRANSFER_OUTCOME_TIMEOUT_S` (float seconds), default **20.0** —
blind transfers usually resolve in 1–8 s including callee ring, and 20 s bounds a
slow-ring case without hanging the tool. `0` opts out of the wait (preserves the
prior fast "initiated" behaviour → `OUTCOME_UNKNOWN`); negative is rejected at
config load (mirrors the existing non-negative knobs). Parsed in `load_media_config`
via `_parse_non_negative_int`'s float analogue; registered in both `plugin.yaml`
copies and `_KNOWN_ENV_KEYS`.

### 6. RFC 4488 (`Refer-Sub: false`) fast path

If the referee's `2xx` to the REFER carries `Refer-Sub: false` it has declined the
subscription; no NOTIFY will arrive. Detect it and return `OUTCOME_UNKNOWN`
immediately rather than waiting the full timeout. (The timeout is the functional
backstop if the header is absent but the peer still never NOTIFIes.)

## Consequences

- **Correctness/UX:** the agent learns whether a transfer actually completed and can
  recover on failure — the point of the feature.
- **Latency:** a transfer tool call now blocks up to `TRANSFER_OUTCOME_TIMEOUT_S`
  (default 20 s) in the unknown/slow case; the common success case returns as soon
  as the terminal NOTIFY arrives (typically 1–8 s). Operators who prefer the old
  immediate return set the knob to `0`.
- **Efficiency (rule 22):** an `asyncio.Event` wait, no polling; O(1) memory, no CPU
  between NOTIFYs. The only added cost is bounded tool-call latency, which is the
  intended trade.
- **Robustness (rule 37):** a malformed NOTIFY is still answered `400` by
  `_on_notify` and never sets the terminal event, so it collapses to the timeout
  path; the wait never raises past `OUTCOME_UNKNOWN`. The BYE-race is handled
  explicitly (§3). DTMF-confirm gating is unchanged (it precedes the REFER).
- **Tool-contract change:** transfer tool results change from always-"initiated" to
  reporting the real outcome. This is a behavioural change agents/operators must
  expect; it is documented here and covered by tests.
- **Test plan (TDD):** terminal-200 → `COMPLETED`; terminal-486/603 → `FAILED` with
  status; timeout (no NOTIFY) → `OUTCOME_UNKNOWN`; BYE before terminal →
  `OUTCOME_UNKNOWN`; `Refer-Sub: false` → immediate `OUTCOME_UNKNOWN`; terminal
  NOTIFY racing the 202 (event created pre-REFER) → `COMPLETED`; both blind and
  attended paths; config parse/validation of the new knob; the agent-facing message
  strings.

## Alternatives considered

- **Async out-of-band surfacing** — rejected (§1): no agent-notification channel;
  new infrastructure.
- **Infer success from the referrer leg being BYE'd** — rejected (§3): a BYE follows
  both success and failure; inferring would report false successes.
- **No wait, just expose `transfer_progress` for the agent to poll** — rejected: the
  agent has no polling loop and the tool call is the only touch-point; a written-but-
  unread field (today's state) is what this ADR removes.
