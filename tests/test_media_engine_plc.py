"""Packet-loss concealment in RtpMediaTransport (ADR-0056 items 2 + 3).

A lost frame must no longer leave a hole in the inbound stream: the engine emits
one concealment PcmFrame at the analysis rate so the VAD/endpointer/STT see a
continuous stream, and the wideband codecs (G.722/Opus) must not degrade *more*
than G.711 under loss — Opus uses its in-band FEC/PLC, G.711/G.722 repeat the
last good frame attenuated.

Deterministic, white-box: packets are injected straight into the engine's recv
queue (the established ``_recv_queue`` test seam) with a sequence gap that makes
the jitter buffer declare ``Lost`` once ``jitter_depth`` later packets pile up.
No real network or wall clock.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import struct

import pytest

from hermes_voip.media.audio import G711_SAMPLE_RATE, Resampler, encode_ulaw
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.media.g722 import G722_SAMPLE_RATE, G722Encoder
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.rtp import RtpPacket

_PTIME_MS = 20
_SAMPLES_PER_FRAME = (G711_SAMPLE_RATE * _PTIME_MS) // 1000  # 160
_FAKE_SSRC = 0xDEADBEEF
_FAKE_SRC: tuple[str, int] = ("198.51.100.7", 6000)


def _dummy_clock() -> int:
    return 0


def _tone_pcm16(
    n_samples: int, rate: int, freq_hz: float = 440.0, amp: int = 8000
) -> bytes:
    return struct.pack(
        f"<{n_samples}h",
        *[
            int(amp * math.sin(2 * math.pi * freq_hz * n / rate))
            for n in range(n_samples)
        ],
    )


def _rms(pcm16: bytes) -> float:
    n = len(pcm16) // 2
    if n == 0:
        return 0.0
    vals = struct.unpack(f"<{n}h", pcm16)
    return math.sqrt(sum(v * v for v in vals) / n)


def _make_rtp(
    seq: int, payload: bytes, *, pt: int = 0, ts: int = 0, ssrc: int = _FAKE_SSRC
) -> bytes:
    return RtpPacket(
        payload_type=pt,
        sequence_number=seq,
        timestamp=ts,
        ssrc=ssrc,
        payload=payload,
    ).pack()


async def _collect_n(engine: RtpMediaTransport, n: int) -> list[PcmFrame]:
    """Drain ``n`` frames from the inbound generator, then stop it."""
    frames: list[PcmFrame] = []
    done = asyncio.Event()

    async def _run() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)
            if len(frames) >= n:
                done.set()
                break

    task = asyncio.create_task(_run())
    await asyncio.sleep(0)
    await asyncio.wait_for(done.wait(), timeout=2.0)
    await asyncio.wait_for(task, timeout=2.0)
    return frames


# ---------------------------------------------------------------------------
# G.711: a lost frame is concealed (not skipped) by an attenuated repeat.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g711_lost_frame_is_concealed_not_skipped() -> None:
    """A G.711 gap yields a concealment frame in the hole, not an absence.

    seq 0 (tone) arrives, seq 1 is lost, seqs 2/3/4 arrive. With jitter_depth=3
    the buffer declares Lost(1). The engine must yield: frame 0, a CONCEALMENT
    frame for 1 (non-silent, derived from frame 0), then 2, 3, 4 — five frames.
    Previously the Lost was skipped (only four frames), leaving an audible hole.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        jitter_depth=3,
        clock=_dummy_clock,
        aec_enabled=False,  # isolate concealment from the echo canceller
    )
    await engine.connect()

    tone = encode_ulaw(_tone_pcm16(_SAMPLES_PER_FRAME, G711_SAMPLE_RATE))
    for seq in (0, 2, 3, 4):
        engine._recv_queue.put_nowait((_make_rtp(seq, tone), _FAKE_SRC))

    frames = await _collect_n(engine, 5)
    await engine.stop()

    assert len(frames) == 5  # the hole at seq 1 is filled, not skipped
    # The concealment frame (index 1) carries energy (a repeat of frame 0's tone),
    # NOT silence — a hard mute would read as end-of-speech to the endpointer.
    assert _rms(frames[1].samples) > 0.0
    # It is at the analysis (G.711 wire) rate, like a real decoded frame.
    assert frames[1].sample_rate == G711_SAMPLE_RATE
    assert len(frames[1].samples) == _SAMPLES_PER_FRAME * 2


