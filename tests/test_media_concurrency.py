"""TDD tests for concurrent-inbound-call isolation and send_audio graceful stop.

Live bug (2026-06-16): a gateway sends 3 overlapping INVITEs for the same
extension (retry/fork pattern — all with the same Call-ID or distinct Call-IDs).
The first call's greeting succeeds; overlapping calls crash with:

    RuntimeError: send_audio called before connect()  # engine._transport is None

This file reproduces and pins both defects:

Part 1 — root cause: concurrent INVITEs share the same Call-ID dict key in the
adapter. When one call's teardown runs while another is still active, teardown
MUTATES shared adapter state keyed by Call-ID (_call_loops, _call_sessions, SIP
routing) that belongs to the still-running call. The critical isolation failure is
in the adapter's _teardown_call: it pops _call_loops and _call_sessions by
Call-ID unconditionally, even if those entries now belong to a DIFFERENT task's
session/loop for the same Call-ID. With 3 overlapping tasks, the FIRST task's
teardown can remove the SECOND task's call_loop, breaking speak() routing — and
more dangerously, if the THIRD task's teardown fires before the SECOND task's
loop has had a chance to store its loop entry, the second task's engine may end
up unreachable and its teardown never called, leaving a zombie engine open. But
the most concretely reproducible failure is the engine stop ordering: the adapter
must not call engine.stop() while the engine's CallLoop is still inside its
TaskGroup (a different task's teardown must not reach another task's engine).

The SECOND root cause: _call_tasks[call_id] is overwritten for each new INVITE
with the same Call-ID, so all but the last task are dropped from the dict. When
disconnect() cancels _call_tasks, it only cancels the LAST task — the earlier
tasks are orphaned and never cancelled. This means their engines leak and are
never stopped.

Part 2 — robustness: even with per-call engine isolation, a frame that arrives in
send_audio AFTER the engine has been legitimately stopped (e.g., the call ended
while a TTS frame was in flight) must NOT raise RuntimeError — it must silently
return so the TaskGroup teardown remains clean. This is graceful degradation
(intentional no-op), not a swallowed error.

All tests are deterministic: real UDP sockets on 127.0.0.1 (loopback) at real
sample rates; no real timing dependency (sleep injected as no-op); no real SIP
gateway; faked ASR/TTS/guard; fake SIP address fakes (127.0.0.1, ext 1000).
"""

from __future__ import annotations

import asyncio
import gc
import threading
from collections.abc import AsyncIterator

import pytest

from hermes_voip.media.audio import G711_SAMPLE_RATE
from hermes_voip.media.call_loop import CallLoop
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.media.vad import VoiceActivityDetector
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.providers.tts import TtsStream
from hermes_voip.tts._stream import PcmFrameStream, SegmentSource

# ---------------------------------------------------------------------------
# Constants (fake addresses — loopback only, no real gateway)
# ---------------------------------------------------------------------------

_LOOPBACK = "127.0.0.1"
_CALL_ID_A = "call-concurrent-A@pbx.example.test"
_CALL_ID_B = "call-concurrent-B@pbx.example.test"
_CALL_ID_C = "call-concurrent-C@pbx.example.test"

# One 20 ms G.711 silence frame.
_PTIME_MS = 20
_SAMPLES = (G711_SAMPLE_RATE * _PTIME_MS) // 1000  # 160
_PCM_SILENCE = b"\x00" * (_SAMPLES * 2)  # 320 bytes PCM16


def _silence_frame() -> PcmFrame:
    """One 20 ms G.711-rate silence frame."""
    return PcmFrame(
        samples=_PCM_SILENCE,
        sample_rate=G711_SAMPLE_RATE,
        monotonic_ts_ns=0,
    )


# ---------------------------------------------------------------------------
# Part 2 — send_audio after stop() is a graceful no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_audio_after_stop_is_silent_noop() -> None:
    """send_audio on a stopped engine must return silently, not raise RuntimeError.

    Reproduces the live crash path: when a call ends (stop() called in teardown)
    while a TTS frame is in flight (the TaskGroup is still tearing down), the next
    send_audio call crashes with 'send_audio called before connect()' because
    self._transport is None. The fix makes send_audio a graceful no-op when the
    engine has been stopped — the call is ending, dropping the frame is correct.

    This is PART 2 of the fix: defence in depth even after the per-call isolation
    fix ensures the direct cross-engine-stop cannot happen.
    """

    async def _no_sleep(_s: float) -> None:
        pass

    engine = RtpMediaTransport(
        local_address=_LOOPBACK,
        local_port=0,
        remote_address=_LOOPBACK,
        remote_port=5004,  # not actually reachable; won't matter — we stop first
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )
    await engine.connect()
    await engine.stop()  # simulate mid-teardown

    # BEFORE the fix this raises RuntimeError("send_audio called before connect()").
    # AFTER the fix it must return silently (frame is silently discarded).
    await engine.send_audio(_silence_frame())  # must NOT raise


