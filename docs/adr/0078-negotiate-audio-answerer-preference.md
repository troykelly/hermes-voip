# ADR-0078: `negotiate_audio` orders the answer by OUR preference (RFC 3264 §6.1)

- **Date:** 2026-06-26
- **Status:** Accepted
- **Deciders:** agent session (SDP negotiation lane). Supersedes (in part) the
  offer-order codec selection in ADR-0005 and ADR-0022's amendment; composes with
  ADR-0032 (WebRTC/Opus menu) and ADR-0049 (Opus on the SIP path).

## Context

`negotiate_audio` (`src/hermes_voip/sdp.py`) intersects the peer's offered audio
codecs with our advertised menu (`adapter._SUPPORTED_ENCODINGS` on the SIP/SDES path,
`adapter._WEBRTC_SUPPORTED_ENCODINGS` on the WebRTC path) and returns the codecs to put
in the SDP answer's `m=audio` payload list. It previously kept the **offer's** order:

```python
chosen = tuple(c for c in offer.codecs if c.encoding.upper() in wanted)
```

Both menus are written most-preferred-first (G.722 wideband ahead of G.711; Opus ahead
of G.711). But keeping offer order discards that preference. A gateway that offers a
narrowband codec **before** our preferred wideband one — `PCMU` before `Opus`, or
`PCMU` before `G.722` — was answered with the narrowband codec it happened to list
first. The RTP stream then ran at 8 kHz even though both ends could carry wideband:
a **silent quality downgrade** dictated by the peer's list order, not by capability.

RFC 3264 §6.1 is explicit that the answer expresses the **answerer's** preference among
the codecs it accepts, not the offerer's. The offerer proposes a set; the answerer picks
and orders. So reordering the answer by our menu is the standards-correct behaviour, and
the old offer-order behaviour was a defect, not a design choice we were entitled to.

(This is distinct from `a=crypto` suite selection, where ADR-0073 already selects the
**strongest** offered SRTP suite rather than honouring offer order — same principle:
the answerer, not the offerer, decides.)

## Decision

`negotiate_audio` has an explicit role switch:

- `prefer_local=True` (the default): we are the **answerer** building an SDP answer.
  The negotiated codecs are ordered by **our** preference — the `supported` sequence
  the caller passes, first = most preferred — via a **stable** sort on each codec's
  preference rank (its encoding's index in `supported`).
- `prefer_local=False`: we are the **offerer** parsing a received SDP answer. The peer
  is the answerer, so its `m=audio` order is the selection signal. We preserve that
  peer order while still filtering to the intersection of codecs in the answer and the
  menu we actually offered/supported.

```python
rank = {name.upper(): i for i, name in enumerate(supported)}
common = [c for c in offer.codecs if c.encoding.upper() in rank]
chosen = tuple(sorted(common, key=lambda c: rank[c.encoding.upper()]))  # prefer_local
chosen = tuple(common)  # received answer parse
```

Properties that fall out of a stable sort by rank when `prefer_local=True`:

- **Wideband/preferred wins regardless of offer order.** `PCMU`-before-`Opus` →
  `Opus` leads; `PCMU`-before-`G.722` → `G.722` leads.
- **Already-preference-ordered offers are unchanged.** When the offer already matches
  our order the sort is a no-op and returns the same `Codec` objects in the same order
  (byte-for-byte identical answer; existing offer-order tests for that case still pass).
- **Ties keep offer order.** Two offered codecs sharing one encoding (hence one rank)
  retain their relative offer order.
- **`telephone-event` stays where it sits in `supported`** (conventionally last), so
  DTMF negotiation is unaffected.

The keyword-only `prefer_local` parameter makes the SDP role explicit without changing
existing answer-building callers: the default remains local preference. Outbound 2xx
answer parsing passes `prefer_local=False` so `_first_voice_codec()` selects the peer
answerer's first accepted voice codec. The voice-codec floor (`has_voice` guard,
DTMF-only rejection) is unchanged.

## Consequences

- A wideband-capable peer is answered wideband even when it lists G.711/PCMU first —
  the fix the backlog item (bk158) called for.
- `build_audio_answer`'s answer `m=` payload order now follows `supported`, not the
  offer. The docstring and the two adapter menu comments that claimed offer-order
  behaviour are corrected in the same change (rule 27 — they were now contradictory).
- Outbound call answer validation preserves the received 2xx answer order. A peer that
  answers `PCMU` before `Opus` selected `PCMU`; we must not reorder that answer to our
  offer/support menu before choosing the RTP engine codec.
- One existing test (`test_answer_preserves_offer_codec_order_not_supported_order`)
  asserted the old, now-superseded contract; it is rewritten as
  `test_answer_orders_codecs_by_supported_not_offer_order` (its own commit, justified)
  to assert the corrected behaviour. No assertion was weakened.

### Superseded-in-part

- **ADR-0005 / ADR-0022 amendment** — the clause "RFC 3264 negotiation honours the
  peer's order" is superseded by this ADR: negotiation now honours **our** order
  (answerer preference). The wideband-preferred menu and G.711 fallback are unchanged.

## Alternatives considered

- **Keep offer order (status quo).** Rejected: lets the peer's list order silently
  pick a narrower codec than both ends support, contrary to RFC 3264 §6.1.
- **Re-rank by a hard-coded quality table instead of the `supported` order.** Rejected:
  the menus already encode our preference order; a second source of truth would drift.

## Verification

- TDD: red tests assert `PCMU`-before-`Opus` → `Opus`, `PCMU`-before-`G.722` →
  `G.722`, and `build_audio_answer` leads `PCMU` before `PCMA` when our menu prefers
  `PCMU`; companion tests pin the stable no-op, same-rank-keeps-offer-order,
  telephone-event-menu-position, and received-answer-preserves-peer-order invariants.
  The outbound E2E regression proves a 2xx answer listing `PCMU` before `Opus` leaves
  the RTP engine on `PCMU`. Implementation turns them green without touching the tests.
- Full local gate (ruff format/lint, mypy --strict, pytest) plus the hermes-contract
  extra gate (`adapter.py` is the contract surface).
