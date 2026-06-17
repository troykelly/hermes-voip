"""Tests for the vendored pure-Python G.722 codec (ADR-0022).

The codec is asserted **bit-exact** against known-answer vectors produced by the
public-domain ITU G.722 reference (see ``tests/g722_kat_vectors.py`` and
``tests/fixtures/regen_g722_kat.py`` for provenance), plus round-trip fidelity on
an independent signal. These run in the DEFAULT (no-extra) gate: the codec is
pure Python with no optional dependency.
"""

from __future__ import annotations

import math
import struct

import pytest

from hermes_voip.media.g722 import (
    G722_RTP_CLOCK_RATE,
    G722_SAMPLE_RATE,
    G722Decoder,
    G722Encoder,
)
from tests.g722_kat_vectors import (
    kat_decoded_samples,
    kat_g722_bytes,
    kat_input_pcm16,
)


def _pcm16(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _unpack(pcm16: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(pcm16) // 2}h", pcm16))


def test_rates_match_rfc3551() -> None:
    # G.722 audio is sampled at 16 kHz but its RTP clock is 8 kHz (RFC 3551).
    assert G722_SAMPLE_RATE == 16_000
    assert G722_RTP_CLOCK_RATE == 8_000


def test_encode_is_bit_exact_with_reference() -> None:
    # The pure-Python encoder must produce byte-identical output to the
    # public-domain reference for the canonical input (320 samples -> 160 bytes).
    encoded = G722Encoder().encode(kat_input_pcm16)
    assert encoded == kat_g722_bytes
    assert len(encoded) == len(kat_input_pcm16) // 2 // 2  # 1 byte per sample-pair


def test_decode_is_bit_exact_with_reference() -> None:
    # The pure-Python decoder must produce sample-identical output to the
    # public-domain reference for the reference bitstream (160 bytes -> 320 samples).
    decoded = G722Decoder().decode(kat_g722_bytes)
    assert _unpack(decoded) == list(kat_decoded_samples)
    assert len(decoded) == len(kat_g722_bytes) * 2 * 2  # 2 samples per byte


def test_encode_rejects_odd_sample_count() -> None:
    # G.722 consumes input two samples at a time (the QMF split); an odd PCM16
    # sample count is a programming error, not silently truncated.
    with pytest.raises(ValueError, match="even"):
        G722Encoder().encode(_pcm16([1, 2, 3]))


def test_encode_rejects_non_pcm16_byte_length() -> None:
    with pytest.raises(ValueError, match="16-bit"):
        G722Encoder().encode(b"\x00\x00\x00")  # 3 bytes: not whole samples


def _max_xcorr(a: list[int], b: list[int], max_lag: int) -> float:
    """Best normalised cross-correlation of ``a`` vs ``b`` over lags 0..max_lag.

    G.722's QMF has a group delay (~38 samples), so the decoded signal is
    time-shifted relative to the input. A lag-0 correlation on a tone would
    measure that phase offset, not fidelity — so we maximise over a small lag
    window, which is the honest reconstruction-quality metric for a codec with
    delay (the public-domain reference itself scores ~0.997 here).
    """
    best = 0.0
    for lag in range(max_lag + 1):
        aa = a[: len(a) - lag]
        bb = b[lag:]
        m = min(len(aa), len(bb))
        aa, bb = aa[:m], bb[:m]
        dot = sum(x * y for x, y in zip(aa, bb, strict=True))
        ea = math.sqrt(sum(x * x for x in aa)) or 1.0
        eb = math.sqrt(sum(y * y for y in bb)) or 1.0
        best = max(best, dot / (ea * eb))
    return best


def test_round_trip_preserves_a_wideband_tone() -> None:
    # An independent signal (a 5 kHz tone — strictly in the G.722 upper band,
    # ABOVE the 4 kHz G.711 ceiling) survives encode->decode with high fidelity.
    # This is the wideband payoff: a 5 kHz component cannot be carried by 8 kHz
    # G.711 at all, but G.722 reconstructs it. A lossy ADPCM codec never
    # reproduces samples exactly, so we test delay-tolerant correlation + energy
    # retention, not equality.
    n = 1600  # 100 ms at 16 kHz
    src = [
        int(0.5 * 32767 * math.sin(2 * math.pi * 5000 * i / 16_000)) for i in range(n)
    ]
    encoded = G722Encoder().encode(_pcm16(src))
    decoded = _unpack(G722Decoder().decode(encoded))
    assert len(decoded) == n

    # Drop the codec warm-up, then correlate over the QMF group-delay window.
    lead = 64
    a, b = src[lead:], decoded[lead:]
    correlation = _max_xcorr(a, b, max_lag=40)
    assert correlation > 0.95, f"5 kHz tone correlation too low: {correlation:.3f}"
    # The decoded energy is within a reasonable band of the source energy (the
    # 5 kHz tone is reconstructed, not collapsed to silence as G.711 would).
    ea = math.sqrt(sum(x * x for x in a)) or 1.0
    eb = math.sqrt(sum(y * y for y in b)) or 1.0
    assert 0.5 < (eb / ea) < 1.5, f"energy ratio out of band: {eb / ea:.3f}"


def test_round_trip_silence_stays_quiet() -> None:
    # All-zero input round-trips to near-silence (no DC offset / runaway predictor).
    n = 640
    encoded = G722Encoder().encode(_pcm16([0] * n))
    decoded = _unpack(G722Decoder().decode(encoded))
    assert max(abs(s) for s in decoded) <= 4, "silence produced audible output"


def test_encoder_and_decoder_are_independently_stateful() -> None:
    # Encoding the input in two halves (one encoder carrying state across the
    # boundary) yields the same bytes as encoding it in one pass — the adaptive
    # predictor + QMF history must persist across calls on one instance.
    enc = G722Encoder()
    half = len(kat_input_pcm16) // 2
    # Split on a sample-pair boundary (4 bytes = 2 samples) so each half is whole.
    half -= half % 4
    part1 = enc.encode(kat_input_pcm16[:half])
    part2 = enc.encode(kat_input_pcm16[half:])
    assert part1 + part2 == kat_g722_bytes
