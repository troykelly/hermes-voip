"""Sans-IO call-progress detection — fax tones, AMD, and the record cue (ADR-0064).

The conversational pipeline (ADR-0003/0005/0008) assumes a human caller. Two
telephony events break that and are handled here, with no sockets and no Hermes
runtime import — the detector receives decoded :class:`PcmFrame`s plus the VAD
edge stream and **returns typed events**:

* **Fax tones (ITU-T T.30), both directions.** A *calling* fax emits **CNG**
  (1100 Hz, cadence 0.5 s on / 3 s off); an *answering* fax/modem emits **CED**
  (2100 Hz, ~2.6-4 s continuous). Each is a single pure tone detected by the same
  Goertzel power + false-positive-rejection discipline as the in-band DTMF detector
  (:mod:`hermes_voip.dtmf`): a tone counts only when it holds most of a frame's
  energy AND dominates its neighbour bins, sustained over consecutive frames. Voiced
  speech spreads energy across a harmonic series and never sustains one pure bin, so
  it is rejected.
* **Answering machines (outbound only).** A sans-IO state machine fed the
  speech/silence **segment durations** the VAD stream produces: a short greeting then
  a pause awaiting a response ≈ a human; long continuous speech ≈ a machine. Once a
  machine is classified, the record cue — a ~1000 Hz **beep**, or the greeting-ended
  (silence held for the response gap) signal when no beep comes — emits
  :class:`ReadyToLeaveMessage`. The no-beep fallback fires from
  :meth:`CallProgressDetector.on_audio_frame` on the audio/sample clock, because a
  machine that has stopped talking to record sends no further VAD edge to wait on.
  **AMD is heuristic** (~85-95 % even commercially); the thresholds below are tunable
  :data:`Final` constants and this module makes no claim of exactness. The
  hang-up-vs-speak decision belongs to the Hermes agent (its call-control tools,
  ADR-0009/0010/0031) — this detector only emits the cue.

On an *inbound* call the agent **is** the answerer, so AMD is gated behind the
``outbound`` flag; fax detection runs in both directions. The detector is
**sample-rate parameterised** (the inbound pipeline runs at 16 kHz, ADR-0017; the
G.711 wire at 8 kHz) and O(n) per frame, matching the per-frame CPU budget (rule 22).

The live media-engine/adapter wiring is a same-push follow-on (ADR-0064): feed
:meth:`CallProgressDetector.on_audio_frame` the decoded inbound frame and
:meth:`on_vad_event` each VAD edge; surface the events to the agent.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final, Literal

from hermes_voip.dtmf import _goertzel_power
from hermes_voip.media.vad import SILERO_WINDOW_SAMPLES, SpeechEdge, VadEvent
from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame

__all__ = [
    "AnsweringMachine",
    "CallProgressDetector",
    "CallProgressEvent",
    "FaxCed",
    "FaxCng",
    "LikelyHuman",
    "ReadyToLeaveMessage",
]

_MS_PER_SECOND: Final[int] = 1000


# ---------------------------------------------------------------------------
# Event model — a tagged, frozen discriminated union (rule 17: exhaustive match,
# no catch-all default). ``kind`` is the discriminant; ``elapsed_s`` is seconds
# since detector start on the frame clock.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FaxCng:
    """A calling fax was heard (CNG, 1100 Hz; inbound). T.30 §5.2.2."""

    elapsed_s: float
    kind: Literal["fax_cng"] = "fax_cng"


@dataclass(frozen=True, slots=True)
class FaxCed:
    """An answering fax/modem was heard (CED, 2100 Hz; outbound). T.30 §5.2.1."""

    elapsed_s: float
    kind: Literal["fax_ced"] = "fax_ced"


@dataclass(frozen=True, slots=True)
class AnsweringMachine:
    """An answering machine / voicemail greeting was classified (outbound).

    Attributes:
        elapsed_s: Seconds since detector start when the verdict fired.
        beep_at_s: When a record beep was detected, else ``None`` (no beep yet).
        why: A short human-readable rationale, for logging — not control flow.
    """

    elapsed_s: float
    beep_at_s: float | None
    why: str
    kind: Literal["answering_machine"] = "answering_machine"


@dataclass(frozen=True, slots=True)
class LikelyHuman:
    """The far end is likely a live human (a short greeting then a pause).

    Attributes:
        elapsed_s: Seconds since detector start when the verdict fired.
        why: A short human-readable rationale, for logging — not control flow.
    """

    elapsed_s: float
    why: str
    kind: Literal["likely_human"] = "likely_human"


@dataclass(frozen=True, slots=True)
class ReadyToLeaveMessage:
    """The record cue: the agent may now speak its voicemail message.

    Emitted after an :class:`AnsweringMachine` classification, on the first of a
    detected beep or a sufficient post-greeting silence.

    Attributes:
        elapsed_s: Seconds since detector start when the cue fired.
        beep_at_s: When triggered by a detected beep, else ``None`` (silence path).
    """

    elapsed_s: float
    beep_at_s: float | None
    kind: Literal["ready_to_leave_message"] = "ready_to_leave_message"


#: The complete set of events the detector emits. Match exhaustively on ``.kind``.
type CallProgressEvent = (
    FaxCng | FaxCed | AnsweringMachine | LikelyHuman | ReadyToLeaveMessage
)


# ---------------------------------------------------------------------------
# Single-tone detection (Goertzel) — fax CNG/CED + the AMD record beep.
#
# Mirrors hermes_voip.dtmf.InbandDtmfDetector's rejection discipline for ONE tone:
# the target frequency must hold most of the frame's energy AND dominate a set of
# guard bins, sustained over consecutive frames. Voiced speech spreads its energy
# across a harmonic series, so one pure bin never clears the fraction floor.
# ---------------------------------------------------------------------------

#: A frame must carry this much energy (sum of squared int16 samples) before any
#: tone test runs — rejects silence / comfort noise and avoids a divide-by-zero.
#: Same units and rationale as ``hermes_voip.dtmf._INBAND_MIN_FRAME_ENERGY``.
_MIN_FRAME_ENERGY: Final[float] = 5.0e5

#: The target tone must hold at least this fraction of the frame's energy. Goertzel
#: power is N-normalised (x 2/N) into the frame's energy units, so a pure tone scores
#: ~1.0 here. Voiced speech puts its energy in a fundamental + harmonics, so any one
#: bin (even a harmonic that lands on the target) scores far below this. 0.55 is the
#: primary speech rejecter — high enough that a single voiced harmonic cannot pass,
#: low enough to tolerate the band-limiting and mild noise on a real G.711 tone.
_TONE_MIN_FRACTION: Final[float] = 0.55

#: The target tone's power must exceed each guard bin's by this factor. A pure tone
#: leaves the off-target guard bins near zero; this rejects broadband energy that
#: clears the fraction floor without a genuine single-frequency peak.
_TONE_GUARD_DOMINANCE: Final[float] = 4.0

#: Guard-bin offsets (Hz) probed either side of a target tone. Far enough from the
#: target that a clean tone barely excites them, near enough to catch a wideband
#: source masquerading as a tone. (200 Hz separation resolves cleanly even in a
#: 20 ms / 160-sample frame at 8 kHz, whose Goertzel bin width is ~50 Hz.)
_GUARD_OFFSETS_HZ: Final[tuple[int, ...]] = (-300, -200, 200, 300)


def _tone_present(
    samples: list[float], total_energy: float, freq: int, rate: int
) -> bool:
    """Whether ``freq`` Hz dominates this frame as a clean single tone.

    Args:
        samples: The frame's PCM samples as floats.
        total_energy: ``sum(s*s for s in samples)`` (precomputed once per frame).
        freq: The target tone frequency in Hz.
        rate: The sample rate in Hz.

    Returns:
        ``True`` only when the frame carries real energy, the target bin holds at
        least :data:`_TONE_MIN_FRACTION` of it, and the target dominates every
        guard bin by :data:`_TONE_GUARD_DOMINANCE`.
    """
    if total_energy < _MIN_FRAME_ENERGY:
        return False
    n = len(samples)
    norm = 2.0 / n
    tone_p = _goertzel_power(samples, freq, rate) * norm
    if tone_p < _TONE_MIN_FRACTION * total_energy:
        return False
    for offset in _GUARD_OFFSETS_HZ:
        guard_freq = freq + offset
        # A guard bin below ~0 Hz or at/above Nyquist is not a meaningful probe;
        # skip it rather than alias. (Only the beep band gets near 0 with -300.)
        if guard_freq <= 0 or guard_freq * 2 >= rate:
            continue
        guard_p = _goertzel_power(samples, guard_freq, rate) * norm
        if tone_p < guard_p * _TONE_GUARD_DOMINANCE:
            return False
    return True


class _ToneRun:
    """Tracks the running duration (in seconds) of one continuously-present tone.

    A frame either has the tone present or not; a present frame extends the run by
    that frame's own duration, an absent frame resets it to zero. Accumulating
    seconds (not a frame count) keeps the run length exact under variable frame
    sizes — the same correctness the sample clock gives ``elapsed_s``.
    """

    def __init__(self) -> None:
        self._seconds = 0.0

    def update(self, *, present: bool, frame_s: float) -> float:
        """Advance by one frame; return the new run duration in seconds.

        Args:
            present: Whether the tone is present in this frame.
            frame_s: This frame's duration in seconds.
        """
        self._seconds = self._seconds + frame_s if present else 0.0
        return self._seconds

    def reset(self) -> None:
        """Clear the run (used on detector reset)."""
        self._seconds = 0.0


# ---------------------------------------------------------------------------
# Fax-tone thresholds (durations). Public ITU-T T.30 frequencies.
# ---------------------------------------------------------------------------

#: CNG — the calling fax's tone (T.30): 1100 Hz, cadence 0.5 s on / 3 s off.
_CNG_HZ: Final[int] = 1100
#: CED / answer tone — the answering fax/modem's tone (T.30): 2100 Hz, ~2.6-4 s.
_CED_HZ: Final[int] = 2100
#: AMD record beep band centre. Machines conventionally beep at ~1000 Hz.
_BEEP_HZ: Final[int] = 1000

#: Minimum continuous on-duration to call a 1100 Hz burst a CNG. The T.30 on-period
#: is 0.5 s; a slightly shorter floor tolerates the burst's onset/offset ramp while
#: still demanding far more than any incidental harmonic. One burst is diagnostic —
#: requiring the full 3.5 s cadence cycle only adds latency.
_CNG_MIN_ON_S: Final[float] = 0.4

#: Minimum continuous duration to call a 2100 Hz tone a CED/answer tone. T.30 says
#: 2.6-4 s; 1.0 s is long enough that voiced speech cannot masquerade as a sustained
#: single tone, short enough to abort before the modem handshake completes.
_CED_MIN_ON_S: Final[float] = 1.0

#: Minimum continuous duration of a ~1000 Hz tone to call it a record beep. Machine
#: beeps run a few hundred ms; 0.2 s rejects a transient click.
_BEEP_MIN_ON_S: Final[float] = 0.2


# ---------------------------------------------------------------------------
# Answering-machine detection — a sans-IO state machine over VAD segments.
# ---------------------------------------------------------------------------

#: Opening speech longer than this (seconds) is a machine greeting, not a human's
#: "Hello?". Industry AMD uses ~2-4 s; 3.5 s favours not cutting off a chatty human.
_AMD_MACHINE_SPEECH_S: Final[float] = 3.5

#: The opening speech is "short" (human-greeting-shaped) at or below this many
#: seconds. A real "Hello?" / "Hi, this is …" lands well under 2 s.
_AMD_HUMAN_SPEECH_S: Final[float] = 2.0

#: Trailing silence (seconds) after the opening speech that counts as the far end
#: yielding the floor — a human awaiting a response, or a machine greeting that has
#: ended. Below this, a gap is treated as an in-greeting pause, not a turn boundary.
_AMD_RESPONSE_GAP_S: Final[float] = 1.0


class _AmdState(Enum):
    """The answering-machine state machine's phase."""

    #: Awaiting the first speech onset.
    INITIAL = auto()
    #: Accumulating the opening greeting's speech (across short in-greeting pauses).
    GREETING = auto()
    #: A verdict has fired; further segments only drive the record cue.
    DECIDED = auto()


