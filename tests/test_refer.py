"""Tests for the sans-IO transfer layer: REFER / Replaces / NOTIFY (ADR-0011 PR5).

Exercises both RFC 5589 roles: Transferor (builds blind/attended REFER, parses
NOTIFY progress) and Transferee/Target (parses REFER, builds the triggered
INVITE, matches an inbound ``Replaces`` against its dialogs). The load-bearing
correctness point is the RFC 3891 §3 tag orientation, validated end-to-end by a
build -> parse -> trigger -> match round trip.

Fakes only (``pbx.example.test``, ext ``1000``/``2000``/``3000``, ``198.51.100.x``).
"""

from __future__ import annotations

import pytest

from hermes_voip.dialog import Dialog
from hermes_voip.message import SipRequest
from hermes_voip.refer import (
    NotifyProgress,
    ReferError,
    ReferRequest,
    ReplacesSpec,
    TransferOutcomeClass,
    build_attended_refer,
    build_blind_refer,
    build_notify_sipfrag,
    build_triggered_invite,
    classify_transfer_progress,
    match_replaces,
    parse_notify_sipfrag,
    parse_refer,
)


def _dialog(*, call_id: str = "ab-call", local_cseq: int = 2) -> Dialog:
    # Primary dialog A<->B (we are A the transferor; B is the held caller).
    return Dialog(
        call_id=call_id,
        local_uri="sip:1000@pbx.example.test",
        local_tag="a-tag",
        remote_uri="sip:2000@pbx.example.test",
        remote_tag="b-tag",
        remote_target="sip:2000@198.51.100.99:5061;transport=tls",
        route_set=(),
        local_contact="<sip:1000@198.51.100.7:5061;transport=tls>",
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
        local_cseq=local_cseq,
        sdp_version=0,
    )


def _consult() -> Dialog:
    # Consultation dialog A<->C (we are A; C is the transfer target).
    return Dialog(
        call_id="ac-call",
        local_uri="sip:1000@pbx.example.test",
        local_tag="a-ctag",
        remote_uri="sip:3000@pbx.example.test",
        remote_tag="c-tag",
        remote_target="sip:3000@198.51.100.50:5061;transport=tls",
        route_set=(),
        local_contact="<sip:1000@198.51.100.7:5061;transport=tls>",
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
        local_cseq=5,
        sdp_version=0,
    )


# ---- Transferor: build REFER -----------------------------------------------


def test_build_blind_refer() -> None:
    result = build_blind_refer(_dialog(local_cseq=2), "sip:3000@pbx.example.test")
    req = SipRequest.parse(result.text)
    assert req.method == "REFER"
    assert req.request_uri == "sip:2000@198.51.100.99:5061;transport=tls"
    assert req.header("CSeq") == "3 REFER"
    assert req.header("Refer-To") == "<sip:3000@pbx.example.test>"
    assert req.header("Referred-By") is None
    assert result.dialog.local_cseq == 3


def test_build_blind_refer_with_referred_by() -> None:
    result = build_blind_refer(
        _dialog(),
        "sip:3000@pbx.example.test",
        referred_by="sip:1000@pbx.example.test",
    )
    req = SipRequest.parse(result.text)
    assert req.header("Referred-By") == "<sip:1000@pbx.example.test>"


def test_build_attended_refer_embeds_escaped_replaces() -> None:
    result = build_attended_refer(_dialog(local_cseq=2), _consult())
    req = SipRequest.parse(result.text)
    assert req.method == "REFER"
    assert req.header("CSeq") == "3 REFER"
    refer_to = req.header("Refer-To")
    assert refer_to is not None
    # The Replaces must be percent-escaped inside the Refer-To URI so its ';'
    # and '=' do not split the URI.
    assert "?Replaces=" in refer_to
    assert "%3Bto-tag%3D" in refer_to
    assert ";to-tag=" not in refer_to.split("?", 1)[1]


# ---- Transferee/Target: parse REFER ----------------------------------------


def test_parse_blind_refer() -> None:
    refer = SipRequest(
        method="REFER",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(("Refer-To", "<sip:3000@pbx.example.test>"),),
        body="",
    )
    parsed = parse_refer(refer)
    assert isinstance(parsed, ReferRequest)
    assert parsed.refer_to == "sip:3000@pbx.example.test"
    assert parsed.replaces is None
    assert parsed.referred_by is None


