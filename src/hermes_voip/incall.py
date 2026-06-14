"""Sans-IO in-call control: hold/resume re-INVITE and re-INVITE classification.

ADR-0011 §2. This layer turns a :class:`~hermes_voip.dialog.Dialog` plus the
call's local media into the wire text for a hold/resume re-INVITE, classifies the
response to one we sent, and classifies an inbound re-INVITE the peer sent. It
owns no socket, no timer, and no media plane — :class:`CallSession` (the IO
driver, a later unit) calls these and applies the result.

The load-bearing rule is **invariant 1**: a hold/resume re-INVITE advances **both**
the dialog ``local_cseq`` (via ``build_in_dialog_request``) **and** the SDP ``o=``
version (via ``Dialog.with_next_sdp_version``), while the SDP session-id stays
constant for the life of the dialog. The two are incremented by separate calls so
they can never silently couple.

Glare (RFC 3261 §14.2): if we have an unanswered re-INVITE outstanding and the
peer sends one, we answer ``491 Request Pending``; if the peer answers ours with
``491`` we back off and retry. Both surface here as explicit outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes_voip.dialog import Dialog, InDialogRequest, build_in_dialog_request
from hermes_voip.digest import DigestChallenge
from hermes_voip.message import SipRequest, SipResponse
from hermes_voip.sdp import Codec, SessionDescription, build_audio_offer

__all__ = [
    "Glare",
    "HoldConfirmed",
    "InboundReinvite",
    "IncallError",
    "LocalMediaSession",
    "MediaUpdate",
    "OfferlessReinvite",
    "ReinviteChallenged",
    "ReinviteProgress",
    "ReinviteRejected",
    "ReinviteResponse",
    "build_hold_reinvite",
    "classify_inbound_reinvite",
    "handle_reinvite_response",
]

_SDP_CONTENT_TYPE = ("Content-Type", "application/sdp")
_REQUEST_PENDING = 491
_UNAUTHORIZED = 401
_PROXY_AUTH_REQUIRED = 407

# A hold/resume target is one of these two directions (ADR-0011 §2).
_HOLD_DIRECTIONS = frozenset({"sendonly", "sendrecv"})

# RFC 3264 §6.1: the answer direction mirrors the offer direction.
_ANSWER_MIRROR: dict[str, str] = {
    "sendrecv": "sendrecv",
    "sendonly": "recvonly",
    "recvonly": "sendonly",
    "inactive": "inactive",
}

# Offer directions under which the peer is not receiving our media — i.e. it has
# put us on hold (classic hold sends MOH ``sendonly``; full hold is ``inactive``).
_HELD_OFFER_DIRECTIONS = frozenset({"sendonly", "inactive"})


class IncallError(ValueError):
    """An in-call control input is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class LocalMediaSession:
    """The local media parameters reused across a dialog's offers.

    Attributes:
        local_address: The IPv4 literal we receive RTP on.
        port: The local RTP port.
        codecs: The codecs we offer, in preference order.
        session_id: The SDP ``o=`` session id — constant for the dialog; the
            version comes from :attr:`Dialog.sdp_version`.
        ptime: The packetisation time in ms.
    """

    local_address: str
    port: int
    codecs: tuple[Codec, ...]
    session_id: int
    ptime: int = 20


def build_hold_reinvite(
    dialog: Dialog, media: LocalMediaSession, direction: str
) -> InDialogRequest:
    """Build a hold (``sendonly``) or resume (``sendrecv``) re-INVITE.

    Advances the SDP version first (new offer), then builds the in-dialog INVITE
    (advances CSeq) — invariant 1. The returned :class:`InDialogRequest` carries
    the wire text and the dialog with **both** counters incremented and the
    session-id unchanged.

    Raises:
        IncallError: if ``direction`` is not ``sendonly`` or ``sendrecv``.
    """
    if direction not in _HOLD_DIRECTIONS:
        msg = f"hold/resume direction must be sendonly or sendrecv, got {direction!r}"
        raise IncallError(msg)
    offered = dialog.with_next_sdp_version()
    body = build_audio_offer(
        local_address=media.local_address,
        port=media.port,
        codecs=media.codecs,
        direction=direction,
        ptime=media.ptime,
        session_id=media.session_id,
        version=offered.sdp_version,
    )
    return build_in_dialog_request(
        offered,
        "INVITE",
        extra_headers=(_SDP_CONTENT_TYPE,),
        body=body,
    )


