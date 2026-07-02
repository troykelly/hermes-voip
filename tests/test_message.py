"""Tests for hermes_voip.message — SIP message assembly and response parsing.

Covers the generic request builder (RFC 3261 start-line + CRLF headers +
auto Content-Length), response parsing (status/reason/headers/body with
case-insensitive, repeatable header access), and the token generators
(branch with the RFC 3261 magic cookie, tags, Call-IDs). Fakes only.
"""

import re
from collections.abc import Callable
from typing import Final

import pytest

from hermes_voip.digest import DigestChallenge
from hermes_voip.message import (
    SipRequest,
    SipResponse,
    build_request,
    build_response,
    new_branch,
    new_call_id,
    new_tag,
)


def _inbound(method: str = "BYE", *, to_tag: str | None = "ourtag") -> SipRequest:
    to = "<sip:1000@pbx.example.test>"
    if to_tag is not None:
        to = f"{to};tag={to_tag}"
    return SipRequest(
        method=method,
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Via", "SIP/2.0/TLS 198.51.100.50:5061;branch=z9hG4bK-a"),
            ("Via", "SIP/2.0/TLS 198.51.100.60:5061;branch=z9hG4bK-b"),
            ("From", "<sip:2000@pbx.example.test>;tag=theirtag"),
            ("To", to),
            ("Call-ID", "call-xyz"),
            ("CSeq", f"5 {method}"),
        ),
        body="",
    )


def test_build_response_echoes_routing_headers() -> None:
    text = build_response(_inbound("BYE"), 200, "OK")
    resp = SipResponse.parse(text)
    assert resp.status_code == 200
    assert resp.reason == "OK"
    assert resp.header("Call-ID") == "call-xyz"
    assert resp.header("CSeq") == "5 BYE"
    assert resp.header("From") == "<sip:2000@pbx.example.test>;tag=theirtag"
    assert resp.header("To") == "<sip:1000@pbx.example.test>;tag=ourtag"


def test_build_response_echoes_the_full_via_stack_in_order() -> None:
    text = build_response(_inbound("BYE"), 200, "OK")
    resp = SipResponse.parse(text)
    assert resp.headers_all("Via") == (
        "SIP/2.0/TLS 198.51.100.50:5061;branch=z9hG4bK-a",
        "SIP/2.0/TLS 198.51.100.60:5061;branch=z9hG4bK-b",
    )


def test_build_response_adds_to_tag_when_absent() -> None:
    text = build_response(_inbound("INVITE", to_tag=None), 200, "OK", to_tag="newtag")
    resp = SipResponse.parse(text)
    assert resp.header("To") == "<sip:1000@pbx.example.test>;tag=newtag"


def test_build_response_does_not_duplicate_existing_to_tag() -> None:
    text = build_response(_inbound("INVITE", to_tag="already"), 200, "OK", to_tag="new")
    resp = SipResponse.parse(text)
    assert resp.header("To") == "<sip:1000@pbx.example.test>;tag=already"


def test_build_response_carries_extra_headers_and_body() -> None:
    sdp = "v=0\r\n"
    text = build_response(
        _inbound("INVITE", to_tag=None),
        200,
        "OK",
        to_tag="t",
        extra_headers=(
            ("Contact", "<sip:1000@198.51.100.7:5061;transport=tls>"),
            ("Content-Type", "application/sdp"),
        ),
        body=sdp,
    )
    resp = SipResponse.parse(text)
    assert resp.header("Contact") == "<sip:1000@198.51.100.7:5061;transport=tls>"
    assert resp.header("Content-Type") == "application/sdp"
    assert resp.body == sdp


def test_build_response_request_pending() -> None:
    resp = SipResponse.parse(build_response(_inbound("INVITE"), 491, "Request Pending"))
    assert resp.status_code == 491
    assert resp.reason == "Request Pending"


def test_build_response_rejects_invalid_status() -> None:
    with pytest.raises(ValueError, match="status"):
        build_response(_inbound("BYE"), 99, "Bad")


