"""RTCP packet layer — Sender/Receiver Reports, SDES, BYE (RFC 3550 §6).

The media transport (ADR-0005) carries RTP audio; this module is its RTCP control
channel: the periodic Sender Report (SR) / Receiver Report (RR) exchange that lets
each side report what it sent and what it received (loss, jitter, round-trip time),
plus the SDES CNAME that names the source and the BYE that ends participation.

This is **sans-IO**: it builds and parses the wire bytes and computes the RFC 3550
statistics from a stream of observed packets; it owns no socket and no clock. The
engine (``media/engine.py``) drives it with an injected clock and packet sink, so
the send schedule and the reception maths are deterministic and unit-testable.

Wire layout (RFC 3550 §6.4):

* **Report block** (§6.4.1) — 24 bytes per reported source: SSRC, fraction lost
  (8-bit fixed point), cumulative lost (signed 24-bit), extended highest sequence
  number, interarrival jitter, last SR timestamp (LSR), delay since last SR (DLSR).
* **Sender Report** (§6.4.1) — header + sender info (SSRC, 64-bit NTP timestamp,
  RTP timestamp, sender packet/octet counts) + N report blocks.
* **Receiver Report** (§6.4.2) — header + reporter SSRC + N report blocks (a sender
  info-less SR, for a receive-only endpoint).
* **SDES** (§6.5) — per-source chunks of TLV items; we emit/parse the mandatory
  CNAME (item type 1).
* **BYE** (§6.6) — the leaving SSRC(s) and an optional reason string.

Compound packets (§6.1): every RTCP datagram is a concatenation of individual
packets beginning with an SR or RR. :func:`build_compound` / :func:`parse_compound`
handle the sequence; unknown packet types are traversed (by their length field) and
skipped, never fatal.

The statistics maths (:class:`ReceptionStats`, :func:`compute_rtcp_interval`,
:func:`rtt_from_report_block`) follows RFC 3550 Appendix A.1/A.3/A.7/A.8.
"""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass, field

# RTCP packet types (RFC 3550 §12.1 / IANA).
RTCP_PT_SR: int = 200
RTCP_PT_RR: int = 201
RTCP_PT_SDES: int = 202
RTCP_PT_BYE: int = 203
RTCP_PT_APP: int = 204

# SDES item type for the canonical name (RFC 3550 §6.5.1).
_SDES_ITEM_CNAME: int = 1

_RTP_VERSION: int = 2
_VERSION_SHIFT: int = 6
_COUNT_MASK: int = 0x1F  # the low-5 RC / SC count in the first byte
_PADDING_BIT: int = 0x20

_U8_MAX: int = 0xFF
_U32: int = 0xFFFFFFFF
_U64: int = (1 << 64) - 1
_S24_MIN: int = -(1 << 23)
_S24_MAX: int = (1 << 23) - 1
_MAX_COUNT: int = 0x1F  # RC/SC is a 5-bit field

_WORD: int = 4  # bytes per 32-bit RTCP length unit
_SSRC_LEN: int = 4  # an SSRC/CSRC identifier is one 32-bit word
_SDES_ITEM_HEADER_LEN: int = 2  # an SDES item's type + length octets
_COMMON_HEADER_LEN: int = 4
_REPORT_BLOCK_LEN: int = 24
_SENDER_INFO_LEN: int = 20  # SSRC(4) + NTP(8) + RTP ts(4) + pkt(4) ... see below
# Sender info per §6.4.1 is NTP(8) + RTP ts(4) + packet count(4) + octet count(4)
# = 20 bytes, AFTER the 4-byte sender SSRC that follows the common header.

# Seconds between the NTP epoch (1900-01-01 00:00) and the Unix epoch (1970-01-01).
_NTP_UNIX_EPOCH_DELTA: int = 2_208_988_800
_FRAC_SCALE: int = 1 << 32  # 32-bit NTP fraction resolution
_COMPACT_FRAC_SCALE: int = 1 << 16  # LSR/DLSR are NTP middle-32 (16.16 s) units

# RFC 3550 §6.2 / Appendix A.7 transmission-interval constants.
_RTCP_MIN_TIME: float = 5.0  # seconds — the minimum deterministic interval
_RTCP_SENDER_BW_FRACTION: float = 0.25  # senders share 25% of the RTCP bandwidth
# §6.3.1 compensation for the [0.5, 1.5] uniform randomisation (e - 3/2).
_COMPENSATION: float = 1.21828

# RFC 3550 Appendix A.1 sequence-validity constants. A forward jump larger than
# _MAX_DROPOUT, or a backward jump larger than _MAX_MISORDER, is not normal
# loss/reorder: it is a possible source restart, validated by a second in-sequence
# packet from the new base (the bad_seq mechanism) before the baseline is re-based.
_MAX_DROPOUT: int = 3000
_MAX_MISORDER: int = 100
_SEQ_MOD: int = 1 << 16
# The "impossible" sentinel A.1 seeds bad_seq with (one past the 16-bit space) so the
# first out-of-range packet cannot accidentally match it.
_RTP_SEQ_NONE: int = _SEQ_MOD + 1


class RtcpError(Exception):
    """A malformed or unsupported RTCP packet was encountered while parsing."""


# ---------------------------------------------------------------------------
# NTP timestamp conversion (RFC 3550 §4)
# ---------------------------------------------------------------------------


