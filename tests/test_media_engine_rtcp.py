"""Engine RTCP integration: SR/RR build, inbound parse, stats, periodic loop.

RTCP (RFC 3550 §6) on the media engine, ADR-0061. The engine builds a Sender
Report when it has sent media (a Receiver Report when receive-only), parses
inbound peer reports to derive round-trip time, feeds a per-source
:class:`ReceptionStats` from the inbound RTP stream, and runs a periodic sender on
the §6.2 interval — all DETERMINISTIC: an injected NTP clock, an injected pacing
clock + sleep, and a fake RTCP sink. No real sockets, no wall-clock, no threads.

The LIVE wiring (constructing the RTCP socket/destination per the negotiated mux
and starting :meth:`run_rtcp` on the event loop) is the ADAPTER's job (ADR-0061);
these tests exercise the capability through the injectable seams.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
import time
from typing import Final

import pytest

from hermes_voip.media.engine import (
    OUTBOUND_AUDIO_SSRC,
    Codec,
    RtpMediaTransport,
)
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.rtcp import (
    Bye,
    ReceiverReport,
    ReportBlock,
    RtcpError,
    SenderReport,
    SourceDescription,
    build_compound,
    compact_ntp_now,
    parse_compound,
    to_ntp,
)
from hermes_voip.rtp import RtpPacket

_G711_RATE = 8000
_PTIME_MS = 20
_SAMPLES_PER_FRAME = (_G711_RATE * _PTIME_MS) // 1000  # 160
_PEER_SSRC = 0x12345678


class _RtcpSink:
    """Records every RTCP datagram the engine emits (the injectable RTCP sink)."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def __call__(self, data: bytes) -> None:
        self.sent.append(data)


