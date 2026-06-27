"""Tests for hermes_voip.rtp — RTP packetisation + a reordering jitter buffer.

Covers the RFC 3550 fixed header (pack/parse, pinned byte layout, CSRC +
extension skip, field-range validation) and a jitter buffer that reorders,
drops duplicates/late/too-far-ahead packets, signals loss for concealment, and
is RFC 1982 wraparound-safe.
"""

import struct

import pytest

from hermes_voip.rtp import JitterBuffer, Lost, RtpPacket, _seq_before


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
    jb = JitterBuffer(target_depth=2, max_ahead=256)
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
