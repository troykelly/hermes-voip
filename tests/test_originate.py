"""Unit tests for hermes_voip.originate — RED phase (TDD, ADR-0019).

Tests cover:
- build_outbound_invite: required headers, stable ids, re-auth reuse
- OutboundCallFailed: string format and attributes
- build_srtp_crypto_attrs: structure + uniqueness

These tests fail until originate.py is implemented.
"""

from __future__ import annotations

from hermes_voip.originate import (
    OutboundCallFailed,
    build_outbound_invite,
    build_srtp_crypto_attrs,
)

# ---------------------------------------------------------------------------
# OutboundCallFailed
# ---------------------------------------------------------------------------


def test_outbound_call_failed_str() -> None:
    exc = OutboundCallFailed(486, "Busy Here")
    assert str(exc) == "486 Busy Here"
    assert exc.status == 486
    assert exc.reason == "Busy Here"


def test_outbound_call_failed_503() -> None:
    exc = OutboundCallFailed(503, "no registered extension")
    assert exc.status == 503
    assert "503" in str(exc)


# ---------------------------------------------------------------------------
# build_outbound_invite — required headers
# ---------------------------------------------------------------------------


def test_build_outbound_invite_returns_tuple() -> None:
    text, call_id, from_tag = build_outbound_invite(
        target_uri="sip:1000@pbx.example.test",
        local_aor="sip:1000@pbx.example.test",
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
    )
    assert isinstance(text, str)
    assert isinstance(call_id, str)
    assert isinstance(from_tag, str)


def test_build_outbound_invite_has_required_headers() -> None:
    text, call_id, from_tag = build_outbound_invite(
        target_uri="sip:1000@pbx.example.test",
        local_aor="sip:1000@pbx.example.test",
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
    )
    assert "INVITE sip:1000@pbx.example.test SIP/2.0" in text
    assert "Via:" in text
    assert "Max-Forwards: 70" in text
    assert "From:" in text
    assert f"tag={from_tag}" in text
    assert "To: <sip:1000@pbx.example.test>" in text
    assert f"Call-ID: {call_id}" in text
    assert "CSeq: 1 INVITE" in text
    assert "Contact:" in text


def test_build_outbound_invite_via_has_branch() -> None:
    text, _, _ = build_outbound_invite(
        target_uri="sip:1000@pbx.example.test",
        local_aor="sip:1000@pbx.example.test",
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
    )
    assert "z9hG4bK" in text  # RFC 3261 magic cookie
    assert ";rport" in text


def test_build_outbound_invite_via_uses_transport() -> None:
    text, _, _ = build_outbound_invite(
        target_uri="sip:1000@pbx.example.test",
        local_aor="sip:1000@pbx.example.test",
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
    )
    assert "SIP/2.0/TLS" in text


def test_build_outbound_invite_no_content_type_without_body() -> None:
    text, _, _ = build_outbound_invite(
        target_uri="sip:1000@pbx.example.test",
        local_aor="sip:1000@pbx.example.test",
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
    )
    assert "Content-Type" not in text


def test_build_outbound_invite_content_type_with_body() -> None:
    text, _, _ = build_outbound_invite(
        target_uri="sip:1000@pbx.example.test",
        local_aor="sip:1000@pbx.example.test",
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
        body="v=0\r\n",
    )
    assert "Content-Type: application/sdp" in text
    assert "v=0" in text


# ---------------------------------------------------------------------------
# Stable call_id / from_tag when provided (re-auth path)
# ---------------------------------------------------------------------------


def test_build_outbound_invite_returns_stable_call_id_and_from_tag() -> None:
    """Fresh call: each invocation generates new call_id and from_tag."""
    _, cid1, ft1 = build_outbound_invite(
        target_uri="sip:1000@pbx.example.test",
        local_aor="sip:1000@pbx.example.test",
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
    )
    _, cid2, ft2 = build_outbound_invite(
        target_uri="sip:1000@pbx.example.test",
        local_aor="sip:1000@pbx.example.test",
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
    )
    # Two separate calls should produce different ids (probabilistically true)
    assert cid1 != cid2 or ft1 != ft2  # at least one differs


def test_build_outbound_invite_reuses_call_id_from_tag_on_reauth() -> None:
    """Re-auth: same call_id and from_tag, new branch, CSeq+1."""
    _, cid, ft = build_outbound_invite(
        target_uri="sip:1000@pbx.example.test",
        local_aor="sip:1000@pbx.example.test",
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
        call_id="fixed-call-id",
        from_tag="fixed-from-tag",
        cseq=1,
    )
    text2, cid2, ft2 = build_outbound_invite(
        target_uri="sip:1000@pbx.example.test",
        local_aor="sip:1000@pbx.example.test",
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
        call_id=cid,
        from_tag=ft,
        cseq=2,  # re-auth increments CSeq
    )
    assert cid2 == "fixed-call-id"
    assert ft2 == "fixed-from-tag"
    assert "Call-ID: fixed-call-id" in text2
    assert "tag=fixed-from-tag" in text2
    assert "CSeq: 2 INVITE" in text2


def test_build_outbound_invite_auth_header_included() -> None:
    text, _, _ = build_outbound_invite(
        target_uri="sip:1000@pbx.example.test",
        local_aor="sip:1000@pbx.example.test",
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
        auth=("Authorization", 'Digest username="1000", realm="pbx"'),
    )
    assert "Authorization: Digest" in text


# ---------------------------------------------------------------------------
# build_srtp_crypto_attrs
# ---------------------------------------------------------------------------


def test_build_srtp_crypto_attrs_returns_two() -> None:
    attrs = build_srtp_crypto_attrs()
    assert len(attrs) == 2


def test_build_srtp_crypto_attrs_tags() -> None:
    a1, a2 = build_srtp_crypto_attrs()
    assert a1.tag == 1
    assert a2.tag == 2


def test_build_srtp_crypto_attrs_suites() -> None:
    a1, a2 = build_srtp_crypto_attrs()
    assert a1.suite == "AES_CM_128_HMAC_SHA1_80"
    assert a2.suite == "AES_CM_128_HMAC_SHA1_32"


def test_build_srtp_crypto_attrs_keys_differ() -> None:
    """Each call generates fresh keys (probabilistic)."""
    a1, _a2 = build_srtp_crypto_attrs()
    b1, _b2 = build_srtp_crypto_attrs()
    assert a1.key_params != b1.key_params


def test_build_srtp_crypto_attrs_inline_prefix() -> None:
    a1, a2 = build_srtp_crypto_attrs()
    assert a1.key_params.startswith("inline:")
    assert a2.key_params.startswith("inline:")