def test_parse_attended_refer_extracts_replaces() -> None:
    refer = SipRequest.parse(build_attended_refer(_dialog(), _consult()).text)
    parsed = parse_refer(refer)
    assert parsed.refer_to == "sip:3000@198.51.100.50:5061;transport=tls"
    assert parsed.replaces is not None
    assert parsed.replaces.call_id == "ac-call"
    assert parsed.replaces.to_tag == "c-tag"  # consult.remote_tag (target's tag)
    assert parsed.replaces.from_tag == "a-ctag"  # consult.local_tag (transferor's)


def test_parse_refer_with_referred_by() -> None:
    refer = SipRequest(
        method="REFER",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Refer-To", "<sip:3000@pbx.example.test>"),
            ("Referred-By", '"Agent" <sip:1000@pbx.example.test>'),
        ),
        body="",
    )
    parsed = parse_refer(refer)
    assert parsed.referred_by == "sip:1000@pbx.example.test"


def test_parse_refer_refer_to_survives_bracketed_display_name() -> None:
    # RFC 3261 §25.1 permits ``<`` and ``>`` inside a quoted display-name. A
    # Refer-To name-addr whose quoted display-name contains a full ``<...>`` span
    # must extract the real addr-spec, not the bracketed display text. Locating
    # the angle-addr on the FIRST ``<...>`` extracts ``<Team>`` — which the
    # injection guard then rejects as a non-dialable target.
    refer = SipRequest(
        method="REFER",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(("Refer-To", '"Support <Team>" <sip:3000@pbx.example.test>'),),
        body="",
    )
    parsed = parse_refer(refer)
    assert parsed.refer_to == "sip:3000@pbx.example.test"


def test_parse_refer_referred_by_survives_bracketed_display_name() -> None:
    # Referred-By flows through the same angle-addr locator. A bracketed
    # display-name must not desync the addr-spec extraction.
    refer = SipRequest(
        method="REFER",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Refer-To", "<sip:3000@pbx.example.test>"),
            ("Referred-By", '"Agent <A>" <sip:1000@pbx.example.test>'),
        ),
        body="",
    )
    parsed = parse_refer(refer)
    assert parsed.referred_by == "sip:1000@pbx.example.test"


def test_parse_refer_without_refer_to_raises() -> None:
    refer = SipRequest(
        method="REFER",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(("CSeq", "3 REFER"),),
        body="",
    )
    with pytest.raises(ReferError):
        parse_refer(refer)


# ---- Transferee: build the triggered INVITE --------------------------------


def test_build_triggered_invite_blind() -> None:
    text = build_triggered_invite(
        target_uri="sip:3000@pbx.example.test",
        local_aor="sip:2000@pbx.example.test",
        local_contact="<sip:2000@198.51.100.88:5061;transport=tls>",
        local_sent_by="198.51.100.88:5061",
        transport="TLS",
    )
    invite = SipRequest.parse(text)
    assert invite.method == "INVITE"
    assert invite.request_uri == "sip:3000@pbx.example.test"
    assert invite.header("Replaces") is None
    from_header = invite.header("From")
    assert from_header is not None
    assert ";tag=" in from_header


def test_build_triggered_invite_attended_with_replaces_and_body() -> None:
    replaces = ReplacesSpec(call_id="ac-call", to_tag="c-tag", from_tag="a-ctag")
    text = build_triggered_invite(
        target_uri="sip:3000@pbx.example.test",
        local_aor="sip:2000@pbx.example.test",
        local_contact="<sip:2000@198.51.100.88:5061;transport=tls>",
        local_sent_by="198.51.100.88:5061",
        transport="TLS",
        body="v=0\r\n",
        replaces=replaces,
        referred_by="sip:1000@pbx.example.test",
    )
    invite = SipRequest.parse(text)
    assert invite.header("Replaces") == "ac-call;to-tag=c-tag;from-tag=a-ctag"
    assert invite.header("Referred-By") == "<sip:1000@pbx.example.test>"
    assert invite.header("Content-Type") == "application/sdp"
    assert invite.body == "v=0\r\n"


