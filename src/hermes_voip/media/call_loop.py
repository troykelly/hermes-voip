"""Duplex conversational call loop — the W13 orchestrator (ADR-0003).

``CallLoop`` turns far-end audio into agent turns and agent text into near-end
speech with barge-in. It is the seam between the media layer (transport/VAD/
endpointer/ASR/TTS) and the Hermes adapter that owns the Hermes conversation.

Architecture
------------
``run()`` runs two concurrent tasks on the asyncio event loop:

* **Inbound pump** — consumes ``transport.inbound_audio()``, feeds every frame
  through VAD + endpointer, and also routes each frame into the ASR stream via
  a bounded asyncio.Queue. On a speech ONSET while the agent is speaking,
  ``barge_in()`` is called immediately to cancel the active TtsStream before
  the ASR task can see more audio. On a finalised end-of-turn transcript the
  pump awaits the guard, records the result in ``guard_state``, and (if the
  verdict is not REFUSE) awaits ``deliver_turn`` to hand the text to the agent.

* **ASR consumer** — iterates ``asr.stream(audio_queue_iter)``, yielding
  ``Transcript`` objects. Final/end-of-turn transcripts are sent to the inbound
  pump via a second queue so the pump serialises guard screening and delivery.

Barge-in
--------
``speak()`` holds a reference to the active ``TtsStream`` on
``self._active_tts_stream``. ``barge_in()`` calls ``stream.cancel()`` which
makes the stream stop yielding (per the TtsStream protocol) so ``speak()``'s
``async for`` loop exits; no more frames are sent to ``transport.send_audio``.
The ``barge_in()`` call completes synchronously-within-the-event-loop, so
cancellation is immediate relative to any subsequent ``send_audio`` calls.

Task safety
-----------
* The inbound pump and ASR consumer run as ``asyncio.Task`` objects; ``run()``
  cancels and awaits them in a ``finally`` block so no task leaks on any exit
  path (clean end or exception).
* ``speak()`` is not launched as a background task by ``run()`` — the Hermes
  adapter calls it from its own coroutine while ``run()`` is live. It is
  independently cancellable via ``barge_in()``.
* The audio tee (inbound → ASR) uses a bounded ``asyncio.Queue`` so the pump
  never buffers unbounded frames while a slow ASR consumer lags behind.
* ``_active_tts_stream`` is accessed only from the event loop; no locking is
  needed.

Tool gating
-----------
``gate_voip_tool`` is a thin re-export of ``gate_tool_call`` from
``hermes_voip.providers.policy``.  The adapter calls it before executing any
tool action so the guard-session state (``degraded``, ``flagged_turns``) is
honoured without the call loop needing to know tool semantics.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final

from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.vad import SpeechEdge, VoiceActivityDetector
from hermes_voip.providers.asr import StreamingASR
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardVerdict, InjectionGuard
from hermes_voip.providers.policy import GuardSessionState, ToolRisk, gate_tool_call
from hermes_voip.providers.transport import MediaTransport
from hermes_voip.providers.tts import StreamingTTS, TtsStream

#: Maximum audio frames buffered between the inbound pump and the ASR consumer
#: (back-pressure: pump blocks once the ASR task is this far behind).
_AUDIO_QUEUE_MAX: Final[int] = 32

#: Sentinel that signals end-of-audio to the ASR consumer queue.
_AUDIO_DONE: Final[None] = None


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
    """

    def __init__(  # noqa: PLR0913 — 10-arg constructor; all keyword-only, no default
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
        # The currently-active TtsStream, or None when the agent is silent.
        # Accessed only from the event loop; no locking required.
        self._active_tts_stream: TtsStream | None = None

    async def run(self) -> None:  # noqa: PLR0915 — long but linear; extraction loses coupling
        """Run the duplex loop until the transport's inbound stream ends.

        Starts two concurrent tasks (inbound audio pump + ASR consumer), drives
        them to completion when the transport closes, and cancels/cleans up both
        tasks on any exit path so no asyncio tasks are leaked.

        The caller (the Hermes adapter) can call ``speak()`` and ``barge_in()``
        concurrently while this coroutine is live.

        Raises:
            Any exception propagated from the inbound pump or ASR consumer.
        """
        # Bounded queue: inbound pump → ASR consumer.
        # PcmFrame | None: None is the end-of-audio sentinel.
        audio_q: asyncio.Queue[PcmFrame | None] = asyncio.Queue(
            maxsize=_AUDIO_QUEUE_MAX
        )
        # Queue from ASR consumer back to the pump for finalised transcripts.
        transcript_q: asyncio.Queue[str] = asyncio.Queue()

        async def _asr_consumer() -> None:
            """Drain the ASR stream; push final end-of-turn text to transcript_q."""

            async def _audio_iter() -> AsyncIterator[PcmFrame]:
                while True:
                    frame = await audio_q.get()
                    if frame is None:
                        return
                    yield frame

            async for transcript in self._asr.stream(_audio_iter()):
                if transcript.is_final and transcript.end_of_turn:
                    await transcript_q.put(transcript.text)

        pump_task: asyncio.Task[None] | None = None
        asr_task: asyncio.Task[None] | None = None

        async def _inbound_pump() -> None:
            """Consume inbound frames; route to VAD, endpointer, audio_q, guard."""
            window_index = 0
            try:
                async for frame in self._transport.inbound_audio():
                    # --- VAD / barge-in detection ---
                    for vad_event in self._vad.feed(frame):
                        self._endpointer.on_event(vad_event)
                        # Speech onset while agent is speaking → barge-in.
                        if (
                            vad_event.edge is SpeechEdge.ONSET
                            and self._active_tts_stream is not None
                        ):
                            await self.barge_in()
                    self._endpointer.advance(window_index)
                    window_index += 1

                    # --- Route to ASR consumer ---
                    await audio_q.put(frame)

                    # --- Drain any finalised transcripts (non-blocking) ---
                    while not transcript_q.empty():
                        text = transcript_q.get_nowait()
                        await self._screen_and_deliver(text)

            finally:
                # Signal end-of-audio to the ASR consumer.
                await audio_q.put(_AUDIO_DONE)

            # Drain the final transcript flush after the stream ends.
            # The ASR consumer will emit any buffered final transcript before it
            # exits, so we wait for the ASR task to flush before draining.
            if asr_task is not None:
                await asr_task  # wait for the consumer to finish draining
            while not transcript_q.empty():
                text = transcript_q.get_nowait()
                await self._screen_and_deliver(text)

        try:
            asr_task = asyncio.create_task(_asr_consumer())
            pump_task = asyncio.create_task(_inbound_pump())
            # Wait for the pump to complete (it drives the loop's lifetime).
            await pump_task
        except BaseException:
            if pump_task is not None and not pump_task.done():
                pump_task.cancel()
            raise
        finally:
            if asr_task is not None and not asr_task.done():
                asr_task.cancel()
            # Gather with return_exceptions so cancellation of one does not
            # swallow the other's error; exceptions discarded here because
            # the pump already surfaced any meaningful error above.
            tasks: list[asyncio.Task[None]] = []
            if pump_task is not None:
                tasks.append(pump_task)
            if asr_task is not None:
                tasks.append(asr_task)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def speak(self, text: AsyncIterator[str]) -> None:
        """Synthesise agent text and send frames to the far end.

        Drives ``tts.synthesize(text, voice)`` and forwards each output
        ``PcmFrame`` to ``transport.send_audio()``. Returns once all frames are
        sent or ``barge_in()`` has cancelled the stream.

        ``barge_in()`` may be called concurrently (from the inbound pump) to
        cancel the active stream; once cancelled the ``TtsStream`` stops
        yielding and this coroutine exits cleanly.

        Args:
            text: The agent's incremental text output.
        """
        stream = self._tts.synthesize(text, self._voice)
        self._active_tts_stream = stream
        try:
            async for frame in stream:
                await self._transport.send_audio(frame)
        finally:
            # Clear the reference so barge_in() knows there is no active stream.
            if self._active_tts_stream is stream:
                self._active_tts_stream = None

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
