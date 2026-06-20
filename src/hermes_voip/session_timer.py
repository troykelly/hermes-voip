"""Sans-IO RFC 4028 session-timer logic (Session-Expires / Min-SE).

RFC 4028 keeps a SIP dialog alive with a periodic in-dialog **refresh**: a party
(the *refresher*) re-sends an INVITE/UPDATE carrying ``Session-Expires`` before
the session interval elapses, and the other party tears the dialog down with a
BYE if no refresh arrives in time. This module is the **pure** half — it owns no
socket, asyncio task, or dialog state. It parses the ``Session-Expires`` (delta +
optional ``;refresher=uac|uas``) and ``Min-SE`` header values, elects the
refresher, computes the refresher's refresh interval (SE/2) and the
non-refresher's teardown deadline (``SE - min(32, SE/3)``), decides whether an
inbound ``Session-Expires`` is too small (``< Min-SE`` → ``422 Session Interval
Too Small``), and renders the outbound header values. The :class:`VoipAdapter`
drives the IO around these results.

Spec anchors (RFC 4028):

* §4/§5 — the absolute minimum ``Session-Expires``/``Min-SE`` is **90 seconds**.
* §6 — a request whose ``Session-Expires`` is below the server's minimum is
  rejected ``422`` with a ``Min-SE`` header carrying that minimum.
* §7.2/§9 — the refresher SHOULD refresh once **half** the interval has elapsed.
* §9 — the UAS MAY reduce the offered interval but MUST NOT increase it, and MUST
  NOT go below ``Min-SE``; if the request carried no ``refresher`` parameter the
  UAS picks one; if it did, the UAS MUST NOT override it.
* §10 — the non-refresher sends BYE slightly before expiry: the recommended guard
  band is ``min(32, SE/3)`` seconds.

The result types are discriminated frozen dataclasses, not stringly-typed flags,
so the caller branches exhaustively (rule 17).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

__all__ = [
    "MIN_SE_FLOOR",
    "TEARDOWN_GUARD_CAP_SECS",
    "AcceptTimers",
    "Refresher",
    "Reject422",
    "SessionExpires",
    "UasTimerDecision",
    "build_session_expires_value",
    "elect_refresher",
    "negotiate_uas_timers",
    "parse_min_se",
    "refresh_interval_secs",
    "teardown_deadline_secs",
]

# RFC 4028 §4/§5: the absolute minimum Session-Expires / Min-SE is 90 seconds.
MIN_SE_FLOOR = 90

# RFC 4028 §10: the non-refresher's teardown guard band is capped at 32 seconds.
TEARDOWN_GUARD_CAP_SECS = 32

# A leading non-negative integer delta (the rest is parameters or whitespace).
_LEADING_DELTA = re.compile(r"\s*(\d+)")


class Refresher(enum.Enum):
    """Which side performs the session refresh (RFC 4028 ``refresher`` parameter)."""

    UAC = "uac"
    UAS = "uas"

    @classmethod
    def parse(cls, token: str) -> Refresher:
        """Parse a ``refresher`` token (case-insensitive); raise on an unknown value."""
        normalised = token.strip().lower()
        for member in cls:
            if member.value == normalised:
                return member
        msg = f"unknown refresher value: {token!r} (expected 'uac' or 'uas')"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SessionExpires:
    """A parsed ``Session-Expires`` header: the interval and the optional refresher.

    Attributes:
        delta: The session interval in seconds (a non-negative integer).
        refresher: The party that refreshes (``Refresher.UAC``/``UAS``), or ``None``
            when the header carried no ``refresher`` parameter.
    """

    delta: int
    refresher: Refresher | None

    @classmethod
    def parse(cls, value: str) -> SessionExpires:
        """Parse a ``Session-Expires`` (or compact ``x``) header value.

        The grammar is ``delta-seconds *(";" generic-param)``; only the
        ``refresher`` parameter is interpreted (any other parameter is ignored,
        not fatal — a future extension parameter must not break the parse).

        Raises:
            ValueError: if the value has no leading integer delta, or a present
                ``refresher`` parameter carries an unknown value.
        """
        match = _LEADING_DELTA.match(value)
        if match is None or not value.strip():
            msg = f"malformed Session-Expires (no integer delta): {value!r}"
            raise ValueError(msg)
        delta = int(match.group(1))
        refresher = _refresher_param(value)
        return cls(delta=delta, refresher=refresher)


def _refresher_param(value: str) -> Refresher | None:
    """Return the ``refresher`` parameter from a header value, or ``None``."""
    for part in value.split(";")[1:]:
        key, sep, raw = part.partition("=")
        if sep and key.strip().lower() == "refresher":
            return Refresher.parse(raw)
    return None


def parse_min_se(value: str) -> int:
    """Parse a ``Min-SE`` header value (delta-seconds, ignoring generic params).

    Raises:
        ValueError: if the value has no leading integer delta.
    """
    match = _LEADING_DELTA.match(value)
    if match is None or not value.strip():
        msg = f"malformed Min-SE (no integer delta): {value!r}"
        raise ValueError(msg)
    return int(match.group(1))


def elect_refresher(*, peer: Refresher | None, default: Refresher) -> Refresher:
    """Decide who refreshes (RFC 4028 §9).

    If the peer pinned a ``refresher`` we honour it (the UAS MUST NOT override the
    UAC's choice). Otherwise we pick our configured ``default``.
    """
    return peer if peer is not None else default


def refresh_interval_secs(delta: int) -> float:
    """The refresher's refresh interval: SE/2 (RFC 4028 §7.2/§9)."""
    return delta / 2


def teardown_deadline_secs(delta: int) -> float:
    """The non-refresher's BYE deadline: ``SE - min(32, SE/3)`` (RFC 4028 §10).

    The guard band before the full interval is ``min(32, SE/3)`` seconds, so the
    BYE is armed slightly before the session would otherwise expire.
    """
    guard_band = min(float(TEARDOWN_GUARD_CAP_SECS), delta / 3)
    return delta - guard_band


@dataclass(frozen=True, slots=True)
class Reject422:
    """The inbound ``Session-Expires`` is below our ``Min-SE`` — reject ``422``.

    Attributes:
        min_se: Our minimum session interval, to echo in the ``Min-SE`` header of
            the ``422 Session Interval Too Small`` response so the UAC can retry.
    """

    min_se: int


@dataclass(frozen=True, slots=True)
class AcceptTimers:
    """We accept session timers for this dialog with the negotiated parameters.

    Attributes:
        delta: The agreed session interval (seconds) to advertise in our 2xx.
        refresher: The elected refresher to advertise (``;refresher=…``).
    """

    delta: int
    refresher: Refresher


type UasTimerDecision = Reject422 | AcceptTimers


def negotiate_uas_timers(
    *,
    offered: SessionExpires | None,
    min_se: int,
    local_se: int,
    default_refresher: Refresher,
) -> UasTimerDecision:
    """Decide the UAS session-timer outcome for an inbound INVITE (RFC 4028 §6/§9).

    * ``offered`` below ``min_se`` → :class:`Reject422` (carry ``min_se``).
    * ``offered`` at or above ``min_se`` → :class:`AcceptTimers` with the offered
      interval (we honour the peer's value — the UAS MUST NOT *increase* it, and
      our policy does not reduce it) and the elected refresher (the peer's if it
      pinned one, else our ``default_refresher``).
    * no ``offered`` (the request carried no ``Session-Expires``) →
      :class:`AcceptTimers` with our ``local_se`` and ``default_refresher``
      (RFC 4028 §9 lets a timer-supporting UAS insert its own).

    Args:
        offered: The parsed inbound ``Session-Expires``, or ``None`` if absent.
        min_se: Our minimum session interval (must be ``>= 90``; the caller's config
            enforces the floor).
        local_se: Our configured session interval, used when the request offered none.
        default_refresher: The refresher we pick when the peer did not.

    Raises:
        ValueError: if ``local_se`` is below the RFC floor while it is needed (no
            offer) — a misconfiguration surfaced loudly, never a silent sub-floor SE.
    """
    if offered is None:
        if local_se < MIN_SE_FLOOR:
            msg = (
                f"local session interval {local_se} is below the RFC 4028 floor "
                f"of {MIN_SE_FLOOR}s; cannot insert a Session-Expires"
            )
            raise ValueError(msg)
        return AcceptTimers(delta=local_se, refresher=default_refresher)
    if offered.delta < min_se:
        return Reject422(min_se=min_se)
    refresher = elect_refresher(peer=offered.refresher, default=default_refresher)
    return AcceptTimers(delta=offered.delta, refresher=refresher)


def build_session_expires_value(delta: int, refresher: Refresher) -> str:
    """Render a ``Session-Expires`` header value: ``<delta>;refresher=<role>``."""
    return f"{delta};refresher={refresher.value}"