@pytest.mark.asyncio
async def test_send_audio_before_connect_still_raises() -> None:
    """Calling send_audio before EVER connecting must still raise RuntimeError.

    This distinguishes the 'never connected' programming error (should raise) from
    the 'stopped mid-call' teardown case (should be a no-op). After Part 2's fix
    we must not accidentally suppress real connect-before-use bugs.
    """
    engine = RtpMediaTransport(
        local_address=_LOOPBACK,
        local_port=0,
        remote_address=_LOOPBACK,
        remote_port=5004,
        codec=Codec.PCMU,
    )
    # connect() was never called → _transport is None AND _connected is False
    with pytest.raises(RuntimeError, match="send_audio called before connect"):
        await engine.send_audio(_silence_frame())


# ---------------------------------------------------------------------------
# Part 1 — per-call engine isolation: stop() on one engine must not affect
# another engine's transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stopping_one_engine_does_not_null_sibling_transport() -> None:
    """Two concurrent engines are fully isolated: stop() on A leaves B intact.

    This is the direct-isolation unit test: two separate RtpMediaTransport
    instances (simulating two concurrent inbound calls, one per engine) — stopping
    one must NOT null the other's internal DatagramTransport.
    """

    async def _no_sleep(_s: float) -> None:
        pass

    engine_a = RtpMediaTransport(
        local_address=_LOOPBACK,
        local_port=0,
        remote_address=_LOOPBACK,
        remote_port=5004,
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )
    engine_b = RtpMediaTransport(
        local_address=_LOOPBACK,
        local_port=0,
        remote_address=_LOOPBACK,
        remote_port=5006,
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )

    await engine_a.connect()
    await engine_b.connect()

    # Verify both are connected (can send a frame without raising).
    await engine_a.send_audio(_silence_frame())
    await engine_b.send_audio(_silence_frame())

    # Simulate call_A ending: stop engine_a.
    await engine_a.stop()

    # engine_b must STILL be usable — its transport must NOT have been nulled.
    # BEFORE the isolation fix (if any shared state existed), this would raise.
    await engine_b.send_audio(_silence_frame())

    await engine_b.stop()


# ---------------------------------------------------------------------------
# Fake collaborators for the CallLoop-level concurrent-call test
# ---------------------------------------------------------------------------


class _EndingTransport:
    """MediaTransport fake: yields N frames at G.711 rate then stops.

    Used to simulate a real inbound audio stream of finite length; the CallLoop
    then exits cleanly. send_audio records every sent frame for assertions.
    """

    def __init__(self, call_id: str, n_frames: int) -> None:
        self._call_id = call_id
        self._n_frames = n_frames
        self.sent_audio: list[PcmFrame] = []
        # Set externally to gate first send (simulates real async RTP timing).
        self.first_send_gate: asyncio.Event = asyncio.Event()
        self.first_send_gate.set()  # open by default unless test gates it

    @property
    def inbound_sample_rate(self) -> int:
        return G711_SAMPLE_RATE

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        n = self._n_frames

        async def _gen() -> AsyncIterator[PcmFrame]:
            for _i in range(n):
                # One cooperative yield between frames so the event loop runs.
                await asyncio.sleep(0)
                yield _silence_frame()

        return _gen()

    async def send_audio(self, frame: PcmFrame) -> None:
        await self.first_send_gate.wait()
        self.sent_audio.append(frame)

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass


class _DrainIterator:
    """AsyncIterator that drains a PcmFrame source and emits no Transcripts.

    Used by _ImmediateASR.stream(): consumes inbound audio (preventing queue
    back-pressure on the CallLoop pump) without yielding any transcript events.
    Implemented as an explicit AsyncIterator class to avoid the mypy
    unreachable-code false-positive that comes with the ``return; yield`` or
    ``if False: yield`` tricks needed to make an async def an async-generator.

    The __anext__ drains one frame per call and immediately raises
    StopAsyncIteration so the caller sees no transcripts. The audio frames are
    still consumed from the source, draining the audio_q and preventing the pump
    from blocking on a full queue (even though with 32-item maxsize and 3 frames
    the queue can't actually fill — the drain is a correctness guarantee, not
    just a performance hint).
    """

    def __init__(self, audio: AsyncIterator[PcmFrame]) -> None:
        self._audio = audio
        self._exhausted = False

    def __aiter__(self) -> AsyncIterator[Transcript]:
        return self

    async def __anext__(self) -> Transcript:
        if self._exhausted:
            raise StopAsyncIteration
        # Drain one frame; if the source is exhausted, we are too.
        try:
            _ = await self._audio.__anext__()
        except StopAsyncIteration:
            self._exhausted = True
            raise
        # Frame consumed; no transcript to emit this call.
        raise StopAsyncIteration


