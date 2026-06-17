"""Opus behaviour of RtpMediaTransport (ADR-0032).

Deterministic, no real network / wall-clock (injected ``sleep``). Verifies the
engine carries Opus end-to-end through the SAME send/recv machinery as G.711 /
G.722: the wire rate is 48 kHz, the RTP clock is also 48 kHz (no quirk, unlike
G.722), a 20 ms frame is 960 samples and the RTP timestamp advances by 960.

opuslib + libopus live in the optional ``webrtc`` extra, so the suite skips on the
default gate (the engine module itself imports without opuslib).
"""

from __future__ import annotations

import contextlib
import math
import socket
import struct
from collections.abc import Iterator

import pytest

from hermes_voip.media.engine import (
    Codec,
    RtpMediaTransport,
    UnsupportedCodecError,
    codec_for_encoding,
)
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.rtp import RtpPacket

# Skip the whole suite unless opuslib (+ libopus) is present.
pytest.importorskip("opuslib", reason="webrtc extra (opuslib) not installed")

from hermes_voip.media.opus import OPUS_FRAME_SAMPLES, OpusDecoder, OpusEncoder

_OPUS_SAMPLE_RATE = 48_000
_OPUS_RTP_CLOCK = 48_000
# Inbound conversational pipeline runs at 16 kHz (Silero VAD cap; ADR-0032).
_OPUS_ANALYSIS_RATE = 16_000
_PTIME_MS = 20
_SAMPLES_PER_FRAME = (_OPUS_SAMPLE_RATE * _PTIME_MS) // 1000  # 960
_RTP_TS_PER_FRAME = (_OPUS_RTP_CLOCK * _PTIME_MS) // 1000  # 960
_OPUS_PT = 111


