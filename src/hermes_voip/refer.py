"""Sans-IO call transfer: REFER / Replaces / Referred-By / NOTIFY (ADR-0011 §2).

Transfer is REFER (RFC 3515). A **blind** transfer is a REFER carrying
``Refer-To: <target>``; an **attended** transfer is a REFER whose ``Refer-To``
embeds a ``Replaces`` header (RFC 3891) naming the consultation dialog, so the
transferee's triggered INVITE replaces it at the target. Progress flows back over
the implicit subscription as a ``NOTIFY`` with a ``message/sipfrag`` status-line.

The agent both makes and receives calls, so this module covers **both** RFC 5589
roles, all sans-IO (produce/consume wire text, no socket):

* Transferor — :func:`build_blind_refer`, :func:`build_attended_refer`,
  :func:`parse_notify_sipfrag`.
* Transferee — :func:`parse_refer`, :func:`build_triggered_invite`,
  :func:`build_notify_sipfrag`.
* Target — :func:`match_replaces` (RFC 3891 §3 tag orientation).

RFC 3891 §3 orientation (the load-bearing detail): in a ``Replaces`` naming a
dialog, ``to-tag`` is the **local** tag at the party that receives the
Replaces-INVITE and ``from-tag`` is that party's **remote** tag. So an attended
REFER built from our consultation dialog (we are the transferor) sets
``to-tag = consult.remote_tag`` (the target's tag) and
``from-tag = consult.local_tag`` (ours); and when we are the target,
:func:`match_replaces` matches ``dialog.local_tag == to-tag`` and
``dialog.remote_tag == from-tag``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from urllib.parse import quote, unquote

from hermes_voip._chars import contains_control
from hermes_voip._name_addr import find_name_addr
from hermes_voip.dialog import Dialog, InDialogRequest, build_in_dialog_request
from hermes_voip.message import (
    SipRequest,
    build_request,
    new_branch,
    new_call_id,
    new_tag,
)

__all__ = [
    "NotifyProgress",
    "ReferError",
    "ReferRequest",
    "ReplacesSpec",
    "TransferOutcomeClass",
    "TransferOutcomeReport",
    "TransferUnknownReason",
    "build_attended_refer",
    "build_blind_refer",
    "build_notify_sipfrag",
    "build_triggered_invite",
    "classify_transfer_progress",
    "match_replaces",
    "parse_notify_sipfrag",
    "parse_refer",
]

_DEFAULT_USER_AGENT = "hermes-voip/0"
_MAX_FORWARDS = "70"
_SDP_CONTENT_TYPE = ("Content-Type", "application/sdp")
_SIPFRAG_CONTENT_TYPE = ("Content-Type", "message/sipfrag;version=2.0")
_DEFAULT_SUBSCRIPTION_STATE = "active;expires=60"

# ASCII [0-9]{3} (NOT \d{3}) so a non-ASCII 3-digit status code is rejected, not folded
# to an int() equivalent — matching message.py's strict posture (item 1670).
_SIPFRAG_STATUS = re.compile(r"SIP/2\.0\s+([0-9]{3})\s*(.*)")

# Refer-To injection guard (security; the transfer analogue of the outbound
# Request-URI guard ``adapter._validate_dialable_target``).
#
# ``build_blind_refer`` interpolates the AGENT-SUPPLIED transfer target into a
# ``Refer-To: <target>`` header. Unlike the digits-only dial target, a blind
# transfer target may LEGITIMATELY be EITHER (a) a bare dialable extension —
# optional leading ``+``, decimal digits, ``*`` / ``#`` for feature codes — OR
# (b) a full ``sip:``/``sips:`` URI for an attended/cross-domain transfer. Any
# other shape is an injection vector: a bare ``1001@evil.com`` hijacks the host;
# a ``?Header=`` smuggles an embedded SIP header (``?Replaces=`` would redirect
# the triggered INVITE to seize another dialog); a rogue ``;``-param smuggles
# routing (``;Route=``); an unescaped ``>`` or CR/LF (literal or percent-encoded)
# breaks out of the bracketed addr-spec to forge headers. The guard is a STRICT
# ALLOWLIST applied BEFORE the header is built, so an injection-bearing target
# raises ``ValueError`` and NO REFER is produced.
_DIALABLE_TARGET = re.compile(r"\+?[0-9*#]+")
# A well-formed sip:/sips: URI, constrained to a clean ``scheme:user@authority``
# with optional ``;``-params — and deliberately NO ``?``-header form (a
# ``Refer-To`` transfer target has no legitimate embedded-header need, and
# ``?Header=`` is a header-injection vector; rejected outright below). The
# authority is a well-formed ``host[:port]``: an RFC-1123 hostname, an IPv4
# literal, or a bracketed IPv6 literal, with an optional numeric port — so a
# ``/path``, a quote/comma, or a non-numeric port cannot pass. ``;``-param NAMES
# are further restricted to ``_ALLOWED_URI_PARAMS`` in code (so a token-shaped
# but dangerous ``;Replaces=`` / ``;Route=`` is rejected); this regex only fixes
# the structural shape. Angle brackets, whitespace, and control characters are
# rejected (literal and percent-decoded) before this runs, so it need not.
_HOST_LABEL = r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
_HOSTNAME = rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*\.?"
# `[0-9]` not `\d`: `\d` is Unicode-aware and would fold fullwidth / Arabic-Indic
# digits into a "valid" IPv4 octet or port, letting a non-ASCII target smuggle past
# this security-sensitive guard onto the UTF-8-encoded Refer-To wire (RFC 3261
# requires US-ASCII DIGIT). Matches the strict-ASCII posture of message.py and the
# _SIPFRAG_STATUS `\d{3}` -> `[0-9]{3}` fix (#479).
_IPV4 = r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}"
_IPV6_REF = r"\[[0-9A-Fa-f:.]+\]"
_AUTHORITY = rf"(?:{_IPV4}|{_IPV6_REF}|{_HOSTNAME})(?::[0-9]{{1,5}})?"
# user-part: any URI char except the structural separators / brackets / ws / a
# password-introducing ``:`` — kept restrictive so ``;``/``?``/``@`` cannot hide.
_URI_USER = r"[^\s@<>;?:/\"',]+"
# A ``;``-param token's character class (RFC 3261 token chars); param names are
# allowlisted in code.
_URI_PARAM_CHARS = r"[A-Za-z0-9\-.!%*_+`'~]+"
_SIP_URI = re.compile(
    rf"sips?:{_URI_USER}@{_AUTHORITY}(?:;{_URI_PARAM_CHARS}(?:={_URI_PARAM_CHARS})?)*",
    re.IGNORECASE,
)
# Allowlisted sip-URI ``;``-parameter names (RFC 3261 §19.1.1). These carry no
# destination-overriding capability, so a transfer cannot be re-aimed via a smuggled
# param: ``transport`` only selects the wire protocol, ``user``/``method``/``ttl``
# are benign, and ``lr`` is a loose-route flag. ``maddr`` is DELIBERATELY excluded
# (ADR-0112): per RFC 3261 §19.1.1 it OVERRIDES the destination a compliant proxy
# routes to while the URI host stays innocuous — a covert host-hijack a Refer-To
# transfer target has no legitimate need for. ANY other/unknown name (notably
# ``replaces``, ``route``, ``refer-to``, and ``maddr``) is rejected.
_ALLOWED_URI_PARAMS = frozenset({"transport", "user", "method", "ttl", "lr"})
# A bound on the target length: ample for an international E.164 number with a
# few DTMF digits, or a sip URI with a host and a couple of params, but short
# enough to refuse an absurd value before it reaches the wire.
_MAX_TRANSFER_TARGET_LEN = 256


def _validate_transfer_target(target: str) -> None:
    """Reject a blind-transfer target that is not a clean extension or sip URI.

    Validates the agent-supplied ``Refer-To`` target against a strict allowlist
    BEFORE it is interpolated into the ``Refer-To: <target>`` header. A target is
    accepted only if it is EITHER a bare dialable user-part (optional leading
    ``+``, digits, ``*`` / ``#``) OR a well-formed ``sip:``/``sips:`` URI of the
    shape ``scheme:user@host[:port]`` where:

    * the **authority** is a well-formed ``host[:port]`` — an RFC-1123 hostname,
      an IPv4 literal, or a bracketed IPv6 literal, with an optional numeric port
      — so a ``/path``, a quote/comma, or a non-numeric port is rejected;
    * the URI carries **no** ``?``-header form at all — a ``?`` anywhere in the
      target is rejected, because a ``Refer-To`` transfer target has no legitimate
      embedded-SIP-header need and ``?Header=`` (e.g. ``?Replaces=``) is a
      header-injection / dialog-seizing vector;
    * any ``;``-**parameters** are restricted to the safe allowlist
      ``_ALLOWED_URI_PARAMS`` (``transport``, ``user``, ``method``, ``ttl``,
      ``lr`` — NOT ``maddr``, which overrides the routed destination, ADR-0112)
      with token-only names/values; any other/unknown param name (notably
      ``replaces``, ``route``, ``refer-to``, ``maddr``) is rejected, so a transfer
      cannot be re-aimed via a smuggled parameter.

    Anything else — a bare-extension host hijack (``1001@evil.com``), a
    ``?Replaces=`` header or a ``;Route=`` param smuggle, an angle-bracket ``>``
    breakout, a CR/LF or other control character, whitespace, a non-ASCII character
    (e.g. a fullwidth / Arabic-Indic "digit" that a Unicode-aware regex class would
    fold), or an angle bracket in EITHER the literal value OR its percent-decoded
    form (so a percent-escaped ``%0D%0A`` / ``%20`` / ``%3C`` / ``%EF%BC%90`` that a
    gateway would unescape is caught), an over-long value, or empty — raises
    :class:`ValueError`, so the injection never reaches the header and no REFER is
    built.

    Args:
        target: The agent-supplied transfer target (extension or sip URI).

    Raises:
        ValueError: If ``target`` is empty, over-long, carries a control char,
            whitespace, or an angle bracket (literal or percent-encoded), embeds a
            ``?``-header form, carries a non-allowlisted ``;``-param, or is neither
            a dialable extension nor a well-formed sip:/sips: URI.
    """
    if not target:
        msg = "transfer target is empty"
        raise ValueError(msg)
    if len(target) > _MAX_TRANSFER_TARGET_LEN:
        msg = f"transfer target too long (>{_MAX_TRANSFER_TARGET_LEN} chars)"
        raise ValueError(msg)
    # Reject control chars, whitespace, and ``<`` / ``>`` in BOTH the literal
    # value and the percent-decoded value: a ``%0D%0A`` / ``%20`` / ``%3C``
    # survives the literal check, but a gateway unescaping the URI would then
    # inject CR/LF, split on the decoded space, or break out of the
    # ``Refer-To: <...>`` addr-spec on the decoded ``<``/``>``. ``unquote`` never
    # raises and leaves non-escapes intact, so this catches both forms. Do NOT
    # echo the raw value (it may carry injection bytes); name the violated rule.
    for candidate in (target, unquote(target)):
        if contains_control(candidate):
            msg = "transfer target contains a control character"
            raise ValueError(msg)
        if any(char.isspace() for char in candidate):
            msg = "transfer target contains whitespace"
            raise ValueError(msg)
        if "<" in candidate or ">" in candidate:
            msg = "transfer target contains an angle bracket"
            raise ValueError(msg)
        # RFC 3261 SIP URIs and dialable numbers are US-ASCII. Reject ANY non-ASCII
        # character (in the literal OR the percent-decoded form) so a fullwidth /
        # Arabic-Indic "digit" cannot fold past this injection allowlist onto the
        # UTF-8-encoded Refer-To wire: a Unicode-aware regex class (the negated
        # ``_URI_USER`` user-part, or a stray ``\d``/``\w``) would otherwise accept
        # it. The authority regexes (``_IPV4``/``_AUTHORITY``) also use explicit
        # ``[0-9]`` — this is the end-to-end enforcement, those are defence in depth.
        if not candidate.isascii():
            msg = "transfer target contains a non-ASCII character"
            raise ValueError(msg)
    if _DIALABLE_TARGET.fullmatch(target) is not None:
        return
    # A ``?``-header form is rejected outright (header-injection vector); name it
    # explicitly so the failure is not conflated with a malformed authority.
    if "?" in target:
        msg = "transfer target embeds a '?' SIP-header form (rejected)"
        raise ValueError(msg)
    if _SIP_URI.fullmatch(target) is not None and _uri_params_allowed(target):
        return
    msg = (
        "transfer target is neither a dialable extension "
        "(optional leading '+', digits, '*' and '#') nor a well-formed "
        "sip:/sips: URI (scheme:user@host[:port] with only allowlisted "
        ";params)"
    )
    raise ValueError(msg)


def _uri_params_allowed(uri: str) -> bool:
    """Return ``True`` iff every ``;``-param name in ``uri`` is allowlisted.

    The URI has already matched :data:`_SIP_URI` (so it is
    ``scheme:user@authority`` followed by zero or more ``;``-params and carries no
    ``?``). The user-part forbids ``@``/``;``, so the first ``@`` splits off the
    authority-and-params; the authority is the segment before the first ``;`` and
    each later ``;``-segment is a ``name`` or ``name=value`` param whose name must
    be in :data:`_ALLOWED_URI_PARAMS` (compared case-insensitively).
    """
    after_at = uri.split("@", 1)[1]
    for param in after_at.split(";")[1:]:
        name = param.split("=", 1)[0].lower()
        if name not in _ALLOWED_URI_PARAMS:
            return False
    return True


class ReferError(ValueError):
    """A REFER / Replaces / NOTIFY message is malformed or incomplete."""


@dataclass(frozen=True, slots=True)
class ReplacesSpec:
    """The identifiers of the dialog a ``Replaces`` header names (RFC 3891).

    Attributes:
        call_id: The ``Call-ID`` of the dialog to replace.
        to_tag: The ``to-tag`` — the local tag at the Replaces recipient.
        from_tag: The ``from-tag`` — the remote tag at the Replaces recipient.
        early_only: The ``early-only`` flag (replace only an early dialog).
    """

    call_id: str
    to_tag: str
    from_tag: str
    early_only: bool = False

    def header_value(self) -> str:
        """Render the ``Replaces`` header value (unescaped, for a real header)."""
        base = f"{self.call_id};to-tag={self.to_tag};from-tag={self.from_tag}"
        return f"{base};early-only" if self.early_only else base


@dataclass(frozen=True, slots=True)
class ReferRequest:
    """A parsed inbound REFER.

    Attributes:
        refer_to: The transfer target URI (without any embedded ``Replaces``).
        replaces: The embedded ``Replaces`` (attended transfer), else ``None``.
        referred_by: The ``Referred-By`` URI, if present.
    """

    refer_to: str
    replaces: ReplacesSpec | None
    referred_by: str | None


@dataclass(frozen=True, slots=True)
class NotifyProgress:
    """A parsed transfer-progress NOTIFY (``message/sipfrag`` status-line).

    Attributes:
        status_code: The sipfrag status-line code (e.g. ``100``, ``200``).
        reason: The sipfrag reason phrase.
        terminated: ``True`` when ``Subscription-State`` is ``terminated`` — the
            transfer reached a final outcome and the subscription is over.
    """

    status_code: int
    reason: str
    terminated: bool


class TransferOutcomeClass(Enum):
    """The terminal classification of a transfer's progress NOTIFY (ADR-0109).

    A blind/attended transfer's referee answers ``202`` (received), then reports the
    real outcome over the RFC 3515 implicit subscription as a ``message/sipfrag``
    NOTIFY. This is the pure verdict :func:`classify_transfer_progress` derives from
    the terminal :class:`NotifyProgress` (or its absence):

    * ``COMPLETED`` — a terminated NOTIFY with a ``2xx`` sipfrag status.
    * ``FAILED`` — a terminated NOTIFY with a ``3xx``/``4xx``/``5xx``/``6xx`` status
      (busy / declined / unreachable): the transfer definitively did not complete.
    * ``OUTCOME_UNKNOWN`` — no terminal NOTIFY was observed (``None``: the wait timed
      out, the leg was BYE'd first, or the peer declined the subscription), or only a
      non-terminal progress update (a ``100 Trying`` that leaked through). We never
      infer success or failure from an unterminated/absent NOTIFY.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class TransferUnknownReason(Enum):
    """Why a transfer's outcome is OUTCOME_UNKNOWN (ADR-0109 §4, P2).

    :func:`classify_transfer_progress` collapses every non-terminal transfer to a
    single ``OUTCOME_UNKNOWN`` verdict, but the FOUR ways a transfer reaches it are
    materially different to the agent — and only one of them is a wait that actually
    elapsed. This enum discriminates them so the tool message states the real reason
    instead of always claiming ``outcome not confirmed within Ns``:

    * ``TIMEOUT`` — the bounded outcome wait elapsed with no terminal NOTIFY.
    * ``SUBSCRIPTION_DECLINED`` — the referee declined the RFC 3515 implicit
      subscription (RFC 4488 ``Refer-Sub: false``); no NOTIFY will ever arrive.
    * ``CALL_ENDED`` — the referrer leg was BYE'd before any terminal NOTIFY (we
      never infer success from a torn-down leg).
    * ``WAIT_DISABLED`` — outcome confirmation is opted out (``timeout <= 0``): the
      REFER was sent but no wait was attempted.
    """

    TIMEOUT = "timeout"
    SUBSCRIPTION_DECLINED = "subscription_declined"
    CALL_ENDED = "call_ended"
    WAIT_DISABLED = "wait_disabled"


@dataclass(frozen=True, slots=True)
class TransferOutcomeReport:
    """A transfer's terminal outcome: the progress NOTIFY, or why it is unknown.

    Returned by :meth:`~hermes_voip.call.CallSession.transfer_blind` /
    :meth:`~hermes_voip.call.CallSession.transfer_attended` (ADR-0109 P2). Exactly
    one arm is populated: a terminal ``progress`` (with ``unknown_reason is None``)
    OR an ``unknown_reason`` naming why no terminal outcome arrived (with ``progress
    is None``). The invariant — ``unknown_reason`` is ``None`` iff a terminal
    ``progress`` arrived — lets the tool layer render a reason-specific message
    instead of the single always-"outcome not confirmed" string the bare ``None``
    return previously forced.

    Attributes:
        progress: The terminal transfer-progress NOTIFY, or ``None`` when none did.
        unknown_reason: Why no terminal outcome was reported, or ``None`` when a
            terminal ``progress`` did arrive.
    """

    progress: NotifyProgress | None
    unknown_reason: TransferUnknownReason | None


# The SIP status bands (RFC 3515 sipfrag): 2xx = the transfer succeeded, 3xx-6xx =
# it failed. A terminated status outside both bands (a malformed terminated 1xx) is
# treated as unknown rather than claimed either way.
_SUCCESS_STATUS_FLOOR = 200
_FAILURE_STATUS_FLOOR = 300
_FAILURE_STATUS_CEILING = 699


def classify_transfer_progress(
    progress: NotifyProgress | None,
) -> TransferOutcomeClass:
    """Classify a transfer's terminal progress NOTIFY into an outcome (ADR-0109).

    Pure: maps the terminal :class:`NotifyProgress` (or ``None`` when none was
    observed) to a :class:`TransferOutcomeClass`. Only a ``terminated`` NOTIFY yields
    a definitive verdict — a ``2xx`` is :attr:`~TransferOutcomeClass.COMPLETED`, a
    ``3xx``-``6xx`` is :attr:`~TransferOutcomeClass.FAILED`; ``None`` or a
    non-terminal update is :attr:`~TransferOutcomeClass.OUTCOME_UNKNOWN`.

    Args:
        progress: The terminal transfer-progress NOTIFY, or ``None`` when the bounded
            wait produced none (timeout, BYE, or a declined subscription).

    Returns:
        The terminal outcome classification.
    """
    if progress is None or not progress.terminated:
        return TransferOutcomeClass.OUTCOME_UNKNOWN
    if _SUCCESS_STATUS_FLOOR <= progress.status_code < _FAILURE_STATUS_FLOOR:
        return TransferOutcomeClass.COMPLETED
    if _FAILURE_STATUS_FLOOR <= progress.status_code <= _FAILURE_STATUS_CEILING:
        return TransferOutcomeClass.FAILED
    return TransferOutcomeClass.OUTCOME_UNKNOWN


# --- Transferor: build REFER ------------------------------------------------


def build_blind_refer(
    dialog: Dialog,
    target_uri: str,
    *,
    referred_by: str | None = None,
    auth: tuple[str, str] | None = None,
) -> InDialogRequest:
    """Build a blind-transfer REFER (``Refer-To: <target>``) in ``dialog``.

    ``auth`` is an optional ``(Authorization|Proxy-Authorization, value)`` header
    carried when re-sending after a 401/407.

    Raises:
        ValueError: If ``target_uri`` is not a clean transfer target — neither a
            dialable extension nor a well-formed ``sip:/sips:`` URI
            (``scheme:user@host[:port]``), or it carries an injection vector: a
            ``@`` host hijack on a bare extension, a ``?``-header form (e.g.
            ``?Replaces=``), a non-allowlisted ``;``-param (e.g. ``;Route=``), a
            malformed authority (``/path``, quote/comma, non-numeric port), an
            angle bracket, or a CR/LF / other control char / whitespace (literal
            or percent-encoded). The guard runs BEFORE the header is built, so an
            injection-bearing target never reaches the ``Refer-To`` and no REFER
            is produced.
    """
    _validate_transfer_target(target_uri)
    extra: list[tuple[str, str]] = [("Refer-To", f"<{target_uri}>")]
    if referred_by is not None:
        extra.append(("Referred-By", _wrap_uri(referred_by)))
    if auth is not None:
        extra.append(auth)
    return build_in_dialog_request(dialog, "REFER", extra_headers=tuple(extra))


def build_attended_refer(
    dialog: Dialog,
    consult: Dialog,
    *,
    referred_by: str | None = None,
    auth: tuple[str, str] | None = None,
) -> InDialogRequest:
    """Build an attended-transfer REFER in ``dialog`` (the primary call).

    The ``Refer-To`` targets the consultation peer and embeds a ``Replaces``
    naming the consultation dialog with RFC 3891 orientation from the target's
    point of view (``to-tag`` = the target's tag, ``from-tag`` = ours). The
    ``Replaces`` is percent-escaped so its ``;``/``=`` do not split the URI.
    ``auth`` carries an ``Authorization``/``Proxy-Authorization`` on a re-send.
    """
    replaces = ReplacesSpec(
        call_id=consult.call_id,
        to_tag=consult.remote_tag,
        from_tag=consult.local_tag,
    )
    escaped = quote(replaces.header_value(), safe="")
    # Join with '&' when the target URI already carries a header (a leading '?'),
    # never a malformed second '?'.
    separator = "&" if "?" in consult.remote_target else "?"
    refer_to = f"<{consult.remote_target}{separator}Replaces={escaped}>"
    extra: list[tuple[str, str]] = [("Refer-To", refer_to)]
    if referred_by is not None:
        extra.append(("Referred-By", _wrap_uri(referred_by)))
    if auth is not None:
        extra.append(auth)
    return build_in_dialog_request(dialog, "REFER", extra_headers=tuple(extra))


# --- Transferee/Target: parse REFER -----------------------------------------


def parse_refer(request: SipRequest) -> ReferRequest:
    """Parse an inbound REFER into its target, ``Replaces``, and ``Referred-By``.

    Two injection guards run in order before a :class:`ReferRequest` is built:

    1. **Target guard** (:func:`_validate_transfer_target`) — validates the
       pre-``?`` URI part (extension or ``sip:``/``sips:`` URI shape); rejects
       foreign-host hijacks, control chars, angle brackets, etc.
    2. **Query guard** (:func:`_validate_refer_to_query`) — if the URI carries a
       ``?``-query, it must contain ONLY a single ``Replaces=`` key
       (case-insensitive). Any other embedded header key (``?Route=``,
       ``?Header=``, etc.) or a duplicate ``Replaces`` is rejected as a
       header-injection vector, even when the pre-``?`` host is valid. A bare
       ``?Replaces=<dialog-id>`` is the legitimate attended-transfer case (RFC
       3515/3891) and is accepted.

    Raises:
        ReferError: if the REFER does not carry exactly one ``Refer-To`` header,
            or if the pre-``?`` target fails the injection guard, or if the
            ``?``-query carries any embedded header key other than ``Replaces``.
    """
    refer_to_values = request.headers_all("Refer-To")
    if len(refer_to_values) != 1:
        msg = f"REFER must have exactly one Refer-To header, got {len(refer_to_values)}"
        raise ReferError(msg)
    addr = _bracketed_uri(refer_to_values[0])
    target, _, query = addr.partition("?")
    # Guard 1: validate the pre-``?`` target (extension or sip: URI shape).
    # _validate_transfer_target raises ValueError; wrap as ReferError with a
    # FIXED message and ``from exc`` to preserve the cause-chain for debugging
    # (rule 37/34). The user-facing message never interpolates the exception text
    # or the raw target, so even a future leaky validator message cannot escape
    # here — the no-echo contract holds at this boundary regardless.
    try:
        _validate_transfer_target(target.strip())
    except ValueError as exc:
        msg = "REFER Refer-To target rejected by injection guard"
        raise ReferError(msg) from exc
    # Guard 2: if a ``?``-query is present, only ``?Replaces=`` is accepted.
    # Any other embedded header key (``?Route=``, ``?Header=``, …) or duplicate
    # ``Replaces`` is a header-injection vector; _validate_refer_to_query raises
    # ReferError directly with a sanitised message (no attacker-supplied content).
    _validate_refer_to_query(query)
    replaces = _replaces_from_uri_query(query)
    referred_by_raw = request.header("Referred-By")
    referred_by = _bracketed_uri(referred_by_raw) if referred_by_raw else None
    return ReferRequest(
        refer_to=target.strip(), replaces=replaces, referred_by=referred_by
    )


def build_triggered_invite(  # noqa: PLR0913 — an out-of-dialog INVITE needs the full local endpoint plus the transfer headers; all keyword-only
    *,
    target_uri: str,
    local_aor: str,
    local_contact: str,
    local_sent_by: str,
    transport: str,
    body: str = "",
    replaces: ReplacesSpec | None = None,
    referred_by: str | None = None,
    user_agent: str = _DEFAULT_USER_AGENT,
) -> str:
    """Build the out-of-dialog INVITE a transferee places to the REFER target.

    A fresh dialog (new ``Call-ID``/``From``-tag/branch, ``CSeq 1``). For an
    attended transfer ``replaces`` is rendered as a real ``Replaces`` header so
    the target can replace the consultation dialog; ``body`` (when present) is an
    SDP offer.
    """
    via = f"SIP/2.0/{transport} {local_sent_by};branch={new_branch()};rport"
    headers: list[tuple[str, str]] = [
        ("Via", via),
        ("Max-Forwards", _MAX_FORWARDS),
        ("From", f"<{local_aor}>;tag={new_tag()}"),
        ("To", f"<{target_uri}>"),
        ("Call-ID", new_call_id()),
        ("CSeq", "1 INVITE"),
        ("Contact", local_contact),
        ("User-Agent", user_agent),
    ]
    if replaces is not None:
        headers.append(("Replaces", replaces.header_value()))
    if referred_by is not None:
        headers.append(("Referred-By", _wrap_uri(referred_by)))
    if body:
        headers.append(_SDP_CONTENT_TYPE)
    return build_request("INVITE", target_uri, headers, body)


# --- Target: match an inbound Replaces --------------------------------------


def match_replaces(request: SipRequest, dialogs: Iterable[Dialog]) -> Dialog | None:
    """Return the dialog an inbound INVITE's ``Replaces`` names, or ``None``.

    RFC 3891 §3: the ``Replaces`` ``to-tag`` is our local tag and ``from-tag`` is
    our remote tag. Returns ``None`` when there is no ``Replaces`` header or no
    dialog matches (the caller answers ``481``).
    """
    raw = request.header("Replaces")
    if raw is None:
        return None
    spec = _parse_replaces_value(raw)
    for dialog in dialogs:
        if (
            dialog.call_id == spec.call_id
            and dialog.local_tag == spec.to_tag
            and dialog.remote_tag == spec.from_tag
        ):
            return dialog
    return None


# --- NOTIFY message/sipfrag progress ----------------------------------------


def build_notify_sipfrag(
    dialog: Dialog,
    status_line: str,
    *,
    subscription_state: str = _DEFAULT_SUBSCRIPTION_STATE,
) -> InDialogRequest:
    """Build a transfer-progress NOTIFY carrying a ``message/sipfrag`` status."""
    body = status_line if status_line.endswith("\r\n") else status_line + "\r\n"
    extra = (
        ("Event", "refer"),
        ("Subscription-State", subscription_state),
        _SIPFRAG_CONTENT_TYPE,
    )
    return build_in_dialog_request(dialog, "NOTIFY", extra_headers=extra, body=body)


def parse_notify_sipfrag(request: SipRequest) -> NotifyProgress:
    """Parse a transfer-progress NOTIFY's sipfrag status-line and subscription.

    Raises:
        ReferError: if the body has no ``SIP/2.0`` status-line.
    """
    subscription_raw = request.header("Subscription-State")
    if subscription_raw is None:
        msg = "NOTIFY has no Subscription-State header (RFC 6665 §8.2.1)"
        raise ReferError(msg)
    terminated = subscription_raw.strip().lower().startswith("terminated")
    first_line = request.body.strip().split("\n", 1)[0].strip()
    match = _SIPFRAG_STATUS.match(first_line)
    if match is None:
        msg = f"NOTIFY sipfrag body has no status-line: {first_line!r}"
        raise ReferError(msg)
    return NotifyProgress(
        status_code=int(match.group(1)),
        reason=match.group(2).strip(),
        terminated=terminated,
    )


# --- helpers ----------------------------------------------------------------


def _wrap_uri(value: str) -> str:
    return value if value.startswith("<") else f"<{value}>"


def _bracketed_uri(value: str) -> str:
    """Return the URI inside ``<...>``, or the bare value when unbracketed.

    The angle-addr is located outside any quoted display-name (RFC 3261 §25.1),
    so a bracketed display-name (e.g. ``"Support <Team>" <sip:…>``) cannot be
    mistaken for the addr-spec and corrupt a Refer-To/Referred-By target.
    """
    name_addr = find_name_addr(value)
    return name_addr[0].strip() if name_addr is not None else value.strip()


def _validate_refer_to_query(query: str) -> None:
    """Reject a Refer-To URI ``?``-query that carries any non-``Replaces`` header.

    An inbound ``Refer-To`` URI may legitimately carry ``?Replaces=<dialog-id>``
    for an attended transfer (RFC 3891 via RFC 3515), and ONLY that. Any other
    embedded header key — ``?Route=``, ``?Header=``, or any extra ``&``-joined
    header alongside a ``Replaces`` — is a header-injection vector and MUST be
    rejected before the query is parsed. Duplicate ``Replaces`` keys are equally
    rejected (ambiguous / attack-surface).

    Args:
        query: The raw ``?``-query string from the Refer-To URI (after
            ``partition("?")``, so without the leading ``?``).

    Raises:
        ReferError: If the query contains any key other than ``replaces``
            (case-insensitive), or more than one ``replaces`` key.
    """
    if not query:
        return
    replaces_count = 0
    for pair in query.split("&"):
        name, _sep, _val = pair.partition("=")
        key = name.strip().lower()
        if key != "replaces":
            # Do NOT echo the key — it is attacker-supplied.
            msg = (
                "Refer-To URI carries an embedded header other than 'Replaces' "
                "(header-injection vector rejected)"
            )
            raise ReferError(msg)
        replaces_count += 1
    if replaces_count > 1:
        msg = "Refer-To URI carries duplicate 'Replaces' embedded headers"
        raise ReferError(msg)


def _replaces_from_uri_query(query: str) -> ReplacesSpec | None:
    """Extract a ``Replaces`` from a Refer-To URI header query, if present.

    Callers MUST invoke :func:`_validate_refer_to_query` first to ensure the
    query contains only an optional single ``Replaces=`` key; this function
    then parses that key. It returns ``None`` when the query is empty or carries
    no ``Replaces`` pair (which cannot happen after a valid-query guard, but is
    kept defensively for the no-``?`` case).
    """
    if not query:
        return None
    for pair in query.split("&"):
        name, _, raw = pair.partition("=")
        if name.strip().lower() == "replaces":
            return _parse_replaces_value(unquote(raw))
    return None


def _parse_replaces_value(value: str) -> ReplacesSpec:
    """Parse a ``Replaces`` value ``call-id;to-tag=..;from-tag=..`` (RFC 3891).

    Requires a non-empty call-id and **exactly one** non-empty ``to-tag`` and
    ``from-tag`` each; duplicates or empties are rejected.
    """
    parts = value.split(";")
    call_id = parts[0].strip()
    to_tags: list[str] = []
    from_tags: list[str] = []
    early_only = False
    for part in parts[1:]:
        key, sep, val = part.partition("=")
        name = key.strip().lower()
        if name == "to-tag" and sep:
            to_tags.append(val.strip())
        elif name == "from-tag" and sep:
            from_tags.append(val.strip())
        elif name == "early-only":
            early_only = True
    if not call_id:
        msg = f"Replaces requires a call-id: {value!r}"
        raise ReferError(msg)
    to_tag = _single_tag(to_tags, "to-tag", value)
    from_tag = _single_tag(from_tags, "from-tag", value)
    return ReplacesSpec(
        call_id=call_id, to_tag=to_tag, from_tag=from_tag, early_only=early_only
    )


def _single_tag(tags: list[str], name: str, value: str) -> str:
    """Return the one non-empty tag, rejecting absence, duplicates, or emptiness."""
    if len(tags) != 1 or not tags[0]:
        msg = f"Replaces requires exactly one non-empty {name}: {value!r}"
        raise ReferError(msg)
    return tags[0]
