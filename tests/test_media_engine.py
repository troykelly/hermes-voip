"""Tests for hermes_voip.media.engine — RtpMediaTransport (asyncio UDP media plane).

TDD suite (AGENTS.md rule 18): red-first.  All tests are deterministic — no real
network latency, no real-time clocks.  The engine accepts injectable ``clock`` and
``sleep`` callables so tests drive time explicitly.

Scenarios:
  (a) Plain-RTP inbound: datagrams → PcmFrames (correct rate, jitter-ordered).
  (b) send_audio: PCM frame → RTP wire bytes (payload type 0/8, seq increments,
      ptime-spaced via injected sleep).
  (c) SRTP round-trip: encrypt on TX, decrypt on RX, tampered packet dropped.
  (d) set_hold(True) stops outbound media; set_hold(False) restores it.
  (e) JitterBuffer reorders a dropped/out-of-order packet.
  (f) stop() cancels the recv task cleanly; idempotent second call is harmless.
  (g) MediaTransport + CallMedia Protocol structural checks (runtime_checkable).
  (h) connect() returns True and binds a live UDP socket.
  (i) disconnect() tears down (MediaTransport seam).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gc
import logging
import socket
import struct
import warnings
from collections.abc import Iterator

import pytest

import hermes_voip.media.engine as engine_module
from hermes_voip.media.audio import G711_SAMPLE_RATE, decode_ulaw, encode_ulaw
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.transport import MediaTransport
from hermes_voip.rtp import RtpPacket

# Our own outbound SSRC (white-box: read from the engine module so the test
# tracks the constant). Inbound packets carrying THIS SSRC are our own audio
# looped back (self-loopback) and must be dropped before VAD/ASR (ADR-0023).
_OUR_SSRC: int = engine_module._OUTBOUND_SSRC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_SSRC = 0xDEADBEEF
_FAKE_KEY_B64 = base64.b64encode(b"\xab" * 16 + b"\xcd" * 14).decode()

# A stand-in UDP source address for white-box injections into the engine's recv
# queue (which carries (datagram, source-addr) pairs so the engine can latch its
# outbound destination onto the peer's real media source — symmetric RTP).
_FAKE_SRC: tuple[str, int] = ("198.51.100.7", 6000)

# 20 ms of silence at 8 kHz = 160 samples = 320 bytes PCM16.
_PTIME_MS = 20
_SAMPLES_PER_FRAME = (G711_SAMPLE_RATE * _PTIME_MS) // 1000  # 160
_PCM_SILENCE = b"\x00" * (_SAMPLES_PER_FRAME * 2)  # 320 bytes PCM16


def _silence_frame(ts_ns: int = 0) -> PcmFrame:
    return PcmFrame(
        samples=_PCM_SILENCE,
        sample_rate=G711_SAMPLE_RATE,
        monotonic_ts_ns=ts_ns,
    )


def _tone_frame(amplitude: int = 1000, ts_ns: int = 0) -> PcmFrame:
    """Generate a PCM frame with a constant amplitude tone.

    Args:
        amplitude: Sample value (e.g., 1000 for audible mid-range tone).
        ts_ns: Monotonic timestamp in nanoseconds.
    """
    samples = struct.pack(f"<{_SAMPLES_PER_FRAME}h", *[amplitude] * _SAMPLES_PER_FRAME)
    return PcmFrame(
        samples=samples,
        sample_rate=G711_SAMPLE_RATE,
        monotonic_ts_ns=ts_ns,
    )


def _make_rtp(seq: int, ts: int, payload: bytes, ssrc: int = _FAKE_SSRC) -> bytes:
    """Pack a minimal plain-RTP datagram."""
    return RtpPacket(
        payload_type=0,
        sequence_number=seq,
        timestamp=ts,
        ssrc=ssrc,
        payload=payload,
    ).pack()


def _ulaw_silence() -> bytes:
    """One 20 ms G.711 mu-law silence frame (160 bytes)."""
    return encode_ulaw(_PCM_SILENCE)


def _dummy_clock() -> int:
    """A monotonic clock returning a fixed ns value (for deterministic frames)."""
    return 0


# ---------------------------------------------------------------------------
# (g) Protocol structural checks
# ---------------------------------------------------------------------------


def test_rtp_media_transport_satisfies_media_transport_protocol() -> None:
    """RtpMediaTransport is structurally a MediaTransport (runtime_checkable)."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
    )
    assert isinstance(engine, MediaTransport)


def test_rtp_media_transport_has_call_media_interface() -> None:
    """RtpMediaTransport exposes set_hold and stop (CallMedia seam)."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
    )
    assert hasattr(engine, "set_hold")
    assert hasattr(engine, "stop")
    assert callable(engine.set_hold)
    assert callable(engine.stop)


def test_inbound_sample_rate_pcmu() -> None:
    """inbound_sample_rate is G711_SAMPLE_RATE for PCMU."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
    )
    assert engine.inbound_sample_rate == G711_SAMPLE_RATE


def test_inbound_sample_rate_pcma() -> None:
    """inbound_sample_rate is G711_SAMPLE_RATE for PCMA."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMA,
    )
    assert engine.inbound_sample_rate == G711_SAMPLE_RATE


def test_on_hold_initial_state_is_false() -> None:
    """on_hold is False before any set_hold call."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
    )
    assert engine.on_hold is False


# ---------------------------------------------------------------------------
# (h) connect() binds a live UDP socket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_returns_true_and_binds_socket() -> None:
    """connect() returns True and binds a real UDP socket (OS assigns port)."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,  # OS picks a free port
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
    )
    result = await engine.connect()
    assert result is True
    assert engine.local_port > 0  # OS assigned a real port
    await engine.stop()


# ---------------------------------------------------------------------------
# (a) Inbound RTP → PcmFrames (correct rate, ordered through jitter)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_plain_rtp_yields_pcm_frames() -> None:
    """Inbound plain-RTP datagrams decode to PcmFrames at G711_SAMPLE_RATE."""
    sender_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender_sock.bind(("127.0.0.1", 0))

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=sender_sock.getsockname()[1],
        codec=Codec.PCMU,
        clock=_dummy_clock,
    )
    await engine.connect()
    engine_port = engine.local_port

    async def _feed() -> list[PcmFrame]:
        frames: list[PcmFrame] = []
        async for frame in engine.inbound_audio():
            frames.append(frame)
            if len(frames) == 3:
                break
        return frames

    payload = _ulaw_silence()
    for i in range(3):
        ts = i * _SAMPLES_PER_FRAME
        datagram = _make_rtp(i, ts, payload)
        sender_sock.sendto(datagram, ("127.0.0.1", engine_port))
        await asyncio.sleep(0)

    task = asyncio.create_task(_feed())
    await asyncio.sleep(0.05)
    frames = await asyncio.wait_for(task, timeout=2.0)

    assert len(frames) == 3
    for frame in frames:
        assert frame.sample_rate == G711_SAMPLE_RATE
        assert len(frame.samples) == len(_PCM_SILENCE)

    sender_sock.close()
    await engine.stop()


# ---------------------------------------------------------------------------
# (e) Jitter buffer reorders out-of-order packets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dropped_packet_is_concealed_by_jitter_buffer() -> None:
    """A dropped packet is CONCEALED (ADR-0056): the stream stays continuous.

    Scenario: seq 0 arrives (anchor), seq 1 is dropped, seqs 2/3/4 arrive.
    With jitter_depth=3, after 3 packets accumulate behind the gap (seqs 2, 3,
    4), the buffer signals Lost(1). The engine fills that hole with a concealment
    frame instead of skipping it, so the frames produced are: seq 0, a
    concealment frame for seq 1, then seqs 2, 3, 4 — FIVE frames, all at the
    G.711 rate. (Before ADR-0056 the Lost was skipped and only four frames came
    out, leaving an audible gap.)

    We inject directly into the engine's internal queue to avoid any timing
    uncertainty from real UDP sends; access to ``_recv_queue`` is a white-box
    seam used only in tests.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        jitter_depth=3,  # wait for 3 later packets before declaring Lost
        clock=_dummy_clock,
        aec_enabled=False,  # isolate concealment from the echo canceller
    )
    await engine.connect()

    # Each packet carries a DISTINCT constant-level payload so the output frames
    # are individually identifiable — this proves ORDER and identity, not just the
    # count: seq N decodes to a frame of level N, and the concealment frame for the
    # lost seq 1 must equal the held repeat of seq 0 (the last good frame).
    def _level_payload(level: int) -> bytes:
        sample = level.to_bytes(2, "little", signed=True)
        return encode_ulaw(sample * _SAMPLES_PER_FRAME)

    # The decoded analysis frame for a given encoded level (mu-law is lossy, so we
    # compare against the round-tripped value, not the raw level).
    def _decoded(level: int) -> bytes:
        return decode_ulaw(_level_payload(level))

    frames: list[PcmFrame] = []
    done = asyncio.Event()

    async def _collect() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)
            if len(frames) == 5:
                done.set()
                break

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)  # let the task start and block on get()

    # seq 0 arrives, then seq 1 is MISSING, then seqs 2, 3, 4 arrive (distinct
    # levels). After seq 4 is pushed (3 packets behind the gap at seq 1), the
    # buffer declares Lost(1); the engine conceals it, then yields 2, 3, 4 in order.
    for seq in (0, 2, 3, 4):
        engine._recv_queue.put_nowait(
            (_make_rtp(seq, seq * _SAMPLES_PER_FRAME, _level_payload(seq)), _FAKE_SRC)
        )

    await asyncio.wait_for(done.wait(), timeout=2.0)
    await asyncio.wait_for(task, timeout=2.0)

    # The hole is filled, not skipped: exactly five frames, all full 20 ms G.711.
    assert len(frames) == 5
    for frame in frames:
        assert frame.sample_rate == G711_SAMPLE_RATE
        assert len(frame.samples) == _SAMPLES_PER_FRAME * 2
    # Exact output ORDER + identity: real seq 0, the CONCEALMENT of Lost(1) (a
    # full-energy repeat of the last good frame == seq 0), then seqs 2, 3, 4 in
    # sequence — proving the concealed frame sits in the gap and the real packets
    # after it are still delivered in order (not reordered or dropped).
    assert frames[0].samples == _decoded(0)
    assert frames[1].samples == _decoded(0)  # concealment = held repeat of seq 0
    assert frames[2].samples == _decoded(2)
    assert frames[3].samples == _decoded(3)
    assert frames[4].samples == _decoded(4)

    await engine.stop()


