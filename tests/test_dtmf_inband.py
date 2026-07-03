"""Tests for in-band (Goertzel) DTMF — detection + tone generation (ADR-0010/0034).

The in-band backend is the last resort for a gateway that passes no
telephone-event and no SIP INFO. It runs Goertzel tone detection on decoded
G.711 PCM and synthesises dual-tone PCM for sending. Detection is a security
control (it can resolve an ADR-0009 confirmation), so the load-bearing tests
here are the false-positive rejecters: speech and white noise must NOT be
decoded as digits.
"""

from __future__ import annotations

import math
import struct

import pytest

from hermes_voip.dtmf import InbandDtmfDetector, inband_tone_pcm

_RATE = 8000  # G.711 narrowband; in-band is trusted only here (ADR-0010)
_FRAME = 160  # one 20 ms frame at 8 kHz


def _sine(freq: float, samples: int, *, amplitude: float = 0.3) -> bytes:
    """A pure sine of ``freq`` Hz, ``samples`` long, PCM16-LE at 8 kHz."""
    out = bytearray()
    scale = amplitude * 32767.0
    for n in range(samples):
        v = int(scale * math.sin(2 * math.pi * freq * n / _RATE))
        out += struct.pack("<h", max(-32768, min(32767, v)))
    return bytes(out)


def _silence(samples: int) -> bytes:
    return b"\x00\x00" * samples


def _feed_all(detector: InbandDtmfDetector, pcm: bytes) -> list[str]:
    """Feed ``pcm`` to the detector one 20 ms frame at a time; collect digits."""
    out: list[str] = []
    for off in range(0, len(pcm) - _FRAME * 2 + 1, _FRAME * 2):
        digit = detector.feed(pcm[off : off + _FRAME * 2])
        if digit is not None:
            out.append(digit)
    return out


# --- generation -------------------------------------------------------------


def test_inband_tone_is_dual_tone_pcm16() -> None:
    pcm = inband_tone_pcm("5", sample_rate=_RATE, duration_ms=100)
    # 100 ms at 8 kHz = 800 samples = 1600 bytes PCM16.
    assert len(pcm) == 800 * 2
    assert len(pcm) % 2 == 0


def test_inband_tone_rejects_non_dtmf_char() -> None:
    with pytest.raises(ValueError, match="DTMF"):
        inband_tone_pcm("Z", sample_rate=_RATE, duration_ms=100)


# --- the round trip: generated tone is detected -----------------------------


@pytest.mark.parametrize("digit", ["1", "2", "3", "4", "5", "6", "0", "*", "#"])
def test_generated_tone_is_detected(digit: str) -> None:
    detector = InbandDtmfDetector(sample_rate=_RATE)
    # A real keypress: tone then a gap of silence (so the press is debounced
    # and the trailing silence resets the detector).
    pcm = inband_tone_pcm(digit, sample_rate=_RATE, duration_ms=120) + _silence(
        _FRAME * 4
    )
    assert _feed_all(detector, pcm) == [digit]


def test_two_presses_of_same_digit_are_two_digits() -> None:
    detector = InbandDtmfDetector(sample_rate=_RATE)
    tone = inband_tone_pcm("7", sample_rate=_RATE, duration_ms=100)
    gap = _silence(_FRAME * 4)
    assert _feed_all(detector, tone + gap + tone + gap) == ["7", "7"]


def test_single_long_press_emits_once() -> None:
    detector = InbandDtmfDetector(sample_rate=_RATE)
    # One sustained 300 ms tone with no interior gap = ONE press.
    pcm = inband_tone_pcm("9", sample_rate=_RATE, duration_ms=300) + _silence(
        _FRAME * 4
    )
    assert _feed_all(detector, pcm) == ["9"]


@pytest.mark.parametrize("gap_frames", [1, 2])
def test_short_gap_mid_press_does_not_duplicate_digit(gap_frames: int) -> None:
    """A gap SHORTER than the release threshold must not re-arm the detector.

    One (or a couple of) lost or momentarily-silent frames in the middle of a held
    key-press — packet loss, an AEC transient, or a frame dipping below the energy
    floor — must NOT clear the emitted-digit state. Otherwise, when the SAME tone
    resumes, it emits a second, spurious digit: a duplicate DTMF digit, which is
    security-sensitive (a digit can resolve an ADR-0009 confirmation). Only a
    *sustained* gap (>= ``_INBAND_GAP_RELEASE_FRAMES``, 3 frames / 60 ms) is a real
    release; a 1-2 frame dropout is not, so the resumed tone stays one held press.
    """
    detector = InbandDtmfDetector(sample_rate=_RATE)
    tone = inband_tone_pcm("5", sample_rate=_RATE, duration_ms=100)  # 5 frames, emits
    dropout = _silence(_FRAME * gap_frames)  # a brief 1-2 frame gap, NOT a release
    release = _silence(_FRAME * 4)  # a genuine trailing release closes the stream
    assert _feed_all(detector, tone + dropout + tone + release) == ["5"]


