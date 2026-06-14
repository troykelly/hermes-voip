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
_MAX_PAYLOAD_TYPE = 0x7F
_MARKER_BIT = 0x80
_PADDING_BIT = 0x20
_CSRC_MASK = 0x0F
_CSRC_WORD = 4
_SEQ_MOD = 1 << 16
_SEQ_HALF = 1 << 15
_U32 = 0xFFFFFFFF


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

    def pack(self) -> bytes:
        """Serialise to wire bytes (12-byte header, no CSRC/extension/padding).

        Raises:
            ValueError: If ``payload_type`` is outside ``0..127``.
        """
        if not 0 <= self.payload_type <= _MAX_PAYLOAD_TYPE:
            msg = f"payload_type out of range 0..127: {self.payload_type}"
            raise ValueError(msg)
        byte0 = _RTP_VERSION << 6  # V=2, P=0, X=0, CC=0
        byte1 = (_MARKER_BIT if self.marker else 0) | self.payload_type
        header = struct.pack(
            "!BBHII",
            byte0,
            byte1,
            self.sequence_number & 0xFFFF,
            self.timestamp & _U32,
            self.ssrc & _U32,
        )
        return header + self.payload

    @classmethod
    def parse(cls, data: bytes) -> RtpPacket:
        """Parse wire bytes into an :class:`RtpPacket`.

        Skips any CSRC list and strips RFC 3550 padding; an extension header,
        if present, is left in the payload (telephony peers do not use one).

        Raises:
            ValueError: If the data is too short or the RTP version is not 2.
        """
        if len(data) < _HEADER_LEN:
            msg = f"RTP packet too short: {len(data)} bytes"
            raise ValueError(msg)
        byte0, byte1, seq, timestamp, ssrc = struct.unpack("!BBHII", data[:_HEADER_LEN])
        version = byte0 >> 6
        if version != _RTP_VERSION:
            msg = f"unsupported RTP version: {version}"
            raise ValueError(msg)
        header_len = _HEADER_LEN + (byte0 & _CSRC_MASK) * _CSRC_WORD
        if len(data) < header_len:
            msg = "RTP packet too short for its CSRC count"
            raise ValueError(msg)
        payload = data[header_len:]
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

    def __init__(self, target_depth: int = 2) -> None:
        """Create a buffer that declares loss after ``target_depth`` later packets."""
        if target_depth < 1:
            msg = f"target_depth must be >= 1, got {target_depth}"
            raise ValueError(msg)
        self._depth = target_depth
        self._packets: dict[int, RtpPacket] = {}
        self._next: int | None = None

    def push(self, packet: RtpPacket) -> None:
        """Add a packet; duplicates and already-played (late) packets are dropped."""
        seq = packet.sequence_number
        if self._next is not None and _seq_before(seq, self._next):
            return  # too late: this sequence has already been emitted
        self._packets.setdefault(seq, packet)  # first arrival wins; ignore duplicates
        if self._next is None:
            self._next = seq  # anchor playout at the first packet seen

    def pop(self) -> JitterOutput | None:
        """Return the next packet, a :class:`Lost` marker, or ``None`` (underflow)."""
        if self._next is None:
            return None
        expected = self._next
        packet = self._packets.pop(expected, None)
        if packet is not None:
            self._next = _seq_next(expected)
            return packet
        if len(self._packets) >= self._depth:
            self._next = _seq_next(expected)
            return Lost(expected)
        return None
