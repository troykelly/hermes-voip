# ADR-0068: Outbound SIP CANCEL (RFC 3261 §9.1) for an unanswered `place_call`

- **Date:** 2026-06-20
- **Status:** Accepted
- **Deciders:** operator (direction) — agent session (outbound-cancel lane)

## Context

The plugin is a full UAC for outbound calls (`place_call`, ADR-0019/0029/0049/0067), but
it has **no client-side CANCEL**. RFC 3261 §9.1 says a UAC that wants to give up on an
INVITE it has sent — but for which **no final response** has yet arrived — sends a CANCEL.
We only handle the *inbound* CANCEL direction (§9.2, ADR-0065's sibling work in
`connection.py`: `_PendingInvite` + `_handle_cancel` + late-200 suppression). There is no
`build_cancel` anywhere in `src/`; the only `CANCEL` literals are `Allow`-header capability
advertisements.

The consequence is a real defect:

- `_handle_outbound_invite` parks in `_QueueSink.get()` (35 s `_OUTBOUND_INVITE_TIMEOUT`)
  during the whole ring. On a callee that never answers, that `get()` raises
  `asyncio.TimeoutError`; the `finally` tears down **local** media only — it never sends a
  CANCEL, so the INVITE transaction is abandoned on the wire and the gateway must rely on
  its own Timer C (~3 min) to reap the leg.
- There is **no programmatic way** for the agent / operator to abort an outbound INVITE
  that is still ringing. A long-running `place_call` cannot be interrupted before the 2xx.
- A 180-Ringing is provisional, so the awaiter sits in `get()` for the full ring with no
  exit lever.

Constraints: fully-typed `mypy --strict`, errors propagate (rule 37), the plugin stays
gateway-agnostic (CANCEL is standard RFC behaviour, no vendor quirk), no secrets/PII in
tracked files or logs (rule 34), TDD (rule 18).

A CANCEL has exact wire requirements (§9.1): the CANCEL's request line and **every** header
(Request-URI, `Call-ID`, `From` including tag, `To` **without** any tag added, the topmost
`Via` with the **same branch** as the INVITE, and the same `CSeq` **number** with method
`CANCEL`) match the INVITE; any `Route` headers are repeated; the body is empty. Building it
therefore requires the original INVITE's Via branch and routing headers — which the current
client-transaction tracking (`_client_txns`, keyed `(Call-ID, CSeq-num)`) records as an
`InviteClientTransaction` but does not expose.

## Decision

**Add a client-side CANCEL.** The transport learns to *build and send* the §9.1 CANCEL and
to *suppress the glare* of a late 2xx; the adapter exposes a public `abort_call` and a
`ring_timeout_secs` knob on `place_call`, and maps the resulting `487 Request Terminated`
to a new typed `OutboundCallCancelled`.

### Transport (`src/hermes_voip/transport/connection.py`)

- A lightweight `_OutboundInvite` record (request-URI, the **exact** top `Via`, `From`,
  `To`, `Call-ID`, CSeq number, `Route` headers) is captured **alongside** the
  `InviteClientTransaction` whenever we send an INVITE — keyed `(Call-ID, CSeq-num)` (same
  key as `_client_txns`). The re-auth INVITE (CSeq 2) overwrites CSeq 1, so the tracked
  record is always the latest in-flight transaction (the one a CANCEL must target).
- `async send_cancel(call_id) -> bool` builds the §9.1 CANCEL from the most recent tracked
  outbound INVITE for `call_id` and sends it; returns `True` when an INVITE was tracked and
  a CANCEL went out, `False` when there is nothing to cancel (no in-flight INVITE — e.g.
  the call already got its final response). It records `call_id` in a `_cancelled_outbound`
  set so a racing 2xx is suppressed (below).
- **Glare suppression (mirrors the inbound §9.2 suppression).** RFC 3261 §9.1: a 2xx can
  race the CANCEL. When a `2xx`-to-INVITE arrives for a `call_id` in `_cancelled_outbound`,
  the transport **does not route it to the call's sink** (so `place_call` never treats the
  dead call as answered) and — because the 2xx established a dialog on the callee — it
  sends an `ACK` then an in-dialog `BYE` so the remote tears down cleanly rather than being
  stranded. This is the RFC-correct realisation of "suppress": the local side ignores the
  stale success, the remote side is closed. The ACK/BYE are built from the tracked INVITE +
  the 2xx (`To`-tag/`Contact`/`Record-Route`) and are best-effort (a send failure is logged
  structurally, never the key/PII — rule 34/37).
