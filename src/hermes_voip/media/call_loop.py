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
import contextlib
import logging
import math
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import Enum
from typing import Final

from hermes_voip.media.audio import generate_tone_frames
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.vad import SpeechEdge, VadEvent, VoiceActivityDetector
from hermes_voip.providers.asr import StreamingASR
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardVerdict, InjectionGuard
from hermes_voip.providers.policy import GuardSessionState, ToolRisk, gate_tool_call
from hermes_voip.providers.transport import MediaTransport
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.spoken_text import sanitize_for_speech

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


class BargeInMode(Enum):
    """How an inbound speech onset is allowed to interrupt the agent (ADR-0022).

    * ``OFF`` — never barge in; the agent always finishes its turn.
    * ``GATED`` — (default) while the agent's TTS is playing (and for a short tail
      after), a barge-in counts only once it is a SUSTAINED voiced run, so short
      echo blips (the gateway reflecting our own TTS back) cannot interrupt but a
      genuine sustained interruption still does. Outside playout/tail any onset
      barges in immediately (nothing to echo when the agent is silent).
    * ``FULL`` — any speech onset barges in immediately, even during playout
      (correct only on a gateway with its own echo cancellation).
    """

    OFF = "off"
    GATED = "gated"
    FULL = "full"


class BargeInGate:
    """Echo-robust barge-in decision (ADR-0022).

    The live self-interruption bug: the gateway reflects the agent's own TTS back
    on the inbound path, the VAD/ASR transcribe it as the caller, and a single
    speech ONSET barged the agent in — ending its own turn. Echo arrives as SHORT,
    broken voiced runs (repeatedly OFFSET as the reflected energy dips); a genuine
    human interruption is a SUSTAINED continuous voiced run. This gate separates
    the two by *duration* without an acoustic echo canceller.

    Drive it from the inbound pump:

    * :meth:`tts_active` — call every iteration with whether the agent's TTS is
      currently playing (``_active_tts_stream is not None``). The gate is *armed*
      whenever TTS is active.
    * :meth:`tail_from` — call at the window where TTS *stops* to keep the gate
      armed for ``tail_windows`` more windows (echo lags the TTS via the jitter
      buffer / network).
    * :meth:`on_event` — feed every :class:`VadEvent`. ONSET starts a candidate
      voiced run; OFFSET ends it (an echo blip is dismissed here).
    * :meth:`should_barge_in` — call once per processed window with the current
      window ordinal; returns ``True`` exactly once when a barge-in should fire.

    While armed in ``GATED`` mode a barge-in fires only once the candidate run has
    lasted ``min_voiced_windows`` (inclusive of the onset window). While NOT armed
    (agent silent, past the tail), ``GATED`` behaves like ``FULL``: an onset barges
    in immediately. ``OFF`` never fires; ``FULL`` always fires on the onset.
    """

    def __init__(
        self, *, mode: BargeInMode, min_voiced_windows: int, tail_windows: int
    ) -> None:
        """Create a gate.

        Args:
            mode: The :class:`BargeInMode`.
            min_voiced_windows: In ``GATED`` mode while armed, the minimum
                consecutive voiced windows (inclusive of the onset window) before
                a barge-in fires. Must be ``>= 1``.
            tail_windows: How many windows after TTS stops the gate stays armed.
                ``>= 0``.

        Raises:
            ValueError: If ``min_voiced_windows < 1`` or ``tail_windows < 0``.
        """
        if min_voiced_windows < 1:
            msg = f"min_voiced_windows must be >= 1, got {min_voiced_windows}"
            raise ValueError(msg)
        if tail_windows < 0:
            msg = f"tail_windows must be >= 0, got {tail_windows}"
            raise ValueError(msg)
        self.mode = mode
        self._min_voiced_windows = min_voiced_windows
        self._tail_windows = tail_windows
        self._tts_active = False
        # Inclusive last window ordinal the post-TTS tail covers, or None when no
        # tail is pending. While the current window is <= this, the gate is armed
        # even though TTS has stopped (echo lags the TTS).
        self._tail_until: int | None = None
        # Window ordinal of the current candidate voiced run's ONSET, or None when
        # no run is in progress (no speech / ended by OFFSET / already fired).
        self._onset_window: int | None = None
        # True once a barge-in has fired for the current run, so the same run does
        # not re-fire on every later window (which would spam barge_in()).
        self._fired_for_run = False
        # True iff the MOST RECENT speech run was an authorised (real) barge-in.
        # Unlike ``_fired_for_run`` (cleared on OFFSET so the gate can re-arm),
        # this PERSISTS through the run's trailing silence — the endpointer fires
        # its end-of-turn there, and ``delivery_suppressed`` consults this to drop
        # an unauthorised echo turn. Reset only on a NEW onset (a fresh run is
        # unauthorised until it earns a barge-in), so authorisation never leaks
        # across runs.
        self._last_run_authorised = False

    def tts_active(self, active: bool) -> None:
        """Record whether the agent's TTS is currently playing.

        While active the gate is armed (gated mode requires a sustained run). The
        caller pairs a True→False transition with :meth:`tail_from` to keep the
        gate armed across the echo tail.
        """
        self._tts_active = active

    def tail_from(self, current_window: int) -> None:
        """Arm the post-TTS tail for ``tail_windows`` windows from ``current_window``.

        Called when the active TTS stream ends. Echo can arrive shortly after the
        last TTS frame (jitter buffer + network), so the gate keeps requiring a
        sustained run until ``current_window + tail_windows - 1`` (inclusive). A
        ``tail_windows`` of 0 leaves the tail already expired (disarm at once).
        """
        if self._tail_windows == 0:
            self._tail_until = current_window - 1
        else:
            self._tail_until = current_window + self._tail_windows - 1

    def _armed(self, current_window: int) -> bool:
        """Whether the gate currently requires a sustained run (gated mode)."""
        if self._tts_active:
            return True
        return self._tail_until is not None and current_window <= self._tail_until

    def on_event(self, event: VadEvent) -> None:
        """Update the candidate voiced run from a VAD edge.

        ONSET starts a fresh candidate run (clears the fired latch so a new
        interruption after one fired can fire again); OFFSET ends the run — the
        key step that dismisses a short echo blip before it reaches the threshold.
        """
        if event.edge is SpeechEdge.ONSET:
            self._onset_window = event.frame_index
            self._fired_for_run = False
            # A fresh run is unauthorised until it earns a barge-in: clear the
            # persisted authorisation so the prior run's verdict cannot leak into
            # this one (e.g. an authorised interruption followed by a short echo
            # blip must still have its blip-turn suppressed).
            self._last_run_authorised = False
        else:  # SpeechEdge.OFFSET — voicing stopped; the candidate run ends.
            # Clear only the live-run state so the gate can re-arm; KEEP
            # ``_last_run_authorised`` so the endpointer's end-of-turn on the
            # trailing silence still knows whether this run was a real barge-in.
            self._onset_window = None
            self._fired_for_run = False

    def should_barge_in(self, current_window: int) -> bool:
        """Return ``True`` exactly once when a barge-in should fire this window.

        Args:
            current_window: The ordinal of the window just processed.

        Returns:
            ``True`` to barge in now, else ``False``. Returns ``True`` at most once
            per candidate voiced run (latched until the run ends / a new onset).
        """
        if self.mode is BargeInMode.OFF:
            return False
        if self._onset_window is None or self._fired_for_run:
            return False
        if self.mode is BargeInMode.FULL or not self._armed(current_window):
            # Immediate barge-in: any onset interrupts (full mode, or gated while
            # the agent is silent — nothing to echo).
            return self._fire()
        # Gated + armed: require a SUSTAINED run. Inclusive of the onset window,
        # ``current_window - onset + 1`` windows have been voiced.
        voiced_windows = current_window - self._onset_window + 1
        if voiced_windows >= self._min_voiced_windows:
            return self._fire()
        return False

    def _fire(self) -> bool:
        """Latch the barge-in for the current run and authorise its turn."""
        self._fired_for_run = True
        self._last_run_authorised = True
        return True

    def delivery_suppressed(self, current_window: int) -> bool:
        """Whether an end-of-turn at this window is unauthorised echo to be dropped.

        Even when a short echo blip does not fire ``should_barge_in`` (so it never
        cancels the TTS), the endpointer still fires an end-of-turn on the echo's
        trailing silence and the recogniser's final fragment would be delivered to
        the agent as a caller turn — re-triggering an interrupt. The call loop
        calls this when the endpointer fires; it returns ``True`` (drop the turn)
        while the gate is armed (the agent's TTS is playing, or within the echo
        tail) AND the most-recent speech run was NOT an authorised barge-in.

        A genuine sustained interruption authorises its run (``should_barge_in``
        fired and also cancelled the TTS, so the agent is no longer speaking),
        so its transcript is delivered. Outside playout/tail nothing is
        suppressed — normal caller turns during silence always deliver.
        """
        return self._armed(current_window) and not self._last_run_authorised


