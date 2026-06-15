"""Asyncio UDP media plane — RtpMediaTransport (ADR-0005).

This module wires the sans-IO building blocks (RtpPacket/JitterBuffer, G.711
codec, SrtpSession) onto a real non-blocking UDP socket to produce the concrete
:class:`MediaTransport` / :class:`CallMedia` implementation for telephony calls.

Architecture:

* One ``asyncio`` DatagramTransport receives datagrams from the event loop and
  places them on an internal ``asyncio.Queue``.
* :meth:`inbound_audio` drains the queue, optionally un-protects via SRTP, feeds
  the :class:`~hermes_voip.rtp.JitterBuffer`, and decodes each ordered packet to
  a :class:`~hermes_voip.providers.audio.PcmFrame`.
* :meth:`send_audio` encodes the outbound frame, packs it into an RTP datagram,
  optionally protects it via SRTP, and sends it to the remote address.  A
  configurable ``sleep`` callable paces the outbound stream at ``ptime`` ms per
  frame (injectable so tests drive time without wall-clock delays).
* A ``clock`` callable (also injectable) stamps inbound ``PcmFrame`` objects with
  a monotonic nanosecond timestamp — downstream stages (VAD, STT) rely on this
  for gap-free presentation time.

**Plain RTP vs SRTP**: passing ``srtp_outbound`` / ``srtp_inbound``
:class:`_SrtpProtect` / :class:`_SrtpUnprotect` objects enables SRTP/SAVP;
omitting them (``None``) gives plain RTP/AVP.  Tampered or unauthenticated SRTP
packets are silently dropped — their ``SrtpError`` is logged at DEBUG level and
the event loop continues.  (A bad *incoming* packet is an environmental event,
not a programming error; dropping it is the correct behaviour here.)

**Timing seam**: ``clock`` defaults to :func:`time.monotonic_ns` and ``sleep``
defaults to :func:`asyncio.sleep`.  Both are injected by tests.  No code path
calls the real clock in a way that makes determinism impossible.

**SRTP type seam**: ``cryptography`` lives in the optional ``media`` extra and is
absent from the default mypy gate.  Rather than use ``Any`` or cast, we declare
narrow local Protocols (:class:`_SrtpProtect`, :class:`_SrtpUnprotect`) covering
only the methods the engine calls.  :class:`~hermes_voip.media.srtp.SrtpSession`
is structurally assignable to both without import — clean in both gate
environments with zero ``# type: ignore``.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from hermes_voip.media.audio import (
    G711_SAMPLE_RATE,
    alaw_to_frame,
    frame_to_alaw,
    frame_to_ulaw,
    ulaw_to_frame,
)
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.rtp import JitterBuffer, Lost, RtpPacket

__all__ = ["Codec", "RtpMediaTransport"]

_log = logging.getLogger(__name__)

# Default ptime in milliseconds (one packet per 20 ms = 50 pps).
_DEFAULT_PTIME_MS = 20

# Starting RTP sequence number and timestamp for outbound streams.
_INITIAL_SEQ = 0
_INITIAL_TS = 0

# A fixed SSRC for the outbound stream — an obvious test fake.
# (No real PBX should assign 0xCAFEBABE; the repo is public.)
_OUTBOUND_SSRC: int = 0xCAFEBABE

# Size of the inbound datagram queue (datagrams; 512 * ~180 bytes ~ 90 kB).
_QUEUE_MAXSIZE = 512


class Codec(enum.Enum):
    """The G.711 codec variant for this media session."""

    PCMU = 0  # mu-law, RTP payload type 0
    PCMA = 8  # a-law, RTP payload type 8


# ---------------------------------------------------------------------------
# Narrow SRTP Protocol seam (no cryptography import needed at type-check time)
# ---------------------------------------------------------------------------


class _SrtpProtect(Protocol):
    """The protect (outbound encrypt) method surface of SrtpSession."""

    def protect(self, packet: RtpPacket) -> bytes:
        """Encrypt and authenticate an outbound RTP packet."""
        ...


class _SrtpUnprotect(Protocol):
    """The unprotect (inbound decrypt) method surface of SrtpSession."""

    def unprotect(self, data: bytes) -> RtpPacket:
        """Authenticate and decrypt an inbound SRTP packet."""
        ...


# ---------------------------------------------------------------------------
# asyncio DatagramProtocol — receives inbound datagrams into a queue.
# ---------------------------------------------------------------------------


class _UdpReceiver(asyncio.DatagramProtocol):
    """DatagramProtocol that enqueues each received datagram.

    The engine creates one instance and passes it to
    ``loop.create_datagram_endpoint``.  The queue is drained by
    :meth:`RtpMediaTransport.inbound_audio`.
    """

    def __init__(self, queue: asyncio.Queue[bytes]) -> None:
        self._queue = queue
        self._transport: asyncio.BaseTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Store the transport so the engine can send datagrams."""
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Place the datagram on the queue (non-blocking; drops on overflow)."""
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(data)
        if self._queue.full():
            _log.debug("inbound queue full — datagram dropped from %s", addr)

    def error_received(self, exc: Exception) -> None:
        """Log ICMP / socket errors; do not propagate (env error, rule 37)."""
        _log.debug("UDP error received: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        """The socket was closed."""
        if exc is not None:
            _log.debug("UDP connection lost: %s", exc)


# ---------------------------------------------------------------------------
# RtpMediaTransport
# ---------------------------------------------------------------------------


class RtpMediaTransport:
    """Asyncio UDP media plane: RTP/SRTP send + receive for one telephony call.

    Implements both :class:`~hermes_voip.providers.transport.MediaTransport` and
    :class:`~hermes_voip.call.CallMedia` (the hold-gating + teardown seam).

    Args:
        local_address:   IPv4 address to bind the UDP socket to.
        local_port:      Local UDP port.  Pass ``0`` and read :attr:`local_port`
                         after :meth:`connect` to discover the OS-assigned port.
        remote_address:  IPv4 address to send RTP datagrams to.
        remote_port:     UDP port on the remote (gateway) side.
        codec:           :class:`Codec` (PCMU or PCMA).
        ptime:           Packetisation time in ms (default 20 ms = 50 pps).
        srtp_inbound:    Optional SRTP session for decrypting inbound packets.
                         Must satisfy :class:`_SrtpUnprotect` (i.e. be a
                         :class:`~hermes_voip.media.srtp.SrtpSession`).
                         ``None`` → plain RTP/AVP.
        srtp_outbound:   Optional SRTP session for encrypting outbound packets.
                         Must satisfy :class:`_SrtpProtect`.
                         ``None`` → plain RTP/AVP.
        jitter_depth:    ``target_depth`` parameter for
                         :class:`~hermes_voip.rtp.JitterBuffer`.
        clock:           Callable returning the current monotonic time in ns.
                         Defaults to :func:`time.monotonic_ns`.  Inject in tests.
        sleep:           Async callable for outbound pacing (default
                         :func:`asyncio.sleep`).  Inject a no-op in tests.
    """

    def __init__(  # noqa: PLR0913 — all params required (two network endpoints, codec, SRTP sessions, timing seams)
        self,
        *,
        local_address: str,
        local_port: int,
        remote_address: str,
        remote_port: int,
        codec: Codec,
        ptime: int = _DEFAULT_PTIME_MS,
        srtp_inbound: _SrtpUnprotect | None = None,
        srtp_outbound: _SrtpProtect | None = None,
        jitter_depth: int = 2,
        clock: Callable[[], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Construct the engine; no socket is opened until :meth:`connect`."""
        self._local_address = local_address
        self._local_port = local_port
        self._remote_address = remote_address
        self._remote_port = remote_port
        self._codec = codec
        self._ptime = ptime
        self._srtp_in = srtp_inbound
        self._srtp_out = srtp_outbound
        self._jitter_depth = jitter_depth
        self._clock: Callable[[], int] = (
            clock if clock is not None else time.monotonic_ns
        )
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else asyncio.sleep
        )

        # Outbound RTP sequence / timestamp counters (per-session).
        self._seq: int = _INITIAL_SEQ
        self._ts: int = _INITIAL_TS

        # Hold state.
        self.on_hold: bool = False

        # Socket / asyncio transport state (populated by connect()).
        self._transport: asyncio.DatagramTransport | None = None
        self._recv_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._jitter: JitterBuffer = JitterBuffer(target_depth=jitter_depth)

    # ------------------------------------------------------------------
    # MediaTransport Protocol
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Open a non-blocking UDP socket bound to local_address:local_port.

        Returns:
            ``True`` on success.  Raises on socket / OS error.
        """
        loop = asyncio.get_running_loop()
        # Create a bound UDP socket first so port=0 lets the OS choose.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind((self._local_address, self._local_port))
        # Record the OS-assigned port before handing the socket to asyncio.
        self._local_port = sock.getsockname()[1]

        self._recv_queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._jitter = JitterBuffer(target_depth=self._jitter_depth)
        protocol = _UdpReceiver(self._recv_queue)

        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            sock=sock,
        )
        # create_datagram_endpoint returns (transport, protocol); the transport
        # is always a DatagramTransport when a UDP socket is passed.
        assert isinstance(transport, asyncio.DatagramTransport)  # noqa: S101 — invariant, not a test assertion
        self._transport = transport
        return True

    async def disconnect(self) -> None:
        """Tear down media and signalling; idempotent (MediaTransport seam)."""
        await self.stop()

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        """Far-end audio decoded to PCM16 at :attr:`inbound_sample_rate`.

        Receives datagrams, un-protects SRTP (if configured), feeds the jitter
        buffer, and decodes each ordered packet to a
        :class:`~hermes_voip.providers.audio.PcmFrame`.  Tampered or
        unauthenticated packets are silently dropped.
        :class:`~hermes_voip.rtp.Lost` signals are silently skipped (no PLC
        yet — that is out of scope for this unit).

        Returns:
            An :class:`~collections.abc.AsyncIterator` of decoded
            :class:`~hermes_voip.providers.audio.PcmFrame` objects.
        """
        return self._inbound_gen()

    async def _inbound_gen(self) -> AsyncIterator[PcmFrame]:
        """Internal async generator implementing inbound_audio."""
        while True:
            try:
                data = await self._recv_queue.get()
            except asyncio.CancelledError:
                return

            # Empty sentinel from stop() — exit cleanly.
            if not data:
                return

            rtp_pkt: RtpPacket

            # SRTP un-protect (if configured); drop on auth failure.
            if self._srtp_in is not None:
                try:
                    from hermes_voip.media.srtp import SrtpError  # noqa: PLC0415

                    try:
                        rtp_pkt = self._srtp_in.unprotect(data)
                    except SrtpError as exc:
                        _log.debug(
                            "SRTP auth/replay failure — datagram dropped: %s", exc
                        )
                        continue
                except ImportError:
                    # media extra absent; skip SRTP (should not occur in practice
                    # if srtp_inbound was constructed with the extra installed).
                    continue
            else:
                # Plain RTP: drop malformed datagrams.
                try:
                    rtp_pkt = RtpPacket.parse(data)
                except ValueError as exc:
                    _log.debug("malformed RTP datagram — dropped: %s", exc)
                    continue

            # Feed the jitter buffer.
            self._jitter.push(rtp_pkt)

            # Drain all available ordered output from the jitter buffer.
            while True:
                output = self._jitter.pop()
                if output is None:
                    break
                if isinstance(output, Lost):
                    _log.debug("JitterBuffer: lost packet seq=%d", output.sequence)
                    continue
                # Decode G.711 payload to PcmFrame.
                ts_ns = self._clock()
                frame = self._decode(output.payload, ts_ns)
                yield frame

    async def send_audio(self, frame: PcmFrame) -> None:
        """Encode and packetise one near-end frame; gate on hold state.

        When :attr:`on_hold` is ``True`` the datagram is silently discarded.
        Otherwise the frame is encoded, packed into an RTP packet (incrementing
        seq/timestamp), optionally SRTP-protected, and sent to the remote.
        The injected ``sleep`` callable paces the stream at ``ptime`` ms.

        Args:
            frame: PCM16 audio at the G.711 wire rate (8 kHz).
        """
        if self.on_hold:
            return

        if self._transport is None:
            msg = "send_audio called before connect()"
            raise RuntimeError(msg)

        payload = self._encode(frame)

        pkt = RtpPacket(
            payload_type=self._codec.value,
            sequence_number=self._seq,
            timestamp=self._ts,
            ssrc=_OUTBOUND_SSRC,
            payload=payload,
        )

        wire = self._srtp_out.protect(pkt) if self._srtp_out is not None else pkt.pack()

        # Advance counters (mod 2^16 for seq, mod 2^32 for ts).
        self._seq = (self._seq + 1) % (1 << 16)
        samples_per_frame = (G711_SAMPLE_RATE * self._ptime) // 1000
        self._ts = (self._ts + samples_per_frame) % (1 << 32)

        self._transport.sendto(wire, (self._remote_address, self._remote_port))

        await self._sleep(self._ptime / 1000.0)

    @property
    def inbound_sample_rate(self) -> int:
        """Rate of frames from :meth:`inbound_audio` (always 8000 Hz for G.711)."""
        return G711_SAMPLE_RATE

    # ------------------------------------------------------------------
    # CallMedia Protocol
    # ------------------------------------------------------------------

    async def set_hold(self, on_hold: bool) -> None:
        """Gate or restore outbound media (hold = stop sending; idempotent).

        Args:
            on_hold: ``True`` to hold; ``False`` to resume.
        """
        self.on_hold = on_hold

    async def stop(self) -> None:
        """Tear down the media plane; close the socket; idempotent.

        Safe to call multiple times or before :meth:`connect`.
        """
        transport = self._transport
        self._transport = None
        if transport is not None:
            transport.close()
        # Push an empty sentinel so any awaiter on _recv_queue.get() unblocks
        # and the inbound generator exits cleanly.
        with contextlib.suppress(asyncio.QueueFull):
            self._recv_queue.put_nowait(b"")

    # ------------------------------------------------------------------
    # Discovered port (after connect with local_port=0)
    # ------------------------------------------------------------------

    @property
    def local_port(self) -> int:
        """The local UDP port (OS-assigned when 0 was passed to __init__)."""
        return self._local_port

    # ------------------------------------------------------------------
    # Internal codec helpers
    # ------------------------------------------------------------------

    def _decode(self, payload: bytes, ts_ns: int) -> PcmFrame:
        """Decode a G.711 RTP payload to a PcmFrame."""
        if self._codec is Codec.PCMU:
            return ulaw_to_frame(payload, monotonic_ts_ns=ts_ns)
        return alaw_to_frame(payload, monotonic_ts_ns=ts_ns)

    def _encode(self, frame: PcmFrame) -> bytes:
        """Encode a PcmFrame to a G.711 RTP payload."""
        if self._codec is Codec.PCMU:
            return frame_to_ulaw(frame)
        return frame_to_alaw(frame)
