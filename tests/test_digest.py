"""Tests for hermes_voip.digest — RFC 2617 HTTP/SIP digest authentication.

The load-bearing case is the canonical worked example from RFC 2617 section 3.5,
used as a known-answer vector: with the published inputs the computed ``response``
must equal the published digest ``6629fae49393a05397450978507c4ef1``. The rest
cover SIP-shaped usage and the RFC 2069 (no-``qop``) fallback. Fakes only — no real
gateway host or credentials (the repo is public; AGENTS.md invariant).
"""

import re

import pytest
from hermes_voip.digest import DigestChallenge, DigestCredentials, build_authorization


def _param(header: str, name: str) -> str | None:
    """Return the value of one auth-param from an Authorization header, or None."""
    m = re.search(rf'\b{name}=(?:"([^"]*)"|([^,\s]+))', header)
    if m is None:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def test_rfc2617_section_3_5_known_answer() -> None:
    challenge = DigestChallenge.parse(
        'Digest realm="testrealm@host.com", qop="auth,auth-int", '
        'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
        'opaque="5ccc069c403ebaf9f0171e9517f40e41"'
    )
    header = build_authorization(
        challenge,
        DigestCredentials(username="Mufasa", password="Circle Of Life"),
        method="GET",
        uri="/dir/index.html",
        cnonce="0a4f113b",
        nc=1,
    )
    assert _param(header, "response") == "6629fae49393a05397450978507c4ef1"
    assert _param(header, "qop") == "auth"
    assert _param(header, "nc") == "00000001"
    assert _param(header, "cnonce") == "0a4f113b"
    assert _param(header, "username") == "Mufasa"
    assert _param(header, "realm") == "testrealm@host.com"
    assert _param(header, "uri") == "/dir/index.html"
    assert _param(header, "opaque") == "5ccc069c403ebaf9f0171e9517f40e41"


def test_parse_extracts_all_fields() -> None:
    challenge = DigestChallenge.parse(
        'Digest realm="voip.example.test", nonce="abc/def", algorithm=md5, '
        'qop="auth", opaque="deadbeef"'
    )
    assert challenge.realm == "voip.example.test"
    assert challenge.nonce == "abc/def"
    assert challenge.algorithm == "md5"
    assert challenge.qop == ("auth",)
    assert challenge.opaque == "deadbeef"


def test_parse_tolerates_leading_scheme_and_unquoted_tokens() -> None:
    challenge = DigestChallenge.parse(
        "Digest realm=r, nonce=n, algorithm=MD5, qop=auth"
    )
    assert challenge.realm == "r"
    assert challenge.nonce == "n"
    assert challenge.qop == ("auth",)


def test_parse_rejects_challenge_without_nonce() -> None:
    with pytest.raises(ValueError, match="nonce"):
        DigestChallenge.parse('Digest realm="r"')


def test_sip_register_shape_echoes_algorithm_and_opaque() -> None:
    challenge = DigestChallenge.parse(
        'Digest realm="voip002", nonce="171/9c", algorithm=md5, qop="auth", '
        'opaque="55aa"'
    )
    header = build_authorization(
        challenge,
        DigestCredentials(username="1000", password="s3cr3t"),
        method="REGISTER",
        uri="sip:pbx.example.test",
        cnonce="fixedcnonce",
        nc=1,
    )
    assert header.startswith("Digest ")
    assert _param(header, "username") == "1000"
    assert _param(header, "uri") == "sip:pbx.example.test"
    assert _param(header, "algorithm") == "md5"  # echoed as challenged
    assert _param(header, "opaque") == "55aa"
    assert _param(header, "qop") == "auth"
    # response is deterministic for fixed inputs
    assert re.fullmatch(r"[0-9a-f]{32}", _param(header, "response") or "")


def test_no_qop_uses_rfc2069_response() -> None:
    challenge = DigestChallenge.parse('Digest realm="r", nonce="n"')
    header = build_authorization(
        challenge,
        DigestCredentials(username="u", password="p"),
        method="REGISTER",
        uri="sip:pbx.example.test",
    )
    # RFC 2069: response = MD5(HA1:nonce:HA2); no qop/nc/cnonce in the header.
    assert _param(header, "qop") is None
    assert _param(header, "nc") is None
    assert _param(header, "cnonce") is None
    assert _param(header, "response") is not None


def test_nc_is_zero_padded_hex() -> None:
    challenge = DigestChallenge.parse('Digest realm="r", nonce="n", qop="auth"')
    creds = DigestCredentials(username="u", password="p")
    h1 = build_authorization(
        challenge, creds, method="REGISTER", uri="sip:x", cnonce="c", nc=1
    )
    h16 = build_authorization(
        challenge, creds, method="REGISTER", uri="sip:x", cnonce="c", nc=16
    )
    assert _param(h1, "nc") == "00000001"
    assert _param(h16, "nc") == "00000010"


def test_response_is_deterministic_for_fixed_cnonce() -> None:
    challenge = DigestChallenge.parse('Digest realm="r", nonce="n", qop="auth"')
    creds = DigestCredentials(username="u", password="p")
    a = build_authorization(
        challenge, creds, method="REGISTER", uri="sip:x", cnonce="c", nc=1
    )
    b = build_authorization(
        challenge, creds, method="REGISTER", uri="sip:x", cnonce="c", nc=1
    )
    assert _param(a, "response") == _param(b, "response")


def test_generated_cnonce_is_random_when_not_supplied() -> None:
    challenge = DigestChallenge.parse('Digest realm="r", nonce="n", qop="auth"')
    creds = DigestCredentials(username="u", password="p")
    a = build_authorization(challenge, creds, method="REGISTER", uri="sip:x")
    b = build_authorization(challenge, creds, method="REGISTER", uri="sip:x")
    assert _param(a, "cnonce") != _param(b, "cnonce")
