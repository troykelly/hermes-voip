"""Byte-level KAT tests for the RTCP packet layer (RFC 3550 §6, RFC 5761).

These pin the wire format of Sender Report (SR), Receiver Report (RR), SDES, and
BYE — build → parse round-trips AND fixed expected bytes for a hand-computed
vector — so a regression in field order, width, or the RC/length/padding rules is
caught deterministically. The reception-statistics accumulator (fraction lost,
cumulative lost, extended highest sequence, interarrival jitter, RTT from
LSR/DLSR) and the §6.2 transmission-interval helper are unit-tested against
hand-worked numbers, not "it ran".
"""

from __future__ import annotations

import random
import struct

import pytest

import hermes_voip.rtcp as rtcp_module
from hermes_voip.rtcp import (
    RTCP_PT_BYE,
    RTCP_PT_RR,
    RTCP_PT_SDES,
    RTCP_PT_SR,
    Bye,
    ReceiverReport,
    ReceptionStats,
    ReportBlock,
    RtcpError,
    SdesChunk,
    SenderReport,
    SourceDescription,
    build_compound,
    compute_rtcp_interval,
    from_ntp,
    parse_compound,
    rtt_from_report_block,
    to_ntp,
)

# ---------------------------------------------------------------------------
# NTP timestamp conversion (RFC 3550 §4: 64-bit fixed point, seconds since 1900)
# ---------------------------------------------------------------------------

# Seconds between the NTP epoch (1900-01-01) and the Unix epoch (1970-01-01).
_NTP_UNIX_EPOCH_DELTA = 2_208_988_800


def test_to_ntp_unix_epoch_is_the_delta_seconds() -> None:
    """Unix time 0 is exactly the NTP/Unix epoch delta, with a zero fraction."""
    ntp = to_ntp(0.0)
    assert ntp >> 32 == _NTP_UNIX_EPOCH_DELTA
    assert ntp & 0xFFFFFFFF == 0


def test_to_ntp_half_second_fraction() -> None:
    """A .5 s fraction is the top bit of the 32-bit fractional word (2^31)."""
    ntp = to_ntp(100.5)  # 100.5 s past the Unix epoch
    assert ntp >> 32 == _NTP_UNIX_EPOCH_DELTA + 100
    assert ntp & 0xFFFFFFFF == 1 << 31


def test_ntp_round_trip_is_within_one_lsb() -> None:
    """``from_ntp(to_ntp(t))`` recovers ``t`` to within a 2^-32 s quantum."""
    t = 1_700_000_000.123456
    recovered = from_ntp(to_ntp(t))
    assert abs(recovered - t) < 1.0 / (1 << 32)


def test_to_ntp_rejects_negative_time() -> None:
    """A pre-Unix-epoch time cannot be represented and is a hard error."""
    with pytest.raises(ValueError, match="negative"):
        to_ntp(-1.0)


# ---------------------------------------------------------------------------
# ReportBlock — the 24-byte per-source reception report (RFC 3550 §6.4.1)
# ---------------------------------------------------------------------------


def test_report_block_packs_to_24_bytes_with_exact_fields() -> None:
    """A report block is exactly 24 bytes in the RFC 3550 §6.4.1 field order.

    Hand vector: SSRC=0x11223344, fraction lost=0x80 (half), cumulative lost=1000,
    extended highest seq=0x0001_2345, jitter=42, LSR=0xAABBCCDD, DLSR=0x00010000.
    """
    block = ReportBlock(
        ssrc=0x11223344,
        fraction_lost=0x80,
        cumulative_lost=1000,
        extended_highest_seq=0x00012345,
        jitter=42,
        lsr=0xAABBCCDD,
        dlsr=0x00010000,
    )
    packed = block.pack()
    assert len(packed) == 24
    expected = (
        struct.pack("!I", 0x11223344)
        + bytes([0x80])
        + (1000).to_bytes(3, "big")
        + struct.pack("!I", 0x00012345)
        + struct.pack("!I", 42)
        + struct.pack("!I", 0xAABBCCDD)
        + struct.pack("!I", 0x00010000)
    )
    assert packed == expected


def test_report_block_round_trip() -> None:
    """parse(pack(x)) == x for a representative report block."""
    block = ReportBlock(
        ssrc=0xDEADBEEF,
        fraction_lost=12,
        cumulative_lost=-5,  # signed 24-bit: a duplicate/late surplus (RFC 3550)
        extended_highest_seq=70000,
        jitter=123456,
        lsr=0x12345678,
        dlsr=65536,
    )
    assert ReportBlock.parse(block.pack()) == block