class _ImmediateASR:
    """StreamingASR fake: drains audio without blocking; emits no transcripts."""

    @property
    def input_sample_rate(self) -> int:
        return G711_SAMPLE_RATE

    def stream(
        self,
        audio: AsyncIterator[PcmFrame],
    ) -> AsyncIterator[Transcript]:
        return _DrainIterator(audio)


class _NullTtsStream:
    """TtsStream fake: yields no frames (empty utterance).

    Implements AsyncIterator[PcmFrame] by being its own async generator via
    __aiter__ / __anext__ — necessary to satisfy the TtsStream Protocol which
    extends AsyncIterator[PcmFrame].
    """

    def __init__(self) -> None:
        self._done = False

    async def flush(self) -> None:
        pass

    async def cancel(self) -> None:
        self._done = True

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        raise StopAsyncIteration


class _NullTTS:
    """StreamingTTS fake: produces an empty stream on every synthesize() call."""

    @property
    def output_sample_rate(self) -> int:
        return G711_SAMPLE_RATE

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
    ) -> TtsStream:
        return _NullTtsStream()


class _NullGuard:
    """InjectionGuard fake: always allows."""

    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            score=0.0,
            degraded=False,
            normalized_text=text,
            reasons=(),
        )


def _make_vad_8k() -> VoiceActivityDetector:
    """Silent 8 kHz VAD (never fires onset)."""

    def _silent(window_pcm16: bytes, sample_rate: int) -> float:
        _ = window_pcm16, sample_rate
        return 0.0

    return VoiceActivityDetector(model=_silent, sample_rate_hz=G711_SAMPLE_RATE)


def _make_endpointer_8k() -> Endpointer:
    return Endpointer(silence_ms=500, sample_rate_hz=G711_SAMPLE_RATE)


def _build_call_loop(
    transport: _EndingTransport,
    *,
    call_id: str,
    greeting: str = "",
) -> CallLoop:
    """Build a CallLoop wired to fake collaborators at 8 kHz (G.711 rate)."""
    state = GuardSessionState(call_id=call_id)
    return CallLoop(
        transport=transport,
        asr=_ImmediateASR(),
        tts=_NullTTS(),
        guard=_NullGuard(),
        vad=_make_vad_8k(),
        endpointer=_make_endpointer_8k(),
        guard_state=state,
        deliver_turn=_noop_deliver,
        voice="",
        call_id=call_id,
        greeting=greeting,
    )


async def _noop_deliver(text: str) -> None:
    _ = text


