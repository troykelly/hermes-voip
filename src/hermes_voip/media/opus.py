"""Opus wire codec for the WebRTC media path (RFC 6716, ADR-0032).

Opus is the de-facto WebRTC audio codec. This module wraps ``opuslib`` (a
pure-Python BSD-3-Clause ctypes binding to the system ``libopus`` shared library)
to encode/decode the engine's wire PCM at **48 kHz mono, 20 ms frames** (960
samples per Opus packet). Unlike G.722 (whose RTP clock is 8 kHz while its audio
is 16 kHz — RFC 3551 §4.5.2), Opus's RTP clock **equals** its audio sample rate
(both 48 kHz), so the engine's rate-follows-codec bookkeeping carries it with no
special-casing.

State (the encoder/decoder's internal predictor history) is carried across calls,
so feeding a continuous stream frame-by-frame produces a coherent stream — one
``OpusEncoder`` / ``OpusDecoder`` belongs to one direction of one call. The codec
classes are stateful objects; this module only does sample<->packet transcoding,
and the engine (:mod:`hermes_voip.media.engine`) owns the RTP timestamp/rate
bookkeeping via its codec descriptor.

**Dependency gating.** ``opuslib`` lives in the optional ``webrtc`` extra and is
lazy-imported so that ``import hermes_voip.media.opus`` works in the default
install (the constants below are plain ints, always available). :class:`OpusEncoder`
/ :class:`OpusDecoder` raise :class:`ImportError` at *construction* time when the
extra (or the system ``libopus.so``) is absent — the error propagates naturally
(AGENTS.md rule 37), never a silent fallback to dead audio. No FFmpeg/PyAV (LGPL)
is pulled in; ``opuslib`` carries no compiled extension and dlopen's the system
``libopus`` (``ctypes.util.find_library('opus')``).

**Typing strategy.** ``opuslib`` ships no ``py.typed`` and its ``Decoder.decode``
even returns ``bytes | Any``. Mirroring :mod:`hermes_voip.media.dtls` and
:mod:`hermes_voip.media.ice` (which face the same problem for pyOpenSSL / aioice),
we declare narrow local :class:`typing.Protocol` classes over the
``importlib``-loaded module surface this codec uses, so mypy verifies every call
site with zero ``# type: ignore`` in both the default and the webrtc gate.
"""

from __future__ import annotations

import importlib
from typing import Final, Protocol

from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE

__all__ = [
    "OPUS_DEFAULT_PAYLOAD_TYPE",
    "OPUS_FRAME_SAMPLES",
    "OPUS_RTP_CLOCK_RATE",
    "OPUS_SAMPLE_RATE",
    "OpusDecoder",
    "OpusEncoder",
    "ensure_opus_available",
]

#: The audio sample rate Opus encodes/decodes for the WebRTC wire (48 kHz).
OPUS_SAMPLE_RATE: Final[int] = 48_000

#: The RTP clock rate Opus declares (RFC 7587 §4.1): 48 kHz — equal to the audio
#: sample rate (no quirk, unlike G.722's 8000 clock at 16 kHz audio).
OPUS_RTP_CLOCK_RATE: Final[int] = 48_000

#: The ptime in ms each Opus packet carries (the standard WebRTC frame duration).
OPUS_PTIME_MS: Final[int] = 20

#: Samples per 20 ms frame at 48 kHz (one Opus packet's worth).
OPUS_FRAME_SAMPLES: Final[int] = (OPUS_SAMPLE_RATE * OPUS_PTIME_MS) // 1000  # 960

#: The conventional dynamic RTP payload type for Opus. Opus has no static PT
#: (RFC 3551); 111 is the value most WebRTC stacks use. The wire PT is always the
#: NEGOTIATED one (the engine threads ``payload_type`` separately from the codec
#: kind); this is only the default when nothing else is negotiated.
OPUS_DEFAULT_PAYLOAD_TYPE: Final[int] = 111

