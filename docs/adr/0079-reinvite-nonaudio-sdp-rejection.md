# ADR-0079: Reject non-audio re-INVITE SDP offers instead of sending a fresh offer

- **Date:** 2026-06-26
- **Status:** Accepted
- **Deciders:** agent session (bk626 re-INVITE correctness lane). Extends ADR-0011
  (in-dialog re-INVITE control) and ADR-0044 (video SDP parsing).

## Context

RFC 3264 allows one outstanding offer/answer exchange per dialog transaction: when an INVITE
request carries an SDP offer, the 2xx response must answer that offer. `classify_inbound_reinvite`
previously collapsed an empty body and a non-empty SDP body whose parsed description had no usable
`m=audio` into the same `OfferlessReinvite` outcome. `CallSession._on_reinvite` answers an
`OfferlessReinvite` by placing a fresh local offer in the 2xx, which is correct only when the peer
sent no offer. For a video-only re-INVITE, that produces a second offer where RFC 3264 requires an
answer to the received offer.

The plugin's in-call media controller currently negotiates audio only. ADR-0044 adds video parsing
for WebRTC video answers, but the SIP call-control path in `CallSession` has no accepted audio-less
media mode to switch to mid-call.

## Decision

A non-empty re-INVITE SDP body that parses but has no usable `m=audio` is classified as
`UnsupportedReinviteOffer`, not as `OfferlessReinvite`. `CallSession` rejects that outcome with
`488 Not Acceptable Here` and no SDP body.

Truly empty re-INVITEs remain `OfferlessReinvite` and continue to receive a fresh local audio offer
in the 2xx. Normal audio re-INVITEs remain `MediaUpdate` and are answered with mirrored audio
direction.

## Consequences

- The call-control path preserves the RFC 3264 offer/answer state machine: a re-INVITE that already
  carried an SDP offer no longer receives a new local offer in the 2xx.
- A video-only or otherwise audio-less re-INVITE does not change hold state or media keys; the
  established call continues on its previous media until the peer sends an acceptable offer or ends
  the dialog.
- If this package later implements audio-less in-call modes, this decision must be revisited so the
  unsupported-offer branch can generate a valid answer for those modes.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Rejected/port-0 audio answer | RFC 3264 §6 port-zero rejection answers media streams that are present in the offer. A video-only offer has no audio stream to reject, and this SIP call path has no video answer semantics; `488` is clearer and leaves the established audio session untouched. |
| Keep treating audio-less SDP as offerless | It sends a new offer in the 2xx even though the INVITE already carried an offer, violating RFC 3264 offer/answer ordering. |
| Accept video-only as a media update | `CallSession` does not have an audio-less/video-only media mode, so accepting would be an aspirational state change rather than implemented behaviour. |