def to_ntp(unix_seconds: float) -> int:
    """Convert Unix seconds to a 64-bit NTP timestamp (RFC 3550 §4).

    The result is a 64-bit fixed-point value: the high 32 bits are seconds since
    1900-01-01, the low 32 bits the fraction of a second.

    Raises:
        ValueError: If ``unix_seconds`` is negative (it would precede the Unix
            epoch and cannot be represented as an unsigned NTP value here).
    """
    if unix_seconds < 0:
        msg = f"cannot represent a negative time as NTP: {unix_seconds}"
        raise ValueError(msg)
    total = unix_seconds + _NTP_UNIX_EPOCH_DELTA
    seconds = int(total)
    fraction = round((total - seconds) * _FRAC_SCALE)
    # Rounding the fraction up can carry into the next second; normalise it.
    if fraction > _U32:
        fraction = 0
        seconds += 1
    return ((seconds & _U32) << 32) | (fraction & _U32)


def from_ntp(ntp: int) -> float:
    """Convert a 64-bit NTP timestamp back to Unix seconds (RFC 3550 §4)."""
    seconds = (ntp >> 32) & _U32
    fraction = ntp & _U32
    return (seconds - _NTP_UNIX_EPOCH_DELTA) + fraction / _FRAC_SCALE


def _ntp_compact(ntp: int) -> int:
    """The middle 32 bits of a 64-bit NTP timestamp (the LSR field, RFC 3550 §6.4.1)."""
    return (ntp >> 16) & _U32


# ---------------------------------------------------------------------------
# Report block (RFC 3550 §6.4.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReportBlock:
    """One reception report block: how we received from one source (RFC 3550 §6.4.1).

    Attributes:
        ssrc: The SSRC of the source this block reports on.
        fraction_lost: Packets lost in the interval since the previous report, as
            an 8-bit fixed-point fraction (lost/expected * 256, 0..255).
        cumulative_lost: Total packets lost over the whole session, a SIGNED
            24-bit value (duplicates can make it negative).
        extended_highest_seq: The highest sequence number received, extended with
            the 16-bit cycle count in the high half.
        jitter: The interarrival jitter estimate, in RTP timestamp units of the
            source's clock (RFC 3550 §6.4.1 / Appendix A.8).
        lsr: The middle 32 bits of the NTP timestamp of the last SR received from
            this source (0 if none), the "last SR" field.
        dlsr: Delay since that last SR, in units of 1/65536 s (the DLSR field).
    """

    ssrc: int
    fraction_lost: int
    cumulative_lost: int
    extended_highest_seq: int
    jitter: int
    lsr: int
    dlsr: int

    def __post_init__(self) -> None:
        """Validate every field fits its RFC 3550 §6.4.1 width."""
        if not 0 <= self.ssrc <= _U32:
            msg = f"ssrc out of range 0..2^32-1: {self.ssrc}"
            raise ValueError(msg)
        if not 0 <= self.fraction_lost <= _U8_MAX:
            msg = f"fraction_lost out of range 0..255: {self.fraction_lost}"
            raise ValueError(msg)
        if not _S24_MIN <= self.cumulative_lost <= _S24_MAX:
            msg = (
                "cumulative_lost out of signed-24-bit range "
                f"{_S24_MIN}..{_S24_MAX}: {self.cumulative_lost}"
            )
            raise ValueError(msg)
        if not 0 <= self.extended_highest_seq <= _U32:
            ehsn = self.extended_highest_seq
            msg = f"extended_highest_seq out of range 0..2^32-1: {ehsn}"
            raise ValueError(msg)
        if not 0 <= self.jitter <= _U32:
            msg = f"jitter out of range 0..2^32-1: {self.jitter}"
            raise ValueError(msg)
        if not 0 <= self.lsr <= _U32:
            msg = f"lsr out of range 0..2^32-1: {self.lsr}"
            raise ValueError(msg)
        if not 0 <= self.dlsr <= _U32:
            msg = f"dlsr out of range 0..2^32-1: {self.dlsr}"
            raise ValueError(msg)

    def pack(self) -> bytes:
        """Serialise to the 24-byte wire form (RFC 3550 §6.4.1)."""
        cumulative = self.cumulative_lost & 0xFFFFFF  # signed 24-bit two's complement
        return (
            struct.pack("!I", self.ssrc)
            + bytes([self.fraction_lost])
            + cumulative.to_bytes(3, "big")
            + struct.pack(
                "!IIII", self.extended_highest_seq, self.jitter, self.lsr, self.dlsr
            )
        )

    @classmethod
    def parse(cls, data: bytes) -> ReportBlock:
        """Parse a 24-byte report block.

        Raises:
            RtcpError: If ``data`` is not exactly 24 bytes.
        """
        if len(data) != _REPORT_BLOCK_LEN:
            msg = f"report block must be 24 bytes, got {len(data)}"
            raise RtcpError(msg)
        (ssrc,) = struct.unpack("!I", data[0:4])
        fraction_lost = data[4]
        cumulative = int.from_bytes(data[5:8], "big")
        if cumulative >= 1 << 23:  # sign-extend the 24-bit two's-complement field
            cumulative -= 1 << 24
        extended_highest_seq, jitter, lsr, dlsr = struct.unpack("!IIII", data[8:24])
        return cls(
            ssrc=ssrc,
            fraction_lost=fraction_lost,
            cumulative_lost=cumulative,
            extended_highest_seq=extended_highest_seq,
            jitter=jitter,
            lsr=lsr,
            dlsr=dlsr,
        )