def test_report_block_cumulative_lost_is_signed_24_bit() -> None:
    """Cumulative lost is a signed 24-bit two's-complement field (RFC 3550 §6.4.1).

    A negative count (more packets received than expected, from duplicates)
    must survive the round-trip as the same negative number.
    """
    block = ReportBlock(
        ssrc=1,
        fraction_lost=0,
        cumulative_lost=-1,
        extended_highest_seq=0,
        jitter=0,
        lsr=0,
        dlsr=0,
    )
    packed = block.pack()
    # Layout: SSRC(4) | fraction_lost(1) | cumulative_lost(3). The cumulative
    # field is bytes 5..8; -1 in signed 24-bit two's complement is 0xFFFFFF.
    assert packed[5:8] == b"\xff\xff\xff"
    assert ReportBlock.parse(packed).cumulative_lost == -1


def test_report_block_rejects_out_of_range_fraction() -> None:
    """fraction_lost is one octet (0..255); out of range is a construction error."""
    with pytest.raises(ValueError, match="fraction_lost"):
        ReportBlock(
            ssrc=1,
            fraction_lost=256,
            cumulative_lost=0,
            extended_highest_seq=0,
            jitter=0,
            lsr=0,
            dlsr=0,
        )


def test_report_block_rejects_cumulative_lost_overflow() -> None:
    """cumulative_lost outside signed-24-bit range is rejected (RFC 3550 §6.4.1)."""
    with pytest.raises(ValueError, match="cumulative_lost"):
        ReportBlock(
            ssrc=1,
            fraction_lost=0,
            cumulative_lost=1 << 23,  # 8388608, one past the positive cap
            extended_highest_seq=0,
            jitter=0,
            lsr=0,
            dlsr=0,
        )


# ---------------------------------------------------------------------------
# Sender Report (RFC 3550 §6.4.1)
# ---------------------------------------------------------------------------


def test_sender_report_header_and_sender_info_bytes() -> None:
    """A no-report-block SR packs as a 28-byte packet with the exact header.

    Header: V=2,P=0,RC=0 → 0x80; PT=200; length in 32-bit words minus one = 6
    (28 bytes = 7 words). Sender info: SSRC, NTP (8 bytes), RTP ts, packet count,
    octet count.
    """
    sr = SenderReport(
        ssrc=0x0A0B0C0D,
        ntp_timestamp=(_NTP_UNIX_EPOCH_DELTA << 32) | (1 << 31),
        rtp_timestamp=160000,
        packet_count=1000,
        octet_count=160000,
        report_blocks=(),
    )
    packed = sr.pack()
    assert len(packed) == 28
    assert packed[0] == 0x80  # V=2, P=0, RC=0
    assert packed[1] == RTCP_PT_SR  # 200
    assert struct.unpack("!H", packed[2:4])[0] == 6  # (28/4) - 1
    assert struct.unpack("!I", packed[4:8])[0] == 0x0A0B0C0D
    ntp = struct.unpack("!Q", packed[8:16])[0]
    assert ntp == (_NTP_UNIX_EPOCH_DELTA << 32) | (1 << 31)
    assert struct.unpack("!I", packed[16:20])[0] == 160000
    assert struct.unpack("!I", packed[20:24])[0] == 1000
    assert struct.unpack("!I", packed[24:28])[0] == 160000


def test_sender_report_with_one_block_sets_rc_and_length() -> None:
    """One report block bumps RC to 1 and length to 12 words (28 + 24 = 52 bytes)."""
    block = ReportBlock(
        ssrc=0x99887766,
        fraction_lost=0,
        cumulative_lost=0,
        extended_highest_seq=500,
        jitter=10,
        lsr=0x11112222,
        dlsr=0x00003333,
    )
    sr = SenderReport(
        ssrc=1,
        ntp_timestamp=0,
        rtp_timestamp=0,
        packet_count=0,
        octet_count=0,
        report_blocks=(block,),
    )
    packed = sr.pack()
    assert len(packed) == 52
    assert packed[0] & 0x1F == 1  # RC = 1
    assert struct.unpack("!H", packed[2:4])[0] == 12  # (52/4) - 1


def test_sender_report_round_trip_with_two_blocks() -> None:
    """parse(pack(sr)) == sr for an SR carrying two report blocks."""
    blocks = (
        ReportBlock(
            ssrc=0xAAAA0001,
            fraction_lost=4,
            cumulative_lost=20,
            extended_highest_seq=12345,
            jitter=99,
            lsr=0xDEAD0000,
            dlsr=0x00010001,
        ),
        ReportBlock(
            ssrc=0xBBBB0002,
            fraction_lost=0,
            cumulative_lost=0,
            extended_highest_seq=54321,
            jitter=1,
            lsr=0xBEEF0000,
            dlsr=0x00020002,
        ),
    )
    sr = SenderReport(
        ssrc=0xCAFEBABE,
        ntp_timestamp=0x123456789ABCDEF0,
        rtp_timestamp=987654,
        packet_count=4321,
        octet_count=691360,
        report_blocks=blocks,
    )
    assert SenderReport.parse(sr.pack()) == sr


# ---------------------------------------------------------------------------
# Receiver Report (RFC 3550 §6.4.2)
# ---------------------------------------------------------------------------