@pytest.mark.asyncio
async def test_g711_concealment_attenuates_toward_silence_on_a_run() -> None:
    """Consecutive losses fade the concealment so a long outage does not drone.

    seq 0 (tone) then a long gap; with jitter_depth=2 the buffer declares Lost
    for each missing slot as later packets pile up. Each successive concealment
    frame is no louder than the previous (monotonically attenuating), and a long
    run ends in silence rather than a sustained held tone.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        jitter_depth=2,
        clock=_dummy_clock,
        aec_enabled=False,
    )
    await engine.connect()

    tone = encode_ulaw(_tone_pcm16(_SAMPLES_PER_FRAME, G711_SAMPLE_RATE))
    # seq 0 arrives; then 8..18 pile up far ahead, forcing a run of Lost(1..7).
    engine._recv_queue.put_nowait((_make_rtp(0, tone), _FAKE_SRC))
    for seq in range(8, 19):
        engine._recv_queue.put_nowait((_make_rtp(seq, tone), _FAKE_SRC))

    frames = await _collect_n(engine, 8)  # frame 0 + 7 concealment frames
    await engine.stop()

    conceal = list(frames[1:8])
    energies = [_rms(f.samples) for f in conceal]
    # Monotonically non-increasing energy (attenuation), and the last is silent.
    for earlier, later in itertools.pairwise(energies):
        assert later <= earlier + 1e-9
    assert energies[-1] == 0.0  # faded to silence on a sustained outage


# ---------------------------------------------------------------------------
# G.722: wideband concealment must be no worse than G.711 (same strategy).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g722_lost_frame_is_concealed_at_wideband_rate() -> None:
    """A G.722 gap is concealed with a 16 kHz frame, not skipped (gap 3)."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.G722,
        payload_type=9,
        jitter_depth=3,
        clock=_dummy_clock,
        aec_enabled=False,
    )
    await engine.connect()

    enc = G722Encoder()
    # G.722 wire frame = 320 samples @ 16 kHz -> 160 octets.
    g722_payload = enc.encode(_tone_pcm16(320, G722_SAMPLE_RATE))
    for seq in (0, 2, 3, 4):
        engine._recv_queue.put_nowait((_make_rtp(seq, g722_payload, pt=9), _FAKE_SRC))

    frames = await _collect_n(engine, 5)
    await engine.stop()

    assert len(frames) == 5  # hole filled
    assert frames[1].sample_rate == G722_SAMPLE_RATE  # concealed at 16 kHz
    assert _rms(frames[1].samples) > 0.0  # non-silent repeat, not a hole


# ---------------------------------------------------------------------------
# Opus: FEC recovery from the next packet, PLC fallback otherwise (gap 3 + 4).
# ---------------------------------------------------------------------------

pytest.importorskip("opuslib", reason="webrtc extra (opuslib) not installed")

from hermes_voip.media.opus import (  # noqa: E402 — after the importorskip guard
    OPUS_FRAME_SAMPLES,
    OPUS_SAMPLE_RATE,
    OpusDecoder,
    OpusEncoder,
)

_OPUS_PT = 111
_OPUS_ANALYSIS_RATE = 16_000


