"""Tests for SIP INFO DTMF body parsing + building (ADR-0010/0034).

SIP INFO carries a DTMF digit in an in-dialog INFO request body in one of two
formats: ``application/dtmf-relay`` (``Signal=`` / ``Duration=`` lines) and bare
``application/dtmf`` (the digit alone). The codec parses both to a keypad digit
and builds the relay body for sending.
"""

from __future__ import annotations

import pytest

from hermes_voip.dtmf_sipinfo import (
    DTMF_RELAY_CONTENT_TYPE,
    build_dtmf_relay_body,
    parse_dtmf_info,
)


def test_parse_dtmf_relay_signal_and_duration() -> None:
    body = "Signal=5\r\nDuration=160\r\n"
    assert parse_dtmf_info("application/dtmf-relay", body) == "5"


def test_parse_dtmf_relay_star_and_hash() -> None:
    assert parse_dtmf_info("application/dtmf-relay", "Signal=*\r\n") == "*"
    assert parse_dtmf_info("application/dtmf-relay", "Signal=#\r\n") == "#"


def test_parse_dtmf_relay_numeric_star_hash_codes() -> None:
    # Some gateways encode * and # as 10 and 11 in Signal=.
    assert parse_dtmf_info("application/dtmf-relay", "Signal=10\r\n") == "*"
    assert parse_dtmf_info("application/dtmf-relay", "Signal=11\r\n") == "#"


def test_parse_dtmf_relay_letters() -> None:
    assert parse_dtmf_info("application/dtmf-relay", "Signal=A\r\nDuration=100") == "A"


def test_parse_dtmf_relay_case_insensitive_key() -> None:
    assert parse_dtmf_info("application/dtmf-relay", "signal=3\r\n") == "3"


def test_parse_bare_dtmf_body() -> None:
    assert parse_dtmf_info("application/dtmf", "7") == "7"
    assert parse_dtmf_info("application/dtmf", "  9 \r\n") == "9"


def test_parse_content_type_with_params() -> None:
    # A Content-Type may carry parameters / casing.
    assert (
        parse_dtmf_info("Application/DTMF-Relay; charset=utf-8", "Signal=2\r\n") == "2"
    )


def test_parse_non_dtmf_content_type_is_none() -> None:
    assert parse_dtmf_info("application/sdp", "v=0\r\n") is None


def test_parse_missing_signal_is_none() -> None:
    assert parse_dtmf_info("application/dtmf-relay", "Duration=160\r\n") is None


def test_parse_invalid_signal_is_none() -> None:
    assert parse_dtmf_info("application/dtmf-relay", "Signal=Z\r\n") is None
    assert parse_dtmf_info("application/dtmf-relay", "Signal=99\r\n") is None


def test_parse_empty_bare_body_is_none() -> None:
    assert parse_dtmf_info("application/dtmf", "   ") is None


def test_build_relay_body_round_trips() -> None:
    body = build_dtmf_relay_body("5", duration_ms=160)
    assert parse_dtmf_info(DTMF_RELAY_CONTENT_TYPE, body) == "5"
    assert "Signal=5" in body
    assert "Duration=160" in body


def test_build_relay_body_star_hash() -> None:
    assert "Signal=*" in build_dtmf_relay_body("*", duration_ms=100)
    assert "Signal=#" in build_dtmf_relay_body("#", duration_ms=100)


def test_build_relay_body_rejects_non_dtmf() -> None:
    with pytest.raises(ValueError, match="DTMF"):
        build_dtmf_relay_body("Z", duration_ms=100)


def test_content_type_constant() -> None:
    assert DTMF_RELAY_CONTENT_TYPE == "application/dtmf-relay"
