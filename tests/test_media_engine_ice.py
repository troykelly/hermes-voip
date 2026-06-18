"""RtpMediaTransport over the ICE datagram pipe (the WebRTC socket seam, ADR-0032).

Deterministic, no real network. When an ``ice_transport`` is supplied, the engine
does NOT bind a UDP socket: it sends SRTP/RTP via ``ice.send`` and receives via a
background reader that ``await ice.recv()``s, applies the RFC 7983 first-byte demux
(only SRTP — first byte 128-191 — reaches the engine; the DTLS handshake is already
done, so any residual DTLS/STUN bytes are dropped), and feeds the same inbound
queue. The whole SRTP/jitter/decode/pacing machinery is reused verbatim — only the
datagram I/O is swapped. The TLS path (no ``ice_transport``) is untouched.

These tests use a pure in-memory fake ICE pipe (no aioice needed), so they run in
the DEFAULT gate against a plain-RTP (no-SRTP) PCMU engine.
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.rtp import RtpPacket

_PCMU_PT = 0
_SAMPLE_RATE = 8_000
_PTIME_MS = 20
_SAMPLES_PER_FRAME = (_SAMPLE_RATE * _PTIME_MS) // 1000  # 160


class _FakeIcePipe:
    """In-memory bidirectional ICE pipe stand-in (the engine's send/recv seam).

    ``send`` appends to ``sent`` (the wire bytes the engine emitted). ``recv``
    yields datagrams pushed via :meth:`push_inbound`, then blocks. ``close`` marks
    the pipe closed so a blocked ``recv`` raises (mirrors aioice on teardown).
    """

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False

    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def recv(self) -> bytes:
        if self.closed:
            msg = "ICE pipe closed"
            raise ConnectionError(msg)
        return await self._inbound.get()

    async def close(self) -> None:
        self.closed = True

    def push_inbound(self, data: bytes) -> None:
        self._inbound.put_nowait(data)


async def _noop_sleep(_seconds: float) -> None:
    return None


def _engine(pipe: _FakeIcePipe) -> RtpMediaTransport:
    return RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=40_000,
        codec=Codec.PCMU,
        payload_type=_PCMU_PT,
        ptime=_PTIME_MS,
        sleep=_noop_sleep,
        initial_seq=10,
        initial_ts=0,
        ice_transport=pipe,
    )


def _pcm_frame(n_samples: int) -> PcmFrame:
    # A constant non-zero PCM16 frame (silence would still encode, but this is clearer).
    return PcmFrame(
        samples=(b"\x10\x00" * n_samples),
        sample_rate=_SAMPLE_RATE,
        monotonic_ts_ns=0,
    )


@pytest.mark.asyncio
async def test_connect_with_ice_does_not_bind_socket() -> None:
    """With an ICE pipe, connect() opens no UDP socket (local_port stays 0)."""
    pipe = _FakeIcePipe()
    engine = _engine(pipe)
    await engine.connect()
    try:
        # No OS port was assigned because no socket was bound.
        assert engine.local_port == 0
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_send_audio_emits_over_ice_pipe() -> None:
    """send_audio routes RTP through ice.send, not a UDP socket."""
    pipe = _FakeIcePipe()
    engine = _engine(pipe)
    await engine.connect()
    await engine.send_audio(_pcm_frame(_SAMPLES_PER_FRAME))
    # Give the fire-and-forget ICE send task a turn to run.
    await asyncio.sleep(0)
    await engine.stop()

    assert len(pipe.sent) >= 1
    pkt = RtpPacket.parse(pipe.sent[0])
    assert pkt.payload_type == _PCMU_PT


@pytest.mark.asyncio
async def test_inbound_srtp_byte_range_is_delivered() -> None:
    """An inbound RTP datagram (first byte 128-191) reaches inbound_audio()."""
    pipe = _FakeIcePipe()
    engine = _engine(pipe)
    await engine.connect()

    # RTP version-2 header → first byte 0x80 (128), in the SRTP demux range.
    rtp = RtpPacket(
        payload_type=_PCMU_PT,
        sequence_number=1,
        timestamp=0,
        ssrc=0x0BAD_F00D,  # not our outbound SSRC
        payload=b"\x7f" * _SAMPLES_PER_FRAME,
    ).pack()
    assert rtp[0] == 0x80  # demux: SRTP/RTP range
    pipe.push_inbound(rtp)

    try:
        frame = await asyncio.wait_for(anext(engine.inbound_audio()), timeout=2.0)
    finally:
        await engine.stop()
    assert frame.sample_rate == _SAMPLE_RATE
    assert len(frame.samples) == _SAMPLES_PER_FRAME * 2


@pytest.mark.asyncio
async def test_inbound_dtls_and_stun_bytes_are_dropped() -> None:
    """Residual DTLS (20-63) / STUN (0-3) datagrams are demuxed out, not decoded.

    After the handshake completes only SRTP should arrive, but a late DTLS record
    or STUN consent packet must never be fed to the RTP decoder (it would be
    garbage). The engine drops anything outside the SRTP first-byte range.
    """
    pipe = _FakeIcePipe()
    engine = _engine(pipe)
    await engine.connect()

    # A DTLS record (first byte 23 = application-data / 0x16 handshake both in 20-63)
    pipe.push_inbound(b"\x16\x01\x02\x03dtls-bytes")
    # A STUN binding (first byte 0x00, range 0-3)
    pipe.push_inbound(b"\x00\x01\x00\x00stun-bytes")
    # Then a real RTP packet that MUST come through.
    rtp = RtpPacket(
        payload_type=_PCMU_PT,
        sequence_number=2,
        timestamp=0,
        ssrc=0x0BAD_F00D,
        payload=b"\x7f" * _SAMPLES_PER_FRAME,
    ).pack()
    pipe.push_inbound(rtp)

    try:
        frame = await asyncio.wait_for(anext(engine.inbound_audio()), timeout=2.0)
    finally:
        await engine.stop()
    # The first frame delivered is from the RTP packet — the DTLS/STUN bytes were
    # silently dropped, never decoded into a (garbage) frame.
    assert len(frame.samples) == _SAMPLES_PER_FRAME * 2


@pytest.mark.asyncio
async def test_stop_closes_the_ice_pipe() -> None:
    """stop() closes the injected ICE pipe (releases aioice's sockets)."""
    pipe = _FakeIcePipe()
    engine = _engine(pipe)
    await engine.connect()
    await engine.stop()
    assert pipe.closed is True


@pytest.mark.asyncio
async def test_ice_pipe_close_mid_call_tears_down_as_transport_loss() -> None:
    """An ICE pipe that closes mid-call ends the call (consent-loss teardown).

    This is the gap-analysis #6 chain end-to-end: aioice runs RFC 7675 consent and
    close()s the ICE connection on consent loss; that close() makes a blocked
    ``ice.recv()`` raise; the engine's ICE reader catches it and marks the call a
    transport loss (``media_timed_out``) + sets the stop event so the call loop
    exits. No media-inactivity timeout is needed — the teardown is driven by ICE
    consent, independent of whether media was flowing.
    """
    pipe = _FakeIcePipe()
    engine = _engine(pipe)
    await engine.connect()
    try:
        # The reader is parked in ice.recv(). Simulate aioice's consent-driven
        # close(): the pipe closes, so the parked recv() raises ConnectionError.
        assert engine.media_timed_out is False
        await pipe.close()

        # The reader task must observe the close and flip the teardown state.
        async def _await_teardown() -> None:
            while not engine.media_timed_out:
                await asyncio.sleep(0)

        await asyncio.wait_for(_await_teardown(), timeout=2.0)
        assert engine.media_timed_out is True
    finally:
        await engine.stop()
