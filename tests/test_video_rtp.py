"""Tests for hermes_voip.media.video_rtp — RFC 6184 H.264 RTP packetisation.

ADR-0044: outbound WebRTC video is a pre-encoded H.264 Annex-B file, packetised
per RFC 6184 (single-NAL / STAP-A / FU-A) and sent over the BUNDLE'd DTLS-SRTP
video stream. There is NO in-process encoder — the named PyPI bindings do not
exist and the system-library ctypes route corrupts the heap (ADR-0044 Context),
so this module does pure byte manipulation only.

These tests cover the Annex-B NAL splitter, the RFC 6184 packetiser (single-NAL,
FU-A boundary cases, marker placement, NAL-header F/NRI propagation, STAP-A for
the parameter sets), the 90 kHz timestamp clock, and the no-encoder-import
invariant. Fixtures are synthetic NAL bytes; no real media.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_voip.media import video_rtp
from hermes_voip.media.video_rtp import (
    H264_PAYLOAD_TYPE,
    VIDEO_CLOCK_RATE,
    H264Packetiser,
    RtpVideoSender,
    group_access_units,
    read_annex_b_nals,
    split_annex_b_nals,
)
from hermes_voip.rtp import RtpPacket

# RFC 6184 NAL unit types / packetisation NAL types.
_NAL_TYPE_FU_A = 28
_NAL_TYPE_STAP_A = 24
_NAL_TYPE_SPS = 7
_NAL_TYPE_PPS = 8
_NAL_TYPE_IDR = 5
_FU_START = 0x80
_FU_END = 0x40


def _nal(nal_type: int, *, nri: int = 3, body: bytes = b"") -> bytes:
    """Build a synthetic NAL unit: a 1-byte header (F=0, NRI, type) + body."""
    header = (0 << 7) | (nri << 5) | nal_type
    return bytes([header]) + body


# ---------------------------------------------------------------------------
# Annex-B NAL splitting
# ---------------------------------------------------------------------------


def test_split_annex_b_handles_4byte_start_codes() -> None:
    sps = _nal(_NAL_TYPE_SPS, body=b"\x01\x02")
    pps = _nal(_NAL_TYPE_PPS, body=b"\x03")
    stream = b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps
    assert split_annex_b_nals(stream) == [sps, pps]


def test_split_annex_b_handles_3byte_start_codes() -> None:
    sps = _nal(_NAL_TYPE_SPS, body=b"\xaa")
    idr = _nal(_NAL_TYPE_IDR, body=b"\xbb\xcc")
    stream = b"\x00\x00\x01" + sps + b"\x00\x00\x01" + idr
    assert split_annex_b_nals(stream) == [sps, idr]


def test_split_annex_b_mixed_start_codes() -> None:
    a = _nal(_NAL_TYPE_SPS, body=b"\x11")
    b = _nal(_NAL_TYPE_PPS, body=b"\x22")
    c = _nal(_NAL_TYPE_IDR, body=b"\x33")
    stream = b"\x00\x00\x00\x01" + a + b"\x00\x00\x01" + b + b"\x00\x00\x00\x01" + c
    assert split_annex_b_nals(stream) == [a, b, c]


def test_split_annex_b_empty_between_start_codes_is_skipped() -> None:
    nal = _nal(_NAL_TYPE_IDR, body=b"\x01")
    # Two adjacent start codes with nothing between them must not yield an empty NAL.
    stream = b"\x00\x00\x00\x01" + b"\x00\x00\x00\x01" + nal
    assert split_annex_b_nals(stream) == [nal]


def test_split_annex_b_no_start_code_returns_empty() -> None:
    assert split_annex_b_nals(b"not annex b data") == []


def test_read_annex_b_nals_reads_file(tmp_path: Path) -> None:
    sps = _nal(_NAL_TYPE_SPS, body=b"\x01")
    idr = _nal(_NAL_TYPE_IDR, body=b"\x02\x03")
    path = tmp_path / "clip.h264"
    path.write_bytes(b"\x00\x00\x00\x01" + sps + b"\x00\x00\x01" + idr)
    assert read_annex_b_nals(path) == [sps, idr]


def test_read_annex_b_nals_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_annex_b_nals(tmp_path / "nope.h264")


# ---------------------------------------------------------------------------
# Single-NAL packetisation
# ---------------------------------------------------------------------------


def test_small_nal_is_single_nal_packet() -> None:
    packetiser = H264Packetiser(ssrc=0x1234, mtu_payload=1200)
    idr = _nal(_NAL_TYPE_IDR, body=b"\x00" * 50)
    packets = packetiser.packetise_access_unit([idr], timestamp=9000)
    assert len(packets) == 1
    pkt = packets[0]
    # Single-NAL: the payload is the NAL verbatim (no aggregation/fragmentation).
    assert pkt.payload == idr
    assert pkt.payload_type == H264_PAYLOAD_TYPE
    assert pkt.timestamp == 9000
    assert pkt.ssrc == 0x1234
    # Marker on the last (here only) packet of the access unit (RFC 6184 §5.1).
    assert pkt.marker is True


def test_single_nal_marker_only_on_last_of_access_unit() -> None:
    packetiser = H264Packetiser(ssrc=1, mtu_payload=1200)
    a = _nal(_NAL_TYPE_IDR, body=b"\x01" * 10)
    b = _nal(_NAL_TYPE_IDR, body=b"\x02" * 10)
    packets = packetiser.packetise_access_unit([a, b], timestamp=0)
    assert [p.marker for p in packets] == [False, True]
    assert [p.payload for p in packets] == [a, b]


# ---------------------------------------------------------------------------
# FU-A fragmentation — boundary cases
# ---------------------------------------------------------------------------


def test_nal_exactly_at_mtu_is_single_packet() -> None:
    mtu = 64
    packetiser = H264Packetiser(ssrc=1, mtu_payload=mtu)
    # A NAL whose total length == mtu fits a single-NAL packet.
    nal = _nal(_NAL_TYPE_IDR, body=b"\x00" * (mtu - 1))
    assert len(nal) == mtu
    packets = packetiser.packetise_access_unit([nal], timestamp=0)
    assert len(packets) == 1
    assert packets[0].payload == nal


def test_nal_one_over_mtu_is_fu_a() -> None:
    mtu = 64
    packetiser = H264Packetiser(ssrc=1, mtu_payload=mtu)
    nal = _nal(_NAL_TYPE_IDR, body=b"\x00" * mtu)  # total length mtu+1 -> must split
    assert len(nal) == mtu + 1
    packets = packetiser.packetise_access_unit([nal], timestamp=0)
    assert len(packets) >= 2
    # Each fragment is a FU-A packet (type 28 in the FU indicator).
    for pkt in packets:
        assert pkt.payload[0] & 0x1F == _NAL_TYPE_FU_A


def test_fu_a_start_and_end_flags_and_reassembly() -> None:
    mtu = 32
    packetiser = H264Packetiser(ssrc=1, mtu_payload=mtu)
    body = bytes(range(100))
    nal = _nal(_NAL_TYPE_IDR, nri=2, body=body)
    packets = packetiser.packetise_access_unit([nal], timestamp=4500)
    # First fragment: S=1, E=0; last: S=0, E=1; middles: S=0, E=0.
    fu_headers = [p.payload[1] for p in packets]
    assert fu_headers[0] & _FU_START
    assert not fu_headers[0] & _FU_END
    assert fu_headers[-1] & _FU_END
    assert not fu_headers[-1] & _FU_START
    for mid in fu_headers[1:-1]:
        assert not mid & _FU_START
        assert not mid & _FU_END
    # The FU indicator preserves F (0) and NRI (2) from the original NAL header.
    for pkt in packets:
        indicator = pkt.payload[0]
        assert indicator >> 7 == 0  # F bit
        assert (indicator >> 5) & 0x03 == 2  # NRI
    # The FU header's low 5 bits carry the original NAL type (IDR=5) on every frag.
    for pkt in packets:
        assert pkt.payload[1] & 0x1F == _NAL_TYPE_IDR
    # Reassembling the fragment bodies reconstructs the original NAL payload (the
    # 1-byte NAL header is dropped; FU bodies concatenate to the rest).
    reassembled = b"".join(p.payload[2:] for p in packets)
    assert reassembled == body
    # Marker set only on the last fragment of the access unit.
    assert [p.marker for p in packets][-1] is True
    assert not any(p.marker for p in packets[:-1])
    # Timestamp constant across all fragments of one access unit.
    assert {p.timestamp for p in packets} == {4500}


def test_fu_a_fragments_have_monotonic_sequence_numbers() -> None:
    packetiser = H264Packetiser(ssrc=1, mtu_payload=32, initial_sequence=100)
    nal = _nal(_NAL_TYPE_IDR, body=bytes(range(80)))
    packets = packetiser.packetise_access_unit([nal], timestamp=0)
    seqs = [p.sequence_number for p in packets]
    assert seqs == list(range(100, 100 + len(packets)))


def test_sequence_number_wraps_at_16_bits() -> None:
    packetiser = H264Packetiser(ssrc=1, mtu_payload=1200, initial_sequence=0xFFFF)
    a = _nal(_NAL_TYPE_IDR, body=b"\x01")
    b = _nal(_NAL_TYPE_IDR, body=b"\x02")
    packets = packetiser.packetise_access_unit([a, b], timestamp=0)
    assert [p.sequence_number for p in packets] == [0xFFFF, 0]


# ---------------------------------------------------------------------------
# STAP-A aggregation of the parameter sets
# ---------------------------------------------------------------------------


def test_stap_a_aggregates_sps_and_pps() -> None:
    packetiser = H264Packetiser(ssrc=1, mtu_payload=1200)
    sps = _nal(_NAL_TYPE_SPS, body=b"\xaa\xbb")
    pps = _nal(_NAL_TYPE_PPS, body=b"\xcc")
    packet = packetiser.aggregate_stap_a([sps, pps], timestamp=0)
    # STAP-A header is one byte: F/NRI + type 24.
    assert packet.payload[0] & 0x1F == _NAL_TYPE_STAP_A
    # Body: [u16 size][nal] per aggregated unit (RFC 6184 §5.7.1).
    body = packet.payload[1:]
    off = 0
    aggregated: list[bytes] = []
    while off < len(body):
        size = int.from_bytes(body[off : off + 2], "big")
        off += 2
        aggregated.append(body[off : off + size])
        off += size
    assert aggregated == [sps, pps]


def test_stap_a_nri_is_max_of_aggregated() -> None:
    packetiser = H264Packetiser(ssrc=1, mtu_payload=1200)
    low = _nal(_NAL_TYPE_SPS, nri=1, body=b"\x01")
    high = _nal(_NAL_TYPE_PPS, nri=3, body=b"\x02")
    packet = packetiser.aggregate_stap_a([low, high], timestamp=0)
    # The STAP-A NRI must be the maximum NRI of the aggregated NALs (RFC 6184 §5.7.1).
    assert (packet.payload[0] >> 5) & 0x03 == 3


# ---------------------------------------------------------------------------
# Constants + no-encoder invariant (ADR-0044)
# ---------------------------------------------------------------------------


def test_video_clock_rate_is_90khz() -> None:
    assert VIDEO_CLOCK_RATE == 90000


# ---------------------------------------------------------------------------
# Access-unit grouping (parameter sets ride with the next coded picture)
# ---------------------------------------------------------------------------


def test_group_access_units_attaches_param_sets_to_next_vcl() -> None:
    sps = _nal(_NAL_TYPE_SPS, body=b"\x01")
    pps = _nal(_NAL_TYPE_PPS, body=b"\x02")
    idr = _nal(_NAL_TYPE_IDR, body=b"\x03")
    p_slice = _nal(1, body=b"\x04")  # non-IDR coded slice (VCL type 1)
    units = group_access_units([sps, pps, idr, p_slice])
    # SPS+PPS group with the IDR; the P-slice is its own access unit.
    assert units == [[sps, pps, idr], [p_slice]]


def test_group_access_units_single_idr() -> None:
    sps = _nal(_NAL_TYPE_SPS, body=b"\x01")
    pps = _nal(_NAL_TYPE_PPS, body=b"\x02")
    idr = _nal(_NAL_TYPE_IDR, body=b"\x03")
    assert group_access_units([sps, pps, idr]) == [[sps, pps, idr]]


# ---------------------------------------------------------------------------
# RtpVideoSender — loops pre-packetised RTP over the SRTP-protect + ICE pipe
# ---------------------------------------------------------------------------


class _FakeSrtp:
    """A protect() that tags the packet so the test can assert it was applied."""

    def __init__(self) -> None:
        self.protected: list[RtpPacket] = []

    def protect(self, packet: RtpPacket) -> bytes:
        self.protected.append(packet)
        return b"SRTP:" + packet.pack()


class _FakeIce:
    """An async send() pipe capturing the datagrams the sender writes."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_video_sender_sends_one_loop_iteration() -> None:
    sps = _nal(_NAL_TYPE_SPS, body=b"\x01")
    pps = _nal(_NAL_TYPE_PPS, body=b"\x02")
    idr = _nal(_NAL_TYPE_IDR, body=b"\x00" * 30)
    srtp = _FakeSrtp()
    ice = _FakeIce()
    sender = RtpVideoSender(
        nals=[sps, pps, idr],
        srtp=srtp,
        ice=ice,
        ssrc=0xABCD,
        fps=10,
        mtu_payload=1200,
    )
    await sender.send_loop_once()
    # Every emitted RTP packet was SRTP-protected before going on the ICE pipe.
    assert len(ice.sent) == len(srtp.protected)
    assert ice.sent, "the sender emitted at least one packet"
    # The protected packets are all on the video SSRC + payload type.
    assert all(p.ssrc == 0xABCD for p in srtp.protected)
    assert all(p.payload_type == H264_PAYLOAD_TYPE for p in srtp.protected)
    # The wire bytes are the SRTP-protected form (not plaintext RTP).
    assert all(d.startswith(b"SRTP:") for d in ice.sent)
    # The last packet of the access unit carries the marker (frame boundary).
    assert srtp.protected[-1].marker is True


