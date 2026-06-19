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
import struct

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
) -> RtpMediaTransport:
    kwargs: dict[str, object] = {
        "local_address": "127.0.0.1",
        "local_port": 0,
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
    """An inbound BYE is parsed without raising (it is a clean control packet)."""
    engine = _make_engine()
    engine.ingest_rtcp(build_compound((Bye(ssrcs=(_PEER_SSRC,), reason="bye"),)))


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
    # The last datagram is a compound whose final packet is a BYE for our SSRC.
    last = parse_compound(sink.sent[-1])
    assert any(isinstance(p, Bye) and OUTBOUND_AUDIO_SSRC in p.ssrcs for p in last)


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
