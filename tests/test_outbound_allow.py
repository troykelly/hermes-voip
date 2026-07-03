"""Tests for the outbound dial allowlist parser (ADR-0028, ADR-0029, ADR-0099).

``HERMES_VOIP_OUTBOUND_ALLOW`` is a comma-separated list of permitted dial targets
(extensions and/or SIP URIs). The DEFAULT is EMPTY — the agent-initiated outbound
feature ships INERT (no number may be dialled) until the operator opts numbers in.

ADR-0099 adds an OPTIONAL file-backed source ``HERMES_VOIP_OUTBOUND_ALLOW_FILE`` (a
path to a gitignored plain list of the SAME entries, comma/newline separated), whose
entries are UNIONed with the inline var — so a real-number corpus can live off the
shell-visible environment. A configured-but-missing/unreadable file fails loud
(:class:`ConfigError`, rule 37).

These are pure tests (no hermes-agent runtime): they exercise
:mod:`hermes_voip.outbound_allow` directly with fake env mappings and fake targets
(ext ``1000``/``1001``, ``sip:1000@pbx.example.test``). Fakes only — never a real
number (the repo is public; a real dial target lives only in the gitignored ``.env``
or a gitignored allow file).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_voip.config import ConfigError
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


# ---------------------------------------------------------------------------
# ADR-0099: file-backed source (HERMES_VOIP_OUTBOUND_ALLOW_FILE), union-merged.
# A dial target may live in a gitignored FILE (off the shell-visible env) instead
# of / in addition to the inline var. Fakes only (1000/1001/sip:…@pbx.example.test).
# ---------------------------------------------------------------------------


def test_file_backed_entries_are_allowed_when_inline_var_unset(tmp_path: Path) -> None:
    """A file of entries permits those targets even with the inline var unset.

    THE CORE ASK: real numbers/SIP URIs need not live in a shell-visible env var —
    a gitignored file (newline-separated here) is a first-class allowlist source.
    """
    allow_file = tmp_path / "outbound_allow.txt"
    allow_file.write_text("1000\n1001\n", encoding="utf-8")
    allow = load_outbound_allowlist(
        {"HERMES_VOIP_OUTBOUND_ALLOW_FILE": str(allow_file)}
    )
    assert is_outbound_allowed("1000", allow) is True
    assert is_outbound_allowed("1001", allow) is True
    # An unlisted target is still rejected (the gate holds via the file source).
    assert is_outbound_allowed("2000", allow) is False


def test_file_entries_may_be_comma_separated(tmp_path: Path) -> None:
    """The file uses the SAME comma-separated grammar as the inline var."""
    allow_file = tmp_path / "outbound_allow.txt"
    allow_file.write_text("1000, 1001", encoding="utf-8")
    allow = load_outbound_allowlist(
        {"HERMES_VOIP_OUTBOUND_ALLOW_FILE": str(allow_file)}
    )
    assert is_outbound_allowed("1000", allow) is True
    assert is_outbound_allowed("1001", allow) is True


def test_file_and_inline_entries_are_unioned(tmp_path: Path) -> None:
    """Precedence rule (ADR-0099): file entries UNION inline entries — both apply.

    Neither source can silently drop the other's entries; the effective allowlist is
    every operator-authored entry from either place.
    """
    allow_file = tmp_path / "outbound_allow.txt"
    allow_file.write_text("1001\n", encoding="utf-8")
    allow = load_outbound_allowlist(
        {
            "HERMES_VOIP_OUTBOUND_ALLOW": "1000",
            "HERMES_VOIP_OUTBOUND_ALLOW_FILE": str(allow_file),
        }
    )
    assert is_outbound_allowed("1000", allow) is True  # inline
    assert is_outbound_allowed("1001", allow) is True  # file
    assert is_outbound_allowed("2000", allow) is False  # neither


def test_file_entries_use_the_same_mask_and_uri_grammar(tmp_path: Path) -> None:
    """A file entry gets the identical normalization/validation as the inline path.

    An ``x``-digit mask and a SIP URI behave exactly as they do inline: ``10xx``
    matches ``1000``..``1099`` (not ``10``/``10000``), and a URI is exact-only.
    """
    allow_file = tmp_path / "outbound_allow.txt"
    allow_file.write_text("10xx\nsip:1000@pbx.example.test\n", encoding="utf-8")
    allow = load_outbound_allowlist(
        {"HERMES_VOIP_OUTBOUND_ALLOW_FILE": str(allow_file)}
    )
    assert is_outbound_allowed("1000", allow) is True  # mask lower bound
    assert is_outbound_allowed("1099", allow) is True  # mask upper bound
    assert is_outbound_allowed("10", allow) is False  # mask is fixed-length
    assert is_outbound_allowed("10000", allow) is False  # no over-match
    assert is_outbound_allowed("sip:1000@pbx.example.test", allow) is True
    assert is_outbound_allowed("sip:9999@pbx.example.test", allow) is False


def test_file_entries_are_trimmed_and_blanks_dropped(tmp_path: Path) -> None:
    """Surrounding whitespace, blank lines, and empty comma fields are dropped."""
    allow_file = tmp_path / "outbound_allow.txt"
    allow_file.write_text("  1000 ,\n\n  1001  ,, \n", encoding="utf-8")
    allow = load_outbound_allowlist(
        {"HERMES_VOIP_OUTBOUND_ALLOW_FILE": str(allow_file)}
    )
    assert is_outbound_allowed("1000", allow) is True
    assert is_outbound_allowed("1001", allow) is True
    # No empty entry was admitted (a blank candidate is never allowed).
    assert is_outbound_allowed("", allow) is False


def test_blank_file_does_not_wipe_inline_entries(tmp_path: Path) -> None:
    """Union robustness: a present-but-empty file contributes nothing (no silent wipe).

    Under UNION (not file-overrides), an empty file must NOT drop the inline
    allowlist the operator has configured — the ADR-0099 anti-footgun property.
    """
    allow_file = tmp_path / "outbound_allow.txt"
    allow_file.write_text("   \n\n", encoding="utf-8")  # whitespace only
    allow = load_outbound_allowlist(
        {
            "HERMES_VOIP_OUTBOUND_ALLOW": "1000",
            "HERMES_VOIP_OUTBOUND_ALLOW_FILE": str(allow_file),
        }
    )
    assert is_outbound_allowed("1000", allow) is True  # inline survived


def test_empty_file_and_unset_inline_is_deny_all(tmp_path: Path) -> None:
    """Fail-closed: an empty file plus no inline var permits nothing (inert default)."""
    allow_file = tmp_path / "outbound_allow.txt"
    allow_file.write_text("", encoding="utf-8")
    allow = load_outbound_allowlist(
        {"HERMES_VOIP_OUTBOUND_ALLOW_FILE": str(allow_file)}
    )
    assert allow == frozenset()
    assert is_outbound_allowed("1000", allow) is False


def test_missing_file_raises_configerror(tmp_path: Path) -> None:
    """A configured-but-missing path fails loud (rule 37), not a silent empty list."""
    missing = tmp_path / "does_not_exist.txt"
    with pytest.raises(ConfigError):
        load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW_FILE": str(missing)})


def test_unreadable_file_raises_configerror(tmp_path: Path) -> None:
    """A path that exists but cannot be read (a directory) fails loud (OSError)."""
    a_directory = tmp_path / "a_dir"
    a_directory.mkdir()
    with pytest.raises(ConfigError):
        load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW_FILE": str(a_directory)})


def test_unset_file_key_leaves_inline_behaviour_unchanged() -> None:
    """Back-compat: with no file key set, behaviour is exactly the inline path."""
    allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "1000"})
    assert is_outbound_allowed("1000", allow) is True
    assert is_outbound_allowed("1001", allow) is False


def test_blank_file_path_value_is_treated_as_unset(tmp_path: Path) -> None:
    """A blank/whitespace file-path value is treated as unset (inline-only, no read)."""
    allow = load_outbound_allowlist(
        {
            "HERMES_VOIP_OUTBOUND_ALLOW": "1000",
            "HERMES_VOIP_OUTBOUND_ALLOW_FILE": "   ",
        }
    )
    assert is_outbound_allowed("1000", allow) is True
