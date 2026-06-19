"""Tests for sans-IO call-progress detection — fax / AMD / beep (ADR-0064).

Two signal sources drive the detector and both are synthesised here with the
stdlib only (no scipy/numpy): decoded PCM frames (for the Goertzel fax + beep
tone tests) and ``VadEvent`` onset/offset edges (for the answering-machine
heuristic). The load-bearing tests are the NEGATIVE controls — ordinary voiced
speech must never decode as a fax tone or a beep, and a human "Hello? ... yes?"
cadence must never be classed as a machine — because a false fax/AMD verdict
silently kills a real conversation.
"""

from __future__ import annotations

import math
import struct

import pytest

from hermes_voip.media.call_progress import (
    AnsweringMachine,
    CallProgressDetector,
    CallProgressEvent,
    FaxCed,
    FaxCng,
    LikelyHuman,
    ReadyToLeaveMessage,
)
from hermes_voip.media.vad import SILERO_WINDOW_SAMPLES, SpeechEdge, VadEvent
from hermes_voip.providers.audio import PcmFrame

# The inbound conversational pipeline runs at 16 kHz (ADR-0017); the G.711 wire
# is 8 kHz. The detector is sample-rate parameterised — both rates are exercised.
_RATE_8K = 8000
_RATE_16K = 16000
_FRAME_MS = 20  # one RTP audio frame


def _frame_samples(rate: int) -> int:
    return (rate * _FRAME_MS) // 1000


def _sine_frame(
    freq: float, *, rate: int, ts_ns: int, amplitude: float = 0.5
) -> PcmFrame:
    """One 20 ms PCM16-LE mono frame of a pure ``freq`` Hz sine at ``rate``."""
    n = _frame_samples(rate)
    scale = amplitude * 32767.0
    out = bytearray()
    for k in range(n):
        v = int(scale * math.sin(2 * math.pi * freq * k / rate))
        out += struct.pack("<h", max(-32768, min(32767, v)))
    return PcmFrame(samples=bytes(out), sample_rate=rate, monotonic_ts_ns=ts_ns)


def _silence_frame(*, rate: int, ts_ns: int) -> PcmFrame:
    n = _frame_samples(rate)
    return PcmFrame(samples=b"\x00\x00" * n, sample_rate=rate, monotonic_ts_ns=ts_ns)


def _voiced_frame(f0: float, *, rate: int, ts_ns: int) -> PcmFrame:
    """A vowel-like frame: a fundamental plus a harmonic series (spread energy)."""
    n = _frame_samples(rate)
    out = bytearray()
    for k in range(n):
        v = (
            0.5 * math.sin(2 * math.pi * f0 * k / rate)
            + 0.4 * math.sin(2 * math.pi * 2 * f0 * k / rate)
            + 0.3 * math.sin(2 * math.pi * 3 * f0 * k / rate)
            + 0.2 * math.sin(2 * math.pi * 4 * f0 * k / rate)
            + 0.15 * math.sin(2 * math.pi * 5 * f0 * k / rate)
        )
        s = int(0.4 * 32767.0 * v / 1.55)
        out += struct.pack("<h", max(-32768, min(32767, s)))
    return PcmFrame(samples=bytes(out), sample_rate=rate, monotonic_ts_ns=ts_ns)


def _feed_tone(
    detector: CallProgressDetector,
    freq: float,
    *,
    rate: int,
    duration_ms: int,
    start_frame: int = 0,
) -> list[CallProgressEvent]:
    """Feed ``duration_ms`` of a pure tone frame-by-frame; collect emitted events."""
    out: list[CallProgressEvent] = []
    n_frames = duration_ms // _FRAME_MS
    ns_per_frame = _FRAME_MS * 1_000_000
    for i in range(n_frames):
        idx = start_frame + i
        ev = detector.on_audio_frame(
            _sine_frame(freq, rate=rate, ts_ns=idx * ns_per_frame)
        )
        if ev is not None:
            out.append(ev)
    return out


def _feed_silence(
    detector: CallProgressDetector, *, rate: int, duration_ms: int, start_frame: int = 0
) -> list[CallProgressEvent]:
    out: list[CallProgressEvent] = []
    n_frames = duration_ms // _FRAME_MS
    ns_per_frame = _FRAME_MS * 1_000_000
    for i in range(n_frames):
        idx = start_frame + i
        ev = detector.on_audio_frame(
            _silence_frame(rate=rate, ts_ns=idx * ns_per_frame)
        )
        if ev is not None:
            out.append(ev)
    return out


