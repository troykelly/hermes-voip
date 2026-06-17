"""WebRtcMediaSession orchestration: ICE → DTLS-SRTP keying (ADR-0032).

TDD suite (AGENTS.md rule 18), red-first, deterministic. The session glues the
already-tested primitives — media/ice.IceConnection (gather/connect/send/recv) and
media/dtls.DtlsEndpoint (the memory-BIO DTLS handshake + RFC 5705 SRTP export) —
into the two-phase flow the adapter needs:

  * ``prepare()``  — pick the DTLS role from the offer's a=setup, build the
    DtlsEndpoint + IceConnection, gather candidates, and expose the fingerprint /
    setup / ICE creds + candidates for the SDP answer.
  * ``run_handshake()`` — apply the peer's ICE creds + candidates, connect ICE,
    pump DTLS records over the ICE pipe (RFC 7983 demux), verify the peer
    fingerprint (RFC 5763 §5), and derive the (inbound, outbound) SrtpSession pair.

These tests drive the REAL DtlsEndpoint over a pure in-memory bidirectional fake
ICE pipe pair (so no aioice / real sockets are needed), and the SDES-vs-DTLS
SrtpSession transform is the same one already KAT-tested. The webrtc extra
(pyOpenSSL, for DtlsEndpoint) is required, so the suite skips without it.
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_voip.media.srtp import SrtpSession
from hermes_voip.rtp import RtpPacket
from hermes_voip.sdp import Fingerprint, SetupRole

# DtlsEndpoint needs pyOpenSSL; skip the suite without the webrtc extra.
pytest.importorskip("OpenSSL", reason="webrtc extra (pyOpenSSL) not installed")

from hermes_voip.media.ice import IceCandidate
from hermes_voip.media.webrtc_session import (
    WebRtcMediaSession,
    answer_setup_for_offer,
)


class _FakeIce:
    """A fake :class:`IceConnection` for the session: full ICE surface, linked pipe.

    Implements exactly what :class:`WebRtcMediaSession` drives on the ICE object:
    the local-credential properties, candidate gathering, remote-credential/candidate
    setters, ``connect``, the ``send``/``recv`` datagram pipe (linked to a peer's
    inbound queue), and ``close``. Real connectivity checks are not run — the pipe is
    already "connected" — so a real DtlsEndpoint handshake can complete in-process.
    """

    def __init__(self, ufrag: str, pwd: str) -> None:
        self._ufrag = ufrag
        self._pwd = pwd
        self.inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self.peer: _FakeIce | None = None
        self.closed = False
        self.remote_ufrag: str | None = None
        self.remote_pwd: str | None = None
        self.remote_candidates: list[IceCandidate] = []

    @property
    def local_ufrag(self) -> str:
        return self._ufrag

    @property
    def local_pwd(self) -> str:
        return self._pwd

    @property
    def local_candidates(self) -> list[IceCandidate]:
        # One host candidate (the shape the SDP answer renders).
        return [
            IceCandidate(
                foundation="candidate:1",
                component=1,
                transport="UDP",
                priority=2130706431,
                host="127.0.0.1",
                port=50000,
                type="host",
                related_address=None,
                related_port=None,
            )
        ]

    async def gather_candidates(self) -> None:
        return None

    def set_remote_credentials(self, ufrag: str, pwd: str) -> None:
        self.remote_ufrag = ufrag
        self.remote_pwd = pwd

    async def add_remote_candidate(self, candidate: IceCandidate | None) -> None:
        if candidate is not None:
            self.remote_candidates.append(candidate)

    async def connect(self) -> None:
        return None

    async def send(self, data: bytes) -> None:
        assert self.peer is not None
        self.peer.inbound.put_nowait(bytes(data))

    async def recv(self) -> bytes:
        return await self.inbound.get()

    async def close(self) -> None:
        self.closed = True


def _linked_ice(
    a_creds: tuple[str, str], b_creds: tuple[str, str]
) -> tuple[_FakeIce, _FakeIce]:
    a, b = _FakeIce(*a_creds), _FakeIce(*b_creds)
    a.peer, b.peer = b, a
    return a, b


def test_answer_setup_for_offer_picks_passive_for_actpass() -> None:
    """An offer of actpass/active makes us passive/active per RFC 5763 §5."""
    # actpass offer → we are passive (and thus the DTLS server)
    assert answer_setup_for_offer(SetupRole("actpass")).value == "passive"
    # active offer → we are passive
    assert answer_setup_for_offer(SetupRole("active")).value == "passive"
    # passive offer → we are active (the DTLS client)
    assert answer_setup_for_offer(SetupRole("passive")).value == "active"


@pytest.mark.asyncio
async def test_prepare_exposes_answer_attributes() -> None:
    """prepare() yields the fingerprint + setup + ICE creds the SDP answer needs."""
    ice, _peer = _linked_ice(("ufragAAAA", "pwdAAAAAAAAAAAAAAAAAAAA"), ("u2", "p2"))
    session = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),
        ice_factory=lambda **_kw: ice,
    )
    await session.prepare()
    # Our setup is passive (answerer to an actpass offer).
    assert session.setup.value == "passive"
    # The fingerprint is a sha-256 a=fingerprint value.
    assert isinstance(session.fingerprint, Fingerprint)
    assert session.fingerprint.algorithm == "sha-256"
    # ICE credentials + candidates present (for the SDP answer).
    assert session.ice_ufrag == "ufragAAAA"
    assert session.ice_pwd == "pwdAAAAAAAAAAAAAAAAAAAA"
    assert len(session.ice_candidates) == 1
    await session.close()


@pytest.mark.asyncio
async def test_two_sessions_complete_dtls_and_derive_matching_srtp() -> None:
    """Two paired sessions run ICE+DTLS over the fake pipe and key compatible SRTP.

    The answerer (passive/server) and the offerer-side peer (active/client) each
    derive an (inbound, outbound) SrtpSession pair; the server's outbound must
    decrypt on the client's inbound and vice-versa (RFC 5764 role mirroring).
    """
    server_ice, client_ice = _linked_ice(
        ("svrU", "svrPwwwwwwwwwwwwww"), ("cliU", "cliPwwwwwwwwwwwwww")
    )

    # The answerer: we are passive (DTLS server) responding to an actpass offer.
    server = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),
        ice_factory=lambda **_kw: server_ice,
    )
    # The "peer" (acts as the offerer's active/client side) — modelled with the same
    # session class but role=active, so we can complete a real handshake in-process.
    client = WebRtcMediaSession(
        offer_setup=SetupRole("passive"),  # => this side is active/client
        ice_factory=lambda **_kw: client_ice,
    )

    await asyncio.gather(server.prepare(), client.prepare())
    assert server.setup.value == "passive"
    assert client.setup.value == "active"

    # Each side runs the handshake against the OTHER's fingerprint, concurrently
    # (the fake pipes are linked, so DTLS records flow both ways).
    (s_in, s_out), (c_in, c_out) = await asyncio.gather(
        server.run_handshake(
            peer_fingerprint=client.fingerprint,
            peer_ice_ufrag=client.ice_ufrag,
            peer_ice_pwd=client.ice_pwd,
        ),
        client.run_handshake(
            peer_fingerprint=server.fingerprint,
            peer_ice_ufrag=server.ice_ufrag,
            peer_ice_pwd=server.ice_pwd,
        ),
    )
    for s in (s_in, s_out, c_in, c_out):
        assert isinstance(s, SrtpSession)

    # Cross-decrypt: the server's outbound protects a packet the client's inbound
    # unprotects (and vice versa).
    pkt = RtpPacket(
        payload_type=111, sequence_number=5, timestamp=0, ssrc=0x11, payload=b"voice"
    )
    wire = s_out.protect(pkt)
    recovered = c_in.unprotect(wire)
    assert recovered.payload == b"voice"

    pkt2 = RtpPacket(
        payload_type=111, sequence_number=6, timestamp=160, ssrc=0x22, payload=b"reply"
    )
    wire2 = c_out.protect(pkt2)
    assert s_in.unprotect(wire2).payload == b"reply"

    await asyncio.gather(server.close(), client.close())


@pytest.mark.asyncio
async def test_run_handshake_rejects_fingerprint_mismatch() -> None:
    """A peer fingerprint that does not match the handshake cert raises (RFC 5763)."""
    server_ice, client_ice = _linked_ice(
        ("svrU", "svrPwwwwwwwwwwwwww"), ("cliU", "cliPwwwwwwwwwwwwww")
    )
    server = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),
        ice_factory=lambda **_kw: server_ice,
    )
    client = WebRtcMediaSession(
        offer_setup=SetupRole("passive"),
        ice_factory=lambda **_kw: client_ice,
    )
    await asyncio.gather(server.prepare(), client.prepare())

    bogus = Fingerprint(algorithm="sha-256", value=":".join(["AA"] * 32))
    with pytest.raises(ValueError, match="fingerprint"):
        await asyncio.gather(
            server.run_handshake(
                peer_fingerprint=bogus,  # wrong — not the client's cert
                peer_ice_ufrag=client.ice_ufrag,
                peer_ice_pwd=client.ice_pwd,
            ),
            client.run_handshake(
                peer_fingerprint=server.fingerprint,
                peer_ice_ufrag=server.ice_ufrag,
                peer_ice_pwd=server.ice_pwd,
            ),
        )
    await asyncio.gather(server.close(), client.close())


@pytest.mark.asyncio
async def test_close_closes_the_ice_pipe() -> None:
    """close() releases the ICE connection (so aioice sockets are freed)."""
    ice, _peer = _linked_ice(("u", "pwwwwwwwwwwwwwwwww"), ("u2", "p2wwwwwwwwwwwwwww"))
    session = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),
        ice_factory=lambda **_kw: ice,
    )
    await session.prepare()
    await session.close()
    assert ice.closed is True
