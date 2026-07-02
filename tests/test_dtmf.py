"""Tests for hermes_voip.dtmf — RFC 4733 telephone-event handling (ADR-0010).

Covers the 4-byte telephone-event payload codec, the digit<->event mapping, the
outbound payload sequence for a key-press (incremental duration + redundant end),
and a receiver that emits each pressed digit exactly once despite RFC 4733's
triplicated end packets.

bk354: DtmfReceiver._order/_seen replaced with a single insertion-ordered dict.
bk366: feed() returns DtmfPress | DtmfNoPress instead of str | None (ADR-0077).
"""

import pytest

from hermes_voip.dtmf import (
    DtmfEvent,
    DtmfNoPress,
    DtmfPress,
    DtmfReceiver,
    digit_to_event,
    event_payloads,
    event_to_digit,
)


def test_event_codec_round_trip() -> None:
    event = DtmfEvent(event=1, end=True, volume=10, duration=160)
    raw = event.encode()
    assert len(raw) == 4
    back = DtmfEvent.decode(raw)
    assert back == event


def test_pinned_payload_bytes() -> None:
    raw = DtmfEvent(event=5, end=True, volume=10, duration=800).encode()
    assert raw[0] == 0x05  # event 5
    assert raw[1] == 0x8A  # E=1 (0x80) | volume 10
    assert raw[2:4] == (800).to_bytes(2, "big")  # duration


def test_decode_clears_end_bit_and_reads_volume() -> None:
    event = DtmfEvent.decode(
        bytes([0x0B, 0x1F, 0x00, 0x50])
    )  # event 11 (#), E=0, vol 31
    assert event.event == 11
    assert event.end is False
    assert event.volume == 31
    assert event.duration == 0x50


def test_decode_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="4 bytes"):
        DtmfEvent.decode(b"\x00\x00\x00")


def test_digit_event_mapping_full_keypad() -> None:
    assert [digit_to_event(d) for d in "0123456789"] == list(range(10))
    assert digit_to_event("*") == 10
    assert digit_to_event("#") == 11
    assert [digit_to_event(d) for d in "ABCD"] == [12, 13, 14, 15]
    assert digit_to_event("a") == 12  # case-insensitive
    assert event_to_digit(0) == "0"
    assert event_to_digit(11) == "#"


def test_digit_mapping_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="DTMF digit"):
        digit_to_event("X")
    with pytest.raises(ValueError, match="DTMF event"):
        event_to_digit(99)


def test_event_payloads_for_keypress_end_with_redundant_end_packets() -> None:
    payloads = list(event_payloads("5", total_duration=480, step=160))
    events = [DtmfEvent.decode(p) for p in payloads]
    assert all(e.event == 5 for e in events)
    # increasing duration across the tone, then 3 redundant end packets (RFC 4733)
    assert [e.duration for e in events if not e.end] == [160, 320, 480]
    end_packets = [e for e in events if e.end]
    assert len(end_packets) == 3
    assert all(e.duration == 480 for e in end_packets)


# ---------------------------------------------------------------------------
# bk366: DtmfReceiver.feed() returns DtmfPress | DtmfNoPress (ADR-0077)
# ---------------------------------------------------------------------------


def test_dtmf_press_carries_digit() -> None:
    """DtmfPress is a frozen dataclass with a digit field.

    A completed key-press returns DtmfPress(digit='3'), not the bare string '3'.
    This lets callers distinguish a press from every no-press case via isinstance.
    """
    press = DtmfPress(digit="3")
    assert press.digit == "3"
    # immutable
    with pytest.raises((AttributeError, TypeError)):
        # Justification for type: ignore[misc] below: mypy statically rejects
        # assignment to a frozen dataclass field — that IS the behaviour under
        # test; the assignment must raise FrozenInstanceError at runtime.
        press.digit = "5"  # type: ignore[misc]