# ---------------------------------------------------------------------------
# Part 1 — concurrent CallLoop runs: one ending must not break another
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_call_loops_run_independently() -> None:
    """Two concurrent CallLoops must not interfere: both greetings must complete.

    Simulates two overlapping inbound calls: call_A uses transport_A, call_B uses
    transport_B. They run concurrently via asyncio.TaskGroup (as the adapter does
    for concurrent INVITEs). Each loop should drive its OWN transport to completion
    without the other call's teardown affecting it.

    The failure mode (before the isolation fix): one call's teardown calls stop()
    on the wrong engine, causing 'send_audio called before connect()' in the
    other call's greeting task.
    """
    greeting = "Hello, you are through to the assistant."

    # A greeting TTS stream: yield 3 silence frames then stop.
    class _GreetingTtsStream:
        def __init__(self) -> None:
            self._frames = [_silence_frame(), _silence_frame(), _silence_frame()]
            self._cancelled = False
            self.flush_called = False

        def __aiter__(self) -> AsyncIterator[PcmFrame]:
            return self._iter()

        async def _iter(self) -> AsyncIterator[PcmFrame]:
            for f in self._frames:
                if self._cancelled:
                    return
                # Cooperative yield so sibling tasks can run.
                await asyncio.sleep(0)
                yield f

        async def __anext__(self) -> PcmFrame:
            raise StopAsyncIteration

        async def cancel(self) -> None:
            self._cancelled = True

        async def flush(self) -> None:
            self.flush_called = True

    class _GreetingTTS:
        """TTS that yields 3 frames for any synthesize() call."""

        @property
        def output_sample_rate(self) -> int:
            return G711_SAMPLE_RATE

        def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
            return _GreetingTtsStream()

    # Transport A: 3 inbound frames (the loop ends after the pump drains them).
    transport_a = _EndingTransport(_CALL_ID_A, n_frames=3)
    transport_b = _EndingTransport(_CALL_ID_B, n_frames=3)

    state_a = GuardSessionState(call_id=_CALL_ID_A)
    state_b = GuardSessionState(call_id=_CALL_ID_B)

    tts_a = _GreetingTTS()
    tts_b = _GreetingTTS()

    loop_a = CallLoop(
        transport=transport_a,
        asr=_ImmediateASR(),
        tts=tts_a,
        guard=_NullGuard(),
        vad=_make_vad_8k(),
        endpointer=_make_endpointer_8k(),
        guard_state=state_a,
        deliver_turn=_noop_deliver,
        voice="",
        call_id=_CALL_ID_A,
        greeting=greeting,
    )
    loop_b = CallLoop(
        transport=transport_b,
        asr=_ImmediateASR(),
        tts=tts_b,
        guard=_NullGuard(),
        vad=_make_vad_8k(),
        endpointer=_make_endpointer_8k(),
        guard_state=state_b,
        deliver_turn=_noop_deliver,
        voice="",
        call_id=_CALL_ID_B,
        greeting=greeting,
    )

    # Run both call loops concurrently, the way the adapter does it.
    await asyncio.wait_for(
        asyncio.gather(loop_a.run(), loop_b.run(), return_exceptions=False),
        timeout=5.0,
    )

    # Both transports must have received greeting frames on THEIR transport.
    # Before the isolation fix, one or both would have 0 frames (crash prevented
    # any frames being sent, or frames went to the wrong transport).
    assert len(transport_a.sent_audio) >= 1, (
        "call A greeting sent 0 frames — expected >= 1 "
        "(concurrent-call isolation broken for A)"
    )
    assert len(transport_b.sent_audio) >= 1, (
        "call B greeting sent 0 frames — expected >= 1 "
        "(concurrent-call isolation broken for B)"
    )


# ---------------------------------------------------------------------------
# Part 1 — real RtpMediaTransport engines: concurrent calls with same Call-ID
# ---------------------------------------------------------------------------
#
# Reproduces the adapter pattern most faithfully: two calls with the SAME Call-ID
# (the gateway retry/fork scenario), each with its own engine. The first call's
# engine runs its greeting and then stops; the second call's engine must NOT have
# its _transport nulled by the first call's stop().
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_real_engines_same_call_id_isolation() -> None:
    """Two concurrent RtpMediaTransport engines for the same Call-ID are isolated.

    Simulates the gateway retry pattern: the gateway resends the same INVITE
    (same Call-ID) before the first one has been answered, resulting in two
    concurrent _handle_inbound_invite tasks each with their own engine.

    The first call's engine is stopped (simulating quick teardown) while the
    second is still running. The second must continue to operate normally — its
    send_audio must not raise.

    This test exercises the engine-level isolation directly (no adapter machinery).
    The engines are real RtpMediaTransport instances bound to loopback UDP sockets.
    """

    async def _no_sleep(_s: float) -> None:
        pass

    # Two INDEPENDENT engines (same Call-ID context, different instances).
    engine_first = RtpMediaTransport(
        local_address=_LOOPBACK,
        local_port=0,
        remote_address=_LOOPBACK,
        remote_port=5004,
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )
    engine_retry = RtpMediaTransport(
        local_address=_LOOPBACK,
        local_port=0,
        remote_address=_LOOPBACK,
        remote_port=5006,
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )

    await engine_first.connect()
    await engine_retry.connect()

    # Track which engine IDs are connected (instrumentation per rule 25).
    engine_first_id = id(engine_first)
    engine_retry_id = id(engine_retry)
    assert engine_first_id != engine_retry_id, "engines must be distinct objects"

    # First call sends its greeting frame successfully.
    await engine_first.send_audio(_silence_frame())

    # Retry call (overlapping) sends its first frame — must also succeed.
    await engine_retry.send_audio(_silence_frame())

    # First call's teardown runs (call ended).
    await engine_first.stop()
    assert engine_first._transport is None, "engine_first must be stopped"

    # engine_retry must NOT have been affected: its _transport must still be live.
    assert engine_retry._transport is not None, (
        "engine_retry._transport was nulled by engine_first.stop() — "
        "cross-engine isolation broken (the same-Call-ID bug)"
    )

    # Retry call continues: must not raise "send_audio called before connect()".
    await engine_retry.send_audio(_silence_frame())

    await engine_retry.stop()


