"""Tests for hermes_voip.rtp — RTP packetisation + a reordering jitter buffer.

Covers the RFC 3550 fixed header (pack/parse + a pinned byte layout) and a
jitter buffer that reorders by sequence number, drops duplicates and late
packets, signals loss for concealment, and survives 16-bit sequence wraparound.
"""

import pytest

from hermes_voip.rtp import JitterBuffer, Lost, RtpPacket


def _pkt(seq: int, *, timestamp: int = 0, payload: bytes = b"\xff" * 160) -> RtpPacket:
    return RtpPacket(
        payload_type=0,
        sequence_number=seq,
        timestamp=timestamp,
        ssrc=0xDEADBEEF,
        payload=payload,
        marker=False,
    )


def test_pack_parse_round_trip() -> None:
    pkt = _pkt(1234, timestamp=160)
    raw = pkt.pack()
    back = RtpPacket.parse(raw)
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
    # byte0 = V=2,P=0,X=0,CC=0 -> 0x80 ; byte1 = M=1,PT=0 -> 0x80
    assert raw[0] == 0x80
    assert raw[1] == 0x80
    assert raw[2:4] == (1).to_bytes(2, "big")  # sequence
    assert raw[4:8] == (160).to_bytes(4, "big")  # timestamp
    assert raw[8:12] == (0xDEADBEEF).to_bytes(4, "big")  # ssrc


def test_marker_and_payload_type_round_trip() -> None:
    raw = _pkt(7).pack()
    assert (raw[1] & 0x80) == 0  # marker bit clear
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


def test_jitter_in_order_emits_in_order() -> None:
    jb = JitterBuffer(target_depth=2)
    for seq in (10, 11, 12):
        jb.push(_pkt(seq))
    assert jb.pop().sequence_number == 10  # type: ignore[union-attr]
    assert jb.pop().sequence_number == 11  # type: ignore[union-attr]
    assert jb.pop().sequence_number == 12  # type: ignore[union-attr]
    assert jb.pop() is None  # underflow


def test_jitter_reorders_within_depth() -> None:
    jb = JitterBuffer(target_depth=3)
    for seq in (10, 12, 11):
        jb.push(_pkt(seq))
    assert [jb.pop().sequence_number for _ in range(3)] == [10, 11, 12]  # type: ignore[union-attr]


def test_jitter_signals_loss_after_depth() -> None:
    jb = JitterBuffer(target_depth=2)
    jb.push(_pkt(10))
    jb.push(_pkt(12))
    jb.push(_pkt(13))  # two later packets buffered while 11 is missing
    assert jb.pop().sequence_number == 10  # type: ignore[union-attr]
    lost = jb.pop()
    assert isinstance(lost, Lost)
    assert lost.sequence == 11
    assert jb.pop().sequence_number == 12  # type: ignore[union-attr]


def test_jitter_underflow_returns_none_not_loss() -> None:
    jb = JitterBuffer(target_depth=2)
    jb.push(_pkt(10))
    assert jb.pop().sequence_number == 10  # type: ignore[union-attr]
    assert jb.pop() is None  # 11 missing but nothing buffered behind it -> wait


def test_jitter_drops_duplicates_and_late_packets() -> None:
    jb = JitterBuffer(target_depth=2)
    jb.push(_pkt(10))
    jb.push(_pkt(10))  # duplicate
    assert jb.pop().sequence_number == 10  # type: ignore[union-attr]
    jb.push(_pkt(10))  # now late (already past)
    assert jb.pop() is None


def test_jitter_handles_sequence_wraparound() -> None:
    jb = JitterBuffer(target_depth=2)
    for seq in (65534, 65535, 0, 1):
        jb.push(_pkt(seq))
    assert [jb.pop().sequence_number for _ in range(4)] == [65534, 65535, 0, 1]  # type: ignore[union-attr]
