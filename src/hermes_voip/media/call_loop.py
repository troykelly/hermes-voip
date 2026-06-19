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
import random
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import Enum
from typing import Final, Protocol

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
from hermes_voip.tts.failover import reset_failover_if_supported

_log: Final = logging.getLogger(__name__)

#: Maximum audio frames buffered between the pump and the ASR task (back-pressure:
#: the pump blocks once the ASR task is this far behind). Bounds inbound memory.
_AUDIO_QUEUE_MAX: Final[int] = 32

#: Maximum finalised transcripts buffered between the ASR task and the delivery
#: task. Bounds memory if guard screening / delivery lags behind ASR finals
#: (finding #3): ASR back-pressures rather than buffering an unbounded backlog.
_TRANSCRIPT_QUEUE_MAX: Final[int] = 32

#: Default comfort-filler phrase set (ADR-0030, ADR-0054), used when a CallLoop is
#: constructed without an explicit set. Each phrase reads naturally on every TTS model
#: (no bracket tag), so the default never depends on v3 tag rendering; the set is
#: intentionally varied so random (no-immediate-repeat) selection does not sound
#: cyclic. The adapter normally passes ``MediaConfig.comfort_filler_phrases`` (the
#: language-selected set, English by default — the same phrases as here).
_DEFAULT_COMFORT_FILLER_PHRASES: Final[tuple[str, ...]] = (
    "Just a moment.",
    "One moment please.",
    "Bear with me.",
    "Let me check that for you.",
    "Just a second.",
    "Almost there.",
    "Hold on a moment.",
    "Let me look into that.",
    "Give me just a second.",
    "One moment.",
)

#: Default comfort-filler master switch (ADR-0054): ON. The adapter passes the
#: operator's ``MediaConfig.comfort_filler``; this is the bare-construction default.
_DEFAULT_COMFORT_FILLER: Final[bool] = True

#: Default dead-air threshold (ms) before the FIRST comfort filler fires (ADR-0030).
_DEFAULT_COMFORT_FILLER_DELAY_MS: Final[int] = 900

#: Default PERIODIC repeat interval (ms): on a sustained gap a fresh filler fires this
#: often after the first, until the reply audio starts (ADR-0054). Defaults to the
#: dead-air delay (one cadence). The adapter passes ``comfort_filler_repeat_ms``.
_DEFAULT_COMFORT_FILLER_REPEAT_MS: Final[int] = _DEFAULT_COMFORT_FILLER_DELAY_MS

#: Default caller-silence reprompt master switch (ADR-0057): ON. A live-but-silent
#: caller (RTP flowing, no end-of-turn) otherwise sits in dead air indefinitely (the
#: engine's RTP watchdog only fires on DEAD media, not silent-but-flowing media). The
#: adapter passes the operator's config; this is the bare-construction default.
_DEFAULT_NO_INPUT_REPROMPT: Final[bool] = True

#: Default silence window (ms) of no caller end-of-turn before a reprompt fires
#: (ADR-0057). 10 s is long enough not to nag a thinking caller, short enough that a
#: dropped/abandoned line is noticed promptly.
_DEFAULT_NO_INPUT_TIMEOUT_MS: Final[int] = 10_000

#: Default number of unanswered reprompts before the loop ends the call gracefully
#: (ADR-0057). After this many silent windows each followed by an unanswered reprompt,
#: the caller is treated as gone and the call is wound up (goodbye, then a clean end).
_DEFAULT_NO_INPUT_MAX_REPROMPTS: Final[int] = 2

#: Default reprompt phrase set (ADR-0057), used when a CallLoop is built without an
#: explicit set. Each phrase reads naturally on every TTS model (no bracket tag), and is
#: chosen at RANDOM per fire (no immediate repeat) so repeated reprompts on one call do
#: not sound mechanical. Multi-language-ready: the adapter/config selects a language set
#: the same way the comfort-filler phrases are selected (ADR-0054).
_DEFAULT_NO_INPUT_REPROMPT_PHRASES: Final[tuple[str, ...]] = (
    "Are you still there?",
    "Hello, are you still there?",
    "Sorry, I can't hear anything. Are you still there?",
)

#: Default spoken-goodbye master switch (ADR-0057): ON. On a loop-initiated graceful end
#: (the no-input reprompt limit is exhausted) a short closing line is spoken and flushed
#: BEFORE the loop returns (so it still has a live media path), instead of dropping
#: silently to BYE. NOT spoken on a caller-hangup / inbound-EOS / error end: there is no
#: media path once the caller is gone or the pipeline has failed.
_DEFAULT_GOODBYE: Final[bool] = True

#: Default goodbye phrase (ADR-0057). Reads naturally on every TTS model; multi-language
#: selection follows the comfort-filler phrase mechanism (the adapter/config passes the
#: language-appropriate line).
_DEFAULT_GOODBYE_PHRASE: Final[str] = "Goodbye."

#: Default inter-digit gap (ms) after which a buffered DTMF group is delivered as a
#: menu turn when no ``#`` terminator arrives (ADR-0010). Used when the adapter passes
#: ``dtmf_interdigit_ms=None`` (the config key was unset). 2 s is comfortably longer
#: than a human's between-key gap so a single multi-digit entry is not split.
_DEFAULT_DTMF_INTERDIGIT_MS: Final[int] = 2000

#: The DTMF group terminator: a ``#`` ends the current digit group immediately (the
#: conventional "enter" key for keypad data entry), ADR-0010.
_DTMF_TERMINATOR: Final[str] = "#"

#: The tag prefixing a delivered DTMF menu group, so a model turn can never confuse a
#: keypress for spoken words (ADR-0010 §surfacing). Digits never pass through STT/LLM
#: as a fake transcript; they arrive clearly marked.
_DTMF_TURN_PREFIX: Final[str] = "[DTMF] "


