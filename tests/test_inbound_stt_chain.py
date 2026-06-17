"""RED test — inbound STT end_of_turn never delivered (ADR-0008 bug).

Root cause (proved by this test before the fix):
  ``CallLoop._asr()`` only forwards a transcript when
  ``transcript.is_final and transcript.end_of_turn``.  But ``SherpaOnnxASR``
  ALWAYS yields ``end_of_turn=False`` (ADR-0008: the endpointer owns the turn
  boundary, not the recogniser).  And ``_pump()`` calls
  ``self._endpointer.advance(window_index)`` but discards its return value, so
  the endpointer's True signal never reaches the ASR task.  Result: no
  transcript is EVER forwarded and ``deliver_turn`` is never called.

Fix (ADR-0008 §wiring):
  An ``asyncio.Event`` ``_eot`` is shared between ``_pump`` and ``_asr`` inside
  ``run()``.  When ``endpointer.advance()`` returns ``True``, ``_pump`` sets the
  event.  ``_asr`` checks it on each ``is_final=True`` transcript: if set,
  clears it and treats the transcript as end-of-turn regardless of the
  recogniser's own ``end_of_turn`` field.

Test structure:
  - ``_SpeechThenSilenceVAD``: a fake VAD model that returns ``1.0`` (speech)
    for the first ``N`` windows then ``0.0`` (silence) — simulates a caller
    saying something then going quiet.  This is enough for the real endpointer
    to arm its silence timer and eventually fire.
  - ``_EotFalseASR``: fake ASR that always sets ``end_of_turn=False`` (exactly
    what ``SherpaOnnxASR`` does per ADR-0008).  Drains audio to avoid blocking
    the pump's bounded queue, then yields exactly one ``is_final=True`` transcript.
  - The test drives a ``CallLoop`` with a real ``Endpointer`` and asserts
    ``deliver_turn`` is called with the transcript text.

The test is written to run in the BASE gate (no ml/numpy dependencies):
  - VAD uses a pure-Python callable model; no SileroVAD import.
  - ASR uses a fake; no SherpaOnnxASR import.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from hermes_voip.media.call_loop import CallLoop
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.vad import VoiceActivityDetector
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.providers.tts import TtsStream

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CALL_ID = "test-inbound-stt-001"
_VOICE = "fake-voice"
_SAMPLE_RATE = 8_000
# One silero window at 8 kHz = 256 samples = 512 bytes
# (from vad.py SILERO_WINDOW_SAMPLES)
_WINDOW_SAMPLES = 256
_WINDOW_BYTES = _WINDOW_SAMPLES * 2  # PCM16


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _window_frame(index: int, *, speech: bool) -> PcmFrame:
    """An 8 kHz PCM16 frame exactly one silero window wide.

    Non-zero bytes for "speech" (the VAD model looks at the actual PCM in
    real use, but our fake model ignores the content and uses a counter).
    Zero bytes for silence.
    """
    if speech:
        samples = bytes([0x10, 0x01] * (_WINDOW_SAMPLES // 2))
    else:
        samples = bytes(_WINDOW_BYTES)
    return PcmFrame(
        samples=samples,
        sample_rate=_SAMPLE_RATE,
        monotonic_ts_ns=index * (_WINDOW_SAMPLES * 1_000_000_000 // _SAMPLE_RATE),
    )


# ---------------------------------------------------------------------------
# Fake VAD: speaks for the first ``_SPEECH_WINDOWS`` windows, then silence
# ---------------------------------------------------------------------------

_SPEECH_WINDOWS = 5  # 5 x 32 ms = 160 ms of speech
# At 8 kHz with silence_ms=500 ms → ceiling(500/32) = 16 silence windows needed.
# We drive 20 silence windows so the endpointer definitely fires.
_SILENCE_WINDOWS = 20
_TOTAL_WINDOWS = _SPEECH_WINDOWS + _SILENCE_WINDOWS


class _SpeechThenSilenceModel:
    """VAD model callable: 1.0 for the first ``_SPEECH_WINDOWS`` calls, 0.0 after."""

    def __init__(self) -> None:
        self._count = 0

    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        self._count += 1
        return 1.0 if self._count <= _SPEECH_WINDOWS else 0.0


# ---------------------------------------------------------------------------
# Fake transport: yields ``_TOTAL_WINDOWS`` frames at 8 kHz then ends
# ---------------------------------------------------------------------------


class _FiniteTransport:
    """MediaTransport fake: yields a fixed sequence of 8 kHz PCM frames."""

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = frames
        self.sent_audio: list[PcmFrame] = []

    @property
    def inbound_sample_rate(self) -> int:
        return _SAMPLE_RATE

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        frames = self._frames

        async def _gen() -> AsyncIterator[PcmFrame]:
            for frame in frames:
                yield frame

        return _gen()

    async def send_audio(self, frame: PcmFrame) -> None:
        self.sent_audio.append(frame)

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fake ASR: always yields ``end_of_turn=False`` (like SherpaOnnxASR)
# ---------------------------------------------------------------------------


class _EotFalseTranscriptIter:
    """Drains audio (to unblock the pump), then yields one is_final=True transcript.

    The critical invariant: ``end_of_turn`` is always ``False``, exactly as
    ``SherpaOnnxASR`` behaves per ADR-0008.  The transcript must only be
    delivered if the endpointer's signal reaches the ASR task through a side
    channel (the ``_eot`` event wired by the fix).
    """

    def __init__(self, audio: AsyncIterator[PcmFrame], text: str) -> None:
        self._audio = audio
        self._text = text
        self._yielded = False

    def __aiter__(self) -> _EotFalseTranscriptIter:
        return self

    async def __anext__(self) -> Transcript:
        if not self._yielded:
            # Drain all audio first so the pump's audio_q never fills up.
            async for _ in self._audio:
                pass
            self._yielded = True
            # yield one final transcript — but with end_of_turn=False, as
            # SherpaOnnxASR always does.
            return Transcript(
                text=self._text,
                is_final=True,
                end_of_turn=False,  # <-- this is the bug: always False
                confidence=0.95,
            )
        raise StopAsyncIteration


class _EotFalseASR:
    """StreamingASR fake that always emits end_of_turn=False (ADR-0008 contract)."""

    def __init__(self, text: str = "hello from the caller") -> None:
        self._text = text

    @property
    def input_sample_rate(self) -> int:
        return _SAMPLE_RATE

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        return _EotFalseTranscriptIter(audio, self._text)


# ---------------------------------------------------------------------------
# Fake TTS: no-op
# ---------------------------------------------------------------------------


class _SilentTtsStream:
    """A TtsStream that yields no frames."""

    def __aiter__(self) -> _SilentTtsStream:
        return self

    async def __anext__(self) -> PcmFrame:
        raise StopAsyncIteration

    async def flush(self) -> None:
        pass

    async def cancel(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _NoopTTS:
    @property
    def output_sample_rate(self) -> int:
        return _SAMPLE_RATE

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        return _SilentTtsStream()


# ---------------------------------------------------------------------------
# Fake guard: always ALLOW
# ---------------------------------------------------------------------------


class _AllowGuard:
    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            normalized_text=text,
            reasons=(),
            degraded=False,
            score=0.0,
        )


# ---------------------------------------------------------------------------
# THE RED TEST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpointer_eot_signal_reaches_asr_and_delivers_turn() -> None:
    """Endpointer end-of-turn fires → deliver_turn is called with the transcript.

    This test FAILS before the fix because:
    1. ``_EotFalseASR`` always sets ``end_of_turn=False`` (SherpaOnnxASR contract).
    2. ``_pump()`` calls ``endpointer.advance()`` but discards its return value.
    3. ``_asr()`` gate: ``transcript.is_final and transcript.end_of_turn`` — never
       True → ``deliver_turn`` never called.

    After the fix:
    1. ``_pump()`` sets ``_eot`` event when ``endpointer.advance()`` returns True.
    2. ``_asr()`` checks ``_eot`` on each ``is_final`` transcript, clears it, and
       delivers the transcript.
    3. ``deliver_turn`` IS called with "hello from the caller".
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    # Build the speech-then-silence frame sequence so the endpointer fires.
    frames = [
        _window_frame(i, speech=(i < _SPEECH_WINDOWS)) for i in range(_TOTAL_WINDOWS)
    ]

    vad_model = _SpeechThenSilenceModel()
    vad = VoiceActivityDetector(model=vad_model, sample_rate_hz=_SAMPLE_RATE)
    # 500 ms silence → ceil(500 / 32) = 16 windows; we drive 20 so it definitely fires.
    endpointer = Endpointer(silence_ms=500, sample_rate_hz=_SAMPLE_RATE)
    asr = _EotFalseASR("hello from the caller")
    transport = _FiniteTransport(frames)

    loop = CallLoop(
        transport=transport,
        asr=asr,
        tts=_NoopTTS(),
        guard=_AllowGuard(),
        vad=vad,
        endpointer=endpointer,
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=capture,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="",
    )

    await asyncio.wait_for(loop.run(), timeout=10.0)

    assert delivered == ["hello from the caller"], (
        f"deliver_turn was not called — the endpointer's end-of-turn signal was lost. "
        f"Got: {delivered!r}"
    )


