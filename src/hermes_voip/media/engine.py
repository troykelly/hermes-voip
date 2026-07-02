"""Asyncio UDP media plane — RtpMediaTransport (ADR-0005).

This module wires the sans-IO building blocks (RtpPacket/JitterBuffer, G.711
codec, SrtpSession) onto a real non-blocking UDP socket to produce the concrete
:class:`MediaTransport` / :class:`CallMedia` implementation for telephony calls.

Architecture:

* One ``asyncio`` DatagramTransport receives datagrams from the event loop and
  places them on an internal ``asyncio.Queue``.
* :meth:`inbound_audio` drains the queue, optionally un-protects via SRTP, feeds
  the :class:`~hermes_voip.rtp.JitterBuffer`, and decodes each ordered packet to
  a :class:`~hermes_voip.providers.audio.PcmFrame`.
* :meth:`send_audio` encodes the outbound frame, packs it into an RTP datagram,
  optionally protects it via SRTP, and sends it to the remote address.  The
  outbound stream is **deadline-paced** to one ``ptime`` per packet: each frame
  sleeps only the time remaining until its scheduled slot (measured on
  ``pace_clock``) BEFORE the encode + send, so per-frame encode cost (notably
  G.722's pure-Python encoder) is absorbed into the interval rather than added on
  top — a steady 50 pps for any codec. Both ``sleep`` and ``pace_clock`` are
  injectable so tests drive time without wall-clock delays.
* A ``clock`` callable (also injectable) stamps inbound ``PcmFrame`` objects with
  a monotonic nanosecond timestamp — downstream stages (VAD, STT) rely on this
  for gap-free presentation time.

**Plain RTP vs SRTP**: passing ``srtp_outbound`` / ``srtp_inbound``
:class:`_SrtpProtect` / :class:`_SrtpUnprotect` objects enables SRTP/SAVP;
omitting them (``None``) gives plain RTP/AVP.  Tampered or unauthenticated SRTP
packets are silently dropped — their ``SrtpError`` is logged at DEBUG level and
the event loop continues.  (A bad *incoming* packet is an environmental event,
not a programming error; dropping it is the correct behaviour here.)

**Timing seam**: ``clock`` (inbound presentation time, ns) defaults to
:func:`time.monotonic_ns`, ``pace_clock`` (outbound pacing, s) to
:func:`time.monotonic`, and ``sleep`` to :func:`asyncio.sleep`.  All are injected
by tests.  No code path calls the real clock in a way that makes determinism
impossible.

**SRTP type seam**: ``cryptography`` lives in the optional ``media`` extra and is
absent from the default mypy gate.  Rather than use ``Any`` or cast, we declare
narrow local Protocols (:class:`_SrtpProtect`, :class:`_SrtpUnprotect`) covering
only the methods the engine calls.  :class:`~hermes_voip.media.srtp.SrtpSession`
is structurally assignable to both without import — clean in both gate
environments with zero ``# type: ignore``.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import random
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

import audioop  # audioop-lts (rule 38) — used for PLC attenuation of a held frame

from hermes_voip.dtmf import (
    DtmfEvent,
    DtmfNoPress,
    DtmfPress,
    DtmfReceiver,
    DtmfSendMode,
    InbandDtmfDetector,
    event_payloads,
    inband_tone_pcm,
)
from hermes_voip.media.aec import EchoCanceller
from hermes_voip.media.audio import (
    G711_SAMPLE_RATE,
    Resampler,
    alaw_to_frame,
    frame_to_alaw,
    frame_to_ulaw,
    linear_fade_out,
    ulaw_to_frame,
)
from hermes_voip.media.g722 import (
    G722_RTP_CLOCK_RATE,
    G722_SAMPLE_RATE,
    G722Decoder,
    G722Encoder,
)

# Opus RATE constants only (plain ints — ADR-0032). Importing them at module scope
# is safe in the default (no-webrtc) gate: media.opus's opuslib/libopus import is
# lazy (inside the codec constructors), so this import never pulls opuslib in. The
# OpusEncoder/OpusDecoder CLASSES are imported lazily inside _encode/_decode, mirroring
# how the G.722 codec objects are created on first use.
from hermes_voip.media.opus import OPUS_RTP_CLOCK_RATE, OPUS_SAMPLE_RATE

# SrtpError is defined at srtp.py module level and carries no cryptography
# dependency (the cryptography backend is imported lazily inside SrtpSession),
# so importing it here is safe in the default (no-`media`-extra) environment.
# Importing it at module scope — rather than per-packet inside the inbound loop
# — means an import failure propagates at module load (rule 37) instead of
# being silently swallowed as a per-packet drop.
from hermes_voip.media.srtcp import SrtcpError
from hermes_voip.media.srtp import SrtpError
from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame
from hermes_voip.rtcp import (
    RTCP_PT_APP,
    RTCP_PT_SR,
    Bye,
    ReceiverReport,
    ReceptionStats,
    ReportBlock,
    RtcpError,
    SdesChunk,
    SenderReport,
    SourceDescription,
    build_compound,
    compact_ntp_now,
    compute_rtcp_interval,
    parse_compound,
    rtt_from_report_block,
    to_ntp,
)
from hermes_voip.rtp import JitterBuffer, Lost, RtpPacket

if TYPE_CHECKING:
    from hermes_voip.sdp import CryptoAttribute

__all__ = [
    "CallQuality",
    "Codec",
    "RtpMediaTransport",
    "UnsupportedCodecError",
    "codec_for_encoding",
]

_log = logging.getLogger(__name__)

# Default ptime in milliseconds (one packet per 20 ms = 50 pps).
_DEFAULT_PTIME_MS = 20

# Packet-loss concealment (ADR-0056). For G.711/G.722 (no in-codec PLC) a lost
# frame is concealed by repeating the last good decoded frame. The FIRST held
# frame plays at full energy (the standard G.711 Appendix-I-style repeat — the
# lost frame most resembles its immediate predecessor); each SUBSEQUENT
# consecutive loss multiplies by this factor (~-6 dB/step), and the repeat is
# forced to silence after a short run so a sustained outage fades out instead of
# droning a held tone. Opus uses its own in-band FEC / native PLC and ignores these.
_PLC_ATTENUATION_PER_FRAME: Final[float] = 0.5  # ~ -6 dB per SUBSEQUENT held frame
# After this many consecutive losses the repeat is dropped to silence (a long gap
# is better represented as quiet than as a decaying buzz). 5 frames = 100 ms at
# the standard 20 ms ptime.
_PLC_MAX_REPEAT_FRAMES: Final[int] = 5

# RFC 4733 DTMF defaults (ADR-0010/0031). A 100 ms tone with a 70 ms inter-digit
# gap is comfortably within the ITU-T Q.24 minimums (>= 40 ms tone, >= 40 ms gap)
# and is what most gateways/IVRs expect; the named-event volume is -10 dBm0.
_DEFAULT_DTMF_TONE_MS = 100
_DEFAULT_DTMF_GAP_MS = 70
_DEFAULT_DTMF_VOLUME = 10

# RFC 3550 §5.1: the initial RTP sequence number and timestamp SHOULD be random
# to make known-plaintext attacks on SRTP harder and to avoid collision with
# a prior session on the same SSRC.  Randomised at construction time; tests may
# inject fixed values via the ``initial_seq`` / ``initial_ts`` constructor kwargs
# to keep send_audio assertions deterministic.

# A fixed SSRC for the outbound stream — an obvious test fake.
# (No real PBX should assign 0xCAFEBABE; the repo is public.)
_OUTBOUND_SSRC: int = 0xCAFEBABE
# Public alias of the fixed outbound (audio) SSRC, so the WebRTC video sender
# (ADR-0044) can EXCLUDE it when randomising the BUNDLE'd video SSRC — a
# collision would confuse the shared-5-tuple demux of audio vs video.
OUTBOUND_AUDIO_SSRC: int = _OUTBOUND_SSRC

# Default SDES CNAME for the outbound RTCP source (RFC 3550 §6.5.1). The CNAME
# stably identifies our stream across an SSRC change; the adapter overrides it with
# a per-call value. It is NOT PII (no host/extension): a fixed, public-repo-safe
# label is the safe default (the live value comes from the adapter).
_DEFAULT_CNAME: Final[str] = "hermes-voip"

# RTCP transmission-interval inputs (RFC 3550 §6.2). The session "RTCP bandwidth"
# is ~5% of the media bandwidth; for a single bidirectional voice call (~80 kbit/s
# with G.711 + headers) that 5% is ~500 bytes/s. With the 2-party session this
# yields a sub-second arithmetic value that the §6.2 5 s floor then dominates, so a
# real call reports about every 5 s regardless — the default below only matters for
# the (unused here) many-party case. Bytes/s.
_DEFAULT_RTCP_BANDWIDTH: Final[float] = 500.0
# RFC 5761 §4: RTP payload types 64-95 alias the RTCP packet-type byte (an RTP M+PT
# of 200-204 is indistinguishable from SR/RR/SDES/BYE/APP) on a muxed stream, so
# RTP/RTCP cannot be demultiplexed. start_rtcp refuses mux activation for such a PT
# (the adapter also refuses it before activation — defense in depth).
_RTCP_MUX_CONFLICT_PT_MIN: Final[int] = 64
_RTCP_MUX_CONFLICT_PT_MAX: Final[int] = 95
# Seed average compound-RTCP packet size (bytes incl. UDP/IP, RFC 3550 §6.2) before
# the first report is sent; refined toward the real sizes as reports are emitted.
_INITIAL_AVG_RTCP_SIZE: Final[float] = 96.0
# RFC 3550 §6.3.3: the average RTCP size is smoothed with a 1/16 gain.
_AVG_RTCP_SIZE_GAIN: Final[float] = 1.0 / 16.0

_MS_PER_S: Final[float] = 1000.0
_RTCP_FRACTION_SCALE: Final[float] = 256.0  # fraction-lost 8-bit fixed point


@dataclass(frozen=True, slots=True)
class CallQuality:
    """Per-call media-quality snapshot derived from RTCP (RFC 3550 §6.4, ADR-0061).

    Two views of one call:

    * ``local_*`` — what WE received from the peer, computed by our own
      :class:`~hermes_voip.rtcp.ReceptionStats` from the inbound RTP stream (this is
      what our outbound report blocks carry).
    * ``remote_*`` — what the PEER reported it received from US, parsed from inbound
      RTCP report blocks about our SSRC. ``None`` until a peer report arrives.

    ``rtt_seconds`` is the round-trip time from the most recent peer report block's
    LSR/DLSR (``None`` until one arrives that acknowledges an SR of ours). These are
    the loss/jitter/RTT numbers the SLO catalogue (runbook 0014) and ADR-0056's
    concealment count consume.

    Attributes:
        local_fraction_lost: CUMULATIVE loss fraction (0..1) WE observed on the
            inbound stream over the whole call so far (lost / expected), or ``None``
            if no RTP received yet. This is a poll-anytime session view — distinct
            from the per-interval fraction RFC 3550 §6.4.1 puts in a wire report
            block (which `build_rtcp_report` computes separately, A.3).
        local_cumulative_lost: Total inbound packets lost this call (may be negative
            with duplicates), or ``None`` if no RTP received yet.
        local_jitter_ms: Our interarrival jitter estimate in milliseconds, or
            ``None`` if no RTP received yet.
        remote_fraction_lost: Loss fraction (0..1) the PEER reported on our outbound
            stream, or ``None`` if no peer report received.
        remote_cumulative_lost: Total outbound packets the peer reported lost, or
            ``None`` if no peer report received.
        remote_jitter_ms: The peer's jitter estimate on our stream in milliseconds,
            or ``None`` if no peer report received.
        rtt_seconds: Round-trip time in seconds, or ``None`` if not yet measurable.
    """

    local_fraction_lost: float | None
    local_cumulative_lost: int | None
    local_jitter_ms: float | None
    remote_fraction_lost: float | None
    remote_cumulative_lost: int | None
    remote_jitter_ms: float | None
    rtt_seconds: float | None


# Size of the inbound datagram queue (datagrams; 512 * ~180 bytes ~ 90 kB).
_QUEUE_MAXSIZE = 512

# How often to log a rolling peak amplitude for outbound TX audio.
# Every _TX_AMPLITUDE_LOG_PERIOD packets (= 1 second at 20 ms ptime, 50 pps)
# the engine emits one INFO line showing the peak amplitude seen in that window.
# This replaces the old "first 3 chunks only" approach, which always logged 0
# because the TTS synthesiser has a brief silent lead-in before actual speech.
_TX_AMPLITUDE_LOG_PERIOD: Final[int] = 50

# Default echo-canceller adaptive-filter length in milliseconds (ADR-0033), the
# engine's standalone default when no ``aec_filter_ms`` is passed (the adapter always
# passes the configured value). 64 ms so the window spans the realistic echo-return
# delay (round-trip ≈ tens of ms), not just the impulse response. The engine converts
# ms → taps at the live analysis rate and CAPS them at _AEC_MAX_TAPS.
_AEC_DEFAULT_FILTER_MS: Final[int] = 64

# Hard ceiling on the echo-canceller tap count, REGARDLESS of the configured
# ``aec_filter_ms`` x rate (rule 22). The canceller's per-sample cost is O(taps) in
# pure Python; 512 taps measures ~6.9 ms/frame at 8 kHz and ~13.8 ms at 16 kHz — both
# safely under the 20 ms ptime. Above this the media loop risks falling behind, so the
# tap count is clamped here (so the 64 ms default gives a full 64 ms window at 8 kHz
# but ~32 ms at 16 kHz — the wideband path trades echo-delay reach for the CPU budget;
# a longer 16 kHz echo needs ``aec_bulk_delay_ms``). An operator who raises
# ``aec_filter_ms`` past this on a fast host can only do so by accepting the clamp.
_AEC_MAX_TAPS: Final[int] = 512

# A received datagram paired with the UDP source address it arrived from. The
# source address is what symmetric-RTP (comedia) latching needs: we send our
# media back to wherever the peer's media ACTUALLY originates, not blindly to the
# negotiated SDP c=/m= address (which may be a private or SBC-rewritten address
# under NAT).
type _Datagram = tuple[bytes, tuple[str, int]]


class Codec(enum.Enum):
    """The audio codec for this media session (the value is the default payload type).

    PCMU/PCMA are 8 kHz narrowband G.711; G722 is 16 kHz wideband (ADR-0022); OPUS
    is 48 kHz wideband for the WebRTC wire (ADR-0032). The rate behaviour (wire
    sample rate, RTP clock rate) is NOT encoded by the enum — it lives in
    :data:`_CODEC_DESCRIPTORS`, because G.722's RTP clock (8000) differs from its
    audio sample rate (16000) per RFC 3551 (Opus's clock equals its rate, 48000).

    The enum value is the codec's DEFAULT payload type. PCMU/PCMA/G722 have static
    PTs (RFC 3551); Opus has no static PT, so its value is the conventional dynamic
    PT 111 — the wire PT is always the negotiated one (the engine threads
    ``payload_type`` separately from the codec kind).
    """

    PCMU = 0  # mu-law, RTP payload type 0 (8 kHz)
    PCMA = 8  # a-law, RTP payload type 8 (8 kHz)
    G722 = 9  # G.722 wideband, RTP payload type 9 (16 kHz audio, 8 kHz RTP clock)
    OPUS = 111  # Opus, dynamic PT (48 kHz audio + RTP clock; WebRTC — ADR-0032)


class UnsupportedCodecError(ValueError):
    """A negotiated SDP codec cannot be carried by the media engine.

    Raised by :func:`codec_for_encoding` when an ``(encoding, clock_rate)`` pair
    is not in the engine's capability table — there is NO silent fallback (the
    historical ``else -> PCMA`` mis-map answered calls the engine could not
    actually carry, yielding dead audio).

    Subclasses :class:`ValueError` so it composes with the SDP-negotiation
    failure handling (which already maps ``ValueError`` to a 488 reject). The
    message carries only the structural codec facts (encoding name + clock rate)
    for diagnostics — never a SIP host, extension, or caller number.

    Attributes:
        encoding: The offending RTP encoding name (e.g. ``G729``).
        clock_rate: The offending RTP clock rate in Hz.
    """

    def __init__(self, encoding: str, clock_rate: int) -> None:
        """Initialise with the encoding name and clock rate that cannot be carried."""
        self.encoding = encoding
        self.clock_rate = clock_rate
        super().__init__(
            f"media engine cannot carry codec {encoding}/{clock_rate} "
            f"(supported: {_supported_codec_summary()})"
        )


# The engine's capability table: the EXACT ``(uppercased encoding, clock_rate)``
# pairs the RTP encode/decode path can carry, mapped to the runnable ``Codec``.
# Both G.711 variants are 8 kHz narrowband (RFC 3551 §6); G.722 (ADR-0022) is
# wideband at the static ``G722/8000`` rtpmap — note the rtpmap clock is 8000 even
# though the audio is 16 kHz (RFC 3551 §4.5.2), so the KEY is ("G722", 8000). This
# is the single source of truth for engine capability; the adapter's SDP offer
# allow-list must never advertise an encoding absent here (the drift guard
# enforces it). To add a codec, add its encode/decode + descriptor to the engine
# AND an entry here first, then widen the adapter's advertised menu — never the
# reverse.
_ENGINE_CODEC_TABLE: Final[dict[tuple[str, int], Codec]] = {
    ("PCMU", G711_SAMPLE_RATE): Codec.PCMU,
    ("PCMA", G711_SAMPLE_RATE): Codec.PCMA,
    ("G722", G722_RTP_CLOCK_RATE): Codec.G722,
    # Opus (ADR-0032): the rtpmap clock is the 48 kHz audio rate (RFC 7587), so the
    # KEY matches reality directly — no G.722-style 8000/16000 split. Only carriable
    # when the webrtc extra (opuslib + libopus) is installed; the codec is advertised
    # ONLY on the WebRTC path (adapter._WEBRTC_SUPPORTED_ENCODINGS), so a TLS/SDES
    # offer never reaches an Opus branch and the default gate never needs opuslib.
    ("OPUS", OPUS_RTP_CLOCK_RATE): Codec.OPUS,
}


# Opus's wire/encode rate is 48 kHz, but the inbound CONVERSATIONAL pipeline (Silero
# VAD + endpointer + STT) runs at 16 kHz: Silero VAD accepts only 8 kHz or 16 kHz, so
# 48 kHz inbound is downsampled to 16 kHz before the pipeline sees it (ADR-0032). The
# wideband content is preserved (16 kHz >> the 8 kHz G.711 path). 16 kHz reuses
# G.722's analysis rate exactly, so VAD/endpoint/barge-in/STT bookkeeping is unchanged.
_OPUS_ANALYSIS_RATE: Final[int] = 16_000


@dataclass(frozen=True, slots=True)
class _CodecDescriptor:
    """Per-codec rate facts that drive the engine's RTP/resample bookkeeping.

    Centralises the rates that differ per codec (and, for G.722/Opus, differ from
    EACH OTHER):

    * ``wire_sample_rate`` — the rate of the PCM the codec encodes/decodes (8 kHz
      G.711, 16 kHz G.722, 48 kHz Opus) and the rate TTS frames are resampled to
      before encoding (the outbound/encode rate).
    * ``rtp_clock_rate`` — the RTP timestamp clock (8 kHz for G.711 AND G.722 — RFC
      3551 §4.5.2 fixes G.722's clock at 8000 despite the 16 kHz audio; 48 kHz for
      Opus per RFC 7587). The RTP timestamp increment per packet derives from this.
    * ``analysis_sample_rate`` — the rate the INBOUND conversational pipeline (VAD,
      endpointer, STT) runs at, i.e. the rate :meth:`inbound_audio` delivers and
      :attr:`inbound_sample_rate` reports. Equals ``wire_sample_rate`` for G.711 and
      G.722 (decode delivers the wire PCM directly). For Opus it is 16 kHz, NOT the
      48 kHz wire rate: Silero VAD accepts only 8/16 kHz, so decoded 48 kHz Opus is
      downsampled to 16 kHz for the pipeline (the wideband content survives). This is
      the one place the wire/encode rate and the analysis/decode-delivery rate split.
    """

    wire_sample_rate: int
    rtp_clock_rate: int
    analysis_sample_rate: int


# One descriptor per runnable Codec. Exhaustive: every Codec has an entry, so the
# engine never falls back to a hardcoded rate literal on any codec path.
_CODEC_DESCRIPTORS: Final[dict[Codec, _CodecDescriptor]] = {
    Codec.PCMU: _CodecDescriptor(
        wire_sample_rate=G711_SAMPLE_RATE,
        rtp_clock_rate=G711_SAMPLE_RATE,
        analysis_sample_rate=G711_SAMPLE_RATE,
    ),
    Codec.PCMA: _CodecDescriptor(
        wire_sample_rate=G711_SAMPLE_RATE,
        rtp_clock_rate=G711_SAMPLE_RATE,
        analysis_sample_rate=G711_SAMPLE_RATE,
    ),
    Codec.G722: _CodecDescriptor(
        wire_sample_rate=G722_SAMPLE_RATE,
        rtp_clock_rate=G722_RTP_CLOCK_RATE,
        analysis_sample_rate=G722_SAMPLE_RATE,
    ),
    # Opus (ADR-0032): wire/encode + RTP clock are both 48 kHz (RFC 7587 — unlike
    # G.722 they coincide, so a 20 ms frame is 960 samples and the timestamp advances
    # 960). The inbound pipeline runs at 16 kHz (Silero VAD's cap), so decoded 48 kHz
    # Opus is downsampled to 16 kHz; outbound TTS is resampled up to 48 kHz to encode.
    Codec.OPUS: _CodecDescriptor(
        wire_sample_rate=OPUS_SAMPLE_RATE,
        rtp_clock_rate=OPUS_RTP_CLOCK_RATE,
        analysis_sample_rate=_OPUS_ANALYSIS_RATE,
    ),
}


def _supported_codec_summary() -> str:
    """A human-readable ``ENCODING/rate`` list of carriable codecs (no PII)."""
    return ", ".join(f"{enc}/{rate}" for enc, rate in _ENGINE_CODEC_TABLE)


def _new_opus_encoder() -> _OpusEncode:
    """Construct an :class:`hermes_voip.media.opus.OpusEncoder` (lazy webrtc import).

    Imported inside the function (not at module scope) so the default no-webrtc gate
    never pulls opuslib. Returns the narrow :class:`_OpusEncode` Protocol surface the
    engine uses; the concrete class is structurally assignable.

    Raises:
        ImportError: If the ``webrtc`` extra (opuslib) or the system libopus is
            absent (propagated from :class:`~hermes_voip.media.opus.OpusEncoder`).
    """
    from hermes_voip.media.opus import OpusEncoder  # noqa: PLC0415 — lazy webrtc import

    return OpusEncoder()


def _new_opus_decoder() -> _OpusDecode:
    """Construct an :class:`hermes_voip.media.opus.OpusDecoder` (lazy webrtc import).

    Raises:
        ImportError: If the ``webrtc`` extra (opuslib) or the system libopus is
            absent (propagated from :class:`~hermes_voip.media.opus.OpusDecoder`).
    """
    from hermes_voip.media.opus import OpusDecoder  # noqa: PLC0415 — lazy webrtc import

    return OpusDecoder()


def codec_for_encoding(encoding: str, clock_rate: int) -> Codec:
    """Map an SDP ``(encoding, clock_rate)`` pair to a runnable engine ``Codec``.

    The check is rate-aware: the encoding name alone is insufficient (e.g. a PCMU
    rtpmap at 16 kHz is not the 8 kHz G.711 the engine carries; and G.722's
    rtpmap clock of 8000 famously does not match its 16 kHz sample rate — RFC
    3551 — so future codecs must be matched on the real carriable pair, not the
    name). The lookup is exhaustive against :data:`_ENGINE_CODEC_TABLE`; there is
    no catch-all default (AGENTS.md rule 17).

    Args:
        encoding: The RTP encoding name (case-insensitive, e.g. ``PCMU``).
        clock_rate: The RTP clock rate in Hz.

    Returns:
        The engine :class:`Codec` that can carry this pair.

    Raises:
        UnsupportedCodecError: If the engine cannot carry ``(encoding, clock_rate)``.
    """
    codec = _ENGINE_CODEC_TABLE.get((encoding.upper(), clock_rate))
    if codec is None:
        raise UnsupportedCodecError(encoding, clock_rate)
    return codec


# ---------------------------------------------------------------------------
# Narrow SRTP Protocol seam (no cryptography import needed at type-check time)
# ---------------------------------------------------------------------------


class _SrtpProtect(Protocol):
    """The protect (outbound encrypt) method surface of SrtpSession."""

    def protect(self, packet: RtpPacket) -> bytes:
        """Encrypt and authenticate an outbound RTP packet."""
        ...


class _SrtpUnprotect(Protocol):
    """The unprotect (inbound decrypt) method surface of SrtpSession."""

    def unprotect(self, data: bytes) -> RtpPacket:
        """Authenticate and decrypt an inbound SRTP packet."""
        ...


# ---------------------------------------------------------------------------
# Narrow SRTCP Protocol seam (RFC 3711 §3.4) — the secured-RTCP transform.
#
# SRTCP works on RAW compound-RTCP BYTES (not RtpPacket): protect() wraps an
# outbound compound RTCP datagram into an SRTCP packet, unprotect() reverses it.
# media.srtcp.SrtcpSession is structurally assignable to both (same as the SRTP
# seam above) — no concrete import is needed at type-check time.
# ---------------------------------------------------------------------------


class _SrtcpProtect(Protocol):
    """The protect (outbound encrypt) method surface of SrtcpSession."""

    def protect(self, rtcp_compound: bytes) -> bytes:
        """Encrypt and authenticate an outbound compound RTCP datagram."""
        ...


class _SrtcpUnprotect(Protocol):
    """The unprotect (inbound decrypt) method surface of SrtcpSession."""

    def unprotect(self, data: bytes) -> bytes:
        """Authenticate and decrypt an inbound SRTCP packet to cleartext RTCP."""
        ...


# ---------------------------------------------------------------------------
# Narrow Opus codec Protocol seam (no opuslib import needed at type-check time).
#
# media.opus.OpusEncoder/OpusDecoder are imported LAZILY inside _encode/_decode so
# the default (no-webrtc) gate never pulls opuslib. Declaring these narrow
# Protocols lets the engine hold/typed the codec objects without importing the
# concrete classes at module scope — OpusEncoder/OpusDecoder are structurally
# assignable to them (same as the SRTP seam above).
# ---------------------------------------------------------------------------


class _OpusEncode(Protocol):
    """The encode method surface of :class:`hermes_voip.media.opus.OpusEncoder`."""

    def encode(self, pcm16: bytes) -> bytes:
        """Encode one 20 ms 48 kHz PCM16 frame to an Opus packet."""
        ...


class _OpusDecode(Protocol):
    """The decode method surface of :class:`hermes_voip.media.opus.OpusDecoder`."""

    def decode(self, packet: bytes) -> bytes:
        """Decode one Opus packet to a 20 ms 48 kHz PCM16 frame."""
        ...

    def decode_fec(self, next_packet: bytes) -> bytes:
        """Recover a lost frame from the next packet's in-band FEC (ADR-0056)."""
        ...

    def decode_plc(self) -> bytes:
        """Conceal one lost frame with no packet (Opus PLC; ADR-0056)."""
        ...


