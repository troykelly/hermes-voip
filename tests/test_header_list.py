"""Tests for the shared comma-combined SIP header splitter (RFC 3261 §7.3.1)."""

from __future__ import annotations

from hermes_voip._header_list import split_header_list


def test_splits_top_level_commas_between_name_addrs() -> None:
    value = "<sip:p1.example.test;lr>, <sip:p2.example.test;lr>"
    assert split_header_list(value) == [
        "<sip:p1.example.test;lr>",
        "<sip:p2.example.test;lr>",
    ]


def test_comma_inside_angle_brackets_is_not_a_separator() -> None:
    value = "<sip:p1.example.test;lr;foo=a,b>, <sip:p2.example.test;lr>"
    assert split_header_list(value) == [
        "<sip:p1.example.test;lr;foo=a,b>",
        "<sip:p2.example.test;lr>",
    ]


def test_comma_inside_quoted_display_name_is_not_a_separator() -> None:
    value = '"Sales, East" <sip:p1.example.test;lr>, <sip:p2.example.test;lr>'
    assert split_header_list(value) == [
        '"Sales, East" <sip:p1.example.test;lr>',
        "<sip:p2.example.test;lr>",
    ]


def test_escaped_quote_in_display_name_does_not_end_the_quoted_string() -> None:
    """RFC 3261 quoted-pair: ``\\"`` is a literal quote, not a string terminator.

    A naive toggle on every ``"`` ends the quoted display-name early, so a comma
    inside it is wrongly treated as a top-level separator. The escaped quotes here
    keep the whole display-name (incl. its comma) inside one entry.
    """
    value = (
        r'"Sales \"East, West\"" <sip:p1.example.test;lr>, <sip:p2.example.test;lr>'
    )
    assert split_header_list(value) == [
        r'"Sales \"East, West\"" <sip:p1.example.test;lr>',
        "<sip:p2.example.test;lr>",
    ]