def test_build_response_rejects_caller_supplied_content_length() -> None:
    # Content-Length is computed from the body; a caller-supplied one would
    # duplicate the framing header and corrupt the message.
    with pytest.raises(ValueError, match="Content-Length"):
        build_response(
            _inbound("BYE"), 200, "OK", extra_headers=(("Content-Length", "5"),)
        )


def test_build_response_rejects_request_without_via() -> None:
    no_via = SipRequest(
        method="BYE",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("From", "<sip:2000@pbx.example.test>;tag=t"),
            ("To", "<sip:1000@pbx.example.test>;tag=o"),
            ("Call-ID", "c"),
            ("CSeq", "5 BYE"),
        ),
        body="",
    )
    with pytest.raises(ValueError, match="Via"):
        build_response(no_via, 200, "OK")


def test_build_request_assembles_start_line_headers_and_content_length() -> None:
    msg = build_request(
        "REGISTER",
        "sip:pbx.example.test",
        [
            ("Via", "SIP/2.0/TLS host.invalid;branch=z9hG4bKxyz"),
            ("From", "<sip:1000@pbx.example.test>;tag=abc"),
            ("CSeq", "1 REGISTER"),
        ],
        body="",
    )
    lines = msg.split("\r\n")
    assert lines[0] == "REGISTER sip:pbx.example.test SIP/2.0"
    assert "Via: SIP/2.0/TLS host.invalid;branch=z9hG4bKxyz" in lines
    assert "Content-Length: 0" in lines
    # blank line separates headers from the (empty) body; message ends with CRLFCRLF
    assert msg.endswith("\r\n\r\n")


def test_build_request_sets_content_length_from_body_bytes() -> None:
    body = "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n"
    msg = build_request(
        "INVITE",
        "sip:1000@pbx.example.test",
        [("Content-Type", "application/sdp")],
        body=body,
    )
    assert f"Content-Length: {len(body.encode('utf-8'))}" in msg
    assert msg.endswith("\r\n\r\n" + body)


def test_build_request_content_length_is_byte_length_not_char_count() -> None:
    # Body contains multibyte UTF-8 characters (3 bytes each in UTF-8) so that
    # len(body) (char-count) != len(body.encode('utf-8')) (byte-count).
    # This pins the byte-length behaviour and kills any len(body) char-count mutant.
    # U+00E9 (é) is 2 bytes; U+4E2D (中) is 3 bytes — both non-ASCII.
    body = "café 中文"  # "café 中文" — 7 chars, 12 UTF-8 bytes
    byte_len = len(body.encode("utf-8"))
    char_len = len(body)
    # pre-condition: char-count must differ from byte-count
    assert byte_len != char_len
    msg = build_request(
        "MESSAGE",
        "sip:1000@pbx.example.test",
        [("Content-Type", "text/plain;charset=utf-8")],
        body=body,
    )
    assert f"Content-Length: {byte_len}" in msg
    assert f"Content-Length: {char_len}" not in msg


def test_build_response_content_length_is_byte_length_not_char_count() -> None:
    # Same mutant-kill for build_response: Content-Length must be byte length.
    body = "café 中文"  # "café 中文" — 7 chars, 12 UTF-8 bytes
    byte_len = len(body.encode("utf-8"))
    char_len = len(body)
    # pre-condition: char-count must differ from byte-count
    assert byte_len != char_len
    resp = build_response(
        _inbound("INVITE"),
        200,
        "OK",
        extra_headers=[("Content-Type", "text/plain;charset=utf-8")],
        body=body,
    )
    assert f"Content-Length: {byte_len}" in resp
    assert f"Content-Length: {char_len}" not in resp


def test_parse_response_status_reason_and_body() -> None:
    raw = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS host.invalid;branch=z9hG4bKabc\r\n"
        "CSeq: 2 REGISTER\r\n"
        "Content-Length: 5\r\n"
        "\r\n"
        "hello"
    )
    resp = SipResponse.parse(raw)
    assert resp.status_code == 200
    assert resp.reason == "OK"
    assert resp.body == "hello"
    assert resp.header("cseq") == "2 REGISTER"  # case-insensitive lookup


