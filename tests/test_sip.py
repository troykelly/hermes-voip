"""Tests for hermes_voip.sip.

Use deliberately fake values (ext 1000, *.example.test) — never the real PBX host
or extension, which are secret (public repo; AGENTS.md invariant).
"""

import pytest

from hermes_voip import sip_address_of_record


def test_builds_aor_from_extension_and_host() -> None:
    aor = sip_address_of_record("1000", "pbx.example.test")
    assert aor == "sip:1000@pbx.example.test"


def test_trims_surrounding_whitespace() -> None:
    aor = sip_address_of_record("  1000 ", " pbx.example.test ")
    assert aor == "sip:1000@pbx.example.test"


def test_rejects_empty_extension() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        sip_address_of_record("", "pbx.example.test")


def test_rejects_blank_host() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        sip_address_of_record("1000", "   ")
