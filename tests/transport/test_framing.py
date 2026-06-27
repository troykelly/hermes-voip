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


def test_folded_content_length_header_is_unfolded_before_framing() -> None:
    framer = SipMessageFramer()
    body = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n"
    message = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKabc\r\n"
        "Call-ID: call-folded\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Type: application/sdp\r\n"
        f"Content-Length:\r\n\t{len(body.encode())}\r\n\r\n{body}"
    )
    framer.feed(message.encode())
    assert list(framer) == [message]


def test_leading_crlf_keepalive_ping_is_skipped_before_a_message() -> None:
    # RFC 5626 §3.5.1: a bare CRLF (ping) / CRLFCRLF (pong) precedes no message.
    framer = SipMessageFramer()
    framer.feed(b"\r\n" + b"\r\n\r\n" + _register("a"))
    messages = list(framer)
    assert len(messages) == 1
    assert "Call-ID: a" in messages[0]


def test_keepalive_only_feed_yields_no_message_and_does_not_fault() -> None:
    framer = SipMessageFramer()
    framer.feed(b"\r\n\r\n")  # a pong with no following message
    assert list(framer) == []
    # A subsequent real message still frames (the buffer is left consistent).
    framer.feed(_register("b"))
    messages = list(framer)
    assert len(messages) == 1
    assert "Call-ID: b" in messages[0]


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


def test_head_without_terminator_is_capped_to_bound_memory() -> None:
    framer = SipMessageFramer()
    framer.feed(b"A" * (64 * 1024 + 1))  # never a CRLFCRLF
    with pytest.raises(FramingError):
        list(framer)


def test_oversized_content_length_is_rejected_without_buffering_the_body() -> None:
    framer = SipMessageFramer()
    framer.feed(
        b"SIP/2.0 200 OK\r\n"
        b"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bK1\r\n"
        b"Content-Length: 300000\r\n\r\n"  # head only; body never sent
    )
    with pytest.raises(FramingError):
        list(framer)


def test_unicode_nondecimal_content_length_is_a_framing_error() -> None:
    framer = SipMessageFramer()
    framer.feed(
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bK1\r\n"
        "Content-Length: ²\r\n\r\n".encode()  # isdigit() but not int()-parseable
    )
    with pytest.raises(FramingError):
        list(framer)


# ---------------------------------------------------------------------------
# RFC 5626 keepalive interleaving (reconnect feature)
# ---------------------------------------------------------------------------


def test_leading_crlf_skipped() -> None:
    """A bare CRLF preceding a message is skipped; exactly one message yields."""
    framer = SipMessageFramer()
    framer.feed(b"\r\n" + _register("ping-skip"))
    messages = list(framer)
    assert len(messages) == 1
    assert "Call-ID: ping-skip" in messages[0]


def test_pong_between_messages_both_framed() -> None:
    """A double-CRLF pong interleaved between two messages must not drop either."""
    framer = SipMessageFramer()
    framer.feed(_register("first") + b"\r\n\r\n" + _register("second"))
    messages = list(framer)
    assert len(messages) == 2
    assert "Call-ID: first" in messages[0]
    assert "Call-ID: second" in messages[1]


# ---------------------------------------------------------------------------
# RFC 3261 §20.14: exactly one Content-Length per message (duplicate = error)
# ---------------------------------------------------------------------------


def test_duplicate_content_length_raises_framing_error() -> None:
    """Two Content-Length header lines (both full form) must raise FramingError.

    RFC 3261 §20.14 permits exactly one Content-Length per message.  A peer
    that sends two values is malformed; first-wins would let an attacker
    control the read boundary, so we fail-closed instead.
    """
    # First header has value 5 (the real body size); second has 999 (bogus).
    # The framer must detect the duplicate before reading any body bytes.
    framer = SipMessageFramer()
    framer.feed(
        b"SIP/2.0 200 OK\r\n"
        b"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKdup\r\n"
        b"Call-ID: dup-cl\r\n"
        b"Content-Length: 5\r\n"
        b"Content-Length: 999\r\n"
        b"\r\n"
        b"hello"
    )
    with pytest.raises(FramingError, match="duplicate Content-Length"):
        list(framer)


def test_duplicate_compact_and_full_content_length_raises_framing_error() -> None:
    """A compact 'l' header followed by a full Content-Length must raise FramingError.

    Both compact and full forms name the same header; a message carrying both is
    equally malformed and must not be silently accepted.
    """
    framer = SipMessageFramer()
    framer.feed(
        b"SIP/2.0 200 OK\r\n"
        b"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKdup2\r\n"
        b"Call-ID: dup-cl-compact\r\n"
        b"l: 5\r\n"
        b"Content-Length: 999\r\n"
        b"\r\n"
        b"hello"
    )
    with pytest.raises(FramingError, match="duplicate Content-Length"):
        list(framer)