class _SendRecorder:
    """Minimal DatagramTransport stand-in recording each ``sendto``."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int] | None = None) -> None:
        assert addr is not None
        self.sent.append((bytes(data), addr))

    def close(self) -> None:
        """No-op: owns no socket."""

    def is_closing(self) -> bool:
        return False


@contextlib.contextmanager
def _capture_sends(engine: RtpMediaTransport) -> Iterator[_SendRecorder]:
    recorder = _SendRecorder()
    engine._transport = recorder  # _SendRecorder satisfies the _DatagramSink seam
    try:
        yield recorder
    finally:
        engine._transport = None


def _tone_frame(n_samples: int, freq_hz: float = 600.0) -> PcmFrame:
    """A PCM16 tone frame at the Opus wire rate (48 kHz)."""
    samples = struct.pack(
        f"<{n_samples}h",
        *[
            int(9000 * math.sin(2 * math.pi * freq_hz * n / _OPUS_SAMPLE_RATE))
            for n in range(n_samples)
        ],
    )
    return PcmFrame(samples=samples, sample_rate=_OPUS_SAMPLE_RATE, monotonic_ts_ns=0)


async def _noop_sleep(_seconds: float) -> None:
    return None


def _new_engine() -> RtpMediaTransport:
    return RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=40_000,
        codec=Codec.OPUS,
        payload_type=_OPUS_PT,
        ptime=_PTIME_MS,
        sleep=_noop_sleep,
        initial_seq=100,
        initial_ts=0,
    )


def test_codec_for_encoding_maps_opus() -> None:
    """The engine capability table carries Opus at 48 kHz (rate-aware lookup)."""
    assert codec_for_encoding("opus", 48_000) is Codec.OPUS
    assert codec_for_encoding("OPUS", 48_000) is Codec.OPUS
    # The name alone is insufficient: Opus at the wrong clock is unsupported.
    with pytest.raises(UnsupportedCodecError):
        codec_for_encoding("opus", 8_000)


def test_inbound_sample_rate_is_analysis_rate_for_opus() -> None:
    """Inbound Opus is delivered at the 16 kHz analysis rate, not the 48 kHz wire.

    Silero VAD accepts only 8/16 kHz, so the engine downsamples decoded 48 kHz Opus
    to 16 kHz for the VAD/endpointer/STT pipeline (ADR-0032). The wire/encode rate
    stays 48 kHz (asserted by the send tests).
    """
    assert _new_engine().inbound_sample_rate == _OPUS_ANALYSIS_RATE


@pytest.mark.asyncio
async def test_send_audio_emits_opus_rtp_decodable_back() -> None:
    """send_audio encodes 48 kHz PCM to Opus RTP; the payload decodes to ~960 samples.

    The RTP timestamp advances by 960 per 20 ms frame (clock == audio rate).
    """
    engine = _new_engine()
    await engine.connect()
    with _capture_sends(engine) as rec:
        # Two whole 20 ms frames.
        await engine.send_audio(_tone_frame(2 * _SAMPLES_PER_FRAME))
    await engine.stop()

    assert len(rec.sent) == 2
    pkt0 = RtpPacket.parse(rec.sent[0][0])
    pkt1 = RtpPacket.parse(rec.sent[1][0])
    assert pkt0.payload_type == _OPUS_PT
    # RTP timestamp advances by 960 (48 kHz clock) per frame.
    assert (pkt1.timestamp - pkt0.timestamp) % (1 << 32) == _RTP_TS_PER_FRAME
    # The payload is a real Opus packet: decode it back to one 20 ms 48 kHz frame.
    dec = OpusDecoder()
    decoded = dec.decode(pkt0.payload)
    assert len(decoded) == OPUS_FRAME_SAMPLES * 2


@pytest.mark.asyncio
async def test_inbound_opus_packet_decodes_to_analysis_rate_pcm_frame() -> None:
    """An inbound Opus RTP packet decodes + downsamples to a 16 kHz analysis frame."""
    engine = _new_engine()
    await engine.connect()
    engine_port = engine.local_port

    # Encode one tone frame as Opus and craft an inbound RTP packet from a
    # DIFFERENT SSRC (so the self-loopback drop does not eat it).
    enc = OpusEncoder()
    payload = enc.encode(_tone_frame(_SAMPLES_PER_FRAME).samples)
    inbound = RtpPacket(
        payload_type=_OPUS_PT,
        sequence_number=7,
        timestamp=0,
        ssrc=0x1234_5678,
        payload=payload,
    ).pack()

    sender = socket_send(engine_port, inbound)
    try:
        frame: PcmFrame | None = None
        async for f in engine.inbound_audio():
            frame = f
            break
    finally:
        sender.close()
        await engine.stop()

    assert frame is not None
    # Downsampled 48 kHz -> 16 kHz analysis rate: 960 samples become 320 (640 bytes).
    assert frame.sample_rate == _OPUS_ANALYSIS_RATE
    expected_analysis_samples = (
        OPUS_FRAME_SAMPLES * _OPUS_ANALYSIS_RATE // _OPUS_SAMPLE_RATE
    )
    assert len(frame.samples) == expected_analysis_samples * 2


def socket_send(port: int, data: bytes) -> socket.socket:
    """Send one UDP datagram to ``127.0.0.1:port`` and return the socket to close."""
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.sendto(data, ("127.0.0.1", port))
    return sk


@pytest.mark.asyncio
async def test_opus_round_trip_through_engine_send_and_decode() -> None:
    """A tone sent via send_audio survives Opus and decodes with comparable energy."""
    engine = _new_engine()
    await engine.connect()
    src = _tone_frame(_SAMPLES_PER_FRAME)
    with _capture_sends(engine) as rec:
        # Prime with several frames so the steady-state frame is past codec delay.
        for _ in range(5):
            await engine.send_audio(_tone_frame(_SAMPLES_PER_FRAME))
    await engine.stop()

    dec = OpusDecoder()
    decoded = b""
    for wire, _addr in rec.sent:
        decoded = dec.decode(RtpPacket.parse(wire).payload)
    last_vals = struct.unpack(f"<{OPUS_FRAME_SAMPLES}h", decoded)
    src_vals = struct.unpack(f"<{_SAMPLES_PER_FRAME}h", src.samples)
    rms_src = math.sqrt(sum(v * v for v in src_vals) / len(src_vals))
    rms_out = math.sqrt(sum(v * v for v in last_vals) / len(last_vals))
    assert rms_out > 0.3 * rms_src
