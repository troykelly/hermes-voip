"""Tests for the in-dialog state object and request builder (ADR-0011 PR3).

A :class:`~hermes_voip.dialog.Dialog` is sans-IO: it is constructed from parsed
messages and produces request wire text plus an updated (immutable) dialog. The
load-bearing property is **invariant 1** — the ``local_cseq`` and ``sdp_version``
counters move independently: an in-dialog request bumps CSeq only; an SDP offer
bumps the ``o=`` version only; a hold re-INVITE bumps both.

Fakes only (``pbx.example.test``, ext ``1000``/``2000``, ``198.51.100.x``).
"""

from __future__ import annotations

import pytest

from hermes_voip.dialog import (
    Dialog,
    DialogError,
    build_in_dialog_request,
)
from hermes_voip.message import SipRequest, SipResponse

_LOCAL_CONTACT = "<sip:1000@198.51.100.7:5061;transport=tls>"
_LOCAL_VIA = "SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bK-a;rport"
_PEER_CONTACT = "<sip:2000@198.51.100.99:5061;transport=tls>"


def _invite(
    *,
    call_id: str = "call-abc",
    from_tag: str = "ftag",
    cseq: int = 1,
    record_route: tuple[str, ...] = (),
) -> SipRequest:
    # An INVITE *we* sent: From is us (1000), To is the peer (2000).
    headers: list[tuple[str, str]] = [
        ("Via", _LOCAL_VIA),
        ("Max-Forwards", "70"),
        ("From", f"<sip:1000@pbx.example.test>;tag={from_tag}"),
        ("To", "<sip:2000@pbx.example.test>"),
        ("Call-ID", call_id),
        ("CSeq", f"{cseq} INVITE"),
        ("Contact", _LOCAL_CONTACT),
        ("User-Agent", "hermes-voip/test"),
    ]
    headers += [("Record-Route", rr) for rr in record_route]
    return SipRequest(
        method="INVITE",
        request_uri="sip:2000@pbx.example.test",
        headers=tuple(headers),
        body="",
    )


def _ok(
    *,
    call_id: str = "call-abc",
    to_tag: str = "ttag",
    contact: str = _PEER_CONTACT,
    record_route: tuple[str, ...] = (),
) -> SipResponse:
    headers: list[tuple[str, str]] = [
        ("Via", _LOCAL_VIA),
        ("From", "<sip:1000@pbx.example.test>;tag=ftag"),
        ("To", f"<sip:2000@pbx.example.test>;tag={to_tag}"),
        ("Call-ID", call_id),
        ("CSeq", "1 INVITE"),
        ("Contact", contact),
    ]
    headers += [("Record-Route", rr) for rr in record_route]
    return SipResponse(status_code=200, reason="OK", headers=tuple(headers), body="")


def _inbound_invite(
    *,
    call_id: str = "call-xyz",
    from_tag: str = "peertag",
    record_route: tuple[str, ...] = (),
) -> SipRequest:
    # An INVITE *we* received: From is the peer (3000), To is us (1000).
    headers: list[tuple[str, str]] = [
        ("Via", "SIP/2.0/TLS 198.51.100.50:5061;branch=z9hG4bK-peer;rport"),
        ("Max-Forwards", "70"),
        ("From", f"<sip:3000@pbx.example.test>;tag={from_tag}"),
        ("To", "<sip:1000@pbx.example.test>"),
        ("Call-ID", call_id),
        ("CSeq", "1 INVITE"),
        ("Contact", "<sip:3000@198.51.100.50:5061;transport=tls>"),
    ]
    headers += [("Record-Route", rr) for rr in record_route]
    return SipRequest(
        method="INVITE",
        request_uri="sip:1000@pbx.example.test",
        headers=tuple(headers),
        body="",
    )


# ---- UAC construction (from_invite_2xx) ------------------------------------


def test_from_invite_2xx_uac_identity() -> None:
    d = Dialog.from_invite_2xx(_invite(), _ok())
    assert d.call_id == "call-abc"
    assert d.local_uri == "sip:1000@pbx.example.test"
    assert d.local_tag == "ftag"
    assert d.remote_uri == "sip:2000@pbx.example.test"
    assert d.remote_tag == "ttag"
    assert d.remote_target == "sip:2000@198.51.100.99:5061;transport=tls"
    assert d.local_contact == _LOCAL_CONTACT
    assert d.local_sent_by == "198.51.100.7:5061"
    assert d.transport == "TLS"
    assert d.local_cseq == 1  # the INVITE CSeq; next request increments
    assert d.sdp_version == 0


