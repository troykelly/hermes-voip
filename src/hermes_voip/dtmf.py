"""DTMF over RTP (RFC 4733 telephone-events) plus the in-band backend (ADR-0010/0034).

The named-event RTP payload is four bytes: the event code, an end bit + volume,
and a 16-bit duration. This module is the payload codec, the digit<->event
mapping, the outbound payload sequence for a key-press (incremental duration
plus the redundant end packets RFC 4733 §2.5.1.4 requires), and a receiver that
yields each pressed digit exactly once despite that triplicated end. RTP framing
of these payloads is the transport's job (hermes_voip.rtp). DTMF is also the
spoof-resistant confirmation channel for irreversible tools (ADR-0009).

ADR-0036 adds the **in-band** backend — the last resort for a gateway that
negotiates no telephone-event: :class:`InbandDtmfDetector` (Goertzel tone
detection on decoded G.711 PCM, with the row/column + twist + second-harmonic +
duration tests that reject speech and noise) and :func:`inband_tone_pcm` (dual-tone
synthesis for sending). In-band is trusted ONLY on clean G.711 (ADR-0005), so its
detector runs at 8 kHz. :class:`DtmfSendMode` is the per-call send-backend selector
shared with the media engine and the :class:`~hermes_voip.call.CallSession`.
"""

from __future__ import annotations

import enum
import math
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

__all__ = [
    "DtmfEvent",
    "DtmfNoPress",
    "DtmfPress",
    "DtmfReceiver",
    "DtmfSendMode",
    "InbandDtmfDetector",
    "digit_to_event",
    "event_payloads",
    "event_to_digit",
    "inband_tone_pcm",
]

# Event codes 0..15 map to the keypad; 16 is flash. Index == event code.
_DIGITS: Final[str] = "0123456789*#ABCD"
_PAYLOAD_LEN: Final[int] = 4
_END_BIT: Final[int] = 0x80
_VOLUME_MASK: Final[int] = 0x3F
_MAX_EVENT: Final[int] = 0xFF
_MAX_VOLUME: Final[int] = 0x3F
_MAX_DURATION: Final[int] = 0xFFFF
_REDUNDANT_END_COUNT: Final[int] = 3
_RECEIVER_HISTORY: Final[int] = 32


@dataclass(frozen=True, slots=True)
class DtmfPress:
    """A completed DTMF key-press returned by :meth:`DtmfReceiver.feed` (ADR-0096).

    Attributes:
        digit: The keypad character that was pressed (``"0"``-``"9"``, ``"*"``,
            ``"#"``, ``"A"``-``"D"``).
    """

    digit: str


class DtmfNoPress(enum.Enum):
    """Non-press outcomes of :meth:`DtmfReceiver.feed`.

    ``STILL_PRESSING``/``DUPLICATE_END``/``NON_DIGIT_EVENT`` are the original
    result-type shape (bk366, ADR-0077); ``AWAITING_CORROBORATION`` and
    ``CONFLICTING_EVENT`` were added by the end-packet corroboration gate
    (ADR-0096). Replaces the bare ``None`` that previously conflated all
    non-press cases, letting callers branch on the exact reason without extra
    bookkeeping:

    * ``STILL_PRESSING``       - the end bit is not set; the tone is still in
      progress.
    * ``AWAITING_CORROBORATION`` - the end bit is set and this is the FIRST
      end packet ever seen for this RTP timestamp. DTMF is the ADR-0009
      spoof-resistant confirmation channel (ADR-0010), so a single packet is
      never trusted outright — that is exactly what a forged packet racing
      the genuine one looks like. The digit is trusted only once a SECOND
      end packet agreeing with the first is seen (see ``DtmfReceiver.feed``).
    * ``DUPLICATE_END``        - the end bit is set and this RTP timestamp
      already has an EMITTED digit recorded with the SAME event code (a
      further RFC 4733 redundant end or a reordered duplicate) — including
      one that arrives after an unrelated ``CONFLICTING_EVENT`` for a
      DIFFERENT code, since the digit was already safely corroborated by two
      agreeing packets before that conflict was ever seen.
    * ``CONFLICTING_EVENT``    - the end bit is set and this RTP timestamp
      has a DIFFERENT event code recorded for it than this packet carries.
      Before a digit has been emitted for the timestamp this is permanent:
      the timestamp is contested and neither code can be told apart from a
      forgery without transport-layer authentication (SRTP), so no press is
      ever emitted for it, no matter which code a later packet carries —
      including one that matches the code first recorded, since that first
      code might itself have been the forged one. After a digit HAS already
      been emitted (via two packets that agreed before any conflict was
      seen), a later disagreeing packet is flagged this way too, for
      visibility, but cannot un-emit the digit already returned.
    * ``NON_DIGIT_EVENT``      - a second, corroborating end packet agreed
      with the first, but the event code is not a keypad digit (e.g. flash =
      event 16).
    """

    STILL_PRESSING = "still_pressing"
    AWAITING_CORROBORATION = "awaiting_corroboration"
    DUPLICATE_END = "duplicate_end"
    CONFLICTING_EVENT = "conflicting_event"
    NON_DIGIT_EVENT = "non_digit_event"