@pytest.mark.asyncio
async def test_opus_lost_frame_recovered_via_fec() -> None:
    """An Opus gap is recovered from the next packet's in-band FEC, as real audio.

    With FEC on, packet 2 carries a redundant copy of frame 1. seq 1 is lost but
    seqs 2/3/4 arrive (jitter_depth=3 → Lost(1)); the engine must reconstruct
    frame 1 from packet 2's FEC and yield it (non-silent), at the 16 kHz analysis
    rate — so wideband degrades LESS than G.711, not more.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.OPUS,
        payload_type=_OPUS_PT,
        jitter_depth=3,
        clock=_dummy_clock,
        aec_enabled=False,
    )
    await engine.connect()

    enc = OpusEncoder(expected_packet_loss_pct=30)
    # A frequency-varying stream so each frame genuinely differs (FEC has content).
    payloads = [
        enc.encode(
            _tone_pcm16(OPUS_FRAME_SAMPLES, OPUS_SAMPLE_RATE, freq_hz=300.0 + 25.0 * i)
        )
        for i in range(5)
    ]
    for seq in (0, 2, 3, 4):  # seq 1 lost; its FEC rides inside packet 2
        engine._recv_queue.put_nowait(
            (_make_rtp(seq, payloads[seq], pt=_OPUS_PT), _FAKE_SRC)
        )

    frames = await _collect_n(engine, 5)
    await engine.stop()

    assert len(frames) == 5  # hole filled
    assert frames[1].sample_rate == _OPUS_ANALYSIS_RATE
    assert _rms(frames[1].samples) > 0.0  # real recovered audio, not a hole
    # Predictor continuity: the FEC decode of frame 1 advances the decoder past it,
    # so the SUCCESSOR (seq 2, frames[2]) must then decode normally to plausible
    # audio — a garbled predictor would collapse it toward silence. This proves the
    # libopus FEC-then-normal-decode pattern keeps the decoder state coherent.
    assert frames[2].sample_rate == _OPUS_ANALYSIS_RATE
    assert _rms(frames[2].samples) > 0.0  # successor decodes cleanly post-FEC
    assert _rms(frames[4].samples) > 0.0  # and the stream stays healthy after


@pytest.mark.asyncio
async def test_opus_lone_loss_falls_back_to_plc() -> None:
    """An Opus loss with no FEC successor uses native PLC (no crash, no hole).

    seqs 0,1 arrive, then 4,5 arrive (jitter_depth=2). The buffer declares
    Lost(2) — its successor (packet 3) is NOT buffered, so FEC is impossible and
    the engine must fall back to Opus PLC — then Lost(3) — whose successor
    (packet 4) IS buffered, so that one recovers via FEC. Either way every hole
    is filled with a full 16 kHz frame: 0,1,conceal(2),conceal(3),4,5 = six
    frames. Without concealment the stream is only 0,1,4,5 = four frames.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.OPUS,
        payload_type=_OPUS_PT,
        jitter_depth=2,
        clock=_dummy_clock,
        aec_enabled=False,
    )
    await engine.connect()

    enc = OpusEncoder()
    payloads = [
        enc.encode(
            _tone_pcm16(OPUS_FRAME_SAMPLES, OPUS_SAMPLE_RATE, freq_hz=300.0 + 25.0 * i)
        )
        for i in range(6)
    ]
    # seqs 0,1 arrive; 2 and 3 are lost; 4,5 arrive -> Lost(2) [PLC], Lost(3) [FEC].
    for seq in (0, 1, 4, 5):
        engine._recv_queue.put_nowait(
            (_make_rtp(seq, payloads[seq], pt=_OPUS_PT), _FAKE_SRC)
        )

    frames = await _collect_n(engine, 6)
    await engine.stop()

    assert len(frames) == 6  # the two holes at 2 and 3 are filled, not skipped
    samples_per_analysis_frame = (
        OPUS_FRAME_SAMPLES * _OPUS_ANALYSIS_RATE // OPUS_SAMPLE_RATE
    )
    for idx in (2, 3):  # both concealment frames are full analysis-rate frames
        assert frames[idx].sample_rate == _OPUS_ANALYSIS_RATE
        assert len(frames[idx].samples) == samples_per_analysis_frame * 2


# ---------------------------------------------------------------------------
# Corrupt (not lost) Opus payload: an authenticated packet that ARRIVES
# in-order but whose bytes are malformed Opus data. opuslib raises its own
# untyped ``OpusError`` on this (verified empirically: code -4, "corrupted
# stream") — neither a sequence gap the jitter buffer flags as ``Lost`` nor a
# ``ValueError``/``SrtpError`` the transport/SRTP guards already catch. Before
# the fix this propagated out of ``_decode`` -> ``_inbound_gen``, ending
# ``inbound_audio()`` on a single corrupt packet (in the real call loop, a
# one-packet call teardown — the same systemic class as the RTCP/transport/
# registration/SRTP fixes, ADR-0081). The fix conceals it exactly like a
# genuine loss so the stream — and the call — survives.
# ---------------------------------------------------------------------------