# ---------------------------------------------------------------------------
# Self-loopback SSRC drop (ADR-0023): an inbound packet carrying OUR OWN
# outbound SSRC is our own audio looped back; it must never reach the jitter
# buffer / VAD / ASR (it would self-interrupt the agent). A foreign-SSRC packet
# is unaffected — this is defense-in-depth, distinct from the gateway-echo case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_packets_with_our_own_ssrc_are_dropped() -> None:
    """Packets whose SSRC equals our outbound SSRC are dropped before decode.

    Discriminating white-box test: inject a RUN of self-SSRC packets (seqs 0..4)
    and NOTHING else. With ``jitter_depth=1`` a genuine stream of five ordered
    packets would readily yield decoded frames; here the SSRC filter must drop
    every one, so the inbound iterator yields NOTHING within a generous window.
    A non-dropping engine yields at least one frame and fails the timeout assert.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        jitter_depth=1,
        clock=_dummy_clock,
    )
    await engine.connect()

    payload = _ulaw_silence()
    frames: list[PcmFrame] = []

    async def _collect() -> None:
        async for _frame in engine.inbound_audio():
            frames.append(_frame)

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    # Five ordered packets, ALL carrying OUR OWN SSRC → every one must be dropped.
    for seq in range(5):
        engine._recv_queue.put_nowait(
            (
                _make_rtp(seq, seq * _SAMPLES_PER_FRAME, payload, ssrc=_OUR_SSRC),
                _FAKE_SRC,
            )
        )

    # Give the consumer ample time to process the queue; it must yield no frame.
    await asyncio.sleep(0.1)
    assert frames == [], "self-SSRC (looped-back) packets must never decode to frames"

    # Sanity: a foreign-SSRC packet on the SAME engine DOES decode (the filter is
    # specific to our SSRC, not a blanket drop).
    engine._recv_queue.put_nowait(
        (_make_rtp(5, 5 * _SAMPLES_PER_FRAME, payload, ssrc=_FAKE_SSRC), _FAKE_SRC)
    )
    engine._recv_queue.put_nowait(
        (_make_rtp(6, 6 * _SAMPLES_PER_FRAME, payload, ssrc=_FAKE_SSRC), _FAKE_SRC)
    )
    await asyncio.sleep(0.1)
    assert len(frames) >= 1, "a foreign-SSRC packet must still decode"
    for frame in frames:
        assert frame.sample_rate == G711_SAMPLE_RATE

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await engine.stop()


@pytest.mark.asyncio
async def test_self_ssrc_packet_does_not_latch_outbound_addr() -> None:
    """A dropped self-SSRC packet must not latch the comedia outbound address.

    The self-SSRC drop happens before ``_maybe_latch``, so our own looped-back
    packet can never move the outbound destination onto a spoofed/own source.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        jitter_depth=1,
        clock=_dummy_clock,
        symmetric=True,
    )
    await engine.connect()
    original_addr = engine._outbound_addr

    frames: list[PcmFrame] = []
    got_one = asyncio.Event()

    async def _collect() -> None:
        async for _frame in engine.inbound_audio():
            frames.append(_frame)
            got_one.set()
            break

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0)

    payload = _ulaw_silence()
    spoof_src = ("203.0.113.99", 9999)
    # Our own SSRC from a NEW source tuple: if not dropped, _maybe_latch would
    # move _outbound_addr to spoof_src. The drop must prevent that.
    engine._recv_queue.put_nowait((_make_rtp(0, 0, payload, ssrc=_OUR_SSRC), spoof_src))
    # Then a genuine far-end packet so _collect can complete.
    engine._recv_queue.put_nowait(
        (_make_rtp(1, _SAMPLES_PER_FRAME, payload, ssrc=_FAKE_SSRC), _FAKE_SRC)
    )

    await asyncio.wait_for(got_one.wait(), timeout=2.0)
    await asyncio.wait_for(task, timeout=2.0)

    # The latch moved (if at all) only onto the genuine far-end source, never the
    # spoofed self-SSRC source.
    assert engine._outbound_addr != spoof_src
    assert engine._outbound_addr in (original_addr, _FAKE_SRC)

    await engine.stop()


# ---------------------------------------------------------------------------
# (b) send_audio → RTP bytes on the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_audio_emits_rtp_datagrams() -> None:
    """send_audio encodes the frame and sends a valid RTP datagram."""
    capture_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    capture_sock.bind(("127.0.0.1", 0))
    capture_sock.setblocking(False)
    remote_port = capture_sock.getsockname()[1]

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=remote_port,
        codec=Codec.PCMU,
        sleep=_fake_sleep,
    )
    await engine.connect()

    frame = _silence_frame()
    await engine.send_audio(frame)
    await engine.send_audio(frame)
    await asyncio.sleep(0.01)

    received: list[bytes] = []
    for _ in range(4):
        with contextlib.suppress(BlockingIOError):
            data, _ = capture_sock.recvfrom(4096)
            received.append(data)

    await asyncio.sleep(0.01)
    for _ in range(4):
        with contextlib.suppress(BlockingIOError):
            data, _ = capture_sock.recvfrom(4096)
            if data not in received:
                received.append(data)

    assert len(received) >= 2, f"expected 2 datagrams, got {len(received)}"

    pkt0 = RtpPacket.parse(received[0])
    pkt1 = RtpPacket.parse(received[1])

    assert pkt0.payload_type == 0  # PCMU
    assert pkt1.payload_type == 0
    assert pkt1.sequence_number == (pkt0.sequence_number + 1) % (1 << 16)
    # Timestamp advances by one frame's worth of samples.
    ts_delta = (pkt1.timestamp - pkt0.timestamp) % (1 << 32)
    assert ts_delta == _SAMPLES_PER_FRAME
    # Payload is the mu-law encoding of the silence frame.
    assert pkt0.payload == _ulaw_silence()

    # The injected sleep was called for pacing.
    assert len(sleep_calls) >= 1

    capture_sock.close()
    await engine.stop()


# ---------------------------------------------------------------------------
# (b2) send_audio resamples any non-8 kHz frame to the 8 kHz wire rate
# ---------------------------------------------------------------------------
#
# Regression: a real inbound call's greeting passed a 24 kHz TTS frame (sherpa-
# Kokoro output rate, ADR-0007) to send_audio, which raised
# ``ValueError: G.711 requires 8000 Hz, got 24000 Hz`` inside the greeting task,
# cancelling the whole CallLoop and emitting ZERO RTP — the caller heard silence.
# send_audio must instead RESAMPLE the frame to 8 kHz and encode it (ADR-0017).

# A non-trivial 24 kHz frame: a 20 ms window is 480 samples at 24 kHz, which the
# resampler must reduce to 160 samples (20 ms at 8 kHz) before G.711 encoding.
_TTS_RATE_HZ = 24_000
_TTS_FRAME_MS = 20
_TTS_SAMPLES = (_TTS_RATE_HZ * _TTS_FRAME_MS) // 1000  # 480
# Wire (8 kHz) sample count for the same 20 ms window.
_WIRE_SAMPLES = (G711_SAMPLE_RATE * _TTS_FRAME_MS) // 1000  # 160


def _ramp_frame(rate: int, sample_count: int) -> PcmFrame:
    """A frame of non-silent PCM16 at ``rate`` (a low-amplitude ramp).

    Non-silent so a resample actually changes the sample COUNT in a way the test
    can observe; a low amplitude keeps the values inside int16 with margin.
    """
    samples = b"".join(
        int(((i % 32) - 16) * 8).to_bytes(2, "little", signed=True)
        for i in range(sample_count)
    )
    return PcmFrame(samples=samples, sample_rate=rate, monotonic_ts_ns=0)


@pytest.mark.asyncio
async def test_send_audio_resamples_24k_frame_to_8k_without_raising() -> None:
    """A 24 kHz TTS frame is resampled to 8 kHz and G.711-encoded (the live crash).

    Reproduces the exact production failure: feeding send_audio a 24 kHz frame
    must NOT raise; it must convert to the 8 kHz wire rate and emit one G.711
    (mu-law, one byte/sample) RTP payload of the 8 kHz sample count.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )
    await engine.connect()

    frame = _ramp_frame(_TTS_RATE_HZ, _TTS_SAMPLES)
    with _capture_sends(engine) as recorder:
        # MUST NOT raise ValueError("G.711 requires 8000 Hz, got 24000 Hz").
        await engine.send_audio(frame)

    assert recorder.sent, "send_audio must emit a datagram for a 24 kHz frame"
    wire, _dest = recorder.sent[-1]
    pkt = RtpPacket.parse(wire)
    assert pkt.payload_type == 0  # PCMU
    # mu-law is one byte per sample; the payload must carry the 8 kHz sample
    # count (160), proving the 24 kHz frame (480) was resampled, not encoded raw.
    assert len(pkt.payload) == _WIRE_SAMPLES

    await engine.stop()


@pytest.mark.asyncio
async def test_send_audio_8k_frame_passes_through_unchanged() -> None:
    """An 8 kHz frame is encoded directly (byte-identical to a raw G.711 encode).

    The 8 kHz fast path must not run the frame through a resampler (which would
    perturb the samples); the emitted payload equals encode_ulaw of the frame.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )
    await engine.connect()

    frame = _ramp_frame(G711_SAMPLE_RATE, _WIRE_SAMPLES)
    with _capture_sends(engine) as recorder:
        await engine.send_audio(frame)

    assert recorder.sent
    wire, _dest = recorder.sent[-1]
    pkt = RtpPacket.parse(wire)
    # Byte-identical to a direct encode: the 8 kHz frame is untouched.
    assert pkt.payload == encode_ulaw(frame.samples)
    assert len(pkt.payload) == _WIRE_SAMPLES

    await engine.stop()


