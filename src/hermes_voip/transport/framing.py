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


class FramingError(ValueError):
    """A message head is malformed: no Content-Length, or a non-numeric one.

    The stream cannot be re-synchronised after this — a missing or unparseable
    Content-Length means the body length is unknown, so the transport cannot find
    the next message boundary. The transport surfaces it (rule 37), never guesses.
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
            FramingError: if a complete head declares no usable Content-Length.
        """
        self._skip_keepalive_crlf()
        end = self._buffer.find(_HEAD_END)
        if end == -1:
            return None  # head not yet terminated
        head = bytes(self._buffer[:end]).decode("utf-8", errors="replace")
        body_start = end + len(_HEAD_END)
        content_length = _content_length(head)
        body_end = body_start + content_length
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
    # Skip the start-line; scan header lines for Content-Length / compact ``l``.
    for line in head.split(_CRLF.decode())[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() in _CONTENT_LENGTH_NAMES:
            stripped = value.strip()
            if not stripped.isdigit():
                msg = f"non-numeric Content-Length: {stripped!r}"
                raise FramingError(msg)
            return int(stripped)
    msg = "message head has no Content-Length"
    raise FramingError(msg)
