"""Barge-in clean-stop + fade on the outbound RTP path (ADR-0028).

On an authorised barge-in the call loop must make the agent go quiet within ~1
packet — not after the buffered TTS audio drains — and the final frames must
ramp down so the cut does not click/pop. The engine grows
:meth:`RtpMediaTransport.flush_outbound`:

* it drops the pending outbound audio (the carry buffer + any in-flight frame)
  so nothing more of the superseded utterance reaches the wire, and
* before dropping it, emits a short linear FADE-OUT (config ms) on the tail of
  that pending audio, encoded as one or two final RTP packets, so the last thing
  the caller hears is a click-free ramp to silence.

These tests are deterministic: an injected ``pace_clock`` + ``sleep`` model time
and a recorder captures the actual datagrams so the fade ramp is decoded and
checked. They run in the default (no-extra) gate. G.711 and G.722 are both
exercised: the fade is applied in the linear PCM16 domain BEFORE the codec encode
on both paths.
"""

from __future__ import annotations

import struct

import pytest

from hermes_voip.media.audio import decode_ulaw
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.media.g722 import G722Decoder
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.rtp import RtpPacket

_PTIME_MS = 20
_G711_RATE = 8_000
_G722_RATE = 16_000
_G711_SAMPLES_PER_FRAME = (_G711_RATE * _PTIME_MS) // 1000  # 160
_G722_SAMPLES_PER_FRAME = (_G722_RATE * _PTIME_MS) // 1000  # 320


class _Clock:
    """Deterministic monotonic clock: ``sleep`` advances modelled time."""

    def __init__(self) -> None:
        self._t = 0.0

    def now(self) -> float:
        return self._t

    async def sleep(self, secs: float) -> None:
        if secs > 0:
            self._t += secs


class _CapturingTransport:
    """A DatagramTransport stand-in that records every datagram's payload bytes."""

    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def sendto(self, data: bytes, addr: tuple[str, int] | None = None) -> None:
        assert addr is not None
        self.packets.append(data)

    def close(self) -> None:
        """No-op: owns no socket."""

    def is_closing(self) -> bool:
        return False


async def _new_engine(codec: Codec) -> tuple[RtpMediaTransport, _CapturingTransport]:
    clock = _Clock()
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=codec,
        sleep=clock.sleep,
        pace_clock=clock.now,
        initial_seq=0,
        initial_ts=0,
    )
    await engine.connect()
    recorder = _CapturingTransport()
    engine._transport = recorder  # type: ignore[assignment]  # recorder satisfies sendto/close/is_closing
    return engine, recorder


def _ulaw_payload_samples(datagram: bytes) -> tuple[int, ...]:
    """Decode an RTP/PCMU datagram's payload to PCM16 samples."""
    pkt = RtpPacket.parse(datagram)
    pcm = decode_ulaw(pkt.payload)
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)


def _const_g711_frame(n_frames: int, value: int) -> PcmFrame:
    """``n_frames`` whole 20 ms G.711 frames of a constant PCM16 ``value``."""
    n = n_frames * _G711_SAMPLES_PER_FRAME
    return PcmFrame(
        samples=struct.pack(f"<{n}h", *([value] * n)),
        sample_rate=_G711_RATE,
        monotonic_ts_ns=0,
    )


def _const_g722_frame(n_frames: int, value: int) -> PcmFrame:
    n = n_frames * _G722_SAMPLES_PER_FRAME
    return PcmFrame(
        samples=struct.pack(f"<{n}h", *([value] * n)),
        sample_rate=_G722_RATE,
        monotonic_ts_ns=0,
    )


# ---------------------------------------------------------------------------
# flush_outbound drops the pending buffer (quiet within ~1 packet)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_outbound_drops_pending_buffer() -> None:
    """A large queued utterance is NOT all transmitted after a flush.

    Hand send_audio a 20-frame (400 ms) utterance, then flush mid-stream. Without
    the flush all 20 frames pace out; with it, only the fade tail goes out and the
    rest is dropped — the agent goes quiet within ~1 ptime instead of after the
    whole buffer drains.
    """
    engine, recorder = await _new_engine(Codec.PCMU)
    # Stuff the carry buffer directly (no pacing drain yet): one big chunk of
    # 20 whole frames. send_audio would pace these out over 400 ms; we flush first.
    engine._tx_buffer = _const_g711_frame(20, 12_000).samples

    await engine.flush_outbound(fade_ms=30)

    # At most the fade window's worth of packets reach the wire (a 30 ms fade is
    # <= 2 frames), NOT the 20 buffered frames.
    assert 1 <= len(recorder.packets) <= 2, (
        f"flush should emit only the short fade tail, got {len(recorder.packets)} "
        f"packets — the buffered audio kept draining (the abruptness/delay bug)"
    )
    # The carry buffer is emptied — nothing of the superseded utterance remains.
    assert engine._tx_buffer == b""


