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
- **An explicit terminal outcome (`_DialogOutcome`).** The guard's terminal state is an
  **enum** — `ACK_CONFIRMED | PEER_BYE | TIMEOUT` — not a mutable flag read at one
  moment. `peer_ended` is an instantaneous property (the success-path check);
  `await wait_outcome(timeout)` blocks until the ACK/BYE arrives (or the bound elapses)
  and returns the outcome, with **`PEER_BYE` winning over `ACK_CONFIRMED`** so a peer
  BYE racing the ACK closes the double-BYE window. Winning requires two waits, because
  the transport drives **one sequential read loop**: a BYE the peer sends right behind
  its ACK is a separate, not-yet-read frame when the ACK wakes the waiter, so a single
  post-wake re-check would still return `ACK_CONFIRMED` and the abort would BYE into the
  peer's arriving BYE (glare). So after an ACK confirms, `wait_outcome` waits a short
  bounded **settle** (`_ACK_BYE_SETTLE_S`, 0.5 s) for a trailing BYE to supersede it;
  a BYE after the settle is independent teardown the SIP layer absorbs (both-sides BYE,
  RFC 3261 §15). `handle_request` sets the peer-BYE flag *before* its 200-send `await`,
  so the flag is observable the instant the BYE frame is dispatched. Making the outcome
  explicit collapses the concurrency edges (peer-BYE-during-success, double-BYE on a
  BYE during the wait, and double-BYE on a BYE trailing the ACK) into one race-free
  decision used by both the success path and the abort.
- **Registration at answer-time:** right after `_send_answer_200`, the setup helper
  registers the guard via `manager.add_call(dialog_id, guard)` +
  `transport.add_call(call_id, guard)`. The `2xx`-ACK now routes to the guard (no longer
  unroutable) and confirms the dialog.
- **Success — peer-ended check (`_abort_if_peer_ended_during_setup`):** after the
  handshake **and** the engine are up, but **before** the inbound handler registers the
  real `CallSession` / starts the `CallLoop`, the setup helper checks `guard.peer_ended`.
  If the peer BYE'd during the handshake, the dialog is gone: it stops the
  just-connected engine, closes the session, deregisters the guard, and raises
  `_AnsweredCallPeerEnded` so the inbound handler returns with **no `CallLoop`** (a clean
  end, no BYE from us — the peer ended it). Otherwise the real `CallSession`'s existing
  `add_call`/`transport.add_call` **overwrite** the guard on the same keys (a seamless
  upgrade). There is no `await` between the `peer_ended` check returning and the
  overwrite, so check + registration are atomic relative to inbound routing on the
  single-threaded loop (a later BYE then routes to the real `CallSession`).
- **Failure (handshake or engine setup), shared `_abort_answered_call`, non-blocking:**
  release media **immediately and synchronously** (stop the engine / close the session —
  release the UDP socket / SRTP state / ICE), then spawn the bounded ACK-wait + BYE as a
  **tracked background task** (`_finish_answered_abort`) and return, so the inbound
  handler and its **admission slot are freed at once** (a flood of failing handshakes
  cannot exhaust admission). The background task `await guard.wait_outcome(...)`
  (≈ Timer H, 32 s) and BYEs the dialog only on `ACK_CONFIRMED` / `TIMEOUT` — **never on
  `PEER_BYE`** (the peer already ended it, so no double-BYE) — then deregisters the
  guard. The `TIMEOUT` fallback BYE still closes a dialog whose peer answered but never
  ACKed (non-conformant; a stray BYE on a truly-dead dialog is harmless). The task is
  registered in `_call_tasks` so `disconnect` cancels + awaits it within the bounded
  shutdown (no orphaned task / unobserved exception, rule 37).

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