def test_receiver_report_empty_is_8_bytes() -> None:
    """A receive-only endpoint with no sources yet emits an empty RR (RFC 3550).

    8 bytes: header (4) + the reporter SSRC (4). PT=201, RC=0, length=1 word.
    """
    rr = ReceiverReport(ssrc=0x01020304, report_blocks=())
    packed = rr.pack()
    assert len(packed) == 8
    assert packed[0] == 0x80  # V=2, P=0, RC=0
    assert packed[1] == RTCP_PT_RR  # 201
    assert struct.unpack("!H", packed[2:4])[0] == 1  # (8/4) - 1
    assert struct.unpack("!I", packed[4:8])[0] == 0x01020304


def test_receiver_report_round_trip_with_block() -> None:
    """parse(pack(rr)) == rr for an RR with one report block."""
    block = ReportBlock(
        ssrc=0x55667788,
        fraction_lost=64,
        cumulative_lost=7,
        extended_highest_seq=2000,
        jitter=512,
        lsr=0xCAFE0000,
        dlsr=0x00004000,
    )
    rr = ReceiverReport(ssrc=0x0BADF00D, report_blocks=(block,))
    assert ReceiverReport.parse(rr.pack()) == rr


# ---------------------------------------------------------------------------
# SDES (RFC 3550 §6.5) — the CNAME chunk
# ---------------------------------------------------------------------------


def test_sdes_cname_chunk_round_trip() -> None:
    """parse(pack(sdes)) == sdes for a single CNAME chunk."""
    sdes = SourceDescription(
        chunks=(SdesChunk(ssrc=0xCAFEBABE, cname="hermes@host.invalid"),)
    )
    assert SourceDescription.parse(sdes.pack()) == sdes


def test_sdes_packet_is_32_bit_aligned_and_null_terminated() -> None:
    """An SDES chunk is null-terminated and zero-padded to a 32-bit boundary.

    RFC 3550 §6.5: the item list ends with a single null octet, then the chunk is
    padded with nulls to the next 32-bit boundary. Total packet length must be a
    whole number of 32-bit words.
    """
    sdes = SourceDescription(chunks=(SdesChunk(ssrc=1, cname="ab"),))
    packed = sdes.pack()
    assert len(packed) % 4 == 0
    assert packed[1] == RTCP_PT_SDES  # 202
    # The CNAME item: type 1, length 2, "ab", then the terminating null.
    # chunk = SSRC(4) + [1][2]["ab"](4) + null(1) → 9 bytes → padded to 12.
    body = packed[4:]
    assert body[4] == 1  # SDES item type CNAME
    assert body[5] == 2  # CNAME length
    assert body[6:8] == b"ab"
    assert body[8] == 0  # terminating null item


def test_sdes_rejects_non_ascii_cname() -> None:
    """A CNAME must be representable on the wire; non-encodable input is an error."""
    with pytest.raises((ValueError, UnicodeEncodeError)):
        SourceDescription(chunks=(SdesChunk(ssrc=1, cname="café" * 100),)).pack()


