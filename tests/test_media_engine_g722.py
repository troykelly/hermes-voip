"""G.722 wideband behaviour of RtpMediaTransport (ADR-0022).

Deterministic, no real network / wall-clock (injected ``sleep``). Verifies the
engine carries G.722 with the RFC 3551 framing quirk handled: the audio sample
rate is 16 kHz but the RTP clock is 8 kHz, so a 20 ms frame is 320 input samples
-> 160 octets and the RTP timestamp advances by 160 (the 8 kHz clock), NOT 320.

Runs in the DEFAULT (no-extra) gate — the G.722 codec is pure Python.
"""

from __future__ import annotations

import contextlib
import math
import struct
from collections.abc import Iterator

import pytest

from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.media.g722 import G722Decoder
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.rtp import RtpPacket

# G.722: payload type 9, RTP clock 8000, audio 16000 (RFC 3551).
_G722_PT = 9
_G722_RTP_CLOCK = 8_000
_G722_SAMPLE_RATE = 16_000
_PTIME_MS = 20
# 20 ms at the 16 kHz AUDIO rate = 320 samples; the RTP timestamp increment is at
# the 8 kHz CLOCK rate = 160. This split is the whole point of the test.
_AUDIO_SAMPLES_PER_FRAME = (_G722_SAMPLE_RATE * _PTIME_MS) // 1000  # 320
_RTP_TS_PER_FRAME = (_G722_RTP_CLOCK * _PTIME_MS) // 1000  # 160


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
    real = engine._transport
    recorder = _SendRecorder()
    engine._transport = recorder  # type: ignore[assignment]  # narrow stand-in is sufficient for sendto/close
    try:
        yield recorder
    finally:
        engine._transport = real


async def _no_sleep(_secs: float) -> None:
    return None


def _wideband_frame(n_samples: int, freq_hz: float = 5000.0) -> PcmFrame:
    """A 16 kHz PCM16 frame with a tone above the G.711 4 kHz ceiling."""
    samples = [
        int(0.5 * 32767 * math.sin(2 * math.pi * freq_hz * i / _G722_SAMPLE_RATE))
        for i in range(n_samples)
    ]
    return PcmFrame(
        samples=struct.pack(f"<{n_samples}h", *samples),
        sample_rate=_G722_SAMPLE_RATE,
        monotonic_ts_ns=0,
    )


def _new_engine() -> RtpMediaTransport:
    return RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.G722,
        sleep=_no_sleep,
        initial_seq=0,
        initial_ts=0,
    )


@pytest.mark.asyncio
async def test_inbound_sample_rate_is_16000_for_g722() -> None:
    # The engine reports the codec's TRUE audio sample rate (16 kHz) so the STT
    # path is fed native wideband, not upsampled-from-8k.
    engine = _new_engine()
    await engine.connect()
    try:
        assert engine.inbound_sample_rate == _G722_SAMPLE_RATE
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_outbound_packets_use_payload_type_9() -> None:
    engine = _new_engine()
    await engine.connect()
    with _capture_sends(engine) as recorder:
        await engine.send_audio(_wideband_frame(_AUDIO_SAMPLES_PER_FRAME))
        await engine.stop()
    assert recorder.sent, "no RTP emitted"
    pkt = RtpPacket.parse(recorder.sent[0][0])
    assert pkt.payload_type == _G722_PT


@pytest.mark.asyncio
async def test_one_20ms_frame_emits_160_octets() -> None:
    # 320 input samples (20 ms @ 16 kHz) -> exactly one G.722 packet of 160 octets.
    engine = _new_engine()
    await engine.connect()
    with _capture_sends(engine) as recorder:
        await engine.send_audio(_wideband_frame(_AUDIO_SAMPLES_PER_FRAME))
        await engine.stop()
    payloads = [RtpPacket.parse(w).payload for w, _ in recorder.sent]
    assert len(payloads) == 1, f"expected one frame, got {len(payloads)}"
    assert len(payloads[0]) == 160, (
        f"G.722 20 ms frame must be 160 octets, got {len(payloads[0])}"
    )


@pytest.mark.asyncio
async def test_rtp_timestamp_advances_by_160_not_320_per_frame() -> None:
    # THE quirk: the RTP timestamp increment is the 8 kHz CLOCK delta (160), not
    # the 16 kHz audio sample count (320). A wrong implementation that advanced by
    # the audio sample count (320) would fail here — that is the discriminating
    # assertion this test exists for.
    engine = _new_engine()
    await engine.connect()
    # Two whole 20 ms frames back to back.
    with _capture_sends(engine) as recorder:
        await engine.send_audio(_wideband_frame(2 * _AUDIO_SAMPLES_PER_FRAME))
        await engine.stop()
    packets = [RtpPacket.parse(w) for w, _ in recorder.sent]
    assert len(packets) >= 2, f"need at least two packets, got {len(packets)}"
    ts_delta = (packets[1].timestamp - packets[0].timestamp) % (1 << 32)
    assert ts_delta == _RTP_TS_PER_FRAME, (
        f"G.722 RTP timestamp must advance by {_RTP_TS_PER_FRAME} (8 kHz clock) "
        f"per 20 ms frame, got {ts_delta} (advancing by the 16 kHz audio sample "
        f"count {_AUDIO_SAMPLES_PER_FRAME} is the classic G.722 bug)"
    )


@pytest.mark.asyncio
async def test_send_audio_encodes_g722_decodable_back_to_wideband() -> None:
    # The emitted payloads decode (via the same codec) back to a 16 kHz signal
    # whose 5 kHz component survives — proving the engine really G.722-encoded the
    # frame (a G.711 path could not carry 5 kHz at all).
    engine = _new_engine()
    await engine.connect()
    n = 4 * _AUDIO_SAMPLES_PER_FRAME  # 80 ms
    src_frame = _wideband_frame(n, freq_hz=5000.0)
    with _capture_sends(engine) as recorder:
        await engine.send_audio(src_frame)
        await engine.stop()
    g722_payload = b"".join(RtpPacket.parse(w).payload for w, _ in recorder.sent)
    decoded = G722Decoder().decode(g722_payload)
    out = list(struct.unpack(f"<{len(decoded) // 2}h", decoded))
    # The decoded stream is 16 kHz and carries real energy at 5 kHz.
    assert len(out) >= n
    src = list(struct.unpack(f"<{n}h", src_frame.samples))
    lead = 64
    a, b = src[lead:], out[lead : lead + len(src) - lead]
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    ea = math.sqrt(sum(x * x for x in a)) or 1.0
    eb = math.sqrt(sum(y * y for y in b)) or 1.0
    assert dot / (ea * eb) > 0.85, "wideband content lost in the G.722 send path"


@pytest.mark.asyncio
async def test_g711_timestamp_still_advances_by_160() -> None:
    # Regression guard: G.711 is unchanged — 8 kHz audio == 8 kHz clock, so its
    # 20 ms frame still advances the RTP timestamp by 160 (160 audio samples).
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        sleep=_no_sleep,
        initial_seq=0,
        initial_ts=0,
    )
    await engine.connect()
    pcm_8k = b"\x00" * (160 * 2 * 2)  # two 20 ms 8 kHz frames
    with _capture_sends(engine) as recorder:
        await engine.send_audio(
            PcmFrame(samples=pcm_8k, sample_rate=8000, monotonic_ts_ns=0)
        )
        await engine.stop()
    packets = [RtpPacket.parse(w) for w, _ in recorder.sent]
    assert len(packets) >= 2
    ts_delta = (packets[1].timestamp - packets[0].timestamp) % (1 << 32)
    assert ts_delta == 160