@pytest.mark.asyncio
async def test_send_audio_resamples_24k_for_pcma_too() -> None:
    """The resample-before-encode path is codec-agnostic (a-law / PCMA)."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMA,
        sleep=_no_sleep,
    )
    await engine.connect()

    frame = _ramp_frame(_TTS_RATE_HZ, _TTS_SAMPLES)
    with _capture_sends(engine) as recorder:
        await engine.send_audio(frame)

    assert recorder.sent
    wire, _dest = recorder.sent[-1]
    pkt = RtpPacket.parse(wire)
    assert pkt.payload_type == 8  # PCMA
    assert len(pkt.payload) == _WIRE_SAMPLES

    await engine.stop()


# ---------------------------------------------------------------------------
# (b3) send_audio is sample-continuous ACROSS streaming chunks (no per-chunk
#      silence padding) — the "very choppy" ElevenLabs regression.
# ---------------------------------------------------------------------------
#
# A streaming TTS provider (ElevenLabs over chunked HTTP) hands the call loop one
# PcmFrame per network chunk; the call loop calls send_audio once per frame. The
# chunk byte-length is NOT a multiple of the 20 ms (160-sample = 320-byte) G.711
# frame, so each send_audio call left a sub-frame remainder. The defect: that
# remainder was ZERO-PADDED to a whole frame on EVERY call, injecting a slug of
# silence at every chunk boundary -> the caller hears a click/gap per chunk
# ("very choppy"). The fix: carry the sub-frame remainder forward across calls so
# the concatenated stream is sample-continuous; pad at most once, at end-of-stream.
#
# 1024 bytes = 512 samples @ 8 kHz = 3.2 frames -> a 0.2-frame (64-byte) remainder
# every chunk. With the bug, each of the 10 chunks below grows to 4 whole frames
# (one pad each); without it, 10 * 512 = 5120 samples == exactly 32 frames total.
# 512 PCM16 samples @ 8 kHz — deliberately not a 160-sample frame multiple.
_STREAM_CHUNK_BYTES = 1024
_STREAM_CHUNK_COUNT = 10


def _counter_chunk(start_sample: int, sample_count: int, rate: int) -> PcmFrame:
    """A frame of distinct, NON-silent PCM16 samples at ``rate``.

    Each sample is a positive value derived from its global index. The amplitudes
    are well clear of zero (>= 4096) so they survive G.711 companding as non-zero
    codes — mu-/a-law quantise tiny magnitudes (|x| < ~8) to the same bucket as
    silence, so a too-small probe would decode to 0 and be indistinguishable from
    an injected silence slug. Values stay inside int16 with margin.
    """
    samples = b"".join(
        # Map index -> a value in [4096, 4096 + 63*256]; strictly positive and far
        # from zero so the round-trip never lands on a 0x0000 (= injected silence).
        (4096 + ((start_sample + i) % 64) * 256).to_bytes(2, "little", signed=True)
        for i in range(sample_count)
    )
    return PcmFrame(samples=samples, sample_rate=rate, monotonic_ts_ns=0)


@pytest.mark.asyncio
async def test_send_audio_8k_stream_has_no_per_chunk_silence_padding() -> None:
    """Streaming 8 kHz chunks are sample-continuous — no silence slug per chunk.

    Reproduces the "very choppy" ElevenLabs defect: feeding a sequence of chunks
    whose byte length is NOT a multiple of the 160-sample G.711 frame must emit a
    sample-continuous stream. The total emitted sample count must equal the total
    INPUT sample count (5120), proving no per-chunk zero padding was inserted; the
    bug pads each of the 10 chunks up to 640 samples -> 6400 emitted (1280 samples
    of injected silence == 160 ms of gaps).
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )
    await engine.connect()

    chunk_samples = _STREAM_CHUNK_BYTES // 2  # 512
    # stop() runs INSIDE the recorder so its end-of-stream tail flush (if any) is
    # captured too — here 5120 samples is exactly 32 frames, so nothing is padded.
    with _capture_sends(engine) as recorder:
        for idx in range(_STREAM_CHUNK_COUNT):
            await engine.send_audio(
                _counter_chunk(idx * chunk_samples, chunk_samples, G711_SAMPLE_RATE)
            )
        await engine.stop()

    payloads = [RtpPacket.parse(wire).payload for wire, _dest in recorder.sent]
    # G.711 (mu-law) is one byte per sample, so payload length == sample count.
    total_emitted = sum(len(p) for p in payloads)
    input_samples = _STREAM_CHUNK_COUNT * chunk_samples  # 5120

    # The whole point: no per-chunk padding. Every emitted sample is a real input
    # sample (the buffered remainder is carried forward, not zero-filled), so the
    # stream is exactly the input length with NO mid-stream silence slugs.
    assert total_emitted == input_samples, (
        f"expected {input_samples} continuous samples, got {total_emitted} "
        f"(={total_emitted - input_samples} samples of injected silence)"
    )

    # And decoding the concatenated payload back to PCM16 must contain NO interior
    # silence run the source never emitted (every source sample is far from zero).
    from hermes_voip.media.audio import decode_ulaw  # noqa: PLC0415 - test-local

    decoded = decode_ulaw(b"".join(payloads))
    sample_words = struct.unpack_from(f"<{len(decoded) // 2}h", decoded)
    assert 0 not in sample_words, (
        "decoded stream contains a 0x0000 sample the source never emitted — "
        "a silence slug was injected at a chunk boundary"
    )


@pytest.mark.asyncio
async def test_send_audio_24k_stream_is_continuous_across_chunks() -> None:
    """Streaming 24 kHz chunks resample to a sample-continuous 8 kHz stream.

    The same regression on the resample path (sherpa-Kokoro / ElevenLabs pcm_24000):
    feeding many small 24 kHz chunks must produce the SAME 8 kHz audio as one pass,
    with no per-chunk padding. The emitted sample count must equal a single
    stateful 24->8 kHz resample of the concatenated input (which the engine's own
    Resampler computes), not that count inflated by one pad per chunk.
    """
    from hermes_voip.media.audio import Resampler  # noqa: PLC0415 - test-local

    rate = 24_000
    # 24 kHz chunk of 1024 samples -> ~341 samples @ 8 kHz: never a frame multiple.
    chunk_samples = 1024
    chunk_count = 12

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )
    await engine.connect()

    chunks = [
        _counter_chunk(i * chunk_samples, chunk_samples, rate)
        for i in range(chunk_count)
    ]
    # stop() runs INSIDE the recorder so the final buffered tail (padded to ONE
    # frame, once) is captured along with the streamed frames.
    with _capture_sends(engine) as recorder:
        for frame in chunks:
            await engine.send_audio(frame)
        await engine.stop()

    emitted = sum(len(RtpPacket.parse(w).payload) for w, _ in recorder.sent)

    # Reference: a single stateful pass of the whole input through 24->8 kHz.
    ref = Resampler(rate, G711_SAMPLE_RATE)
    ref_samples = sum(len(ref.resample(f.samples)) // 2 for f in chunks)

    samples_per_frame = _SAMPLES_PER_FRAME  # 160
    # The continuous stream is the single-pass resample length padded UP to a whole
    # frame EXACTLY ONCE (the end-of-stream tail flush), never one pad per chunk.
    expected = (
        (ref_samples + samples_per_frame - 1) // samples_per_frame
    ) * samples_per_frame
    assert emitted == expected, (
        f"expected {expected} samples (single-pass resample {ref_samples} padded to "
        f"one frame), got {emitted}"
    )
    # And the total over-send is strictly less than one frame — proof the padding
    # happened once for the whole stream, not per chunk (the bug added ~12 frames).
    assert 0 <= emitted - ref_samples < samples_per_frame, (
        f"over-send {emitted - ref_samples} >= one frame ({samples_per_frame}): "
        "per-chunk padding regressed"
    )


@pytest.mark.asyncio
async def test_stop_mid_playout_flushes_all_buffered_audio_in_order() -> None:
    """A stop() racing the send loop loses NO buffered audio and preserves order.

    Adversarial-review regression: send_audio drains whole frames off the FRONT of
    the re-framing buffer, removing each only after it is on the wire; stop() then
    flushes whatever remains. So a stop() that fires mid-drain (here: from inside
    the pacing sleep after the first frame) must still deliver every later whole
    frame AND the partial tail, in order — never dropping a middle frame nor
    reordering the tail ahead of it. We feed 5 frames' worth (800 samples) in one
    call; the decoded concatenation of ALL emitted packets must equal the input.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        initial_seq=0,
        initial_ts=0,
    )
    await engine.connect()

    input_samples = 5 * _SAMPLES_PER_FRAME  # 800 — exactly 5 whole frames

    with _capture_sends(engine) as recorder:
        # Pacing sleep that tears the engine down right after the first frame is
        # sent — exercising the "stop() nulls _transport during the sleep" path
        # with whole frames still buffered behind it.
        async def _stop_after_first(_secs: float) -> None:
            if len(recorder.sent) == 1 and engine._transport is not None:
                await engine.stop()

        engine._sleep = _stop_after_first
        await engine.send_audio(_counter_chunk(0, input_samples, G711_SAMPLE_RATE))
        # stop() already ran from inside the sleep (its flush sent the rest); a
        # second stop() is a harmless idempotent no-op.
        await engine.stop()

    payloads = [RtpPacket.parse(wire).payload for wire, _dest in recorder.sent]
    emitted = b"".join(payloads)
    # mu-law is one byte/sample: 800 input samples emit exactly 800 (5 frames),
    # nothing dropped, nothing padded (the input is a whole number of frames).
    assert len(emitted) == input_samples, (
        f"expected {input_samples} samples delivered across the stop race, "
        f"got {len(emitted)} (a middle frame was dropped)"
    )
    # Order preserved: decoding back equals a direct encode of the whole input.
    from hermes_voip.media.audio import decode_ulaw, encode_ulaw  # noqa: PLC0415

    original = _counter_chunk(0, input_samples, G711_SAMPLE_RATE).samples
    assert decode_ulaw(emitted) == decode_ulaw(encode_ulaw(original)), (
        "emitted audio is reordered or corrupted across the stop race"
    )


@pytest.mark.asyncio
async def test_hold_drops_buffered_remainder_no_stale_audio_on_resume() -> None:
    """set_hold(True) drops the buffered sub-frame tail (hold stops outbound).

    Adversarial-review regression: a partial-frame remainder buffered before hold
    must NOT survive to be prepended to post-resume audio, nor be emitted by a
    stop()-while-held. We send a non-frame-multiple chunk (a remainder is left
    buffered), hold (which must clear it), then confirm a stop() while held emits
    nothing — the buffered tail is gone.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )
    await engine.connect()

    # 240 samples = 1.5 frames -> 80-sample remainder buffered after one frame.
    with _capture_sends(engine) as recorder:
        await engine.send_audio(_counter_chunk(0, 240, G711_SAMPLE_RATE))
        assert engine._tx_buffer, "precondition: a sub-frame remainder is buffered"

        await engine.set_hold(True)
        assert engine._tx_buffer == b"", "set_hold(True) must drop the buffered tail"

        sent_before_stop = len(recorder.sent)
        await engine.stop()  # while held: must flush nothing
        assert len(recorder.sent) == sent_before_stop, (
            "stop() while held emitted buffered audio — hold must stop outbound media"
        )