def test_parse_compound_sdes_utf8_cname_roundtrips() -> None:
    """A structurally-valid SDES CNAME carrying multibyte UTF-8 round-trips (§6.5).

    RFC 3550 §6.5: SDES item text is UTF-8. Browsers/Asterisk/FreeSWITCH routinely
    send a UTF-8 CNAME alongside the SR/RR in a compound. The CNAME path must be
    UTF-8 end-to-end, so a valid non-ASCII CNAME must NOT raise UnicodeEncodeError
    on construction nor be mangled to U+FFFD on parse.
    """
    # Build the on-wire CNAME bytes directly: "h" + U+00E9 (2-byte 0xC3 0xA9) + "y".
    cname_bytes = b"h\xc3\xa9y"
    cname_text = cname_bytes.decode("utf-8")
    item = bytes([1, len(cname_bytes)]) + cname_bytes  # type=CNAME(1), len, text
    raw = struct.pack("!I", 0xCAFEBABE) + item + b"\x00"
    raw += b"\x00" * ((-len(raw)) % 4)  # pad chunk to a 32-bit boundary
    header = bytes([0x80 | 1, RTCP_PT_SDES]) + struct.pack("!H", len(raw) // 4)
    wire = header + raw

    parsed = parse_compound(wire)
    assert len(parsed) == 1
    sdes = parsed[0]
    assert isinstance(sdes, SourceDescription)
    assert sdes.chunks[0].ssrc == 0xCAFEBABE
    assert sdes.chunks[0].cname == cname_text
    # And it survives a full re-pack/parse round-trip with the multibyte CNAME.
    assert SourceDescription.parse(sdes.pack()) == sdes


def test_sdes_cname_over_255_utf8_bytes_is_error() -> None:
    """A CNAME whose UTF-8 encoding exceeds 255 octets is rejected (§6.5, 8-bit len).

    The 255-octet cap is on the UTF-8 BYTE length, not the character count: 86 copies
    of U+00E9 (2 bytes each = 172 bytes) fit, but 128 copies (256 bytes) do not.
    """
    SdesChunk(ssrc=1, cname="é" * 86)  # 172 octets: within the 255-octet limit
    with pytest.raises((ValueError, RtcpError)):
        SdesChunk(ssrc=1, cname="é" * 128)  # 256 octets: over the limit


# ---------------------------------------------------------------------------
# BYE (RFC 3550 §6.6)
# ---------------------------------------------------------------------------


def test_bye_round_trip_no_reason() -> None:
    """parse(pack(bye)) == bye for a BYE listing one SSRC, no reason."""
    bye = Bye(ssrcs=(0xCAFEBABE,), reason=None)
    packed = bye.pack()
    assert packed[1] == RTCP_PT_BYE  # 203
    assert packed[0] & 0x1F == 1  # SC = 1
    assert Bye.parse(packed) == bye


def test_bye_round_trip_with_reason() -> None:
    """A BYE reason string round-trips (length-prefixed, padded to 32 bits)."""
    bye = Bye(ssrcs=(0xAABBCCDD, 0x11223344), reason="call ended")
    packed = bye.pack()
    assert len(packed) % 4 == 0
    assert packed[0] & 0x1F == 2  # SC = 2
    assert Bye.parse(packed) == bye


# ---------------------------------------------------------------------------
# Compound packets (RFC 3550 §6.1)
# ---------------------------------------------------------------------------


def test_build_compound_sr_then_sdes_round_trips() -> None:
    """A compound SR + SDES builds and parses back to the same packet sequence.

    RFC 3550 §6.1: every compound RTCP packet begins with an SR or RR and SHOULD
    carry an SDES with a CNAME. parse_compound returns the packets in order.
    """
    sr = SenderReport(
        ssrc=0xCAFEBABE,
        ntp_timestamp=to_ntp(1_700_000_000.5),
        rtp_timestamp=8000,
        packet_count=50,
        octet_count=8000,
        report_blocks=(),
    )
    sdes = SourceDescription(
        chunks=(SdesChunk(ssrc=0xCAFEBABE, cname="hermes@host.invalid"),)
    )
    wire = build_compound((sr, sdes))
    parsed = parse_compound(wire)
    assert parsed == [sr, sdes]


def test_build_compound_rr_then_sdes_then_bye() -> None:
    """A three-element compound (RR, SDES, BYE) round-trips in order."""
    rr = ReceiverReport(
        ssrc=0x0BADF00D,
        report_blocks=(
            ReportBlock(
                ssrc=0x12345678,
                fraction_lost=0,
                cumulative_lost=0,
                extended_highest_seq=100,
                jitter=5,
                lsr=0x11112222,
                dlsr=0x00000100,
            ),
        ),
    )
    sdes = SourceDescription(
        chunks=(SdesChunk(ssrc=0x0BADF00D, cname="rx@host.invalid"),)
    )
    bye = Bye(ssrcs=(0x0BADF00D,), reason=None)
    wire = build_compound((rr, sdes, bye))
    assert parse_compound(wire) == [rr, sdes, bye]


def test_parse_compound_rejects_non_rtcp_version() -> None:
    """A first byte without RTP version 2 is not RTCP and is rejected."""
    with pytest.raises(RtcpError, match="version"):
        parse_compound(b"\x00\xc8\x00\x06" + b"\x00" * 24)


def test_parse_compound_rejects_truncated_packet() -> None:
    """A length field running past the buffer is a hard parse error, not silent."""
    # Claims length 6 words (28 bytes) but only 8 bytes are present.
    with pytest.raises(RtcpError):
        parse_compound(b"\x80\xc8\x00\x06\x00\x00\x00\x01")


def test_parse_compound_ignores_unknown_payload_type() -> None:
    """An unknown RTCP PT (e.g. APP=204) in a compound is skipped, not fatal.

    RFC 3550 §6.1 + profile extensibility: a receiver must traverse the whole
    compound using the length fields and ignore packet types it does not parse,
    not choke. The known SR before it must still be returned.
    """
    sr = SenderReport(
        ssrc=1,
        ntp_timestamp=0,
        rtp_timestamp=0,
        packet_count=0,
        octet_count=0,
        report_blocks=(),
    )
    # A minimal APP packet (PT=204): header word + SSRC + 4-byte name, length=2.
    app = struct.pack("!BBHI", 0x80, 204, 2, 0xDEADBEEF) + b"TEST"
    wire = sr.pack() + app
    parsed = parse_compound(wire)
    assert parsed == [sr]


# ---------------------------------------------------------------------------
# ReceptionStats — RFC 3550 Appendix A.1 / A.3 / A.8
# ---------------------------------------------------------------------------


def test_reception_stats_counts_and_extended_highest_seq() -> None:
    """A clean run of N in-order packets reports N received and the right EHSN.

    First packet seq=1000; after 5 in-order packets the extended highest sequence
    is 1004 (no wrap, cycles=0) and the report block shows zero loss.
    """
    stats = ReceptionStats(clock_rate=8000)
    for i, seq in enumerate((1000, 1001, 1002, 1003, 1004)):
        # Arrival times and RTP timestamps advance one 20 ms G.711 frame each.
        stats.on_packet(seq=seq, rtp_timestamp=160 * i, arrival_ts=0.02 * i)
    block = stats.report_block(source_ssrc=0xABCD, lsr=0, dlsr=0)
    assert block.extended_highest_seq == 1004
    assert block.cumulative_lost == 0
    assert block.fraction_lost == 0


def test_reception_stats_fraction_lost_one_in_four() -> None:
    """Dropping one of four packets in an interval yields fraction lost == 64/256.

    RFC 3550 A.3: fraction lost is the 8-bit fixed-point ratio of lost to expected
    over the interval since the last report. Second interval expects 4 packets
    (2,3,4,5) but 4 is lost → 1 of 4 lost → 256 * 1/4 = 64.
    """
    stats = ReceptionStats(clock_rate=8000)
    # First report interval: a clean baseline (seq 1).
    stats.on_packet(seq=1, rtp_timestamp=0, arrival_ts=0.0)
    stats.report_block(source_ssrc=1, lsr=0, dlsr=0)
    # Second interval: expect 2,3,4,5 (4 packets) but 4 is lost → 1 of 4 lost.
    stats.on_packet(seq=2, rtp_timestamp=160, arrival_ts=0.02)
    stats.on_packet(seq=3, rtp_timestamp=320, arrival_ts=0.04)
    stats.on_packet(seq=5, rtp_timestamp=640, arrival_ts=0.08)
    block = stats.report_block(source_ssrc=1, lsr=0, dlsr=0)
    assert block.fraction_lost == 64  # 256 * 1/4
    assert block.cumulative_lost == 1


def test_reception_stats_handles_sequence_wrap() -> None:
    """Crossing the 16-bit sequence boundary increments the cycle count (A.1).

    seq 65534, 65535, 0, 1 is a clean run across the wrap: extended highest
    sequence = (1 cycle << 16) | 1, and no loss is declared.
    """
    stats = ReceptionStats(clock_rate=8000)
    for i, seq in enumerate((65534, 65535, 0, 1)):
        stats.on_packet(seq=seq, rtp_timestamp=160 * i, arrival_ts=0.02 * i)
    block = stats.report_block(source_ssrc=1, lsr=0, dlsr=0)
    assert block.extended_highest_seq == (1 << 16) | 1
    assert block.cumulative_lost == 0


def test_reception_stats_interarrival_jitter_zero_for_isochronous() -> None:
    """Perfectly isochronous arrival (transit constant) yields zero jitter (A.8).

    When every packet's transit time D is identical, the smoothed jitter estimate
    converges to (and starts at) zero.
    """
    stats = ReceptionStats(clock_rate=8000)
    # arrival advances 20 ms, RTP timestamp advances 160 (= 20 ms at 8 kHz): the
    # transit time is constant, so D(i-1,i) == 0 for every pair → jitter 0.
    for i, seq in enumerate(range(100, 110)):
        stats.on_packet(seq=seq, rtp_timestamp=160 * i, arrival_ts=0.02 * i)
    block = stats.report_block(source_ssrc=1, lsr=0, dlsr=0)
    assert block.jitter == 0


def test_reception_stats_interarrival_jitter_positive_when_arrival_varies() -> None:
    """A late packet inflates the smoothed jitter estimate above zero (A.8)."""
    stats = ReceptionStats(clock_rate=8000)
    stats.on_packet(seq=1, rtp_timestamp=0, arrival_ts=0.0)
    stats.on_packet(seq=2, rtp_timestamp=160, arrival_ts=0.02)
    # Third packet arrives 30 ms late (0.05 s gap vs the expected 0.02 s).
    stats.on_packet(seq=3, rtp_timestamp=320, arrival_ts=0.07)
    block = stats.report_block(source_ssrc=1, lsr=0, dlsr=0)
    assert block.jitter > 0


def test_reception_stats_jitter_in_rtp_clock_units() -> None:
    """Jitter is reported in RTP timestamp units of the source clock (RFC 3550 A.8).

    One packet 30 ms late at an 8 kHz clock contributes a transit delta of
    0.03 s * 8000 = 240 clock units; the first non-zero jitter sample is that
    delta / 16 (the §A.8 gain). |D| = 240, J += (|D| - J)/16 from J=0 → 15.
    """
    stats = ReceptionStats(clock_rate=8000)
    stats.on_packet(seq=1, rtp_timestamp=0, arrival_ts=0.0)
    stats.on_packet(seq=2, rtp_timestamp=160, arrival_ts=0.05)  # 30 ms late
    block = stats.report_block(source_ssrc=1, lsr=0, dlsr=0)
    assert block.jitter == 15


# ---------------------------------------------------------------------------
# RTT from LSR/DLSR (RFC 3550 §6.4.1)
# ---------------------------------------------------------------------------


def test_rtt_from_report_block_basic() -> None:
    """RTT = (now - LSR - DLSR) in seconds, using the compact NTP clock.

    The peer last received our SR whose middle-32 NTP was LSR; it then waited
    DLSR (1/65536 s units) before sending this report. With now - LSR == 0.5 s
    (in 1/65536 units) and DLSR == 0.1 s, RTT == 0.4 s.
    """
    # A non-zero LSR (LSR == 0 is the "no SR acknowledged" sentinel). Compact NTP
    # is 16.16 fixed-point seconds; pick an arbitrary base time.
    lsr = 1000 << 16  # middle 32 bits of our SR's send NTP time = 1000.0 s
    # "now" expressed in the same compact NTP form: 0.5 s after the LSR.
    now_compact = lsr + int(0.5 * (1 << 16))
    dlsr = int(0.1 * (1 << 16))
    rtt = rtt_from_report_block(now_compact_ntp=now_compact, lsr=lsr, dlsr=dlsr)
    assert rtt is not None
    assert abs(rtt - 0.4) < 1e-3


def test_rtt_zero_when_no_sr_acknowledged() -> None:
    """LSR == 0 means the peer has not yet timed any SR of ours: RTT is None."""
    rtt = rtt_from_report_block(now_compact_ntp=12345, lsr=0, dlsr=0)
    assert rtt is None


# ---------------------------------------------------------------------------
# RTCP transmission interval (RFC 3550 §6.2, Appendix A.7)
# ---------------------------------------------------------------------------


def test_rtcp_interval_respects_minimum() -> None:
    """The computed interval is never below the RFC 3550 minimum (5 s deterministic).

    With a tiny session bandwidth the bandwidth share would suggest a sub-second
    interval, but §6.2 floors the deterministic interval at 5 s (here we ask for
    the un-randomised value).
    """
    interval = compute_rtcp_interval(
        members=2,
        senders=1,
        rtcp_bw=1000.0,  # bytes/s — small
        we_sent=True,
        avg_rtcp_size=100.0,
        randomize=False,
    )
    assert interval >= 5.0


def test_rtcp_interval_scales_with_members() -> None:
    """More members → a longer interval (the ~5% bandwidth rule, RFC 3550 §6.2).

    Members are all senders (no §6.2 sender/receiver split), and the bandwidth /
    size are chosen so both deterministic values clear the 5 s floor — so the test
    pins the linear ``n * size / bw`` scaling, not the floor.
    """
    small = compute_rtcp_interval(
        members=100,
        senders=100,
        rtcp_bw=10_000.0,
        we_sent=True,
        avg_rtcp_size=1000.0,
        randomize=False,
    )  # 1000 * 100 / 10000 = 10 s
    large = compute_rtcp_interval(
        members=300,
        senders=300,
        rtcp_bw=10_000.0,
        we_sent=True,
        avg_rtcp_size=1000.0,
        randomize=False,
    )  # 1000 * 300 / 10000 = 30 s
    assert large > small
    assert small >= 5.0  # both above the floor, so this exercises the scaling


def test_rtcp_interval_randomization_within_compensated_band() -> None:
    """Randomised interval stays in the §6.3.1 compensated band of deterministic.

    The randomised value is the deterministic one scaled by a uniform factor in
    [0.5, 1.5] and divided by the 1.21828 compensation constant. We drive the RNG
    with a fixed seed and assert the bound holds across draws.
    """
    rng = random.Random(1234)  # noqa: S311 — RFC 3550 jitter randomization is not cryptographic; seeded for a deterministic test
    det = compute_rtcp_interval(
        members=10,
        senders=5,
        rtcp_bw=50_000.0,
        we_sent=True,
        avg_rtcp_size=120.0,
        randomize=False,
    )
    for _ in range(100):
        r = compute_rtcp_interval(
            members=10,
            senders=5,
            rtcp_bw=50_000.0,
            we_sent=True,
            avg_rtcp_size=120.0,
            randomize=True,
            rng=rng,
        )
        # The /1.21828 compensation means the randomised interval spans
        # [0.5/1.21828, 1.5/1.21828] x det ≈ [0.41, 1.23] x det.
        assert 0.40 * det <= r <= 1.24 * det


def test_rtcp_interval_receiver_uses_receiver_fraction() -> None:
    """A non-sender computes its interval from the 75% receiver bandwidth slice.

    RFC 3550 §6.2: senders share 25% of the RTCP bandwidth, receivers 75%. With
    senders a minority, a receiver (we_sent=False) gets a DIFFERENT interval than
    the same node would as a sender — the function must branch on we_sent.
    """
    # senders (2) < members*0.25 (25) → the §6.2 split applies. size/bw chosen so
    # the receiver slice clears the 5 s floor and the two slices differ.
    as_sender = compute_rtcp_interval(
        members=100,
        senders=2,
        rtcp_bw=10_000.0,
        we_sent=True,
        avg_rtcp_size=1000.0,
        randomize=False,
    )  # n=2, bw=2500 → 0.8 s → floored to 5 s
    as_receiver = compute_rtcp_interval(
        members=100,
        senders=2,
        rtcp_bw=10_000.0,
        we_sent=False,
        avg_rtcp_size=1000.0,
        randomize=False,
    )  # n=98, bw=7500 → ~13.07 s
    assert as_sender != as_receiver
    assert as_receiver > as_sender


# ---------------------------------------------------------------------------
# Parser strictness: padding (RFC 3550 §6.4.1 P bit) + exact RC/length
# (codex review, ADR-0061)
# ---------------------------------------------------------------------------


def test_parse_strips_rtcp_padding() -> None:
    """A P-bit RTCP packet's trailing pad octets are removed, not parsed as body.

    RFC 3550 §6.4.1: when the padding bit is set, the last octet is the pad count
    (including itself) and those octets are NOT part of the packet's fields. Here an
    empty RR (8 bytes) is padded with 4 octets (last = 4); parsing must recover the
    same RR, not choke on or mis-read the 4 pad bytes.
    """
    rr = ReceiverReport(ssrc=0x01020304, report_blocks=())
    base = rr.pack()  # 8 bytes, length word = 1
    # Append 4 pad bytes (0,0,0,4), set P bit, bump the length word from 1 to 3.
    padded = bytearray(base + b"\x00\x00\x00\x04")
    padded[0] |= 0x20  # set the padding bit
    # length in words minus one: (12 bytes / 4) - 1 = 2.
    padded[2:4] = struct.pack("!H", 2)
    parsed = parse_compound(bytes(padded))
    assert parsed == [rr]


def test_parse_rejects_trailing_garbage_after_report_blocks() -> None:
    """A packet whose length covers MORE than its RC report blocks is malformed.

    RFC 3550: the length field is authoritative and RC fixes the block count. A
    declared length covering bytes beyond (RC report blocks + sender/SSRC), with no
    padding bit to account for them, is a structurally broken packet — reject it
    rather than silently ignore the extra bytes.
    """
    rr = ReceiverReport(ssrc=1, report_blocks=())  # RC=0, body = 4-byte SSRC
    base = bytearray(rr.pack())
    # Append 4 non-padding bytes and bump the length, WITHOUT setting the P bit.
    broken = base + b"\xde\xad\xbe\xef"
    broken[2:4] = struct.pack("!H", (len(broken) // 4) - 1)
    with pytest.raises(RtcpError):
        parse_compound(bytes(broken))


# ---------------------------------------------------------------------------
# ReceptionStats source-restart handling (RFC 3550 Appendix A.1, codex review)
# ---------------------------------------------------------------------------


def test_reception_stats_recovers_after_source_sequence_restart() -> None:
    """A same-SSRC sender restart to a far sequence is recovered (RFC 3550 A.1).

    After a clean run, the source restarts at a wildly different random sequence
    (a re-INVITE/codec reset on the same SSRC). Appendix A.1 confirms a restart
    once two packets arrive in sequence from the new base, then re-bases — so the
    extended highest sequence and loss track the NEW stream rather than declaring
    ~65000 packets lost forever.
    """
    stats = ReceptionStats(clock_rate=8000)
    for i, seq in enumerate((1000, 1001, 1002)):
        stats.on_packet(seq=seq, rtp_timestamp=160 * i, arrival_ts=0.02 * i)
    # Restart far away (a big jump, treated as a possible restart, not loss).
    stats.on_packet(seq=40000, rtp_timestamp=0, arrival_ts=1.0)
    stats.on_packet(seq=40001, rtp_timestamp=160, arrival_ts=1.02)  # confirms restart
    stats.on_packet(seq=40002, rtp_timestamp=320, arrival_ts=1.04)
    block = stats.report_block(source_ssrc=1, lsr=0, dlsr=0)
    # The highest sequence now tracks the NEW stream, not the stale 1002 nor a vast
    # cycle count — restart re-based the baseline.
    assert block.extended_highest_seq == 40002
    # Cumulative loss is bounded (the restart is not counted as ~39000 lost).
    assert block.cumulative_lost < 100


def test_stray_out_of_range_packet_does_not_poison_jitter() -> None:
    """A far out-of-range (unvalidated) packet must not update jitter (RFC 3550 A.1).

    A.1 update_seq() returns false for a far-sequence packet held as a possible
    restart; the caller must NOT fold it into reception-dependent stats. Here a clean
    isochronous run yields zero jitter; injecting ONE stray far-sequence packet (with
    a wildly wrong RTP timestamp) between them must leave the jitter estimate exactly
    where the clean stream put it — the stray packet's bogus transit is ignored.
    """
    clean = ReceptionStats(clock_rate=8000)
    stray = ReceptionStats(clock_rate=8000)
    seqs = list(range(100, 110))
    for i, seq in enumerate(seqs):
        clean.on_packet(seq=seq, rtp_timestamp=160 * i, arrival_ts=0.02 * i)
        stray.on_packet(seq=seq, rtp_timestamp=160 * i, arrival_ts=0.02 * i)
        if seq == 104:
            # One stray far-sequence packet with a nonsense RTP timestamp/arrival:
            # A.1 holds it (bad_seq), so it must not perturb jitter.
            stray.on_packet(seq=40000, rtp_timestamp=999_999, arrival_ts=0.021)
    clean_block = clean.report_block(source_ssrc=1, lsr=0, dlsr=0)
    stray_block = stray.report_block(source_ssrc=1, lsr=0, dlsr=0)
    assert stray_block.jitter == clean_block.jitter == 0


def test_module_constants_pinned_values() -> None:
    """Pin the documented numeric contract between module constants.

    ``typing.Final`` is enforced statically by mypy --strict (not at runtime);
    these assertions are value-typo regression guards, not behavioural tests.
    They catch a transcription error in any constant and pin the cross-constant
    relationships the code relies on.
    """
    # RTCP packet types are consecutive (RFC 3550 §12.1 / IANA): SR=200, RR=201,
    # SDES=202, BYE=203, APP=204.
    assert rtcp_module.RTCP_PT_SR == 200
    assert rtcp_module.RTCP_PT_RR == rtcp_module.RTCP_PT_SR + 1
    assert rtcp_module.RTCP_PT_SDES == rtcp_module.RTCP_PT_RR + 1
    assert rtcp_module.RTCP_PT_BYE == rtcp_module.RTCP_PT_SDES + 1
    assert rtcp_module.RTCP_PT_APP == rtcp_module.RTCP_PT_BYE + 1

    # SDES CNAME is item type 1 (RFC 3550 §6.5.1); item type 0 is END.
    assert rtcp_module._SDES_ITEM_CNAME == 1

    # Version and header bitfields: V=2 in top 2 bits (shift 6), padding in bit 5,
    # count in low 5 bits; they must not overlap.
    assert rtcp_module._RTP_VERSION == 2
    assert rtcp_module._VERSION_SHIFT == 6
    assert rtcp_module._COUNT_MASK == 0x1F  # low 5 bits
    assert rtcp_module._PADDING_BIT == 0x20  # bit 5
    assert rtcp_module._MAX_COUNT == rtcp_module._COUNT_MASK  # same 5-bit field
    assert (rtcp_module._COUNT_MASK & rtcp_module._PADDING_BIT) == 0  # no overlap

    # Field-width masks: unsigned byte, word, double-word, and signed 24-bit range.
    assert rtcp_module._U8_MAX == 0xFF
    assert rtcp_module._U32 == 0xFFFFFFFF
    assert rtcp_module._U64 == (1 << 64) - 1
    assert rtcp_module._S24_MIN == -(rtcp_module._S24_MAX + 1)  # symmetric 24-bit range
    assert rtcp_module._S24_MAX == (1 << 23) - 1

    # Length constants: WORD=4; SSRC and common header are both one word; report
    # block is 6 words (24 bytes); sender info is 5 words (20 bytes).
    assert rtcp_module._WORD == 4
    assert rtcp_module._SSRC_LEN == rtcp_module._WORD
    assert rtcp_module._COMMON_HEADER_LEN == rtcp_module._WORD
    assert rtcp_module._REPORT_BLOCK_LEN == 6 * rtcp_module._WORD
    assert rtcp_module._SENDER_INFO_LEN == 5 * rtcp_module._WORD
    assert rtcp_module._SDES_ITEM_HEADER_LEN == 2  # type(1) + length(1)

    # NTP conversion: the fractional scale is 2^32; the compact (LSR/DLSR) scale
    # is 2^16 — exactly the square root of the full scale.
    assert rtcp_module._FRAC_SCALE == 1 << 32
    assert rtcp_module._COMPACT_FRAC_SCALE == 1 << 16
    assert (
        rtcp_module._COMPACT_FRAC_SCALE * rtcp_module._COMPACT_FRAC_SCALE
        == rtcp_module._FRAC_SCALE
    )
    assert rtcp_module._NTP_UNIX_EPOCH_DELTA == 2_208_988_800  # 70 years in seconds

    # RFC 3550 timing: sender bandwidth fraction is less than 1; compensation factor
    # approximates e - 3/2 and must be positive.
    assert 0.0 < rtcp_module._RTCP_SENDER_BW_FRACTION < 1.0
    assert rtcp_module._RTCP_MIN_TIME > 0.0
    assert rtcp_module._COMPENSATION > 1.0  # e - 3/2 ≈ 1.218

    # Sequence-validity thresholds: dropout window is much larger than misorder
    # window (RFC 3550 App. A.1), and both are less than the full modulus.
    assert rtcp_module._SEQ_MOD == 1 << 16
    assert rtcp_module._MAX_DROPOUT > rtcp_module._MAX_MISORDER
    assert rtcp_module._MAX_DROPOUT < rtcp_module._SEQ_MOD
    # _RTP_SEQ_NONE must be outside the valid 16-bit space so it can never match.
    assert rtcp_module._RTP_SEQ_NONE > rtcp_module._SEQ_MOD - 1
