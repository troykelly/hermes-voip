"""Tests for hermes_voip.dtmf — RFC 4733 telephone-event handling (ADR-0010).

Covers the 4-byte telephone-event payload codec, the digit<->event mapping, the
outbound payload sequence for a key-press (incremental duration + redundant end),
and a receiver that emits each pressed digit exactly once despite RFC 4733's
triplicated end packets.
"""

import pytest

from hermes_voip.dtmf import (
    DtmfEvent,
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


def test_receiver_emits_digit_once_per_press() -> None:
    rx = DtmfReceiver()
    # one key-press: two start packets, then three redundant end packets, one RTP ts
    assert (
        rx.feed(DtmfEvent(event=3, end=False, volume=10, duration=160), timestamp=1000)
        is None
    )
    assert (
        rx.feed(DtmfEvent(event=3, end=False, volume=10, duration=320), timestamp=1000)
        is None
    )
    assert (
        rx.feed(DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=1000)
        == "3"
    )
    assert (
        rx.feed(DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=1000)
        is None
    )
    assert (
        rx.feed(DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=1000)
        is None
    )


def test_receiver_distinguishes_repeated_digit_by_timestamp() -> None:
    rx = DtmfReceiver()
    assert (
        rx.feed(DtmfEvent(event=7, end=True, volume=10, duration=480), timestamp=1000)
        == "7"
    )
    # same digit pressed again -> new RTP timestamp -> a distinct emission
    assert (
        rx.feed(DtmfEvent(event=7, end=True, volume=10, duration=480), timestamp=2000)
        == "7"
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


def test_receiver_dedups_reordered_end_packets() -> None:
    rx = DtmfReceiver()
    assert (
        rx.feed(DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=1000)
        == "3"
    )
    assert (
        rx.feed(DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=2000)
        == "3"
    )
    # a late duplicate of the first press must NOT double-emit
    assert (
        rx.feed(DtmfEvent(event=3, end=True, volume=10, duration=480), timestamp=1000)
        is None
    )


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
# ---------------------------------------------------------------------------


def test_receiver_bounded_window_evicts_oldest_timestamp() -> None:
    """With history=2 the window holds exactly 2 timestamps; the third evicts the first.

    Sequence:
      1. Press ts=1000 -> emits '3'.  _seen={1000}, _order=[1000].
      2. Press ts=2000 -> emits '3'.  _seen={1000,2000}, _order=[1000,2000].
      3. Press ts=3000 -> emits '3'.  Window is full; ts=1000 is evicted before
         ts=3000 is added.  _seen={2000,3000}, _order=[2000,3000].
      4. Late duplicate of ts=1000 arrives.  Because 1000 was evicted from _seen,
         the receiver treats it as a NEW press and RE-EMITS '3'.

    This pins the eviction-on-overflow behaviour: a mutant that never discards from
    _seen (e.g. removes the ``self._seen.discard`` call) would suppress step 4 instead
    of re-emitting, causing the assertion to fail.
    """
    rx = DtmfReceiver(history=2)
    ev = DtmfEvent(event=3, end=True, volume=10, duration=480)

    # press 1: ts=1000
    assert rx.feed(ev, timestamp=1000) == "3"
    # press 2: ts=2000 — window now full
    assert rx.feed(ev, timestamp=2000) == "3"
    # press 3: ts=3000 — evicts ts=1000 from _seen
    assert rx.feed(ev, timestamp=3000) == "3"
    # late duplicate of ts=1000: evicted, so re-emits
    assert rx.feed(ev, timestamp=1000) == "3", (
        "evicted timestamp must re-emit; _seen must not retain it past the window"
    )


def test_receiver_bounded_window_retains_recent_timestamps() -> None:
    """Timestamps still within the window are suppressed (dedup still works).

    With history=2 and three presses (ts 1000, 2000, 3000), ts=2000 and ts=3000
    remain in the window.  A duplicate of either must NOT re-emit.

    Kills a mutant that evicts too aggressively (e.g. evicts all entries instead
    of just the oldest).
    """
    rx = DtmfReceiver(history=2)
    ev = DtmfEvent(event=3, end=True, volume=10, duration=480)
    rx.feed(ev, timestamp=1000)
    rx.feed(ev, timestamp=2000)
    rx.feed(ev, timestamp=3000)

    # ts=2000 and ts=3000 are still in the window — must be suppressed
    assert rx.feed(ev, timestamp=2000) is None
    assert rx.feed(ev, timestamp=3000) is None