_CORRUPT_OPUS_PAYLOAD = (
    b"\xff" * 20
)  # genuine malformed Opus (opuslib: "corrupted stream")


@pytest.mark.asyncio
async def test_opus_corrupt_payload_is_concealed_and_call_survives() -> None:
    """A corrupt (but authenticated, in-order) Opus payload is concealed, not fatal.

    seq 1 carries genuinely malformed Opus bytes — NOT a sequence-number gap;
    the packet arrives normally. This must not raise out of inbound_audio():
    the corrupt slot is concealed (a full analysis-rate frame), AND the packet
    AFTER it (seq 2) still decodes to real, non-silent audio — proving the
    decoder's predictor state stayed coherent and the stream kept flowing,
    which is the actual security property (not merely a different exception
    type). Before the fix, _collect_n would time out here: only frame 0 is
    ever emitted before the uncaught OpusDecodeError kills the generator.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.OPUS,
        payload_type=_OPUS_PT,
        jitter_depth=1,
        clock=_dummy_clock,
        aec_enabled=False,
    )
    await engine.connect()

    enc = OpusEncoder()
    good = [
        enc.encode(
            _tone_pcm16(OPUS_FRAME_SAMPLES, OPUS_SAMPLE_RATE, freq_hz=300.0 + 25.0 * i)
        )
        for i in range(2)
    ]
    engine._recv_queue.put_nowait((_make_rtp(0, good[0], pt=_OPUS_PT), _FAKE_SRC))
    engine._recv_queue.put_nowait(
        (_make_rtp(1, _CORRUPT_OPUS_PAYLOAD, pt=_OPUS_PT), _FAKE_SRC)
    )
    engine._recv_queue.put_nowait((_make_rtp(2, good[1], pt=_OPUS_PT), _FAKE_SRC))

    frames = await _collect_n(engine, 3)
    await engine.stop()

    assert len(frames) == 3  # the corrupt packet is concealed, not fatal
    assert frames[0].sample_rate == _OPUS_ANALYSIS_RATE
    assert _rms(frames[0].samples) > 0.0  # seq 0 decodes normally

    samples_per_analysis_frame = (
        OPUS_FRAME_SAMPLES * _OPUS_ANALYSIS_RATE // OPUS_SAMPLE_RATE
    )
    assert frames[1].sample_rate == _OPUS_ANALYSIS_RATE
    assert (
        len(frames[1].samples) == samples_per_analysis_frame * 2
    )  # full concealment frame

    # THE call survives: the packet AFTER the corrupt one still decodes to
    # real, non-silent audio, proving the stream (and decoder predictor state)
    # stayed healthy — not just that an exception type changed.
    assert frames[2].sample_rate == _OPUS_ANALYSIS_RATE
    assert _rms(frames[2].samples) > 0.0


@pytest.mark.asyncio
async def test_opus_corrupt_successor_payload_falls_back_to_plc() -> None:
    """A genuine loss whose FEC-bearing successor is ALSO corrupt still conceals.

    seq 1 is truly lost (sequence gap, jitter_depth=1 -> Lost(1)); its would-be
    FEC source, seq 2, carries corrupt bytes instead of real Opus data. FEC
    recovery of frame 1 from packet 2 is therefore impossible — the engine must
    fall back to native PLC rather than let decode_fec's OpusDecodeError escape.
    The stream must still continue afterward (seq 3, a real packet, decodes).
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.OPUS,
        payload_type=_OPUS_PT,
        jitter_depth=1,
        clock=_dummy_clock,
        aec_enabled=False,
    )
    await engine.connect()

    enc = OpusEncoder()
    seq0 = enc.encode(_tone_pcm16(OPUS_FRAME_SAMPLES, OPUS_SAMPLE_RATE, freq_hz=300.0))
    seq3 = enc.encode(_tone_pcm16(OPUS_FRAME_SAMPLES, OPUS_SAMPLE_RATE, freq_hz=400.0))
    engine._recv_queue.put_nowait((_make_rtp(0, seq0, pt=_OPUS_PT), _FAKE_SRC))
    # seq 1 is never sent (a genuine loss); seq 2's payload is corrupt (would-be
    # FEC source for frame 1); seq 3 is a real, later packet.
    engine._recv_queue.put_nowait(
        (_make_rtp(2, _CORRUPT_OPUS_PAYLOAD, pt=_OPUS_PT), _FAKE_SRC)
    )
    engine._recv_queue.put_nowait((_make_rtp(3, seq3, pt=_OPUS_PT), _FAKE_SRC))

    frames = await _collect_n(engine, 3)
    await engine.stop()

    assert len(frames) == 3  # Lost(1) concealed via PLC fallback, not fatal
    samples_per_analysis_frame = (
        OPUS_FRAME_SAMPLES * _OPUS_ANALYSIS_RATE // OPUS_SAMPLE_RATE
    )
    assert len(frames[1].samples) == samples_per_analysis_frame * 2

    # Frame 2 (the corrupt packet itself, seq 2) is ALSO concealed rather than
    # propagating — the same protection applies to a corrupt "current" packet
    # as to a corrupt FEC-source successor.
    assert len(frames[2].samples) == samples_per_analysis_frame * 2


