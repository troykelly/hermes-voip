"""TDD tests for the call-progress detector wired into the CallLoop pump (#43).

The sans-IO :class:`~hermes_voip.media.call_progress.CallProgressDetector` is
merged (ADR-0064, PR #149) but the live wiring is here: the CallLoop pump feeds
every decoded inbound :class:`PcmFrame` to ``on_audio_frame`` and every VAD edge
to ``on_vad_event``, surfacing each emitted :class:`CallProgressEvent` through a
new ``call_progress_callback``.

These tests drive the REAL detector through the REAL pump (an integration of the
seam), with a controllable transport (preset inbound frames) and a controllable
VAD model (so the speech/silence segments that feed AMD are deterministic). The
callback records every surfaced event.

Load-bearing NEGATIVE CONTROLS (rule 18, the operator's hard requirement):

* ``test_voiced_human_speech_never_triggers_fax`` — a vowel-like harmonic frame
  stream (a real human "aaah") must NEVER surface a FaxCng/FaxCed: the fax
  Goertzel rejecter sees spread harmonic energy, not a sustained pure tone.
* ``test_voiced_human_short_greeting_then_pause_is_human_not_amd`` — a short
  voiced greeting followed by a pause on an OUTBOUND call must surface
  ``LikelyHuman`` and NEVER ``AnsweringMachine`` / ``ReadyToLeaveMessage``: a
  live human is not a machine and the agent must not start leaving a message.

All fakes are synchronous; no real timing, threads, or network.
"""

from __future__ import annotations

import math
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final

import pytest

from hermes_voip.media.call_loop import CallLoop
from hermes_voip.media.call_progress import (
    AnsweringMachine,
    CallProgressDetector,
    CallProgressEvent,
    FaxCed,
    FaxCng,
    LikelyHuman,
    ReadyToLeaveMessage,
)
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.vad import VoiceActivityDetector
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.providers.tts import TtsStream

_CALL_ID: Final[str] = "call-cp-001"
_VOICE: Final[str] = "fake-voice"
_RATE: Final[int] = 8000
_FRAME_MS: Final[int] = 20


# ---------------------------------------------------------------------------
# Frame generators (8 kHz wire rate — the engine's inbound_sample_rate)
# ---------------------------------------------------------------------------


def _frame_samples() -> int:
    return (_RATE * _FRAME_MS) // 1000


def _sine_frame(freq: float, *, ts_ns: int, amplitude: float = 0.5) -> PcmFrame:
    """One 20 ms PCM16-LE mono frame of a pure ``freq`` Hz sine."""
    n = _frame_samples()
    scale = amplitude * 32767.0
    out = bytearray()
    for k in range(n):
        v = int(scale * math.sin(2 * math.pi * freq * k / _RATE))
        out += struct.pack("<h", max(-32768, min(32767, v)))
    return PcmFrame(samples=bytes(out), sample_rate=_RATE, monotonic_ts_ns=ts_ns)


def _silence_frame(*, ts_ns: int) -> PcmFrame:
    n = _frame_samples()
    return PcmFrame(samples=b"\x00\x00" * n, sample_rate=_RATE, monotonic_ts_ns=ts_ns)


def _voiced_frame(f0: float, *, ts_ns: int) -> PcmFrame:
    """A vowel-like frame: a fundamental plus a harmonic series (spread energy)."""
    n = _frame_samples()
    out = bytearray()
    for k in range(n):
        v = (
            0.5 * math.sin(2 * math.pi * f0 * k / _RATE)
            + 0.4 * math.sin(2 * math.pi * 2 * f0 * k / _RATE)
            + 0.3 * math.sin(2 * math.pi * 3 * f0 * k / _RATE)
            + 0.2 * math.sin(2 * math.pi * 4 * f0 * k / _RATE)
            + 0.15 * math.sin(2 * math.pi * 5 * f0 * k / _RATE)
        )
        s = int(0.4 * 32767.0 * v / 1.55)
        out += struct.pack("<h", max(-32768, min(32767, s)))
    return PcmFrame(samples=bytes(out), sample_rate=_RATE, monotonic_ts_ns=ts_ns)