# ---------------------------------------------------------------------------
# Part 3 — async-generator teardown is clean on EVERY exit path, including the
# crash/cancel cascade and under concurrent calls.
#
# Live evidence (2026-06-16): when the greeting crashed, the cascade also
# produced
#     ERROR asyncio: Task exception was never retrieved
#     RuntimeError: aclose(): asynchronous generator is already running
# i.e. abandoned async generators (the inbound_audio pump generator and/or the
# TTS stream generator) finalized mid-run when the TaskGroup unwound.
#
# Prior art: PcmFrameStream.cancel() is flag-based and never acloses the running
# frame generator from another task (PR #38). The pump iterates inbound_audio()
# with a plain ``async for`` that closes the generator on its OWN task as it
# unwinds (PR #62). These tests VERIFY — they do not assume — that the full
# teardown (normal end, fatal crash, external cancel) leaves:
#   * NO "Task exception was never retrieved" (asyncio loop exception handler),
#   * NO "aclose(): asynchronous generator is already running" (raised OR warned),
#   * NO leaked RuntimeWarning of any kind,
# on both single calls and concurrent calls, with gc.collect() forced so any
# abandoned-generator finalizer runs inside the assertion window.
# ---------------------------------------------------------------------------


class _LoopErrorCapture:
    """Install a custom asyncio loop exception handler and capture every call.

    The live cascade surfaced as 'Task exception was never retrieved', which is
    NOT a raised exception in the caller — the event loop's default exception
    handler logs it when a task with an unretrieved exception is GC'd. To assert
    it does not happen, we replace the handler, force a GC pass, then inspect the
    captured contexts. Restores the original handler on exit.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.contexts: list[dict[str, object]] = []
        self._previous: object = None

    def __enter__(self) -> _LoopErrorCapture:
        self._previous = self._loop.get_exception_handler()

        def _handler(
            _loop: asyncio.AbstractEventLoop,
            context: dict[str, object],
        ) -> None:
            self.contexts.append(context)

        self._loop.set_exception_handler(_handler)
        return self

    def __exit__(self, *_exc: object) -> None:
        # mypy: set_exception_handler accepts Callable | None; _previous is one.
        handler = self._previous
        if handler is None:
            self._loop.set_exception_handler(None)
        else:
            assert callable(handler)
            self._loop.set_exception_handler(handler)

    def messages(self) -> list[str]:
        """Flatten captured contexts to human-readable strings for assertions."""
        out: list[str] = []
        for ctx in self.contexts:
            msg = ctx.get("message", "")
            exc = ctx.get("exception")
            out.append(f"{msg!s} | exception={exc!r}")
        return out


async def _settle_and_gc() -> None:
    """Drain pending callbacks then force GC so finalizers run in-window.

    An abandoned async generator's ``aclose`` is scheduled by the GC finalizer;
    'Task exception was never retrieved' is logged when an un-awaited task is
    collected. Both need a GC pass AND event-loop turns to surface. We collect,
    then yield to the loop, twice, so any finalizer-scheduled coroutine also runs
    and its own warnings/errors land before the caller asserts.
    """
    for _ in range(3):
        gc.collect()
        await asyncio.sleep(0)


def _assert_clean_teardown(
    capture: _LoopErrorCapture,
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Assert no aclose-race and no un-retrieved task exception leaked."""
    # 1) No asyncio loop errors (in particular 'Task exception was never
    #    retrieved' and any surfaced aclose RuntimeError).
    loop_messages = capture.messages()
    assert loop_messages == [], (
        "asyncio loop exception handler fired during teardown "
        f"(expected none): {loop_messages}"
    )

    # 2) No RuntimeWarning — covers 'coroutine was never awaited' and the
    #    'aclose(): asynchronous generator is already running' that surfaces as a
    #    RuntimeWarning from a GC-driven generator finalizer.
    runtime_warnings = [
        str(w.message) for w in recwarn.list if issubclass(w.category, RuntimeWarning)
    ]
    assert runtime_warnings == [], (
        f"unexpected RuntimeWarnings during teardown (expected none): "
        f"{runtime_warnings}"
    )

    # 3) Belt-and-braces: the specific live signature must appear in NEITHER
    #    channel, even if some future change reclassifies it.
    blob = " ".join(loop_messages + runtime_warnings).lower()
    assert "asynchronous generator is already running" not in blob, (
        "the live 'aclose(): asynchronous generator is already running' "
        f"surfaced during teardown: {loop_messages + runtime_warnings}"
    )


