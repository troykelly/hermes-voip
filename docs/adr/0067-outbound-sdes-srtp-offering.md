# ADR-0067: Outbound SDES-SRTP offering on `place_call` — opt-in offer, fail-closed on a plaintext answer

- **Date:** 2026-06-19
- **Status:** Accepted
- **Deciders:** operator direction ("require SRTP … for our SIP over TLS", ADR-0053) —
  agent session (outbound-SRTP lane). Closes the named follow-on in ADR-0053 §Scope
  ("outbound SRTP offering is a separate, named follow-on").

## Context

ADR-0053 made the **inbound** SIP-over-TLS answer path negotiate SRTP — SDES (RFC 4568
`a=crypto`, Stage 1) and DTLS-SRTP (RFC 5763/5764, Stage 2) — **opportunistically**: an
encrypted offer is answered encrypted, a plain `RTP/AVP` offer is answered in the clear
(interop over hard-fail). It explicitly **deferred** the symmetric outbound case.

Today the agent-originated outbound call (`VoipAdapter.place_call` →
`_handle_outbound_invite`) builds its INVITE with **plain `RTP/AVP`**: the engine is
constructed with `srtp_inbound=None`/`srtp_outbound=None` and `build_audio_offer` is
called **without** `crypto=`, and the 2xx-answer path never reads `a=crypto`. So
**outbound calls are unencrypted while inbound calls are encrypted** — an asymmetry
against the operator's "require SRTP" direction. (Verified: `_handle_outbound_invite`
adapter.py offer build + 2xx parse carry no crypto token; the only SRTP references are
the two `None` ctor args.)

Every building block already exists and is used inbound:

- `sdp.build_audio_offer(crypto=…)` emits `RTP/SAVP` + a single `a=crypto` line when
  given a `CryptoAttribute` (it otherwise emits plain `RTP/AVP`).
- `originate.build_srtp_crypto_attrs()` already mints **fresh** random SDES keys
  (`secrets.token_bytes`, suite-correct 30-octet key‖salt, `key_params` `repr=False`).
- `media.srtp.SrtpSession(crypto)` runs the RFC 3711 transform (reused inbound for both
  TX and RX); the inbound `_setup_sdes_call` is the keying reference.

This is **SDES only**. Outbound DTLS-SRTP offering (running the DTLS handshake as the
*offerer*/active client over the bare UDP socket) is a larger, separate change and is
**out of scope** here (named follow-on, below). The WebRTC outbound path
(`_handle_outbound_webrtc_invite`) already offers DTLS-SRTP and is untouched.

The asymmetry that makes the offerer's policy **different** from the answerer's: the
answerer reacts to what the peer asked for (a plain offer is a genuine plain request, so
a plain answer is not a downgrade). The **offerer chooses** the security floor. If we
offer `RTP/SAVP` (we asked for encryption) and the peer answers plain `RTP/AVP`, that is
a **downgrade of an encrypted offer** — accepting it would silently stream plaintext for
a call we asked to protect (a bid-down attack surface). RFC 4568 §5.1.2 likewise treats
SRTP keying as mandatory-to-honour for an `RTP/SAVP` m-line.

## Decision

### 1. Opt-in outbound SDES offer (config flag, default OFF)

A new `MediaConfig.sip_sdes_offer` (env `HERMES_VOIP_SIP_SDES_OFFER`, default
**`false`**) controls whether an outbound SIP-over-TLS INVITE offers SDES-SRTP:

- **`false` (default):** the outbound INVITE offers plain `RTP/AVP` — **today's
  behaviour, unchanged**. This preserves the live-validated default (the merged Stage 1
  live call ran against a Grandstream that offered plain RTP; ADR-0053) and avoids
  regressing any deployment whose gateway/extension is not SRTP-capable on the
  terminating leg.
- **`true`:** the outbound INVITE offers `RTP/SAVP` with one `a=crypto`
  (`AES_CM_128_HMAC_SHA1_80`, the preferred supported suite), keyed by a **fresh
  per-call** random master key. The operator's "prefer SRTP" direction is realised by
  opting in.

