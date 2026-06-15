"""RTP packetisation and a reordering jitter buffer (RFC 3550).

The transport (ADR-0005) packs outbound PCM payloads into RTP and feeds inbound
packets through a :class:`JitterBuffer` that reorders by sequence number, drops
duplicates and late arrivals, and signals loss (so the media loop can conceal)
— all using RFC 1982 serial-number arithmetic so 16-bit sequence wraparound is
handled. SRTP keying lives in the transport; this module is the plaintext RTP
shape.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

_RTP_VERSION = 2
_HEADER_LEN = 12
_EXT_HEADER_LEN = 4
_MAX_PAYLOAD_TYPE = 0x7F
_MARKER_BIT = 0x80
_PADDING_BIT = 0x20
_EXTENSION_BIT = 0x10
_CSRC_MASK = 0x0F
_CSRC_WORD = 4
_SEQ_MOD = 1 << 16
_SEQ_HALF = 1 << 15
_MAX_SEQ = 0xFFFF
_U32 = 0xFFFFFFFF
_DEFAULT_MAX_AHEAD = 256


@dataclass(frozen=True, slots=True)
class RtpPacket:
    """An RTP packet's fixed header fields plus its payload (RFC 3550 §5.1).

    Attributes:
        payload_type: The 7-bit payload type (e.g. 0 = PCMU, 101 = telephone-event).
        sequence_number: The 16-bit sequence number.
        timestamp: The 32-bit RTP timestamp.
        ssrc: The 32-bit synchronisation source identifier.
        payload: The media payload bytes.
        marker: The marker bit (e.g. first packet of a talk-spurt / DTMF end).
    """

    payload_type: int
    sequence_number: int
    timestamp: int
    ssrc: int
    payload: bytes
    marker: bool = False

    def __post_init__(self) -> None:
        """Validate that every fixed-width field fits its RFC 3550 range."""
        if not 0 <= self.payload_type <= _MAX_PAYLOAD_TYPE:
            msg = f"payload_type out of range 0..127: {self.payload_type}"
            raise ValueError(msg)
        if not 0 <= self.sequence_number <= _MAX_SEQ:
            msg = f"sequence_number out of range 0..65535: {self.sequence_number}"
            raise ValueError(msg)
        if not 0 <= self.timestamp <= _U32:
            msg = f"timestamp out of range 0..2^32-1: {self.timestamp}"
            raise ValueError(msg)
        if not 0 <= self.ssrc <= _U32:
            msg = f"ssrc out of range 0..2^32-1: {self.ssrc}"
            raise ValueError(msg)

    def pack(self) -> bytes:
        """Serialise to wire bytes (12-byte header, no CSRC/extension/padding)."""
        byte0 = _RTP_VERSION << 6  # V=2, P=0, X=0, CC=0
        byte1 = (_MARKER_BIT if self.marker else 0) | self.payload_type
        header = struct.pack(
            "!BBHII",
            byte0,
            byte1,
            self.sequence_number,
            self.timestamp,
            self.ssrc,
        )
        return header + self.payload

    @classmethod
    def parse(cls, data: bytes) -> RtpPacket:
        """Parse wire bytes into an :class:`RtpPacket`.

        Skips any CSRC list and the RFC 3550 §5.3.1 extension header (if the X
        bit is set), and strips padding, so ``payload`` is the media bytes only.

        Raises:
            ValueError: If the data is too short for its declared fields or the
                RTP version is not 2.
        """
        if len(data) < _HEADER_LEN:
            msg = f"RTP packet too short: {len(data)} bytes"
            raise ValueError(msg)
        byte0, byte1, seq, timestamp, ssrc = struct.unpack("!BBHII", data[:_HEADER_LEN])
        version = byte0 >> 6
        if version != _RTP_VERSION:
            msg = f"unsupported RTP version: {version}"
            raise ValueError(msg)
        offset = _HEADER_LEN + (byte0 & _CSRC_MASK) * _CSRC_WORD
        if len(data) < offset:
            msg = "RTP packet too short for its CSRC count"
            raise ValueError(msg)
        if byte0 & _EXTENSION_BIT:
            if len(data) < offset + _EXT_HEADER_LEN:
                msg = "RTP packet too short for its extension header"
                raise ValueError(msg)
            ext_words = int.from_bytes(data[offset + 2 : offset + 4], "big")
            offset += _EXT_HEADER_LEN + ext_words * _CSRC_WORD
            if len(data) < offset:
                msg = "RTP packet too short for its extension data"
                raise ValueError(msg)
        payload = data[offset:]
        if byte0 & _PADDING_BIT and payload:
            pad = payload[-1]
            if 0 < pad <= len(payload):
                payload = payload[:-pad]
        return cls(
            payload_type=byte1 & _MAX_PAYLOAD_TYPE,
            sequence_number=seq,
            timestamp=timestamp,
            ssrc=ssrc,
            payload=payload,
            marker=bool(byte1 & _MARKER_BIT),
        )


@dataclass(frozen=True, slots=True)
class Lost:
    """A jitter-buffer signal that the packet for ``sequence`` is lost."""

    sequence: int


type JitterOutput = RtpPacket | Lost


def _seq_before(a: int, b: int) -> bool:
    """True if ``a`` precedes ``b`` in 16-bit serial-number space (RFC 1982)."""
    return a != b and (b - a) % _SEQ_MOD < _SEQ_HALF


def _seq_next(seq: int) -> int:
    """The sequence number following ``seq`` (16-bit wraparound)."""
    return (seq + 1) % _SEQ_MOD


class JitterBuffer:
    """Reorders inbound RTP and signals loss for concealment.

    ``pop`` returns the next in-order packet when available, a :class:`Lost`
    marker once ``target_depth`` later packets have piled up behind a gap (so
    the caller conceals and moves on), or ``None`` on underflow (wait for more).
    """

    def __init__(
        self, target_depth: int = 2, max_ahead: int = _DEFAULT_MAX_AHEAD
    ) -> None:
        """Create the buffer.

        Args:
            target_depth: Declare loss once this many later packets have piled
                up behind a gap.
            max_ahead: Playout window — packets more than this many sequence
                numbers ahead of the next expected one are dropped, bounding
                storage even under a permanent gap or a source resync.
        """
        if target_depth < 1:
            msg = f"target_depth must be >= 1, got {target_depth}"
            raise ValueError(msg)
        if max_ahead < 1:
            msg = f"max_ahead must be >= 1, got {max_ahead}"
            raise ValueError(msg)
        self._depth = target_depth
        self._max_ahead = max_ahead
        self._packets: dict[int, RtpPacket] = {}
        self._next: int | None = None
        self._emitted = False

    def push(self, packet: RtpPacket) -> None:
        """Add a packet; duplicate, late, and too-far-ahead packets are dropped.

        The anchor (next expected sequence) is set tentatively at the first
        arrival and revised downward by any earlier sequence that arrives before
        the first :meth:`pop`, so a reordered opening packet (a lower sequence
        arriving after a higher one) is never skipped — the start of a call is
        the most reordering-prone. The playout window is measured against this
        tentative anchor throughout, keeping storage bounded. Once a packet has
        been emitted the anchor only advances, and late arrivals are dropped.
        """
        seq = packet.sequence_number
        if self._next is not None:
            if _seq_before(seq, self._next):
                if self._emitted:
                    return  # too late: this sequence has already been emitted
                self._next = seq  # pre-playout reorder: revise the anchor down
            elif (seq - self._next) % _SEQ_MOD > self._max_ahead:
                return  # outside the playout window: keep storage bounded
        else:
            self._next = seq  # tentative anchor at the first arrival
        self._packets.setdefault(seq, packet)  # first arrival wins; ignore duplicates

    def pop(self) -> JitterOutput | None:
        """Return the next packet, a :class:`Lost` marker, or ``None`` (underflow)."""
        if self._next is None:
            return None
        expected = self._next
        packet = self._packets.pop(expected, None)
        if packet is not None:
            self._emitted = True
            self._next = _seq_next(expected)
            return packet
        if len(self._packets) >= self._depth:
            self._emitted = True
            self._next = _seq_next(expected)
            return Lost(expected)
        return None