def test_dtmf_no_press_variants_exist() -> None:
    """DtmfNoPress has four distinguishable no-press cases (ADR-0077).

    Callers who previously tested 'result is None' must now pick the right variant:
      STILL_PRESSING    — end bit not set; tone still in progress
      DUPLICATE_END     — end bit set but this timestamp was already recorded
                          with the SAME event code (ordinary RFC 4733 redundancy)
      CONFLICTING_EVENT — end bit set but this timestamp was already recorded
                          with a DIFFERENT event code (a forged/mismatching
                          packet racing the genuine one — never a press)
      NON_DIGIT_EVENT   — end bit set, new timestamp, but the event is not a
                          keypad digit
    """
    assert DtmfNoPress.STILL_PRESSING is DtmfNoPress.STILL_PRESSING
    assert DtmfNoPress.DUPLICATE_END is DtmfNoPress.DUPLICATE_END
    assert DtmfNoPress.CONFLICTING_EVENT is DtmfNoPress.CONFLICTING_EVENT
    assert DtmfNoPress.NON_DIGIT_EVENT is DtmfNoPress.NON_DIGIT_EVENT
    # All four must be distinct
    variants = {
        DtmfNoPress.STILL_PRESSING,
        DtmfNoPress.DUPLICATE_END,
        DtmfNoPress.CONFLICTING_EVENT,
        DtmfNoPress.NON_DIGIT_EVENT,
    }
    assert len(variants) == 4


def test_receiver_emits_press_once_and_suppresses_duplicates() -> None:
    """feed() returns DtmfPress on first end packet and DtmfNoPress for others.

    Replaces test_receiver_emits_digit_once_per_press with the new API (bk366).
    The two start/update packets yield STILL_PRESSING; the first end packet yields
    DtmfPress('3'); the two redundant end packets yield DUPLICATE_END.
    """
    rx = DtmfReceiver()
    # two start/update packets — not end, must be STILL_PRESSING
    result = rx.feed(
        DtmfEvent(event=3, end=False, volume=10, duration=160), timestamp=1000
    )
    assert result is DtmfNoPress.STILL_PRESSING
    result = rx.feed(
        DtmfEvent(event=3, end=False, volume=10, duration=320), timestamp=1000
    )
    assert result is DtmfNoPress.STILL_PRESSING
    # first end packet — new timestamp, keypad digit → DtmfPress
    result = rx.feed(
        DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=1000
    )
    assert result == DtmfPress(digit="3")
    # redundant end packets — same timestamp already seen → DUPLICATE_END
    result = rx.feed(
        DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=1000
    )
    assert result is DtmfNoPress.DUPLICATE_END
    result = rx.feed(
        DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=1000
    )
    assert result is DtmfNoPress.DUPLICATE_END


def test_receiver_distinguishes_repeated_digit_by_timestamp() -> None:
    """Two presses of the same digit with different timestamps each yield DtmfPress.

    New timestamp = new press, regardless of the digit character (bk366).
    """
    rx = DtmfReceiver()
    assert rx.feed(
        DtmfEvent(event=7, end=True, volume=10, duration=480), timestamp=1000
    ) == DtmfPress(digit="7")
    # same digit pressed again -> new RTP timestamp -> a distinct DtmfPress
    assert rx.feed(
        DtmfEvent(event=7, end=True, volume=10, duration=480), timestamp=2000
    ) == DtmfPress(digit="7")


def test_receiver_non_digit_event_yields_non_digit_variant() -> None:
    """A flash event (event=16) on a new end packet yields NON_DIGIT_EVENT (bk366).

    Previously both a duplicate end AND a non-digit event were indistinguishable None.
    Now NON_DIGIT_EVENT lets callers handle them explicitly.
    """
    rx = DtmfReceiver()
    # event=16 is flash — valid telephone-event, but NOT a keypad digit
    result = rx.feed(
        DtmfEvent(event=16, end=True, volume=10, duration=480), timestamp=5000
    )
    assert result is DtmfNoPress.NON_DIGIT_EVENT


def test_receiver_dedups_reordered_end_packets() -> None:
    """Late duplicate end packet after the window is suppressed as DUPLICATE_END.

    Tests the old test_receiver_dedups_reordered_end_packets semantics with the
    new API: a re-ordered end packet for an already-recorded timestamp is
    DUPLICATE_END, not a new DtmfPress (bk366).
    """
    rx = DtmfReceiver()
    assert rx.feed(
        DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=1000
    ) == DtmfPress(digit="3")
    assert rx.feed(
        DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=2000
    ) == DtmfPress(digit="3")
    # a late duplicate of the first press must NOT double-emit
    assert (
        rx.feed(DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=1000)
        is DtmfNoPress.DUPLICATE_END
    )


