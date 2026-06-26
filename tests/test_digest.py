"""Tests for hermes_voip.digest — RFC 2617/7616/8760 SIP digest authentication.

The load-bearing cases are known-answer vectors from published RFCs:

- RFC 2617 §3.5 MD5 (``6629fae49393a05397450978507c4ef1``)
- RFC 7616 §3.9.1 SHA-256
  (``753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1``)
- MD5-sess, SHA-256-sess: independently-computed vectors (rule 19; pinned per test)

Fakes only — no real gateway host or credentials (the repo is public; AGENTS.md).
"""

import hashlib
import re

import pytest

from hermes_voip.digest import (
    _PARAM,
    DigestChallenge,
    DigestCredentials,
    _quoted,
    _unescape,
    build_authorization,
    pick_best_challenge,
)


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
        'Digest realm="pbx.example.test", nonce="171/9c", algorithm=md5, '
        'qop="auth", opaque="55aa"'
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
    # pinned known-answer for these fixed inputs (rule 19: no shape-only assertions)
    assert _param(header, "response") == "74e090d5d1ded4f9c97e68d9823d559e"


def test_no_qop_uses_rfc2069_response() -> None:
    challenge = DigestChallenge.parse('Digest realm="pbx.example.test", nonce="abc123"')
    header = build_authorization(
        challenge,
        DigestCredentials(username="1000", password="s3cr3t"),
        method="REGISTER",
        uri="sip:pbx.example.test",
    )
    # RFC 2069: response = MD5(HA1:nonce:HA2); no qop/nc/cnonce in the header.
    assert _param(header, "qop") is None
    assert _param(header, "nc") is None
    assert _param(header, "cnonce") is None
    assert _param(header, "response") == "63d60cc16a94d108c62cddcff1c171af"


def test_rejects_qop_present_without_auth() -> None:
    # A server offering only auth-int must not be answered with the RFC 2069 form.
    challenge = DigestChallenge.parse(
        'Digest realm="pbx.example.test", nonce="n", qop="auth-int"'
    )
    with pytest.raises(ValueError, match="qop"):
        build_authorization(
            challenge,
            DigestCredentials(username="1000", password="s3cr3t"),
            method="REGISTER",
            uri="sip:pbx.example.test",
        )


def test_rejects_sha512_unsupported_algorithm() -> None:
    # SHA-512 is not a mandated SIP digest algorithm; reject rather than mis-sign.
    challenge = DigestChallenge.parse(
        'Digest realm="pbx.example.test", nonce="n", algorithm=SHA-512, qop="auth"'
    )
    with pytest.raises(ValueError, match="algorithm"):
        build_authorization(
            challenge,
            DigestCredentials(username="1000", password="s3cr3t"),
            method="REGISTER",
            uri="sip:pbx.example.test",
        )


def test_rejects_crlf_in_auth_param_value() -> None:
    # A nonce carrying CRLF (hostile/garbled peer) must not be able to inject
    # additional SIP headers via the Authorization value.
    challenge = DigestChallenge.parse('Digest realm="pbx.example.test", nonce="n"')
    poisoned = DigestChallenge(realm=challenge.realm, nonce="n\r\nInjected: x")
    with pytest.raises(ValueError, match="control"):
        build_authorization(
            poisoned,
            DigestCredentials(username="1000", password="s3cr3t"),
            method="REGISTER",
            uri="sip:pbx.example.test",
        )


@pytest.mark.parametrize("invalid_nc", [0, -1, 2**32])
def test_rejects_out_of_range_nonce_count(invalid_nc: int) -> None:
    challenge = DigestChallenge.parse('Digest realm="r", nonce="n", qop="auth"')
    creds = DigestCredentials(username="u", password="p")
    with pytest.raises(ValueError, match="nonce-count"):
        build_authorization(
            challenge,
            creds,
            method="REGISTER",
            uri="sip:x",
            cnonce="c",
            nc=invalid_nc,
        )


def test_rejects_control_char_in_realm_auth_param_value() -> None:
    challenge = DigestChallenge(
        realm="pbx.example.test\x1fzone", nonce="n", qop=("auth",)
    )
    with pytest.raises(ValueError, match="control"):
        build_authorization(
            challenge,
            DigestCredentials(username="1000", password="s3cr3t"),
            method="REGISTER",
            uri="sip:pbx.example.test",
            cnonce="fixedcnonce",
        )


