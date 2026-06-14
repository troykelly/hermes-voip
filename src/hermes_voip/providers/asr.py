"""Streaming speech-to-text provider seam (ADR-0004; impls in ADR-0006)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hermes_voip.providers.audio import PcmFrame


@dataclass(frozen=True, slots=True)
class Transcript:
    """One ASR hypothesis (interim or final) for the current utterance.

    Attributes:
        text: The recognised text of this hypothesis.
        is_final: True when this hypothesis will not change.
        end_of_turn: True when the speaker has yielded the floor (turn boundary).
        confidence: Recogniser confidence in ``0.0..1.0``.
    """

    text: str
    is_final: bool
    end_of_turn: bool
    confidence: float


@runtime_checkable
class StreamingASR(Protocol):
    """Streaming recogniser: PCM16 frames in, interim/final transcripts out."""

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        """Consume PCM16 frames, yield interim and final ``Transcript`` values.

        Drains ``audio`` until exhausted (the caller closes it on hang-up).
        Engines without native turn detection set ``end_of_turn`` from the VAD
        signal the media layer supplies (ADR-0008); fused engines (e.g. Deepgram
        Flux) set it natively. This is a synchronous factory returning an async
        iterator — the caller iterates it, it does not ``await`` this call.
        """
        ...

    @property
    def input_sample_rate(self) -> int:
        """Declared input rate; the media layer resamples to match (e.g. 16000)."""
        ...
