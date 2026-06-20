# ADR-0071: RFC 4028 SIP session timers — Session-Expires / Min-SE keep-alive refresh

- **Date:** 2026-06-20
- **Status:** Accepted
- **Deciders:** agent session (session-timers lane, #74). Composes with the in-dialog
  re-INVITE/hold machinery (ADR-0011), the SDES re-key continuity (ADR-0053 / PR #135), the
  secure-media guard (ADR-0070), and the lifecycle teardown chokepoint (ADR-0026 / ADR-0059).

## Context

A SIP dialog can outlive the actual call: a `BYE` can be lost, a gateway or peer can vanish
without signalling, or a stateful proxy mid-path can lose its state. Nothing in the base
protocol periodically proves the dialog is still alive, so a half-dead dialog can pin our
per-call resources (an RTP socket, the STT/TTS/AEC/VAD pipeline, a registration slot,
admission capacity) indefinitely — the caller is in dead air, but we still think the call is
up.

RFC 4028 (Session Timers) closes this with a periodic **refresh**: the parties negotiate a
session interval (`Session-Expires`), one party (the *refresher*) re-sends an
INVITE/UPDATE before the interval elapses, and the other tears the dialog down with a `BYE`
if no refresh arrives in time. Before this change the plugin advertised no `Supported:
timer`, never inserted a `Session-Expires`, ignored an inbound one, and never rejected a
too-small interval — so a dead dialog was only reclaimed by the RTP-inactivity watchdog
(ADR-0026), which fires on *media* silence and not on a dialog the gateway believes is up
with media still flowing to a black hole.

## Decision

Implement RFC 4028 session timers end-to-end, as a UAS (inbound) and UAC (outbound).

### A pure RFC-math core (`session_timer.py`)

All the timer arithmetic and header grammar lives in a new sans-IO module so it is
exhaustively unit-testable off the event loop (no socket, no asyncio):

- `SessionExpires.parse` / `parse_min_se` — parse the `Session-Expires` (delta + optional
  `;refresher=uac|uas`) and `Min-SE` header values; a malformed value raises (the adapter
  treats a malformed inbound header as *absent* for robustness, RFC 3261 §8.1.3.2).
- `elect_refresher` — honour the peer's pinned `refresher`; otherwise pick our default
  (RFC 4028 §9: the UAS MUST NOT override the UAC's choice).
- `refresh_interval_secs(delta) = delta/2` (RFC 4028 §7.2/§9) — the refresher's cadence.
- `teardown_deadline_secs(delta) = delta - min(32, delta/3)` (RFC 4028 §10) — the
  non-refresher's BYE deadline, slightly before expiry.
- `negotiate_uas_timers` — the UAS decision: an offered interval below our `Min-SE` →
  `Reject422` (carry our Min-SE); at/above → `AcceptTimers` (honour the offered interval —
  the UAS MUST NOT *increase* it — and the elected refresher); no offer → `AcceptTimers`
  with our own configured interval (a timer-supporting UAS MAY insert one).
- `build_session_expires_value` — render `<delta>;refresher=<role>`.

Results are discriminated frozen dataclasses (`AcceptTimers` | `Reject422`), not
stringly-typed flags (rule 17).

### Inbound (UAS) negotiation in `_handle_inbound_invite`

A single negotiation step runs **after** the ADR-0070 secure-media `488` guard and the
SDP-has-audio check, **before** any `Dialog` / `CallSession` / media engine / admission
slot is created:

- An offered `Session-Expires` below `Min-SE` → `422 Session Interval Too Small` with a
  `Min-SE` header — no dialog, no media, no leaked task — and the UAC retries larger.
- Otherwise the accepted interval + elected refresher are carried into the dialog-forming
  `200 OK` (the single shared `_send_answer_200` seam, so all three media paths — SDES,
  SIP-DTLS, WebRTC — emit it identically): `Session-Expires` + `Supported: timer`, and
  `Require: timer` **only when permitted by RFC 4028 §9 / Table 2** — i.e. when the
  refresher is the UAC, or the refresher is the UAS *and the request advertised
  `Supported: timer`*. To a timer-IGNORANT UAC (no `Supported: timer`) we still insert our
  `Session-Expires` + `Supported: timer` and become the UAS refresher, but **omit
  `Require: timer`** (Table 2 disallows it with a non-supporting peer — a strict stack
  would reject the dialog). Our default refresher is **UAS** — *we* send the refreshes, so
  a peer that silently stops responding is detected by *our* refresh re-INVITE failing, not
  by waiting for the peer to refresh.

### Outbound (UAC) negotiation in `place_call`

The outbound INVITE carries `Session-Expires` (with `refresher=uac` — we offer to refresh)
+ `Supported: timer`. A `422` answer raises our `Session-Expires` to the peer's `Min-SE`
and re-sends the INVITE once (RFC 4028 §6), before the auth/final-response handling so a
`422` is never mis-classified as a call failure. The 2xx may echo a (possibly reduced)
`Session-Expires`; we honour the answered interval + refresher.

### The per-call watchdog (the only IO)

After the dialog is confirmed (200 OK sent / 2xx ACKed) a per-call asyncio watchdog is
started, tracked in `_session_timers` and `_call_tasks`:

- **Refresher:** sleep `SE/2`, then send a session-refresh re-INVITE. This **reuses the
  existing in-dialog re-INVITE machinery** — `CallSession.refresh_session` calls the same
  `_reinvite` path as hold/resume (so SDES re-key continuity, glare/491 handling, and
  digest re-auth all apply unchanged), carrying `Session-Expires` + `Supported: timer`.
  The refresh **re-asserts** the current media direction — `sendonly` while the call is on
  hold, else `sendrecv` — so refreshing a held call never silently un-holds it.
  `refresh_session` returns a **discriminated outcome** that the watchdog acts on per
  RFC 4028 §10 / RFC 3261 §14.1, rather than tearing the call down on every non-2xx: a
  **timeout / 408 / 481** is a dead dialog → `BYE`; a **491 glare** is retried after a
  randomized backoff (RFC 3261 §14.1: 2.1–4 s as the UAC, 0–2 s as the UAS), bounded to a
  few consecutive attempts and without resetting the SE deadline; **any other non-2xx**
  (5xx/6xx/488…) logs a warning and **continues** — a transient server error must not kill
  a live call, and the next `SE/2` tick (or the non-refresher's own deadline) still guards
  liveness.
- **Non-refresher:** sleep to `SE - min(32, SE/3)`; if the dialog has not ended (the peer
  never refreshed), `BYE` it.

The watchdog's sleep goes through an injectable `_session_timer_sleep` seam (default
`asyncio.sleep`) so tests drive the `SE/2` / teardown timing deterministically instead of
sleeping real minutes. It is cancelled first in `_teardown_call` so an in-flight refresh
never races the teardown's `BYE`.

### Config (`MediaConfig`)

Two integer fields with the RFC floor enforced at load (and on direct construction):

- `session_expires` (env `HERMES_VOIP_SESSION_EXPIRES`, default **600**) — the interval we
  offer/insert.
- `min_se` (env `HERMES_VOIP_MIN_SE`, default **90**) — the smallest we accept inbound.
- `__post_init__` enforces `min_se >= 90` (RFC 4028 §4/§5 floor) and
  `session_expires >= min_se` (else our own `Session-Expires` would be below our advertised
  minimum and a strict peer could 422 it). A sub-floor value is a loud `ConfigError` at
  startup, never a silently-accepted out-of-spec interval.

## Why this composes rather than duplicates

- **Re-INVITE/hold path (ADR-0011 / ADR-0053):** the refresh is *not* a new transaction
  type — it is a `sendrecv` re-INVITE through the same `build_hold_reinvite` →
  `_reinvite` → `_send_and_await_final` path, extended only with a `Session-Expires`
  extra-header. SDES re-key continuity (PR #135), the `491` glare answer, and the digest
  re-auth retry are inherited, not re-implemented.
- **Secure-media guard (ADR-0070):** the 422 negotiation runs *after* the `488`
  cleartext-reject, so a cleartext-and-too-small offer is still refused on security grounds
  first. They are independent reject gates in a fixed order.
- **RTP-inactivity watchdog (ADR-0026):** orthogonal — that detects *media* silence;
  this detects a *signalling* dialog the peer no longer refreshes. A call can be torn down
  by either.
- **Teardown chokepoint (ADR-0026 / ADR-0059):** the watchdog's BYE-on-expiry goes through
  the same idempotent `CallSession.hang_up`; the teardown cancels the watchdog up front so
  there is one BYE, never a double.

## Consequences

- **Dead dialogs are reclaimed.** A peer/proxy that vanishes is detected within ~`SE/2`
  (our refresh fails → BYE) or `SE - min(32, SE/3)` (we never see the peer's refresh →
  BYE), instead of pinning resources until the media watchdog or a manual hangup.
- **Two reversible knobs**, both floored at the RFC minimum; defaults (600 / 90) are a
  conservative voice-call liveness window. No new dependency, no new transport.
- **Hot-path cost** is one watchdog task per call (idle, sleeping to `SE/2`) plus, for the
  refresher, one in-dialog re-INVITE every `SE/2` seconds — negligible against the
  conversational pipeline.
- **Scope / not in scope.** `UPDATE` (RFC 3311) is not used for the refresh — RFC 4028 §6
  allows re-INVITE, and we already own robust re-INVITE machinery while no `UPDATE` support
  exists; adding `UPDATE` is a future option, not a requirement. Live PBX validation (a real
  gateway honouring our 422 / our refresh / our BYE-on-expiry) is the operator step.

## References

- RFC 4028 (Session Timers): §4/§5 (the 90 s floor), §6 (422 Session Interval Too Small +
  Min-SE), §7.2/§9 (refresh at SE/2; the UAS 2xx carries Session-Expires + Supported: timer,
  and `Require: timer` per §9 Table 2 — required when refresher=uac, allowed when
  refresher=uas *only if the request advertised Supported: timer*, otherwise omitted;
  refresher election), §10 (refresher re-INVITE; the refresher BYEs **only** on a
  timeout / 408 / 481 — other non-2xx follow that response code's rules and retry if
  possible; non-refresher BYE at `SE - min(32, SE/3)`).
- RFC 3261 §8.1.3.2 (ignore an unparseable header), §14 / §14.1 (re-INVITE; UAC retries a
  491 Request Pending after a random 2.1–4 s / 0–2 s interval), §15 (BYE).
- ADR-0011 (in-dialog re-INVITE/hold), ADR-0053 + PR #135 (SDES re-key continuity),
  ADR-0026 (RTP-inactivity watchdog + teardown reasons), ADR-0059 (admission/teardown),
  ADR-0070 (secure-media guard ordering).
- `src/hermes_voip/session_timer.py` (RFC math), `src/hermes_voip/adapter.py` (inbound 422
  + 2xx headers, outbound offer + 422 retry, watchdog), `src/hermes_voip/call.py`
  (`refresh_session`), `src/hermes_voip/incall.py` (`build_hold_reinvite` extra headers),
  `src/hermes_voip/originate.py` (`build_outbound_invite` extra headers),
  `src/hermes_voip/config.py` (`session_expires` / `min_se`),
  `tests/test_session_timer.py`, `tests/test_adapter_session_timers.py`.