# ---------------------------------------------------------------------------
# Coalesced Lost(count=N): Opus PLC must match the pre-coalesce per-packet
# semantics — only the slot immediately before the successor uses FEC; the
# interpolated slots use PLC (finding #3, run-length-coalescing lane).
# ---------------------------------------------------------------------------


class _CountingOpusDecoder:
    """A spy wrapping a real OpusDecoder, counting FEC/PLC calls (test seam).

    Structurally assignable to the engine's ``_OpusDecode`` Protocol, so it can be
    injected as ``engine._opus_decoder`` to observe exactly how the coalesced-Lost
    concealment loop drives the decoder, without touching real audio behaviour.
    """

    def __init__(self) -> None:
        self._inner = OpusDecoder()
        self.decode_calls = 0
        self.fec_calls = 0
        self.plc_calls = 0
        self.fec_payloads: list[bytes] = []

    def decode(self, packet: bytes) -> bytes:
        self.decode_calls += 1
        return self._inner.decode(packet)

    def decode_fec(self, next_packet: bytes) -> bytes:
        self.fec_calls += 1
        self.fec_payloads.append(next_packet)
        return self._inner.decode_fec(next_packet)

    def decode_plc(self) -> bytes:
        self.plc_calls += 1
        return self._inner.decode_plc()