# ---------------------------------------------------------------------------
# WebRTC ICE datagram seam (ADR-0032).
#
# On the WebRTC path the ICE agent (aioice, via media.ice.IceConnection) owns the
# nominated UDP socket and runs STUN consent on it; the engine must NOT bind its
# own socket but carry SRTP media over the ICE agent's send/recv datagram pipe (the
# socket-handoff seam ADR-0016 made explicit). A narrow Protocol covers exactly the
# three async methods the engine drives, so engine.py needs no aioice import and
# IceConnection is structurally assignable without a cast.
# ---------------------------------------------------------------------------


class _IceDatagramPipe(Protocol):
    """The async datagram-pipe surface of ``media.ice.IceConnection`` (ADR-0032)."""

    async def send(self, data: bytes) -> None:
        """Send one datagram over the nominated ICE pair."""
        ...

    async def recv(self) -> bytes:
        """Receive the next datagram from the nominated ICE pair."""
        ...

    async def close(self) -> None:
        """Close the ICE connection and release its sockets."""
        ...


# RFC 7983 first-byte demux on a single rtcp-mux 5-tuple. After the DTLS handshake
# completes the engine should see only SRTP/SRTCP; anything else (a late STUN
# consent packet or a stray DTLS record) is dropped rather than fed to the RTP
# decoder as garbage. SRTP/SRTCP packets carry the RTP version-2 bits, so the first
# byte is in 128-191 (RFC 7983 §7, updating RFC 5764 §5.1.2).
_RFC7983_SRTP_FIRST_BYTE_MIN: Final[int] = 128
_RFC7983_SRTP_FIRST_BYTE_MAX: Final[int] = 191

# The RFC 5761 §4 RTP-vs-RTCP discriminator on a muxed stream is the SECOND byte
# (the RTP M+PT byte aliases the RTCP packet-type byte), so a datagram must be at
# least 2 bytes to demux. ADR-0061 adapter activation.
_RTCP_DEMUX_MIN_LEN: Final[int] = 2


class _DatagramSink(Protocol):
    """The synchronous datagram-send surface the engine's TX path uses.

    Both :class:`asyncio.DatagramTransport` (the TLS/UDP path) and
    :class:`_IceDatagramTransport` (the WebRTC/ICE path, ADR-0032) satisfy it, so
    the engine holds ``_transport`` as this Protocol and the send sites are
    transport-agnostic.
    """

    def sendto(self, data: bytes, addr: tuple[str, int] | None = ...) -> None:
        """Send one datagram (the ``addr`` is honoured only on the UDP path)."""
        ...

    def close(self) -> None:
        """Close the underlying transport."""
        ...