def _pack_header(*, count: int, payload_type: int, body_words: int) -> bytes:
    """Pack the 4-byte RTCP common header (V=2, P=0).

    ``body_words`` is the number of 32-bit words AFTER the header; the wire length
    field is the total packet length in words minus one (RFC 3550 §6.4.1).
    """
    if not 0 <= count <= _MAX_COUNT:
        msg = f"RTCP report/source count out of range 0..31: {count}"
        raise ValueError(msg)
    byte0 = (_RTP_VERSION << _VERSION_SHIFT) | count
    length = body_words  # (total words) - 1 == header(1) + body_words - 1
    return struct.pack("!BBH", byte0, payload_type, length)


@dataclass(frozen=True, slots=True)
class _CommonHeader:
    """The decoded RTCP common header plus the byte span of this packet's body."""

    count: int
    padding: bool
    payload_type: int
    body: bytes


def _parse_header(data: bytes, offset: int) -> tuple[_CommonHeader, int]:
    """Parse the common header at ``offset`` and return it plus the next offset.

    Validates the RTP version and that the declared length fits the buffer. The
    returned body excludes the 4-byte header. ``offset`` of the next packet (after
    this one's full length) is returned for compound traversal.

    Raises:
        RtcpError: On a bad version or a length that runs past ``data``.
    """
    if len(data) - offset < _COMMON_HEADER_LEN:
        msg = "RTCP packet too short for its common header"
        raise RtcpError(msg)
    byte0, payload_type, length_words = struct.unpack(
        "!BBH", data[offset : offset + _COMMON_HEADER_LEN]
    )
    version = byte0 >> _VERSION_SHIFT
    if version != _RTP_VERSION:
        msg = f"unsupported RTCP version: {version}"
        raise RtcpError(msg)
    total_len = (length_words + 1) * _WORD
    end = offset + total_len
    if end > len(data):
        msg = (
            f"RTCP length field ({total_len} bytes) runs past the "
            f"{len(data) - offset}-byte remaining buffer"
        )
        raise RtcpError(msg)
    body = data[offset + _COMMON_HEADER_LEN : end]
    padding = bool(byte0 & _PADDING_BIT)
    if padding:
        # RFC 3550 §6.4.1: the padding bit means the last octet of THIS packet is
        # the pad count (including itself); those octets are not part of the fields.
        # Strip them so the body parser sees only the real content. The pad count
        # must be in 1..len(body) — anything else is a malformed packet (rule 37).
        if not body:
            msg = "RTCP padding bit set on an empty packet body"
            raise RtcpError(msg)
        pad = body[-1]
        if not 1 <= pad <= len(body):
            msg = f"RTCP pad count {pad} out of range 1..{len(body)}"
            raise RtcpError(msg)
        body = body[:-pad]
    header = _CommonHeader(
        count=byte0 & _COUNT_MASK,
        padding=padding,
        payload_type=payload_type,
        body=body,
    )
    return header, end


# ---------------------------------------------------------------------------
# Sender Report (RFC 3550 §6.4.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SenderReport:
    """An RTCP Sender Report: what we sent + reception reports (RFC 3550 §6.4.1).

    Attributes:
        ssrc: Our (the sender's) SSRC.
        ntp_timestamp: The 64-bit NTP wallclock at the moment of this report.
        rtp_timestamp: The RTP timestamp corresponding to ``ntp_timestamp`` (same
            clock as the media stream), so a receiver can synchronise streams.
        packet_count: Total RTP data packets we have sent this session.
        octet_count: Total RTP payload octets we have sent this session.
        report_blocks: Reception report blocks for sources we receive from.
    """

    ssrc: int
    ntp_timestamp: int
    rtp_timestamp: int
    packet_count: int
    octet_count: int
    report_blocks: tuple[ReportBlock, ...] = ()

    def __post_init__(self) -> None:
        """Validate the fixed-width fields and the report-block count."""
        if not 0 <= self.ssrc <= _U32:
            msg = f"ssrc out of range 0..2^32-1: {self.ssrc}"
            raise ValueError(msg)
        if not 0 <= self.ntp_timestamp <= _U64:
            msg = f"ntp_timestamp out of range 0..2^64-1: {self.ntp_timestamp}"
            raise ValueError(msg)
        for name, value in (
            ("rtp_timestamp", self.rtp_timestamp),
            ("packet_count", self.packet_count),
            ("octet_count", self.octet_count),
        ):
            if not 0 <= value <= _U32:
                msg = f"{name} out of range 0..2^32-1: {value}"
                raise ValueError(msg)
        if len(self.report_blocks) > _MAX_COUNT:
            n = len(self.report_blocks)
            msg = f"a single SR carries at most 31 report blocks, got {n}"
            raise ValueError(msg)

    def pack(self) -> bytes:
        """Serialise the SR (header + sender info + report blocks)."""
        # Sender SSRC(4) + NTP(8) + RTP timestamp(4) + packet count(4) + octet count(4).
        sender_info = struct.pack(
            "!IQIII",
            self.ssrc,
            self.ntp_timestamp,
            self.rtp_timestamp,
            self.packet_count,
            self.octet_count,
        )
        blocks = b"".join(b.pack() for b in self.report_blocks)
        body = sender_info + blocks
        header = _pack_header(
            count=len(self.report_blocks),
            payload_type=RTCP_PT_SR,
            body_words=len(body) // _WORD,
        )
        return header + body

    @classmethod
    def parse(cls, data: bytes) -> SenderReport:
        """Parse a standalone SR packet (RFC 3550 §6.4.1)."""
        header, _ = _parse_header(data, 0)
        if header.payload_type != RTCP_PT_SR:
            msg = f"not a Sender Report (PT={header.payload_type})"
            raise RtcpError(msg)
        return cls._from_body(header)

    @classmethod
    def _from_body(cls, header: _CommonHeader) -> SenderReport:
        """Build an SR from an already-parsed common header + body."""
        body = header.body
        min_len = 4 + _SENDER_INFO_LEN  # SSRC + sender info
        if len(body) < min_len:
            msg = "SR body too short for the sender info"
            raise RtcpError(msg)
        ssrc, ntp, rtp_ts, pkt, octets = struct.unpack("!IQIII", body[:min_len])
        blocks = _parse_report_blocks(body[min_len:], header.count)
        return cls(
            ssrc=ssrc,
            ntp_timestamp=ntp,
            rtp_timestamp=rtp_ts,
            packet_count=pkt,
            octet_count=octets,
            report_blocks=blocks,
        )