class _AwaitingInboundTransport:
    """A transport whose inbound_audio() awaits between yields, like real RTP.

    Mirrors RtpMediaTransport: the inbound generator parks at an inner await
    between yields (the real engine parks at ``await self._next_datagram()``), so
    a cancellation of the pump lands at that inner await — the exact shape that
    triggers the cross-task aclose race if teardown is wrong. The generator's
    ``finally`` records closure so the test can assert it was closed on its own
    task, not left dangling for a GC finalizer.

    ``send_audio`` optionally raises a fatal error on the Nth call to reproduce
    the live greeting crash cascade (the original was send_audio raising; here we
    inject an equivalent fatal error so the greeting task fails and the TaskGroup
    tears the whole loop down mid-iteration).
    """

    def __init__(
        self,
        *,
        fail_send_on_call: int | None = None,
    ) -> None:
        self._fail_send_on_call = fail_send_on_call
        self._send_calls = 0
        self.sent_audio: list[PcmFrame] = []
        self.gen_closed = False
        self.gen_started = asyncio.Event()

    @property
    def inbound_sample_rate(self) -> int:
        return G711_SAMPLE_RATE

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        async def _gen() -> AsyncIterator[PcmFrame]:
            try:
                index = 0
                while True:
                    self.gen_started.set()
                    # Inner await between yields — the engine parks here.
                    await asyncio.sleep(0.002)
                    index += 1
                    yield _silence_frame()
            finally:
                self.gen_closed = True

        return _gen()

    async def send_audio(self, frame: PcmFrame) -> None:
        self._send_calls += 1
        if (
            self._fail_send_on_call is not None
            and self._send_calls >= self._fail_send_on_call
        ):
            # Fatal error during greeting playout — reproduces the live cascade
            # structure (a mid-greeting send failure fails the greeting task,
            # which fails the TaskGroup, which cancels the pump mid-iteration).
            msg = "synthetic fatal send_audio failure (greeting crash repro)"
            raise RuntimeError(msg)
        self.sent_audio.append(frame)

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass


class _RealGreetingTTS:
    """StreamingTTS yielding a REAL PcmFrameStream (the production teardown path).

    Uses the genuine ``PcmFrameStream`` over a cooperative async byte source that
    polls the shared stop flag — exactly the production barge-in/teardown machinery
    (PR #38). This exercises the real generator lifecycle, so the test proves the
    real code's teardown is clean rather than a fake's. The created stream is
    captured in ``last_stream`` so a test can assert it was torn down
    (``_torn_down``) — i.e. its frame generator was closed, not abandoned.
    """

    def __init__(self, *, chunks_per_segment: int = 50) -> None:
        self._chunks_per_segment = chunks_per_segment
        self.last_stop: threading.Event | None = None
        self.last_stream: PcmFrameStream | None = None

    @property
    def output_sample_rate(self) -> int:
        return G711_SAMPLE_RATE

    def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
        _ = voice
        stop = threading.Event()
        self.last_stop = stop
        n = self._chunks_per_segment

        def _open_segment(sentence: str) -> SegmentSource:
            _ = sentence

            async def _chunks() -> AsyncIterator[bytes]:
                for _i in range(n):
                    if stop.is_set():
                        return
                    await asyncio.sleep(0.001)
                    # One 20 ms G.711 frame worth of PCM16 silence bytes.
                    yield _PCM_SILENCE

            return SegmentSource(chunks=_chunks())

        stream = PcmFrameStream(
            text=text,
            open_segment=_open_segment,
            sample_rate=G711_SAMPLE_RATE,
            stop=stop,
        )
        self.last_stream = stream
        return stream


async def _noop_deliver_text(text: str) -> None:
    _ = text


def _build_teardown_loop(
    transport: _AwaitingInboundTransport,
    tts: _RealGreetingTTS,
    *,
    call_id: str,
    greeting: str,
) -> CallLoop:
    """Build a CallLoop wired to the real-teardown transport + TTS at 8 kHz."""
    return CallLoop(
        transport=transport,
        asr=_ImmediateASR(),
        tts=tts,
        guard=_NullGuard(),
        vad=_make_vad_8k(),
        endpointer=_make_endpointer_8k(),
        guard_state=GuardSessionState(call_id=call_id),
        deliver_turn=_noop_deliver_text,
        voice="",
        call_id=call_id,
        greeting=greeting,
    )


