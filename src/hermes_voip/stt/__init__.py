"""Streaming speech-to-text providers (ADR-0006).

The default :class:`~hermes_voip.stt.sherpa_onnx.SherpaOnnxASR` is a fully
in-process, Apache-2.0 streaming zipformer (no cloud, no egress);
:class:`~hermes_voip.stt.deepgram.DeepgramASR` is the operator-gated cloud
fallback. Both implement the ADR-0004 ``StreamingASR`` Protocol
(:mod:`hermes_voip.providers.asr`), consuming PCM16 :class:`PcmFrame` at their
declared ``input_sample_rate`` and yielding :class:`Transcript` values. The media
glue (PCM16<->float32, 8 kHz->16 kHz upsampling) lives in
:mod:`hermes_voip.stt.resample`.
"""

from __future__ import annotations

from hermes_voip.stt.deepgram import DeepgramASR
from hermes_voip.stt.sherpa_onnx import SherpaOnnxASR

__all__ = [
    "DeepgramASR",
    "SherpaOnnxASR",
]
