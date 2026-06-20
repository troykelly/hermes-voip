"""Engine secured-path RTCP via SRTCP (RFC 3711 §3.4), ADR-0061/ADR-0066.

The RTCP control channel is activated on a SECURED media path by wrapping every
outbound compound RTCP datagram in an :class:`~hermes_voip.media.srtcp.SrtcpSession`
``protect`` and unwrapping every inbound one with ``unprotect`` — so SDES / DTLS /
WebRTC calls send AUTHENTICATED+ENCRYPTED RTCP instead of leaking SSRC/CNAME/timing
in cleartext (which is why RTCP was DORMANT on secured paths before SRTCP landed).

These tests drive the engine's three SRTCP seams directly with REAL
:class:`SrtcpSession` instances (the ``media`` extra's cryptography backend):

* :meth:`RtpMediaTransport._emit_rtcp` protects outbound RTCP (a peer SrtcpSession
  decrypts it back to the cleartext compound);
* the inbound RTCP ingest path unprotects (and rejects auth-fail / replay);
* :meth:`RtpMediaTransport.start_rtcp` ACTIVATES on a secured engine **when SRTCP is
  available** (the dormancy guard flips), and stays dormant on a secured engine with
  NO SRTCP;
* the inbound muxed demux recognises secured RTCP (cleartext header, encrypted body).

All deterministic — no real sockets, no wall clock.
"""

from __future__ import annotations

import asyncio
import base64
import struct

import pytest

pytest.importorskip("cryptography")

from hermes_voip.media.engine import (
    OUTBOUND_AUDIO_SSRC,
    Codec,
    RtpMediaTransport,
)
from hermes_voip.media.srtcp import SrtcpSession
from hermes_voip.rtcp import (
    ReceiverReport,
    ReportBlock,
    build_compound,
    parse_compound,
)
from hermes_voip.sdp import CryptoAttribute

_PEER_SSRC = 0x12345678

# Two distinct 30-byte SDES master key||salt values, generated at runtime (never a
# literal secret in a tracked file — rule 34). Each is a valid AES_CM_128_HMAC_SHA1_80
# inline key (16-byte key + 14-byte salt, base64-encoded).
_KEY_A = bytes(range(30))
_KEY_B = bytes(range(100, 130))


def _crypto(raw: bytes, *, tag: int = 1) -> CryptoAttribute:
    """An AES_CM_128_HMAC_SHA1_80 a=crypto carrying ``raw`` as the inline key||salt."""
    inline = base64.b64encode(raw).decode("ascii")
    return CryptoAttribute(
        tag=tag,
        suite="AES_CM_128_HMAC_SHA1_80",
        key_params=f"inline:{inline}",
    )


def _peer_srtcp(*, raw: bytes, ssrc: int) -> SrtcpSession:
    """A peer-side SrtcpSession for ``raw``, pre-bound to ``ssrc``."""
    return SrtcpSession(_crypto(raw), ssrc=ssrc)


def _make_secured_engine(
    *,
    srtcp_inbound: SrtcpSession | None,
    srtcp_outbound: SrtcpSession | None,
) -> RtpMediaTransport:
    """A secured (SRTP-marked) engine optionally carrying SRTCP sessions.

    The SRTCP sessions are the units under test; ``_is_secured`` is satisfied because
    a non-None ``srtcp_inbound``/``srtcp_outbound`` also marks the engine secured.
    """
    return RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        initial_seq=1000,
        initial_ts=0,
        cname="hermes@host.invalid",
        srtcp_inbound=srtcp_inbound,
        srtcp_outbound=srtcp_outbound,
    )


