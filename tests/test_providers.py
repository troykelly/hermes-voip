"""Validation tests for provider dataclasses (PcmFrame, Transcript, GuardResult).

Covers the __post_init__ guards added per backlog item providers/:
- PcmFrame: odd-length samples, zero/negative sample_rate
- Transcript.confidence: below 0.0, above 1.0, NaN, valid range
- GuardResult.score: below 0.0, above 1.0, NaN, inf, valid range (SECURITY-ADJACENT)
"""

from __future__ import annotations

import math

import pytest

from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardResult, GuardVerdict

# ---------------------------------------------------------------------------
# PcmFrame validation
# ---------------------------------------------------------------------------


class TestPcmFrameValidation:
    """PcmFrame.__post_init__ must reject malformed samples and sample rates."""

    def test_odd_length_samples_raises(self) -> None:
        """A single orphan byte is not a whole 16-bit sample."""
        with pytest.raises(ValueError, match="samples"):
            PcmFrame(samples=b"\x00", sample_rate=8000, monotonic_ts_ns=0)

    def test_three_byte_samples_raises(self) -> None:
        """3 bytes = 1.5 samples — also odd."""
        with pytest.raises(ValueError, match="samples"):
            PcmFrame(samples=b"\x00\x01\x02", sample_rate=8000, monotonic_ts_ns=0)

    def test_sample_rate_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            PcmFrame(samples=b"\x00\x00", sample_rate=0, monotonic_ts_ns=0)

    def test_sample_rate_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            PcmFrame(samples=b"\x00\x00", sample_rate=-8000, monotonic_ts_ns=0)

    # Valid cases must still construct without error.

    def test_even_length_samples_ok(self) -> None:
        frame = PcmFrame(
            samples=b"\x00\x00\x01\x01", sample_rate=8000, monotonic_ts_ns=0
        )
        assert frame.sample_count == 2

    def test_empty_samples_ok(self) -> None:
        """Empty frame is a valid comfort-noise or gap packet."""
        frame = PcmFrame(samples=b"", sample_rate=16000, monotonic_ts_ns=0)
        assert frame.sample_count == 0

    def test_positive_sample_rate_ok(self) -> None:
        PcmFrame(samples=b"\x00\x00", sample_rate=1, monotonic_ts_ns=0)

    def test_standard_rates_ok(self) -> None:
        for rate in (8000, 16000, 24000, 48000):
            PcmFrame(samples=b"\x00\x00", sample_rate=rate, monotonic_ts_ns=0)


# ---------------------------------------------------------------------------
# Transcript.confidence validation
# ---------------------------------------------------------------------------


class TestTranscriptConfidenceValidation:
    """Transcript.confidence must be a real number in [0.0, 1.0]."""

    def _make(self, confidence: float) -> Transcript:
        return Transcript(
            text="hello",
            is_final=True,
            end_of_turn=False,
            confidence=confidence,
        )

    def test_confidence_below_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            self._make(-0.1)

    def test_confidence_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            self._make(1.1)

    def test_confidence_nan_raises(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            self._make(float("nan"))

    def test_confidence_zero_ok(self) -> None:
        t = self._make(0.0)
        assert t.confidence == pytest.approx(0.0)

    def test_confidence_one_ok(self) -> None:
        t = self._make(1.0)
        assert t.confidence == pytest.approx(1.0)

    def test_confidence_mid_range_ok(self) -> None:
        t = self._make(0.75)
        assert t.confidence == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# GuardResult.score validation  (SECURITY-ADJACENT: ADR-0009 thresholds gate on it)
# ---------------------------------------------------------------------------


class TestGuardResultScoreValidation:
    """GuardResult.score must be a real number in [0.0, 1.0].

    A NaN or out-of-range score could silently defeat an ADR-0009 injection
    threshold (e.g. NaN comparisons are always False, so score > 0.7 would
    never trigger REFUSE).  Validation here is a defence-in-depth control.
    """

    def _make(self, score: float) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            normalized_text="hello",
            reasons=(),
            degraded=False,
            score=score,
        )

    def test_score_below_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="score"):
            self._make(-0.1)

    def test_score_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="score"):
            self._make(1.1)

    def test_score_nan_raises(self) -> None:
        with pytest.raises(ValueError, match="score"):
            self._make(float("nan"))

    def test_score_positive_inf_raises(self) -> None:
        with pytest.raises(ValueError, match="score"):
            self._make(math.inf)

    def test_score_negative_inf_raises(self) -> None:
        with pytest.raises(ValueError, match="score"):
            self._make(-math.inf)

    def test_score_zero_ok(self) -> None:
        r = self._make(0.0)
        assert r.score == pytest.approx(0.0)

    def test_score_one_ok(self) -> None:
        r = self._make(1.0)
        assert r.score == pytest.approx(1.0)

    def test_score_mid_range_ok(self) -> None:
        r = self._make(0.42)
        assert r.score == pytest.approx(0.42)
