# Multi-registration + call-control build plan

Implements ADR-0011. TDD-ordered (rule 18); each PR is sans-IO and fake-testable unless
marked **[live]** (needs the ADR-0005 transport / Hermes runtime, i.e. P2/P3). Every module
composes on the merged `message`/`sdp`/`registration`/`digest`/`dtmf`/`policy` modules with
**no existing signature change**. Fakes only: `pbx.example.test`, ext `1000`/`1001`, RFC 5737
addresses.

## Build-now vs blocked (honest split)

- **Buildable now (sans-IO, pure):** PR1–PR6 below — `SipRequest.parse`, `config.py`,
  `dialog.py`, `incall.py` (hold/unhold + inbound classify + 491), `refer.py` (REFER/Replaces/
  NOTIFY-sipfrag build+parse), the `RegistrationFlow.call_id` property. These mirror the
  existing foundation and need no transport, model, or credential.
- **Shipped (PR7–PR9):** `manager.py` (`RegistrationManager`, N flows, shared transport, demux),
  `call.py` (`CallSession` orchestrator), and `tools.py` (hold/resume/transfer/dtmf/list tools
  wired into `register(ctx)`) are all merged and wired. The "blocked on live transport" caveat
  from the original plan no longer applies — all three modules shipped alongside the concrete
  `SipTransport`/`MediaTransport` and the Hermes adapter.

## Phased PRs

| PR | Module | Scope | TDD focus |
|----|--------|-------|-----------|
| **PR1** | `message.SipRequest` | A `SipRequest.parse` sibling of `SipResponse.parse` (method + request-URI + headers + body), same parsing style + the existing hardening (header unfolding, control-char rejection). | request-line parse, in-dialog header extraction (Call-ID/CSeq/To-tag/From-tag), reject malformed. |
| **PR2** | `config.py` | Parse the indexed `HERMES_SIP_EXTENSIONS` scheme → `tuple[RegistrationConfig, ...]`; single-extension `HERMES_SIP_EXTENSION` back-compat. | N-extension parse, back-compat, missing/duplicate/garbled env rejects. |
| **PR3** | `dialog.py` | `Dialog` (peer target, route set, tags, **dual** `local_cseq`/`sdp_version` counters) + `from_invite_2xx`/`from_inbound_invite` constructors + `build_in_dialog_request`. | **invariant 1**: both counters increment independently; in-dialog header block correctness. |
| **PR4** | `incall.py` | `build_hold_reinvite(sendonly/sendrecv)`, `handle_reinvite_response` → `HoldConfirmed|ReinviteChallenged(401/407)|ReinviteRejected(491…)`, `classify_inbound_reinvite` (answer mirrored direction; `c=0.0.0.0`/`sendonly`/`inactive` → held; glare → 491). | hold/unhold offer SDP (`a=sendonly`/`a=sendrecv` + o= version bump), inbound-hold classify, 491 glare, digest re-auth on challenge. |
| **PR5** | `refer.py` | `build_blind_refer`, `build_attended_refer` (Replaces-into-Refer-To), `parse_refer`, `build_triggered_invite`, `match_replaces` (RFC 3891 tag orientation), `build_notify_sipfrag` + tiny status-line sipfrag parser, `norefersub` handling. | blind vs attended REFER shape, Replaces escape/parse round-trip, Replaces match (481/603/486 cases), sipfrag 1xx/2xx/≥300 classification. |
| **PR6** | `registration.py` | +1: read-only `call_id` property for manager demux. | property returns the flow's stable Call-ID. |
| **PR7 [shipped]** | `manager.py` | `RegistrationManager`: owns N flows, shared-per-registrar transport, per-flow refresh timers, `on_response`/`on_request` demux (Call-ID for responses; Request-URI user-part for INVITEs; dialog key for in-dialog). | demux routing (**invariant 2**: response→right flow, INVITE→right registration by user-part), at-least-one-up `connect`. Logic testable with a fake `SipTransport`. |
| **PR8 [shipped]** | `call.py` | `CallSession`: owns `Dialog`+`MediaTransport`+`GuardSessionState`; `hold/unhold/transfer_blind/transfer_attended`; inbound re-INVITE/REFER/NOTIFY handling; glare serialisation; MOH/jitter-pause on hold. | the verb orchestration against a fake transport + the sans-IO modules; attended = hold + consult + Replaces REFER. |
| **PR9 [shipped]** | `tools.py` + adapter wiring | Register `hold_call`/`resume_call`/`transfer_blind`/`transfer_attended`/`list_registrations` with `ToolRisk`; `pre_tool_call` → `gate_tool_call`; confirmation sourced from the per-call `DtmfReceiver`. | **invariant 3**: `IRREVERSIBLE` transfer blocked unconfirmed / while `degraded`, even when the guard returned `ALLOW` (the ADR-0009 classifier-miss test). |

## Sequencing vs the main plan

PR1–PR6 are independent of the P1 ML providers and the P2 transport — they can ship in
parallel with `IMPLEMENTATION-PLAN.md`'s P1 (VAD/guard/STT-TTS). PR7–PR9 land alongside P3
(the `VoipAdapter` + call loop), since they share the live transport and the adapter. The
agent-facing transfer tools reuse ADR-0009's `gate_tool_call` verbatim — no new policy code.

## The three invariants under test (rule 18)

1. A hold re-INVITE bumps **CSeq = N+1 AND o= version = M+1** with an identical session-id.
2. A registration's Call-ID/CSeq is never reused by a call dialog; responses route by Call-ID.
3. An `IRREVERSIBLE` transfer with `confirmed=False` returns `gate_tool_call → False` even
   when the injection guard returned `ALLOW`.
