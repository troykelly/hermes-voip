"""Shared ``TtsStream`` plumbing for the streaming TTS providers (ADR-0007).

Both providers (sherpa-Kokoro, ElevenLabs) turn the agent's text stream into 24
kHz ``PcmFrame``s by the same shape: segment the text (ADR-0007), then, per
sentence, pull PCM16 byte chunks from a per-segment async byte source. They
differ only in *what* that byte source is — a worker-thread-bridged sherpa
synthesis, or a worker-thread-bridged HTTP body. :class:`PcmFrameStream`
captures the common streaming/flush/cancel(barge-in) behaviour over an injected
``open_segment`` factory.

The subtle part is cancellation. The per-segment byte source is itself an async
generator running a worker thread; closing the *outer* frame generator does
**not** transitively close it (a ``GeneratorExit`` at the outer ``yield`` unwinds
the ``async for`` without awaiting the inner generator's ``aclose``). So this
class keeps a handle on the active byte source and closes it explicitly — on
barge-in ``cancel()`` and at each segment boundary — which joins the worker and
tears the backend/connection down (rule 37: no leaked thread).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable

from hermes_voip.providers.audio import PcmFrame

# Frames leave the provider with a placeholder timestamp; the media layer
# (ADR-0005) restamps every frame on the de-jittered playout clock.
_NO_TIMESTAMP = 0


def _surface_teardown_error(task: asyncio.Task[None]) -> None:
    """Surface a background teardown failure to the loop handler (rule 37).

    The cancel-path teardown runs detached; if it raised (a backend that ignored
    the stop flag, so ``stream_from_thread`` hit its join timeout), report it via
    the running loop's exception handler rather than letting it vanish as an
    unretrieved task exception. A normal cancellation is ignored.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        asyncio.get_running_loop().call_exception_handler(
            {"message": "TTS stream teardown failed", "exception": exc}
        )


class PcmFrameStream:
    """A ``TtsStream`` over a per-segment PCM-byte source (ADR-0004/0007).

    Iterates 24 kHz ``PcmFrame``s; ``cancel()`` stops mid-utterance for barge-in
    (ADR-0008) and closes the active byte source; ``flush()`` is a no-op because
    draining to exhaustion already synthesises the trailing sentence (the
    segmenter flushes its tail at stream end).
    """

    def __init__(
        self,
        *,
        segments: AsyncIterator[str],
        open_segment: Callable[[str], AsyncIterator[bytes]],
        sample_rate: int,
        stop: threading.Event,
    ) -> None:
        """Wire the stream.

        Args:
            segments: The agent's text already segmented into sentences (ADR-0007).
            open_segment: Factory: given one sentence, return an async iterator of
                PCM16-LE byte chunks for it (the worker-thread-bridged backend).
            sample_rate: The provider's output rate (stamped on every frame).
            stop: The barge-in flag the backend polls; ``cancel()`` sets it. Shared
                with the backend so a cooperative producer can observe it.
        """
        self._segments = segments
        self._open_segment = open_segment
        self._sample_rate = sample_rate
        self._stop = stop
        self._active: AsyncIterator[bytes] | None = None
        self._frames: AsyncGenerator[PcmFrame] = self._run()
        self._teardown: asyncio.Task[None] | None = None

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        if self._stop.is_set():
            # Barge-in: stop yielding. Reap the cancel-path teardown first so its
            # effect (the backend observing the stop flag, the worker joining) is
            # complete and observable by the time iteration ends.
            await self._reap_teardown()
            raise StopAsyncIteration
        return await self._frames.__anext__()

    async def flush(self) -> None:
        """No-op: iterating to exhaustion already emits the final sentence."""

    async def cancel(self) -> None:
        """Stop now (barge-in): flag the backend; tear the source down off-path.

        Idempotent and **prompt**: setting the stop flag halts iteration
        immediately (``__anext__`` then raises ``StopAsyncIteration``). The actual
        teardown — closing the frame generator, whose ``finally`` closes the active
        per-segment byte source and joins its worker — runs as a background task so
        ``cancel()`` does not block on a backend that only observes the stop flag
        on its *next* chunk (e.g. one parked in a native call). Its outcome is
        surfaced via the loop's exception handler, never swallowed (rule 37); a
        cooperative backend that polls the flag joins within
        ``stream_from_thread``'s shutdown timeout.
        """
        if self._teardown is not None:
            return  # already cancelling
        self._stop.set()
        self._teardown = asyncio.ensure_future(self._frames.aclose())
        self._teardown.add_done_callback(_surface_teardown_error)

    async def aclose(self) -> None:
        """Cancel (if not already) and await teardown to completion (clean exit).

        Unlike :meth:`cancel`, this awaits the worker join, so on return no thread
        is left running. A teardown error (a backend that ignored the stop flag)
        propagates here rather than only to the loop handler.
        """
        await self.cancel()
        await self._reap_teardown()

    async def _reap_teardown(self) -> None:
        """Await the in-flight cancel-path teardown to completion, once.

        Takes ownership of the teardown task (so a second call is a no-op),
        detaches the loop-handler callback — because awaiting here surfaces any
        teardown error directly (rule 37) rather than only via the loop handler —
        and awaits the worker join. Safe to call when no teardown is in flight.
        """
        teardown, self._teardown = self._teardown, None
        if teardown is not None:
            teardown.remove_done_callback(_surface_teardown_error)
            await teardown

    async def _run(self) -> AsyncGenerator[PcmFrame]:
        """Segment by segment, yield re-framed PCM until done or cancelled."""
        async for sentence in self._segments:
            if self._stop.is_set():
                return
            self._active = self._open_segment(sentence)
            try:
                async for chunk in self._active:
                    if self._stop.is_set():
                        return
                    if chunk:
                        yield self._frame(chunk)
            finally:
                # Close this segment's source before the next one (and on the
                # GeneratorExit that cancel()/early-return raises here).
                await self._aclose_active()

    async def _aclose_active(self) -> None:
        """Close the active per-segment byte source, if any (idempotent)."""
        active, self._active = self._active, None
        if active is not None:
            aclose = getattr(active, "aclose", None)
            if aclose is not None:
                await aclose()

    def _frame(self, pcm16: bytes) -> PcmFrame:
        """Wrap a PCM16-LE chunk as a ``PcmFrame`` at the provider's rate."""
        return PcmFrame(
            samples=pcm16,
            sample_rate=self._sample_rate,
            monotonic_ts_ns=_NO_TIMESTAMP,
        )