@pytest.mark.asyncio
async def test_video_sender_advances_timestamp_between_frames() -> None:
    idr = _nal(_NAL_TYPE_IDR, body=b"\x00" * 10)
    p1 = _nal(1, body=b"\x01" * 10)
    srtp = _FakeSrtp()
    ice = _FakeIce()
    sender = RtpVideoSender(
        nals=[idr, p1], srtp=srtp, ice=ice, ssrc=1, fps=10, mtu_payload=1200
    )
    await sender.send_loop_once()
    timestamps = sorted({p.timestamp for p in srtp.protected})
    # Two access units at 10 fps -> the second frame's timestamp is +9000 ticks.
    assert timestamps[1] - timestamps[0] == VIDEO_CLOCK_RATE // 10


@pytest.mark.asyncio
async def test_video_sender_empty_source_sends_nothing() -> None:
    srtp = _FakeSrtp()
    ice = _FakeIce()
    sender = RtpVideoSender(
        nals=[], srtp=srtp, ice=ice, ssrc=1, fps=10, mtu_payload=1200
    )
    await sender.send_loop_once()
    assert ice.sent == []


def test_no_in_process_encoder_imported() -> None:
    """ADR-0044: the module must import NO codec/encoder library (heap-corruption).

    A regression guard so a future change cannot re-introduce the in-process
    openh264/libvpx/PyAV path that corrupts the heap.
    """
    source = video_rtp.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    banned = ("openh264", "pyopenh264", "vp8codec", "libvpx", "import av", "aiortc")
    for token in banned:
        assert token not in text, f"banned codec token in video_rtp.py: {token}"
