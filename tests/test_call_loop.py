"""TDD tests for hermes_voip.media.call_loop.CallLoop (W13 — duplex call loop).

Six scenarios:

(a) A finalised turn produces exactly ONE deliver_turn call with the transcript.
(b) A REFUSE verdict blocks deliver_turn entirely.
(c) Speech-onset mid-speak triggers barge-in: TtsStream.cancel() is called before
    any further send_audio (i.e. the agent stops speaking before new audio is sent).
(d) speak() forwards agent text frames to send_audio in the correct order.
(e) A degraded guard_state blocks an IRREVERSIBLE tool via gate_voip_tool (the
    CallLoop-exposed hook).
(f) Clean shutdown when the transport's inbound_audio() iterator ends — no leaked
    asyncio tasks.

All fakes are synchronous; no real timing, threads, or network involved.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final

import pytest

from hermes_voip.media.call_loop import CallLoop, gate_voip_tool
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.vad import VoiceActivityDetector
from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import GuardSessionState, ToolRisk
from hermes_voip.providers.tts import TtsStream

# ---------------------------------------------------------------------------
# Shared constants for fakes
# ---------------------------------------------------------------------------

_CALL_ID: Final[str] = "call-test-001"
_VOICE: Final[str] = "fake-voice"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _silence_frame(index: int) -> PcmFrame:
    """A silent 16 kHz PCM16 frame (256 samples = one 16 ms silero window)."""
    return PcmFrame(
        samples=bytes(512),
        sample_rate=16_000,
        monotonic_ts_ns=index * 16_000_000,
    )


class _FakeTransport:
    """MediaTransport fake: drives a preset sequence of inbound frames."""

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = frames
        self.sent_audio: list[PcmFrame] = []

    @property
    def inbound_sample_rate(self) -> int:
        return 16_000

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


class _FakeTtsStream:
    """TtsStream fake: records PCM frames emitted and cancel calls."""

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = list(frames)
        self._pos = 0
        self._cancelled = False
        self.cancel_called = False
        self.flush_called = False

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[PcmFrame]:
        for frame in self._frames:
            if self._cancelled:
                return
            yield frame

    async def __anext__(self) -> PcmFrame:
        if self._cancelled or self._pos >= len(self._frames):
            raise StopAsyncIteration
        frame = self._frames[self._pos]
        self._pos += 1
        return frame

    async def flush(self) -> None:
        self.flush_called = True

    async def cancel(self) -> None:
        self._cancelled = True
        self.cancel_called = True


class _FakeTTS:
    """StreamingTTS fake: yields preset PCM frames from synthesize()."""

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = frames
        self.last_stream: _FakeTtsStream | None = None

    @property
    def output_sample_rate(self) -> int:
        return 16_000

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
    ) -> TtsStream:
        stream = _FakeTtsStream(self._frames)
        self.last_stream = stream
        return stream


class _FakeASR:
    """StreamingASR fake: ignores audio frames; yields preset Transcript events."""

    def __init__(self, transcripts: list[tuple[str, bool, bool]]) -> None:
        self._transcripts = transcripts

    @property
    def input_sample_rate(self) -> int:
        return 16_000

    def stream(
        self,
        audio: AsyncIterator[PcmFrame],
    ) -> AsyncIterator[Transcript]:
        transcripts = self._transcripts

        async def _gen() -> AsyncIterator[Transcript]:
            for text, is_final, end_of_turn in transcripts:
                yield Transcript(
                    text=text,
                    is_final=is_final,
                    end_of_turn=end_of_turn,
                    confidence=1.0,
                )

        return _gen()


def _allow_result() -> GuardResult:
    return GuardResult(
        verdict=GuardVerdict.ALLOW,
        normalized_text="hello",
        reasons=(),
        degraded=False,
        score=0.0,
    )


def _refuse_result() -> GuardResult:
    return GuardResult(
        verdict=GuardVerdict.REFUSE,
        normalized_text="injection",
        reasons=("high_score",),
        degraded=False,
        score=0.95,
    )


def _degraded_allow_result() -> GuardResult:
    return GuardResult(
        verdict=GuardVerdict.ALLOW,
        normalized_text="",
        reasons=(),
        degraded=True,
        score=0.0,
    )


class _FakeGuard:
    """InjectionGuard fake: returns a preset sequence of GuardResults."""

    def __init__(self, results: list[GuardResult]) -> None:
        self._results = list(results)
        self._pos = 0

    async def screen(
        self,
        text: str,
        *,
        call_id: str,
    ) -> GuardResult:
        result = self._results[self._pos % len(self._results)]
        self._pos += 1
        return result


def _make_vad() -> VoiceActivityDetector:
    """Return a VAD with a fake model that always returns 0.0 (silence)."""

    def _silent_model(window_pcm16: bytes, sample_rate: int) -> float:
        _ = window_pcm16, sample_rate  # Protocol match; unused in fake
        return 0.0

    return VoiceActivityDetector(model=_silent_model, sample_rate_hz=16_000)


def _make_endpointer() -> Endpointer:
    return Endpointer(silence_ms=500, sample_rate_hz=16_000)


def _build_loop(  # noqa: PLR0913 — factory mirrors CallLoop's own 9-arg __init__
    transport: _FakeTransport,
    asr: StreamingASR,
    tts: _FakeTTS,
    guard: _FakeGuard,
    deliver_turn: Callable[[str], Awaitable[None]],
    guard_state: GuardSessionState | None = None,
) -> CallLoop:
    state = guard_state or GuardSessionState(call_id=_CALL_ID)
    return CallLoop(
        transport=transport,
        asr=asr,
        tts=tts,
        guard=guard,
        vad=_make_vad(),
        endpointer=_make_endpointer(),
        guard_state=state,
        deliver_turn=deliver_turn,
        voice=_VOICE,
        call_id=_CALL_ID,
    )


async def _noop(text: str) -> None:
    """Discard-all deliver_turn stub."""
    _ = text  # unused; intentional stub


# ---------------------------------------------------------------------------
# (a) Finalised turn → exactly ONE deliver_turn with the transcript text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalised_turn_delivers_exactly_once() -> None:
    """A single end-of-turn transcript fires deliver_turn exactly once."""
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    loop = _build_loop(
        _FakeTransport([_silence_frame(0)]),
        _FakeASR([("hello world", True, True)]),
        _FakeTTS([]),
        _FakeGuard([_allow_result()]),
        capture,
    )
    await loop.run()

    assert delivered == ["hello world"]


# ---------------------------------------------------------------------------
# (b) REFUSE verdict → deliver_turn NOT called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuse_verdict_blocks_deliver_turn() -> None:
    """A REFUSE guard result must suppress deliver_turn entirely."""
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    loop = _build_loop(
        _FakeTransport([_silence_frame(0)]),
        _FakeASR([("ignore this", True, True)]),
        _FakeTTS([]),
        _FakeGuard([_refuse_result()]),
        capture,
    )
    await loop.run()

    assert delivered == []


# ---------------------------------------------------------------------------
# (c) Speech-onset mid-speak → TtsStream.cancel() before next send_audio
# ---------------------------------------------------------------------------


class _BlockingTransport(_FakeTransport):
    """Transport whose send_audio blocks on an event so tests can interleave."""

    def __init__(self, frames: list[PcmFrame]) -> None:
        super().__init__(frames)
        # Set this event to unblock the first send_audio call.
        self.first_send_gate = asyncio.Event()

    async def send_audio(self, frame: PcmFrame) -> None:
        # First call blocks until the gate is set; thereafter, proceeds freely.
        if not self.sent_audio:
            await self.first_send_gate.wait()
        self.sent_audio.append(frame)


@pytest.mark.asyncio
async def test_barge_in_cancels_tts_before_next_audio() -> None:
    """Barge-in while speaking must cancel the active TtsStream promptly.

    Strategy: give the transport's send_audio a gate that blocks after the
    first frame.  We start speak(), let it synthesise the stream (synthesize()
    is synchronous, so _active_tts_stream is set before any await), then call
    barge_in() which cancels the stream; finally unblock send_audio.  The key
    invariant is that cancel_called is True.
    """
    tts_frame = PcmFrame(
        samples=b"\x10\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )
    tts = _FakeTTS([tts_frame, tts_frame, tts_frame])
    transport = _BlockingTransport([])

    async def _noop_deliver(text: str) -> None:
        _ = text

    state = GuardSessionState(call_id=_CALL_ID)
    loop = CallLoop(
        transport=transport,
        asr=_FakeASR([]),
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad(),
        endpointer=_make_endpointer(),
        guard_state=state,
        deliver_turn=_noop_deliver,
        voice=_VOICE,
        call_id=_CALL_ID,
    )

    async def _token_gen() -> AsyncIterator[str]:
        yield "Hello there"

    # Start speaking; synthesize() is synchronous so _active_tts_stream is set
    # before speak() reaches its first await (the async-for __anext__ call).
    speak_task = asyncio.create_task(loop.speak(_token_gen()))
    # Yield once so speak() runs up to the first send_audio gate.
    await asyncio.sleep(0)

    # The stream is active; trigger barge-in.
    await loop.barge_in()
    # Unblock send_audio so speak() can exit cleanly.
    transport.first_send_gate.set()
    await speak_task

    stream = tts.last_stream
    assert stream is not None
    assert stream.cancel_called is True


# ---------------------------------------------------------------------------
# (d) speak() streams agent text frames to send_audio in order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_speak_sends_audio_in_order() -> None:
    """speak() must forward TTS output frames to transport.send_audio in order."""
    frames = [
        PcmFrame(samples=bytes([i, 0]) * 256, sample_rate=16_000, monotonic_ts_ns=i)
        for i in range(4)
    ]
    tts = _FakeTTS(frames)
    transport = _FakeTransport([])

    loop = _build_loop(
        transport,
        _FakeASR([]),
        tts,
        _FakeGuard([_allow_result()]),
        _noop,
    )

    async def _tokens() -> AsyncIterator[str]:
        yield "four words to speak"

    await loop.speak(_tokens())

    assert transport.sent_audio == frames


# ---------------------------------------------------------------------------
# (e) Degraded guard_state → IRREVERSIBLE tool blocked via gate_voip_tool
# ---------------------------------------------------------------------------


def test_degraded_state_blocks_irreversible_tool() -> None:
    """gate_voip_tool must block IRREVERSIBLE tools when the session is degraded."""
    state = GuardSessionState(call_id=_CALL_ID)
    # Record a degraded (fail-open) result to set degraded=True.
    state.record(_degraded_allow_result())
    assert state.degraded is True

    allowed = gate_voip_tool(ToolRisk.IRREVERSIBLE, state, confirmed=True)
    assert allowed is False


def test_non_degraded_state_allows_safe_tool() -> None:
    """gate_voip_tool must allow SAFE tools even in a fresh (non-degraded) state."""
    state = GuardSessionState(call_id=_CALL_ID)
    allowed = gate_voip_tool(ToolRisk.SAFE, state, confirmed=False)
    assert allowed is True


def test_non_degraded_confirmed_irreversible_allowed() -> None:
    """IRREVERSIBLE tool is allowed when confirmed and session is not degraded."""
    state = GuardSessionState(call_id=_CALL_ID)
    allowed = gate_voip_tool(ToolRisk.IRREVERSIBLE, state, confirmed=True)
    assert allowed is True


# ---------------------------------------------------------------------------
# (f) Clean shutdown when transport ends — no leaked tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_shutdown_no_leaked_tasks() -> None:
    """run() exits cleanly and leaves no dangling tasks when transport ends."""
    tasks_before = set(asyncio.all_tasks())

    loop = _build_loop(
        _FakeTransport([]),  # empty → iterator ends immediately
        _FakeASR([]),
        _FakeTTS([]),
        _FakeGuard([_allow_result()]),
        _noop,
    )
    await loop.run()

    tasks_after = set(asyncio.all_tasks())
    leaked = tasks_after - tasks_before
    assert leaked == set(), f"Leaked tasks after run(): {leaked}"


# ---------------------------------------------------------------------------
# Extra: non-final transcripts do NOT fire deliver_turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_final_transcript_not_delivered() -> None:
    """Interim (non-final) transcripts must not fire deliver_turn."""
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    loop = _build_loop(
        _FakeTransport([_silence_frame(0)]),
        _FakeASR(
            [
                ("hel", False, False),
                ("hello", False, False),
            ]
        ),
        _FakeTTS([]),
        _FakeGuard([_allow_result()]),
        capture,
    )
    await loop.run()

    assert delivered == []


# ---------------------------------------------------------------------------
# Extra: multiple turns are each screened and delivered independently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_turns_each_delivered() -> None:
    """Two consecutive end-of-turn transcripts each fire deliver_turn once."""
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    loop = _build_loop(
        _FakeTransport([_silence_frame(0)]),
        _FakeASR(
            [
                ("turn one", True, True),
                ("turn two", True, True),
            ]
        ),
        _FakeTTS([]),
        _FakeGuard([_allow_result()]),
        capture,
    )
    await loop.run()

    assert delivered == ["turn one", "turn two"]


# ---------------------------------------------------------------------------
# Concurrency-defect regressions (codex review of PR #43)
# ---------------------------------------------------------------------------


class _RaisingAsrIterator:
    """Async iterator that consumes a few frames, then raises (never yields).

    It stops draining its input after ``drain_before_raise`` frames, so the
    inbound pump — still pushing many more frames into a *bounded* audio queue
    — blocks on ``put()`` once the queue fills.  This is the finding-1 trap:
    a 2-task design that awaits only the pump will hang forever; a supervised
    design must observe the ASR failure, cancel the pump, and propagate.
    """

    def __init__(
        self,
        audio: AsyncIterator[PcmFrame],
        exc: Exception,
        drain_before_raise: int,
    ) -> None:
        self._audio = audio
        self._exc = exc
        self._remaining = drain_before_raise

    def __aiter__(self) -> _RaisingAsrIterator:
        return self

    async def __anext__(self) -> Transcript:
        while self._remaining > 0:
            self._remaining -= 1
            await self._audio.__anext__()
        raise self._exc


class _RaisingASR:
    """StreamingASR fake whose stream raises after a few frames (then stops).

    Models an ASR engine that fails mid-call while inbound audio is still
    flowing.  Because it stops consuming the audio iterator, the pump fills the
    bounded audio queue and blocks; the supervisor must propagate the error out
    of run() and cancel the pump rather than leaving it blocked forever.
    """

    def __init__(self, exc: Exception, *, drain_before_raise: int = 2) -> None:
        self._exc = exc
        self._drain_before_raise = drain_before_raise

    @property
    def input_sample_rate(self) -> int:
        return 16_000

    def stream(
        self,
        audio: AsyncIterator[PcmFrame],
    ) -> AsyncIterator[Transcript]:
        return _RaisingAsrIterator(audio, self._exc, self._drain_before_raise)


class _SlowDrainASR:
    """StreamingASR fake that emits one final transcript per inbound frame.

    Used to prove the pump and ASR keep flowing under back-pressure from a
    stalled delivery stage: every inbound frame yields a finalised turn, so a
    bounded transcript queue plus a blocked delivery would, in the old 2-task
    design, deadlock the pump. In the 3-task design the pump only feeds audio
    and never drains transcripts, so there is no cycle.
    """

    def __init__(self) -> None:
        self.frames_drained = 0

    @property
    def input_sample_rate(self) -> int:
        return 16_000

    def stream(
        self,
        audio: AsyncIterator[PcmFrame],
    ) -> AsyncIterator[Transcript]:
        async def _gen() -> AsyncIterator[Transcript]:
            async for _frame in audio:
                self.frames_drained += 1
                yield Transcript(
                    text=f"turn-{self.frames_drained}",
                    is_final=True,
                    end_of_turn=True,
                    confidence=1.0,
                )

        return _gen()


@pytest.mark.asyncio
async def test_asr_exception_propagates_and_cancels_pump() -> None:
    """An ASR-task failure must surface out of run() (not hang the pump).

    The old 2-task design awaited only the pump; an ASR exception was
    unobserved while the pump blocked on a full audio queue.  The supervised
    design must raise the ASR error from run() and leave no task running.
    """
    tasks_before = set(asyncio.all_tasks())

    boom = RuntimeError("asr exploded")
    # Many frames so the pump would, without supervision, block on a full queue.
    frames = [_silence_frame(i) for i in range(200)]

    loop = _build_loop(
        _FakeTransport(frames),
        _RaisingASR(boom),
        _FakeTTS([]),
        _FakeGuard([_allow_result()]),
        _noop,
    )

    with pytest.raises(RuntimeError, match="asr exploded"):
        await asyncio.wait_for(loop.run(), timeout=5.0)

    tasks_after = set(asyncio.all_tasks())
    leaked = tasks_after - tasks_before
    assert leaked == set(), f"Leaked tasks after failed run(): {leaked}"


@pytest.mark.asyncio
async def test_stalled_delivery_does_not_deadlock_pump_and_asr() -> None:
    """A blocked delivery stage must not deadlock the pump/ASR (bounded memory).

    With one finalised turn per inbound frame and delivery gated on an event,
    the transcript queue back-pressures the ASR (and the audio queue
    back-pressures the pump) — but there is no cycle, so once delivery is
    released the call completes.  This would hang under the old 2-task design.
    """
    gate = asyncio.Event()
    delivered: list[str] = []

    async def gated_deliver(text: str) -> None:
        await gate.wait()
        delivered.append(text)

    n_frames = 100
    frames = [_silence_frame(i) for i in range(n_frames)]
    asr = _SlowDrainASR()

    loop = _build_loop(
        _FakeTransport(frames),
        asr,
        _FakeTTS([]),
        _FakeGuard([_allow_result()]),
        gated_deliver,
    )

    run_task = asyncio.create_task(loop.run())
    # Let the pump and ASR make as much progress as back-pressure allows while
    # delivery is stalled.  They must not deadlock: the task is simply pending.
    await asyncio.sleep(0)
    assert not run_task.done(), "run() finished before delivery was released"

    # Release delivery; the whole pipeline must now drain and complete.
    gate.set()
    await asyncio.wait_for(run_task, timeout=5.0)

    assert len(delivered) == n_frames
    assert asr.frames_drained == n_frames


@pytest.mark.asyncio
async def test_external_cancellation_leaks_no_tasks() -> None:
    """Cancelling run() mid-flight must tear down every child task cleanly."""
    tasks_before = set(asyncio.all_tasks())

    # Delivery blocks forever so run() cannot complete on its own.
    block_forever = asyncio.Event()

    async def stuck_deliver(text: str) -> None:
        _ = text
        await block_forever.wait()

    frames = [_silence_frame(i) for i in range(50)]

    loop = _build_loop(
        _FakeTransport(frames),
        _SlowDrainASR(),
        _FakeTTS([]),
        _FakeGuard([_allow_result()]),
        stuck_deliver,
    )

    run_task = asyncio.create_task(loop.run())
    await asyncio.sleep(0)  # let the pipeline start and back-pressure build
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    tasks_after = set(asyncio.all_tasks())
    leaked = tasks_after - tasks_before
    assert leaked == set(), f"Leaked tasks after cancellation: {leaked}"