def test_rejects_control_char_in_uri_auth_param_value() -> None:
    challenge = DigestChallenge.parse(
        'Digest realm="pbx.example.test", nonce="n", qop="auth"'
    )
    with pytest.raises(ValueError, match="control"):
        build_authorization(
            challenge,
            DigestCredentials(username="1000", password="s3cr3t"),
            method="REGISTER",
            uri="sip:pbx.example.test\ninvalid",
            cnonce="fixedcnonce",
        )


def test_rejects_control_char_in_username_auth_param_value() -> None:
    challenge = DigestChallenge.parse(
        'Digest realm="pbx.example.test", nonce="n", qop="auth"'
    )
    with pytest.raises(ValueError, match="control"):
        build_authorization(
            challenge,
            DigestCredentials(username="1000\rname", password="s3cr3t"),
            method="REGISTER",
            uri="sip:pbx.example.test",
            cnonce="fixedcnonce",
        )


def test_rejects_control_char_in_opaque_auth_param_value() -> None:
    challenge = DigestChallenge(
        realm="pbx.example.test",
        nonce="n",
        qop=("auth",),
        opaque="opaque\x7fvalue",
    )
    with pytest.raises(ValueError, match="control"):
        build_authorization(
            challenge,
            DigestCredentials(username="1000", password="s3cr3t"),
            method="REGISTER",
            uri="sip:pbx.example.test",
            cnonce="fixedcnonce",
        )


def test_rejects_control_char_in_cnonce_auth_param_value() -> None:
    challenge = DigestChallenge.parse(
        'Digest realm="pbx.example.test", nonce="n", qop="auth"'
    )
    with pytest.raises(ValueError, match="control"):
        build_authorization(
            challenge,
            DigestCredentials(username="1000", password="s3cr3t"),
            method="REGISTER",
            uri="sip:pbx.example.test",
            cnonce="fixed\r\ncnonce",
        )


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


def test_parse_honours_quoted_pair_escapes_in_quoted_strings() -> None:
    # RFC 2617 quoted-strings allow quoted-pair escapes: \" is a literal quote
    # and \\ a literal backslash. The parser must unescape them, not stop at the
    # first inner quote (which truncates realm and silently corrupts HA1).
    challenge = DigestChallenge.parse(
        r'Digest realm="a\"b\\c", nonce="n\"x", qop="auth"'
    )
    assert challenge.realm == 'a"b\\c'  # a"b\c
    assert challenge.nonce == 'n"x'


def test_response_hashes_the_unescaped_realm_not_the_wire_form() -> None:
    # The security-sensitive property: realm/nonce feed HA1/response in their
    # UNESCAPED form, while the emitted header escapes them for the wire. A
    # future "simplify" that hashed the escaped form (or stopped escaping the
    # header) must fail this independently-computed known-answer vector.
    realm = 'a"b\\c'  # contains a literal quote and a literal backslash
    nonce = "nonce42"
    challenge = DigestChallenge(realm=realm, nonce=nonce, qop=("auth",))
    creds = DigestCredentials(username="1000", password="s3cr3t")
    header = build_authorization(
        challenge,
        creds,
        method="REGISTER",
        uri="sip:pbx.example.test",
        cnonce="fixedcnonce",
        nc=1,
    )

    def md5_hex(value: str) -> str:
        return hashlib.md5(value.encode("utf-8")).hexdigest()  # noqa: S324 - test KAT

    ha1 = md5_hex(f"1000:{realm}:s3cr3t")
    ha2 = md5_hex("REGISTER:sip:pbx.example.test")
    expected = md5_hex(f"{ha1}:{nonce}:00000001:fixedcnonce:auth:{ha2}")

    # The header renders the escaped wire form of the realm...
    assert r'realm="a\"b\\c"' in header
    # ...but the response equals the digest of the UNESCAPED inputs.
    assert f'response="{expected}"' in header


