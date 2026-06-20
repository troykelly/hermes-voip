"""Unit tests for the sans-IO UA-liveness responder (``hermes_voip.keepalive``).

These pin the pure response builders that keep the registration *qualified*: a
gateway sends out-of-dialog ``OPTIONS`` qualify pings to the registered contact
and routes inbound calls to voicemail unless it receives a ``200 OK`` (RFC 3261
§11). The builders here produce that ``200 OK`` (and the ``200 OK`` that ACKs an
unsolicited MWI ``NOTIFY``) as plain wire text — no socket, no transport.

Fakes only — ``pbx.example.test``, ext ``1000``, ``198.51.100.x`` / ``127.0.0.1``.
"""

from __future__ import annotations

from hermes_voip.keepalive import (
    ALLOW_METHODS,
    build_keepalive_ok,
    build_options_ok,
)
from hermes_voip.message import SipRequest


def _options(*, call_id: str = "opt-call-1", cseq: str = "1 OPTIONS") -> SipRequest:
    """A realistic gateway-style out-of-dialog OPTIONS qualify ping (no To-tag)."""
    raw = (
        "OPTIONS sip:1000@127.0.0.1:5061;transport=tls SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bKopt1;rport\r\n"
        "Max-Forwards: 70\r\n"
        "From: <sip:pbx@pbx.example.test>;tag=qualify-1\r\n"
        "To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq}\r\n"
        "Contact: <sip:pbx@198.51.100.7:5061;transport=tls>\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    return SipRequest.parse(raw)


def _notify(*, call_id: str = "mwi-call-1", cseq: str = "1 NOTIFY") -> SipRequest:
    """An unsolicited message-summary (MWI) NOTIFY, out of dialog (no To-tag)."""
    body = "Messages-Waiting: no\r\nVoice-Message: 0/0 (0/0)\r\n"
    raw = (
        "NOTIFY sip:1000@127.0.0.1:5061;transport=tls SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bKmwi1;rport\r\n"
        "Max-Forwards: 70\r\n"
        "From: <sip:1000@pbx.example.test>;tag=mwi-1\r\n"
        "To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq}\r\n"
        "Event: message-summary\r\n"
        "Subscription-State: active\r\n"
        "Content-Type: application/simple-message-summary\r\n"
        f"Content-Length: {len(body.encode())}\r\n\r\n"
        f"{body}"
    )
    return SipRequest.parse(raw)


# --- OPTIONS 200 OK ---------------------------------------------------------


def test_options_ok_is_a_200_response() -> None:
    response = build_options_ok(_options(), to_tag="ua-tag-1")
    assert response.startswith("SIP/2.0 200 OK\r\n")


def test_options_ok_allow_lists_invite_and_options() -> None:
    response = build_options_ok(_options(), to_tag="ua-tag-1")
    parsed = SipRequest.parse(_swap_status_for_request_line(response))
    allow = parsed.header("Allow")
    assert allow is not None
    methods = {m.strip() for m in allow.split(",")}
    # The qualify ping is satisfied by any 200; the Allow advertises the UA's
    # real capability set so the gateway knows it can ring + transfer us.
    assert "INVITE" in methods
    assert "OPTIONS" in methods
    assert {"ACK", "CANCEL", "BYE", "REFER", "NOTIFY"} <= methods


def test_options_ok_advertises_supported_timer() -> None:
    """The OPTIONS 200 OK advertises ``Supported: timer`` (RFC 4028 §8, ADR-0071).

    A peer/proxy querying our capabilities learns we engage RFC 4028 session timers.
    """
    response = build_options_ok(_options(), to_tag="ua-tag-1")
    parsed = SipRequest.parse(_swap_status_for_request_line(response))
    supported = ",".join(parsed.headers_all("Supported")).lower()
    assert "timer" in supported, (
        f"OPTIONS 200 missing Supported: timer; got {supported!r}"
    )


def test_options_ok_echoes_call_id_and_cseq() -> None:
    response = build_options_ok(
        _options(call_id="echo-me", cseq="7 OPTIONS"), to_tag="ua-tag-1"
    )
    parsed = SipRequest.parse(_swap_status_for_request_line(response))
    assert parsed.header("Call-ID") == "echo-me"
    assert parsed.header("CSeq") == "7 OPTIONS"


def test_options_ok_adds_a_to_tag() -> None:
    response = build_options_ok(_options(), to_tag="ua-tag-xyz")
    parsed = SipRequest.parse(_swap_status_for_request_line(response))
    to = parsed.header("To")
    assert to is not None
    assert "tag=ua-tag-xyz" in to


def test_options_ok_echoes_via_and_from() -> None:
    response = build_options_ok(_options(), to_tag="ua-tag-1")
    parsed = SipRequest.parse(_swap_status_for_request_line(response))
    via = parsed.header("Via")
    assert via is not None
    assert "branch=z9hG4bKopt1" in via
    from_value = parsed.header("From")
    assert from_value is not None
    assert "tag=qualify-1" in from_value


def test_options_ok_has_zero_content_length_and_no_body() -> None:
    response = build_options_ok(_options(), to_tag="ua-tag-1")
    assert response.endswith("\r\n\r\n")
    parsed = SipRequest.parse(_swap_status_for_request_line(response))
    assert parsed.header("Content-Length") == "0"
    assert parsed.body == ""


def test_allow_methods_constant_contains_the_core_set() -> None:
    assert {
        "INVITE",
        "ACK",
        "CANCEL",
        "BYE",
        "OPTIONS",
        "REFER",
        "NOTIFY",
    } <= set(ALLOW_METHODS)


# --- NOTIFY (and other ack-only) 200 OK -------------------------------------


def test_keepalive_ok_acks_an_mwi_notify_with_200() -> None:
    response = build_keepalive_ok(_notify(), to_tag="ua-tag-2")
    assert response.startswith("SIP/2.0 200 OK\r\n")


def test_keepalive_ok_echoes_notify_call_id_and_adds_to_tag() -> None:
    response = build_keepalive_ok(
        _notify(call_id="mwi-echo", cseq="3 NOTIFY"), to_tag="ua-tag-2"
    )
    parsed = SipRequest.parse(_swap_status_for_request_line(response))
    assert parsed.header("Call-ID") == "mwi-echo"
    assert parsed.header("CSeq") == "3 NOTIFY"
    to = parsed.header("To")
    assert to is not None
    assert "tag=ua-tag-2" in to


def test_keepalive_ok_does_not_echo_the_notify_body() -> None:
    # We acknowledge the MWI but do not process it; the 200 carries no body.
    response = build_keepalive_ok(_notify(), to_tag="ua-tag-2")
    parsed = SipRequest.parse(_swap_status_for_request_line(response))
    assert parsed.header("Content-Length") == "0"
    assert parsed.body == ""


def _swap_status_for_request_line(response: str) -> str:
    """Re-head a 200-OK response as a parseable request so ``SipRequest`` reads it.

    ``SipResponse`` exposes ``header()`` already, but parsing the response *as a
    request* lets these tests assert header values with one parser and also
    proves the response is well-formed wire text (request-line tokenises, headers
    fold). The status-line is replaced by a synthetic request-line; every header
    and the body are untouched.
    """
    _status_line, _, rest = response.partition("\r\n")
    return "OPTIONS sip:x SIP/2.0\r\n" + rest
