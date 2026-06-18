"""DTMF over RTP (RFC 4733 telephone-events) plus the in-band backend (ADR-0010/0034).

The named-event RTP payload is four bytes: the event code, an end bit + volume,
and a 16-bit duration. This module is the payload codec, the digit<->event
mapping, the outbound payload sequence for a key-press (incremental duration
plus the redundant end packets RFC 4733 §2.5.1.4 requires), and a receiver that
yields each pressed digit exactly once despite that triplicated end. RTP framing
of these payloads is the transport's job (hermes_voip.rtp). DTMF is also the
spoof-resistant confirmation channel for irreversible tools (ADR-0009).

ADR-0035 adds the **in-band** backend — the last resort for a gateway that
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
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass

# Event codes 0..15 map to the keypad; 16 is flash. Index == event code.
_DIGITS = "0123456789*#ABCD"
_PAYLOAD_LEN = 4
_END_BIT = 0x80
_VOLUME_MASK = 0x3F
_MAX_EVENT = 0xFF
_MAX_VOLUME = 0x3F
_MAX_DURATION = 0xFFFF
_REDUNDANT_END_COUNT = 3
_RECEIVER_HISTORY = 32


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


class DtmfReceiver:
    """Collapses RFC 4733 events into one digit per key-press.

    A key-press ends with redundant end packets at one RTP timestamp; the
    receiver emits on the first and suppresses duplicates, including ones that
    arrive reordered (after a later press), by remembering a bounded window of
    recently-emitted timestamps. A later press carries a new RTP timestamp and
    is emitted again. The end packet is the trigger: a press whose start/update
    packets were all lost is still surfaced (favouring not missing a digit), and
    a non-digit event (e.g. flash) is not surfaced as a digit.
    """

    def __init__(self, history: int = _RECEIVER_HISTORY) -> None:
        """Create a receiver remembering the last ``history`` press timestamps."""
        self._order: deque[int] = deque(maxlen=history)
        self._seen: set[int] = set()

    def feed(self, event: DtmfEvent, *, timestamp: int) -> str | None:
        """Process one decoded event at its RTP ``timestamp``.

        Returns:
            The pressed digit when a new key-press ends, else ``None`` (still
            pressing, a duplicate/reordered end packet, or a non-digit event).
        """
        if not event.end or timestamp in self._seen:
            return None
        if len(self._order) == self._order.maxlen and self._order:
            self._seen.discard(self._order[0])  # evicted with the deque append
        self._order.append(timestamp)
        self._seen.add(timestamp)
        if 0 <= event.event < len(_DIGITS):
            return _DIGITS[event.event]
        return None  # a non-digit telephone-event (e.g. flash); not surfaced


# ---------------------------------------------------------------------------
# In-band DTMF (ADR-0010 last resort / ADR-0035): Goertzel detect + tone gen
# ---------------------------------------------------------------------------

# The eight DTMF tones (Hz). A keypad symbol is one low (row) + one high (column).
_DTMF_LOW: tuple[int, ...] = (697, 770, 852, 941)
_DTMF_HIGH: tuple[int, ...] = (1209, 1336, 1477, 1633)
# Row x column -> keypad symbol (standard 4x4 incl. A-D on the 1633 Hz column).
_DTMF_KEYPAD: tuple[tuple[str, ...], ...] = (
    ("1", "2", "3", "A"),
    ("4", "5", "6", "B"),
    ("7", "8", "9", "C"),
    ("*", "0", "#", "D"),
)

_INBAND_MAX_AMPLITUDE = 0x3FFF  # per-tone amplitude (sum stays < int16 full-scale)

# Detection thresholds (tuned on 8 kHz clean G.711; ADR-0035 re-measures live).
# Goertzel power is N-normalised (x 2/N) so a pure full-amplitude tone scores ~the
# frame's energy and a DTMF pair scores ~half each — same units as the frame energy,
# so the fractions below are dimensionless and frame-length independent.
#
# A frame must carry real energy before any tone test runs (rejects silence/comfort
# noise without a divide-by-zero). Units: sum of squared int16 samples per frame.
_INBAND_MIN_FRAME_ENERGY = 5.0e5
# The winning low + high tone must EACH hold at least this fraction of the frame's
# energy (each genuine DTMF tone is ~0.5).
_INBAND_MIN_TONE_FRACTION = 0.33
# ...and their COMBINED energy must be at least this fraction of the frame's energy.
# This is the PRIMARY speech rejecter: a clean DTMF pair scores ~1.0 (all energy in
# two bins), whereas voiced speech spreads energy across its full harmonic series and
# tops out far below (measured ~0.04-0.17 on harmonic glides). Set HIGH (0.80) so a
# voiced source whose two strongest harmonics happen to land near a DTMF pair is still
# rejected by the energy it carries in its OTHER harmonics. (A signal that is spectrally
# ONLY two pure DTMF tones is, by definition, indistinguishable from a real keypress —
# the fundamental limit that makes in-band the LAST resort, ADR-0010/0034.)
_INBAND_MIN_COMBINED_FRACTION = 0.80
# Forward/reverse twist: the louder tone may exceed the quieter by at most this ratio
# (~8 dB). A wildly mismatched "pair" is not a keypress.
_INBAND_MAX_TWIST = 6.3
# A detected tone's energy must dominate the others IN ITS OWN group by this factor
# (rejects a single tone that merely happens to clear the fraction floor — its
# group runner-up would be comparable for broadband noise).
_INBAND_GROUP_DOMINANCE = 4.0
# Harmonic-corroboration rejecter: a voiced source that lands two harmonics on a DTMF
# pair ALSO carries energy at the OTHER harmonics it implies — most tellingly the second
# harmonic of each detected tone (2x low, 2x high) and the difference frequency (the
# would-be fundamental). A real DTMF generator emits ONLY the two tones, so those bins
# are ~empty. Reject when their combined energy rivals the weaker detected tone by this
# fraction. This catches the harmonic-speech case the bare second-harmonic test missed.
_INBAND_MAX_HARMONIC_CORROBORATION = 0.25
# Debounce: a digit must persist this many consecutive validated frames before it
# emits (one 20 ms frame is enough to flag a glitch; ~2 frames = a real press).
_INBAND_MIN_PRESS_FRAMES = 2


def inband_tone_pcm(
    digit: str,
    *,
    sample_rate: int,
    duration_ms: int,
    amplitude: int = _INBAND_MAX_AMPLITUDE,
) -> bytes:
    """Synthesise one key-press as in-band dual-tone PCM16-LE mono (ADR-0035).

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
    """Goertzel-filter power of ``samples`` at ``freq`` Hz (ADR-0035).

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
    press; the digit re-arms only after a frame with no validated tone (a gap).

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
        # between presses. A new press must differ from this OR follow a gap.
        self._emitted: str | None = None
        # The candidate digit accumulating consecutive validated frames, and its run.
        self._candidate: str | None = None
        self._run = 0

    def feed(self, pcm16: bytes) -> str | None:
        """Process one PCM16-LE mono frame; return a digit on a new press, else None.

        Returns:
            The pressed digit on the frame that completes the debounce of a NEW
            press, else ``None`` (no validated tone, still debouncing, or the same
            held press continuing).
        """
        digit = self._detect_frame(pcm16)
        if digit is None:
            # A gap frame: the held press ends, so the next press (even the same
            # digit) is a fresh key-press.
            self._emitted = None
            self._candidate = None
            self._run = 0
            return None
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
