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


def test_answer_setup_for_offer_picks_active_for_actpass() -> None:
    """The RFC 8842 default: an actpass offer makes us active (ADR-0050).

    RFC 8842 §5.3: the answerer SHOULD be ``active`` (the DTLS client, sending the
    ClientHello). A real Asterisk/UCM gateway offers ``actpass`` but behaves as the
    DTLS server, so an answerer that picks ``passive`` deadlocks (both servers). The
    ``auto`` default therefore answers ``active`` to an ``actpass`` offer. The forced
    roles in the offer are still honoured: an ``active`` offer pins us ``passive``;
    a ``passive`` offer pins us ``active`` (RFC 5763 §5).
    """
    # actpass offer → we are ACTIVE (the DTLS client) under the RFC-8842 default.
    assert answer_setup_for_offer(SetupRole("actpass")).value == "active"
    # active offer → we MUST be passive (the offerer pinned itself client).
    assert answer_setup_for_offer(SetupRole("active")).value == "passive"
    # passive offer → we are active (the offerer pinned itself server).
    assert answer_setup_for_offer(SetupRole("passive")).value == "active"
    # A missing a=setup is treated as actpass (RFC 5763 §5 default) → active.
    assert answer_setup_for_offer(None).value == "active"


def test_answer_setup_for_offer_forced_passive_only_affects_actpass() -> None:
    """A forced ``passive`` answer role overrides an actpass offer, not a pinned one.

    The operator knob ``HERMES_VOIP_WEBRTC_DTLS_SETUP=passive`` forces us to be the
    DTLS server for an ``actpass`` offer (some gateways insist on being the client).
    The forced-vs-offer compatibility rules still bind: a peer that pinned itself
    ``active`` (DTLS client) MUST be answered ``passive`` regardless of the knob, and
    a peer that pinned itself ``passive`` MUST be answered ``active`` regardless.
    """
    # actpass offer + forced passive → we are passive (the server).
    assert answer_setup_for_offer(SetupRole("actpass"), forced="passive").value == (
        "passive"
    )
    # active offer can ONLY be answered passive — the knob cannot create two clients.
    assert answer_setup_for_offer(SetupRole("active"), forced="passive").value == (
        "passive"
    )
    # passive offer can ONLY be answered active — the knob cannot create two servers.
    assert answer_setup_for_offer(SetupRole("passive"), forced="passive").value == (
        "active"
    )


def test_answer_setup_for_offer_forced_active_only_affects_actpass() -> None:
    """A forced ``active`` answer role matches the RFC-8842 default for actpass.

    ``forced="active"`` makes the actpass answer ``active`` (same as ``auto``), and
    still cannot override a pinned offer: an ``active`` offer is answered ``passive``,
    a ``passive`` offer is answered ``active``.
    """
    assert answer_setup_for_offer(SetupRole("actpass"), forced="active").value == (
        "active"
    )
    assert answer_setup_for_offer(SetupRole("active"), forced="active").value == (
        "passive"
    )
    assert answer_setup_for_offer(SetupRole("passive"), forced="active").value == (
        "active"
    )


def test_answer_setup_for_offer_auto_is_the_default() -> None:
    """``forced="auto"`` (the default) is the RFC-8842 mapping (active for actpass)."""
    assert answer_setup_for_offer(SetupRole("actpass"), forced="auto").value == "active"
    # Omitting forced entirely is the same as forced="auto".
    assert answer_setup_for_offer(SetupRole("actpass")) == answer_setup_for_offer(
        SetupRole("actpass"), forced="auto"
    )


def test_session_answer_setup_threads_to_role() -> None:
    """``WebRtcMediaSession(answer_setup=...)`` selects the DTLS role for actpass.

    ``passive`` makes us the DTLS server for an actpass offer; ``auto``/``active``
    make us the client (the RFC-8842 default, ADR-0050).
    """
    forced_passive = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),
        answer_setup="passive",
        ice_factory=lambda **_kw: _FakeIce("u", "pwwwwwwwwwwwwwwwww"),
    )
    assert forced_passive.setup.value == "passive"

    auto = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),
        ice_factory=lambda **_kw: _FakeIce("u", "pwwwwwwwwwwwwwwwww"),
    )
    assert auto.setup.value == "active"


@pytest.mark.asyncio
async def test_prepare_exposes_answer_attributes() -> None:
    """prepare() yields the fingerprint + setup + ICE creds the SDP answer needs."""
    ice, _peer = _linked_ice(("ufragAAAA", "pwdAAAAAAAAAAAAAAAAAAAA"), ("u2", "p2"))
    session = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),
        ice_factory=lambda **_kw: ice,
    )
    await session.prepare()
    # Our setup is active (RFC 8842 answerer to an actpass offer; ADR-0050).
    assert session.setup.value == "active"
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

    # The answerer (DTLS server): an *active* offer pins us passive/server. This is
    # the role the gateway holds; under the RFC-8842 default an actpass offer would
    # make us the client, so to model the server side here we answer an active offer.
    server = WebRtcMediaSession(
        offer_setup=SetupRole("active"),  # => this side is passive/server
        ice_factory=lambda **_kw: server_ice,
    )
    # The "peer" (the DTLS client) — an actpass offer makes us active under the
    # RFC-8842 default (ADR-0050), so this side sends the ClientHello.
    client = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),  # => this side is active/client (RFC 8842)
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
        offer_setup=SetupRole("active"),  # => passive/server (RFC 8842, ADR-0050)
        ice_factory=lambda **_kw: server_ice,
    )
    client = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),  # => active/client (RFC 8842, ADR-0050)
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


