"""Streaming text-to-speech providers (ADR-0007).

The default is the self-hosted sherpa-onnx + Kokoro-82M synthesiser
(:class:`SherpaKokoroTTS`); :class:`ElevenLabsTTS` is an opt-in cloud fallback.
Both implement the ``StreamingTTS`` / ``TtsStream`` seam (ADR-0004): incremental
agent text in, 24 kHz PCM16 frames out, with ``flush()`` and ``cancel()`` for
barge-in. Sentence/clause segmentation of the agent's token stream lives in
:mod:`hermes_voip.tts.segment`.
"""

from __future__ import annotations

from hermes_voip.tts.elevenlabs import ElevenLabsTTS
from hermes_voip.tts.failover import (
    FailoverTTS,
    SupportsCallReset,
    reset_failover_if_supported,
)
from hermes_voip.tts.segment import (
    FlushableSegmenter,
    SentenceAggregator,
    segment_stream,
)
from hermes_voip.tts.sherpa_kokoro import SherpaKokoroTTS, Synthesizer

__all__ = [
    "ElevenLabsTTS",
    "FailoverTTS",
    "FlushableSegmenter",
    "SentenceAggregator",
    "SherpaKokoroTTS",
    "SupportsCallReset",
    "Synthesizer",
    "reset_failover_if_supported",
    "segment_stream",
]