# ---- Target: match an inbound Replaces -------------------------------------


def _target_view() -> Dialog:
    # As target C, the dialog with transferor A: local=C, remote=A.
    return Dialog(
        call_id="ac-call",
        local_uri="sip:3000@pbx.example.test",
        local_tag="c-tag",
        remote_uri="sip:1000@pbx.example.test",
        remote_tag="a-ctag",
        remote_target="sip:1000@198.51.100.7:5061;transport=tls",
        route_set=(),
        local_contact="<sip:3000@198.51.100.50:5061;transport=tls>",
        local_sent_by="198.51.100.50:5061",
        transport="TLS",
        local_cseq=1,
        sdp_version=0,
    )


def _invite_with_replaces(value: str) -> SipRequest:
    return SipRequest(
        method="INVITE",
        request_uri="sip:3000@198.51.100.50:5061",
        headers=(("Replaces", value),),
        body="",
    )


def test_match_replaces_finds_dialog() -> None:
    other = _dialog(call_id="unrelated")
    invite = _invite_with_replaces("ac-call;to-tag=c-tag;from-tag=a-ctag")
    matched = match_replaces(invite, [other, _target_view()])
    assert matched is not None
    assert matched.call_id == "ac-call"
    assert matched.local_tag == "c-tag"


def test_match_replaces_no_match_returns_none() -> None:
    invite = _invite_with_replaces("nope;to-tag=x;from-tag=y")
    assert match_replaces(invite, [_target_view()]) is None


def test_match_replaces_without_header_returns_none() -> None:
    invite = SipRequest(
        method="INVITE",
        request_uri="sip:3000@198.51.100.50:5061",
        headers=(),
        body="",
    )
    assert match_replaces(invite, [_target_view()]) is None


def test_attended_transfer_round_trip_matches_target() -> None:
    # A builds the attended REFER; B parses it and places the triggered INVITE;
    # C matches the Replaces against its own dialog with A. End-to-end orientation.
    refer = SipRequest.parse(build_attended_refer(_dialog(), _consult()).text)
    parsed = parse_refer(refer)
    assert parsed.replaces is not None
    triggered = build_triggered_invite(
        target_uri=parsed.refer_to,
        local_aor="sip:2000@pbx.example.test",
        local_contact="<sip:2000@198.51.100.88:5061;transport=tls>",
        local_sent_by="198.51.100.88:5061",
        transport="TLS",
        replaces=parsed.replaces,
    )
    invite = SipRequest.parse(triggered)
    matched = match_replaces(invite, [_target_view()])
    assert matched is not None
    assert matched.call_id == "ac-call"


# ---- NOTIFY message/sipfrag progress ---------------------------------------


def test_build_notify_sipfrag() -> None:
    result = build_notify_sipfrag(_dialog(local_cseq=3), "SIP/2.0 200 OK")
    req = SipRequest.parse(result.text)
    assert req.method == "NOTIFY"
    assert req.header("CSeq") == "4 NOTIFY"
    assert req.header("Event") == "refer"
    assert (req.header("Content-Type") or "").startswith("message/sipfrag")
    assert (req.header("Subscription-State") or "").startswith("active")
    assert "SIP/2.0 200 OK" in req.body


def test_parse_notify_sipfrag_in_progress() -> None:
    notify = SipRequest(
        method="NOTIFY",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Event", "refer"),
            ("Subscription-State", "active;expires=60"),
            ("Content-Type", "message/sipfrag;version=2.0"),
        ),
        body="SIP/2.0 100 Trying\r\n",
    )
    progress = parse_notify_sipfrag(notify)
    assert isinstance(progress, NotifyProgress)
    assert progress.status_code == 100
    assert progress.terminated is False


def test_parse_notify_sipfrag_terminated() -> None:
    notify = SipRequest(
        method="NOTIFY",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Subscription-State", "terminated;reason=noresource"),
            ("Content-Type", "message/sipfrag"),
        ),
        body="SIP/2.0 200 OK",
    )
    progress = parse_notify_sipfrag(notify)
    assert progress.status_code == 200
    assert progress.terminated is True


