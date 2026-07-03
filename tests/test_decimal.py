"""Shared fail-closed ASCII-decimal parsing helper tests."""

from __future__ import annotations

from hermes_voip._decimal import _parse_decimal


def test_parse_decimal_rejects_unicode_digits() -> None:
    """Unicode digit characters are not valid protocol decimal tokens."""
    assert _parse_decimal("²") is None


def test_parse_decimal_rejects_overlong_ascii_decimal() -> None:
    """An ASCII-decimal run beyond CPython's int limit fails closed to None."""
    assert _parse_decimal("9" * 5000) is None


def test_parse_decimal_preserves_leading_zero_value_semantics() -> None:
    """Leading zeros are allowed by SIP CSeq grammar; compare by numeric value."""
    assert _parse_decimal("00000000001", max_exclusive=2**31) == 1
    assert _parse_decimal("0000000000002147483647", max_exclusive=2**31) == 2**31 - 1
    assert _parse_decimal("0000000000002147483648", max_exclusive=2**31) is None