def test_parse_response_reason_may_contain_spaces() -> None:
    resp = SipResponse.parse("SIP/2.0 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
    assert resp.status_code == 401
    assert resp.reason == "Unauthorized"


def test_header_missing_returns_none_and_repeatable_headers_collected() -> None:
    raw = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS a.invalid;branch=z9hG4bK1\r\n"
        "Via: SIP/2.0/TLS b.invalid;branch=z9hG4bK2\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )
    resp = SipResponse.parse(raw)
    assert resp.header("contact") is None
    assert resp.headers_all("via") == (
        "SIP/2.0/TLS a.invalid;branch=z9hG4bK1",
        "SIP/2.0/TLS b.invalid;branch=z9hG4bK2",
    )


def test_parse_401_challenge_feeds_digest_layer() -> None:
    raw = (
        "SIP/2.0 401 Unauthorized\r\n"
        'WWW-Authenticate: Digest realm="pbx.example.test", nonce="171/9c", '
        'algorithm=md5, qop="auth"\r\n'
        "Content-Length: 0\r\n"
        "\r\n"
    )
    resp = SipResponse.parse(raw)
    challenge = DigestChallenge.parse(resp.header("WWW-Authenticate") or "")
    assert challenge.realm == "pbx.example.test"
    assert challenge.qop == ("auth",)


def test_parse_unfolds_continuation_lines() -> None:
    # RFC 3261 allows a header value to continue on a line starting with SP/HTAB.
    raw = (
        "SIP/2.0 401 Unauthorized\r\n"
        'WWW-Authenticate: Digest realm="pbx.example.test",\r\n'
        '  nonce="171/9c", qop="auth"\r\n'
        "Content-Length: 0\r\n"
        "\r\n"
    )
    resp = SipResponse.parse(raw)
    value = resp.header("WWW-Authenticate") or ""
    assert "nonce=" in value
    challenge = DigestChallenge.parse(value)
    assert challenge.qop == ("auth",)


def test_parse_rejects_malformed_status_line() -> None:
    with pytest.raises(ValueError, match="status-line"):
        SipResponse.parse("SIP/2.0 200OK\r\nContent-Length: 0\r\n\r\n")


def test_parse_reason_less_status_line_is_rejected() -> None:
    # "SIP/2.0 200" with no SP-and-reason is malformed framing; the parser must
    # raise a ValueError per its contract (never leak an AttributeError, rule 37).
    with pytest.raises(ValueError, match="status-line"):
        SipResponse.parse("SIP/2.0 200\r\nContent-Length: 0\r\n\r\n")


def test_parse_empty_reason_status_line_yields_empty_reason() -> None:
    # The reason phrase MAY be empty, but the SP after the code is mandatory:
    # "SIP/2.0 200 " parses with reason == "".
    resp = SipResponse.parse("SIP/2.0 200 \r\nContent-Length: 0\r\n\r\n")
    assert resp.status_code == 200
    assert resp.reason == ""


def test_build_request_rejects_caller_supplied_content_length() -> None:
    # build_request owns Content-Length (computed from the body); a caller value
    # would produce two Content-Length headers -> ambiguous framing (RFC 7230).
    with pytest.raises(ValueError, match="Content-Length"):
        build_request(
            "REGISTER",
            "sip:pbx.example.test",
            [("Content-Length", "999")],
            body="",
        )


def test_build_request_rejects_caller_content_length_case_insensitively() -> None:
    # Header names are case-insensitive: a lower-case duplicate is rejected too.
    with pytest.raises(ValueError, match="Content-Length"):
        build_request(
            "INVITE",
            "sip:1000@pbx.example.test",
            [("content-length", "5")],
            body="hello",
        )


def test_build_request_rejects_crlf_injection_in_header_value() -> None:
    with pytest.raises(ValueError, match="control"):
        build_request(
            "REGISTER",
            "sip:pbx.example.test",
            [("Contact", "<sip:1000@host.invalid>\r\nEvil: injected")],
        )


def test_build_request_rejects_crlf_in_request_uri() -> None:
    with pytest.raises(ValueError, match="control"):
        build_request("REGISTER", "sip:pbx.example.test\r\nEvil: x", [])


def test_build_request_rejects_invalid_header_name() -> None:
    with pytest.raises(ValueError, match="header name"):
        build_request("REGISTER", "sip:pbx.example.test", [("Bad Name", "value")])


def test_new_branch_has_exact_rfc3261_magic_cookie_and_hex_length() -> None:
    assert re.fullmatch(r"z9hG4bK[0-9a-f]{16}", new_branch())


def test_new_branch_is_unique() -> None:
    assert new_branch() != new_branch()


def test_new_tag_has_exact_hex_length_and_is_unique() -> None:
    assert re.fullmatch(r"[0-9a-f]{12}", new_tag())
    assert new_tag() != new_tag()


def test_new_call_id_has_exact_hex_length_and_is_unique() -> None:
    assert re.fullmatch(r"[0-9a-f]{24}", new_call_id())
    assert new_call_id() != new_call_id()


# RFC 3261 token validation for the request-line method and request-URI
# (Wave-3 robustness audit finding: build_request previously accepted an empty
# method and empty/space URI, producing a malformed start-line).


def test_build_request_rejects_empty_method() -> None:
    """An empty method produces a malformed SIP start-line; must raise ValueError."""
    with pytest.raises(ValueError, match="method"):
        build_request("", "sip:1000@pbx.example.test", [])


def test_build_request_rejects_method_with_embedded_space() -> None:
    """A method containing a space is not an RFC 3261 token; must raise ValueError."""
    with pytest.raises(ValueError, match="method"):
        build_request("INVI TE", "sip:1000@pbx.example.test", [])


def test_build_request_rejects_empty_request_uri() -> None:
    """An empty request URI produces a malformed SIP start-line; ValueError expected."""
    with pytest.raises(ValueError, match=r"request.?uri|URI"):
        build_request("REGISTER", "", [])


def test_build_request_rejects_request_uri_with_whitespace() -> None:
    """A request URI with whitespace breaks start-line framing (ValueError expected)."""
    with pytest.raises(ValueError, match=r"request.?uri|URI"):
        build_request("REGISTER", "sip:1000@pbx.example.test transport=tls", [])


# Malformed header-block line rejection (Wave-3 robustness audit finding:
# a non-empty, non-continuation header-block line without a colon was silently
# dropped instead of being rejected, allowing garbage to vanish undetected).


def test_parse_response_rejects_colon_less_header_line() -> None:
    """A non-empty, non-continuation header line without a colon is malformed.

    Previously _parse_headers() silently dropped such lines (``if sep:`` with
    no else branch). The parser must instead raise ValueError to prevent garbage
    data from being swallowed undetected.
    """
    raw = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS a.invalid;branch=z9hG4bK1\r\n"
        "GARBAGELINE\r\n"  # no colon — must raise, not silently drop
        "Content-Length: 0\r\n"
        "\r\n"
    )
    with pytest.raises(ValueError, match=r"malformed header"):
        SipResponse.parse(raw)