class _DtmfConfirmationSink(Protocol):
    """The narrow surface the call loop drives on the armed-confirmation resolver.

    The loop offers each received digit to a bound confirmation while it is armed;
    ``feed`` returns whether it CONSUMED the digit (a window was armed and unresolved),
    so the loop knows not to also surface the digit as a menu turn. Concretely a
    :class:`hermes_voip.dtmf_confirm.ArmedConfirmation`; the Protocol keeps the call
    loop decoupled from that module (the adapter wires the concrete resolver).
    """

    def feed(self, digit: str) -> bool:
        """Offer one received DTMF digit; return whether an armed window consumed it."""
        ...


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
    """How an inbound speech onset is allowed to interrupt the agent (ADR-0023).

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
    """Echo-robust barge-in decision (ADR-0023).

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
        barge_in_mode: Echo-robust barge-in mode (ADR-0023). ``GATED`` (default)
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
        barge_in_fade_ms: Length (ms) of the linear fade-out the engine applies to
            the final frames when a barge-in flushes the outbound audio (ADR-0028),
            so the clean stop is click-free. ``0`` is an instant hard cut. The
            adapter passes ``HERMES_VOIP_BARGE_IN_FADE_MS`` (default 30).
        comfort_filler: Dead-air comfort filler master switch (ADR-0030, ADR-0054),
            ``True`` by default. When ``True``, after a caller turn is delivered the
            loop schedules a task that, once the gap exceeds ``comfort_filler_delay_ms``
            before the agent's reply audio starts, emits a short natural filler
            ("One moment please.") through the normal TTS/send path, then RE-EMITS a
            fresh random phrase every ``comfort_filler_repeat_ms`` until the reply
            starts — so a long wait does not leave a long silence. Cancelled by the
            real reply (:meth:`speak`) or a barge-in (:meth:`barge_in`). Off = the
            pre-filler behaviour exactly (no filler task is created).
        comfort_filler_delay_ms: Dead-air threshold (ms) before the FIRST filler fires.
            Must be positive (the adapter passes the validated config value).
        comfort_filler_repeat_ms: Periodic interval (ms) between subsequent fillers on
            a sustained gap (ADR-0054). Must be positive.
        comfort_filler_phrases: The filler phrase set; one is chosen at RANDOM per fire,
            never repeating the immediately-previous phrase. Each phrase reads naturally
            on every TTS model. Must be non-empty (defaults to the built-in set).
        no_input_reprompt: Caller-silence reprompt master switch (ADR-0057), ``True`` by
            default. When ``True``, a live-but-silent caller (RTP flowing but no
            end-of-turn) is reprompted ("Are you still there?") after
            ``no_input_timeout_ms`` of silence, and after ``no_input_max_reprompts``
            unanswered reprompts the loop ends the call gracefully (a spoken goodbye if
            ``goodbye``, then a CLEAN :meth:`run` return — never a raise). Off = no
            watchdog task is created (the prior behaviour). The window resets the
            instant the caller speaks (a delivered turn) or barges in.
        no_input_timeout_ms: Silence window (ms) of no caller end-of-turn before a
            reprompt fires (ADR-0057). Must be positive (the adapter passes the
            validated config value).
        no_input_max_reprompts: Unanswered reprompts before the loop ends the call
            gracefully (ADR-0057). Must be ``>= 0``; ``0`` ends on the first silent
            window with no reprompt at all (straight to goodbye + end).
        no_input_reprompt_phrases: The reprompt phrase set; one is chosen at RANDOM per
            fire, never repeating the immediately-previous phrase. Each phrase reads
            naturally on every TTS model. Must be non-empty (defaults to the built-in
            set). Multi-language-ready like the comfort-filler phrases (ADR-0054).
        goodbye: Spoken-goodbye master switch (ADR-0057), ``True`` by default. When
            ``True``, the loop-initiated graceful end (no-input limit exhausted) speaks
            ``goodbye_phrase`` and flushes it BEFORE :meth:`run` returns, so it has a
            live media path. NOT spoken on a caller-hangup / inbound-EOS / error end (no
            media path there). Off = the graceful end still happens, just silently.
        goodbye_phrase: The closing line spoken on a loop-initiated graceful end. Reads
            naturally on every TTS model; multi-language selection follows the
            comfort-filler phrase mechanism.
        rng: The random source for filler AND reprompt phrase selection (ADR-0054/0057).
            Defaults to a fresh :class:`random.Random`; tests inject a seeded one for
            determinism. It is for variety only, never security.
        dtmf_interdigit_ms: Inter-digit gap (ms) after which a buffered DTMF menu
            group is delivered when no ``#`` terminator arrives (ADR-0010 §surfacing).
            ``None`` (the config key unset) uses the built-in default (2000 ms). The
            adapter passes ``MediaConfig.dtmf_interdigit_ms``. Only used for inbound
            DTMF the controller surfaces as a menu turn — a digit consumed by an armed
            confirmation never starts this timer.
        sleep: The async sleep seam the comfort-filler delay + periodic repeats, the
            no-input silence window (ADR-0057), AND the DTMF inter-digit timer await.
            Defaults to :func:`asyncio.sleep`; tests inject a controllable sleep for
            determinism (the loop has no other wall-clock dependency).
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
        barge_in_fade_ms: int = 30,
        comfort_filler: bool = _DEFAULT_COMFORT_FILLER,
        comfort_filler_delay_ms: int = _DEFAULT_COMFORT_FILLER_DELAY_MS,
        comfort_filler_repeat_ms: int = _DEFAULT_COMFORT_FILLER_REPEAT_MS,
        comfort_filler_phrases: tuple[str, ...] = _DEFAULT_COMFORT_FILLER_PHRASES,
        no_input_reprompt: bool = _DEFAULT_NO_INPUT_REPROMPT,
        no_input_timeout_ms: int = _DEFAULT_NO_INPUT_TIMEOUT_MS,
        no_input_max_reprompts: int = _DEFAULT_NO_INPUT_MAX_REPROMPTS,
        no_input_reprompt_phrases: tuple[str, ...] = _DEFAULT_NO_INPUT_REPROMPT_PHRASES,
        goodbye: bool = _DEFAULT_GOODBYE,
        goodbye_phrase: str = _DEFAULT_GOODBYE_PHRASE,
        rng: random.Random | None = None,
        dtmf_interdigit_ms: int | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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
        # Length (ms) of the linear fade-out the engine applies to the final frames
        # when a barge-in flushes the outbound audio (ADR-0028), so the cut is
        # click-free. 0 = instant hard cut.
        self._barge_in_fade_ms = barge_in_fade_ms
        # The echo-robust barge-in decision (ADR-0023). The pump drives it from
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
        # Whether the agent's TTS is actually emitting audio on the wire (the
        # first frame has been sent and the stream has not ended). This — NOT the
        # mere registration of ``_active_tts_stream`` — is what arms the echo gate
        # (ADR-0023, codex finding C): during the TTS synthesis/startup latency no
        # audio has gone out, so there is no echo yet, and a real short caller turn
        # in that pre-playout window must NOT be withheld from the ASR. Barge-in
        # still targets ``_active_tts_stream`` from the moment it registers (a
        # caller can cut off the greeting before its first frame).
        self._tts_audio_active: bool = False
        # Serialises near-end playout so only ONE TtsStream is ever draining to
        # ``transport.send_audio`` at a time. A new ``speak`` supersedes any
        # in-flight stream (cancels it, then takes the lock), so the greeting and
        # a following agent reply never interleave frames on the wire.
        self._playout_lock = asyncio.Lock()
        # Dead-air comfort filler (ADR-0030, extended ADR-0054).
        self._comfort_filler = comfort_filler
        # Delay (seconds) the filler gap awaits before the FIRST filler. The config
        # value is validated positive; stored in seconds for the asyncio sleep seam.
        self._comfort_filler_delay_s = comfort_filler_delay_ms / 1000.0
        # Periodic interval (seconds) between subsequent fillers on a sustained gap.
        self._comfort_filler_repeat_s = comfort_filler_repeat_ms / 1000.0
        self._comfort_filler_phrases = comfort_filler_phrases
        # The random source for phrase selection (ADR-0054). Variety only, never
        # security. A fresh ``random.Random`` unless a seeded one is injected (tests).
        self._comfort_rng = rng if rng is not None else random.Random()  # noqa: S311 — non-crypto; phrase variety
        self._sleep = sleep
        # The single in-flight comfort-filler task for the current turn gap, or None
        # when no gap is pending. ``barge_in`` cancels it; ``run`` cancels any
        # lingering one at teardown so the task never leaks even mid-playout. The
        # done-callback nulls it on completion. Touched only from the event loop.
        self._comfort_filler_task: asyncio.Task[None] | None = None
        # The phrase chosen on the previous fire, so the random selector can avoid an
        # immediate repeat (ADR-0054). ``None`` until the first fire. Touched only from
        # the loop.
        self._last_comfort_phrase: str | None = None

        # Caller-silence reprompt / no-input handling (ADR-0057).
        self._no_input_reprompt = no_input_reprompt
        # Silence window (seconds) of no caller end-of-turn before a reprompt. Stored in
        # seconds for the asyncio sleep seam; the config value is validated positive.
        self._no_input_timeout_s = no_input_timeout_ms / 1000.0
        self._no_input_max_reprompts = no_input_max_reprompts
        self._no_input_reprompt_phrases = no_input_reprompt_phrases
        # The reprompt phrase chosen on the previous fire, so the random selector can
        # avoid an immediate repeat. ``None`` until the first fire. Loop-only.
        self._last_reprompt_phrase: str | None = None
        # Spoken goodbye on the loop-initiated graceful end (ADR-0057).
        self._goodbye = goodbye
        self._goodbye_phrase = goodbye_phrase
        # The single in-flight no-input watchdog task, or None when off / not yet armed.
        # ``run`` arms it at loop start (when enabled) and cancels+joins it at teardown
        # so it never leaks. The done-callback nulls it on completion.
        self._no_input_task: asyncio.Task[None] | None = None
        # Set TRUE whenever the caller shows life within the current silence window: a
        # delivered turn (:meth:`_screen_and_deliver`) or a barge-in (:meth:`barge_in`).
        # The watchdog clears it at the start of each window and re-checks it after the
        # window elapses, so any caller activity during the window resets the reprompt
        # cycle. Touched only from the event loop (no lock needed).
        self._caller_active_in_window = False
        # Set by the watchdog when it decides to end the call (no-input limit spent),
        # observed by the pump to break out of its inbound loop and emit the end marker,
        # so a loop-initiated graceful end terminates ``run`` CLEANLY (markers
        # flow down the chain, no task raises), which the adapter classifies as a normal
        # end. On a real silent-but-live call inbound RTP keeps flowing, so the pump
        # observes this within ~one frame; if inbound were truly dead the engine's RTP
        # watchdog ends the call instead (ADR-0026), so this never relies on dead media.
        self._end_call = asyncio.Event()

        # Inbound DTMF surfacing (ADR-0010). The engine fires :meth:`feed_dtmf` for
        # each received digit; the controller routes it (NOT the engine): while a
        # confirmation is armed the digit resolves it directly (a control action,
        # spoof-resistant); otherwise the digit joins a buffered group delivered as a
        # tagged ``[DTMF] …`` menu turn on a ``#`` terminator or an inter-digit timeout.
        self._dtmf_interdigit_s = (
            dtmf_interdigit_ms
            if dtmf_interdigit_ms is not None
            else _DEFAULT_DTMF_INTERDIGIT_MS
        ) / 1000.0
        # The optional armed-confirmation resolver bound for this call (an object with
        # ``feed(digit) -> bool``; concretely a ``hermes_voip.dtmf_confirm.Armed-
        # Confirmation``). ``None`` until :meth:`bind_confirmation` wires one.
        self._confirmation: _DtmfConfirmationSink | None = None
        # The digits accumulated for the current (unterminated) menu group, in order.
        self._dtmf_buffer: list[str] = []
        # The single in-flight inter-digit flush TIMER, or None when no group is
        # pending. Each new digit cancels + reschedules it; a terminator / teardown
        # cancels it. Touched only from the event loop. Distinct from the delivery
        # tasks below: cancelling the timer must never drop an in-flight delivery.
        self._dtmf_flush_task: asyncio.Task[None] | None = None
        # In-flight menu-group DELIVERY tasks (review #4). A delivery snapshots the
        # buffer at its firing instant and runs here, NOT in ``_dtmf_flush_task``, so a
        # following digit's timer (re)arm cannot cancel it — the group is never lost.
        # Strong refs held until done (asyncio keeps only weak refs); cancelled + joined
        # at loop teardown.
        self._dtmf_delivery_tasks: set[asyncio.Task[None]] = set()

    def bind_confirmation(self, confirmation: _DtmfConfirmationSink) -> None:
        """Bind the armed-confirmation resolver received digits route to (ADR-0010).

        While the bound resolver is armed, :meth:`feed_dtmf` hands it each digit and
        does NOT surface the digit as a menu turn — keeping the spoof-resistant
        confirmation channel off the STT/LLM path. The adapter binds the per-call
        resolver after constructing the loop.
        """
        self._confirmation = confirmation

    def feed_dtmf(self, digit: str) -> None:
        """Route one RECEIVED DTMF ``digit`` (the engine's ``on_dtmf`` callback).

        Synchronous (the engine fires it on the event loop). Routing:

        * if a bound confirmation resolver is armed and CONSUMES the digit, stop —
          the digit resolved a control action and must never become a menu turn;
        * otherwise buffer the digit; a ``#`` terminator delivers the group at once,
          and any other digit (re)arms the inter-digit flush timer.

        Delivery and the timer are async, so this schedules them as tracked tasks (the
        engine callback cannot await). :meth:`feed_dtmf_async` is the awaitable twin
        the tests use to drive delivery deterministically.
        """
        if self._route_confirmation(digit):
            return
        if digit == _DTMF_TERMINATOR:
            # Terminator: snapshot the buffer NOW (synchronously) and dispatch the
            # group as an uncancellable delivery task. Snapshotting before the await
            # means a following digit starts a fresh group and can NEVER cancel/lose
            # this one (cross-vendor review #4).
            self._cancel_dtmf_flush_timer()
            self._dispatch_dtmf_group(self._take_dtmf_group())
        else:
            self._dtmf_buffer.append(digit)
            self._arm_dtmf_flush_timer()

    async def feed_dtmf_async(self, digit: str) -> None:
        """Awaitable twin of :meth:`feed_dtmf` (delivers synchronously on a terminator).

        Identical routing, but a ``#`` terminator AWAITS the group delivery rather
        than scheduling it as a task — used by tests (and any awaiting caller) to
        observe the delivered turn deterministically. A non-terminator digit (re)arms
        the inter-digit timer exactly as :meth:`feed_dtmf` does.
        """
        if self._route_confirmation(digit):
            return
        if digit == _DTMF_TERMINATOR:
            self._cancel_dtmf_flush_timer()
            await self._deliver_dtmf_group(self._take_dtmf_group())
        else:
            self._dtmf_buffer.append(digit)
            self._arm_dtmf_flush_timer()

    def _route_confirmation(self, digit: str) -> bool:
        """Offer ``digit`` to a bound, armed confirmation; return whether consumed."""
        confirmation = self._confirmation
        if confirmation is None:
            return False
        return confirmation.feed(digit)

    def _take_dtmf_group(self) -> str:
        """Atomically take + clear the buffered digits (the ``#`` terminator aside).

        Synchronous, so the snapshot is taken at the firing instant: a following digit
        appends to the now-empty buffer, a fresh group, and cannot alter the one just
        taken (cross-vendor review #4). Returns ``""`` when nothing is buffered.
        """
        digits = "".join(self._dtmf_buffer)
        self._dtmf_buffer = []
        return digits

    def _arm_dtmf_flush_timer(self) -> None:
        """(Re)start the inter-digit timer so a multi-digit entry flushes as one group.

        Each new non-terminator digit cancels the prior timer and starts a fresh one,
        so the group is delivered only once ``dtmf_interdigit_ms`` elapses with no
        further digit (or a ``#`` arrives first). No-op if the buffer is empty. This
        TIMER is the only thing a following digit cancels — an in-flight DELIVERY task
        is tracked separately and is never cancelled by a new digit (review #4).
        """
        self._cancel_dtmf_flush_timer()
        if not self._dtmf_buffer:
            return
        self._dtmf_flush_task = asyncio.create_task(self._dtmf_flush_after_gap())
        self._dtmf_flush_task.add_done_callback(self._on_dtmf_timer_done)

    def _cancel_dtmf_flush_timer(self) -> None:
        """Cancel and forget the inter-digit flush timer, if any (idempotent)."""
        task = self._dtmf_flush_task
        if task is not None:
            self._dtmf_flush_task = None
            task.cancel()

    def _on_dtmf_timer_done(self, task: asyncio.Task[None]) -> None:
        """Clear the timer handle on completion; propagate nothing (cancel is normal).

        The timer's only job is to await the gap then dispatch a delivery (or be
        cancelled by a new digit / terminator). Its own body cannot fail — the
        delivery it dispatches runs in a SEPARATE tracked task whose result is handled
        by :meth:`_on_dtmf_delivery_done` — so here we only clear the handle and ignore
        the expected cancellation.
        """
        if self._dtmf_flush_task is task:
            self._dtmf_flush_task = None

    def _dispatch_dtmf_group(self, digits: str) -> None:
        """Deliver a (already-snapshotted) digit group as an UNCANCELLABLE tracked task.

        The task is held in ``_dtmf_delivery_tasks`` (NOT ``_dtmf_flush_task``), so a
        following digit's timer (re)arm cannot cancel an in-flight delivery (review #4)
        — the group is never silently dropped. A strong reference is kept until the
        task completes (asyncio holds only a weak ref); the done-callback discards it
        and logs any failure (rule 37). A no-op when ``digits`` is empty.
        """
        if not digits:
            return
        task = asyncio.create_task(self._deliver_dtmf_group(digits))
        self._dtmf_delivery_tasks.add(task)
        task.add_done_callback(self._on_dtmf_delivery_done)

    def _on_dtmf_delivery_done(self, task: asyncio.Task[None]) -> None:
        """Discard a finished delivery task; log a failure (rule 37), ignore cancel.

        A delivery routes through ``deliver_turn``; a failure there is logged and is
        non-fatal (a dropped menu group must not kill an otherwise-working call). A
        cancellation only happens at loop teardown.
        """
        self._dtmf_delivery_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.warning("DTMF group delivery failed (call continues): %r", exc)

    async def _dtmf_flush_after_gap(self) -> None:
        """Wait the inter-digit gap, then dispatch the buffered group as a menu turn.

        A new digit cancels this (raising ``CancelledError`` here, which propagates to
        end it without delivering — rule 37); a terminator delivers directly. The
        snapshot is taken AFTER the sleep (so all digits up to the gap are included)
        and dispatched as an uncancellable delivery task (review #4).
        """
        await self._sleep(self._dtmf_interdigit_s)
        self._dispatch_dtmf_group(self._take_dtmf_group())

    async def _deliver_dtmf_group(self, digits: str) -> None:
        """Deliver an already-snapshotted ``digits`` group as a tagged ``[DTMF]`` turn.

        A no-op when ``digits`` is empty (a terminator with nothing buffered). The
        digits are delivered to the agent via ``deliver_turn`` with the ``[DTMF]`` tag
        (never a fake STT transcript), so a normal turn can act on keypad menu input.
        The buffer was already taken by :meth:`_take_dtmf_group` at the firing instant,
        so this method holds no shared state a concurrent feed could race.
        """
        if not digits:
            return
        text = f"{_DTMF_TURN_PREFIX}{digits}"
        _log.info("dtmf: delivering menu group %r", text)
        await self._deliver_turn(text)

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
        # Reset any per-call TTS failover latch (ADR-0025) at call start: the
        # providers are process-wide, so a FailoverTTS that latched to its self-host
        # fallback on a PRIOR call must retry the primary on this fresh call. A plain
        # TTS without the hook is untouched (duck-typed via SupportsCallReset).
        reset_failover_if_supported(self._tts)

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
            # Track the agent's TTS-audio-active state across frames so the echo
            # gate (ADR-0023) can arm a post-TTS tail at the True→False edge.
            prev_tts_active = self._tts_audio_active
            # One-shot DEBUG flag: log the first echo frame withheld from the ASR
            # so the operator sees the gate engage once without flooding the log.
            echo_drop_logged = False
            async for frame in self._transport.inbound_audio():
                # Loop-initiated graceful end (ADR-0057): the no-input watchdog has
                # decided the caller is gone and already spoken the goodbye. Stop
                # consuming inbound audio and fall through to the end-of-stream marker
                # below, so the chain drains and ``run`` returns CLEANLY (a normal end),
                # exactly as if the inbound stream had ended. Checked here (top of the
                # per-frame body) so it is observed within ~one inbound frame on a live
                # call; dead media is handled by the engine's RTP watchdog, not here.
                if self._end_call.is_set():
                    _log.info(
                        "pump: graceful end (no-input) after %d frames",
                        frames_received,
                    )
                    break
                # Collect this frame's VAD edges. They drive the barge-in gate
                # unconditionally (it must see echo to keep its run state correct),
                # but the ENDPOINTER must only see edges from audio that actually
                # reaches the ASR — feeding it echo edges would let a stale echo
                # OFFSET fire a spurious end-of-turn once the gate later disarms.
                frame_events = list(self._vad.feed(frame))
                for vad_event in frame_events:
                    # ONSET starts a candidate voiced run; OFFSET ends it (dismissing
                    # a short echo blip). The gate's window clock is the VAD window
                    # ordinal on the edge, NOT the per-frame counter below.
                    self._barge_in_gate.on_event(vad_event)
                    _log.debug(
                        "pump: VAD %s at vad-window %d",
                        vad_event.edge.name,
                        vad_event.frame_index,
                    )
                # ``window_index`` (the VAD property) is the NEXT window ordinal, so
                # the last scored window is one less; -1 before any window is scored
                # is harmless (no onset is pending then). The echo gate is armed
                # whenever the agent's TTS is actually emitting audio on the wire
                # (``_tts_audio_active``) — NOT merely while a stream is registered:
                # during TTS synthesis latency no audio has gone out, so there is no
                # echo and a real short caller turn must not be withheld (codex
                # finding C). A short tail after audio stops covers the echo lag.
                tts_audio = self._tts_audio_active
                latest_vad_window = self._vad.window_index - 1
                if prev_tts_active and not tts_audio:
                    # The agent's audio just stopped: keep gating across the echo
                    # tail (echo lags the TTS via the jitter buffer / network).
                    self._barge_in_gate.tail_from(latest_vad_window)
                self._barge_in_gate.tts_active(tts_audio)
                prev_tts_active = tts_audio
                # Drive the gate's authorisation on EVERY frame (codex finding #B),
                # not only while TTS is active: a sustained run that authorises
                # during the post-TTS tail must still count, or its turn would be
                # withheld below. ``should_barge_in`` fires (and authorises the run)
                # under the gate's own armed/sustained rules; ``barge_in()`` is a
                # no-op when no TTS stream is active, so calling it unconditionally
                # is safe.
                if self._barge_in_gate.should_barge_in(latest_vad_window):
                    _log.debug(
                        "pump: barge-in authorised at vad-window %d",
                        latest_vad_window,
                    )
                    await self.barge_in()
                # Echo-robust turn gate (ADR-0023, codex findings #1/#A): while the
                # agent's TTS is playing (or in the tail) and the current speech run
                # is NOT an authorised barge-in, the inbound audio is the gateway's
                # echo of the agent's own speech. WITHHOLD it from the ASR entirely
                # (do not forward the frame, do not feed/advance the endpointer), so
                # no echo transcript is ever produced — on the endpointer path OR a
                # native-EOT recogniser's own end_of_turn (e.g. Deepgram Flux). A
                # genuine sustained interruption authorises its run (and cancels the
                # TTS), after which audio flows and its transcript is delivered.
                suppress_echo = self._barge_in_gate.delivery_suppressed(
                    latest_vad_window
                )
                if not suppress_echo:
                    # Only now (real audio bound for the ASR) does the endpointer see
                    # the edges and tick — so a withheld echo run never arms it.
                    for vad_event in frame_events:
                        self._endpointer.on_event(vad_event)
                    if self._endpointer.advance(window_index):
                        # ADR-0008: endpointer owns the turn boundary. Increment the
                        # EOT counter so the ASR task's next is_final transcript is
                        # delivered as end-of-turn. Each increment is consumed by
                        # exactly one decrement in _asr (W2 fix: an int counts fires;
                        # an Event is boolean and loses duplicates).
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
                if suppress_echo:
                    # Drop the echoed frame from the ASR input (see above). The VAD
                    # and the barge-in gate were still driven on it (so a sustained
                    # run is still detected); only the recogniser and the endpointer
                    # never see it.
                    if not echo_drop_logged:
                        echo_drop_logged = True
                        _log.debug(
                            "pump: withholding echo audio from ASR at window %d "
                            "(unauthorised speech during TTS playout/tail)",
                            window_index,
                        )
                    continue
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

        # Arm the caller-silence / no-input watchdog (ADR-0057) for the whole call, as a
        # tracked task OUTSIDE the TaskGroup: it is best-effort (a reprompt/goodbye
        # synthesis failure must never kill the call — rule 37) and may end the call
        # itself, so it must not be a TaskGroup child whose raise would cancel the
        # pipeline. No-op when disabled. Cancelled + joined in the finally (no leak).
        self._schedule_no_input_watchdog()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(_pump())
                tg.create_task(_asr())
                tg.create_task(_delivery())
                if greeting_stream is not None:
                    tg.create_task(self._play_greeting(greeting_stream))
        finally:
            # The no-input watchdog (ADR-0057) is a tracked task outside the TaskGroup
            # (armed at loop start). Cancel + JOIN it on every exit path so it never
            # outlives the call — incl. one parked mid-reprompt/goodbye playout. Same
            # best-effort join as the filler: its result is handled by its own
            # done-callback (rule 37) and must not mask the loop's teardown/exception.
            watchdog = self._no_input_task
            self._cancel_no_input_watchdog()
            if watchdog is not None:
                await asyncio.wait({watchdog})
            # The comfort filler runs as a tracked task OUTSIDE the TaskGroup (it is
            # armed per delivered turn, not at loop start). On every exit path —
            # normal end, error, or cancellation — cancel it AND JOIN it so it fully
            # unwinds (it may be mid-_play / inside send_audio / closing the TTS
            # stream via aclosing) before run() returns: no task continues running
            # past the call (ADR-0030; no leaked task). Capture the handle first
            # (``_cancel_comfort_filler`` nulls it). ``asyncio.wait`` joins the task
            # WITHOUT re-raising its result here — a filler error is best-effort and
            # already logged by ``_on_comfort_filler_done`` (rule 37: logged, not
            # swallowed), and must not mask the loop's own teardown/exception.
            filler = self._comfort_filler_task
            self._cancel_comfort_filler()
            if filler is not None:
                await asyncio.wait({filler})
            # The inbound-DTMF tasks (ADR-0010) are likewise tracked outside the
            # TaskGroup (armed per received digit): the inter-digit TIMER and any
            # in-flight group DELIVERY tasks. Cancel + JOIN them all so none outlives
            # the call. Best-effort join (like the filler): each task's result is
            # handled by its own done-callback (rule 37) and must not mask the loop's
            # teardown. Snapshot the delivery set before cancelling (the callbacks
            # mutate it).
            dtmf_tasks: set[asyncio.Task[None]] = set(self._dtmf_delivery_tasks)
            dtmf_timer = self._dtmf_flush_task
            self._cancel_dtmf_flush_timer()
            if dtmf_timer is not None:
                dtmf_tasks.add(dtmf_timer)
            for delivery in dtmf_tasks:
                delivery.cancel()
            if dtmf_tasks:
                await asyncio.wait(dtmf_tasks)

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
        # The agent is committing to a reply: the comfort filler covers the
        # caller-finish→reply *processing* gap (STT/LLM think time — the operator's
        # "while STT/LLM are processing"), and stands down the moment the agent has a
        # reply to speak (ADR-0030 §"when the filler stands down"). So:
        #  * cancel a PENDING (not-yet-fired) filler — the agent has the floor now, do
        #    not start a filler that would race the imminent reply (and, because the
        #    reply's _play below holds the single playout lock through its own TTS
        #    first-audio latency, a filler could not cleanly interleave anyway);
        #  * a filler already PLAYING is superseded by the stream-supersede inside
        #    _speak_text (its stream is cancelled like any in-flight stream).
        self._cancel_comfort_filler()
        await self._speak_text(text, on_first_frame=on_first_frame)

    async def _speak_text(
        self,
        text: AsyncIterator[str],
        *,
        on_first_frame: Callable[[], None] | None,
    ) -> None:
        """Synthesise *text* and play it, superseding any in-flight stream.

        The shared body of :meth:`speak` and the comfort filler: it does NOT touch
        the comfort-filler task (so the filler can call it without cancelling
        itself), only the stream-supersede + playout.
        """
        # Sanitize each text chunk before TTS synthesis so that emoji,
        # markdown markup, and raw URLs are never voiced by the TTS engine.
        # Pass the negotiated wire rate (codec-derived: 8 kHz G.711, 16 kHz G.722)
        # so the synthesiser emits the negotiated rate (ADR-0022) — no wideband
        # thrown away, no needless G.711 resample. The engine still reconciles any
        # off-rate frames (e.g. Kokoro's fixed 24 kHz) to the wire rate.
        stream = self._tts.synthesize(
            _sanitize_iter(text),
            self._voice,
            sample_rate=self._transport.inbound_sample_rate,
        )
        # Register synchronously (no await before this line), then supersede any
        # previously-active stream so playout is single-owner.
        previous = self._active_tts_stream
        self._active_tts_stream = stream
        if previous is not None and previous is not stream:
            await previous.cancel()
        await self._play(stream, on_first_frame=on_first_frame)

    def _schedule_comfort_filler(self) -> None:
        """Arm a one-shot dead-air comfort filler for the turn just delivered.

        Called from :meth:`_screen_and_deliver` right after a turn is handed to the
        agent, only when the filler is enabled. Replaces any prior pending filler
        (a new turn's gap supersedes an older one) and launches :meth:`_comfort_gap`
        as a tracked task so it runs concurrently with the agent turn — it never
        blocks delivery. No-op when the filler is disabled.
        """
        if not self._comfort_filler:
            return
        # A new turn supersedes any still-pending prior gap.
        self._cancel_comfort_filler()
        task = asyncio.create_task(self._comfort_gap())
        # The task is fire-and-forget (it runs concurrently with the agent turn and
        # is never awaited), so attach a done-callback to RETRIEVE its result: a
        # cancellation is the normal stop (reply/barge-in/teardown) and is ignored,
        # but a synthesis/send failure is LOGGED (rule 37: never silently swallowed)
        # and is NOT fatal to the call — the filler is a best-effort comfort feature,
        # so a working call must survive a failed filler and still play the real
        # reply. Without this callback the exception would surface as an unretrieved
        # "Task exception was never retrieved" warning.
        task.add_done_callback(self._on_comfort_filler_done)
        self._comfort_filler_task = task

    def _on_comfort_filler_done(self, task: asyncio.Task[None]) -> None:
        """Retrieve a finished filler task's result; log a real failure, ignore cancel.

        Also clears ``_comfort_filler_task`` when this is the current handle, so a
        naturally-completed filler leaves no stale handle. A comfort filler is
        best-effort: a synthesis/send failure is logged at warning and deliberately
        does not propagate (a failed filler must not kill an otherwise-working call),
        while a cancellation (the normal reply-supersede / barge-in / teardown stop)
        is expected and ignored. This is a logged, intentional degradation — not a
        swallowed error.
        """
        # Clear our handle on completion (only if it is still us — a newer gap may
        # have already replaced it), so a finished filler is not later cancelled.
        if self._comfort_filler_task is task:
            self._comfort_filler_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.warning("comfort filler task failed (call continues): %r", exc)

    def _cancel_comfort_filler(self) -> None:
        """Cancel and forget the comfort-filler task, if any (pending OR playing).

        Idempotent and safe to call from the event loop. Called by :meth:`speak` (the
        agent has a reply — stand down), :meth:`barge_in` (the caller is taking the
        floor), and :meth:`run`'s teardown (no leaked task, even one mid-playout).
        Cancelling a PLAYING filler is harmless: its ``_play`` unwinds via
        ``aclosing`` and the cancel is ignored by the done-callback. A real reply
        arriving while the filler plays *also* supersedes the filler stream in
        :meth:`_speak_text`; the cancel here is the same intent via the task.
        """
        task = self._comfort_filler_task
        if task is not None:
            self._comfort_filler_task = None
            task.cancel()

    def _next_comfort_phrase(self) -> str:
        """Return a RANDOM filler phrase, never the immediately-previous one (ADR-0054).

        Random (not round-robin) so a multi-fire / multi-gap call does not sound
        mechanically cyclic; the one guarantee is no back-to-back repeat. Selection is a
        *direct* choice from the distinct phrases other than the last one (not rejection
        sampling), so it terminates even when the set has duplicates and no other
        distinct value (e.g. ``("A", "A")``): then there is no alternative and the only
        phrase is returned (no starvation, no spin). A single-phrase set is the same
        case. Uses the injected :attr:`_comfort_rng` (a seeded ``random.Random`` in
        tests) — for variety only, never security.
        """
        phrases = self._comfort_filler_phrases
        # Distinct candidates other than the immediately-previous phrase, order-kept
        # (a seeded RNG draw stays deterministic). Empty only when every phrase equals
        # the last (incl. a single-phrase set) — then the no-repeat rule has no choice.
        alternatives = tuple(
            dict.fromkeys(p for p in phrases if p != self._last_comfort_phrase)
        )
        phrase = self._comfort_rng.choice(alternatives) if alternatives else phrases[0]
        self._last_comfort_phrase = phrase
        return phrase

    async def _comfort_gap(self) -> None:
        """Fill dead air: one filler after the delay, then a fresh one each repeat.

        Awaits the configured delay on the injected sleep seam, then — on GENUINE dead
        air only — emits a short random filler through the normal TTS/send path, and
        keeps re-emitting a fresh random phrase every ``comfort_filler_repeat_ms`` for
        as long as the gap persists (ADR-0054). A single ~1 s phrase does not fill a
        10 s LLM wait; the periodic re-fire does, so a long wait never leaves a long
        silence. The loop runs until the real reply or a barge-in/teardown cancels it.

        Each iteration re-checks ``_tts_audio_active`` and SKIPS the fire while agent
        audio is on the wire right now — the ADR-0030 dead-air guard, preserved
        per-iteration: a greeting or a prior reply still playing (audio that began
        before this gap was armed), or the real reply's own audio, is not dead air, so
        we never speak over it. A reply commit also cancels this task in :meth:`speak`,
        so in the common case the loop is gone before its audio even starts.

        Every fired phrase routes through :meth:`_speak_text`, so it is sanitised,
        model-conditional-tag-aware (ADR-0027), echo-gate-arming and flushable
        (ADR-0023/0028) like any agent audio. A real reply arriving mid-filler
        supersedes the filler stream there; the next iteration's audio check (or the
        ``speak`` cancel) then stops the loop.

        Best-effort (rule 37): a filler synthesis/send failure is caught, logged, and
        the loop CONTINUES to the next interval — a transient TTS hiccup neither stops
        the periodic fill nor (being a fire-and-forget task) the call. A
        ``CancelledError`` is the normal stop and is never caught.

        A barge-in / reply-commit / teardown cancels this task (raising
        ``CancelledError`` here — at the sleep or mid-playout), which propagates to end
        it cleanly; it is never swallowed (rule 37). The task keeps its
        ``_comfort_filler_task`` handle for its whole life (including playout) so
        :meth:`run`'s teardown can cancel it even mid-stream (no leak); the
        done-callback clears the handle on completion (and logs any unexpected escape).
        """
        await self._sleep(self._comfort_filler_delay_s)
        while True:
            # Fire only on GENUINE dead air: no agent audio on the wire right now
            # (`_tts_audio_active`). This covers BOTH the agent's reply for this gap (a
            # reply commit also cancels this task in `speak()`) AND any other agent
            # audio still playing — a greeting or a prior reply that began before this
            # gap was armed. Firing while such audio plays would supersede live speech,
            # which is not dead air; so we skip this iteration and re-check after the
            # next repeat interval (the audio may have cleared by then).
            if not self._tts_audio_active:
                phrase = self._next_comfort_phrase()
                _log.info("comfort filler: emitting %r on dead air", phrase)

                async def _single_chunk(text: str = phrase) -> AsyncIterator[str]:
                    yield text

                # Play through the shared _speak_text path (sanitised, model-tag-aware,
                # echo-gate-arming, flushable). A real reply arriving mid-filler
                # supersedes this stream via _speak_text's stream-supersede; the next
                # iteration's audio check (or the speak() cancel) stops the loop.
                #
                # Best-effort (rule 37): a filler synthesis/send failure is LOGGED and
                # the loop CONTINUES — a transient TTS hiccup must not end periodic
                # fill for the rest of the gap, nor (a fire-and-forget task) the call.
                # A CancelledError is the normal stop (reply/barge-in/teardown) and MUST
                # propagate to end the loop — it is never caught here.
                try:
                    await self._speak_text(_single_chunk(), on_first_frame=None)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — best-effort filler; logged, never fatal (rule 37)
                    _log.warning(
                        "comfort filler: synthesis/send failed (call continues), "
                        "will retry next interval",
                        exc_info=True,
                    )
            # Wait the repeat interval, then re-check. Cancelled here (the common stop)
            # when the reply commits or a barge-in/teardown fires — CancelledError ends
            # the loop cleanly without another fire.
            await self._sleep(self._comfort_filler_repeat_s)

    def _schedule_no_input_watchdog(self) -> None:
        """Arm the caller-silence / no-input watchdog for this call (ADR-0057).

        Called once from :meth:`run` at loop start, only when the feature is enabled.
        Launches :meth:`_no_input_gap` as a tracked task so it runs concurrently with
        the pipeline (it never blocks audio). No-op when the watchdog is disabled. The
        done-callback (:meth:`_on_no_input_done`) retrieves the task's result so a
        failure is logged (rule 37) and never surfaces as an unretrieved-task warning.
        """
        if not self._no_input_reprompt:
            return
        task = asyncio.create_task(self._no_input_gap())
        task.add_done_callback(self._on_no_input_done)
        self._no_input_task = task

    def _on_no_input_done(self, task: asyncio.Task[None]) -> None:
        """Retrieve a finished watchdog's result; log a real failure, ignore cancel.

        Clears ``_no_input_task`` when this is the current handle. The watchdog is
        best-effort: a reprompt/goodbye synthesis or send failure is logged at warning
        and deliberately does NOT propagate (a failed reprompt must not kill an
        otherwise working call); a cancellation (the teardown stop) is expected. A
        logged, intentional degradation — not a swallowed error (rule 37).
        """
        if self._no_input_task is task:
            self._no_input_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.warning("no-input watchdog failed (call continues): %r", exc)

    def _cancel_no_input_watchdog(self) -> None:
        """Cancel and forget the no-input watchdog task, if any (idempotent).

        Called from :meth:`run`'s teardown so the watchdog never outlives the call, even
        one parked mid-reprompt/goodbye playout. Cancelling raises ``CancelledError``
        inside :meth:`_no_input_gap` (at its sleep or mid-playout), which propagates to
        end it cleanly; the cancel is ignored by the done-callback.
        """
        task = self._no_input_task
        if task is not None:
            self._no_input_task = None
            task.cancel()

    def _next_reprompt_phrase(self) -> str:
        """Return a RANDOM reprompt phrase, never the immediately-previous one.

        Same selection discipline as the comfort filler (:meth:`_next_comfort_phrase`,
        ADR-0057): random for variety, with the one guarantee of no back-to-back repeat,
        and a direct choice among the distinct alternatives so it terminates even for a
        single-phrase or all-duplicate set. Uses the shared injected ``_comfort_rng``
        (seeded in tests).
        """
        phrases = self._no_input_reprompt_phrases
        alternatives = tuple(
            dict.fromkeys(p for p in phrases if p != self._last_reprompt_phrase)
        )
        phrase = self._comfort_rng.choice(alternatives) if alternatives else phrases[0]
        self._last_reprompt_phrase = phrase
        return phrase

    async def _no_input_gap(self) -> None:
        """Reprompt a silent caller; end gracefully after N unanswered reprompts.

        The whole-call watchdog for a live-but-silent caller (ADR-0057). Each iteration:

        1. clears the per-window activity flag and awaits one silence window on the
           injected sleep seam;
        2. if the caller showed life during the window (a delivered turn or a barge-in
           set ``_caller_active_in_window``), RESETS the reprompt count and re-arms — a
           caller who is talking is plainly still there;
        3. else, if the agent's TTS is on the wire right now (``_tts_audio_active``), it
           is not dead air (a reply / greeting / prior reprompt is still playing), so it
           SKIPS this window and re-checks after the next (never speaks over the agent);
        4. else, on genuine dead air: if the reprompt budget is not yet spent, speak ONE
           reprompt (best-effort) and continue; once the budget is spent, end the call
           gracefully — speak the goodbye (if enabled) and signal the pump to wind up.

        The reprompt and goodbye route through :meth:`_speak_text`, so they are
        sanitised, model-tag-aware (ADR-0027), echo-gate-arming and flushable
        (ADR-0023/0028) like any agent audio — a barge-in cancels a playing reprompt
        exactly like a reply.

        Best-effort (rule 37): a reprompt synthesis/send failure is caught, logged, and
        the loop CONTINUES (a transient TTS hiccup neither stops the watchdog nor the
        call); a ``CancelledError`` (teardown) is the normal stop and is never caught.
        """
        reprompts_sent = 0
        while True:
            await self._sleep(self._no_input_timeout_s)
            # Consume the activity flag at exactly ONE point (here, after the window),
            # never at the loop top (codex review): caller life that arrives AFTER this
            # check — e.g. a barge-in WHILE a reprompt is playing, or during a skipped
            # agent-audio window — would be wiped by a top-of-loop clear before the next
            # window's check sees it, hanging up on a caller who just answered. Clearing
            # only on consume makes such activity persist to the next window's check.
            # Read-then-clear with no await between them: single-loop asyncio, so no
            # activity can interleave and be lost across these two lines.
            if self._caller_active_in_window:
                # The caller spoke / barged in within the window (or during the previous
                # reprompt/skip): reset the reprompt cycle and re-arm.
                self._caller_active_in_window = False
                reprompts_sent = 0
                continue
            if self._tts_audio_active:
                # Agent audio on the wire right now: not dead air; do not speak over it.
                # Re-check next window (it may have cleared by then). This does NOT use
                # the reprompt budget — no reprompt was actually heard.
                continue
            if reprompts_sent < self._no_input_max_reprompts:
                reprompts_sent += 1
                phrase = self._next_reprompt_phrase()
                _log.info(
                    "no-input: reprompt %d/%d on caller silence: %r",
                    reprompts_sent,
                    self._no_input_max_reprompts,
                    phrase,
                )
                await self._speak_phrase_best_effort(phrase, what="reprompt")
                continue
            # The reprompt budget is spent and the caller is still silent: wind up the
            # call gracefully (a spoken goodbye if enabled), then stop.
            _log.info(
                "no-input: caller silent after %d reprompt(s); ending the call",
                reprompts_sent,
            )
            await self._end_call_gracefully()
            return

    async def _speak_phrase_best_effort(self, phrase: str, *, what: str) -> None:
        """Speak one short ``phrase`` through the normal TTS path; best-effort.

        Used for a no-input reprompt and the goodbye. Routes through :meth:`_speak_text`
        (sanitised, model-tag-aware, echo-gate-arming, flushable) — NOT :meth:`speak`,
        so it does not cancel the no-input watchdog task calling it (mirroring how the
        comfort filler calls ``_speak_text`` directly). A synthesis/send failure is
        logged and swallowed so a working call survives a TTS hiccup (rule 37); a
        ``CancelledError`` (the teardown / barge-in stop) propagates to end it cleanly.
        """

        async def _single_chunk() -> AsyncIterator[str]:
            yield phrase

        try:
            await self._speak_text(_single_chunk(), on_first_frame=None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — best-effort spoken UX; logged, never fatal (rule 37)
            _log.warning(
                "no-input: %s synthesis/send failed (call continues): %r",
                what,
                phrase,
                exc_info=True,
            )

    async def _end_call_gracefully(self) -> None:
        """Speak the goodbye (if enabled), flush it, then signal the pump to wind up.

        The loop-initiated graceful end (ADR-0057). The goodbye is spoken and fully
        flushed to ``send_audio`` (``_speak_text`` returns only once the stream is
        drained) BEFORE :attr:`_end_call` is set, so the closing line reaches the wire
        while the media path is still live — the adapter stops the engine only after
        :meth:`run` returns. Setting :attr:`_end_call` makes the pump break out of its
        inbound loop and emit the end-of-stream marker, so ``run`` returns CLEANLY (a
        normal end, not a ``/stop``). The goodbye is best-effort: a failure is logged
        and the call still ends cleanly (a missing goodbye must never strand the call).
        """
        if self._goodbye and self._goodbye_phrase:
            _log.info("no-input: speaking goodbye before end: %r", self._goodbye_phrase)
            await self._speak_phrase_best_effort(self._goodbye_phrase, what="goodbye")
        self._end_call.set()

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
            # This stream now owns playout. The lock serialises _play calls, so any
            # superseding-prior stream's _play has fully finished (its finally has
            # run) before we get here — so THIS stream has not emitted audio yet:
            # disarm the echo gate until our own first frame (ADR-0023, codex C').
            # Without this, a superseded prior stream's stale `_tts_audio_active`
            # could leave the gate armed through THIS stream's synthesis latency,
            # withholding a real short caller turn in that pre-playout gap.
            self._tts_audio_active = False
            try:
                async with contextlib.aclosing(stream):
                    async for frame in stream:
                        await self._transport.send_audio(frame)
                        # Echo-gate arming (ADR-0023): real outbound audio is now on
                        # the wire, so any inbound audio from here can be its echo.
                        # Set BEFORE on_first_frame so the pump sees it as soon as
                        # the frame is sent. The comfort filler (ADR-0030) reads this
                        # at its delay boundary to detect dead air (don't fire while
                        # any agent audio — reply, greeting, or tone — is playing).
                        self._tts_audio_active = True
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
                # speak() may have already pointed it at a newer stream). Disarm the
                # echo gate when our audio stops — but only if a superseding speak()
                # has not already started a NEW stream's audio (then the flag
                # belongs to that newer stream and must stay set).
                if self._active_tts_stream is stream:
                    self._active_tts_stream = None
                    self._tts_audio_active = False
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

        # Greeting at the negotiated wire rate too (ADR-0022), same as speak().
        stream = self._tts.synthesize(
            _single_chunk(),
            self._voice,
            sample_rate=self._transport.inbound_sample_rate,
        )
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
        """Cancel the in-flight TtsStream AND flush queued audio for a clean stop.

        Two steps, because cancelling the stream alone is not enough to make the
        agent go quiet (ADR-0028):

        1. ``cancel()`` the active ``TtsStream`` so ``_play()``'s loop stops PULLING
           new frames from the synthesiser.
        2. ``transport.flush_outbound`` to DROP the audio already handed to the
           engine — the re-framing carry buffer and any in-flight frame, which the
           engine otherwise deadline-paces out over real time (a single large TTS
           chunk is hundreds of ms on the wire). The flush emits a short linear
           fade-out (``barge_in_fade_ms``) on the final frames so the cut is
           click-free, then goes silent within ~1 packet. Without step 2 the caller
           kept hearing the agent for the duration of the already-queued audio after
           they interrupted — the abruptness/delay the operator reported.

        Idempotent and side-effect-free when no stream is active: with nothing to
        cut off there is no queued agent audio to flush, so the engine is left
        untouched (a flush while the agent is silent would be a needless no-op send).

        Also cancels any pending dead-air comfort filler (ADR-0030): a barge-in
        during the turn gap means the caller is taking the floor, so a filler that
        has not yet fired must not fire. A filler already PLAYING is the active
        stream, so it is cancelled + flushed by the steps below like any agent audio.

        Also marks caller activity for the no-input watchdog (ADR-0057): a barge-in is
        the caller speaking, so it resets the silence window — a pending reprompt stands
        down and the reprompt count clears on the watchdog's next wake. This runs even
        when no TTS stream is active (the flush below is the only stream-gated step),
        because a caller onset during silence is still proof of life.
        """
        # The caller is interrupting / speaking: reset the no-input silence window so a
        # pending reprompt stands down and the reprompt cycle restarts (ADR-0057).
        self._caller_active_in_window = True
        # The caller is interrupting: a pending (not-yet-fired) filler must not fire.
        self._cancel_comfort_filler()
        stream = self._active_tts_stream
        if stream is not None:
            await stream.cancel()
            # Flush the already-queued near-end audio so the agent goes quiet within
            # ~1 packet (the cancel only stops pulling NEW frames). Only when a
            # stream is/was active — nothing queued to flush otherwise.
            await self._transport.flush_outbound(fade_ms=self._barge_in_fade_ms)

    async def _screen_and_deliver(self, text: str) -> None:
        """Screen one finalised turn through the guard; deliver if not refused.

        On a non-REFUSE verdict the dead-air comfort filler is armed (ADR-0030) and
        then the turn is handed to the agent. A REFUSE never reaches the agent, so no
        gap and no filler.

        A delivered turn is also caller activity for the no-input watchdog (ADR-0057):
        the caller finished a turn, so the silence window resets (a pending reprompt
        stands down and the count clears on the watchdog's next wake). Marked even on a
        REFUSE — the caller DID speak; the guard merely declined to forward it — so a
        prompt-injection attempt does not look like an absent caller.
        """
        # The caller finished a turn (heard, even if the guard refuses to forward it):
        # reset the no-input silence window (ADR-0057).
        self._caller_active_in_window = True
        result = await self._guard.screen(text, call_id=self._call_id)
        self._guard_state.record(result)
        if result.verdict is not GuardVerdict.REFUSE:
            # Arm the filler BEFORE handing off the turn, so its delay measures the
            # dead-air gap from the caller-finish moment (this point) — robust even if
            # ``_deliver_turn`` were to block on agent work, rather than relying on it
            # being a non-blocking enqueue. The filler task runs concurrently with the
            # hand-off and the agent turn (no-op when disabled).
            self._schedule_comfort_filler()
            await self._deliver_turn(text)