def test_parse_notify_sipfrag_without_status_line_raises() -> None:
    notify = SipRequest(
        method="NOTIFY",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(("Subscription-State", "active"),),
        body="not a status line",
    )
    with pytest.raises(ReferError):
        parse_notify_sipfrag(notify)


def test_parse_notify_sipfrag_rejects_non_ascii_status_digits() -> None:
    r"""A sipfrag status-line with non-ASCII digits is rejected, not folded (item 1670).

    The status code is matched ASCII-only (``[0-9]{3}``, not ``\d``), so an
    Arabic-Indic or fullwidth 3-digit code does not silently parse to an int().
    """
    # Arabic-Indic 200 (U+0662 U+0660 U+0660), built from code points so this source
    # stays ASCII; the [0-9]{3}-only status guard must reject it.
    arabic_200 = "".join(chr(0x0660 + int(d)) for d in "200")
    notify = SipRequest(
        method="NOTIFY",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Subscription-State", "active;expires=60"),
            ("Content-Type", "message/sipfrag;version=2.0"),
        ),
        body=f"SIP/2.0 {arabic_200} OK\r\n",
    )
    with pytest.raises(ReferError):
        parse_notify_sipfrag(notify)


def test_replaces_spec_header_value_round_trips() -> None:
    spec = ReplacesSpec(call_id="x@h", to_tag="t1", from_tag="f1", early_only=True)
    assert spec.header_value() == "x@h;to-tag=t1;from-tag=f1;early-only"


# --- ADR-0109: pure terminal-outcome classification of a transfer NOTIFY ------


def test_classify_transfer_progress_terminal_2xx_is_completed() -> None:
    """A terminated NOTIFY with a 2xx sipfrag status classifies as COMPLETED."""
    progress = NotifyProgress(status_code=200, reason="OK", terminated=True)
    assert classify_transfer_progress(progress) is TransferOutcomeClass.COMPLETED


def test_classify_transfer_progress_terminal_486_is_failed() -> None:
    """A terminated NOTIFY carrying a 4xx (busy) classifies as FAILED."""
    progress = NotifyProgress(status_code=486, reason="Busy Here", terminated=True)
    assert classify_transfer_progress(progress) is TransferOutcomeClass.FAILED


def test_classify_transfer_progress_terminal_603_is_failed() -> None:
    """A terminated NOTIFY carrying a 6xx (decline) classifies as FAILED."""
    progress = NotifyProgress(status_code=603, reason="Decline", terminated=True)
    assert classify_transfer_progress(progress) is TransferOutcomeClass.FAILED


def test_classify_transfer_progress_none_is_outcome_unknown() -> None:
    """No terminal NOTIFY (``None``) classifies as OUTCOME_UNKNOWN."""
    assert classify_transfer_progress(None) is TransferOutcomeClass.OUTCOME_UNKNOWN


def test_classify_transfer_progress_non_terminal_100_is_outcome_unknown() -> None:
    """A non-terminal 100 Trying that leaked through is OUTCOME_UNKNOWN, not COMPLETED.

    Only a ``terminated`` NOTIFY reaches a final verdict; a progress update with
    ``terminated is False`` (e.g. the interim 100) must never be read as a success.
    """
    progress = NotifyProgress(status_code=100, reason="Trying", terminated=False)
    assert classify_transfer_progress(progress) is TransferOutcomeClass.OUTCOME_UNKNOWN


# ---- review hardening: strict RFC 3515/3891/6665 parsing -------------------


def test_parse_refer_rejects_duplicate_refer_to() -> None:
    # RFC 3515: a REFER carries exactly one Refer-To.
    refer = SipRequest(
        method="REFER",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Refer-To", "<sip:3000@pbx.example.test>"),
            ("Refer-To", "<sip:4000@pbx.example.test>"),
        ),
        body="",
    )
    with pytest.raises(ReferError):
        parse_refer(refer)


def test_match_replaces_rejects_empty_tag() -> None:
    invite = _invite_with_replaces("ac-call;to-tag=;from-tag=a-ctag")
    with pytest.raises(ReferError):
        match_replaces(invite, [_target_view()])


def test_match_replaces_rejects_duplicate_tag() -> None:
    invite = _invite_with_replaces("ac-call;to-tag=c-tag;to-tag=x;from-tag=a-ctag")
    with pytest.raises(ReferError):
        match_replaces(invite, [_target_view()])


