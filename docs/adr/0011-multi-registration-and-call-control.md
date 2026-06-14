# ADR-0011: Multiple registrations + in-call control (hold, blind/attended transfer)

- **Date:** 2026-06-14
- **Status:** Accepted
- **Deciders:** agent session (operator requirement)

## Context

The operator requires the plugin to (1) hold **multiple simultaneous SIP registrations**
(one plugin instance registers N extensions at once) and (2) offer in-call control:
**hold/un-hold** and **transfer** (blind and attended). ADR-0002 framed the plugin around a
single `kind: platform` adapter owning the media plane; ADR-0005 routes transport/media; the
merged `registration.py` (`RegistrationFlow`) is a sans-IO, transaction-aware REGISTER flow
for **one** extension. This ADR extends those without changing any existing module signature.

Binding facts (RFC-verified):

- **Hold is not a SIP method** — it is an in-dialog re-INVITE carrying a new SDP offer that
  changes only the media direction. RFC 3264 §8.4: a `sendrecv` stream is held by marking it
  `sendonly`; RFC 6337 §5.3 prefers `sendonly` over `inactive` (it permits music-on-hold and
  never produces a worse outcome). The held peer answers `recvonly` (RFC 3264 §6.1). Un-hold
  is another re-INVITE restoring `sendrecv`. The legacy `c=0.0.0.0` hold (RFC 2543) is
  deprecated: we **tolerate it on receive** but **never generate** it.
- **The `o=` (origin) version MUST increment on every new offer** (RFC 3264 §8) — independent
  of the dialog CSeq. A re-INVITE that bumps CSeq but reuses the SDP version is a no-op offer;
  this is the most common hold bug.
- **Transfer is REFER** (RFC 3515): blind transfer = `REFER` with `Refer-To: <target>`;
  attended transfer = `REFER` whose `Refer-To` embeds a `Replaces` header (RFC 3891) naming
  the consultation dialog. Progress is reported by an implicit subscription + `NOTIFY` with a
  `message/sipfrag` status-line body; `norefersub` (RFC 4488) may suppress it. The agent both
  makes and receives calls, so it plays **both** roles (RFC 5589): Transferor (sends REFER)
  and Transferee/Target (parses REFER, places the triggered INVITE, matches `Replaces`).
- **Glare** (RFC 3264 §4 / RFC 6337 §4.2): a UA must not emit a new offer while one is
  outstanding; a colliding inbound re-INVITE offer is rejected `491 Request Pending`.

## Decision

Add a `RegistrationManager` that owns N `RegistrationFlow` instances, and a sans-IO in-call
control layer (`dialog.py` / `incall.py` / `refer.py`) plus a `CallSession` orchestrator,
exposed to the agent as policy-gated tools. **Zero** existing signatures change; everything
composes on `message.py`, `sdp.py`, `registration.py`, and the `policy.gate_tool_call` /
`ToolRisk` types. The four design decisions:

**1. Multiple registrations — one manager, N flows.** A single async `RegistrationManager`
owns `list[RegistrationFlow]` (each the unchanged per-extension unit, with its own
credentials/Contact/Call-ID/CSeq/refresh timer). Config comes from an **indexed
`HERMES_SIP_*` scheme** parsed into `tuple[RegistrationConfig, ...]` (`config.py`), with
single-extension backward compatibility. Transport is **shared-per-registrar by default**
(one TLS socket carries all N) behind a per-registration handle, so a future separate-socket
policy needs no reshape. Inbound demux (the load-bearing routing):

- REGISTER **responses** → owning flow by **Call-ID**.
- Inbound **INVITE** (new call) → owning registration by **Request-URI user-part** (the
  registrar retargets to the registered Contact, so the user-part *is* the extension);
  fallback `To:` AOR user-part → configured **default registration**. When all N share one
  Contact host:port, the host is identical — the manager **MUST key on the user-part**.
- In-dialog request (re-INVITE / REFER / NOTIFY / BYE) → owning `CallSession` by dialog key
  `(Call-ID, local-tag, remote-tag)`.

**2. In-call control — a sans-IO layer + `CallSession`.**

- `dialog.py` — a `Dialog` state object (peer target, route set, tags, and **two independent
  counters** `local_cseq` and `sdp_version`) + a `build_in_dialog_request` helper (the
  in-dialog analogue of `RegistrationFlow._build`).
- `incall.py` — `build_hold_reinvite(dialog, media, target)` (`sendonly`/`sendrecv`),
  `handle_reinvite_response` (→ `HoldConfirmed | ReinviteChallenged | ReinviteRejected`,
  incl. 491), and `classify_inbound_reinvite` for the consume side (answer with the mirrored
  direction; glare → 491).
