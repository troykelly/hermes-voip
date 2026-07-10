"""Tests for the shared quote-aware name-addr / header-parameter primitives.

RFC 3261 §25.1 permits a ``quoted-string`` display-name or a quoted generic-param
value to contain ``<``, ``>``, and even a literal ``;tag=`` — none of which are
structural. The helpers here are the single quote-aware split every identity site
shares, so ``<sip:a@b>;g=";tag=fake";tag=real`` yields the REAL tag, not the
forged one hidden inside the quoted ``g`` parameter. Fakes only.
"""

from __future__ import annotations

from hermes_voip._name_addr import (
    find_name_addr,
    name_addr_parts,
    params_after_addr,
    split_params,
    tag_param,
)


def test_split_params_ignores_semicolons_inside_quotes() -> None:
    # A ';' inside a double-quoted generic-param value is NOT a parameter
    # separator, and a backslash-escaped quote does not end the quoted span.
    trailing = ';g=";tag=fake;x";expires=60'
    assert split_params(trailing) == ["", 'g=";tag=fake;x"', "expires=60"]

    escaped = r';g="a\"; b";tag=real'
    assert split_params(escaped) == ["", r'g="a\"; b"', "tag=real"]


def test_tag_param_prefers_real_tag_over_quoted_forgery() -> None:
    # The forged ';tag=fake' lives inside the quoted 'g' generic-param; the real
    # dialog tag is the top-level ';tag=' that follows.
    assert tag_param(';g=";tag=fake";tag=realtag99') == "realtag99"
    # No top-level tag at all — only the quoted forgery — is no tag.
    assert tag_param(';g=";tag=fake"') is None
    # A plain trailing tag is returned unchanged.
    assert tag_param(";tag=plain") == "plain"
    assert tag_param(";expires=60") is None


def test_name_addr_parts_splits_outside_quoted_display_name() -> None:
    # The quoted display-name legitimately contains '<', '>', and ';tag='; the
    # real angle-addr and its trailing header params must come from OUTSIDE it.
    value = '"X <Y>;tag=fake" <sip:2000@pbx.example.test>;tag=real'
    parts = name_addr_parts(value)
    assert parts is not None
    display, addr_spec, trailing = parts
    assert display == '"X <Y>;tag=fake" '
    assert addr_spec == "sip:2000@pbx.example.test"
    assert trailing == ";tag=real"

    # find_name_addr stays a thin (addr-spec, trailing) view of the same split.
    assert find_name_addr(value) == ("sip:2000@pbx.example.test", ";tag=real")


def test_name_addr_parts_none_for_bare_addr_spec() -> None:
    # A bare addr-spec has no angle-addr, so there is no display-name to split.
    assert name_addr_parts("sip:2000@pbx.example.test;tag=abc") is None
    assert find_name_addr("sip:2000@pbx.example.test;tag=abc") is None


def test_tag_param_rejects_empty_or_whitespace_tag() -> None:
    # A present-but-empty ';tag=' (or whitespace-only) is malformed (RFC 3261
    # tag = token, 1+ chars) and must count as ABSENT — so it neither classifies a
    # request as in-dialog nor suppresses our own To-tag minting.
    assert tag_param(";tag=") is None
    assert tag_param(";tag=   ") is None
    assert tag_param('g=";tag=fake";tag=') is None
    # A non-empty tag is still returned unchanged.
    assert tag_param(";tag=realtag") == "realtag"


def test_params_after_addr_is_quote_aware_for_the_none_fallback() -> None:
    # When find_name_addr() returns None (a bare addr-spec, or a MALFORMED
    # unterminated '<'), the four identity sites fall back to params_after_addr().
    # A forged ';tag=fake' hidden inside a quoted display-name before an
    # unterminated '<' must NOT be exposed as a real parameter (the old naive
    # str.partition(';') split it inside the quoted span and forged a dialog tag).
    forged = '"X;tag=fake" <sip:1000@pbx.example.test'  # no closing '>'
    assert find_name_addr(forged) is None
    assert params_after_addr(forged) == ""  # the quoted ';' is not a top-level boundary
    assert tag_param(params_after_addr(forged)) is None  # so no forged tag survives
    # A bare addr-spec's real trailing params ARE returned (after the first
    # top-level ';').
    assert params_after_addr("sip:1000@pbx.example.test;tag=abc") == "tag=abc"
    assert tag_param(params_after_addr("sip:1000@pbx.example.test;tag=abc")) == "abc"
    # No parameters at all.
    assert params_after_addr("sip:1000@pbx.example.test") == ""


def test_params_after_addr_honours_backslash_escaped_quote() -> None:
    # A backslash-escaped quote (RFC 3261 §25.1 quoted-pair) inside the quoted
    # display-name does NOT end the quoted span, so a ';tag=' after it is still
    # inside the quotes — not a top-level parameter. Mirrors split_params.
    forged = r'"X\";tag=fake" <sip:1000@pbx.example.test'  # unterminated '<'
    assert find_name_addr(forged) is None
    assert params_after_addr(forged) == ""
    assert tag_param(params_after_addr(forged)) is None