def test_parse_notify_sipfrag_rejects_missing_subscription_state() -> None:
    # RFC 6665 §8.2.1: a NOTIFY MUST carry Subscription-State.
    notify = SipRequest(
        method="NOTIFY",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(("Content-Type", "message/sipfrag"),),
        body="SIP/2.0 200 OK",
    )
    with pytest.raises(ReferError):
        parse_notify_sipfrag(notify)


def test_attended_refer_appends_replaces_when_target_has_uri_header() -> None:
    # This test checks build_attended_refer's ``&`` separator logic only: when the
    # consultation dialog's remote_target already carries a ``?``-headed URI (an
    # unusual but syntactically valid SIP Contact), the outbound REFER must join
    # the ``Replaces=`` with ``&``, never produce a malformed second ``?``.
    #
    # NOTE: parse_refer is NOT called here because a Refer-To carrying
    # ``?Subject=consult&Replaces=...`` would be rejected by the inbound query
    # guard (_validate_refer_to_query rejects any non-Replaces embedded header key).
    # The build-side test and the parse-side security boundary are intentionally
    # separate concerns: a transferee receiving such a REFER with an extra header
    # MUST reject it (see test_parse_refer_rejects_non_replaces_embedded_header).
    consult = _consult()
    with_header = Dialog(
        call_id=consult.call_id,
        local_uri=consult.local_uri,
        local_tag=consult.local_tag,
        remote_uri=consult.remote_uri,
        remote_tag=consult.remote_tag,
        remote_target="sip:3000@198.51.100.50:5061?Subject=consult",
        route_set=consult.route_set,
        local_contact=consult.local_contact,
        local_sent_by=consult.local_sent_by,
        transport=consult.transport,
        local_cseq=consult.local_cseq,
        sdp_version=consult.sdp_version,
    )
    refer = SipRequest.parse(build_attended_refer(_dialog(), with_header).text)
    refer_to = refer.header("Refer-To") or ""
    # A second URI header is joined with '&', never a malformed second '?'.
    assert refer_to.count("?") == 1
    assert "&Replaces=" in refer_to


def test_build_blind_refer_carries_auth_header() -> None:
    result = build_blind_refer(
        _dialog(),
        "sip:3000@pbx.example.test",
        auth=("Proxy-Authorization", "Digest username=1000"),
    )
    parsed = SipRequest.parse(result.text)
    assert parsed.header("Proxy-Authorization") == "Digest username=1000"


# ---- Refer-To injection guard (security) -----------------------------------
#
# ``transfer_blind`` passes an AGENT-SUPPLIED target straight into the
# ``Refer-To: <target>`` header. A target may LEGITIMATELY be a dialable
# user-part OR a well-formed ``sip:``/``sips:`` URI ``scheme:user@host[:port]``
# with only allowlisted ``;``-params (``transport``/``user``/``method``/``ttl``/
# ``maddr``/``lr``). Anything else must be REJECTED with ``ValueError`` so NO
# REFER is built:
#   * a host hijack on a bare extension (``1001@evil.com``);
#   * a ``?``-header form (``?Replaces=`` / any ``?Header=``) — no ``?`` at all;
#   * a non-allowlisted ``;``-param (``;Replaces=`` / ``;Route=``);
#   * a malformed authority (``host/path``, a non-numeric port, quote/comma);
#   * a ``>`` angle-bracket breakout or CR/LF header injection;
#   * any control char, whitespace, or angle bracket — literal OR percent-encoded
#     (``%0D%0A`` / ``%20`` / ``%3C`` / ``%3E``), since a gateway would decode it.


def test_build_blind_refer_accepts_bare_extension() -> None:
    # A plain dialable extension is a legitimate blind-transfer target.
    result = build_blind_refer(_dialog(), "3000")
    req = SipRequest.parse(result.text)
    assert req.header("Refer-To") == "<3000>"


def test_build_blind_refer_accepts_dialable_extension_with_plus_star_hash() -> None:
    result = build_blind_refer(_dialog(), "+1*2#3")
    req = SipRequest.parse(result.text)
    assert req.header("Refer-To") == "<+1*2#3>"