def test_parse_request_rejects_colon_less_header_line() -> None:
    """SipRequest.parse() delegates to _parse_headers() and must also reject garbage."""
    raw = (
        "REGISTER sip:pbx.example.test SIP/2.0\r\n"
        "Via: SIP/2.0/TLS host.invalid;branch=z9hG4bKxyz\r\n"
        "NOTAHEADER\r\n"  # no colon — must raise, not silently drop
        "Content-Length: 0\r\n"
        "\r\n"
    )
    with pytest.raises(ValueError, match=r"malformed header"):
        SipRequest.parse(raw)


def test_parse_response_accepts_valid_folded_continuation_line() -> None:
    """A SP/HTAB-led continuation line is valid RFC 3261 folding — must not raise."""
    raw = (
        "SIP/2.0 200 OK\r\n"
        "Contact: <sip:1000@198.51.100.7:5061;\r\n"
        "  transport=tls>\r\n"  # valid continuation (starts with SP)
        "Content-Length: 0\r\n"
        "\r\n"
    )
    resp = SipResponse.parse(raw)
    assert "transport=tls" in (resp.header("Contact") or "")


def test_parse_unfolds_tab_continuation() -> None:
    r"""RFC 3261 §7.3.1 allows header continuation with HTAB (\t), not just SP.

    The _parse_headers() unfolding logic checks ``line[:1] in (" ", "\t")`` to
    detect continuation lines. This test pins the HTAB case explicitly, separate
    from the existing SP-only test_parse_unfolds_continuation_lines().

    The normalised value must be the two logical-line fragments joined with a
    single SP (the HTAB is stripped and replaced with one space), producing the
    exact single-line string asserted below.
    """
    raw = (
        "SIP/2.0 200 OK\r\n"
        'WWW-Authenticate: Digest realm="pbx.example.test",\r\n'
        '\tnonce="171/9c", qop="auth"\r\n'  # continuation starts with HTAB, not SP
        "Content-Length: 0\r\n"
        "\r\n"
    )
    resp = SipResponse.parse(raw)
    value = resp.header("WWW-Authenticate") or ""
    # Direct assertion: the reconstructed header value must be the exact
    # single-line string produced by RFC 3261 §7.3.1 unfolding (HTAB stripped,
    # continuation joined with one SP).
    assert value == 'Digest realm="pbx.example.test", nonce="171/9c", qop="auth"'


