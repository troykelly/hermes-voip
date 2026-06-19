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
  speech/silence **segment durations** the VAD stream produces (plus an optional STT
  greeting-text length): a short greeting then a pause awaiting a response ≈ a human;
  long continuous speech ≈ a machine. Once a machine is classified, the record cue —
  a ~1000 Hz **beep**, or the greeting-ended (long-speech→silence) signal when no
  beep comes — emits :class:`ReadyToLeaveMessage`. **AMD is heuristic** (~85-95 %
  even commercially); the thresholds below are tunable :data:`Final` constants and
  this module makes no claim of exactness. The hang-up-vs-speak decision belongs to
  the Hermes agent (its call-control tools, ADR-0009/0010/0031) — this detector only
  emits the cue.

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
    """Tracks the running length (in frames) of one continuously-present tone.

    A frame either has the tone present or not; consecutive present-frames extend
    the run, an absent frame resets it. The frame duration (seconds) lets a caller
    test the run's length in time, rate-independently.
    """

    def __init__(self) -> None:
        self._frames = 0

    @property
    def frames(self) -> int:
        """Consecutive present-frames in the current run."""
        return self._frames

    def update(self, present: bool) -> int:
        """Advance with this frame's presence; return the new run length (frames)."""
        self._frames = self._frames + 1 if present else 0
        return self._frames

    def reset(self) -> None:
        """Clear the run (used on detector reset)."""
        self._frames = 0


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

