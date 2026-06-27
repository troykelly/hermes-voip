"""Tests for hermes_voip.rtp — RTP packetisation + a reordering jitter buffer.

Covers the RFC 3550 fixed header (pack/parse, pinned byte layout, CSRC +
extension skip, field-range validation) and a jitter buffer that reorders,
drops duplicates/late/too-far-ahead packets, signals loss for concealment, and
is RFC 1982 wraparound-safe.
"""

import dataclasses
import struct

import pytest

import hermes_voip.rtp as rtp_module
from hermes_voip.rtp import _SEQ_HALF, JitterBuffer, Lost, RtpPacket, _seq_before


def _pkt(
    seq: int,
    *,
    timestamp: int = 0,
    ssrc: int = 0xDEADBEEF,
    payload: bytes = b"\xff" * 160,
) -> RtpPacket:
    return RtpPacket(
        payload_type=0,
        sequence_number=seq,
        timestamp=timestamp,
        ssrc=ssrc,
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


def _padded(payload: bytes) -> bytes:
    """A minimal V=2, P=1 RTP packet (no CSRC/extension) wrapping ``payload``."""
    byte0 = (2 << 6) | 0x20  # V=2, P=1, X=0, CC=0
    header = struct.pack("!BBHII", byte0, 0, 7, 0, 0xDEADBEEF)
    return header + payload


def test_parse_strips_valid_padding() -> None:
    # P=1 with a trailing count byte of 3 means the last 3 octets (the two pad
    # bytes plus the count byte itself) are padding: b"AB\x00\x00\x03" -> b"AB".
    pkt = RtpPacket.parse(_padded(b"AB\x00\x00\x03"))
    assert pkt.payload == b"AB"


def test_parse_rejects_padding_count_exceeding_payload() -> None:
    # A trailing pad count larger than the padded region is malformed; it must
    # raise like every other parse failure, never silently leak the bogus byte.
    with pytest.raises(ValueError, match="padding"):
        RtpPacket.parse(_padded(b"\x05"))  # count 5 > 1 byte present


def test_parse_rejects_zero_padding_count_with_padding_bit() -> None:
    # P=1 but a zero trailing count byte is malformed: a padded packet always
    # counts at least the count byte itself, so 0 is never valid (RFC 3550 §5.1).
    with pytest.raises(ValueError, match="padding"):
        RtpPacket.parse(_padded(b"\x00"))


def test_parse_rejects_padding_bit_with_empty_payload() -> None:
    # P=1 with no payload at all has no trailing count byte to read: malformed.
    with pytest.raises(ValueError, match="padding"):
        RtpPacket.parse(_padded(b""))


def test_jitter_depth_counts_total_occupancy_not_contiguous_backlog() -> None:
    # PINNED SEMANTICS: target_depth gates loss on TOTAL buffered occupancy, not
    # the contiguous backlog sitting immediately behind the next gap. So a single
    # far-future cluster (here 100/101/102, all within max_ahead) is enough to
    # push occupancy to the depth and make pop() declare the immediate gap Lost,
    # even though nothing contiguous sits right behind it. This total-occupancy
    # rule is what guarantees forward progress past a permanent gap (a buffered
    # cluster always drains rather than stalling); the cost pinned here is that an
    # isolated far-future cluster declares the near gap lost eagerly. Changing
    # this is the larger jitter-buffer API redesign (separate backlog item).
    jb = JitterBuffer(target_depth=3, max_ahead=256)
    for seq in (10, 100, 101, 102):
        jb.push(_pkt(seq))
    assert _packet(jb.pop()).sequence_number == 10  # anchor advances to 11
    # Occupancy is 3 (100, 101, 102) == depth, so the gap at 11 is declared Lost
    # immediately even though 100/101/102 are far from it.
    lost = jb.pop()
    assert isinstance(lost, Lost)
    assert lost.sequence == 11


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


def test_jitter_far_behind_first_arrival_does_not_reset_anchor() -> None:
    # A packet more than max_ahead behind the tentative anchor is not a start-of-
    # call reorder; it must be dropped, not move the anchor down out of the window
    # (which would strand the buffered higher packet behind a huge artificial gap).
    jb = JitterBuffer(target_depth=1, max_ahead=100)
    jb.push(_pkt(1000))
    jb.push(_pkt(1))  # 999 behind > max_ahead -> dropped, anchor stays at 1000
    out = jb.pop()
    assert _packet(out).sequence_number == 1000


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


def test_jitter_len_reports_buffered_packet_count() -> None:
    jb = JitterBuffer(target_depth=3)
    assert len(jb) == 0
    jb.push(_pkt(10))
    jb.push(_pkt(12))
    jb.push(_pkt(12))  # duplicate does not add depth
    assert len(jb) == 2
    assert _packet(jb.pop()).sequence_number == 10
    assert len(jb) == 1


def test_jitter_peek_is_non_destructive_and_matches_next_pop() -> None:
    jb = JitterBuffer(target_depth=3)
    for seq in (10, 12, 11):
        jb.push(_pkt(seq))
    first_peek = jb.peek()
    second_peek = jb.peek()
    assert first_peek == _pkt(10)
    assert second_peek == _pkt(10)
    assert len(jb) == 3
    assert jb.pop() == first_peek
    assert _packet(jb.pop()).sequence_number == 11


def test_jitter_flush_returns_ordered_remainder_and_empties() -> None:
    jb = JitterBuffer(target_depth=3)
    for seq in (10, 13, 11, 12):
        jb.push(_pkt(seq))
    assert _packet(jb.pop()).sequence_number == 10
    assert [packet.sequence_number for packet in jb.flush()] == [11, 12, 13]
    assert len(jb) == 0
    assert jb.pop() is None
    assert jb.peek() is None
    assert jb.flush() == []


def test_jitter_reset_clears_buffer_and_sequence_state() -> None:
    jb = JitterBuffer(target_depth=2)
    for seq in (1000, 1002):
        jb.push(_pkt(seq))
    assert len(jb) == 2
    jb.reset()
    assert len(jb) == 0
    assert jb.pop() is None
    assert jb.peek() is None
    jb.push(_pkt(1))
    assert _packet(jb.pop()).sequence_number == 1


def test_jitter_ssrc_change_auto_resets_sequence_anchor() -> None:
    # With ssrc_hysteresis=1 the reset fires on the very first foreign-SSRC
    # packet — preserving the pre-hysteresis behaviour for callers that opt in.
    jb = JitterBuffer(target_depth=2, max_ahead=256, ssrc_hysteresis=1)
    old_ssrc = 0x11111111
    new_ssrc = 0x22222222
    for seq in (65000, 65001):
        jb.push(_pkt(seq, ssrc=old_ssrc))
    assert _packet(jb.pop()).sequence_number == 65000
    jb.push(_pkt(1, ssrc=new_ssrc))
    assert len(jb) == 1
    assert _packet(jb.pop()).sequence_number == 1
    assert jb.pop() is None


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


def test_jitter_reordered_first_pair_does_not_drop_the_earlier_packet() -> None:
    # At stream start the first packets are the most reordering-prone. A higher
    # sequence arriving before a lower one must NOT anchor playout above the
    # lower one (which would permanently lose the call's opening audio).
    jb = JitterBuffer(target_depth=2)
    jb.push(_pkt(12))
    jb.push(_pkt(10))  # arrives second but is earlier in sequence
    assert _packet(jb.pop()).sequence_number == 10  # earliest emitted first
    assert jb.pop() is None  # 11 missing, only 12 buffered (< depth): wait
    jb.push(_pkt(11))  # the gap fills in
    assert _packet(jb.pop()).sequence_number == 11
    assert _packet(jb.pop()).sequence_number == 12


def test_jitter_anchors_at_minimum_of_a_reordered_start_cluster() -> None:
    # A burst of reordered packets at stream start must all survive, emitted in
    # ascending order from the lowest sequence seen before the first pop.
    jb = JitterBuffer(target_depth=4)
    for seq in (13, 11, 14, 10, 12):  # heavily reordered opening cluster
        jb.push(_pkt(seq))
    assert [_packet(jb.pop()).sequence_number for _ in range(5)] == [10, 11, 12, 13, 14]


# ---------------------------------------------------------------------------
# Adaptive jitter depth (ADR-0056, item 1). The buffer adapts its reorder
# tolerance to the observed reordering/late-drop of the push/pop stream — no
# wall clock, so every case is deterministic.
# ---------------------------------------------------------------------------


def test_adaptive_disabled_by_default_keeps_fixed_depth() -> None:
    # The default (non-adaptive) buffer never moves its depth: byte-for-byte the
    # legacy behaviour. current_depth stays at the constructed target_depth no
    # matter what the stream does.
    jb = JitterBuffer(target_depth=2)
    assert jb.current_depth == 2
    for seq in (10, 12, 13, 14, 15):  # a gap that would grow an adaptive buffer
        jb.push(_pkt(seq))
    for _ in range(5):
        jb.pop()
    assert jb.current_depth == 2  # unchanged: adaptation is opt-in


def test_adaptive_grows_depth_on_late_drop() -> None:
    # A packet that arrives AFTER its slot was already emitted as Lost (a late
    # drop) means the buffer declared loss too eagerly: grow the reorder
    # tolerance so the next such reorder is absorbed instead of dropped.
    jb = JitterBuffer(target_depth=2, max_depth=8, adapt=True)
    assert jb.current_depth == 2
    # Open a gap at 11 and pile up 12,13 so 11 is declared Lost at depth 2.
    for seq in (10, 12, 13):
        jb.push(_pkt(seq))
    assert _packet(jb.pop()).sequence_number == 10
    assert isinstance(jb.pop(), Lost)  # 11 declared lost
    # 11 now arrives late — proof we were too eager. Depth must have grown.
    jb.push(_pkt(11))
    assert jb.current_depth == 3


def test_adaptive_grows_depth_on_wide_reorder() -> None:
    # An in-window packet arriving with a reorder span >= the current depth means
    # the link reorders further than we tolerate; grow toward that span.
    jb = JitterBuffer(target_depth=2, max_depth=8, adapt=True)
    jb.push(_pkt(10))
    assert _packet(jb.pop()).sequence_number == 10  # anchor now at 11, emitted
    # 14 arrives (anchor 11), then 11 arrives 3 behind the highest expected — a
    # reorder span of 3 >= depth 2 -> grow.
    jb.push(_pkt(14))
    jb.push(_pkt(11))
    assert jb.current_depth >= 3


def test_adaptive_depth_never_exceeds_max() -> None:
    # Repeated late drops must not run the depth past max_depth.
    jb = JitterBuffer(target_depth=2, max_depth=4, adapt=True)
    seq = 100
    for _ in range(20):
        # Each round: emit one, declare the next lost, then deliver it late.
        jb.push(_pkt(seq))
        jb.push(_pkt(seq + 2))
        jb.push(_pkt(seq + 3))
        jb.push(_pkt(seq + 4))
        jb.push(_pkt(seq + 5))
        for _ in range(10):
            if jb.pop() is None:
                break
        jb.push(_pkt(seq + 1))  # late drop
        seq += 6
    assert jb.current_depth <= 4


def test_adaptive_shrinks_depth_after_clean_run() -> None:
    # After the depth has grown, a long clean in-order run (no loss, no late
    # drop) must shrink it back toward the floor so a calmed link stops paying
    # the latency. Floor is target_depth and is never breached.
    jb = JitterBuffer(target_depth=2, max_depth=8, adapt=True)
    # Grow once via a late drop.
    for seq in (10, 12, 13):
        jb.push(_pkt(seq))
    assert _packet(jb.pop()).sequence_number == 10
    assert isinstance(jb.pop(), Lost)  # 11
    jb.push(_pkt(11))  # late -> depth 3
    assert jb.current_depth == 3
    # Drain the already-buffered 12, 13 (anchor is now at 12) so the clean run
    # starts from a clean slate.
    assert _packet(jb.pop()).sequence_number == 12
    assert _packet(jb.pop()).sequence_number == 13
    # Now feed a long clean in-order run; depth must come back down to 2.
    seq = 14
    for _ in range(200):
        jb.push(_pkt(seq))
        out = jb.pop()
        assert _packet(out).sequence_number == seq
        seq += 1
    assert jb.current_depth == 2  # shrank to the floor, never below


def test_adaptive_floor_is_target_depth() -> None:
    # A perfectly clean stream on an adaptive buffer never shrinks below the
    # constructed floor (a 1-packet depth would lose all reorder tolerance).
    jb = JitterBuffer(target_depth=3, max_depth=8, adapt=True)
    for seq in range(500):
        jb.push(_pkt(seq))
        jb.pop()
    assert jb.current_depth == 3


def test_adaptive_constructor_validates_max_depth() -> None:
    with pytest.raises(ValueError, match="max_depth"):
        JitterBuffer(target_depth=4, max_depth=2, adapt=True)  # max < floor


# ---------------------------------------------------------------------------
# SSRC auto-reset hysteresis (backlog ~1089-e).
#
# A single stray/misrouted foreign-SSRC packet must NOT flush buffered audio.
# Only N CONSECUTIVE packets sharing the same foreign SSRC trigger a reset.
# Default N is 3; a constructor param ``ssrc_hysteresis`` overrides it.
# ---------------------------------------------------------------------------


def test_ssrc_hysteresis_default_is_3() -> None:
    """JitterBuffer exposes ssrc_hysteresis defaulting to 3."""
    jb = JitterBuffer()
    assert jb.ssrc_hysteresis == 3


def test_ssrc_hysteresis_constructor_validates_positive_int() -> None:
    """ssrc_hysteresis rejects zero, negatives, booleans, and non-ints."""
    with pytest.raises(ValueError, match="ssrc_hysteresis"):
        JitterBuffer(ssrc_hysteresis=0)
    with pytest.raises(ValueError, match="ssrc_hysteresis"):
        JitterBuffer(ssrc_hysteresis=-1)
    with pytest.raises((ValueError, TypeError), match="ssrc_hysteresis"):
        # bool is a subclass of int — must be rejected
        JitterBuffer(ssrc_hysteresis=True)
    with pytest.raises((ValueError, TypeError), match="ssrc_hysteresis"):
        JitterBuffer(ssrc_hysteresis=1.5)  # type: ignore[arg-type]  # float not valid


def test_ssrc_hysteresis_one_foreign_packet_does_not_reset() -> None:
    """A single foreign-SSRC packet leaves the buffer intact."""
    home_ssrc = 0x11111111
    foreign_ssrc = 0x22222222
    jb = JitterBuffer(target_depth=3, ssrc_hysteresis=3)
    for seq in (10, 11, 12):
        jb.push(_pkt(seq, ssrc=home_ssrc))
    # One foreign packet — should NOT trigger a reset.
    jb.push(_pkt(20, ssrc=foreign_ssrc))
    # Buffer still has the home packets.
    assert len(jb) == 3
    assert _packet(jb.pop()).sequence_number == 10


def test_ssrc_hysteresis_n_minus_1_consecutive_foreign_do_not_reset() -> None:
    """N-1 consecutive foreign-SSRC packets do not trigger a reset."""
    home_ssrc = 0x11111111
    foreign_ssrc = 0x22222222
    jb = JitterBuffer(target_depth=4, ssrc_hysteresis=3)
    for seq in (10, 11, 12):
        jb.push(_pkt(seq, ssrc=home_ssrc))
    buffered_before = len(jb)
    # Push N-1 = 2 consecutive foreign packets.
    for seq in (100, 101):
        jb.push(_pkt(seq, ssrc=foreign_ssrc))
    # Reset must NOT have fired; original buffer intact.
    assert len(jb) == buffered_before
    assert _packet(jb.pop()).sequence_number == 10


def test_ssrc_hysteresis_nth_consecutive_foreign_resets() -> None:
    """Exactly N consecutive foreign-SSRC packets trigger a reset and SSRC adoption."""
    home_ssrc = 0x11111111
    foreign_ssrc = 0x22222222
    jb = JitterBuffer(target_depth=4, ssrc_hysteresis=3)
    for seq in (10, 11, 12):
        jb.push(_pkt(seq, ssrc=home_ssrc))
    # Push N consecutive foreign packets; the Nth should fire the reset.
    for seq in (100, 101, 102):
        jb.push(_pkt(seq, ssrc=foreign_ssrc))
    # The reset must have cleared the old packets and adopted the foreign SSRC.
    assert len(jb) <= 3  # at most the three foreign packets survive
    # The anchor is now on the foreign stream; the Nth foreign packet itself
    # was pushed into the new buffer.
    out = jb.pop()
    assert out is not None
    assert isinstance(out, RtpPacket)
    assert out.ssrc == foreign_ssrc


def test_ssrc_hysteresis_home_packet_interleaved_resets_candidate() -> None:
    """A home-SSRC packet mid-run resets the candidate; the run must restart."""
    home_ssrc = 0x11111111
    foreign_ssrc = 0x22222222
    # N=3: two foreign, then one home, then two more foreign → still no reset.
    jb = JitterBuffer(target_depth=4, ssrc_hysteresis=3)
    for seq in (10, 11, 12):
        jb.push(_pkt(seq, ssrc=home_ssrc))
    buffered_before = len(jb)
    # Two foreign packets (N-1).
    jb.push(_pkt(100, ssrc=foreign_ssrc))
    jb.push(_pkt(101, ssrc=foreign_ssrc))
    # Home packet interrupts — must reset the candidate count.
    jb.push(_pkt(13, ssrc=home_ssrc))
    # Two more foreign — total run restarts at 2, still < N=3, no reset.
    jb.push(_pkt(102, ssrc=foreign_ssrc))
    jb.push(_pkt(103, ssrc=foreign_ssrc))
    # Home packets still in buffer.
    assert len(jb) >= buffered_before
    assert _packet(jb.pop()).sequence_number == 10


def test_ssrc_hysteresis_different_foreign_ssrc_mid_run_restarts_count() -> None:
    """A DIFFERENT foreign SSRC mid-run restarts the candidate at 1, not reset."""
    home_ssrc = 0x11111111
    foreign_ssrc_a = 0x22222222
    foreign_ssrc_b = 0x33333333
    jb = JitterBuffer(target_depth=4, ssrc_hysteresis=3)
    for seq in (10, 11, 12):
        jb.push(_pkt(seq, ssrc=home_ssrc))
    buffered_before = len(jb)
    # Two packets from foreign_a (count=2, N-1).
    jb.push(_pkt(100, ssrc=foreign_ssrc_a))
    jb.push(_pkt(101, ssrc=foreign_ssrc_a))
    # One packet from foreign_b → candidate switches to foreign_b, count=1.
    jb.push(_pkt(200, ssrc=foreign_ssrc_b))
    # One more from foreign_b (count=2, still < 3): no reset.
    jb.push(_pkt(201, ssrc=foreign_ssrc_b))
    # Original home buffer must be intact.
    assert len(jb) >= buffered_before
    assert _packet(jb.pop()).sequence_number == 10


def test_ssrc_hysteresis_1_resets_immediately() -> None:
    """ssrc_hysteresis=1 degrades to the pre-hysteresis: reset on first foreign."""
    home_ssrc = 0x11111111
    foreign_ssrc = 0x22222222
    jb = JitterBuffer(target_depth=3, ssrc_hysteresis=1)
    for seq in (10, 11, 12):
        jb.push(_pkt(seq, ssrc=home_ssrc))
    jb.push(_pkt(100, ssrc=foreign_ssrc))
    # Must have reset immediately on the first foreign packet.
    assert len(jb) <= 1  # only the foreign packet in buffer (possibly)
    out = jb.pop()
    assert out is not None
    assert isinstance(out, RtpPacket)
    assert out.ssrc == foreign_ssrc


def test_ssrc_hysteresis_reset_clears_candidate_state() -> None:
    """reset() clears the SSRC candidate/count so a subsequent push starts fresh."""
    home_ssrc = 0x11111111
    foreign_ssrc = 0x22222222
    jb = JitterBuffer(target_depth=3, ssrc_hysteresis=3)
    for seq in (10, 11, 12):
        jb.push(_pkt(seq, ssrc=home_ssrc))
    # Accumulate a partial candidate run without firing.
    jb.push(_pkt(100, ssrc=foreign_ssrc))
    jb.push(_pkt(101, ssrc=foreign_ssrc))
    # Explicit reset.
    jb.reset()
    # After reset, a single home packet should anchor cleanly.
    jb.push(_pkt(1, ssrc=home_ssrc))
    assert _packet(jb.pop()).sequence_number == 1


# ---------------------------------------------------------------------------
# Run-length coalescing for far-ahead packet loss (perf/jitterbuffer-lost-runlength)
# ---------------------------------------------------------------------------


def test_lost_has_count_field_defaulting_to_one() -> None:
    """Lost.count is a backward-compatible optional field with default 1."""
    # Old callers that only check .sequence still work.
    lost = Lost(sequence=42)
    assert lost.sequence == 42
    assert lost.count == 1
    # Callers may also construct with an explicit count.
    lost2 = Lost(sequence=100, count=5)
    assert lost2.count == 5


def test_lost_count_one_is_immutable() -> None:
    """Lost is frozen — count cannot be mutated after construction."""
    lost = Lost(sequence=10, count=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        lost.count = 1  # type: ignore[misc]


def test_far_ahead_gap_emits_single_coalesced_lost() -> None:
    """With depth=1, push seq 10 then seq 50: pop() must emit exactly one Lost.

    Before the fix pop() emitted 39 separate Lost events (one per missing seq
    11..49) — O(gap) allocations + loop iterations for a single jump.
    After the fix a single Lost(sequence=11, count=39) is emitted, advancing
    the anchor from 11 to 50 in O(1) steps.
    """
    jb = JitterBuffer(target_depth=1, max_ahead=256)
    jb.push(_pkt(10))
    jb.push(_pkt(50))  # 39-packet gap (seqs 11..49 missing)

    # Pop seq 10 (the real packet).
    first = jb.pop()
    assert isinstance(first, RtpPacket)
    assert first.sequence_number == 10

    # Next pop must yield a SINGLE coalesced Lost covering the gap 11..49.
    coalesced = jb.pop()
    assert isinstance(coalesced, Lost)
    assert coalesced.sequence == 11
    assert coalesced.count == 39

    # No more Lost events — the gap was consumed in one shot.
    real = jb.pop()
    assert isinstance(real, RtpPacket)
    assert real.sequence_number == 50

    # Buffer is now empty.
    assert jb.pop() is None


def test_far_ahead_gap_sequence_accounting_exact() -> None:
    """After the coalesced Lost the anchor is exactly at the real packet seq."""
    jb = JitterBuffer(target_depth=1, max_ahead=256)
    jb.push(_pkt(10))
    jb.push(_pkt(50))

    jb.pop()  # seq 10
    lost = jb.pop()
    assert isinstance(lost, Lost)
    # count must be exactly 39 (seqs 11..49 inclusive).
    assert lost.count == (50 - 10 - 1)  # == 39

    # The anchor is now 50 — next pop gives the real packet, not another Lost.
    out = jb.pop()
    assert isinstance(out, RtpPacket)
    assert out.sequence_number == 50


def test_small_gap_still_emits_single_lost_with_count_one() -> None:
    """A gap of exactly 1 packet still produces Lost(count=1) — no regression."""
    jb = JitterBuffer(target_depth=2)
    jb.push(_pkt(10))
    jb.push(_pkt(12))
    jb.push(_pkt(13))

    assert _packet(jb.pop()).sequence_number == 10
    lost = jb.pop()
    assert isinstance(lost, Lost)
    assert lost.sequence == 11
    assert lost.count == 1  # single-gap: no coalescing needed

    assert _packet(jb.pop()).sequence_number == 12


def test_coalesced_lost_pop_count_measures_allocations() -> None:
    """Verify O(1) Lost objects for a gap of 39: exactly one Lost is returned.

    This is the allocation benchmark mandated by rule 22:
      BEFORE: 39 Lost allocations for a gap of 39
      AFTER:   1 Lost allocation  for a gap of 39
    """
    gap = 39
    jb = JitterBuffer(target_depth=1, max_ahead=256)
    jb.push(_pkt(10))
    jb.push(_pkt(10 + gap + 1))  # seq 50

    jb.pop()  # consume the real packet at 10

    lost_events: list[Lost] = []
    while True:
        out = jb.pop()
        if out is None:
            break
        if isinstance(out, Lost):
            lost_events.append(out)
        else:
            # Real packet at seq 50 — stop counting Lost events.
            break

    # AFTER fix: exactly one Lost object, carrying count=gap.
    assert len(lost_events) == 1, (
        f"Expected 1 coalesced Lost event for gap={gap}, got {len(lost_events)}"
    )
    assert lost_events[0].count == gap


def test_coalesced_lost_wraparound_boundary() -> None:
    """Coalescing across the 16-bit sequence-number wrap boundary is correct."""
    # Push seq 65533, then 65533+5=5 (wraps through 65535→0→1→2→3→4→5).
    # Gap seqs: 65534, 65535, 0, 1, 2, 3, 4  → 7 missing.
    jb = JitterBuffer(target_depth=1, max_ahead=256)
    jb.push(_pkt(65533))
    jb.push(_pkt(5))

    assert _packet(jb.pop()).sequence_number == 65533

    lost = jb.pop()
    assert isinstance(lost, Lost)
    assert lost.sequence == 65534  # first missing seq
    assert lost.count == 7  # 65534, 65535, 0, 1, 2, 3, 4

    out = jb.pop()
    assert isinstance(out, RtpPacket)
    assert out.sequence_number == 5


# ---------------------------------------------------------------------------
# Coalescing gap-cap boundary (finding #1: gap must cap at EXACTLY max_ahead)
# ---------------------------------------------------------------------------


def test_coalesced_boundary_successor_is_delivered() -> None:
    """The real successor at the window boundary is delivered, never skipped.

    The successor buried behind a long gap sits at the far edge of the playout
    window (``max_ahead`` sequence numbers ahead of the anchor that pop() will
    reach after consuming the gap). The coalesced ``Lost`` must advance the anchor
    to EXACTLY that slot so the very next pop() returns the buffered packet — an
    anchor that over-advanced by one would step past it and return ``None``.

    Reachable end-to-end through push/pop (the window edge is admitted by push,
    then the coalescing scan walks up to it), so this guards the observable
    contract directly.
    """
    max_ahead = 8
    jb = JitterBuffer(target_depth=1, max_ahead=max_ahead)
    jb.push(_pkt(10))  # anchor learns seq 10
    # The successor at the push-window edge (10 + max_ahead) survives push; after
    # popping seq 10 the anchor is 11 and the successor is at scan-diff
    # max_ahead - 1, the deepest reachable empty-run before a buffered slot.
    edge = 10 + max_ahead  # 18
    jb.push(_pkt(edge))

    assert _packet(jb.pop()).sequence_number == 10

    lost = jb.pop()
    assert isinstance(lost, Lost)
    assert lost.sequence == 11
    assert lost.count == edge - 11  # gap 11..(edge-1), anchor lands on edge

    out = jb.pop()
    assert isinstance(out, RtpPacket), (
        "boundary successor must be delivered, not skipped by an over-advanced anchor"
    )
    assert out.sequence_number == edge


def test_coalesced_scan_caps_gap_at_exactly_max_ahead() -> None:
    """pop()'s coalescing scan caps the gap at EXACTLY ``max_ahead`` — never +1.

    This drives the defensive bound the public push/pop API provably cannot reach
    (push admits only packets within ``max_ahead`` of the anchor, and every
    backward anchor revision deposits a packet inside the scan window, so an
    all-empty window with occupancy beyond it never forms through the API — a
    >4.6M-sequence search found no path). The off-by-one therefore can only be
    exercised by constructing that internal state directly: anchor at the head, a
    single buffered packet BEYOND the window, and every slot in
    ``[anchor+1, anchor+max_ahead]`` empty. The scan must then advance the anchor
    by ``max_ahead`` (``Lost(count=max_ahead)``), not ``max_ahead + 1``.

    The bug: ``<=`` in the scan condition lets the gap reach ``max_ahead + 1`` and
    ``_next`` over-advances past the boundary slot, so a packet that later arrives
    there is dropped as "behind" the anchor.
    """
    max_ahead = 256
    jb = JitterBuffer(target_depth=1, max_ahead=max_ahead)
    jb.push(_pkt(1000))  # learn the SSRC and anchor at 1000

    # Construct the otherwise-unreachable all-positions-fail state: anchor 1000,
    # nothing in 1001..1256, one packet far beyond the window (diff 400 > 256) to
    # satisfy occupancy >= depth so the loss branch runs the full scan.
    # Private-member access is deliberate here: this is the ONLY way to reach the
    # defensive scan bound (see the docstring — the public API cannot form this
    # state). It exercises a real pop() code path, not internal plumbing.
    anchor = 1000
    jb._next = anchor
    jb._packets.clear()
    beyond = anchor + max_ahead + 144  # 1400 — past the scan window
    jb._packets[beyond] = _pkt(beyond)
    jb._emitted = True  # past the start-reorder phase

    out = jb.pop()
    assert isinstance(out, Lost)
    assert out.sequence == anchor
    # Bug: count == max_ahead + 1 (257). Correct: capped at exactly max_ahead.
    assert out.count == max_ahead
    # The anchor advances by the capped count, never one past the window edge.
    assert jb._next == (anchor + max_ahead) % (1 << 16)


# ---------------------------------------------------------------------------
# Lost.count construction validation (finding #2)
# ---------------------------------------------------------------------------


def test_lost_count_zero_rejected() -> None:
    """Lost(count=0) is rejected — a zero-length run breaks stream continuity."""
    with pytest.raises(ValueError, match="count"):
        Lost(sequence=10, count=0)


def test_lost_count_negative_rejected() -> None:
    """Lost(count=-1) is rejected — a negative run would crash the consumer."""
    with pytest.raises(ValueError, match="count"):
        Lost(sequence=10, count=-1)


def test_lost_count_bool_rejected() -> None:
    """Lost(count=True) is rejected at runtime.

    ``bool`` is an ``int`` subtype to the type checker (so there is no static
    error), but it is not a valid packet count.
    """
    with pytest.raises(TypeError, match="count"):
        Lost(sequence=10, count=True)


def test_lost_count_float_rejected() -> None:
    """Lost(count=1.5) is rejected — a non-int count is a type error."""
    with pytest.raises(TypeError, match="count"):
        Lost(sequence=10, count=1.5)  # type: ignore[arg-type]


def test_lost_count_one_accepted() -> None:
    """Lost(count=1) is the valid single-packet-loss case."""
    lost = Lost(sequence=10, count=1)
    assert lost.count == 1


def test_lost_count_thirtynine_accepted() -> None:
    """Lost(count=39) is a valid coalesced far-ahead run."""
    lost = Lost(sequence=11, count=39)
    assert lost.count == 39


# ---------------------------------------------------------------------------
# max_ahead boundary edge, buffered-duplicate first-wins, and fidelity (backlog 330-341)
# ---------------------------------------------------------------------------


def test_max_ahead_boundary_inclusive_edge_packet_accepted() -> None:
    """A packet at exactly next+max_ahead is accepted and buffered, not dropped.

    The playout window is [next, next+max_ahead] inclusive. A packet at the
    boundary (diff == max_ahead from anchor) must pass the push-side window check
    (the line checking `> max_ahead`). This test verifies that the boundary packet
    is NOT dropped at push time; it survives in the buffer for retrieval.
    """
    max_ahead = 10
    jb = JitterBuffer(target_depth=1, max_ahead=max_ahead)
    jb.push(_pkt(100))  # anchor at 100
    boundary_seq = 100 + max_ahead  # exactly at the edge (diff = 10)
    jb.push(_pkt(boundary_seq))  # must not be dropped by push()

    # Verify the boundary packet is in the buffer (not dropped).
    assert len(jb) == 2  # both 100 and 110 buffered
    assert 110 in jb._packets  # boundary packet is actually there

    # Pop the anchor and then the boundary — the boundary must be retrievable.
    assert _packet(jb.pop()).sequence_number == 100
    # Now declare the gap as Lost (occupancy 1 >= depth 1).
    lost = jb.pop()
    assert isinstance(lost, Lost)
    # After the loss, the boundary packet is now available at the new anchor.
    assert _packet(jb.pop()).sequence_number == boundary_seq


def test_max_ahead_boundary_exclusive_beyond_packet_dropped() -> None:
    """A packet at next+max_ahead+1 is dropped and never reaches pop().

    Packets beyond max_ahead are outside the playout window and discarded at
    push time. A packet one step beyond the boundary (diff == max_ahead + 1)
    must be dropped silently.
    """
    jb = JitterBuffer(target_depth=1, max_ahead=10)
    jb.push(_pkt(100))  # anchor at 100
    beyond_seq = 100 + 10 + 1  # beyond the edge
    jb.push(_pkt(beyond_seq))

    # Only the anchor should be buffered.
    assert len(jb) == 1
    assert _packet(jb.pop()).sequence_number == 100

    # The beyond-boundary packet was dropped; no more packets.
    assert jb.pop() is None


def test_buffered_duplicate_first_payload_wins() -> None:
    """A duplicate of a buffered packet keeps the FIRST arrival's payload.

    The buffer uses setdefault(seq, packet): the first packet to arrive at a
    sequence is buffered; later arrivals with the same sequence are silently
    dropped. This guards against bit-corruption or retransmission overwriting
    the original payload. Fidelity: the FIRST payload is preserved.
    """
    first_payload = b"FIRST_PAYLOAD"
    second_payload = b"SECOND_PAYLOAD"

    jb = JitterBuffer(target_depth=2)
    jb.push(_pkt(100, payload=first_payload))
    jb.push(_pkt(100, payload=second_payload))  # duplicate at same seq

    # Pop must return the packet with the FIRST payload.
    out = jb.pop()
    assert isinstance(out, RtpPacket)
    assert out.sequence_number == 100
    assert out.payload == first_payload


def test_payload_and_timestamp_fidelity_through_pop() -> None:
    """Payload and timestamp values are preserved exactly through pop().

    The jitter buffer reorders/buffers packets without modifying their payload
    or timestamp. A packet arriving with specific payload bytes and a timestamp
    must emerge from pop() byte-for-byte identical.
    """
    timestamps = [0, 160, 320, 480]  # e.g. 20ms ptime: 8kHz sample clock
    payloads = [
        b"PKT_A" * 32,  # 160 bytes
        b"PKT_B" * 32,
        b"PKT_C" * 32,
        b"PKT_D" * 32,
    ]

    jb = JitterBuffer(target_depth=2)

    # Push in reordered: 100, 102, 101, 103 with varying payloads/timestamps.
    jb.push(_pkt(100, timestamp=timestamps[0], payload=payloads[0]))
    jb.push(_pkt(102, timestamp=timestamps[2], payload=payloads[2]))
    jb.push(_pkt(101, timestamp=timestamps[1], payload=payloads[1]))
    jb.push(_pkt(103, timestamp=timestamps[3], payload=payloads[3]))

    # Pop in order: must see all payloads and timestamps preserved.
    out1 = _packet(jb.pop())
    assert out1.sequence_number == 100
    assert out1.timestamp == timestamps[0]
    assert out1.payload == payloads[0]

    out2 = _packet(jb.pop())
    assert out2.sequence_number == 101
    assert out2.timestamp == timestamps[1]
    assert out2.payload == payloads[1]

    out3 = _packet(jb.pop())
    assert out3.sequence_number == 102
    assert out3.timestamp == timestamps[2]
    assert out3.payload == payloads[2]

    out4 = _packet(jb.pop())
    assert out4.sequence_number == 103
    assert out4.timestamp == timestamps[3]
    assert out4.payload == payloads[3]

    # Underflow after all packets drained.
    assert jb.pop() is None


def test_max_ahead_guard_rejects_invalid_construction() -> None:
    """JitterBuffer.__init__ raises ValueError when max_ahead >= _SEQ_HALF.

    The serial-number arithmetic (RFC 1982) uses modulo 2^16. Reorder windows
    larger than or equal to half the space (2^15 = 32768) break the comparison
    logic and allow ambiguous sequence comparisons. The __init__ must raise
    ValueError (not assert, which is stripped under -O) to catch configuration
    errors early and unconditionally (rule 37: errors propagate).
    """
    # max_ahead exactly at the half: must raise ValueError.
    with pytest.raises(ValueError, match="max_ahead"):
        JitterBuffer(target_depth=1, max_ahead=_SEQ_HALF)

    # max_ahead beyond the half: must raise ValueError.
    with pytest.raises(ValueError, match="max_ahead"):
        JitterBuffer(target_depth=1, max_ahead=_SEQ_HALF + 1)

    # max_ahead just below the half: must succeed.
    jb = JitterBuffer(target_depth=1, max_ahead=_SEQ_HALF - 1)
    assert jb._max_ahead == _SEQ_HALF - 1


def test_module_constants_pinned_values() -> None:
    """Verify that module constants have the documented, invariant values.

    Module-level constants should be annotated with typing.Final to signal that
    they are immutable configuration/protocol values, never reassignable. This
    test pins their documented values so a typo in any constant is caught
    deterministically (e.g. a transcription error in _SEQ_MOD = 1 << 16).
    """
    # RTP version and header structure
    assert rtp_module._RTP_VERSION == 2
    assert rtp_module._HEADER_LEN == 12
    assert rtp_module._EXT_HEADER_LEN == 4
    assert rtp_module._MAX_PAYLOAD_TYPE == 0x7F
    assert rtp_module._MARKER_BIT == 0x80
    assert rtp_module._PADDING_BIT == 0x20
    assert rtp_module._EXTENSION_BIT == 0x10
    assert rtp_module._CSRC_MASK == 0x0F
    assert rtp_module._CSRC_WORD == 4
    assert rtp_module._SEQ_MOD == 1 << 16
    assert rtp_module._SEQ_HALF == 1 << 15
    assert rtp_module._MAX_SEQ == 0xFFFF
    assert rtp_module._U32 == 0xFFFFFFFF
    assert rtp_module._DEFAULT_MAX_AHEAD == 256
    assert rtp_module._DEFAULT_MAX_DEPTH == 10
    assert rtp_module._SHRINK_AFTER == 50
