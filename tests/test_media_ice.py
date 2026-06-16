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
from hermes_voip.media.ice import (  # noqa: E402 — after importorskip guard
    IceCandidate,
    IceConnection,
    IceSelectedPair,
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
    assert cand.foundation == "1"
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