# ---------------------------------------------------------------------------
# Security: conflicting-event (digit-substitution) rejection
#
# DTMF is the ADR-0009 spoof-resistant confirmation channel (ADR-0010): a
# forged or otherwise mismatching telephone-event packet arriving at the SAME
# RTP timestamp as a genuine key-press must never be able to substitute a
# different digit for the one the real press produced, and the anomaly must
# be distinguishable from ordinary RFC 4733 redundancy (agreeing duplicate end
# packets), which must keep collapsing to a single press exactly as before.
#
# THE property is about the DANGEROUS ordering (forged arrives FIRST, before
# the genuine packet) -- not the safe ordering (genuine first, forged second).
# A same-timestamp conflict can only ever be DETECTED on the packet that
# disagrees with whatever was seen already, so a design that trusts the very
# FIRST end packet it ever sees for a timestamp (unconditionally, before any
# chance of a disagreeing packet arriving) cannot close this gap no matter how
# the SECOND packet is classified. The test below is the one that actually
# proves the property; it fails against a receiver that trusts on first sight.
# ---------------------------------------------------------------------------


def test_receiver_never_accepts_forged_digit_that_arrives_first() -> None:
    """A forged end-event racing ahead of the genuine one must never win.

    This is THE security property, and this is the dangerous ordering the
    substitution vulnerability actually
    depends on. Trusting whichever end packet arrives FIRST for a timestamp
    (unconditionally) leaves this ordering wide open even if a later,
    disagreeing packet at the same timestamp is correctly labelled a
    conflict: relabelling the second, already-too-late packet does nothing to
    stop the first (forged) one from having already been accepted as the
    digit the system acts on. Proving this property requires the dangerous
    order specifically -- forged, then genuine -- not the safe order (genuine
    then forged).
    """
    rx = DtmfReceiver()
    # The attacker's forged end(9) wins the race and arrives FIRST.
    forged_first = rx.feed(
        DtmfEvent(event=9, end=True, volume=10, duration=480), timestamp=1000
    )
    assert not isinstance(forged_first, DtmfPress), (
        "a single, uncorroborated end packet must never be trusted as a "
        "press outright -- that is exactly what a forged packet racing the "
        "genuine one looks like from the receiver's point of view, since "
        "the receiver cannot yet know whether a disagreeing packet is about "
        "to arrive at the same timestamp"
    )
    # The genuine end(3) arrives second, at the same timestamp, disagreeing.
    genuine_second = rx.feed(
        DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=1000
    )
    assert genuine_second != DtmfPress(digit="9"), (
        "the forged digit must never be substituted for the real one"
    )
    assert not isinstance(genuine_second, DtmfPress), (
        "once a same-timestamp conflict is seen, NEITHER side can be "
        "trusted -- there is no way to know which packet was genuine, so "
        "the receiver must fail safe (no digit emitted) rather than fail "
        "open (the wrong digit accepted)"
    )


def test_receiver_rejects_conflicting_event_at_same_timestamp() -> None:
    """A same-timestamp mismatching event must never substitute a digit.

    It must also never be silently conflated with the digit already reported
    for that timestamp.
    """
    rx = DtmfReceiver()
    genuine = rx.feed(
        DtmfEvent(event=5, end=True, volume=10, duration=480), timestamp=1000
    )
    assert genuine == DtmfPress(digit="5")
    # A later packet at the SAME timestamp but a DIFFERENT event code (a
    # forged telephone-event payload, or two overlapping senders) must never
    # substitute the digit already reported, and must not be silently
    # conflated with an ordinary agreeing-event redundant duplicate.
    forged = rx.feed(
        DtmfEvent(event=9, end=True, volume=10, duration=480), timestamp=1000
    )
    assert not isinstance(forged, DtmfPress), (
        "a mismatching event at an already-decided timestamp must never "
        "produce a DtmfPress — that would substitute the wrong digit"
    )
    assert forged is not DtmfNoPress.DUPLICATE_END, (
        "a conflicting event must be distinguishable from an ordinary "
        "agreeing-event redundant duplicate"
    )
    assert forged is DtmfNoPress.CONFLICTING_EVENT


def test_receiver_still_dedups_agreeing_duplicate_after_conflict_probe() -> None:
    """A conflicting probe must not corrupt dedup of the original digit.

    The original digit's own genuine redundant end packets arriving
    afterwards must still be recognised as ordinary duplicates.
    """
    rx = DtmfReceiver()
    assert rx.feed(
        DtmfEvent(event=5, end=True, volume=10, duration=480), timestamp=1000
    ) == DtmfPress(digit="5")
    assert (
        rx.feed(DtmfEvent(event=9, end=True, volume=10, duration=480), timestamp=1000)
        is DtmfNoPress.CONFLICTING_EVENT
    )
    # The genuine digit's own redundant end packet (matching event=5) must
    # still be recognised as an ordinary duplicate, not another conflict.
    assert (
        rx.feed(DtmfEvent(event=5, end=True, volume=10, duration=480), timestamp=1000)
        is DtmfNoPress.DUPLICATE_END
    )


