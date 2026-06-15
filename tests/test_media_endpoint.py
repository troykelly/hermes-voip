"""Tests for hermes_voip.media.endpoint — silence-timer end-of-turn (ADR-0008).

The endpointer is a deterministic state machine driven by VAD edges and the
monotonic 16 kHz window ordinal (no wall clock), so the trailing-silence timer is
exact and unit-testable offline. End-of-turn fires once, after a turn has had
speech and then ``endpoint_silence_ms`` of trailing silence.
"""

from __future__ import annotations

import pytest

from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.vad import SpeechEdge, VadEvent

_RATE_16K = 16_000
# silero 16 kHz window = 512 samples = 32 ms; 500 ms ~= 15.625 windows -> 16.
_WINDOW_MS = 32.0


def _onset(idx: int) -> VadEvent:
    return VadEvent(edge=SpeechEdge.ONSET, frame_index=idx, probability=0.9)


def _offset(idx: int) -> VadEvent:
    return VadEvent(edge=SpeechEdge.OFFSET, frame_index=idx, probability=0.1)


def test_silence_windows_rounds_up_from_ms() -> None:
    ep = Endpointer(silence_ms=500, sample_rate_hz=_RATE_16K)
    # 500 ms / 32 ms = 15.625 -> 16 windows of trailing silence
    assert ep.silence_windows == 16


def test_no_end_of_turn_before_any_speech() -> None:
    ep = Endpointer(silence_ms=500, sample_rate_hz=_RATE_16K)
    # plenty of silent windows tick by, but no speech ever started
    fired = [ep.advance(idx) for idx in range(100)]
    assert not any(fired)


def test_end_of_turn_fires_after_trailing_silence() -> None:
    ep = Endpointer(silence_ms=500, sample_rate_hz=_RATE_16K)
    n = ep.silence_windows
    ep.on_event(_onset(0))
    ep.on_event(_offset(10))  # window 10 is the FIRST silent window
    # windows 10..10+n-1 inclusive are the n silent windows of the timer; the
    # turn ends on the LAST of them (10 + n - 1), not one past it. Advancing up
    # to 10 + n - 2 must not fire yet.
    assert not any(ep.advance(idx) for idx in range(11, 10 + n - 1))
    # the (n-1)th window after the offset completes n silent windows -> fire.
    assert ep.advance(10 + n - 1) is True


def test_end_of_turn_fires_on_exact_window_index() -> None:
    # Pin the EXACT firing ordinal against the VAD's OFFSET semantics: vad.py
    # emits OFFSET on the FIRST window whose probability is below exit, i.e. the
    # first silent window. With a silence budget of `n` windows, the n silent
    # windows are [offset, offset + n - 1]; end-of-turn fires on the last of
    # them (offset + n - 1) and on no earlier window. Use a small explicit n so
    # the index is concrete, not derived from silence_windows.
    ep = Endpointer(silence_ms=96, sample_rate_hz=_RATE_16K)  # ceil(96/32) = 3
    assert ep.silence_windows == 3
    offset = 40  # window 40 = first silent window
    ep.on_event(_onset(38))
    ep.on_event(_offset(offset))
    # the three silent windows are 40, 41, 42; fire on exactly window 42.
    fired_at = [idx for idx in range(offset + 1, offset + 10) if ep.advance(idx)]
    assert fired_at == [42]
    assert offset + ep.silence_windows - 1 == 42  # documents the exact relation


def test_end_of_turn_fires_exactly_once() -> None:
    ep = Endpointer(silence_ms=500, sample_rate_hz=_RATE_16K)
    n = ep.silence_windows
    ep.on_event(_onset(0))
    ep.on_event(_offset(10))
    assert ep.advance(10 + n - 1) is True
    # subsequent advances in the same silent run do not re-fire
    assert not any(ep.advance(idx) for idx in range(10 + n, 10 + n + 50))


def test_speech_resumption_resets_the_timer() -> None:
    ep = Endpointer(silence_ms=500, sample_rate_hz=_RATE_16K)
    n = ep.silence_windows
    ep.on_event(_onset(0))
    ep.on_event(_offset(10))
    # caller pauses then speaks again before the timer elapses
    assert not any(ep.advance(idx) for idx in range(11, 10 + n - 2))
    ep.on_event(_onset(10 + n - 2))  # resumed speech cancels the pending endpoint
    ep.on_event(_offset(20 + n))  # new pause begins here (first silent window)
    # the old deadline must NOT fire; only the (n-1)th window after the *new*
    # offset, completing n silent windows, does.
    assert not any(ep.advance(idx) for idx in range(20 + n + 1, 20 + n + n - 1))
    assert ep.advance(20 + n + n - 1) is True


def test_multiple_turns_each_fire_once() -> None:
    ep = Endpointer(silence_ms=500, sample_rate_hz=_RATE_16K)
    n = ep.silence_windows
    # turn 1
    ep.on_event(_onset(0))
    ep.on_event(_offset(5))  # window 5 is the first silent window
    assert ep.advance(5 + n - 1) is True
    assert not ep.advance(5 + n)
    # turn 2 begins later
    ep.on_event(_onset(200))
    ep.on_event(_offset(210))
    assert not any(ep.advance(idx) for idx in range(211, 210 + n - 1))
    assert ep.advance(210 + n - 1) is True


def test_reset_clears_pending_turn() -> None:
    ep = Endpointer(silence_ms=500, sample_rate_hz=_RATE_16K)
    n = ep.silence_windows
    ep.on_event(_onset(0))
    ep.on_event(_offset(10))
    ep.reset()
    # after reset, the pending deadline is gone and no speech is in progress
    assert not any(ep.advance(idx) for idx in range(11, 10 + n + 50))


def test_consume_events_yields_end_of_turn_marks() -> None:
    # the convenience driver: feed (event, current_index) pairs and collect the
    # window ordinals at which end-of-turn fired.
    ep = Endpointer(silence_ms=64, sample_rate_hz=_RATE_16K)  # 2 windows
    assert ep.silence_windows == 2
    ep.on_event(_onset(0))
    ep.on_event(_offset(0))  # window 0 is the first silent window
    marks = [idx for idx in range(1, 5) if ep.advance(idx)]
    # two silent windows are 0 and 1; the turn ends on window 1 (the second), not
    # window 2 (which would be three silent windows: 0, 1, 2).
    assert marks == [1]


def test_invalid_silence_ms_rejected() -> None:
    with pytest.raises(ValueError, match="silence_ms"):
        Endpointer(silence_ms=0, sample_rate_hz=_RATE_16K)


def test_invalid_rate_rejected() -> None:
    with pytest.raises(ValueError, match="8000 or 16000"):
        Endpointer(silence_ms=500, sample_rate_hz=22_050)


def test_advance_rejects_non_monotonic_index() -> None:
    ep = Endpointer(silence_ms=500, sample_rate_hz=_RATE_16K)
    ep.on_event(_onset(0))
    ep.advance(5)
    with pytest.raises(ValueError, match="monotonic"):
        ep.advance(4)
