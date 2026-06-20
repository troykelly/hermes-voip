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
import random
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "MIN_SE_FLOOR",
    "TEARDOWN_GUARD_CAP_SECS",
    "AcceptTimers",
    "RefreshContinue",
    "RefreshFailureAction",
    "RefreshOutcome",
    "RefreshRetry",
    "RefreshSucceeded",
    "RefreshTeardown",
    "Refresher",
    "Reject422",
    "SessionExpires",
    "UasTimerDecision",
    "build_session_expires_value",
    "classify_refresh_failure",
    "elect_refresher",
    "glare_backoff_secs",
    "negotiate_uas_timers",
    "parse_min_se",
    "refresh_interval_secs",
    "teardown_deadline_secs",
]

# RFC 3261 §17.1.1.2: the SIP "transaction does not exist" / request-timeout
# statuses. A refresh re-INVITE that draws one of these (or no final response at
# all) means the dialog itself is gone — the refresher tears it down (RFC 4028 §10).
_REQUEST_TIMEOUT = 408
_CALL_LEG_DOES_NOT_EXIST = 481

# RFC 3261 §14.1: a UAC that receives 491 Request Pending to its re-INVITE retries
# after a random interval - 2.1-4.0 s when it is the UAC of the original INVITE,
# 0-2.0 s when it is the UAS. The refresher re-sends the refresh, never BYEing.
_REQUEST_PENDING = 491
_UAC_GLARE_BACKOFF_LO = 2.1
_UAC_GLARE_BACKOFF_HI = 4.0
_UAS_GLARE_BACKOFF_LO = 0.0
_UAS_GLARE_BACKOFF_HI = 2.0

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


# --- refresh-failure classification (RFC 4028 §10 + RFC 3261 §14.1) ----------
#
# RFC 4028 §10 is explicit that the refresher tears the dialog down (BYE) ONLY on
# a timeout or a 408/481 — for any other non-2xx it "SHOULD follow the rules
# specific to that response code and retry if possible". So a failed refresh is
# NOT uniformly fatal: a 491 (glare) is retried after a randomized backoff, and a
# transient 5xx/6xx leaves the call up (the next SE/2 tick / the non-refresher's
# own deadline still protects liveness). These discriminated outcomes let the
# watchdog branch exhaustively instead of collapsing every failure to a BYE.


@dataclass(frozen=True, slots=True)
class RefreshSucceeded:
    """The refresh re-INVITE was accepted (2xx) — the session timer is reset."""


@dataclass(frozen=True, slots=True)
class RefreshTeardown:
    """The refresh proved the dialog is dead (timeout / 408 / 481) — BYE it."""


@dataclass(frozen=True, slots=True)
class RefreshRetry:
    """The refresh hit glare (491) — retry after a randomized backoff, do not BYE."""


@dataclass(frozen=True, slots=True)
class RefreshContinue:
    """A transient non-2xx (5xx/6xx/488…) — keep the call up, log and continue.

    Attributes:
        status_code: The non-2xx status, surfaced for the warning the watchdog logs.
    """

    status_code: int


type RefreshFailureAction = RefreshTeardown | RefreshRetry | RefreshContinue

# The full outcome of one refresh attempt: success, or one of the failure actions.
type RefreshOutcome = RefreshSucceeded | RefreshFailureAction


def classify_refresh_failure(status_code: int | None) -> RefreshFailureAction:
    """Decide what a failed session-refresh re-INVITE means (RFC 4028 §10).

    Args:
        status_code: The final non-2xx status of the refresh re-INVITE, or ``None``
            when the refresh timed out (no final response arrived).

    Returns:
        * :class:`RefreshTeardown` — timeout / 408 / 481: the dialog is dead, BYE it.
        * :class:`RefreshRetry` — 491 Request Pending (glare): retry after a backoff.
        * :class:`RefreshContinue` — any other non-2xx (5xx/6xx/488…): the call stays
          up; the next refresh tick / the peer's own deadline still guards liveness.
    """
    if status_code is None:
        return RefreshTeardown()
    if status_code in (_REQUEST_TIMEOUT, _CALL_LEG_DOES_NOT_EXIST):
        return RefreshTeardown()
    if status_code == _REQUEST_PENDING:
        return RefreshRetry()
    return RefreshContinue(status_code=status_code)


def glare_backoff_secs(
    role: Refresher,
    *,
    uniform: Callable[[float, float], float] = random.uniform,
) -> float:
    """The 491-glare retry backoff window for our dialog ``role`` (RFC 3261 §14.1).

    When we are the UAC of the original INVITE the retry waits a random interval in
    ``[2.1, 4.0]`` s; when we are the UAS it waits ``[0.0, 2.0]`` s. ``uniform`` is
    injected (default :func:`random.uniform`) so callers/tests can make the draw
    deterministic. This is application code, so the production ``random`` module is
    the right source of the jitter (no cryptographic strength is required).
    """
    if role is Refresher.UAC:
        return uniform(_UAC_GLARE_BACKOFF_LO, _UAC_GLARE_BACKOFF_HI)
    return uniform(_UAS_GLARE_BACKOFF_LO, _UAS_GLARE_BACKOFF_HI)
