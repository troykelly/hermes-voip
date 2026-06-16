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
import struct
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final, Protocol

from hermes_voip.media.audio import (
    G711_SAMPLE_RATE,
    Resampler,
    alaw_to_frame,
    frame_to_alaw,
    frame_to_ulaw,
    ulaw_to_frame,
)

# SrtpError is defined at srtp.py module level and carries no cryptography
# dependency (the cryptography backend is imported lazily inside SrtpSession),
# so importing it here is safe in the default (no-`media`-extra) environment.
# Importing it at module scope — rather than per-packet inside the inbound loop
# — means an import failure propagates at module load (rule 37) instead of
# being silently swallowed as a per-packet drop.
from hermes_voip.media.srtp import SrtpError
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

# Number of TX frames for which peak amplitude is logged at INFO on call open.
# Lets the operator confirm the signal is non-silent without noise in steady state.
_TX_AMPLITUDE_LOG_FRAMES: Final[int] = 3

# A received datagram paired with the UDP source address it arrived from. The
# source address is what symmetric-RTP (comedia) latching needs: we send our
# media back to wherever the peer's media ACTUALLY originates, not blindly to the
# negotiated SDP c=/m= address (which may be a private or SBC-rewritten address
# under NAT).
type _Datagram = tuple[bytes, tuple[str, int]]


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

    def __init__(self, queue: asyncio.Queue[_Datagram]) -> None:
        self._queue = queue
        self._transport: asyncio.BaseTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Store the transport so the engine can send datagrams."""
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Queue the datagram with its source address (non-blocking; drop on overflow).

        The source ``addr`` is preserved so the engine can latch its outbound
        destination onto the peer's real media source (symmetric RTP / comedia).
        """
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait((data, addr))
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
        symmetric: bool = True,
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
        self._symmetric = symmetric
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

        # Symmetric-RTP (comedia) latch state.  ``_outbound_addr`` is the actual
        # destination send_audio aims at; it starts as the SDP-negotiated remote
        # so the first outbound (the greeting) goes out immediately and a comedia
        # gateway can latch onto us.  On the FIRST valid inbound RTP packet we
        # latch it onto that packet's real UDP source (when symmetric is on) and
        # never move it again — see :meth:`_maybe_latch`.
        self._outbound_addr: tuple[str, int] = (remote_address, remote_port)
        self._latched: bool = False

        # One-shot diagnostic flags: log the first outbound and first inbound
        # RTP packet at INFO so the media path is visible in the operator log.
        self._first_tx_logged: bool = False
        self._first_rx_logged: bool = False
        # Peak amplitude logged for the first few TX frames (post-resample,
        # pre-encode) so the operator can confirm the signal is non-silent.
        self._tx_amplitude_frames_left: int = _TX_AMPLITUDE_LOG_FRAMES

        # Socket / asyncio transport state (populated by connect()).
        # _ever_connected distinguishes "stopped after a real call" (silent no-op
        # in send_audio — the frame is dropped cleanly because the call is ending)
        # from "never connected" (programming error — still raises RuntimeError).
        self._ever_connected: bool = False
        self._transport: asyncio.DatagramTransport | None = None
        self._recv_queue: asyncio.Queue[_Datagram] = asyncio.Queue(
            maxsize=_QUEUE_MAXSIZE
        )
        self._jitter: JitterBuffer = JitterBuffer(target_depth=jitter_depth)

        # Outbound rate reconciliation (ADR-0017): a TTS provider emits frames at
        # its own output rate (e.g. sherpa-Kokoro at 24 kHz), but the G.711 wire
        # is fixed at 8 kHz. send_audio resamples any non-8 kHz frame down to the
        # wire rate before encoding. We keep one state-carrying Resampler PER
        # source rate so a continuous stream resamples click-free (frame-by-frame
        # output matches a single pass); 8 kHz frames bypass it entirely.
        self._tx_resamplers: dict[int, Resampler] = {}

        # Stop signal: set by stop(), selected on by the inbound generator so it
        # wakes promptly regardless of recv-queue fullness (a bounded queue can
        # otherwise drop a sentinel datagram and strand the consumer).
        self._stop_event: asyncio.Event = asyncio.Event()

        # Lossless one-item rollback slot.  When the stop flag wins a race in
        # which a datagram had already been dequeued, that datagram is parked
        # here (never re-queued, so it cannot be lost to a full queue) and
        # returned first by the next _next_datagram() call.
        self._pending: _Datagram | None = None

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
        self._stop_event = asyncio.Event()
        self._pending = None
        # Drop any carried outbound-resample state so a reused engine starts a
        # fresh stream (no stale sub-sample phase from a previous call).
        self._tx_resamplers = {}
        # Reset the latch so a reused engine re-latches on its next call: aim
        # back at the SDP-negotiated remote until the first valid inbound packet.
        self._outbound_addr = (self._remote_address, self._remote_port)
        self._latched = False
        # Reset one-shot diagnostic flags so a reconnected engine logs the first
        # outbound and inbound packets of the new call.
        self._first_tx_logged = False
        self._first_rx_logged = False
        self._tx_amplitude_frames_left = _TX_AMPLITUDE_LOG_FRAMES
        protocol = _UdpReceiver(self._recv_queue)

        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            sock=sock,
        )
        # create_datagram_endpoint returns (transport, protocol); the transport
        # is always a DatagramTransport when a UDP socket is passed.
        assert isinstance(transport, asyncio.DatagramTransport)  # noqa: S101 — invariant, not a test assertion
        self._transport = transport
        self._ever_connected = True
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
        """Internal async generator implementing inbound_audio.

        Terminates when :meth:`stop` sets the stop flag (even if the recv queue
        is full).  Cancellation (``asyncio.CancelledError``) propagates — it is
        never swallowed (rule 37), so a caller timeout or cancel is honoured.
        """
        while True:
            item = await self._next_datagram()
            # ``None`` ⇒ stop signalled; exit cleanly.
            if item is None:
                return
            data, source = item

            rtp_pkt: RtpPacket

            # SRTP un-protect (if configured).  Only an authentication/replay
            # failure (SrtpError) drops a single packet; any other exception
            # (e.g. a misconfigured backend) propagates (rule 37).
            if self._srtp_in is not None:
                try:
                    rtp_pkt = self._srtp_in.unprotect(data)
                except SrtpError as exc:
                    _log.debug("SRTP auth/replay failure — datagram dropped: %s", exc)
                    continue
            else:
                # Plain RTP: drop malformed datagrams.
                try:
                    rtp_pkt = RtpPacket.parse(data)
                except ValueError as exc:
                    _log.debug("malformed RTP datagram — dropped: %s", exc)
                    continue

            # The datagram is genuine RTP (it parsed / authenticated): this is
            # the only point at which a comedia latch may fire (anti-spoofing —
            # garbage that does not parse never reaches here).
            self._maybe_latch(rtp_pkt, source)

            if not self._first_rx_logged:
                self._first_rx_logged = True
                _log.info(
                    "rtp rx: first packet <- %s:%d (%d bytes)",
                    source[0],
                    source[1],
                    len(data),
                )

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

    def _maybe_latch(self, packet: RtpPacket, source: tuple[str, int]) -> None:
        """Latch the outbound destination onto the peer's real media source.

        Symmetric-RTP (comedia) NAT traversal: the SDP ``c=``/``m=`` address can
        be a private or SBC-rewritten address the peer's media never actually
        comes from, so we send our RTP back to the source tuple of the peer's
        first genuine RTP packet instead.

        Anti-spoofing — three guards before we move the target:

        * ``self._symmetric`` must be on (``HERMES_VOIP_RTP_SYMMETRIC``); when
          off we always honour the SDP address.
        * We latch only ONCE per call (``self._latched``): the first valid source
          wins and a later packet from a different tuple (a spoof, or a re-routed
          media path) cannot move it.
        * The caller has already proven the datagram is genuine RTP (it parsed,
          or — under SRTP — authenticated), and we additionally require the
          negotiated audio payload type, so neither random noise that happens to
          set the RTP version bits nor an off-codec stray triggers a latch.

        Latching to a tuple we are already sending to is a silent no-op (no log).
        """
        if not self._symmetric or self._latched:
            return
        if packet.payload_type != self._codec.value:
            return  # not the negotiated audio stream — not a latch trigger
        self._latched = True
        if source == self._outbound_addr:
            return  # already aimed here (SDP address matched reality); nothing to do
        self._outbound_addr = source
        # The peer's media ip:port is operational, not PII — logging it is how a
        # live NAT'd call is traced.  No other identifier is emitted.
        _log.info("rtp: latched to %s:%d", source[0], source[1])

    async def _next_datagram(self) -> _Datagram | None:
        """Await the next inbound datagram, or return ``None`` if stopped.

        Races :meth:`asyncio.Queue.get` against the stop flag so the consumer
        wakes promptly when :meth:`stop` is called — even when the bounded recv
        queue is full and a sentinel datagram could not be enqueued.  When both
        are ready, the stop flag wins (we never start draining a full queue that
        a caller has asked us to abandon).

        ``asyncio.CancelledError`` from the awaited tasks propagates to the
        caller (rule 37).  Both race tasks are always cancelled AND awaited
        before this method returns or propagates, so no task is ever left
        pending (no "Task was destroyed but it is pending" warning).

        Rollback is lossless: if the stop flag wins a race in which a datagram
        had already been dequeued, that datagram is parked in :attr:`_pending`
        (never re-queued, so a full queue cannot drop it) and returned first by
        the next call.

        Returns:
            The next datagram, or ``None`` when the stop flag is set.
        """
        # A datagram rolled back from a previous stop-tie is delivered first,
        # in order, independent of recv-queue capacity.
        if self._pending is not None:
            data = self._pending
            self._pending = None
            return data

        # Fast path: already stopped.
        if self._stop_event.is_set():
            return None

        get_task: asyncio.Task[_Datagram] = asyncio.ensure_future(
            self._recv_queue.get()
        )
        stop_task: asyncio.Task[bool] = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait(
                {get_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # Cancel whichever task did not complete, then AWAIT both so their
            # cancellation is fully processed before we return — nothing is left
            # pending.  return_exceptions=True absorbs the CancelledError of the
            # losing task; a cancellation of THIS coroutine still propagates out
            # of the await below (and thus out of _next_datagram) per rule 37.
            for task in (get_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get_task, stop_task, return_exceptions=True)

        # Stop wins over a delivered datagram (abandon a full queue on stop).
        if stop_task.done() and not stop_task.cancelled():
            # A datagram may have been dequeued in the same loop step; park it
            # losslessly so the next call returns it (no silent drop).
            if get_task.done() and not get_task.cancelled():
                self._pending = get_task.result()
            return None

        return get_task.result()

    async def send_audio(self, frame: PcmFrame) -> None:
        """Encode and packetise one near-end frame; gate on hold state.

        When :attr:`on_hold` is ``True`` the datagram is silently discarded.
        Otherwise the frame is resampled to the 8 kHz G.711 wire rate (if it
        arrives at any other rate — e.g. a 24 kHz TTS frame), encoded, packed
        into an RTP packet (incrementing seq/timestamp), optionally
        SRTP-protected, and sent to the remote.  The injected ``sleep`` callable
        paces the stream at ``ptime`` ms.

        Args:
            frame: PCM16 audio at ANY sample rate.  A frame already at 8 kHz is
                encoded directly; any other rate (e.g. the TTS provider's 24 kHz
                output) is resampled down to 8 kHz first (ADR-0017), so this
                method never raises on a non-8 kHz frame — it converts.
        """
        if self.on_hold:
            return

        if self._transport is None:
            if self._ever_connected:
                # Engine has been stopped mid-call (teardown while TTS was in
                # flight). Dropping this frame is correct — the call is ending.
                # This is intentional graceful degradation (AGENTS.md rule 37
                # exemption: the stopped state is established control flow, not
                # an unexpected error; raising here would propagate through the
                # TaskGroup and tear the call down with an abnormal exit instead
                # of a clean BYE).
                return
            msg = "send_audio called before connect()"
            raise RuntimeError(msg)

        wire_rate_frame = self._to_wire_rate(frame)
        payload = self._encode(wire_rate_frame)

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

        # Send to the latched peer source if symmetric-RTP has latched, else to
        # the SDP-negotiated remote (the initial value of _outbound_addr).
        if not self._first_tx_logged:
            self._first_tx_logged = True
            _log.info(
                "rtp tx: first packet -> %s:%d pt=%d ssrc=0x%08x",
                self._outbound_addr[0],
                self._outbound_addr[1],
                self._codec.value,
                _OUTBOUND_SSRC,
            )
        # Log peak amplitude for the first few TX frames (post-resample,
        # pre-encode) so the operator can verify the outbound signal is
        # non-silent even when they cannot capture the wire.
        if self._tx_amplitude_frames_left > 0:
            self._tx_amplitude_frames_left -= 1
            n_samples = len(wire_rate_frame.samples) // 2
            if n_samples > 0:
                pcm_samples = struct.unpack_from(
                    f"<{n_samples}h", wire_rate_frame.samples
                )
                peak = max(abs(s) for s in pcm_samples)
                _log.info(
                    "rtp tx: frame %d peak_amplitude=%d (%.1f%% full-scale)",
                    _TX_AMPLITUDE_LOG_FRAMES - self._tx_amplitude_frames_left,
                    peak,
                    peak / 327.67,
                )
        self._transport.sendto(wire, self._outbound_addr)

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
        # Set the stop flag so the inbound generator wakes and exits cleanly.
        # Unlike a queued sentinel, this is independent of recv-queue capacity:
        # a full queue cannot drop the signal and strand the consumer.
        self._stop_event.set()

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

    def _to_wire_rate(self, frame: PcmFrame) -> PcmFrame:
        """Return ``frame`` resampled to the 8 kHz G.711 wire rate (ADR-0017).

        A frame already at :data:`G711_SAMPLE_RATE` is returned unchanged (no
        resampler touches it, so the 8 kHz fast path is byte-exact). A frame at
        any other rate — typically the TTS provider's 24 kHz output — is
        downsampled to 8 kHz using a per-source-rate, state-carrying
        :class:`~hermes_voip.media.audio.Resampler`, so a continuous stream
        converts click-free (frame-by-frame output equals a single pass). This
        is a conversion, not an error: ``send_audio`` therefore never raises on a
        non-8 kHz frame.

        The output frame keeps the source ``monotonic_ts_ns`` (the presentation
        clock is unaffected by the rate change) and is stamped at the wire rate.
        """
        if frame.sample_rate == G711_SAMPLE_RATE:
            return frame
        resampler = self._tx_resamplers.get(frame.sample_rate)
        if resampler is None:
            resampler = Resampler(frame.sample_rate, G711_SAMPLE_RATE)
            self._tx_resamplers[frame.sample_rate] = resampler
        return PcmFrame(
            samples=resampler.resample(frame.samples),
            sample_rate=G711_SAMPLE_RATE,
            monotonic_ts_ns=frame.monotonic_ts_ns,
        )

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