# --- the load-bearing false-positive rejecters ------------------------------


def test_silence_yields_no_digit() -> None:
    detector = InbandDtmfDetector(sample_rate=_RATE)
    assert _feed_all(detector, _silence(_FRAME * 50)) == []


def test_white_noise_yields_no_digit() -> None:
    detector = InbandDtmfDetector(sample_rate=_RATE)
    # Deterministic pseudo-random noise at speech-like amplitude.
    rng = 0x1234_5678
    out = bytearray()
    for _ in range(_FRAME * 50):
        rng = (1103515245 * rng + 12345) & 0x7FFF_FFFF
        v = (rng % 20000) - 10000
        out += struct.pack("<h", v)
    assert _feed_all(detector, bytes(out)) == []


def test_single_tone_is_not_a_digit() -> None:
    # A single frequency (not a row+column pair) must never decode — a pure
    # 1000 Hz tone, or one DTMF row frequency alone, is not a keypress.
    detector = InbandDtmfDetector(sample_rate=_RATE)
    assert _feed_all(detector, _sine(1000.0, _FRAME * 20)) == []
    detector2 = InbandDtmfDetector(sample_rate=_RATE)
    assert _feed_all(detector2, _sine(697.0, _FRAME * 20)) == []


def test_voiced_speech_sweep_is_not_a_digit() -> None:
    # A voiced vowel-like signal: a fundamental + harmonics that sweep through
    # the DTMF band (formant glide). The second-harmonic + twist + duration
    # tests must reject it — a false digit here could resolve a confirmation.
    detector = InbandDtmfDetector(sample_rate=_RATE)
    out = bytearray()
    total = _FRAME * 40
    for n in range(total):
        # Fundamental glides 180 Hz -> 260 Hz; strong 2nd/3rd harmonics land
        # in the DTMF band and drift (unlike a steady DTMF pair).
        f0 = 180.0 + 80.0 * (n / total)
        v = (
            0.5 * math.sin(2 * math.pi * f0 * n / _RATE)
            + 0.4 * math.sin(2 * math.pi * 2 * f0 * n / _RATE)
            + 0.3 * math.sin(2 * math.pi * 3 * f0 * n / _RATE)
            + 0.2 * math.sin(2 * math.pi * 5 * f0 * n / _RATE)
        )
        s = int(0.3 * 32767.0 * v / 1.4)
        out += struct.pack("<h", max(-32768, min(32767, s)))
    assert _feed_all(detector, bytes(out)) == []


def test_harmonic_speech_landing_on_dtmf_pair_is_rejected() -> None:
    """A voiced source whose harmonics land on a DTMF pair is rejected (review #1).

    f0 = 189 Hz has a 4th harmonic near 770 (a low tone) and a 7th near 1323 (a high
    tone); a bare two-bin test could read this as a keypress. The harmonic-corroboration
    test rejects it because the voiced source ALSO carries energy at the second harmonic
    of each tone and the difference frequency, which a real DTMF generator never emits.
    """
    detector = InbandDtmfDetector(sample_rate=_RATE)
    out = bytearray()
    for n in range(_FRAME * 30):
        f0 = 189.0
        v = sum(
            amp * math.sin(2 * math.pi * h * f0 * n / _RATE)
            for h, amp in (
                (1, 0.6),
                (2, 0.4),
                (3, 0.3),
                (4, 0.55),
                (5, 0.25),
                (6, 0.2),
                (7, 0.5),
                (8, 0.15),
            )
        )
        s = int(0.3 * 32767.0 * v / 2.95)
        out += struct.pack("<h", max(-32768, min(32767, s)))
    assert _feed_all(detector, bytes(out)) == []


def test_mismatched_twist_pair_is_rejected() -> None:
    # A row+column pair where one tone is far louder than the other (twist far
    # beyond the allowed bound) is not a valid keypress.
    detector = InbandDtmfDetector(sample_rate=_RATE)
    out = bytearray()
    for n in range(_FRAME * 20):
        low = 0.9 * math.sin(2 * math.pi * 697 * n / _RATE)
        high = 0.02 * math.sin(2 * math.pi * 1209 * n / _RATE)
        v = int(0.3 * 32767.0 * (low + high))
        out += struct.pack("<h", max(-32768, min(32767, v)))
    assert _feed_all(detector, bytes(out)) == []
