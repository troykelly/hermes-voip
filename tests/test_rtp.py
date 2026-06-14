"""Tests for hermes_voip.rtp — RTP packetisation + a reordering jitter buffer.

Covers the RFC 3550 fixed header (pack/parse, pinned byte layout, CSRC +
extension skip, field-range validation) and a jitter buffer that reorders,
drops duplicates/late/too-far-ahead packets, signals loss for concealment, and
is RFC 1982 wraparound-safe.
"""

import pytest

from hermes_voip.rtp import JitterBuffer, Lost, RtpPacket, _seq_before


def _pkt(seq: int, *, timestamp: int = 0, payload: bytes = b"\xff" * 160) -> RtpPacket:
    return RtpPacket(
        payload_type=0,
        sequence_number=seq,
        timestamp=timestamp,
        ssrc=0xDEADBEEF,
        payload=payload,
        marker=False,
    )


def _packet(output: object) -> RtpPacket:
    """Assert a jitter-buffer output is a packet and return it (no type:ignore)."""
    assert isinstance(output, RtpPacket)
    return output


def test_pack_parse_round_trip() -> None:
    back = RtpPacket.parse(_pkt(1234, timestamp=160).pack())
    assert back.payload_type == 0
    assert back.sequence_number == 1234
    assert back.timestamp == 160
    assert back.ssrc == 0xDEADBEEF
    assert back.payload == b"\xff" * 160
    assert back.marker is False


def test_pinned_header_byte_layout() -> None:
    raw = RtpPacket(
        payload_type=0,
        sequence_number=1,
        timestamp=160,
        ssrc=0xDEADBEEF,
        payload=b"",
        marker=True,
    ).pack()
    assert raw[0] == 0x80  # V=2,P=0,X=0,CC=0
    assert raw[1] == 0x80  # M=1,PT=0
    assert raw[2:4] == (1).to_bytes(2, "big")
    assert raw[4:8] == (160).to_bytes(4, "big")
    assert raw[8:12] == (0xDEADBEEF).to_bytes(4, "big")


def test_marker_and_payload_type_round_trip() -> None:
    assert (_pkt(7).pack()[1] & 0x80) == 0
    pkt = RtpPacket.parse(
        RtpPacket(
            payload_type=101,
            sequence_number=7,
            timestamp=0,
            ssrc=1,
            payload=b"x",
            marker=True,
        ).pack()
    )
    assert pkt.payload_type == 101
    assert pkt.marker is True


def test_parse_rejects_truncated_header() -> None:
    with pytest.raises(ValueError, match="too short"):
        RtpPacket.parse(b"\x80\x00\x00")


def test_parse_skips_extension_header() -> None:
    # V=2,X=1,CC=0 (byte0=0x90); ext = 2-byte profile + 2-byte len(=1 word) + 4 bytes
    header = bytes([0x90, 0x00]) + (5).to_bytes(2, "big") + (160).to_bytes(4, "big")
    header += (0x1234).to_bytes(4, "big")  # ssrc
    ext = (0xBEDE).to_bytes(2, "big") + (1).to_bytes(2, "big") + b"\xaa\xbb\xcc\xdd"
    pkt = RtpPacket.parse(header + ext + b"PAYLOAD")
    assert pkt.payload == b"PAYLOAD"  # extension stripped, not leaked into payload


def test_parse_rejects_truncated_extension() -> None:
    header = bytes([0x90, 0x00]) + (5).to_bytes(2, "big") + (160).to_bytes(4, "big")
    header += (0x1234).to_bytes(4, "big")
    truncated = header + (0xBEDE).to_bytes(2, "big") + (4).to_bytes(2, "big") + b"\x00"
    with pytest.raises(ValueError, match="extension"):
        RtpPacket.parse(truncated)