def test_parse_body_with_embedded_blank_line() -> None:
    r"""A body containing an embedded blank line (\r\n\r\n) is preserved intact.

    parse() uses partition(CRLFCRLF) to split headers from body, so the body
    retains any embedded CRLFCRLF sequences. This test pins that behaviour
    against a potential regression to split()-style parsing.
    """
    body_with_blank = "line1\r\n\r\nline2\r\nline3"
    raw = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS host.invalid;branch=z9hG4bKxyz\r\n"
        f"Content-Length: {len(body_with_blank)}\r\n"
        "\r\n" + body_with_blank
    )
    resp = SipResponse.parse(raw)
    assert resp.body == body_with_blank
    assert resp.body == "line1\r\n\r\nline2\r\nline3"


# ---------------------------------------------------------------------------
# Wave-13 ingress hardening: ASCII-only status digits (LOW-1), status-code
# range check on parse (LOW-2), redacted length-only parse-error messages
# (LOW-4), and the ADR-0081 exception-escape invariant locked against
# regression (G3), plus the previously-uncovered orphan-continuation (G1) and
# malformed-request-line (G2) rejections.
# ---------------------------------------------------------------------------


# The ADR-0081 invariant the transport read loop rests on: SipRequest.parse and
# SipResponse.parse are TOTAL over str — every input either parses or raises
# ValueError, never any other exception type. A KeyError / IndexError /
# AttributeError / TypeError / StopIteration / struct.error escaping parse would
# propagate past the read loop's ``except ValueError``, end the reader task, and
# tear down the ENTIRE signalling connection (registration + every concurrent
# call) over one hostile inbound message. hypothesis is not a dev dependency, so
# this is a hand-built table of adversarial inputs, not a property test.
_ADVERSARIAL_PARSE_INPUTS: Final[tuple[str, ...]] = (
    "",  # empty string
    "\r\n",  # a lone CRLF
    "\r\n\r\n",  # only the head/body separator
    "\n",  # bare LF, no CRLF
    "\r",  # bare CR
    " ",  # a lone space (continuation-shaped)
    "\t",  # a lone HTAB
    "   \r\n",  # whitespace-only first line
    "\tHeader: v\r\n\r\n",  # continuation line, no preceding header (HTAB)
    " leading-space\r\n\r\n",  # continuation-first header (SP)
    "SIP/2.0",  # truncated status-line, no code
    "SIP/2.0 ",  # code position empty
    "SIP/2.0 2\r\n\r\n",  # one-digit code
    "SIP/2.0 20\r\n\r\n",  # two-digit code
    "SIP/2.0 2000 OK\r\n\r\n",  # four-digit code
    "SIP/2.0 -20 OK\r\n\r\n",  # negative-looking code
    "SIP/2.0 xyz OK\r\n\r\n",  # non-numeric code
    "SIP/2.0 \u0662\u0660\u0660 OK\r\n\r\n",  # Arabic-Indic digits (fold to 200)
    "SIP/2.0 \uff12\uff10\uff10 OK\r\n\r\n",  # fullwidth digits (fold to 200)
    "SIP/2.0 000 X\r\n\r\n",  # below-range code
    "SIP/2.0 999 X\r\n\r\n",  # above-range code
    "SIP/2.0 200 OK",  # no CRLF anywhere
    "SIP/2.0 200 OK\r\nno-colon-here\r\n\r\n",  # colon-less header line
    "SIP/2.0 200 OK\r\n\x00\x01\x02\r\n\r\n",  # control chars where a header goes
    "SIP/2.0 200 OK\r\n: emptyname\r\n\r\n",  # empty header name
    "SIP/2.0 200 OK\r\n\t\r\n\r\n",  # lone-HTAB continuation, no preceding header
    "INVITE",  # truncated request-line
    "INVITE sip:x",  # request-line missing SIP-version
    "INVITE  SIP/2.0\r\n\r\n",  # empty request-URI (double space)
    "INVITE sip:x SIP/2.0\r\n leading\r\n\r\n",  # continuation-first header (request)
    "\x00\x00\x00",  # raw NULs, no structure
    "SIP/2.0 200 OK\r\nX: " + "a" * 100_000 + "\r\n\r\n",  # very long header value
    "SIP/2.0 200 " + "R" * 100_000 + "\r\n\r\n",  # very long reason phrase
    "SIP/2.0\r200 OK\r\n\r\n",  # bare CR inside the status-line
    "a\r\n" * 1000,  # very many short lines
)