#: Mono. The WebRTC answer advertises Opus with ``stereo=0`` (telephony is mono).
_CHANNELS: Final[int] = 1

#: ``opuslib`` application mode for low-latency speech (= ``OPUS_APPLICATION_VOIP``).
_APPLICATION_VOIP: Final[int] = 2048

#: Default expected packet-loss percentage the encoder is biased for (ADR-0056).
#: Opus scales its in-band FEC strength with this hint; 10% is a reasonable
#: open-internet default — enough redundancy to recover typical single-frame
#: losses without a large bitrate cost. A future control path can raise it from
#: measured loss via the ``expected_packet_loss_pct`` constructor argument.
_DEFAULT_EXPECTED_PACKET_LOSS_PCT: Final[int] = 10

#: Inclusive bounds for a packet-loss percentage hint.
_MIN_LOSS_PCT: Final[int] = 0
_MAX_LOSS_PCT: Final[int] = 100


# ---------------------------------------------------------------------------
# Narrow Protocol surface over the optional ``opuslib`` extra.
#
# ``opuslib`` ships no ``py.typed`` (and ``Decoder.decode`` returns ``bytes | Any``),
# so — exactly as media/dtls.py and media/ice.py do for pyOpenSSL / aioice — we
# declare narrow local Protocols over the importlib-loaded module rather than use
# an escape hatch. A module attribute resolved via ``importlib.import_module``
# yields ``Any``, assignable to these structural callables without a cast, and
# every subsequent call is then fully type-checked.
# ---------------------------------------------------------------------------


# The libopus encoder/decoder handles are opaque ctypes pointer structures
# (``opuslib.api.encoder.LP_Encoder`` / ``LP_Decoder``); this module only passes
# them back into the ``opuslib.api`` CTL/decode functions, never inspecting them,
# so an opaque ``object`` is the right type — it carries no Any.
type _OpusHandle = object


class _RawEncoder(Protocol):
    """The ``opuslib.Encoder`` surface this module uses.

    Includes ``encoder_state`` (the ctypes handle) and the read-back getters,
    which work — only opuslib's CTL *setters* are broken (they omit the value
    argument), so FEC/loss are enabled via the low-level CTL on ``encoder_state``.
    """

    encoder_state: _OpusHandle
    inband_fec: int
    packet_loss_perc: int

    def encode(self, pcm_data: bytes, frame_size: int) -> bytes:
        """Encode one frame of interleaved PCM16 to an Opus packet."""
        ...


class _RawDecoder(Protocol):
    """The ``opuslib.Decoder`` surface this module uses.

    ``decoder_state`` (the ctypes handle) is used for the low-level NULL-packet
    PLC path, since opuslib's high-level ``decode(None, …)`` crashes on
    ``len(None)``.
    """

    decoder_state: _OpusHandle

    def decode(
        self, opus_data: bytes, frame_size: int, decode_fec: bool = ...
    ) -> bytes:
        """Decode one Opus packet to interleaved PCM16 (optionally in FEC mode)."""
        ...


class _CtlRequest(Protocol):
    """An ``opuslib.api.ctl`` request function (e.g. ``set_inband_fec``)."""

    def __call__(self, func: object, obj: _OpusHandle, value: int) -> int:
        """Apply the CTL ``request`` with ``value`` to the codec ``obj``."""
        ...


class _CtlModule(Protocol):
    """The ``opuslib.api.ctl`` surface: the CTL request callables this module sets."""

    set_inband_fec: _CtlRequest
    set_packet_loss_perc: _CtlRequest


class _EncoderApi(Protocol):
    """The ``opuslib.api.encoder`` surface: the low-level CTL entry point."""

    def encoder_ctl(
        self, encoder_state: _OpusHandle, request: _CtlRequest, value: int = ...
    ) -> int:
        """Invoke a CTL ``request`` on ``encoder_state`` (passing ``value``)."""
        ...