# ---------------------------------------------------------------------------
# Receiver Report (RFC 3550 §6.4.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReceiverReport:
    """An RTCP Receiver Report: reception reports from a non-sender (RFC 3550 §6.4.2).

    Attributes:
        ssrc: Our (the reporter's) SSRC.
        report_blocks: Reception report blocks for the sources we receive from.
    """

    ssrc: int
    report_blocks: tuple[ReportBlock, ...] = ()

    def __post_init__(self) -> None:
        """Validate the reporter SSRC and the report-block count."""
        if not 0 <= self.ssrc <= _U32:
            msg = f"ssrc out of range 0..2^32-1: {self.ssrc}"
            raise ValueError(msg)
        if len(self.report_blocks) > _MAX_COUNT:
            n = len(self.report_blocks)
            msg = f"a single RR carries at most 31 report blocks, got {n}"
            raise ValueError(msg)

    def pack(self) -> bytes:
        """Serialise the RR (header + reporter SSRC + report blocks)."""
        body = struct.pack("!I", self.ssrc) + b"".join(
            b.pack() for b in self.report_blocks
        )
        header = _pack_header(
            count=len(self.report_blocks),
            payload_type=RTCP_PT_RR,
            body_words=len(body) // _WORD,
        )
        return header + body

    @classmethod
    def parse(cls, data: bytes) -> ReceiverReport:
        """Parse a standalone RR packet (RFC 3550 §6.4.2)."""
        header, _ = _parse_header(data, 0)
        if header.payload_type != RTCP_PT_RR:
            msg = f"not a Receiver Report (PT={header.payload_type})"
            raise RtcpError(msg)
        return cls._from_body(header)

    @classmethod
    def _from_body(cls, header: _CommonHeader) -> ReceiverReport:
        """Build an RR from an already-parsed common header + body."""
        body = header.body
        if len(body) < _SSRC_LEN:
            msg = "RR body too short for the reporter SSRC"
            raise RtcpError(msg)
        (ssrc,) = struct.unpack("!I", body[:_SSRC_LEN])
        blocks = _parse_report_blocks(body[_SSRC_LEN:], header.count)
        return cls(ssrc=ssrc, report_blocks=blocks)


def _parse_report_blocks(data: bytes, count: int) -> tuple[ReportBlock, ...]:
    """Parse EXACTLY ``count`` consecutive report blocks from ``data``.

    The RC/SC field fixes the block count, and (padding already stripped by
    :func:`_parse_header`) the region must be exactly ``count * 24`` bytes — we
    advertise no RTP profile that defines a post-block extension, so any bytes
    beyond the declared blocks are a malformed/oversized packet, not silently
    ignored (codex review, ADR-0061).

    Raises:
        RtcpError: If the region is not exactly ``count`` whole blocks.
    """
    needed = count * _REPORT_BLOCK_LEN
    if len(data) != needed:
        msg = (
            f"report-block region has {len(data)} bytes, "
            f"expected exactly {needed} for {count} blocks "
            f"(RC count and length field disagree, or trailing bytes present)"
        )
        raise RtcpError(msg)
    return tuple(
        ReportBlock.parse(data[i * _REPORT_BLOCK_LEN : (i + 1) * _REPORT_BLOCK_LEN])
        for i in range(count)
    )


# ---------------------------------------------------------------------------
# SDES (RFC 3550 §6.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SdesChunk:
    """One SDES chunk: an SSRC and its canonical name (RFC 3550 §6.5).

    Only the mandatory CNAME item (type 1) is modelled — the one item every
    participant must send (§6.5.1). The CNAME identifies the source stably across
    an SSRC collision/change.
    """

    ssrc: int
    cname: str

    def __post_init__(self) -> None:
        """Validate the SSRC range and that the CNAME fits the 8-bit item length."""
        if not 0 <= self.ssrc <= _U32:
            msg = f"ssrc out of range 0..2^32-1: {self.ssrc}"
            raise ValueError(msg)
        encoded = self.cname.encode("ascii")
        if len(encoded) > _U8_MAX:
            msg = f"CNAME exceeds the 255-octet SDES item limit: {len(encoded)} octets"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SourceDescription:
    """An RTCP SDES packet: one CNAME chunk per source (RFC 3550 §6.5)."""

    chunks: tuple[SdesChunk, ...]

    def __post_init__(self) -> None:
        """Validate the chunk count against the 5-bit source count field."""
        if not self.chunks:
            msg = "an SDES packet needs at least one chunk"
            raise ValueError(msg)
        if len(self.chunks) > _MAX_COUNT:
            msg = f"an SDES packet carries at most 31 chunks, got {len(self.chunks)}"
            raise ValueError(msg)

    def pack(self) -> bytes:
        """Serialise the SDES packet (each chunk 32-bit aligned, RFC 3550 §6.5)."""
        body = b"".join(_pack_sdes_chunk(c) for c in self.chunks)
        header = _pack_header(
            count=len(self.chunks),
            payload_type=RTCP_PT_SDES,
            body_words=len(body) // _WORD,
        )
        return header + body

    @classmethod
    def parse(cls, data: bytes) -> SourceDescription:
        """Parse a standalone SDES packet (RFC 3550 §6.5)."""
        header, _ = _parse_header(data, 0)
        if header.payload_type != RTCP_PT_SDES:
            msg = f"not an SDES packet (PT={header.payload_type})"
            raise RtcpError(msg)
        return cls._from_body(header)

    @classmethod
    def _from_body(cls, header: _CommonHeader) -> SourceDescription:
        """Build an SDES from an already-parsed common header + body."""
        chunks: list[SdesChunk] = []
        offset = 0
        body = header.body
        for _ in range(header.count):
            chunk, offset = _parse_sdes_chunk(body, offset)
            chunks.append(chunk)
        if not chunks:
            msg = "SDES packet declared zero chunks"
            raise RtcpError(msg)
        return cls(chunks=tuple(chunks))