# ---------------------------------------------------------------------------
# TURN threading + trickle end-of-candidates control (ADR-0034)
# ---------------------------------------------------------------------------


class _RecordingIce(_FakeIce):
    """A fake ICE that records the end-of-candidates signal (None marker)."""

    def __init__(self, ufrag: str, pwd: str) -> None:
        super().__init__(ufrag, pwd)
        self.end_of_candidates_signalled = False

    async def add_remote_candidate(self, candidate: IceCandidate | None) -> None:
        if candidate is None:
            self.end_of_candidates_signalled = True
        await super().add_remote_candidate(candidate)


def test_session_threads_turn_params_to_factory() -> None:
    """WebRtcMediaSession passes TURN url+creds to its ICE factory (ADR-0034)."""
    captured: dict[str, object] = {}

    def _factory(**kwargs: object) -> _FakeIce:
        captured.update(kwargs)
        return _FakeIce("u", "pwwwwwwwwwwwwwwwww")

    WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),
        stun_urls=("stun:stun.example.test:3478",),
        turn_urls=("turn:turn.example.test:3478",),
        turn_username="relay-user",
        turn_password="relay-secret",
        ice_factory=_factory,
    )
    assert captured["turn_urls"] == ("turn:turn.example.test:3478",)
    assert captured["turn_username"] == "relay-user"
    assert captured["turn_password"] == "relay-secret"


def test_session_threads_ipv6_flags_to_factory() -> None:
    """WebRtcMediaSession passes the IPv6/IPv4 family flags to ICE (ADR-0043)."""
    captured: dict[str, object] = {}

    def _factory(**kwargs: object) -> _FakeIce:
        captured.update(kwargs)
        return _FakeIce("u", "pwwwwwwwwwwwwwwwww")

    WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),
        use_ipv4=False,
        use_ipv6=True,
        ice_factory=_factory,
    )
    assert captured["use_ipv4"] is False
    assert captured["use_ipv6"] is True


def test_session_defaults_are_ipv6_first_dual_stack() -> None:
    """Default construction gathers both families (IPv6-first), ADR-0043."""
    captured: dict[str, object] = {}

    def _factory(**kwargs: object) -> _FakeIce:
        captured.update(kwargs)
        return _FakeIce("u", "pwwwwwwwwwwwwwwwww")

    WebRtcMediaSession(offer_setup=SetupRole("actpass"), ice_factory=_factory)
    assert captured["use_ipv6"] is True
    assert captured["use_ipv4"] is True


class _MixedFamilyIce(_FakeIce):
    """A fake ICE that returns an IPv4 host candidate BEFORE an IPv6 one."""

    @property
    def local_candidates(self) -> list[IceCandidate]:
        return [
            IceCandidate(
                foundation="candidate:1",
                component=1,
                transport="UDP",
                priority=2130706000,
                host="192.0.2.10",
                port=50000,
                type="host",
                related_address=None,
                related_port=None,
            ),
            IceCandidate(
                foundation="candidate:2",
                component=1,
                transport="UDP",
                priority=2130706431,
                host="2001:db8::10",
                port=50001,
                type="host",
                related_address=None,
                related_port=None,
            ),
        ]


def test_ice_candidates_are_ipv6_first() -> None:
    """ice_candidates lists IPv6 before IPv4 regardless of gather order (ADR-0043)."""
    ice = _MixedFamilyIce("u", "pwwwwwwwwwwwwwwwww")
    session = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"), ice_factory=lambda **_kw: ice
    )
    families = [(":" in c.address) for c in session.ice_candidates]
    # All IPv6 (True) must come before any IPv4 (False).
    assert families == sorted(families, reverse=True)
    assert families[0] is True  # IPv6 first even though it was gathered second


