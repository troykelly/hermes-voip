"""H.264 RTP packetisation + outbound video sender (RFC 6184, ADR-0044).

Outbound WebRTC video is a **pre-encoded H.264 Annex-B file** the operator
supplies; this module packetises those bytes per RFC 6184 and loops them over the
BUNDLE'd DTLS-SRTP video stream. There is **no in-process encoder** — the named
PyPI bindings do not exist and the system-library ``ctypes`` route corrupts the
process heap (ADR-0044 Context), so everything here is pure byte manipulation on
operator-supplied bytes. ``tests/test_video_rtp.py`` asserts this module imports
no codec library, so that foot-gun cannot be re-introduced.

Pieces:

* :func:`split_annex_b_nals` / :func:`read_annex_b_nals` — Annex-B (3- and 4-byte
  start-code) framing → a list of NAL units.
* :func:`group_access_units` — group NALs into access units (coded picture +
  the parameter sets that precede it).
* :class:`H264Packetiser` — single-NAL / STAP-A / FU-A packetisation (RFC 6184
  §5.6/§5.7.1/§5.8) on a 90 kHz clock, marker on the last packet of an access
  unit, FU-A indicator preserving the NAL header's F/NRI bits.
* :class:`RtpVideoSender` — loops the pre-packetised access units, SRTP-protects
  each RTP packet, and writes it to the BUNDLE'd ICE datagram pipe.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from hermes_voip.rtp import RtpPacket

__all__ = [
    "H264_PAYLOAD_TYPE",
    "VIDEO_CLOCK_RATE",
    "H264Packetiser",
    "RtpVideoSender",
    "group_access_units",
    "read_annex_b_nals",
    "split_annex_b_nals",
]

#: The standard RTP video clock (RFC 3551 §4): 90 kHz.
VIDEO_CLOCK_RATE = 90000
#: The dynamic RTP payload type we use for the answered H.264 stream. The answer's
#: ``a=rtpmap`` binds this to ``H264/90000``; the value matches what
#: ``sdp.negotiate_video_h264`` chose from the offer (see the adapter wiring).
H264_PAYLOAD_TYPE = 96

# RFC 6184 NAL/packetisation types.
_NAL_TYPE_MASK = 0x1F
_NAL_HEADER_F_MASK = 0x80
_NAL_HEADER_NRI_MASK = 0x60
_FU_A_TYPE = 28
_STAP_A_TYPE = 24
_FU_HEADER_START = 0x80
_FU_HEADER_END = 0x40
# H.264 VCL NAL unit types (coded slices): 1..5 (RFC 6184 §1.3 / H.264 Table 7-1).
_VCL_NAL_TYPES = frozenset(range(1, 6))
# A VCL NAL needs at least a 1-byte NAL header + 1 RBSP byte for us to read its
# slice header's leading first_mb_in_slice bit (H.264 §7.4.1.2.4).
_NAL_MIN_SLICE_LEN = 2
# first_mb_in_slice is an Exp-Golomb ue(v); value 0 is the single bit `1`, so the
# first RBSP byte (after the NAL header) has its MSB set when first_mb_in_slice==0.
_RBSP_LEADING_ONE_BIT = 0x80
# STAP-A aggregation-unit size prefix is a 16-bit length (RFC 6184 §5.7.1).
_STAP_A_SIZE_PREFIX = 2
_U16_MAX = 0xFFFF
_SEQ_MOD = 1 << 16
_U32 = 0xFFFFFFFF

# Conservative default RTP payload budget (bytes) below the typical 1500-byte MTU,
# leaving room for IP/UDP + the SRTP auth tag + the DTLS/ICE 5-tuple overhead.
_DEFAULT_MTU_PAYLOAD = 1100


def split_annex_b_nals(stream: bytes) -> list[bytes]:
    """Split an H.264 Annex-B elementary stream into its NAL units.

    Recognises both the 4-byte (``00 00 00 01``) and 3-byte (``00 00 01``) start
    codes (H.264 Annex B). Empty regions between adjacent start codes are dropped.
    A stream with no start code yields an empty list.

    Args:
        stream: The raw Annex-B bytes.

    Returns:
        The NAL units in stream order, each *without* its start code.
    """
    nals: list[bytes] = []
    n = len(stream)
    i = 0
    start: int | None = None  # offset of the current NAL's first byte
    while i < n:
        # A start code is 00 00 01 (3-byte) or 00 00 00 01 (4-byte).
        if i + 3 <= n and stream[i] == 0 and stream[i + 1] == 0 and stream[i + 2] == 1:
            if start is not None:
                nal = stream[start:i]
                if nal:
                    nals.append(nal)
            i += 3
            start = i
            continue
        if (
            i + 4 <= n
            and stream[i] == 0
            and stream[i + 1] == 0
            and stream[i + 2] == 0
            and stream[i + 3] == 1
        ):
            if start is not None:
                nal = stream[start:i]
                if nal:
                    nals.append(nal)
            i += 4
            start = i
            continue
        i += 1
    if start is not None:
        nal = stream[start:n]
        if nal:
            nals.append(nal)
    return nals


def read_annex_b_nals(path: Path) -> list[bytes]:
    """Read an H.264 Annex-B file and split it into NAL units.

    Args:
        path: The Annex-B (``.h264``/``.264``) file the operator supplied via
            ``HERMES_VOIP_VIDEO_SOURCE_PATH``.

    Returns:
        The NAL units in file order (see :func:`split_annex_b_nals`).

    Raises:
        FileNotFoundError: If ``path`` does not exist (propagated, rule 37).
    """
    return split_annex_b_nals(path.read_bytes())


def _nal_type(nal: bytes) -> int:
    """The NAL unit type (low 5 bits of the 1-byte NAL header)."""
    return nal[0] & _NAL_TYPE_MASK


def _starts_new_primary_picture(nal: bytes) -> bool:
    """Whether a VCL NAL begins a new primary coded picture (H.264 §7.4.1.2.4).

    The slice header's first field is ``first_mb_in_slice``, an Exp-Golomb
    ``ue(v)``: the codeword for value 0 is the single bit ``1``, so the first
    RBSP byte (immediately after the 1-byte NAL header) has its MSB set. The
    *first* slice of a coded picture has ``first_mb_in_slice == 0``; later slices
    of the SAME picture carry ``first_mb_in_slice > 0`` (MSB clear). A short or
    malformed VCL NAL with no slice-header byte is treated as NOT a new picture,
    so it stays in the current access unit rather than spuriously splitting it.
    """
    return len(nal) >= _NAL_MIN_SLICE_LEN and bool(nal[1] & _RBSP_LEADING_ONE_BIT)


def group_access_units(nals: Sequence[bytes]) -> list[list[bytes]]:
    """Group NAL units into access units — one *primary coded picture* each.

    An access unit is one coded picture plus the non-VCL NALs (SPS/PPS, SEI, AUD)
    that precede it. A coded picture may carry MORE THAN ONE VCL slice NAL (a
    multi-slice picture); all slices of one picture share a single access unit, so
    the packetiser gives them ONE RTP timestamp and ONE marker (RFC 6184 §5.1).

    A new access unit begins at:

    * a VCL NAL that starts a new primary coded picture
      (:func:`_starts_new_primary_picture`, ``first_mb_in_slice == 0``) once the
      current access unit already holds a coded picture; or
    * a non-VCL NAL (AUD/SPS/PPS/SEI) arriving *after* a coded picture — the
      delimiter and parameter sets belong to the FOLLOWING picture.

    Trailing non-VCL NALs with no following coded picture are dropped (they carry
    no displayable picture on their own).

    Args:
        nals: NAL units in decode order.

    Returns:
        A list of access units, each a non-empty list of NAL units containing
        exactly one primary coded picture (one or more VCL slice NALs).
    """
    units: list[list[bytes]] = []
    pending: list[bytes] = []
    seen_vcl = False
    for nal in nals:
        if not nal:
            continue
        is_vcl = _nal_type(nal) in _VCL_NAL_TYPES
        # Close the current access unit when, holding a coded picture already, we
        # reach the next picture's first slice or a non-VCL NAL that belongs to it.
        if seen_vcl and ((is_vcl and _starts_new_primary_picture(nal)) or not is_vcl):
            units.append(pending)
            pending = []
            seen_vcl = False
        pending.append(nal)
        if is_vcl:
            seen_vcl = True
    if seen_vcl:
        units.append(pending)
    # A trailing non-VCL run (no following coded picture) carries no picture; drop.
    return units


class H264Packetiser:
    """Packetise H.264 NAL units into RTP packets per RFC 6184.

    Holds the outbound sequence-number counter (wrapping at 16 bits) and the
    stream SSRC + payload type. The timestamp is supplied per access unit by the
    caller (the 90 kHz clock advances per frame), so the packetiser is reusable
    across access units while keeping a strictly-monotonic sequence number.

    Args:
        ssrc: The video stream's synchronisation source.
        mtu_payload: The maximum RTP *payload* length (bytes) before a NAL is
            FU-A fragmented. Defaults to a conservative sub-MTU budget.
        initial_sequence: The first RTP sequence number (random in production,
            fixed in tests).
        payload_type: The dynamic RTP payload type (default
            :data:`H264_PAYLOAD_TYPE`).
    """

    def __init__(
        self,
        *,
        ssrc: int,
        mtu_payload: int = _DEFAULT_MTU_PAYLOAD,
        initial_sequence: int = 0,
        payload_type: int = H264_PAYLOAD_TYPE,
    ) -> None:
        """Initialise the packetiser; see the class docstring for the arguments."""
        if mtu_payload < _STAP_A_SIZE_PREFIX + 1:
            msg = f"mtu_payload too small for any packet: {mtu_payload}"
            raise ValueError(msg)
        self._ssrc = ssrc & _U32
        self._mtu = mtu_payload
        self._seq = initial_sequence & (_SEQ_MOD - 1)
        self._pt = payload_type

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) % _SEQ_MOD
        return seq

    def _packet(self, payload: bytes, *, timestamp: int, marker: bool) -> RtpPacket:
        return RtpPacket(
            payload_type=self._pt,
            sequence_number=self._next_seq(),
            timestamp=timestamp & _U32,
            ssrc=self._ssrc,
            payload=payload,
            marker=marker,
        )

    def _packetise_nal(self, nal: bytes) -> list[bytes]:
        """Return the RTP *payloads* for one NAL (single-NAL or FU-A fragments)."""
        if len(nal) <= self._mtu:
            return [nal]
        return self._fu_a_payloads(nal)

    def _fu_a_payloads(self, nal: bytes) -> list[bytes]:
        """Fragment one oversized NAL into FU-A payloads (RFC 6184 §5.8)."""
        header = nal[0]
        f_nri = header & (_NAL_HEADER_F_MASK | _NAL_HEADER_NRI_MASK)
        nal_type = header & _NAL_TYPE_MASK
        fu_indicator = f_nri | _FU_A_TYPE
        body = nal[1:]  # the NAL header byte is not carried in FU-A bodies
        # Each FU-A packet spends 2 bytes on the FU indicator + FU header.
        chunk = self._mtu - 2
        if chunk < 1:
            msg = "mtu_payload too small for FU-A fragmentation"
            raise ValueError(msg)
        payloads: list[bytes] = []
        offset = 0
        total = len(body)
        while offset < total:
            fragment = body[offset : offset + chunk]
            is_first = offset == 0
            is_last = offset + len(fragment) >= total
            fu_header = nal_type
            if is_first:
                fu_header |= _FU_HEADER_START
            if is_last:
                fu_header |= _FU_HEADER_END
            payloads.append(bytes([fu_indicator, fu_header]) + fragment)
            offset += len(fragment)
        return payloads

    def payloads_for_access_unit(self, nals: Sequence[bytes]) -> list[bytes]:
        """The loop-invariant RTP payload bytes for one access unit.

        Each NAL becomes a single-NAL payload (when it fits ``mtu_payload``) or a
        run of FU-A fragment payloads (when it does not). These bytes depend ONLY
        on the NAL bytes + the MTU budget — not on sequence number, timestamp, or
        marker — so for a static source they can be computed ONCE and reused on
        every loop iteration (ADR-0044 steady-state cost; rule 22). Empty NALs are
        skipped.

        Args:
            nals: The NAL units of one access unit, in decode order.

        Returns:
            The RTP payloads, in send order (no RTP header applied yet).
        """
        payloads: list[bytes] = []
        for nal in nals:
            if not nal:
                continue
            payloads.extend(self._packetise_nal(nal))
        return payloads

    def wrap_payloads(
        self, payloads: Sequence[bytes], *, timestamp: int
    ) -> list[RtpPacket]:
        """Wrap one access unit's precomputed payloads into RTP packets.

        Assigns the next sequence number to each packet, the shared 90 kHz
        ``timestamp``, and the RTP **marker bit** on the **last** packet only
        (RFC 6184 §5.1: the marker signals the end of the coded picture). An empty
        access unit yields no packets.

        Args:
            payloads: The RTP payloads of one access unit (see
                :meth:`payloads_for_access_unit`).
            timestamp: The 90 kHz RTP timestamp for this access unit.

        Returns:
            The RTP packets, in send order.
        """
        last = len(payloads) - 1
        return [
            self._packet(payload, timestamp=timestamp, marker=idx == last)
            for idx, payload in enumerate(payloads)
        ]

    def packetise_access_unit(
        self, nals: Sequence[bytes], *, timestamp: int
    ) -> list[RtpPacket]:
        """Packetise one access unit's NAL units into RTP packets.

        Each NAL becomes a single-NAL packet (when it fits ``mtu_payload``) or a
        run of FU-A fragments (when it does not). The RTP **marker bit** is set on
        the **last** packet of the access unit only (RFC 6184 §5.1: the marker
        signals the end of a coded picture). All packets share ``timestamp``.

        Args:
            nals: The NAL units of one access unit, in decode order.
            timestamp: The 90 kHz RTP timestamp for this access unit.

        Returns:
            The RTP packets, in send order.
        """
        return self.wrap_payloads(
            self.payloads_for_access_unit(nals), timestamp=timestamp
        )

    def aggregate_stap_a(
        self, nals: Sequence[bytes], *, timestamp: int, marker: bool = False
    ) -> RtpPacket:
        """Aggregate small NALs (e.g. SPS+PPS) into one STAP-A packet (§5.7.1).

        The STAP-A header byte takes ``F`` = the OR of the aggregated NALs' F bits
        (RFC 6184 §5.7.1: set when any aggregated NAL had a forbidden_zero_bit),
        ``NRI`` = the max of the aggregated NALs' NRI, and type 24. Each
        aggregation unit is a 16-bit big-endian size followed by the NAL bytes.
        The caller keeps the total within the MTU (parameter sets are tiny).

        Args:
            nals: The NAL units to aggregate (each must fit a 16-bit size).
            timestamp: The 90 kHz RTP timestamp.
            marker: The RTP marker bit (usually ``False`` for a parameter-set
                STAP-A that is not the last packet of an access unit).

        Returns:
            One STAP-A RTP packet.

        Raises:
            ValueError: If ``nals`` is empty or a NAL exceeds the 16-bit size.
        """
        if not nals:
            msg = "aggregate_stap_a requires at least one NAL"
            raise ValueError(msg)
        max_nri = max((nal[0] & _NAL_HEADER_NRI_MASK) for nal in nals)
        # RFC 6184 §5.7.1: the aggregation packet's F bit is the OR of the
        # aggregated NALs' F bits; NRI is the maximum of their NRI values.
        f_bit = max((nal[0] & _NAL_HEADER_F_MASK) for nal in nals)
        stap_header = f_bit | max_nri | _STAP_A_TYPE
        body = bytearray([stap_header])
        for nal in nals:
            if len(nal) > _U16_MAX:
                msg = f"NAL too large for STAP-A aggregation: {len(nal)} bytes"
                raise ValueError(msg)
            body += len(nal).to_bytes(_STAP_A_SIZE_PREFIX, "big")
            body += nal
        return self._packet(bytes(body), timestamp=timestamp, marker=marker)


class _SrtpProtect(Protocol):
    """The outbound SRTP encrypt surface (``media.srtp.SrtpSession.protect``)."""

    def protect(self, packet: RtpPacket) -> bytes:
        """Encrypt + authenticate one RTP packet into SRTP wire bytes."""
        ...


class _IceSend(Protocol):
    """The async datagram-send surface of ``media.ice.IceConnection`` (BUNDLE)."""

    async def send(self, data: bytes) -> None:
        """Send one datagram over the nominated ICE pair."""
        ...


class RtpVideoSender:
    """Loop a pre-packetised H.264 source over the BUNDLE'd SRTP + ICE pipe.

    The NALs are grouped into access units AND packetised into their (loop-
    invariant) RTP payloads ONCE at construction; each loop iteration only wraps
    those precomputed payloads in RTP packets (advancing the 90 kHz timestamp per
    frame + a fresh sequence number), SRTP-``protect``-s each, and writes it to
    the ICE pipe — the identical seam the audio engine uses (ADR-0044 §4: BUNDLE
    shares the ICE 5-tuple + the DTLS handshake, so no second gather/handshake).
    Re-fragmenting the static source every frame would be wasted work on a
    forever loop (rule 22), so the FU-A/single-NAL split is done exactly once.

    The looping task is driven by :meth:`run` (paced at ``fps``); :meth:`stop`
    ends it. :meth:`send_loop_once` sends exactly one pass over the source (used
    in tests and once per loop iteration), so the behaviour is deterministic and
    unit-testable without wall-clock waits.

    Args:
        nals: The source NAL units (from :func:`read_annex_b_nals`).
        srtp: The video SRTP session (DTLS-derived; protects outbound packets).
        ice: The connected ICE pipe shared with audio (BUNDLE).
        ssrc: The video stream SSRC (distinct from audio).
        fps: The source frame rate; the per-frame timestamp increment is
            ``VIDEO_CLOCK_RATE // fps``.
        mtu_payload: The RTP payload budget before FU-A fragmentation.
        initial_sequence: The first RTP sequence number.
        payload_type: The negotiated dynamic payload type.
    """

    def __init__(  # noqa: PLR0913 — independent per-stream config (source/SRTP/ICE/SSRC/rate/MTU/PT)
        self,
        *,
        nals: Sequence[bytes],
        srtp: _SrtpProtect,
        ice: _IceSend,
        ssrc: int,
        fps: int,
        mtu_payload: int = _DEFAULT_MTU_PAYLOAD,
        initial_sequence: int = 0,
        payload_type: int = H264_PAYLOAD_TYPE,
    ) -> None:
        """Initialise the sender; see the class docstring for the arguments."""
        if fps <= 0:
            msg = f"fps must be positive, got {fps}"
            raise ValueError(msg)
        self._access_units = group_access_units(nals)
        self._srtp = srtp
        self._ice = ice
        self._fps = fps
        self._ts_step = VIDEO_CLOCK_RATE // fps
        self._packetiser = H264Packetiser(
            ssrc=ssrc,
            mtu_payload=mtu_payload,
            initial_sequence=initial_sequence,
            payload_type=payload_type,
        )
        # Precompute each access unit's loop-invariant RTP payloads ONCE: the
        # source is static, so its FU-A/single-NAL split never changes across
        # loops. Each send only wraps these in RTP packets (seq/ts/marker), not
        # re-fragment them every frame on the forever loop (rule 22).
        self._frames: list[list[bytes]] = [
            self._packetiser.payloads_for_access_unit(unit)
            for unit in self._access_units
        ]
        # Wall-clock 90 kHz timestamp accumulator (advances across loops).
        self._timestamp = 0
        self._stop = asyncio.Event()

    async def send_loop_once(self) -> None:
        """Send one full pass over the source (every access unit once).

        Each access unit's precomputed payloads are wrapped in RTP packets,
        SRTP-protected, and written to the ICE pipe; the 90 kHz timestamp advances
        by ``VIDEO_CLOCK_RATE // fps`` per access unit. An empty source sends
        nothing.
        """
        for payloads in self._frames:
            packets = self._packetiser.wrap_payloads(
                payloads, timestamp=self._timestamp
            )
            for packet in packets:
                await self._ice.send(self._srtp.protect(packet))
            self._timestamp = (self._timestamp + self._ts_step) & _U32

    async def run(self) -> None:
        """Loop the source on the wire until :meth:`stop` is called.

        Paces frames at ``1 / fps`` between access units. Returns immediately
        when the source is empty (nothing to send). Errors from SRTP/ICE
        propagate (rule 37) — a send failure aborts the sender, it is not
        swallowed.
        """
        if not self._frames:
            return
        frame_interval = 1.0 / self._fps
        while not self._stop.is_set():
            for payloads in self._frames:
                if self._stop.is_set():
                    return
                packets = self._packetiser.wrap_payloads(
                    payloads, timestamp=self._timestamp
                )
                for packet in packets:
                    await self._ice.send(self._srtp.protect(packet))
                self._timestamp = (self._timestamp + self._ts_step) & _U32
                await asyncio.sleep(frame_interval)

    def stop(self) -> None:
        """Signal :meth:`run` to stop after the current frame (idempotent)."""
        self._stop.set()
