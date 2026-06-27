"""Tests for hermes_voip._chars control-character detection."""

import pytest

from hermes_voip._chars import contains_control


@pytest.mark.parametrize("value", ["\x00", "\r", "\n", "\t", "\x7f"])
def test_contains_control_rejects_c0_and_del(value: str) -> None:
    assert contains_control(value) is True


@pytest.mark.parametrize("value", ["ASCII text 123", "Ā"])
def test_contains_control_allows_printable_text(value: str) -> None:
    assert contains_control(value) is False
