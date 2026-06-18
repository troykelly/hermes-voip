"""AEC wired into the media engine RX/TX taps (ADR-0033).

The canceller lives inside :class:`RtpMediaTransport`:

* the TX path (``send_audio`` → ``_transmit_frame``) pushes every outbound
  wire-rate frame as the AEC reference, and
* the RX path (``inbound_audio`` → ``_decode``) passes every decoded inbound
  frame through ``cancel`` before yielding it.

So a gateway that reflects our outbound audio back on the inbound leg has that
echo cancelled before the VAD/ASR (the call loop) ever see it. These tests drive
the engine end to end (real ``send_audio`` + real ``inbound_audio``) with a
deterministic clock and a recorder transport, and decode the yielded inbound
frames to assert the echo is removed when AEC is on and present when it is off.
"""

from __future__ import annotations

import asyncio
import math
import random
import struct

import pytest

from hermes_voip.media.audio import encode_ulaw
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.rtp import RtpPacket

_G711_RATE = 8_000
_PTIME_MS = 20
_SPF = (_G711_RATE * _PTIME_MS) // 1000  # 160
_FAKE_SRC: tuple[str, int] = ("198.51.100.7", 6000)
_GATEWAY_SSRC = 0x11223344


class _Clock:
    def __init__(self) -> None:
        self._t = 0.0

    def now(self) -> float:
        return self._t

    async def sleep(self, secs: float) -> None:
        if secs > 0:
            self._t += secs


class _Recorder:
    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def sendto(self, data: bytes, addr: tuple[str, int] | None = None) -> None:
        self.packets.append(data)

    def close(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False


def _sine_frame(values: list[int]) -> PcmFrame:
    return PcmFrame(
        samples=struct.pack(f"<{len(values)}h", *values),
        sample_rate=_G711_RATE,
        monotonic_ts_ns=0,
    )


def _sine(n: int, *, freq_hz: float, amplitude: float) -> list[int]:
    peak = amplitude * 32767.0
    w = 2.0 * math.pi * freq_hz / _G711_RATE
    return [int(peak * math.sin(w * i)) for i in range(n)]


def _noise(n: int, *, amplitude: float, seed: int) -> list[int]:
    """Broadband (speech-like) PCM16 noise — a delay is not a 2-tap trick on it."""
    rng = random.Random(seed)  # noqa: S311 — test signal, not cryptographic
    peak = amplitude * 32767.0
    return [int(rng.uniform(-peak, peak)) for _ in range(n)]


def _rms(samples: tuple[int, ...]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(v * v for v in samples) / len(samples))


async def _make_engine(*, aec_enabled: bool) -> tuple[RtpMediaTransport, _Recorder]:
    clock = _Clock()
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        sleep=clock.sleep,
        pace_clock=clock.now,
        initial_seq=0,
        initial_ts=0,
        aec_enabled=aec_enabled,
        aec_filter_ms=64,  # 512 taps at 8 kHz — the default; spans a 20 ms echo delay
        aec_bulk_delay_ms=0,
        aec_mu=0.5,
    )
    await engine.connect()
    recorder = _Recorder()  # silence the TX socket; the recorder satisfies the sink
    engine._transport = recorder
    return engine, recorder