def test_build_blind_refer_accepts_valid_sip_uri() -> None:
    result = build_blind_refer(_dialog(), "sip:3000@pbx.example.test")
    req = SipRequest.parse(result.text)
    assert req.header("Refer-To") == "<sip:3000@pbx.example.test>"


def test_build_blind_refer_accepts_valid_sips_uri_with_params() -> None:
    # The tightened policy still accepts a sips: URI carrying ONLY allowlisted
    # ``;``-params (here ``transport``/``ttl``/``maddr``, names compared
    # case-insensitively) — ``transport=tls`` is the common legitimate case.
    target = "sips:3000@pbx.example.test;transport=tls;ttl=1;Maddr=198.51.100.1"
    result = build_blind_refer(_dialog(), target)
    req = SipRequest.parse(result.text)
    assert req.header("Refer-To") == f"<{target}>"


def test_build_blind_refer_accepts_sip_uri_with_port() -> None:
    # A numeric port on the authority is well-formed and accepted.
    result = build_blind_refer(_dialog(), "sip:3000@198.51.100.50:5061")
    req = SipRequest.parse(result.text)
    assert req.header("Refer-To") == "<sip:3000@198.51.100.50:5061>"


def test_build_blind_refer_accepts_sip_uri_with_ipv6_host() -> None:
    # A bracketed IPv6 literal authority is well-formed and accepted.
    result = build_blind_refer(_dialog(), "sip:3000@[2001:db8::1]:5061;transport=tls")
    req = SipRequest.parse(result.text)
    assert req.header("Refer-To") == "<sip:3000@[2001:db8::1]:5061;transport=tls>"


def test_build_blind_refer_rejects_bare_extension_host_hijack() -> None:
    # ``1001@evil.com`` on a bare extension would redirect the transfer to an
    # arbitrary host — reject it (it is neither a dialable user-part nor a
    # well-formed sip: URI).
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "1001@evil.com")


def test_build_blind_refer_rejects_bare_extension_with_replaces() -> None:
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "1001?Replaces=abc%3Bto-tag%3Dx")


def test_build_blind_refer_rejects_bare_extension_with_params() -> None:
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "1001;maddr=evil.com")


def _to_fullwidth_digits(ascii_digits: str) -> str:
    """Map ASCII digits to their U+FF10..U+FF19 fullwidth forms.

    Built via code points so this source file stays plain-ASCII (no RUF001
    ambiguous-character literals) while the RUNTIME string carries the real
    non-ASCII "digits" the transfer-target bypass depends on.
    """
    table = str.maketrans("0123456789", "".join(chr(0xFF10 + i) for i in range(10)))
    return ascii_digits.translate(table)


def test_build_blind_refer_rejects_fullwidth_digit_port() -> None:
    # A non-ASCII (fullwidth) digit in the port must NOT be Unicode-folded into a
    # "valid" port by the authority regex: RFC 3261 requires US-ASCII DIGIT
    # (%x30-39), and folding lets a multi-byte "digit" ride into the wire-emitted
    # Refer-To, bypassing the transfer-target injection allowlist. Same strict-ASCII
    # posture as #479's _SIPFRAG_STATUS `\d{3}` -> `[0-9]{3}` fix.
    target = "sip:3000@198.51.100.50:" + _to_fullwidth_digits("5061")
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), target)


def test_build_blind_refer_rejects_fullwidth_digit_ipv4_octet() -> None:
    # Fullwidth digits in an IPv4 authority octet must likewise be rejected, not
    # folded to ASCII by a Unicode-aware `\d` in _IPV4.
    target = "sip:3000@" + _to_fullwidth_digits("192") + ".168.1.1:5061"
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), target)


def test_build_blind_refer_rejects_crlf_header_injection() -> None:
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test>\r\nEvil-Header: x")


def test_build_blind_refer_rejects_angle_bracket_breakout() -> None:
    # A ``>`` inside the target would close the ``Refer-To: <...>`` early and let
    # the remainder smuggle out of the bracketed addr-spec.
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test>")


def test_build_blind_refer_rejects_bare_cr_in_uri() -> None:
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test\rmore")


def test_build_blind_refer_rejects_control_char_in_uri() -> None:
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test\x00null")