def _parse_input_id(raw: str) -> str:
    """A length-based parametrize id so hostile/huge inputs never render in output."""
    return f"{len(raw)}chars"


def _assert_parse_raises_only_value_error(
    parse: Callable[[str], object], raw: str
) -> None:
    """Assert ``parse(raw)`` either returns or raises ``ValueError`` — no other type.

    A non-``ValueError`` escape is the exact ADR-0081 regression this guards: it
    would propagate past the transport read loop's ``except ValueError`` and DoS
    the whole connection. The failure message is length-only (never the raw
    input) to keep the same redaction discipline this lane enforces on the parser.
    """
    try:
        parse(raw)
    except ValueError:
        return  # the ONLY permitted failure mode
    except Exception as exc:  # noqa: BLE001 — asserting NO non-ValueError type escapes parse
        pytest.fail(
            f"parse raised {type(exc).__name__}, not ValueError, on a "
            f"{len(raw)}-char input — ADR-0081 exception-escape regression"
        )


@pytest.mark.parametrize("raw", _ADVERSARIAL_PARSE_INPUTS, ids=_parse_input_id)
def test_response_parse_raises_only_value_error_on_hostile_input(raw: str) -> None:
    """SipResponse.parse parses or raises ValueError on any str (ADR-0081)."""
    _assert_parse_raises_only_value_error(SipResponse.parse, raw)


@pytest.mark.parametrize("raw", _ADVERSARIAL_PARSE_INPUTS, ids=_parse_input_id)
def test_request_parse_raises_only_value_error_on_hostile_input(raw: str) -> None:
    """SipRequest.parse parses or raises ValueError on any str (ADR-0081)."""
    _assert_parse_raises_only_value_error(SipRequest.parse, raw)


def test_parse_response_rejects_continuation_first_header_line() -> None:
    """A first header line starting with SP has no preceding header to continue.

    Exercises the ``if not unfolded: raise`` branch (uncovered before this test):
    _parse_headers must raise ValueError, never an IndexError from ``unfolded[-1]``
    on an empty list.
    """
    raw = (
        "SIP/2.0 200 OK\r\n"
        " orphan-continuation\r\n"  # SP-led first header line — nothing precedes it
        "Content-Length: 0\r\n"
        "\r\n"
    )
    with pytest.raises(ValueError, match="continuation"):
        SipResponse.parse(raw)


def test_parse_request_rejects_htab_continuation_first_header_line() -> None:
    """Orphan-continuation guard holds via SipRequest.parse with a HTAB lead."""
    raw = (
        "REGISTER sip:pbx.example.test SIP/2.0\r\n"
        "\torphan-continuation\r\n"  # HTAB-led first header line — nothing precedes it
        "Content-Length: 0\r\n"
        "\r\n"
    )
    with pytest.raises(ValueError, match="continuation"):
        SipRequest.parse(raw)


