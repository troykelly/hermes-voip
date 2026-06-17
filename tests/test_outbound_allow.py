"""Tests for the outbound dial allowlist parser (ADR-0028).

``HERMES_VOIP_OUTBOUND_ALLOW`` is a comma-separated list of permitted dial targets
(extensions and/or SIP URIs). The DEFAULT is EMPTY — the agent-initiated outbound
feature ships INERT (no number may be dialled) until the operator opts numbers in.

These are pure tests (no hermes-agent runtime): they exercise
:mod:`hermes_voip.outbound_allow` directly with fake env mappings and fake targets
(ext ``1000``/``1001``, ``+1555…``). Fakes only — never a real number (the repo is
public; a real dial target lives only in the gitignored ``.env``).
"""

from __future__ import annotations

from hermes_voip.outbound_allow import is_outbound_allowed, load_outbound_allowlist


def test_default_is_empty_so_nothing_is_allowed() -> None:
    """Absent env => empty allowlist => the feature is inert (no target permitted)."""
    allow = load_outbound_allowlist({})
    assert allow == frozenset()
    assert is_outbound_allowed("1000", allow) is False
    assert is_outbound_allowed("sip:1000@pbx.example.test", allow) is False


def test_blank_value_is_empty() -> None:
    """A blank / whitespace-only value parses to the empty (inert) allowlist."""
    assert load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "   "}) == frozenset()
    assert load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": ""}) == frozenset()


def test_comma_separated_extensions_parse_and_trim() -> None:
    """A comma list of extensions parses, trimming surrounding whitespace + blanks."""
    allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": " 1000 , 1001 ,, "})
    assert allow == frozenset({"1000", "1001"})


def test_listed_extension_is_allowed_unlisted_is_not() -> None:
    """A listed target is permitted; any other target is rejected (the hard gate)."""
    allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "1000,1001"})
    assert is_outbound_allowed("1000", allow) is True
    assert is_outbound_allowed("1001", allow) is True
    # An unlisted extension is rejected even if it merely shares a prefix.
    assert is_outbound_allowed("10000", allow) is False
    assert is_outbound_allowed("2000", allow) is False


def test_sip_uri_targets_are_supported() -> None:
    """SIP-URI dial targets are first-class allowlist entries (exact match)."""
    allow = load_outbound_allowlist(
        {"HERMES_VOIP_OUTBOUND_ALLOW": "sip:1000@pbx.example.test, 1001"}
    )
    assert is_outbound_allowed("sip:1000@pbx.example.test", allow) is True
    assert is_outbound_allowed("1001", allow) is True
    assert is_outbound_allowed("sip:9999@pbx.example.test", allow) is False


def test_match_is_whitespace_insensitive_on_the_candidate() -> None:
    """The candidate is trimmed before matching (a stray space never bypasses)."""
    allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "1000"})
    assert is_outbound_allowed("  1000  ", allow) is True
    assert is_outbound_allowed("", allow) is False