class _RtcpSink:
    """Records every RTCP datagram the engine emits (the injectable RTCP sink)."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def __call__(self, data: bytes) -> None:
        self.sent.append(data)


def _our_rr_compound() -> bytes:
    """A cleartext compound RTCP datagram WE would emit (an RR about the peer).

    Built with our outbound SSRC as the sender so SRTCP binds to it.
    """
    rr = ReceiverReport(
        ssrc=OUTBOUND_AUDIO_SSRC,
        report_blocks=(
            ReportBlock(
                ssrc=_PEER_SSRC,
                fraction_lost=0,
                cumulative_lost=0,
                extended_highest_seq=2000,
                jitter=0,
                lsr=0,
                dlsr=0,
            ),
        ),
    )
    return build_compound((rr,))


def _peer_rr_about_us(*, fraction_lost: int = 128, cumulative_lost: int = 7) -> bytes:
    """A cleartext compound RTCP datagram the PEER would send us (an RR about us)."""
    rr = ReceiverReport(
        ssrc=_PEER_SSRC,
        report_blocks=(
            ReportBlock(
                ssrc=OUTBOUND_AUDIO_SSRC,
                fraction_lost=fraction_lost,
                cumulative_lost=cumulative_lost,
                extended_highest_seq=1000,
                jitter=0,
                lsr=0,
                dlsr=0,
            ),
        ),
    )
    return build_compound((rr,))


# ---------------------------------------------------------------------------
# Outbound: _emit_rtcp protects via SRTCP
# ---------------------------------------------------------------------------


def test_emit_rtcp_protects_outbound_via_srtcp() -> None:
    """With ``srtcp_outbound`` set, _emit_rtcp emits SRTCP, never cleartext.

    The emitted datagram is NOT the cleartext compound: it carries the SRTCP E-flag +
    index trailer + auth tag and decrypts (via a peer SrtcpSession keyed the same) back
    to the exact cleartext compound (RFC 3711 §3.4).
    """
    sink = _RtcpSink()
    tx = SrtcpSession(_crypto(_KEY_A), ssrc=OUTBOUND_AUDIO_SSRC)
    engine = _make_secured_engine(srtcp_inbound=None, srtcp_outbound=tx)
    engine._rtcp_send = sink

    cleartext = _our_rr_compound()
    engine._emit_rtcp(cleartext)

    assert len(sink.sent) == 1
    wire = sink.sent[0]
    # The wire bytes are the SRTCP transform, never the cleartext compound.
    assert wire != cleartext
    assert len(wire) > len(cleartext)  # index trailer (4) + auth tag (10) appended
    # A peer keyed the same recovers the exact cleartext compound.
    peer = _peer_srtcp(raw=_KEY_A, ssrc=OUTBOUND_AUDIO_SSRC)
    assert peer.unprotect(wire) == cleartext


def test_emit_rtcp_increments_srtcp_index() -> None:
    """Each outbound RTCP advances the SRTCP index (1, 2, …) — no IV reuse."""
    sink = _RtcpSink()
    tx = SrtcpSession(_crypto(_KEY_A), ssrc=OUTBOUND_AUDIO_SSRC)
    engine = _make_secured_engine(srtcp_inbound=None, srtcp_outbound=tx)
    engine._rtcp_send = sink

    engine._emit_rtcp(_our_rr_compound())
    engine._emit_rtcp(_our_rr_compound())
    assert len(sink.sent) == 2
    # The sender's index advanced twice (the engine drove protect() twice).
    assert tx.index == 2
    # The two wire packets carry distinct index trailers (no IV reuse).
    assert sink.sent[0] != sink.sent[1]


def test_emit_rtcp_plain_engine_unchanged_no_srtcp() -> None:
    """Negative control: a non-secured engine (no SRTCP) emits cleartext unchanged."""
    sink = _RtcpSink()
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        cname="hermes@host.invalid",
        rtcp_send=sink,
    )
    assert engine._has_srtcp is False
    cleartext = _our_rr_compound()
    engine._emit_rtcp(cleartext)
    assert sink.sent == [cleartext]  # byte-for-byte cleartext (RTCP-mux plain path)


# ---------------------------------------------------------------------------
# Inbound: the RTCP ingest path unprotects via SRTCP
# ---------------------------------------------------------------------------


def test_ingest_rtcp_unprotects_inbound_via_srtcp() -> None:
    """A secured engine with ``srtcp_inbound`` unprotects an inbound SRTCP datagram.

    The peer encrypts an RR about us; the engine unprotects + parses it, so the
    far-end loss view lands on ``call_quality`` (proving the unprotect ran).
    """
    rx = SrtcpSession(_crypto(_KEY_B), ssrc=_PEER_SSRC)
    engine = _make_secured_engine(srtcp_inbound=rx, srtcp_outbound=None)

    peer = _peer_srtcp(raw=_KEY_B, ssrc=_PEER_SSRC)
    srtcp_wire = peer.protect(_peer_rr_about_us(cumulative_lost=21))
    # Feed via the wire-ingest path (the muxed-demux + sibling-socket both call this).
    engine._ingest_rtcp_datagram(srtcp_wire)

    assert engine.call_quality.remote_cumulative_lost == 21


def test_ingest_rtcp_drops_srtcp_auth_failure() -> None:
    """A tampered SRTCP datagram is dropped on auth-fail: no raise, no state change."""
    rx = SrtcpSession(_crypto(_KEY_B), ssrc=_PEER_SSRC)
    engine = _make_secured_engine(srtcp_inbound=rx, srtcp_outbound=None)

    peer = _peer_srtcp(raw=_KEY_B, ssrc=_PEER_SSRC)
    srtcp_wire = bytearray(peer.protect(_peer_rr_about_us(cumulative_lost=21)))
    srtcp_wire[-1] ^= 0xFF  # corrupt the auth tag
    engine._ingest_rtcp_datagram(bytes(srtcp_wire))  # must not raise

    # Auth failed → nothing parsed → no far-end view recorded.
    assert engine.call_quality.remote_cumulative_lost is None


def test_ingest_rtcp_drops_srtcp_replay() -> None:
    """A replayed SRTCP datagram is rejected (the engine drops the second copy)."""
    rx = SrtcpSession(_crypto(_KEY_B), ssrc=_PEER_SSRC)
    engine = _make_secured_engine(srtcp_inbound=rx, srtcp_outbound=None)

    peer = _peer_srtcp(raw=_KEY_B, ssrc=_PEER_SSRC)
    wire = peer.protect(_peer_rr_about_us(cumulative_lost=33))
    engine._ingest_rtcp_datagram(wire)  # accepted
    assert engine.call_quality.remote_cumulative_lost == 33
    # The exact same bytes again: the SRTCP replay list rejects it (dropped, no raise).
    engine._ingest_rtcp_datagram(wire)
    assert engine.call_quality.remote_cumulative_lost == 33  # unchanged


def test_ingest_rtcp_plain_engine_parses_cleartext_unchanged() -> None:
    """Negative control: a non-secured engine still parses cleartext RTCP directly."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        cname="hermes@host.invalid",
    )
    assert engine._has_srtcp is False
    engine._ingest_rtcp_datagram(_peer_rr_about_us(cumulative_lost=9))
    assert engine.call_quality.remote_cumulative_lost == 9