class _FakeClock:
    """A deterministic monotonic seconds clock + a sleep that advances it."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, secs: float) -> None:
        if secs > 0:
            self.t += secs


def _g711_frame(n_frames: int = 1) -> PcmFrame:
    """``n_frames`` whole 20 ms frames of PCM16 silence at 8 kHz."""
    n = n_frames * _SAMPLES_PER_FRAME
    return PcmFrame(samples=b"\x00\x00" * n, sample_rate=_G711_RATE, monotonic_ts_ns=0)


def _make_engine(
    *,
    rtcp_send: _RtcpSink | None = None,
    ntp_clock: object | None = None,
    pace_clock: object | None = None,
    local_port: int = 0,
) -> RtpMediaTransport:
    kwargs: dict[str, object] = {
        "local_address": "127.0.0.1",
        "local_port": local_port,
        "remote_address": "127.0.0.1",
        "remote_port": 5004,
        "codec": Codec.PCMU,
        "initial_seq": 1000,
        "initial_ts": 0,
        "cname": "hermes@host.invalid",
    }
    if rtcp_send is not None:
        kwargs["rtcp_send"] = rtcp_send
    if ntp_clock is not None:
        kwargs["ntp_clock"] = ntp_clock
    if pace_clock is not None:
        kwargs["pace_clock"] = pace_clock
    return RtpMediaTransport(**kwargs)  # type: ignore[arg-type]  # test seam: kwargs mirror the constructor


# ---------------------------------------------------------------------------
# Deterministic non-muxed RTCP setup (flake-hardening, not test-weakening).
#
# The non-muxed path binds a SIBLING RTCP socket on RTP-port+1 (RFC 3550 §11).
# With the engine bound to port 0 the OS picks the RTP port but does NOT reserve
# port+1, so under full-suite load another test's ephemeral socket can already
# hold RTP-port+1; start_rtcp then catches the bind OSError and DEGRADES the call
# to RTCP-off (rule 5), leaving ``_rtcp_local_port`` None — which made
# ``assert rtcp_port is not None`` flake as ``assert None is not None`` in CI
# (full-suite, main 4a9cadb). This is a port-isolation flake in the TEST, not a
# production race: degrading on a real port collision is correct (and covered by
# test_start_rtcp_non_muxed_socket_failure_degrades_call_stays_up).
#
# The fix removes the collision window WITHOUT weakening any assertion: probe a
# FREE CONSECUTIVE (P, P+1) pair, bind the engine to P, then start_rtcp(mux=False)
# so it binds the (just-confirmed-free) P+1 itself — exercising the real
# production bind + reader. The residual race (P+1 taken between probe-release and
# the engine's bind) is closed by a bounded retry over fresh pairs against a real
# deadline; failure to bind within the deadline RAISES (never a silent skip).
# ---------------------------------------------------------------------------

_RTCP_SETUP_DEADLINE_SECS: Final[float] = 5.0


def _reserve_consecutive_udp_pair(host: str) -> tuple[int, socket.socket]:
    """Find a free ``(P, P+1)`` UDP pair on ``host``; return ``P`` + a held P+1 socket.

    Binds an ephemeral probe to discover ``P``, then binds ``P+1``; on success the
    probe is released (so the engine can bind ``P``) while the returned socket KEEPS
    ``P+1`` reserved, shrinking the window in which a sibling test could steal it.
    Retries with fresh ephemeral ports until a consecutive pair is free.

    Raises:
        OSError: If no consecutive pair can be reserved before the deadline (so a
            genuine inability to bind surfaces, never a silent skip — rule 19/37).
    """
    deadline = time.monotonic() + _RTCP_SETUP_DEADLINE_SECS
    last_exc: OSError | None = None
    while time.monotonic() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((host, 0))
            rtp_port = probe.getsockname()[1]
            sibling = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sibling.bind((host, rtp_port + 1))
            except OSError as exc:  # P+1 already taken — try another ephemeral P.
                last_exc = exc
                sibling.close()
                continue
        finally:
            probe.close()
        # P is now free (probe released) and P+1 is held by ``sibling``.
        return rtp_port, sibling
    msg = "could not reserve a free consecutive UDP port pair before deadline"
    raise OSError(msg) from last_exc


async def _connect_non_muxed_rtcp_engine(
    *, remote_rtcp_addr: tuple[str, int] = ("127.0.0.1", 5005)
) -> RtpMediaTransport:
    """Connect an engine and activate non-muxed RTCP with its sibling socket LIVE.

    Returns an engine on which ``start_rtcp(mux=False)`` genuinely bound the sibling
    RTCP socket on RTP-port+1 and started its reader — i.e. ``_rtcp_local_port`` is
    non-None. Deterministic under full-suite load: it reserves a free consecutive
    port pair, binds the engine's RTP socket to ``P``, releases the ``P+1``
    reservation, then starts RTCP (which binds ``P+1``). If that residual window is
    lost to a sibling test the engine degrades to RTCP-off; this retries on a fresh
    pair against a real deadline rather than returning a half-set-up engine.

    Raises:
        OSError: If RTCP could not be activated on a real sibling socket before the
            deadline (surfacing a genuine bind failure, never weakening the caller's
            ``assert _rtcp_local_port is not None`` into a pass — rule 19).
    """
    host = "127.0.0.1"
    deadline = time.monotonic() + _RTCP_SETUP_DEADLINE_SECS
    while True:
        rtp_port, sibling = _reserve_consecutive_udp_pair(host)
        engine = _make_engine(local_port=rtp_port)
        try:
            await engine.connect()
        except OSError:
            # The sub-ms window where P itself was stolen after the probe released it.
            # Free the P+1 reservation and retry on a fresh pair (until the deadline).
            sibling.close()
            if time.monotonic() >= deadline:
                raise
            continue
        # Release the P+1 reservation only now, immediately before start_rtcp binds
        # it — minimising the window a sibling test could steal it.
        sibling.close()
        await engine.start_rtcp(
            mux=False, rtp_payload_types=(), remote_rtcp_addr=remote_rtcp_addr
        )
        if engine._rtcp_local_port is not None:
            return engine
        # Residual race: P+1 was taken between release and the engine's bind, so RTCP
        # degraded to off. Tear down and retry on a fresh pair until the deadline.
        await engine.stop()
        if time.monotonic() >= deadline:
            msg = "could not activate non-muxed RTCP on a sibling socket in time"
            raise OSError(msg)


def _rtp_datagram(*, seq: int, ts: int, ssrc: int = _PEER_SSRC) -> bytes:
    """A minimal inbound RTP datagram (PCMU, one frame of silence)."""
    byte0 = 0x80
    byte1 = 0  # PCMU payload type 0, no marker
    header = struct.pack("!BBHII", byte0, byte1, seq, ts, ssrc)
    return header + b"\x00" * _SAMPLES_PER_FRAME


# ---------------------------------------------------------------------------
# build_rtcp_report: SR vs RR selection + contents (RFC 3550 §6.4)
# ---------------------------------------------------------------------------


def test_no_report_before_any_media() -> None:
    """With nothing sent and nothing received, there is no report to build yet."""
    engine = _make_engine()
    assert engine.build_rtcp_report() is None


@pytest.mark.asyncio
async def test_builds_sender_report_after_sending_media() -> None:
    """Once we have sent RTP, the periodic report is a compound SR + SDES.

    The SR's sender SSRC is our outbound SSRC, packet/octet counts reflect the
    media sent, and the compound includes an SDES CNAME (RFC 3550 §6.1).
    """
    clock = _FakeClock()
    engine = _make_engine(pace_clock=clock.monotonic)
    engine._sleep = clock.sleep
    engine._transport = _CaptureTransport()
    # Send two 20 ms frames so the sender counters advance.
    await engine.send_audio(_g711_frame(2))

    report = engine.build_rtcp_report()
    assert report is not None
    packets = parse_compound(report)
    assert isinstance(packets[0], SenderReport)
    sr = packets[0]
    assert sr.ssrc == OUTBOUND_AUDIO_SSRC
    assert sr.packet_count == 2
    assert sr.octet_count == 2 * _SAMPLES_PER_FRAME  # PCMU: 1 octet per sample
    # The compound carries an SDES with our CNAME (RFC 3550 §6.1 / §6.5).
    sdes = next(p for p in packets if isinstance(p, SourceDescription))
    assert sdes.chunks[0].ssrc == OUTBOUND_AUDIO_SSRC
    assert sdes.chunks[0].cname == "hermes@host.invalid"


def test_builds_receiver_report_when_receive_only() -> None:
    """Receiving RTP but sending none yields a compound RR + SDES (RFC 3550 §6.4.2).

    The RR carries a report block for the received source with its highest
    sequence number — proof the inbound RTP fed the per-source statistics.
    """
    engine = _make_engine()
    # Feed a few inbound RTP packets through the stats tap (no media sent).
    for i in range(4):
        engine._note_rtp_received(  # white-box: the inbound-RTP stats tap
            seq=100 + i, rtp_timestamp=160 * i, arrival_ts=0.02 * i, ssrc=_PEER_SSRC
        )
    report = engine.build_rtcp_report()
    assert report is not None
    packets = parse_compound(report)
    assert isinstance(packets[0], ReceiverReport)
    rr = packets[0]
    assert rr.ssrc == OUTBOUND_AUDIO_SSRC
    assert len(rr.report_blocks) == 1
    block = rr.report_blocks[0]
    assert block.ssrc == _PEER_SSRC
    assert block.extended_highest_seq == 103
    assert block.cumulative_lost == 0


def test_receiver_report_block_reflects_loss() -> None:
    """A gap in the received sequence shows up as loss in the RR block (A.3)."""
    engine = _make_engine()
    # seq 100,101,102 then 105 — packets 103 and 104 are missing.
    for seq, i in ((100, 0), (101, 1), (102, 2), (105, 5)):
        engine._note_rtp_received(
            seq=seq, rtp_timestamp=160 * i, arrival_ts=0.02 * i, ssrc=_PEER_SSRC
        )
    report = engine.build_rtcp_report()
    assert report is not None
    rr = parse_compound(report)[0]
    assert isinstance(rr, ReceiverReport)
    assert rr.report_blocks[0].cumulative_lost == 2
    assert rr.report_blocks[0].fraction_lost > 0


# ---------------------------------------------------------------------------
# ingest_rtcp: inbound parsing, RTT, and quality stats (RFC 3550 §6.4.1)
# ---------------------------------------------------------------------------


def test_ingest_peer_sender_report_records_lsr_for_our_next_block() -> None:
    """Receiving a peer SR sets the LSR/DLSR our next report block carries (§6.4.1).

    After we ingest the peer's SR (at a known NTP time) and then receive one of its
    RTP packets, our RR block about that source reports a non-zero LSR (the middle
    32 bits of the peer's SR NTP) — i.e. we acknowledge their SR back to them.
    """
    ntp_t = {"now": 1_700_000_000.0}
    engine = _make_engine(ntp_clock=lambda: ntp_t["now"])
    # We must have a source to report on first.
    engine._note_rtp_received(seq=100, rtp_timestamp=0, arrival_ts=0.0, ssrc=_PEER_SSRC)
    peer_sr = SenderReport(
        ssrc=_PEER_SSRC,
        ntp_timestamp=to_ntp(1_699_999_999.0),
        rtp_timestamp=8000,
        packet_count=50,
        octet_count=8000,
        report_blocks=(),
    )
    engine.ingest_rtcp(build_compound((peer_sr,)))
    report = engine.build_rtcp_report()
    assert report is not None
    rr = parse_compound(report)[0]
    assert isinstance(rr, ReceiverReport)
    # LSR is the middle 32 bits of the peer's SR NTP (RFC 3550 §6.4.1).
    expected_lsr = (to_ntp(1_699_999_999.0) >> 16) & 0xFFFFFFFF
    assert rr.report_blocks[0].lsr == expected_lsr


def test_ingest_peer_receiver_report_yields_round_trip_time() -> None:
    """A peer RR echoing our SR (via LSR/DLSR) gives us the round-trip time (§6.4.1).

    We send an SR at NTP T (recording the compact NTP). The peer's RR reports on
    our SSRC with lsr = that compact NTP and a small dlsr; ingesting it at T+0.4 s
    sets call_quality.rtt to ~0.4 s.
    """
    ntp_t = {"now": 1_700_000_000.0}
    clock = _FakeClock()
    engine = _make_engine(ntp_clock=lambda: ntp_t["now"], pace_clock=clock.monotonic)
    engine._sleep = clock.sleep
    engine._transport = _CaptureTransport()
    # Drive a send so an SR can be built, then build it (records our last SR NTP).
    # The peer's RR: it received our SR sent at ntp_t["now"], held it 0.05 s, then
    # replied. We ingest it 0.4 s later → RTT ≈ 0.4 - 0.05 = 0.35 s.
    our_sr_compact = compact_ntp_now(ntp_t["now"])
    engine._record_outbound_sr_ntp(our_sr_compact)  # white-box: arm the SR timestamp
    dlsr = int(0.05 * (1 << 16))
    peer_rr = ReceiverReport(
        ssrc=_PEER_SSRC,
        report_blocks=(
            ReportBlock(
                ssrc=OUTBOUND_AUDIO_SSRC,
                fraction_lost=0,
                cumulative_lost=0,
                extended_highest_seq=10,
                jitter=0,
                lsr=our_sr_compact,
                dlsr=dlsr,
            ),
        ),
    )
    ntp_t["now"] = 1_700_000_000.4  # 0.4 s later
    engine.ingest_rtcp(build_compound((peer_rr,)))
    rtt = engine.call_quality.rtt_seconds
    assert rtt is not None
    assert abs(rtt - 0.35) < 0.01


def test_ingest_updates_loss_and_jitter_quality_from_peer_report() -> None:
    """A peer report block updates the call-quality loss/jitter the SLOs read.

    The peer tells us (about OUR outbound stream) a fraction lost and jitter; the
    engine surfaces them on call_quality so the adapter/SLO catalogue can read the
    far-end view of our media.
    """
    engine = _make_engine()
    peer_rr = ReceiverReport(
        ssrc=_PEER_SSRC,
        report_blocks=(
            ReportBlock(
                ssrc=OUTBOUND_AUDIO_SSRC,
                fraction_lost=128,  # half
                cumulative_lost=42,
                extended_highest_seq=1000,
                jitter=240,  # 8 kHz clock units → 30 ms
                lsr=0,
                dlsr=0,
            ),
        ),
    )
    engine.ingest_rtcp(build_compound((peer_rr,)))
    q = engine.call_quality
    assert q.remote_fraction_lost == pytest.approx(128 / 256)
    assert q.remote_cumulative_lost == 42
    # 240 clock units / 8000 Hz = 0.03 s = 30 ms.
    assert q.remote_jitter_ms == pytest.approx(30.0, abs=0.01)


def test_ingest_malformed_rtcp_raises() -> None:
    """A structurally broken RTCP datagram propagates an error (rule 37)."""
    engine = _make_engine()
    with pytest.raises(RtcpError):
        engine.ingest_rtcp(b"\x80\xc8\x00\x06\x00\x00\x00\x01")  # truncated SR


def test_ingest_ignores_bye_without_error() -> None:
    """An inbound BYE is parsed without raising (it is a clean control packet).

    Strengthened: first ingest an SR/RR to establish call_quality state, snapshot it,
    ingest the BYE, and assert call_quality is UNCHANGED. This catches mutations that
    reset/zero call_quality on BYE (rule 25: failing-then-passing test detects the fix).
    """
    engine = _make_engine()
    # Establish call_quality state with a peer RR about our outbound stream.
    peer_rr = ReceiverReport(
        ssrc=_PEER_SSRC,
        report_blocks=(
            ReportBlock(
                ssrc=OUTBOUND_AUDIO_SSRC,
                fraction_lost=128,  # half
                cumulative_lost=42,
                extended_highest_seq=1000,
                jitter=240,  # 8 kHz clock units → 30 ms
                lsr=0,
                dlsr=0,
            ),
        ),
    )
    engine.ingest_rtcp(build_compound((peer_rr,)))
    # Snapshot the quality state after ingesting the RR.
    quality_before = engine.call_quality
    assert quality_before.remote_fraction_lost is not None
    assert quality_before.remote_cumulative_lost == 42
    # Ingest the BYE without raising.
    engine.ingest_rtcp(build_compound((Bye(ssrcs=(_PEER_SSRC,), reason="bye"),)))
    # Assert call_quality is UNCHANGED after BYE (mutation zeroing it must FAIL).
    quality_after = engine.call_quality
    assert quality_after.remote_fraction_lost == quality_before.remote_fraction_lost
    assert quality_after.remote_cumulative_lost == quality_before.remote_cumulative_lost
    assert quality_after.remote_jitter_ms == quality_before.remote_jitter_ms
    assert quality_after.rtt_seconds == quality_before.rtt_seconds


# ---------------------------------------------------------------------------
# Periodic RTCP loop (RFC 3550 §6.2) — deterministic, no threads/sockets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rtcp_loop_sends_periodically_then_stops() -> None:
    """run_rtcp emits a report each interval and exits cleanly on stop().

    Deterministic: an injected sleep advances a fake clock, so N intervals pass in
    N awaited sleeps with no wall-clock. The loop honours the §6.2 interval (here
    >= the 5 s minimum) and stops when the engine's stop event is set.
    """
    clock = _FakeClock()
    sink = _RtcpSink()
    engine = _make_engine(rtcp_send=sink, pace_clock=clock.monotonic)
    engine._transport = _CaptureTransport()
    # Send media so each tick builds an SR.
    engine._sleep = clock.sleep
    await engine.send_audio(_g711_frame(1))

    async def rtcp_sleep(secs: float) -> None:
        # Advance the fake clock; after 3 ticks, signal stop so the loop exits.
        clock.t += secs
        if len(sink.sent) >= 3:
            engine._stop_event.set()

    task = asyncio.create_task(engine.run_rtcp(sleep=rtcp_sleep))
    await asyncio.wait_for(task, timeout=1.0)
    # At least 3 RTCP datagrams went to the sink, each a parseable compound.
    assert len(sink.sent) >= 3
    for datagram in sink.sent:
        packets = parse_compound(datagram)
        assert packets  # non-empty compound


@pytest.mark.asyncio
async def test_rtcp_loop_muxes_over_transport_when_no_sink_given() -> None:
    """With no rtcp_send injected, RTCP is sent over the RTP transport (rtcp-mux).

    The muxed datagram lands on the same _transport.sendto the RTP path uses — the
    RFC 5761 muxed case the engine defaults to when the adapter does not supply a
    separate-port sink.
    """
    clock = _FakeClock()
    engine = _make_engine(pace_clock=clock.monotonic)
    engine._sleep = clock.sleep
    capture = _CaptureTransport()
    engine._transport = capture
    await engine.send_audio(_g711_frame(1))
    n_rtp = len(capture.sent)

    async def rtcp_sleep(secs: float) -> None:
        # First sleep returns → loop sends one report; stop only once a report
        # has actually gone out (so exactly one RTCP datagram is emitted).
        clock.t += secs
        if len(capture.sent) > n_rtp:
            engine._stop_event.set()

    await asyncio.wait_for(engine.run_rtcp(sleep=rtcp_sleep), timeout=1.0)
    # One more datagram (the RTCP compound) was sent over the RTP transport.
    assert len(capture.sent) == n_rtp + 1
    rtcp_datagram = capture.sent[-1][0]
    packets = parse_compound(rtcp_datagram)
    assert isinstance(packets[0], SenderReport)


@pytest.mark.asyncio
async def test_start_rtcp_does_not_inherit_a_no_op_pacing_sleep() -> None:
    """start_rtcp runs the RTCP loop on the real wall clock, not the pacing stub.

    Regression (rule 25): start_rtcp started run_rtcp WITHOUT a sleep arg, so the
    loop inherited ``engine._sleep`` — which callers (e.g. the e2e fake gateway)
    legitimately stub to a no-op for instant outbound RTP pacing. The §6.2 interval
    then never elapsed, the loop SPUN, and it starved the call's audio TX (a real
    e2e hang). start_rtcp must pin ``sleep=asyncio.sleep``. Here ``_sleep`` is a
    no-op that only yields (no wall time): a real-clock loop stays parked on its
    multi-second first interval and emits NOTHING in sub-ms test time, while a loop
    that inherited the stub floods the sink.
    """
    sink = _RtcpSink()
    engine = _make_engine(rtcp_send=sink)  # unsecured ⇒ start_rtcp activates RTCP
    engine._transport = _CaptureTransport()

    async def _no_op_pacing_sleep(_secs: float) -> None:
        # Models the e2e's instant outbound-pacing sleep: returns at once (no wall
        # time) but yields, so a spinning RTCP loop floods rather than hangs the test.
        await asyncio.sleep(0)

    engine._sleep = _no_op_pacing_sleep
    # Send one frame so build_rtcp_report() yields an SR; without prior media it
    # returns None and the loop emits nothing, hiding the spin (a false GREEN).
    await engine.send_audio(_g711_frame(1))
    await engine.start_rtcp(mux=True, rtp_payload_types=())
    try:
        # 200 instant event-loop turns: a wall-clock loop is still parked on its first
        # multi-second §6.2 interval (no real time has passed), so the sink stays empty.
        for _ in range(200):
            await asyncio.sleep(0)
        assert sink.sent == [], (
            "start_rtcp inherited the no-op pacing sleep and spun: "
            f"{len(sink.sent)} RTCP datagrams emitted in sub-millisecond test time"
        )
    finally:
        task = engine._rtcp_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_start_rtcp_refuses_mux_for_an_rfc5761_conflict_payload_type() -> None:
    """start_rtcp(mux=True) leaves RTCP dormant when an RTP PT is in 64-95 (RFC 5761).

    Engine-side last-line guard (codex review): a payload type in 64-95 aliases the
    RTCP packet-type byte on a muxed stream, so the RTP/RTCP demux would be ambiguous.
    The adapter already refuses mux for such PTs; the engine refuses too — it must NOT
    start the loop (``_rtcp_active`` stays False, no task). A PT outside the range
    activates normally (positive control).
    """
    engine = _make_engine()  # unsecured ⇒ start_rtcp would otherwise activate
    await engine.start_rtcp(mux=True, rtp_payload_types=(72,))  # 72 ∈ 64-95
    assert engine._rtcp_active is False
    assert engine._rtcp_task is None
    # Positive control (a fresh engine — PCMU(0)/PCMA(8)/dynamic(96) are outside 64-95).
    ok_engine = _make_engine()
    await ok_engine.start_rtcp(mux=True, rtp_payload_types=(0, 8, 96))
    try:
        assert ok_engine._rtcp_active is True
        assert ok_engine._rtcp_task is not None
    finally:
        await ok_engine.stop()


@pytest.mark.asyncio
async def test_stop_restores_constructor_rtcp_send_dropping_a_socket_lambda() -> None:
    """stop() restores the constructor RTCP sink, dropping a non-mux socket lambda.

    Lifecycle (codex review): the non-muxed start_rtcp installs an ``_rtcp_send``
    lambda bound to the sibling socket; stop() (and connect()) must restore the
    constructor value so a REUSED engine never sends RTCP through the previous,
    now-closed socket.
    """
    engine = _make_engine()  # constructor rtcp_send is None (the muxed prod default)

    def _doomed_socket_sink(_datagram: bytes) -> None:
        # Stands in for the non-mux sibling-socket lambda start_rtcp installs.
        return None

    engine._rtcp_send = _doomed_socket_sink
    await engine.stop()
    assert engine._rtcp_send is None


@pytest.mark.asyncio
async def test_rtcp_loop_sends_bye_on_stop_when_muxed() -> None:
    """Stopping the loop after media flushes a final RTCP BYE (RFC 3550 §6.6).

    A BYE tells the peer our SSRC is leaving, so it stops reporting on us promptly.
    """
    clock = _FakeClock()
    sink = _RtcpSink()
    engine = _make_engine(rtcp_send=sink, pace_clock=clock.monotonic)
    engine._transport = _CaptureTransport()
    engine._sleep = clock.sleep
    await engine.send_audio(_g711_frame(1))

    async def rtcp_sleep(secs: float) -> None:
        clock.t += secs
        engine._stop_event.set()

    await asyncio.wait_for(
        engine.run_rtcp(sleep=rtcp_sleep, send_bye_on_stop=True), timeout=1.0
    )
    # The last datagram is a COMPOUND that leads with SR/RR (RFC 3550 §6.1 — a BYE
    # is never sent standalone) and whose final packet is a BYE for our SSRC.
    last = parse_compound(sink.sent[-1])
    assert isinstance(last[0], (SenderReport, ReceiverReport))
    assert isinstance(last[-1], Bye)
    assert OUTBOUND_AUDIO_SSRC in last[-1].ssrcs


def test_rtcp_interval_is_at_least_five_seconds() -> None:
    """The engine's RTCP interval honours the RFC 3550 §6.2 5 s minimum."""
    engine = _make_engine()
    # Even with our default tiny 2-party session, the interval floors at 5 s.
    assert engine.rtcp_interval(randomize=False) >= 5.0