- `remove_call` clears the `_OutboundInvite` records and the `_cancelled_outbound` entry
  for the torn-down `call_id` (the call is definitively gone).

### Adapter (`src/hermes_voip/adapter.py` + `src/hermes_voip/originate.py`)

- **`OutboundCallCancelled(Exception)`** (new, in `originate.py`) — raised by `place_call`
  when the INVITE is answered `487 Request Terminated` (our CANCEL took effect). It is
  **distinct** from `OutboundCallFailed`: a 487 after we asked to cancel is the *expected*
  outcome of an abort/ring-timeout, not a peer error. It carries the `call_id` and the
  reason string.
- **`async abort_call(call_id, reason) -> bool`** (public) — sends the CANCEL via
  `transport.send_cancel`, marks the in-flight outbound call cancel-requested, and **stops
  the engine immediately** (the socket-leak guard: we are about to keep awaiting the late
  `487`, which a hung gateway might never send within the sink timeout — the RTP socket must
  not stay open for that whole window). Idempotent: a second `abort_call` for the same call
  is a no-op. A no-op returning `False` when the call is unknown or already answered (the
  CANCEL would be too late — §9.1 only applies before the final response). The half-built
  call's own `_handle_outbound_invite` `finally` performs the rest of teardown when its
  `await` unblocks with the 487.
- **`place_call(..., ring_timeout_secs: float | None = None)`** — when set, arms a tracked
  timeout task that, after `ring_timeout_secs`, calls `abort_call(call_id, "ring timeout")`.
  The task is **cancelled on the 2xx** (the call answered — no abort). Default `None` keeps
  today's behaviour (only the 35 s hard sink timeout bounds the wait).
- The `place_call` response-await loop **absorbs the `200 OK` to the CANCEL** (a response
  whose CSeq method is `CANCEL` is skipped, never mistaken for the INVITE's final) and, when
  cancellation has been requested, treats the `487` as `OutboundCallCancelled`.
- In-flight outbound calls are tracked in `_outbound_pending` (`call_id` → the engine + a
  cancel-requested flag + the ring-timeout task handle) so `abort_call` can find the call
  and the establishment path can observe the request.

This is WSS-symmetric in spirit, but **scoped to the SIP/TLS UAC** (`_handle_outbound_invite`)
for this lane: the WSS WebRTC UAC (`_handle_outbound_webrtc_invite`) keeps today's behaviour
(its abort path is a follow-on; the WSS transport frames a different exchange and has no
`_client_txns`/Via transaction registry).

## Consequences

- An unanswered outbound call can be aborted deterministically — by a `ring_timeout_secs`
  or an explicit `abort_call` — and the gateway sees a proper CANCEL instead of an
  abandoned transaction, so the callee stops ringing immediately rather than after Timer C.
- The RTP socket is released the instant we abort (engine stopped before the late-`487`
  await), closing the no-answer socket-leak window.
- A 487 is now a first-class, typed, non-error outcome (`OutboundCallCancelled`), so callers
  can distinguish "we cancelled" from "the peer rejected" (a 4xx/5xx/6xx → `OutboundCallFailed`).
- We are committed to maintaining the outbound CANCEL glare handling in lockstep with the
  inbound §9.2 suppression — both live in `connection.py` and share the "a 2xx can race the
  CANCEL" invariant.
- `place_call` gains one optional kwarg; existing callers are unchanged (default `None`).

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Rely on the gateway's Timer C to reap the un-CANCELled leg | Leaves the callee ringing for minutes, leaks our RTP socket for the whole sink-timeout window, and gives the agent/operator no abort lever — the defect this ADR fixes. |
| Build the CANCEL in the adapter from the INVITE text it kept | The adapter does not keep the sent INVITE wire text (it builds a *synthetic* INVITE for the dialog); the transport already owns the client-transaction registry and the Via branch, so CANCEL construction belongs there (single source of the branch, §9.1). |
| Wrap the response await in `asyncio.timeout()` and just raise `TimeoutError` | Sends **no** CANCEL (the wire defect remains), produces an untyped failure, and gives no public `abort_call` for an agent-driven abort. |
| Treat the racing 2xx by ACK-only and letting the callee's session timer reap it | Strands an established dialog on the callee until its timer fires; §9.1 expects the UAC that cancelled to ACK **and** BYE a 2xx that slips through. We send both. |
| Suppress the late 2xx by dropping it silently with no ACK/BYE | Violates RFC 3261 §13.2.2.4 (a 2xx MUST be ACKed) and strands the remote dialog; "suppress" here means *don't treat as success locally*, not *ignore on the wire*. |
