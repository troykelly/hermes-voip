"""G.722 wideband behaviour of RtpMediaTransport (ADR-0022).

Deterministic, no real network / wall-clock (injected ``sleep``). Verifies the
engine carries G.722 with the RFC 3551 framing quirk handled: the audio sample
rate is 16 kHz but the RTP clock is 8 kHz, so a 20 ms frame is 320 input samples
-> 160 octets and the RTP timestamp advances by 160 (the 8 kHz clock), NOT 320.

Runs in the DEFAULT (no-extra) gate — the G.722 codec is pure Python.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import socket
import struct
from collections.abc import Iterator

import pytest

from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.media.g722 import G722Decoder, G722Encoder
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
    engine._transport = recorder  # _SendRecorder satisfies the _DatagramSink seam
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
    # The decoded stream is 16 kHz and carries real energy at 5 kHz. G.722's QMF
    # has a group delay, so we correlate over a small lag window (the honest
    # fidelity metric for a delayed reconstruction), not at lag 0.
    assert len(out) >= n
    src = list(struct.unpack(f"<{n}h", src_frame.samples))
    lead = 64
    a, b = src[lead:], out[lead:]

    def _max_xcorr(xa: list[int], xb: list[int], max_lag: int) -> float:
        best = 0.0
        for lag in range(max_lag + 1):
            aa = xa[: len(xa) - lag]
            bb = xb[lag:]
            m = min(len(aa), len(bb))
            aa, bb = aa[:m], bb[:m]
            dot = sum(x * y for x, y in zip(aa, bb, strict=True))
            ea = math.sqrt(sum(x * x for x in aa)) or 1.0
            eb = math.sqrt(sum(y * y for y in bb)) or 1.0
            best = max(best, dot / (ea * eb))
        return best

    assert _max_xcorr(a, b, max_lag=40) > 0.9, (
        "wideband content lost in the G.722 send path"
    )


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


# ---------------------------------------------------------------------------
# Negotiated RTP payload type (codex review finding): G.722 may be offered at a
# DYNAMIC payload type (RFC 3551 reserves static PT 9, but gateways do use dynamic
# PTs and the SDP answer mirrors the offer's PT). The engine must send and latch
# on the NEGOTIATED payload type, not the codec's static enum value — otherwise we
# advertise PT N but send PT 9, and inbound PT-N packets never trigger the NAT
# (comedia) latch -> no audio. (Masked for G.711: PCMU/PCMA are always 0/8.)
# ---------------------------------------------------------------------------

_DYNAMIC_G722_PT = 109


@pytest.mark.asyncio
async def test_outbound_uses_the_negotiated_dynamic_payload_type() -> None:
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.G722,
        payload_type=_DYNAMIC_G722_PT,  # the negotiated (dynamic) PT, not 9
        sleep=_no_sleep,
        initial_seq=0,
        initial_ts=0,
    )
    await engine.connect()
    with _capture_sends(engine) as recorder:
        await engine.send_audio(_wideband_frame(_AUDIO_SAMPLES_PER_FRAME))
        await engine.stop()
    assert recorder.sent, "no RTP emitted"
    pkt = RtpPacket.parse(recorder.sent[0][0])
    assert pkt.payload_type == _DYNAMIC_G722_PT, (
        f"engine sent PT {pkt.payload_type}, expected the negotiated "
        f"{_DYNAMIC_G722_PT} (sending the static codec PT 9 is the bug)"
    )


@pytest.mark.asyncio
async def test_symmetric_latch_accepts_the_negotiated_dynamic_payload_type() -> None:
    # The comedia latch must fire on a genuine inbound RTP packet bearing the
    # NEGOTIATED payload type. Drive one inbound datagram with PT 109 through the
    # real receive path and assert the engine latched onto its source.
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.G722,
        payload_type=_DYNAMIC_G722_PT,
        sleep=_no_sleep,
        symmetric=True,
    )
    await engine.connect()
    engine_port = engine.local_port
    # A genuine inbound G.722 RTP packet at the negotiated PT 109, from a distinct
    # source tuple (so a successful latch is observable as a moved _outbound_addr).
    g722_payload = G722Encoder().encode(
        struct.pack(f"<{_AUDIO_SAMPLES_PER_FRAME}h", *([0] * _AUDIO_SAMPLES_PER_FRAME))
    )
    pkt = RtpPacket(
        payload_type=_DYNAMIC_G722_PT,
        sequence_number=0,
        timestamp=0,
        ssrc=0xDEADBEEF,
        payload=g722_payload,
    ).pack()

    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Bind the sender to a concrete loopback port so the engine sees a stable,
        # known source tuple (an unbound socket's getsockname() reports 0.0.0.0,
        # which is not what the engine receives).
        sender.bind(("127.0.0.1", 0))
        sender_port = sender.getsockname()[1]
        sender.sendto(pkt, ("127.0.0.1", engine_port))

        async def _one() -> None:
            async for _frame in engine.inbound_audio():
                return

        task: asyncio.Task[None] = asyncio.create_task(_one())
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        sender.close()
        await engine.stop()

    # The engine latched its outbound destination onto the packet's real source —
    # proof the PT-109 packet was accepted as the negotiated audio stream (it moved
    # off the negotiated remote 5004 onto the sender's actual tuple).
    assert engine._outbound_addr == ("127.0.0.1", sender_port), (
        f"engine did not latch on the negotiated PT {_DYNAMIC_G722_PT} packet "
        f"(outbound_addr={engine._outbound_addr}, expected 127.0.0.1:{sender_port})"
    )


# ---------------------------------------------------------------------------
# Wideband re-framing continuity across odd-sized chunks + the deadline pacer's
# stop-race (the G.722 jitter fix). G.722's encoder is STATEFUL (sub-band ADPCM
# predictor + QMF history), so the re-framing buffer must feed it ONE continuous
# 16 kHz stream in whole 320-sample frames regardless of how the producer chunks
# arrive — a per-chunk discontinuity would both inject silence AND corrupt the
# predictor. These mirror the G.711 continuity tests at the 320-sample wideband
# frame, and lock in that a stop() racing the pacer loses no audio and keeps order.
# ---------------------------------------------------------------------------


def _wideband_counter_pcm(start_sample: int, n_samples: int) -> bytes:
    """``n_samples`` of a distinct, non-silent 16 kHz wideband tone, by global index.

    A 5 kHz tone (above the G.711 ceiling, so it exercises the high band) phased on
    the GLOBAL sample index, so concatenating successive slices is one continuous
    waveform — exactly what the re-framing buffer must reconstruct from odd chunks.
    """
    samples = [
        int(0.4 * 32767 * math.sin(2 * math.pi * 5000.0 * (start_sample + i) / 16_000))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


@pytest.mark.asyncio
async def test_g722_stream_continuous_across_non_320_multiple_chunks() -> None:
    """Odd-sized 16 kHz chunks encode to the SAME bytes as one continuous pass.

    A streaming TTS hands send_audio a frame per producer chunk whose length is not
    a multiple of the 320-sample (640-byte) G.722 frame. The re-framing buffer must
    carry the sub-frame remainder across calls and feed the stateful encoder ONE
    continuous stream. The discriminating check: the concatenation of all emitted
    G.722 payloads must equal a single-pass encode of the same total PCM by a fresh
    G722Encoder — byte-for-byte. A per-chunk silence pad (the "very choppy" bug) or
    any framing discontinuity would diverge here, because the stateful predictor
    would see different inputs.
    """
    # 11 chunks of 532 samples = 5852 samples total. 532 is deliberately not a
    # multiple of 320 (a 212-sample remainder rolls forward each chunk). The total
    # 5852 is also not a whole number of frames (18.2875), so a tail remainder
    # exists at stop — exercising the final padded flush too.
    chunk_samples = 532
    chunk_count = 11
    total_samples = chunk_samples * chunk_count

    engine = _new_engine()
    await engine.connect()
    with _capture_sends(engine) as recorder:
        for idx in range(chunk_count):
            pcm = _wideband_counter_pcm(idx * chunk_samples, chunk_samples)
            await engine.send_audio(
                PcmFrame(samples=pcm, sample_rate=_G722_SAMPLE_RATE, monotonic_ts_ns=0)
            )
        await engine.stop()  # flushes the sub-frame tail (padded once)

    emitted = b"".join(RtpPacket.parse(w).payload for w, _ in recorder.sent)

    # Reference: the same total PCM, zero-padded up to a whole 320-sample frame
    # exactly once at the end (mirroring stop()'s single tail pad), encoded in ONE
    # pass by a fresh encoder — the definition of a sample-continuous wideband stream.
    frame_samples = _AUDIO_SAMPLES_PER_FRAME  # 320
    remainder = total_samples % frame_samples
    pad = (frame_samples - remainder) if remainder else 0
    whole_pcm = b"".join(
        _wideband_counter_pcm(i * chunk_samples, chunk_samples)
        for i in range(chunk_count)
    ) + bytes(pad * 2)
    expected = G722Encoder().encode(whole_pcm)

    assert emitted == expected, (
        "G.722 send path is not sample-continuous across odd chunks: the emitted "
        "octets differ from a single-pass encode of the same PCM (a per-chunk "
        "discontinuity corrupted the stateful predictor or injected silence)"
    )
    # And every emitted packet is exactly one 20 ms wideband frame (160 octets) bar
    # the last, which is the padded tail — also 160 octets.
    payload_lens = {len(RtpPacket.parse(w).payload) for w, _ in recorder.sent}
    assert payload_lens == {160}, (
        f"every G.722 packet must be one 20 ms frame (160 octets); got {payload_lens}"
    )


@pytest.mark.asyncio
async def test_g722_stop_mid_playout_flushes_all_frames_in_order() -> None:
    """A stop() racing the G.722 deadline pacer loses no audio and keeps order.

    The deadline pacer encodes a frame (advancing the stateful encoder + committing
    seq/ts) BEFORE its pacing sleep, then sends on wake. If stop() nulls the
    transport during that sleep, the already-encoded datagram must still be
    delivered — FIRST, ahead of the raw frames stop() flushes from the buffer — or
    the wideband stream is reordered/dropped. We feed 6 whole frames in one call,
    tear the engine down from inside the pacing sleep right after the first frame,
    and assert all 6 frames are on the wire, in strictly increasing RTP sequence,
    decoding to the original wideband waveform.
    """
    n_frames = 6
    total_samples = n_frames * _AUDIO_SAMPLES_PER_FRAME  # 1920 — exactly 6 frames
    src_pcm = _wideband_counter_pcm(0, total_samples)

    engine = _new_engine()
    await engine.connect()
    with _capture_sends(engine) as recorder:
        # Pacing sleep that tears the engine down right after the first frame is sent,
        # exercising the "stop() nulls _transport during the pacing sleep" path with
        # whole wideband frames still buffered (and one encoded, in-flight) behind it.
        async def _stop_after_first(_secs: float) -> None:
            if len(recorder.sent) == 1 and engine._transport is not None:
                await engine.stop()

        engine._sleep = _stop_after_first
        await engine.send_audio(
            PcmFrame(samples=src_pcm, sample_rate=_G722_SAMPLE_RATE, monotonic_ts_ns=0)
        )
        await engine.stop()  # idempotent no-op (stop already ran from the sleep)

    packets = [RtpPacket.parse(w) for w, _ in recorder.sent]
    assert len(packets) == n_frames, (
        f"expected all {n_frames} G.722 frames across the stop race, got "
        f"{len(packets)} (a frame was dropped)"
    )
    # Strictly increasing, contiguous RTP sequence numbers — no reorder, no gap.
    seqs = [p.sequence_number for p in packets]
    assert seqs == list(range(seqs[0], seqs[0] + n_frames)), (
        f"RTP sequence not contiguous/in-order across the stop race: {seqs}"
    )
    # The concatenated payloads decode (one fresh decoder) back to the original
    # wideband waveform — order preserved and the encoder saw one continuous stream.
    decoded = G722Decoder().decode(b"".join(p.payload for p in packets))
    expected = G722Decoder().decode(G722Encoder().encode(src_pcm))
    assert decoded == expected, (
        "emitted G.722 audio is reordered or corrupted across the stop race"
    )
