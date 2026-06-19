"""SIP DTLS-SRTP media session over plain UDP — no ICE (ADR-0053 Stage 2).

Mirrors :class:`~hermes_voip.media.webrtc_session.WebRtcMediaSession` but replaces
ICE with a **plain UDP datagram pipe**: a SIP-over-TLS call has the peer's RTP
address directly in the SDP (``c=``/port), so the DTLS handshake (RFC 5763/5764)
runs over a bound UDP socket instead of an ICE-nominated pair.

Two-phase flow the adapter activation wave will drive:

* :meth:`SipDtlsMediaSession.prepare` — bind the UDP socket; its port is what the
  SDP answer advertises (``a=audio <port>``). The DTLS role + our ``a=fingerprint``
  are fixed at construction (from the offer's ``a=setup`` and the
  ``HERMES_VOIP_SIP_DTLS_SETUP`` knob, reusing the WebRTC ``answer_setup_for_offer``
  rationale, RFC 8842 §5.3 / ADR-0050).
* :meth:`SipDtlsMediaSession.run_handshake` — pump the DTLS records over the UDP
  pipe (the same RFC-7983 first-byte demux as the WebRTC pump: only DTLS records,
  first byte 20-63, feed the state machine), verify the peer's certificate against
  the offered ``a=fingerprint`` (RFC 5763 §5 — a mismatch aborts the call), and
  derive the ``(inbound, outbound)`` :class:`~hermes_voip.media.srtp.SrtpSession`
  pair.

After the handshake the **same pipe** (:attr:`pipe`) is handed to the engine as its
``ice_transport`` seam (the engine carries SRTP over the pipe's async
``send``/``recv``/``close``, applying its own RFC-7983 SRTP demux), and the two SRTP
sessions become the engine's ``srtp_inbound``/``srtp_outbound`` — no engine change.

**Comedia (no ICE).** A DTLS-SRTP SIP call may sit behind NAT, so the peer's real
source ``(host, port)`` can differ from the SDP-advertised ``c=``/port. The pipe
sends to the advertised peer initially but **latches** its send destination onto the
source of the first inbound datagram (the symmetric-RTP / comedia latch), which
arrives during the DTLS handshake — so SRTP media reaches the correct 5-tuple. The
latch is one-shot (first-source-wins) to avoid mid-call source-spoofing redirection.

**Security invariants.** No key/cert material is logged or raised in exception text
(inherited from :mod:`hermes_voip.media.dtls`). The peer fingerprint is verified
before any SRTP key is derived (``derive_srtp_sessions`` itself enforces this).

**Dependency gating.** :class:`~hermes_voip.media.dtls.DtlsEndpoint` lazy-imports
pyOpenSSL (the ``webrtc`` extra); this module imports it at module scope but it in
turn defers the heavy import, so ``import hermes_voip.media.sip_dtls_session`` stays
light and the ImportError surfaces only at construction (rule 37).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

from hermes_voip.media.dtls import DtlsEndpoint, DtlsRole
from hermes_voip.media.srtp import SrtpSession
from hermes_voip.media.webrtc_session import answer_setup_for_offer
from hermes_voip.sdp import Fingerprint, SetupRole

__all__ = ["SipDtlsMediaSession"]

_log = logging.getLogger(__name__)

# RFC 7983 first-byte demux: during the handshake the UDP pipe carries DTLS records
# (first byte 20-63); a stray early SRTP/RTP datagram (128-191) is dropped so it
# never corrupts the handshake. (Once keyed the engine applies its own SRTP demux.)
_RFC7983_DTLS_MIN = 20
_RFC7983_DTLS_MAX = 63

# Safety bound on the DTLS handshake pump (datagram round-trips) so a stuck peer
# cannot hang call setup forever; each iteration awaits at most one recv with a
# per-recv timeout. Mirrors the WebRTC pump (webrtc_session.py).
_MAX_HANDSHAKE_ROUNDS = 200
# Per-recv timeout (seconds) inside the handshake pump. A handshake that stalls this
# long on any single inbound datagram is treated as failed.
_HANDSHAKE_RECV_TIMEOUT_S = 10.0


class _DatagramPipe(Protocol):
    """The datagram-pipe surface this session drives.

    A superset of the engine's ``ice_transport`` seam
    (:class:`hermes_voip.media.engine._IceDatagramPipe` — async ``send`` / ``recv``
    / ``close``): the session additionally reads :attr:`local_port` (for the SDP
    answer) and calls :meth:`set_peer` (the initial send destination). The
    send/recv/close subset is exactly the engine seam, so the same pipe object is
    handed to the engine as ``ice_transport`` after the handshake. Declared as a
    Protocol so tests can inject an in-memory linked pipe pair without real sockets;
    :class:`_UdpDatagramPipe` satisfies it structurally.
    """

    @property
    def local_port(self) -> int:
        """The bound local UDP port (for the SDP ``m=audio <port>`` answer)."""
        ...

    def set_peer(self, host: str, port: int) -> None:
        """Set the initial send destination (the SDP-advertised peer ``c=``/port)."""
        ...

    async def send(self, data: bytes) -> None:
        """Send one datagram to the (possibly latched) peer."""
        ...

    async def recv(self) -> bytes:
        """Receive the next datagram (data only; the source is latched internally)."""
        ...

    async def close(self) -> None:
        """Close the pipe and release its socket."""
        ...


class _PipeFactory(Protocol):
    """Builds the datagram pipe (the real :class:`_UdpDatagramPipe`, or a test fake).

    A typed callable Protocol (not ``Callable[..., _DatagramPipe]``) so the keyword
    signature is explicit and ``disallow_any_explicit`` stays satisfied.
    """

    async def __call__(self, *, local_address: str, local_port: int) -> _DatagramPipe:
        """Bind a datagram pipe on ``(local_address, local_port)`` (0 ⇒ OS port)."""
        ...


class _UdpDatagramPipe(asyncio.DatagramProtocol):
    """A plain-UDP datagram pipe satisfying the engine's ``ice_transport`` seam.

    Owns one ``asyncio`` UDP endpoint (a ``DatagramTransport`` + this
    ``DatagramProtocol``). Inbound datagrams are queued (data only) and yielded by
    :meth:`recv`; :meth:`send` writes to the peer ``(host, port)``. The send
    destination is the SDP-advertised peer until the **first** inbound datagram
    arrives, after which it latches onto that datagram's real source (the comedia /
    symmetric-RTP latch — the no-ICE NAT-traversal mechanism). The latch is one-shot.

    Construct via :meth:`bind` (an async classmethod that creates the endpoint).
    """

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self._transport: asyncio.DatagramTransport | None = None
        self._peer: tuple[str, int] | None = None
        self._latched = False
        self._local_port = 0

    @classmethod
    async def bind(
        cls, loop: asyncio.AbstractEventLoop, local_address: str, local_port: int
    ) -> _UdpDatagramPipe:
        """Bind a UDP endpoint on ``(local_address, local_port)`` (0 ⇒ OS-assigned).

        Returns the constructed pipe with :attr:`local_port` populated from the
        actually-bound socket.
        """
        pipe = cls()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: pipe, local_addr=(local_address, local_port)
        )
        # Read the actually-bound port (it was 0 when the OS assigns one).
        sockname = transport.get_extra_info("sockname")
        if sockname is not None:
            pipe._local_port = int(sockname[1])
        return pipe

    @property
    def local_port(self) -> int:
        """The bound local UDP port (for the SDP ``m=audio <port>`` answer)."""
        return self._local_port

    @property
    def latched(self) -> bool:
        """Whether the send destination has latched onto a received source."""
        return self._latched

    def set_peer(self, host: str, port: int) -> None:
        """Set the initial send destination (the SDP-advertised peer ``c=``/port)."""
        self._peer = (host, port)

    # asyncio.DatagramProtocol -------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Store the datagram transport so :meth:`send` can write to it."""
        if isinstance(transport, asyncio.DatagramTransport):
            self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Queue an inbound datagram and latch the send destination once (comedia)."""
        if not self._latched:
            # First-source-wins: latch outbound onto the peer's real media source.
            self._peer = (addr[0], int(addr[1]))
            self._latched = True
        with contextlib.suppress(asyncio.QueueFull):
            self._inbound.put_nowait(bytes(data))

    def error_received(self, exc: Exception) -> None:
        """Log a transient ICMP/socket error (the handshake pump's timeout covers it).

        A fatal socket error during the handshake surfaces as a recv timeout (the
        pump raises), and during media the engine's own recv loop reports the loss;
        we do not swallow it silently — it is logged here and acted on there.
        """
        _log.warning("sip-dtls udp error received: %s", exc)

    # Datagram pipe surface (the engine's ice_transport seam) ------------------

    async def send(self, data: bytes) -> None:
        """Send one datagram to the (initial or latched) peer.

        Raises:
            RuntimeError: If called before the endpoint is connected, or before a
                send destination is known (``set_peer`` not called and nothing
                received yet) — both are programming errors (rule 37, no silent drop).
        """
        if self._transport is None:
            msg = "_UdpDatagramPipe.send() before the endpoint was bound"
            raise RuntimeError(msg)
        if self._peer is None:
            msg = "_UdpDatagramPipe.send() before a peer destination is known"
            raise RuntimeError(msg)
        self._transport.sendto(bytes(data), self._peer)

    async def recv(self) -> bytes:
        """Await the next inbound datagram (data only; source latched internally)."""
        return await self._inbound.get()

    async def close(self) -> None:
        """Close the UDP endpoint (idempotent)."""
        if self._transport is not None and not self._transport.is_closing():
            self._transport.close()


async def _default_pipe_factory(
    *, local_address: str, local_port: int
) -> _DatagramPipe:
    """Bind the real :class:`_UdpDatagramPipe` on the running event loop."""
    loop = asyncio.get_running_loop()
    return await _UdpDatagramPipe.bind(loop, local_address, local_port)


class SipDtlsMediaSession:
    """Orchestrates plain-UDP DTLS-SRTP keying for one inbound SIP call (ADR-0053).

    Construct with the offer's ``a=setup`` role; call :meth:`prepare` (binds the UDP
    socket and exposes :attr:`local_port` for the SDP answer), build + send the SDP
    answer from :attr:`fingerprint` / :attr:`setup` / :attr:`local_port`, then call
    :meth:`run_handshake` (DTLS handshake + SRTP keying). The :attr:`pipe` and the
    derived SRTP pair then drive the engine (``ice_transport=session.pipe``).

    Args:
        offer_setup: The offered ``a=setup`` role (``None`` ⇒ treated as actpass).
        answer_setup: Our DTLS-role preference for an ``actpass`` offer
            (``HERMES_VOIP_SIP_DTLS_SETUP``, mirroring ADR-0050): ``"auto"``
            (RFC 8842 active answerer — the default), ``"active"``, or ``"passive"``.
            A pinned ``active``/``passive`` offer always overrides this
            (:func:`~hermes_voip.media.webrtc_session.answer_setup_for_offer`).
        cipher_list: Optional DTLS cipher pin passed to :class:`DtlsEndpoint`.
        pipe_factory: Factory binding the datagram pipe (defaults to the real
            :class:`_UdpDatagramPipe`; injected in tests).
    """

    def __init__(
        self,
        *,
        offer_setup: SetupRole | None,
        answer_setup: str = "auto",
        cipher_list: bytes | None = None,
        pipe_factory: _PipeFactory = _default_pipe_factory,
    ) -> None:
        """Pick the DTLS role from the offer + knob, and build the DTLS endpoint."""
        self._setup = answer_setup_for_offer(offer_setup, answer_setup)
        role = DtlsRole.CLIENT if self._setup.value == "active" else DtlsRole.SERVER
        self._dtls = DtlsEndpoint(role=role, cipher_list=cipher_list)
        self._pipe_factory = pipe_factory
        self._pipe: _DatagramPipe | None = None
        self._local_port = 0

    # ------------------------------------------------------------------
    # Phase A: prepare (bind the UDP socket; expose the answer port)
    # ------------------------------------------------------------------

    async def prepare(self, *, local_address: str, local_port: int = 0) -> None:
        """Bind the UDP datagram pipe so :attr:`local_port` becomes available.

        Idempotent guard: must be called once before :attr:`local_port`,
        :attr:`pipe`, and :meth:`run_handshake`.

        Args:
            local_address: The local address to bind the RTP socket on.
            local_port: The local UDP port (``0`` ⇒ OS-assigned, the usual case).
        """
        pipe = await self._pipe_factory(
            local_address=local_address, local_port=local_port
        )
        self._pipe = pipe
        self._local_port = pipe.local_port

    @property
    def setup(self) -> SetupRole:
        """Our ``a=setup`` role for the SDP answer (``active`` or ``passive``)."""
        return self._setup

    @property
    def fingerprint(self) -> Fingerprint:
        """Our DTLS certificate fingerprint for the SDP ``a=fingerprint``."""
        algo, _, value = self._dtls.fingerprint().partition(" ")
        return Fingerprint(algorithm=algo.lower(), value=value)

    @property
    def local_port(self) -> int:
        """The bound local UDP port to advertise in the SDP answer ``m=audio``."""
        return self._local_port

    @property
    def pipe(self) -> _DatagramPipe:
        """The datagram pipe (handed to the engine as ``ice_transport``).

        Raises:
            RuntimeError: If accessed before :meth:`prepare`.
        """
        if self._pipe is None:
            msg = "SipDtlsMediaSession.pipe accessed before prepare()"
            raise RuntimeError(msg)
        return self._pipe

    # ------------------------------------------------------------------
    # Phase B: run the DTLS handshake, derive SRTP
    # ------------------------------------------------------------------

    async def run_handshake(
        self,
        *,
        peer_fingerprint: Fingerprint,
        peer_address: str,
        peer_port: int,
    ) -> tuple[SrtpSession, SrtpSession]:
        """Run the DTLS handshake over the UDP pipe, returning the SRTP pair.

        Sets the initial send destination to ``(peer_address, peer_port)`` (the SDP
        ``c=``/port), pumps the DTLS records over the pipe (RFC 7983 demux), verifies
        the peer's certificate against ``peer_fingerprint`` (RFC 5763 §5), and derives
        the SRTP sessions. The comedia latch (see the class docstring) repoints the
        destination onto the peer's real source on the first inbound datagram.

        Args:
            peer_fingerprint: The peer's ``a=fingerprint`` from their offer.
            peer_address: The peer's RTP address from the offer's ``c=``.
            peer_port: The peer's RTP port from the offer's ``m=audio``.

        Returns:
            ``(inbound, outbound)`` SRTP sessions for the engine.

        Raises:
            RuntimeError: If called before :meth:`prepare`, or the DTLS handshake
                fails to complete within the round/recv bound.
            ValueError: If the peer's certificate fingerprint does not match
                ``peer_fingerprint`` (the call must be aborted — no plaintext
                fallback).
        """
        if self._pipe is None:
            msg = "SipDtlsMediaSession.run_handshake() called before prepare()"
            raise RuntimeError(msg)
        # Set the initial send destination (the SDP-advertised peer). The pipe
        # latches onto the real source on the first inbound datagram (comedia).
        self._pipe.set_peer(peer_address, peer_port)

        await self._pump_dtls_handshake(self._pipe)

        # RFC 5763 §5: verify the peer cert fingerprint BEFORE deriving keys. A
        # mismatch raises ValueError (the call must be rejected).
        self._dtls.verify_peer_fingerprint(
            f"{peer_fingerprint.algorithm} {peer_fingerprint.value}"
        )

        inbound, outbound = self._dtls.derive_srtp_sessions()
        _log.info("sip-dtls: DTLS-SRTP keyed (setup=%s)", self._setup.value)
        return inbound, outbound

    async def _pump_dtls_handshake(self, pipe: _DatagramPipe) -> None:
        """Exchange DTLS records over the UDP pipe until the handshake completes.

        Each round: drain the DTLS state machine's outbound datagrams and send them;
        then — if not yet done — receive the next datagram, demux DTLS (RFC 7983
        first byte 20-63; non-DTLS bytes are dropped), and feed it back. The CLIENT
        (``a=setup:active``) produces the ClientHello on the first drain; the SERVER
        waits for it. A fatal alert re-raises from feed/get_outbound_datagrams
        (rule 37).

        Raises:
            RuntimeError: If the handshake does not complete within the round/recv
                bound.
        """
        for _ in range(_MAX_HANDSHAKE_ROUNDS):
            for dg in self._dtls.get_outbound_datagrams():
                await pipe.send(dg)
            if self._dtls.handshake_done():
                return
            try:
                data = await asyncio.wait_for(
                    pipe.recv(), timeout=_HANDSHAKE_RECV_TIMEOUT_S
                )
            except TimeoutError as exc:
                msg = "DTLS handshake stalled waiting for a peer datagram"
                raise RuntimeError(msg) from exc
            if not data:
                continue
            first = data[0]
            if _RFC7983_DTLS_MIN <= first <= _RFC7983_DTLS_MAX:
                self._dtls.feed(data)
            else:
                # A non-DTLS datagram during the handshake (early SRTP/RTP). Ignore
                # it — only DTLS records advance the state machine.
                _log.debug(
                    "sip-dtls: ignoring non-DTLS datagram during handshake "
                    "(first byte %d)",
                    first,
                )
        msg = "DTLS handshake did not complete within the round limit"
        raise RuntimeError(msg)

    async def close(self) -> None:
        """Close the datagram pipe (release the UDP socket); idempotent."""
        if self._pipe is not None:
            await self._pipe.close()
