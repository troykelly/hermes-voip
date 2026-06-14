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