class _DecoderApi(Protocol):
    """The ``opuslib.api.decoder`` surface: the low-level decode (NULL-packet PLC)."""

    def decode(  # noqa: PLR0913 — this mirrors opuslib.api.decoder.decode's real 6-parameter signature (decoder_state, opus_data, length, frame_size, decode_fec, channels); the Protocol must match it exactly to type the call site
        self,
        decoder_state: _OpusHandle,
        opus_data: bytes | None,
        length: int,
        frame_size: int,
        decode_fec: bool,
        channels: int = ...,
    ) -> bytes:
        """Decode (or, with ``opus_data=None``, conceal) one frame to PCM16."""
        ...


class _OpusApiModule(Protocol):
    """The ``opuslib.api`` surface used for the low-level CTL/PLC paths."""

    encoder: _EncoderApi
    decoder: _DecoderApi
    ctl: _CtlModule


class _EncoderCtor(Protocol):
    """``opuslib.Encoder(fs, channels, application) -> _RawEncoder``."""

    def __call__(self, fs: int, channels: int, application: int) -> _RawEncoder:
        """Construct an Opus encoder."""
        ...


class _DecoderCtor(Protocol):
    """``opuslib.Decoder(fs, channels) -> _RawDecoder``."""

    def __call__(self, fs: int, channels: int) -> _RawDecoder:
        """Construct an Opus decoder."""
        ...


class _OpuslibModule(Protocol):
    """The ``opuslib`` module surface used by this module."""

    Encoder: _EncoderCtor
    Decoder: _DecoderCtor
    api: _OpusApiModule


def _get_opuslib() -> _OpuslibModule:
    """Return the ``opuslib`` module; raise :exc:`ImportError` if it/libopus is absent.

    Called once per :class:`OpusEncoder` / :class:`OpusDecoder` construction.

    Raises:
        ImportError: If the ``webrtc`` extra (``opuslib``) is not installed, or the
            system ``libopus`` shared library cannot be loaded (``opuslib`` raises
            at import when ``ctypes.util.find_library('opus')`` returns ``None``).
    """
    try:
        mod: _OpuslibModule = importlib.import_module("opuslib")
    except ModuleNotFoundError as exc:
        msg = (
            "opuslib is required for the Opus codec (install the 'webrtc' extra: "
            "uv sync --extra webrtc)"
        )
        raise ImportError(msg) from exc
    except Exception as exc:
        # opuslib's loader raises a bare ``Exception('Could not find Opus library.
        # …')`` (NOT ModuleNotFoundError) when the system libopus.so is absent. We
        # catch it and re-raise as ImportError so callers handle one missing-
        # dependency type and the operator sees a clear "install libopus" message —
        # the error is acted upon and re-raised, never swallowed (rule 37), which is
        # why ruff's blind-except (BLE001) is satisfied without a suppression.
        msg = (
            "the Opus codec requires the system 'libopus' shared library "
            "(apt: libopus0); it could not be loaded"
        )
        raise ImportError(msg) from exc
    else:
        return mod


def ensure_opus_available() -> None:
    """Raise :class:`ImportError` unless the Opus codec can actually run.

    A pre-flight the adapter calls BEFORE answering a WebRTC/Opus call (ADR-0032): it
    forces the ``opuslib`` import AND the system ``libopus`` load (by constructing a
    throwaway encoder), so a host missing either is a clean call REJECT rather than an
    answered-but-dead call discovered only on the first encode. A no-op for codecs
    that do not need Opus — callers gate on the negotiated codec.

    Raises:
        ImportError: If the ``webrtc`` extra (``opuslib``) or the system ``libopus``
            shared library is unavailable.
    """
    # Constructing the encoder triggers _get_opuslib() (the import + the ctypes
    # libopus load via opuslib's loader), which is exactly the runtime path a real
    # call exercises; it is cheap (one libopus encoder) and discarded immediately.
    OpusEncoder()