def test_username_with_quote_is_escaped_on_the_wire_and_hashed_raw() -> None:
    # The same asymmetry must hold for the username (it feeds HA1 and is quoted).
    username = 'jo"hn'
    realm = "pbx.example.test"
    nonce = "n"
    challenge = DigestChallenge(realm=realm, nonce=nonce, qop=("auth",))
    creds = DigestCredentials(username=username, password="pw")
    header = build_authorization(
        challenge, creds, method="REGISTER", uri="sip:x", cnonce="c", nc=1
    )

    def md5_hex(value: str) -> str:
        return hashlib.md5(value.encode("utf-8")).hexdigest()  # noqa: S324 - test KAT

    ha1 = md5_hex(f"{username}:{realm}:pw")
    ha2 = md5_hex("REGISTER:sip:x")
    expected = md5_hex(f"{ha1}:{nonce}:00000001:c:auth:{ha2}")
    assert r'username="jo\"hn"' in header
    assert f'response="{expected}"' in header


@pytest.mark.parametrize(
    "value",
    [
        'a"b\\c',  # literal quote and literal backslash
        '"',  # a lone quote
        "\\",  # a lone backslash
        'realm "with" quotes \\ and slashes',  # mixed
        "plain",  # no escapes at all (must survive the round-trip too)
    ],
)
def test_quoted_then_parse_round_trip_recovers_the_literal_value(value: str) -> None:
    # The builder (_quoted) and the parser (_PARAM + _unescape) are inverses:
    # a value rendered into a quoted-string must parse back to the same literal.
    # An asymmetric escape/unescape silently corrupts realm/nonce before they
    # reach HA1/HA2, so the round-trip is the load-bearing invariant.
    rendered = f'realm="{_quoted(value)}"'
    match = _PARAM.search(rendered)
    assert match is not None
    quoted_body = match.group(2)
    assert quoted_body is not None  # the quoted alternative (not the bare one) matched
    assert _unescape(quoted_body) == value


def test_parse_bare_value_stops_at_semicolon_not_swallowing_trailing_params() -> None:
    # RFC 3261/2617 token cannot contain ';' (it is a separator). A bare value
    # must terminate at ';', not run on and swallow semicolon-delimited trailing
    # content. The original bare alternative [^,\s]+ swallowed it, so a realm of
    # 'r;x=y' fed a corrupted realm into HA1.
    challenge = DigestChallenge.parse("Digest realm=r;x=y, nonce=n")
    assert challenge.realm == "r"
    assert challenge.nonce == "n"


# ---------------------------------------------------------------------------
# SHA-256 (RFC 7616 / RFC 8760) — known-answer vectors
# ---------------------------------------------------------------------------


def test_rfc7616_section_3_9_1_sha256_known_answer() -> None:
    """RFC 7616 §3.9.1 worked example — SHA-256 qop=auth.

    Published inputs and expected response (hex) taken verbatim from the RFC.
    Any implementation that produces a different digest fails this test, which
    catches wrong hash function, wrong HA1/HA2 construction, or wrong ordering.
    """
    # Inputs from RFC 7616 §3.9.1
    challenge = DigestChallenge.parse(
        'Digest realm="http-auth@example.org", '
        'qop="auth, auth-int", '
        "algorithm=SHA-256, "
        'nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v", '
        'opaque="FQhe/qaU925kfnzjCev0ciny7QMkPqMAFRtzCUYo5tdS"'
    )
    assert challenge.algorithm == "SHA-256"
    header = build_authorization(
        challenge,
        DigestCredentials(username="Mufasa", password="Circle of Life"),
        method="GET",
        uri="/dir/index.html",
        cnonce="f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ",
        nc=1,
    )
    # Expected from RFC 7616 §3.9.1
    assert (
        _param(header, "response")
        == "753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1"
    )
    assert _param(header, "algorithm") == "SHA-256"
    assert _param(header, "qop") == "auth"


def test_sha256_sip_register_known_answer() -> None:
    """SHA-256 SIP REGISTER — independently computed KAT vector.

    HA1 = SHA-256("1000:pbx.example.test:s3cr3t") = 76d8fb619…
    HA2 = SHA-256("REGISTER:sip:pbx.example.test") = acc841bfe…
    response = SHA-256(HA1:sip_nonce_42:00000001:sip_cnonce_42:auth:HA2)
             = 266fff235961b5de438936f840df947e16342a5854d9da67de3d19b4072a9844
    """
    challenge = DigestChallenge.parse(
        'Digest realm="pbx.example.test", nonce="sip_nonce_42", '
        'algorithm=SHA-256, qop="auth"'
    )
    header = build_authorization(
        challenge,
        DigestCredentials(username="1000", password="s3cr3t"),
        method="REGISTER",
        uri="sip:pbx.example.test",
        cnonce="sip_cnonce_42",
        nc=1,
    )
    assert (
        _param(header, "response")
        == "266fff235961b5de438936f840df947e16342a5854d9da67de3d19b4072a9844"
    )
    assert _param(header, "algorithm") == "SHA-256"