@pytest.mark.asyncio
async def test_flush_outbound_emits_linear_fade_to_silence_g711() -> None:
    """The final emitted frame ramps a constant signal DOWN to ~zero (G.711).

    A constant full-amplitude buffer is the worst case for a click on a hard cut.
    After flush, the last datagram's decoded PCM must descend monotonically across
    the fade and end at (essentially) silence — not a full-amplitude hard edge.
    """
    engine, recorder = await _new_engine(Codec.PCMU)
    engine._tx_buffer = _const_g711_frame(10, 16_000).samples

    await engine.flush_outbound(fade_ms=20)  # 20 ms = 160 samples = exactly 1 frame

    assert recorder.packets, "flush emitted no fade packet"
    last = _ulaw_payload_samples(recorder.packets[-1])
    # Descends across the frame (allowing G.711 quantisation: each later sample is
    # not greater than an earlier one beyond a small codec tolerance).
    first_half_peak = max(abs(s) for s in last[: len(last) // 2])
    last_quarter_peak = max(abs(s) for s in last[3 * len(last) // 4 :])
    assert last_quarter_peak < first_half_peak // 2, (
        "fade tail did not ramp down: last quarter peak "
        f"{last_quarter_peak} not well below first-half peak {first_half_peak}"
    )
    # The very last sample is essentially silent (G.711 mu-law min step ~ a few).
    assert abs(last[-1]) <= 64, f"fade did not reach silence: last sample {last[-1]}"


@pytest.mark.asyncio
async def test_flush_outbound_fade_is_monotone_in_linear_domain_g722() -> None:
    """G.722: the fade is applied in linear PCM BEFORE encode (decoded ramp falls).

    Decode the flushed G.722 datagram(s) and confirm the reconstructed envelope
    descends to near-zero — proving the fade happens in the PCM domain on the
    wideband path too, not only for G.711.
    """
    engine, recorder = await _new_engine(Codec.G722)
    # 8 whole wideband frames of a constant signal in the carry buffer.
    engine._tx_buffer = _const_g722_frame(8, 14_000).samples

    await engine.flush_outbound(fade_ms=30)

    assert recorder.packets, "flush emitted no fade packet on G.722"
    decoder = G722Decoder()
    pcm = b"".join(decoder.decode(RtpPacket.parse(p).payload) for p in recorder.packets)
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    # Compare the start of the fade region to its end: the envelope must collapse.
    head_peak = max(abs(s) for s in samples[: len(samples) // 3])
    tail_peak = max(abs(s) for s in samples[2 * len(samples) // 3 :])
    assert tail_peak < head_peak // 2, (
        f"G.722 fade envelope did not collapse: tail peak {tail_peak} not well "
        f"below head peak {head_peak} (fade not applied in the linear domain)"
    )


@pytest.mark.asyncio
async def test_flush_outbound_zero_fade_emits_no_audio() -> None:
    """fade_ms=0 drops the buffer with NO trailing audio (immediate hard silence).

    An operator who explicitly disables the fade gets an instant cut: the pending
    buffer is discarded and nothing is emitted.
    """
    engine, recorder = await _new_engine(Codec.PCMU)
    engine._tx_buffer = _const_g711_frame(10, 12_000).samples

    await engine.flush_outbound(fade_ms=0)

    assert recorder.packets == [], "fade_ms=0 must emit no audio"
    assert engine._tx_buffer == b""


@pytest.mark.asyncio
async def test_flush_outbound_on_empty_buffer_is_noop() -> None:
    """Flushing with nothing pending emits nothing and does not raise."""
    engine, recorder = await _new_engine(Codec.PCMU)
    await engine.flush_outbound(fade_ms=30)
    assert recorder.packets == []


@pytest.mark.asyncio
async def test_flush_outbound_advances_rtp_sequence() -> None:
    """Each fade packet is a well-formed RTP packet with an advancing sequence.

    The flushed fade frames must keep the RTP stream RFC-3550 consistent (seq/ts
    advance) so the gateway accepts them as the natural tail of the stream.
    """
    engine, recorder = await _new_engine(Codec.PCMU)
    engine._tx_buffer = _const_g711_frame(6, 10_000).samples
    await engine.flush_outbound(fade_ms=40)  # 40 ms => 2 frames

    seqs = [RtpPacket.parse(p).sequence_number for p in recorder.packets]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), (
        f"fade packet sequence numbers not contiguous/advancing: {seqs}"
    )
