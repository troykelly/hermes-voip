"""Streaming text-to-speech provider seam (ADR-0004; impls in ADR-0007)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from hermes_voip.providers.audio import PcmFrame


@runtime_checkable
class TtsStream(AsyncIterator[PcmFrame], Protocol):
    """Async iterator of PCM16 output frames with explicit lifecycle control."""

    async def flush(self) -> None:
        """Force synthesis of any buffered text and emit remaining frames."""
        ...

    async def cancel(self) -> None:
        """Stop synthesis NOW for barge-in: stop yielding and free the backend.

        Maps to the vendor primitive: Deepgram Aura ``Clear``, Cartesia cancel,
        ElevenLabs websocket ``close_context``, sherpa-onnx chunk callback ``0``.
        """
        ...

    async def aclose(self) -> None:
        """Close the stream and release its backend; idempotent.

        Drives teardown to completion when the single consumer is no longer
        iterating — it has finished, broken out, or an exception aborted the
        loop body. The consumer (the call loop's playout) wraps iteration in
        :func:`contextlib.aclosing`, so this is called on **every** exit path
        (normal end, barge-in, or a fatal error mid-playout) on the consumer's
        own task — never abandoning the underlying async generator for a GC
        finalizer to close later (which would race a parked pull and raise
        ``RuntimeError: aclose(): asynchronous generator is already running``).
        Safe to call more than once.
        """
        ...


@runtime_checkable
class StreamingTTS(Protocol):
    """Streaming synthesiser: incremental text in, PCM16 frames out."""

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        """Stream token/sentence text in, stream PCM16 frames out.

        ``text`` is the agent's incremental output; the engine begins emitting
        audio before ``text`` completes. ``voice`` is an opaque provider-scoped
        id. This is a synchronous factory returning a ``TtsStream`` — the caller
        iterates the result, it does not ``await`` this call.

        ``sample_rate`` is the negotiated wire rate the call loop requests PER CALL
        (ADR-0022): the process-wide provider does not know the per-call codec, so
        the loop passes the codec-derived rate (8 kHz G.711, 16 kHz G.722). A
        provider that can synthesise at that rate emits it (no wideband thrown away,
        no needless G.711 resample); a provider whose rate is fixed by its model
        (e.g. Kokoro at 24 kHz) ignores it and the media layer resamples. ``None``
        means "use the provider's default rate" (back-compat).
        """
        ...

    @property
    def output_sample_rate(self) -> int:
        """Declared default output rate; the media layer resamples to the wire rate."""
        ...