Default-off (not on) because the offerer's fail-closed posture (§3) turns a
non-SRTP-capable terminating leg into a **failed call**; flipping the default would break
existing outbound deployments silently. Opt-in is the reversible, safe default (rule 4);
the operator turns it on once the terminating side is known SRTP-capable.

We offer a **single** suite (`AES_CM_128_HMAC_SHA1_80`), not the two
`build_srtp_crypto_attrs` mints. `build_audio_offer`/`_build_audio_body` render exactly
one `a=crypto`; offering multiple suites would require multi-`a=crypto` rendering **and**
per-tag answer-key matching — more surface than this change needs. One strong suite is
RFC-4568-valid and matches the inbound answer's single-suite behaviour. (The 32-bit
fallback is reachable inbound when a peer *offers* it; we simply do not *offer* it.)

### 2. Keying — RFC 4568 §6.1 (sender-keyed), mirrored for the offerer

Each direction is keyed by its **sender**. As the **offerer**:

- **Outbound (TX/encrypt):** keyed by **OUR offer** `a=crypto` — the key we advertised in
  the INVITE. (Inbound this role is keyed by our *answer* crypto; the offer/answer
  mirror.)
- **Inbound (RX/decrypt):** keyed by the **peer's answer** `a=crypto` — parsed from the
  2xx SDP.

So `srtp_outbound = SrtpSession(our_offer_crypto)` and
`srtp_inbound = SrtpSession(peer_answer_crypto)`. This is the exact mirror of the inbound
helpers `_srtp_outbound_from_answer` (our key) / `_srtp_inbound_from_offer` (peer key).

### 3. Answer validation + fail-closed teardown — never silently downgrade or mis-key

When we offered `RTP/SAVP`, the 2xx answer is validated against **our offer**
(`_validate_outbound_answer_crypto`, RFC 4568 §5.1.2 / §6.1). The answer is **accepted**
only when it is `RTP/SAVP` carrying **exactly one** supported, well-formed `a=crypto`
whose **tag AND suite echo our offered crypto** — then the engine comes up **secured**
with the keying in §2.

Every other answer is **rejected, fail-closed**:

- plain `RTP/AVP` (a downgrade of our encrypted offer);
- a **secure but non-`RTP/SAVP`** profile — `UDP/TLS/RTP/SAVP` (DTLS-SRTP) or `RTP/SAVPF`
  (WebRTC) — **even with one otherwise-matching `a=crypto`**. The profile must be
  **exactly `RTP/SAVP`** (the `is_srtp`/`"SAVP" in protocol` test is too broad — keying a
  DTLS/AVPF media line with a bare SDES `a=crypto` is spec-invalid, a dead call; codex r3);
- **multiple** `a=crypto` lines **as sent on the wire** (an answer must select exactly
  one — RFC 4568 §5.1.2). The count is the **raw** `a=crypto` line count, not the
  parse-filtered supported subset: a matching line plus a *malformed* extra has a raw
  count of two (ambiguous) but filters to one, so counting only the filtered subset would
  wrongly accept it (codex r2);
- a single `a=crypto` that is **malformed or an unsupported suite** (the one raw line does
  not parse — equivalent to no usable key);
- a crypto whose **tag or suite we did not offer** (keying from it would use parameters
  we never proposed — a mis-key).

**Teardown of a rejected 2xx (RFC 3261 §13.2.2.4 + §15 — the codex-r1/r2/r3 BLOCKING
fix).** A 2xx **establishes the dialog** and **MUST be ACKed by the UAC** (the transaction
layer auto-ACKs only *non*-2xx; an un-ACKed 2xx leaves a half-open, remote-established
dialog + retransmits — the same bug fixed inbound in ADR-0065). So the dialog is built and
the 2xx **ACKed FIRST** (which sets an `ack_sent` flag), and **then** every "we cannot
accept this answer" check runs. There is **one** BYE point: the handler's `finally` sends
an in-dialog BYE (`_bye_answered_outbound_dialog`) whenever `ack_sent and not
session_established`, before it stops the engine and frees the RTP socket. So **ANY**
failure after the ACK and before the call is wired — a codec/crypto
`OutboundCallFailed(488, …)` **or an unexpected exception** in the acceptance or
session-wiring steps (codex r3) — tears the established dialog down with **exactly one**
BYE (no double-BYE), and the original error still propagates (rule 37). No media is ever
started, and no half-open dialog is left on the callee.