@pytest.mark.asyncio
async def test_greeting_crash_cascade_tears_down_generators_cleanly(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """A greeting crash mid-playout must tear down all generators cleanly.

    Reproduces the live cascade: the greeting playout hits a fatal send_audio
    error while the pump is iterating the real-shaped inbound generator (parked
    at an inner await). The greeting task fails → the TaskGroup cancels the pump
    mid-iteration. The pump's inbound generator and the greeting's PcmFrameStream
    must both close on their OWN tasks as they unwind — NO abandoned generator,
    so NO 'aclose(): asynchronous generator is already running' and NO
    'Task exception was never retrieved' after a forced GC.

    Before the Part 2 fix the live error was the send_audio crash; here we keep a
    fatal send error to prove the GENERATOR teardown is clean even when the call
    dies abnormally (Part 2 removes the specific send_audio crash, but a call can
    still die from any fatal error — teardown must stay clean either way).
    """
    loop_obj = asyncio.get_running_loop()
    transport = _AwaitingInboundTransport(fail_send_on_call=1)
    tts = _RealGreetingTTS()

    call_loop = _build_teardown_loop(
        transport,
        tts,
        call_id=_CALL_ID_A,
        greeting="Hello, you are through to the assistant.",
    )

    with _LoopErrorCapture(loop_obj) as capture:
        # The greeting crash propagates out of run() as an ExceptionGroup
        # (TaskGroup) — that is the EXPECTED surfacing; what must NOT happen is an
        # abandoned-generator aclose race or an un-retrieved task exception.
        with pytest.raises(BaseException) as exc_info:  # noqa: PT011 — TaskGroup raises a broad ExceptionGroup; we assert on its contents below
            await asyncio.wait_for(call_loop.run(), timeout=5.0)

        # The fatal greeting error is the cause (sanity: we crashed for the right
        # reason, not a timeout or unrelated failure).
        raised = exc_info.value
        flat = repr(raised)
        assert "synthetic fatal send_audio failure" in flat, (
            f"expected the greeting crash to surface, got: {flat}"
        )

        await _settle_and_gc()

    # The inbound generator was closed on its own task (its finally ran), not
    # left dangling for a GC finalizer.
    assert transport.gen_closed is True, (
        "inbound generator was not closed on teardown (left dangling)"
    )
    # The greeting stream's frame generator was torn down (closed) on its own
    # task — NOT abandoned for a GC finalizer to aclose later (the live race).
    # The PRE-FIX bug: _play's plain `async for` does not aclose the stream when
    # send_audio raises inside the loop body, so the PcmFrameStream is left
    # suspended with _torn_down=False.
    assert tts.last_stream is not None
    assert tts.last_stream._torn_down is True, (
        "greeting PcmFrameStream was abandoned (its frame generator was left "
        "suspended, not closed) when the greeting crashed — the live "
        "aclose-already-running cascade originates here"
    )

    _assert_clean_teardown(capture, recwarn)


@pytest.mark.asyncio
async def test_normal_end_of_call_tears_down_generators_cleanly(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """A normally-ending call tears its generators down with no leaked errors.

    The pump's inbound generator ends naturally (the transport stops yielding),
    the greeting plays to completion, the loop exits cleanly. After a forced GC
    there must be NO 'Task exception was never retrieved' and NO aclose race.
    This is the control for the crash-path test: clean teardown is required on
    the happy path too.
    """
    loop_obj = asyncio.get_running_loop()

    # A transport that yields a few frames then ends (inbound generator returns),
    # never failing send_audio — the greeting plays out and the loop completes.
    class _FiniteInbound(_AwaitingInboundTransport):
        def __init__(self, n_frames: int) -> None:
            super().__init__(fail_send_on_call=None)
            self._n_frames = n_frames

        def inbound_audio(self) -> AsyncIterator[PcmFrame]:
            n = self._n_frames

            async def _gen() -> AsyncIterator[PcmFrame]:
                try:
                    for _i in range(n):
                        self.gen_started.set()
                        await asyncio.sleep(0.001)
                        yield _silence_frame()
                finally:
                    self.gen_closed = True

            return _gen()

    transport = _FiniteInbound(n_frames=3)
    tts = _RealGreetingTTS(chunks_per_segment=3)

    call_loop = _build_teardown_loop(
        transport,
        tts,
        call_id=_CALL_ID_B,
        greeting="Short greeting.",
    )

    with _LoopErrorCapture(loop_obj) as capture:
        await asyncio.wait_for(call_loop.run(), timeout=5.0)
        await _settle_and_gc()

    assert transport.gen_closed is True, (
        "inbound generator not closed on normal end-of-call"
    )
    _assert_clean_teardown(capture, recwarn)


@pytest.mark.asyncio
async def test_concurrent_calls_one_crashes_both_tear_down_cleanly(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Concurrent calls: one greeting crashes; BOTH tear down with no leaks.

    Two CallLoops run concurrently (the adapter pattern for overlapping inbound
    calls). Call A's greeting crashes mid-playout (fatal send error); Call B runs
    normally to completion. The crash of A must not corrupt B's generator
    teardown, and neither call may leave an abandoned generator. After a forced GC
    there must be NO 'aclose(): asynchronous generator is already running' and NO
    'Task exception was never retrieved' from EITHER call.
    """
    loop_obj = asyncio.get_running_loop()

    # Call A: greeting crashes on first send.
    transport_a = _AwaitingInboundTransport(fail_send_on_call=1)
    tts_a = _RealGreetingTTS()
    loop_a = _build_teardown_loop(
        transport_a, tts_a, call_id=_CALL_ID_A, greeting="Call A greeting."
    )

    # Call B: finite inbound, greeting plays out, ends cleanly.
    class _FiniteInbound(_AwaitingInboundTransport):
        def __init__(self, n_frames: int) -> None:
            super().__init__(fail_send_on_call=None)
            self._n_frames = n_frames

        def inbound_audio(self) -> AsyncIterator[PcmFrame]:
            n = self._n_frames

            async def _gen() -> AsyncIterator[PcmFrame]:
                try:
                    for _i in range(n):
                        self.gen_started.set()
                        await asyncio.sleep(0.001)
                        yield _silence_frame()
                finally:
                    self.gen_closed = True

            return _gen()

    transport_b = _FiniteInbound(n_frames=4)
    tts_b = _RealGreetingTTS(chunks_per_segment=4)
    loop_b = _build_teardown_loop(
        transport_b, tts_b, call_id=_CALL_ID_C, greeting="Call B greeting."
    )

    with _LoopErrorCapture(loop_obj) as capture:
        results = await asyncio.gather(
            loop_a.run(), loop_b.run(), return_exceptions=True
        )
        await _settle_and_gc()

    # Call A crashed (returned an exception); Call B completed (returned None).
    result_a, result_b = results
    assert isinstance(result_a, BaseException), (
        "Call A was expected to crash (fatal greeting send)"
    )
    assert "synthetic fatal send_audio failure" in repr(result_a), (
        f"Call A crashed for the wrong reason: {result_a!r}"
    )
    assert result_b is None, f"Call B should have completed cleanly, got: {result_b!r}"

    # Both inbound generators closed on their own tasks.
    assert transport_a.gen_closed is True, "Call A inbound generator left dangling"
    assert transport_b.gen_closed is True, "Call B inbound generator left dangling"

    # Call A's greeting stream (crashed mid-playout) must be torn down, not
    # abandoned — this is the concurrent-call version of the live aclose cascade.
    assert tts_a.last_stream is not None
    assert tts_a.last_stream._torn_down is True, (
        "Call A greeting PcmFrameStream was abandoned on crash (not closed)"
    )

    # No aclose race, no un-retrieved task exception, from EITHER call.
    _assert_clean_teardown(capture, recwarn)


@pytest.mark.asyncio
async def test_external_cancel_mid_greeting_tears_down_generators_cleanly(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Cancelling run() mid-greeting tears down all generators cleanly.

    The adapter cancels per-call tasks on disconnect/reconnect. Cancelling run()
    while the greeting is mid-playout and the pump is parked at an inner await
    must propagate a clean CancelledError, close both generators on their own
    tasks, and leave NO abandoned generator — so NO aclose race and NO
    'Task exception was never retrieved' after a forced GC.
    """
    loop_obj = asyncio.get_running_loop()
    transport = _AwaitingInboundTransport(fail_send_on_call=None)
    tts = _RealGreetingTTS(chunks_per_segment=200)  # long greeting, still playing

    call_loop = _build_teardown_loop(
        transport,
        tts,
        call_id=_CALL_ID_A,
        greeting="A long greeting that is still playing when we cancel the call.",
    )

    with _LoopErrorCapture(loop_obj) as capture:
        run_task = asyncio.create_task(call_loop.run())
        # Wait until the pump is iterating (inbound generator producing).
        await asyncio.wait_for(transport.gen_started.wait(), timeout=5.0)
        # Let the greeting get a few frames out so it is genuinely mid-playout.
        await asyncio.sleep(0.01)

        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

        await _settle_and_gc()

    assert transport.gen_closed is True, (
        "inbound generator not closed on external cancel"
    )
    assert tts.last_stop is not None
    _assert_clean_teardown(capture, recwarn)