class DtmfSendMode(enum.Enum):
    """The resolved outbound-DTMF backend for one call (ADR-0010/0034).

    * ``RFC4733`` — emit the named-event RTP train at the negotiated telephone-event
      payload type (the engine's :meth:`send_dtmf` default).
    * ``SIP_INFO`` — emit one in-dialog ``INFO`` per digit (the
      :class:`~hermes_voip.call.CallSession` owns this; the media engine never resolves
      to it).
    * ``INBAND`` — synthesise dual-tone PCM, sent on the audio TX path (G.711 only).
    * ``UNAVAILABLE`` — no send backend can run on this call (e.g. ``rfc4733`` forced
      but no telephone-event negotiated). Sending raises rather than silently dropping.
    """

    RFC4733 = "rfc4733"
    SIP_INFO = "sip_info"
    INBAND = "inband"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DtmfEvent:
    """One RFC 4733 telephone-event payload.

    Attributes:
        event: The event code (0-9, 10=``*``, 11=``#``, 12-15=A-D, 16=flash).
        end: The end bit — set on the final packets of a tone.
        volume: The tone power in -dBm0 (0-63).
        duration: The tone duration so far, in RTP timestamp units (0-65535).
    """

    event: int
    end: bool
    volume: int
    duration: int

    def __post_init__(self) -> None:
        """Validate each field fits its telephone-event width."""
        if not 0 <= self.event <= _MAX_EVENT:
            msg = f"event out of range 0..255: {self.event}"
            raise ValueError(msg)
        if not 0 <= self.volume <= _MAX_VOLUME:
            msg = f"volume out of range 0..63: {self.volume}"
            raise ValueError(msg)
        if not 0 <= self.duration <= _MAX_DURATION:
            msg = f"duration out of range 0..65535: {self.duration}"
            raise ValueError(msg)

    def encode(self) -> bytes:
        """Serialise to the 4-byte telephone-event payload."""
        byte1 = (_END_BIT if self.end else 0) | self.volume
        return bytes([self.event, byte1]) + self.duration.to_bytes(2, "big")

    @classmethod
    def decode(cls, data: bytes) -> DtmfEvent:
        """Parse a 4-byte telephone-event payload.

        Raises:
            ValueError: If ``data`` is not exactly four bytes.
        """
        if len(data) != _PAYLOAD_LEN:
            msg = f"DTMF telephone-event payload must be 4 bytes, got {len(data)}"
            raise ValueError(msg)
        # The reserved R bit (0x40, bit 6) in byte 1 is intentionally ignored.
        return cls(
            event=data[0],
            end=bool(data[1] & _END_BIT),
            volume=data[1] & _VOLUME_MASK,
            duration=int.from_bytes(data[2:4], "big"),
        )


def digit_to_event(digit: str) -> int:
    """Map a keypad character to its event code (case-insensitive).

    Raises:
        ValueError: If ``digit`` is not a single DTMF keypad character.
    """
    index = _DIGITS.find(digit.upper()) if len(digit) == 1 else -1
    if index < 0:
        msg = f"not a DTMF digit: {digit!r}"
        raise ValueError(msg)
    return index


