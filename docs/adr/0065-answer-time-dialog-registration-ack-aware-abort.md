# ADR-0065: Register the answered dialog at answer-time; ACK-aware teardown of a post-200 media-setup failure

- **Date:** 2026-06-19
- **Status:** Accepted
- **Deciders:** operator (team-lead, "Option B") — agent session (SIP DTLS-SRTP lane)

## Context

The DTLS-SRTP (ADR-0053 Stage 2) and WebRTC (ADR-0032) inbound paths send the
`200 OK` **before** running their media handshake (the peer needs our
fingerprint/role/ICE creds to start its half — RFC 5763 §4). A handshake failure
(fingerprint mismatch, ICE/DTLS timeout) is therefore detected **after** the call is
answered: the SIP dialog is established on the peer, so the failure must be torn down
with an in-dialog **BYE** (RFC 3261 §15.1), not a 4xx.

Two coupled defects made that teardown unreliable (codex review of PR #151):

1. **The dialog is registered too late.** Both `_setup_sip_dtls_call` and
   `_setup_webrtc_call` run the handshake, then return, and only **after** that does
   the inbound handler build the `CallSession` and call
   `manager.add_call`/`transport.add_call`. So during the handshake **no dialog is
   registered** — an inbound `2xx`-ACK is routed by `manager._route_in_dialog` to
   `None` → `Unroutable("no matching dialog")` → dropped at DEBUG (the WebRTC e2e
   harness already documents this "ACK briefly unroutable" reality). The call never
   *observes* its own confirmation.
2. **The abort sent a pre-ACK BYE.** The first round-2 fix sent the BYE immediately on
   a post-200 failure. But RFC 3261 §15.1.1 forbids sending BYE on a dialog that is
   not yet **confirmed** (no ACK received for the 2xx) — and a DTLS fingerprint
   mismatch is detected on the first handshake message, which can arrive **before** the
   peer's ACK. A BYE in an unconfirmed dialog may be ignored/rejected, leaving the
   exact half-open call the fix meant to close.

The investigation confirmed there is **no** existing ACK-tracking machinery for this
window: `CallSession.handle_request` treats an inbound ACK as a no-op (it has no
`on_ack` hook / dialog-confirmed signal), and `InviteServerTransaction` terminates on
a 2xx without arming a retransmission/Timer-H timer over the reliable TLS transport.

## Decision

**Register the answered dialog's in-dialog route at answer-time — immediately after
the `200 OK` and before the handshake — with a lightweight `_AnsweredDialogGuard`, and
make the post-200 abort ACK-aware** (close media now; send the BYE only once the dialog
is confirmed, with a bounded fallback). This reuses the existing `add_call` routing
(no new transport plumbing) and applies identically to the DTLS-SRTP and WebRTC paths.

Concretely (`src/hermes_voip/adapter.py`):

- **`_AnsweredDialogGuard`** implements `DialogConsumer` (`handle_request`) and
  `CallResponseSink` (`on_response`). It owns the `Dialog`, the signalling transport,
  the `call_id`, and an `asyncio.Event`. `handle_request`: an `ACK` confirms the dialog
  (sets the event); a peer `BYE` is answered `200 OK`, marks `peer_bye`, and sets the
  event (the dialog is now ended by the peer); an in-window re-`INVITE` is answered
  `491 Request Pending` (media is not up yet); anything else is benignly absorbed.
  `await wait_confirmed(timeout)` blocks until the ACK/BYE arrives or the timeout
  elapses.
- **Registration at answer-time:** right after `_send_answer_200`, the setup helper
  registers the guard via `manager.add_call(dialog_id, guard)` +
  `transport.add_call(call_id, guard)`. The `2xx`-ACK now routes to the guard (no longer
  unroutable) and confirms the dialog.
- **Success:** the inbound handler builds the real `CallSession` and its existing
  `add_call`/`transport.add_call` **overwrite** the guard on the same keys — a seamless
  upgrade; the guard is discarded.
- **Failure (handshake or engine setup), shared `_abort_answered_call`:** close the
  media session/engine **immediately** (release the UDP socket / SRTP state / ICE), then
  `await guard.wait_confirmed(timeout=_ANSWERED_ABORT_ACK_TIMEOUT_S)` (≈ Timer H, 32 s).
  If the peer already sent a `BYE`, the dialog is closed — only deregister. Otherwise
  send the in-dialog `BYE` on the dialog (`build_in_dialog_request(dialog, "BYE")` — the
  same request `CallSession.hang_up` builds), then deregister the guard. The bounded
  wait guarantees the abort completes even if the ACK never comes (a fallback BYE is
  still sent — a peer that answered but never ACKed is non-conformant; closing the
  dialog is the safe action and a stray BYE on a truly-dead dialog is harmless).

**Security invariant (preserved):** the guard is **signalling only** (ACK/BYE/CANCEL
routing). The media engine is still constructed and `connect()`ed **only after the
handshake verifies the peer certificate fingerprint** — registering the dialog route
early does **not** start any RTP/inbound-audio path. No media flows before
verification (RFC 5763 §5). A regression test asserts the engine is not constructed
during the handshake window.

## Consequences

- A post-200 DTLS/WebRTC failure now closes the dialog **reliably and RFC-correctly**
  (BYE only on a confirmed dialog), instead of a best-effort pre-ACK BYE that a strict
  peer could ignore. No half-open answered calls.
- The "ACK briefly unroutable during the handshake" window is closed for **all**
  answered calls (DTLS, WebRTC), so an ACK that races the handshake is consumed, not
  dropped — and the WebRTC e2e harness's workaround comment is removed (rule 27).
- One new small per-call object during the handshake window (the guard), discarded on
  success (overwritten by the real `CallSession`) — negligible cost, no steady-state
  overhead (rule 22).
- The abort path can wait up to ≈32 s for an ACK before its fallback BYE, but media is
  released **immediately**, so no media resource is held during that wait — only the
  small guard + the dialog bookkeeping.
- We do **not** add transport-layer ACK plumbing or a Timer-H in the transaction layer;
  the confirmation signal is the routed ACK landing on the guard, and the fallback is a
  local `asyncio.wait_for` in the abort. The plain-RTP / SDES paths are unchanged (their
  media is up before the 200 OK, so there is no post-200 handshake window).

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Keep the immediate (round-2) BYE | Violates RFC 3261 §15.1.1 — a pre-ACK BYE on an unconfirmed dialog may be ignored, leaving the call half-open (the bug it meant to fix). |
| Add a transport-layer ACK observer + Timer H in `InviteServerTransaction` (Option A) | Larger surface; invents parallel ACK plumbing for a window the existing `add_call` routing already covers once registration is moved earlier. |
| Immediate BYE + one delayed retransmit (Option C) | Pragmatic but still not §15.1.1-correct (the first BYE is still pre-ACK); a band-aid, not a fix. |
| Build the full `CallSession` before the handshake | The `CallSession` needs the connected engine + `local_media`, which only exist post-handshake; building it early would couple media construction into the pre-verification window. The guard is a minimal signalling-only stand-in instead. |