@pytest.mark.asyncio
async def test_run_handshake_signals_end_of_candidates_by_default() -> None:
    """run_handshake signals end-of-candidates to ICE (non-trickle peer)."""
    server_ice = _RecordingIce("svrU", "svrPwwwwwwwwwwwwww")
    client_ice = _RecordingIce("cliU", "cliPwwwwwwwwwwwwww")
    server_ice.peer, client_ice.peer = client_ice, server_ice

    server = WebRtcMediaSession(
        offer_setup=SetupRole("active"),  # => passive/server (RFC 8842, ADR-0050)
        ice_factory=lambda **_kw: server_ice,
    )
    client = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),  # => active/client (RFC 8842, ADR-0050)
        ice_factory=lambda **_kw: client_ice,
    )
    await asyncio.gather(server.prepare(), client.prepare())
    await asyncio.gather(
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
    # Non-trickle: each side signalled end-of-candidates.
    assert server_ice.end_of_candidates_signalled is True
    assert client_ice.end_of_candidates_signalled is True
    await asyncio.gather(server.close(), client.close())


@pytest.mark.asyncio
async def test_run_handshake_always_ends_candidates() -> None:
    """run_handshake ALWAYS signals end-of-candidates (ADR-0034 — no trickle-receive).

    The plugin has no in-dialog SIP-INFO transport (RFC 8840) to receive a peer's
    trickled candidates, so it must never withhold the end marker — that would hang
    ICE waiting for candidates that can never arrive. It acts on the offer's
    candidate set and ends candidates, even though it advertises trickle capability
    in the SDP answer (RFC 8838 §4.1 half-trickle).
    """
    server_ice = _RecordingIce("svrU", "svrPwwwwwwwwwwwwww")
    client_ice = _RecordingIce("cliU", "cliPwwwwwwwwwwwwww")
    server_ice.peer, client_ice.peer = client_ice, server_ice

    server = WebRtcMediaSession(
        offer_setup=SetupRole("active"),  # => passive/server (RFC 8842, ADR-0050)
        ice_factory=lambda **_kw: server_ice,
    )
    client = WebRtcMediaSession(
        offer_setup=SetupRole("actpass"),  # => active/client (RFC 8842, ADR-0050)
        ice_factory=lambda **_kw: client_ice,
    )
    await asyncio.gather(server.prepare(), client.prepare())
    await asyncio.gather(
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
    # End-of-candidates is always signalled — there is no path to receive more.
    assert server_ice.end_of_candidates_signalled is True
    assert client_ice.end_of_candidates_signalled is True
    await asyncio.gather(server.close(), client.close())


# ---------------------------------------------------------------------------
# Outbound offerer mode (ADR-0049): for_outbound_offer() constructs the session
# as the ICE-CONTROLLING DTLS CLIENT (a=setup:active) so place_call can carry
# OUR own WebRTC offer over WSS.
# ---------------------------------------------------------------------------


def test_for_outbound_offer_is_ice_controlling_and_active() -> None:
    """The outbound offerer is ICE-CONTROLLING (RFC 8445) and DTLS active (client)."""
    captured: dict[str, object] = {}

    def _factory(**kw: object) -> _FakeIce:
        captured.update(kw)
        return _FakeIce("ourUfrag01", "ourPwd012345678901234567")

    session = WebRtcMediaSession.for_outbound_offer(ice_factory=_factory)
    # The offerer is ICE-controlling (we drive nomination on outbound).
    assert captured["ice_controlling"] is True
    # We offer a concrete active role => we are the DTLS CLIENT (send ClientHello).
    assert session.setup.value == "active"


@pytest.mark.asyncio
async def test_outbound_offerer_and_answerer_complete_dtls() -> None:
    """An outbound offerer (active/controlling) + an answerer key matching SRTP.

    The offerer is built via for_outbound_offer() (a=setup:active, ICE-controlling);
    the answerer answers an active offer as passive (DTLS server). The real DTLS
    handshake completes over the linked fake ICE pipe and the SRTP pair cross-decrypts.
    """
    offerer_ice, answerer_ice = _linked_ice(
        ("offU", "offPwwwwwwwwwwwwww"), ("ansU", "ansPwwwwwwwwwwwwww")
    )
    offerer = WebRtcMediaSession.for_outbound_offer(
        ice_factory=lambda **_kw: offerer_ice
    )
    # The answerer sees our active offer => it answers passive (DTLS server).
    answerer = WebRtcMediaSession(
        offer_setup=SetupRole("active"),
        ice_factory=lambda **_kw: answerer_ice,
    )
    await asyncio.gather(offerer.prepare(), answerer.prepare())
    assert offerer.setup.value == "active"
    assert answerer.setup.value == "passive"

    (o_in, o_out), (a_in, a_out) = await asyncio.gather(
        offerer.run_handshake(
            peer_fingerprint=answerer.fingerprint,
            peer_ice_ufrag=answerer.ice_ufrag,
            peer_ice_pwd=answerer.ice_pwd,
        ),
        answerer.run_handshake(
            peer_fingerprint=offerer.fingerprint,
            peer_ice_ufrag=offerer.ice_ufrag,
            peer_ice_pwd=offerer.ice_pwd,
        ),
    )
    for s in (o_in, o_out, a_in, a_out):
        assert isinstance(s, SrtpSession)

    pkt = RtpPacket(
        payload_type=111, sequence_number=7, timestamp=0, ssrc=0x33, payload=b"out"
    )
    assert a_in.unprotect(o_out.protect(pkt)).payload == b"out"
    await asyncio.gather(offerer.close(), answerer.close())
