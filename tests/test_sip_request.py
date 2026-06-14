"""Tests for hermes_voip.message.SipRequest — parsing inbound SIP requests.

Inbound re-INVITE / REFER / NOTIFY / BYE are *requests* (ADR-0011); the registrar
and call layers must parse them. SipRequest mirrors SipResponse (same header
machinery: case-insensitive + repeatable access, RFC 3261 line unfolding). Fakes
only (pbx.example.test, ext 1000/2000).
"""

import pytest

from hermes_voip.message import SipRequest


def test_parse_request_line_and_body() -> None:
    raw = (
        "INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        "Via: SIP/2.0/TLS host.invalid;branch=z9hG4bKabc\r\n"
        "CSeq: 2 INVITE\r\n"
        "Content-Length: 3\r\n"
        "\r\n"
        "v=0"
    )
    req = SipRequest.parse(raw)
    assert req.method == "INVITE"
    assert req.request_uri == "sip:1000@pbx.example.test"
    assert req.body == "v=0"
    assert req.header("cseq") == "2 INVITE"  # case-insensitive


def test_refer_request_headers() -> None:
    raw = (
        "REFER sip:1000@pbx.example.test SIP/2.0\r\n"
        "Refer-To: <sip:2000@pbx.example.test>\r\n"
        "Referred-By: <sip:1000@pbx.example.test>\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )
    req = SipRequest.parse(raw)
    assert req.method == "REFER"
    assert req.header("Refer-To") == "<sip:2000@pbx.example.test>"
    assert req.header("Referred-By") == "<sip:1000@pbx.example.test>"


def test_repeatable_headers_and_missing() -> None:
    raw = (
        "BYE sip:1000@pbx.example.test SIP/2.0\r\n"
        "Via: SIP/2.0/TLS a.invalid;branch=z9hG4bK1\r\n"
        "Via: SIP/2.0/TLS b.invalid;branch=z9hG4bK2\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )
    req = SipRequest.parse(raw)
    assert req.header("contact") is None
    assert req.headers_all("via") == (
        "SIP/2.0/TLS a.invalid;branch=z9hG4bK1",
        "SIP/2.0/TLS b.invalid;branch=z9hG4bK2",
    )


def test_unfolds_continuation_lines() -> None:
    raw = (
        "REFER sip:1000@pbx.example.test SIP/2.0\r\n"
        "Refer-To: <sip:2000@pbx.example.test\r\n"
        " ?Replaces=abc>\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )
    req = SipRequest.parse(raw)
    assert req.header("Refer-To") == "<sip:2000@pbx.example.test ?Replaces=abc>"


def test_rejects_malformed_request_line() -> None:
    with pytest.raises(ValueError, match="request-line"):
        SipRequest.parse(
            "INVITE sip:1000@pbx.example.test\r\nContent-Length: 0\r\n\r\n"
        )
    with pytest.raises(ValueError, match="request-line"):
        SipRequest.parse("not a request line\r\n\r\n")
