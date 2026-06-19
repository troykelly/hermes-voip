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
    # -1 in signed 24-bit two's complement is 0xFFFFFF.
    assert packed[4:7] == b"\xff\xff\xff"
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
    stats = ReceptionStats()
    for i, seq in enumerate((1000, 1001, 1002, 1003, 1004)):
        # Arrival times and RTP timestamps advance one 20 ms G.711 frame each.
        stats.on_packet(seq=seq, rtp_timestamp=160 * i, arrival_ts=0.02 * i)
    block = stats.report_block(source_ssrc=0xABCD, lsr=0, dlsr=0, clock_rate=8000)
    assert block.extended_highest_seq == 1004
    assert block.cumulative_lost == 0
    assert block.fraction_lost == 0


def test_reception_stats_fraction_lost_one_in_four() -> None:
    """Dropping one of four packets in an interval yields fraction lost == 64/256.

    RFC 3550 A.3: fraction lost is the 8-bit fixed-point ratio of lost to expected
    over the interval since the last report. Second interval expects 4 packets
    (2,3,4,5) but 4 is lost → 1 of 4 lost → 256 * 1/4 = 64.
    """
    stats = ReceptionStats()
    # First report interval: a clean baseline (seq 1).
    stats.on_packet(seq=1, rtp_timestamp=0, arrival_ts=0.0)
    stats.report_block(source_ssrc=1, lsr=0, dlsr=0, clock_rate=8000)
    # Second interval: expect 2,3,4,5 (4 packets) but 4 is lost → 1 of 4 lost.
    stats.on_packet(seq=2, rtp_timestamp=160, arrival_ts=0.02)
    stats.on_packet(seq=3, rtp_timestamp=320, arrival_ts=0.04)
    stats.on_packet(seq=5, rtp_timestamp=640, arrival_ts=0.08)
    block = stats.report_block(source_ssrc=1, lsr=0, dlsr=0, clock_rate=8000)
    assert block.fraction_lost == 64  # 256 * 1/4
    assert block.cumulative_lost == 1


def test_reception_stats_handles_sequence_wrap() -> None:
    """Crossing the 16-bit sequence boundary increments the cycle count (A.1).

    seq 65534, 65535, 0, 1 is a clean run across the wrap: extended highest
    sequence = (1 cycle << 16) | 1, and no loss is declared.
    """
    stats = ReceptionStats()
    for i, seq in enumerate((65534, 65535, 0, 1)):
        stats.on_packet(seq=seq, rtp_timestamp=160 * i, arrival_ts=0.02 * i)
    block = stats.report_block(source_ssrc=1, lsr=0, dlsr=0, clock_rate=8000)
    assert block.extended_highest_seq == (1 << 16) | 1
    assert block.cumulative_lost == 0


def test_reception_stats_interarrival_jitter_zero_for_isochronous() -> None:
    """Perfectly isochronous arrival (transit constant) yields zero jitter (A.8).

    When every packet's transit time D is identical, the smoothed jitter estimate
    converges to (and starts at) zero.
    """
    stats = ReceptionStats()
    # arrival advances 20 ms, RTP timestamp advances 160 (= 20 ms at 8 kHz): the
    # transit time is constant, so D(i-1,i) == 0 for every pair → jitter 0.
    for i, seq in enumerate(range(100, 110)):
        stats.on_packet(seq=seq, rtp_timestamp=160 * i, arrival_ts=0.02 * i)
    block = stats.report_block(source_ssrc=1, lsr=0, dlsr=0, clock_rate=8000)
    assert block.jitter == 0


def test_reception_stats_interarrival_jitter_positive_when_arrival_varies() -> None:
    """A late packet inflates the smoothed jitter estimate above zero (A.8)."""
    stats = ReceptionStats()
    stats.on_packet(seq=1, rtp_timestamp=0, arrival_ts=0.0)
    stats.on_packet(seq=2, rtp_timestamp=160, arrival_ts=0.02)
    # Third packet arrives 30 ms late (0.05 s gap vs the expected 0.02 s).
    stats.on_packet(seq=3, rtp_timestamp=320, arrival_ts=0.07)
    block = stats.report_block(source_ssrc=1, lsr=0, dlsr=0, clock_rate=8000)
    assert block.jitter > 0


def test_reception_stats_jitter_in_rtp_clock_units() -> None:
    """Jitter is reported in RTP timestamp units of the source clock (RFC 3550 A.8).

    One packet 30 ms late at an 8 kHz clock contributes a transit delta of
    0.03 s * 8000 = 240 clock units; the first non-zero jitter sample is that
    delta / 16 (the §A.8 gain). |D| = 240, J += (|D| - J)/16 from J=0 → 15.
    """
    stats = ReceptionStats()
    stats.on_packet(seq=1, rtp_timestamp=0, arrival_ts=0.0)
    stats.on_packet(seq=2, rtp_timestamp=160, arrival_ts=0.05)  # 30 ms late
    block = stats.report_block(source_ssrc=1, lsr=0, dlsr=0, clock_rate=8000)
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
    lsr = 0x00000000  # middle 32 bits of our SR's send NTP time
    # "now" expressed in the same compact NTP form (middle 32 bits): 0.5 s later.
    now_compact = int(0.5 * (1 << 16))
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
    """More members → a longer interval (the ~5% bandwidth rule, RFC 3550 §6.2)."""
    small = compute_rtcp_interval(
        members=2,
        senders=1,
        rtcp_bw=100_000.0,
        we_sent=True,
        avg_rtcp_size=100.0,
        randomize=False,
    )
    large = compute_rtcp_interval(
        members=200,
        senders=100,
        rtcp_bw=100_000.0,
        we_sent=True,
        avg_rtcp_size=100.0,
        randomize=False,
    )
    assert large > small


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
    as_sender = compute_rtcp_interval(
        members=100,
        senders=2,
        rtcp_bw=100_000.0,
        we_sent=True,
        avg_rtcp_size=100.0,
        randomize=False,
    )
    as_receiver = compute_rtcp_interval(
        members=100,
        senders=2,
        rtcp_bw=100_000.0,
        we_sent=False,
        avg_rtcp_size=100.0,
        randomize=False,
    )
    assert as_sender != as_receiver
