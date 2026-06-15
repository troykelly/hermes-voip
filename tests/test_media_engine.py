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
import socket

import pytest

from hermes_voip.media.audio import G711_SAMPLE_RATE, encode_ulaw
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.transport import MediaTransport
from hermes_voip.rtp import RtpPacket

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_SSRC = 0xDEADBEEF
_FAKE_KEY_B64 = base64.b64encode(b"\xab" * 16 + b"\xcd" * 14).decode()

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
async def test_inbound_reordered_packets_come_out_in_order() -> None:
    """Out-of-order inbound packets are reordered by the JitterBuffer."""
    sender_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender_sock.bind(("127.0.0.1", 0))

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=sender_sock.getsockname()[1],
        codec=Codec.PCMU,
        jitter_depth=3,  # wait for 3 later packets before declaring loss
        clock=_dummy_clock,
    )
    await engine.connect()
    engine_port = engine.local_port

    payload_a = encode_ulaw(b"\x01" * (_SAMPLES_PER_FRAME * 2))
    payload_b = encode_ulaw(b"\x02" * (_SAMPLES_PER_FRAME * 2))
    payload_c = encode_ulaw(b"\x03" * (_SAMPLES_PER_FRAME * 2))

    # Send out-of-order: seq 1 first, then 0, then 2.
    sender_sock.sendto(
        _make_rtp(1, _SAMPLES_PER_FRAME, payload_b), ("127.0.0.1", engine_port)
    )
    await asyncio.sleep(0.005)
    sender_sock.sendto(_make_rtp(0, 0, payload_a), ("127.0.0.1", engine_port))
    await asyncio.sleep(0.005)
    sender_sock.sendto(
        _make_rtp(2, _SAMPLES_PER_FRAME * 2, payload_c), ("127.0.0.1", engine_port)
    )
    await asyncio.sleep(0.005)

    frames: list[PcmFrame] = []

    async def _collect() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)
            if len(frames) == 3:
                break

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.1)
    await asyncio.wait_for(task, timeout=2.0)

    # seq 0 (payload_a=\x01 decoded) must come out first.
    assert len(frames) == 3
    assert frames[0].samples == b"\x01" * (_SAMPLES_PER_FRAME * 2)
    assert frames[1].samples == b"\x02" * (_SAMPLES_PER_FRAME * 2)
    assert frames[2].samples == b"\x03" * (_SAMPLES_PER_FRAME * 2)

    sender_sock.close()
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
    with contextlib.suppress(BlockingIOError):
        while True:
            data, _ = capture_sock.recvfrom(4096)
            received.append(data)

    assert received == [], f"expected no datagrams during hold, got {len(received)}"

    # Unhold — subsequent sends should reach the wire again.
    await engine.set_hold(False)
    assert engine.on_hold is False
    await engine.send_audio(frame)
    await asyncio.sleep(0.01)

    after: list[bytes] = []
    with contextlib.suppress(BlockingIOError):
        while True:
            data, _ = capture_sock.recvfrom(4096)
            after.append(data)

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
    gen = engine.inbound_audio()
    recv_task = asyncio.create_task(gen.__anext__())
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

    srtp_out = SrtpSession(crypto, ssrc=_FAKE_SSRC)
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