class _ToneStream:
    """``TtsStream``-compatible iterator that emits pure-sine 8 kHz PcmFrames.

    A thin adapter around :func:`~hermes_voip.media.audio.generate_tone_frames`
    that satisfies the ``TtsStream`` protocol (``__aiter__`` / ``__anext__`` /
    ``flush`` / ``cancel`` / ``aclose``) without touching any TTS provider.
    Because the frames are already at 8 kHz they go through the fast path in
    ``RtpMediaTransport.send_audio`` (no resample), so the tone validates the
    G.711 encode + UDP send layer in isolation from TTS and sample-rate
    conversion.

    ``cancel()`` sets a stop flag that causes ``__anext__`` to raise
    ``StopAsyncIteration`` on the next pull — identical to the barge-in
    contract of the real TTS streams.  ``aclose()`` is idempotent and sets the
    same flag so ``contextlib.aclosing`` in ``_play`` closes the generator
    safely on every exit path.
    """

    def __init__(self, *, duration_secs: float) -> None:
        self._iter = generate_tone_frames(duration_secs=duration_secs)
        self._stopped = False

    def __aiter__(self) -> _ToneStream:
        return self

    async def __anext__(self) -> PcmFrame:
        if self._stopped:
            raise StopAsyncIteration
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None

    async def flush(self) -> None:
        """No-op: all frames are pre-computed; nothing is buffered."""

    async def cancel(self) -> None:
        """Stop yielding immediately (barge-in)."""
        self._stopped = True

    async def aclose(self) -> None:
        """Idempotent teardown; sets the stop flag so further pulls are safe."""
        self._stopped = True