# ---------------------------------------------------------------------------
# Inbound RTP feeds the stats automatically (the _inbound_gen tap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_rtp_feeds_reception_stats() -> None:
    """Real inbound RTP through inbound_audio() populates the RR report block.

    End-to-end of the receive path: datagrams arrive, decode to audio, AND update
    the per-source statistics, so a report built afterwards reflects what was
    received (highest sequence number) — the stats tap is wired into _inbound_gen,
    not only reachable via the white-box helper.
    """
    engine = _make_engine()
    engine._transport = _CaptureTransport()
    # Push three inbound RTP datagrams directly onto the recv queue.
    remote = ("127.0.0.1", 5004)
    for i in range(3):
        engine._recv_queue.put_nowait((_rtp_datagram(seq=200 + i, ts=160 * i), remote))

    gen = engine.inbound_audio()
    frames = []
    for _ in range(3):
        frames.append(await asyncio.wait_for(gen.__anext__(), timeout=1.0))
    assert len(frames) == 3

    report = engine.build_rtcp_report()
    assert report is not None
    rr = parse_compound(report)[0]
    assert isinstance(rr, ReceiverReport)
    assert rr.report_blocks[0].ssrc == _PEER_SSRC
    assert rr.report_blocks[0].extended_highest_seq == 202