def test_parse_request_rejects_malformed_request_line() -> None:
    """A first line that is not a valid request-line must raise ValueError.

    Only the response status-line had a malformed-first-line test; this pins the
    symmetric SipRequest guard (never an AttributeError from a None regex match).
    """
    with pytest.raises(ValueError, match="request-line"):
        SipRequest.parse("NOT A REQUEST LINE\r\nContent-Length: 0\r\n\r\n")


def test_parse_rejects_non_ascii_digit_status_code() -> None:
    r"""Non-ASCII decimal digits in the status code are rejected (RFC 3261 ABNF).

    RFC 3261 requires three ASCII DIGITs (%x30-39). Arabic-Indic (U+0660-0669)
    and fullwidth (U+FF10-FF19) digits are Unicode Nd that ``\d`` matched and
    ``int()`` folds to 200 — a parser differential (two distinct wire strings
    decode to the same status). The status-line regex uses ``[0-9]`` so both are
    rejected as a malformed status-line rather than silently normalised.
    """
    # Arabic-Indic and fullwidth "200" (Unicode Nd that int() folds to ASCII 200).
    for status in ("\u0662\u0660\u0660", "\uff12\uff10\uff10"):
        raw = f"SIP/2.0 {status} OK\r\nContent-Length: 0\r\n\r\n"
        with pytest.raises(ValueError, match="status-line"):
            SipResponse.parse(raw)


def test_parse_rejects_out_of_range_status_code() -> None:
    """A well-formed but out-of-ABNF-range status code is rejected on parse.

    build_response enforces 100..699; parse must be symmetric so a hostile
    000/099/700/999 status-line cannot yield a SipResponse the builder would
    never emit.
    """
    for code in ("000", "099", "700", "999"):
        raw = f"SIP/2.0 {code} X\r\nContent-Length: 0\r\n\r\n"
        with pytest.raises(ValueError, match="range"):
            SipResponse.parse(raw)


def test_parse_accepts_status_codes_at_the_inclusive_range_boundaries() -> None:
    """100 and 699 are the inclusive RFC 3261 boundaries — both still parse.

    Pins the boundaries against an off-by-one regression in the range check (a
    ``< 100``/``> 699`` mutant would wrongly reject exactly 100 or 699).
    """
    for code in (100, 699):
        raw = f"SIP/2.0 {code} X\r\nContent-Length: 0\r\n\r\n"
        assert SipResponse.parse(raw).status_code == code


def test_parse_error_does_not_embed_raw_status_line() -> None:
    """A malformed status-line's ValueError is length-only, not the raw wire text.

    The sole catcher today (_dispatch) logs type+len, never str(exc), but a
    request start-line carries the callee URI and a stray header line could be a
    From/To — parse errors stay length-only to match the egress-log redaction
    discipline (rule 34) and to be safe if any future caller logs str(exc).
    """
    with pytest.raises(ValueError, match="status-line") as excinfo:
        SipResponse.parse(
            "SIP/2.0 zz sip:secret-callee@internal.example\r\nContent-Length: 0\r\n\r\n"
        )
    message = str(excinfo.value)
    assert "secret-callee" not in message
    assert "internal.example" not in message


def test_parse_error_does_not_embed_raw_request_line() -> None:
    """A malformed request-line carries the callee URI; its error stays length-only."""
    with pytest.raises(ValueError, match="request-line") as excinfo:
        SipRequest.parse("BOGUS sip:secret-callee@internal.example\r\n\r\n")
    message = str(excinfo.value)
    assert "secret-callee" not in message
    assert "internal.example" not in message


def test_parse_error_does_not_embed_raw_header_line() -> None:
    """A colon-less header line could carry routing/PII; its error stays length-only."""
    raw = (
        "SIP/2.0 200 OK\r\n"
        "secret-caller-token-no-colon\r\n"  # no colon -> malformed header line
        "Content-Length: 0\r\n"
        "\r\n"
    )
    with pytest.raises(ValueError, match="malformed header") as excinfo:
        SipResponse.parse(raw)
    assert "secret-caller-token-no-colon" not in str(excinfo.value)