# --- hardening per cross-vendor review ---


def test_digit_to_event_rejects_empty_and_multichar() -> None:
    for bad in ("", "12", "*#"):
        with pytest.raises(ValueError, match="DTMF digit"):
            digit_to_event(bad)


def test_event_payloads_validates_step_and_duration() -> None:
    with pytest.raises(ValueError, match="step"):
        list(event_payloads("5", total_duration=480, step=0))
    with pytest.raises(ValueError, match="step"):
        list(event_payloads("5", total_duration=480, step=-160))
    with pytest.raises(ValueError, match="total_duration"):
        list(event_payloads("5", total_duration=0, step=160))
    with pytest.raises(ValueError, match="total_duration"):
        list(event_payloads("5", total_duration=70000, step=160))


def test_receiver_rejects_history_zero_and_negative() -> None:
    """DtmfReceiver must raise ValueError when history < 1.

    history=0 makes _order a maxlen=0 deque (no-op) so the eviction guard never
    fires; _seen grows unbounded for the call's lifetime — a memory leak that
    silently breaks the bounded-window dedup contract (Wave-3 audit).
    history=-1 is equally invalid.  The guard must fail fast at construction.
    """
    with pytest.raises(ValueError, match="history"):
        DtmfReceiver(history=0)
    with pytest.raises(ValueError, match="history"):
        DtmfReceiver(history=-1)


# ---------------------------------------------------------------------------
# Mutation-hardening: encode() end-bit / volume byte (backlog §370)
# ---------------------------------------------------------------------------


def test_encode_end_false_volume_10() -> None:
    """end=False must leave the 0x80 bit clear in byte 1.

    Byte 1 layout: E(bit7) | volume(bits5-0).
    end=False, volume=10 -> 0x00 | 0x0A = 0x0A.

    Kills a mutant that replaces `0` with `_END_BIT` in the false branch of
    ``(_END_BIT if self.end else 0) | self.volume``, and a mutant that drops the
    conditional entirely (always OR-ing END_BIT).
    """
    raw = DtmfEvent(event=5, end=False, volume=10, duration=160).encode()
    assert raw[1] == 0x0A  # E=0, volume=10


def test_encode_end_true_volume_zero() -> None:
    """end=True with volume=0 must produce exactly 0x80 (only the end bit set).

    Kills a mutant that replaces ``|`` with ``&`` (result would be 0x00 for
    volume=0) and a mutant that always clears the end bit (result 0x00).
    """
    raw = DtmfEvent(event=5, end=True, volume=0, duration=160).encode()
    assert raw[1] == 0x80  # E=1, volume=0


def test_encode_end_false_volume_zero() -> None:
    """end=False, volume=0 -> byte 1 must be 0x00 (no bits set).

    Differentiates a dropped-END_BIT mutant from a swapped-branch mutant:
    a ``always-set-end-bit`` mutant would produce 0x80 here.
    """
    raw = DtmfEvent(event=5, end=False, volume=0, duration=160).encode()
    assert raw[1] == 0x00  # E=0, volume=0


def test_encode_end_true_volume_max() -> None:
    """end=True, volume=63 (0x3F) -> byte 1 must be 0xBF (all 7 payload bits set).

    0x80 | 0x3F = 0xBF.  A ``|``->``&`` mutant yields 0x00; an end-bit-drop
    mutant yields 0x3F; a VOLUME_MASK misapplication on encode yields a different
    value if volume bits bleed into bit 7.
    """
    raw = DtmfEvent(event=5, end=True, volume=0x3F, duration=160).encode()
    assert raw[1] == 0xBF  # E=1, volume=63


def test_encode_end_false_volume_max() -> None:
    """end=False, volume=63 (0x3F) -> byte 1 must be 0x3F (volume only, no end bit).

    Distinguishes from end=True,volume=max (0xBF).  Kills a mutant that
    always sets the end bit regardless of self.end.
    """
    raw = DtmfEvent(event=5, end=False, volume=0x3F, duration=160).encode()
    assert raw[1] == 0x3F  # E=0, volume=63