# ---------------------------------------------------------------------------
# stop() awaits the registered RTCP loop task (codex review, ADR-0061)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_awaits_and_cancels_registered_rtcp_task() -> None:
    """stop() cancels AND awaits a registered run_rtcp task so it never dangles.

    The adapter registers the loop task on the engine; stop() must cancel it and
    await it to completion (so the BYE flush in run_rtcp's finally runs and no
    'task was destroyed but it is pending' warning is left). After stop() the task
    is done and the engine no longer holds it.
    """
    engine = _make_engine()
    await engine.connect()
    started = asyncio.Event()

    async def _never(_secs: float) -> None:
        started.set()
        await asyncio.Event().wait()  # block forever until cancelled

    task = asyncio.create_task(engine.run_rtcp(sleep=_never))
    engine._rtcp_task = task
    await asyncio.wait_for(started.wait(), timeout=1.0)  # loop is parked in sleep

    await engine.stop()
    assert task.done()
    assert engine._rtcp_task is None


class _CaptureTransport:
    """A DatagramTransport stand-in recording (data, addr) for each sendto."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int] | None]] = []

    def sendto(self, data: bytes, addr: tuple[str, int] | None = None) -> None:
        self.sent.append((bytes(data), addr))

    def close(self) -> None:
        """No-op: owns no socket."""

    def is_closing(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# start_rtcp: the adapter-activation entry point (ADR-0061 §"Adapter activation")
#
# The adapter calls start_rtcp() AFTER connect(); it (1) chooses the RTCP
# transport from the negotiated mux — muxed rides the RTP transport, non-muxed
# opens a sibling socket on RTP-port+1 — (2) registers the run_rtcp loop task on
# the engine so stop() cancels it, and (3) engages the inbound muxed-RTCP demux.
# ---------------------------------------------------------------------------


def _peer_rr_about_us(*, fraction_lost: int = 128, cumulative_lost: int = 7) -> bytes:
    """A compound RTCP datagram the peer would send us (an RR about our SSRC)."""
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


@pytest.mark.asyncio
async def test_start_rtcp_muxed_registers_loop_task_and_rides_rtp_transport() -> None:
    """start_rtcp(mux=True) registers a run_rtcp task and muxes over the RTP path.

    On the muxed (RFC 5761) path the engine sends RTCP over its existing RTP
    transport (no separate socket), and the loop task is registered on the engine
    so stop() cancels it.
    """
    engine = _make_engine()
    await engine.connect()
    engine._transport = _CaptureTransport()  # deterministic, no real socket
    await engine.start_rtcp(mux=True, rtp_payload_types=())
    try:
        assert engine._rtcp_task is not None
        # Muxed: no separate RTCP sink is installed (RTCP rides _transport).
        assert engine._rtcp_send is None
    finally:
        await engine.stop()
    assert engine._rtcp_task is None


@pytest.mark.asyncio
async def test_start_rtcp_non_muxed_opens_sibling_socket_on_rtp_port_plus_one() -> None:
    """start_rtcp(mux=False) opens an RTCP socket on RTP-port+1 and installs a sink.

    RFC 3550 §11: when RTCP is not multiplexed it travels on the odd port one above
    the RTP port. The engine binds that sibling socket and routes outbound RTCP to
    the peer's RTCP address through it (so RTCP never lands on the RTP socket).
    """
    engine = await _connect_non_muxed_rtcp_engine()
    try:
        assert engine._rtcp_task is not None
        # A separate sink was installed (NOT muxed over the RTP transport).
        assert engine._rtcp_send is not None
        # The sibling RTCP socket is bound one above the RTP port (RFC 3550 §11).
        assert engine._rtcp_local_port == engine.local_port + 1
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_inbound_muxed_rtcp_datagram_reaches_ingest_not_audio() -> None:
    """A muxed inbound RTCP datagram is demuxed to ingest_rtcp, not decoded as audio.

    RFC 5761 §4: on a muxed stream the second byte (the RTP M+PT byte aliases the
    RTCP packet-type byte) discriminates — 200..204 is RTCP. When RTCP is active the
    engine routes such a datagram to ingest_rtcp (updating call_quality) and never
    yields it as a PcmFrame.
    """
    engine = _make_engine()
    await engine.connect()
    engine._transport = _CaptureTransport()
    await engine.start_rtcp(mux=True, rtp_payload_types=())
    try:
        remote = ("127.0.0.1", 5004)
        # One real audio packet and one muxed RTCP packet, interleaved.
        engine._recv_queue.put_nowait((_rtp_datagram(seq=300, ts=0), remote))
        engine._recv_queue.put_nowait((_peer_rr_about_us(), remote))
        engine._recv_queue.put_nowait((_rtp_datagram(seq=301, ts=160), remote))

        gen = engine.inbound_audio()
        # Exactly TWO audio frames come out (the RTCP datagram is NOT yielded).
        f1 = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        f2 = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert f1.sample_rate == _G711_RATE
        assert f2.sample_rate == _G711_RATE
        # The RTCP datagram reached ingest_rtcp: the peer's loss is on call_quality.
        assert engine.call_quality.remote_fraction_lost == pytest.approx(128 / 256)
        assert engine.call_quality.remote_cumulative_lost == 7
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_inbound_muxed_rtcp_inert_when_rtcp_not_started() -> None:
    """Without start_rtcp the muxed demux is OFF: an RTCP-typed datagram is dropped.

    Zero-regression guarantee: an engine on which the adapter never activated RTCP
    behaves exactly as before — an RTCP-typed datagram is an unknown payload type and
    is dropped (never fed to ingest_rtcp, never decoded as audio).
    """
    engine = _make_engine()
    await engine.connect()
    engine._transport = _CaptureTransport()
    try:
        remote = ("127.0.0.1", 5004)
        engine._recv_queue.put_nowait((_peer_rr_about_us(), remote))
        engine._recv_queue.put_nowait((_rtp_datagram(seq=400, ts=0), remote))
        gen = engine.inbound_audio()
        frame = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert frame.sample_rate == _G711_RATE
        # RTCP was NOT ingested (no peer report absorbed) — quality stays unset.
        assert engine.call_quality.remote_fraction_lost is None
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_inbound_malformed_muxed_rtcp_is_dropped_not_fatal() -> None:
    """A structurally broken muxed RTCP datagram is logged + dropped, not fatal.

    Per ingest_rtcp's contract the adapter decides how to handle a bad inbound RTCP
    datagram; the engine's muxed demux treats it like a malformed RTP datagram —
    drop the one packet and keep the call alive (the next audio packet still flows).
    """
    engine = _make_engine()
    await engine.connect()
    engine._transport = _CaptureTransport()
    await engine.start_rtcp(mux=True, rtp_payload_types=())
    try:
        remote = ("127.0.0.1", 5004)
        # A datagram whose 2nd byte is an RTCP PT (200) but is truncated garbage.
        engine._recv_queue.put_nowait((b"\x80\xc8\x00\x06bad", remote))
        engine._recv_queue.put_nowait((_rtp_datagram(seq=500, ts=0), remote))
        gen = engine.inbound_audio()
        frame = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert frame.sample_rate == _G711_RATE
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_non_muxed_inbound_rtcp_socket_feeds_ingest() -> None:
    """The sibling RTCP socket pumps inbound RTCP into ingest_rtcp (non-muxed path).

    With mux=False the engine reads its RTCP socket and feeds each datagram to
    ingest_rtcp, so a peer report sent to RTP-port+1 updates call_quality. Sent over
    a real loopback UDP socket (the engine bound a real one) to prove the reader.
    """
    engine = await _connect_non_muxed_rtcp_engine()
    try:
        rtcp_port = engine._rtcp_local_port
        assert rtcp_port is not None
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(
                _peer_rr_about_us(cumulative_lost=11), ("127.0.0.1", rtcp_port)
            )
            # Poll a bounded real deadline for the reader to ingest the datagram.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if engine.call_quality.remote_cumulative_lost is not None:
                    break
        finally:
            sender.close()
        assert engine.call_quality.remote_cumulative_lost == 11
    finally:
        await engine.stop()


# ---------------------------------------------------------------------------
# Codex review fixes (ADR-0061 adapter activation, fresh-context review)
# ---------------------------------------------------------------------------


class _IdentitySrtp:
    """A minimal SRTP stand-in: protect/unprotect are byte-for-byte identity.

    Structurally satisfies BOTH the engine's ``_SrtpProtect``
    (``protect(RtpPacket) -> bytes``) and ``_SrtpUnprotect``
    (``unprotect(bytes) -> RtpPacket``) Protocols — so it can be assigned to the
    engine's ``_srtp_in``/``_srtp_out`` slots with no cast or type-ignore — without
    pulling in the real cryptography backend. Only the PRESENCE of those attributes
    (which marks the engine secured) matters for the RTCP-refusal tests, not the
    transform.
    """

    def protect(self, packet: RtpPacket) -> bytes:
        return packet.pack()

    def unprotect(self, data: bytes) -> RtpPacket:
        return RtpPacket.parse(data)


class _FakeIcePipe:
    """A minimal ``_IceDatagramPipe`` stand-in (WebRTC marker for these tests).

    Structurally satisfies the engine's ``_IceDatagramPipe`` Protocol (async
    send/recv/close) so it needs no type-ignore. Marks the engine as carrying an
    ICE/DTLS transport without a real ICE stack: ``recv`` parks forever (no inbound)
    and ``send`` records, so the engine treats it as the secured WebRTC media path;
    only ``_ice is not None`` matters for the RTCP-refusal under test.
    """

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def recv(self) -> bytes:
        await asyncio.Event().wait()  # park: no inbound for these tests
        raise AssertionError("unreachable")  # pragma: no cover

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        """No-op."""


def test_rtcp_active_does_not_engage_demux_on_non_muxed_call() -> None:
    """BLOCKING #1: a non-muxed active call must NOT demux RTCP off the RTP socket.

    RFC 3550 §11: with mux=False, RTCP arrives on the SIBLING socket only — the RTP
    socket carries pure RTP. The inbound RTP-socket demux is gated on a SEPARATE
    ``_rtcp_mux_active`` flag (true only when mux=True), not on ``_rtcp_active``
    (true for BOTH muxed and non-muxed). Without the separate flag the demux fires on
    a non-muxed call and would mis-route an RTP packet whose 2nd byte aliases an RTCP
    PT. This is a pure-state assertion on the flag the demux reads.
    """
    engine = _make_engine()
    # Simulate start_rtcp(mux=False) having set RTCP active on the non-muxed path.
    engine._rtcp_active = True
    # The inbound RTP-socket demux must read a mux-specific flag, false here.
    assert engine._rtcp_mux_active is False


@pytest.mark.asyncio
async def test_non_muxed_active_call_does_not_demux_rtcp_typed_rtp_off_rtp_socket() -> (
    None
):
    """BLOCKING #1 (behavioural): a non-muxed active call yields RTP, never ingests it.

    On the non-muxed path the engine binds a sibling RTCP socket; the RTP socket must
    stay pure RTP. A datagram arriving on the RTP recv queue whose 2nd byte happens to
    sit in the RTCP PT range (an RTP packet with a high PT + marker) must be processed
    as RTP, NOT swallowed by the muxed demux into ingest_rtcp.
    """
    engine = await _connect_non_muxed_rtcp_engine()
    try:
        remote = ("127.0.0.1", 5004)
        # A genuine peer RR (2nd byte 201) delivered on the RTP socket. On a non-muxed
        # call this must NOT be demuxed to ingest_rtcp (RTCP comes via the sibling
        # socket). It is an unknown PT for the audio path, so it is simply dropped —
        # the key assertion is that it is NOT ingested as RTCP.
        engine._recv_queue.put_nowait((_peer_rr_about_us(cumulative_lost=42), remote))
        engine._recv_queue.put_nowait((_rtp_datagram(seq=700, ts=0), remote))
        gen = engine.inbound_audio()
        frame = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert frame.sample_rate == _G711_RATE
        # The RR on the RTP socket was NOT absorbed by the muxed demux.
        assert engine.call_quality.remote_cumulative_lost is None
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_start_rtcp_refuses_secured_engine_srtp_inbound() -> None:
    """MAJOR #3: start_rtcp is a no-op on a secured engine (SRTP inbound set).

    The engine emits/parses CLEARTEXT RTCP only — it has no SRTCP (RFC 3711 §3.4)
    transform. start_rtcp must REFUSE a secured transport rather than emit cleartext
    RTCP on an encrypted 5-tuple (leaking SSRC/CNAME/timing). The loop is never
    started and the inbound demux is never engaged. This is the seam a future SRTCP
    lane flips to ENABLE.
    """
    engine = _make_engine()
    engine._srtp_in = _IdentitySrtp()  # mark inbound as secured
    await engine.connect()
    await engine.start_rtcp(mux=True, rtp_payload_types=())
    try:
        assert engine._rtcp_task is None  # no loop started
        assert engine._rtcp_active is False  # demux not engaged
        assert engine._rtcp_mux_active is False
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_start_rtcp_refuses_secured_engine_srtp_outbound() -> None:
    """MAJOR #3: start_rtcp is a no-op on a secured engine (SRTP outbound set)."""
    engine = _make_engine()
    engine._srtp_out = _IdentitySrtp()  # mark outbound as secured
    await engine.connect()
    await engine.start_rtcp(mux=True, rtp_payload_types=())
    try:
        assert engine._rtcp_task is None
        assert engine._rtcp_active is False
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_start_rtcp_refuses_secured_engine_ice_dtls() -> None:
    """MAJOR #3: start_rtcp is a no-op on the secured WebRTC (ICE/DTLS) path.

    A WebRTC engine carries an ``ice_transport`` (and DTLS-derived SRTP). Cleartext
    RTCP must never be sent over it; start_rtcp refuses and the loop never starts.
    Constructed with the ICE pipe so the engine takes its ICE branch.
    """
    ice = _FakeIcePipe()
    # Construct directly with the ICE pipe so the engine takes its WebRTC branch
    # (the shared _make_engine helper does not expose ice_transport).
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        cname="hermes@host.invalid",
        ice_transport=ice,
    )
    engine._srtp_in = _IdentitySrtp()
    engine._srtp_out = _IdentitySrtp()
    await engine.start_rtcp(mux=True, rtp_payload_types=())
    try:
        assert engine._rtcp_task is None
        assert engine._rtcp_active is False
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_start_rtcp_non_muxed_socket_failure_degrades_call_stays_up() -> None:
    """MAJOR #5: a non-mux RTCP socket-open failure leaves the call up, RTCP off.

    The sibling RTCP socket (RTP-port+1) may be unavailable (port in use). That must
    NOT crash/reject the CALL: start_rtcp catches the OSError, closes any partial
    socket, leaves RTCP inactive, and returns so media continues. We genuinely OCCUPY
    RTP-port+1 with another bound UDP socket so the engine's real bind raises OSError.
    """
    # Reserve a free consecutive pair: bind the engine's RTP socket to P and KEEP the
    # P+1 reservation as the blocker so the engine's sibling-RTCP bind fails for real
    # (no mock). Using a reserved pair (not bind-to-0-then-grab-P+1) keeps even THIS
    # negative test deterministic under full-suite load — P+1 cannot have been stolen.
    rtp_port, blocker = _reserve_consecutive_udp_pair("127.0.0.1")
    engine = _make_engine(local_port=rtp_port)
    await engine.connect()
    try:
        # Must NOT raise — the call degrades to RTCP-off.
        await engine.start_rtcp(
            mux=False, rtp_payload_types=(), remote_rtcp_addr=("127.0.0.1", 5005)
        )
        assert engine._rtcp_active is False  # RTCP left inactive
        assert engine._rtcp_task is None  # loop never started
        assert engine._rtcp_transport is None  # no partial socket retained
        assert engine._rtcp_local_port is None
        # The media path is still alive: the engine still yields inbound audio.
        engine._recv_queue.put_nowait((_rtp_datagram(seq=900, ts=0), ("127.0.0.1", 1)))
        gen = engine.inbound_audio()
        frame = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert frame.sample_rate == _G711_RATE
    finally:
        blocker.close()
        await engine.stop()


@pytest.mark.asyncio
async def test_stop_awaits_and_clears_non_muxed_rtcp_reader() -> None:
    """MAJOR #6: stop() cancels AND awaits the RTCP reader, then clears it.

    The sibling-socket reader task must be cancelled AND awaited (mirroring the
    run_rtcp task teardown) so it cannot outlive the call (teardown race /
    send-after-close, "task was destroyed but it is pending"). After stop() the
    reader handle is cleared and the awaited task is truly done.
    """
    engine = await _connect_non_muxed_rtcp_engine()
    reader = engine._rtcp_reader
    assert reader is not None
    assert not reader.done()
    await engine.stop()
    # The reader was awaited to completion and the handle cleared.
    assert engine._rtcp_reader is None
    assert reader.done()