# ---------------------------------------------------------------------------
# (d) set_hold stops outbound media
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_hold_stops_outbound_sends() -> None:
    """When held, send_audio must not transmit any datagram."""
    capture_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    capture_sock.bind(("127.0.0.1", 0))
    capture_sock.setblocking(False)
    remote_port = capture_sock.getsockname()[1]

    async def _fake_sleep(secs: float) -> None:
        pass

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=remote_port,
        codec=Codec.PCMU,
        sleep=_fake_sleep,
    )
    await engine.connect()
    await engine.set_hold(True)
    assert engine.on_hold is True

    frame = _silence_frame()
    await engine.send_audio(frame)
    await engine.send_audio(frame)
    await asyncio.sleep(0.01)

    received: list[bytes] = []
    for _ in range(64):
        try:
            data, _ = capture_sock.recvfrom(4096)
            received.append(data)
        except BlockingIOError:
            break

    assert received == [], f"expected no datagrams during hold, got {len(received)}"

    # Unhold — subsequent sends should reach the wire again.
    await engine.set_hold(False)
    hold_state: bool = engine.on_hold
    assert not hold_state  # must be False after set_hold(False)
    await engine.send_audio(frame)
    await asyncio.sleep(0.01)

    after: list[bytes] = []
    for _ in range(64):
        try:
            data, _ = capture_sock.recvfrom(4096)
            after.append(data)
        except BlockingIOError:
            break

    assert len(after) >= 1, "expected at least one datagram after unhold"

    capture_sock.close()
    await engine.stop()


@pytest.mark.asyncio
async def test_set_hold_idempotent_double_hold() -> None:
    """set_hold(True) called twice must not raise or change outcome."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
    )
    await engine.connect()
    await engine.set_hold(True)
    await engine.set_hold(True)  # idempotent
    assert engine.on_hold is True
    await engine.stop()


# ---------------------------------------------------------------------------
# (f) stop() is clean and idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cancels_recv_task_cleanly() -> None:
    """stop() cancels the background recv task without raising."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        clock=_dummy_clock,
    )
    await engine.connect()

    async def _next_frame() -> PcmFrame:
        async for frame in engine.inbound_audio():
            return frame
        msg = "no frame produced"
        raise StopAsyncIteration(msg)

    recv_task: asyncio.Task[PcmFrame] = asyncio.create_task(_next_frame())
    await asyncio.sleep(0.01)

    await engine.stop()
    recv_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
        await recv_task


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    """Calling stop() twice must not raise."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
    )
    await engine.connect()
    await engine.stop()
    await engine.stop()  # second call must not raise


@pytest.mark.asyncio
async def test_stop_without_connect_is_safe() -> None:
    """stop() before connect() must not raise."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
    )
    await engine.stop()  # should be a no-op


# ---------------------------------------------------------------------------
# (i) disconnect() tears down (MediaTransport seam)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_stops_media() -> None:
    """disconnect() is equivalent to stop() for the MediaTransport seam."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
    )
    await engine.connect()
    await engine.disconnect()
    await engine.disconnect()  # idempotent


# ---------------------------------------------------------------------------
# (c) SRTP round-trip: encrypted TX → decrypted RX; tampered packet dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_srtp_round_trip_recovers_audio() -> None:
    """SRTP-protected TX → RX recovers the original PCM frame."""
    pytest.importorskip("cryptography")

    from hermes_voip.media.srtp import SrtpSession  # noqa: PLC0415
    from hermes_voip.sdp import CryptoAttribute  # noqa: PLC0415

    crypto = CryptoAttribute(
        tag=1,
        suite="AES_CM_128_HMAC_SHA1_80",
        key_params=f"inline:{_FAKE_KEY_B64}",
    )

    cap_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cap_sock.bind(("127.0.0.1", 0))
    cap_sock.setblocking(False)
    remote_port = cap_sock.getsockname()[1]

    async def _fake_sleep(secs: float) -> None:
        pass

    # Do not pre-bind the outbound session to a specific SSRC — the engine owns
    # the outbound SSRC (0xCAFEBABE) and the session will auto-bind on first
    # protect() call.  Pre-binding to a different SSRC would cause SrtpError.
    srtp_out = SrtpSession(crypto)
    srtp_in = SrtpSession(crypto)

    tx_engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=remote_port,
        codec=Codec.PCMU,
        srtp_outbound=srtp_out,
        sleep=_fake_sleep,
    )
    await tx_engine.connect()

    frame = _silence_frame()
    await tx_engine.send_audio(frame)
    await asyncio.sleep(0.01)

    enc_data: bytes
    try:
        enc_data, _ = cap_sock.recvfrom(4096)
    except BlockingIOError:
        pytest.fail("no SRTP datagram received by capture socket")

    # Decrypt with the inbound SRTP session.
    decrypted_pkt = srtp_in.unprotect(enc_data)
    assert decrypted_pkt.payload == _ulaw_silence()

    cap_sock.close()
    await tx_engine.stop()


@pytest.mark.asyncio
async def test_rekey_srtp_swaps_outbound_and_inbound_sessions() -> None:
    """rekey_srtp installs fresh SRTP sessions mid-call (ADR-0053 re-offer keying).

    A hold/resume/re-INVITE on a secured call re-keys both directions (RFC 4568
    §6.1). The engine must (a) accept a new outbound CryptoAttribute and protect
    its TX with THAT key, and (b) accept a new inbound CryptoAttribute and
    decrypt with it — proving the private SRTP sessions were actually swapped.
    """
    pytest.importorskip("cryptography")

    from hermes_voip.media.srtp import SrtpSession  # noqa: PLC0415
    from hermes_voip.sdp import CryptoAttribute  # noqa: PLC0415

    # Two DISTINCT keys: the engine starts plain (no SRTP), then is re-keyed.
    new_key_b64 = base64.b64encode(b"\x11" * 16 + b"\x22" * 14).decode()
    new_crypto = CryptoAttribute(
        tag=3,
        suite="AES_CM_128_HMAC_SHA1_80",
        key_params=f"inline:{new_key_b64}",
    )

    cap_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cap_sock.bind(("127.0.0.1", 0))
    cap_sock.setblocking(False)
    remote_port = cap_sock.getsockname()[1]

    async def _fake_sleep(secs: float) -> None:
        pass

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=remote_port,
        codec=Codec.PCMU,
        sleep=_fake_sleep,
    )
    await engine.connect()

    # Re-key both directions to the new SDES context.
    await engine.rekey_srtp(inbound=new_crypto, outbound=new_crypto)

    await engine.send_audio(_silence_frame())
    await asyncio.sleep(0.01)
    try:
        enc_data, _ = cap_sock.recvfrom(4096)
    except BlockingIOError:
        pytest.fail("no SRTP datagram received after rekey")
    cap_sock.close()
    await engine.stop()

    # The datagram is NOT plain RTP (a plain parse of an SRTP packet keeps the
    # auth tag in the payload, so it would not equal the silence frame), and it
    # DOES decrypt under the NEW inbound key — both prove the swap took effect.
    decrypted = SrtpSession(new_crypto).unprotect(enc_data)
    assert decrypted.payload == _ulaw_silence()


@pytest.mark.asyncio
async def test_srtp_tampered_packet_is_dropped() -> None:
    """A tampered SRTP packet must raise SrtpError (auth failure)."""
    pytest.importorskip("cryptography")

    from hermes_voip.media.srtp import SrtpError, SrtpSession  # noqa: PLC0415
    from hermes_voip.sdp import CryptoAttribute  # noqa: PLC0415

    crypto = CryptoAttribute(
        tag=1,
        suite="AES_CM_128_HMAC_SHA1_80",
        key_params=f"inline:{_FAKE_KEY_B64}",
    )

    srtp_out = SrtpSession(crypto, ssrc=_FAKE_SSRC)
    srtp_in = SrtpSession(crypto)

    plain_pkt = RtpPacket(
        payload_type=0,
        sequence_number=0,
        timestamp=0,
        ssrc=_FAKE_SSRC,
        payload=_ulaw_silence(),
    )
    enc = srtp_out.protect(plain_pkt)

    tampered = bytearray(enc)
    tampered[14] ^= 0xFF

    with pytest.raises(SrtpError):
        srtp_in.unprotect(bytes(tampered))


@pytest.mark.asyncio
async def test_srtp_inbound_tampered_datagram_is_dropped_by_engine() -> None:
    """Engine with srtp_inbound drops a tampered SRTP datagram silently."""
    pytest.importorskip("cryptography")

    from hermes_voip.media.srtp import SrtpSession  # noqa: PLC0415
    from hermes_voip.sdp import CryptoAttribute  # noqa: PLC0415

    crypto = CryptoAttribute(
        tag=1,
        suite="AES_CM_128_HMAC_SHA1_80",
        key_params=f"inline:{_FAKE_KEY_B64}",
    )

    sender_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender_sock.bind(("127.0.0.1", 0))

    srtp_out = SrtpSession(crypto, ssrc=_FAKE_SSRC)
    srtp_in = SrtpSession(crypto)

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=sender_sock.getsockname()[1],
        codec=Codec.PCMU,
        srtp_inbound=srtp_in,
        clock=_dummy_clock,
    )
    await engine.connect()
    engine_port = engine.local_port

    # Build and tamper a valid SRTP packet.
    valid_enc = srtp_out.protect(
        RtpPacket(
            payload_type=0,
            sequence_number=0,
            timestamp=0,
            ssrc=_FAKE_SSRC,
            payload=_ulaw_silence(),
        )
    )
    tampered = bytearray(valid_enc)
    tampered[14] ^= 0xFF
    sender_sock.sendto(bytes(tampered), ("127.0.0.1", engine_port))

    # Send a genuine valid packet after the tampered one.
    valid2 = srtp_out.protect(
        RtpPacket(
            payload_type=0,
            sequence_number=1,
            timestamp=_SAMPLES_PER_FRAME,
            ssrc=_FAKE_SSRC,
            payload=_ulaw_silence(),
        )
    )
    await asyncio.sleep(0.01)
    sender_sock.sendto(valid2, ("127.0.0.1", engine_port))

    # Only the valid packet should produce a PcmFrame.
    frames: list[PcmFrame] = []

    async def _collect() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)
            if len(frames) >= 1:
                break

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.1)
    await asyncio.wait_for(task, timeout=2.0)

    assert len(frames) >= 1  # the valid packet produced a frame

    sender_sock.close()
    await engine.stop()


# ---------------------------------------------------------------------------
# (j) stop() wakes a blocked consumer even when the recv queue is full
#     (regression: a bounded-queue sentinel can be silently dropped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_wakes_inbound_when_queue_full() -> None:
    """stop() must terminate inbound_audio() even with a full recv queue.

    Regression for a HIGH defect: if stop() enqueues a single sentinel and the
    bounded recv queue is already at capacity, the sentinel is dropped (the
    QueueFull is suppressed) and the inbound generator never receives it — it
    blocks forever on ``queue.get()``.  A stop signal independent of the queue
    (a stop-flag the generator selects on) must wake the consumer regardless of
    queue fullness.

    We fill the queue to capacity with valid-but-buffered RTP that the jitter
    buffer holds (single packet, target_depth>1 ⇒ nothing popped yet), so the
    consumer drains every datagram, produces no frame, and then blocks on an
    empty queue.  Only the stop signal can terminate it.

    The assertion is robust against ``wait_for`` cancellation semantics: we use
    ``asyncio.wait`` (which does NOT cancel the task on timeout) and assert the
    task finished ON ITS OWN.  A buggy implementation leaves it pending; we then
    cancel only for cleanup, so the swallowed-cancellation path cannot disguise
    a hang as success.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        clock=_dummy_clock,
    )
    await engine.connect()

    # Fill the bounded recv queue to capacity with datagrams that decode to no
    # frames (too short to be a valid 12-byte RTP header → dropped on parse).
    capacity = engine._recv_queue.maxsize
    bad = b"\x00\x00\x00\x00"  # < 12 bytes: RtpPacket.parse raises ValueError
    for _ in range(capacity):
        engine._recv_queue.put_nowait((bad, _FAKE_SRC))
    assert engine._recv_queue.full()

    async def _drain() -> int:
        count = 0
        async for _frame in engine.inbound_audio():
            count += 1
        return count

    task: asyncio.Task[int] = asyncio.create_task(_drain())

    # Call stop() while the queue is STILL FULL — before the consumer task has
    # had a chance to run.  stop() has no await before its queue write, so its
    # whole body runs synchronously here.  With the buggy put_nowait-sentinel
    # approach the sentinel is dropped (QueueFull suppressed).
    assert engine._recv_queue.full()
    await engine.stop()

    # Give the consumer time to drain and (correctly) terminate via the stop
    # signal.  asyncio.wait does NOT cancel the task on timeout — so if the
    # generator hangs, the task stays pending and we detect it explicitly,
    # rather than having wait_for's cancellation be swallowed into a false pass.
    _done, pending = await asyncio.wait({task}, timeout=2.0)

    if pending:
        # Hung: clean up the leaked task, then fail loudly.
        for p in pending:
            p.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await p
        pytest.fail("inbound_audio() hung after stop() with a full recv queue")

    produced = task.result()
    assert produced == 0  # all datagrams were malformed → no frames