# ---------------------------------------------------------------------------
# Mutation-hardening: DtmfReceiver bounded-window eviction (backlog §373)
# Updated to use DtmfPress | DtmfNoPress API (bk366).
# ---------------------------------------------------------------------------


def test_receiver_bounded_window_evicts_oldest_timestamp() -> None:
    """With history=2 the window holds exactly 2 timestamps; the third evicts the first.

    Sequence:
      1. Press ts=1000 -> DtmfPress('3').  _window={1000: None}.
      2. Press ts=2000 -> DtmfPress('3').  _window={1000: None, 2000: None}.
      3. Press ts=3000 -> DtmfPress('3').  Window is full; ts=1000 is evicted before
         ts=3000 is added.  _window={2000: None, 3000: None}.
      4. Late duplicate of ts=1000 arrives.  Because 1000 was evicted from _window,
         the receiver treats it as a NEW press and returns DtmfPress('3') again.

    This pins the eviction-on-overflow behaviour: a mutant that never discards
    from the window would return DUPLICATE_END at step 4 instead of re-emitting.
    It also verifies that the single-structure (bk354) preserves the old eviction
    semantics exactly — _window acts as an insertion-ordered bounded set.
    """
    rx = DtmfReceiver(history=2)
    ev = DtmfEvent(event=3, end=True, volume=10, duration=480)

    # press 1: ts=1000
    assert rx.feed(ev, timestamp=1000) == DtmfPress(digit="3")
    # press 2: ts=2000 — window now full
    assert rx.feed(ev, timestamp=2000) == DtmfPress(digit="3")
    # press 3: ts=3000 — evicts ts=1000 from _window
    assert rx.feed(ev, timestamp=3000) == DtmfPress(digit="3")
    # late duplicate of ts=1000: evicted, so re-emits as a new press
    assert rx.feed(ev, timestamp=1000) == DtmfPress(digit="3"), (
        "evicted timestamp must re-emit as DtmfPress; _window must not retain it "
        "past the bounded window"
    )


def test_receiver_bounded_window_retains_recent_timestamps() -> None:
    """Timestamps still within the window yield DUPLICATE_END (dedup still works).

    With history=2 and three presses (ts 1000, 2000, 3000), ts=2000 and ts=3000
    remain in the window.  A duplicate of either must return DUPLICATE_END.

    Kills a mutant that evicts too aggressively (e.g. evicts all entries instead
    of just the oldest).
    """
    rx = DtmfReceiver(history=2)
    ev = DtmfEvent(event=3, end=True, volume=10, duration=480)
    rx.feed(ev, timestamp=1000)
    rx.feed(ev, timestamp=2000)
    rx.feed(ev, timestamp=3000)

    # ts=2000 and ts=3000 are still in the window — must be DUPLICATE_END
    assert rx.feed(ev, timestamp=2000) is DtmfNoPress.DUPLICATE_END
    assert rx.feed(ev, timestamp=3000) is DtmfNoPress.DUPLICATE_END


# ---------------------------------------------------------------------------
# bk354: single _window structure replaces _order + _seen (structural test)
# ---------------------------------------------------------------------------


def test_receiver_has_no_separate_order_and_seen_attrs() -> None:
    """DtmfReceiver must NOT expose _order and _seen as separate attributes (bk354).

    The two-structure sync (deque + set) is replaced by a single insertion-ordered
    dict (_window). A mutant that keeps both would fail this test.
    """
    rx = DtmfReceiver()
    assert not hasattr(rx, "_order"), "_order must be removed (bk354 consolidation)"
    assert not hasattr(rx, "_seen"), "_seen must be removed (bk354 consolidation)"
    assert hasattr(rx, "_window"), "_window must be the single dedup structure (bk354)"


# ---------------------------------------------------------------------------
# DtmfEvent.__post_init__ boundary validation (backlog ~393-410)
# ---------------------------------------------------------------------------


def test_dtmf_event_accepts_event_at_boundary_255() -> None:
    """event=255 (0xFF, max valid) must be accepted without error.

    Kills a mutant that uses > instead of <= in the boundary check.
    """
    event = DtmfEvent(event=255, end=False, volume=10, duration=100)
    assert event.event == 255