# ---------------------------------------------------------------------------
# start_rtcp: the dormancy-guard FLIP (secured + SRTCP → ACTIVE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_rtcp_activates_on_secured_engine_with_srtcp() -> None:
    """THE FLIP: start_rtcp ACTIVATES on a secured engine WHEN SRTCP is available.

    Before SRTCP, ``_is_secured`` was an unconditional dormancy guard. Now a secured
    engine that carries BOTH SRTCP sessions activates RTCP — the loop starts and the
    muxed demux engages — because outbound RTCP is SRTCP-protected and inbound is
    SRTCP-unprotected (no cleartext on the wire).
    """
    tx = SrtcpSession(_crypto(_KEY_A), ssrc=OUTBOUND_AUDIO_SSRC)
    rx = SrtcpSession(_crypto(_KEY_B), ssrc=_PEER_SSRC)
    engine = _make_secured_engine(srtcp_inbound=rx, srtcp_outbound=tx)
    await engine.connect()
    await engine.start_rtcp(mux=True, rtp_payload_types=(0, 8))
    try:
        assert engine._has_srtcp is True
        assert engine._rtcp_active is True  # the loop is live
        assert engine._rtcp_mux_active is True  # muxed demux engaged
        assert engine._rtcp_task is not None
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_start_rtcp_still_dormant_on_secured_engine_without_srtcp() -> None:
    """A secured engine with NO SRTCP stays DORMANT (cleartext RTCP would leak).

    The guard only flips when SRTCP is available. Without it (SRTP media only) the
    secured engine must NOT activate RTCP — cleartext RTCP on a secured 5-tuple is
    still forbidden.
    """
    engine = _make_secured_engine(srtcp_inbound=None, srtcp_outbound=None)
    await engine.connect()
    await engine.start_rtcp(mux=True, rtp_payload_types=(0, 8))
    try:
        assert engine._is_secured is True
        assert engine._has_srtcp is False
        assert engine._rtcp_active is False  # dormant
        assert engine._rtcp_task is None
    finally:
        await engine.stop()