#: An STT greeting transcript at least this long (characters) corroborates a machine
#: when supplied. Optional — it raises confidence but never overrides the durations.
_AMD_MACHINE_TEXT_CHARS: Final[int] = 80


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

    Fed one :class:`_Segment` at a time via :meth:`on_segment`. The verdict fires at
    the first silence that follows the opening speech:

    * the opening speech run (accumulated across in-greeting pauses shorter than the
      response gap) exceeded the machine threshold → :class:`AnsweringMachine`;
    * the opening speech was short and the trailing silence reached the response gap
      → :class:`LikelyHuman`.

    After a machine verdict, the first detected beep or a post-greeting silence that
    reaches the response gap yields the record cue (driven by the owning facade).
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

    def on_segment(
        self, segment: _Segment, *, greeting_text_len: int = 0
    ) -> AnsweringMachine | LikelyHuman | None:
        """Feed one VAD segment; return a verdict on the segment that decides it.

        Args:
            segment: The speech/silence segment that just completed (or, for the
                final open silence, the silence accumulated so far).
            greeting_text_len: Optional length of the STT greeting transcript so
                far (characters); corroborates a machine, never overrides duration.

        Returns:
            An :class:`AnsweringMachine` or :class:`LikelyHuman` on the deciding
            segment, else ``None``.
        """
        if self._state is _AmdState.DECIDED:
            return None
        if segment.is_speech:
            return self._on_speech(segment)
        return self._on_silence(segment, greeting_text_len)

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

    def _on_silence(
        self, segment: _Segment, greeting_text_len: int
    ) -> AnsweringMachine | LikelyHuman | None:
        """A silence run after speech: a short greeting + a real pause is a human."""
        if self._state is not _AmdState.GREETING:
            # Leading silence before any speech — nothing to classify yet.
            return None
        if segment.duration_s < self._response_gap_s:
            # An in-greeting pause, not a turn boundary; keep accumulating speech.
            return None
        end_s = segment.start_s + segment.duration_s
        if (
            self._greeting_speech_s > self._human_speech_s
            or greeting_text_len >= _AMD_MACHINE_TEXT_CHARS
        ):
            # Spoke for a while (between the human and machine speech thresholds) and
            # then went quiet, or a long transcript — treat as a machine greeting.
            self._state = _AmdState.DECIDED
            self._decided_machine = True
            corroborated = (
                " (corroborated by transcript length)"
                if greeting_text_len >= _AMD_MACHINE_TEXT_CHARS
                else ""
            )
            return AnsweringMachine(
                elapsed_s=end_s,
                beep_at_s=None,
                why=(
                    f"greeting {self._greeting_speech_s:.1f}s then "
                    f"{segment.duration_s:.1f}s silence{corroborated}"
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
        self._frames_seen = 0
        # AMD + record-cue state (used only when outbound).
        self._amd = _AnsweringMachineDetector()
        self._ready_emitted = False
        # VAD segmentation: the window ordinal of the last edge and its kind.
        self._last_edge_index: int | None = None
        self._last_edge_was_onset = False

    def on_audio_frame(self, frame: PcmFrame) -> CallProgressEvent | None:
        """Process one decoded PCM16-LE mono frame; return an event or ``None``.

        Runs the CNG/CED fax-tone detectors (both directions) and, once a machine
        has been classified, the record-beep detector. Frame timing comes from the
        running frame count times the frame's sample duration, so ``elapsed_s`` is
        exact and independent of any wall clock.

        Args:
            frame: PCM16-LE mono audio at this detector's ``sample_rate``.

        Returns:
            A :class:`FaxCng`, :class:`FaxCed`, or :class:`ReadyToLeaveMessage`
            (beep path) on the frame that completes a detection, else ``None``.

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
        self._frames_seen += 1
        if n == 0:
            return None
        frame_s = n / self._rate
        elapsed_s = self._frames_seen * frame_s

        samples = [float(v) for v in struct.unpack(f"<{n}h", frame.samples)]
        total_energy = sum(s * s for s in samples)

        cng_present = _tone_present(samples, total_energy, _CNG_HZ, self._rate)
        ced_present = _tone_present(samples, total_energy, _CED_HZ, self._rate)

        cng_frames = self._cng_run.update(cng_present)
        ced_frames = self._ced_run.update(ced_present)

        if not self._cng_emitted and cng_frames * frame_s >= _CNG_MIN_ON_S:
            self._cng_emitted = True
            return FaxCng(elapsed_s=elapsed_s)
        if not self._ced_emitted and ced_frames * frame_s >= _CED_MIN_ON_S:
            self._ced_emitted = True
            return FaxCed(elapsed_s=elapsed_s)

        # The record beep only matters once a machine greeting has been classified,
        # and only on an outbound call.
        if self._outbound and self._amd.decided_machine and not self._ready_emitted:
            beep_present = _tone_present(samples, total_energy, _BEEP_HZ, self._rate)
            beep_frames = self._beep_run.update(beep_present)
            if beep_frames * frame_s >= _BEEP_MIN_ON_S:
                self._ready_emitted = True
                return ReadyToLeaveMessage(elapsed_s=elapsed_s, beep_at_s=elapsed_s)
        return None

    def on_vad_event(self, event: VadEvent) -> CallProgressEvent | None:
        """Process one VAD onset/offset edge; return an event or ``None``.

        Segments the edge stream into speech/silence runs (ONSET→OFFSET = speech,
        OFFSET→next ONSET = silence), converts window ordinals to seconds, and feeds
        the answering-machine state machine. On an inbound call (``outbound`` False)
        this is inert. Once a machine has been classified, an ONSET after a
        sufficient post-greeting silence yields the record cue via the silence path
        (no beep heard).

        Args:
            event: A VAD speech onset/offset edge (window-ordinal timestamped).

        Returns:
            An :class:`AnsweringMachine`, :class:`LikelyHuman`, or
            :class:`ReadyToLeaveMessage` (silence path) on the deciding edge, else
            ``None``.
        """
        if not self._outbound:
            return None
        segment = self._close_segment(event)
        if segment is None:
            return None
        verdict = self._amd.on_segment(segment)
        if verdict is not None:
            return verdict
        # No new verdict — but if a machine was already decided and this completed
        # SILENCE segment ran long enough, the greeting has ended with no beep:
        # signal the record cue via the silence path.
        if (
            not segment.is_speech
            and self._amd.decided_machine
            and not self._ready_emitted
            and segment.duration_s >= _AMD_RESPONSE_GAP_S
        ):
            self._ready_emitted = True
            return ReadyToLeaveMessage(
                elapsed_s=segment.start_s + segment.duration_s, beep_at_s=None
            )
        return None

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
        self._frames_seen = 0
        self._amd.reset()
        self._ready_emitted = False
        self._last_edge_index = None
        self._last_edge_was_onset = False