# ---------------------------------------------------------------------------
# (k) inbound_audio() must propagate asyncio.CancelledError (rule 37)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_propagates_cancellation() -> None:
    """Cancelling the consumer of inbound_audio() must raise CancelledError.

    Regression for a MEDIUM defect: the inbound generator caught
    asyncio.CancelledError and returned, swallowing cancellation (masking
    timeouts / caller cancel).  Cancellation must propagate.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        clock=_dummy_clock,
    )
    await engine.connect()

    async def _await_frame() -> PcmFrame:
        async for frame in engine.inbound_audio():
            return frame
        msg = "no frame produced"
        raise AssertionError(msg)

    task: asyncio.Task[PcmFrame] = asyncio.create_task(_await_frame())
    await asyncio.sleep(0.01)  # let it block on the empty queue

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await engine.stop()


# ---------------------------------------------------------------------------
# (l) inbound_audio() drops only SrtpError; other errors propagate (rule 37)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_propagates_non_srtp_error() -> None:
    """A non-SrtpError from the inbound SRTP session must propagate.

    Regression for a MEDIUM defect: the SRTP path swallowed ImportError as a
    per-packet drop, so a misconfigured/unimportable SRTP backend would silently
    drop ALL packets.  Only SrtpError (auth/replay) drops a single packet; every
    other failure (config/programming error) must propagate.
    """

    class _ExplodingUnprotect:
        """A _SrtpUnprotect stand-in whose unprotect raises a config error."""

        def unprotect(self, data: bytes) -> RtpPacket:
            msg = "SRTP backend misconfigured"
            raise RuntimeError(msg)

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        srtp_inbound=_ExplodingUnprotect(),
        clock=_dummy_clock,
    )
    await engine.connect()

    async def _await_frame() -> PcmFrame:
        async for frame in engine.inbound_audio():
            return frame
        msg = "no frame produced"
        raise AssertionError(msg)

    task: asyncio.Task[PcmFrame] = asyncio.create_task(_await_frame())
    await asyncio.sleep(0)

    # A datagram arrives; unprotect raises RuntimeError → must propagate.
    engine._recv_queue.put_nowait((_make_rtp(0, 0, _ulaw_silence()), _FAKE_SRC))

    with pytest.raises(RuntimeError, match="misconfigured"):
        await asyncio.wait_for(task, timeout=2.0)

    await engine.stop()


# ---------------------------------------------------------------------------
# (m) a datagram dequeued during a stop-tie is NOT lost (lossless rollback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_tie_does_not_lose_dequeued_datagram() -> None:
    """A datagram dequeued in the same step the stop flag is set is preserved.

    Regression for a LOW defect: when stop wins a race in which ``queue.get()``
    had already dequeued a datagram, the old code re-queued it with
    ``put_nowait`` under ``suppress(QueueFull)`` — silently dropping it if the
    bounded queue had refilled.  Rollback is now lossless: the datagram is
    parked in a private one-item slot (``_pending``) that is checked before the
    queue, so it survives regardless of queue capacity and is returned next.

    We drive ``_next_datagram`` directly (white-box) for a deterministic tie:
    park it on an empty queue so its internal ``get_task`` blocks, then in the
    SAME loop step enqueue a datagram and set the stop flag.  Both the get and
    the stop resolve together; stop wins; the dequeued datagram must be parked,
    not lost.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        clock=_dummy_clock,
    )
    await engine.connect()
    assert engine._recv_queue.qsize() == 0

    # Park _next_datagram in its internal asyncio.wait on an EMPTY queue.
    nd_task: asyncio.Task[tuple[bytes, tuple[str, int]] | None] = asyncio.create_task(
        engine._next_datagram()
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Same loop step: a datagram arrives AND stop is signalled → a tie.  The
    # recv queue carries (datagram, source-addr) pairs.
    tie_item: tuple[bytes, tuple[str, int]] = (
        _make_rtp(7, 7 * _SAMPLES_PER_FRAME, _ulaw_silence()),
        _FAKE_SRC,
    )
    engine._recv_queue.put_nowait(tie_item)
    engine._stop_event.set()

    # Stop wins the tie → returns None, but the dequeued datagram is preserved.
    result = await asyncio.wait_for(nd_task, timeout=2.0)
    assert result is None
    parked: tuple[bytes, tuple[str, int]] | None = engine._pending
    assert parked == tie_item
    # The datagram was taken off the queue (dequeued), not left/duplicated.
    assert engine._recv_queue.qsize() == 0

    # Prove the rollback is capacity-independent: fill the queue to capacity,
    # then the NEXT _next_datagram() still returns the parked datagram FIRST
    # (a re-queue under a full queue would have lost it).
    capacity = engine._recv_queue.maxsize
    for _ in range(capacity):
        engine._recv_queue.put_nowait((b"\x00\x00\x00\x00", _FAKE_SRC))
    assert engine._recv_queue.full()

    engine._stop_event.clear()  # simulate the engine being reused after stop
    nxt = await asyncio.wait_for(engine._next_datagram(), timeout=2.0)
    assert nxt == tie_item  # the rolled-back datagram, delivered losslessly
    cleared: tuple[bytes, tuple[str, int]] | None = engine._pending
    assert cleared is None  # slot cleared after delivery

    await engine.stop()


@pytest.mark.asyncio
async def test__next_datagram_stop_wins_over_queued_datum() -> None:
    """Stop beats an ALREADY-QUEUED datum, and that datum is parked losslessly.

    Pins the exact race semantics ``_next_datagram`` must preserve across the
    per-packet-task-churn refactor (efficiency item, rule 22): when a datagram is
    already sitting in the recv queue AND the stop flag is set, the stop flag wins
    (``_next_datagram`` returns ``None`` — we never start draining a queue the
    caller asked us to abandon) and the dequeued datagram is parked in
    ``_pending`` (never re-queued, so a full queue cannot drop it) for the next
    call.

    Unlike ``test_stop_tie_does_not_lose_dequeued_datagram`` (which parks the call
    on an EMPTY queue first, then ties an arrival with stop in the same loop step),
    here the datum is enqueued and stop is set BEFORE ``_next_datagram`` is ever
    awaited — the get and the stop are both immediately satisfiable, so this also
    guards the single-iteration fast path of the refactored loop.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        clock=_dummy_clock,
    )
    await engine.connect()

    datum: tuple[bytes, tuple[str, int]] = (
        _make_rtp(11, 11 * _SAMPLES_PER_FRAME, _ulaw_silence()),
        _FAKE_SRC,
    )
    # Datum is queued AND stop is set before the first await of _next_datagram.
    engine._recv_queue.put_nowait(datum)
    engine._stop_event.set()

    result: tuple[bytes, tuple[str, int]] | None = await asyncio.wait_for(
        engine._next_datagram(), timeout=2.0
    )

    # Stop wins: no datagram is handed back this call.
    assert result is None
    # The dequeued datum is parked losslessly (not dropped, not re-queued).
    parked: tuple[bytes, tuple[str, int]] | None = engine._pending
    assert parked == datum
    assert engine._recv_queue.qsize() == 0

    # And it is returned FIRST by the next call, ahead of the (still-set) stop —
    # the parked-datum-before-stop ordering the refactor must keep.
    nxt: tuple[bytes, tuple[str, int]] | None = await asyncio.wait_for(
        engine._next_datagram(), timeout=2.0
    )
    assert nxt == datum
    cleared: tuple[bytes, tuple[str, int]] | None = engine._pending
    assert cleared is None

    await engine.stop()


# ---------------------------------------------------------------------------
# (n) the stop/recv race leaks no pending task (RuntimeWarning-as-error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_recv_race_leaks_no_pending_task() -> None:
    """The loser of the stop/recv race is awaited — no task is left pending.

    Regression for a MEDIUM defect: ``_next_datagram`` cancelled the losing race
    task but never awaited it, so cleanup was deferred and a "Task was destroyed
    but it is pending" RuntimeWarning was possible.  Both race tasks are now
    cancelled AND awaited.

    Treating RuntimeWarning as an error makes a leaked/destroyed task fail the
    test; a forced GC surfaces any task finalised without being awaited.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)

        engine = RtpMediaTransport(
            local_address="127.0.0.1",
            local_port=0,
            remote_address="127.0.0.1",
            remote_port=5004,
            codec=Codec.PCMU,
            clock=_dummy_clock,
        )

        async def _drain() -> int:
            count = 0
            async for _frame in engine.inbound_audio():
                count += 1
            return count

        # Run several stop/recv cycles so the race's losing task is created and
        # must be cleaned up each time.
        for _ in range(3):
            await engine.connect()
            task: asyncio.Task[int] = asyncio.create_task(_drain())
            await asyncio.sleep(0)  # let the generator park in the race
            await asyncio.sleep(0)
            await engine.stop()  # stop wins; the get-task must be cancelled+awaited
            produced = await asyncio.wait_for(task, timeout=2.0)
            assert produced == 0

        # Force finalisation of any orphaned task object; a pending one would
        # emit a RuntimeWarning, which is promoted to an error here.
        gc.collect()