def _pack_sdes_chunk(chunk: SdesChunk) -> bytes:
    """Pack one SDES chunk: SSRC + CNAME item + null terminator, padded to 4 bytes."""
    cname = chunk.cname.encode("ascii")
    item = bytes([_SDES_ITEM_CNAME, len(cname)]) + cname
    # RFC 3550 §6.5: the item list is terminated by at least one null octet, then
    # padded with nulls to the next 32-bit boundary.
    raw = struct.pack("!I", chunk.ssrc) + item + b"\x00"
    pad = (-len(raw)) % _WORD
    return raw + b"\x00" * pad


def _parse_sdes_chunk(data: bytes, offset: int) -> tuple[SdesChunk, int]:
    """Parse one SDES chunk starting at ``offset``; return it and the next offset.

    Raises:
        RtcpError: On truncation or a missing CNAME item.
    """
    if len(data) - offset < _WORD:
        msg = "SDES chunk too short for its SSRC"
        raise RtcpError(msg)
    (ssrc,) = struct.unpack("!I", data[offset : offset + _WORD])
    pos = offset + _WORD
    cname: str | None = None
    while pos < len(data):
        item_type = data[pos]
        if item_type == 0:  # null terminator: end of this chunk's item list
            pos += 1
            break
        if len(data) - pos < _SDES_ITEM_HEADER_LEN:
            msg = "SDES item truncated (no length octet)"
            raise RtcpError(msg)
        item_len = data[pos + 1]
        start = pos + _SDES_ITEM_HEADER_LEN
        end = start + item_len
        if end > len(data):
            msg = "SDES item length runs past the packet"
            raise RtcpError(msg)
        if item_type == _SDES_ITEM_CNAME:
            cname = data[start:end].decode("ascii", errors="replace")
        pos = end
    # Advance over the null padding to the next 32-bit boundary.
    pos += (-(pos - offset)) % _WORD
    if cname is None:
        msg = "SDES chunk carried no CNAME item"
        raise RtcpError(msg)
    return SdesChunk(ssrc=ssrc, cname=cname), pos


