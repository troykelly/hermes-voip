# ADR-0070: Secure-media mandate on the inbound answer path — reject cleartext RTP/AVP with 488

- **Date:** 2026-06-20
- **Status:** Accepted
- **Deciders:** operator direction ("require SRTP … for our SIP over TLS", ADR-0053) —
  agent session (secure-media lane, #73). Composes with ADR-0053 (inbound SDES Stage 1 /
  DTLS Stage 2), ADR-0067 (outbound SDES offering) and the transport-restriction policy in
  `config.py`; closes the remaining inbound MEDIA-plane cleartext gap those left open.

## Context

The plugin only ever **registers and signals over TLS/WSS**: `load_gateway_config`
constrains the SIP transport to `_VIA_TRANSPORT = {tls, wss}` (`config.py`), so an inbound
INVITE always arrives on an encrypted signalling channel. The **media** plane, however,
has been negotiated **opportunistically** since ADR-0053:

- ADR-0053 made the inbound answer path negotiate SRTP — SDES (RFC 4568 `a=crypto`,
  Stage 1) and DTLS-SRTP (RFC 5763/5764, Stage 2) — but **opportunistically**: an
  encrypted offer is answered encrypted; a plain `RTP/AVP` offer is still answered **in the
  clear** (interop preferred over hard-fail).
- ADR-0067 added the symmetric **outbound** SDES offer (opt-in, fail-closed on a plaintext
  answer).

So today (verified against `adapter._handle_inbound_invite` on current `main`) a peer that
offers plain `RTP/AVP` audio is **accepted and answered 200 OK as a cleartext RTP call**
via the SDES/plain handler (`_setup_sdes_call` builds the engine with
`srtp_inbound=None`/`srtp_outbound=None` and answers `RTP/AVP`). A RED adapter test
(`test_adapter_secure_media.py::test_plain_rtp_avp_rejected_488_when_mandate_on`) proves
it: with the mandate the offer must be `488`, but the unmodified adapter returns `[200]`.

That is the last cleartext exposure on an otherwise-encrypted call: encrypted SIP carrying
a media offer whose RTP/RTCP would ride in the clear. An on-path attacker who cannot read
the (TLS) signalling can still capture/inject the (plaintext) audio. For a deployment whose
entire transport posture is "TLS-only", silently downgrading the media to cleartext is a
policy hole, not a feature.

## Decision

Add a media-security **mandate** on the inbound answer path, gated by a new config flag
`MediaConfig.require_secure_media` (env `HERMES_VOIP_REQUIRE_SECURE_MEDIA`), **default
`True`**.

When the mandate is on, `_handle_inbound_invite` applies a single guard **before** the
WebRTC / SIP-DTLS / SDES media-path selection:

```text
if media_cfg.require_secure_media and not audio.is_srtp:
    send 488 "Not Acceptable Here"; return
```

- `AudioMedia.is_srtp` is `"SAVP" in protocol` (`sdp.py`), so it is `True` for **every**
  secured profile — SDES `RTP/SAVP`, DTLS-SRTP `UDP/TLS/RTP/SAVP`, and WebRTC
  `UDP/TLS/RTP/SAVPF` — and `False` **only** for plain `RTP/AVP`. The guard therefore
  rejects exactly cleartext audio and nothing else.
- The reject is sent **before** any `Dialog`, `CallSession`, RTP engine, or admission slot
  is created (it sits right after `media_cfg` is resolved), so a rejected call leaves no
  registered dialog, no media session, and no leaked task — the same early-return shape as
  the existing unparseable-SDP / no-audio / no-common-codec `488` guards in the same
  handler. The reject reason is redaction-safe (no SDP body, no key, no caller content).
- `require_secure_media=False` is the **rollback switch**: it restores the prior
  opportunistic-plaintext behaviour for a gateway that can only offer cleartext media.

## Why this composes rather than duplicates

The prior work made secured media **work**; this makes cleartext media **refused** — two
orthogonal concerns:

- ADR-0053 / ADR-0067 negotiate and key SRTP (SDES + DTLS) on the inbound/outbound paths.
  This ADR does **not** touch keying; it adds a pre-selection gate and otherwise leaves the
  SDES/DTLS/WebRTC branches byte-for-byte unchanged. A secured offer still flows through
  exactly the same negotiation (`test_inbound_savp_offer_answered_with_sdes_srtp` runs with
  the mandate **on** to lock this).
- It is **not** the same as the existing per-branch `488`s: those reject a profile we
  cannot *carry* (bad codec, unkeyable SAVP). This rejects a profile we *can* carry but
  *policy* forbids (cleartext). The DTLS-disabled rollback test
  (`test_sip_dtls_disabled_falls_through_to_sdes_plain`) is unaffected: a
  `UDP/TLS/RTP/SAVP` offer is `is_srtp` and so passes this guard, then 488s in the
  SDES/plain handler as the unkeyable-SAVP case it always did.
- It mirrors ADR-0067's **outbound** fail-closed posture on the **inbound** side: outbound
  is explicit-offer + fail-closed (opt-in); inbound is now reject-cleartext (default-on),
  with the opportunistic-plaintext fallback preserved behind the flag.

## Default = on (and its blast radius)

The flag defaults `True` because the operator's transport posture is TLS-only and a
default-off security control protects no one. The cost is that any plain-`RTP/AVP`-offering
peer now fails to connect unless the operator opts out. The test suite's plain-RTP
behaviour tests (codec/dialog/ptime/RTCP mechanics, the e2e cleartext path) set
`HERMES_VOIP_REQUIRE_SECURE_MEDIA=false` explicitly — they exercise the cleartext answer
path, which still exists; the mandate's own accept/reject behaviour is asserted directly,
both flag states, in `tests/test_adapter_secure_media.py`. No assertion was weakened
(rule 19); the changed test setups are documented opt-outs.

## Consequences

- **Default-secure media.** With signalling already TLS/WSS, a connected inbound call now
  has both planes encrypted by default; a cleartext media offer is a loud `488`, not a
  silent downgrade.
- **One reversible knob.** `HERMES_VOIP_REQUIRE_SECURE_MEDIA=false` reverts to
  opportunistic plaintext for a cleartext-only gateway — no code change, documented in
  `.env.example` and runbook 0002.
- **No new dependency, no new transport, no hot-path cost** — one boolean check per INVITE
  before any media setup.
- **Scope / not in scope.** Inbound answer path only. Outbound offering keeps its own
  independent `HERMES_VOIP_SIP_SDES_OFFER` knob (ADR-0067). Mid-call re-INVITE downgrade
  protection is already handled by the SDES re-key continuity work (PR #135); this ADR is
  the initial-offer gate. SRTCP for secured RTCP is ADR-0066.

## References

- RFC 3711 (SRTP), RFC 4568 (SDES), RFC 5763/5764 (DTLS-SRTP), RFC 3261 §21.4.26 (488 Not
  Acceptable Here).
- ADR-0053 (inbound SDES/DTLS, opportunistic), ADR-0066 (SRTCP), ADR-0067 (outbound SDES).
- `src/hermes_voip/adapter.py` (`_handle_inbound_invite` guard), `src/hermes_voip/config.py`
  (`require_secure_media`), `src/hermes_voip/sdp.py` (`AudioMedia.is_srtp`),
  `tests/test_adapter_secure_media.py`.