@pytest.mark.asyncio
async def test_eot_not_delivered_when_endpointer_never_fires() -> None:
    """Sanity: endpointer never fires → no transcript delivered.

    If the endpointer never fires (pure silence, no speech onset),
    no transcript is delivered even though the ASR yields is_final=True.

    This guards the fix: we must not deliver every is_final transcript, only
    those where the endpointer (or the ASR itself) signals end_of_turn.
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    # All-silence frames: the endpointer never fires because there was no speech onset.
    frames = [_window_frame(i, speech=False) for i in range(_TOTAL_WINDOWS)]

    def _always_silent(window_pcm16: bytes, sample_rate: int) -> float:
        return 0.0

    vad = VoiceActivityDetector(model=_always_silent, sample_rate_hz=_SAMPLE_RATE)
    endpointer = Endpointer(silence_ms=500, sample_rate_hz=_SAMPLE_RATE)
    asr = _EotFalseASR("this should not be delivered")
    transport = _FiniteTransport(frames)

    loop = CallLoop(
        transport=transport,
        asr=asr,
        tts=_NoopTTS(),
        guard=_AllowGuard(),
        vad=vad,
        endpointer=endpointer,
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=capture,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="",
    )

    await asyncio.wait_for(loop.run(), timeout=10.0)

    assert delivered == [], (
        f"deliver_turn should NOT be called when endpointer never fires. "
        f"Got: {delivered!r}"
    )