# ---------------------------------------------------------------------------
# BYE (RFC 3550 §6.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bye:
    """An RTCP BYE: the SSRC(s) leaving and an optional reason (RFC 3550 §6.6)."""

    ssrcs: tuple[int, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        """Validate the SSRC list and the reason length."""
        if not self.ssrcs:
            msg = "a BYE packet needs at least one SSRC"
            raise ValueError(msg)
        if len(self.ssrcs) > _MAX_COUNT:
            msg = f"a BYE packet carries at most 31 SSRCs, got {len(self.ssrcs)}"
            raise ValueError(msg)
        for s in self.ssrcs:
            if not 0 <= s <= _U32:
                msg = f"ssrc out of range 0..2^32-1: {s}"
                raise ValueError(msg)
        if self.reason is not None and len(self.reason.encode("utf-8")) > _U8_MAX:
            msg = "BYE reason exceeds the 255-octet limit"
            raise ValueError(msg)

    def pack(self) -> bytes:
        """Serialise the BYE (SSRC list + optional length-prefixed reason)."""
        body = b"".join(struct.pack("!I", s) for s in self.ssrcs)
        if self.reason is not None:
            reason = self.reason.encode("utf-8")
            raw = bytes([len(reason)]) + reason
            pad = (-len(raw)) % _WORD
            body += raw + b"\x00" * pad
        header = _pack_header(
            count=len(self.ssrcs),
            payload_type=RTCP_PT_BYE,
            body_words=len(body) // _WORD,
        )
        return header + body

    @classmethod
    def parse(cls, data: bytes) -> Bye:
        """Parse a standalone BYE packet (RFC 3550 §6.6)."""
        header, _ = _parse_header(data, 0)
        if header.payload_type != RTCP_PT_BYE:
            msg = f"not a BYE packet (PT={header.payload_type})"
            raise RtcpError(msg)
        return cls._from_body(header)

    @classmethod
    def _from_body(cls, header: _CommonHeader) -> Bye:
        """Build a BYE from an already-parsed common header + body."""
        body = header.body
        needed = header.count * _WORD
        if len(body) < needed:
            msg = "BYE body too short for its SSRC list"
            raise RtcpError(msg)
        ssrcs = tuple(
            struct.unpack("!I", body[i * _WORD : (i + 1) * _WORD])[0]
            for i in range(header.count)
        )
        reason: str | None = None
        if len(body) > needed:
            rlen = body[needed]
            start = needed + 1
            end = start + rlen
            if end > len(body):
                msg = "BYE reason length runs past the packet"
                raise RtcpError(msg)
            reason = body[start:end].decode("utf-8", errors="replace")
        return cls(ssrcs=ssrcs, reason=reason)


# ---------------------------------------------------------------------------
# Compound packets (RFC 3550 §6.1)
# ---------------------------------------------------------------------------

type RtcpPacket = SenderReport | ReceiverReport | SourceDescription | Bye


def build_compound(packets: tuple[RtcpPacket, ...]) -> bytes:
    """Concatenate ``packets`` into one compound RTCP datagram (RFC 3550 §6.1).

    The caller is responsible for the §6.1 ordering rule (lead with an SR or RR,
    then SDES). This just serialises each in turn — every individual ``pack`` is
    already 32-bit aligned, so the compound is too.
    """
    return b"".join(p.pack() for p in packets)


def parse_compound(data: bytes) -> list[RtcpPacket]:
    """Parse a compound RTCP datagram into its individual packets (RFC 3550 §6.1).

    Traverses the buffer by each packet's length field. Packet types we model
    (SR/RR/SDES/BYE) are decoded; any other type (e.g. APP=204, or a profile
    extension) is skipped via its length, never fatal — a receiver must tolerate
    unknown RTCP types in a compound (§6.1).

    Raises:
        RtcpError: On a bad version or a length field that runs past the buffer
            (a structurally broken datagram, which we never silently accept).
    """
    if not data:
        msg = "empty RTCP datagram"
        raise RtcpError(msg)
    packets: list[RtcpPacket] = []
    offset = 0
    while offset < len(data):
        header, offset = _parse_header(data, offset)
        if header.payload_type == RTCP_PT_SR:
            packets.append(SenderReport._from_body(header))
        elif header.payload_type == RTCP_PT_RR:
            packets.append(ReceiverReport._from_body(header))
        elif header.payload_type == RTCP_PT_SDES:
            packets.append(SourceDescription._from_body(header))
        elif header.payload_type == RTCP_PT_BYE:
            packets.append(Bye._from_body(header))
        # else: an unknown/unmodelled RTCP type — already skipped by ``offset``.
    return packets


# ---------------------------------------------------------------------------
# Reception statistics (RFC 3550 Appendix A.1 / A.3 / A.8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReceptionSnapshot:
    """A read-only view of a source's reception statistics (no interval roll).

    Unlike :meth:`ReceptionStats.report_block`, taking a snapshot does NOT advance
    the loss-interval baseline, so a quality poll never disturbs the RTCP report
    cadence. ``cumulative_lost`` and ``expected`` are session totals; ``jitter`` is
    the current smoothed estimate in source clock units.
    """

    expected: int
    received: int
    cumulative_lost: int
    jitter: float
    extended_highest_seq: int


@dataclass(slots=True)
class ReceptionStats:
    """Per-source reception statistics for building RR/SR report blocks.

    Implements the source bookkeeping from RFC 3550 Appendix A.1 (sequence /
    cycle tracking, expected vs received), A.3 (fraction and cumulative loss over
    a report interval), and A.8 (the smoothed interarrival jitter estimate). It is
    fed one call per received RTP packet via :meth:`on_packet`; :meth:`report_block`
    snapshots the current numbers into a :class:`ReportBlock` and rolls the
    interval baseline forward, exactly as a report is emitted.

    The instance is driven entirely by the values passed in (sequence, RTP
    timestamp, arrival time) plus the source clock rate fixed at construction — it
    holds no clock — so it is deterministic.

    Args:
        clock_rate: The source RTP clock rate in Hz (e.g. 8000 for G.711, 16000
            for G.722's RTP clock, 48000 for Opus). Needed per-packet to express
            interarrival jitter in clock units (A.8). Must be positive.
    """

    clock_rate: int = 8000
    _base_seq: int = field(default=0, init=False)
    _max_seq: int = field(default=0, init=False)
    _cycles: int = field(default=0, init=False)
    _received: int = field(default=0, init=False)
    _started: bool = field(default=False, init=False)
    # Loss-interval baselines (A.3): the expected/received counts at the previous
    # report, so a fraction can be computed over just the interval since then.
    _expected_prior: int = field(default=0, init=False)
    _received_prior: int = field(default=0, init=False)
    # Jitter estimate (A.8): the smoothed interarrival jitter in clock units, and
    # the previous packet's (arrival - rtp_timestamp) transit, in clock units.
    _jitter: float = field(default=0.0, init=False)
    _last_transit: float | None = field(default=None, init=False)
    # RFC 3550 Appendix A.1 restart detection: the sequence one past the last
    # out-of-range packet. A second packet equal to it confirms a source restart.
    _bad_seq: int = field(default=_RTP_SEQ_NONE, init=False)

    def __post_init__(self) -> None:
        """Validate the source clock rate."""
        if self.clock_rate <= 0:
            msg = f"clock_rate must be positive, got {self.clock_rate}"
            raise ValueError(msg)

    def _init_seq(self, seq: int) -> None:
        """(Re)base the sequence bookkeeping on ``seq`` (RFC 3550 A.1 init_seq).

        Used at the first packet and on a confirmed source restart: the new packet
        becomes both the base and the highest, cycles reset, and the loss-interval
        baselines re-anchor so loss is counted from the new stream, not the gap.
        """
        self._base_seq = seq
        self._max_seq = seq
        self._cycles = 0
        self._received = 1  # this packet
        self._expected_prior = 0
        self._received_prior = 0
        self._bad_seq = _RTP_SEQ_NONE

    def on_packet(self, *, seq: int, rtp_timestamp: int, arrival_ts: float) -> None:
        """Record one received RTP packet (RFC 3550 Appendix A.1 + A.8).

        Args:
            seq: The packet's 16-bit RTP sequence number.
            rtp_timestamp: The packet's RTP timestamp (source-clock units).
            arrival_ts: Local arrival time in SECONDS (any monotonic origin).

        Sequence handling follows RFC 3550 Appendix A.1 ``update_seq`` exactly,
        including the source-restart path: a forward jump > ``_MAX_DROPOUT`` or a
        backward jump > ``_MAX_MISORDER`` is held as a possible restart (``bad_seq``)
        and only re-bases the stream once a SECOND in-sequence packet confirms it —
        so a same-SSRC sender resync does not leave loss/EHSN wrong forever, and a
        single stray packet does not corrupt a healthy stream.

        Jitter (A.8) is maintained in source RTP clock units, using the clock rate
        fixed at construction. The first packet only establishes the transit
        baseline (J starts at 0, the RFC 3550 warm-up); smoothing begins on the
        second packet.
        """
        if not self._started:
            self._started = True
            self._init_seq(seq)
        else:
            self._update_seq(seq)

        # Interarrival jitter (A.8): transit = arrival (in clock units) - RTP
        # timestamp; D = transit(i) - transit(i-1); J += (|D| - J) / 16.
        transit = arrival_ts * self.clock_rate - rtp_timestamp
        if self._last_transit is not None:
            d = abs(transit - self._last_transit)
            self._jitter += (d - self._jitter) / 16.0
        self._last_transit = transit

    def _update_seq(self, seq: int) -> None:
        """RFC 3550 Appendix A.1 ``update_seq`` (post-init): count + restart detect.

        ``delta`` is the forward distance from the current highest in 16-bit serial
        space. A small forward step (incl. a wrap) advances the highest and counts
        the packet. A jump beyond ``_MAX_DROPOUT`` forward or ``_MAX_MISORDER``
        backward is out of range: if it continues the previous out-of-range packet
        (``seq == bad_seq``) two such in a row confirm a source restart and re-base;
        otherwise it is held as the new ``bad_seq`` and DROPPED from the count. A
        within-tolerance reorder/duplicate is simply counted.
        """
        delta = (seq - self._max_seq) % _SEQ_MOD
        if delta < _MAX_DROPOUT:
            # In order (with a permissible small forward gap = possible loss).
            if seq < self._max_seq:
                self._cycles += _SEQ_MOD  # 16-bit wraparound
            self._max_seq = seq
            self._received += 1
        elif delta <= _SEQ_MOD - _MAX_MISORDER:
            # Far out of range — a large forward jump or far-behind packet. The
            # window (_MAX_DROPOUT, _SEQ_MOD - _MAX_MISORDER] is "the made-a-jump"
            # zone (A.1): treat as a possible restart, confirmed by a successor.
            if seq == self._bad_seq:
                # Two out-of-range packets IN SEQUENCE → the source restarted.
                self._init_seq(seq)
            else:
                self._bad_seq = (seq + 1) & (_SEQ_MOD - 1)
                # Dropped: not counted in _received (it is not a valid packet yet).
        else:
            # A duplicate or a small-misorder reorder within tolerance: count it but
            # do not move the highest-sequence baseline.
            self._received += 1

    def _expected(self) -> int:
        """Total packets expected so far (A.3): extended highest - base + 1."""
        if not self._started:
            return 0
        extended_max = self._cycles + self._max_seq
        return extended_max - self._base_seq + 1

    def snapshot(self) -> ReceptionSnapshot:
        """A read-only statistics view that does NOT roll the loss interval forward.

        Use this to poll quality (e.g. the engine's ``call_quality``) without
        disturbing the per-interval fraction-lost computed by :meth:`report_block`.
        """
        expected = self._expected()
        cumulative = max(_S24_MIN, min(_S24_MAX, expected - self._received))
        return ReceptionSnapshot(
            expected=expected,
            received=self._received,
            cumulative_lost=cumulative,
            jitter=self._jitter,
            extended_highest_seq=(self._cycles + self._max_seq) & _U32,
        )

    def report_block(self, *, source_ssrc: int, lsr: int, dlsr: int) -> ReportBlock:
        """Snapshot the current statistics into a report block (RFC 3550 §6.4.1).

        Computes fraction lost and the interval baselines (A.3), the extended
        highest sequence number (A.1), and the jitter (A.8), then rolls the
        interval baseline forward so the NEXT block's fraction covers only the new
        interval.

        Args:
            source_ssrc: The SSRC this block reports on.
            lsr: Middle-32 NTP of the last SR received from the source (0 if none).
            dlsr: Delay since that SR in 1/65536 s units (0 if none).
        """
        expected = self._expected()
        # Cumulative lost over the whole session (A.3), clamped to signed 24-bit.
        cumulative = expected - self._received
        cumulative = max(_S24_MIN, min(_S24_MAX, cumulative))

        # Fraction lost over the interval since the previous report (A.3).
        expected_interval = expected - self._expected_prior
        received_interval = self._received - self._received_prior
        lost_interval = expected_interval - received_interval
        if expected_interval <= 0 or lost_interval <= 0:
            fraction = 0
        else:
            fraction = (lost_interval << 8) // expected_interval
            fraction = min(fraction, _U8_MAX)
        self._expected_prior = expected
        self._received_prior = self._received

        return ReportBlock(
            ssrc=source_ssrc,
            fraction_lost=fraction,
            cumulative_lost=cumulative,
            extended_highest_seq=(self._cycles + self._max_seq) & _U32,
            jitter=int(self._jitter),
            lsr=lsr & _U32,
            dlsr=dlsr & _U32,
        )


def rtt_from_report_block(*, now_compact_ntp: int, lsr: int, dlsr: int) -> float | None:
    """Round-trip time in seconds from an inbound report block (RFC 3550 §6.4.1).

    The peer echoes, in the block reporting on us, the middle-32 NTP of our last
    SR (``lsr``) and how long it then held the report before sending (``dlsr``, in
    1/65536 s units). RTT = (now - lsr - dlsr), all in the compact 16.16-second
    NTP form.

    Args:
        now_compact_ntp: The current time as the middle-32 bits of a 64-bit NTP
            timestamp (i.e. :func:`to_ntp` then :func:`_ntp_compact`).
        lsr: The ``lsr`` field from the inbound report block.
        dlsr: The ``dlsr`` field from the inbound report block.

    Returns:
        The RTT in seconds, or ``None`` when ``lsr`` is 0 — the peer has not yet
        timed any SR of ours, so no RTT can be computed.
    """
    if lsr == 0:
        return None
    delay = (now_compact_ntp - lsr) & _U32  # wrap-safe 32-bit subtraction
    rtt_units = delay - dlsr
    if rtt_units < 0:
        return 0.0
    return rtt_units / _COMPACT_FRAC_SCALE


def compact_ntp_now(unix_seconds: float) -> int:
    """Return the compact (middle-32) NTP form of ``unix_seconds`` for RTT maths."""
    return _ntp_compact(to_ntp(unix_seconds))


# ---------------------------------------------------------------------------
# Transmission interval (RFC 3550 §6.2, Appendix A.7)
# ---------------------------------------------------------------------------


def compute_rtcp_interval(  # noqa: PLR0913 — the RFC 3550 §6.2 inputs are independent session quantities; all keyword-only
    *,
    members: int,
    senders: int,
    rtcp_bw: float,
    we_sent: bool,
    avg_rtcp_size: float,
    randomize: bool = True,
    rng: random.Random | None = None,
) -> float:
    """The RTCP transmission interval in seconds (RFC 3550 §6.2, Appendix A.7).

    Keeps total RTCP traffic to ~5% of the session bandwidth (``rtcp_bw`` is that
    5% slice, in bytes/s). Senders share 25% of it and receivers 75% so reports
    scale with the number of participants. The result is floored at
    :data:`_RTCP_MIN_TIME` (5 s) and, when ``randomize`` is set, scaled by a
    uniform factor in [0.5, 1.5] and divided by the §6.3.1 compensation constant
    to avoid synchronised reporting.

    Args:
        members: Total participants currently known.
        senders: How many of them are active senders.
        rtcp_bw: The RTCP bandwidth share in bytes/s (typically 5% of the session
            bandwidth). Must be positive.
        we_sent: Whether we have sent RTP since the last report (selects the
            sender vs receiver bandwidth slice for our own next interval).
        avg_rtcp_size: The running average compound-RTCP packet size in bytes
            (including UDP/IP per §6.2). Must be positive.
        randomize: Apply the §6.3.1 randomisation. ``False`` returns the raw
            deterministic interval (used in tests and for the floor check).
        rng: The random source for ``randomize`` (defaults to a module RNG); inject
            a seeded :class:`random.Random` for deterministic tests.

    Returns:
        The interval in seconds (never below the 5 s deterministic minimum once the
        floor and any randomisation are applied).

    Raises:
        ValueError: If ``rtcp_bw`` or ``avg_rtcp_size`` is not positive, or
            ``members``/``senders`` is negative.
    """
    if rtcp_bw <= 0:
        msg = f"rtcp_bw must be positive bytes/s, got {rtcp_bw}"
        raise ValueError(msg)
    if avg_rtcp_size <= 0:
        msg = f"avg_rtcp_size must be positive bytes, got {avg_rtcp_size}"
        raise ValueError(msg)
    if members < 0 or senders < 0:
        msg = f"members/senders must be non-negative, got {members}/{senders}"
        raise ValueError(msg)

    n = members
    bw = rtcp_bw
    # RFC 3550 §6.2: if at least one sender, and senders are a minority (< 25% of
    # members), split the bandwidth so senders use 25% and receivers 75%. Our own
    # interval then uses the slice matching whether WE are currently sending.
    if senders > 0 and senders < members * _RTCP_SENDER_BW_FRACTION:
        if we_sent:
            n = senders
            bw = rtcp_bw * _RTCP_SENDER_BW_FRACTION
        else:
            n = members - senders
            bw = rtcp_bw * (1.0 - _RTCP_SENDER_BW_FRACTION)

    # The deterministic calculated interval (A.7): max(Tmin, n * avg_size / bw).
    interval = avg_rtcp_size * n / bw
    interval = max(interval, _RTCP_MIN_TIME)

    if not randomize:
        return interval

    source = rng if rng is not None else random
    # §6.3.1: multiply by a uniform number in [0.5, 1.5], divide by the
    # compensation factor 1.21828 (= e - 3/2) for the reconsideration algorithm.
    factor = source.uniform(0.5, 1.5)
    return interval * factor / _COMPENSATION