async def _sanitize_iter(text: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield sanitised text chunks from *text*, stripping emoji/markdown/URLs.

    Each chunk emitted by the agent is passed through
    :func:`~hermes_voip.spoken_text.sanitize_for_speech` before being forwarded
    to TTS synthesis. Empty chunks (reduced to nothing by sanitisation) are
    skipped so the TTS segmenter never receives an empty string.
    """
    async for chunk in text:
        clean = sanitize_for_speech(chunk)
        if clean:
            yield clean


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
        tone_secs: When positive, the call opening plays a generated 440 Hz sine
            tone for this many seconds at 8 kHz directly, bypassing TTS +
            resample entirely (``HERMES_VOIP_TEST_TONE`` env var).  When set,
            ``greeting`` is ignored.  ``0.0`` (the default) means normal
            operation.
        barge_in_mode: Echo-robust barge-in mode (ADR-0022). ``GATED`` (default)
            requires a sustained voiced run to interrupt while the agent's TTS is
            playing (and a short tail after), so the gateway echoing the agent's
            own TTS back cannot self-interrupt it; a genuine sustained
            interruption still does. ``FULL`` is the legacy immediate barge-in;
            ``OFF`` disables barge-in.
        barge_in_min_voiced_windows: In ``GATED`` mode, the minimum consecutive
            voiced VAD windows (inclusive of the onset window) before a barge-in
            counts while armed. The adapter derives this from
            ``barge_in_min_speech_ms`` and the inbound rate. Must be ``>= 1``.
        barge_in_tail_windows: How many VAD windows after the agent's TTS stops the
            gate keeps requiring a sustained run (echo lags the TTS). ``>= 0``.
    """

    def __init__(  # noqa: PLR0913 — keyword-only constructor; all params are real dependencies/config
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
        tone_secs: float = 0.0,
        barge_in_mode: BargeInMode = BargeInMode.GATED,
        barge_in_min_voiced_windows: int = 1,
        barge_in_tail_windows: int = 0,
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
        self._tone_secs = tone_secs
        self.barge_in_mode = barge_in_mode
        # The echo-robust barge-in decision (ADR-0022). The pump drives it from
        # VAD edges + the TTS-active state; ``should_barge_in`` says when to fire.
        self._barge_in_gate = BargeInGate(
            mode=barge_in_mode,
            min_voiced_windows=barge_in_min_voiced_windows,
            tail_windows=barge_in_tail_windows,
        )
        # The currently-active TtsStream, or None when the agent is silent.
        # Written synchronously (``synthesize`` is a sync factory) before any
        # await, so a single ``speak``/greeting always registers its stream
        # before it yields control; read by ``barge_in`` from the same loop.
        self._active_tts_stream: TtsStream | None = None
        # Serialises near-end playout so only ONE TtsStream is ever draining to
        # ``transport.send_audio`` at a time. A new ``speak`` supersedes any
        # in-flight stream (cancels it, then takes the lock), so the greeting and
        # a following agent reply never interleave frames on the wire.
        self._playout_lock = asyncio.Lock()

    async def run(self) -> None:  # noqa: PLR0915 — run() hosts three nested tasks; statement count reflects pipeline complexity, not a refactor opportunity
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

        # End-of-turn counter shared between _pump and _asr (ADR-0008 §wiring).
        # The endpointer owns the turn boundary; the ASR recogniser does not
        # (SherpaOnnxASR always yields end_of_turn=False). When the endpointer
        # fires, _pump increments this counter; _asr decrements it once per
        # is_final transcript (if > 0) and delivers the transcript as end-of-turn.
        #
        # Why int instead of asyncio.Event (W2 fix): asyncio.Event is boolean —
        # two endpointer fires before the ASR produces a is_final collapse to
        # one signal, so only ONE turn is delivered. An int counter lets each
        # fire be consumed by exactly one is_final: N fires → N turns.
        #
        # Both _pump and _asr run on the same asyncio event loop and never yield
        # between the int read and write, so no lock is needed: asyncio tasks are
        # co-operatively scheduled and only yield at explicit ``await`` points.
        _eot_count: int = 0

        async def _pump() -> None:
            """inbound_audio → VAD/endpoint (+ barge-in) → audio_q (bounded).

            The pump is the SOLE iterator of ``transport.inbound_audio()``, so it
            owns that generator's lifecycle: a plain ``async for`` closes it on
            this task on every exit path — normal end, an error raised in the loop
            body (e.g. a VAD rate mismatch), or cancellation when the TaskGroup
            tears the loop down — because ``async for`` drives the generator's
            ``aclose`` as it unwinds. The generator is therefore never left
            suspended for a foreign ``aclose()`` (which would race a running
            ``__anext__`` and raise ``RuntimeError: aclose(): asynchronous
            generator is already running``). The cross-task cancel that the loop
            *does* perform — barge-in cancelling the TTS stream — goes through
            ``TtsStream.cancel`` (a stop flag, never ``aclose`` from another task).
            """
            nonlocal _eot_count
            window_index = 0
            frames_received = 0
            # Track the agent's TTS-active state across frames so the barge-in
            # gate (ADR-0022) can arm a post-TTS echo tail at the True→False edge.
            prev_tts_active = self._active_tts_stream is not None
            async for frame in self._transport.inbound_audio():
                for vad_event in self._vad.feed(frame):
                    self._endpointer.on_event(vad_event)
                    # Feed the edge to the echo-robust barge-in gate. ONSET starts
                    # a candidate voiced run; OFFSET ends it (dismissing a short
                    # echo blip). The gate's window clock is the VAD window ordinal
                    # carried on the edge, NOT the per-frame counter below.
                    self._barge_in_gate.on_event(vad_event)
                    _log.debug(
                        "pump: VAD %s at vad-window %d",
                        vad_event.edge.name,
                        vad_event.frame_index,
                    )
                # Drive the barge-in gate once per frame at the latest scored VAD
                # window. ``window_index`` (the VAD property) is the NEXT window
                # ordinal, so the last scored window is one less; -1 before any
                # window is scored is harmless (no onset is pending then). The gate
                # is armed whenever the agent's TTS is playing, plus a short tail
                # after it stops, during which a sustained run is required.
                tts_active = self._active_tts_stream is not None
                latest_vad_window = self._vad.window_index - 1
                if prev_tts_active and not tts_active:
                    # The agent just stopped speaking: keep gating across the echo
                    # tail (echo lags the TTS via the jitter buffer / network).
                    self._barge_in_gate.tail_from(latest_vad_window)
                self._barge_in_gate.tts_active(tts_active)
                prev_tts_active = tts_active
                if tts_active and self._barge_in_gate.should_barge_in(
                    latest_vad_window
                ):
                    _log.debug(
                        "pump: sustained speech at vad-window %d → barge-in",
                        latest_vad_window,
                    )
                    await self.barge_in()
                if self._endpointer.advance(window_index):
                    # ADR-0008: endpointer owns the turn boundary. Increment the
                    # EOT counter so the ASR task's next is_final transcript is
                    # delivered as an end-of-turn. Each increment is consumed by
                    # exactly one decrement in _asr (W2 fix: int counter counts
                    # fires; Event is boolean and loses duplicates).
                    #
                    # Echo-robust turn gate (ADR-0022, codex finding #1): while the
                    # agent's TTS is playing (or in the tail) and the just-ended
                    # speech run was NOT an authorised barge-in, this end-of-turn is
                    # the gateway's echo of the agent's own speech — suppress it so
                    # the echoed fragment is never delivered to the agent as a
                    # caller turn (the second self-interruption route). A genuine
                    # sustained interruption authorised its run (and cancelled the
                    # TTS), so it is delivered normally.
                    if self._barge_in_gate.delivery_suppressed(latest_vad_window):
                        _log.debug(
                            "pump: end-of-turn at window %d SUPPRESSED "
                            "(echo during TTS playout)",
                            window_index,
                        )
                    else:
                        _eot_count += 1
                        _log.info(
                            "pump: end-of-turn at window %d (frames=%d)",
                            window_index,
                            frames_received,
                        )
                window_index += 1
                frames_received += 1
                if frames_received % 50 == 0:
                    _log.debug(
                        "pump: %d frames received, window=%d",
                        frames_received,
                        window_index,
                    )
                await audio_q.put(frame)
            # End-of-stream marker on the normal path (not a finally): the ASR
            # task is still draining audio_q, so this put cannot block forever.
            _log.debug("pump: inbound stream ended after %d frames", frames_received)
            await audio_q.put(_END_OF_STREAM)

        async def _asr() -> None:
            """audio_q → asr.stream(...) → transcript_q (bounded).

            ``_audio_iter`` is an async generator this task creates and hands to
            ``asr.stream``. The ``async for`` over the recogniser output closes on
            this task when the stream ends, errors, or is cancelled; the provider
            owns its own teardown of the audio iterator it consumes (the
            ``StreamingASR`` contract: it drains ``audio`` until exhausted and the
            ``asr.stream`` result is only an
            :class:`~collections.abc.AsyncIterator`, so it is consumed with a
            plain ``async for`` — the protocol does not promise ``aclose``).

            End-of-turn wiring (ADR-0008): the recogniser always yields
            ``end_of_turn=False``; the turn boundary comes from the endpointer via
            the ``_eot_count`` counter.  When a transcript is final, we check
            ``_eot_count``: if > 0, decrement and deliver as end-of-turn.  The
            recogniser's own ``end_of_turn`` field is still honoured (fused
            engines like Deepgram Flux set it natively), so this is additive.
            """
            nonlocal _eot_count

            async def _audio_iter() -> AsyncIterator[PcmFrame]:
                while True:
                    item = await audio_q.get()
                    if item is _END_OF_STREAM:
                        return
                    yield item

            prev_text = ""
            async for transcript in self._asr.stream(_audio_iter()):
                if transcript.text != prev_text:
                    _log.debug("asr: hypothesis %r", transcript.text)
                    prev_text = transcript.text
                if transcript.is_final:
                    # Check the endpointer's end-of-turn counter (ADR-0008): the
                    # recogniser always returns end_of_turn=False; the endpointer
                    # increments _eot_count when trailing silence fires. We
                    # consume one count (non-blocking) per is_final — each
                    # decrement matches exactly one endpointer fire, so N fires
                    # yield N turns (W2 fix: was asyncio.Event which is boolean
                    # and collapses multiple fires before the first is_final).
                    # Both tasks are on the same event loop: no lock needed
                    # (asyncio is single-threaded; there is no yield between the
                    # read and the write).
                    eot_from_endpointer: bool
                    if _eot_count > 0:
                        _eot_count -= 1
                        eot_from_endpointer = True
                    else:
                        eot_from_endpointer = False
                    if eot_from_endpointer or transcript.end_of_turn:
                        _log.debug(
                            "asr: delivering turn %r (eot_endpointer=%s, eot_asr=%s)",
                            transcript.text,
                            eot_from_endpointer,
                            transcript.end_of_turn,
                        )
                        await transcript_q.put(transcript.text)
            await transcript_q.put(_END_OF_STREAM)

        async def _delivery() -> None:
            """transcript_q → guard.screen → record → (non-REFUSE) deliver_turn."""
            while True:
                item = await transcript_q.get()
                if item is _END_OF_STREAM:
                    return
                await self._screen_and_deliver(item)

        # Optional one-shot opening: either a diagnostic tone (when tone_secs > 0)
        # or the TTS greeting (when greeting is non-empty). Both are registered
        # synchronously here, before the TaskGroup starts the pump, so a caller
        # speech onset on the very first inbound frame already sees an active
        # stream and barges in. The opening is then played by its own task so it
        # runs concurrently with the pump and never blocks inbound audio
        # (NAT-latch rationale: ADR-0002).
        #
        # A tone/greeting failure is intentionally FATAL to the call: it runs as
        # a TaskGroup child (or raises synchronously here), so a synth/send error
        # propagates and cancels the loop rather than being swallowed (rule 37).
        # The rationale is causal, not just policy — if the opening cannot emit
        # RTP then a NAT'd gateway never latches, so the return media path never
        # opens and the call is already dead; failing fast surfaces it.
        #
        # Tone path: when tone_secs > 0, bypass TTS + resample entirely by
        # playing a generated 440 Hz sine tone directly at 8 kHz through the
        # existing _play path (uses the real send_audio, so it validates the RTP
        # encode/send path independently of the TTS layer — ADR-0002 §diagnostic).
        if self._tone_secs > 0:
            greeting_stream: TtsStream | None = self._begin_tone()
        elif self._greeting:
            greeting_stream = self._begin_greeting()
        else:
            greeting_stream = None

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_pump())
            tg.create_task(_asr())
            tg.create_task(_delivery())
            if greeting_stream is not None:
                tg.create_task(self._play_greeting(greeting_stream))

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

        A new ``speak`` **supersedes** any in-flight stream: it registers its own
        stream and cancels the previous one, then plays under the playout lock —
        so the opening greeting and a subsequent agent reply (or two replies)
        never interleave frames on the wire, and ``barge_in`` always targets the
        most recent stream. ``barge_in`` may be called concurrently (from the
        inbound pump) to cancel the active stream; once cancelled the
        ``TtsStream`` stops yielding and this coroutine exits cleanly.

        Args:
            text: The agent's incremental text output.
            on_first_frame: Optional zero-arg callback invoked exactly once, just
                after the first synthesised frame is successfully sent to the
                transport (used by the greeting to log the real first-RTP
                moment). Not called if the stream is cancelled before any frame
                is sent.
        """
        # Sanitize each text chunk before TTS synthesis so that emoji,
        # markdown markup, and raw URLs are never voiced by the TTS engine.
        stream = self._tts.synthesize(_sanitize_iter(text), self._voice)
        # Register synchronously (no await before this line), then supersede any
        # previously-active stream so playout is single-owner.
        previous = self._active_tts_stream
        self._active_tts_stream = stream
        if previous is not None and previous is not stream:
            await previous.cancel()
        await self._play(stream, on_first_frame=on_first_frame)

    async def _play(
        self,
        stream: TtsStream,
        *,
        on_first_frame: Callable[[], None] | None,
    ) -> None:
        """Drain one ``TtsStream`` to ``send_audio`` under the playout lock.

        The lock guarantees only one stream is ever sending at a time; a
        superseding ``speak`` has already cancelled the prior stream, so the
        prior ``_play`` exits its loop promptly and releases the lock to this
        one. ``on_first_frame`` fires once, right after the first frame is
        actually sent (so a cancelled-before-any-frame stream never logs it).

        The iteration runs under ``contextlib.aclosing`` so the stream is closed
        on EVERY exit path — normal end, barge-in, cancellation, OR a fatal error
        raised inside the loop body (e.g. ``send_audio`` failing). A plain
        ``async for`` does NOT call ``aclose`` when the loop body raises, leaving
        the stream's frame generator suspended and abandoned for a GC finalizer to
        close later — which races a parked pull and raises ``RuntimeError:
        aclose(): asynchronous generator is already running`` (the live cascade).
        Closing here, on this consumer task, is race-free: ``aclose`` runs only
        after the body has finished, never concurrently with a pull.
        """
        first_frame_pending = on_first_frame is not None
        total_samples = 0
        peak_amplitude = 0
        tts_sample_rate = 0
        async with self._playout_lock:
            try:
                async with contextlib.aclosing(stream):
                    async for frame in stream:
                        await self._transport.send_audio(frame)
                        if first_frame_pending and on_first_frame is not None:
                            on_first_frame()
                            first_frame_pending = False
                        # Accumulate audio stats for the end-of-stream log.
                        n_samp = len(frame.samples) // 2
                        if n_samp > 0:
                            total_samples += n_samp
                            if tts_sample_rate == 0:
                                tts_sample_rate = frame.sample_rate
                            pcm_vals = struct.unpack_from(f"<{n_samp}h", frame.samples)
                            frame_peak = max(abs(s) for s in pcm_vals)
                            peak_amplitude = max(peak_amplitude, frame_peak)
            finally:
                # Clear the reference only if it is still ours (a superseding
                # speak() may have already pointed it at a newer stream).
                if self._active_tts_stream is stream:
                    self._active_tts_stream = None
        if total_samples > 0 and tts_sample_rate > 0:
            duration_ms = math.floor(total_samples * 1000 / tts_sample_rate)
            _log.info(
                "tts playout: %d ms of audio synthesised (peak=%d, %.1f%% full-scale)",
                duration_ms,
                peak_amplitude,
                peak_amplitude / 327.67,
            )

    def _begin_greeting(self) -> TtsStream:
        """Synthesise + register the greeting stream synchronously (no await).

        Called from ``run`` before the TaskGroup starts the pump, so the greeting
        stream is the active stream before any inbound frame can be processed —
        a first-frame caller onset therefore barges in correctly. ``synthesize``
        is a synchronous factory, so this whole method runs without yielding.
        Logs the synth start at INFO. Only called when ``self._greeting`` is
        non-empty.
        """
        greeting = sanitize_for_speech(self._greeting)
        _log.info("greeting: synthesising %d chars", len(greeting))

        async def _single_chunk() -> AsyncIterator[str]:
            yield greeting

        stream = self._tts.synthesize(_single_chunk(), self._voice)
        self._active_tts_stream = stream
        return stream

    def _begin_tone(self) -> TtsStream:
        """Build + register a pure-sine tone stream synchronously (no await).

        Creates a :class:`_ToneStream` wrapping :func:`generate_tone_frames` at
        the configured ``tone_secs`` duration.  The stream is registered as the
        active TTS stream before any ``await`` so a first-frame caller onset
        barges in correctly — identical lifecycle to :meth:`_begin_greeting`.
        Only called when ``self._tone_secs > 0``.
        """
        duration = self._tone_secs
        _log.info("tone diagnostic: %.1f s at 440 Hz (bypassing TTS)", duration)
        stream: TtsStream = _ToneStream(duration_secs=duration)
        self._active_tts_stream = stream
        return stream

    async def _play_greeting(self, stream: TtsStream) -> None:
        """Play the pre-registered greeting stream; log the first RTP frame.

        Runs as its own TaskGroup task so the greeting plays concurrently with
        the inbound pump (never blocking it) and stops at once on barge-in (the
        pump cancels ``_active_tts_stream`` — this stream — on a caller onset).
        """

        def _log_first_rtp() -> None:
            _log.info("greeting: first RTP sent")

        await self._play(stream, on_first_frame=_log_first_rtp)

    async def barge_in(self) -> None:
        """Cancel the in-flight TtsStream for immediate barge-in.

        Calls ``cancel()`` on the active ``TtsStream`` if one is in progress,
        causing ``_play()``'s iteration loop to exit before the next
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
