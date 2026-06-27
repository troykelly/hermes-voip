"""Frame a SIP byte stream into whole messages by Content-Length (RFC 3261 §7.5).

Over a stream transport (TLS/TCP) a SIP message is **not** self-delimiting at the
read boundary: one ``recv`` may carry a fragment, a whole message, or several
messages, and the head/body split can fall anywhere. :class:`SipMessageFramer`
buffers received bytes and yields each complete message as text: it reads the
message head up to the first ``CRLFCRLF``, takes the **exact** ``Content-Length``
body bytes that follow, and retains any surplus for the next message.

It is **sans-IO** — it owns no socket; the transport feeds it bytes and iterates
the messages — and it parses no further than the framing boundary:
:meth:`~hermes_voip.message.SipResponse.parse` /
:meth:`~hermes_voip.message.SipRequest.parse` own the rest. Bodies are framed by
byte count, so a body with embedded ``CRLFCRLF`` (multipart) frames correctly.
"""

from __future__ import annotations

from collections.abc import Iterator

__all__ = ["FramingError", "SipMessageFramer"]

_CRLF = b"\r\n"
_HEAD_END = b"\r\n\r\n"
# Content-Length and its RFC 3261 §20 compact form ``l`` (case-insensitive).
_CONTENT_LENGTH_NAMES = frozenset({"content-length", "l"})

# Defensive bounds: real SIP messages are a few KiB (large SDP+ICE ~8 KiB); these
# caps sit far above any legitimate message yet bound memory if a peer never
# terminates a head or declares an oversized body (the TLS peer is authenticated
# but may be buggy/compromised).
_MAX_HEAD = 64 * 1024
_MAX_MESSAGE = 256 * 1024


class FramingError(ValueError):
    """A message cannot be framed: a malformed, missing, or oversized boundary.

    Raised for an absent / unparseable / out-of-range ``Content-Length``, a head
    that exceeds :data:`_MAX_HEAD` without terminating, or a declared message size
    above :data:`_MAX_MESSAGE`. The stream cannot be re-synchronised after the
    first three (the next boundary is unknown); the size caps fault before the
    buffer can grow toward an oversized body. The transport surfaces it (rule 37),
    never guesses.
    """


class SipMessageFramer:
    """Accumulate received bytes and yield complete SIP messages as text."""

    def __init__(self) -> None:
        """Start with an empty buffer."""
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        """Append received bytes; completed messages become available via iteration."""
        self._buffer += data

    def __iter__(self) -> Iterator[str]:
        """Yield and consume every message currently complete in the buffer."""
        while True:
            message = self._next_message()
            if message is None:
                return
            yield message

    def _next_message(self) -> str | None:
        """Pop one complete message, or return ``None`` if none is complete yet.

        Raises:
            FramingError: if a complete head declares no usable Content-Length, an
                unterminated head exceeds :data:`_MAX_HEAD`, or the declared
                message size exceeds :data:`_MAX_MESSAGE`.
        """
        self._skip_keepalive_crlf()
        end = self._buffer.find(_HEAD_END)
        if end == -1:
            if len(self._buffer) > _MAX_HEAD:
                msg = f"message head exceeds {_MAX_HEAD} bytes without termination"
                raise FramingError(msg)
            return None  # head not yet terminated
        head = bytes(self._buffer[:end]).decode("utf-8", errors="replace")
        body_start = end + len(_HEAD_END)
        content_length = _content_length(head)
        body_end = body_start + content_length
        if body_end > _MAX_MESSAGE:
            msg = f"declared message size {body_end} exceeds {_MAX_MESSAGE} bytes"
            raise FramingError(msg)
        if len(self._buffer) < body_end:
            return None  # body not fully arrived
        message = bytes(self._buffer[:body_end])
        del self._buffer[:body_end]
        return message.decode("utf-8")

    def _skip_keepalive_crlf(self) -> None:
        """Drop leading CRLF keep-alive pings/pongs (RFC 5626 §3.5.1 / RFC 6223).

        A stream UA may send a bare ``CRLF`` (ping) or ``CRLFCRLF`` (pong) for NAT
        keep-alive; these precede no message, so they are consumed before the head
        is located (otherwise an empty head would look like a missing
        Content-Length and fault the stream).
        """
        skip = 0
        while self._buffer[skip : skip + len(_CRLF)] == _CRLF:
            skip += len(_CRLF)
        if skip:
            del self._buffer[:skip]


def _content_length(head: str) -> int:
    """Extract the Content-Length value from a decoded message head (§20.14)."""
    # Skip the start-line; unfold RFC 3261 §7.3.1 continuations before scanning
    # for Content-Length / compact ``l`` so folded headers frame identically to
    # the full header parser in ``message._parse_headers``.
    unfolded: list[str] = []
    for line in head.split(_CRLF.decode())[1:]:
        if line[:1] in (" ", "\t"):
            if not unfolded:
                msg = "header continuation line with no preceding header"
                raise FramingError(msg)
            unfolded[-1] = f"{unfolded[-1]} {line.strip()}"
        else:
            unfolded.append(line)
    found: int | None = None
    for line in unfolded:
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() in _CONTENT_LENGTH_NAMES:
            if found is not None:
                msg = "duplicate Content-Length header"
                raise FramingError(msg)
            stripped = value.strip()
            # RFC 3261 Content-Length is ASCII 1*DIGIT. ``isascii() and
            # isdecimal()`` rejects superscripts/exotic Unicode digits (which
            # ``isdigit()`` admits but ``int()`` cannot parse), signs, and empty —
            # all as FramingError, never a bare ValueError from int().
            if not (stripped.isascii() and stripped.isdecimal()):
                msg = f"non-numeric Content-Length: {stripped!r}"
                raise FramingError(msg)
            found = int(stripped)
    if found is None:
        msg = "message head has no Content-Length"
        raise FramingError(msg)
    return found