def _check_frame(pcm16: bytes) -> None:
    """Raise :class:`ValueError` unless ``pcm16`` is exactly one 20 ms 48 kHz frame."""
    expected = OPUS_FRAME_SAMPLES * PCM16_BYTES_PER_SAMPLE
    if len(pcm16) != expected:
        msg = (
            f"Opus encode expects exactly one 20 ms 48 kHz frame "
            f"({OPUS_FRAME_SAMPLES} samples / {expected} bytes), got {len(pcm16)} bytes"
        )
        raise ValueError(msg)


class OpusEncoder:
    """Stateful Opus encoder for one direction of one call (48 kHz mono, 20 ms).

    Construct one per outbound stream; :meth:`encode` takes exactly one 20 ms
    48 kHz PCM16 frame (960 samples / 1920 bytes) and returns the Opus packet.

    **In-band FEC (ADR-0056).** The encoder enables Opus in-band forward error
    correction and an *expected packet-loss percentage* at construction, so each
    packet can carry a low-bitrate redundant copy of the previous frame — the
    loss-resilience the WebRTC SDP advertises (``useinbandfec=1``) and the reason
    Opus is chosen on the open internet. opuslib 3.0.1's ``inband_fec`` /
    ``packet_loss_perc`` *property setters are broken* (the lambda omits the CTL
    value argument and raises ``TypeError``), so these are set via the low-level
    ``opuslib.api.encoder.encoder_ctl(state, request, value)`` CTL; the getters,
    which work, read the applied state back (see :attr:`inband_fec_enabled` /
    :attr:`expected_packet_loss_pct`).

    Raises:
        ImportError: At construction if the ``webrtc`` extra (``opuslib``) or the
            system ``libopus`` is absent.
        ValueError: If ``expected_packet_loss_pct`` is outside ``0..100``.
    """

    def __init__(
        self, *, expected_packet_loss_pct: int = _DEFAULT_EXPECTED_PACKET_LOSS_PCT
    ) -> None:
        """Build the ``opuslib`` encoder in VOIP mode at 48 kHz mono with FEC on.

        Args:
            expected_packet_loss_pct: The loss percentage (0..100) Opus biases its
                in-band FEC strength for. Higher = more redundancy (and bitrate);
                the default (:data:`_DEFAULT_EXPECTED_PACKET_LOSS_PCT`) suits the
                open internet.
        """
        if not _MIN_LOSS_PCT <= expected_packet_loss_pct <= _MAX_LOSS_PCT:
            msg = (
                "expected_packet_loss_pct must be a percentage in "
                f"{_MIN_LOSS_PCT}..{_MAX_LOSS_PCT}, got {expected_packet_loss_pct}"
            )
            raise ValueError(msg)
        opuslib = _get_opuslib()
        self._api = opuslib.api
        self._enc: _RawEncoder = opuslib.Encoder(
            OPUS_SAMPLE_RATE, _CHANNELS, _APPLICATION_VOIP
        )
        # Enable in-band FEC and set the expected loss via the low-level CTL (the
        # high-level property setters are broken — see the class docstring). A
        # non-OK CTL raises opuslib.OpusError, which propagates (rule 37) rather
        # than silently leaving FEC off.
        ctl = opuslib.api.ctl
        self._api.encoder.encoder_ctl(self._enc.encoder_state, ctl.set_inband_fec, 1)
        self._api.encoder.encoder_ctl(
            self._enc.encoder_state, ctl.set_packet_loss_perc, expected_packet_loss_pct
        )

    @property
    def inband_fec_enabled(self) -> bool:
        """Whether in-band FEC is on (read back from libopus via the working getter)."""
        return self._enc.inband_fec != 0

    @property
    def expected_packet_loss_pct(self) -> int:
        """The expected packet-loss percentage the encoder is biased for."""
        return self._enc.packet_loss_perc

    def encode(self, pcm16: bytes) -> bytes:
        """Encode one 20 ms 48 kHz PCM16 frame to an Opus packet.

        Args:
            pcm16: Exactly one frame of PCM16-LE mono at 48 kHz (960 samples).

        Returns:
            The Opus-encoded packet bytes (carrying in-band FEC redundancy when
            the encoder judges it worthwhile for the configured expected loss).

        Raises:
            ValueError: If ``pcm16`` is not exactly one 20 ms 48 kHz frame.
        """
        _check_frame(pcm16)
        return self._enc.encode(pcm16, OPUS_FRAME_SAMPLES)