def test_construction_validates_field_ranges() -> None:
    with pytest.raises(ValueError, match="payload_type"):
        RtpPacket(payload_type=128, sequence_number=0, timestamp=0, ssrc=0, payload=b"")
    with pytest.raises(ValueError, match="sequence_number"):
        RtpPacket(
            payload_type=0, sequence_number=70000, timestamp=0, ssrc=0, payload=b""
        )
    with pytest.raises(ValueError, match="timestamp"):
        RtpPacket(payload_type=0, sequence_number=0, timestamp=-1, ssrc=0, payload=b"")
    with pytest.raises(ValueError, match="ssrc"):
        RtpPacket(
            payload_type=0, sequence_number=0, timestamp=0, ssrc=1 << 33, payload=b""
        )


def test_jitter_in_order_emits_in_order() -> None:
    jb = JitterBuffer(target_depth=2)
    for seq in (10, 11, 12):
        jb.push(_pkt(seq))
    assert [_packet(jb.pop()).sequence_number for _ in range(3)] == [10, 11, 12]
    assert jb.pop() is None


def test_jitter_reorders_within_depth() -> None:
    jb = JitterBuffer(target_depth=3)
    for seq in (10, 12, 11):
        jb.push(_pkt(seq))
    assert [_packet(jb.pop()).sequence_number for _ in range(3)] == [10, 11, 12]


def test_jitter_signals_loss_after_depth() -> None:
    jb = JitterBuffer(target_depth=2)
    for seq in (10, 12, 13):
        jb.push(_pkt(seq))
    assert _packet(jb.pop()).sequence_number == 10
    lost = jb.pop()
    assert isinstance(lost, Lost)
    assert lost.sequence == 11
    assert _packet(jb.pop()).sequence_number == 12


def test_jitter_underflow_returns_none_not_loss() -> None:
    jb = JitterBuffer(target_depth=2)
    jb.push(_pkt(10))
    assert _packet(jb.pop()).sequence_number == 10
    assert jb.pop() is None


def test_jitter_drops_duplicates_and_late_packets() -> None:
    jb = JitterBuffer(target_depth=2)
    jb.push(_pkt(10))
    jb.push(_pkt(10))  # duplicate
    assert _packet(jb.pop()).sequence_number == 10
    jb.push(_pkt(10))  # late
    assert jb.pop() is None


def test_jitter_drops_packets_beyond_window() -> None:
    jb = JitterBuffer(target_depth=2, max_ahead=256)
    jb.push(_pkt(10))
    jb.push(_pkt(400))  # 390 ahead of the anchor -> outside the playout window
    assert _packet(jb.pop()).sequence_number == 10
    assert jb.pop() is None  # 400 was never buffered


def test_jitter_permanent_gap_stays_bounded() -> None:
    jb = JitterBuffer(target_depth=2, max_ahead=256)
    jb.push(_pkt(10))
    for seq in (200, 201, 202):  # a far-future cluster behind a permanent gap
        jb.push(_pkt(seq))
    assert _packet(jb.pop()).sequence_number == 10
    seen_loss = False
    for _ in range(1000):  # draining must terminate at the cluster, not spin forever
        out = jb.pop()
        if isinstance(out, Lost):
            seen_loss = True
        elif isinstance(out, RtpPacket) and out.sequence_number == 200:
            break
    else:
        pytest.fail("jitter buffer never reached the buffered cluster")
    assert seen_loss


def test_jitter_handles_sequence_wraparound() -> None:
    jb = JitterBuffer(target_depth=2)
    for seq in (65534, 65535, 0, 1):
        jb.push(_pkt(seq))
    assert [_packet(jb.pop()).sequence_number for _ in range(4)] == [65534, 65535, 0, 1]


def test_jitter_reorders_across_wraparound() -> None:
    jb = JitterBuffer(target_depth=3)
    for seq in (65534, 0, 65535):  # reordered across the wrap point
        jb.push(_pkt(seq))
    assert [_packet(jb.pop()).sequence_number for _ in range(3)] == [65534, 65535, 0]


def test_seq_before_edge_cases() -> None:
    assert _seq_before(10, 11) is True
    assert _seq_before(11, 10) is False
    assert _seq_before(5, 5) is False  # equality is never "before"
    assert _seq_before(65535, 0) is True  # across wrap
    assert _seq_before(0, 65535) is False