def event_to_digit(event: int) -> str:
    """Map an event code (0-15) to its keypad character.

    Raises:
        ValueError: If ``event`` has no keypad digit (e.g. flash = 16).
    """
    if not 0 <= event < len(_DIGITS):
        msg = f"not a DTMF event with a digit: {event}"
        raise ValueError(msg)
    return _DIGITS[event]


def event_payloads(
    digit: str, *, total_duration: int, step: int, volume: int = 10
) -> Iterator[bytes]:
    """Yield the telephone-event payloads to send one key-press.

    Emits a packet at each ``step`` of growing duration up to ``total_duration``,
    then the redundant end packets RFC 4733 §2.5.1.4 requires.

    Args:
        digit: The keypad character to send.
        total_duration: The full tone duration in RTP timestamp units.
        step: The duration increment per update packet.
        volume: The tone power in -dBm0.

    Yields:
        4-byte telephone-event payloads, in send order.

    Raises:
        ValueError: If ``step`` is not positive or ``total_duration`` is outside
            ``1..65535`` (duration 0 is reserved for state events, not tones).
    """
    if step <= 0:
        msg = f"step must be positive, got {step}"
        raise ValueError(msg)
    if not 1 <= total_duration <= _MAX_DURATION:
        msg = f"total_duration out of range 1..65535: {total_duration}"
        raise ValueError(msg)
    event = digit_to_event(digit)
    duration = step
    while duration < total_duration:
        yield DtmfEvent(
            event=event, end=False, volume=volume, duration=duration
        ).encode()
        duration += step
    yield DtmfEvent(
        event=event, end=False, volume=volume, duration=total_duration
    ).encode()
    for _ in range(_REDUNDANT_END_COUNT):
        yield DtmfEvent(
            event=event, end=True, volume=volume, duration=total_duration
        ).encode()


@dataclass(slots=True)
class _PendingTimestamp:
    """Mutable per-timestamp state for :class:`DtmfReceiver`'s corroboration gate.

    Attributes:
        event: The event code first recorded for this timestamp (compared
            against every later packet at the same timestamp).
        emitted: Whether a :class:`DtmfPress` has already been returned for
            this timestamp (set on the second, corroborating packet).
        poisoned: Whether a disagreeing event code was seen for this
            timestamp BEFORE a digit was emitted. While ``emitted`` is still
            ``False``, ``poisoned=True`` is permanent: no further packet for
            this timestamp is ever emitted as a press, even one that matches
            ``event`` — a pre-emission disagreement proves this timestamp is
            contested and there is no way to tell which sender was genuine.
            Once ``emitted`` is ``True``, ``poisoned`` is never consulted
            again: the digit was already safely corroborated by two
            agreeing packets before any conflict was seen, so it cannot be
            un-emitted by a conflict arriving afterwards.
    """

    event: int
    emitted: bool = False
    poisoned: bool = False