def test_parse_accepts_sha256_algorithm_token() -> None:
    """Parser must accept ``SHA-256`` (and ``sha-256``) as the algorithm field."""
    ch = DigestChallenge.parse(
        'Digest realm="pbx.example.test", nonce="n", algorithm=SHA-256, qop="auth"'
    )
    assert ch.algorithm == "SHA-256"

    ch_lower = DigestChallenge.parse(
        'Digest realm="pbx.example.test", nonce="n", algorithm=sha-256, qop="auth"'
    )
    assert ch_lower.algorithm == "sha-256"


def test_parse_accepts_sha256_in_quoted_form() -> None:
    ch = DigestChallenge.parse(
        'Digest realm="pbx.example.test", nonce="n", algorithm="SHA-256", qop="auth"'
    )
    assert ch.algorithm == "SHA-256"


# ---------------------------------------------------------------------------
# MD5-sess — known-answer vectors
# ---------------------------------------------------------------------------


def test_md5_sess_sip_register_known_answer() -> None:
    """MD5-sess HA1 = MD5(MD5(user:realm:pass):nonce:cnonce).

    All inputs and the pinned response are independently derived; the test is
    deterministic for any fixed (username, realm, password, nonce, cnonce).

    HA1-base = MD5("1000:pbx.example.test:s3cr3t") = fd244f9ae6a9c052fbd9301be3490375
    HA1-sess = MD5(HA1-base:sip_nonce_42:sip_cnonce_42)
             = 15602ab3deffb069805d5775e5e5626f
    HA2      = MD5("REGISTER:sip:pbx.example.test") = c585fdc849a05928f3ed76cc3270bbc8
    response = MD5(HA1-sess:sip_nonce_42:00000001:sip_cnonce_42:auth:HA2)
             = 5755195a21c2ac0abb4cba6554ac7efe
    """
    challenge = DigestChallenge.parse(
        'Digest realm="pbx.example.test", nonce="sip_nonce_42", '
        'algorithm=MD5-sess, qop="auth"'
    )
    assert challenge.algorithm == "MD5-sess"
    header = build_authorization(
        challenge,
        DigestCredentials(username="1000", password="s3cr3t"),
        method="REGISTER",
        uri="sip:pbx.example.test",
        cnonce="sip_cnonce_42",
        nc=1,
    )
    assert _param(header, "response") == "5755195a21c2ac0abb4cba6554ac7efe"
    assert _param(header, "algorithm") == "MD5-sess"
    assert _param(header, "cnonce") == "sip_cnonce_42"


def test_md5_sess_rfc7616_inputs() -> None:
    """MD5-sess with the RFC 7616 §3.9.1 username/realm/password/nonce/cnonce.

    Using the same inputs as the SHA-256 RFC example to cross-check that the
    -sess construction differs from plain MD5 and from SHA-256.

    HA1-base = MD5("Mufasa:http-auth@example.org:Circle of Life")
             = 3d78807defe7de2157e2b0b6573a855f
    HA1-sess = MD5(HA1-base:7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v:
                   f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ)
             = 2b3d906f52651c3136e1502b3d6f38ee
    HA2      = MD5("GET:/dir/index.html") = 39aff3a2bab6126f332b942af96d3366
    response = MD5(HA1-sess:7ypf/…:00000001:f2/…:auth:HA2)
             = e783283f46242139c486a698fec7211d
    """
    nonce = "7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v"
    cnonce = "f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ"
    challenge = DigestChallenge(
        realm="http-auth@example.org",
        nonce=nonce,
        algorithm="MD5-sess",
        qop=("auth",),
    )
    header = build_authorization(
        challenge,
        DigestCredentials(username="Mufasa", password="Circle of Life"),
        method="GET",
        uri="/dir/index.html",
        cnonce=cnonce,
        nc=1,
    )
    assert _param(header, "response") == "e783283f46242139c486a698fec7211d"


# ---------------------------------------------------------------------------
# SHA-256-sess — known-answer vectors
# ---------------------------------------------------------------------------


