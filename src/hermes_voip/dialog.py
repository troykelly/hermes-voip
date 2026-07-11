"""Sans-IO SIP dialog state and in-dialog request builder (RFC 3261 §12).

A :class:`Dialog` captures the routing identity of an established call — the
peer's target URI, the route set, the local/remote URIs and tags, and the local
``Call-ID`` — plus the two counters that drive in-dialog requests:

* ``local_cseq`` — the dialog's local sequence number; **every** request we send
  in the dialog increments it (RFC 3261 §12.2.1.1).
* ``sdp_version`` — the ``o=`` version of the offers/answers **we** send; it
  advances only when our SDP changes (a re-INVITE/UPDATE with a new body).

These counters are **independent** (ADR-0011 invariant 1): a BYE/REFER/NOTIFY
bumps CSeq alone; an SDP offer bumps the version alone; a hold re-INVITE bumps
both, in that order. The :class:`Dialog` is frozen — :func:`build_in_dialog_request`
and :meth:`Dialog.with_next_sdp_version` return a new dialog with the advanced
counter, so the call owner threads the state forward explicitly (no hidden
mutation), exactly like the rest of the sans-IO core.

This module owns no socket and no timer. It mirrors ``RegistrationFlow._build``:
produce wire text, return the next state, and let the transport do the IO.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from hermes_voip._header_list import split_header_list
from hermes_voip._name_addr import find_name_addr, params_after_addr, tag_param
from hermes_voip.message import (
    SipRequest,
    SipResponse,
    build_request,
    new_branch,
)

__all__ = [
    "Dialog",
    "DialogError",
    "InDialogRequest",
    "build_in_dialog_request",
]

_DEFAULT_USER_AGENT = "hermes-voip/0"
_MAX_FORWARDS = "70"

# RFC 3261 §8.1.1.5: the CSeq sequence number must be below 2**31.
_MAX_CSEQ = 2**31

# Anchored at the start of the topmost Via value: transport then sent-by
# (host:port, no whitespace or parameters).
_VIA_TOP = re.compile(r"SIP/2\.0/(\S+)\s+([^\s;]+)")
# RFC 3261 §25: the CSeq number is 1*DIGIT (US-ASCII 0x30-0x39) and the method is a
# token — both ASCII. ``[0-9]`` (not Unicode-aware ``\d``) plus ``re.ASCII`` (so ``\s``
# and ``\w`` never match Unicode) reject fullwidth/Arabic-Indic digits an RFC-strict
# proxy would parse differently — a parser-differential (same class as #479/#485).
_CSEQ = re.compile(r"([0-9]+)\s+(\w+)", re.ASCII)
# A loose-routing first hop carries an ``lr`` flag parameter (RFC 3261 §16.12).
_LR_PARAM = re.compile(r";\s*lr\b", re.IGNORECASE)

type _Message = SipRequest | SipResponse


class DialogError(ValueError):
    """A message lacks the headers required to establish or use a dialog."""


@dataclass(frozen=True, slots=True)
class Dialog:
    """The routing identity and counters of one established dialog.

    Attributes:
        call_id: The dialog ``Call-ID`` (from the INVITE).
        local_uri: Our address-of-record (the ``From`` URI as UAC, ``To`` as UAS).
        local_tag: Our dialog tag.
        remote_uri: The peer's address-of-record.
        remote_tag: The peer's dialog tag.
        remote_target: The peer's ``Contact`` URI — the request-URI for
            in-dialog requests under loose routing.
        route_set: The ordered route set (URIs from ``Record-Route``), emitted as
            ``Route`` headers; reversed for a UAC, in-order for a UAS.
        local_contact: Our ``Contact`` header value (re-emitted on each request).
        local_sent_by: Our Via ``sent-by``.
        transport: Our Via transport token (``TLS`` | ``WSS``).
        local_cseq: The dialog's local sequence number.
        sdp_version: The ``o=`` version of the SDP we last offered/answered.
        user_agent: The ``User-Agent`` header value.
    """

    call_id: str
    local_uri: str
    local_tag: str
    remote_uri: str
    remote_tag: str
    remote_target: str
    route_set: tuple[str, ...]
    local_contact: str
    local_sent_by: str
    transport: str
    local_cseq: int
    sdp_version: int
    user_agent: str = _DEFAULT_USER_AGENT

    @property
    def dialog_id(self) -> tuple[str, str, str]:
        """The demux key ``(Call-ID, local-tag, remote-tag)`` (ADR-0011)."""
        return (self.call_id, self.local_tag, self.remote_tag)

    def with_next_sdp_version(self) -> Dialog:
        """Return a copy with ``sdp_version`` advanced; ``local_cseq`` untouched.

        Called when building a fresh SDP offer/answer (e.g. a hold re-INVITE),
        so the ``o=`` version increments while the SIP session-id stays constant.
        """
        return replace(self, sdp_version=self.sdp_version + 1)

    def with_remote_target(self, target: str) -> Dialog:
        """Return a copy whose ``remote_target`` is refreshed (target refresh)."""
        return replace(self, remote_target=target)

    @classmethod
    def from_invite_2xx(cls, invite: SipRequest, response: SipResponse) -> Dialog:
        """Build the UAC dialog after our INVITE is answered with a 2xx.

        Our identity comes from the INVITE we sent (``From``/``Contact``/``Via``/
        ``CSeq``); the peer's comes from the 2xx (``To``-tag/``Contact``/
        ``Record-Route``). The route set is the 2xx ``Record-Route`` **reversed**.
        """
        local_uri, local_tag = _uri_and_tag(_require(invite, "From"))
        remote_uri, remote_tag = _uri_and_tag(_require(response, "To"))
        if local_tag is None:
            msg = "INVITE From header has no tag"
            raise DialogError(msg)
        if remote_tag is None:
            msg = "2xx To header has no tag"
            raise DialogError(msg)
        transport, sent_by = _via_transport_sent_by(_require(invite, "Via"))
        cseq, _ = _cseq(_require(invite, "CSeq"))
        return cls(
            call_id=_require(invite, "Call-ID"),
            local_uri=local_uri,
            local_tag=local_tag,
            remote_uri=remote_uri,
            remote_tag=remote_tag,
            remote_target=_addr_spec(_require(response, "Contact")),
            route_set=tuple(reversed(_record_route_set(response))),
            local_contact=_require(invite, "Contact"),
            local_sent_by=sent_by,
            transport=transport,
            local_cseq=cseq,
            sdp_version=0,
            user_agent=invite.header("User-Agent") or _DEFAULT_USER_AGENT,
        )

    @classmethod
    def from_inbound_invite(  # noqa: PLR0913 — the UAS local endpoint (tag, contact, sent-by, transport) is minted by the caller, not derivable from the inbound INVITE
        cls,
        invite: SipRequest,
        *,
        local_tag: str,
        local_contact: str,
        local_sent_by: str,
        transport: str,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> Dialog:
        """Build the UAS dialog for an inbound INVITE we are answering.

        The peer's identity comes from the INVITE (``From``-tag/``Contact``); our
        tag/Contact/sent-by are supplied by the caller (we mint them — they go
        into our 2xx). The route set is the INVITE ``Record-Route`` **in order**.
        The local sequence number starts empty (``0``); our first in-dialog
        request is CSeq ``1``.
        """
        remote_uri, remote_tag = _uri_and_tag(_require(invite, "From"))
        local_uri, _ = _uri_and_tag(_require(invite, "To"))
        if remote_tag is None:
            msg = "inbound INVITE From header has no tag"
            raise DialogError(msg)
        return cls(
            call_id=_require(invite, "Call-ID"),
            local_uri=local_uri,
            local_tag=local_tag,
            remote_uri=remote_uri,
            remote_tag=remote_tag,
            remote_target=_addr_spec(_require(invite, "Contact")),
            route_set=_record_route_set(invite),
            local_contact=local_contact,
            local_sent_by=local_sent_by,
            transport=transport,
            local_cseq=0,
            sdp_version=0,
            user_agent=user_agent,
        )


@dataclass(frozen=True, slots=True)
class InDialogRequest:
    """The wire text of an in-dialog request and the dialog with advanced CSeq."""

    text: str
    dialog: Dialog


def build_in_dialog_request(
    dialog: Dialog,
    method: str,
    *,
    extra_headers: Sequence[tuple[str, str]] = (),
    body: str = "",
) -> InDialogRequest:
    """Build an in-dialog request and return it with the next dialog state.

    Increments ``local_cseq`` by one (and **only** CSeq — the SDP version is the
    caller's concern, advanced via :meth:`Dialog.with_next_sdp_version` before
    this call when the body carries a new offer). Under loose routing the
    request-URI is the remote target and the route set is emitted as ``Route``
    headers; ``extra_headers`` (e.g. ``Refer-To``, ``Content-Type``) follow the
    standard block, and ``Content-Length`` is computed by :func:`build_request`.

    Loose routing only: a strict-router first hop (a route set head without an
    ``lr`` parameter) raises :class:`DialogError` rather than producing a
    silently mis-routed request. Strict routers predate RFC 3261 and no
    RFC-compliant gateway uses them.

    Raises:
        DialogError: if the route set head is a strict router.
    """
    if dialog.route_set and _LR_PARAM.search(dialog.route_set[0]) is None:
        msg = (
            "route set head is a strict router (no lr parameter); "
            "only loose routing is supported"
        )
        raise DialogError(msg)
    next_cseq = dialog.local_cseq + 1
    if next_cseq >= _MAX_CSEQ:
        msg = (
            f"CSeq sequence {next_cseq} must be below 2**31 (RFC 3261 §8.1.1.5); "
            "dialog has exhausted its local sequence number space"
        )
        raise DialogError(msg)
    via = (
        f"SIP/2.0/{dialog.transport} {dialog.local_sent_by};branch={new_branch()};rport"
    )
    headers: list[tuple[str, str]] = [
        ("Via", via),
        ("Max-Forwards", _MAX_FORWARDS),
    ]
    headers += [("Route", route) for route in dialog.route_set]
    headers += [
        ("From", f"<{dialog.local_uri}>;tag={dialog.local_tag}"),
        ("To", f"<{dialog.remote_uri}>;tag={dialog.remote_tag}"),
        ("Call-ID", dialog.call_id),
        ("CSeq", f"{next_cseq} {method}"),
        ("Contact", dialog.local_contact),
        ("User-Agent", dialog.user_agent),
    ]
    headers.extend(extra_headers)
    text = build_request(method, dialog.remote_target, headers, body)
    return InDialogRequest(text=text, dialog=replace(dialog, local_cseq=next_cseq))


# --- header parsing helpers -------------------------------------------------


def _record_route_set(message: _Message) -> tuple[str, ...]:
    """Return the ordered route set from a message's ``Record-Route`` headers.

    A single ``Record-Route`` header may combine several proxy URIs with
    top-level commas (RFC 3261 §7.3.1), so every header value is split into its
    individual entries before being flattened — otherwise a multi-proxy route set
    collapses into ONE entry and the in-dialog request emits a single malformed
    ``Route`` line. Entries are returned in received order; the caller reverses
    them for a UAC (§12.1.2) and keeps them in order for a UAS (§12.1.1).
    """
    return tuple(
        entry
        for header in message.headers_all("Record-Route")
        for entry in split_header_list(header)
    )


def _require(message: _Message, name: str) -> str:
    value = message.header(name)
    if value is None:
        msg = f"message has no {name} header"
        raise DialogError(msg)
    return value


def _addr_spec(value: str) -> str:
    """Return the (non-empty) addr-spec from a name-addr / addr-spec value.

    Inside ``<...>`` if present (params there are URI params), otherwise up to
    the first ``;`` (where params are header params, per RFC 3261 §20). The
    angle-addr is located outside any quoted display-name (RFC 3261 §25.1), so a
    bracketed display-name (e.g. ``"Support <Team>" <sip:…>``) cannot be mistaken
    for the addr-spec.
    """
    name_addr = find_name_addr(value)
    spec = (
        name_addr[0].strip()
        if name_addr is not None
        else value.split(";", 1)[0].strip()
    )
    if not spec:
        msg = f"empty addr-spec in header: {value!r}"
        raise DialogError(msg)
    return spec


def _uri_and_tag(value: str) -> tuple[str, str | None]:
    """Return ``(addr-spec, tag)`` from a ``From``/``To`` header value."""
    uri = _addr_spec(value)
    # Header parameters live after the closing ``>`` of a name-addr, or after the
    # first ``;`` for a bare addr-spec (which has no URI parameters). Locate the
    # ``<addr-spec>`` the same way ``_addr_spec`` does — quote-aware, outside any
    # display-name — so a literal ``<`` or ``>`` inside a quoted display-name
    # (RFC 3261 §25.1) cannot desync the split and hide the tag by matching on the
    # wrong bracket.
    name_addr = find_name_addr(value)
    params = name_addr[1] if name_addr is not None else params_after_addr(value)
    return uri, tag_param(params)


def _via_transport_sent_by(value: str) -> tuple[str, str]:
    """Return ``(transport, sent-by)`` from the **topmost** Via header value.

    A comma-folded Via combines several Via values; only the first (our own) is
    parsed, and the match is anchored so a garbled prefix is rejected.
    """
    topmost = value.split(",", 1)[0].strip()
    match = _VIA_TOP.match(topmost)
    if match is None:
        msg = f"malformed Via header: {value!r}"
        raise DialogError(msg)
    return match.group(1), match.group(2)


def _cseq(value: str) -> tuple[int, str]:
    """Return ``(sequence, method)`` from a ``CSeq`` header value."""
    match = _CSEQ.fullmatch(value.strip())
    if match is None:
        msg = f"malformed CSeq header: {value!r}"
        raise DialogError(msg)
    sequence = int(match.group(1))
    if sequence >= _MAX_CSEQ:
        msg = f"CSeq sequence {sequence} must be below 2**31 (RFC 3261 §8.1.1.5)"
        raise DialogError(msg)
    return sequence, match.group(2)