def test_from_invite_2xx_route_set_reversed() -> None:
    rr = ("<sip:proxy1.example.test;lr>", "<sip:proxy2.example.test;lr>")
    d = Dialog.from_invite_2xx(_invite(), _ok(record_route=rr))
    # UAC reverses the Record-Route order (RFC 3261 §12.1.2).
    assert d.route_set == (
        "<sip:proxy2.example.test;lr>",
        "<sip:proxy1.example.test;lr>",
    )


def test_dialog_id_is_callid_localtag_remotetag() -> None:
    d = Dialog.from_invite_2xx(_invite(), _ok())
    assert d.dialog_id == ("call-abc", "ftag", "ttag")


# ---- UAS construction (from_inbound_invite) --------------------------------


def test_from_inbound_invite_uas_identity() -> None:
    d = Dialog.from_inbound_invite(
        _inbound_invite(),
        local_tag="ourtag",
        local_contact=_LOCAL_CONTACT,
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
    )
    assert d.call_id == "call-xyz"
    assert d.local_uri == "sip:1000@pbx.example.test"  # the To of the inbound INVITE
    assert d.local_tag == "ourtag"  # we generate this
    assert d.remote_uri == "sip:3000@pbx.example.test"
    assert d.remote_tag == "peertag"
    assert d.remote_target == "sip:3000@198.51.100.50:5061;transport=tls"
    assert d.local_cseq == 0  # UAS local sequence starts empty; first request -> 1
    assert d.sdp_version == 0


def test_from_inbound_invite_route_set_in_order() -> None:
    rr = ("<sip:proxy1.example.test;lr>", "<sip:proxy2.example.test;lr>")
    d = Dialog.from_inbound_invite(
        _inbound_invite(record_route=rr),
        local_tag="ourtag",
        local_contact=_LOCAL_CONTACT,
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
    )
    # UAS keeps the Record-Route order (RFC 3261 §12.1.1).
    assert d.route_set == rr


# ---- build_in_dialog_request -----------------------------------------------


def test_build_bye_bumps_cseq_only() -> None:
    d = Dialog.from_invite_2xx(_invite(), _ok())
    result = build_in_dialog_request(d, "BYE")
    req = SipRequest.parse(result.text)
    assert req.method == "BYE"
    assert req.request_uri == "sip:2000@198.51.100.99:5061;transport=tls"
    assert req.header("CSeq") == "2 BYE"  # 1 (INVITE) + 1
    assert req.header("Call-ID") == "call-abc"
    assert req.header("From") == "<sip:1000@pbx.example.test>;tag=ftag"
    assert req.header("To") == "<sip:2000@pbx.example.test>;tag=ttag"
    assert "TLS 198.51.100.7:5061" in (req.header("Via") or "")
    # invariant 1: CSeq advanced, SDP version untouched.
    assert result.dialog.local_cseq == 2
    assert result.dialog.sdp_version == 0


def test_build_emits_route_headers_for_loose_routing() -> None:
    rr = ("<sip:proxy1.example.test;lr>", "<sip:proxy2.example.test;lr>")
    d = Dialog.from_invite_2xx(_invite(), _ok(record_route=rr))
    req = SipRequest.parse(build_in_dialog_request(d, "BYE").text)
    assert req.headers_all("Route") == (
        "<sip:proxy2.example.test;lr>",
        "<sip:proxy1.example.test;lr>",
    )


def test_build_no_route_header_without_route_set() -> None:
    d = Dialog.from_invite_2xx(_invite(), _ok())
    req = SipRequest.parse(build_in_dialog_request(d, "BYE").text)
    assert req.headers_all("Route") == ()


def test_build_carries_extra_headers_and_body() -> None:
    d = Dialog.from_invite_2xx(_invite(), _ok())
    sdp = "v=0\r\no=- 1 0 IN IP4 198.51.100.7\r\n"
    result = build_in_dialog_request(
        d,
        "INVITE",
        extra_headers=(("Content-Type", "application/sdp"),),
        body=sdp,
    )
    req = SipRequest.parse(result.text)
    assert req.header("Content-Type") == "application/sdp"
    assert req.body == sdp


def test_consecutive_requests_increment_cseq_monotonically() -> None:
    d = Dialog.from_invite_2xx(_invite(), _ok())
    first = build_in_dialog_request(d, "INFO")
    second = build_in_dialog_request(first.dialog, "INFO")
    assert SipRequest.parse(first.text).header("CSeq") == "2 INFO"
    assert SipRequest.parse(second.text).header("CSeq") == "3 INFO"
    assert second.dialog.local_cseq == 3