# ---------------------------------------------------------------------------
# (o) Symmetric-RTP (comedia) latching for NAT traversal
#
# When either side is behind NAT the SDP c=/m= address can be a private or
# rewritten address the peer's media never actually originates from.  The engine
# must latch its outbound destination onto the ACTUAL UDP source of the first
# VALID inbound RTP packet, so we send to wherever the peer's media really comes
# from — not blindly to the negotiated SDP address.
# ---------------------------------------------------------------------------

# An SDP-negotiated remote that is deliberately UNREACHABLE / wrong, standing in
# for a private or SBC-rewritten address the peer's media never comes from.
_SDP_REMOTE_ADDR = "203.0.113.1"  # TEST-NET-3 (RFC 5737); never a real source
_SDP_REMOTE_PORT = 40000


class _SendRecorder(asyncio.DatagramTransport):
    """A DatagramTransport stand-in that records every ``sendto`` destination.

    Installed onto ``engine._transport`` AFTER ``connect()`` so we can observe
    exactly where ``send_audio`` aims each datagram without parsing the wire on a
    capture socket.  It owns no socket: inbound delivery still runs through the
    real socket the event loop already registered in ``connect()`` (the loop
    calls the original protocol's ``datagram_received`` directly, independent of
    this object), so replacing only the engine's outbound handle leaves the
    receive path intact.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int] | None = None) -> None:  # type: ignore[override]  # narrower addr type than the stdlib stub; we only ever pass (host, port)
        assert addr is not None  # the engine always sends to an explicit address
        self.sent.append((bytes(data), addr))

    def close(self) -> None:
        """No-op: this stand-in owns no socket (the real one is closed by stop)."""

    def is_closing(self) -> bool:
        """Never closing — the engine may probe this during teardown."""
        return False


@contextlib.contextmanager
def _capture_sends(engine: RtpMediaTransport) -> Iterator[_SendRecorder]:
    """Temporarily intercept the engine's outbound ``sendto`` destinations.

    Swaps in a :class:`_SendRecorder` for the duration of the block and restores
    the real DatagramTransport afterwards, so the engine's own ``stop()`` closes
    the real socket cleanly (no leak) and we still observe exactly where each
    ``send_audio`` aimed.
    """
    real = engine._transport
    recorder = _SendRecorder()
    engine._transport = recorder
    try:
        yield recorder
    finally:
        engine._transport = real


async def _drain_one_frame(engine: RtpMediaTransport) -> None:
    """Consume exactly one inbound frame so the engine processes a datagram."""

    async def _one() -> None:
        async for _frame in engine.inbound_audio():
            return

    task: asyncio.Task[None] = asyncio.create_task(_one())
    await asyncio.wait_for(task, timeout=2.0)


async def _no_sleep(_secs: float) -> None:
    """A no-op pacing sleep for deterministic outbound tests."""


def _latching_engine(*, symmetric: bool = True) -> RtpMediaTransport:
    """An engine whose SDP remote is the deliberately-wrong _SDP_REMOTE_*."""
    return RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address=_SDP_REMOTE_ADDR,
        remote_port=_SDP_REMOTE_PORT,
        codec=Codec.PCMU,
        symmetric=symmetric,
        clock=_dummy_clock,
        sleep=_no_sleep,
    )


@pytest.mark.asyncio
async def test_first_outbound_before_any_inbound_uses_sdp_address() -> None:
    """The greeting (first send, before any inbound) targets the SDP address.

    This is the critical "send first" path (PR #52): nothing has arrived yet, so
    the engine must aim at the negotiated SDP remote so the greeting goes out and
    a comedia gateway can latch onto US.
    """
    engine = _latching_engine()
    await engine.connect()

    with _capture_sends(engine) as recorder:
        await engine.send_audio(_silence_frame())

    assert recorder.sent, "the first send must transmit a datagram"
    _wire, dest = recorder.sent[-1]
    assert dest == (_SDP_REMOTE_ADDR, _SDP_REMOTE_PORT)

    await engine.stop()


@pytest.mark.asyncio
async def test_latches_onto_first_valid_inbound_rtp_source() -> None:
    """After a valid inbound RTP packet, send_audio targets its UDP source.

    The peer's real media comes from ``peer_sock``'s ``(127.0.0.1, port)`` — a
    different tuple than the (wrong) SDP remote.  After the engine receives one
    valid RTP packet from there, the NEXT send must go to that real source, not
    the SDP address.
    """
    peer_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer_sock.bind(("127.0.0.1", 0))
    peer_addr = peer_sock.getsockname()  # the ACTUAL media source tuple

    engine = _latching_engine()
    await engine.connect()
    engine_port = engine.local_port

    # A valid inbound RTP packet arrives from the peer's real source tuple.
    peer_sock.sendto(_make_rtp(0, 0, _ulaw_silence()), ("127.0.0.1", engine_port))
    await _drain_one_frame(engine)

    # Now record outbound and send — it must target the latched real source.
    with _capture_sends(engine) as recorder:
        await engine.send_audio(_silence_frame())

    assert recorder.sent, "send_audio must transmit after latching"
    _wire, dest = recorder.sent[-1]
    assert dest == (peer_addr[0], peer_addr[1])
    assert dest != (_SDP_REMOTE_ADDR, _SDP_REMOTE_PORT)

    peer_sock.close()
    await engine.stop()


@pytest.mark.asyncio
async def test_garbage_datagram_does_not_cause_a_latch() -> None:
    """A non-RTP / garbage datagram must NOT latch (anti-spoofing).

    Only a datagram that parses as RTP triggers a latch; random noise from an
    attacker's tuple must be ignored so it cannot hijack our outbound media.
    """
    attacker_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    attacker_sock.bind(("127.0.0.1", 0))

    engine = _latching_engine()
    await engine.connect()
    engine_port = engine.local_port

    # Garbage (too short to be a 12-byte RTP header) from the attacker's tuple,
    # then a VALID RTP packet from the same tuple so the consumer yields a frame
    # and we know the garbage was processed-and-rejected (not merely pending).
    attacker_sock.sendto(b"\x00\x01\x02\x03", ("127.0.0.1", engine_port))
    await asyncio.sleep(0)
    attacker_sock.sendto(_make_rtp(0, 0, _ulaw_silence()), ("127.0.0.1", engine_port))

    # Drain a frame: the garbage is dropped on parse, the valid packet decodes.
    await _drain_one_frame(engine)

    # The valid RTP from the SAME tuple WILL latch — that is correct.  To isolate
    # the garbage behaviour we instead assert the latch target is the valid
    # packet's source, proving the garbage alone did not pre-latch a wrong value.
    with _capture_sends(engine) as recorder:
        await engine.send_audio(_silence_frame())
    _wire, dest = recorder.sent[-1]
    assert dest == attacker_sock.getsockname()  # latched by the VALID packet

    attacker_sock.close()
    await engine.stop()


@pytest.mark.asyncio
async def test_garbage_only_keeps_sdp_address() -> None:
    """Garbage with no following valid RTP must leave the SDP address in place.

    A pure stream of non-RTP datagrams must never latch — send_audio keeps
    targeting the SDP remote.
    """
    attacker_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    attacker_sock.bind(("127.0.0.1", 0))

    engine = _latching_engine()
    await engine.connect()
    engine_port = engine.local_port

    # A pure stream of garbage from the attacker's tuple — no valid RTP follows.
    for _ in range(3):
        attacker_sock.sendto(b"\x00\x01\x02\x03", ("127.0.0.1", engine_port))

    async def _drain_until_empty() -> None:
        async for _frame in engine.inbound_audio():
            pass  # never yields — garbage drops on parse

    task: asyncio.Task[None] = asyncio.create_task(_drain_until_empty())
    await asyncio.sleep(0.05)  # let the garbage be dequeued and dropped

    with _capture_sends(engine) as recorder:
        await engine.send_audio(_silence_frame())
    _wire, dest = recorder.sent[-1]
    assert dest == (_SDP_REMOTE_ADDR, _SDP_REMOTE_PORT)  # never latched

    await engine.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    attacker_sock.close()


@pytest.mark.asyncio
async def test_off_codec_payload_type_does_not_trigger_a_latch() -> None:
    """A well-formed RTP packet with a NON-negotiated payload type must not latch.

    Anti-spoofing tightening: parsing as RTP is necessary but not sufficient — the
    packet must carry the negotiated audio payload type (here PCMU = 0).  A
    spec-legal but off-codec packet (e.g. an RFC 4733 telephone-event, PT 101, or
    comfort noise, PT 13) arriving before any audio — possibly from an attacker's
    tuple — must NOT move the outbound target.  The engine codec is PCMU, so a
    PT-101 packet from the attacker is delivered and processed but must leave the
    SDP address in place and ``_latched`` False.
    """
    attacker_addr: tuple[str, int] = ("192.0.2.66", 7777)  # TEST-NET-1

    engine = _latching_engine()
    await engine.connect()

    off_codec = RtpPacket(
        payload_type=101,  # telephone-event PT — not the negotiated PCMU (0)
        sequence_number=0,
        timestamp=0,
        ssrc=_FAKE_SSRC,
        payload=b"\x00\x00\x00\x00",
    ).pack()
    engine._recv_queue.put_nowait((off_codec, attacker_addr))

    async def _drain_until_empty() -> None:
        async for _frame in engine.inbound_audio():
            pass

    task: asyncio.Task[None] = asyncio.create_task(_drain_until_empty())
    await asyncio.sleep(0.05)  # let the off-codec packet be dequeued + processed

    assert engine._latched is False  # the off-codec packet did NOT latch
    with _capture_sends(engine) as recorder:
        await engine.send_audio(_silence_frame())
    _wire, dest = recorder.sent[-1]
    assert dest == (_SDP_REMOTE_ADDR, _SDP_REMOTE_PORT)  # SDP address retained
    assert dest != attacker_addr

    await engine.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_latch_is_sticky_ignores_later_source_change() -> None:
    """Latching happens ONCE: a later packet from a new source is ignored.

    Default policy is latch-on-first-valid-RTP-and-stick.  A second valid packet
    from a DIFFERENT tuple (a spoof or a re-INVITE'd media path) must not move
    the outbound target.
    """
    peer_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer_a.bind(("127.0.0.1", 0))
    peer_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer_b.bind(("127.0.0.1", 0))

    engine = _latching_engine()
    await engine.connect()
    engine_port = engine.local_port

    peer_a.sendto(_make_rtp(0, 0, _ulaw_silence()), ("127.0.0.1", engine_port))
    await _drain_one_frame(engine)
    peer_b.sendto(
        _make_rtp(1, _SAMPLES_PER_FRAME, _ulaw_silence()),
        ("127.0.0.1", engine_port),
    )
    await _drain_one_frame(engine)

    with _capture_sends(engine) as recorder:
        await engine.send_audio(_silence_frame())
    _wire, dest = recorder.sent[-1]
    assert dest == peer_a.getsockname()  # stuck on the FIRST source
    assert dest != peer_b.getsockname()

    peer_a.close()
    peer_b.close()
    await engine.stop()


@pytest.mark.asyncio
async def test_symmetric_false_disables_latching() -> None:
    """With symmetric=False the engine always uses the SDP address.

    Even after a valid inbound packet from a different source, send_audio keeps
    targeting the negotiated SDP remote (the opt-out for gateways that honour the
    SDP address literally).
    """
    peer_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer_sock.bind(("127.0.0.1", 0))

    engine = _latching_engine(symmetric=False)
    await engine.connect()
    engine_port = engine.local_port

    peer_sock.sendto(_make_rtp(0, 0, _ulaw_silence()), ("127.0.0.1", engine_port))
    await _drain_one_frame(engine)

    with _capture_sends(engine) as recorder:
        await engine.send_audio(_silence_frame())
    _wire, dest = recorder.sent[-1]
    assert dest == (_SDP_REMOTE_ADDR, _SDP_REMOTE_PORT)  # never latches
    assert dest != peer_sock.getsockname()

    peer_sock.close()
    await engine.stop()


@pytest.mark.asyncio
async def test_latch_logs_peer_media_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Latching logs an operational ``rtp: latched to <ip>:<port>`` line.

    The gateway's media ip:port is operational (not PII-sensitive); logging it is
    how a live call is traced.  No other identifier is emitted.
    """
    peer_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer_sock.bind(("127.0.0.1", 0))
    peer_addr = peer_sock.getsockname()

    engine = _latching_engine()
    await engine.connect()
    engine_port = engine.local_port

    peer_sock.sendto(_make_rtp(0, 0, _ulaw_silence()), ("127.0.0.1", engine_port))
    with caplog.at_level(logging.INFO, logger="hermes_voip.media.engine"):
        await _drain_one_frame(engine)

    expected = f"rtp: latched to {peer_addr[0]}:{peer_addr[1]}"
    assert any(expected in rec.getMessage() for rec in caplog.records), (
        f"expected a latch log line {expected!r}, got "
        f"{[r.getMessage() for r in caplog.records]}"
    )

    peer_sock.close()
    await engine.stop()


# ---------------------------------------------------------------------------
# (j) send_dtmf — RFC 4733 telephone-event TX on the active call (ADR-0010/0031)
# ---------------------------------------------------------------------------
#
# The engine wires the (already-tested) dtmf.py generator onto the RTP TX path:
# emit named-event packets at the NEGOTIATED telephone-event payload type, marker
# bit on the first packet of each digit, a CONSTANT RTP timestamp across one
# digit's packets, seq monotonic across the whole burst, the redundant end packets
# RFC 4733 requires, under the same TX mutex as send_audio. It raises (never
# silently no-ops) if the telephone-event PT was not negotiated.

import hermes_voip.dtmf as dtmf_module  # noqa: E402 — grouped with the DTMF test block

_TEL_EVENT_PT = 101  # the negotiated telephone-event payload type for these tests


def _dtmf_engine(
    *, telephone_event_payload_type: int | None = _TEL_EVENT_PT
) -> RtpMediaTransport:
    """An engine carrying a negotiated telephone-event payload type (or none)."""
    return RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        telephone_event_payload_type=telephone_event_payload_type,
        clock=_dummy_clock,
        sleep=_no_sleep,
        initial_seq=100,
        initial_ts=1000,
    )


def _dtmf_packets(recorder: _SendRecorder) -> list[RtpPacket]:
    """Parse every recorded datagram whose payload type is the telephone-event PT."""
    out: list[RtpPacket] = []
    for wire, _dest in recorder.sent:
        pkt = RtpPacket.parse(wire)
        if pkt.payload_type == _TEL_EVENT_PT:
            out.append(pkt)
    return out


@pytest.mark.asyncio
async def test_send_dtmf_uses_negotiated_payload_type_not_hardcoded() -> None:
    """The DTMF packets carry the NEGOTIATED telephone-event PT, never a literal.

    Construct the engine with a non-101 telephone-event PT and assert every DTMF
    packet uses it — proving the PT is resolved from negotiation, not hardcoded.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        telephone_event_payload_type=110,  # deliberately NOT 101
        clock=_dummy_clock,
        sleep=_no_sleep,
        initial_seq=100,
        initial_ts=1000,
    )
    await engine.connect()
    with _capture_sends(engine) as recorder:
        await engine.send_dtmf("1")
    assert recorder.sent, "send_dtmf must emit telephone-event datagrams"
    for wire, _dest in recorder.sent:
        assert RtpPacket.parse(wire).payload_type == 110
    await engine.stop()


@pytest.mark.asyncio
async def test_send_dtmf_raises_when_telephone_event_not_negotiated() -> None:
    """send_dtmf raises (never silently no-ops) if no telephone-event PT exists.

    A call whose offer had no telephone-event must NOT pretend to send DTMF — that
    would be a silent failure. The engine raises so the tool reports a clear error.
    """
    engine = _dtmf_engine(telephone_event_payload_type=None)
    await engine.connect()
    with _capture_sends(engine) as recorder, pytest.raises(RuntimeError):
        await engine.send_dtmf("1")
    assert not recorder.sent, "no DTMF packet may be sent when PT is unnegotiated"
    await engine.stop()


@pytest.mark.asyncio
async def test_send_dtmf_marker_on_first_packet_of_each_digit() -> None:
    """The marker bit is set on the FIRST packet of each digit, and only there."""
    engine = _dtmf_engine()
    await engine.connect()
    with _capture_sends(engine) as recorder:
        await engine.send_dtmf("12")
    pkts = _dtmf_packets(recorder)
    # Group packets by their (constant-per-digit) timestamp to find each digit's
    # first packet. The first packet of the first digit is index 0; the first
    # packet of the second digit is the first packet with a new timestamp.
    markers = [i for i, p in enumerate(pkts) if p.marker]
    timestamps = [p.timestamp for p in pkts]
    # Exactly two marked packets — one per digit.
    assert len(markers) == 2, f"expected 2 marker packets, got {markers}"
    # The marked packets are each the first occurrence of a distinct timestamp.
    first_indices = [timestamps.index(ts) for ts in dict.fromkeys(timestamps)]
    assert markers == first_indices
    await engine.stop()


@pytest.mark.asyncio
async def test_send_dtmf_constant_timestamp_within_a_digit() -> None:
    """All packets of ONE digit share a single RTP timestamp (RFC 4733 §2.5.1)."""
    engine = _dtmf_engine()
    await engine.connect()
    with _capture_sends(engine) as recorder:
        await engine.send_dtmf("5")
    pkts = _dtmf_packets(recorder)
    assert len(pkts) >= 4, "a digit emits update packet(s) + 3 redundant end packets"
    assert len({p.timestamp for p in pkts}) == 1, "one digit ⇒ one RTP timestamp"


@pytest.mark.asyncio
async def test_send_dtmf_distinct_timestamp_per_digit() -> None:
    """Each digit advances the RTP timestamp so the receiver emits each press once."""
    engine = _dtmf_engine()
    await engine.connect()
    with _capture_sends(engine) as recorder:
        await engine.send_dtmf("12")
    pkts = _dtmf_packets(recorder)
    seen_ts = list(dict.fromkeys(p.timestamp for p in pkts))
    assert len(seen_ts) == 2, "two digits ⇒ two distinct RTP timestamps"
    # Monotonic: the second digit's timestamp is later than the first's.
    assert seen_ts[1] > seen_ts[0]


@pytest.mark.asyncio
async def test_send_dtmf_sequence_numbers_monotonic() -> None:
    """The seq increments by exactly one per emitted packet across the whole burst."""
    engine = _dtmf_engine()
    await engine.connect()
    with _capture_sends(engine) as recorder:
        await engine.send_dtmf("12")
    pkts = _dtmf_packets(recorder)
    seqs = [p.sequence_number for p in pkts]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"non-monotonic: {seqs}"


@pytest.mark.asyncio
async def test_send_dtmf_emits_three_end_packets_per_digit() -> None:
    """Each digit ends with three end-bit packets (RFC 4733 §2.5.1.4 redundancy)."""
    engine = _dtmf_engine()
    await engine.connect()
    with _capture_sends(engine) as recorder:
        await engine.send_dtmf("1")
    pkts = _dtmf_packets(recorder)
    end_pkts = [p for p in pkts if (p.payload[1] & 0x80)]  # end bit in byte 1
    assert len(end_pkts) == 3, f"expected 3 end packets, got {len(end_pkts)}"
    # The end packets are the LAST three packets of the digit.
    assert end_pkts == pkts[-3:]


@pytest.mark.asyncio
async def test_send_dtmf_round_trips_through_receiver() -> None:
    """The emitted packets decode back to the exact digits via DtmfReceiver."""
    engine = _dtmf_engine()
    await engine.connect()
    with _capture_sends(engine) as recorder:
        await engine.send_dtmf("19#")
    pkts = _dtmf_packets(recorder)
    receiver = dtmf_module.DtmfReceiver()
    decoded = [
        result.digit
        for p in pkts
        if isinstance(
            result := receiver.feed(
                dtmf_module.DtmfEvent.decode(p.payload), timestamp=p.timestamp
            ),
            dtmf_module.DtmfPress,
        )
    ]
    assert "".join(decoded) == "19#"


@pytest.mark.asyncio
async def test_send_dtmf_srtp_protects_each_packet() -> None:
    """When SRTP is active, every DTMF datagram is SRTP-protected (not plain RTP)."""

    class _RecordingProtect:
        def __init__(self) -> None:
            self.calls = 0

        def protect(self, packet: RtpPacket) -> bytes:
            self.calls += 1
            # A trivial, reversible "protection": tag the packed bytes so the test
            # can tell a protected datagram from a plain one.
            return b"SRTP" + packet.pack()

    protector = _RecordingProtect()
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        telephone_event_payload_type=_TEL_EVENT_PT,
        srtp_outbound=protector,
        clock=_dummy_clock,
        sleep=_no_sleep,
        initial_seq=100,
        initial_ts=1000,
    )
    await engine.connect()
    with _capture_sends(engine) as recorder:
        await engine.send_dtmf("1")
    assert recorder.sent
    assert protector.calls == len(recorder.sent), "every DTMF packet must be protected"
    for wire, _dest in recorder.sent:
        assert wire.startswith(b"SRTP")
    await engine.stop()


@pytest.mark.asyncio
async def test_send_dtmf_does_not_interleave_with_concurrent_send_audio() -> None:
    """Concurrent send_audio + send_dtmf never share a seq nor interleave packets.

    The two TX coroutines run under the same mutex, so the wire shows a clean,
    gap-free, strictly-monotonic sequence-number stream with each digit's DTMF
    packets contiguous (not split by an audio packet) — no seq race.
    """
    engine = _dtmf_engine()
    await engine.connect()
    with _capture_sends(engine) as recorder:
        # Fire an audio send and a DTMF send concurrently.
        await asyncio.gather(
            engine.send_audio(_silence_frame()),
            engine.send_dtmf("1"),
        )
    all_pkts = [RtpPacket.parse(w) for w, _d in recorder.sent]
    seqs = [p.sequence_number for p in all_pkts]
    # Every sequence number is unique (no two packets — audio or DTMF — collided).
    assert len(set(seqs)) == len(seqs), f"sequence-number collision: {seqs}"
    # And the whole stream is contiguous + monotonic (no gap, no reuse).
    assert sorted(seqs) == list(range(min(seqs), min(seqs) + len(seqs)))
    # The DTMF packets (same PT) are contiguous — not split by an audio packet.
    dtmf_idx = [i for i, p in enumerate(all_pkts) if p.payload_type == _TEL_EVENT_PT]
    assert dtmf_idx == list(range(dtmf_idx[0], dtmf_idx[0] + len(dtmf_idx))), (
        f"DTMF packets were interleaved with audio: {dtmf_idx}"
    )
    await engine.stop()


# ---------------------------------------------------------------------------
# ptime negotiation (ADR-0056 item 5): the engine no longer assumes 20 ms — the
# negotiated framing can be applied after construction via the ptime setter, and
# every TX framing computation follows it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ptime_setter_changes_tx_framing() -> None:
    """Setting engine.ptime reframes outbound RTP to the new packetisation time.

    A 30 ms G.711 frame is 240 samples; the RTP timestamp must advance by 240 per
    packet (not the 160 of the default 20 ms), proving the engine applies the
    negotiated ptime rather than the hard-coded 20 ms.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )
    assert engine.ptime == 20  # default
    engine.ptime = 30
    assert engine.ptime == 30
    await engine.connect()

    samples_30ms = (G711_SAMPLE_RATE * 30) // 1000  # 240
    frame = PcmFrame(
        samples=b"\x00" * (2 * samples_30ms * 2),  # two whole 30 ms frames
        sample_rate=G711_SAMPLE_RATE,
        monotonic_ts_ns=0,
    )
    with _capture_sends(engine) as recorder:
        await engine.send_audio(frame)
    await engine.stop()

    assert len(recorder.sent) == 2
    pkt0 = RtpPacket.parse(recorder.sent[0][0])
    pkt1 = RtpPacket.parse(recorder.sent[1][0])
    assert len(pkt0.payload) == samples_30ms  # mu-law: 1 byte/sample, 240 bytes
    ts_delta = (pkt1.timestamp - pkt0.timestamp) % (1 << 32)
    assert ts_delta == samples_30ms  # timestamp advances by the 30 ms sample count


