"""SipDtlsMediaSession: plain-UDP DTLS-SRTP keying (ADR-0053 Stage 2).

TDD suite (AGENTS.md rule 18), red-first, deterministic. The session mirrors
``WebRtcMediaSession`` but replaces ICE with a plain-UDP datagram pipe: it owns a
bound UDP socket, runs the same memory-BIO ``DtlsEndpoint`` handshake over it
(RFC 7983 first-byte demux), verifies the peer fingerprint (RFC 5763 §5), and
derives the ``(inbound, outbound)`` SrtpSession pair. The derived pipe satisfies
the engine's ``ice_transport`` seam (async ``send``/``recv``/``close``), so the
engine carries SRTP over it unchanged.

These tests drive the REAL ``DtlsEndpoint`` over an in-memory linked UDP pipe pair
(no real sockets) for the handshake-orchestration tests, plus one real-socket test
proving the session binds a UDP endpoint and reports its port for the SDP answer.
No cert/key material is hard-coded: ``DtlsEndpoint`` mints an ephemeral self-signed
cert at construction (gitleaks-safe). The webrtc extra (pyOpenSSL) is required, so
the suite skips without it.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from hermes_voip.rtp import RtpPacket
from hermes_voip.sdp import Fingerprint, SetupRole

# DtlsEndpoint needs pyOpenSSL; skip the suite without the webrtc extra.
pytest.importorskip("OpenSSL", reason="webrtc extra (pyOpenSSL) not installed")

from hermes_voip.media.dtls import DtlsEndpoint, DtlsRole
from hermes_voip.media.sip_dtls_session import (
    SipDtlsMediaSession,
    _UdpDatagramPipe,
)

# RFC 7983 first-byte demux range for DTLS records (20-63).
_DTLS_MIN = 20
_DTLS_MAX = 63


class _FakeUdpPipe:
    """An in-memory bidirectional datagram pipe (the ``_UdpDatagramPipe`` surface).

    Implements exactly what ``SipDtlsMediaSession`` drives on its pipe — async
    ``send``/``recv``/``close`` plus the ``latched`` property the comedia test
    asserts. ``send`` enqueues onto the peer's inbound queue; ``recv`` awaits this
    pipe's inbound queue. No real socket — a real ``DtlsEndpoint`` handshake
    completes in-process over the linked pair.
    """

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self.peer: _FakeUdpPipe | None = None
        self.closed = False
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        assert self.peer is not None
        self.sent.append(bytes(data))
        self.peer.inbound.put_nowait(bytes(data))

    async def recv(self) -> bytes:
        return await self.inbound.get()

    async def close(self) -> None:
        self.closed = True


def _linked_pipes() -> tuple[_FakeUdpPipe, _FakeUdpPipe]:
    a, b = _FakeUdpPipe(), _FakeUdpPipe()
    a.peer, b.peer = b, a
    return a, b


def _pipe_factory(
    pipe: _FakeUdpPipe,
) -> object:
    """Return a one-shot factory yielding ``pipe`` (the session's pipe seam)."""

    async def factory(*, local_address: str, local_port: int) -> _FakeUdpPipe:
        _ = (local_address, local_port)  # the fake pipe ignores bind params
        return pipe

    return factory


async def _pump_dtls(endpoint: DtlsEndpoint, pipe: _FakeUdpPipe) -> None:
    """Pump a bare DtlsEndpoint's handshake over a fake pipe (the peer side)."""
    for _ in range(200):
        for dg in endpoint.get_outbound_datagrams():
            await pipe.send(dg)
        if endpoint.handshake_done():
            return
        data = await asyncio.wait_for(pipe.recv(), timeout=10.0)
        if data and _DTLS_MIN <= data[0] <= _DTLS_MAX:
            endpoint.feed(data)


def _endpoint_fingerprint(endpoint: DtlsEndpoint) -> Fingerprint:
    """The SDP Fingerprint for a DtlsEndpoint (strip the 'sha-256 ' prefix)."""
    return Fingerprint(
        algorithm="sha-256", value=endpoint.fingerprint().split(" ", 1)[1]
    )


# ---------------------------------------------------------------------------
# Construction + DTLS role
# ---------------------------------------------------------------------------


def test_session_role_active_for_actpass_offer() -> None:
    """An actpass offer makes us active (the DTLS client) by default (ADR-0050)."""
    pipe = _FakeUdpPipe()
    session = SipDtlsMediaSession(
        offer_setup=SetupRole("actpass"),
        pipe_factory=_pipe_factory(pipe),
    )
    assert session.setup.value == "active"


def test_session_role_passive_for_active_offer() -> None:
    """A pinned active offer MUST be answered passive (RFC 5763 §5)."""
    pipe = _FakeUdpPipe()
    session = SipDtlsMediaSession(
        offer_setup=SetupRole("active"),
        pipe_factory=_pipe_factory(pipe),
    )
    assert session.setup.value == "passive"


def test_session_role_forced_passive_only_for_actpass() -> None:
    """The setup knob forces passive only on an actpass offer (ADR-0050)."""
    pipe = _FakeUdpPipe()
    session = SipDtlsMediaSession(
        offer_setup=SetupRole("actpass"),
        answer_setup="passive",
        pipe_factory=_pipe_factory(pipe),
    )
    assert session.setup.value == "passive"


def test_session_exposes_fingerprint() -> None:
    """The session exposes its DTLS cert fingerprint for the SDP a=fingerprint."""
    pipe = _FakeUdpPipe()
    session = SipDtlsMediaSession(
        offer_setup=SetupRole("actpass"),
        pipe_factory=_pipe_factory(pipe),
    )
    fp = session.fingerprint
    assert isinstance(fp, Fingerprint)
    assert fp.algorithm == "sha-256"
    # 32 colon-separated hex byte pairs (SHA-256).
    assert len(fp.value.split(":")) == 32


# ---------------------------------------------------------------------------
# The bound UDP socket (real-socket test) — prepare() reports its port
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_binds_udp_socket_and_reports_port() -> None:
    """prepare() binds a real UDP endpoint and exposes the bound port for the SDP."""
    session = SipDtlsMediaSession(offer_setup=SetupRole("actpass"))
    try:
        await session.prepare(local_address="127.0.0.1", local_port=0)
        # The OS assigned a real port; the SDP answer advertises THIS port.
        assert session.local_port > 0
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_run_handshake_requires_prepare_first() -> None:
    """run_handshake before prepare is a programming error (RuntimeError)."""
    pipe = _FakeUdpPipe()
    session = SipDtlsMediaSession(
        offer_setup=SetupRole("actpass"),
        pipe_factory=_pipe_factory(pipe),
    )
    with pytest.raises(RuntimeError, match="prepare"):
        await session.run_handshake(
            peer_fingerprint=Fingerprint(algorithm="sha-256", value="00:11"),
            peer_address="192.0.2.30",
            peer_port=41000,
        )


# ---------------------------------------------------------------------------
# Two sessions complete a real DTLS handshake over the linked pipe + key match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_sessions_complete_dtls_and_derive_matching_srtp() -> None:
    """An answerer session + an offerer DtlsEndpoint complete DTLS; keys interop.

    The answerer's OUTBOUND key must equal the offerer's INBOUND key (so what the
    answerer encrypts, the offerer decrypts) and vice-versa — proven by encrypting a
    packet on one side and decrypting it on the other (RFC 5764 role-mirrored keys).
    """
    a_pipe, o_pipe = _linked_pipes()
    # Answerer: actpass offer -> active (DTLS client). So the offerer is the SERVER.
    answerer = SipDtlsMediaSession(
        offer_setup=SetupRole("actpass"),
        pipe_factory=_pipe_factory(a_pipe),
    )
    await answerer.prepare(local_address="127.0.0.1", local_port=0)
    offerer = DtlsEndpoint(role=DtlsRole.SERVER)

    offerer_task = asyncio.create_task(_pump_dtls(offerer, o_pipe))
    try:
        a_inbound, a_outbound = await answerer.run_handshake(
            peer_fingerprint=_endpoint_fingerprint(offerer),
            peer_address="192.0.2.30",
            peer_port=41000,
        )
    finally:
        await offerer_task
    # The offerer verifies the answerer too (RFC 5763 §5) — proves cross-binding.
    offerer.verify_peer_fingerprint(
        f"{answerer.fingerprint.algorithm} {answerer.fingerprint.value}"
    )
    o_inbound, o_outbound = offerer.derive_srtp_sessions()

    # What the answerer encrypts (outbound), the offerer decrypts (inbound).
    pkt = RtpPacket(
        payload_type=0,
        sequence_number=1,
        timestamp=160,
        ssrc=0xCAFEBABE,
        payload=b"\x00" * 160,
    )
    wire = a_outbound.protect(pkt)
    recovered = o_inbound.unprotect(wire)
    assert recovered.payload == pkt.payload
    # And the converse direction round-trips too.
    pkt2 = RtpPacket(
        payload_type=0,
        sequence_number=7,
        timestamp=1120,
        ssrc=0x12345678,
        payload=b"\x11" * 160,
    )
    wire2 = o_outbound.protect(pkt2)
    recovered2 = a_inbound.unprotect(wire2)
    assert recovered2.payload == pkt2.payload

    await answerer.close()


@pytest.mark.asyncio
async def test_run_handshake_rejects_fingerprint_mismatch() -> None:
    """A peer cert that does not match the offered a=fingerprint aborts (RFC 5763)."""
    a_pipe, o_pipe = _linked_pipes()
    answerer = SipDtlsMediaSession(
        offer_setup=SetupRole("actpass"),
        pipe_factory=_pipe_factory(a_pipe),
    )
    await answerer.prepare(local_address="127.0.0.1", local_port=0)
    offerer = DtlsEndpoint(role=DtlsRole.SERVER)

    offerer_task = asyncio.create_task(_pump_dtls(offerer, o_pipe))
    try:
        with pytest.raises(ValueError, match="fingerprint"):
            await answerer.run_handshake(
                # A deliberately WRONG fingerprint (not the offerer's real cert).
                peer_fingerprint=Fingerprint(
                    algorithm="sha-256",
                    value=":".join(["00"] * 32),
                ),
                peer_address="192.0.2.30",
                peer_port=41000,
            )
    finally:
        offerer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await offerer_task
    await answerer.close()


# ---------------------------------------------------------------------------
# close() closes the pipe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_closes_the_pipe() -> None:
    """Closing the session closes its datagram pipe (releases the UDP socket)."""
    pipe = _FakeUdpPipe()
    session = SipDtlsMediaSession(
        offer_setup=SetupRole("actpass"),
        pipe_factory=_pipe_factory(pipe),
    )
    await session.prepare(local_address="127.0.0.1", local_port=0)
    await session.close()
    assert pipe.closed is True


# ---------------------------------------------------------------------------
# _UdpDatagramPipe: real-socket comedia latch + datagram round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_udp_pipe_round_trips_datagrams_over_real_sockets() -> None:
    """Two _UdpDatagramPipe over real loopback sockets exchange datagrams."""
    loop = asyncio.get_running_loop()
    a = await _UdpDatagramPipe.bind(loop, "127.0.0.1", 0)
    b = await _UdpDatagramPipe.bind(loop, "127.0.0.1", 0)
    try:
        a.set_peer("127.0.0.1", b.local_port)
        b.set_peer("127.0.0.1", a.local_port)
        await a.send(b"\x14hello")  # 0x14 = 20, a DTLS-range first byte
        got = await asyncio.wait_for(b.recv(), timeout=5.0)
        assert got == b"\x14hello"
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_udp_pipe_latches_send_dest_to_first_inbound_source() -> None:
    """The pipe latches its send destination to the first inbound source (comedia).

    NAT case: the peer's real source port differs from the SDP-advertised one. The
    pipe is told to send to a BOGUS port (a black hole) initially, but once it
    receives a datagram from the peer's real socket it latches onto that source, so
    the reply reaches the peer. This is the no-ICE NAT-traversal mechanism.
    """
    loop = asyncio.get_running_loop()
    a = await _UdpDatagramPipe.bind(loop, "127.0.0.1", 0)
    b = await _UdpDatagramPipe.bind(loop, "127.0.0.1", 0)
    try:
        # a is told the WRONG destination (a port nothing listens on) — like a
        # NATed peer whose advertised c=/port is unreachable.
        a.set_peer("127.0.0.1", 1)  # port 1: a black hole
        b.set_peer("127.0.0.1", a.local_port)
        # b reaches a (correct dest). a learns b's real source and latches.
        await b.send(b"\x14from-b")
        got = await asyncio.wait_for(a.recv(), timeout=5.0)
        assert got == b"\x14from-b"
        assert a.latched is True
        # Now a's reply must reach b's REAL source, not the black-hole port 1.
        await a.send(b"\x14reply")
        back = await asyncio.wait_for(b.recv(), timeout=5.0)
        assert back == b"\x14reply"
    finally:
        await a.close()
        await b.close()