# --- VAD segment helpers ----------------------------------------------------
#
# silero stamps one 32 ms window ordinal per edge (256 samples at 8 kHz, 512 at
# 16 kHz — both 32 ms). A speech run of D ms starting at window W is an ONSET at
# W and an OFFSET at W + ceil(D / 32). The detector converts ordinals to seconds
# with the same window duration, so these helpers mirror that arithmetic.


def _windows_for(duration_ms: int, rate: int) -> int:
    window_ms = SILERO_WINDOW_SAMPLES[rate] / rate * 1000
    return max(1, round(duration_ms / window_ms))


def _w8(duration_ms: int) -> int:
    """Window ordinals spanning ``duration_ms`` at 8 kHz (256-sample windows)."""
    return _windows_for(duration_ms, _RATE_8K)


def _w16(duration_ms: int) -> int:
    """Window ordinals spanning ``duration_ms`` at 16 kHz (512-sample windows)."""
    return _windows_for(duration_ms, _RATE_16K)


def _onset(index: int) -> VadEvent:
    return VadEvent(edge=SpeechEdge.ONSET, frame_index=index, probability=0.9)


def _offset(index: int) -> VadEvent:
    return VadEvent(edge=SpeechEdge.OFFSET, frame_index=index, probability=0.1)


def _drive_vad(
    detector: CallProgressDetector, edges: list[VadEvent]
) -> list[CallProgressEvent]:
    out: list[CallProgressEvent] = []
    for edge in edges:
        ev = detector.on_vad_event(edge)
        if ev is not None:
            out.append(ev)
    return out


# ===========================================================================
# Fax CNG (1100 Hz, calling fax — inbound)
# ===========================================================================


@pytest.mark.parametrize("rate", [_RATE_8K, _RATE_16K])
def test_cng_1100hz_burst_detected_as_fax_cng(rate: int) -> None:
    # A calling fax: a ~0.5 s burst of 1100 Hz. One FaxCng, exactly once.
    detector = CallProgressDetector(sample_rate=rate, outbound=False)
    events = _feed_tone(detector, 1100.0, rate=rate, duration_ms=520)
    kinds = [e.kind for e in events]
    assert kinds.count("fax_cng") == 1
    fax = next(e for e in events if isinstance(e, FaxCng))
    assert fax.elapsed_s > 0.0


def test_cng_emits_only_once_across_a_full_cadence_cycle() -> None:
    # 0.5 s on, 3 s off, 0.5 s on — the burst-completed emit fires once and does
    # not re-fire on the second burst within the same call's detection.
    detector = CallProgressDetector(sample_rate=_RATE_8K, outbound=False)
    out = _feed_tone(detector, 1100.0, rate=_RATE_8K, duration_ms=520, start_frame=0)
    out += _feed_silence(detector, rate=_RATE_8K, duration_ms=3000, start_frame=26)
    out += _feed_tone(detector, 1100.0, rate=_RATE_8K, duration_ms=520, start_frame=176)
    assert [e.kind for e in out].count("fax_cng") == 1


# ===========================================================================
# Fax CED (2100 Hz, answering fax/modem — outbound)
# ===========================================================================


@pytest.mark.parametrize("rate", [_RATE_8K, _RATE_16K])
def test_ced_2100hz_sustained_detected_as_fax_ced(rate: int) -> None:
    # An answering fax: a sustained 2100 Hz tone (>= 1.0 s). One FaxCed.
    detector = CallProgressDetector(sample_rate=rate, outbound=True)
    events = _feed_tone(detector, 2100.0, rate=rate, duration_ms=1200)
    assert [e.kind for e in events].count("fax_ced") == 1
    ced = next(e for e in events if isinstance(e, FaxCed))
    assert ced.elapsed_s >= 1.0


def test_short_2100hz_blip_is_not_ced() -> None:
    # A 2100 Hz blip far shorter than the sustained-CED minimum is not a fax.
    detector = CallProgressDetector(sample_rate=_RATE_8K, outbound=True)
    events = _feed_tone(detector, 2100.0, rate=_RATE_8K, duration_ms=200)
    assert [e.kind for e in events] == []


# ===========================================================================
# Beep detection + leave-message protocol
# ===========================================================================