# --- response to a re-INVITE we sent ----------------------------------------


@dataclass(frozen=True, slots=True)
class HoldConfirmed:
    """The peer accepted our re-INVITE (2xx); ``answer`` is its SDP, if any."""

    answer: SessionDescription | None


@dataclass(frozen=True, slots=True)
class ReinviteChallenged:
    """The re-INVITE needs digest auth (401/407); resend authenticated."""

    challenge: DigestChallenge
    proxy: bool


@dataclass(frozen=True, slots=True)
class ReinviteRejected:
    """The peer rejected our re-INVITE with a final non-2xx status."""

    status_code: int
    reason: str

    @property
    def is_glare(self) -> bool:
        """``True`` for ``491 Request Pending`` — retry after a short backoff."""
        return self.status_code == _REQUEST_PENDING


@dataclass(frozen=True, slots=True)
class ReinviteProgress:
    """A provisional (1xx) response; keep waiting for the final response."""


type ReinviteResponse = (
    HoldConfirmed | ReinviteChallenged | ReinviteRejected | ReinviteProgress
)


def handle_reinvite_response(response: SipResponse) -> ReinviteResponse:
    """Classify a response to a re-INVITE we sent.

    Returns :class:`ReinviteProgress` (1xx), :class:`HoldConfirmed` (2xx, with the
    parsed SDP answer when present), :class:`ReinviteChallenged` (401/407), or
    :class:`ReinviteRejected` (every other final status, ``is_glare`` for 491).
    """
    code = response.status_code
    if 100 <= code < 200:  # noqa: PLR2004 — 1xx provisional range
        return ReinviteProgress()
    if 200 <= code < 300:  # noqa: PLR2004 — 2xx success range
        answer = (
            SessionDescription.parse(response.body) if response.body.strip() else None
        )
        return HoldConfirmed(answer=answer)
    if code in (_UNAUTHORIZED, _PROXY_AUTH_REQUIRED):
        proxy = code == _PROXY_AUTH_REQUIRED
        header = "Proxy-Authenticate" if proxy else "WWW-Authenticate"
        challenge = DigestChallenge.parse(response.header(header) or "")
        return ReinviteChallenged(challenge=challenge, proxy=proxy)
    return ReinviteRejected(status_code=code, reason=response.reason)


# --- inbound re-INVITE the peer sent ----------------------------------------


@dataclass(frozen=True, slots=True)
class Glare:
    """An inbound re-INVITE arrived while ours is outstanding; answer 491."""


@dataclass(frozen=True, slots=True)
class MediaUpdate:
    """The peer offered a media change; answer with the mirrored direction.

    Attributes:
        offer: The peer's parsed SDP offer.
        offer_direction: The offer's media direction.
        answer_direction: The direction to answer with (RFC 3264 §6.1 mirror).
        held_by_peer: ``True`` when the peer has placed us on hold (it is not
            receiving our media: ``sendonly`` or ``inactive`` offer).
    """

    offer: SessionDescription
    offer_direction: str
    answer_direction: str
    held_by_peer: bool


@dataclass(frozen=True, slots=True)
class OfferlessReinvite:
    """A re-INVITE with no SDP offer; we must offer in the 2xx."""


type InboundReinvite = Glare | MediaUpdate | OfferlessReinvite


def classify_inbound_reinvite(
    request: SipRequest, *, pending_local_offer: bool
) -> InboundReinvite:
    """Classify an inbound re-INVITE for the consume side.

    Glare takes priority: if we already have an unanswered re-INVITE outstanding,
    the result is :class:`Glare` (answer 491) regardless of the offer. Otherwise
    an offer present yields a :class:`MediaUpdate` with the mirrored answer
    direction; an absent offer yields :class:`OfferlessReinvite`.

    Raises:
        IncallError: if the offer carries an unknown media direction.
    """
    if pending_local_offer:
        return Glare()
    if not request.body.strip():
        return OfferlessReinvite()
    offer = SessionDescription.parse(request.body)
    if offer.audio is None:
        return OfferlessReinvite()
    offer_direction = offer.audio.direction
    answer_direction = _ANSWER_MIRROR.get(offer_direction)
    if answer_direction is None:
        msg = f"unknown offer media direction: {offer_direction!r}"
        raise IncallError(msg)
    return MediaUpdate(
        offer=offer,
        offer_direction=offer_direction,
        answer_direction=answer_direction,
        held_by_peer=offer_direction in _HELD_OFFER_DIRECTIONS,
    )
