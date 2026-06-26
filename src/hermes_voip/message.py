"""SIP message assembly and response parsing (RFC 3261, transport-agnostic).

A SIP request is a start-line, CRLF-folded headers, a blank line, then an
optional body; a response replaces the start-line with a status-line. This
module builds requests and parses responses as plain text — it owns no socket,
TLS, or WebSocket concern (those belong to the transport layer) and no dialog
state. Token generators produce the per-transaction identifiers (branch, tag,
Call-ID) a registrar needs.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass

# RFC 3261 §8.1.1.7: a branch value MUST begin with this magic cookie.
_MAGIC_COOKIE = "z9hG4bK"
_CRLF = "\r\n"
# A status-line is "SIP/2.0 <code> <reason>" with a mandatory single SP before
# the (possibly empty) reason phrase: "SIP/2.0 200OK" (no SP) and "SIP/2.0 200"
# (no reason SP at all) are both rejected as malformed framing, while
# "SIP/2.0 200 " parses with an empty reason. build_response always emits the
# SP, so parse and build agree on the wire shape.
_STATUS_LINE = re.compile(r"SIP/2\.0 (\d{3}) (.*)")
# A request-line is "METHOD request-uri SIP/2.0"; method is an RFC 3261 token
# (covers extension methods, not just the alphabetic standard ones).
_REQUEST_LINE = re.compile(r"([!#$%&'*+.^_`|~0-9A-Za-z-]+) (\S+) SIP/2\.0")
# A header field name is an RFC 3261 token: no whitespace, colon, or controls.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# A To/From header already carrying a dialog tag parameter.
_TAG_PRESENT = re.compile(r";\s*tag=", re.IGNORECASE)
# The header this module computes itself; callers must not also supply it.
_CONTENT_LENGTH = "Content-Length"

# A SIP status code is in the 1xx..6xx range.
_MIN_STATUS = 100
_MAX_STATUS = 699

# Forbidden control characters: the ASCII C0 range (code points below this) and DEL.
_C0_END = 0x20
_DEL = 0x7F


def _reject_controls(value: str, what: str) -> None:
    """Raise if ``value`` carries a control character (CR/LF/NUL injection guard)."""
    if any(ord(char) < _C0_END or ord(char) == _DEL for char in value):
        msg = f"{what} contains a control character"
        raise ValueError(msg)


def _parse_headers(lines: list[str]) -> tuple[tuple[str, str], ...]:
    """Unfold (RFC 3261 §7.3.1) and split header lines into ``(name, value)`` pairs.

    Raises:
        ValueError: If a continuation line has no preceding header.
    """
    unfolded: list[str] = []
    for line in lines:
        if line[:1] in (" ", "\t"):
            if not unfolded:
                msg = "header continuation line with no preceding header"
                raise ValueError(msg)
            unfolded[-1] = f"{unfolded[-1]} {line.strip()}"
        else:
            unfolded.append(line)
    headers: list[tuple[str, str]] = []
    for line in unfolded:
        name, sep, value = line.partition(":")
        if sep:
            headers.append((name.strip(), value.strip()))
        elif line:
            # A non-empty line with no colon is not a valid header field — it is
            # not a continuation (those are handled above) and not blank. Silently
            # dropping it would allow garbage to vanish undetected, which is
            # inconsistent with the module's strict-parse stance.
            msg = f"malformed header line (no colon separator): {line!r}"
            raise ValueError(msg)
    return tuple(headers)


def _header_value(headers: tuple[tuple[str, str], ...], name: str) -> str | None:
    low = name.lower()
    for field_name, value in headers:
        if field_name.lower() == low:
            return value
    return None


def _header_values(headers: tuple[tuple[str, str], ...], name: str) -> tuple[str, ...]:
    low = name.lower()
    return tuple(v for field_name, v in headers if field_name.lower() == low)


def new_branch() -> str:
    """Return a fresh Via branch token with the RFC 3261 magic cookie prefix."""
    return _MAGIC_COOKIE + secrets.token_hex(8)


def new_tag() -> str:
    """Return a fresh From/To tag (random hex)."""
    return secrets.token_hex(6)


def new_call_id() -> str:
    """Return a fresh, globally-unique Call-ID (random hex)."""
    return secrets.token_hex(12)


def build_request(
    method: str,
    request_uri: str,
    headers: Sequence[tuple[str, str]],
    body: str = "",
) -> str:
    """Assemble a SIP request as wire text.

    ``Content-Length`` is computed from the UTF-8 byte length of ``body`` and
    appended automatically; callers supply every other header in order and must
    NOT pass their own ``Content-Length`` (a duplicate would make the message
    ambiguously framed per RFC 7230 §3.3.3).

    Args:
        method: The SIP method (e.g. ``REGISTER``, ``INVITE``).
        request_uri: The request URI (e.g. ``sip:host``).
        headers: Header ``(name, value)`` pairs, emitted in the given order.
        body: The message body; defaults to empty.

    Returns:
        The full request text, terminated by the blank line then ``body``.

    Raises:
        ValueError: If the method, request URI, or any header name/value would
            corrupt the message (control characters; a non-token header name),
            or if a caller supplies a ``Content-Length`` header (it is owned by
            this function).
    """
    _reject_controls(method, "method")
    if not method or _HEADER_NAME.fullmatch(method) is None:
        msg = f"method must be a non-empty RFC 3261 token (no spaces): {method!r}"
        raise ValueError(msg)
    _reject_controls(request_uri, "request URI")
    if not request_uri or any(c in request_uri for c in (" ", "\t")):
        msg = (
            f"request URI must be non-empty and contain no whitespace: {request_uri!r}"
        )
        raise ValueError(msg)
    lines = [f"{method} {request_uri} SIP/2.0"]
    for name, value in headers:
        if _HEADER_NAME.fullmatch(name) is None:
            msg = f"invalid header name: {name!r}"
            raise ValueError(msg)
        if name.lower() == _CONTENT_LENGTH.lower():
            msg = "Content-Length is computed automatically; do not supply it"
            raise ValueError(msg)
        _reject_controls(value, "header value")
        lines.append(f"{name}: {value}")
    lines.append(f"{_CONTENT_LENGTH}: {len(body.encode('utf-8'))}")
    return _CRLF.join(lines) + _CRLF + _CRLF + body


def build_response(  # noqa: PLR0913 — status, reason, to-tag, extra headers and body are irreducible response fields; 3 are keyword-only
    request: SipRequest,
    status_code: int,
    reason: str,
    *,
    to_tag: str | None = None,
    extra_headers: Sequence[tuple[str, str]] = (),
    body: str = "",
) -> str:
    """Assemble a SIP response to ``request`` as wire text (RFC 3261 §8.2.6).

    Echoes the request's full ``Via`` stack (in order), ``From``, ``To``,
    ``Call-ID`` and ``CSeq`` so the response routes back. When ``to_tag`` is
    given and the request's ``To`` has no tag, it is appended (a dialog-forming
    2xx); a ``To`` that already carries a tag (an in-dialog request) is echoed
    unchanged. ``Content-Length`` is computed from ``body``.

    Args:
        request: The request being answered.
        status_code: The response status (100..699).
        reason: The reason phrase (may be empty).
        to_tag: Our dialog tag, added to ``To`` only when it has none.
        extra_headers: Response headers (e.g. ``Contact``, ``Content-Type``).
        body: The message body.

    Raises:
        ValueError: If ``status_code`` is out of range, the request is missing a
            mandatory header to echo, or a header name/value would corrupt the
            message.
    """
    if not _MIN_STATUS <= status_code <= _MAX_STATUS:
        msg = f"status code out of range 100..699: {status_code}"
        raise ValueError(msg)
    _reject_controls(reason, "reason phrase")
    for name, _ in extra_headers:
        if name.lower() == _CONTENT_LENGTH.lower():
            msg = "Content-Length is computed automatically; do not supply it"
            raise ValueError(msg)
    vias = request.headers_all("Via")
    if not vias:
        msg = "cannot build a response: request has no Via header"
        raise ValueError(msg)
    from_value = _require_echo(request, "From")
    to_value = _require_echo(request, "To")
    call_id = _require_echo(request, "Call-ID")
    cseq = _require_echo(request, "CSeq")
    if to_tag is not None and _TAG_PRESENT.search(_after_angle(to_value)) is None:
        to_value = f"{to_value};tag={to_tag}"
    headers: list[tuple[str, str]] = [("Via", via) for via in vias]
    headers.append(("From", from_value))
    headers.append(("To", to_value))
    headers.append(("Call-ID", call_id))
    headers.append(("CSeq", cseq))
    headers.extend(extra_headers)
    lines = [f"SIP/2.0 {status_code} {reason}"]
    for name, value in headers:
        if _HEADER_NAME.fullmatch(name) is None:
            msg = f"invalid header name: {name!r}"
            raise ValueError(msg)
        _reject_controls(value, "header value")
        lines.append(f"{name}: {value}")
    lines.append(f"Content-Length: {len(body.encode('utf-8'))}")
    return _CRLF.join(lines) + _CRLF + _CRLF + body


def _require_echo(request: SipRequest, name: str) -> str:
    value = request.header(name)
    if value is None:
        msg = f"cannot build a response: request has no {name} header"
        raise ValueError(msg)
    return value


def _after_angle(header_value: str) -> str:
    """The header-parameter span of a name-addr value (after ``>``)."""
    return header_value.split(">", 1)[1] if ">" in header_value else header_value


@dataclass(frozen=True, slots=True)
class SipResponse:
    """A parsed SIP response: status-line, headers, and body.

    Attributes:
        status_code: The numeric status (e.g. ``200``, ``401``).
        reason: The reason phrase (e.g. ``OK``, ``Unauthorized``).
        headers: Header ``(name, value)`` pairs in received order.
        body: The message body (empty when absent).
    """

    status_code: int
    reason: str
    headers: tuple[tuple[str, str], ...]
    body: str

    def header(self, name: str) -> str | None:
        """Return the first header value matching ``name`` (case-insensitive)."""
        return _header_value(self.headers, name)

    def headers_all(self, name: str) -> tuple[str, ...]:
        """Return every header value matching ``name`` (case-insensitive)."""
        return _header_values(self.headers, name)

    @classmethod
    def parse(cls, raw: str) -> SipResponse:
        """Parse response wire text into a :class:`SipResponse`.

        Args:
            raw: The full response text (status-line, headers, blank line, body).

        Returns:
            The parsed response.

        Raises:
            ValueError: If the first line is not a valid SIP status-line, or a
                header continuation line has no preceding header.

        Note:
            ``raw`` must be exactly one complete message. Octet-accurate stream
            framing (consuming ``Content-Length`` bytes, splitting pipelined
            messages) is the transport layer's responsibility, not this parser's.
        """
        head, _, body = raw.partition(_CRLF + _CRLF)
        lines = head.split(_CRLF)
        match = _STATUS_LINE.fullmatch(lines[0])
        if match is None:
            msg = f"not a SIP status-line: {lines[0]!r}"
            raise ValueError(msg)
        return cls(
            status_code=int(match.group(1)),
            # group(2) is always present (possibly "") now the SP is mandatory.
            reason=match.group(2).strip(),
            headers=_parse_headers(lines[1:]),
            body=body,
        )


@dataclass(frozen=True, slots=True)
class SipRequest:
    """A parsed inbound SIP request: request-line, headers, and body.

    Attributes:
        method: The SIP method (e.g. ``INVITE``, ``REFER``, ``NOTIFY``, ``BYE``).
        request_uri: The request URI.
        headers: Header ``(name, value)`` pairs in received order.
        body: The message body (empty when absent).
    """

    method: str
    request_uri: str
    headers: tuple[tuple[str, str], ...]
    body: str

    def header(self, name: str) -> str | None:
        """Return the first header value matching ``name`` (case-insensitive)."""
        return _header_value(self.headers, name)

    def headers_all(self, name: str) -> tuple[str, ...]:
        """Return every header value matching ``name`` (case-insensitive)."""
        return _header_values(self.headers, name)

    @classmethod
    def parse(cls, raw: str) -> SipRequest:
        """Parse request wire text into a :class:`SipRequest`.

        Args:
            raw: One complete request (request-line, headers, blank line, body).

        Returns:
            The parsed request.

        Raises:
            ValueError: If the first line is not a valid SIP request-line, or a
                header continuation line has no preceding header.
        """
        head, _, body = raw.partition(_CRLF + _CRLF)
        lines = head.split(_CRLF)
        match = _REQUEST_LINE.fullmatch(lines[0])
        if match is None:
            msg = f"not a SIP request-line: {lines[0]!r}"
            raise ValueError(msg)
        return cls(
            method=match.group(1),
            request_uri=match.group(2),
            headers=_parse_headers(lines[1:]),
            body=body,
        )
