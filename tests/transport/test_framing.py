"""Tests for the sans-IO SIP message framer (RFC 3261 §7.5 over a stream).

A SIP message over a stream transport (TLS/TCP) is *not* self-delimiting at the
read boundary: a single ``recv`` can deliver half a message, several messages, or
a message split across the head/body boundary. The framer turns the byte stream
into whole messages by reading the head up to the first CRLFCRLF, then exactly
``Content-Length`` body bytes. These tests pin split, coalesced, and pipelined
reads, plus the framing-error guards.

Fakes only — no real host/PII. Bodies are tiny fake SDP-shaped strings.
"""

from __future__ import annotations

import pytest

from hermes_voip.transport.framing import FramingError, SipMessageFramer

_CRLF = "\r\n"


def _register(call_id: str = "reg-1") -> bytes:
    return (
        "REGISTER sip:pbx.example.test SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKabc\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 REGISTER\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()


def _with_body() -> tuple[bytes, str]:
    body = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n"
    head = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKabc\r\n"
        "Call-ID: call-1\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Type: application/sdp\r\n"
        f"Content-Length: {len(body.encode())}\r\n\r\n"
    )
    return (head + body).encode(), head + body


def test_single_complete_message_in_one_feed() -> None:
    framer = SipMessageFramer()
    framer.feed(_register())
    messages = list(framer)
    assert len(messages) == 1
    assert messages[0].startswith("REGISTER sip:pbx.example.test SIP/2.0")
    assert messages[0].endswith(_CRLF + _CRLF)
    # The buffer is drained; a second drain yields nothing.
    assert list(framer) == []


def test_message_split_across_two_feeds_at_header_boundary() -> None:
    framer = SipMessageFramer()
    whole = _register()
    cut = 20  # mid-header-block, before the terminating CRLFCRLF
    framer.feed(whole[:cut])
    assert list(framer) == []  # head not yet complete
    framer.feed(whole[cut:])
    messages = list(framer)
    assert len(messages) == 1
    assert messages[0] == whole.decode()


def test_message_split_inside_the_body() -> None:
    framer = SipMessageFramer()
    raw, expected = _with_body()
    # Cut after the CRLFCRLF but before the full body has arrived.
    head_len = raw.index(b"\r\n\r\n") + 4
    cut = head_len + 5
    framer.feed(raw[:cut])
    assert list(framer) == []  # body short by some bytes
    framer.feed(raw[cut:])
    messages = list(framer)
    assert messages == [expected]


def test_coalesced_messages_in_one_feed() -> None:
    framer = SipMessageFramer()
    framer.feed(_register("a") + _register("b"))
    messages = list(framer)
    assert len(messages) == 2
    assert "Call-ID: a" in messages[0]
    assert "Call-ID: b" in messages[1]


def test_pipelined_message_with_body_then_another() -> None:
    framer = SipMessageFramer()
    raw, expected = _with_body()
    framer.feed(raw + _register("after"))
    messages = list(framer)
    assert len(messages) == 2
    assert messages[0] == expected
    assert "Call-ID: after" in messages[1]


def test_byte_at_a_time_delivery_reassembles() -> None:
    framer = SipMessageFramer()
    raw, expected = _with_body()
    for i in range(len(raw)):
        framer.feed(raw[i : i + 1])
    messages = list(framer)
    assert messages == [expected]


def test_body_consumes_exactly_content_length_not_more() -> None:
    # A trailing byte after the declared body length belongs to the NEXT message,
    # not this one (Content-Length is exact, not a minimum).
    framer = SipMessageFramer()
    raw, expected = _with_body()
    framer.feed(raw + b"X")  # one stray byte begins an (incomplete) next message
    messages = list(framer)
    assert messages == [expected]
    # The stray byte is retained for the next message, not appended to this body.
    framer.feed(b"")  # no new data; still incomplete
    assert list(framer) == []


def test_missing_content_length_is_a_framing_error() -> None:
    framer = SipMessageFramer()
    framer.feed(
        b"SIP/2.0 200 OK\r\nVia: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bK1\r\n\r\n"
    )
    with pytest.raises(FramingError):
        list(framer)


def test_non_numeric_content_length_is_a_framing_error() -> None:
    framer = SipMessageFramer()
    framer.feed(
        b"SIP/2.0 200 OK\r\n"
        b"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bK1\r\n"
        b"Content-Length: not-a-number\r\n\r\n"
    )
    with pytest.raises(FramingError):
        list(framer)