class OpusDecoder:
    """Stateful Opus decoder for one direction of one call (48 kHz mono, 20 ms).

    Construct one per inbound stream; :meth:`decode` takes one Opus packet and
    returns one 20 ms 48 kHz PCM16 frame (960 samples / 1920 bytes).

    **Loss recovery (ADR-0056).** Two concealment paths keep the decoder's
    internal predictor coherent across a lost frame (so wideband does not degrade
    worse than G.711):

    * :meth:`decode_fec` reconstructs the *previous* (lost) frame from the FEC
      copy carried inside the *next* received packet — exact recovery when the
      sender enabled in-band FEC and the successor packet has arrived.
    * :meth:`decode_plc` extrapolates a concealment frame from decoder state with
      no packet at all (a lone loss with no successor available yet).

    Raises:
        ImportError: At construction if the ``webrtc`` extra (``opuslib``) or the
            system ``libopus`` is absent.
    """

    def __init__(self) -> None:
        """Build the underlying ``opuslib`` decoder at 48 kHz mono."""
        opuslib = _get_opuslib()
        self._api = opuslib.api
        self._dec: _RawDecoder = opuslib.Decoder(OPUS_SAMPLE_RATE, _CHANNELS)

    def decode(self, packet: bytes) -> bytes:
        """Decode one Opus packet to a 20 ms 48 kHz PCM16 frame.

        Args:
            packet: One Opus packet (as produced by :meth:`OpusEncoder.encode` or
                received on the wire).

        Returns:
            One 20 ms 48 kHz PCM16-LE mono frame (960 samples / 1920 bytes).
        """
        return self._dec.decode(packet, OPUS_FRAME_SAMPLES)

    def decode_fec(self, next_packet: bytes) -> bytes:
        """Recover a lost frame from the in-band FEC in the NEXT received packet.

        When frame N is lost but packet N+1 has arrived, Opus can reconstruct
        frame N from the redundant low-bitrate copy the encoder embedded in packet
        N+1 (``decode_fec=True``). The decoder's predictor state is advanced for
        frame N exactly as a normal decode would — the caller then decodes packet
        N+1 normally on its next :meth:`decode`.

        Args:
            next_packet: The packet for frame N+1 (the successor of the lost one).

        Returns:
            The reconstructed lost frame as one 20 ms 48 kHz PCM16-LE mono frame.
        """
        return self._dec.decode(next_packet, OPUS_FRAME_SAMPLES, decode_fec=True)

    def decode_plc(self) -> bytes:
        """Conceal one lost frame with no packet (Opus packet-loss concealment).

        Opus extrapolates the missing frame from its decoder state. The opuslib
        *high-level* ``Decoder.decode(None, …)`` crashes on ``len(None)``, so this
        calls the low-level ``opuslib.api.decoder.decode(state, None, 0,
        frame_size, False, channels)`` — i.e. ``opus_decode(dec, NULL, 0, pcm,
        frame_size, 0)``, the documented NULL-packet PLC path.

        Returns:
            A concealment frame as one 20 ms 48 kHz PCM16-LE mono frame.
        """
        return self._api.decoder.decode(
            self._dec.decoder_state,
            None,
            0,
            OPUS_FRAME_SAMPLES,
            False,
            channels=_CHANNELS,
        )