class DtmfReceiver:
    """Collapses RFC 4733 events into one digit per key-press.

    A key-press ends with ``_REDUNDANT_END_COUNT`` (3) redundant end packets at
    one RTP timestamp, all carrying the same event code. The receiver never
    trusts a single, uncorroborated end packet: DTMF is the ADR-0009
    spoof-resistant confirmation channel (ADR-0010), and a lone packet is
    exactly what a forged packet racing the genuine one looks like. The FIRST
    end packet seen for a new timestamp returns ``AWAITING_CORROBORATION``
    (recorded, not yet trusted); only a SECOND end packet that AGREES with the
    first is emitted as :class:`DtmfPress` — collapsing the remaining
    redundant copies to ``DUPLICATE_END``. A disagreeing packet seen BEFORE a
    digit has been emitted permanently poisons that timestamp
    (``CONFLICTING_EVENT``): no press is ever emitted for it, not even one
    that matches an event code seen earlier, because the disagreement proves
    the timestamp is contested and there is no cryptographic way to tell
    which sender was genuine — the receiver fails safe (no digit) rather
    than fail open (the wrong digit accepted). A disagreement seen AFTER a
    digit has already been emitted cannot un-emit it (the digit was already
    safely corroborated by two agreeing packets first); it is still reported
    as ``CONFLICTING_EVENT`` for visibility, while further packets that agree
    with the emitted digit keep collapsing to ordinary ``DUPLICATE_END``. A
    later press carries a new RTP timestamp and is judged fresh. A non-digit
    event (e.g. flash) that corroborates is not surfaced as a digit
    (``NON_DIGIT_EVENT``).

    ``_window`` is an insertion-ordered ``dict[int, _PendingTimestamp]``
    mapping each remembered RTP timestamp (oldest-to-newest) to its
    corroboration state; eviction pops the oldest (first) key when the window
    is full. This replaces the previous ``_order: deque[int]`` + ``_seen:
    set[int]`` pair (bk354), the later ``dict[int, None]`` form that recorded
    a timestamp had been seen but not what digit it had decided, and the
    ``dict[int, int]`` form that recorded the event code but trusted it
    immediately on first sight (the digit-substitution gap fixed here: see
    ADR-0096 — a receiver that emits on the FIRST packet it ever sees for a
    timestamp can never detect a disagreement before that packet has already
    been acted on, no matter how a LATER disagreeing packet is classified).

    Residual scope: corroboration requires two end packets to AGREE, not two
    end packets from the SAME sender — two colluding forged copies are
    indistinguishable from a genuine corroborated pair without
    transport-layer authentication (SRTP). An attacker who wins the arrival
    race on BOTH of the first two end packets for a timestamp (not merely
    one) still has a forged digit accepted. This raises the bar from a
    single-packet race to a same-outcome two-packet race; it does not
    eliminate spoofing on an unauthenticated media path, and no receiver-side
    heuristic can without SRTP.
    """

    def __init__(self, history: int = _RECEIVER_HISTORY) -> None:
        """Create a receiver remembering the last ``history`` press timestamps.

        Args:
            history: The size of the bounded dedup window (must be >= 1).  With
                ``history=0`` the eviction guard never fires and the window
                would grow unbounded across a long call — a memory leak that
                silently breaks the dedup contract.

        Raises:
            ValueError: If ``history`` is less than 1.
        """
        if history < 1:
            msg = f"history must be >= 1, got {history}"
            raise ValueError(msg)
        self._history = history
        # Single insertion-ordered structure: keys are remembered timestamps
        # in insertion order, values are each timestamp's corroboration state;
        # eviction pops the oldest (first) key when the window is full.  O(1)
        # membership tests, O(1) oldest-key access (next(iter(d))), and O(1)
        # lookup to detect a same-timestamp conflict or complete corroboration.
        self._window: dict[int, _PendingTimestamp] = {}

    def feed(self, event: DtmfEvent, *, timestamp: int) -> DtmfPress | DtmfNoPress:
        """Process one decoded event at its RTP ``timestamp``.

        Returns:
            :class:`DtmfPress` carrying the pressed digit when a SECOND,
            corroborating end packet agrees with the first one seen for this
            timestamp.  One of the :class:`DtmfNoPress` variants otherwise:

            * ``STILL_PRESSING``          — the end bit is not set.
            * ``AWAITING_CORROBORATION``  — the end bit is set and this is
              the FIRST end packet seen for this timestamp. Not yet trusted;
              recorded so a second packet can be compared against it.
            * ``CONFLICTING_EVENT``       — the end bit is set and this
              timestamp already has a DIFFERENT event code recorded for it.
              Before a digit has been emitted for it, this is permanent —
              never treated as a press again, even by a packet that matches
              an event code seen earlier, since a forged or otherwise
              mismatching packet racing the genuine one must not substitute
              a different digit for it, and once contested pre-emission
              neither side can be told apart from a forgery. After a digit
              HAS been emitted, a later disagreeing packet is still flagged
              this way for visibility, but cannot un-emit it.
            * ``DUPLICATE_END``           — the end bit is set and this
              timestamp's digit was already emitted with the SAME event code
              (a further RFC 4733 redundant end or reordered duplicate),
              including one arriving after an unrelated post-emission
              conflict.
            * ``NON_DIGIT_EVENT``         — a corroborating end packet agreed
              with the first, but the event code is not a keypad digit (e.g.
              flash = 16).
        """
        if not event.end:
            return DtmfNoPress.STILL_PRESSING
        state = self._window.get(timestamp)
        if state is None:
            # Evict the oldest entry if the window is full.
            if len(self._window) >= self._history:
                oldest = next(iter(self._window))
                del self._window[oldest]
            self._window[timestamp] = _PendingTimestamp(event=event.event)
            return DtmfNoPress.AWAITING_CORROBORATION
        disagrees = state.event != event.event
        if disagrees:
            # A disagreement always poisons the timestamp. Pre-emission this
            # is permanent (no press is ever emitted for it again, no matter
            # which code a later packet carries). Post-emission it cannot
            # un-emit the digit already returned; it only flags the anomaly.
            state.poisoned = True
        if disagrees or (state.poisoned and not state.emitted):
            # Either this packet itself disagrees, or an EARLIER packet did
            # and no digit has been emitted yet — either way, a pre-emission
            # conflict proves the timestamp contested (there is no way to
            # tell which sender was genuine), so refuse to emit even for a
            # packet that happens to match the code first recorded.
            return DtmfNoPress.CONFLICTING_EVENT
        if state.emitted:
            # Agrees with the recorded code and a digit was already emitted:
            # an ordinary further redundant/reordered copy, harmless even if
            # an unrelated conflict was flagged in between — the emission
            # already happened safely, on two packets that agreed before any
            # conflict was seen.
            return DtmfNoPress.DUPLICATE_END
        # Second (corroborating) end packet agreeing with the first, with no
        # conflict seen before it: only now is the digit trusted.
        state.emitted = True
        if 0 <= state.event < len(_DIGITS):
            return DtmfPress(digit=_DIGITS[state.event])
        return DtmfNoPress.NON_DIGIT_EVENT


