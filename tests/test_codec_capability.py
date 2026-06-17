"""Engine codec-capability guard tests.

Operator invariant: never carry a codec we cannot actually encode/decode.

The engine's :class:`~hermes_voip.media.engine.Codec` enum is the single source
of truth for what the RTP media plane can carry.
:func:`~hermes_voip.media.engine.codec_for_encoding` is the exhaustive,
rate-aware bridge from an SDP ``(encoding, clock_rate)`` pair to that enum: it
returns the matching ``Codec`` for a carriable pair and RAISES
:class:`~hermes_voip.media.engine.UnsupportedCodecError` for anything else — no
silent catch-all default (AGENTS.md rule 17).

The adapter-level drift guard (every voice entry in ``_SUPPORTED_ENCODINGS`` maps
without raising) and the ``_to_engine_codec`` delegation live in
``tests/test_adapter.py`` because they import the optional-runtime adapter; the
mapping nucleus is tested HERE in the default gate (no ``hermes`` extra), fully
typed under ``mypy --strict``.

Fakes only — synthetic encoding names and RTP clock rates; no SIP host,
extension, or PII.
"""

from __future__ import annotations

import pytest

from hermes_voip.media.engine import Codec, UnsupportedCodecError, codec_for_encoding

# Telephony G.711 wire clock rate (RFC 3551 §6): both PCMU and PCMA run at 8 kHz.
_G711_RATE = 8000


# ---------------------------------------------------------------------------
# codec_for_encoding: exact, rate-aware mapping; raises (no silent default)
# ---------------------------------------------------------------------------


def test_pcmu_8000_maps_to_engine_pcmu() -> None:
    assert codec_for_encoding("PCMU", _G711_RATE) is Codec.PCMU


def test_pcma_8000_maps_to_engine_pcma() -> None:
    assert codec_for_encoding("PCMA", _G711_RATE) is Codec.PCMA


def test_encoding_name_match_is_case_insensitive() -> None:
    # SDP encoding names are compared case-insensitively (RFC 4566 is lenient).
    assert codec_for_encoding("pcmu", _G711_RATE) is Codec.PCMU
    assert codec_for_encoding("pcma", _G711_RATE) is Codec.PCMA


def test_unknown_encoding_raises_unsupported_codec() -> None:
    # G.729 is a real codec the engine cannot carry — it must RAISE, never silently
    # fall through to PCMA (the historical bug).
    with pytest.raises(UnsupportedCodecError):
        codec_for_encoding("G729", _G711_RATE)


def test_opus_raises_unsupported_codec() -> None:
    # Opus is out of scope for the current G.711-only engine; advertising it would
    # be a capability lie. It must RAISE.
    with pytest.raises(UnsupportedCodecError):
        codec_for_encoding("opus", 48000)


def test_correct_name_wrong_rate_raises() -> None:
    # CODEC AND RATE, not just name: a PCMU rtpmap at the wrong clock rate is not a
    # codec the 8 kHz G.711 engine can carry. (Guards the G.722 trap for later:
    # rtpmap clock 8000 but actual sample rate 16000 — rate matters.)
    with pytest.raises(UnsupportedCodecError):
        codec_for_encoding("PCMU", 16000)
    with pytest.raises(UnsupportedCodecError):
        codec_for_encoding("PCMA", 16000)


def test_unsupported_codec_error_is_value_error_subclass() -> None:
    # A capability mismatch is a value problem (a codec value we cannot accept),
    # so it composes with the existing ``except ValueError`` negotiation guard.
    assert issubclass(UnsupportedCodecError, ValueError)


def test_unsupported_codec_error_message_carries_encoding_and_rate() -> None:
    # The error names only the structural codec facts (encoding + rate) for
    # diagnostics — never a SIP host/extension/number.
    err = UnsupportedCodecError("G729", _G711_RATE)
    text = str(err)
    assert "G729" in text
    assert str(_G711_RATE) in text