@pytest.mark.asyncio
async def test_opus_coalesced_lost_uses_fec_once_plc_for_rest() -> None:
    """A coalesced Lost(count=3) drives FEC exactly once, PLC for the other slots.

    seqs 0,1 arrive then 5,6,7 (jitter_depth=3) -> the buffer coalesces the gap
    into a single Lost(seq=2, count=3) with the successor (seq 5) already buffered.

    Pre-coalesce this gap was three separate Lost events: Lost(2) and Lost(3) saw
    an EMPTY anchor (PLC), and only Lost(4) — the slot immediately before the
    successor — saw the buffered successor and recovered via FEC. Opus in-band FEC
    only ever carries the ONE frame immediately preceding its packet, so the
    correct behaviour conceals the 3 slots as PLC, PLC, FEC(packet 5).

    The bug: the coalesced loop calls _conceal_opus() with the anchor ALREADY at
    the successor, so peek_next() returns packet 5 on every iteration and
    decode_fec(packet 5) runs 3 times (0 PLC) — redundant and semantically wrong
    (it "recovers" frames 2 and 3 from a FEC copy that is only frame 4's).
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.OPUS,
        payload_type=_OPUS_PT,
        jitter_depth=3,
        clock=_dummy_clock,
        aec_enabled=False,
    )
    await engine.connect()

    spy = _CountingOpusDecoder()
    engine._opus_decoder = spy  # inject the counting decoder before any decode

    enc = OpusEncoder(expected_packet_loss_pct=30)
    payloads = [
        enc.encode(
            _tone_pcm16(OPUS_FRAME_SAMPLES, OPUS_SAMPLE_RATE, freq_hz=300.0 + 25.0 * i)
        )
        for i in range(8)
    ]
    # seqs 0,1 arrive; 2,3,4 lost; 5,6,7 arrive -> one Lost(seq=2, count=3).
    for seq in (0, 1, 5, 6, 7):
        engine._recv_queue.put_nowait(
            (_make_rtp(seq, payloads[seq], pt=_OPUS_PT), _FAKE_SRC)
        )

    # 2 real (0,1) + 3 concealment (2,3,4) + 3 real (5,6,7) = 8 frames.
    frames = await _collect_n(engine, 8)
    await engine.stop()

    assert len(frames) == 8  # every hole filled
    # The crux: exactly ONE FEC decode (for the slot before the successor) and the
    # other two concealed slots use PLC — NOT three FEC decodes of packet 5.
    assert spy.fec_calls == 1, (
        f"expected 1 FEC decode for the coalesced run, got {spy.fec_calls}"
    )
    assert spy.plc_calls == 2, (
        f"expected 2 PLC decodes for the interpolated slots, got {spy.plc_calls}"
    )
    # The single FEC decode used the SUCCESSOR's payload (packet 5), not a repeat.
    assert spy.fec_payloads == [payloads[5]]


@pytest.mark.asyncio
async def test_opus_coalesced_lost_matches_individual_lost_events() -> None:
    """Lost(count=3) yields byte-identical concealment to three separate Losses.

    Drive the SAME stream twice against two engines with identical state — one
    where the gap coalesces into Lost(count=3), and a reference whose decoder is
    driven through the exact pre-coalesce FEC/PLC pattern (PLC, PLC, FEC(succ)).
    The three concealment frames must be byte-for-byte identical, proving the
    coalesced loop reproduces the per-packet PLC semantics exactly (no extra FEC
    decodes corrupting the predictor for the interpolated slots).
    """
    # Build the concealment frames the coalesced engine produces.
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.OPUS,
        payload_type=_OPUS_PT,
        jitter_depth=3,
        clock=_dummy_clock,
        aec_enabled=False,
    )
    await engine.connect()
    enc = OpusEncoder(expected_packet_loss_pct=30)
    payloads = [
        enc.encode(
            _tone_pcm16(OPUS_FRAME_SAMPLES, OPUS_SAMPLE_RATE, freq_hz=300.0 + 25.0 * i)
        )
        for i in range(8)
    ]
    for seq in (0, 1, 5, 6, 7):
        engine._recv_queue.put_nowait(
            (_make_rtp(seq, payloads[seq], pt=_OPUS_PT), _FAKE_SRC)
        )
    frames = await _collect_n(engine, 8)
    await engine.stop()
    coalesced_conceal = [f.samples for f in frames[2:5]]  # slots 2,3,4

    # Reference: a fresh decoder driven through frames 0,1 then the pre-coalesce
    # per-packet concealment pattern PLC, PLC, FEC(packet 5).
    ref = OpusDecoder()
    resampler = Resampler(OPUS_SAMPLE_RATE, _OPUS_ANALYSIS_RATE)
    # Mirror the engine EXACTLY: it decodes the two real frames through the same
    # state-carrying resampler it then uses for the concealment frames, so the
    # decoder AND resampler state at the point of concealment must match for a
    # byte-equal comparison. Drive frames 0,1 (decode + downsample), then the
    # pre-coalesce per-packet concealment pattern PLC, PLC, FEC(packet 5).
    resampler.resample(ref.decode(payloads[0]))
    resampler.resample(ref.decode(payloads[1]))
    expected_conceal = [
        resampler.resample(ref.decode_plc()),
        resampler.resample(ref.decode_plc()),
        resampler.resample(ref.decode_fec(payloads[5])),
    ]

    assert coalesced_conceal == expected_conceal
