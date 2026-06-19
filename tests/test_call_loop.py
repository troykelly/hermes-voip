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
import itertools
import random
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Final

import pytest

from hermes_voip.media.call_loop import BargeInMode, CallLoop, gate_voip_tool
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.vad import VoiceActivityDetector
from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import GuardSessionState, ToolRisk
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.tts._stream import PcmFrameStream, SegmentSource

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
        # Number of times flush_outbound was called and the last fade_ms seen — the
        # barge-in clean-stop assertions (ADR-0028) read these.
        self.flush_calls: int = 0
        self.last_flush_fade_ms: int | None = None

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

    async def flush_outbound(self, *, fade_ms: int) -> None:
        self.flush_calls += 1
        self.last_flush_fade_ms = fade_ms

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

    async def aclose(self) -> None:
        self._cancelled = True


class _FakeTTS:
    """StreamingTTS fake: yields preset PCM frames from synthesize()."""

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = frames
        self.last_stream: _FakeTtsStream | None = None
        self.last_sample_rate: int | None = None

    @property
    def output_sample_rate(self) -> int:
        return 16_000

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        self.last_sample_rate = sample_rate
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


def _make_speaking_vad() -> VoiceActivityDetector:
    """Return a VAD whose model always returns 1.0 → fires ONSET on first window."""

    def _voiced_model(window_pcm16: bytes, sample_rate: int) -> float:
        _ = window_pcm16, sample_rate  # Protocol match; unused in fake
        return 1.0

    return VoiceActivityDetector(model=_voiced_model, sample_rate_hz=16_000)


def _make_endpointer() -> Endpointer:
    return Endpointer(silence_ms=500, sample_rate_hz=16_000)


def _build_loop(  # noqa: PLR0913 — factory mirrors CallLoop's own keyword __init__
    transport: _FakeTransport,
    asr: StreamingASR,
    tts: StreamingTTS,
    guard: _FakeGuard,
    deliver_turn: Callable[[str], Awaitable[None]],
    guard_state: GuardSessionState | None = None,
    *,
    vad: VoiceActivityDetector | None = None,
    greeting: str = "",
) -> CallLoop:
    state = guard_state or GuardSessionState(call_id=_CALL_ID)
    return CallLoop(
        transport=transport,
        asr=asr,
        tts=tts,
        guard=guard,
        vad=vad or _make_vad(),
        endpointer=_make_endpointer(),
        guard_state=state,
        deliver_turn=deliver_turn,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting=greeting,
    )


async def _noop(text: str) -> None:
    """Discard-all deliver_turn stub."""
    _ = text  # unused; intentional stub


