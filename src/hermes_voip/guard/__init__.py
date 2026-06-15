"""The in-process prompt-injection guard (ADR-0009).

The default ``InjectionGuard`` (ADR-0004): screen each finalized caller turn for
prompt injection *before* it reaches the agent. Caller speech is untrusted input,
so the turn is de-obfuscated (:mod:`~hermes_voip.guard.normalize`) and classified
in-process by an offline DeBERTa ONNX model (:mod:`~hermes_voip.guard.onnx`); the
result is graded and stateful, and the guard fails *open* (RESTRICT + degraded) on
any inference error so a broken model never drops a legitimate caller.

The detector is layer 1, **not** the security boundary: the enforceable control is
the typed tool-policy gate (:func:`hermes_voip.providers.policy.gate_tool_call`),
which blocks irreversible actions even when the classifier misses.
"""

from __future__ import annotations

from hermes_voip.guard.normalize import NormalizedText, normalize
from hermes_voip.guard.onnx import (
    Classifier,
    GuardConfig,
    OnnxInjectionGuard,
    build_onnx_classifier,
)

__all__ = [
    "Classifier",
    "GuardConfig",
    "NormalizedText",
    "OnnxInjectionGuard",
    "build_onnx_classifier",
    "normalize",
]