@dataclass(frozen=True, slots=True)
class _Segment:
    """One speech-or-silence segment from the VAD stream.

    Attributes:
        is_speech: ``True`` for a speech run, ``False`` for a silence run.
        duration_s: The segment's duration in seconds.
        start_s: Seconds since detector start when the segment began.
    """

    is_speech: bool
    duration_s: float
    start_s: float


class _AnsweringMachineDetector:
    """Classifies human vs machine from VAD speech/silence segments (sans-IO).

    Fed one :class:`_Segment` at a time via :meth:`on_segment`. A long enough opening
    speech run fires :class:`AnsweringMachine` as soon as it crosses the machine
    threshold; otherwise the verdict fires at the first silence that follows the
    opening speech:

    * the opening speech run (accumulated across in-greeting pauses shorter than the
      response gap) exceeded the machine threshold → :class:`AnsweringMachine`;
    * the opening speech was short and the trailing silence reached the response gap
      → :class:`LikelyHuman`.

    This classifier only decides human vs machine. The record cue
    (:class:`ReadyToLeaveMessage`) is the owning :class:`CallProgressDetector`'s job —
    via a detected beep or the audio-clock no-beep fallback — not this state machine's.
    """

    def __init__(
        self,
        *,
        machine_speech_s: float = _AMD_MACHINE_SPEECH_S,
        human_speech_s: float = _AMD_HUMAN_SPEECH_S,
        response_gap_s: float = _AMD_RESPONSE_GAP_S,
    ) -> None:
        self._machine_speech_s = machine_speech_s
        self._human_speech_s = human_speech_s
        self._response_gap_s = response_gap_s
        self._state = _AmdState.INITIAL
        self._greeting_speech_s = 0.0
        self._decided_machine = False

    @property
    def decided_machine(self) -> bool:
        """Whether the classifier has fired an :class:`AnsweringMachine` verdict."""
        return self._decided_machine

    def on_segment(self, segment: _Segment) -> AnsweringMachine | LikelyHuman | None:
        """Feed one VAD segment; return a verdict on the segment that decides it.

        Args:
            segment: The speech/silence segment that just completed.

        Returns:
            An :class:`AnsweringMachine` or :class:`LikelyHuman` on the deciding
            segment, else ``None``.
        """
        if self._state is _AmdState.DECIDED:
            return None
        if segment.is_speech:
            return self._on_speech(segment)
        return self._on_silence(segment)

    def _on_speech(self, segment: _Segment) -> AnsweringMachine | None:
        """A speech run: accumulate the greeting; a long-enough run is a machine."""
        self._state = _AmdState.GREETING
        self._greeting_speech_s += segment.duration_s
        if self._greeting_speech_s >= self._machine_speech_s:
            self._state = _AmdState.DECIDED
            self._decided_machine = True
            end_s = segment.start_s + segment.duration_s
            return AnsweringMachine(
                elapsed_s=end_s,
                beep_at_s=None,
                why=(
                    f"continuous greeting {self._greeting_speech_s:.1f}s "
                    f">= {self._machine_speech_s:.1f}s machine threshold"
                ),
            )
        return None

    def _on_silence(self, segment: _Segment) -> AnsweringMachine | LikelyHuman | None:
        """A silence run after speech: a short greeting + a real pause is a human."""
        if self._state is not _AmdState.GREETING:
            # Leading silence before any speech — nothing to classify yet.
            return None
        if segment.duration_s < self._response_gap_s:
            # An in-greeting pause, not a turn boundary; keep accumulating speech.
            return None
        end_s = segment.start_s + segment.duration_s
        if self._greeting_speech_s > self._human_speech_s:
            # Spoke for a while (between the human and machine speech thresholds) and
            # then went quiet — treat as a machine greeting.
            self._state = _AmdState.DECIDED
            self._decided_machine = True
            return AnsweringMachine(
                elapsed_s=end_s,
                beep_at_s=None,
                why=(
                    f"greeting {self._greeting_speech_s:.1f}s then "
                    f"{segment.duration_s:.1f}s silence"
                ),
            )
        self._state = _AmdState.DECIDED
        return LikelyHuman(
            elapsed_s=end_s,
            why=(
                f"short greeting {self._greeting_speech_s:.1f}s then "
                f"{segment.duration_s:.1f}s pause awaiting a response"
            ),
        )

    def reset(self) -> None:
        """Drop all per-call state for reuse on a new call."""
        self._state = _AmdState.INITIAL
        self._greeting_speech_s = 0.0
        self._decided_machine = False