def test_sha256_sess_known_answer() -> None:
    """SHA-256-sess: HA1 = SHA-256(SHA-256(user:realm:pass):nonce:cnonce).

    HA1-base = SHA-256("Mufasa:http-auth@example.org:Circle of Life")
             = 7987c64c30e25f1b74be53f966b49b90f2808aa92faf9a00262392d7b4794232
    HA1-sess = SHA-256(HA1-base:7ypf/…:f2/…)
             = bca21f4c7d7e8bf70d96361085370c7d219947abc1b8cd628f710917b89bed5b
    HA2      = SHA-256("GET:/dir/index.html")
             = 9a3fdae9a622fe8de177c24fa9c070f2b181ec85e15dcbdc32e10c82ad450b04
    response = SHA-256(HA1-sess:nonce:00000001:cnonce:auth:HA2)
             = 2fd51b3a77ad75bad6afad6003e818d767133c46d9e2749e7f5232ae1ea3efd7
    """
    nonce = "7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v"
    cnonce = "f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ"
    challenge = DigestChallenge(
        realm="http-auth@example.org",
        nonce=nonce,
        algorithm="SHA-256-sess",
        qop=("auth",),
    )
    header = build_authorization(
        challenge,
        DigestCredentials(username="Mufasa", password="Circle of Life"),
        method="GET",
        uri="/dir/index.html",
        cnonce=cnonce,
        nc=1,
    )
    assert (
        _param(header, "response")
        == "2fd51b3a77ad75bad6afad6003e818d767133c46d9e2749e7f5232ae1ea3efd7"
    )
    assert _param(header, "algorithm") == "SHA-256-sess"


# ---------------------------------------------------------------------------
# Algorithm preference selection (RFC 8760 §3 preference order)
# ---------------------------------------------------------------------------


def test_parse_picks_strongest_algorithm_from_multiple_challenges() -> None:
    """pick_best_challenge selects SHA-256 over MD5 (RFC 8760 §3 preference)."""
    sha256_challenge = DigestChallenge(
        realm="pbx.example.test", nonce="n1", algorithm="SHA-256", qop=("auth",)
    )
    md5_challenge = DigestChallenge(
        realm="pbx.example.test", nonce="n2", algorithm="MD5", qop=("auth",)
    )
    # SHA-256 wins regardless of order
    assert pick_best_challenge([md5_challenge, sha256_challenge]) is sha256_challenge
    assert pick_best_challenge([sha256_challenge, md5_challenge]) is sha256_challenge


def test_pick_best_challenge_sha256_sess_over_sha256() -> None:
    """SHA-256-sess is preferred over plain SHA-256 (offers session binding)."""
    sha256 = DigestChallenge(
        realm="pbx.example.test", nonce="n1", algorithm="SHA-256", qop=("auth",)
    )
    sha256_sess = DigestChallenge(
        realm="pbx.example.test", nonce="n1", algorithm="SHA-256-sess", qop=("auth",)
    )
    assert pick_best_challenge([sha256, sha256_sess]) is sha256_sess


def test_pick_best_challenge_falls_back_to_single_md5() -> None:
    """A single MD5 challenge is selected when it is the only option."""
    md5 = DigestChallenge(
        realm="pbx.example.test", nonce="n", algorithm="MD5", qop=("auth",)
    )
    assert pick_best_challenge([md5]) is md5


def test_pick_best_challenge_rejects_empty_list() -> None:
    """pick_best_challenge raises ValueError on an empty list."""
    with pytest.raises(ValueError, match="empty"):
        pick_best_challenge([])


def test_pick_best_challenge_md5_sess_over_md5() -> None:
    """MD5-sess is preferred over plain MD5 (adds session binding)."""
    md5 = DigestChallenge(
        realm="pbx.example.test", nonce="n", algorithm="MD5", qop=("auth",)
    )
    md5_sess = DigestChallenge(
        realm="pbx.example.test", nonce="n", algorithm="MD5-sess", qop=("auth",)
    )
    assert pick_best_challenge([md5, md5_sess]) is md5_sess


# ---------------------------------------------------------------------------
# Regression: unsupported algorithms still raise
# ---------------------------------------------------------------------------