def _tone_frames(freq: float, *, duration_ms: int, start_idx: int) -> list[PcmFrame]:
    ns = _FRAME_MS * 1_000_000
    return [
        _sine_frame(freq, ts_ns=(start_idx + i) * ns)
        for i in range(duration_ms // _FRAME_MS)
    ]


def _silence_frames(*, duration_ms: int, start_idx: int) -> list[PcmFrame]:
    ns = _FRAME_MS * 1_000_000
    return [
        _silence_frame(ts_ns=(start_idx + i) * ns)
        for i in range(duration_ms // _FRAME_MS)
    ]


def _voiced_frames(f0: float, *, duration_ms: int, start_idx: int) -> list[PcmFrame]:
    ns = _FRAME_MS * 1_000_000
    return [
        _voiced_frame(f0, ts_ns=(start_idx + i) * ns)
        for i in range(duration_ms // _FRAME_MS)
    ]


# ---------------------------------------------------------------------------
# Fakes (mirrors tests/test_call_loop.py)
# ---------------------------------------------------------------------------


class _FakeTransport:
    """MediaTransport fake driving a preset 8 kHz inbound frame sequence."""

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = frames
        self.sent_audio: list[PcmFrame] = []
        self.flush_calls = 0
        self.last_flush_fade_ms: int | None = None

    @property
    def inbound_sample_rate(self) -> int:
        return _RATE

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        frames = self._frames

        async def _gen() -> AsyncIterator[PcmFrame]:
            for frame in frames:
                yield frame

        return _gen()

    async def send_audio(self, frame: PcmFrame) -> None:
        self.sent_audio.append(frame)

    async def flush_outbound(self, *, fade_ms: int) -> None:
        self.flush_calls += 1
        self.last_flush_fade_ms = fade_ms

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass


class _EmptyTtsStream:
    """A TtsStream that yields no frames (no greeting audio in these tests)."""

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        async def _gen() -> AsyncIterator[PcmFrame]:
            return
            yield  # pragma: no cover - makes this an async generator

        return _gen()

    async def flush(self) -> None:
        pass

    async def cancel(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _FakeTTS:
    """StreamingTTS fake that yields nothing (no greeting audio in these tests)."""

    @property
    def output_sample_rate(self) -> int:
        return _RATE

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        _ = text, voice, sample_rate
        return _EmptyTtsStream()


class _FakeASR:
    """StreamingASR fake: drains audio, yields no transcripts."""

    @property
    def input_sample_rate(self) -> int:
        return _RATE

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        async def _gen() -> AsyncIterator[Transcript]:
            async for _ in audio:
                pass
            return
            yield  # pragma: no cover - makes this an async generator

        return _gen()


class _FakeGuard:
    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        _ = text, call_id
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            normalized_text="",
            reasons=(),
            degraded=False,
            score=0.0,
        )


class _ScriptedVadModel:
    """A VAD model returning a preset probability per scored window.

    The pump scores one window per ``SILERO_WINDOW_SAMPLES`` (256 at 8 kHz) of
    fed audio. The script lists probabilities in window order; windows past the
    script return the last value (steady state). This lets a test sculpt the
    exact ONSET/OFFSET edge sequence the AMD segment classifier sees.
    """

    def __init__(self, probabilities: list[float]) -> None:
        self._probs = probabilities
        self._i = 0

    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        _ = window_pcm16, sample_rate
        if self._i < len(self._probs):
            p = self._probs[self._i]
        else:
            p = self._probs[-1] if self._probs else 0.0
        self._i += 1
        return p


def _silent_vad() -> VoiceActivityDetector:
    return VoiceActivityDetector(model=lambda _w, _r: 0.0, sample_rate_hz=_RATE)


def _make_endpointer() -> Endpointer:
    return Endpointer(silence_ms=500, sample_rate_hz=_RATE)


async def _noop(text: str) -> None:
    _ = text


def _build_loop(
    transport: _FakeTransport,
    *,
    detector: CallProgressDetector | None,
    on_progress: Callable[[CallProgressEvent], Awaitable[None]] | None,
    vad: VoiceActivityDetector | None = None,
) -> CallLoop:
    return CallLoop(
        transport=transport,
        asr=_FakeASR(),
        tts=_FakeTTS(),
        guard=_FakeGuard(),
        vad=vad or _silent_vad(),
        endpointer=_make_endpointer(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=_noop,
        voice=_VOICE,
        call_id=_CALL_ID,
        call_progress_detector=detector,
        call_progress_callback=on_progress,
    )


def _collect_callback() -> tuple[
    list[CallProgressEvent], Callable[[CallProgressEvent], Awaitable[None]]
]:
    events: list[CallProgressEvent] = []

    async def _cb(event: CallProgressEvent) -> None:
        events.append(event)

    return events, _cb


# ===========================================================================
# Positive: fax CNG on the audio path surfaces through the callback
# ===========================================================================


@pytest.mark.asyncio
async def test_inbound_fax_cng_surfaces_through_callback() -> None:
    """A calling-fax CNG burst (1100 Hz) on inbound audio surfaces a FaxCng."""
    frames = _tone_frames(1100.0, duration_ms=600, start_idx=0)
    transport = _FakeTransport(frames)
    detector = CallProgressDetector(sample_rate=_RATE, outbound=False)
    events, cb = _collect_callback()
    loop = _build_loop(transport, detector=detector, on_progress=cb)

    await loop.run()

    assert any(isinstance(e, FaxCng) for e in events), (
        f"expected a FaxCng surfaced, got {[type(e).__name__ for e in events]}"
    )


# ===========================================================================
# Positive: fax CED on an outbound call surfaces through the callback
# ===========================================================================


@pytest.mark.asyncio
async def test_outbound_fax_ced_surfaces_through_callback() -> None:
    """An answering-fax CED tone (2100 Hz, sustained) surfaces a FaxCed."""
    frames = _tone_frames(2100.0, duration_ms=1200, start_idx=0)
    transport = _FakeTransport(frames)
    detector = CallProgressDetector(sample_rate=_RATE, outbound=True)
    events, cb = _collect_callback()
    loop = _build_loop(transport, detector=detector, on_progress=cb)

    await loop.run()

    assert any(isinstance(e, FaxCed) for e in events), (
        f"expected a FaxCed surfaced, got {[type(e).__name__ for e in events]}"
    )


# ===========================================================================
# Positive: AMD on an outbound call surfaces AnsweringMachine via the VAD path
# ===========================================================================


@pytest.mark.asyncio
async def test_outbound_long_greeting_surfaces_answering_machine() -> None:
    """A long continuous greeting (>= machine threshold) surfaces AnsweringMachine.

    The scripted VAD returns 1.0 for enough windows that the speech run exceeds
    the 3.5 s machine threshold, then 0.0 so the OFFSET closes the run. The pump
    feeds those VAD edges to ``on_vad_event``; the AMD classifier fires
    AnsweringMachine, which surfaces through the callback.
    """
    # 4 s of voiced audio (200 frames at 20 ms) then 0.6 s silence. The VAD scores
    # one window per 256 samples = 32 ms; 4 s -> 125 voiced windows then silence.
    voiced = _voiced_frames(180.0, duration_ms=4000, start_idx=0)
    tail = _silence_frames(duration_ms=600, start_idx=200)
    transport = _FakeTransport(voiced + tail)
    # 125 windows of speech (>= 3.5 s) then silence windows.
    probs = [1.0] * 125 + [0.0] * 40
    vad = VoiceActivityDetector(model=_ScriptedVadModel(probs), sample_rate_hz=_RATE)
    detector = CallProgressDetector(sample_rate=_RATE, outbound=True)
    events, cb = _collect_callback()
    loop = _build_loop(transport, detector=detector, on_progress=cb, vad=vad)

    await loop.run()

    assert any(isinstance(e, AnsweringMachine) for e in events), (
        f"expected an AnsweringMachine surfaced, got "
        f"{[type(e).__name__ for e in events]}"
    )


# ===========================================================================
# Positive: the no-beep ReadyToLeaveMessage fallback fires on the audio clock
# ===========================================================================


@pytest.mark.asyncio
async def test_outbound_machine_then_silence_surfaces_ready_to_leave_message() -> None:
    """After an AnsweringMachine verdict, sustained silence surfaces the record cue.

    The machine greeting ends and the line goes silent with NO beep — the
    audio-clock no-beep fallback must surface ReadyToLeaveMessage once the silence
    exceeds the response gap. This proves the pump keeps feeding ``on_audio_frame``
    THROUGH the post-greeting silence (not gated off when the caller stops).
    """
    voiced = _voiced_frames(180.0, duration_ms=4000, start_idx=0)
    # 2 s of trailing silence — well past the 1.0 s response gap.
    tail = _silence_frames(duration_ms=2000, start_idx=200)
    transport = _FakeTransport(voiced + tail)
    probs = [1.0] * 125 + [0.0] * 80
    vad = VoiceActivityDetector(model=_ScriptedVadModel(probs), sample_rate_hz=_RATE)
    detector = CallProgressDetector(sample_rate=_RATE, outbound=True)
    events, cb = _collect_callback()
    loop = _build_loop(transport, detector=detector, on_progress=cb, vad=vad)

    await loop.run()

    assert any(isinstance(e, ReadyToLeaveMessage) for e in events), (
        f"expected a ReadyToLeaveMessage (no-beep fallback) surfaced, got "
        f"{[type(e).__name__ for e in events]}"
    )


# ===========================================================================
# NEGATIVE CONTROL 1: voiced human speech must NEVER false-trigger a fax event
# ===========================================================================


@pytest.mark.asyncio
async def test_voiced_human_speech_never_triggers_fax() -> None:
    """A human 'aaah' (harmonic spread) must surface NO fax event, either direction.

    The fax Goertzel rejecter requires a sustained PURE tone holding most of a
    frame's energy; voiced speech spreads energy across a harmonic series and
    never clears the single-bin fraction floor. Run it on an OUTBOUND call (both
    CNG and CED detectors live).
    """
    frames = _voiced_frames(180.0, duration_ms=3000, start_idx=0)
    transport = _FakeTransport(frames)
    detector = CallProgressDetector(sample_rate=_RATE, outbound=True)
    events, cb = _collect_callback()
    loop = _build_loop(transport, detector=detector, on_progress=cb)

    await loop.run()

    fax = [e for e in events if isinstance(e, (FaxCng, FaxCed))]
    assert fax == [], f"voiced human speech false-triggered fax: {fax!r}"


# ===========================================================================
# NEGATIVE CONTROL 2: a short human greeting then a pause is LikelyHuman, not AMD
# ===========================================================================


@pytest.mark.asyncio
async def test_voiced_human_short_greeting_then_pause_is_human_not_amd() -> None:
    """A short greeting + a response pause surfaces LikelyHuman, NEVER AMD/record cue.

    On an outbound call a real person says a short "Hello?" then waits. The AMD
    classifier must read that as a human (short speech < 2 s then a >= 1 s pause),
    surface LikelyHuman, and NEVER surface AnsweringMachine or ReadyToLeaveMessage
    — the agent must not start leaving a voicemail to a live human.
    """
    # ~0.8 s of voiced greeting (40 frames) then 1.5 s silence.
    voiced = _voiced_frames(180.0, duration_ms=800, start_idx=0)
    tail = _silence_frames(duration_ms=1500, start_idx=40)
    transport = _FakeTransport(voiced + tail)
    # 25 windows of speech (~0.8 s < 2 s human threshold) then silence windows.
    probs = [1.0] * 25 + [0.0] * 60
    vad = VoiceActivityDetector(model=_ScriptedVadModel(probs), sample_rate_hz=_RATE)
    detector = CallProgressDetector(sample_rate=_RATE, outbound=True)
    events, cb = _collect_callback()
    loop = _build_loop(transport, detector=detector, on_progress=cb, vad=vad)

    await loop.run()

    assert any(isinstance(e, LikelyHuman) for e in events), (
        f"expected LikelyHuman, got {[type(e).__name__ for e in events]}"
    )
    bad = [e for e in events if isinstance(e, (AnsweringMachine, ReadyToLeaveMessage))]
    assert bad == [], f"a live human was misclassified as a machine: {bad!r}"


# ===========================================================================
# Disabled: no detector => no callback, no crash (feature off by default)
# ===========================================================================


@pytest.mark.asyncio
async def test_no_detector_means_no_call_progress_callback() -> None:
    """With no detector wired, the callback never fires even on a fax tone."""
    frames = _tone_frames(1100.0, duration_ms=600, start_idx=0)
    transport = _FakeTransport(frames)
    events, cb = _collect_callback()
    loop = _build_loop(transport, detector=None, on_progress=cb)

    await loop.run()

    assert events == [], f"callback fired with no detector: {events!r}"
