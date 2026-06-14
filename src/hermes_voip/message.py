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
# A status-line requires a single SP between the code and the (possibly empty)
# reason phrase, so "SIP/2.0 200OK" is rejected as malformed framing.
_STATUS_LINE = re.compile(r"SIP/2\.0 (\d{3})(?: (.*))?")
# A header field name is an RFC 3261 token: no whitespace, colon, or controls.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

# Forbidden control characters: the ASCII C0 range (code points below this) and DEL.
_C0_END = 0x20
_DEL = 0x7F


def _reject_controls(value: str, what: str) -> None:
    """Raise if ``value`` carries a control character (CR/LF/NUL injection guard)."""
    if any(ord(char) < _C0_END or ord(char) == _DEL for char in value):
        msg = f"{what} contains a control character"
        raise ValueError(msg)


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
    appended automatically; callers supply every other header in order.

    Args:
        method: The SIP method (e.g. ``REGISTER``, ``INVITE``).
        request_uri: The request URI (e.g. ``sip:host``).
        headers: Header ``(name, value)`` pairs, emitted in the given order.
        body: The message body; defaults to empty.

    Returns:
        The full request text, terminated by the blank line then ``body``.

    Raises:
        ValueError: If the method, request URI, or any header name/value would
            corrupt the message (control characters; a non-token header name).
    """
    _reject_controls(method, "method")
    _reject_controls(request_uri, "request URI")
    lines = [f"{method} {request_uri} SIP/2.0"]
    for name, value in headers:
        if _HEADER_NAME.fullmatch(name) is None:
            msg = f"invalid header name: {name!r}"
            raise ValueError(msg)
        _reject_controls(value, "header value")
        lines.append(f"{name}: {value}")
    lines.append(f"Content-Length: {len(body.encode('utf-8'))}")
    return _CRLF.join(lines) + _CRLF + _CRLF + body


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
        low = name.lower()
        for field_name, value in self.headers:
            if field_name.lower() == low:
                return value
        return None

    def headers_all(self, name: str) -> tuple[str, ...]:
        """Return every header value matching ``name`` (case-insensitive)."""
        low = name.lower()
        return tuple(v for field_name, v in self.headers if field_name.lower() == low)

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
        # RFC 3261 §7.3.1: a line starting with SP/HTAB continues the prior header.
        unfolded: list[str] = []
        for line in lines[1:]:
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
        return cls(
            status_code=int(match.group(1)),
            reason=match.group(2).strip(),
            headers=tuple(headers),
            body=body,
        )