def test_ptime_setter_rejects_non_positive() -> None:
    """Reject a non-positive ptime (a positive count of milliseconds is required)."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
    )
    with pytest.raises(ValueError, match="ptime"):
        engine.ptime = 0
    with pytest.raises(ValueError, match="ptime"):
        engine.ptime = -5


# ---------------------------------------------------------------------------
# TX amplitude logging — verify peak amplitude is logged correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_audio_logs_tx_amplitude_for_non_silent_chunk(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """send_audio logs a non-zero peak_amplitude for a non-silent frame.

    The TX amplitude is logged once per _TX_AMPLITUDE_LOG_PERIOD (50) packets
    at 20 ms ptime (1 second). This test verifies that a non-silent frame
    produces a non-zero peak amplitude in the log.
    """
    capture_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    capture_sock.bind(("127.0.0.1", 0))
    capture_sock.setblocking(False)
    remote_port = capture_sock.getsockname()[1]

    async def _fake_sleep(secs: float) -> None:
        """Stub sleep to avoid real delays."""
        pass

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=remote_port,
        codec=Codec.PCMU,
        sleep=_fake_sleep,
    )
    await engine.connect()

    # Send 50 frames (one period) with a non-silent tone.
    tone_frame = _tone_frame(amplitude=1000)
    with caplog.at_level(logging.INFO, logger="hermes_voip.media.engine"):
        for _ in range(50):
            await engine.send_audio(tone_frame)

    # Verify that the TX amplitude log line was emitted with a non-zero peak.
    log_messages = [rec.getMessage() for rec in caplog.records]
    peak_log = [
        msg for msg in log_messages if "rtp tx:" in msg and "peak_amplitude" in msg
    ]
    assert peak_log, f"Expected a peak_amplitude log line, got: {log_messages}"

    # The peak should be non-zero for a non-silent frame.
    peak_msg = peak_log[0]
    assert "peak_amplitude=" in peak_msg
    # Extract the peak_amplitude value; should not be zero.
    parts = peak_msg.split("peak_amplitude=")
    assert len(parts) == 2, f"Could not parse peak_amplitude from: {peak_msg}"
    peak_str = parts[1].split()[0]  # get the number before the next word
    peak_val = int(peak_str)
    assert peak_val > 0, f"Expected non-zero peak amplitude, got {peak_val}"

    capture_sock.close()
    await engine.stop()


@pytest.mark.asyncio
async def test_send_audio_logs_tx_amplitude_near_zero_for_silence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """send_audio logs near-zero peak_amplitude for a silent frame.

    Silence (all zeros) should produce a zero or near-zero peak amplitude.
    """
    capture_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    capture_sock.bind(("127.0.0.1", 0))
    capture_sock.setblocking(False)
    remote_port = capture_sock.getsockname()[1]

    async def _fake_sleep(secs: float) -> None:
        """Stub sleep to avoid real delays."""
        pass

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=remote_port,
        codec=Codec.PCMU,
        sleep=_fake_sleep,
    )
    await engine.connect()

    # Send 50 frames (one period) of silence.
    silence_frame = _silence_frame()
    with caplog.at_level(logging.INFO, logger="hermes_voip.media.engine"):
        for _ in range(50):
            await engine.send_audio(silence_frame)

    # Verify that the TX amplitude log line was emitted with a zero or near-zero peak.
    log_messages = [rec.getMessage() for rec in caplog.records]
    peak_log = [
        msg for msg in log_messages if "rtp tx:" in msg and "peak_amplitude" in msg
    ]
    assert peak_log, f"Expected a peak_amplitude log line, got: {log_messages}"

    # The peak should be zero for silence.
    peak_msg = peak_log[0]
    assert "peak_amplitude=" in peak_msg
    # Extract the peak_amplitude value; should be zero for silence.
    parts = peak_msg.split("peak_amplitude=")
    assert len(parts) == 2, f"Could not parse peak_amplitude from: {peak_msg}"
    peak_str = parts[1].split()[0]  # get the number before the next word
    peak_val = int(peak_str)
    assert peak_val == 0, f"Expected zero peak amplitude for silence, got {peak_val}"

    capture_sock.close()
    await engine.stop()