def test_build_blind_refer_rejects_percent_encoded_crlf() -> None:
    # %0D%0A decodes to CR/LF: a gateway that unescapes the URI would inject a
    # header. Reject the encoded form too (no percent-escaped controls).
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test%0D%0AEvil:%20x")


def test_build_blind_refer_rejects_whitespace_in_uri() -> None:
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test evil")


def test_build_blind_refer_rejects_empty_target() -> None:
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "")


def test_build_blind_refer_rejects_non_sip_scheme() -> None:
    # Only sip:/sips: URIs are valid transfer targets; a tel:/http: scheme (or a
    # bare ``user@host`` that looks URI-ish) is not a dialable extension and not a
    # sip URI — reject it.
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "http://evil.com/")


# -- must-fix #1: ``?``-header form / dangerous ``;``-param on a sip: URI ------


def test_build_blind_refer_rejects_sip_uri_with_replaces_header() -> None:
    # ``?Replaces=`` on a sip: URI would point the triggered INVITE at another
    # dialog (RFC 3891) — a dialog-seizing header-injection vector. No ``?`` form
    # is accepted at all.
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(
            _dialog(), "sips:3000@pbx.example.test?Replaces=abc%3Bto-tag%3Dx"
        )


def test_build_blind_refer_rejects_sip_uri_with_arbitrary_header() -> None:
    # Any embedded ``?Header=`` is rejected, not only ``?Replaces=``.
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test?Subject=evil")


def test_build_blind_refer_rejects_sip_uri_with_unknown_param() -> None:
    # A token-shaped but non-allowlisted ``;``-param (here a dangerous ``;Route=``
    # that would source-route the request) is rejected; only the safe allowlist
    # (transport/user/method/ttl/maddr/lr) passes.
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test;Route=<sip:evil>")


def test_build_blind_refer_rejects_sip_uri_with_replaces_param() -> None:
    # ``;Replaces=`` smuggled as a URI parameter is rejected (not allowlisted).
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test;Replaces=abc")


# -- must-fix #2: malformed authority (host[:port]) ---------------------------


def test_build_blind_refer_rejects_sip_uri_with_path_in_host() -> None:
    # ``host/path`` is not a well-formed authority — a ``/`` must not pass.
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test/path")


def test_build_blind_refer_rejects_sip_uri_with_non_numeric_port() -> None:
    # A non-numeric port (``:badport``) is a malformed authority — reject it.
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test:badport")


def test_build_blind_refer_rejects_sip_uri_with_comma_in_host() -> None:
    # A comma in the authority is malformed (and could smuggle a second URI in a
    # comma-separated header) — reject it.
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test,evil.test")


# -- must-fix #3: percent-encoded whitespace / angle brackets -----------------


def test_build_blind_refer_rejects_percent_encoded_space() -> None:
    # ``%20`` decodes to a space: a gateway unescaping the URI would split on it.
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test%20evil")


def test_build_blind_refer_rejects_percent_encoded_open_angle() -> None:
    # ``%3C`` decodes to ``<`` — an addr-spec breakout once unescaped.
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test%3Cevil")


def test_build_blind_refer_rejects_percent_encoded_close_angle() -> None:
    # ``%3E`` decodes to ``>`` — closes the ``Refer-To: <...>`` early.
    with pytest.raises(ValueError, match="transfer target"):
        build_blind_refer(_dialog(), "sip:3000@pbx.example.test%3Eevil")


# ---- parse_refer injection guard (security) --------------------------------
#
# An attacker-influenced inbound REFER carries a hostile Refer-To target that
# parse_refer must reject before building a ReferRequest.  The same guard
# (_validate_transfer_target) used by build_blind_refer runs here.


def test_parse_refer_rejects_foreign_host_in_refer_to() -> None:
    # ``1001@evil.com`` is a host-hijack on a bare extension: not a dialable
    # user-part and not a well-formed sip: URI, so parse_refer must raise ReferError.
    refer = SipRequest(
        method="REFER",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(("Refer-To", "<1001@evil.com>"),),
        body="",
    )
    with pytest.raises(ReferError):
        parse_refer(refer)


