"""Tests for hermes_voip.media.ice — ICE agent (RFC 8445), wrapping aioice.

TDD suite (AGENTS.md rule 18): red-first, deterministic over loopback.

Scenarios:
  (a) aioice is importable via the `webrtc` extra (importorskip guards the suite).
  (b) Candidate gathering yields at least one host candidate with non-empty host/port.
  (c) IceCandidate round-trips to/from an SDP a=candidate line verbatim.
  (d) Two in-process IceConnection agents (controlling + controlled) exchange
      ufrag/pwd + host candidates over loopback, complete connectivity checks, and
      produce a connected nominated pair; send→recv echoes bytes across the pair.
  (e) IceConnection exposes local_ufrag and local_pwd before gathering.
  (f) IceSelectedPair carries local_candidate and remote_candidate typed fields.
  (g) close() is idempotent (double-close does not raise).
  (h) Gathering without a STUN server yields host candidates only (no error).
"""

from __future__ import annotations

import asyncio

import pytest

# --------------------------------------------------------------------------
# Guard: skip the entire module when the webrtc extra (aioice) is absent.
# The default gate runs without the extra; a dedicated CI `webrtc` job
# installs it and runs these tests for real.
# --------------------------------------------------------------------------
aioice = pytest.importorskip("aioice", reason="webrtc extra (aioice) not installed")