def test_beep_after_machine_greeting_emits_ready_to_leave_message() -> None:
    # Outbound, a realistic timeline: 4 s of greeting audio (with the VAD ONSET at
    # its start and OFFSET at its end), then ~0.5 s of post-greeting silence audio,
    # then a ~1000 Hz beep. The cue must fire on the BEEP (with beep_at_s set),
    # before the silence-fallback would. Audio + VAD are interleaved on one clock.
    detector = CallProgressDetector(sample_rate=_RATE_8K, outbound=True)
    events: list[CallProgressEvent] = []
    assert detector.on_vad_event(_onset(0)) is None
    events += _feed_tone(detector, 300.0, rate=_RATE_8K, duration_ms=4000)
    machine = detector.on_vad_event(_offset(_windows_for(4000, _RATE_8K)))
    assert isinstance(machine, AnsweringMachine)
    # 0.4 s of silence (below the 1.0 s fallback gap) then the beep.
    events += _feed_silence(detector, rate=_RATE_8K, duration_ms=400, start_frame=200)
    events += _feed_tone(
        detector, 1000.0, rate=_RATE_8K, duration_ms=300, start_frame=220
    )
    ready = [e for e in events if isinstance(e, ReadyToLeaveMessage)]
    assert len(ready) == 1
    assert ready[0].beep_at_s is not None


def test_machine_greeting_ends_then_silence_signals_ready_without_a_later_onset() -> (
    None
):
    # The PRODUCTION no-beep voicemail path: the machine greeting ends (VAD OFFSET),
    # the line goes silent, and NO further VAD edge ever arrives (the machine is now
    # recording, not speaking). The silence-fallback must fire on the AUDIO/sample
    # clock alone, emitting ReadyToLeaveMessage(beep_at_s=None) — never waiting for a
    # later ONSET that production will not send.
    detector = CallProgressDetector(sample_rate=_RATE_8K, outbound=True)
    events: list[CallProgressEvent] = []
    assert detector.on_vad_event(_onset(0)) is None
    events += _feed_tone(detector, 300.0, rate=_RATE_8K, duration_ms=4000)
    machine = detector.on_vad_event(_offset(_windows_for(4000, _RATE_8K)))
    assert isinstance(machine, AnsweringMachine)
    # Now ONLY silent audio frames, no further VAD edge. The fallback fires once the
    # post-greeting silence reaches the response gap (1.0 s).
    events += _feed_silence(detector, rate=_RATE_8K, duration_ms=2000, start_frame=200)
    ready = [e for e in events if isinstance(e, ReadyToLeaveMessage)]
    assert len(ready) == 1
    assert ready[0].beep_at_s is None


@pytest.mark.parametrize("audio_ms_at_offset", [3500, 4000, 4500])
def test_no_beep_fallback_is_independent_of_offset_delivery_order(
    audio_ms_at_offset: int,
) -> None:
    # The OFFSET's silence boundary is the VAD EDGE'S OWN time (window ordinal), not
    # "samples seen when on_vad_event happened" — so the no-beep cue fires at the same
    # elapsed time whether the OFFSET is delivered slightly BEFORE the matching audio
    # has caught up (3500 ms fed), exactly at it (4000 ms), or slightly AFTER (4500
    # ms). The greeting OFFSET edge is at window 4000 ms regardless; the cue must fire
    # at edge_time + response_gap = 5.0 s.
    detector = CallProgressDetector(sample_rate=_RATE_8K, outbound=True)
    assert detector.on_vad_event(_onset(0)) is None
    # Feed greeting audio up to the (possibly skewed) point the OFFSET is delivered.
    fed = _feed_tone(detector, 300.0, rate=_RATE_8K, duration_ms=audio_ms_at_offset)
    assert not any(isinstance(e, ReadyToLeaveMessage) for e in fed)
    # The OFFSET edge is always stamped at the true greeting end (window = 4000 ms).
    machine = detector.on_vad_event(_offset(_windows_for(4000, _RATE_8K)))
    assert isinstance(machine, AnsweringMachine)
    # Continue silent audio from where the audio clock currently is, well past 5 s.
    start = audio_ms_at_offset // _FRAME_MS
    events = _feed_silence(detector, rate=_RATE_8K, duration_ms=3000, start_frame=start)
    ready = [e for e in events if isinstance(e, ReadyToLeaveMessage)]
    assert len(ready) == 1
    assert ready[0].beep_at_s is None
    # Fires at edge_time (4.0 s) + response_gap (1.0 s) = 5.0 s, within one frame —
    # NOT keyed to when on_vad_event was called relative to the audio clock.
    assert ready[0].elapsed_s == pytest.approx(5.0, abs=_FRAME_MS / 1000)