def test_parse_refer_target_rejection_message_does_not_echo_cause() -> None:
    # Defense-in-depth (rule 34): the ReferError raised when the pre-``?`` target
    # fails _validate_transfer_target carries a FIXED message that never
    # interpolates the underlying ValueError or the raw attacker-supplied target,
    # so a future validator message that echoed the raw value could not leak
    # through this boundary. Pins the no-echo contract at parse_refer. (An earlier
    # form interpolated ``: {exc}`` and would fail this exact-message assertion.)
    refer = SipRequest(
        method="REFER",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(("Refer-To", "<1001@evil.com>"),),
        body="",
    )
    with pytest.raises(ReferError) as excinfo:
        parse_refer(refer)
    assert str(excinfo.value) == "REFER Refer-To target rejected by injection guard"
    # The underlying ValueError cause is still preserved for debugging (rule 37).
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_parse_refer_rejects_refer_to_with_non_replaces_header_form() -> None:
    # A Refer-To carrying any embedded header key other than ``Replaces`` is a
    # header-injection vector; parse_refer must raise ReferError.
    # NOTE: ``?Replaces=<dialog-id>`` on a valid host IS accepted for attended
    # transfer (see test_parse_attended_refer_extracts_replaces); this test covers
    # the non-Replaces injection case with an otherwise-valid host and target.
    _uri = "sip:1001@pbx.example.test?Subject=evil"
    refer = SipRequest(
        method="REFER",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(("Refer-To", f"<{_uri}>"),),
        body="",
    )
    with pytest.raises(ReferError, match="embedded header"):
        parse_refer(refer)


def test_parse_refer_rejects_non_replaces_embedded_header() -> None:
    # A Refer-To URI carrying an embedded header other than ``Replaces=`` is a
    # header-injection vector (e.g. ``?Route=`` would source-route the triggered
    # INVITE; ``?Header=evil`` is arbitrary injection). Only ``?Replaces=`` is a
    # legitimate embedded header in an attended-transfer Refer-To; anything else
    # must raise ReferError even when the pre-``?`` target host is valid.
    #
    # This is the fail-open the security review found: ``_replaces_from_uri_query``
    # silently ignores non-Replaces pairs, so without an explicit query allowlist
    # check, ``?Header=evil`` passes right through and produces a ReferRequest
    # with ``replaces=None``.
    for injected_query in (
        "Header=evil",
        "Route=%3Csip%3Aevil%40attacker.example%3E",
        "Replaces=abc%40x%3Bto-tag%3D1%3Bfrom-tag%3D2&Route=%3Csip%3Aevil%3E",
    ):
        uri = f"sip:1001@pbx.example.test?{injected_query}"
        refer = SipRequest(
            method="REFER",
            request_uri="sip:1000@198.51.100.7:5061",
            headers=(("Refer-To", f"<{uri}>"),),
            body="",
        )
        with pytest.raises(ReferError, match="embedded header"):
            parse_refer(refer)


def test_parse_refer_accepts_valid_bare_extension() -> None:
    # A valid bare extension (digits only) is a legitimate blind-transfer target
    # and must parse without error, returning the extension as refer_to.
    refer = SipRequest(
        method="REFER",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(("Refer-To", "<1001>"),),
        body="",
    )
    parsed = parse_refer(refer)
    assert parsed.refer_to == "1001"
    assert parsed.replaces is None


# ---- _validate_transfer_target: control-character rejection ----------------
#
# NUL (\x00), CR (\r), and DEL (\x7f) are all ASCII C0/DEL control characters
# that must be rejected by _validate_transfer_target (and therefore by
# build_blind_refer which calls it). This test pins that behaviour so the
# _chars.contains_control migration in refer.py is verified non-tautologically:
# the test was shown to FAIL when the guard was skipped (see commit message).


def test_validate_transfer_target_rejects_nul_cr_del() -> None:
    # NUL, CR, and DEL are C0/DEL control characters that must be rejected
    # by _validate_transfer_target via contains_control from hermes_voip._chars.
    for bad_char in ("\x00", "\r", "\x7f"):
        with pytest.raises(
            ValueError, match="transfer target contains a control character"
        ):
            build_blind_refer(_dialog(), f"sip:1000@pbx.example.test{bad_char}")