def test_dtmf_event_rejects_event_above_boundary_256() -> None:
    """event=256 exceeds 0xFF and must raise ValueError.

    Kills a mutant that uses >= instead of > in the rejection check.
    """
    with pytest.raises(ValueError, match="event out of range"):
        DtmfEvent(event=256, end=False, volume=10, duration=100)


def test_dtmf_event_accepts_volume_at_boundary_63() -> None:
    """volume=63 (0x3F, max valid) must be accepted without error.

    Kills a mutant that uses > instead of <= in the boundary check.
    """
    event = DtmfEvent(event=0, end=False, volume=63, duration=100)
    assert event.volume == 63


def test_dtmf_event_rejects_volume_above_boundary_64() -> None:
    """volume=64 exceeds 0x3F and must raise ValueError.

    Kills a mutant that uses >= instead of > in the rejection check.
    """
    with pytest.raises(ValueError, match="volume out of range"):
        DtmfEvent(event=0, end=False, volume=64, duration=100)


def test_dtmf_event_accepts_duration_at_boundary_65535() -> None:
    """duration=65535 (0xFFFF, max valid) must be accepted without error.

    Kills a mutant that uses > instead of <= in the boundary check.
    """
    event = DtmfEvent(event=0, end=False, volume=10, duration=65535)
    assert event.duration == 65535


def test_dtmf_event_rejects_duration_above_boundary_65536() -> None:
    """duration=65536 exceeds 0xFFFF and must raise ValueError.

    Kills a mutant that uses >= instead of > in the rejection check.
    """
    with pytest.raises(ValueError, match="duration out of range"):
        DtmfEvent(event=0, end=False, volume=10, duration=65536)


# ---------------------------------------------------------------------------
# New tests: feed non-digit / start-only paths + event_payloads step guard
# ---------------------------------------------------------------------------


def test_feed_non_digit_returns_none() -> None:
    """Event code 16 (flash) is not a keypad digit; feed returns NON_DIGIT_EVENT.

    Re-feeding the same timestamp returns DUPLICATE_END (the timestamp is still
    recorded in the window even for non-digit ends, so the dedup fires correctly).
    """
    rx = DtmfReceiver(history=4)
    flash = DtmfEvent(event=16, end=True, volume=10, duration=100)
    result = rx.feed(flash, timestamp=100)
    assert result is DtmfNoPress.NON_DIGIT_EVENT
    # Re-feeding the same timestamp must be deduped, not re-surfaced.
    result2 = rx.feed(flash, timestamp=100)
    assert result2 is DtmfNoPress.DUPLICATE_END


def test_start_only_press_never_emits() -> None:
    """Non-end packets for digit '1' at ts 200 always return STILL_PRESSING.

    The end bit is not set, so no press is ever emitted regardless of how many
    packets arrive.
    """
    rx = DtmfReceiver(history=4)
    for dur in (80, 160, 240):
        pkt = DtmfEvent(event=1, end=False, volume=10, duration=dur)
        assert rx.feed(pkt, timestamp=200) is DtmfNoPress.STILL_PRESSING


def test_lone_end_packet_emits_once() -> None:
    """A single end-bit packet for '1' at ts 300 emits DtmfPress('1') exactly once.

    Re-feeding the same timestamp returns DUPLICATE_END.
    """
    rx = DtmfReceiver(history=4)
    end_pkt = DtmfEvent(event=1, end=True, volume=10, duration=160)
    result = rx.feed(end_pkt, timestamp=300)
    assert result == DtmfPress(digit="1")
    # Redundant end at same timestamp must be suppressed.
    result2 = rx.feed(end_pkt, timestamp=300)
    assert result2 is DtmfNoPress.DUPLICATE_END


def test_event_payloads_step_exceeds_duration_clamps() -> None:
    """event_payloads('1', step=1000, total_duration=160) must yield exactly 4 packets.

    When step >= total_duration the function must not loop forever or skip the
    final packet; instead it clamps and emits the single final-duration packet
    followed by the three redundant end packets — four packets total.  The first
    packet must carry duration=160 and end=False; each of the last three must carry
    end=True and duration=160.
    """
    packets = list(event_payloads("1", step=1000, total_duration=160))
    # One non-end packet + three redundant end packets.
    assert len(packets) == 4
    first = DtmfEvent.decode(packets[0])
    assert first.end is False
    assert first.duration == 160
    for raw in packets[1:]:
        ep = DtmfEvent.decode(raw)
        assert ep.end is True
        assert ep.duration == 160