def test_machine_resuming_speech_resets_the_no_beep_silence_fallback() -> None:
    # A two-part greeting ("Hi, you've reached … <pause> … leave a message"): the
    # fallback must NOT fire during the interior pause if the machine resumes
    # talking before the response gap elapses — only after the FINAL trailing
    # silence. The interior pause is shorter than the gap.
    detector = CallProgressDetector(sample_rate=_RATE_8K, outbound=True)
    events: list[CallProgressEvent] = []
    assert detector.on_vad_event(_onset(0)) is None
    events += _feed_tone(detector, 300.0, rate=_RATE_8K, duration_ms=4000)
    machine = detector.on_vad_event(_offset(_windows_for(4000, _RATE_8K)))
    assert isinstance(machine, AnsweringMachine)
    # A 0.6 s interior pause (below the 1.0 s gap), then the machine speaks again.
    events += _feed_silence(detector, rate=_RATE_8K, duration_ms=600, start_frame=200)
    resume_at = 200 + 30
    assert detector.on_vad_event(_onset(_windows_for(4600, _RATE_8K))) is None
    events += _feed_tone(
        detector, 300.0, rate=_RATE_8K, duration_ms=1000, start_frame=resume_at
    )
    # No cue yet — the interior pause was too short and speech resumed.
    assert not any(isinstance(e, ReadyToLeaveMessage) for e in events)
    # The machine finishes; the final silence reaches the gap and fires the cue.
    assert detector.on_vad_event(_offset(_windows_for(5600, _RATE_8K))) is None
    events += _feed_silence(detector, rate=_RATE_8K, duration_ms=1500, start_frame=280)
    ready = [e for e in events if isinstance(e, ReadyToLeaveMessage)]
    assert len(ready) == 1
    assert ready[0].beep_at_s is None


# ===========================================================================
# Answering-machine detection via VAD segments (outbound only)
# ===========================================================================


def test_short_greeting_then_pause_is_likely_human() -> None:
    # "Hello?" (~0.8 s) then a pause awaiting a response => LikelyHuman.
    detector = CallProgressDetector(sample_rate=_RATE_16K, outbound=True)
    offset_at = _w16(800)
    events = _drive_vad(
        detector,
        [_onset(0), _offset(offset_at), _onset(offset_at + _w16(1500))],
    )
    assert any(isinstance(e, LikelyHuman) for e in events)
    assert not any(isinstance(e, AnsweringMachine) for e in events)


def test_long_continuous_greeting_is_answering_machine() -> None:
    # A long uninterrupted greeting (~5 s) => AnsweringMachine, not human.
    detector = CallProgressDetector(sample_rate=_RATE_16K, outbound=True)
    events = _drive_vad(detector, [_onset(0), _offset(_w16(5000))])
    assert any(isinstance(e, AnsweringMachine) for e in events)
    assert not any(isinstance(e, LikelyHuman) for e in events)


def test_human_hello_yes_cadence_is_not_a_machine() -> None:
    # NEGATIVE CONTROL: "Hello? ... yes?" — two short utterances with a pause
    # between must classify human, never a machine.
    detector = CallProgressDetector(sample_rate=_RATE_16K, outbound=True)
    o2 = _w16(700) + _w16(1200)  # after "Hello?" + a pause
    events = _drive_vad(
        detector,
        [
            _onset(0),
            _offset(_w16(700)),  # "Hello?"
            _onset(o2),
            _offset(o2 + _w16(500)),  # "yes?"
        ],
    )
    assert not any(isinstance(e, AnsweringMachine) for e in events)
    assert any(isinstance(e, LikelyHuman) for e in events)


def test_amd_is_outbound_only_inbound_never_classifies() -> None:
    # On inbound the agent IS the answerer; AMD must not run.
    detector = CallProgressDetector(sample_rate=_RATE_16K, outbound=False)
    events = _drive_vad(detector, [_onset(0), _offset(_w16(5000))])
    assert not any(isinstance(e, AnsweringMachine) for e in events)
    assert not any(isinstance(e, LikelyHuman) for e in events)


# ===========================================================================
# Negative controls — ordinary speech must not trigger fax or beep
# ===========================================================================