# ---------------------------------------------------------------------------
# In-band DTMF (ADR-0010 last resort / ADR-0036): Goertzel detect + tone gen
# ---------------------------------------------------------------------------

# The eight DTMF tones (Hz). A keypad symbol is one low (row) + one high (column).
_DTMF_LOW: Final[tuple[int, ...]] = (697, 770, 852, 941)
_DTMF_HIGH: Final[tuple[int, ...]] = (1209, 1336, 1477, 1633)
# Row x column -> keypad symbol (standard 4x4 incl. A-D on the 1633 Hz column).
_DTMF_KEYPAD: Final[tuple[tuple[str, ...], ...]] = (
    ("1", "2", "3", "A"),
    ("4", "5", "6", "B"),
    ("7", "8", "9", "C"),
    ("*", "0", "#", "D"),
)

_INBAND_MAX_AMPLITUDE: Final[int] = (
    0x3FFF  # per-tone amplitude (sum stays < int16 full-scale)
)

# Detection thresholds (tuned on 8 kHz clean G.711; ADR-0036 re-measures live).
# Goertzel power is N-normalised (x 2/N) so a pure full-amplitude tone scores ~the
# frame's energy and a DTMF pair scores ~half each — same units as the frame energy,
# so the fractions below are dimensionless and frame-length independent.
#
# A frame must carry real energy before any tone test runs (rejects silence/comfort
# noise without a divide-by-zero). Units: sum of squared int16 samples per frame.
_INBAND_MIN_FRAME_ENERGY: Final[float] = 5.0e5
# The winning low + high tone must EACH hold at least this fraction of the frame's
# energy (each genuine DTMF tone is ~0.5).
_INBAND_MIN_TONE_FRACTION: Final[float] = 0.33
# ...and their COMBINED energy must be at least this fraction of the frame's energy.
# This is the PRIMARY speech rejecter: a clean DTMF pair scores ~1.0 (all energy in
# two bins), whereas voiced speech spreads energy across its full harmonic series and
# tops out far below (measured ~0.04-0.17 on harmonic glides). Set HIGH (0.80) so a
# voiced source whose two strongest harmonics happen to land near a DTMF pair is still
# rejected by the energy it carries in its OTHER harmonics. (A signal that is spectrally
# ONLY two pure DTMF tones is, by definition, indistinguishable from a real keypress —
# the fundamental limit that makes in-band the LAST resort, ADR-0010/0034.)
_INBAND_MIN_COMBINED_FRACTION: Final[float] = 0.80
# Forward/reverse twist: the louder tone may exceed the quieter by at most this ratio
# (~8 dB). A wildly mismatched "pair" is not a keypress.
_INBAND_MAX_TWIST: Final[float] = 6.3
# A detected tone's energy must dominate the others IN ITS OWN group by this factor
# (rejects a single tone that merely happens to clear the fraction floor — its
# group runner-up would be comparable for broadband noise).
_INBAND_GROUP_DOMINANCE: Final[float] = 4.0
# Harmonic-corroboration rejecter: a voiced source that lands two harmonics on a DTMF
# pair ALSO carries energy at the OTHER harmonics it implies — most tellingly the second
# harmonic of each detected tone (2x low, 2x high) and the difference frequency (the
# would-be fundamental). A real DTMF generator emits ONLY the two tones, so those bins
# are ~empty. Reject when their combined energy rivals the weaker detected tone by this
# fraction. This catches the harmonic-speech case the bare second-harmonic test missed.
_INBAND_MAX_HARMONIC_CORROBORATION: Final[float] = 0.25
# Debounce: a digit must persist this many consecutive validated frames before it
# emits (one 20 ms frame is enough to flag a glitch; ~2 frames = a real press).
_INBAND_MIN_PRESS_FRAMES: Final[int] = 2
# Release hysteresis: how many CONSECUTIVE gap (no-validated-tone) frames count as a
# real inter-digit release, after which a held digit is allowed to emit again. A
# single dropped or momentarily-silent frame mid-press (packet loss, an AEC transient,
# a frame that dips below the energy floor) must NOT re-arm the detector — that would
# let the same continuing tone emit a spurious duplicate digit, which is
# security-sensitive (a digit can resolve an ADR-0009 confirmation). At the 20 ms frame
# cadence 3 frames = 60 ms: it tolerates up to two consecutive dropped frames yet stays
# well under any genuine inter-digit pause a caller produces (>= ~100 ms in practice, vs
# the 40 ms ITU-T Q.24 theoretical floor), so distinct back-to-back presses of the same
# digit still register as two.
_INBAND_GAP_RELEASE_FRAMES: Final[int] = 3