**This single teardown covers the SDES-crypto rejections above, the codec-acceptance
rejections** (no common codec / no voice codec / negotiated codec not carriable by the
engine / codec dependency unavailable — pre-existing 488 paths that formerly raised
*before* the ACK with the same half-open shape, codex r2), **and any unexpected post-ACK
exception** (codex r3). The `OutboundCallFailed` message is **structural** (tags / suites /
counts / profile token only) — it must never carry the offending key or `a=crypto` line
(rule 34: it lands in logs, and the repo is PUBLIC).

This is **not** a contradiction of ADR-0053's opportunism: opportunism is the
**answerer's** stance (don't downgrade the *peer's* request). The **offerer** that asked
for encryption must not accept a plaintext-or-mis-keyed answer — the two stances are the
two sides of "never downgrade or mis-key an encrypted media leg".

When the flag is **off** we offer plain and the 2xx answer is plain — no validation, no
new failure mode, unchanged.

### 4. Engine wiring

`_handle_outbound_invite` mints the offer crypto once (when the flag is on), threads it
into both `build_audio_offer(crypto=…)` (the INVITE body and the re-auth re-send share
the one body) and the engine: the engine is still constructed before the answer is known
(the existing PCMU-placeholder pattern), so `srtp_outbound` is set from our offer crypto
at construction and `srtp_inbound` is assigned from the parsed answer crypto after the
2xx, alongside the existing post-answer engine updates (codec, PT, ptime). RTCP stays
**dormant** on a secured outbound call (no SRTCP transform — same rule as inbound,
ADR-0061/ADR-0053).

## Consequences

- **Outbound calls can be encrypted**, closing the inbound/outbound asymmetry, with the
  offerer's correct fail-closed posture. The default-off flag keeps every current
  deployment byte-for-byte unchanged until the operator opts in.
- A fresh random key per call; the key never reaches a log or `repr` (rule 34 — the
  `CryptoAttribute.key_params` is already `repr=False`; tests assert no key leaks).
- **Gateway-agnostic** (CLAUDE.md): standard RFC 4568 SDES, no vendor quirk.
- **Out of scope (named follow-ons):** (a) outbound **DTLS-SRTP** offering (offerer/active
  DTLS over the bare UDP socket); (b) offering **multiple** crypto suites in one INVITE;
  (c) an in-dialog outbound re-offer that *adds* SRTP to an already-plain call. The
  inbound answer path, the SDES re-INVITE continuity fix (PR #135), `media/engine.py`
  internals, and `media/srtp.py` crypto are untouched.

## Alternatives considered

- **Default the flag ON (always offer SRTP outbound).** Rejected: with the fail-closed
  policy this turns any non-SRTP terminating leg into a failed call, a silent regression
  for existing outbound deployments. Opt-in is the reversible default.
- **Opportunistic outbound (offer SAVP, accept a plain answer).** Rejected: accepting a
  plain answer to an `RTP/SAVP` offer silently streams plaintext for a call we asked to
  protect — the exact bid-down the task forbids. The answerer is opportunistic; the
  offerer is not.
- **Offer both 80-bit and 32-bit suites.** Rejected for this change: needs multi-`a=crypto`
  rendering + per-tag answer matching. One strong suite is RFC-valid and minimal.
- **Outbound DTLS-SRTP instead of SDES.** Deferred: a larger change (offerer-side DTLS
  handshake state machine over the UDP socket); SDES is the in-place, tested middle tier
  and matches the inbound SDES path this mirrors.
