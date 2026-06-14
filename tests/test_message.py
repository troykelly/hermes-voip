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
    SipResponse,
    build_request,
    new_branch,
    new_call_id,
    new_tag,
)


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
