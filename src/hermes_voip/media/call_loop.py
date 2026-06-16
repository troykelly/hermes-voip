"""Duplex conversational call loop — the W13 orchestrator (ADR-0003).

``CallLoop`` turns far-end audio into agent turns and agent text into near-end
speech with barge-in. It is the seam between the media layer (transport/VAD/
endpointer/ASR/TTS) and the Hermes adapter that owns the Hermes conversation.

Architecture
------------
``run()`` runs **three one-directional tasks** under an :class:`asyncio.TaskGroup`
(Python 3.13), plus an optional one-shot **greeting** task. Each pipeline task
owns exactly one hop, so no task ever both feeds and drains two bounded queues —
which is what precludes the two-queue deadlock:

* **pump** — consumes ``transport.inbound_audio()``, feeds every frame through
  VAD + endpointer (calling :meth:`barge_in` on a speech ONSET while the agent
  is speaking), and forwards each frame onto the bounded ``audio_q``. When the
  transport ends it puts an end-of-stream marker and returns.

* **asr** — iterates ``asr.stream(audio_iter)`` where ``audio_iter`` drains
  ``audio_q`` until the marker; it forwards each finalised end-of-turn
  transcript onto the bounded ``transcript_q``. When the ASR stream ends it puts
  a marker and returns.

* **delivery** — drains ``transcript_q``; for each transcript it awaits
  ``guard.screen``, folds the result into ``guard_state``, and (when the verdict
  is not REFUSE) awaits ``deliver_turn``. It returns on the marker.

* **greeting** (only when a non-empty ``greeting`` is configured) — synthesises
  the configured opening line via :meth:`speak` the instant the loop starts,
  *before and independently of* any inbound audio, then returns. This makes the
  plugin emit RTP first so the caller hears the opening immediately and a
  symmetric-RTP gateway behind NAT latches onto our source tuple (opening the
  return media path). It runs concurrently with the pump, so it never blocks
  inbound audio, and because :meth:`speak` registers the active ``TtsStream``
  the pump's barge-in cancels it if the caller talks over it (ADR-0002).

Because the pump only *feeds* ``audio_q`` and the delivery task only *drains*
``transcript_q``, the dependency graph is a straight line
(pump → audio_q → asr → transcript_q → delivery) with **no cycle**. Both queues
are bounded, so memory is bounded and a slow stage back-pressures upstream — but
back-pressure on an acyclic chain cannot deadlock: the most-downstream runnable
stage (delivery) always makes progress, releasing the stage above it, and so on.
The old two-task design had the pump *both* feeding ``audio_q`` *and* draining
``transcript_q``, closing a cycle that deadlocked once both bounded queues filled.

Supervision & shutdown
-----------------------
:class:`asyncio.TaskGroup` cancels every sibling the moment any task raises and
re-raises that exception (inside an ``ExceptionGroup``) from ``run()`` — so an
ASR failure can never go unobserved while the pump blocks forever. Shutdown is
**cancellation-driven**: on normal completion the end-of-stream markers flow
down the chain and all three tasks return; on error or external cancellation the
TaskGroup cancels the tasks and the blocked ``put``/``get`` calls raise
``CancelledError``. No cleanup step blocks on putting a sentinel into a possibly
full bounded queue — the end-of-stream markers are emitted on the normal path,
never from a ``finally`` that could re-block.

Barge-in
--------
``speak()`` holds a reference to the active ``TtsStream`` on
``self._active_tts_stream``. ``barge_in()`` calls ``stream.cancel()`` which
makes the stream stop yielding (per the TtsStream protocol) so ``speak()``'s
``async for`` loop exits; no more frames are sent to ``transport.send_audio``.
The ``barge_in()`` call completes synchronously-within-the-event-loop, so
cancellation is immediate relative to any subsequent ``send_audio`` calls.
``speak()`` is not launched by ``run()`` — the Hermes adapter calls it from its
own coroutine while ``run()`` is live; it is independently cancellable via
``barge_in()``. ``_active_tts_stream`` is touched only from the event loop, so
no locking is needed.

Tool gating
-----------
``gate_voip_tool`` is a thin re-export of ``gate_tool_call`` from
``hermes_voip.providers.policy``.  The adapter calls it before executing any
tool action so the guard-session state (``degraded``, ``flagged_turns``) is
honoured without the call loop needing to know tool semantics.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import Enum
from typing import Final

from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.vad import SpeechEdge, VoiceActivityDetector
from hermes_voip.providers.asr import StreamingASR
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardVerdict, InjectionGuard
from hermes_voip.providers.policy import GuardSessionState, ToolRisk, gate_tool_call
from hermes_voip.providers.transport import MediaTransport
from hermes_voip.providers.tts import StreamingTTS, TtsStream

_log: Final = logging.getLogger(__name__)

#: Maximum audio frames buffered between the pump and the ASR task (back-pressure:
#: the pump blocks once the ASR task is this far behind). Bounds inbound memory.
_AUDIO_QUEUE_MAX: Final[int] = 32

#: Maximum finalised transcripts buffered between the ASR task and the delivery
#: task. Bounds memory if guard screening / delivery lags behind ASR finals
#: (finding #3): ASR back-pressures rather than buffering an unbounded backlog.
_TRANSCRIPT_QUEUE_MAX: Final[int] = 32


class _EndOfStream(Enum):
    """Typed end-of-stream marker passed through the inter-task queues.

    A single-member enum gives a distinct, ``is``-comparable sentinel with a
    precise static type (``Literal[_EndOfStream.MARKER]``), so a queue can carry
    ``Frame | _EndOfStream`` with no ``None``-ambiguity and no ``Any``.
    """

    MARKER = "marker"


#: The sole end-of-stream marker value (see :class:`_EndOfStream`).
_END_OF_STREAM: Final[_EndOfStream] = _EndOfStream.MARKER


def gate_voip_tool(
    risk: ToolRisk,
    state: GuardSessionState,
    *,
    confirmed: bool,
) -> bool:
    """Gate a tool invocation through the session guard state.

    A thin re-export of ``gate_tool_call`` (``hermes_voip.providers.policy``):
    the adapter calls this for every tool action so the guard-session state
    (``degraded`` flag, cumulative risk) is honoured regardless of the
    classifier outcome. ``IRREVERSIBLE`` tools require explicit confirmation AND
    a non-degraded session; ``ELEVATED`` tools are blocked while degraded;
    ``SAFE`` tools always run.

    Args:
        risk: The action risk class of the tool.
        state: The per-session guard state for this call.
        confirmed: Whether the caller explicitly confirmed the action
            (e.g. via DTMF — ADR-0010).

    Returns:
        ``True`` if the tool may run, ``False`` if it must be blocked.
    """
    return gate_tool_call(risk, state, confirmed=confirmed)


class CallLoop:
    """Duplex conversational call loop — inbound audio to agent turns and back.

    Drives the full audio ↔ agent pipeline for one active call:

    * Far-end audio from ``transport.inbound_audio()`` →
      VAD/endpointer (barge-in detection) + ASR (transcript production) →
      guard screening → ``deliver_turn`` (hands text to the Hermes agent).
    * Agent text from ``speak()`` → TTS synthesis → ``transport.send_audio()``
      (speech to the far end). Cancelled immediately on barge-in.

    The loop is call-scoped: create one per active call, ``await run()``, then
    discard. It is not re-entrant.

    All constructor arguments are keyword-only to prevent silent positional
    mis-ordering across the 10-parameter surface.

    Args:
        transport: The media seam; provides ``inbound_audio()`` and
            ``send_audio()``.
        asr: The streaming speech recogniser; ``stream(audio)`` yields
            ``Transcript`` objects.
        tts: The streaming synthesiser; ``synthesize(text, voice)`` yields a
            ``TtsStream``.
        guard: The injection guard; ``screen(text, call_id=...)`` returns a
            graded ``GuardResult``.
        vad: The voice-activity detector for barge-in onset detection.
        endpointer: The end-of-turn timer (for engines without native turn
            detection; used here to advance the window ordinal).
        guard_state: The per-call guard session state (degraded flag, flagged
            turns).  Mutated in place.
        deliver_turn: Async callback the loop calls with each finalised,
            non-refused caller transcript so the adapter can route it to the
            Hermes agent.
        voice: Opaque voice identifier passed to ``tts.synthesize()``.
        call_id: The call/session identifier passed to ``guard.screen()``.
        greeting: The opening line spoken the instant the loop starts (before any
            inbound audio). Empty string disables the greeting. Defaults to empty
            so a caller that does not want one need not pass it.
    """

    def __init__(  # noqa: PLR0913 — 11-arg constructor; all keyword-only
        self,
        *,
        transport: MediaTransport,
        asr: StreamingASR,
        tts: StreamingTTS,
        guard: InjectionGuard,
        vad: VoiceActivityDetector,
        endpointer: Endpointer,
        guard_state: GuardSessionState,
        deliver_turn: Callable[[str], Awaitable[None]],
        voice: str,
        call_id: str,
        greeting: str = "",
    ) -> None:
        """Store injected dependencies; initialise mutable state."""
        self._transport = transport
        self._asr = asr
        self._tts = tts
        self._guard = guard
        self._vad = vad
        self._endpointer = endpointer
        self._guard_state = guard_state
        self._deliver_turn = deliver_turn
        self._voice = voice
        self._call_id = call_id
        self._greeting = greeting
        # The currently-active TtsStream, or None when the agent is silent.
        # Accessed only from the event loop; no locking required.
        self._active_tts_stream: TtsStream | None = None

    async def run(self) -> None:
        """Run the duplex loop until the transport's inbound stream ends.

        Runs three one-directional tasks under an :class:`asyncio.TaskGroup`
        (pump → ``audio_q`` → asr → ``transcript_q`` → delivery). The TaskGroup
        supervises them: if any task raises, the others are cancelled and the
        error is re-raised from ``run()`` (wrapped in an ``ExceptionGroup``);
        when the transport ends, end-of-stream markers flow down the chain and
        all three tasks return cleanly. No task is leaked on any exit path, and
        the acyclic two-bounded-queue topology cannot deadlock (see the module
        docstring).

        The caller (the Hermes adapter) can call ``speak()`` and ``barge_in()``
        concurrently while this coroutine is live.

        Raises:
            ExceptionGroup: Wrapping any exception raised by a stage task (e.g.
                a transport, ASR, guard, or delivery failure).
        """
        # Bounded queues on a straight pump → asr → delivery line. Both bounded,
        # so memory is bounded; acyclic, so back-pressure never deadlocks.
        audio_q: asyncio.Queue[PcmFrame | _EndOfStream] = asyncio.Queue(
            maxsize=_AUDIO_QUEUE_MAX
        )
        transcript_q: asyncio.Queue[str | _EndOfStream] = asyncio.Queue(
            maxsize=_TRANSCRIPT_QUEUE_MAX
        )

        async def _pump() -> None:
            """inbound_audio → VAD/endpoint (+ barge-in) → audio_q (bounded)."""
            window_index = 0
            async for frame in self._transport.inbound_audio():
                for vad_event in self._vad.feed(frame):
                    self._endpointer.on_event(vad_event)
                    # Speech onset while the agent is speaking → barge-in.
                    if (
                        vad_event.edge is SpeechEdge.ONSET
                        and self._active_tts_stream is not None
                    ):
                        await self.barge_in()
                self._endpointer.advance(window_index)
                window_index += 1
                await audio_q.put(frame)
            # End-of-stream marker on the normal path (not a finally): the ASR
            # task is still draining audio_q, so this put cannot block forever.
            await audio_q.put(_END_OF_STREAM)

        async def _asr() -> None:
            """audio_q → asr.stream(...) → transcript_q (bounded)."""

            async def _audio_iter() -> AsyncIterator[PcmFrame]:
                while True:
                    item = await audio_q.get()
                    if item is _END_OF_STREAM:
                        return
                    yield item

            async for transcript in self._asr.stream(_audio_iter()):
                if transcript.is_final and transcript.end_of_turn:
                    await transcript_q.put(transcript.text)
            await transcript_q.put(_END_OF_STREAM)

        async def _delivery() -> None:
            """transcript_q → guard.screen → record → (non-REFUSE) deliver_turn."""
            while True:
                item = await transcript_q.get()
                if item is _END_OF_STREAM:
                    return
                await self._screen_and_deliver(item)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_pump())
            tg.create_task(_asr())
            tg.create_task(_delivery())
            # Optional one-shot greeting: speak the configured opening line at
            # once so RTP flows out before any inbound audio (NAT-latch, ADR-0002).
            # Concurrent with the pump (never blocks it); barge-in-cancellable via
            # speak()'s registered TtsStream. Returns after the greeting is sent.
            if self._greeting:
                tg.create_task(self._greet())

    async def speak(
        self,
        text: AsyncIterator[str],
        *,
        on_first_frame: Callable[[], None] | None = None,
    ) -> None:
        """Synthesise agent text and send frames to the far end.

        Drives ``tts.synthesize(text, voice)`` and forwards each output
        ``PcmFrame`` to ``transport.send_audio()``. Returns once all frames are
        sent or ``barge_in()`` has cancelled the stream.

        ``barge_in()`` may be called concurrently (from the inbound pump) to
        cancel the active stream; once cancelled the ``TtsStream`` stops
        yielding and this coroutine exits cleanly.

        Args:
            text: The agent's incremental text output.
            on_first_frame: Optional zero-arg callback invoked exactly once, just
                before the first synthesised frame is sent to the transport (used
                by the greeting to log the real first-RTP moment). Not called at
                all if the stream is cancelled before yielding any frame.
        """
        stream = self._tts.synthesize(text, self._voice)
        self._active_tts_stream = stream
        first_frame_pending = on_first_frame is not None
        try:
            async for frame in stream:
                if first_frame_pending and on_first_frame is not None:
                    on_first_frame()
                    first_frame_pending = False
                await self._transport.send_audio(frame)
        finally:
            # Clear the reference so barge_in() knows there is no active stream.
            if self._active_tts_stream is stream:
                self._active_tts_stream = None

    async def _greet(self) -> None:
        """Speak the configured opening greeting immediately on call answer.

        Synthesises ``self._greeting`` as a single text chunk and sends it via
        :meth:`speak`, so the plugin emits RTP before any inbound audio — the
        caller hears the opening at once and a symmetric-RTP gateway behind NAT
        latches onto our source tuple (ADR-0002). Logs the synth start and the
        first outbound RTP frame at INFO so a live call shows the greeting going
        out. Only called when ``self._greeting`` is non-empty.
        """
        greeting = self._greeting
        _log.info("greeting: synthesising %d chars", len(greeting))

        def _log_first_rtp() -> None:
            _log.info("greeting: first RTP sent")

        async def _single_chunk() -> AsyncIterator[str]:
            yield greeting

        await self.speak(_single_chunk(), on_first_frame=_log_first_rtp)

    async def barge_in(self) -> None:
        """Cancel the in-flight TtsStream for immediate barge-in.

        Calls ``cancel()`` on the active ``TtsStream`` if one is in progress,
        causing ``speak()``'s iteration loop to exit before the next
        ``send_audio`` call. Idempotent: calling when no stream is active is a
        no-op.
        """
        stream = self._active_tts_stream
        if stream is not None:
            await stream.cancel()

    async def _screen_and_deliver(self, text: str) -> None:
        """Screen one finalised turn through the guard; deliver if not refused."""
        result = await self._guard.screen(text, call_id=self._call_id)
        self._guard_state.record(result)
        if result.verdict is not GuardVerdict.REFUSE:
            await self._deliver_turn(text)
