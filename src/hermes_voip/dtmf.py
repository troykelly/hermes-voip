"""DTMF over RTP via RFC 4733 telephone-events (ADR-0010).

The named-event RTP payload is four bytes: the event code, an end bit + volume,
and a 16-bit duration. This module is the payload codec, the digit<->event
mapping, the outbound payload sequence for a key-press (incremental duration
plus the redundant end packets RFC 4733 §2.5.1.4 requires), and a receiver that
yields each pressed digit exactly once despite that triplicated end. RTP framing
of these payloads is the transport's job (hermes_voip.rtp). DTMF is also the
spoof-resistant confirmation channel for irreversible tools (ADR-0009).
"""

from __future__ import annotations

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