- `refer.py` — REFER/`Refer-To`/`Replaces`/`Referred-By` **build and parse**, the triggered
  INVITE, `match_replaces` (RFC 3891 §3 tag orientation), and `NOTIFY` sipfrag build + a tiny
  **status-line-only** parser.
- `CallSession` (the only IO-driving piece) owns the `Dialog`, the ADR-0005 `MediaTransport`,
  and the ADR-0009 `GuardSessionState`, exposing `async hold() / unhold() /
  transfer_blind(target) / transfer_attended(consult)`. Hold gates the RTP send (MOH/silence),
  **pauses (never destroys)** the jitter buffer, and keeps the socket + RTCP alive.

**3. Agent trigger — tools gated by the ADR-0009 policy.** Five tools register via
`ctx.register_tool`, each carrying a `ToolRisk`: `hold_call`/`resume_call` = `ELEVATED`
(reversible), `transfer_blind`/`transfer_attended` = **`IRREVERSIBLE`**, `list_registrations`
= `SAFE`. A `pre_tool_call` hook maps each tool to its risk and calls `gate_tool_call`
**verbatim**: an `IRREVERSIBLE` transfer requires explicit DTMF/human confirmation (ADR-0010
`DtmfReceiver`) **and** is hard-blocked while `degraded` — regardless of the injection-guard
verdict. No new policy code; the classifier-miss path is exactly ADR-0009's load-bearing
control.

**4. Placement — one adapter, N registrations.** One `VoipAdapter`, one `register_platform`
named `voip`, N SIP registrations inside it (the extension count is a media-plane detail
invisible to Hermes core, as ADR-0002 already mandates for the whole media plane).
`connect()` returns `True` once **at least one** registration succeeds (degraded-but-up). A
call is still one Hermes session (`chat_id ← Call-ID`); *which* extension received it is
carried as session metadata (the To-AOR user-part). The one genuinely new `message.py`
primitive is a `SipRequest.parse` sibling of `SipResponse.parse` (inbound re-INVITE/REFER/
NOTIFY/BYE are *requests*).

This amends ADR-0002's `requires_env` note only: the env scheme moves from a single
`HERMES_SIP_EXTENSION` to an indexed `HERMES_SIP_EXTENSIONS` list (single-extension
back-compatible). ADR-0002's seam, process model, and call→session mapping are unchanged.

## Consequences

- **No supersede, minimal blast radius.** New modules (`config`, `dialog`, `incall`, `refer`,
  `manager`, `call`, `tools`) + a one-line `RegistrationFlow.call_id` property + `SipRequest`.
  Existing modules are reused as-is. Each new sans-IO module is TDD-able against fakes exactly
  like the foundation; only `manager`/`call`/`tools` need the live transport (ADR-0005, P2/P3).
- **Three correctness invariants drive the tests:** (1) a hold re-INVITE bumps **both** CSeq
  and the `o=` version (identical session-id); (2) registration Call-ID/CSeq ≠ call-dialog
  Call-ID/CSeq (decoupled state machines, demux by Call-ID); (3) an `IRREVERSIBLE` transfer is
  hard-blocked unconfirmed or while `degraded`, even when the guard returned `ALLOW`.
- **Operational:** N idle registrations hold **zero** RTP ports (media is per-call); one
  flapping extension cannot down the adapter; a transfer hands the caller out of the agent's
  control, so it is correctly the highest-risk action and demands confirmation.
- **We commit to maintaining** the both-roles transfer surface (transferor + transferee +
  target/`Replaces`) and the glare serialisation; partial transfer support is worse than none.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| N `register_platform` entries (one per extension) | Multiplies Hermes platform entries, fragments session routing, and leaks the extension count into core config — the opposite of ADR-0002's "hide the media plane, zero core changes". Inbound demux is already solvable inside one adapter by Request-URI user-part. |
| `a=inactive` for hold | Mutes both directions, so no music-on-hold/comfort audio can flow; RFC 6337 §5.3 prefers `sendonly`, which never produces a worse outcome. Reserved for the genuine no-media case. |
| Legacy `c=0.0.0.0` hold | Deprecated (RFC 3264 §8.4): breaks RTCP, IPv6, and connection-oriented media. Tolerated on receive, never generated. |
| Per-registration media ports | N idle registrations would pin N RTP port pairs for nothing; media belongs to a call, allocated at SDP-answer time on the `CallSession`. |
| Folding hold/transfer into `registration.py` | Conflates the registration state machine with the dialog state machine; they have independent Call-ID/CSeq spaces (invariant 2). Separate `dialog.py`/`incall.py`/`refer.py` keep each testable in isolation. |