async def _one_chunk(text: str) -> AsyncIterator[str]:
    """A single-chunk agent-text iterator for speak()."""
    yield text


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
        greeting="",
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
# (c2) Authorised barge-in FLUSHES the outbound audio (ADR-0028 clean-stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barge_in_flushes_outbound_audio() -> None:
    """barge_in() must flush the transport's outbound buffer, not only cancel TTS.

    Cancelling the TtsStream stops PULLING new frames, but already-queued TTS audio
    keeps pacing out of the engine (the abruptness/delay bug). barge_in() must also
    call transport.flush_outbound so the agent goes quiet within ~1 packet.

    Strategy mirrors test (c): a blocking transport parks speak() mid-utterance (so
    the stream is still the active stream), then barge_in() cuts it. The blocked
    send_audio is released afterward so speak() exits cleanly.
    """
    tts_frame = PcmFrame(
        samples=b"\x10\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )
    transport = _BlockingTransport([])
    tts = _FakeTTS([tts_frame, tts_frame, tts_frame])
    loop = CallLoop(
        transport=transport,
        asr=_FakeASR([]),
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad(),
        endpointer=_make_endpointer(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=_noop,
        voice=_VOICE,
        call_id=_CALL_ID,
        barge_in_fade_ms=33,
    )

    async def _tokens() -> AsyncIterator[str]:
        yield "the agent is speaking"

    # speak() parks on the first send_audio gate with the stream registered.
    speak_task = asyncio.create_task(loop.speak(_tokens()))
    await asyncio.sleep(0)
    await loop.barge_in()
    transport.first_send_gate.set()
    await speak_task

    assert transport.flush_calls >= 1, "barge_in did not flush the outbound audio"
    assert transport.last_flush_fade_ms == 33, "the configured fade_ms was not used"


@pytest.mark.asyncio
async def test_barge_in_with_no_active_stream_does_not_flush() -> None:
    """barge_in() is a no-op (no flush, no error) when the agent is not speaking.

    Flushing the engine when nothing is playing would be a needless side effect;
    barge_in only flushes when there is an active TTS stream to cut off.
    """
    transport = _FakeTransport([])
    loop = CallLoop(
        transport=transport,
        asr=_FakeASR([]),
        tts=_FakeTTS([]),
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad(),
        endpointer=_make_endpointer(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=_noop,
        voice=_VOICE,
        call_id=_CALL_ID,
    )

    await loop.barge_in()  # no active stream

    assert transport.flush_calls == 0


# ---------------------------------------------------------------------------
# (g) Dead-air comfort filler (ADR-0030)
# ---------------------------------------------------------------------------


class _GatedSleep:
    """A controllable ``sleep`` seam for deterministic dead-air tests.

    ``await gated(delay)`` records the requested delay and blocks until the test
    calls :meth:`release` (or returns at once if already released). This lets a
    test decide *exactly* when the comfort-filler delay elapses, with no real
    wall-clock waiting — the deterministic seam ADR-0030 mandates.
    """

    def __init__(self) -> None:
        self.calls: list[float] = []
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        await self._gate.wait()


class _SteppedSleep:
    """A ``sleep`` seam that releases successive waits one :meth:`step` at a time.

    Unlike :class:`_GatedSleep` (one shared gate that frees every wait at once), this
    lets a test advance the periodic comfort-filler (ADR-0054) wait-by-wait and count
    exactly how many fillers fire. Each ``await stepped(delay)`` records the delay then
    blocks until a :meth:`step` releases it (a permit issued before the wait is honoured
    immediately). A wait with no pending ``step`` stays blocked, so a runaway loop is
    caught (it never silently spins). ``steps`` is the number of releases the test
    intends to issue — recorded only for readability; the actual releasing is via
    :meth:`step`.
    """

    def __init__(self, *, steps: int) -> None:
        self.calls: list[float] = []
        self.steps = steps
        self._sem = asyncio.Semaphore(0)

    def step(self) -> None:
        self._sem.release()

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        await self._sem.acquire()


class _HoldOpenTransport(_FakeTransport):
    """Transport that delivers preset inbound frames then holds the stream open.

    The inbound generator yields the preset frames, then awaits an event the test
    controls before ending — so a ``CallLoop.run()`` stays live (its pump/asr/
    delivery tasks running) while the test drives the comfort filler, instead of
    ``run()`` returning the instant the short frame list is exhausted.
    """

    def __init__(self, frames: list[PcmFrame]) -> None:
        super().__init__(frames)
        self._close = asyncio.Event()

    def close_inbound(self) -> None:
        self._close.set()

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        frames = self._frames
        close = self._close

        async def _gen() -> AsyncIterator[PcmFrame]:
            for frame in frames:
                yield frame
            await close.wait()

        return _gen()


def _seeded_rng(seed: int) -> random.Random:
    # The comfort-filler RNG is for phrase variety only, never security (ADR-0054).
    return random.Random(seed)  # noqa: S311 — non-cryptographic; variety, not security


def _comfort_loop(  # noqa: PLR0913 — factory mirrors CallLoop's keyword __init__ plus the filler knobs
    transport: _FakeTransport,
    asr: StreamingASR,
    tts: StreamingTTS,
    *,
    sleep: Callable[[float], Awaitable[None]],
    comfort_filler: bool = True,
    comfort_filler_delay_ms: int = 900,
    comfort_filler_repeat_ms: int = 900,
    comfort_filler_phrases: tuple[str, ...] = ("Hmm,",),
    rng: random.Random | None = None,
    deliver_turn: Callable[[str], Awaitable[None]] | None = None,
) -> CallLoop:
    """Build a CallLoop with the comfort filler wired to an injected sleep seam."""
    return CallLoop(
        transport=transport,
        asr=asr,
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad(),
        endpointer=_make_endpointer(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=deliver_turn or _noop,
        voice=_VOICE,
        call_id=_CALL_ID,
        comfort_filler=comfort_filler,
        comfort_filler_delay_ms=comfort_filler_delay_ms,
        comfort_filler_repeat_ms=comfort_filler_repeat_ms,
        comfort_filler_phrases=comfort_filler_phrases,
        rng=rng if rng is not None else _seeded_rng(0),
        sleep=sleep,
    )


@pytest.mark.asyncio
async def test_comfort_filler_fires_after_delay_when_no_reply() -> None:
    """When the gap exceeds the delay and no reply has started, one filler plays.

    The injected sleep is released (the delay 'elapses') while no agent reply has
    begun, so the filler synthesises exactly one phrase and sends its frames.
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    filler_frame = PcmFrame(
        samples=b"\x20\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )
    sleep = _GatedSleep()
    transport = _HoldOpenTransport([_silence_frame(0)])
    tts = _FakeTTS([filler_frame, filler_frame])
    loop = _comfort_loop(
        transport,
        _FakeASR([("hello there", True, True)]),
        tts,
        sleep=sleep,
        comfort_filler_phrases=("Hmm,",),
        deliver_turn=capture,
    )

    run_task = asyncio.create_task(loop.run())
    # Let the turn be delivered and the filler task park on the injected sleep.
    for _ in range(20):
        await asyncio.sleep(0)
        if sleep.calls:
            break
    assert sleep.calls, "comfort filler did not schedule a delayed wait"
    assert delivered == ["hello there"]

    # The delay elapses with no reply: the filler must synthesise + send.
    sleep.release()
    for _ in range(20):
        await asyncio.sleep(0)
        if transport.sent_audio:
            break

    assert transport.sent_audio == [filler_frame, filler_frame], (
        "the comfort filler did not emit its audio after the delay"
    )
    # The filler text reached TTS synthesis (one of the configured phrases).
    assert tts.last_stream is not None

    transport.close_inbound()
    await run_task


@pytest.mark.asyncio
async def test_comfort_filler_uses_configured_phrase_text() -> None:
    """The filler synthesises one of the configured phrases (model-aware path).

    The phrase text is routed through ``tts.synthesize`` (so the per-segment tag
    strip / sanitiser of the normal speak path applies). This test captures the
    text handed to synthesis and asserts it is the configured filler phrase.
    """
    captured: list[str] = []

    class _CapturingStream(_FakeTtsStream):
        """A TtsStream that drains (and records) the text iterator as it plays.

        A real provider consumes the text it is handed; the bare ``_FakeTtsStream``
        ignores it, so this subclass drains the iterator on first iteration and
        records each chunk into ``captured`` before emitting the preset frames —
        faithfully exercising the text the comfort filler routes to synthesis.
        """

        def __init__(self, frames: list[PcmFrame], text: AsyncIterator[str]) -> None:
            super().__init__(frames)
            self._text = text

        async def _iter(self) -> AsyncIterator[PcmFrame]:
            async for chunk in self._text:
                captured.append(chunk)
            for frame in self._frames:
                if self._cancelled:
                    return
                yield frame

    class _CapturingTTS(_FakeTTS):
        def synthesize(
            self,
            text: AsyncIterator[str],
            voice: str,
            *,
            sample_rate: int | None = None,
        ) -> TtsStream:
            self.last_sample_rate = sample_rate
            stream = _CapturingStream(self._frames, text)
            self.last_stream = stream
            return stream

    filler_frame = PcmFrame(
        samples=b"\x20\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )
    sleep = _GatedSleep()
    transport = _HoldOpenTransport([_silence_frame(0)])
    tts = _CapturingTTS([filler_frame])
    loop = _comfort_loop(
        transport,
        _FakeASR([("a question", True, True)]),
        tts,
        sleep=sleep,
        comfort_filler_phrases=("Let me see,",),
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if sleep.calls:
            break
    sleep.release()
    for _ in range(20):
        await asyncio.sleep(0)
        if transport.sent_audio:
            break

    assert captured == ["Let me see,"], (
        f"filler phrase not synthesised verbatim; got {captured!r}"
    )

    transport.close_inbound()
    await run_task


@pytest.mark.asyncio
async def test_comfort_filler_does_not_fire_when_reply_starts_first() -> None:
    """A reply that begins before the delay elapses suppresses the filler entirely.

    The agent's reply (via speak()) starts and emits its first frame BEFORE the
    injected sleep is released. The post-delay check must see reply audio already
    on the wire and skip the filler — no collision with the agent's opening word.
    """
    reply_frame = PcmFrame(
        samples=b"\x30\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )
    filler_frame = PcmFrame(
        samples=b"\x20\x00" * 256, sample_rate=16_000, monotonic_ts_ns=2
    )
    sleep = _GatedSleep()
    transport = _HoldOpenTransport([_silence_frame(0)])
    # The TTS yields the REPLY frame first (speak), and would yield the filler
    # frame only if the filler fired — which it must not.
    tts = _FakeTTS([reply_frame])
    loop = _comfort_loop(
        transport,
        _FakeASR([("hi", True, True)]),
        tts,
        sleep=sleep,
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if sleep.calls:
            break
    assert sleep.calls, "filler did not schedule"

    # The agent replies BEFORE the delay elapses (sleep still gated).
    async def _reply() -> AsyncIterator[str]:
        yield "here is my answer"

    await loop.speak(_reply())
    assert transport.sent_audio == [reply_frame]

    # NOW the delay elapses — but a reply already played, so no filler may fire.
    _ = filler_frame  # only sent if the filler wrongly fires
    sleep.release()
    for _ in range(20):
        await asyncio.sleep(0)

    assert transport.sent_audio == [reply_frame], (
        "the comfort filler fired even though the reply had already started"
    )

    transport.close_inbound()
    await run_task


@pytest.mark.asyncio
async def test_comfort_filler_fires_periodically_on_sustained_gap() -> None:
    """On a SUSTAINED dead-air gap the filler re-fires every repeat interval (ADR-0054).

    A single ~1 s phrase does not fill a 10 s LLM wait, so once the delay elapses with
    no reply the filler emits one phrase, then keeps re-emitting a fresh phrase every
    ``comfort_filler_repeat_ms`` until the reply audio starts (or a barge-in/teardown
    cancels it). This replaces the old one-shot ("at most once per gap") behaviour; the
    change is recorded in ADR-0054.
    """
    filler_frame = PcmFrame(
        samples=b"\x20\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )
    # One frame per synthesis, so #sent frames == #fillers fired.
    sleep = _SteppedSleep(steps=3)  # the initial delay + two repeat intervals
    transport = _HoldOpenTransport([_silence_frame(0)])
    tts = _FakeTTS([filler_frame])
    loop = _comfort_loop(
        transport,
        _FakeASR([("slow question", True, True)]),
        tts,
        sleep=sleep,
        comfort_filler_delay_ms=900,
        comfort_filler_repeat_ms=900,
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if sleep.calls:
            break
    # Let all three gated waits elapse (delay + 2 repeats), pumping between each.
    for _ in range(3):
        sleep.step()
        for _ in range(20):
            await asyncio.sleep(0)
            if len(transport.sent_audio) >= 3:
                break

    assert len(transport.sent_audio) == 3, (
        "the filler did not re-fire periodically on a sustained gap; "
        f"sent={len(transport.sent_audio)} frames, sleeps={sleep.calls!r}"
    )
    # The loop awaited the initial delay then two repeat intervals (all 0.9 s here).
    assert sleep.calls == [0.9, 0.9, 0.9], (
        f"unexpected periodic wait schedule: {sleep.calls!r}"
    )

    transport.close_inbound()
    await run_task


@pytest.mark.asyncio
async def test_comfort_filler_periodic_skips_iterations_while_audio_active() -> None:
    """Each periodic iteration re-checks dead air; no fire while audio is active.

    ADR-0054 preserves the ADR-0030 guard PER ITERATION, not just the first one. After
    one filler fires, agent audio becomes active on the wire (a reply that has not yet
    cancelled the task, or a still-playing prior stream); the NEXT repeat-interval check
    must see ``_tts_audio_active`` and skip — no filler over live audio. When the audio
    later clears, a later iteration fires again (the loop is alive until cancelled).
    """
    filler_frame = PcmFrame(
        samples=b"\x20\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )
    sleep = _SteppedSleep(steps=3)
    transport = _HoldOpenTransport([_silence_frame(0)])
    tts = _FakeTTS([filler_frame])
    loop = _comfort_loop(
        transport,
        _FakeASR([("slow", True, True)]),
        tts,
        sleep=sleep,
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if sleep.calls:
            break
    # First iteration: dead air → one filler fires.
    sleep.step()
    for _ in range(20):
        await asyncio.sleep(0)
        if transport.sent_audio:
            break
    assert len(transport.sent_audio) == 1, "first periodic filler did not fire"

    # Simulate agent audio now on the wire (e.g. a reply mid-flight). The next iteration
    # must SKIP rather than speak over it.
    loop._tts_audio_active = True
    sleep.step()
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(transport.sent_audio) == 1, (
        "a filler fired while agent audio was active (per-iteration guard failed)"
    )

    # The audio clears; the still-alive loop fires again on the following iteration.
    loop._tts_audio_active = False
    sleep.step()
    for _ in range(20):
        await asyncio.sleep(0)
        if len(transport.sent_audio) >= 2:
            break
    assert len(transport.sent_audio) == 2, (
        "the periodic loop did not resume firing after the audio cleared"
    )

    transport.close_inbound()
    await run_task


@pytest.mark.asyncio
async def test_comfort_filler_selects_phrases_randomly_no_immediate_repeat() -> None:
    """Phrase selection is RANDOM and never repeats the immediately-previous phrase.

    ADR-0054 replaces the deterministic round-robin with random selection (so a
    multi-gap / periodic call does not sound mechanically cyclic), with the one
    guarantee that no phrase is spoken twice in a row. This drives
    ``_next_comfort_phrase`` directly over many draws with a seeded RNG: every
    adjacent pair differs, and — given enough draws — it is not a fixed rotation.
    """
    sleep = _GatedSleep()
    transport = _HoldOpenTransport([_silence_frame(0)])
    tts = _FakeTTS([_silence_frame(0)])
    phrases = ("A", "B", "C", "D")
    loop = _comfort_loop(
        transport,
        _FakeASR([("hi", True, True)]),
        tts,
        sleep=sleep,
        comfort_filler_phrases=phrases,
        rng=_seeded_rng(1234),
    )

    draws = [loop._next_comfort_phrase() for _ in range(200)]
    assert set(draws) <= set(phrases)
    assert set(draws) == set(phrases), "not all phrases were ever chosen (not random)"
    # No phrase is ever immediately repeated.
    assert all(a != b for a, b in itertools.pairwise(draws)), (
        "a filler phrase repeated immediately (must avoid back-to-back repeats)"
    )
    # Not a fixed round-robin: a deterministic rotation would make
    # draws[i + period] == draws[i] for all i; assert it is not perfectly periodic.
    period = len(phrases)
    assert any(draws[i] != draws[i + period] for i in range(len(draws) - period)), (
        "selection looks like a fixed rotation, not random"
    )


@pytest.mark.asyncio
async def test_comfort_filler_single_phrase_set_does_not_deadlock() -> None:
    """A one-phrase set must still work (no-immediate-repeat cannot starve it).

    With only one phrase the 'avoid an immediate repeat' rule has no alternative, so
    the selector must return it rather than loop forever seeking a different phrase.
    """
    sleep = _GatedSleep()
    transport = _HoldOpenTransport([_silence_frame(0)])
    tts = _FakeTTS([_silence_frame(0)])
    loop = _comfort_loop(
        transport,
        _FakeASR([("hi", True, True)]),
        tts,
        sleep=sleep,
        comfort_filler_phrases=("only one,",),
        rng=_seeded_rng(7),
    )
    assert [loop._next_comfort_phrase() for _ in range(5)] == ["only one,"] * 5


@pytest.mark.asyncio
async def test_comfort_filler_off_by_default_emits_nothing() -> None:
    """With the filler OFF (the default), no filler audio and no sleep is scheduled.

    Off must be exactly today's behaviour: the loop neither sleeps on a delay nor
    synthesises any filler.
    """
    sleep = _GatedSleep()
    transport = _HoldOpenTransport([_silence_frame(0)])
    tts = _FakeTTS([_silence_frame(0)])
    loop = _comfort_loop(
        transport,
        _FakeASR([("hello", True, True)]),
        tts,
        sleep=sleep,
        comfort_filler=False,
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(30):
        await asyncio.sleep(0)

    assert sleep.calls == [], "the filler scheduled a delay while disabled"
    assert transport.sent_audio == [], "the filler emitted audio while disabled"

    transport.close_inbound()
    await run_task


@pytest.mark.asyncio
async def test_comfort_filler_cancelled_by_barge_in() -> None:
    """A barge-in during the gap cancels the pending filler — it never fires.

    The filler is parked on its delay when a barge-in occurs. The barge-in must
    cancel the pending filler so no filler audio is ever emitted (and the flush
    path is the ADR-0028 one).
    """
    filler_frame = PcmFrame(
        samples=b"\x20\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )
    sleep = _GatedSleep()
    transport = _HoldOpenTransport([_silence_frame(0)])
    tts = _FakeTTS([filler_frame])
    loop = _comfort_loop(
        transport,
        _FakeASR([("interrupt me", True, True)]),
        tts,
        sleep=sleep,
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if sleep.calls:
            break
    assert sleep.calls, "filler did not schedule"

    # Barge-in while the filler is still parked on its delay.
    await loop.barge_in()
    # Now release the delay; the filler must have been cancelled and not fire.
    sleep.release()
    for _ in range(20):
        await asyncio.sleep(0)

    assert transport.sent_audio == [], (
        "the comfort filler fired after a barge-in cancelled the gap"
    )

    transport.close_inbound()
    await run_task


@pytest.mark.asyncio
async def test_comfort_filler_stands_down_once_agent_commits_a_reply() -> None:
    """Once the agent commits a reply (speak() called), a PENDING filler stands down.

    The filler covers the caller-finish→reply *processing* gap (STT/LLM think time).
    The agent calls speak() before the delay elapses — even though its TTS first-audio
    is latent (no reply frame yet) — and the pending filler must NOT fire: the agent
    has the floor and the reply is imminent (ADR-0030 §"when the filler stands down").
    Were the filler to fire here it would race the imminent reply and contend on the
    single playout lock the reply already holds through its TTS latency.
    """
    reply_frame = PcmFrame(
        samples=b"\x30\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )
    filler_frame = PcmFrame(
        samples=b"\x20\x00" * 256, sample_rate=16_000, monotonic_ts_ns=2
    )

    # A TTS whose stream withholds its first frame until the test releases a gate,
    # modelling TTS first-audio latency (the reply is committed but not yet audible).
    audio_gate = asyncio.Event()

    class _LatentStream(_FakeTtsStream):
        async def _iter(self) -> AsyncIterator[PcmFrame]:
            await audio_gate.wait()
            for frame in self._frames:
                if self._cancelled:
                    return
                yield frame

    class _LatentReplyTTS(_FakeTTS):
        """Reply stream is latent (gated); a filler stream (if any) is immediate."""

        def __init__(
            self, reply_frames: list[PcmFrame], filler_frames: list[PcmFrame]
        ) -> None:
            super().__init__(filler_frames)
            self._reply_frames = reply_frames
            self._first = True

        def synthesize(
            self,
            text: AsyncIterator[str],
            voice: str,
            *,
            sample_rate: int | None = None,
        ) -> TtsStream:
            self.last_sample_rate = sample_rate
            # The FIRST synthesize() call is the agent reply (latent); any SECOND
            # would be a (wrongly-fired) comfort filler.
            if self._first:
                self._first = False
                stream: _FakeTtsStream = _LatentStream(self._reply_frames)
            else:
                stream = _FakeTtsStream(self._frames)
            self.last_stream = stream
            return stream

    sleep = _GatedSleep()
    transport = _HoldOpenTransport([_silence_frame(0)])
    tts = _LatentReplyTTS([reply_frame], [filler_frame])
    loop = _comfort_loop(
        transport,
        _FakeASR([("hi", True, True)]),
        tts,
        sleep=sleep,
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if sleep.calls:
            break
    assert sleep.calls, "filler did not schedule"

    # The agent commits a reply (speak()) before the delay elapses; it parks on the
    # latent stream (TTS first-audio latency) holding the playout lock.
    speak_task = asyncio.create_task(loop.speak(_one_chunk("the answer")))
    for _ in range(10):
        await asyncio.sleep(0)
    assert transport.sent_audio == [], "reply audio should still be latent"

    # The delay elapses — but the agent has already committed a reply, so the filler
    # stands down (no filler audio, no second synthesize()).
    sleep.release()
    for _ in range(20):
        await asyncio.sleep(0)
    assert transport.sent_audio == [], (
        "the filler fired after the agent committed a reply"
    )

    # The reply audio finally arrives; only the reply is heard, never a filler.
    audio_gate.set()
    await speak_task
    assert transport.sent_audio == [reply_frame]

    transport.close_inbound()
    await run_task


@pytest.mark.asyncio
async def test_comfort_filler_playing_is_cancelled_on_teardown_no_leak() -> None:
    """A filler still PLAYING when the call ends is cancelled — no leaked task.

    The filler parks mid-playout on a blocking send_audio; the inbound stream then
    ends (call teardown). run() must cancel the in-flight filler task so it does not
    outlive the call. After run() returns, the filler task is done (cancelled).
    """
    filler_frame = PcmFrame(
        samples=b"\x20\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )

    class _BlockingHoldOpenTransport(_HoldOpenTransport):
        """Holds the inbound open AND blocks the first send_audio on a gate."""

        def __init__(self, frames: list[PcmFrame]) -> None:
            super().__init__(frames)
            self.first_send_gate = asyncio.Event()

        async def send_audio(self, frame: PcmFrame) -> None:
            if not self.sent_audio:
                await self.first_send_gate.wait()
            self.sent_audio.append(frame)

    sleep = _GatedSleep()
    transport = _BlockingHoldOpenTransport([_silence_frame(0)])
    tts = _FakeTTS([filler_frame, filler_frame])
    loop = _comfort_loop(
        transport,
        _FakeASR([("slow", True, True)]),
        tts,
        sleep=sleep,
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if sleep.calls:
            break
    # Release the delay; the filler fires and parks inside _play on the send gate.
    sleep.release()
    for _ in range(20):
        await asyncio.sleep(0)
    # White-box assertion: the filler task handle is kept for the task's whole life
    # (including playout) so teardown can cancel it — that is the no-leak contract.
    filler_task = loop._comfort_filler_task
    assert filler_task is not None, "the filler task should be live (mid-playout)"
    assert not filler_task.done()

    # End the call while the filler is still parked mid-playout.
    transport.close_inbound()
    # Unblock the parked send so the cancelled _play can unwind cleanly.
    transport.first_send_gate.set()
    await run_task

    # run()'s teardown must have cancelled the in-flight filler — no leak.
    assert filler_task.done(), "filler task leaked past the call (not cancelled)"


@pytest.mark.asyncio
async def test_comfort_filler_does_not_supersede_already_playing_audio() -> None:
    """The filler must not fire while agent audio is on the wire RIGHT NOW.

    Regression for the case where agent audio (here a long greeting) is already
    playing when a turn is delivered (arming the filler) — the audio began BEFORE
    the gap's latch reset, so the per-gap latch alone misses it. The post-delay
    ``_tts_audio_active`` guard must still suppress the filler so it never supersedes
    the live agent audio (no dead air while the agent is speaking).
    """
    # A greeting stream that plays one frame then parks (gate) — agent audio is
    # continuously "active" on the wire across the filler's whole delay.
    greet_frame = PcmFrame(
        samples=b"\x40\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )
    filler_frame = PcmFrame(
        samples=b"\x20\x00" * 256, sample_rate=16_000, monotonic_ts_ns=2
    )
    hold = asyncio.Event()

    class _GreetingThenParkStream(_FakeTtsStream):
        async def _iter(self) -> AsyncIterator[PcmFrame]:
            yield greet_frame  # audio is now active on the wire
            await hold.wait()  # keep the greeting "playing" across the delay
            for frame in self._frames:
                if self._cancelled:
                    return
                yield frame

    class _GreetingTTS(_FakeTTS):
        """Greeting stream parks (long); any filler stream would be immediate."""

        def __init__(
            self, greet_frames: list[PcmFrame], filler_frames: list[PcmFrame]
        ) -> None:
            super().__init__(filler_frames)
            self._greet_frames = greet_frames
            self._first = True

        def synthesize(
            self,
            text: AsyncIterator[str],
            voice: str,
            *,
            sample_rate: int | None = None,
        ) -> TtsStream:
            self.last_sample_rate = sample_rate
            if self._first:
                self._first = False
                stream: _FakeTtsStream = _GreetingThenParkStream(self._greet_frames)
            else:
                stream = _FakeTtsStream(self._frames)
            self.last_stream = stream
            return stream

    sleep = _GatedSleep()
    transport = _HoldOpenTransport([_silence_frame(0)])
    tts = _GreetingTTS([greet_frame], [filler_frame])
    loop = CallLoop(
        transport=transport,
        asr=_FakeASR([("hello", True, True)]),
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad(),
        endpointer=_make_endpointer(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=_noop,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="hi there",  # the long greeting that stays active
        comfort_filler=True,
        comfort_filler_delay_ms=900,
        comfort_filler_phrases=("Hmm,",),
        sleep=sleep,
    )

    run_task = asyncio.create_task(loop.run())
    # Let the greeting emit its first frame (audio active) and the filler arm.
    for _ in range(30):
        await asyncio.sleep(0)
        if sleep.calls and transport.sent_audio:
            break
    assert sleep.calls, "filler did not schedule"
    assert transport.sent_audio == [greet_frame], "greeting audio should be active"

    # The delay elapses while the greeting audio is STILL active → no filler.
    sleep.release()
    for _ in range(20):
        await asyncio.sleep(0)
    assert transport.sent_audio == [greet_frame], (
        "the filler superseded already-playing agent audio (no dead air)"
    )

    # Let the greeting finish and the call end.
    hold.set()
    transport.close_inbound()
    await run_task


@pytest.mark.asyncio
async def test_comfort_filler_armed_before_deliver_turn_returns() -> None:
    """The filler is armed BEFORE deliver_turn is awaited (covers a blocking handoff).

    The filler's delay measures the dead-air gap from the caller-finish moment, so it
    must be scheduled before `await deliver_turn` — robust even if deliver_turn blocks
    on agent work. With a deliver_turn that blocks on an event, the filler's delay
    sleep must already be scheduled while deliver_turn is still parked.
    """
    deliver_gate = asyncio.Event()
    delivered: list[str] = []

    async def blocking_deliver(text: str) -> None:
        delivered.append(text)
        await deliver_gate.wait()  # model a deliver_turn that blocks on agent work

    sleep = _GatedSleep()
    transport = _HoldOpenTransport([_silence_frame(0)])
    tts = _FakeTTS([_silence_frame(0)])
    loop = _comfort_loop(
        transport,
        _FakeASR([("question", True, True)]),
        tts,
        sleep=sleep,
        deliver_turn=blocking_deliver,
    )

    run_task = asyncio.create_task(loop.run())
    # While deliver_turn is still BLOCKED, the filler's delay sleep must already be
    # scheduled (it was armed before the deliver_turn await).
    for _ in range(20):
        await asyncio.sleep(0)
        if delivered and sleep.calls:
            break
    assert delivered == ["question"], "the turn was not delivered"
    assert sleep.calls, "the filler was not armed before deliver_turn returned"

    # Release deliver_turn and end the call.
    deliver_gate.set()
    transport.close_inbound()
    await run_task


@pytest.mark.asyncio
async def test_normal_speech_completion_does_not_flush() -> None:
    """A normally-completing utterance (no barge-in) never flushes the engine.

    The flush is a barge-in-only action; an utterance that runs to its natural end
    must let the engine's own stop()/tail logic deliver the residual — flushing it
    would truncate the legitimate tail.
    """
    frames = [
        PcmFrame(samples=bytes([i, 0]) * 256, sample_rate=16_000, monotonic_ts_ns=i)
        for i in range(3)
    ]
    transport = _FakeTransport([])
    loop = _build_loop(
        transport,
        _FakeASR([]),
        _FakeTTS(frames),
        _FakeGuard([_allow_result()]),
        _noop,
    )

    async def _tokens() -> AsyncIterator[str]:
        yield "a complete sentence"

    await loop.speak(_tokens())

    assert transport.sent_audio == frames
    assert transport.flush_calls == 0


@pytest.mark.asyncio
async def test_authorised_full_mode_barge_in_flushes_via_pump() -> None:
    """An end-to-end FULL-mode barge-in from the pump flushes the outbound audio.

    Drives the real pump→VAD→gate path: a speaking VAD fires an ONSET, FULL mode
    authorises immediately, the pump calls barge_in(), which flushes. Proves the
    wiring from the gate's authorisation to the engine flush, not just the direct
    barge_in() call.
    """
    # A few voiced inbound frames so the speaking VAD fires an ONSET in the pump.
    inbound = [_silence_frame(i) for i in range(4)]
    transport = _FakeTransport(inbound)
    # A long-running TTS stream registered as a greeting so it is active when the
    # pump processes the first inbound frame.
    tts_frame = PcmFrame(
        samples=b"\x10\x00" * 256, sample_rate=16_000, monotonic_ts_ns=1
    )
    tts = _FakeTTS([tts_frame] * 50)
    loop = CallLoop(
        transport=transport,
        asr=_FakeASR([]),
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=_make_speaking_vad(),
        endpointer=_make_endpointer(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=_noop,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="hello there caller",
        barge_in_mode=BargeInMode.FULL,
        barge_in_fade_ms=30,
    )

    await loop.run()

    assert transport.flush_calls >= 1, (
        "an authorised FULL-mode barge-in from the pump did not flush the engine"
    )


@pytest.mark.asyncio
async def test_speak_passes_the_negotiated_wire_rate_to_tts() -> None:
    """The call loop tells the TTS the negotiated wire rate (ADR-0022).

    The process-wide TTS provider does not know the per-call codec; the call loop
    does (via ``transport.inbound_sample_rate``, which is codec-derived: 8 kHz
    G.711, 16 kHz G.722). So the loop passes that rate into ``synthesize`` so the
    synthesiser emits the negotiated rate (no wideband thrown away, no needless
    G.711 resample). The fake transport reports 16 kHz (the G.722 case).
    """

    class _CapturingTTS:
        def __init__(self) -> None:
            self.rates: list[int | None] = []

        @property
        def output_sample_rate(self) -> int:
            return 16_000

        def synthesize(
            self,
            text: AsyncIterator[str],
            voice: str,
            *,
            sample_rate: int | None = None,
        ) -> TtsStream:
            self.rates.append(sample_rate)
            return _FakeTtsStream([])

    tts = _CapturingTTS()
    transport = _FakeTransport([])
    loop = _build_loop(
        transport, _FakeASR([]), tts, _FakeGuard([_allow_result()]), _noop
    )

    async def _tokens() -> AsyncIterator[str]:
        yield "hello"

    await loop.speak(_tokens())

    # The loop forwarded the transport's (codec-derived) wire rate to synthesize.
    assert tts.rates == [transport.inbound_sample_rate]
    assert tts.rates == [16_000]


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
    unobserved while the pump blocked on a full audio queue (the run hung
    indefinitely).  The supervised TaskGroup design must surface the ASR error
    from run() promptly and leave no task running.  TaskGroup re-raises a child
    failure inside an ExceptionGroup, so the original RuntimeError is asserted
    to be one of its sub-exceptions.
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

    with pytest.raises(BaseExceptionGroup) as excinfo:
        await asyncio.wait_for(loop.run(), timeout=5.0)

    # The exact ASR failure must be carried inside the group (not swallowed).
    matched, _rest = excinfo.value.split(RuntimeError)
    assert matched is not None
    runtime_errors = [
        exc for exc in matched.exceptions if isinstance(exc, RuntimeError)
    ]
    assert any(str(exc) == "asr exploded" for exc in runtime_errors)

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


# ---------------------------------------------------------------------------
# Greeting on answer (ADR-0002 NAT-latch): run() speaks the configured greeting
# IMMEDIATELY — before/independent of any inbound audio — so RTP flows out at
# once (caller hears it; a symmetric-RTP gateway latches the NAT'd source).
# ---------------------------------------------------------------------------


def _greeting_frame(byte: int) -> PcmFrame:
    """A distinct TTS output frame so greeting RTP is identifiable in send_audio."""
    return PcmFrame(
        samples=bytes([byte, 0]) * 128,
        sample_rate=16_000,
        monotonic_ts_ns=0,
    )


class _RecordingTtsStream:
    """TtsStream fake that records the synthesised text, then yields preset frames.

    Mirrors a real TTS: it first drains the incremental ``text`` iterator
    (recording the concatenation) and then emits audio. Lets a test assert the
    greeting text was the thing synthesised and that frames were produced.
    """

    def __init__(self, text: AsyncIterator[str], frames: list[PcmFrame]) -> None:
        self._text = text
        self._frames = frames
        self.synthesised_text = ""
        self.cancel_called = False
        self.flush_called = False
        self._cancelled = False
        self._gen = self._iter()

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        return await self._gen.__anext__()

    async def _iter(self) -> AsyncIterator[PcmFrame]:
        async for chunk in self._text:
            self.synthesised_text += chunk
        for frame in self._frames:
            if self._cancelled:
                return
            yield frame

    async def flush(self) -> None:
        self.flush_called = True

    async def cancel(self) -> None:
        self._cancelled = True
        self.cancel_called = True

    async def aclose(self) -> None:
        self._cancelled = True


class _RecordingTTS:
    """StreamingTTS fake recording every synthesise() call's text + frames out."""

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = frames
        self.calls = 0
        self.last_stream: _RecordingTtsStream | None = None

    @property
    def output_sample_rate(self) -> int:
        return 16_000

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        _ = voice
        self.calls += 1
        stream = _RecordingTtsStream(text, self._frames)
        self.last_stream = stream
        return stream


@pytest.mark.asyncio
async def test_greeting_synthesised_and_sent_without_inbound_audio() -> None:
    """run() speaks the configured greeting immediately — no inbound audio first.

    The transport delivers ZERO inbound frames, so the only audio that can reach
    ``send_audio`` is the greeting. This proves the plugin sends RTP first (which
    is what makes the caller hear the opening and a symmetric-RTP gateway latch).
    """
    greeting = "Hello, you're through to the assistant."
    frames = [_greeting_frame(1), _greeting_frame(2)]
    tts = _RecordingTTS(frames)
    transport = _FakeTransport([])  # NO inbound audio

    loop = _build_loop(
        transport,
        _FakeASR([]),
        tts,
        _FakeGuard([_allow_result()]),
        _noop,
        greeting=greeting,
    )
    await asyncio.wait_for(loop.run(), timeout=5.0)

    # The greeting was synthesised exactly once, with the configured text…
    assert tts.calls == 1
    assert tts.last_stream is not None
    assert tts.last_stream.synthesised_text == greeting
    # …and its frames were sent as outbound RTP, with no inbound audio first.
    assert transport.sent_audio == frames


@pytest.mark.asyncio
async def test_empty_greeting_sends_no_audio() -> None:
    """An empty greeting config synthesises nothing and sends no RTP on answer."""
    tts = _RecordingTTS([_greeting_frame(9)])
    transport = _FakeTransport([])

    loop = _build_loop(
        transport,
        _FakeASR([]),
        tts,
        _FakeGuard([_allow_result()]),
        _noop,
        greeting="",
    )
    await asyncio.wait_for(loop.run(), timeout=5.0)

    assert tts.calls == 0
    assert transport.sent_audio == []


class _GatedGreetingTransport(_FakeTransport):
    """Transport that withholds its (speaking) inbound frame until released.

    Lets the greeting's synthesis start and register its active stream before
    the caller's barge-in frame is delivered — so the ONSET cancels an
    in-flight greeting, exactly as on a live call where the caller talks over it.
    """

    def __init__(self, frames: list[PcmFrame]) -> None:
        super().__init__(frames)
        self.release = asyncio.Event()

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        frames = self._frames
        release = self.release

        async def _gen() -> AsyncIterator[PcmFrame]:
            await release.wait()
            for frame in frames:
                yield frame

        return _gen()


class _GatedGreetingTtsStream:
    """Greeting TtsStream that blocks mid-playout until cancelled or released.

    It emits its first frame, then parks on an event; ``cancel()`` (barge-in)
    sets the stop flag and unblocks it so the consumer stops yielding — the same
    two-task barge-in shape as a live call (the pump cancels while the greeting
    coroutine is parked in ``__anext__``).
    """

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = frames
        self.cancel_called = False
        self.flush_called = False
        self._resume = asyncio.Event()
        self._gen = self._iter()

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        return await self._gen.__anext__()

    async def _iter(self) -> AsyncGenerator[PcmFrame]:
        # Emit one frame so RTP starts, then park until cancel()/resume.
        if self._frames:
            yield self._frames[0]
        await self._resume.wait()
        # If we got here without a cancel, emit the rest (not exercised here).
        if not self.cancel_called:
            for frame in self._frames[1:]:
                yield frame

    async def flush(self) -> None:
        self.flush_called = True

    async def cancel(self) -> None:
        self.cancel_called = True
        self._resume.set()

    async def aclose(self) -> None:
        # Unblock the parked generator and close it (consumer-task teardown).
        self.cancel_called = True
        self._resume.set()
        await self._gen.aclose()


class _GatedGreetingTTS:
    """StreamingTTS fake returning a single gated (blocking) greeting stream."""

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = frames
        self.last_stream: _GatedGreetingTtsStream | None = None

    @property
    def output_sample_rate(self) -> int:
        return 16_000

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        _ = text, voice
        stream = _GatedGreetingTtsStream(self._frames)
        self.last_stream = stream
        return stream


@pytest.mark.asyncio
async def test_caller_speech_during_greeting_cancels_it() -> None:
    """Barge-in: caller speech onset while the greeting plays cancels the greeting.

    The greeting stream blocks after its first frame; once the gated inbound
    frame (scored as speech by the VAD) is released, the pump detects ONSET and
    cancels the active greeting stream — proving barge-in works against the
    greeting via the existing cancel path.
    """
    # One full 16 kHz silero window (512 samples → 1024 bytes) so the VAD runs
    # exactly one inference and emits an ONSET (model returns 1.0).
    speech_frame = PcmFrame(samples=bytes(1024), sample_rate=16_000, monotonic_ts_ns=0)
    tts = _GatedGreetingTTS([_greeting_frame(1), _greeting_frame(2)])
    transport = _GatedGreetingTransport([speech_frame])

    loop = _build_loop(
        transport,
        _FakeASR([]),
        tts,
        _FakeGuard([_allow_result()]),
        _noop,
        vad=_make_speaking_vad(),
        greeting="Hello there, this is a long greeting the caller talks over.",
    )

    run_task = asyncio.create_task(loop.run())
    # Let the greeting start and emit its first frame (registering the active
    # stream) before the caller's speech frame is delivered.
    for _ in range(5):
        await asyncio.sleep(0)
    # Release the caller's speech frame → ONSET → barge_in() cancels the greeting.
    transport.release.set()
    await asyncio.wait_for(run_task, timeout=5.0)

    assert tts.last_stream is not None
    assert tts.last_stream.cancel_called is True


class _SlowTtsStream:
    """TtsStream fake that yields each frame after a cooperative ``sleep(0)``.

    The per-frame yield lets a *second* concurrent ``speak()`` interleave with
    this one if playback is not single-owner — which is exactly the clobber the
    serialization fix must prevent. Stops promptly when cancelled.
    """

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = frames
        self.cancel_called = False
        self.flush_called = False
        self._cancelled = False
        self._gen = self._iter()

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        return await self._gen.__anext__()

    async def _iter(self) -> AsyncGenerator[PcmFrame]:
        for frame in self._frames:
            # cooperative yield so a rival speak() can run; cancel() may flip the
            # flag across the await. Re-read into a fresh local each time so the
            # post-await check is not narrowed away by the pre-await one.
            await asyncio.sleep(0)
            if self._is_cancelled():
                return
            yield frame

    def _is_cancelled(self) -> bool:
        return self._cancelled

    async def flush(self) -> None:
        self.flush_called = True

    async def cancel(self) -> None:
        self._cancelled = True
        self.cancel_called = True

    async def aclose(self) -> None:
        self._cancelled = True
        await self._gen.aclose()


class _SingleStreamTTS:
    """StreamingTTS fake that returns one preset _SlowTtsStream from synthesize()."""

    def __init__(self, stream: _SlowTtsStream) -> None:
        self._stream = stream

    @property
    def output_sample_rate(self) -> int:
        return 16_000

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        _ = text, voice
        return self._stream


@pytest.mark.asyncio
async def test_second_speak_supersedes_first_no_interleave() -> None:
    """A new speak() must supersede the in-flight one — frames never interleave.

    Regression for the greeting/reply clobber race: ``_active_tts_stream`` is a
    single slot, and without serialization two concurrent ``speak()`` loops push
    frames into ``transport.send_audio`` at once (interleaved on the wire) while
    ``barge_in`` can only cancel the last-registered stream. Speaking again must
    cancel the previous stream and take sole ownership: once the second stream's
    frames begin, none of the first stream's frames may follow.
    """
    a_frames = [_greeting_frame(0xA0 + i) for i in range(6)]
    b_frames = [_greeting_frame(0xB0 + i) for i in range(3)]
    a_stream = _SlowTtsStream(a_frames)
    b_stream = _SlowTtsStream(b_frames)
    transport = _FakeTransport([])

    loop = CallLoop(
        transport=transport,
        asr=_FakeASR([]),
        tts=_SingleStreamTTS(a_stream),
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad(),
        endpointer=_make_endpointer(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=_noop,
        voice=_VOICE,
        call_id=_CALL_ID,
    )

    async def _tokens(word: str) -> AsyncIterator[str]:
        yield word

    # Start the first (long) utterance; let it send a couple of frames.
    first = asyncio.create_task(loop.speak(_tokens("first utterance")))
    for _ in range(3):
        await asyncio.sleep(0)
    assert transport.sent_audio, "first speak() should have started sending"

    # Now swap the TTS to the second stream and speak again — it must supersede.
    loop._tts = _SingleStreamTTS(b_stream)
    second = asyncio.create_task(loop.speak(_tokens("second utterance")))

    await asyncio.wait_for(asyncio.gather(first, second), timeout=5.0)

    # The first stream was cancelled (superseded); the second ran to completion.
    assert a_stream.cancel_called is True
    # Single-owner invariant: once any B-frame is sent, no A-frame follows it.
    sent = transport.sent_audio
    last_b = max(
        (i for i, f in enumerate(sent) if f in b_frames),
        default=-1,
    )
    a_after_b = [f for f in sent[last_b + 1 :] if f in a_frames]
    assert a_after_b == [], f"first-stream frames leaked after supersede: {sent!r}"
    # The full second utterance must have been delivered.
    assert [f for f in sent if f in b_frames] == b_frames


@pytest.mark.asyncio
async def test_first_inbound_onset_cancels_greeting_without_gating() -> None:
    """Barge-in must catch a caller-onset on the VERY FIRST inbound frame.

    Regression for the onset-before-registration race: the pump task is created
    before the greeting, and ``_FakeTransport``'s inbound generator yields its
    first frame WITHOUT awaiting, so the pump can observe an ONSET before the
    greeting task has run. If the greeting registers its active stream only when
    its task runs, that first onset sees ``_active_tts_stream is None`` and
    barge-in is silently missed. The greeting's stream must be registered before
    the pump can process any inbound audio, so even a first-frame onset cancels
    it.
    """
    # A full 16 kHz silero window scored as speech → ONSET on the first frame.
    speech_frame = PcmFrame(samples=bytes(1024), sample_rate=16_000, monotonic_ts_ns=0)
    tts = _FakeTTS([_greeting_frame(1), _greeting_frame(2), _greeting_frame(3)])
    transport = _FakeTransport([speech_frame])  # ungated; first __anext__ won't await

    loop = _build_loop(
        transport,
        _FakeASR([]),
        tts,
        _FakeGuard([_allow_result()]),
        _noop,
        vad=_make_speaking_vad(),
        greeting="Hello there, the caller barges in on the first frame.",
    )
    await asyncio.wait_for(loop.run(), timeout=5.0)

    assert tts.last_stream is not None
    assert tts.last_stream.cancel_called is True


# ---------------------------------------------------------------------------
# LIVE BUG 1 — the inbound chain must be rate-consistent at the engine's
# telephony rate. The RtpMediaTransport yields 8 kHz G.711 frames
# (inbound_sample_rate == 8000); the VAD + endpointer must accept them at that
# exact rate. A 16 kHz detector against an 8 kHz stream raises
# ``ValueError: frame rate 8000 != detector rate 16000`` inside the pump, fails
# the TaskGroup, and cancels the greeting before it finishes — the caller hears
# silence. silero-vad runs natively at 8 kHz, so the inbound chain stays at the
# wire rate (the STT resamples 8->16 kHz internally; ADR-0017).
# ---------------------------------------------------------------------------


_G711_INBOUND_RATE: Final[int] = 8_000


def _silence_frame_8k(index: int) -> PcmFrame:
    """A silent 8 kHz PCM16 frame (256 samples = one 32 ms silero window at 8 kHz)."""
    return PcmFrame(
        samples=bytes(512),
        sample_rate=_G711_INBOUND_RATE,
        monotonic_ts_ns=index * 32_000_000,
    )


class _Transport8k(_FakeTransport):
    """MediaTransport fake at the engine's real G.711 inbound rate (8000 Hz)."""

    @property
    def inbound_sample_rate(self) -> int:
        return _G711_INBOUND_RATE


def _make_vad_8k() -> VoiceActivityDetector:
    """A silero VAD at the engine inbound rate (8 kHz) with a silent fake model."""

    def _silent_model(window_pcm16: bytes, sample_rate: int) -> float:
        _ = window_pcm16, sample_rate
        return 0.0

    return VoiceActivityDetector(model=_silent_model, sample_rate_hz=_G711_INBOUND_RATE)


def _make_speaking_vad_8k() -> VoiceActivityDetector:
    """An 8 kHz VAD whose model always returns 1.0 → ONSET on the first window."""

    def _voiced_model(window_pcm16: bytes, sample_rate: int) -> float:
        _ = window_pcm16, sample_rate
        return 1.0

    return VoiceActivityDetector(model=_voiced_model, sample_rate_hz=_G711_INBOUND_RATE)


def _make_endpointer_8k() -> Endpointer:
    """An endpointer at the engine inbound rate (8 kHz)."""
    return Endpointer(silence_ms=500, sample_rate_hz=_G711_INBOUND_RATE)


@pytest.mark.asyncio
async def test_inbound_8khz_stream_drives_vad_and_one_turn() -> None:
    """An 8 kHz inbound stream feeds VAD->endpoint->ASR with NO rate ValueError.

    Reproduces the live inbound topology: the transport yields 8 kHz G.711
    frames, the VAD + endpointer are built at that same engine rate, and the ASR
    finalises exactly one end-of-turn. The pump must feed every 8 kHz frame into
    the VAD without raising, and the single finalised turn must deliver exactly
    once. Before the fix the adapter built the VAD/endpointer at 16 kHz, so the
    very first 8 kHz frame raised ``ValueError: frame rate 8000 != detector rate
    16000`` and no turn was ever delivered.
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    # Several real 8 kHz windows so the VAD actually scores them (one window is
    # 256 samples = 512 bytes at 8 kHz; each frame here is exactly one window).
    frames = [_silence_frame_8k(i) for i in range(4)]

    loop = CallLoop(
        transport=_Transport8k(frames),
        asr=_FakeASR([("caller said hello", True, True)]),
        tts=_FakeTTS([]),
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad_8k(),
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=capture,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="",
    )

    # No ValueError must escape run(); the single end-of-turn delivers once.
    await asyncio.wait_for(loop.run(), timeout=5.0)

    assert delivered == ["caller said hello"]


@pytest.mark.asyncio
async def test_inbound_rate_mismatch_is_what_crashes_the_pump() -> None:
    """Root-cause proof (rule 25): a 16 kHz VAD vs an 8 kHz stream crashes run().

    This pins the exact failure the fix removes: building the detector at the
    wrong rate (16 kHz, the old adapter default) and feeding it the engine's real
    8 kHz frames raises the live ``ValueError`` from inside the pump, which the
    TaskGroup surfaces. The companion test above proves the rate-consistent build
    does NOT raise — together they show the mismatch, not the CallLoop, is the
    defect, and that matching the rate fixes it.
    """
    frames = [_silence_frame_8k(0)]

    # Deliberately mis-built at 16 kHz against the 8 kHz transport.
    loop = CallLoop(
        transport=_Transport8k(frames),
        asr=_FakeASR([]),
        tts=_FakeTTS([]),
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad(),  # 16 kHz default — the bug
        endpointer=_make_endpointer(),  # 16 kHz default
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=_noop,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="",
    )

    with pytest.raises(BaseExceptionGroup) as excinfo:
        await asyncio.wait_for(loop.run(), timeout=5.0)

    matched, _rest = excinfo.value.split(ValueError)
    assert matched is not None
    value_errors = [e for e in matched.exceptions if isinstance(e, ValueError)]
    assert any("frame rate 8000 != detector rate 16000" in str(e) for e in value_errors)


# ---------------------------------------------------------------------------
# LIVE BUG 2 — async-generator teardown lifecycle. The pump iterates the
# transport's ``inbound_audio()`` async generator; on shutdown the consuming
# task must drive that generator's close on its OWN task (cancel + unwind), never
# leave it suspended for a foreign ``aclose()`` or the GC finalizer to close
# while a pull is still running (``RuntimeError: aclose(): asynchronous generator
# is already running``). The real engine generator awaits between yields, so a
# cancellation lands at an inner await — the regression case.
# ---------------------------------------------------------------------------


class _EngineLikeTransport(_FakeTransport):
    """A transport whose ``inbound_audio()`` awaits between yields (like RTP).

    The real :class:`RtpMediaTransport` parks its inbound generator at
    ``await self._next_datagram()`` between yields, so a cancellation of the pump
    lands at an *inner* await, not at the ``yield``. This fake reproduces that by
    sleeping each iteration. The generator's ``finally`` records that it ran, so
    the teardown test can assert the generator was closed (its ``finally`` ran),
    not left dangling.
    """

    def __init__(self) -> None:
        super().__init__([])
        self._n = 0
        self.gen_closed = False
        self.gen_started = asyncio.Event()

    @property
    def inbound_sample_rate(self) -> int:
        return _G711_INBOUND_RATE

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        async def _gen() -> AsyncIterator[PcmFrame]:
            try:
                while True:
                    self.gen_started.set()
                    # Inner await between yields — the engine parks here.
                    await asyncio.sleep(0.005)
                    self._n += 1
                    yield _silence_frame_8k(self._n)
            finally:
                self.gen_closed = True

        return _gen()


@pytest.mark.asyncio
async def test_teardown_mid_iteration_no_aclose_race(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Cancelling run() mid-iteration tears the inbound generator down cleanly.

    Runs the loop against an engine-like transport whose inbound generator parks
    at an inner await between yields, then cancels run() while the pump is
    iterating. The cancellation must propagate as a clean ``CancelledError``, the
    inbound generator must be closed (its ``finally`` ran), no task must be left
    leaked, and — the live symptom — there must be NO ``RuntimeWarning`` (e.g.
    ``aclose(): asynchronous generator is already running`` surfaced as a warning,
    or an un-awaited coroutine/generator). All warnings are captured and asserted
    clean. The pump iterates ``inbound_audio()`` under ``contextlib.aclosing`` so
    it closes the generator on its own task as it unwinds.
    """
    tasks_before = set(asyncio.all_tasks())
    transport = _EngineLikeTransport()

    block_forever = asyncio.Event()

    async def stuck_deliver(text: str) -> None:
        _ = text
        await block_forever.wait()

    loop = CallLoop(
        transport=transport,
        asr=_SlowDrainASR(),
        tts=_FakeTTS([]),
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad_8k(),
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=stuck_deliver,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="",
    )

    run_task = asyncio.create_task(loop.run())
    # Wait until the inbound generator is actually producing (pump is iterating).
    await asyncio.wait_for(transport.gen_started.wait(), timeout=5.0)
    await asyncio.sleep(0.01)

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    # The inbound generator was closed (its finally ran), not left dangling.
    assert transport.gen_closed is True

    # No tasks leaked by the cancelled run.
    leaked = set(asyncio.all_tasks()) - tasks_before - {asyncio.current_task()}
    assert leaked == set(), f"leaked tasks after cancellation: {leaked}"

    # No "aclose(): asynchronous generator is already running" RuntimeError, and
    # no RuntimeWarning (un-awaited coroutine / generator-already-running) leaked.
    runtime_warnings = [
        w for w in recwarn.list if issubclass(w.category, RuntimeWarning)
    ]
    assert runtime_warnings == [], (
        "unexpected RuntimeWarnings during teardown: "
        f"{[str(w.message) for w in runtime_warnings]}"
    )


class _CooperativePcmTTS:
    """A real :class:`PcmFrameStream` over a pure-async cooperative byte source.

    This exercises the *real* barge-in teardown path — not a fake. ``synthesize``
    returns a genuine ``PcmFrameStream`` whose per-segment byte source yields PCM
    chunks while polling the shared stop flag. It sets ``parked`` after yielding
    its first chunk, so a test can hold off the barge-in trigger until the
    greeting consumer (``_play`` on its own task) is actually parked inside the
    frame generator's ``__anext__`` — the precise moment the cross-task aclose
    race fires. The point is to prove the production ``PcmFrameStream.cancel()``
    does NOT ``aclose()`` the running frame generator from the (different) pump
    task — which would raise ``RuntimeError: aclose(): asynchronous generator is
    already running``. A ``cancel()`` that acloses the running generator makes
    this test fail.
    """

    def __init__(self) -> None:
        self.last_stop: threading.Event | None = None
        self.parked = asyncio.Event()

    @property
    def output_sample_rate(self) -> int:
        return _G711_INBOUND_RATE

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        _ = voice
        stop = threading.Event()
        self.last_stop = stop

        def _open_segment(sentence: str) -> SegmentSource:
            _ = sentence

            async def _chunks() -> AsyncIterator[bytes]:
                # First chunk goes out immediately; after the consumer pulls it
                # and parks for the next, signal `parked` so the test can fire the
                # barge-in exactly when _play is inside the frame __anext__.
                first = True
                for _ in range(200):
                    if stop.is_set():
                        return
                    if first:
                        yield bytes(160)
                        self.parked.set()
                        first = False
                        continue
                    await asyncio.sleep(0.005)
                    yield bytes(160)

            return SegmentSource(chunks=_chunks())

        return PcmFrameStream(
            text=text,
            open_segment=_open_segment,
            sample_rate=_G711_INBOUND_RATE,
            stop=stop,
        )


class _BargeInTransport(_FakeTransport):
    """8 kHz transport that yields its single speech frame only after `release`.

    The greeting must be parked inside the frame generator's ``__anext__`` before
    the pump processes the speech frame (and fires barge-in), or the cross-task
    aclose race cannot occur. The test sets ``release`` once the TTS reports the
    greeting is parked, then this transport yields the one speech frame.
    """

    def __init__(self) -> None:
        super().__init__([])
        self.release = asyncio.Event()

    @property
    def inbound_sample_rate(self) -> int:
        return _G711_INBOUND_RATE

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        release = self.release

        async def _gen() -> AsyncIterator[PcmFrame]:
            await release.wait()
            # One speech frame, then the generator returns so the loop ends
            # naturally once barge-in has fired and the greeting has stopped.
            yield _silence_frame_8k(0)

        return _gen()


@pytest.mark.asyncio
async def test_barge_in_during_greeting_no_aclose_race(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Barge-in on the greeting must not raise the cross-task aclose RuntimeError.

    This is the live regression guard for ``RuntimeError: aclose(): asynchronous
    generator is already running``. The greeting plays a *real* ``PcmFrameStream``
    on its own task; the pump (a different task) sees a speech ONSET on the first
    8 kHz frame and calls ``barge_in()`` → ``stream.cancel()`` while the greeting
    is parked inside the frame generator's ``__anext__`` (the transport withholds
    the speech frame until the greeting reports it is parked). ``cancel()`` must
    stop the stream via the stop flag — never ``aclose()`` the running generator
    from the pump task. run() must complete with no exception and no
    ``RuntimeWarning``; the greeting's stop flag must be set (barge-in took
    effect). Mutating ``cancel()`` to aclose the running generator makes this fail.
    """
    transport = _BargeInTransport()
    tts = _CooperativePcmTTS()

    loop = CallLoop(
        transport=transport,
        asr=_FakeASR([]),
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=_make_speaking_vad_8k(),  # ONSET on the first 8 kHz window
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=_noop,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="Hello there, this greeting plays while the caller barges in.",
    )

    run_task = asyncio.create_task(loop.run())
    # Wait until the greeting consumer is parked inside the frame __anext__, THEN
    # release the speech frame so the pump's barge-in lands on a running pull.
    await asyncio.wait_for(tts.parked.wait(), timeout=5.0)
    await asyncio.sleep(0)
    transport.release.set()

    # No exception (in particular no aclose RuntimeError) must escape run().
    await asyncio.wait_for(run_task, timeout=5.0)

    # Barge-in took effect: the greeting stream was stopped.
    assert tts.last_stop is not None
    assert tts.last_stop.is_set() is True

    runtime_warnings = [
        w for w in recwarn.list if issubclass(w.category, RuntimeWarning)
    ]
    assert runtime_warnings == [], (
        "unexpected RuntimeWarnings during barge-in teardown: "
        f"{[str(w.message) for w in runtime_warnings]}"
    )


# ---------------------------------------------------------------------------
# SELF-ECHO BARGE-IN (ADR-0023) — the gateway reflects the agent's own TTS back
# on the inbound path; the VAD transcribes it as the caller, and a single ONSET
# barged the agent in, ending its own turn (the live self-interruption loop,
# call 20260617_033116). In the default ``gated`` mode, while TTS is playing a
# barge-in needs a SUSTAINED voiced run — short echo blips must NOT interrupt,
# but a genuine sustained interruption still must.
# ---------------------------------------------------------------------------


class _ScriptedVadModel:
    """A :class:`VadModel` that returns a preset probability per scored window.

    One probability is consumed per window; once the script is exhausted it
    returns ``0.0`` (silence) forever. Stateless beyond the cursor, so the
    detector's hysteresis state machine still derives ONSET/OFFSET edges.
    """

    def __init__(self, probabilities: list[float]) -> None:
        self._probs = probabilities
        self._cursor = 0

    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        _ = window_pcm16, sample_rate
        if self._cursor < len(self._probs):
            value = self._probs[self._cursor]
            self._cursor += 1
            return value
        return 0.0


def _scripted_vad_8k(probabilities: list[float]) -> VoiceActivityDetector:
    """An 8 kHz silero VAD driven by a per-window probability script."""
    return VoiceActivityDetector(
        model=_ScriptedVadModel(probabilities),
        sample_rate_hz=_G711_INBOUND_RATE,
    )


def _one_window_frame_8k() -> PcmFrame:
    """Exactly one 8 kHz silero window (256 samples → 512 bytes)."""
    return PcmFrame(
        samples=bytes(512), sample_rate=_G711_INBOUND_RATE, monotonic_ts_ns=0
    )


class _ScriptedInboundTransport(_FakeTransport):
    """8 kHz transport that releases its (one-window) frames on a gate.

    Each call to :meth:`inbound_audio` yields ``len(frames)`` one-window frames,
    but only after ``release`` is set, and with a cooperative ``sleep(0)`` between
    frames so the concurrent greeting playout task interleaves window-by-window
    (mirrors a live call where the echo arrives once the agent is speaking). One
    inbound frame is exactly one 8 kHz silero window, so the VAD window ordinal
    advances by one per frame.
    """

    def __init__(self, n_frames: int) -> None:
        super().__init__([_one_window_frame_8k() for _ in range(n_frames)])
        self.release = asyncio.Event()

    @property
    def inbound_sample_rate(self) -> int:
        return _G711_INBOUND_RATE

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        frames = self._frames
        release = self.release

        async def _gen() -> AsyncIterator[PcmFrame]:
            await release.wait()
            for frame in frames:
                await asyncio.sleep(0)  # let the greeting playout interleave
                yield frame

        return _gen()


class _LongSlowGreetingTTS:
    """StreamingTTS returning a fresh self-completing slow greeting per call.

    Each ``synthesize`` returns a new :class:`_SlowTtsStream` that yields many
    frames, one per cooperative ``sleep(0)``, so the greeting stays the active TTS
    stream across the whole inbound echo run AND completes on its own when no
    barge-in cancels it (so ``run()`` terminates). ``cancel()`` stops it promptly.
    """

    def __init__(self, n_frames: int) -> None:
        self._n_frames = n_frames
        self.last_stream: _SlowTtsStream | None = None

    @property
    def output_sample_rate(self) -> int:
        return _G711_INBOUND_RATE

    def synthesize(
        self, text: AsyncIterator[str], voice: str, *, sample_rate: int | None = None
    ) -> TtsStream:
        _ = text, voice, sample_rate
        stream = _SlowTtsStream(
            [_greeting_frame(i % 250) for i in range(self._n_frames)]
        )
        self.last_stream = stream
        return stream


def _build_barge_in_loop(  # noqa: PLR0913 — test factory mirrors CallLoop's keyword surface
    transport: _FakeTransport,
    vad: VoiceActivityDetector,
    tts: StreamingTTS,
    *,
    mode: BargeInMode,
    min_voiced_windows: int,
    greeting: str,
) -> CallLoop:
    return CallLoop(
        transport=transport,
        asr=_FakeASR([]),
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=vad,
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=_noop,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting=greeting,
        barge_in_mode=mode,
        barge_in_min_voiced_windows=min_voiced_windows,
        barge_in_tail_windows=8,
    )


@pytest.mark.asyncio
async def test_gated_short_echo_during_tts_does_not_barge_in() -> None:
    """A SHORT echo blip while the greeting plays must NOT cancel it (gated mode).

    Reproduces the live self-interruption: the agent is speaking (greeting TTS
    active), and the inbound stream carries a short voiced run (echo of the
    agent's own audio) — here 6 voiced windows then silence, below the 13-window
    sustained threshold. The greeting stream must run to completion uncancelled.
    """
    # 6 voiced windows (echo blip) then silence; below the 13-window threshold.
    blip = [0.95] * 6 + [0.0] * 20
    vad = _scripted_vad_8k(blip)
    # A long self-completing greeting stays the active TTS stream across the whole
    # echo blip (so the gate is armed) and finishes on its own when not cancelled.
    tts = _LongSlowGreetingTTS(n_frames=60)
    transport = _ScriptedInboundTransport(len(blip))

    loop = _build_barge_in_loop(
        transport,
        vad,
        tts,
        mode=BargeInMode.GATED,
        min_voiced_windows=13,
        greeting="The agent is giving a long spoken answer here.",
    )

    run_task = asyncio.create_task(loop.run())
    # Let the greeting start + register its stream, then release the echo frames.
    for _ in range(5):
        await asyncio.sleep(0)
    transport.release.set()
    await asyncio.wait_for(run_task, timeout=5.0)

    assert tts.last_stream is not None
    assert tts.last_stream.cancel_called is False, (
        "short echo blip during TTS playout must NOT barge in (gated mode)"
    )


@pytest.mark.asyncio
async def test_gated_sustained_speech_during_tts_barges_in() -> None:
    """A SUSTAINED voiced run while the greeting plays MUST cancel it (gated).

    A genuine caller interruption keeps voicing past the 13-window threshold with
    no offset — barge-in must still fire so the agent stops. This proves the gate
    preserves intentional barge-in (it does not simply disable it during TTS).
    """
    # 30 continuous voiced windows → well past the 13-window threshold.
    sustained = [0.95] * 30
    vad = _scripted_vad_8k(sustained)
    tts = _LongSlowGreetingTTS(n_frames=60)
    transport = _ScriptedInboundTransport(len(sustained))

    loop = _build_barge_in_loop(
        transport,
        vad,
        tts,
        mode=BargeInMode.GATED,
        min_voiced_windows=13,
        greeting="The agent starts a long answer but the caller cuts in.",
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(5):
        await asyncio.sleep(0)
    transport.release.set()
    await asyncio.wait_for(run_task, timeout=5.0)

    assert tts.last_stream is not None
    assert tts.last_stream.cancel_called is True, (
        "a sustained caller interruption during TTS must still barge in"
    )


@pytest.mark.asyncio
async def test_full_mode_short_blip_barges_in_legacy_behaviour() -> None:
    """``full`` mode reproduces the pre-fix behaviour: first ONSET barges in.

    This documents that the legacy immediate-barge-in path still exists for
    echo-cancelled gateways — and that it is exactly what made the short echo
    blip self-interrupt before ``gated`` became the default.
    """
    blip = [0.95] * 6 + [0.0] * 20
    vad = _scripted_vad_8k(blip)
    tts = _LongSlowGreetingTTS(n_frames=60)
    transport = _ScriptedInboundTransport(len(blip))

    loop = _build_barge_in_loop(
        transport,
        vad,
        tts,
        mode=BargeInMode.FULL,
        min_voiced_windows=13,
        greeting="The agent speaks and any onset cuts it off in full mode.",
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(5):
        await asyncio.sleep(0)
    transport.release.set()
    await asyncio.wait_for(run_task, timeout=5.0)

    assert tts.last_stream is not None
    assert tts.last_stream.cancel_called is True


class _DrainThenFinalASR:
    """ASR fake that drains the audio it RECEIVES, then yields one final per turn.

    Mirrors a streaming ASR: it consumes the audio stream and yields ``is_final``
    transcripts. ``end_of_turn`` is configurable — ``False`` models the sherpa ASR
    (the endpointer owns the boundary; ADR-0008), ``True`` models a fused engine
    like Deepgram Flux that sets the turn boundary natively. Because the final is
    emitted only after the audio it gets is drained, the test makes the *delivery
    suppression* the deterministic variable.

    Crucially it only ever sees the frames the pump actually FORWARDS — echo that
    the pump drops from the ASR input never reaches here, so an echo transcript is
    never produced on EITHER end-of-turn path. It records how many frames it drew
    so a test can assert the pump withheld the echo.
    """

    def __init__(self, text: str, *, end_of_turn: bool = False) -> None:
        self._text = text
        self._end_of_turn = end_of_turn
        self.frames_drained = 0

    @property
    def input_sample_rate(self) -> int:
        return _G711_INBOUND_RATE

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        text = self._text
        end_of_turn = self._end_of_turn

        async def _gen() -> AsyncIterator[Transcript]:
            async for _frame in audio:  # drain only the frames the pump forwards
                self.frames_drained += 1
            # A real recogniser yields a transcript only when it actually received
            # audio. If the pump withheld ALL frames (echo during TTS), it gets
            # nothing and yields nothing — so withheld echo produces no turn on any
            # end-of-turn path (endpointer OR native).
            if self.frames_drained > 0:
                yield Transcript(
                    text=text, is_final=True, end_of_turn=end_of_turn, confidence=1.0
                )

        return _gen()


@pytest.mark.asyncio
async def test_gated_echo_blip_during_tts_delivers_no_turn() -> None:
    """An echo blip during TTS must NOT be delivered as a caller turn (codex #1).

    The deeper self-interruption route: even when a short echo blip does not call
    ``barge_in()``, the endpointer still fires an end-of-turn on its trailing
    silence and the ASR's final fragment is delivered to the agent as caller
    input — re-triggering an interrupt. While the agent's TTS is playing and the
    speech was not an authorised (sustained) barge-in, the turn delivery must be
    suppressed. Here a 6-window echo blip (below threshold) is followed by enough
    silent windows to fire the endpointer; ``deliver_turn`` must NEVER be called.

    Discriminating: ``_DrainThenFinalASR`` emits its final only AFTER the
    endpointer has fired (it drains all audio first), so the turn is delivered iff
    the endpointer EOT was NOT suppressed — i.e. the test fails without the fix.
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    # 6 voiced echo windows, then 30 silent windows (> the 16-window endpointer
    # silence at 8 kHz) so the endpointer fires an end-of-turn on the echo while
    # the long greeting is still playing (the gate is armed throughout).
    blip = [0.95] * 6 + [0.0] * 30
    vad = _scripted_vad_8k(blip)
    tts = _LongSlowGreetingTTS(n_frames=120)
    transport = _ScriptedInboundTransport(len(blip))

    loop = CallLoop(
        transport=transport,
        asr=_DrainThenFinalASR("NO"),
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=vad,
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=capture,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="The agent is giving a long spoken answer here.",
        barge_in_mode=BargeInMode.GATED,
        barge_in_min_voiced_windows=13,
        barge_in_tail_windows=8,
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(5):
        await asyncio.sleep(0)
    transport.release.set()
    await asyncio.wait_for(run_task, timeout=5.0)

    assert delivered == [], (
        "echoed speech during TTS playout must not be delivered as a caller turn"
    )
    assert tts.last_stream is not None
    assert tts.last_stream.cancel_called is False


@pytest.mark.asyncio
async def test_gated_normal_turn_during_silence_still_delivered() -> None:
    """A real caller turn during SILENCE (agent not speaking) still delivers.

    No-regression guard for the delivery suppression: with no greeting/TTS the
    gate is never armed, so a finalised end-of-turn is delivered normally. (This
    mirrors the existing end-of-turn delivery path, now with the gate present.)
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    loop = CallLoop(
        transport=_Transport8k([_silence_frame_8k(0)]),
        asr=_FakeASR([("hello there", True, True)]),
        tts=_FakeTTS([]),
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad_8k(),
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=capture,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="",  # no TTS → gate never armed
        barge_in_mode=BargeInMode.GATED,
        barge_in_min_voiced_windows=13,
        barge_in_tail_windows=8,
    )
    await asyncio.wait_for(loop.run(), timeout=5.0)
    assert delivered == ["hello there"]


@pytest.mark.asyncio
async def test_gated_echo_with_native_asr_eot_delivers_no_turn() -> None:
    """Echo during TTS must not deliver a turn even with a NATIVE-EOT ASR (codex #A).

    A fused recogniser (e.g. Deepgram Flux) sets ``end_of_turn=True`` itself, which
    ``_asr`` honours independently of the endpointer counter. Suppressing only the
    endpointer EOT therefore would NOT stop echo from being delivered via a native
    EOT. The robust fix withholds the echo audio from the ASR entirely while the
    gate is armed and the run is unauthorised, so NO echo transcript is produced on
    either path. Assert: no turn delivered, and the ASR drew far fewer frames than
    were sent (the echo was withheld).
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    blip = [0.95] * 6 + [0.0] * 30
    vad = _scripted_vad_8k(blip)
    tts = _LongSlowGreetingTTS(n_frames=120)
    transport = _ScriptedInboundTransport(len(blip))
    asr = _DrainThenFinalASR("NO", end_of_turn=True)  # native EOT (Deepgram-style)

    loop = CallLoop(
        transport=transport,
        asr=asr,
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=vad,
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=capture,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="The agent is giving a long spoken answer here.",
        barge_in_mode=BargeInMode.GATED,
        barge_in_min_voiced_windows=13,
        barge_in_tail_windows=8,
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(5):
        await asyncio.sleep(0)
    transport.release.set()
    await asyncio.wait_for(run_task, timeout=5.0)

    assert delivered == [], (
        "echo with a native-EOT ASR must not be delivered as a caller turn"
    )
    # The echo audio was withheld from the ASR (it saw far fewer than 36 frames).
    assert asr.frames_drained < len(blip), (
        "the pump must withhold echo frames from the ASR during TTS playout"
    )


@pytest.mark.asyncio
async def test_gated_sustained_barge_in_cancels_and_delivers_transcript() -> None:
    """A sustained interruption during TTS both CANCELS the agent AND delivers.

    Integrated proof (codex noted the cancel was tested but not the delivery): a
    sustained caller run past the threshold cancels the playing greeting (barge-in)
    AND its transcript is delivered as a caller turn — the run is authorised, so its
    turn is not suppressed. A native-EOT ASR finalises the (authorised) turn.
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    sustained = [0.95] * 30  # well past the 13-window threshold, no offset
    vad = _scripted_vad_8k(sustained)
    tts = _LongSlowGreetingTTS(n_frames=120)
    transport = _ScriptedInboundTransport(len(sustained))
    asr = _DrainThenFinalASR("please stop", end_of_turn=True)

    loop = CallLoop(
        transport=transport,
        asr=asr,
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=vad,
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=capture,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="The agent starts a long answer but the caller cuts in.",
        barge_in_mode=BargeInMode.GATED,
        barge_in_min_voiced_windows=13,
        barge_in_tail_windows=8,
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(5):
        await asyncio.sleep(0)
    transport.release.set()
    await asyncio.wait_for(run_task, timeout=5.0)

    assert tts.last_stream is not None
    assert tts.last_stream.cancel_called is True, "sustained run must cancel the agent"
    assert delivered == ["please stop"], (
        "an authorised sustained interruption must deliver its transcript"
    )


@pytest.mark.asyncio
async def test_gated_sustained_turn_starting_in_tail_is_delivered() -> None:
    """A real caller turn that authorises during the post-TTS tail delivers (codex #B).

    The gate stays armed for a tail after TTS ends. A SUSTAINED run during that tail
    must authorise itself and deliver its turn — it must not be suppressed as echo
    just because TTS recently stopped. A LARGE tail (40 windows > the 13-window
    threshold) is used so the run reaches its sustained threshold while still in the
    tail: the pump must drive the gate's authorisation while armed (not only while
    TTS is active), or the whole run is withheld from the ASR and nothing delivers.
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    # A short greeting (ends fast) arms a long tail; then a sustained caller run.
    script = [0.0] * 3 + [0.95] * 30
    vad = _scripted_vad_8k(script)
    tts = _LongSlowGreetingTTS(n_frames=2)  # greeting ends fast → long tail follows
    transport = _ScriptedInboundTransport(len(script))
    asr = _DrainThenFinalASR("hello operator", end_of_turn=True)

    loop = CallLoop(
        transport=transport,
        asr=asr,
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=vad,
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=capture,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="Hi.",
        barge_in_mode=BargeInMode.GATED,
        barge_in_min_voiced_windows=13,
        barge_in_tail_windows=40,  # tail outlasts the threshold → authorise in tail
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(5):
        await asyncio.sleep(0)
    transport.release.set()
    await asyncio.wait_for(run_task, timeout=5.0)

    assert delivered == ["hello operator"], (
        "a sustained real turn that authorises during the tail must be delivered"
    )


class _PerFrameFinalASR:
    """ASR fake that yields one end-of-turn-less final per frame it RECEIVES.

    Like the sherpa ASR, every forwarded frame produces an ``is_final`` hypothesis
    with ``end_of_turn=False`` (the endpointer owns the boundary). Frames the pump
    withholds (echo) never reach it, so it emits nothing for them. Used to prove the
    endpointer does not accumulate stale echo silence: only a real turn's endpointer
    end-of-turn may promote a final to a delivered turn.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self.frames_drained = 0

    @property
    def input_sample_rate(self) -> int:
        return _G711_INBOUND_RATE

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        text = self._text

        async def _gen() -> AsyncIterator[Transcript]:
            async for _frame in audio:
                self.frames_drained += 1
                yield Transcript(
                    text=text, is_final=True, end_of_turn=False, confidence=1.0
                )

        return _gen()


@pytest.mark.asyncio
async def test_echo_then_tail_expiry_does_not_leak_eot_into_next_turn() -> None:
    """A withheld echo run must not arm the endpointer for a later real turn.

    Regression for the stale-silence edge: an echo blip during TTS, then TTS + the
    tail end with NO real speech, then later a genuine caller turn during silence.
    The echo's OFFSET must not have armed the endpointer (it was withheld), so the
    only end-of-turn is the real turn's own — exactly one delivery, with the real
    text. If the withheld echo leaked into the endpointer, a spurious end-of-turn
    after tail expiry would consume the real turn's first final prematurely.
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    # Echo (6 voiced) during a SHORT greeting + tail, then long silence past the
    # tail, then a real turn (voiced) followed by endpointer silence.
    script = [0.95] * 6 + [0.0] * 40 + [0.95] * 4 + [0.0] * 20
    vad = _scripted_vad_8k(script)
    tts = _LongSlowGreetingTTS(n_frames=8)  # greeting ends early; tail then expires
    transport = _ScriptedInboundTransport(len(script))
    asr = _PerFrameFinalASR("real turn")

    loop = CallLoop(
        transport=transport,
        asr=asr,
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=vad,
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=capture,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="Hi.",
        barge_in_mode=BargeInMode.GATED,
        barge_in_min_voiced_windows=13,
        barge_in_tail_windows=8,
    )

    run_task = asyncio.create_task(loop.run())
    for _ in range(5):
        await asyncio.sleep(0)
    transport.release.set()
    await asyncio.wait_for(run_task, timeout=5.0)

    # Exactly one real turn delivered; no spurious echo-driven turn.
    assert delivered == ["real turn"]


class _PrePlayoutGreetingStream:
    """A greeting TtsStream that registers but WITHHOLDS its first frame until told.

    Models TTS synthesis/startup latency: the stream is the active stream (so
    barge-in can target it) but emits no audio yet. A test sets ``emit`` to release
    the first frame. While parked, ``_tts_audio_active`` stays False, so the echo
    gate must NOT be armed — a real short caller turn in this window must reach the
    ASR (codex finding C).
    """

    def __init__(self, frames: list[PcmFrame], emit: asyncio.Event) -> None:
        self._frames = frames
        self._emit = emit
        self.cancel_called = False
        self._cancelled = False
        self._gen = self._iter()

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        return await self._gen.__anext__()

    async def _iter(self) -> AsyncGenerator[PcmFrame]:
        await self._emit.wait()  # synthesis latency: no audio until released
        for frame in self._frames:
            if self._cancelled:
                return
            yield frame

    async def flush(self) -> None:
        pass

    async def cancel(self) -> None:
        self.cancel_called = True
        self._cancelled = True
        self._emit.set()  # unblock the parked generator so it can finish

    async def aclose(self) -> None:
        self._cancelled = True
        self._emit.set()
        await self._gen.aclose()


class _PrePlayoutGreetingTTS:
    """StreamingTTS returning a single pre-playout-latency greeting stream."""

    def __init__(self, frames: list[PcmFrame], emit: asyncio.Event) -> None:
        self._frames = frames
        self._emit = emit
        self.last_stream: _PrePlayoutGreetingStream | None = None

    @property
    def output_sample_rate(self) -> int:
        return _G711_INBOUND_RATE

    def synthesize(
        self, text: AsyncIterator[str], voice: str, *, sample_rate: int | None = None
    ) -> TtsStream:
        _ = text, voice, sample_rate
        stream = _PrePlayoutGreetingStream(self._frames, self._emit)
        self.last_stream = stream
        return stream


@pytest.mark.asyncio
async def test_short_real_turn_before_first_tts_frame_is_delivered() -> None:
    """A short real caller turn during TTS synthesis latency is delivered (codex C).

    The greeting stream is registered (so barge-in could target it) but emits no
    audio yet — the echo gate must NOT arm on mere registration, only once audio is
    actually on the wire. A SHORT (sub-threshold) caller turn that arrives in this
    pre-playout window is genuine (nothing has been transmitted, so it cannot be
    echo) and must reach the ASR + be delivered. After it ends the greeting audio
    is released so the loop completes.
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    # A short caller turn (4 voiced windows, below the 13-window barge-in
    # threshold), then enough silence to fire the endpointer end-of-turn.
    script = [0.95] * 4 + [0.0] * 24
    vad = _scripted_vad_8k(script)
    emit = asyncio.Event()
    tts = _PrePlayoutGreetingTTS([_greeting_frame(1), _greeting_frame(2)], emit)
    transport = _ScriptedInboundTransport(len(script))
    asr = _DrainThenFinalASR("hello", end_of_turn=False)

    loop = CallLoop(
        transport=transport,
        asr=asr,
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=vad,
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=capture,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="A greeting that is still synthesising while the caller speaks.",
        barge_in_mode=BargeInMode.GATED,
        barge_in_min_voiced_windows=13,
        barge_in_tail_windows=8,
    )

    run_task = asyncio.create_task(loop.run())
    # Let the greeting register (no audio yet) and the pump start; deliver the
    # short caller turn entirely within the pre-playout window.
    for _ in range(5):
        await asyncio.sleep(0)
    transport.release.set()
    # Drain the inbound turn, then release the greeting audio so run() completes.
    for _ in range(30):
        await asyncio.sleep(0)
    emit.set()
    await asyncio.wait_for(run_task, timeout=5.0)

    assert delivered == ["hello"], (
        "a short real caller turn during TTS synthesis latency must be delivered"
    )


class _EmitThenParkStream:
    """A TtsStream that emits its first frame (arming the gate) then parks.

    Stays the active, audio-emitting stream until ``cancel()`` (a superseding
    ``speak()``) unblocks it — modelling a stream that is mid-playout when a newer
    one supersedes it. Because it has emitted a frame, it has set
    ``_tts_audio_active``; because it is superseded (not finished), its ``_play``
    finalizer will not clear the flag (it no longer owns ``_active_tts_stream``).
    """

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = frames
        self.cancel_called = False
        self._cancelled = False
        self._resume = asyncio.Event()
        self._gen = self._iter()

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        return await self._gen.__anext__()

    async def _iter(self) -> AsyncGenerator[PcmFrame]:
        if self._frames:
            yield self._frames[0]  # arm the gate
        await self._resume.wait()  # park until superseded/cancelled
        if not self._cancelled:
            for frame in self._frames[1:]:
                yield frame

    async def flush(self) -> None:
        pass

    async def cancel(self) -> None:
        self.cancel_called = True
        self._cancelled = True
        self._resume.set()

    async def aclose(self) -> None:
        self._cancelled = True
        self._resume.set()
        await self._gen.aclose()


class _SupersedingTTS:
    """StreamingTTS returning stream A first, then a pre-playout-latency stream B.

    Models a superseding ``speak()``: A emits audio then parks (still active), then
    B supersedes it but has synthesis latency before its first frame. Used to prove
    the echo gate disarms for B's pre-playout window even though A had armed it and
    A's finalizer cannot clear it (codex C').
    """

    def __init__(
        self,
        a_frames: list[PcmFrame],
        b_frames: list[PcmFrame],
        b_emit: asyncio.Event,
    ) -> None:
        self._a = _EmitThenParkStream(a_frames)
        self._b = _PrePlayoutGreetingStream(b_frames, b_emit)
        self._calls = 0
        self.a_stream = self._a
        self.b_stream = self._b

    @property
    def output_sample_rate(self) -> int:
        return _G711_INBOUND_RATE

    def synthesize(
        self, text: AsyncIterator[str], voice: str, *, sample_rate: int | None = None
    ) -> TtsStream:
        _ = text, voice, sample_rate
        self._calls += 1
        return self._a if self._calls == 1 else self._b


@pytest.mark.asyncio
async def test_superseding_speak_disarms_echo_gate_for_pre_playout() -> None:
    """A superseding speak() must not leave the echo gate armed for B's pre-playout.

    Stream A emits audio (arming the gate), then speak(B) supersedes A. B has
    synthesis latency before its first frame. The playout lock serialises A's
    teardown before B's _play runs, so B's _play disarms ``_tts_audio_active``
    until B's own first frame — even though A's _play finalizer cannot clear it
    (A no longer owns ``_active_tts_stream``). Asserts the flag is False during B's
    pre-playout gap (codex C'); without the fix it stays True from A.
    """
    a_frames = [_greeting_frame(1), _greeting_frame(2)]
    b_emit = asyncio.Event()
    b_frames = [_greeting_frame(3)]
    tts = _SupersedingTTS(a_frames, b_frames, b_emit)
    transport = _FakeTransport([])

    loop = CallLoop(
        transport=transport,
        asr=_FakeASR([]),
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad_8k(),
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=_noop,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="",
        barge_in_mode=BargeInMode.GATED,
        barge_in_min_voiced_windows=13,
        barge_in_tail_windows=8,
    )

    async def _tokens(word: str) -> AsyncIterator[str]:
        yield word

    # Play A (it emits frames → arms the echo gate). _SlowTtsStream yields each
    # frame after a cooperative sleep(0), so pump the loop until A's first frame
    # has been sent and the gate is armed.
    a_task = asyncio.create_task(loop.speak(_tokens("first")))
    armed_by_a = False
    for _ in range(20):
        await asyncio.sleep(0)
        if loop._tts_audio_active:
            armed_by_a = True
            break
    assert armed_by_a, "A's audio should have armed the gate"

    # Supersede with B (synthesis latency: B emits no frame until b_emit is set).
    b_task = asyncio.create_task(loop.speak(_tokens("second")))
    # Let A tear down and B's _play acquire the lock (which disarms the gate).
    for _ in range(8):
        await asyncio.sleep(0)

    # During B's pre-playout latency the gate must be DISARMED (B has sent no
    # audio yet; A's stale True must not persist). Read into a local so the bool
    # literal narrowing does not make mypy treat the rest as unreachable.
    armed_during_b_pre_playout = loop._tts_audio_active
    assert armed_during_b_pre_playout is False, (
        "echo gate must disarm during a superseding stream's pre-playout latency"
    )

    # Release B's audio and let both speaks finish; the gate re-arms on B's frame.
    b_emit.set()
    await asyncio.wait_for(asyncio.gather(a_task, b_task), timeout=5.0)


@pytest.mark.asyncio
async def test_gated_default_mode_when_unspecified() -> None:
    """A CallLoop built without barge-in args defaults to gated, min 1 window.

    The default constructor (used by older call sites / tests) must not regress:
    with no inbound echo and an active greeting, the greeting completes; the
    default mode is ``gated``. (The adapter supplies real thresholds; the
    constructor default is a safe immediate-ish gate.)
    """
    tts = _LongSlowGreetingTTS(n_frames=3)
    transport = _Transport8k([])  # no inbound audio at all

    loop = CallLoop(
        transport=transport,
        asr=_FakeASR([]),
        tts=tts,
        guard=_FakeGuard([_allow_result()]),
        vad=_make_vad_8k(),
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=_CALL_ID),
        deliver_turn=_noop,
        voice=_VOICE,
        call_id=_CALL_ID,
        greeting="A short greeting with no inbound audio.",
    )
    assert loop.barge_in_mode is BargeInMode.GATED
    await asyncio.wait_for(loop.run(), timeout=5.0)
    # With no inbound audio nothing barges in; the greeting ran to completion.
    assert tts.last_stream is not None
    assert tts.last_stream.cancel_called is False


# ---------------------------------------------------------------------------
# (z) Per-call TTS failover reset (ADR-0025): run() un-latches a FailoverTTS at
# the start of each call so a fresh call retries the primary provider. The hook
# is duck-typed via SupportsCallReset, so a plain TTS is untouched.
# ---------------------------------------------------------------------------


class _ResettableTTS(_FakeTTS):
    """A StreamingTTS fake that records reset_failover() calls (SupportsCallReset)."""

    def __init__(self, frames: list[PcmFrame]) -> None:
        super().__init__(frames)
        self.reset_calls = 0

    def reset_failover(self) -> None:
        self.reset_calls += 1


@pytest.mark.asyncio
async def test_run_resets_failover_latch_at_call_start() -> None:
    """run() calls reset_failover() on a supporting TTS so a fresh call retries."""
    tts = _ResettableTTS([])
    loop = _build_loop(
        _FakeTransport([_silence_frame(0)]),
        _FakeASR([]),
        tts,
        _FakeGuard([_allow_result()]),
        _noop,
    )
    await loop.run()
    assert tts.reset_calls == 1


@pytest.mark.asyncio
async def test_run_does_not_require_reset_hook_on_plain_tts() -> None:
    """A plain TTS without reset_failover() runs unaffected (no crash, no hook)."""
    tts = _FakeTTS([])  # no reset_failover method
    loop = _build_loop(
        _FakeTransport([_silence_frame(0)]),
        _FakeASR([]),
        tts,
        _FakeGuard([_allow_result()]),
        _noop,
    )
    # Must complete cleanly even though the TTS has no reset hook.
    await loop.run()
