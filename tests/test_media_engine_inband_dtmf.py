"""Engine-level in-band DTMF: send (tone generation) + receive (Goertzel).

The RFC 4733 DTMF send/receive paths ship already; ADR-0034 adds the in-band
backend for a G.711 call that negotiated no telephone-event. On SEND, the engine
synthesises dual-tone PCM and sends it on the normal audio TX path (encode + 20 ms
framing + SRTP). On RECEIVE, the inbound generator runs Goertzel detection on the
AEC-cleaned decoded frame and fires ``on_dtmf``.

White-box: TX is captured via a stand-in transport; RX is injected directly into
the engine's ``_recv_queue`` (the same seam the RFC 4733 receive tests use).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from hermes_voip.dtmf import DtmfSendMode, InbandDtmfDetector, inband_tone_pcm
from hermes_voip.media.audio import (
    G711_SAMPLE_RATE,
    decode_ulaw,
    encode_ulaw,
)
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.rtp import RtpPacket

_AUDIO_PT = 0  # PCMU
_FAKE_SSRC = 0xDEADBEEF
_FAKE_SRC: tuple[str, int] = ("198.51.100.7", 6000)
_PTIME_MS = 20
_SAMPLES_PER_FRAME = (G711_SAMPLE_RATE * _PTIME_MS) // 1000  # 160


class _Clock:
    def __init__(self) -> None:
        self._t = 0.0

    def now(self) -> float:
        return self._t

    async def sleep(self, secs: float) -> None:
        if secs > 0:
            self._t += secs


class _CapturingTransport:
    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def sendto(self, data: bytes, addr: tuple[str, int] | None = None) -> None:
        assert addr is not None
        self.packets.append(data)

    def close(self) -> None:
        """No-op."""

    def is_closing(self) -> bool:
        return False


# --- SEND: in-band tone generation through the audio TX path ----------------


@pytest.mark.asyncio
async def test_send_dtmf_inband_emits_detectable_tones() -> None:
    """In send-mode INBAND, send_dtmf synthesises tones a detector recovers.

    Capture every outbound datagram, decode the G.711 payloads back to PCM, and
    run the Goertzel detector over the concatenation — it must recover the digits.
    """
    clock = _Clock()
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        payload_type=_AUDIO_PT,
        dtmf_send_mode=DtmfSendMode.INBAND,
        sleep=clock.sleep,
        pace_clock=clock.now,
        initial_seq=0,
        initial_ts=0,
    )
    await engine.connect()
    recorder = _CapturingTransport()
    engine._transport = recorder  # white-box seam

    await engine.send_dtmf("19", tone_ms=120, gap_ms=80)

    # Reassemble the sent PCM (audio-PT datagrams only) and detect.
    pcm = bytearray()
    for datagram in recorder.packets:
        pkt = RtpPacket.parse(datagram)
        assert pkt.payload_type == _AUDIO_PT  # in-band rides the audio PT, not 101
        pcm += decode_ulaw(pkt.payload)

    detector = InbandDtmfDetector(sample_rate=G711_SAMPLE_RATE)
    digits: list[str] = []
    for off in range(0, len(pcm) - _SAMPLES_PER_FRAME * 2 + 1, _SAMPLES_PER_FRAME * 2):
        d = detector.feed(bytes(pcm[off : off + _SAMPLES_PER_FRAME * 2]))
        if d is not None:
            digits.append(d)
    assert digits == ["1", "9"]


@pytest.mark.asyncio
async def test_send_dtmf_inband_does_not_require_telephone_event_pt() -> None:
    """In-band send works with NO negotiated telephone-event PT (the whole point)."""
    clock = _Clock()
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        payload_type=_AUDIO_PT,
        telephone_event_payload_type=None,
        dtmf_send_mode=DtmfSendMode.INBAND,
        sleep=clock.sleep,
        pace_clock=clock.now,
        initial_seq=0,
        initial_ts=0,
    )
    await engine.connect()
    recorder = _CapturingTransport()
    engine._transport = recorder
    await engine.send_dtmf("5", tone_ms=100, gap_ms=0)
    assert recorder.packets  # tones went out despite no telephone-event PT


# --- RECEIVE: Goertzel on the inbound audio path ----------------------------


def _tone_datagrams(digit: str, *, n_frames: int, start_seq: int) -> list[bytes]:
    """G.711 audio datagrams carrying ``digit`` as an in-band tone (one press)."""
    pcm = inband_tone_pcm(
        digit, sample_rate=G711_SAMPLE_RATE, duration_ms=n_frames * _PTIME_MS
    )
    out: list[bytes] = []
    for i in range(n_frames):
        frame = pcm[i * _SAMPLES_PER_FRAME * 2 : (i + 1) * _SAMPLES_PER_FRAME * 2]
        out.append(
            RtpPacket(
                payload_type=_AUDIO_PT,
                sequence_number=start_seq + i,
                timestamp=(start_seq + i) * _SAMPLES_PER_FRAME,
                ssrc=_FAKE_SSRC,
                payload=encode_ulaw(frame),
            ).pack()
        )
    return out


def _silence_datagrams(*, n_frames: int, start_seq: int) -> list[bytes]:
    silence = encode_ulaw(b"\x00\x00" * _SAMPLES_PER_FRAME)
    return [
        RtpPacket(
            payload_type=_AUDIO_PT,
            sequence_number=start_seq + i,
            timestamp=(start_seq + i) * _SAMPLES_PER_FRAME,
            ssrc=_FAKE_SSRC,
            payload=silence,
        ).pack()
        for i in range(n_frames)
    ]


@pytest.mark.asyncio
async def test_inband_receive_surfaces_digit() -> None:
    """With in-band RX armed on a G.711 call, a tone in the audio surfaces a digit."""
    digits: list[str] = []
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        payload_type=_AUDIO_PT,
        telephone_event_payload_type=None,
        inband_dtmf_rx_enabled=True,
        on_dtmf=digits.append,
        jitter_depth=1,
        symmetric=False,
        aec_enabled=False,
        clock=lambda: 0,
    )
    await engine.connect()

    async def _drain() -> None:
        async for _frame in engine.inbound_audio():
            pass

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0)

    seq = 0
    for dg in _tone_datagrams("6", n_frames=8, start_seq=seq):
        engine._recv_queue.put_nowait((dg, _FAKE_SRC))
        seq += 8
    for dg in _silence_datagrams(n_frames=6, start_seq=seq):
        engine._recv_queue.put_nowait((dg, _FAKE_SRC))

    await asyncio.sleep(0.05)
    assert digits == ["6"]

    await engine.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_inband_receive_inert_when_not_armed() -> None:
    """Without in-band RX armed, a tone in the audio is just audio (no digit)."""
    digits: list[str] = []
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        payload_type=_AUDIO_PT,
        telephone_event_payload_type=None,
        inband_dtmf_rx_enabled=False,
        on_dtmf=digits.append,
        jitter_depth=1,
        symmetric=False,
        aec_enabled=False,
        clock=lambda: 0,
    )
    await engine.connect()
    frames: list[object] = []

    async def _drain() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0)

    seq = 0
    for dg in _tone_datagrams("6", n_frames=8, start_seq=seq):
        engine._recv_queue.put_nowait((dg, _FAKE_SRC))
        seq += 8

    await asyncio.sleep(0.05)
    assert digits == []  # no detection
    assert frames  # the tone still flowed through as audio

    await engine.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