@pytest.mark.parametrize("rate", [_RATE_8K, _RATE_16K])
def test_voiced_speech_is_not_a_fax_tone(rate: int) -> None:
    # A sustained vowel-like signal (fundamental + harmonics) must NOT decode as
    # CNG or CED — its energy is spread across the harmonic series.
    detector = CallProgressDetector(sample_rate=rate, outbound=True)
    out: list[CallProgressEvent] = []
    ns_per_frame = _FRAME_MS * 1_000_000
    for i in range(150):  # ~3 s of voiced speech
        f0 = 180.0 + 40.0 * math.sin(i / 12.0)  # a drifting fundamental
        ev = detector.on_audio_frame(
            _voiced_frame(f0, rate=rate, ts_ns=i * ns_per_frame)
        )
        if ev is not None:
            out.append(ev)
    assert [e.kind for e in out] == []


def test_silence_triggers_no_tone_event() -> None:
    detector = CallProgressDetector(sample_rate=_RATE_8K, outbound=True)
    assert _feed_silence(detector, rate=_RATE_8K, duration_ms=4000) == []


def test_beep_without_a_machine_classification_is_not_ready_to_leave() -> None:
    # A 1000 Hz tone with no preceding machine greeting must NOT yield a record
    # cue — the cue only follows an AnsweringMachine classification.
    detector = CallProgressDetector(sample_rate=_RATE_8K, outbound=True)
    events = _feed_tone(detector, 1000.0, rate=_RATE_8K, duration_ms=400)
    assert not any(isinstance(e, ReadyToLeaveMessage) for e in events)


def test_speech_band_tone_is_not_misread_as_fax() -> None:
    # A 1000 Hz beep tone is neither 1100 (CNG) nor 2100 (CED); it must not
    # produce a fax verdict even sustained.
    detector = CallProgressDetector(sample_rate=_RATE_8K, outbound=True)
    events = _feed_tone(detector, 1000.0, rate=_RATE_8K, duration_ms=1500)
    assert not any(e.kind in {"fax_cng", "fax_ced"} for e in events)


# ===========================================================================
# Sample-clock correctness (elapsed_s from accumulated samples, not frame count)
# ===========================================================================


def test_elapsed_s_tracks_accumulated_samples_not_frame_count() -> None:
    # elapsed_s must be (samples seen so far / rate), so variable-length frames and
    # a zero-length frame compute the exact wall position — NOT (frame count x the
    # latest frame duration), which diverges the moment frame sizes vary.
    detector = CallProgressDetector(sample_rate=_RATE_8K, outbound=True)
    # A leading zero-length frame (a real possibility on the wire) advances no time.
    assert (
        detector.on_audio_frame(
            PcmFrame(samples=b"", sample_rate=_RATE_8K, monotonic_ts_ns=0)
        )
        is None
    )
    # Feed sustained CED at 2100 Hz in 30 ms frames (240 samples) until it fires.
    n = (_RATE_8K * 30) // 1000
    scale = 0.5 * 32767.0
    ced: FaxCed | None = None
    samples_before_emit = 0
    for i in range(60):
        buf = bytearray()
        for k in range(n):
            v = int(scale * math.sin(2 * math.pi * 2100.0 * k / _RATE_8K))
            buf += struct.pack("<h", max(-32768, min(32767, v)))
        ev = detector.on_audio_frame(
            PcmFrame(
                samples=bytes(buf),
                sample_rate=_RATE_8K,
                monotonic_ts_ns=i * 30 * 1_000_000,
            )
        )
        if isinstance(ev, FaxCed):
            ced = ev
            samples_before_emit = (i + 1) * n
            break
    assert ced is not None
    # The reported elapsed equals the true accumulated-sample time (the zero-length
    # frame contributed nothing), to within one float epsilon.
    assert ced.elapsed_s == pytest.approx(samples_before_emit / _RATE_8K)


# ===========================================================================
# Construction + reset
# ===========================================================================


def test_rejects_non_positive_sample_rate() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        CallProgressDetector(sample_rate=0, outbound=True)


def test_reset_clears_state_so_a_second_call_reuses_the_detector() -> None:
    detector = CallProgressDetector(sample_rate=_RATE_8K, outbound=True)
    assert _feed_tone(detector, 2100.0, rate=_RATE_8K, duration_ms=1200)
    detector.reset()
    # After reset the CED run must re-accumulate from scratch: a short blip that
    # would not have qualified mid-run yields nothing.
    assert _feed_tone(detector, 2100.0, rate=_RATE_8K, duration_ms=200) == []