@pytest.mark.asyncio
async def test_engine_cancels_reflected_outbound_on_inbound() -> None:
    """With AEC on, an inbound echo of our outbound audio is cancelled before yield.

    Drive the engine: send an outbound tone (tapped as the reference), then feed
    the SAME tone back inbound (the gateway reflecting our TTS). The frames the
    engine yields from ``inbound_audio`` must be driven toward silence — the VAD
    would never see a speech onset.
    """
    engine, _rec = await _make_engine(aec_enabled=True)

    # Broadband (speech-like) outbound, reflected back DELAYED by one packet (20 ms) —
    # the realistic gateway echo (a round-trip lag a too-short filter cannot reach).
    n_packets = 120
    n = _SPF * n_packets
    tts = _noise(n, amplitude=0.4, seed=11)
    echo = [int(s * 0.7) for s in tts]

    frames_out: list[PcmFrame] = []

    async def _consume() -> None:
        async for frame in engine.inbound_audio():
            frames_out.append(frame)
            if len(frames_out) >= n_packets - 1:
                return

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0)

    # Interleave: each 20 ms slot pushes the outbound reference (send_audio) then the
    # inbound echo of the PREVIOUS packet (a 1-packet round-trip delay).
    for f in range(n_packets):
        chunk = tts[f * _SPF : (f + 1) * _SPF]
        await engine.send_audio(_sine_frame(chunk))
        if f >= 1:
            echo_chunk = echo[(f - 1) * _SPF : f * _SPF]
            pkt = RtpPacket(
                payload_type=0,
                sequence_number=f,
                timestamp=f * _SPF,
                ssrc=_GATEWAY_SSRC,
                payload=encode_ulaw(struct.pack(f"<{_SPF}h", *echo_chunk)),
            ).pack()
            engine._recv_queue.put_nowait((pkt, _FAKE_SRC))
        await asyncio.sleep(0)

    await asyncio.wait_for(consumer, timeout=5.0)
    await engine.stop()

    assert len(frames_out) >= 80
    # Echo level on the wire (what arrived) vs residual the engine yielded, both on
    # the converged tail (second half).
    tail = frames_out[len(frames_out) // 2 :]
    residual_rms = max(
        _rms(struct.unpack(f"<{len(fr.samples) // 2}h", fr.samples)) for fr in tail
    )
    echo_rms = _rms(tuple(echo[n // 2 :]))
    assert echo_rms > 1000.0, f"echo too quiet to be meaningful: {echo_rms}"
    assert residual_rms < echo_rms * 0.4, (
        f"engine did not cancel the reflected echo: "
        f"residual={residual_rms:.1f} echo={echo_rms:.1f}"
    )


@pytest.mark.asyncio
async def test_engine_aec_disabled_passes_echo_through() -> None:
    """With AEC off, the reflected echo reaches the yielded inbound frames intact.

    The contrast case proving the cancellation above is the AEC's doing: the same
    drive with ``aec_enabled=False`` yields the echo essentially unchanged (G.711
    quantisation aside).
    """
    engine, _rec = await _make_engine(aec_enabled=False)
    n = _SPF * 20
    tone = _sine(n, freq_hz=600.0, amplitude=0.4)
    echo = [int(s * 0.7) for s in tone]

    frames_out: list[PcmFrame] = []

    async def _consume() -> None:
        async for frame in engine.inbound_audio():
            frames_out.append(frame)
            if len(frames_out) >= 20:
                return

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0)
    for f in range(20):
        chunk = tone[f * _SPF : (f + 1) * _SPF]
        await engine.send_audio(_sine_frame(chunk))
        echo_chunk = echo[f * _SPF : (f + 1) * _SPF]
        pkt = RtpPacket(
            payload_type=0,
            sequence_number=f,
            timestamp=f * _SPF,
            ssrc=_GATEWAY_SSRC,
            payload=encode_ulaw(struct.pack(f"<{_SPF}h", *echo_chunk)),
        ).pack()
        engine._recv_queue.put_nowait((pkt, _FAKE_SRC))
        await asyncio.sleep(0)

    await asyncio.wait_for(consumer, timeout=5.0)
    await engine.stop()

    tail = frames_out[len(frames_out) // 2 :]
    residual_rms = max(
        _rms(struct.unpack(f"<{len(fr.samples) // 2}h", fr.samples)) for fr in tail
    )
    echo_rms = _rms(tuple(echo[n // 2 :]))
    # Disabled: the echo survives (decode of the same ulaw we encoded). Well above
    # the cancelled threshold from the previous test.
    assert residual_rms > echo_rms * 0.7, (
        f"disabled AEC unexpectedly altered the echo: "
        f"residual={residual_rms:.1f} echo={echo_rms:.1f}"
    )


@pytest.mark.asyncio
async def test_engine_default_enables_aec() -> None:
    """AEC is enabled by default and becomes active on the first outbound frame.

    The canceller is created LAZILY at the current analysis rate (like the
    G.722/Opus codecs) because the outbound path re-sets the codec after connect()
    but before media; building it at connect-time would pin the placeholder rate.
    So ``_aec_enabled`` is True by default and ``_aec`` materialises on first use.
    """
    engine, _rec = await _make_engine(aec_enabled=True)
    assert engine._aec_enabled is True
    # Not built until the first reference push (lazy at the analysis rate); snapshot
    # before so the assertion does not narrow the attribute to ``None`` across the
    # ``send_audio`` mypy cannot see mutate it (which would flag the rest unreachable).
    aec_before = engine._aec
    assert aec_before is None
    await engine.send_audio(_sine_frame([100] * _SPF))
    aec_after = engine._aec
    assert aec_after is not None  # built on the first outbound frame
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_aec_disabled_builds_no_canceller() -> None:
    """With AEC disabled, no canceller is ever built (the RX/TX taps are no-ops)."""
    engine, _rec = await _make_engine(aec_enabled=False)
    assert engine._aec_enabled is False
    await engine.send_audio(_sine_frame([100] * _SPF))
    assert engine._aec is None
    await engine.stop()