# ---------------------------------------------------------------------------
# Inbound muxed demux of SECURED RTCP (cleartext header, encrypted body)
# ---------------------------------------------------------------------------


def _rtp_pcmu(*, seq: int, ts: int, ssrc: int = _PEER_SSRC) -> bytes:
    """A minimal inbound PCMU RTP datagram (one frame of silence)."""
    header = struct.pack("!BBHII", 0x80, 0, seq, ts, ssrc)
    return header + b"\x00" * 160


@pytest.mark.asyncio
async def test_secured_muxed_demux_routes_srtcp_to_ingest_not_audio() -> None:
    """A secured muxed engine demuxes an inbound SRTCP packet to ingest, not audio.

    RFC 3711 leaves the RTCP header (octets 0-7) in the clear, so the RFC 5761 §4
    second-byte discriminator (200..204) still works on a SECURED muxed stream. The
    engine routes such a datagram to the RTCP ingest path (which SRTCP-unprotects it,
    updating call_quality) and never yields it as audio.
    """
    tx = SrtcpSession(_crypto(_KEY_A), ssrc=OUTBOUND_AUDIO_SSRC)
    rx = SrtcpSession(_crypto(_KEY_B), ssrc=_PEER_SSRC)
    engine = _make_secured_engine(srtcp_inbound=rx, srtcp_outbound=tx)
    await engine.connect()
    await engine.start_rtcp(mux=True, rtp_payload_types=(0, 8))
    try:
        remote = ("127.0.0.1", 5004)
        peer = _peer_srtcp(raw=_KEY_B, ssrc=_PEER_SSRC)
        srtcp_wire = peer.protect(_peer_rr_about_us(cumulative_lost=12))
        # The second byte (RR packet type 201) is in the clear, so the muxed demux
        # recognises it as RTCP even though the body is encrypted.
        assert srtcp_wire[1] == 201
        engine._recv_queue.put_nowait((srtcp_wire, remote))
        # A following audio frame surfaces after the RTCP is consumed (not yielded).
        engine._recv_queue.put_nowait((_rtp_pcmu(seq=500, ts=0), remote))
        gen = engine.inbound_audio()
        frame = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert frame.sample_rate == 8000
        # The SRTCP packet was unprotected + parsed: the peer loss is on call_quality.
        assert engine.call_quality.remote_cumulative_lost == 12
    finally:
        await engine.stop()


def test_srtcp_compound_round_trips_through_parse() -> None:
    """Sanity: the engine's outbound SRTCP decrypts to a parseable compound.

    Ties the outbound protect to the inbound parse: protect WHAT we build, unprotect
    it on a peer, and confirm parse_compound reads our RR back (a full §3.4 loop).
    """
    sink = _RtcpSink()
    tx = SrtcpSession(_crypto(_KEY_A), ssrc=OUTBOUND_AUDIO_SSRC)
    engine = _make_secured_engine(srtcp_inbound=None, srtcp_outbound=tx)
    engine._rtcp_send = sink
    engine._emit_rtcp(_our_rr_compound())
    peer = _peer_srtcp(raw=_KEY_A, ssrc=OUTBOUND_AUDIO_SSRC)
    cleartext = peer.unprotect(sink.sent[0])
    packets = list(parse_compound(cleartext))
    assert any(isinstance(p, ReceiverReport) for p in packets)
