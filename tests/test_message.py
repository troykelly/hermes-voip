"""Tests for hermes_voip.message — SIP message assembly and response parsing.

Covers the generic request builder (RFC 3261 start-line + CRLF headers +
auto Content-Length), response parsing (status/reason/headers/body with
case-insensitive, repeatable header access), and the token generators
(branch with the RFC 3261 magic cookie, tags, Call-IDs). Fakes only.
"""

import re

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


def test_new_branch_has_rfc3261_magic_cookie_and_is_unique() -> None:
    a, b = new_branch(), new_branch()
    assert a.startswith("z9hG4bK")
    assert b.startswith("z9hG4bK")
    assert a != b


def test_new_tag_and_call_id_are_unique() -> None:
    assert new_tag() != new_tag()
    assert new_call_id() != new_call_id()
    assert re.fullmatch(r"[0-9a-f]+", new_tag())
