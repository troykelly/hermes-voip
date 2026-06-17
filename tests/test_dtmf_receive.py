"""Tests for inbound RFC 4733 DTMF receive wiring in RtpMediaTransport (ADR-0010).

The DTMF SEND path (``send_dtmf``) shipped in PR #100; this suite drives the
RECEIVE half: inbound RTP whose payload type equals the NEGOTIATED telephone-event
PT is demuxed to a per-call :class:`~hermes_voip.dtmf.DtmfReceiver` and each decoded
digit is emitted exactly once (the RFC 4733 redundant-end collapse) via the engine's
``on_dtmf`` callback — BEFORE the jitter buffer, so a telephone-event packet is never
decoded as audio garbage. Ordinary audio packets are unaffected, and when no
telephone-event PT was negotiated the demux is inert (a stray DTMF packet is simply a
foreign-PT packet the audio path drops).

White-box: inbound packets are injected directly into the engine's ``_recv_queue``
(a ``(datagram, source-addr)`` tuple) exactly as the existing engine tests do, so the
test is deterministic with no real UDP timing.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from hermes_voip.dtmf import DtmfEvent, event_payloads
from hermes_voip.media.audio import G711_SAMPLE_RATE, encode_ulaw
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.rtp import RtpPacket

# The negotiated telephone-event payload type for these tests (a fake dynamic PT —
# the engine must use the NEGOTIATED value, never a hardcoded 101).
_TE_PT = 96
_AUDIO_PT = 0  # PCMU static PT
_FAKE_SSRC = 0xDEADBEEF
_FAKE_SRC: tuple[str, int] = ("198.51.100.7", 6000)

_PTIME_MS = 20
_SAMPLES_PER_FRAME = (G711_SAMPLE_RATE * _PTIME_MS) // 1000  # 160
_PCM_SILENCE = b"\x00" * (_SAMPLES_PER_FRAME * 2)


def _dummy_clock() -> int:
    return 0


def _ulaw_silence() -> bytes:
    return encode_ulaw(_PCM_SILENCE)


def _audio_rtp(seq: int, ts: int) -> bytes:
    """One plain-RTP G.711 audio datagram at the audio payload type."""
    return RtpPacket(
        payload_type=_AUDIO_PT,
        sequence_number=seq,
        timestamp=ts,
        ssrc=_FAKE_SSRC,
        payload=_ulaw_silence(),
    ).pack()


def _dtmf_rtp(
    seq: int, ts: int, payload: bytes, *, marker: bool, pt: int = _TE_PT
) -> bytes:
    """One telephone-event RTP datagram carrying a 4-byte named-event payload."""
    return RtpPacket(
        payload_type=pt,
        sequence_number=seq,
        timestamp=ts,
        ssrc=_FAKE_SSRC,
        payload=payload,
        marker=marker,
    ).pack()


def _digit_event_train(digit: str, *, start_seq: int, ts: int) -> list[bytes]:
    """Build the inbound RTP packets for one key-press of ``digit``.

    Mirrors what a gateway sends: the named-event update packets (growing duration)
    and the three redundant end packets, all at one RTP timestamp, with the marker
    bit set on the first packet only (RFC 4733 §2.5.1).
    """
    payloads = list(event_payloads(digit, total_duration=480, step=160))
    packets: list[bytes] = []
    for i, payload in enumerate(payloads):
        packets.append(_dtmf_rtp(start_seq + i, ts, payload, marker=(i == 0)))
    return packets


def _make_engine(*, telephone_event_payload_type: int | None) -> RtpMediaTransport:
    digits: list[str] = []
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        payload_type=_AUDIO_PT,
        telephone_event_payload_type=telephone_event_payload_type,
        jitter_depth=1,
        clock=_dummy_clock,
        on_dtmf=digits.append,
    )
    # Expose the collected digits on the engine object for the test to read.
    engine._test_digits = digits  # type: ignore[attr-defined]  # white-box test seam
    return engine


@pytest.mark.asyncio
async def test_inbound_telephone_event_emits_single_digit() -> None:
    """A telephone-event train decodes to exactly one digit (no end-packet dupes).

    The engine demuxes the negotiated-PT packets to a DtmfReceiver and fires the
    ``on_dtmf`` callback once for the press, collapsing the three redundant end
    packets. The telephone-event packets must NOT reach the jitter buffer/decoder,
    so the inbound audio iterator yields NOTHING for this train.
    """
    engine = _make_engine(telephone_event_payload_type=_TE_PT)
    await engine.connect()
    frames: list[PcmFrame] = []

    async def _collect() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    for packet in _digit_event_train("5", start_seq=0, ts=1000):
        engine._recv_queue.put_nowait((packet, _FAKE_SRC))

    await asyncio.sleep(0.05)

    assert engine._test_digits == ["5"]  # type: ignore[attr-defined]
    assert frames == []  # telephone-event packets never decoded as audio

    await engine.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_inbound_multiple_digits_in_order_no_dupes() -> None:
    """Several presses (each its own RTP timestamp) decode to the right digits."""
    engine = _make_engine(telephone_event_payload_type=_TE_PT)
    await engine.connect()

    async def _collect() -> None:
        async for _frame in engine.inbound_audio():
            pass

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    # Press "1", "2", "3" — distinct RTP timestamps, each with redundant end packets.
    seq = 0
    for digit, ts in (("1", 1000), ("2", 2000), ("3", 3000)):
        for packet in _digit_event_train(digit, start_seq=seq, ts=ts):
            engine._recv_queue.put_nowait((packet, _FAKE_SRC))
            seq += 1

    await asyncio.sleep(0.05)

    assert engine._test_digits == ["1", "2", "3"]  # type: ignore[attr-defined]

    await engine.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_audio_packets_unaffected_by_dtmf_demux() -> None:
    """Audio packets continue to decode to PcmFrames; only DTMF PT is diverted.

    Interleave one DTMF press between audio packets: the audio frames must still
    be yielded (count unchanged), and the digit surfaced once.
    """
    engine = _make_engine(telephone_event_payload_type=_TE_PT)
    await engine.connect()
    frames: list[PcmFrame] = []
    done = asyncio.Event()

    async def _collect() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)
            if len(frames) == 3:
                done.set()
                return

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    # audio seq 0, a DTMF press (4 packets at its own seqs), then audio seq 1, 2.
    engine._recv_queue.put_nowait((_audio_rtp(0, 0), _FAKE_SRC))
    for packet in _digit_event_train("7", start_seq=100, ts=5000):
        engine._recv_queue.put_nowait((packet, _FAKE_SRC))
    engine._recv_queue.put_nowait((_audio_rtp(1, _SAMPLES_PER_FRAME), _FAKE_SRC))
    engine._recv_queue.put_nowait((_audio_rtp(2, 2 * _SAMPLES_PER_FRAME), _FAKE_SRC))

    await asyncio.wait_for(done.wait(), timeout=2.0)

    assert len(frames) == 3
    for frame in frames:
        assert frame.sample_rate == G711_SAMPLE_RATE
    assert engine._test_digits == ["7"]  # type: ignore[attr-defined]

    await engine.stop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_no_negotiated_telephone_event_does_not_decode_dtmf() -> None:
    """With no negotiated telephone-event PT, DTMF demux is inert.

    A telephone-event-shaped packet at PT 96 when none was negotiated is just a
    foreign payload type: it must NOT fire ``on_dtmf`` (there is no DtmfReceiver),
    and — being neither the audio PT nor a latched stream — it produces no decoded
    audio frame either (the jitter buffer would mis-decode a 4-byte payload, so the
    demux must drop unknown PTs rather than feed them downstream).
    """
    engine = _make_engine(telephone_event_payload_type=None)
    await engine.connect()
    frames: list[PcmFrame] = []

    async def _collect() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    for packet in _digit_event_train("9", start_seq=0, ts=1000):
        engine._recv_queue.put_nowait((packet, _FAKE_SRC))

    await asyncio.sleep(0.05)

    assert engine._test_digits == []  # type: ignore[attr-defined]
    assert frames == []

    await engine.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_dtmf_demux_survives_short_payload() -> None:
    """A malformed (non-4-byte) telephone-event payload is dropped, not fatal.

    A truncated named-event payload must not crash the inbound generator: it is
    dropped (logged) and the stream continues, surfacing a subsequent valid press.
    """
    engine = _make_engine(telephone_event_payload_type=_TE_PT)
    await engine.connect()

    async def _collect() -> None:
        async for _frame in engine.inbound_audio():
            pass

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    # A 2-byte (truncated) telephone-event payload at the negotiated PT.
    bad = _dtmf_rtp(0, 1000, b"\x05\x8a", marker=True)
    engine._recv_queue.put_nowait((bad, _FAKE_SRC))
    # Then a valid press that must still be surfaced.
    for packet in _digit_event_train("4", start_seq=1, ts=2000):
        engine._recv_queue.put_nowait((packet, _FAKE_SRC))

    await asyncio.sleep(0.05)

    assert engine._test_digits == ["4"]  # type: ignore[attr-defined]

    await engine.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_on_dtmf_property_round_trips() -> None:
    """The ``on_dtmf`` callback is settable after construction (outbound path)."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
    )
    assert engine.on_dtmf is None
    seen: list[str] = []
    engine.on_dtmf = seen.append
    assert engine.on_dtmf is not None
    # A direct decode of an end packet emits the digit through the callback chain.
    engine.on_dtmf("2")
    assert seen == ["2"]


def test_decode_named_event_end_packet() -> None:
    """Sanity: the end-bit packet a gateway sends decodes to the pressed digit."""
    payload = DtmfEvent(event=5, end=True, volume=10, duration=480).encode()
    event = DtmfEvent.decode(payload)
    assert event.event == 5
    assert event.end is True