class _IceDatagramTransport:
    """A synchronous ``DatagramTransport``-shaped adapter over an ICE pipe (ADR-0032).

    The engine's send path calls ``transport.sendto(wire, addr)`` and
    ``transport.close()`` (and ``is_closing()``); the ICE agent exposes an *async*
    ``send``. This adapter bridges the two without rewriting the (carefully
    stop-race-safe, pacing-correct) synchronous send machinery: each ``sendto``
    schedules ``ice.send(data)`` as a task on the running loop. The engine already
    serialises its sends under ``_tx_lock`` and creates these tasks in stream order,
    and aioice's ``send`` enqueues onto its own writer, so the wire order is
    preserved. The ``addr`` argument is ignored — the ICE-nominated pair IS the
    destination (there is no comedia latch on the WebRTC path).

    Send-task failures are surfaced via ``on_send_error`` (the engine's
    transport-loss callback) rather than swallowed (rule 37): a dead ICE pipe ends
    the call as a transport loss, exactly as a dead UDP socket does.
    """

    def __init__(
        self,
        pipe: _IceDatagramPipe,
        loop: asyncio.AbstractEventLoop,
        on_send_error: Callable[[Exception], None],
    ) -> None:
        self._pipe = pipe
        self._loop = loop
        self._on_send_error = on_send_error
        self._closing = False
        # Keep strong references to in-flight send tasks so they are not GC'd before
        # completing (asyncio only holds weak refs); discarded on done.
        self._send_tasks: set[asyncio.Task[None]] = set()

    def sendto(self, data: bytes, addr: tuple[str, int] | None = None) -> None:  # noqa: ARG002 — addr ignored: the ICE-nominated pair is the destination
        """Schedule ``data`` to be sent over the ICE pipe (fire-and-forget)."""
        if self._closing:
            return
        task = self._loop.create_task(self._send(bytes(data)))
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)

    async def _send(self, data: bytes) -> None:
        """Await the ICE send; report a failure as a transport loss (rule 37)."""
        try:
            await self._pipe.send(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — report, don't swallow (rule 37)
            self._on_send_error(exc)

    def close(self) -> None:
        """Mark closing and cancel any in-flight send tasks (idempotent).

        The ICE pipe itself is closed by :meth:`RtpMediaTransport.stop` (which awaits
        it); here we only stop scheduling and cancel pending sends so no task
        outlives the call.
        """
        self._closing = True
        for task in list(self._send_tasks):
            task.cancel()

    def is_closing(self) -> bool:
        """Whether this transport is closing (DatagramTransport surface)."""
        return self._closing


# ---------------------------------------------------------------------------
# asyncio DatagramProtocol — receives inbound datagrams into a queue.
# ---------------------------------------------------------------------------


class _UdpReceiver(asyncio.DatagramProtocol):
    """DatagramProtocol that enqueues each received datagram.

    The engine creates one instance and passes it to
    ``loop.create_datagram_endpoint``.  The queue is drained by
    :meth:`RtpMediaTransport.inbound_audio`.

    ``on_lost`` is the engine's transport-loss callback (ADR-0026): a fatal socket
    error (``error_received``) or an ERROR socket close (``connection_lost`` with
    an exception) reports the loss so the engine can end the call as a failure
    instead of leaving the inbound generator blocked forever on a dead socket.
    """

    def __init__(
        self,
        queue: asyncio.Queue[_Datagram],
        on_lost: Callable[[Exception | None], None],
    ) -> None:
        self._queue = queue
        self._on_lost = on_lost
        self._transport: asyncio.BaseTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Store the transport so the engine can send datagrams."""
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Queue the datagram with its source address (non-blocking; drop on overflow).

        The source ``addr`` is preserved so the engine can latch its outbound
        destination onto the peer's real media source (symmetric RTP / comedia).
        """
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait((data, addr))
        if self._queue.full():
            _log.debug("inbound queue full — datagram dropped from %s", addr)

    def error_received(self, exc: Exception) -> None:
        """Report a fatal ICMP / socket error as a transport loss (ADR-0026).

        Previously DEBUG-only, which left a call hanging on a dead socket (e.g. an
        unreachable destination surfaced here as ICMP port-unreachable). Reporting
        it via ``on_lost`` ends the call as a failure (→ ``/stop``): the loss is
        now acted upon, never swallowed (rule 37).
        """
        _log.warning("UDP error received — ending call as transport loss: %s", exc)
        self._on_lost(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        """The socket was closed.

        A clean close (``exc is None``) is our own :meth:`stop` /
        ``transport.close()`` — the engine has already set its stop event, so this
        is a no-op. An ERROR close (``exc`` set) is an unexpected transport drop:
        report it as a loss so the inbound generator ends the call as a failure
        (ADR-0026), replacing the old DEBUG-only no-op that hung the call.
        """
        if exc is not None:
            _log.warning("UDP connection lost — ending call: %s", exc)
            self._on_lost(exc)


class _RtcpReceiver(asyncio.DatagramProtocol):
    """DatagramProtocol for the SEPARATE RTCP socket (non-muxed path, RFC 3550 §11).

    Used only when RTCP is not multiplexed with RTP (the offer carried no
    ``a=rtcp-mux``): the engine binds a sibling UDP socket on RTP-port+1 and this
    protocol enqueues each inbound RTCP datagram for the engine's RTCP reader loop
    to feed into :meth:`~RtpMediaTransport.ingest_rtcp`. A full queue drops the
    datagram (RTCP is periodic; a dropped report is harmless). The source address is
    not retained — RTCP carries the sender SSRC, and we send to the negotiated RTCP
    address, not a comedia-latched one.
    """

    def __init__(self, queue: asyncio.Queue[bytes]) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # noqa: ARG002 — RTCP is keyed by SSRC, not source address
        """Enqueue an inbound RTCP datagram (non-blocking; drop on overflow)."""
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(data)


# ---------------------------------------------------------------------------
# RtpMediaTransport
# ---------------------------------------------------------------------------


class RtpMediaTransport:
    """Asyncio UDP media plane: RTP/SRTP send + receive for one telephony call.

    Implements both :class:`~hermes_voip.providers.transport.MediaTransport` and
    :class:`~hermes_voip.call.CallMedia` (the hold-gating + teardown seam).

    Args:
        local_address:   IPv4 address to bind the UDP socket to.
        local_port:      Local UDP port.  Pass ``0`` and read :attr:`local_port`
                         after :meth:`connect` to discover the OS-assigned port.
        remote_address:  IPv4 address to send RTP datagrams to.
        remote_port:     UDP port on the remote (gateway) side.
        codec:           :class:`Codec` (PCMU or PCMA).
        ptime:           Packetisation time in ms (default 20 ms = 50 pps).
        srtp_inbound:    Optional SRTP session for decrypting inbound packets.
                         Must satisfy :class:`_SrtpUnprotect` (i.e. be a
                         :class:`~hermes_voip.media.srtp.SrtpSession`).
                         ``None`` → plain RTP/AVP.
        srtp_outbound:   Optional SRTP session for encrypting outbound packets.
                         Must satisfy :class:`_SrtpProtect`.
                         ``None`` → plain RTP/AVP.
        srtcp_inbound:   Optional SRTCP session for decrypting inbound RTCP (RFC 3711
                         §3.4, ADR-0066). Must satisfy :class:`_SrtcpUnprotect` (i.e. be
                         a :class:`~hermes_voip.media.srtcp.SrtcpSession`). ``None`` →
                         RTCP is parsed/ingested in the clear (plain-RTP path).
        srtcp_outbound:  Optional SRTCP session for encrypting outbound RTCP. Must
                         satisfy :class:`_SrtcpProtect`. ``None`` → RTCP is emitted in
                         the clear. Pair with ``srtcp_inbound`` to ACTIVATE RTCP on a
                         secured (SDES/DTLS/WebRTC) call (see :meth:`start_rtcp`).
        jitter_depth:    ``target_depth`` parameter for
                         :class:`~hermes_voip.rtp.JitterBuffer` (the fixed depth, or
                         the FLOOR when ``jitter_adapt`` is on).
        jitter_adapt:    Enable the JitterBuffer's adaptive reorder tolerance
                         (ADR-0056). ``False`` (the default) keeps a fixed depth —
                         byte-for-byte the legacy behaviour.
        jitter_max_depth: The CEILING for the adaptive depth (only meaningful with
                         ``jitter_adapt=True``); ``None`` uses the JitterBuffer's own
                         default ceiling. Must be ``>= jitter_depth``.
        clock:           Callable returning the current monotonic time in ns.
                         Defaults to :func:`time.monotonic_ns`.  Inject in tests.
        sleep:           Async callable for outbound pacing (default
                         :func:`asyncio.sleep`).  Inject a no-op in tests.
    """

    def __init__(  # noqa: PLR0913, PLR0915, D417 — all params required (incl. the ADR-0010 on_dtmf receive callback); the body is a flat sequence of per-call field initialisers, not branching logic — splitting it would only scatter the call's state across helpers
        self,
        *,
        local_address: str,
        local_port: int,
        remote_address: str,
        remote_port: int,
        codec: Codec,
        payload_type: int | None = None,
        telephone_event_payload_type: int | None = None,
        on_dtmf: Callable[[str], None] | None = None,
        dtmf_send_mode: DtmfSendMode = DtmfSendMode.RFC4733,
        inband_dtmf_rx_enabled: bool = False,
        ptime: int = _DEFAULT_PTIME_MS,
        srtp_inbound: _SrtpUnprotect | None = None,
        srtp_outbound: _SrtpProtect | None = None,
        srtcp_inbound: _SrtcpUnprotect | None = None,
        srtcp_outbound: _SrtcpProtect | None = None,
        jitter_depth: int = 2,
        jitter_adapt: bool = False,
        jitter_max_depth: int | None = None,
        symmetric: bool = True,
        media_timeout_secs: float = 0.0,
        clock: Callable[[], int] | None = None,
        pace_clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        watchdog_sleep: Callable[[float], Awaitable[None]] | None = None,
        initial_seq: int | None = None,
        initial_ts: int | None = None,
        ice_transport: _IceDatagramPipe | None = None,
        aec_enabled: bool = True,
        aec_filter_ms: int = _AEC_DEFAULT_FILTER_MS,
        aec_bulk_delay_ms: int = 0,
        aec_mu: float = 0.30,
        cname: str = _DEFAULT_CNAME,
        rtcp_send: Callable[[bytes], None] | None = None,
        ntp_clock: Callable[[], float] | None = None,
        rtcp_bandwidth: float = _DEFAULT_RTCP_BANDWIDTH,
    ) -> None:
        """Construct the engine; no socket is opened until :meth:`connect`.

        Args:
            ice_transport: An ICE datagram pipe (WebRTC path, ADR-0032). When
                supplied, :meth:`connect` does NOT bind a UDP socket: outbound
                SRTP/RTP is sent via ``ice_transport.send`` and inbound is pumped
                from ``ice_transport.recv`` (with the RFC 7983 first-byte demux so
                only SRTP reaches the decoder). ``None`` (the default) is the
                SIP-over-TLS path: the engine binds its own UDP socket exactly as
                before. The symmetric-RTP comedia latch is disabled on the ICE path
                — the ICE-nominated pair is the destination.

        Args:
            payload_type: The RTP payload type to send and to accept for the
                symmetric-RTP latch. Defaults to the codec's STATIC payload type
                (``codec.value``). Pass the NEGOTIATED payload type when it differs
                from the static one — G.722 may be offered/answered at a dynamic PT,
                and the SDP answer mirrors the offer's PT, so the wire PT can be
                e.g. 109 while ``Codec.G722.value`` is 9. Sending the static PT
                while advertising a dynamic one would make the gateway drop our
                media and break the comedia latch (no audio). Set
                :attr:`payload_type` later (e.g. the outbound path after the 2xx
                answer) to update it.
            telephone_event_payload_type: The NEGOTIATED RFC 4733 telephone-event
                RTP payload type for in-call DTMF (ADR-0010/0031), or ``None`` when
                the offer carried no ``telephone-event`` (so the call cannot send
                DTMF). :meth:`send_dtmf` uses this PT for every named-event packet
                and RAISES when it is ``None`` — it never falls back to a hardcoded
                101 (the gateway may have negotiated a different dynamic PT) nor
                silently no-ops. Set :attr:`telephone_event_payload_type` later
                (the outbound path, after the 2xx answer echoes the PT).
            on_dtmf: Callback invoked with each RECEIVED DTMF digit (ADR-0010), or
                ``None`` to ignore inbound DTMF. The inbound generator demuxes RTP at
                the negotiated ``telephone_event_payload_type`` to a per-call
                :class:`~hermes_voip.dtmf.DtmfReceiver`, which collapses RFC 4733's
                redundant end packets so this fires EXACTLY ONCE per key-press (a
                digit ``"0"``-``"9"`` / ``"*"`` / ``"#"`` / ``"A"``-``"D"``). It is a
                plain sync callable run on the event loop (the controller routes the
                digit to the armed-confirmation resolver or a menu buffer); it must
                not block. Inert when ``telephone_event_payload_type`` is ``None``
                (no DTMF was negotiated) — a stray telephone-event-PT packet is then
                just an unknown PT and is dropped, never decoded as audio. Settable
                later via :attr:`on_dtmf` (the outbound path wires it after answer).
            pace_clock: Monotonic time source in SECONDS used only to pace the
                outbound RTP stream (the deadline pacer in :meth:`_transmit_frame`
                sleeps the time remaining to each frame's deadline so per-frame
                encode cost is absorbed into the 20 ms interval). Distinct from
                ``clock`` (nanoseconds, inbound presentation time). Defaults to
                :func:`time.monotonic`; inject a fake to drive pacing deterministically.
            media_timeout_secs: RTP-inactivity watchdog window in SECONDS (ADR-0026).
                When positive, the inbound generator ends the call if no datagram
                arrives within this window (re-armed on every datagram), recording
                :attr:`media_timed_out` so the adapter classifies MEDIA_TIMEOUT →
                ``/stop``. ``0.0`` (the default) disables the watchdog — the engine
                then ends only on a received BYE or :meth:`stop` (the legacy
                behaviour). This is the reliability fix for a silent media/network
                drop, which otherwise blocks the inbound generator forever.
            watchdog_sleep: Async callable the inactivity watchdog awaits for one
                window (default :func:`asyncio.sleep`). Inject an event-gated
                coroutine in tests to fire the deadline deterministically without a
                wall-clock wait. Distinct from ``sleep`` (outbound pacing).
            initial_seq: Override the random initial RTP sequence number.
                Pass a fixed value in tests to make send_audio assertions
                deterministic (RFC 3550 §5.1 default: random uint16).
            initial_ts:  Override the random initial RTP timestamp.
                Pass a fixed value in tests (RFC 3550 §5.1 default: random uint32).
            cname: The RTCP SDES canonical name for our outbound source (RFC 3550
                §6.5.1, ADR-0061). The adapter passes a per-call value; the default
                is a fixed, public-repo-safe label (NOT PII).
            rtcp_send: A sink for outbound RTCP datagrams (ADR-0061). ``None`` (the
                default) MULTIPLEXES RTCP onto the RTP transport (RFC 5761 rtcp-mux),
                so :meth:`run_rtcp` sends compound RTCP via ``_transport.sendto``.
                The adapter injects a separate-port sink when the negotiated SDP did
                NOT agree rtcp-mux. The engine builds + sends RTCP through this seam;
                CHOOSING the seam per the negotiated mux is the adapter's job.
            ntp_clock: Wallclock time source in Unix SECONDS for the SR NTP
                timestamp and the LSR/DLSR base (RFC 3550 §6.4.1). Defaults to
                :func:`time.time`; inject a fake for deterministic RTCP tests.
                Distinct from ``clock`` (monotonic ns) and ``pace_clock`` (monotonic
                s) — RTT/SR maths needs a wallclock-derived NTP value.
            rtcp_bandwidth: The session RTCP bandwidth in bytes/s (~5% of the media
                bandwidth) feeding the §6.2 transmission interval. The 5 s floor
                dominates for a 2-party call, so this rarely changes the cadence.
        """
        self._local_address = local_address
        self._local_port = local_port
        self._remote_address = remote_address
        self._remote_port = remote_port
        self._codec = codec
        # The RTP payload type on the wire: the negotiated PT if given, else the
        # codec's static PT. Used for both outbound packets and the comedia latch's
        # acceptance check (see _maybe_latch). Kept separate from _codec because the
        # codec KIND (encode/decode + rate) and the wire PT are independent for a
        # dynamic-PT codec like G.722 (RFC 3551 reserves static 9, but gateways use
        # dynamic PTs and the answer echoes the offer's PT).
        self._payload_type: int = codec.value if payload_type is None else payload_type
        # The negotiated RFC 4733 telephone-event RTP payload type for DTMF
        # (ADR-0010/0031), or None when the offer had no telephone-event. send_dtmf
        # raises if None (never a hardcoded 101, never a silent no-op). Settable
        # after construction (the outbound path adopts the answer's PT).
        self._telephone_event_payload_type: int | None = telephone_event_payload_type
        # Inbound DTMF receive (ADR-0010): the callback fired once per decoded digit,
        # and the per-call RFC 4733 decoder that collapses the redundant end packets.
        # The decoder is re-created in connect() so a reused engine starts a clean
        # digit history. ``_on_dtmf`` None ⇒ inbound DTMF is ignored (the demux still
        # drops telephone-event packets so they never reach the audio decoder).
        self._on_dtmf: Callable[[str], None] | None = on_dtmf
        self._dtmf_receiver: DtmfReceiver = DtmfReceiver()
        # The outbound DTMF backend send_dtmf uses (ADR-0036): RFC4733 emits the
        # named-event train; INBAND synthesises dual-tone audio on the TX path; SIP_INFO
        # is the CallSession's job (never reached here); UNAVAILABLE raises.
        self._dtmf_send_mode: DtmfSendMode = dtmf_send_mode
        # In-band DTMF RECEIVE (ADR-0036): when armed (a G.711 call with no
        # telephone-event), the inbound generator runs a Goertzel detector on the
        # AEC-cleaned decoded frame and fires on_dtmf. Off by default — zero cost on the
        # common RFC 4733 path. Re-created in connect() for a clean per-call state.
        self._inband_dtmf_rx_enabled: bool = inband_dtmf_rx_enabled
        self._inband_detector: InbandDtmfDetector | None = None
        self._ptime = ptime
        self._srtp_in = srtp_inbound
        self._srtp_out = srtp_outbound
        # SRTCP sessions (RFC 3711 §3.4, ADR-0066): secure the RTCP control channel on
        # a SECURED media path. When BOTH are set the engine wraps every outbound RTCP
        # datagram in ``_srtcp_out.protect`` (_emit_rtcp) and unwraps every inbound one
        # with ``_srtcp_in.unprotect`` (_ingest_rtcp_datagram) — so a SDES/DTLS/WebRTC
        # call carries authenticated+encrypted RTCP instead of leaking SSRC/CNAME/timing
        # in cleartext. ``None`` on the cleartext plain-RTP path (RTCP rides in clear)
        # and on a secured engine before SRTCP keys are wired (then RTCP stays dormant —
        # start_rtcp's secured guard only flips when ``_has_srtcp``).
        self._srtcp_in = srtcp_inbound
        self._srtcp_out = srtcp_outbound
        self._jitter_depth = jitter_depth
        # Adaptive jitter (ADR-0056): when ``jitter_adapt`` the RX JitterBuffer's
        # reorder tolerance grows under loss/reorder up to ``jitter_max_depth`` and
        # shrinks back toward ``jitter_depth`` (the floor). ``False`` (the default)
        # keeps a fixed-depth buffer — byte-for-byte the legacy behaviour.
        self._jitter_adapt = jitter_adapt
        self._jitter_max_depth = jitter_max_depth
        # The ICE datagram pipe (WebRTC path, ADR-0032), or None for the TLS/UDP
        # path. When set, connect() routes I/O over it instead of binding a socket;
        # the comedia latch is force-disabled (the ICE pair is the destination).
        self._ice: _IceDatagramPipe | None = ice_transport
        self._symmetric = symmetric and ice_transport is None
        # The ICE inbound-reader task (populated by connect() on the WebRTC path);
        # cancelled in stop(). ``None`` on the TLS/UDP path.
        self._ice_reader: asyncio.Task[None] | None = None
        self._clock: Callable[[], int] = (
            clock if clock is not None else time.monotonic_ns
        )
        # Outbound pacing clock: a monotonic time source in SECONDS used only to
        # pace the TX stream to a steady ``ptime`` per packet (the deadline pacer in
        # _transmit_frame). Distinct from ``clock`` (which is in nanoseconds and
        # stamps inbound presentation time): the pacer needs the wall/loop clock to
        # measure how much real time a send actually took so it can sleep the
        # REMAINDER of the ptime, not a flat ptime on top. Defaults to
        # ``time.monotonic``; tests inject a fake to drive pacing deterministically.
        self._pace_clock: Callable[[], float] = (
            pace_clock if pace_clock is not None else time.monotonic
        )
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else asyncio.sleep
        )
        # RTP-inactivity watchdog (ADR-0026). ``_media_timeout_secs > 0`` arms a
        # no-media deadline in the inbound generator; ``_watchdog_sleep`` is the
        # coroutine it awaits for one window (injectable for deterministic tests).
        # A silent media/network drop otherwise blocks the inbound generator
        # forever — this is the reliability fix behind call-termination.
        self._media_timeout_secs = media_timeout_secs
        self._watchdog_sleep: Callable[[float], Awaitable[None]] = (
            watchdog_sleep if watchdog_sleep is not None else asyncio.sleep
        )

        # Outbound RTP sequence / timestamp counters (RFC 3550 §5.1: randomised at
        # construction so a new session does not collide with a prior one on the same
        # SSRC, and known-plaintext attacks on SRTP are harder). Tests may pass
        # explicit values via initial_seq / initial_ts for determinism.
        self._seq: int = (
            initial_seq if initial_seq is not None else random.randint(0, (1 << 16) - 1)  # noqa: S311 — not cryptographic
        )
        self._ts: int = (
            initial_ts if initial_ts is not None else random.randint(0, (1 << 32) - 1)  # noqa: S311 — not cryptographic
        )

        # Hold state.
        self.on_hold: bool = False

        # Symmetric-RTP (comedia) latch state.  ``_outbound_addr`` is the actual
        # destination send_audio aims at; it starts as the SDP-negotiated remote
        # so the first outbound (the greeting) goes out immediately and a comedia
        # gateway can latch onto us.  On the FIRST valid inbound RTP packet we
        # latch it onto that packet's real UDP source (when symmetric is on) and
        # never move it again — see :meth:`_maybe_latch`.
        self._outbound_addr: tuple[str, int] = (remote_address, remote_port)
        self._latched: bool = False

        # One-shot diagnostic flags: log the first outbound and first inbound
        # RTP packet at INFO so the media path is visible in the operator log.
        self._first_tx_logged: bool = False
        self._first_rx_logged: bool = False
        # One-shot flag: log the first self-loopback (our own SSRC) inbound packet
        # we drop, so the operator sees it once without flooding the log.
        self._self_ssrc_logged: bool = False
        # Rolling TX amplitude tracking: every _TX_AMPLITUDE_LOG_PERIOD packets
        # emit one INFO line showing the peak seen in that window.  Counts and
        # resets on each period boundary so a slow TTS lead-in (which is silent)
        # does not swamp the early log lines with zeros.
        self._tx_amplitude_chunk_count: int = 0
        self._tx_amplitude_period_peak: int = 0

        # Socket / asyncio transport state (populated by connect()).
        # _ever_connected distinguishes "stopped after a real call" (silent no-op
        # in send_audio — the frame is dropped cleanly because the call is ending)
        # from "never connected" (programming error — still raises RuntimeError).
        self._ever_connected: bool = False
        self._transport: _DatagramSink | None = None
        # The UDP protocol object handed to asyncio (populated by connect()).
        # Kept so a transport error/close arriving on it is observable and the
        # transport-loss path is reachable; ``None`` before connect()/after stop().
        self._protocol: _UdpReceiver | None = None
        # Set True when the call ended abnormally on the media plane: the RTP
        # inactivity watchdog fired, or the UDP transport was lost (ADR-0026). The
        # adapter reads this after the call loop returns to classify the end as a
        # failure (MEDIA_TIMEOUT / CONNECTION_LOST → ``/stop``) rather than a clean
        # REMOTE_BYE/EOS. Distinct from a received BYE, which stops media WITHOUT
        # setting this.
        self._media_timed_out: bool = False
        self._recv_queue: asyncio.Queue[_Datagram] = asyncio.Queue(
            maxsize=_QUEUE_MAXSIZE
        )
        self._jitter: JitterBuffer = JitterBuffer(
            target_depth=jitter_depth,
            adapt=jitter_adapt,
            max_depth=jitter_max_depth,
        )

        # Outbound rate reconciliation (ADR-0017/0022): a TTS provider emits frames
        # at its own output rate (e.g. sherpa-Kokoro at 24 kHz), but the wire rate
        # is the negotiated codec's (8 kHz G.711, 16 kHz G.722). send_audio
        # resamples any off-wire-rate frame to the wire rate before encoding. We
        # keep one state-carrying Resampler PER source rate so a continuous stream
        # resamples click-free (frame-by-frame output matches a single pass);
        # already-at-wire-rate frames bypass it entirely.
        self._tx_resamplers: dict[int, Resampler] = {}

        # Inbound rate reconciliation (ADR-0032): when the codec's wire/decode rate
        # exceeds the analysis rate (Opus: decode 48 kHz, deliver 16 kHz to the VAD/
        # STT), a state-carrying Resampler downsamples each decoded frame. ``None``
        # for G.711/G.722 (wire rate == analysis rate, no downsample). Created lazily
        # in _decode on the first Opus frame; reset in connect().
        self._rx_resampler: Resampler | None = None

        # G.722 codec state (ADR-0022): the sub-band ADPCM predictor + QMF history
        # are stateful across packets, so one encoder/decoder lives per call and is
        # reset in connect(). Lazily created (only when the negotiated codec is
        # G.722) and re-created on connect() so a reused engine starts a fresh
        # stream. ``None`` on a G.711 call.
        self._g722_encoder: G722Encoder | None = None
        self._g722_decoder: G722Decoder | None = None

        # Opus codec state (ADR-0032): like G.722, the encoder/decoder carry
        # internal predictor history across packets, so one of each lives per call
        # and is reset in connect(). Lazily created (only when the negotiated codec
        # is Opus) so the default no-webrtc path never imports opuslib. Typed as the
        # narrow Protocols media.opus exposes so engine.py needs no opuslib import at
        # type-check time. ``None`` on a non-Opus call.
        self._opus_encoder: _OpusEncode | None = None
        self._opus_decoder: _OpusDecode | None = None

        # Packet-loss concealment state (ADR-0056). On a JitterBuffer ``Lost`` the
        # engine fills the hole with a concealment frame instead of leaving a gap,
        # so the VAD/endpointer/STT see a continuous stream. For G.711/G.722 it
        # repeats the last good decoded ANALYSIS-rate frame attenuated; ``None``
        # until the first real frame, and reset in connect(). ``_consecutive_lost``
        # counts the run of losses since the last real frame so the repeat fades
        # (and so the Opus decoder-state bookkeeping below is correct). Opus carries
        # its own concealment (FEC/PLC) and does not use ``_last_good_samples``, but
        # shares ``_consecutive_lost``. A future RTCP lane can read this loss count.
        self._last_good_samples: bytes | None = None
        self._consecutive_lost: int = 0

        # In-process acoustic echo canceller (ADR-0033). The gateway reflects our
        # outbound TTS back on the inbound leg; the canceller subtracts the KNOWN
        # outbound reference from each decoded inbound frame BEFORE the VAD/ASR see
        # it, so the reflected echo cannot false-trigger barge-in (which lets the
        # barge-in sustained threshold drop — aggressive barge-in). The TX path taps
        # every outbound wire-rate frame as the reference (``_push_aec_reference``);
        # the RX path runs every decoded frame through ``cancel`` (``_inbound_gen``).
        # It runs at the codec's ANALYSIS rate, which is only final once connect()
        # has the negotiated codec (the outbound path re-sets ``_codec`` after
        # construction), so the canceller is (re)created lazily at first use; these
        # store the constructor params (in MILLISECONDS — rate-independent) until
        # then, and ``_ensure_aec`` converts ms → samples at the live analysis rate.
        # ``None`` when AEC is disabled — the RX/TX taps are then no-ops and the
        # ADR-0023 sustained gate is the only echo defence.
        self._aec_enabled = aec_enabled
        self._aec_filter_ms = aec_filter_ms
        self._aec_bulk_delay_ms = aec_bulk_delay_ms
        self._aec_mu = aec_mu
        self._aec: EchoCanceller | None = None

        # Outbound 20 ms re-framing buffer (the "very choppy" fix), holding the
        # not-yet-sent wire-rate PCM16 bytes in order. A streaming TTS provider
        # (e.g. ElevenLabs over chunked HTTP) hands send_audio one frame PER
        # NETWORK CHUNK, and a chunk is rarely a whole multiple of the 160-sample
        # (320-byte) G.711 frame. We must NOT zero-pad each chunk's sub-frame
        # remainder to a full frame — that injects a slug of silence at every
        # chunk boundary (a click/gap per chunk == "very choppy"). Instead bytes
        # accumulate here and send_audio drains whole frames off the front, so the
        # concatenated outbound stream is sample-continuous. The residual partial
        # is padded to a final frame exactly once, in stop()'s flush, so the last
        # utterance's tail is delivered not stranded; set_hold(True) drops it
        # (hold stops outbound media). Reset in connect()/stop().
        #
        # A bytearray (not immutable bytes) so the per-chunk send_audio drain
        # mutates it in place (efficiency, rule 22): ``extend`` appends each chunk
        # and ``del buf[:n]`` pops whole frames off the front WITHOUT the
        # ``self._tx_buffer = self._tx_buffer + chunk`` / ``[n:]`` reallocation that
        # immutable bytes forced on every append and every drained frame. Resets use
        # ``.clear()`` (keeps the same object) except where a caller snapshots the
        # tail, which copies to immutable ``bytes`` before clearing so the captured
        # tail is not aliased to the (about-to-be-reused) buffer.
        self._tx_buffer: bytearray = bytearray()

        # Barge-in flush generation (ADR-0028). ``flush_outbound`` increments this
        # to signal any send_audio drain loop currently parked on its pacing sleep
        # to STOP emitting the rest of its (now-superseded) utterance the instant it
        # wakes — so the agent goes quiet within ~1 ptime instead of after the whole
        # buffered chunk paces out. send_audio snapshots the value before its drain
        # loop and bails if it changed across a pacing sleep. Reset in connect().
        self._flush_generation: int = 0

        # Outbound pacing deadline (seconds on the ``_pace_clock`` timeline): the
        # time the NEXT frame is scheduled to go on the wire. ``None`` means no
        # schedule is anchored yet (the first frame of a stream sends immediately —
        # critical for the greeting so a NAT'd gateway latches at once — and anchors
        # the schedule from that instant). Each sent frame advances it by exactly one
        # ptime, so the pacer sleeps the REMAINING time to the deadline rather than a
        # flat ptime: a slow per-frame encode (G.722 is pure-Python, ~1.3 ms/frame
        # and variable) is absorbed into the interval instead of being added on top,
        # giving a steady 50 pps for any codec. The pacer re-anchors this to ``now``
        # whenever it has fallen more than one ptime behind (a real idle gap between
        # TTS utterances), so the next burst does NOT dump back-to-back catching up.
        # Reset to ``None`` in connect()/stop() so a fresh or re-used engine
        # re-anchors on its next frame.
        self._next_send_at: float | None = None

        # In-flight frame slot for the deadline pacer's stop-race (the one yield
        # point in _transmit_frame is the pacing sleep BETWEEN encode and sendto).
        # A frame is encoded (the stateful encoder advances, seq/ts are committed
        # into the bytes) BEFORE the pacing sleep, so on the wire it leaves on a
        # fixed grid regardless of encode cost. If a concurrent stop() nulls the
        # transport DURING that sleep, the already-encoded datagram cannot be
        # re-derived from _tx_buffer (re-encoding would double-advance the stateful
        # G.722 encoder and reorder the stream), so it is parked HERE and stop()'s
        # flush sends it FIRST (it is the front of the stream), before the remaining
        # raw frames in _tx_buffer. Cleared the instant the frame reaches the wire on
        # the normal path. ``None`` whenever no frame is mid-flight. Reset in
        # connect()/stop().
        self._inflight_wire: bytes | None = None

        # Stop signal: set by stop(), selected on by the inbound generator so it
        # wakes promptly regardless of recv-queue fullness (a bounded queue can
        # otherwise drop a sentinel datagram and strand the consumer).
        self._stop_event: asyncio.Event = asyncio.Event()

        # Lossless one-item rollback slot.  When the stop flag wins a race in
        # which a datagram had already been dequeued, that datagram is parked
        # here (never re-queued, so it cannot be lost to a full queue) and
        # returned first by the next _next_datagram() call.
        self._pending: _Datagram | None = None

        # Long-lived stop-wait racer reused across every _next_datagram() call
        # (efficiency, rule 22). The inbound generator calls _next_datagram ~50x/s;
        # the stop flag is set exactly once per call's lifetime, so re-creating a
        # `_stop_event.wait()` Task on every receive is pure churn. We park ONE
        # await on the event here and reuse the resulting Task — it is never
        # cancelled by _next_datagram (only the per-call queue.get() racer is), and
        # it is awaited+cleared in stop(). ``None`` until the first race that needs
        # it; reset in connect()/stop() (the event object is replaced there too).
        self._stop_wait_task: asyncio.Task[bool] | None = None

        # Outbound TX serialization (ADR-0031): send_audio (one re-framed TTS
        # chunk) and send_dtmf (one DTMF burst) are the two TX coroutines that
        # ``await`` BETWEEN per-packet sends (their pacing sleeps), so they could
        # otherwise interleave packets and race the shared _seq/_ts/_outbound_addr.
        # Each holds this lock for the duration of its send, so the wire shows a
        # clean, strictly-monotonic sequence with each digit's DTMF packets
        # contiguous. The synchronous flushes (stop()/_flush_tx_tail,
        # flush_outbound) emit without awaiting between packets, so they run atomic
        # relative to these two and need no lock. Re-created in connect() so a reused
        # engine starts with a fresh, unheld lock.
        self._tx_lock: asyncio.Lock = asyncio.Lock()

        # ---- RTCP control channel (RFC 3550 §6, ADR-0061) ----
        self._cname = cname
        self._rtcp_send: Callable[[bytes], None] | None = rtcp_send
        # The constructor-injected RTCP sink (None on a muxed prod call; a test sink
        # or the separate-port adapter sink otherwise). The non-muxed start_rtcp path
        # overwrites self._rtcp_send with a sibling-socket lambda; stop()/connect()
        # restore it to THIS value so a reused engine never sends RTCP through a
        # closed previous socket (codex review: RTCP-send lifecycle).
        self._injected_rtcp_send: Callable[[bytes], None] | None = rtcp_send
        self._ntp_clock: Callable[[], float] = (
            ntp_clock if ntp_clock is not None else time.time
        )
        self._rtcp_bandwidth = rtcp_bandwidth
        # Sender bookkeeping for our SR (RFC 3550 §6.4.1): RTP data packets and
        # payload octets we have sent this call. Incremented per outbound media frame
        # in _transmit_frame; reset in connect()/stop().
        self._rtcp_packets_sent: int = 0
        self._rtcp_octets_sent: int = 0
        # Per-source inbound reception statistics, keyed by the SENDER's SSRC. One
        # ReceptionStats per remote source feeds the report blocks of our SR/RR. Reset
        # in connect().
        self._reception: dict[int, ReceptionStats] = {}
        # For our report blocks' LSR/DLSR (RFC 3550 §6.4.1): per peer-SSRC, the compact
        # (middle-32) NTP of the last SR we received from it, and the wallclock (Unix
        # s) at which we received it (so DLSR = now - that, in 1/65536 s units).
        self._last_sr_from: dict[int, tuple[int, float]] = {}
        # The compact NTP of the last SR WE sent, so a peer RR echoing it (lsr) lets us
        # compute RTT. None until we have sent an SR.
        self._last_sr_sent_compact: int | None = None
        # The far-end view of OUR stream, parsed from the most recent inbound report
        # block about our SSRC (fraction lost, cumulative lost, jitter-in-clock-units),
        # plus the last computed RTT in seconds. None until a peer report arrives.
        self._remote_report: ReportBlock | None = None
        self._rtt_seconds: float | None = None
        # The running average compound-RTCP size (RFC 3550 §6.2/§6.3.3) for the
        # interval calculation; seeded, then smoothed toward real sizes as we send.
        self._avg_rtcp_size: float = _INITIAL_AVG_RTCP_SIZE
        # The background RTCP loop task (set by :meth:`start_rtcp`; cancelled in
        # stop()). None on the TLS/UDP and ICE paths until the adapter starts it.
        self._rtcp_task: asyncio.Task[None] | None = None
        # RTCP activation state (ADR-0061 adapter activation, set by start_rtcp,
        # reset in connect()). ``_rtcp_active`` is True once the adapter activated RTCP
        # for this call on EITHER path (muxed or non-muxed) — it gates the general RTCP
        # machinery (the closing BYE, teardown quality logging). ``_rtcp_mux_active`` is
        # a SEPARATE flag, True ONLY when RTCP is multiplexed onto the RTP transport
        # (mux=True, RFC 5761); it — not ``_rtcp_active`` — gates the inbound muxed-RTCP
        # demux in _inbound_gen, because on a NON-muxed call RTCP arrives on the sibling
        # socket only and the RTP socket carries PURE RTP (codex review #1: gating the
        # demux on ``_rtcp_active`` wrongly fired it on non-muxed calls, where an RTP
        # packet whose 2nd byte aliases an RTCP PT would be mis-routed to ingest_rtcp).
        # Both OFF until the adapter activates RTCP, so an engine on which RTCP was
        # never started behaves byte-for-byte as before (an RTCP-typed datagram on the
        # RTP socket is then just an unhandled payload type and is dropped). On the
        # NON-muxed path start_rtcp binds a sibling RTCP DatagramTransport on RTP-port+1
        # (RFC 3550 §11) and a reader that pumps it into ingest_rtcp;
        # ``_rtcp_transport``/``_rtcp_reader``/``_rtcp_local_port`` hold it for stop()
        # to tear down. All None on the muxed path (RTCP rides the RTP transport).
        self._rtcp_active: bool = False
        self._rtcp_mux_active: bool = False
        self._rtcp_transport: asyncio.DatagramTransport | None = None
        self._rtcp_reader: asyncio.Task[None] | None = None
        self._rtcp_local_port: int | None = None

    # ------------------------------------------------------------------
    # MediaTransport Protocol
    # ------------------------------------------------------------------

    async def connect(self) -> bool:  # noqa: PLR0915 — a flat per-call state-reset sequence (now incl. the RTCP channel), not branching logic; splitting it would scatter the reset across helpers
        """Open the media transport (a UDP socket, or the ICE pipe on the WebRTC path).

        On the SIP-over-TLS path this binds a non-blocking UDP socket to
        ``local_address:local_port``. On the WebRTC path (an ``ice_transport`` was
        supplied, ADR-0032) it binds NO socket: it wraps the ICE pipe in a
        :class:`_IceDatagramTransport` and starts a background reader pumping
        ``ice.recv`` (with the RFC 7983 demux) into the inbound queue.

        Returns:
            ``True`` on success.  Raises on socket / OS error.
        """
        loop = asyncio.get_running_loop()
        sock: socket.socket | None = None
        if self._ice is None:
            # Create a bound UDP socket first so port=0 lets the OS choose.
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.bind((self._local_address, self._local_port))
            # Record the OS-assigned port before handing the socket to asyncio.
            self._local_port = sock.getsockname()[1]

        self._recv_queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._jitter = JitterBuffer(
            target_depth=self._jitter_depth,
            adapt=self._jitter_adapt,
            max_depth=self._jitter_max_depth,
        )
        # Fresh inbound DTMF decoder so a reused engine starts with an empty
        # recently-seen-timestamp window (a stale window could suppress a real first
        # press of the new call). The ``on_dtmf`` callback is preserved across calls.
        self._dtmf_receiver = DtmfReceiver()
        # Fresh in-band detector (ADR-0036) so a reused engine starts with no held
        # press; analysed at the engine's analysis rate (8 kHz G.711). None if unarmed.
        self._inband_detector = (
            InbandDtmfDetector(sample_rate=self._analysis_sample_rate)
            if self._inband_dtmf_rx_enabled
            else None
        )
        # Drop any stop-wait racer left parked on the PREVIOUS call's stop event
        # (a reused engine replaces the event below). It cannot fire usefully on the
        # new event; cancel AND await it so it does not leak (no "Task was destroyed
        # but it is pending"). stop() normally clears this to None, so this only
        # bites on connect-without-prior-stop (reconnect of a never-stopped engine).
        stale_stop_wait = self._stop_wait_task
        self._stop_wait_task = None
        if stale_stop_wait is not None:
            stale_stop_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stale_stop_wait
        self._stop_event = asyncio.Event()
        self._pending = None
        # Fresh TX lock so a reused engine never inherits a held lock (ADR-0031).
        self._tx_lock = asyncio.Lock()
        # A reused engine starts a fresh call: it has not timed out yet.
        self._media_timed_out = False
        # Drop any carried outbound-resample state so a reused engine starts a
        # fresh stream (no stale sub-sample phase from a previous call).
        self._tx_resamplers = {}
        # Drop the inbound downsample state too (ADR-0032 Opus 48->16 kHz path).
        self._rx_resampler = None
        # Reset the G.722 codec state so a reused engine starts with a fresh
        # predictor/QMF history (a stale predictor would corrupt the start of the
        # new call). It is (re)created lazily by _encode/_decode on first use —
        # the OUTBOUND path reassigns self._codec from a PCMU placeholder to the
        # negotiated codec AFTER connect() but BEFORE any media, so creating it at
        # connect() time would miss a connect-as-PCMU-then-G.722 call.
        self._g722_encoder = None
        self._g722_decoder = None
        # Reset the Opus codec state too (ADR-0032), for the same reason: a reused
        # engine must start with a fresh encoder/decoder predictor history. Lazily
        # (re)created by _encode/_decode on first use of an Opus call.
        self._opus_encoder = None
        self._opus_decoder = None
        # Reset packet-loss concealment state (ADR-0056): a reused engine must not
        # conceal the start of a new call with the previous call's last frame.
        self._last_good_samples = None
        self._consecutive_lost = 0
        # Reset the echo canceller too (ADR-0033): a reused engine must start with a
        # zeroed adaptive filter + empty reference history (a stale echo path would
        # mis-cancel the start of the new call). Like the G.722/Opus codecs it is
        # (re)created lazily on first reference/cancel, at the CURRENT analysis rate —
        # the outbound path re-sets ``_codec`` after connect() but before media, so
        # building it here (as PCMU) would pin the wrong rate for a G.722/Opus call.
        self._aec = None
        # Drop any leftover outbound re-framing bytes from a previous call (clear in
        # place — the bytearray object is reused).
        self._tx_buffer.clear()
        # A fresh call starts at flush generation 0 (no barge-in flush yet).
        self._flush_generation = 0
        # Re-anchor the pacing schedule on the first frame of this call (so a reused
        # engine does not inherit a stale, long-past deadline that would dump a burst
        # of packets back-to-back to "catch up").
        self._next_send_at = None
        # No frame is mid-flight at the start of a call.
        self._inflight_wire = None
        # A reused engine starts with no ICE reader (a fresh one is created below on
        # the WebRTC path); stop() cancelled any prior reader.
        self._ice_reader = None
        # Reset the latch so a reused engine re-latches on its next call: aim
        # back at the SDP-negotiated remote until the first valid inbound packet.
        # (On the WebRTC/ICE path symmetric is force-disabled, so this never latches.)
        self._outbound_addr = (self._remote_address, self._remote_port)
        self._latched = False
        # Reset one-shot diagnostic flags so a reconnected engine logs the first
        # outbound and inbound packets of the new call.
        self._first_tx_logged = False
        self._first_rx_logged = False
        self._self_ssrc_logged = False
        self._tx_amplitude_chunk_count = 0
        self._tx_amplitude_period_peak = 0
        # Reset RTCP state (ADR-0061) so a reused engine starts a fresh control
        # channel: no carried sender counts, reception stats, peer reports, or RTT.
        self._rtcp_packets_sent = 0
        self._rtcp_octets_sent = 0
        self._reception = {}
        self._last_sr_from = {}
        self._last_sr_sent_compact = None
        self._remote_report = None
        self._rtt_seconds = None
        self._avg_rtcp_size = _INITIAL_AVG_RTCP_SIZE
        self._rtcp_task = None
        # A reused engine starts a fresh call with RTCP dormant: the adapter must
        # re-activate it (start_rtcp) per the new call's negotiated mux. The sibling
        # RTCP socket (non-muxed path) was torn down in the previous stop().
        self._rtcp_active = False
        self._rtcp_mux_active = False
        self._rtcp_transport = None
        self._rtcp_reader = None
        # Restore the constructor sink: drop any sibling-socket lambda a previous
        # non-muxed call installed, so this call never sends RTCP via a closed socket.
        self._rtcp_send = self._injected_rtcp_send
        self._rtcp_local_port = None

        if self._ice is not None:
            # WebRTC path (ADR-0032): no socket. Wrap the ICE pipe in the synchronous
            # DatagramSink adapter the TX path expects, and start the inbound reader
            # that pumps ICE recv → RFC 7983 demux → the recv queue. A failure of an
            # outbound ICE send is reported as a transport loss (rule 37), exactly as
            # a dead UDP socket is.
            self._protocol = None
            self._transport = _IceDatagramTransport(
                self._ice, loop, self._on_ice_send_error
            )
            self._ice_reader = loop.create_task(self._ice_recv_loop(self._ice))
            self._ever_connected = True
            return True

        # SIP-over-TLS path: bind our own UDP socket.
        assert sock is not None  # noqa: S101 — invariant: sock is bound when _ice is None
        protocol = _UdpReceiver(self._recv_queue, self._on_transport_lost)
        self._protocol = protocol

        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            sock=sock,
        )
        # create_datagram_endpoint returns (transport, protocol); the transport
        # is always a DatagramTransport when a UDP socket is passed.
        assert isinstance(transport, asyncio.DatagramTransport)  # noqa: S101 — invariant, not a test assertion
        self._transport = transport
        self._ever_connected = True
        return True

    def _on_transport_lost(self, exc: Exception | None) -> None:
        """End the call when the UDP transport is lost (ADR-0026).

        Invoked from the :class:`_UdpReceiver` on a fatal ``error_received`` or an
        ERROR ``connection_lost``. Records the media-loss flag (so the adapter
        classifies the end as a failure → ``/stop``) and sets the stop event so the
        inbound generator wakes and ends — the silent-drop call no longer hangs.
        Idempotent: a second loss after stop is harmless (the flag stays set; the
        event is already set). The exception is logged (DEBUG) here, not re-raised,
        because it arrives on the event loop, not a call path — but it is acted
        upon, never swallowed (rule 37).
        """
        _log.debug("media transport lost: %s", exc)
        self._media_timed_out = True
        self._stop_event.set()

    def _on_ice_send_error(self, exc: Exception) -> None:
        """Report a failed outbound ICE send as a transport loss (ADR-0032).

        Routed from :class:`_IceDatagramTransport`'s send task. Mirrors
        :meth:`_on_transport_lost` for the UDP path: a dead ICE pipe ends the call
        as a failure (the adapter classifies it → ``/stop``) instead of silently
        dropping audio (rule 37 — acted upon, not swallowed).
        """
        _log.warning("ICE send failed — ending call as transport loss: %s", exc)
        self._media_timed_out = True
        self._stop_event.set()

    async def _ice_recv_loop(self, ice: _IceDatagramPipe) -> None:
        """Pump inbound datagrams from the ICE pipe into the recv queue (ADR-0032).

        Replaces the asyncio ``DatagramProtocol`` callback on the WebRTC path: it
        ``await``s ``ice.recv()`` and applies the RFC 7983 first-byte demux —
        forwarding ONLY SRTP/SRTCP (first byte 128-191) to the inbound queue and
        dropping anything else (a late STUN consent packet or a stray DTLS record,
        which would be garbage to the RTP decoder). The DTLS handshake is already
        complete before this loop starts, so non-SRTP traffic is residual.

        The queued source address is the ICE-nominated remote (operational, not a
        comedia trigger — symmetric is force-disabled on the ICE path). On a
        ``recv`` failure (the pipe closed/errored) the loop reports a transport loss
        and exits; ``CancelledError`` (from :meth:`stop`) propagates cleanly. Errors
        are acted upon, never swallowed (rule 37).
        """
        remote = (self._remote_address, self._remote_port)
        try:
            while True:
                data = await ice.recv()
                if not data:
                    continue
                first = data[0]
                if not (
                    _RFC7983_SRTP_FIRST_BYTE_MIN
                    <= first
                    <= _RFC7983_SRTP_FIRST_BYTE_MAX
                ):
                    # Not SRTP/SRTCP — a residual DTLS/STUN datagram. Drop it; never
                    # feed it to the RTP decoder.
                    _log.debug(
                        "ice rx: dropped non-SRTP datagram (first byte %d)", first
                    )
                    continue
                with contextlib.suppress(asyncio.QueueFull):
                    self._recv_queue.put_nowait((data, remote))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — report as loss, don't swallow (rule 37)
            _log.warning("ICE recv failed — ending call as transport loss: %s", exc)
            self._media_timed_out = True
            self._stop_event.set()

    async def disconnect(self) -> None:
        """Tear down media and signalling; idempotent (MediaTransport seam)."""
        await self.stop()

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        """Far-end audio decoded to PCM16 at :attr:`inbound_sample_rate`.

        Receives datagrams, un-protects SRTP (if configured), feeds the jitter
        buffer, and decodes each ordered packet to a
        :class:`~hermes_voip.providers.audio.PcmFrame`.  Tampered or
        unauthenticated packets are silently dropped.

        A :class:`~hermes_voip.rtp.Lost` signal is CONCEALED rather than skipped
        (ADR-0056): the engine yields a concealment frame in the hole so the
        VAD/endpointer/STT see a continuous stream (Opus uses its in-band FEC /
        native PLC; G.711/G.722 repeat the last good frame, attenuated). The
        concealment frame flows through the same AEC + in-band-DTMF path as a real
        frame.

        Returns:
            An :class:`~collections.abc.AsyncIterator` of decoded
            :class:`~hermes_voip.providers.audio.PcmFrame` objects.
        """
        return self._inbound_gen()

    async def _inbound_gen(self) -> AsyncIterator[PcmFrame]:  # noqa: PLR0912, PLR0915 — the inbound RX path is a single linear demux (SRTP/parse → self-loopback drop → DTMF divert → latch → jitter → decode-or-conceal); each stage is a guard-and-continue that belongs in one place, not fragmented across helpers (the decode/conceal/AEC/DTMF work IS already factored into helpers)
        """Internal async generator implementing inbound_audio.

        Terminates when :meth:`stop` sets the stop flag (even if the recv queue
        is full).  Cancellation (``asyncio.CancelledError``) propagates — it is
        never swallowed (rule 37), so a caller timeout or cancel is honoured.
        """
        while True:
            item = await self._next_datagram()
            # ``None`` ⇒ stop signalled; exit cleanly.
            if item is None:
                return
            data, source = item

            # Inbound RTCP demux (RFC 5761 §4, ADR-0061/0066): on a MUXED stream RTP
            # and RTCP share the 5-tuple; the discriminator is the second byte — RTP's
            # M+PT byte aliases the RTCP packet-type byte, so 200..204 (SR/RR/SDES/BYE/
            # APP) is RTCP. That byte is in the clear on BOTH plain RTCP and SRTCP (RFC
            # 3711 §3.4 leaves the RTCP header through the sender SSRC unencrypted), so
            # the same discriminator works on a secured muxed stream. Feed such a
            # datagram to ingest_rtcp (which SRTCP-unprotects it when secured) and
            # consume it (control, never audio). Engaged ONLY when RTCP is MULTIPLEXED
            # (``_rtcp_mux_active`` — NOT ``_rtcp_active``, also set on the non-muxed
            # path where RTCP rides a SEPARATE socket and the RTP socket carries pure
            # RTP; codex review #1) AND the stream is either cleartext (``_srtp_in is
            # None``) or has the SRTCP transform wired (``_srtcp_in is not None``) — so
            # a secured engine without SRTCP (RTCP dormant) never mis-routes here. On
            # the non-muxed path inbound RTCP arrives on the sibling socket
            # (_rtcp_recv_loop), not this queue. The mux activation also guarantees no
            # negotiated RTP payload type lies in 64-95 (the adapter refuses rtcp-mux
            # for those per RFC 5761 §4), so a 200..204 second byte here is RTCP, never
            # an aliased RTP M+PT. A malformed/auth-fail datagram is dropped, not fatal
            # (mirrors the malformed-RTP drop below; per the ingest_rtcp contract).
            if (
                self._rtcp_mux_active
                and (self._srtp_in is None or self._srtcp_in is not None)
                and len(data) >= _RTCP_DEMUX_MIN_LEN
                and RTCP_PT_SR <= data[1] <= RTCP_PT_APP
            ):
                self._ingest_rtcp_datagram(data)
                continue

            rtp_pkt: RtpPacket

            # SRTP un-protect (if configured).  Only an authentication/replay
            # failure (SrtpError) drops a single packet; any other exception
            # (e.g. a misconfigured backend) propagates (rule 37).
            if self._srtp_in is not None:
                try:
                    rtp_pkt = self._srtp_in.unprotect(data)
                except SrtpError as exc:
                    _log.debug("SRTP auth/replay failure — datagram dropped: %s", exc)
                    continue
            else:
                # Plain RTP: drop malformed datagrams.
                try:
                    rtp_pkt = RtpPacket.parse(data)
                except ValueError as exc:
                    _log.debug("malformed RTP datagram — dropped: %s", exc)
                    continue

            # Self-loopback drop (ADR-0023): a packet carrying OUR OWN outbound
            # SSRC is our own audio reflected back onto the inbound path. Feeding
            # it to the jitter buffer / VAD / ASR would transcribe the agent's own
            # speech as the caller and self-interrupt the agent. Drop it BEFORE the
            # comedia latch so a looped-back packet can never move the outbound
            # destination either. (This is the correct fix for a self-loopback; it
            # does NOT catch gateway echo, which re-originates under the gateway's
            # own SSRC — the call loop's barge-in gate handles that case.)
            if rtp_pkt.ssrc == _OUTBOUND_SSRC:
                if not self._self_ssrc_logged:
                    self._self_ssrc_logged = True
                    _log.debug(
                        "rtp rx: dropping inbound packet with our own SSRC "
                        "0x%08x from %s:%d (self-loopback)",
                        rtp_pkt.ssrc,
                        source[0],
                        source[1],
                    )
                continue

            # The datagram is genuine RTP (it parsed / authenticated) and is not our
            # own loopback: this is the only point at which a comedia latch may fire
            # (anti-spoofing — garbage that does not parse never reaches here). Run the
            # latch FIRST, for every packet, so the inbound DTMF demux below sees the
            # SAME established remote-source state as audio (the latch is a no-op for a
            # DTMF packet — _maybe_latch only latches on the negotiated audio PT — so
            # this does not let DTMF move the destination). Cross-vendor review: the
            # confirmation channel must not be processed in a pre-latch window the audio
            # path never has.
            self._maybe_latch(rtp_pkt, source)

            # Inbound payload-type dispatch (ADR-0010 + cross-vendor review). Exactly
            # three outcomes, no catch-all that mis-decodes:
            #   * the NEGOTIATED telephone-event PT → RFC 4733 DTMF (never the audio
            #     decoder — a 4-byte named-event payload decoded as G.711 is garbage);
            #   * the negotiated audio PT → the jitter buffer + audio decode below;
            #   * anything else (comfort noise, a stray/unknown PT, or a spoofed
            #     telephone-event PT when none was negotiated) → DROP. The engine has
            #     never PT-filtered inbound audio before; filtering here closes the
            #     rule-27 'inert + safe when no DTMF negotiated' gap (an unknown PT is
            #     no longer mis-decoded as audio) and bounds what reaches either path.
            te_pt = self._telephone_event_payload_type
            if te_pt is not None and rtp_pkt.payload_type == te_pt:
                # Spoof source-binding (cross-vendor review #1): the confirmation
                # channel is a security control, so once the comedia latch has fixed
                # the remote source, a DTMF packet from any OTHER source is a spoof and
                # is dropped — stricter than the audio path, deliberately. Before the
                # latch (or with symmetric off) we accept it, exactly as audio is
                # accepted before the latch.
                if self._symmetric and self._latched and source != self._outbound_addr:
                    _log.warning(
                        "dtmf rx: dropping telephone-event packet from unlatched "
                        "source %s:%d (latched remote is %s:%d) — possible spoof",
                        source[0],
                        source[1],
                        self._outbound_addr[0],
                        self._outbound_addr[1],
                    )
                    continue
                self._handle_inbound_dtmf(rtp_pkt)
                continue
            if rtp_pkt.payload_type != self._payload_type:
                # Neither audio nor DTMF: drop rather than mis-decode as G.711.
                _log.debug(
                    "rtp rx: dropping packet with unhandled payload type %d",
                    rtp_pkt.payload_type,
                )
                continue

            if not self._first_rx_logged:
                self._first_rx_logged = True
                _log.info(
                    "rtp rx: first packet <- %s:%d (%d bytes)",
                    source[0],
                    source[1],
                    len(data),
                )

            # RTCP reception statistics (RFC 3550 §6.4.1, ADR-0061): record this
            # genuine inbound audio packet against its source's stats so our SR/RR
            # report blocks carry the right loss/jitter/EHSN. Done here — after the
            # PT filter, before the jitter buffer — so DTMF/comfort-noise/stray PTs
            # are excluded and the count matches the audio actually received. The
            # arrival time is the monotonic clock in SECONDS (the jitter transit
            # maths needs a wall/loop time, not the ns presentation stamp's units).
            self._note_rtp_received(
                seq=rtp_pkt.sequence_number,
                rtp_timestamp=rtp_pkt.timestamp,
                arrival_ts=self._clock() / 1e9,
                ssrc=rtp_pkt.ssrc,
            )

            # Feed the jitter buffer.
            self._jitter.push(rtp_pkt)

            # Drain all available ordered output from the jitter buffer.
            while True:
                output = self._jitter.pop()
                if output is None:
                    break
                ts_ns = self._clock()
                if isinstance(output, Lost):
                    # Packet-loss concealment (ADR-0056): fill each hole with a
                    # concealment frame so the VAD/STT see a continuous stream.
                    # Lost.count > 1 signals a coalesced run (a far-ahead gap
                    # compressed to one event); iterate count times so the stream
                    # produced here is frame-accurate regardless of gap size.
                    #
                    # The run's anchor (peek_next) is already at the real
                    # successor for the WHOLE run, so only the LAST slot — the one
                    # immediately before the successor — may use the successor's
                    # in-band Opus FEC (which carries exactly that one preceding
                    # frame). The earlier interpolated slots conceal with PLC,
                    # reproducing the pre-coalesce per-packet semantics (one Lost
                    # per pop) instead of redundantly FEC-decoding the same
                    # successor for every slot.
                    _log.debug(
                        "JitterBuffer: lost seq=%d count=%d",
                        output.sequence,
                        output.count,
                    )
                    last_index = output.count - 1
                    for index in range(output.count):
                        ts_ns = self._clock()
                        frame = self._conceal_frame(ts_ns, is_last=index == last_index)
                        frame = self._cancel_echo(frame)
                        self._detect_inband_dtmf(frame)
                        yield frame
                    continue
                # Decode the wire payload to an analysis-rate PcmFrame and
                # remember it as the basis for concealing a future loss.
                frame = self._decode(output.payload, ts_ns)
                self._last_good_samples = frame.samples
                self._consecutive_lost = 0
                # Acoustic echo cancellation (ADR-0033): subtract the KNOWN outbound
                # TTS reference (tapped in the TX path) from this inbound frame
                # BEFORE the VAD/ASR see it, so the gateway's reflected echo cannot
                # false-trigger barge-in. A no-op when AEC is disabled. Runs at the
                # analysis rate (the rate _decode delivers), so far-end and near-end
                # align. Buffer-free (no added latency, rule 22). A concealment frame
                # is the genuine signal estimate for the lost slot, so it is run
                # through AEC identically — the TX reference still advances in step.
                frame = self._cancel_echo(frame)
                # In-band DTMF receive (ADR-0036): run the Goertzel detector on the
                # AEC-cleaned frame BEFORE the VAD/ASR see it, so the agent's own
                # reflected tones (already removed by AEC) never false-trigger and a
                # detected keypress goes to on_dtmf, not into the transcript. Armed only
                # on a G.711 call that negotiated no telephone-event; a no-op otherwise.
                # The audio frame still flows on unchanged — a tone the caller pressed
                # is surfaced as a digit AND heard, like the gateway's own behaviour.
                self._detect_inband_dtmf(frame)
                yield frame

    def _maybe_latch(self, packet: RtpPacket, source: tuple[str, int]) -> None:
        """Latch the outbound destination onto the peer's real media source.

        Symmetric-RTP (comedia) NAT traversal: the SDP ``c=``/``m=`` address can
        be a private or SBC-rewritten address the peer's media never actually
        comes from, so we send our RTP back to the source tuple of the peer's
        first genuine RTP packet instead.

        Anti-spoofing — three guards before we move the target:

        * ``self._symmetric`` must be on (``HERMES_VOIP_RTP_SYMMETRIC``); when
          off we always honour the SDP address.
        * We latch only ONCE per call (``self._latched``): the first valid source
          wins and a later packet from a different tuple (a spoof, or a re-routed
          media path) cannot move it.
        * The caller has already proven the datagram is genuine RTP (it parsed,
          or — under SRTP — authenticated), and we additionally require the
          negotiated audio payload type, so neither random noise that happens to
          set the RTP version bits nor an off-codec stray triggers a latch.

        Latching to a tuple we are already sending to is a silent no-op (no log).
        """
        if not self._symmetric or self._latched:
            return
        if packet.payload_type != self._payload_type:
            return  # not the negotiated audio stream — not a latch trigger
        self._latched = True
        if source == self._outbound_addr:
            return  # already aimed here (SDP address matched reality); nothing to do
        self._outbound_addr = source
        # The peer's media ip:port is operational, not PII — logging it is how a
        # live NAT'd call is traced.  No other identifier is emitted.
        _log.info("rtp: latched to %s:%d", source[0], source[1])

    async def _next_datagram(self) -> _Datagram | None:
        """Await the next inbound datagram, or return ``None`` if stopped.

        Races :meth:`asyncio.Queue.get` against the stop flag — AND, when the RTP
        inactivity watchdog is armed (``_media_timeout_secs > 0``), against a
        no-media deadline (ADR-0026). The consumer wakes promptly when :meth:`stop`
        is called even with the bounded recv queue full, and the call ends instead
        of hanging when media goes silent. When the stop flag and a delivered
        datagram tie, the stop flag wins (we never start draining a queue the
        caller asked us to abandon); a delivered datagram beats the watchdog.

        The deadline RE-ARMS on every datagram: a returned datagram ends this call,
        and the next :meth:`_next_datagram` call creates a FRESH watchdog task, so a
        live call (continuous media) never reaches the deadline. The watchdog firing
        sets :attr:`_media_timed_out` and the stop event so the call is classified
        as a failure (MEDIA_TIMEOUT) — distinct from a clean BYE.

        ``asyncio.CancelledError`` from the awaited tasks propagates to the caller
        (rule 37). The per-call racers (the fresh ``queue.get()`` and, when armed,
        the watchdog) are each cancelled AND awaited before this method returns or
        propagates, so neither is left pending (no "Task was destroyed but it is
        pending" warning). The stop-wait racer is created ONCE and REUSED across
        calls (it is not re-created per receive — efficiency, rule 22); it is never
        cancelled here, and is awaited+cleared in :meth:`stop` (and dropped in
        :meth:`connect` for a reused engine), so it never leaks either.

        Rollback is lossless: if the stop flag wins a race in which a datagram had
        already been dequeued, that datagram is parked in :attr:`_pending` (never
        re-queued, so a full queue cannot drop it) and returned first by the next
        call.

        Returns:
            The next datagram, or ``None`` when the stop flag is set or the
            inactivity watchdog fires.
        """
        # A datagram rolled back from a previous stop-tie is delivered first,
        # in order, independent of recv-queue capacity.
        if self._pending is not None:
            data = self._pending
            self._pending = None
            return data

        # The stop-wait racer is created ONCE and reused across every call
        # (efficiency, rule 22): the inbound generator calls this ~50x/s and the
        # stop flag flips at most once per call, so re-creating a `wait()` Task per
        # receive is pure churn. We park ONE await on the event and reuse the Task;
        # it is never cancelled here (only the per-call get/watchdog racers are) and
        # is awaited+cleared in stop(). A stop set BEFORE the first await still makes
        # this Task resolve on the next loop step, so the stop precedence below
        # applies uniformly — including the stop-with-queued-datum park (no separate
        # is_set() fast path, which would have dropped the queued datum on stop).
        #
        # Measured task churn (rule 22; counted via asyncio.ensure_future over 100
        # receive iterations): watchdog ARMED 3.00 -> 2.00 tasks/call, watchdog OFF
        # 2.00 -> 1.00 tasks/call. The stop-wait Task is now amortised to ~0 per
        # call (created once, reused), halving the per-receive task creation on the
        # event-loop hot path at ~50 receives/s.
        stop_task = self._stop_wait_task
        if stop_task is None or (stop_task.done() and stop_task.cancelled()):
            stop_task = asyncio.ensure_future(self._stop_event.wait())
            self._stop_wait_task = stop_task

        # One fresh queue.get() racer per call (a Future is single-shot). On a clean
        # receive its result is the datagram; on a stop/watchdog win it is cancelled
        # AND awaited below so nothing is left pending.
        get_task: asyncio.Task[_Datagram] = asyncio.ensure_future(
            self._recv_queue.get()
        )
        # The inactivity-watchdog racer (ADR-0026), only when armed. A fresh task per
        # call re-arms the deadline on every datagram. ``None`` when the watchdog is
        # off (``_media_timeout_secs <= 0``) — the legacy stop/BYE-only behaviour.
        watchdog_task: asyncio.Task[None] | None = None
        if self._media_timeout_secs > 0:
            watchdog_task = asyncio.ensure_future(
                self._watchdog_sleep(self._media_timeout_secs)
            )
        # Only these per-call tasks are cancelled+awaited when they lose (the reused
        # stop_task is NOT — it lives across calls). The tuple is typed at the
        # ``object`` element so the heterogeneous tasks share one static type
        # (tuples are covariant, so this widening is sound).
        per_call_tasks: tuple[asyncio.Task[object], ...] = (
            (get_task,) if watchdog_task is None else (get_task, watchdog_task)
        )
        # The awaitables raced this call: the reused stop_task INCLUDED in the race
        # but EXCLUDED from the per-call cleanup above.
        race_tasks: tuple[asyncio.Task[object], ...] = (*per_call_tasks, stop_task)
        try:
            await asyncio.wait(
                set(race_tasks),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # Cancel whichever PER-CALL tasks did not complete, then AWAIT all so
            # their cancellation is fully processed before we return — nothing is
            # left pending. The reused stop_task is deliberately NOT cancelled here
            # (it is reused next call and retired in stop()). return_exceptions=True
            # absorbs the losers' CancelledError; a cancellation of THIS coroutine
            # still propagates out of the gather below (and thus out of
            # _next_datagram) per rule 37.
            for task in per_call_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*per_call_tasks, return_exceptions=True)

        # Stop wins over a delivered datagram (abandon a full queue on stop).
        if stop_task.done() and not stop_task.cancelled():
            # A datagram may have been dequeued in the same loop step; park it
            # losslessly so the next call returns it (no silent drop).
            if get_task.done() and not get_task.cancelled():
                self._pending = get_task.result()
            return None

        # A delivered datagram beats the watchdog: hand it back (and re-arm the
        # deadline on the next call). Checked BEFORE the watchdog so a packet that
        # arrived in the same loop step as the deadline is never discarded.
        if get_task.done() and not get_task.cancelled():
            return get_task.result()

        # The inactivity watchdog fired (no datagram within the window): end the
        # call as a media timeout (the silent-drop call no longer hangs forever).
        if (
            watchdog_task is not None
            and watchdog_task.done()
            and not watchdog_task.cancelled()
        ):
            self._media_timed_out = True
            self._stop_event.set()
            _log.warning(
                "rtp: no inbound media for %.1fs — ending call (MEDIA_TIMEOUT)",
                self._media_timeout_secs,
            )
            return None

        # No racer produced a result without being cancelled — only reachable if a
        # cancellation of THIS coroutine is in flight, which the gather above
        # re-raises. Returning None is the safe, call-ending default (rule 37: no
        # silent hang). In practice the gather propagates the CancelledError first.
        return None

    async def send_audio(self, frame: PcmFrame) -> None:
        """Encode and packetise one near-end frame; gate on hold state.

        When :attr:`on_hold` is ``True`` the datagram is silently discarded.
        Otherwise the frame is resampled to the codec's wire rate (8 kHz G.711,
        16 kHz G.722) if it arrives at any other rate — e.g. a 24 kHz TTS frame —
        re-framed into whole 20 ms slices (carry-buffered across calls so a
        sub-frame remainder is not silence-padded per chunk), encoded, packed into
        an RTP packet (incrementing seq/timestamp), optionally SRTP-protected, and
        sent to the remote. The stream is deadline-paced to one ``ptime`` per packet
        (see :meth:`_transmit_frame`).

        Args:
            frame: PCM16 audio at ANY sample rate.  A frame already at the wire rate
                is encoded directly; any other rate (e.g. the TTS provider's 24 kHz
                output) is resampled to the wire rate first (ADR-0017/0022), so this
                method never raises on an off-rate frame — it converts.
        """
        if self.on_hold:
            return

        if self._transport is None:
            if self._ever_connected:
                # Engine has been stopped mid-call (teardown while TTS was in
                # flight). Dropping this frame is correct — the call is ending.
                # This is intentional graceful degradation (AGENTS.md rule 37
                # exemption: the stopped state is established control flow, not
                # an unexpected error; raising here would propagate through the
                # TaskGroup and tear the call down with an abnormal exit instead
                # of a clean BYE).
                return
            msg = "send_audio called before connect()"
            raise RuntimeError(msg)

        wire_rate_frame = self._to_wire_rate(frame)

        # Re-frame the resampled 8 kHz PCM into standard 20 ms (160-sample)
        # slices and emit one RTP packet per WHOLE slice.
        #
        # WHY (silence at the gateway): one telephony RTP packet carries exactly
        # one ptime of WIRE-RATE audio (G.711: 160 samples = 20 ms @ 8 kHz; G.722:
        # 320 samples = 20 ms @ 16 kHz). TTS providers hand send_audio a frame per
        # producer chunk, NOT per 20 ms: sherpa-Kokoro emits a few huge chunks and
        # ElevenLabs streams MANY small HTTP chunks. Sending one oversized payload
        # makes the gateway silently discard it (the tone path was immune because
        # generate_tone_frames() already yields whole frames), so we MUST split
        # into whole wire-rate frames.
        #
        # WHY (the "very choppy" fix): a producer chunk is rarely a whole multiple
        # of one frame, so each call leaves a sub-frame remainder. Zero-padding
        # that remainder to a full frame on EVERY call injects a slug of silence
        # at every chunk boundary -- inaudible as a single event for Kokoro's few
        # chunks, but a click/gap per HTTP chunk for ElevenLabs (~12+ per second)
        # == "very choppy". Instead we BUFFER the leftover wire-rate bytes in
        # self._tx_buffer and prepend them to the next chunk, so the concatenated
        # outbound stream is sample-continuous (no interior silence). The residual
        # tail is padded to one final frame exactly once, in stop().
        #
        # The chunk size is at the WIRE sample rate (codec-derived), but the RTP
        # timestamp advances at the RTP CLOCK rate (also codec-derived). For G.711
        # these are equal; for G.722 they differ (320-sample frame, +160 clock) —
        # RFC 3551's 8000-clock/16000-sample quirk, handled via the descriptor.
        wire_samples_per_frame = (self._wire_sample_rate * self._ptime) // 1000
        ts_increment = (self._rtp_clock_rate * self._ptime) // 1000
        chunk_bytes = wire_samples_per_frame * 2  # PCM16 = 2 bytes per sample

        # _tx_buffer is the ORDERED source of truth for not-yet-ENCODED outbound
        # audio: append this chunk, then drain whole frames off the FRONT one at a
        # time. _transmit_frame encodes the frame, parks the encoded datagram in
        # _inflight_wire, then paces (the one yield point) and sends. So at any await
        # the still-unsent audio is split across exactly two ordered places — the
        # in-flight encoded datagram (front) and the raw remainder in _tx_buffer — and
        # stop()'s flush delivers them in that order. No frame is dropped, none
        # re-sent, none reordered.
        # Hold the TX lock across the whole drain (ADR-0031) so a concurrent
        # send_dtmf cannot interleave its packets into this utterance nor race the
        # shared _seq/_ts/_tx_buffer. The lock releases on every exit path
        # (including the early returns below) because it is an ``async with``.
        async with self._tx_lock:
            self._tx_buffer.extend(wire_rate_frame.samples)
            # Snapshot the flush generation for this drain: if a barge-in flush bumps
            # it mid-drain (ADR-0028), _transmit_frame suppresses the in-flight frame
            # and this loop stops, so the rest of the superseded utterance never goes
            # out.
            drain_generation = self._flush_generation
            while len(self._tx_buffer) >= chunk_bytes:
                # Remove the frame from the buffer as we hand it to _transmit_frame,
                # which takes ownership: on a clean send the frame is on the wire; on a
                # stop racing its pacing sleep the (already-encoded) frame lives in
                # _inflight_wire for the flush. Either way it must NOT remain in
                # _tx_buffer (that would re-encode/duplicate it). Copy the frame to
                # immutable ``bytes`` (PcmFrame.samples is bytes, and the copy is
                # detached from the buffer) then ``del`` it off the front in place —
                # no whole-buffer realloc per drained frame.
                chunk = bytes(self._tx_buffer[:chunk_bytes])
                del self._tx_buffer[:chunk_bytes]
                sent = await self._transmit_frame(
                    chunk,
                    wire_rate_frame.monotonic_ts_ns,
                    ts_increment,
                    flush_generation=drain_generation,
                )
                if not sent:
                    # Either stop() nulled the transport during the pacing sleep (the
                    # encoded frame is parked in _inflight_wire for stop()'s flush), or
                    # a barge-in flush superseded this utterance (flush_outbound already
                    # cleared _tx_buffer and sent the fade). Either way: stop draining —
                    # the flush owns delivery from here. Nothing to put back.
                    return
                # Read _transport through a fresh local so mypy does not treat this as
                # unreachable: the pre-loop guard narrowed self._transport to non-None
                # and that narrowing does not survive the await inside _transmit_frame
                # at runtime (stop() may have nulled it during the pacing sleep).
                transport_after_send: _DatagramSink | None = self._transport
                if transport_after_send is None:
                    # The frame WAS sent and is already removed from the buffer; the
                    # remaining buffer is owned by stop()'s flush. Stop draining.
                    return

    async def _transmit_frame(
        self,
        chunk: bytes,
        monotonic_ts_ns: int,
        ts_increment: int,
        *,
        flush_generation: int,
    ) -> bool:
        """Encode one 20 ms frame, then deadline-pace, then send it on the wire.

        ``chunk`` is one ptime of WIRE-RATE PCM16 (codec-derived rate). ``ts_increment``
        is the RTP timestamp delta for one frame (the RTP CLOCK rate — equal to the
        wire sample count for G.711, but HALF it for G.722: 160 clock units for a
        320-sample wideband frame, RFC 3551).

        **Deadline pacing** (the G.722 jitter fix): the costly synchronous work — the
        encode (G.722 is pure-Python, ~1.3 ms/frame and variable), pack, and optional
        SRTP protect — happens FIRST; only THEN does the method sleep the time
        remaining until this frame's scheduled deadline (``self._next_send_at``) and
        ``sendto`` immediately on wake. So the datagram leaves the socket ON the fixed
        deadline grid regardless of how long the encode took, and the encode cost is
        absorbed INTO the 20 ms interval rather than added on top — a steady 50 pps
        for any codec. The old model (``sendto`` then a flat ``sleep(ptime)``) made
        the realized interval ``ptime + encode``, which drifted audibly on G.722.

        The first frame of a stream finds ``_next_send_at is None``, anchors the grid
        to ``now``, and sends with no wait (the greeting must go out immediately so a
        NAT'd gateway latches). A frame that overruns its slot by up to a ptime clamps
        the wait to zero — no negative sleep, no busy-spin. If the grid has fallen
        more than one ptime behind (a real idle gap between TTS utterances) it
        re-anchors to ``now`` so the next burst is paced rather than dumped
        back-to-back catching up (cross-vendor review finding).

        **Stop race**: the encode commits the stateful encoder and the seq/ts, so the
        already-built datagram is parked in :attr:`_inflight_wire` across the pacing
        sleep — the sole yield point. If a concurrent :meth:`stop` nulls the transport
        during that sleep, this method returns ``False`` leaving the parked datagram
        for :meth:`_flush_tx_tail` to send FIRST, in order (re-deriving it from
        ``_tx_buffer`` would double-advance the encoder and reorder the stream). On a
        clean send the slot is cleared and ``True`` returned. Whether to keep draining
        after a ``True`` is the caller's decision (it re-checks the transport, which
        :meth:`stop` may have nulled — the frame was still sent).
        """
        if self._transport is None:
            return False

        # Do all the CPU work BEFORE pacing so the encode time is absorbed into the
        # interval, not added after the send. Encode + pack + protect now, and COMMIT
        # the seq/ts (the stateful encoder has advanced; the packet bytes bake in this
        # seq/ts), so a stop racing the pacing sleep below sends exactly these bytes
        # and the flush that follows continues from the next seq/ts — no collision,
        # no reorder.
        chunk_frame = PcmFrame(
            samples=chunk,
            sample_rate=self._wire_sample_rate,
            monotonic_ts_ns=monotonic_ts_ns,
        )
        payload = self._encode(chunk_frame)

        pkt = RtpPacket(
            payload_type=self._payload_type,
            sequence_number=self._seq,
            timestamp=self._ts,
            ssrc=_OUTBOUND_SSRC,
            payload=payload,
        )
        wire = self._srtp_out.protect(pkt) if self._srtp_out is not None else pkt.pack()
        self._seq = (self._seq + 1) % (1 << 16)
        self._ts = (self._ts + ts_increment) % (1 << 32)
        # Park the encoded datagram so a stop() during the pacing sleep can still send
        # it (in order) from _flush_tx_tail. Cleared the instant it reaches the wire.
        self._inflight_wire = wire

        # Deadline pace: wait only the time remaining to this frame's slot, THEN send
        # on wake, so the datagram leaves on the fixed grid (the encode above is
        # already done). The first frame anchors the grid and sends immediately.
        #
        # Re-anchor a STALE grid (codex review): if the deadline is already more than
        # one ptime in the past — a real idle gap between TTS chunks/utterances, or a
        # call that paused — catching up by only +1 ptime per frame would dump the
        # next burst back-to-back (wait <= 0 for many frames) until the schedule
        # caught up. So when the schedule has fallen behind by more than a ptime,
        # snap _next_send_at to ``now``: the first frame after the gap sends
        # immediately and the rest of the burst paces a steady 20 ms from there.
        # Adjacent frames in a continuous stream are at most ~ptime behind, so normal
        # pacing is untouched; only a genuine gap triggers the re-anchor.
        ptime_s = self._ptime / 1000.0
        now = self._pace_clock()
        if self._next_send_at is None or now - self._next_send_at > ptime_s:
            self._next_send_at = now
        wait = self._next_send_at - now
        if wait > 0:
            await self._sleep(wait)

        # Barge-in flush race (ADR-0028): if flush_outbound() ran during the pacing
        # sleep it bumped the flush generation, cleared _tx_buffer, and already sent
        # the fade-to-silence. This pre-flush frame is now superseded — sending it
        # AFTER the fade would put a full-amplitude packet past the ramp (a click).
        # Drop it (it is NOT parked in _inflight_wire; flush_outbound nulled that) and
        # report False so the drain loop stops. Checked before the transport-null /
        # send below so a flush that did NOT null the transport still suppresses it.
        if self._flush_generation != flush_generation:
            self._inflight_wire = None
            return False

        # Re-read the transport AFTER the pacing sleep: stop() may have nulled it while
        # we waited. The explicit ``| None`` annotation defeats mypy's stale narrowing
        # from the entry guard (it cannot see the concurrent stop() mutation across the
        # await). If nulled, this frame did NOT go out HERE — report False and leave it
        # parked in _inflight_wire for stop()'s flush to send first (the lossless,
        # in-order-on-stop contract the old post-send sleep upheld).
        transport: _DatagramSink | None = self._transport
        if transport is None:
            return False

        # The frame is going out on the fixed grid: advance the deadline by exactly one
        # ptime (a single slow frame does not shift every subsequent packet) and clear
        # the in-flight slot now that the datagram is on the wire. The anchor block
        # above guarantees _next_send_at is set (not None) on every path to here.
        self._next_send_at += ptime_s
        self._inflight_wire = None

        # Log the first packet sent in this call.
        if not self._first_tx_logged:
            self._first_tx_logged = True
            _log.info(
                "rtp tx: first packet -> %s:%d pt=%d ssrc=0x%08x",
                self._outbound_addr[0],
                self._outbound_addr[1],
                self._payload_type,
                _OUTBOUND_SSRC,
            )

        # Rolling TX amplitude: accumulate the peak across each
        # _TX_AMPLITUDE_LOG_PERIOD-packet window (1 second at 50 pps) and emit one
        # INFO line at the boundary.  This replaces the old "first 3 chunks"
        # approach, which always sampled silent TTS lead-in and gave the operator
        # a misleading zero reading.
        # Use audioop.max (51x faster: ~259 ns vs ~13,342 ns per frame) for the peak
        # absolute sample; returns 0 for silence, max |sample| for any audio.
        if len(chunk) > 0:
            chunk_peak: int = audioop.max(chunk, 2)
            self._tx_amplitude_period_peak = max(
                self._tx_amplitude_period_peak, chunk_peak
            )
        self._tx_amplitude_chunk_count += 1
        if self._tx_amplitude_chunk_count % _TX_AMPLITUDE_LOG_PERIOD == 0:
            period = self._tx_amplitude_chunk_count // _TX_AMPLITUDE_LOG_PERIOD
            _log.info(
                "rtp tx: period %d peak_amplitude=%d (%.1f%% full-scale)",
                period,
                self._tx_amplitude_period_peak,
                self._tx_amplitude_period_peak / 327.67,
            )
            self._tx_amplitude_period_peak = 0

        transport.sendto(wire, self._outbound_addr)
        # RTCP sender bookkeeping (RFC 3550 §6.4.1, ADR-0061): one more RTP data
        # packet and ``len(payload)`` more PAYLOAD octets (the SR octet count excludes
        # RTP/SRTP headers). Counted only for a frame actually on the wire, in send
        # order, mirroring the AEC tap below.
        self._rtcp_packets_sent += 1
        self._rtcp_octets_sent = (self._rtcp_octets_sent + len(payload)) & 0xFFFFFFFF
        # Tap this outbound frame as the AEC reference (ADR-0033): it is the signal
        # the gateway will reflect back, so the canceller subtracts it from the
        # matching inbound frames. Pushed AFTER sendto so the reference is recorded
        # only for audio actually on the wire, in send order. ``chunk`` is wire-rate
        # PCM16; ``push_reference`` downsamples to the analysis rate if they differ
        # (Opus). A no-op when AEC is disabled.
        self._push_aec_reference(chunk)
        # The frame is on the wire. Pacing already happened above (the wait preceded
        # this send so the datagram leaves on the fixed grid); there is deliberately
        # NO post-send sleep. stop() may null _transport before the caller's NEXT
        # frame, but that governs whether the caller keeps draining (it re-checks
        # _transport) — it does not un-send this frame, so report True.
        return True

    # ------------------------------------------------------------------
    # RTCP control channel (RFC 3550 §6, RFC 5761, ADR-0061)
    # ------------------------------------------------------------------

    def _note_rtp_received(
        self, *, seq: int, rtp_timestamp: int, arrival_ts: float, ssrc: int
    ) -> None:
        """Record one inbound RTP packet against its source's RTCP statistics.

        Looks up (or creates) the :class:`~hermes_voip.rtcp.ReceptionStats` for
        ``ssrc`` and feeds it the packet so our SR/RR report blocks carry the right
        loss, jitter, and extended-highest-sequence numbers (RFC 3550 Appendix
        A.1/A.3/A.8). The clock rate is the codec's RTP clock (the source uses the
        same negotiated codec as us). Called from :meth:`_inbound_gen` for every
        genuine audio packet (after the PT filter, before the jitter buffer).
        """
        stats = self._reception.get(ssrc)
        if stats is None:
            stats = ReceptionStats(clock_rate=self._rtp_clock_rate)
            self._reception[ssrc] = stats
        stats.on_packet(seq=seq, rtp_timestamp=rtp_timestamp, arrival_ts=arrival_ts)

    def _report_blocks(self) -> tuple[ReportBlock, ...]:
        """Build a reception report block for each source we receive from (§6.4.1).

        Each block carries the LSR/DLSR for that source: the compact NTP of the last
        SR we received from it and the delay (1/65536 s units) since we received it
        (0/0 when we have had no SR from it yet). Snapshotting a block also rolls the
        source's loss interval forward (so the next block covers only new packets).
        """
        now_unix = self._ntp_clock()
        blocks: list[ReportBlock] = []
        for src_ssrc, stats in self._reception.items():
            lsr, dlsr = self._lsr_dlsr_for(src_ssrc, now_unix)
            blocks.append(stats.report_block(source_ssrc=src_ssrc, lsr=lsr, dlsr=dlsr))
        return tuple(blocks)

    def _lsr_dlsr_for(self, src_ssrc: int, now_unix: float) -> tuple[int, int]:
        """The (LSR, DLSR) fields for a report block about ``src_ssrc`` (§6.4.1).

        LSR is the compact (middle-32) NTP of the last SR we received from the
        source; DLSR is the delay since we received it, in 1/65536 s units. Both are
        0 when we have received no SR from that source.
        """
        record = self._last_sr_from.get(src_ssrc)
        if record is None:
            return 0, 0
        lsr, received_at = record
        delay_s = max(0.0, now_unix - received_at)
        dlsr = int(delay_s * (1 << 16)) & 0xFFFFFFFF
        return lsr, dlsr

    def _sdes(self) -> SourceDescription:
        """Our SDES packet carrying the outbound SSRC's CNAME (RFC 3550 §6.5)."""
        return SourceDescription(
            chunks=(SdesChunk(ssrc=_OUTBOUND_SSRC, cname=self._cname),)
        )

    def build_rtcp_report(self) -> bytes | None:
        """Build the periodic compound RTCP report, or ``None`` if nothing to report.

        Returns a compound (RFC 3550 §6.1): a **Sender Report** when we have sent
        media this call (it carries our NTP/RTP timestamp pair, sender packet/octet
        counts, and a report block per received source), otherwise a **Receiver
        Report** when we have received media (report blocks only). Both are followed
        by an SDES CNAME. Returns ``None`` only when we have neither sent nor
        received any media yet (there is nothing meaningful to report).

        This builds the bytes; :meth:`run_rtcp` schedules and sends them. Pure and
        synchronous, so it is directly unit-testable.
        """
        blocks = self._report_blocks()
        if self._rtcp_packets_sent > 0:
            ntp = to_ntp(self._ntp_clock())
            self._last_sr_sent_compact = compact_ntp_now(self._ntp_clock())
            sr = SenderReport(
                ssrc=_OUTBOUND_SSRC,
                ntp_timestamp=ntp,
                rtp_timestamp=self._ts,
                packet_count=self._rtcp_packets_sent,
                octet_count=self._rtcp_octets_sent,
                report_blocks=blocks,
            )
            return build_compound((sr, self._sdes()))
        if blocks:
            rr = ReceiverReport(ssrc=_OUTBOUND_SSRC, report_blocks=blocks)
            return build_compound((rr, self._sdes()))
        return None

    def _record_outbound_sr_ntp(self, compact_ntp: int) -> None:
        """Record the compact NTP of an SR we sent, for later RTT computation.

        Exposed for the adapter/tests to arm RTT when an SR is sent outside the
        normal :meth:`build_rtcp_report` path; the report builder also sets it.
        """
        self._last_sr_sent_compact = compact_ntp

    def ingest_rtcp(self, data: bytes) -> None:
        """Parse one inbound RTCP datagram and update per-call quality (§6.4.1).

        Handles a compound packet (RFC 3550 §6.1):

        * a peer **SR** records the compact NTP of its send time and our receive
          time, so our next report block about that source carries the right
          LSR/DLSR (we acknowledge their SR back to them).
        * a peer **SR/RR** report block ABOUT OUR SSRC gives the far-end view of our
          stream (fraction lost, cumulative lost, jitter) and — via its LSR/DLSR —
          the round-trip time (:meth:`call_quality`).
        * a **BYE** is accepted (the peer is leaving); SDES is ignored.

        A structurally broken datagram raises :class:`~hermes_voip.rtcp.RtcpError`
        (rule 37 — the error is not swallowed); the caller (adapter) decides whether
        a single bad RTCP datagram ends the call or is logged and dropped.
        """
        now_unix = self._ntp_clock()
        for packet in parse_compound(data):
            if isinstance(packet, SenderReport):
                # Acknowledge the peer's SR: remember its compact (middle-32) NTP +
                # our receive time so our next RR/SR block reports LSR/DLSR for this
                # source (RFC 3550 §6.4.1).
                self._last_sr_from[packet.ssrc] = (
                    (packet.ntp_timestamp >> 16) & 0xFFFFFFFF,
                    now_unix,
                )
                self._absorb_remote_blocks(packet.report_blocks, now_unix)
            elif isinstance(packet, ReceiverReport):
                self._absorb_remote_blocks(packet.report_blocks, now_unix)
            elif isinstance(packet, Bye):
                _log.debug(
                    "rtcp rx: BYE for ssrc(s) %s", [hex(s) for s in packet.ssrcs]
                )
            # SourceDescription: no per-call state to update (CNAME is informational).

    def _absorb_remote_blocks(
        self, blocks: tuple[ReportBlock, ...], now_unix: float
    ) -> None:
        """Update the far-end view + RTT from report blocks about our SSRC (§6.4.1)."""
        for block in blocks:
            if block.ssrc != _OUTBOUND_SSRC:
                continue  # a block about some other source; not our stream
            self._remote_report = block
            rtt = rtt_from_report_block(
                now_compact_ntp=compact_ntp_now(now_unix),
                lsr=block.lsr,
                dlsr=block.dlsr,
            )
            if rtt is not None:
                self._rtt_seconds = rtt

    def rtcp_interval(
        self, *, randomize: bool = True, rng: random.Random | None = None
    ) -> float:
        """The RTCP transmission interval in seconds (RFC 3550 §6.2, ADR-0061).

        A 2-party voice call (members=2; senders=1 receive-only or 2 both-ways) with
        the small per-call RTCP bandwidth floors at the §6.2 5 s minimum, so a real
        call reports about every 5 s. ``randomize`` applies the §6.3.1 jitter;
        ``rng`` is injectable for deterministic tests.
        """
        senders = 1 if self._rtcp_packets_sent > 0 else 0
        senders += len(
            self._reception
        )  # each remote source we receive from is a sender
        members = 2  # this engine + the one remote peer (point-to-point telephony)
        return compute_rtcp_interval(
            members=members,
            senders=max(1, senders),
            rtcp_bw=self._rtcp_bandwidth,
            we_sent=self._rtcp_packets_sent > 0,
            avg_rtcp_size=self._avg_rtcp_size,
            randomize=randomize,
            rng=rng,
        )

    def _emit_rtcp(self, datagram: bytes) -> None:
        """Send one RTCP datagram via the injected sink, or muxed over the RTP path.

        On a SECURED engine (``_srtcp_out`` set, RFC 3711 §3.4 / ADR-0066) the cleartext
        compound is first wrapped in the SRTCP transform — so the bytes that ever leave
        this process are authenticated+encrypted, never cleartext RTCP on a secured
        5-tuple. On the plain-RTP path (``_srtcp_out`` is ``None``) the cleartext
        compound is sent unchanged (the legacy behaviour, byte-for-byte).

        With an ``rtcp_send`` sink injected (the separate-RTCP-port case the adapter
        wires) the (S)RTCP wire goes there; otherwise it is multiplexed onto the RTP
        transport (RFC 5761 rtcp-mux) via ``_transport.sendto``. A closed/absent
        transport drops the datagram silently (the call is ending). The average RTCP
        size (§6.3.3) is smoothed over the ACTUAL wire bytes (the SRTCP trailer + tag
        count toward the bandwidth budget) for the next interval calculation.
        """
        wire = (
            self._srtcp_out.protect(datagram)
            if self._srtcp_out is not None
            else datagram
        )
        self._avg_rtcp_size += (len(wire) - self._avg_rtcp_size) * _AVG_RTCP_SIZE_GAIN
        if self._rtcp_send is not None:
            self._rtcp_send(wire)
            return
        transport = self._transport
        if transport is not None:
            transport.sendto(wire, self._outbound_addr)

    async def run_rtcp(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        send_bye_on_stop: bool = False,
        rng: random.Random | None = None,
    ) -> None:
        """Run the periodic RTCP sender until the call stops (RFC 3550 §6.2, ADR-0061).

        Each cycle sleeps one (randomised) §6.2 interval then sends the compound
        report from :meth:`build_rtcp_report` via :meth:`_emit_rtcp`. The loop exits
        when the engine's stop event is set (:meth:`stop`); on exit it optionally
        flushes a final BYE (RFC 3550 §6.6) so the peer stops reporting on us
        promptly. Cancellation propagates (rule 37).

        The adapter starts this with ``asyncio.create_task`` AFTER the call's media
        is up — it is the live activation point (the engine never auto-starts it, so
        the SDES/UDP and ICE paths choose the RTCP socket/mux at the adapter).
        ``sleep`` is injectable for deterministic tests (defaults to the engine's
        outbound ``sleep``).

        Args:
            sleep: Async one-shot delay; defaults to the engine's injected ``sleep``.
            send_bye_on_stop: Flush an RTCP BYE for our SSRC when the loop stops.
            rng: Random source for the §6.3.1 interval jitter (injectable in tests).
        """
        delay = sleep if sleep is not None else self._sleep
        # The loop's only exit paths are the stop event (clean) and a CancelledError
        # from stop()'s task cancel; both run the finally, which flushes the BYE.
        # CancelledError is not caught here, so it propagates (rule 37) after the BYE.
        try:
            while not self._stop_event.is_set():
                await delay(self.rtcp_interval(rng=rng))
                if self._stop_event.is_set():
                    break
                report = self.build_rtcp_report()
                if report is not None:
                    self._emit_rtcp(report)
        finally:
            if send_bye_on_stop and self._rtcp_packets_sent > 0:
                self._emit_rtcp(self._bye_compound())

    def _bye_compound(self) -> bytes:
        """A compound RTCP datagram carrying a final report + SDES + BYE.

        RFC 3550 §6.1: every RTCP packet — including a BYE — is sent in a compound
        packet that begins with an SR or RR (and an SDES). A standalone BYE is not
        a valid compound and peers may reject it (codex review, ADR-0061). So the
        leaving datagram leads with the latest SR/RR + SDES, then the BYE for our
        SSRC (§6.6). ``build_rtcp_report`` always returns non-None here because the
        caller guards on ``_rtcp_packets_sent > 0`` (we have sent media → an SR).
        """
        report = self.build_rtcp_report()
        bye = Bye(ssrcs=(_OUTBOUND_SSRC,), reason=None)
        if report is None:
            # Defensive: no SR/RR available (no media sent or received). Lead with an
            # empty RR so the compound still begins with a report packet (§6.1).
            rr = ReceiverReport(ssrc=_OUTBOUND_SSRC, report_blocks=())
            return build_compound((rr, self._sdes(), bye))
        # Append the BYE to the existing SR/RR + SDES compound (both already 32-bit
        # aligned, so concatenation stays a valid compound).
        return report + bye.pack()

    async def start_rtcp(
        self,
        *,
        mux: bool,
        rtp_payload_types: tuple[int, ...],
        remote_rtcp_addr: tuple[str, int] | None = None,
        send_bye_on_stop: bool = True,
    ) -> None:
        """Activate RTCP for this call (ADR-0061 adapter activation, RFC 3550 §6).

        The live activation point the adapter calls AFTER :meth:`connect` (the engine
        never auto-starts RTCP — choosing the socket/mux needs the negotiated SDP the
        adapter holds). It:

        1. Chooses the RTCP transport from the negotiated mux. ``mux=True`` (RFC 5761)
           leaves the outbound RTCP riding the RTP transport and engages the inbound
           muxed-RTCP demux in :meth:`_inbound_gen`. ``mux=False`` binds a sibling UDP
           socket on RTP-port+1 (RFC 3550 §11), routes our outbound RTCP to
           ``remote_rtcp_addr`` through it, and starts a reader pumping that socket
           into :meth:`ingest_rtcp` (the RTP queue never sees this RTCP).
        2. Starts the periodic :meth:`run_rtcp` loop, registering the task on the
           engine so :meth:`stop` cancels + awaits it (and flushes the closing BYE).

        Idempotent-safe: a second call while RTCP is already active is a no-op.

        SECURED-TRANSPORT GUARD (codex review #3, flipped by ADR-0066): on a secured
        engine (SRTP ``_srtp_in``/``_srtp_out`` set, or an ICE/DTLS pipe ``_ice``) RTCP
        activates ONLY when the SRTCP transform is wired (``_has_srtcp`` — BOTH
        ``_srtcp_in`` and ``_srtcp_out`` set). With SRTCP, every outbound RTCP is
        encrypted+authenticated (:meth:`_emit_rtcp`) and every inbound one is
        unprotected (:meth:`_ingest_rtcp_datagram`), so no cleartext RTCP ever rides the
        secured 5-tuple. WITHOUT SRTCP a secured engine leaves RTCP DORMANT (no-op +
        WARNING): cleartext RTCP on an encrypted call would violate the negotiated
        profile and leak SSRC/CNAME/timing. The adapter also gates the secured paths,
        but this engine-level guard is the last line of defence.

        Args:
            mux: ``True`` to multiplex RTCP onto the RTP transport (RFC 5761), ``False``
                to use a separate RTCP socket on RTP-port+1.
            rtp_payload_types: The negotiated/answered RTP payload types. When ``mux``
                is ``True`` and any lies in the RFC 5761 §4 conflict range (64-95),
                RTCP is left dormant (the muxed RTP/RTCP demux would be ambiguous).
            remote_rtcp_addr: The peer's RTCP ``(host, port)`` — REQUIRED when
                ``mux=False`` (where to send our RTCP). Ignored when muxed (RTCP
                follows the RTP destination / comedia latch).
            send_bye_on_stop: Flush a closing RTCP BYE when the loop stops (default
                ``True`` — tells the peer our SSRC is leaving, RFC 3550 §6.6).

        Raises:
            ValueError: When ``mux=False`` but ``remote_rtcp_addr`` is ``None`` (the
                separate-port path has no destination), or when called before
                :meth:`connect` on the non-muxed UDP path (no local port yet).
        """
        if self._rtcp_active:
            return
        if self._is_secured and not self._has_srtcp:
            # Cleartext RTCP must never ride a secured 5-tuple. On a secured engine WITH
            # the SRTCP transform wired (``_has_srtcp``) RTCP is activated and every
            # datagram is SRTCP-protected (the guard FLIP, ADR-0066). Without it (SRTP
            # media only, no SRTCP keys) RTCP stays DORMANT — emitting cleartext RTCP on
            # an encrypted call would violate the profile and leak SSRC/CNAME/timing.
            _log.warning(
                "start_rtcp called on a secured engine with NO SRTCP transform — RTCP "
                "left DORMANT (cleartext RTCP on an encrypted 5-tuple would violate "
                "the profile and leak SSRC/CNAME). Wire srtcp_inbound + srtcp_outbound "
                "to activate secured RTCP."
            )
            return
        # RFC 5761 §4 defense-in-depth: an RTP payload type in 64-95 aliases the RTCP
        # packet-type byte on a muxed stream, so the RTP/RTCP demux is ambiguous. The
        # adapter already refuses mux (sibling port) for such a PT; this is the engine's
        # last-line guard for any other caller — refuse mux, leave RTCP dormant.
        if mux and any(
            _RTCP_MUX_CONFLICT_PT_MIN <= pt <= _RTCP_MUX_CONFLICT_PT_MAX
            for pt in rtp_payload_types
        ):
            _log.warning(
                "start_rtcp(mux=True) with an RFC 5761 §4 conflict-range RTP payload "
                "type (64-95) — RTCP left DORMANT (muxed RTP/RTCP demux would be "
                "ambiguous); a separate RTCP port is required for these payload types."
            )
            return
        if not mux:
            if remote_rtcp_addr is None:
                msg = "start_rtcp(mux=False) requires remote_rtcp_addr"
                raise ValueError(msg)
            # A sibling-socket failure (RTP-port+1 unavailable) must DEGRADE the call
            # to RTCP-off, never crash it (codex review #5): the adapter awaits this
            # during call setup, so an unhandled OSError here would reject the CALL.
            # _open_rtcp_socket closes any partial socket before raising, so on failure
            # we simply log and leave RTCP inactive — media continues.
            try:
                await self._open_rtcp_socket(remote_rtcp_addr)
            except OSError as exc:
                _log.warning(
                    "RTCP sibling socket (RTP-port+1) unavailable — RTCP left off, "
                    "media continues: %s",
                    exc,
                )
                return
        # Engage RTCP only AFTER any non-muxed socket is open, so the flags and the I/O
        # path are set up together. ``_rtcp_mux_active`` gates the inbound RTP-socket
        # demux and is set ONLY for the muxed path (RFC 5761); on the non-muxed path
        # RTCP rides the sibling socket and the RTP socket stays pure RTP.
        self._rtcp_active = True
        self._rtcp_mux_active = mux
        # RTCP cadence is WALL-TIME (RFC 3550 §6.2). Pin the loop to asyncio.sleep —
        # it must NOT inherit ``self._sleep``, the outbound-pacing seam callers (e.g.
        # the e2e harness) legitimately stub to a no-op for instant RTP pacing. With a
        # no-op there the §6.2 interval never elapses, the loop spins, and it starves
        # the media TX (a real e2e hang).
        self._rtcp_task = asyncio.create_task(
            self.run_rtcp(sleep=asyncio.sleep, send_bye_on_stop=send_bye_on_stop)
        )

    @property
    def _is_secured(self) -> bool:
        """True when this engine's media is encrypted (SRTP/SDES or ICE/DTLS-SRTP).

        RTCP activation is gated on this (codex review #3, ADR-0066): a secured session
        must never send/receive CLEARTEXT RTCP — it activates RTCP only when the SRTCP
        transform is wired (:attr:`_has_srtcp`). ``_srtp_in``/``_srtp_out`` cover SDES +
        DTLS-derived SRTP; ``_ice`` covers the WebRTC ICE/DTLS path (always secured); a
        non-None ``_srtcp_in``/``_srtcp_out`` (a secured-but-SRTP-stubbed test, or a
        secured call mid-wiring) also counts as secured.
        """
        return (
            self._srtp_in is not None
            or self._srtp_out is not None
            or self._ice is not None
            or self._srtcp_in is not None
            or self._srtcp_out is not None
        )

    @property
    def _has_srtcp(self) -> bool:
        """True when the SRTCP transform is fully wired for BOTH directions (ADR-0066).

        Secured RTCP needs both an inbound (unprotect) and an outbound (protect) SRTCP
        session — a half-wired engine could not both send and receive secured RTCP, so
        :meth:`start_rtcp` requires both before flipping a secured engine to ACTIVE.
        """
        return self._srtcp_in is not None and self._srtcp_out is not None

    async def _open_rtcp_socket(self, remote_rtcp_addr: tuple[str, int]) -> None:
        """Bind the sibling RTCP socket on RTP-port+1 and start its reader (non-mux).

        RFC 3550 §11: when RTCP is not multiplexed it uses the port one above the RTP
        port. Binds a non-blocking UDP socket on ``(local_address, local_port + 1)``,
        installs an :meth:`ingest_rtcp` sink that targets ``remote_rtcp_addr`` through
        it, and starts a reader task pumping inbound datagrams into
        :meth:`ingest_rtcp`. The socket + reader are torn down in :meth:`stop`.

        Raises:
            OSError: When the sibling port (RTP-port+1) cannot be bound (e.g. already
                in use). A partial socket is CLOSED before the error propagates so no
                file descriptor leaks; :meth:`start_rtcp` catches this and degrades the
                call to RTCP-off (codex review #5).
            ValueError: When called before :meth:`connect` (no local RTP port yet).
        """
        if self._local_port == 0:
            msg = "start_rtcp(mux=False) must be called after connect()"
            raise ValueError(msg)
        loop = asyncio.get_running_loop()
        rtcp_port = self._local_port + 1
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.bind((self._local_address, rtcp_port))
            queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
            protocol = _RtcpReceiver(queue)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol, sock=sock
            )
        except OSError:
            # Close the partial socket before propagating so no FD leaks (the asyncio
            # endpoint may not have taken ownership yet). start_rtcp degrades the call.
            sock.close()
            raise
        assert isinstance(transport, asyncio.DatagramTransport)  # noqa: S101 — UDP socket ⇒ DatagramTransport
        self._rtcp_transport = transport
        self._rtcp_local_port = sock.getsockname()[1]
        # Route outbound RTCP through this socket to the peer's RTCP address.
        self._rtcp_send = lambda d: transport.sendto(d, remote_rtcp_addr)
        self._rtcp_reader = loop.create_task(self._rtcp_recv_loop(queue))

    async def _rtcp_recv_loop(self, queue: asyncio.Queue[bytes]) -> None:
        """Pump the sibling RTCP socket's datagrams into ingest_rtcp (non-mux path).

        Each inbound datagram on the separate RTCP port is fed to
        :meth:`_ingest_rtcp_datagram` (which drops a malformed one rather than ending
        the call). Cancellation propagates (rule 37); the loop is cancelled in
        :meth:`stop`.
        """
        while True:
            data = await queue.get()
            self._ingest_rtcp_datagram(data)

    def _ingest_rtcp_datagram(self, data: bytes) -> None:
        """Feed one inbound RTCP datagram to :meth:`ingest_rtcp`, dropping a bad one.

        On a SECURED engine (``_srtcp_in`` set, RFC 3711 §3.4 / ADR-0066) the inbound
        SRTCP packet is first authenticated + decrypted to its cleartext compound; only
        then is it parsed. An SRTCP auth/replay/format failure (:class:`SrtcpError`) is
        an environmental event — the datagram is logged at DEBUG and DROPPED (the call
        stays up), exactly as a malformed RTP/RTCP datagram is, and the cleartext
        parser never sees unauthenticated bytes. On the plain-RTP path the cleartext
        datagram is parsed directly (unchanged).

        :meth:`ingest_rtcp` raises :class:`RtcpError` on a structurally broken
        (cleartext) datagram and leaves to the caller whether that ends the call (its
        contract). A single malformed inbound RTCP datagram is logged at DEBUG and
        dropped. Not swallowed silently (rule 37): it is logged, and only an
        :class:`SrtcpError` / :class:`RtcpError` is caught — any other exception
        propagates.
        """
        if self._srtcp_in is not None:
            try:
                data = self._srtcp_in.unprotect(data)
            except SrtcpError as exc:
                _log.debug(
                    "inbound SRTCP auth/replay/format failure — dropped: %s", exc
                )
                return
        try:
            self.ingest_rtcp(data)
        except RtcpError as exc:
            _log.debug("inbound RTCP datagram malformed — dropped: %s", exc)

    @property
    def call_quality(self) -> CallQuality:
        """A snapshot of the call's media quality from RTCP (ADR-0061).

        Combines OUR inbound reception view (loss/jitter we measured, from the
        per-source :class:`~hermes_voip.rtcp.ReceptionStats`) with the PEER's
        reported view of our outbound stream and the round-trip time. ``None`` fields
        mean "not yet measurable" (no RTP received, or no peer report yet). These are
        the SLO numbers (runbook 0014: packet loss, jitter, RTT).
        """
        local = self._local_quality_snapshot()
        remote = self._remote_report
        remote_fraction = (
            remote.fraction_lost / _RTCP_FRACTION_SCALE if remote is not None else None
        )
        remote_jitter_ms = (
            remote.jitter / self._rtp_clock_rate * _MS_PER_S
            if remote is not None
            else None
        )
        return CallQuality(
            local_fraction_lost=local[0],
            local_cumulative_lost=local[1],
            local_jitter_ms=local[2],
            remote_fraction_lost=remote_fraction,
            remote_cumulative_lost=remote.cumulative_lost if remote else None,
            remote_jitter_ms=remote_jitter_ms,
            rtt_seconds=self._rtt_seconds,
        )

    def _local_quality_snapshot(
        self,
    ) -> tuple[float | None, int | None, float | None]:
        """Our inbound (fraction_lost, cumulative_lost, jitter_ms) across all sources.

        A READ-ONLY view that does NOT roll the loss interval forward (unlike
        :meth:`_report_blocks`): it sums cumulative loss and takes the worst jitter
        across sources, so polling ``call_quality`` never disturbs the report
        cadence. ``(None, None, None)`` until any RTP is received.
        """
        if not self._reception:
            return None, None, None
        total_cumulative = 0
        worst_jitter_units = 0.0
        total_expected = 0
        total_lost = 0
        for stats in self._reception.values():
            snap = stats.snapshot()
            total_cumulative += snap.cumulative_lost
            worst_jitter_units = max(worst_jitter_units, snap.jitter)
            total_expected += snap.expected
            total_lost += max(0, snap.cumulative_lost)
        fraction = (total_lost / total_expected) if total_expected > 0 else 0.0
        jitter_ms = worst_jitter_units / self._rtp_clock_rate * _MS_PER_S
        return fraction, total_cumulative, jitter_ms

    async def send_dtmf(
        self,
        digits: str,
        *,
        tone_ms: int = _DEFAULT_DTMF_TONE_MS,
        gap_ms: int = _DEFAULT_DTMF_GAP_MS,
        volume: int = _DEFAULT_DTMF_VOLUME,
    ) -> None:
        """Send ``digits`` as DTMF on the active call, per the resolved send backend.

        The backend is :attr:`_dtmf_send_mode` (ADR-0036): ``RFC4733`` emits the
        named-event RTP train (described below); ``INBAND`` synthesises dual-tone PCM
        and sends it on the audio TX path (a G.711 call with no telephone-event);
        ``SIP_INFO`` is the :class:`~hermes_voip.call.CallSession`'s job, so the engine
        never resolves to it; ``UNAVAILABLE`` raises. The whole burst runs under the
        same TX mutex as :meth:`send_audio` regardless of backend.

        RFC 4733 mode wires the (separately-tested)
        :func:`hermes_voip.dtmf.event_payloads` generator onto this engine's TX path
        (ADR-0010/0031). For EACH digit it emits named-event packets at the NEGOTIATED
        telephone-event payload type, with:

        * the **marker bit set on the first packet** of the digit (RFC 4733 §2.5.1:
          the start of a new event), and clear on the rest;
        * a **constant RTP timestamp** across all of that digit's packets (the event
          start time — duration grows in the payload, the RTP timestamp does not);
        * the engine's outbound **SSRC** and a **monotonic sequence number** shared
          with the audio stream (so the receiver sees one coherent RTP stream);
        * the **three redundant end packets** RFC 4733 §2.5.1.4 requires (already
          produced by ``event_payloads``);
        * **SRTP protection** when SRTP is active (same as audio).

        Between digits the RTP timestamp advances by the tone duration and a silent
        inter-digit gap is paced, so a repeated digit is a distinct event the far
        end registers separately. The whole burst runs under the **same TX mutex as
        :meth:`send_audio`**, so DTMF never interleaves with audio nor races the
        shared seq/timestamp.

        Args:
            digits: The DTMF string to send (``0-9``, ``*``, ``#``, ``A``-``D``;
                case-insensitive). Empty ⇒ a no-op (nothing to send).
            tone_ms: Per-digit tone duration in milliseconds (default 100).
            gap_ms: Silent inter-digit gap in milliseconds (default 70).
            volume: Named-event power in -dBm0 (0-63; default 10).

        Raises:
            RuntimeError: In RFC 4733 mode, if the telephone-event payload type was not
                negotiated (the call's offer had no ``telephone-event``) — DTMF is NOT
                sent and the failure is explicit, never a silent no-op. Also if the send
                backend resolved to ``UNAVAILABLE`` (no usable backend on this call), or
                the engine was never connected.
            ValueError: If ``digits`` contains a non-DTMF character (propagated from
                :func:`~hermes_voip.dtmf.digit_to_event`), or ``tone_ms`` is outside
                the representable event-duration range.
        """
        if not digits:
            return
        if self._dtmf_send_mode is DtmfSendMode.INBAND:
            await self._send_inband_dtmf(digits, tone_ms=tone_ms, gap_ms=gap_ms)
            return
        if self._dtmf_send_mode is not DtmfSendMode.RFC4733:
            # SIP_INFO is the CallSession's job (it never reaches the engine);
            # UNAVAILABLE means no backend can run. Either way the engine must not
            # silently drop — the caller (CallSession.send_dtmf) routes SIP_INFO away
            # before here, so reaching this is a real misconfiguration.
            msg = (
                f"cannot send DTMF via the media engine in "
                f"{self._dtmf_send_mode.value} mode — no usable in-band/RFC 4733 "
                "backend for this call. DTMF is not sent (explicit failure)."
            )
            raise RuntimeError(msg)
        event_pt = self._telephone_event_payload_type
        if event_pt is None:
            msg = (
                "cannot send DTMF: no telephone-event payload type was negotiated "
                "for this call (the SDP offer carried no 'telephone-event'). DTMF "
                "is not sent — this is an explicit failure, not a silent no-op."
            )
            raise RuntimeError(msg)

        # The tone duration and the per-update step are in RTP CLOCK units. For both
        # G.711 and G.722 the RTP clock is 8000 (RFC 3551), so the named-event
        # duration counts at 8 kHz regardless of the audio sample rate.
        ts_increment = (self._rtp_clock_rate * self._ptime) // 1000
        total_duration = (self._rtp_clock_rate * tone_ms) // 1000

        # One TX critical section for the whole burst (ADR-0031): serialised against
        # send_audio so packets never interleave and the shared seq/ts never races.
        async with self._tx_lock:
            for digit in digits:
                await self._send_one_dtmf_digit(
                    digit,
                    event_pt=event_pt,
                    total_duration=total_duration,
                    step=ts_increment,
                    volume=volume,
                    gap_ms=gap_ms,
                )

    async def _send_one_dtmf_digit(  # noqa: PLR0913 — each arg is an independent per-digit RFC 4733 parameter resolved once in send_dtmf and threaded through; bundling them into an object would only move the surface
        self,
        digit: str,
        *,
        event_pt: int,
        total_duration: int,
        step: int,
        volume: int,
        gap_ms: int,
    ) -> None:
        """Emit one digit's telephone-event packets, then advance ts + pace the gap.

        Caller holds :attr:`_tx_lock`. All packets carry a CONSTANT timestamp
        (``self._ts`` at entry); the marker bit is set on the first only; seq
        advances per packet. After the digit, ``self._ts`` advances by
        ``total_duration`` so the next digit/audio is a distinct, later event, and a
        ``gap_ms`` silent gap is paced. A transport nulled mid-burst (a concurrent
        :meth:`stop`) ends the send cleanly (the call is tearing down).
        """
        digit_ts = self._ts
        ptime_s = self._ptime / 1000.0
        first = True
        for payload in event_payloads(
            digit, total_duration=total_duration, step=step, volume=volume
        ):
            transport = self._transport
            if transport is None:
                # The call is tearing down (stop() nulled the transport). Abandon the
                # rest of the burst cleanly rather than raising through the call loop.
                return
            pkt = RtpPacket(
                payload_type=event_pt,
                sequence_number=self._seq,
                timestamp=digit_ts,
                ssrc=_OUTBOUND_SSRC,
                payload=payload,
                marker=first,
            )
            wire = (
                self._srtp_out.protect(pkt)
                if self._srtp_out is not None
                else pkt.pack()
            )
            self._seq = (self._seq + 1) % (1 << 16)
            transport.sendto(wire, self._outbound_addr)
            first = False
            # Pace the next packet one ptime later (the redundant end packets are
            # paced too, so the named-event stream keeps a steady 50 pps).
            await self._sleep(ptime_s)
        # Advance the RTP timestamp past this tone so the next digit (or audio) is a
        # distinct, later event the far end registers separately.
        self._ts = (self._ts + total_duration) % (1 << 32)
        # Silent inter-digit gap: pace it so a repeated digit is two events.
        if gap_ms > 0:
            await self._sleep(gap_ms / 1000.0)

    async def _send_inband_dtmf(
        self, digits: str, *, tone_ms: int, gap_ms: int
    ) -> None:
        """Send ``digits`` as in-band dual-tone audio (ADR-0036, G.711 last resort).

        Each digit is synthesised at the engine's WIRE rate (8 kHz for G.711) by
        :func:`hermes_voip.dtmf.inband_tone_pcm` and handed to :meth:`send_audio`, which
        owns the encode + 20 ms framing + deadline pacing + SRTP + the TX mutex — so an
        in-band digit is just audio on the wire and never interleaves with a concurrent
        utterance. A silent inter-digit gap separates digits so a repeat is two tones.
        ``inband_tone_pcm`` validates the digit (raising on a non-DTMF char), so an
        invalid digit fails BEFORE any tone is sent (no partial burst).
        """
        rate = self._wire_sample_rate
        # Synthesise every digit up front so a bad digit raises before any tone is sent.
        tones = [
            inband_tone_pcm(digit, sample_rate=rate, duration_ms=tone_ms)
            for digit in digits
        ]
        gap = b"\x00\x00" * ((rate * gap_ms) // 1000) if gap_ms > 0 else b""
        ts_ns = self._clock()
        for index, tone in enumerate(tones):
            await self.send_audio(
                PcmFrame(samples=tone, sample_rate=rate, monotonic_ts_ns=ts_ns)
            )
            if gap and index < len(tones) - 1:
                await self.send_audio(
                    PcmFrame(samples=gap, sample_rate=rate, monotonic_ts_ns=ts_ns)
                )

    def _detect_inband_dtmf(self, frame: PcmFrame) -> None:
        """Feed one AEC-cleaned inbound frame to the in-band detector (ADR-0036).

        A no-op unless in-band RX is armed (a G.711 call with no telephone-event) and a
        sink is set. A detected key-press fires :attr:`_on_dtmf` exactly once — the same
        callback the RFC 4733 demux uses, so surfacing downstream is uniform. The frame
        is NOT modified (the audio still flows on to the VAD/ASR). The detector consumes
        analysis-rate PCM16, which is what ``_decode``/``_cancel_echo`` deliver here.
        """
        detector = self._inband_detector
        if detector is None or self._on_dtmf is None:
            return
        digit = detector.feed(frame.samples)
        if digit is not None:
            self._on_dtmf(digit)

    def _handle_inbound_dtmf(self, packet: RtpPacket) -> None:
        """Decode one inbound telephone-event packet and surface a completed digit.

        Called by the inbound generator for every packet at the negotiated
        telephone-event PT (ADR-0010). The 4-byte named-event payload is decoded and
        fed to the per-call :class:`~hermes_voip.dtmf.DtmfReceiver` at the packet's RTP
        timestamp; the receiver never trusts a single end packet outright (DTMF is the
        ADR-0009 spoof-resistant confirmation channel) — it returns the digit only once
        a SECOND end packet corroborates the first, collapsing the remaining redundant
        RFC 4733 end packets to duplicates, so ``on_dtmf`` fires exactly once per
        key-press (see ``DtmfReceiver.feed`` for the full corroboration contract).

        A malformed named-event packet is DROPPED with a DEBUG log rather than crashing
        the inbound generator — a corrupt inbound packet is an environmental event, not
        a programming error (the same posture as a malformed RTP datagram above). The
        ``ValueError`` guard spans BOTH the 4-byte ``decode`` AND the receiver ``feed``
        (cross-vendor review #5): a well-formed payload whose event code is not a keypad
        digit (e.g. flash, event 16) is surfaced as ``DtmfNoPress.NON_DIGIT_EVENT`` by
        the receiver and silently dropped here (the ``isinstance(result, DtmfPress)``
        check below rejects it), but wrapping ``feed`` too means any future
        value-domain error in the digit mapping is contained to a single dropped packet,
        not a torn-down call. Any non-``ValueError`` exception still propagates
        (rule 37).

        A same-timestamp packet whose event code disagrees with the one already
        recorded for that timestamp is surfaced as ``DtmfNoPress.CONFLICTING_EVENT`` —
        this permanently withholds a press for that timestamp, whether the conflict is
        seen BEFORE any digit was trusted (the dangerous ordering: a forged packet
        arrives first, and the disagreeing genuine one that follows can never rescue
        it either) or after (a conflicting straggler arriving too late to undo an
        already-emitted press). Either way it is never treated as a (new) press, and is
        logged at DEBUG below, distinctly from an ordinary ``DUPLICATE_END``, for
        diagnostic visibility into attempted substitution.
        """
        if self._on_dtmf is None:
            return  # inbound DTMF ignored; the packet is still kept off the audio path
        try:
            event = DtmfEvent.decode(packet.payload)
            result = self._dtmf_receiver.feed(event, timestamp=packet.timestamp)
        except ValueError as exc:
            _log.debug("malformed telephone-event packet — dropped: %s", exc)
            return
        if isinstance(result, DtmfPress):
            _log.info("dtmf rx: digit %r", result.digit)
            self._on_dtmf(result.digit)
        elif result is DtmfNoPress.CONFLICTING_EVENT:
            # A same-timestamp packet disagreed with the event code already
            # recorded for it (a forged or otherwise mismatching duplicate,
            # never a press — see DtmfReceiver.feed). Logged at the same
            # severity as the SRTP auth/replay and malformed-datagram drops
            # above: the receiver already defended against it, so this is
            # diagnostic noise, not a call-affecting failure.
            _log.debug(
                "dtmf rx: conflicting telephone-event at timestamp %d — dropped "
                "(mismatched duplicate, not treated as a press)",
                packet.timestamp,
            )

    @property
    def on_dtmf(self) -> Callable[[str], None] | None:
        """The callback fired once per RECEIVED DTMF digit, or ``None`` (ADR-0010).

        Settable after construction — the outbound path wires it after the call is up
        (mirroring :attr:`telephone_event_payload_type`). ``None`` ignores inbound
        DTMF; the demux still keeps telephone-event packets off the audio decoder.
        """
        return self._on_dtmf

    @on_dtmf.setter
    def on_dtmf(self, value: Callable[[str], None] | None) -> None:
        self._on_dtmf = value

    @property
    def telephone_event_payload_type(self) -> int | None:
        """The negotiated RFC 4733 telephone-event RTP payload type, or ``None``.

        ``None`` when the call's offer carried no ``telephone-event`` (so
        :meth:`send_dtmf` raises rather than guessing a PT). Settable after
        construction — the outbound path adopts the answer's telephone-event PT,
        mirroring the :attr:`payload_type` / ``engine._codec`` update.
        """
        return self._telephone_event_payload_type

    @telephone_event_payload_type.setter
    def telephone_event_payload_type(self, value: int | None) -> None:
        self._telephone_event_payload_type = value

    @property
    def dtmf_send_mode(self) -> DtmfSendMode:
        """The resolved outbound-DTMF backend :meth:`send_dtmf` uses (ADR-0036).

        Settable after construction — the outbound path resolves it once the answer's
        telephone-event PT / codec are known, mirroring
        :attr:`telephone_event_payload_type`. The :class:`~hermes_voip.call.CallSession`
        reads this to decide whether ``send_dtmf`` routes to SIP INFO (its own job) or
        to this engine (RFC 4733 / in-band).
        """
        return self._dtmf_send_mode

    @dtmf_send_mode.setter
    def dtmf_send_mode(self, value: DtmfSendMode) -> None:
        self._dtmf_send_mode = value

    @property
    def inband_dtmf_rx_enabled(self) -> bool:
        """Whether the in-band Goertzel DTMF detector is armed on the inbound path.

        Settable after construction — the outbound path resolves the receive backend
        only after the 2xx answer, so it arms in-band here. Setting it (re)makes the
        per-call detector at the current analysis rate (or clears it), so a value set
        post-:meth:`connect` takes effect on the next inbound frame. Inbound DTMF still
        only surfaces when :attr:`on_dtmf` is set.
        """
        return self._inband_dtmf_rx_enabled

    @inband_dtmf_rx_enabled.setter
    def inband_dtmf_rx_enabled(self, value: bool) -> None:
        self._inband_dtmf_rx_enabled = value
        # (Re)create or clear the detector now so a value set after connect() — the
        # outbound path resolves the backend post-answer — actually takes effect:
        # connect() built it from the ctor flag; this keeps the two in sync.
        if value:
            if self._inband_detector is None:
                self._inband_detector = InbandDtmfDetector(
                    sample_rate=self._analysis_sample_rate
                )
        else:
            self._inband_detector = None

    @property
    def codec_encoding(self) -> str:
        """The negotiated audio codec's encoding name (``PCMU`` / ``G722`` / ...).

        The :class:`Codec` enum member name is the RTP encoding name, so this is what
        :func:`hermes_voip.dtmf_config.is_g711_codec` needs to gate the in-band DTMF
        backend (G.711 only). Reflects any post-connect codec re-set (the outbound path
        upgrades a PCMU placeholder to the negotiated codec).
        """
        return self._codec.name

    @property
    def payload_type(self) -> int:
        """The RTP payload type on the wire (sent + accepted by the comedia latch).

        Defaults to the codec's static PT; set to the NEGOTIATED PT when it differs
        (the outbound path updates it after the 2xx answer, mirroring the
        ``engine._codec`` update). Kept distinct from the codec kind because a
        dynamic-PT codec (G.722) can negotiate a wire PT other than its static one.
        """
        return self._payload_type

    @payload_type.setter
    def payload_type(self, value: int) -> None:
        self._payload_type = value

    @property
    def ptime(self) -> int:
        """The RTP packetisation time in ms (the negotiated framing; ADR-0056).

        Every TX framing computation reads this (samples per packet, the RTP
        timestamp increment, the pacing interval), so the engine frames at the
        NEGOTIATED ptime rather than a hard-coded 20 ms. Defaults to
        :data:`_DEFAULT_PTIME_MS`; set it (e.g. from
        :func:`hermes_voip.sdp.negotiate_ptime` on the offer) after construction
        to apply the agreed framing — mirroring the post-construction
        :attr:`payload_type` / :attr:`telephone_event_payload_type` setters.
        """
        return self._ptime

    @ptime.setter
    def ptime(self, value: int) -> None:
        if value <= 0:
            msg = f"ptime must be a positive number of milliseconds, got {value}"
            raise ValueError(msg)
        self._ptime = value

    @property
    def _descriptor(self) -> _CodecDescriptor:
        """Rate descriptor for the CURRENT codec.

        Read live so it reflects a codec re-set after construction (the outbound
        path reassigns ``self._codec`` from a placeholder before connect()).
        """
        return _CODEC_DESCRIPTORS[self._codec]

    @property
    def _wire_sample_rate(self) -> int:
        """The PCM rate this codec encodes/decodes (8 kHz G.711, 16 kHz G.722)."""
        return self._descriptor.wire_sample_rate

    @property
    def _rtp_clock_rate(self) -> int:
        """The RTP timestamp clock rate for this codec (8 kHz for G.711 AND G.722)."""
        return self._descriptor.rtp_clock_rate

    @property
    def _analysis_sample_rate(self) -> int:
        """The inbound conversational-pipeline rate (VAD/endpointer/STT delivery).

        Equals the wire rate for G.711 (8 kHz) and G.722 (16 kHz); for Opus it is
        16 kHz, NOT the 48 kHz wire rate (Silero VAD's 8/16 kHz cap — ADR-0032).
        """
        return self._descriptor.analysis_sample_rate

    @property
    def inbound_sample_rate(self) -> int:
        """Rate of frames from :meth:`inbound_audio` — the conversational analysis rate.

        8000 Hz for G.711, 16000 Hz for G.722 (ADR-0022), 16000 Hz for Opus
        (ADR-0032: downsampled from the 48 kHz wire because Silero VAD accepts only
        8/16 kHz). The STT path reads this so the recogniser, VAD, and endpointer all
        build at this rate.
        """
        return self._analysis_sample_rate

    # ------------------------------------------------------------------
    # CallMedia Protocol
    # ------------------------------------------------------------------

    async def set_hold(self, on_hold: bool) -> None:
        """Gate or restore outbound media (hold = stop sending; idempotent).

        Entering hold also DROPS any buffered outbound remainder (the < 20 ms
        sub-frame tail carried by the re-framing buffer), so stale pre-hold audio
        is not prepended to the stream after resume nor flushed by a stop-while-
        held — "hold stops outbound media" holds for the buffered tail too.

        Args:
            on_hold: ``True`` to hold; ``False`` to resume.
        """
        if on_hold:
            self._tx_buffer.clear()
        self.on_hold = on_hold

    async def rekey_srtp(
        self,
        *,
        inbound: CryptoAttribute | None,
        outbound: CryptoAttribute | None,
    ) -> None:
        """Re-key the SRTP context mid-call from new SDES ``a=crypto`` (RFC 4568 §6.1).

        Called on an in-dialog re-offer (hold/resume/re-INVITE) of a secured call so
        the security context survives the re-negotiation (ADR-0053). Each argument is
        the new per-direction SDES key as a :class:`~hermes_voip.sdp.CryptoAttribute`,
        or ``None`` to leave that direction's session unchanged — a re-offer that does
        not re-key a direction keeps the established key, and a plain call (both
        ``None``) is unaffected. RFC 4568 §6.1 directionality: ``outbound`` is OUR key
        (we encrypt + advertise it); ``inbound`` is the peer's key (we decrypt with it).

        The fresh outbound session is bound to the engine's fixed outbound SSRC so it
        keys correctly from the first protected packet; the inbound session binds to
        the peer's SSRC on its first packet (RFC 3711 §3.2.3). The outbound swap is
        done under the TX lock so it cannot race a packet mid-protect; the inbound swap
        is a single attribute store (the inbound reader sees the old or the new session,
        never a torn one).

        Raises:
            ImportError: If the ``media`` extra is not installed (``SrtpSession``
                construction needs the ``cryptography`` backend).
            SrtpError: If a supplied crypto carries unsupported SDES session
                parameters (a non-default lifetime or an MKI).
        """
        from hermes_voip.media.srtp import SrtpSession  # noqa: PLC0415

        if outbound is not None:
            new_out = SrtpSession(outbound, ssrc=_OUTBOUND_SSRC)
            async with self._tx_lock:
                self._srtp_out = new_out
        if inbound is not None:
            self._srtp_in = SrtpSession(inbound)

    async def flush_outbound(self, *, fade_ms: int) -> None:
        """Drop the pending outbound audio with a short click-free fade (ADR-0028).

        Called the instant a barge-in is authorised: the agent must go quiet within
        ~1 packet, NOT after the buffered TTS audio drains. ``TtsStream.cancel()``
        only stops the call loop *pulling* new frames; the audio already handed to
        :meth:`send_audio` is re-framed and deadline-paced over real time (a single
        large TTS chunk can be hundreds of ms on the wire), so without this the
        caller keeps hearing the agent after they interrupted — the abruptness/delay
        the operator reported.

        This method:

        * bumps the flush generation so a :meth:`send_audio` drain loop parked on its
          pacing sleep stops emitting the remainder of the superseded utterance, and
          DROPS any frame the deadline pacer parked mid-flight (it is one ptime of
          full-amplitude pre-cut audio — emitting it would extend the agent's speech
          past the barge-in; only its already-encoded bytes survive, so it cannot be
          folded into the fade);
        * emits a short LINEAR FADE-OUT on the FRONT of the carry buffer (the audio
          that would have played next), re-framed into whole ``ptime`` RTP packets and
          sent immediately (no pacing — the cut is now), so the last thing on the wire
          ramps to silence and does not click/pop; then
        * DROPS the rest of the pending buffer.

        Total audio emitted after a barge-in is therefore at most the fade window
        (``ceil(fade_ms / ptime)`` packets ≈ 1-2 at the 30 ms default) — within the
        operator's ~20-40 ms budget, NOT the whole queued utterance. The fade is
        computed in the linear PCM16 domain BEFORE the codec encode, so G.711 and
        G.722 are both correct. ``fade_ms`` of 0 emits nothing — an instant hard cut.
        While held / before connect, nothing is emitted (hold/closed-socket stop
        outbound media).

        Args:
            fade_ms: Length of the linear fade-out in milliseconds (``>= 0``). The
                adapter passes ``HERMES_VOIP_BARGE_IN_FADE_MS`` (default 30).

        Raises:
            ValueError: If ``fade_ms`` is negative.
        """
        if fade_ms < 0:
            msg = f"fade_ms must be non-negative, got {fade_ms}"
            raise ValueError(msg)
        # Supersede any in-flight send_audio drain: it bails when it wakes (so the
        # rest of the chunk it was pacing never goes out).
        self._flush_generation += 1
        # Snapshot the buffered tail as immutable bytes (the fade path slices it and
        # feeds linear_fade_out, which takes bytes), then clear the buffer in place.
        pending = bytes(self._tx_buffer)
        self._tx_buffer.clear()
        # DROP any frame the deadline pacer parked mid-flight. It is one ptime of
        # full-amplitude (pre-cut) audio; emitting it would add a full-volume packet
        # AFTER the barge-in, beyond the fade budget — exactly the "still talking
        # after I interrupted" the fade exists to avoid. We only hold its already-
        # ENCODED bytes (its PCM was discarded after encode), so it cannot be folded
        # into the fade; dropping it is the right call. The seq/ts it consumed are
        # left spent, so the fade frames below use the NEXT seq/ts — the receiver
        # sees a single 1-packet sequence gap, which RTP treats as one lost packet
        # (benign, concealed). For G.722 the encoder advanced past this frame while
        # the decoder will not see it: the decoder's adaptive sub-band predictor
        # re-converges within a few frames, and since the fade is ramping to SILENCE
        # the brief transient is inaudible (it is not a permanent desync).
        self._inflight_wire = None

        transport = self._transport
        if transport is None or self.on_hold or fade_ms == 0 or not pending:
            # Held / not connected / fade disabled / nothing buffered: the drop above
            # is the whole effect — instant silence, no audio emitted. (stop()'s own
            # flush is likewise gated on on_hold.)
            return

        wire_samples_per_frame = (self._wire_sample_rate * self._ptime) // 1000
        bytes_per_sample = 2
        fade_samples = (self._wire_sample_rate * fade_ms) // 1000
        # Fade the FRONT of the buffer (the immediate continuation of what is
        # playing) so the ramp falls from ~current amplitude to silence; the rest of
        # the buffer is discarded. Cap is the fade window, so the total audio emitted
        # after a barge-in is at most ``ceil(fade_ms / ptime)`` packets — within the
        # operator's ~20-40 ms fade budget, NOT the whole queued utterance. Re-frame
        # the faded audio into whole ptime packets (zero-pad the final partial once).
        fade_bytes = (
            min(fade_samples, len(pending) // bytes_per_sample) * bytes_per_sample
        )
        head = pending[:fade_bytes]
        faded = linear_fade_out(head, fade_samples=len(head) // bytes_per_sample)
        chunk_bytes = wire_samples_per_frame * bytes_per_sample
        remainder = len(faded) % chunk_bytes
        if remainder:
            faded = faded + bytes(chunk_bytes - remainder)
        self._emit_inline_frames(faded, transport)

    async def stop(self) -> None:
        """Tear down the media plane; close the socket; idempotent.

        Safe to call multiple times or before :meth:`connect`.

        Flushes any buffered outbound audio (the unsent whole frames plus a final
        zero-padded partial) through the still-open socket BEFORE closing it, so
        the last utterance's residual audio is delivered rather than stranded in
        the re-framing buffer. The partial is padded at most once — not per chunk
        — so this is not a source of the per-chunk choppiness it replaces. The
        flush is skipped while :attr:`on_hold` (hold stops outbound media), and
        :meth:`set_hold` clears the buffer on entry, so a stop-while-held emits
        nothing.
        """
        transport = self._transport
        if transport is not None and not self.on_hold:
            self._flush_tx_tail(transport)
        self._tx_buffer.clear()
        # Drop any in-flight frame the flush did not send (held call, or stop before
        # connect): hold/stop-without-socket emit nothing. _flush_tx_tail clears it
        # when it runs; this covers the paths where it does not.
        self._inflight_wire = None
        # Clear the pacing deadline so a reconnected engine re-anchors fresh.
        self._next_send_at = None
        self._transport = None
        # Drop the protocol reference: closing the transport below fires a clean
        # connection_lost(None) on it (a no-op now), and a stopped engine holds no
        # live socket. Re-created in connect() for a reused engine.
        self._protocol = None
        if transport is not None:
            transport.close()
        # WebRTC path (ADR-0032): cancel the inbound ICE reader and close the ICE
        # pipe so aioice releases its sockets. The reader is cancelled (it is parked
        # on ice.recv()); closing the pipe is idempotent. ``_ice`` is left set (it
        # identifies this as a WebRTC engine for its lifetime) — but the pipe is now
        # closed, so a reused WebRTC engine is not supported (fresh engine per call,
        # the actual usage: the ICE pair + DTLS keys are per-call). Done after the
        # flush above so the teardown tail had its chance to be scheduled.
        ice_reader, self._ice_reader = self._ice_reader, None
        if ice_reader is not None:
            ice_reader.cancel()
        if self._ice is not None:
            await self._ice.close()
        # RTCP loop (ADR-0061): if the adapter started run_rtcp and registered its
        # task here, cancel it AND AWAIT it so it does not outlive the call and its
        # finally (the BYE flush) completes before stop() returns — otherwise the BYE
        # may never go out and asyncio warns "task was destroyed but it is pending"
        # (codex review). The loop also watches the stop event set below, but a
        # registered task is cancelled for promptness; the CancelledError from the
        # cancel is expected and suppressed here (the loop re-raises it, rule 37,
        # after running its finally). None when the adapter ran the loop without
        # registering it (it then exits on the event, unawaited by design).
        rtcp_task, self._rtcp_task = self._rtcp_task, None
        if rtcp_task is not None:
            rtcp_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rtcp_task
        # Non-muxed RTCP (RFC 3550 §11, ADR-0061): tear down the sibling-socket reader
        # and the RTCP socket. ORDER (codex review #6):
        #   1. cancel AND AWAIT the reader — mirroring the run_rtcp task above — so it
        #      cannot outlive the call (a reader left merely cancelled, never awaited,
        #      races teardown and risks send-after-close / "task was destroyed but it
        #      is pending"). It is parked on its internal queue (NOT the socket), so the
        #      await completes promptly; CancelledError is expected and suppressed (the
        #      loop re-raises it, rule 37). ``None`` on the muxed path.
        #   2. THEN close the RTCP socket — AFTER both the run_rtcp task (whose finally
        #      emits the closing BYE through ``_rtcp_send`` over this very socket) and
        #      the reader have finished, so the BYE has already gone out (closing the
        #      socket first would drop it) and the reader is no longer touching it.
        self._rtcp_active = False
        self._rtcp_mux_active = False
        rtcp_reader, self._rtcp_reader = self._rtcp_reader, None
        if rtcp_reader is not None:
            rtcp_reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rtcp_reader
        rtcp_transport, self._rtcp_transport = self._rtcp_transport, None
        if rtcp_transport is not None:
            rtcp_transport.close()
        self._rtcp_local_port = None
        # Restore the constructor sink AFTER the closing BYE has gone out (above) and
        # the sibling socket is closed: a non-muxed call installed a lambda bound to
        # that now-closed socket, so drop it to the injected value (None on a muxed
        # prod call) — a reused engine never sends RTCP via the dead socket.
        self._rtcp_send = self._injected_rtcp_send
        # Set the stop flag so the inbound generator wakes and exits cleanly.
        # Unlike a queued sentinel, this is independent of recv-queue capacity:
        # a full queue cannot drop the signal and strand the consumer.
        self._stop_event.set()
        # Retire the reused stop-wait racer (efficiency, rule 22): the event is now
        # set, so this task resolves on the next loop step. AWAIT it (it completes
        # at once) so it is never left pending across teardown — mirroring the
        # run_rtcp / reader cleanup above — then clear the slot so a reused engine's
        # connect() starts fresh. CancelledError is not expected here (we do not
        # cancel it — it wins cleanly on the set event) but is suppressed for the
        # connect()-cancelled-it-then-reused-without-await edge, and re-raised by the
        # task itself per rule 37 if it ever carries one.
        stop_wait, self._stop_wait_task = self._stop_wait_task, None
        if stop_wait is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await stop_wait

    def _flush_tx_tail(self, transport: _DatagramSink) -> None:
        """Emit all buffered outbound audio as final RTP packets, in order.

        Sends, in stream order: first any in-flight frame parked by the deadline
        pacer (:attr:`_inflight_wire` — a datagram encoded just before a stop nulled
        the transport mid-pace; it is the FRONT of the stream and already carries its
        committed seq/ts), then every whole wire-rate frame still in
        :attr:`_tx_buffer` (the send loop can leave several when a concurrent stop
        bails out mid-drain), then the trailing sub-frame remainder zero-padded up to
        one whole frame — all through ``transport`` (the still-open socket), then
        clears the buffers. A no-op when nothing is buffered. Sent inline (no ``ptime``
        pacing) because the call is tearing down; seq advances per emitted raw frame
        and the timestamp by the codec's RTP clock increment (160 for G.711 AND
        G.722), so the wire stays RFC 3550-consistent.
        """
        # The in-flight (already-encoded) frame goes out FIRST — it precedes every
        # raw frame still in _tx_buffer, and re-encoding it from PCM would
        # double-advance the stateful encoder and reorder the stream. Its seq/ts were
        # committed at encode time, so just send the bytes.
        inflight, self._inflight_wire = self._inflight_wire, None
        if inflight is not None:
            transport.sendto(inflight, self._outbound_addr)
        # Snapshot the buffered remainder as immutable bytes (it is zero-padded and
        # handed to _emit_inline_frames below), then clear the buffer in place.
        tail = bytes(self._tx_buffer)
        self._tx_buffer.clear()
        if not tail:
            return
        wire_samples_per_frame = (self._wire_sample_rate * self._ptime) // 1000
        chunk_bytes = wire_samples_per_frame * 2
        # Pad only the trailing partial up to a whole frame; whole frames already
        # buffered are sent as-is, in order, each as its own RTP packet.
        remainder = len(tail) % chunk_bytes
        if remainder:
            tail = tail + bytes(chunk_bytes - remainder)
        self._emit_inline_frames(tail, transport)

    def _emit_inline_frames(self, pcm: bytes, transport: _DatagramSink) -> None:
        """Encode + pack + send ``pcm`` as whole ``ptime`` RTP packets, inline.

        ``pcm`` MUST be a whole number of wire-rate ``ptime`` frames (the caller
        zero-pads any partial). Each frame is encoded with the per-call stateful
        codec (so a G.722 stream stays continuous), packed at the next seq/ts, and
        sent immediately with NO pacing — used by the teardown flush
        (:meth:`_flush_tx_tail`) and the barge-in fade (:meth:`flush_outbound`),
        both of which deliver a short final burst. seq advances per frame and the
        timestamp by the codec's RTP clock increment (160 for G.711 AND G.722), so
        the wire stays RFC 3550-consistent.
        """
        ts_increment = (self._rtp_clock_rate * self._ptime) // 1000
        chunk_bytes = ((self._wire_sample_rate * self._ptime) // 1000) * 2
        for offset in range(0, len(pcm), chunk_bytes):
            chunk = pcm[offset : offset + chunk_bytes]
            payload = self._encode(
                PcmFrame(
                    samples=chunk,
                    sample_rate=self._wire_sample_rate,
                    monotonic_ts_ns=0,
                )
            )
            pkt = RtpPacket(
                payload_type=self._payload_type,
                sequence_number=self._seq,
                timestamp=self._ts,
                ssrc=_OUTBOUND_SSRC,
                payload=payload,
            )
            wire = (
                self._srtp_out.protect(pkt)
                if self._srtp_out is not None
                else pkt.pack()
            )
            self._seq = (self._seq + 1) % (1 << 16)
            self._ts = (self._ts + ts_increment) % (1 << 32)
            transport.sendto(wire, self._outbound_addr)
            # Tap the inline burst as the AEC reference too (ADR-0033): the teardown
            # tail and the barge-in fade are outbound audio the gateway can reflect,
            # so the canceller must see them as well. ``chunk`` is wire-rate PCM16.
            self._push_aec_reference(chunk)

    # ------------------------------------------------------------------
    # Discovered port (after connect with local_port=0)
    # ------------------------------------------------------------------

    @property
    def local_port(self) -> int:
        """The local UDP port (OS-assigned when 0 was passed to __init__)."""
        return self._local_port

    @property
    def media_timed_out(self) -> bool:
        """Whether the call ended abnormally on the media plane (ADR-0026).

        ``True`` once the RTP-inactivity watchdog fired (no datagram within the
        configured window) or the UDP transport was lost. The adapter reads this
        after the call loop returns to classify the end as a failure (MEDIA_TIMEOUT
        / CONNECTION_LOST → ``/stop``) rather than a clean REMOTE_BYE/EOS. A
        received BYE stops media WITHOUT setting this (a normal end).
        """
        return self._media_timed_out

    # ------------------------------------------------------------------
    # Acoustic echo cancellation (ADR-0033)
    # ------------------------------------------------------------------

    def _ensure_aec(self) -> EchoCanceller | None:
        """Return the per-call echo canceller, creating it lazily (or ``None``).

        ``None`` when AEC is disabled. Otherwise created on first use at the CURRENT
        analysis rate — deferred (like the G.722/Opus codec objects) because the
        outbound path re-sets ``self._codec`` (and hence the analysis rate) after
        :meth:`connect` but before any media, so building it at connect-time would
        pin the placeholder PCMU rate for a G.722/Opus call. The filter length and
        bulk delay are stored in ms (rate-independent) and converted to samples here
        at the live analysis rate; a configured ``aec_filter_ms`` always yields at
        least one tap.
        """
        if not self._aec_enabled:
            return None
        if self._aec is None:
            rate = self._analysis_sample_rate
            # Clamp the derived tap count to the CPU-safe ceiling (rule 22): the
            # per-sample cost is O(taps) in pure Python, so an unbounded tap count
            # (a high analysis rate x a long filter_ms) could push the per-frame cost
            # past the ptime and stall the media loop. The 64 ms default therefore
            # gives a full window at 8 kHz but a capped (~32 ms) one at 16 kHz.
            filter_len = max(1, (rate * self._aec_filter_ms) // 1000)
            filter_len = min(filter_len, _AEC_MAX_TAPS)
            bulk_delay = (rate * self._aec_bulk_delay_ms) // 1000
            self._aec = EchoCanceller(
                sample_rate=rate,
                filter_len=filter_len,
                bulk_delay=bulk_delay,
                mu=self._aec_mu,
            )
        return self._aec

    def _push_aec_reference(self, chunk: bytes) -> None:
        """Tap one outbound wire-rate frame as the echo-canceller reference.

        ``chunk`` is wire-rate PCM16 (the encoded frame's source PCM). The canceller
        downsamples it to the analysis rate if they differ (Opus 48→16 kHz). A no-op
        when AEC is disabled. Never raises on a whole-sample chunk.
        """
        aec = self._ensure_aec()
        if aec is not None:
            aec.push_reference(chunk, sample_rate=self._wire_sample_rate)

    def _cancel_echo(self, frame: PcmFrame) -> PcmFrame:
        """Return ``frame`` with the known outbound echo cancelled (ADR-0033).

        ``frame`` is one decoded inbound frame at the analysis rate. Subtracts the
        estimated echo of the tapped outbound reference and returns the residual at
        the same rate/length (buffer-free — no added latency). Returns ``frame``
        unchanged when AEC is disabled or the frame is empty.
        """
        aec = self._ensure_aec()
        if aec is None or not frame.samples:
            return frame
        return PcmFrame(
            samples=aec.cancel(frame.samples),
            sample_rate=frame.sample_rate,
            monotonic_ts_ns=frame.monotonic_ts_ns,
        )

    # ------------------------------------------------------------------
    # Internal codec helpers
    # ------------------------------------------------------------------

    def _to_wire_rate(self, frame: PcmFrame) -> PcmFrame:
        """Return ``frame`` resampled to the codec's wire rate (ADR-0017/0022).

        The wire rate is codec-derived (8 kHz G.711, 16 kHz G.722). A frame already
        at the wire rate is returned unchanged (no resampler touches it, so the
        fast path is byte-exact). A frame at any other rate — typically the TTS
        provider's 24 kHz output — is resampled to the wire rate using a
        per-source-rate, state-carrying :class:`~hermes_voip.media.audio.Resampler`,
        so a continuous stream converts click-free (frame-by-frame output equals a
        single pass). This is a conversion, not an error: ``send_audio`` therefore
        never raises on an off-rate frame. For a G.722 call this downsamples a
        24 kHz Kokoro frame to 16 kHz (wideband preserved) rather than to 8 kHz.

        The output frame keeps the source ``monotonic_ts_ns`` (the presentation
        clock is unaffected by the rate change) and is stamped at the wire rate.
        """
        wire_rate = self._wire_sample_rate
        if frame.sample_rate == wire_rate:
            return frame
        resampler = self._tx_resamplers.get(frame.sample_rate)
        if resampler is None:
            resampler = Resampler(frame.sample_rate, wire_rate)
            self._tx_resamplers[frame.sample_rate] = resampler
        return PcmFrame(
            samples=resampler.resample(frame.samples),
            sample_rate=wire_rate,
            monotonic_ts_ns=frame.monotonic_ts_ns,
        )

    def _decode(self, payload: bytes, ts_ns: int) -> PcmFrame:
        """Decode an RTP payload to a PcmFrame at the codec's ANALYSIS rate.

        G.711 (PCMU/PCMA) decodes to 8 kHz; G.722 decodes via the per-call stateful
        :class:`~hermes_voip.media.g722.G722Decoder` to 16 kHz; Opus decodes via the
        per-call stateful :class:`~hermes_voip.media.opus.OpusDecoder` to 48 kHz and
        is then DOWNSAMPLED to the 16 kHz analysis rate (ADR-0032 — Silero VAD accepts
        only 8/16 kHz), via a state-carrying resampler so a continuous stream converts
        click-free. For G.711/G.722 the wire rate equals the analysis rate, so no
        resample runs. Each wideband decoder is created lazily on first use so an
        outbound call that connects as a PCMU placeholder then re-negotiates still gets
        a fresh one, and the default no-webrtc path never imports opuslib.
        """
        if self._codec is Codec.G722:
            if self._g722_decoder is None:
                self._g722_decoder = G722Decoder()
            return PcmFrame(
                samples=self._g722_decoder.decode(payload),
                sample_rate=G722_SAMPLE_RATE,
                monotonic_ts_ns=ts_ns,
            )
        if self._codec is Codec.OPUS:
            if self._opus_decoder is None:
                self._opus_decoder = _new_opus_decoder()
            decoded = self._opus_decoder.decode(payload)  # 48 kHz PCM16
            return PcmFrame(
                samples=self._downsample_to_analysis(decoded, OPUS_SAMPLE_RATE),
                sample_rate=self._analysis_sample_rate,
                monotonic_ts_ns=ts_ns,
            )
        if self._codec is Codec.PCMU:
            return ulaw_to_frame(payload, monotonic_ts_ns=ts_ns)
        return alaw_to_frame(payload, monotonic_ts_ns=ts_ns)

    def _downsample_to_analysis(self, pcm: bytes, decoded_rate: int) -> bytes:
        """Resample decoded ``pcm`` from ``decoded_rate`` to the analysis rate.

        A no-op when the decoded rate already equals the analysis rate (G.711/G.722).
        For Opus (48 kHz decoded, 16 kHz analysis) a per-call state-carrying
        :class:`~hermes_voip.media.audio.Resampler` downsamples each frame so the
        continuous inbound stream is click-free (ADR-0032).
        """
        analysis_rate = self._analysis_sample_rate
        if decoded_rate == analysis_rate:
            return pcm
        if self._rx_resampler is None:
            self._rx_resampler = Resampler(decoded_rate, analysis_rate)
        return self._rx_resampler.resample(pcm)

    def _conceal_frame(self, ts_ns: int, *, is_last: bool = True) -> PcmFrame:
        """Build one concealment frame for a lost packet (ADR-0056, items 2+3).

        Returns an analysis-rate :class:`~hermes_voip.providers.audio.PcmFrame` —
        never an empty/absent slot — so the inbound stream the VAD/endpointer/STT
        consume stays continuous across loss. Per codec:

        * **Opus** — codec concealment that keeps the decoder's predictor coherent
          (so wideband degrades LESS than G.711, not more). When the lost frame's
          SUCCESSOR is already buffered (peeked, not consumed), its packet carries
          an in-band FEC copy of the lost frame: :meth:`OpusDecoder.decode_fec`
          reconstructs it exactly. Otherwise :meth:`OpusDecoder.decode_plc`
          extrapolates from decoder state. Either advances the decoder so the
          successor then decodes normally.
        * **G.711 / G.722** — there is no in-codec concealment, so repeat the last
          good decoded analysis-rate frame, attenuated by
          :data:`_PLC_ATTENUATION_PER_FRAME` per consecutive lost frame, dropping
          to silence after :data:`_PLC_MAX_REPEAT_FRAMES` (a long outage fades out
          rather than droning). Identical for both codecs, so G.722 loss handling
          is no worse than G.711's.

        ``is_last`` marks the final slot of a (possibly coalesced) loss run — the
        slot sitting immediately before the buffered successor. Only that slot may
        consult the successor's Opus in-band FEC (it carries exactly that one
        preceding frame); the interpolated slots of a coalesced run pass
        ``is_last=False`` and conceal with PLC, matching the pre-coalesce
        per-packet behaviour. A lone ``Lost`` defaults to ``is_last=True``.

        ``_consecutive_lost`` is incremented here (a run of losses) and reset by
        the next real decode; a future RTCP lane can read it as a loss count.
        """
        run = self._consecutive_lost
        self._consecutive_lost = run + 1
        if self._codec is Codec.OPUS:
            samples = self._conceal_opus(use_successor_fec=is_last)
            # Track the recovered/concealed estimate as the basis for a subsequent
            # G.711-style repeat should this become a non-Opus run (defensive; the
            # codec does not change mid-call, so this only keeps state coherent).
            self._last_good_samples = samples
            return PcmFrame(
                samples=samples,
                sample_rate=self._analysis_sample_rate,
                monotonic_ts_ns=ts_ns,
            )
        # G.711 / G.722: attenuated repeat of the last good frame, or silence.
        return PcmFrame(
            samples=self._conceal_repeat(run),
            sample_rate=self._analysis_sample_rate,
            monotonic_ts_ns=ts_ns,
        )

    def _conceal_opus(self, *, use_successor_fec: bool = True) -> bytes:
        """Recover/conceal one lost Opus frame at the analysis rate (ADR-0056).

        When ``use_successor_fec`` is set (the default, and the final slot of a
        coalesced run), the next buffered packet's in-band FEC is used if present —
        it carries exactly the frame immediately before it. When it is unset (an
        interpolated slot of a coalesced run, whose true predecessor is NOT the
        buffered successor), native Opus PLC is used instead; FEC-decoding the
        successor for such a slot would "recover" the wrong frame. Both keep the
        decoder predictor coherent. The decoder is created lazily (as in
        :meth:`_decode`) so a connect-as-PCMU-then-Opus call works and the default
        no-webrtc path never imports opuslib.
        """
        if self._opus_decoder is None:
            self._opus_decoder = _new_opus_decoder()
        successor = self._jitter.peek_next() if use_successor_fec else None
        if successor is not None and successor.payload_type == self._payload_type:
            decoded = self._opus_decoder.decode_fec(successor.payload)
        else:
            decoded = self._opus_decoder.decode_plc()
        return self._downsample_to_analysis(decoded, OPUS_SAMPLE_RATE)

    def _conceal_repeat(self, run: int) -> bytes:
        """An attenuated repeat of the last good frame, or silence (G.711/G.722).

        ``run`` is the number of losses BEFORE this one (0 for the first loss after
        a good frame), so the attenuation is ``_PLC_ATTENUATION_PER_FRAME ** run``
        — a full-energy repeat first, decaying each further consecutive loss, and
        silence once the run reaches :data:`_PLC_MAX_REPEAT_FRAMES` or before any
        real frame has arrived.
        """
        last = self._last_good_samples
        if last is None or run >= _PLC_MAX_REPEAT_FRAMES:
            return self._analysis_silence()
        factor = _PLC_ATTENUATION_PER_FRAME**run
        if factor >= 1.0:
            return last
        # audioop.mul scales every PCM16 sample by ``factor`` (saturating), giving
        # a quieter copy of the held frame without per-sample Python arithmetic.
        return audioop.mul(last, PCM16_BYTES_PER_SAMPLE, factor)

    def _analysis_silence(self) -> bytes:
        """One ptime of PCM16 silence at the analysis rate (concealment fallback)."""
        samples_per_frame = (self._analysis_sample_rate * self._ptime) // 1000
        return b"\x00" * (samples_per_frame * PCM16_BYTES_PER_SAMPLE)

    def _encode(self, frame: PcmFrame) -> bytes:
        """Encode a wire-rate PcmFrame to an RTP payload for the codec.

        G.711 encodes the 8 kHz frame to mu-law/a-law; G.722 encodes the 16 kHz
        frame via the per-call stateful
        :class:`~hermes_voip.media.g722.G722Encoder`; Opus encodes the 48 kHz frame
        via the per-call stateful :class:`~hermes_voip.media.opus.OpusEncoder`
        (ADR-0032). Each wideband encoder is created lazily on first use (as for
        :meth:`_decode`).
        """
        if self._codec is Codec.G722:
            if self._g722_encoder is None:
                self._g722_encoder = G722Encoder()
            return self._g722_encoder.encode(frame.samples)
        if self._codec is Codec.OPUS:
            if self._opus_encoder is None:
                self._opus_encoder = _new_opus_encoder()
            return self._opus_encoder.encode(frame.samples)
        if self._codec is Codec.PCMU:
            return frame_to_ulaw(frame)
        return frame_to_alaw(frame)