# hermes_voip.media.ice lazy-imports aioice at construction time; the module
# itself is always importable.  We import it AFTER the importorskip so that a
# missing aioice shows as a skip, not a collection error.
import hermes_voip.media.ice as ice_mod  # noqa: E402 — after importorskip guard
from hermes_voip.media.ice import (  # noqa: E402 — after importorskip guard
    IceCandidate,
    IceConnection,
    IceSelectedPair,
    _parse_turn_url,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_pair() -> tuple[IceConnection, IceConnection]:
    """Create a controlling + controlled IceConnection pair over loopback.

    Both sides gather host-only candidates (no STUN URL), then exchange
    credentials and candidates, then connect.  Returns both connections
    ready for send/recv.
    """
    controlling = IceConnection(ice_controlling=True, stun_urls=())
    controlled = IceConnection(ice_controlling=False, stun_urls=())

    # Gather on both sides concurrently.
    await asyncio.gather(
        controlling.gather_candidates(),
        controlled.gather_candidates(),
    )

    # Cross-wire credentials.
    controlling.set_remote_credentials(controlled.local_ufrag, controlled.local_pwd)
    controlled.set_remote_credentials(controlling.local_ufrag, controlling.local_pwd)

    # Exchange all candidates (non-trickle: all at once, then end-of-candidates).
    for cand in controlling.local_candidates:
        await controlled.add_remote_candidate(cand)
    await controlled.add_remote_candidate(None)  # end-of-candidates

    for cand in controlled.local_candidates:
        await controlling.add_remote_candidate(cand)
    await controlling.add_remote_candidate(None)  # end-of-candidates

    # Run connectivity checks concurrently.
    await asyncio.gather(
        controlling.connect(),
        controlled.connect(),
    )

    return controlling, controlled


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_yields_host_candidate() -> None:
    """(b) Gathering without a STUN URL produces at least one host candidate."""
    conn = IceConnection(ice_controlling=True, stun_urls=())
    await conn.gather_candidates()
    try:
        candidates = conn.local_candidates
        assert len(candidates) >= 1
        host_candidates = [c for c in candidates if c.type == "host"]
        assert len(host_candidates) >= 1
        # Each host candidate must have a non-empty host and a positive port.
        for cand in host_candidates:
            assert cand.host  # non-empty string
            assert cand.port > 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ice_candidate_sdp_round_trip() -> None:
    """(c) IceCandidate.to_sdp() / IceCandidate.from_sdp() round-trip is exact."""
    # Canonical SDP a=candidate line (RFC 8839 §5.1 syntax, without leading 'a=').
    sdp_attr = "candidate:1 1 UDP 2130706431 192.0.2.1 5000 typ host"
    cand = IceCandidate.from_sdp(sdp_attr)
    # aioice includes the "candidate:" token in the foundation field; the full
    # value "candidate:1" is what appears as the foundation token in the
    # a=candidate line (RFC 8839 §5.1 treats the whole "candidate:<id>" as the
    # foundation string, and to_sdp() outputs it verbatim).
    assert cand.foundation == "candidate:1"
    assert cand.component == 1
    assert cand.transport == "UDP"
    assert cand.priority == 2130706431
    assert cand.host == "192.0.2.1"
    assert cand.port == 5000
    assert cand.type == "host"
    assert cand.related_address is None
    assert cand.related_port is None
    # Round-trip: the serialised form must equal the input.
    assert cand.to_sdp() == sdp_attr


@pytest.mark.asyncio
async def test_ice_candidate_srflx_sdp_round_trip() -> None:
    """(c-srflx) Server-reflexive candidate round-trips with raddr/rport fields."""
    sdp_attr = (
        "candidate:2 1 UDP 1694498815 203.0.113.5 54321 typ srflx "
        "raddr 192.168.1.2 rport 54320"
    )
    cand = IceCandidate.from_sdp(sdp_attr)
    assert cand.type == "srflx"
    assert cand.related_address == "192.168.1.2"
    assert cand.related_port == 54320
    assert cand.to_sdp() == sdp_attr


@pytest.mark.asyncio
async def test_local_credentials_before_gather() -> None:
    """(e) local_ufrag and local_pwd are available before gather_candidates."""
    conn = IceConnection(ice_controlling=True, stun_urls=())
    ufrag = conn.local_ufrag
    pwd = conn.local_pwd
    # ufrag is 4+ chars; pwd is 22+ chars (RFC 8445 §15.4 minimums).
    assert len(ufrag) >= 4
    assert len(pwd) >= 22
    # They must not be empty strings (random generation).
    assert ufrag
    assert pwd
    # Gathering must not change the credentials.
    await conn.gather_candidates()
    try:
        assert conn.local_ufrag == ufrag
        assert conn.local_pwd == pwd
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_loopback_connectivity_and_data_transfer() -> None:
    """(d) Two IceConnections complete checks over loopback; send→recv echoes bytes."""
    controlling, controlled = await _make_pair()
    try:
        # The nominated pair must be present after connect().
        assert controlling.selected_pair is not None
        assert controlled.selected_pair is not None

        # Data transfer: controlling → controlled.
        payload = b"hello-ice-loopback"
        await controlling.send(payload)
        received = await asyncio.wait_for(controlled.recv(), timeout=5.0)
        assert received == payload

        # Data transfer: controlled → controlling.
        payload2 = b"reply-from-controlled"
        await controlled.send(payload2)
        received2 = await asyncio.wait_for(controlling.recv(), timeout=5.0)
        assert received2 == payload2
    finally:
        await controlling.close()
        await controlled.close()


@pytest.mark.asyncio
async def test_selected_pair_typed_fields() -> None:
    """(f) IceSelectedPair carries typed local_candidate and remote_candidate."""
    controlling, controlled = await _make_pair()
    try:
        pair = controlling.selected_pair
        assert pair is not None
        assert isinstance(pair, IceSelectedPair)
        assert isinstance(pair.local_candidate, IceCandidate)
        assert isinstance(pair.remote_candidate, IceCandidate)
        # Both candidates must be host-type on loopback.
        assert pair.local_candidate.type == "host"
        assert pair.remote_candidate.type == "host"
    finally:
        await controlling.close()
        await controlled.close()


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    """(g) Double-close does not raise."""
    conn = IceConnection(ice_controlling=True, stun_urls=())
    await conn.close()
    await conn.close()  # must not raise


@pytest.mark.asyncio
async def test_gather_host_only_no_stun() -> None:
    """(h) Gathering without a STUN URL succeeds and yields only host candidates."""
    conn = IceConnection(ice_controlling=True, stun_urls=())
    await conn.gather_candidates()
    try:
        candidates = conn.local_candidates
        assert len(candidates) >= 1
        # Without a STUN server, only host candidates are gathered.
        non_host = [c for c in candidates if c.type != "host"]
        assert non_host == []
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# TURN relay wiring (ADR-0034)
# ---------------------------------------------------------------------------


def test_parse_turn_url_plain() -> None:
    """A turn: URL parses to (host, port, ssl=False, transport=udp); default 3478."""
    assert _parse_turn_url("turn:turn.example.test:3478") == (
        "turn.example.test",
        3478,
        False,
        "udp",
    )


def test_parse_turn_url_default_port() -> None:
    """A turn: URL without a port defaults to 3478 (RFC 8656 §5)."""
    assert _parse_turn_url("turn:turn.example.test") == (
        "turn.example.test",
        3478,
        False,
        "udp",
    )


def test_parse_turn_url_turns_tls_default_port() -> None:
    """A turns: URL => ssl=True and the default TLS port 5349."""
    assert _parse_turn_url("turns:turn.example.test") == (
        "turn.example.test",
        5349,
        True,
        "udp",
    )


def test_parse_turn_url_transport_query() -> None:
    """A ?transport=tcp query selects the TCP transport."""
    assert _parse_turn_url("turn:turn.example.test:3478?transport=tcp") == (
        "turn.example.test",
        3478,
        False,
        "tcp",
    )


def test_parse_turn_url_rejects_non_turn_scheme() -> None:
    """A non-turn scheme is rejected loudly (ValueError)."""
    with pytest.raises(ValueError, match="turn"):
        _parse_turn_url("stun:stun.example.test:3478")


def test_parse_turn_url_rejects_userinfo() -> None:
    """A turn: URL carrying userinfo is rejected (would leak embedded credentials).

    'turn:user:pass@host' must NOT parse host as 'user:pass@host'; credentials come
    only from the env vars. The error must not echo the credential material.
    """
    with pytest.raises(ValueError, match="userinfo") as excinfo:
        _parse_turn_url("turn:relay-user:relay-secret@turn.example.test:3478")
    assert "relay-secret" not in str(excinfo.value)


def test_parse_turn_url_query_error_does_not_echo_query() -> None:
    """A bad ?transport= query errors with a fixed message (no query echo)."""
    with pytest.raises(ValueError, match="transport=udp") as excinfo:
        _parse_turn_url("turn:turn.example.test:3478?transport=quic")
    assert "quic" not in str(excinfo.value)


class _SpyAioiceModule:
    """A fake ``aioice`` module that records the kwargs passed to ``Connection``.

    Injected via ``_get_aioice`` so the real aioice module is never patched and no
    socket is opened. ``Connection`` returns a bare object — ``IceConnection.__init__``
    only stores it; the test asserts on the captured kwargs.
    """

    def __init__(self) -> None:
        self.captured: dict[str, object] = {}

    def Connection(self, **kwargs: object) -> object:  # noqa: N802 — mirrors aioice.Connection
        self.captured = dict(kwargs)
        return object()


def test_turn_params_passed_to_aioice(monkeypatch: pytest.MonkeyPatch) -> None:
    """IceConnection threads TURN url+creds into aioice.Connection (ADR-0034).

    The relay credentials must reach aioice's TURN client. A fake aioice module
    captures the constructor kwargs without opening any socket.
    """
    spy = _SpyAioiceModule()
    monkeypatch.setattr(ice_mod, "_get_aioice", lambda: spy)

    IceConnection(
        ice_controlling=False,
        stun_urls=(),
        turn_urls=("turn:turn.example.test:3478",),
        turn_username="relay-user",
        turn_password="relay-secret",
    )

    assert spy.captured["turn_server"] == ("turn.example.test", 3478)
    assert spy.captured["turn_username"] == "relay-user"
    assert spy.captured["turn_password"] == "relay-secret"
    assert spy.captured["turn_ssl"] is False
    assert spy.captured["turn_transport"] == "udp"


def test_no_turn_when_urls_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no TURN URLs, aioice gets turn_server=None (no relay candidate)."""
    spy = _SpyAioiceModule()
    monkeypatch.setattr(ice_mod, "_get_aioice", lambda: spy)

    IceConnection(ice_controlling=False, stun_urls=())
    assert spy.captured["turn_server"] is None


# ---------------------------------------------------------------------------
# ICE consent freshness (RFC 7675) — aioice-native; ADR-0034 verifies + locks it.
#
# aioice already runs consent freshness internally: Connection.connect() arms a
# query_consent task (5 s interval, 6-failure → close()). These tests LOCK that
# behaviour and the consent-loss → teardown chain (close() wakes a blocked recv(),
# which the engine turns into a transport-loss teardown). No new consent machinery
# is added (rule 28): the gap was verified to NOT exist — aioice's close() already
# enqueues the queue sentinel via the protocol's connection_lost, so a blocked
# recv() raises rather than hanging (gap-analysis #6 is already covered).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_arms_consent_freshness_task() -> None:
    """The RFC 7675 consent task is armed by aioice on connect() (verified).

    Reaches into the wrapped aioice Connection to confirm the query_consent task
    is live after connect — proving consent freshness is active on every WebRTC
    call with no code of ours (the basis for ADR-0034's "do not duplicate").
    """
    controlling, controlled = await _make_pair()
    try:
        # The wrapped aioice Connection's consent task must be a live asyncio.Task.
        # aioice 0.10.2 has no public accessor for it; reach the internal attribute
        # via getattr (returns Any — no type escape hatch, no private-attr typing).
        consent_task = getattr(controlled._conn, "_query_consent_task", None)
        assert isinstance(consent_task, asyncio.Task)
        assert not consent_task.done()
    finally:
        await controlling.close()
        await controlled.close()


@pytest.mark.asyncio
async def test_close_unblocks_blocked_recv() -> None:
    """A recv() blocked when the connection closes raises (does not hang).

    This is the consent-loss teardown path: aioice's query_consent calls close()
    on consent loss, and close() must wake a recv() already parked on the queue so
    the engine ends the call. Verified here against a real aioice loopback pair.
    """
    controlling, controlled = await _make_pair()
    try:
        # Park a recv() on the controlled side (no data will ever arrive).
        recv_task = asyncio.ensure_future(controlled.recv())
        await asyncio.sleep(0)  # let the recv task start awaiting

        # Consent loss path: aioice closes the connection underneath the reader.
        await controlled.close()

        # The previously-blocked recv() must raise (sentinel woke it), not hang.
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(recv_task, timeout=5.0)
    finally:
        await controlling.close()
        await controlled.close()


@pytest.mark.asyncio
async def test_recv_after_close_raises() -> None:
    """A fresh recv() after the connection closed raises (not a silent hang)."""
    controlling, controlled = await _make_pair()
    await controlled.close()
    try:
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(controlled.recv(), timeout=5.0)
    finally:
        await controlling.close()