# ---------------------------------------------------------------------------
# The facade the media engine / adapter drives.
# ---------------------------------------------------------------------------


class CallProgressDetector:
    """Detects fax tones, answering machines, and the record cue for one call.

    Sans-IO: feed it decoded audio frames (:meth:`on_audio_frame`) and VAD edges
    (:meth:`on_vad_event`); each returns a :class:`CallProgressEvent` or ``None``.
    Fax detection runs in both directions; AMD runs only when ``outbound`` (on an
    inbound call the agent is the answerer). :meth:`reset` clears all state for a
    new call. See ADR-0064.
    """

    def __init__(self, *, sample_rate: int, outbound: bool) -> None:
        """Create a detector.

        Args:
            sample_rate: The PCM sample rate (Hz) of frames fed to
                :meth:`on_audio_frame`. Must be positive (8000 on the G.711 wire,
                16000 on the resampled conversational path).
            outbound: ``True`` for an agent-placed call (AMD active), ``False`` for
                an inbound call (AMD inactive — the agent is the answerer).

        Raises:
            ValueError: If ``sample_rate`` is not positive.
        """
        if sample_rate <= 0:
            msg = f"sample_rate must be positive, got {sample_rate}"
            raise ValueError(msg)
        self._rate = sample_rate
        self._outbound = outbound
        # VAD windows are 32 ms at either native silero rate; the window-ordinal
        # clock converts to seconds with this duration. The sample rate need not be
        # a silero rate for the audio path, so fall back to the 8 kHz window when
        # the rate is not in the silero table.
        window_samples = SILERO_WINDOW_SAMPLES.get(
            sample_rate, SILERO_WINDOW_SAMPLES[8000]
        )
        self._vad_window_s = window_samples / (
            sample_rate if sample_rate in SILERO_WINDOW_SAMPLES else 8000
        )
        self._cng_run = _ToneRun()
        self._ced_run = _ToneRun()
        self._beep_run = _ToneRun()
        self._cng_emitted = False
        self._ced_emitted = False
        # The audio/sample clock: total samples fed to on_audio_frame. elapsed_s is
        # derived from this (samples / rate), so variable/zero-length frames keep an
        # exact wall position. This is the single clock the tick-driven no-beep
        # fallback measures post-greeting silence against.
        self._samples_seen = 0
        # AMD + record-cue state (used only when outbound).
        self._amd = _AnsweringMachineDetector()
        self._ready_emitted = False
        # The audio-clock time (seconds) at which the current run of silence began
        # (set on each VAD OFFSET, cleared on each ONSET), or None while speech is in
        # progress / before any edge. The no-beep fallback fires once a machine has
        # been classified and this silence has lasted the response gap — driven by
        # on_audio_frame, NOT by a later ONSET (a recording machine sends no edge).
        self._silence_since_audio_s: float | None = None
        # VAD segmentation: the window ordinal of the last edge and its kind.
        self._last_edge_index: int | None = None
        self._last_edge_was_onset = False

    def on_audio_frame(self, frame: PcmFrame) -> CallProgressEvent | None:
        """Process one decoded PCM16-LE mono frame; return an event or ``None``.

        Runs the CNG/CED fax-tone detectors (both directions); once a machine has
        been classified, the record-beep detector AND the tick-driven no-beep
        fallback. ``elapsed_s`` is the running sample count divided by the rate, so
        it stays exact under variable or zero-length frames with no wall clock.

        Args:
            frame: PCM16-LE mono audio at this detector's ``sample_rate``.

        Returns:
            A :class:`FaxCng`, :class:`FaxCed`, or :class:`ReadyToLeaveMessage`
            (beep path, or the no-beep silence fallback) on the frame that completes
            a detection, else ``None``.

        Raises:
            ValueError: If the frame's rate differs from the detector's, or its
                payload is not a whole number of 16-bit samples.
        """
        if frame.sample_rate != self._rate:
            msg = f"frame rate {frame.sample_rate} != detector rate {self._rate}"
            raise ValueError(msg)
        if len(frame.samples) % PCM16_BYTES_PER_SAMPLE != 0:
            msg = (
                f"PCM16 frame must be whole 16-bit samples, "
                f"got {len(frame.samples)} bytes"
            )
            raise ValueError(msg)
        n = frame.sample_count
        self._samples_seen += n
        elapsed_s = self._samples_seen / self._rate
        if n == 0:
            # A zero-length frame advances no time and carries no tone; nothing to do
            # (the clock is unchanged, so the silence-fallback timer is unaffected).
            return None
        frame_s = n / self._rate

        samples = [float(v) for v in struct.unpack(f"<{n}h", frame.samples)]
        total_energy = sum(s * s for s in samples)

        cng_present = _tone_present(samples, total_energy, _CNG_HZ, self._rate)
        ced_present = _tone_present(samples, total_energy, _CED_HZ, self._rate)

        cng_s = self._cng_run.update(present=cng_present, frame_s=frame_s)
        ced_s = self._ced_run.update(present=ced_present, frame_s=frame_s)

        if not self._cng_emitted and cng_s >= _CNG_MIN_ON_S:
            self._cng_emitted = True
            return FaxCng(elapsed_s=elapsed_s)
        if not self._ced_emitted and ced_s >= _CED_MIN_ON_S:
            self._ced_emitted = True
            return FaxCed(elapsed_s=elapsed_s)

        # The record cue only matters once a machine greeting has been classified,
        # and only on an outbound call.
        if self._outbound and self._amd.decided_machine and not self._ready_emitted:
            beep_present = _tone_present(samples, total_energy, _BEEP_HZ, self._rate)
            beep_s = self._beep_run.update(present=beep_present, frame_s=frame_s)
            if beep_s >= _BEEP_MIN_ON_S:
                self._ready_emitted = True
                return ReadyToLeaveMessage(elapsed_s=elapsed_s, beep_at_s=elapsed_s)
            # No beep — the tick-driven fallback: if the greeting has now been silent
            # for the response gap (the machine stopped to record and sends no further
            # VAD edge), signal the record cue on the sample clock alone.
            if (
                self._silence_since_audio_s is not None
                and elapsed_s - self._silence_since_audio_s >= _AMD_RESPONSE_GAP_S
            ):
                self._ready_emitted = True
                return ReadyToLeaveMessage(elapsed_s=elapsed_s, beep_at_s=None)
        return None

    def on_vad_event(self, event: VadEvent) -> CallProgressEvent | None:
        """Process one VAD onset/offset edge; return an event or ``None``.

        Segments the edge stream into speech/silence runs (ONSET→OFFSET = speech,
        OFFSET→next ONSET = silence), converts window ordinals to seconds, and feeds
        the answering-machine state machine — which classifies human vs machine. On
        an inbound call (``outbound`` False) this is inert.

        The edge also marks the silence boundary the no-beep record-cue fallback
        times against: an OFFSET starts a silence run (stamped on the audio/sample
        clock), an ONSET ends it. The fallback itself fires from
        :meth:`on_audio_frame` (the audio clock keeps ticking when the recording
        machine sends no further VAD edge); this method never emits the cue.

        Args:
            event: A VAD speech onset/offset edge (window-ordinal timestamped).

        Returns:
            An :class:`AnsweringMachine` or :class:`LikelyHuman` on the deciding
            edge, else ``None``.
        """
        if not self._outbound:
            return None
        # Track the silence boundary on the AUDIO clock for the on_audio_frame
        # fallback: OFFSET => silence begins now; ONSET => speech, silence ends.
        if event.edge is SpeechEdge.OFFSET:
            self._silence_since_audio_s = self._samples_seen / self._rate
        else:
            self._silence_since_audio_s = None
        segment = self._close_segment(event)
        if segment is None:
            return None
        return self._amd.on_segment(segment)

    def _close_segment(self, event: VadEvent) -> _Segment | None:
        """Close the speech/silence run the previous edge opened, if any.

        An OFFSET closes the speech run from the previous ONSET; an ONSET closes the
        silence run from the previous OFFSET. The first edge of the call (or two
        same-direction edges, which the VAD never emits) opens a run but closes
        none, so this returns ``None``. Window ordinals convert to seconds via the
        silero window duration.
        """
        index = event.frame_index
        prev_index = self._last_edge_index
        prev_was_onset = self._last_edge_was_onset
        self._last_edge_index = index
        self._last_edge_was_onset = event.edge is SpeechEdge.ONSET
        if prev_index is None:
            return None
        closes_speech = event.edge is SpeechEdge.OFFSET
        if closes_speech != prev_was_onset:
            # OFFSET must follow an ONSET (and ONSET an OFFSET) to close a run.
            return None
        return _Segment(
            is_speech=closes_speech,
            duration_s=(index - prev_index) * self._vad_window_s,
            start_s=prev_index * self._vad_window_s,
        )

    def reset(self) -> None:
        """Drop all per-call state so the detector can be reused on a new call."""
        self._cng_run.reset()
        self._ced_run.reset()
        self._beep_run.reset()
        self._cng_emitted = False
        self._ced_emitted = False
        self._samples_seen = 0
        self._amd.reset()
        self._ready_emitted = False
        self._silence_since_audio_s = None
        self._last_edge_index = None
        self._last_edge_was_onset = False
