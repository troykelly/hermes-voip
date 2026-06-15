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
    ep.on_event(_offset(10))  # speech ended at window 10
    # advancing up to (10 + n - 1) must not fire yet
    assert not any(ep.advance(idx) for idx in range(11, 10 + n))
    # the window exactly n after the offset fires end-of-turn
    assert ep.advance(10 + n) is True


def test_end_of_turn_fires_exactly_once() -> None:
    ep = Endpointer(silence_ms=500, sample_rate_hz=_RATE_16K)
    n = ep.silence_windows
    ep.on_event(_onset(0))
    ep.on_event(_offset(10))
    assert ep.advance(10 + n) is True
    # subsequent advances in the same silent run do not re-fire
    assert not any(ep.advance(idx) for idx in range(10 + n + 1, 10 + n + 50))


def test_speech_resumption_resets_the_timer() -> None:
    ep = Endpointer(silence_ms=500, sample_rate_hz=_RATE_16K)
    n = ep.silence_windows
    ep.on_event(_onset(0))
    ep.on_event(_offset(10))
    # caller pauses then speaks again before the timer elapses
    assert not any(ep.advance(idx) for idx in range(11, 10 + n - 2))
    ep.on_event(_onset(10 + n - 2))  # resumed speech cancels the pending endpoint
    ep.on_event(_offset(20 + n))  # new pause begins here
    # the old deadline must NOT fire; only n windows after the *new* offset does
    assert not any(ep.advance(idx) for idx in range(20 + n + 1, 20 + n + n))
    assert ep.advance(20 + n + n) is True


def test_multiple_turns_each_fire_once() -> None:
    ep = Endpointer(silence_ms=500, sample_rate_hz=_RATE_16K)
    n = ep.silence_windows
    # turn 1
    ep.on_event(_onset(0))
    ep.on_event(_offset(5))
    assert ep.advance(5 + n) is True
    assert not ep.advance(5 + n + 1)
    # turn 2 begins later
    ep.on_event(_onset(200))
    ep.on_event(_offset(210))
    assert not any(ep.advance(idx) for idx in range(211, 210 + n))
    assert ep.advance(210 + n) is True


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
    ep.on_event(_offset(0))
    marks = [idx for idx in range(1, 5) if ep.advance(idx)]
    assert marks == [2]  # offset at 0 + 2 windows = window 2


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