def inband_tone_pcm(
    digit: str,
    *,
    sample_rate: int,
    duration_ms: int,
    amplitude: int = _INBAND_MAX_AMPLITUDE,
) -> bytes:
    """Synthesise one key-press as in-band dual-tone PCM16-LE mono (ADR-0036).

    The signal is the sum of the digit's row and column sine tones, each at
    ``amplitude`` (so the peak sum stays below int16 full-scale). Sent on the audio
    TX path for a G.711 call that negotiated no telephone-event.

    Args:
        digit: The keypad character (``0-9``, ``*``, ``#``, ``A``-``D``; any case).
        sample_rate: PCM sample rate in Hz (8000 for G.711).
        duration_ms: The tone duration in milliseconds (positive).
        amplitude: Per-tone peak amplitude (0..16383).

    Returns:
        ``duration_ms`` of PCM16-LE mono at ``sample_rate``.

    Raises:
        ValueError: If ``digit`` is not a DTMF symbol, or the rate/duration/amplitude
            is out of range.
    """
    low, high = _digit_to_tones(digit)
    if sample_rate <= 0:
        msg = f"sample_rate must be positive, got {sample_rate}"
        raise ValueError(msg)
    if duration_ms <= 0:
        msg = f"duration_ms must be positive, got {duration_ms}"
        raise ValueError(msg)
    if not 0 <= amplitude <= _INBAND_MAX_AMPLITUDE:
        msg = f"amplitude out of range 0..{_INBAND_MAX_AMPLITUDE}: {amplitude}"
        raise ValueError(msg)
    n = (sample_rate * duration_ms) // 1000
    two_pi = 2.0 * math.pi
    out = bytearray()
    for k in range(n):
        sample = amplitude * (
            math.sin(two_pi * low * k / sample_rate)
            + math.sin(two_pi * high * k / sample_rate)
        )
        out += struct.pack("<h", _clamp_int16(int(sample)))
    return bytes(out)


def _digit_to_tones(digit: str) -> tuple[int, int]:
    """Return the (low, high) Hz tone pair for a keypad ``digit``.

    Raises:
        ValueError: If ``digit`` is not a single DTMF keypad character.
    """
    char = digit.upper() if len(digit) == 1 else ""
    for row, symbols in enumerate(_DTMF_KEYPAD):
        for col, symbol in enumerate(symbols):
            if symbol == char:
                return _DTMF_LOW[row], _DTMF_HIGH[col]
    msg = f"not a DTMF digit: {digit!r}"
    raise ValueError(msg)