# ---- invariant 1: independent counters -------------------------------------


def test_next_sdp_version_bumps_sdp_only() -> None:
    d = Dialog.from_invite_2xx(_invite(), _ok())
    bumped = d.with_next_sdp_version()
    assert bumped.sdp_version == 1
    assert bumped.local_cseq == d.local_cseq  # CSeq untouched


def test_hold_reinvite_sequence_bumps_both_independently() -> None:
    # The hold pattern (incall PR4): bump the SDP version for the new offer,
    # then build the re-INVITE which bumps CSeq. Both advance by exactly one.
    d = Dialog.from_invite_2xx(_invite(), _ok())
    offered = d.with_next_sdp_version()
    result = build_in_dialog_request(
        offered,
        "INVITE",
        extra_headers=(("Content-Type", "application/sdp"),),
        body="v=0\r\n",
    )
    assert result.dialog.local_cseq == d.local_cseq + 1
    assert result.dialog.sdp_version == d.sdp_version + 1


# ---- invariant 2 spot check: dialog state is independent of registration ----


def test_dialog_counters_derive_only_from_the_call() -> None:
    # The dialog's Call-ID/CSeq come from the INVITE, never a registration's.
    d = Dialog.from_invite_2xx(_invite(call_id="call-abc", cseq=42), _ok())
    assert d.call_id == "call-abc"
    assert d.local_cseq == 42
    assert build_in_dialog_request(d, "BYE").dialog.local_cseq == 43


# ---- rejections ------------------------------------------------------------


def test_from_invite_2xx_rejects_missing_from_tag() -> None:
    bad = SipRequest(
        method="INVITE",
        request_uri="sip:2000@pbx.example.test",
        headers=(
            ("Via", _LOCAL_VIA),
            ("From", "<sip:1000@pbx.example.test>"),  # no tag
            ("To", "<sip:2000@pbx.example.test>"),
            ("Call-ID", "call-abc"),
            ("CSeq", "1 INVITE"),
            ("Contact", _LOCAL_CONTACT),
        ),
        body="",
    )
    with pytest.raises(DialogError):
        Dialog.from_invite_2xx(bad, _ok())


def test_from_invite_2xx_rejects_missing_to_tag() -> None:
    no_to_tag = SipResponse(
        status_code=200,
        reason="OK",
        headers=(
            ("Via", _LOCAL_VIA),
            ("From", "<sip:1000@pbx.example.test>;tag=ftag"),
            ("To", "<sip:2000@pbx.example.test>"),  # no tag
            ("Call-ID", "call-abc"),
            ("CSeq", "1 INVITE"),
            ("Contact", _PEER_CONTACT),
        ),
        body="",
    )
    with pytest.raises(DialogError):
        Dialog.from_invite_2xx(_invite(), no_to_tag)


def test_from_invite_2xx_rejects_missing_contact() -> None:
    no_contact = SipResponse(
        status_code=200,
        reason="OK",
        headers=(
            ("Via", _LOCAL_VIA),
            ("From", "<sip:1000@pbx.example.test>;tag=ftag"),
            ("To", "<sip:2000@pbx.example.test>;tag=ttag"),
            ("Call-ID", "call-abc"),
            ("CSeq", "1 INVITE"),
        ),
        body="",
    )
    with pytest.raises(DialogError):
        Dialog.from_invite_2xx(_invite(), no_contact)


def test_from_inbound_invite_rejects_missing_from_tag() -> None:
    bad = SipRequest(
        method="INVITE",
        request_uri="sip:1000@pbx.example.test",
        headers=(
            ("Via", "SIP/2.0/TLS 198.51.100.50:5061;branch=z9hG4bK-peer"),
            ("From", "<sip:3000@pbx.example.test>"),  # no tag
            ("To", "<sip:1000@pbx.example.test>"),
            ("Call-ID", "call-xyz"),
            ("CSeq", "1 INVITE"),
            ("Contact", "<sip:3000@198.51.100.50:5061;transport=tls>"),
        ),
        body="",
    )
    with pytest.raises(DialogError):
        Dialog.from_inbound_invite(
            bad,
            local_tag="ourtag",
            local_contact=_LOCAL_CONTACT,
            local_sent_by="198.51.100.7:5061",
            transport="TLS",
        )
