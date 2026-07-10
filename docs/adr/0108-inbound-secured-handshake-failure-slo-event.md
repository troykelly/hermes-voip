# ADR-0108: Emit a structured SLO event on inbound post-200-OK secured-handshake failure

- **Date:** 2026-07-10
- **Status:** Accepted
- **Deciders:** agent session (HIGH-severity observability fix)
- **Relates to:** ADR-0075 (structured per-call lifecycle SLO events), ADR-0084
  (media connection detail is public-repo-sensitive, redact it), ADR-0086
  (outbound failure category = single source of truth), ADR-0032 / ADR-0053
  (WebRTC ICE/DTLS and SIP `UDP/TLS/RTP/SAVP` secured-media setup), ADR-0065
  (answer-time dialog guard + post-200 abort), runbook 0014 (VoIP SLO metrics)

## Context

An inbound WebRTC (ICE/DTLS) or SIP `UDP/TLS/RTP/SAVP` call is **answered** — the
`200 OK` carrying our `a=fingerprint` / ICE credentials is sent — **before** the
DTLS/ICE handshake runs, because the handshake needs the peer to hold our answer
(ADR-0032, ADR-0065). The `call_answered` SLO event (runbook 0014, ADR-0075) fires
inside `_send_answer_200`, i.e. BEFORE the handshake.

Both inbound secured-setup helpers caught their post-200 handshake failure with a
bare `_log.exception("INVITE %s: … handshake failed", call_id)` and **no** structured
`extra={}` — no `event`, no category, `call_id` only interpolated into the message
text. So a call that failed secured-media setup AFTER the `200 OK` was counted as
`call_answered` (success) with ZERO failure signal. runbook-0014's call-setup-success
SLO — `count(call_answered) / (count(call_answered) + count(call_rejected))` —
silently **overcounted**, and an operator could not distinguish a fingerprint mismatch
(misconfig) from an ICE-connectivity failure (network) from a handshake timeout (peer)
via a log query.

The OUTBOUND WebRTC/TLS leg's identical `run_handshake()` failure IS already captured
(`outbound_call_failed` with an `outbound_failure_category(exc)` label, ADR-0086) — so
the inbound gap was an asymmetry, not a new design question.

## Decision

Emit a structured `inbound_secured_handshake_failed` event in BOTH inbound
handshake-failure `except` blocks (`_setup_webrtc_call`, `_setup_sip_dtls_call`),
carrying `call_id`, a fixed `transport` (`webrtc` / `sip-dtls`), and a
`failure_category` derived from the media session's documented `run_handshake`
contract:

- `ValueError` → `"fingerprint"` — the peer certificate did not match the offered
  `a=fingerprint` (RFC 5763 §5): a misconfiguration.
- `ConnectionError` → `"ice"` — ICE connectivity checks failed (WebRTC only): a
  network failure.
- `RuntimeError` → `"dtls_timeout"` — the DTLS handshake did not complete within the
  round/recv bound: a peer timeout.
- any other exception → `"failed"` — an unexpected error on the handshake path.

The classification lives in a pure module-level helper
`_inbound_secured_failure_category(exc)` (mirroring `outbound_failure_category`), with
`_inbound_secured_failure_extra(call_id, exc, transport=…)` assembling the `extra={}`.
The branch order is unambiguous: `ConnectionError` is disjoint from `ValueError` /
`RuntimeError`, and is matched before falling through to the `failed` catch-all.

**Control flow is unchanged.** The call is still torn down exactly as before — media
released, the answered dialog ACK-aware BYE'd (ADR-0065), and `_MediaNegotiationRejected`
re-raised so the inbound handler builds no `CallLoop` on dead media. This is a pure
observability addition: only the structured signal is new. The existing `_log.exception`
call (ERROR level, with traceback) is retained; the `extra={}` fields ride alongside it.

runbook 0014's call-setup-success section documents the event and the corrected SLO:
`(count(call_answered) − count(inbound_secured_handshake_failed)) /
(count(call_answered) + count(call_rejected))`.

## Public-repo safety (rule 34 / ADR-0084)

The record carries ONLY `call_id` + the fixed `transport` token + the fixed
`failure_category` token. The exception itself is NEVER stringified into the structured
fields — a DTLS `RuntimeError` message can embed gateway connection detail (the reason
`media/sip_dtls_session.py` converts a raw `SSL.Error` to `RuntimeError` so the outbound
path can redact it). The category is a closed enumeration of four safe tokens, so a log
pipeline may filter and group on it freely.

## Alternatives considered

- **Reuse `outbound_failure_category` verbatim.** Rejected: its categories
  (`busy`/`no_answer`/`declined`/`failed`) are SIP-status-derived and meaningless for a
  post-200 secured-media handshake, which fails on cryptography/connectivity, not a SIP
  response. A dedicated inbound taxonomy (`fingerprint`/`ice`/`dtls_timeout`/`failed`)
  is what an operator needs to triage misconfig vs network vs peer.
- **Change control flow to a distinct SIP response.** Rejected and out of scope: the
  call was already answered `200 OK`; there is no valid pre-answer reject to send, and
  the ADR-0065 abort is correct. This ADR adds signal only.
- **Emit at INFO like the outbound event.** Rejected: the inbound path already logs the
  failure at ERROR with a traceback (a keying failure on an answered call is an error,
  not a routine outcome); we preserve that level and only attach `extra={}`.

## Consequences

- The call-setup-success SLO can be computed correctly (failures subtracted), and
  secured-media failures are triageable by mode from a single log query.
- The event is LOCAL-ONLY stdlib logging (no external sink), consistent with ADR-0075;
  wiring a metrics sink remains the named follow-up in runbook 0014.
- The four-token category taxonomy is now load-bearing: adding a new secured-media
  failure mode means extending `_inbound_secured_failure_category` and the runbook.