def _clamp_int16(value: int) -> int:
    return max(-32768, min(32767, value))


def _goertzel_power(samples: list[float], freq: int, sample_rate: int) -> float:
    """Goertzel-filter power of ``samples`` at ``freq`` Hz (ADR-0036).

    A single-bin DFT magnitude-squared: O(len(samples)) with two multiplies per
    sample, far cheaper than a full FFT and exactly what tone detection needs.
    """
    k = 2.0 * math.cos(2.0 * math.pi * freq / sample_rate)
    s_prev = 0.0
    s_prev2 = 0.0
    for sample in samples:
        s = sample + k * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 * s_prev2 + s_prev * s_prev - k * s_prev * s_prev2


class InbandDtmfDetector:
    """Goertzel DTMF detection over decoded G.711 PCM frames (ADR-0010/0034).

    The in-band last resort: when a gateway negotiates no telephone-event, this
    detects keypad tones in the audio itself. It is a SECURITY control (a digit can
    resolve an ADR-0009 confirmation), so it is biased hard toward *rejecting*
    speech and noise: a frame yields a candidate digit only when one low tone and
    one high tone each dominate their group AND the frame's total energy, with twist
    bounded and the low tone's second harmonic not rivalling the high group (the
    classic voiced-speech rejecter). A candidate must persist for a minimum number
    of consecutive validated frames (debounce) before it emits, exactly once per
    press; the digit re-arms only after a SUSTAINED gap (``_INBAND_GAP_RELEASE_FRAMES``
    consecutive frames with no validated tone), so a single dropped or momentarily-
    silent frame mid-press does not re-arm it and cannot produce a duplicate digit.

    In-band is trusted ONLY on clean G.711 (ADR-0005), so the detector analyses at
    8 kHz. Feed it the AEC-cleaned decoded frame (ADR-0033) so the agent's own
    reflected tones never false-trigger.
    """

    def __init__(self, *, sample_rate: int = 8000) -> None:
        """Create a detector analysing PCM16 frames at ``sample_rate`` Hz."""
        if sample_rate <= 0:
            msg = f"sample_rate must be positive, got {sample_rate}"
            raise ValueError(msg)
        self._sample_rate = sample_rate
        # The digit currently being held (debounced + already emitted), or None
        # between presses. A new press must differ from this OR follow a sustained
        # gap (release); a single dropped/silent frame does not clear it.
        self._emitted: str | None = None
        # The candidate digit accumulating consecutive validated frames, and its run.
        self._candidate: str | None = None
        self._run = 0
        # Consecutive gap (no-validated-tone) frames seen while a press is held, for
        # release hysteresis: the held press clears only after
        # _INBAND_GAP_RELEASE_FRAMES of them, so a single dropped/silent frame
        # mid-press does not re-arm.
        self._gap_run = 0

    def feed(self, pcm16: bytes) -> str | None:
        """Process one PCM16-LE mono frame; return a digit on a new press, else None.

        Returns:
            The pressed digit on the frame that completes the debounce of a NEW
            press, else ``None`` (no validated tone, still debouncing, or the same
            held press continuing).
        """
        digit = self._detect_frame(pcm16)
        if digit is None:
            # A gap frame. A partial (not-yet-emitted) candidate never survives a gap.
            self._candidate = None
            self._run = 0
            # Release hysteresis: a SINGLE dropped or momentarily-silent frame
            # mid-press must NOT re-arm the detector, or the same continuing tone
            # would emit a spurious duplicate digit. Only a sustained gap counts as a
            # release: keep the emitted press until _INBAND_GAP_RELEASE_FRAMES
            # consecutive gap frames, then allow that digit to be pressed again.
            if self._emitted is not None:
                self._gap_run += 1
                if self._gap_run >= _INBAND_GAP_RELEASE_FRAMES:
                    self._emitted = None
                    self._gap_run = 0
            return None
        # A validated tone frame: the tone is present, so any release in progress is
        # cancelled (an intermittent single-frame dropout does not accumulate).
        self._gap_run = 0
        if digit == self._candidate:
            self._run += 1
        else:
            self._candidate = digit
            self._run = 1
        # Emit once the candidate has been validated for enough consecutive frames,
        # and only if it is not already the held (emitted) press.
        if self._run >= _INBAND_MIN_PRESS_FRAMES and digit != self._emitted:
            self._emitted = digit
            return digit
        return None

    def _detect_frame(self, pcm16: bytes) -> str | None:  # noqa: PLR0911 — each return is one DTMF validation gate (energy, per-tone fraction, combined fraction, dominance x2, twist, second-harmonic); a flat reject-and-return chain is the clearest form and collapsing them would obscure which test rejected
        """Return the validated digit for one frame, or None (no clean tone pair)."""
        n = len(pcm16) // 2
        if n == 0:
            return None
        samples = [float(v) for v in struct.unpack(f"<{n}h", pcm16)]
        total_energy = sum(s * s for s in samples)
        if total_energy < _INBAND_MIN_FRAME_ENERGY:
            return None

        # N-normalise (x 2/N) so each bin's power is in the frame's energy units: a
        # pure tone scores ~the frame energy, a DTMF pair ~half each.
        norm = 2.0 / n
        low_powers = [
            _goertzel_power(samples, f, self._sample_rate) * norm for f in _DTMF_LOW
        ]
        high_powers = [
            _goertzel_power(samples, f, self._sample_rate) * norm for f in _DTMF_HIGH
        ]
        low_idx = _argmax(low_powers)
        high_idx = _argmax(high_powers)
        low_p = low_powers[low_idx]
        high_p = high_powers[high_idx]

        # Each winning tone must hold a real share of the frame's energy, AND the two
        # together must hold most of it. The combined-fraction test is the primary
        # speech rejecter: a DTMF pair scores ~1.0, voiced speech far less.
        if low_p < _INBAND_MIN_TONE_FRACTION * total_energy:
            return None
        if high_p < _INBAND_MIN_TONE_FRACTION * total_energy:
            return None
        if low_p + high_p < _INBAND_MIN_COMBINED_FRACTION * total_energy:
            return None
        # Each winner must dominate its own group (rejects broadband energy that
        # clears the fraction floor without a real single-tone peak).
        if not _dominates(low_powers, low_idx, _INBAND_GROUP_DOMINANCE):
            return None
        if not _dominates(high_powers, high_idx, _INBAND_GROUP_DOMINANCE):
            return None
        # Twist: the louder of the pair may exceed the quieter by at most the bound.
        louder, quieter = (low_p, high_p) if low_p >= high_p else (high_p, low_p)
        if quieter <= 0.0 or louder / quieter > _INBAND_MAX_TWIST:
            return None
        # Harmonic-corroboration rejecter: a voiced source that lands two harmonics on a
        # DTMF pair also carries energy at the second harmonic of EACH detected tone and
        # at the difference frequency (its would-be fundamental). A real DTMF generator
        # emits only the two tones, so those bins are ~empty. Reject when their combined
        # energy rivals the weaker detected tone. Catches the harmonic-speech case the
        # bare 2x-low test missed (cross-vendor review #1).
        low_f = _DTMF_LOW[low_idx]
        high_f = _DTMF_HIGH[high_idx]
        corroboration = (
            _goertzel_power(samples, 2 * low_f, self._sample_rate)
            + _goertzel_power(samples, 2 * high_f, self._sample_rate)
            + _goertzel_power(samples, high_f - low_f, self._sample_rate)
        ) * norm
        if corroboration / quieter > _INBAND_MAX_HARMONIC_CORROBORATION:
            return None
        return _DTMF_KEYPAD[low_idx][high_idx]


def _argmax(values: list[float]) -> int:
    """Index of the largest value (first on ties)."""
    best = 0
    for i in range(1, len(values)):
        if values[i] > values[best]:
            best = i
    return best


def _dominates(powers: list[float], idx: int, factor: float) -> bool:
    """Whether ``powers[idx]`` is at least ``factor`` times every other entry."""
    peak = powers[idx]
    return all(p * factor <= peak for i, p in enumerate(powers) if i != idx)
