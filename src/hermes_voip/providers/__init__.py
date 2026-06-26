"""Typed provider seams for the VoIP plugin (ADR-0004).

Every external, swappable component (streaming ASR/TTS, the prompt-injection
guard, the SIP/WebRTC media transport) sits behind a typed ``Protocol`` here;
the core depends on these contracts, never on a concrete vendor. Audio crossing
any boundary is linear PCM16 framed at a declared sample rate — codec (G.711)
and 8<->16 kHz resampling are the media layer's job, never a provider's.

:func:`build_providers` wires a :class:`~hermes_voip.config.MediaConfig` to live
concrete provider instances; :class:`Providers` carries the result.

All 15 ADR-0004 public names are re-exported here so callers can import from
``hermes_voip.providers`` instead of reaching into sub-modules.
"""

from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import (
    AsrFactory,
    GuardFactory,
    Providers,
    TtsFactory,
    build_providers,
)
from hermes_voip.providers.guard import GuardResult, GuardVerdict, InjectionGuard
from hermes_voip.providers.transport import MediaTransport
from hermes_voip.providers.tts import StreamingTTS, TtsStream

__all__ = [
    "AsrFactory",
    "GuardFactory",
    "GuardResult",
    "GuardVerdict",
    "InjectionGuard",
    "MediaTransport",
    "PcmFrame",
    "Providers",
    "StreamingASR",
    "StreamingTTS",
    "Transcript",
    "TtsFactory",
    "TtsStream",
    "build_providers",
]
