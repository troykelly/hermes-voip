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


class _RawEncoder(Protocol):
    """The ``opuslib.Encoder`` surface this module uses."""

    def encode(self, pcm_data: bytes, frame_size: int) -> bytes:
        """Encode one frame of interleaved PCM16 to an Opus packet."""
        ...


class _RawDecoder(Protocol):
    """The ``opuslib.Decoder`` surface this module uses."""

    def decode(self, opus_data: bytes, frame_size: int) -> bytes:
        """Decode one Opus packet to interleaved PCM16."""
        ...


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

    Raises:
        ImportError: At construction if the ``webrtc`` extra (``opuslib``) or the
            system ``libopus`` is absent.
    """

    def __init__(self) -> None:
        """Build the underlying ``opuslib`` encoder in VOIP mode at 48 kHz mono."""
        opuslib = _get_opuslib()
        self._enc: _RawEncoder = opuslib.Encoder(
            OPUS_SAMPLE_RATE, _CHANNELS, _APPLICATION_VOIP
        )

    def encode(self, pcm16: bytes) -> bytes:
        """Encode one 20 ms 48 kHz PCM16 frame to an Opus packet.

        Args:
            pcm16: Exactly one frame of PCM16-LE mono at 48 kHz (960 samples).

        Returns:
            The Opus-encoded packet bytes.

        Raises:
            ValueError: If ``pcm16`` is not exactly one 20 ms 48 kHz frame.
        """
        _check_frame(pcm16)
        return self._enc.encode(pcm16, OPUS_FRAME_SAMPLES)


class OpusDecoder:
    """Stateful Opus decoder for one direction of one call (48 kHz mono, 20 ms).

    Construct one per inbound stream; :meth:`decode` takes one Opus packet and
    returns one 20 ms 48 kHz PCM16 frame (960 samples / 1920 bytes).

    Raises:
        ImportError: At construction if the ``webrtc`` extra (``opuslib``) or the
            system ``libopus`` is absent.
    """

    def __init__(self) -> None:
        """Build the underlying ``opuslib`` decoder at 48 kHz mono."""
        opuslib = _get_opuslib()
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