def test_rejects_genuinely_unsupported_algorithm() -> None:
    """SHA-512 and unknown tokens must raise, not silently mis-sign."""
    for algo in ("SHA-512", "SHA-1", "unknown-algo"):
        challenge = DigestChallenge(
            realm="pbx.example.test",
            nonce="n",
            algorithm=algo,
            qop=("auth",),
        )
        with pytest.raises(ValueError, match="unsupported"):
            build_authorization(
                challenge,
                DigestCredentials(username="1000", password="s3cr3t"),
                method="REGISTER",
                uri="sip:pbx.example.test",
                cnonce="c",
            )


# ---------------------------------------------------------------------------
# no-qop legacy (RFC 2069) is MD5-only; -sess and SHA-256 require qop
# ---------------------------------------------------------------------------
#
# Rule-19 note: this section REPLACES the earlier
# test_md5_sess_no_qop_emits_cnonce_in_header, which asserted a spec violation.
# RFC 2617 §3.2.2 states "cnonce ... MUST NOT be specified if the server did
# not send a qop directive" — so a -sess challenge WITHOUT qop is internally
# inconsistent (it needs a cnonce to build HA1 but is forbidden from sending
# one). RFC 7616 §3.4.1 / RFC 8760 §2.6 define NO RFC 2069 legacy form for
# SHA-256(/-sess). The correct behaviour for all of these is to RAISE; the
# legacy no-qop form is valid ONLY for plain MD5 (ancient-server back-compat).


def test_md5_sess_without_qop_raises() -> None:
    # RFC 2617 §3.2.2: -sess needs a cnonce for HA1 but MUST NOT send one
    # without qop — the combination is unanswerable, so raise.
    challenge = DigestChallenge(
        realm="pbx.example.test",
        nonce="sip_nonce_42",
        algorithm="MD5-sess",
        qop=(),  # no qop offered
    )
    with pytest.raises(ValueError, match="qop"):
        build_authorization(
            challenge,
            DigestCredentials(username="1000", password="s3cr3t"),
            method="REGISTER",
            uri="sip:pbx.example.test",
            cnonce="sip_cnonce_42",
        )


def test_sha256_without_qop_raises() -> None:
    # RFC 7616 §3.4.1 / RFC 8760 §2.6 define no RFC 2069 legacy form for
    # SHA-256; a challenge that omits qop cannot be answered — raise.
    challenge = DigestChallenge(
        realm="pbx.example.test",
        nonce="sip_nonce_42",
        algorithm="SHA-256",
        qop=(),
    )
    with pytest.raises(ValueError, match="qop"):
        build_authorization(
            challenge,
            DigestCredentials(username="1000", password="s3cr3t"),
            method="REGISTER",
            uri="sip:pbx.example.test",
            cnonce="sip_cnonce_42",
        )


def test_sha256_sess_without_qop_raises() -> None:
    # SHA-256-sess inherits both constraints: no legacy form AND the §3.2.2
    # cnonce-without-qop prohibition. Raise.
    challenge = DigestChallenge(
        realm="pbx.example.test",
        nonce="sip_nonce_42",
        algorithm="SHA-256-sess",
        qop=(),
    )
    with pytest.raises(ValueError, match="qop"):
        build_authorization(
            challenge,
            DigestCredentials(username="1000", password="s3cr3t"),
            method="REGISTER",
            uri="sip:pbx.example.test",
            cnonce="sip_cnonce_42",
        )


def test_plain_md5_no_qop_still_uses_legacy_rfc2069() -> None:
    # Regression guard: plain MD5 with no qop MUST keep the RFC 2069 legacy
    # form response = MD5(HA1:nonce:HA2), with no qop/nc/cnonce in the header.
    # This is the only algorithm for which the no-qop legacy path is valid.
    challenge = DigestChallenge(
        realm="pbx.example.test",
        nonce="abc123",
        algorithm="MD5",
        qop=(),
    )
    header = build_authorization(
        challenge,
        DigestCredentials(username="1000", password="s3cr3t"),
        method="REGISTER",
        uri="sip:pbx.example.test",
    )

    def md5_hex(value: str) -> str:
        return hashlib.md5(value.encode("utf-8")).hexdigest()  # noqa: S324 - test KAT

    ha1 = md5_hex("1000:pbx.example.test:s3cr3t")
    ha2 = md5_hex("REGISTER:sip:pbx.example.test")
    expected = md5_hex(f"{ha1}:abc123:{ha2}")
    assert _param(header, "response") == expected
    assert _param(header, "qop") is None
    assert _param(header, "nc") is None
    assert _param(header, "cnonce") is None
