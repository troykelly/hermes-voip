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
class keeps a handle on the active byte source and closes it explicitly at each
segment boundary and on teardown — which joins the worker and tears the
backend/connection down (rule 37: no leaked thread).

Barge-in (``cancel()``) is the other subtlety. It runs on a *different* task from
the consumer iterating frames, so it must not ``aclose()`` the frame generator
directly — that races the consumer's in-flight ``__anext__`` (``aclose():
asynchronous generator is already running``). Instead ``cancel()`` sets the shared
stop flag and calls the active segment's thread-safe ``abort`` (e.g. close the
HTTP socket) so a consumer parked in a blocking read unblocks; the consumer's own
resuming ``__anext__`` then runs the loop's teardown. A cooperative backend that
polls the stop flag (sherpa-onnx) needs no ``abort``.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field

from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame
from hermes_voip.spoken_text import strip_audio_tags
from hermes_voip.tts.segment import DEFAULT_FIRST_SEGMENT_MAX_CHARS, FlushableSegmenter

# Frames leave the provider with a placeholder timestamp; the media layer
# (ADR-0005) restamps every frame on the de-jittered playout clock.
_NO_TIMESTAMP = 0


def _noop() -> None:
    """Default per-segment abort: nothing to forcibly interrupt (cooperative)."""
    return


@dataclass(frozen=True, slots=True)
class SegmentSource:
    """One sentence's PCM-byte source plus a thread-safe barge-in abort.

    ``chunks`` is the worker-thread-bridged byte stream for the sentence;
    ``abort`` is called (from the loop thread) on ``cancel()`` to interrupt a
    backend parked in an uninterruptible read so iteration unblocks promptly. A
    cooperative backend that already polls the shared stop flag (sherpa-onnx)
    leaves ``abort`` at its no-op default; a blocking transport (ElevenLabs over
    urllib) supplies one that closes the in-flight HTTP response/socket.
    """

    chunks: AsyncIterator[bytes]
    abort: Callable[[], None] = field(default=_noop)


class PcmFrameStream:
    """A ``TtsStream`` over a per-segment PCM-byte source (ADR-0004/0007).

    Iterates 24 kHz ``PcmFrame``s; ``cancel()`` stops mid-utterance for barge-in
    (ADR-0008) and closes the active byte source. ``flush()`` forces the segmenter
    to emit whatever text is buffered as a segment *now* — so partial,
    un-terminated text reaches synthesis while the agent's input iterator is still
    open, not only when it ends (the call loop's end-of-turn / first-audio lever).

    This is the single shared ``TtsStream`` both providers use: it owns the
    :class:`FlushableSegmenter`, so the segmentation, flush, and cancel logic lives
    in one place rather than being duplicated per backend.
    """

    def __init__(  # noqa: PLR0913 — keyword-only constructor; every param is a real dependency/config knob
        self,
        *,
        text: AsyncIterator[str],
        open_segment: Callable[[str], SegmentSource],
        sample_rate: int,
        stop: threading.Event,
        first_segment_max_chars: int = DEFAULT_FIRST_SEGMENT_MAX_CHARS,
        preserve_audio_tags: bool = False,
    ) -> None:
        """Wire the stream.

        Args:
            text: The agent's incremental text stream (segmented internally).
            open_segment: Factory: given one sentence, return a :class:`SegmentSource`
                — its worker-thread-bridged PCM16 byte iterator plus a thread-safe
                barge-in ``abort`` for backends whose read cannot otherwise be
                interrupted.
            sample_rate: The provider's output rate (stamped on every frame).
            stop: The barge-in flag the backend polls; ``cancel()`` sets it. Shared
                with the backend so a cooperative producer can observe it.
            first_segment_max_chars: Forwarded to the segmenter (keeps the opening
                utterance short for first-audio latency, ADR-0007).
            preserve_audio_tags: Whether ElevenLabs v3 audio-tag cues (``[laughs]``,
                ``[breath]``, …) are PRESERVED in each segment (``True``) or STRIPPED
                before synthesis (``False``, the default — ADR-0027). The provider
                passes its own model capability: a v3-family model preserves them so
                they render; every other model (Flash/Turbo/Multilingual/Kokoro)
                strips them so the bracketed word is never voiced literally. Stripping
                is applied per *segment* (a whole sentence), so a tag split across the
                agent's streamed chunks is reassembled before it is removed.
        """
        self._segmenter = FlushableSegmenter(
            text, first_segment_max_chars=first_segment_max_chars
        )
        self._open_segment = open_segment
        self._sample_rate = sample_rate
        self._stop = stop
        self._preserve_audio_tags = preserve_audio_tags
        self._active: AsyncIterator[bytes] | None = None
        self._abort: Callable[[], None] = _noop
        self._frames: AsyncGenerator[PcmFrame] = self._run()
        self._torn_down = False

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        if self._stop.is_set():
            # Barge-in flagged with no pull resuming the run loop: drive teardown
            # here (joins the worker, closes the segmenter) so its effect is
            # complete and observable by the time iteration ends.
            await self._teardown()
            raise StopAsyncIteration
        return await self._frames.__anext__()

    async def flush(self) -> None:
        """Force synthesis of any buffered text now (end-of-turn / first-audio).

        Drains the segmenter's current buffer to a segment, so a consumer
        iterating frames receives audio for partial, un-terminated text while the
        agent's input iterator is still open — not only once it ends. A no-op when
        nothing is buffered or after :meth:`cancel`.
        """
        await self._segmenter.flush()

    async def cancel(self) -> None:
        """Stop now (barge-in): flag the backend and abort the in-flight read.

        Idempotent and **prompt**. Setting the stop flag halts iteration (a fresh
        ``__anext__`` then raises ``StopAsyncIteration``); the per-segment
        ``abort`` interrupts a backend parked in an uninterruptible read (closes
        the HTTP socket) so a consumer already awaiting the next frame unblocks and
        its own ``__anext__`` runs the run loop's teardown to completion. Crucially
        this does **not** ``aclose()`` the frame generator from here — doing so
        would collide with a consumer's in-flight ``__anext__`` on a different task
        (``aclose(): asynchronous generator is already running``). The worker join
        happens on that resuming pull, or via :meth:`aclose`.
        """
        self._stop.set()
        self._abort()

    async def aclose(self) -> None:
        """Cancel and drive teardown to completion (worker join, no thread left).

        For a clean shutdown when the single consumer is no longer iterating (it
        has stopped, broken, or never started). A teardown error — a backend that
        ignored both the stop flag and the abort, so ``stream_from_thread`` hit its
        join timeout — propagates here, never swallowed (rule 37).
        """
        await self.cancel()
        await self._teardown()

    async def _teardown(self) -> None:
        """Close the frame generator once: run its finally, join the worker.

        Idempotent. Safe because it is only reached when the single consumer is not
        parked inside ``self._frames.__anext__`` — either from ``__anext__``'s
        stop-flag short-circuit (which did not await the generator) or from
        ``aclose`` after iteration has ended.
        """
        if self._torn_down:
            return
        self._torn_down = True
        await self._frames.aclose()

    async def _run(self) -> AsyncGenerator[PcmFrame]:
        """Segment by segment, yield re-framed PCM until done or cancelled."""
        try:
            async for sentence in self._segmenter:
                if self._stop.is_set():
                    return
                # Model-conditional audio tags (ADR-0027): on a model that cannot
                # render v3 cues, strip the whole ``[tag]`` token from the (whole-
                # sentence) segment so it is never voiced literally; a v3 model keeps
                # them. A segment that was ONLY a tag reduces to empty — skip it
                # (no synthesis request for empty text).
                speakable = sentence
                if not self._preserve_audio_tags:
                    speakable = strip_audio_tags(sentence)
                    if not speakable:
                        continue
                source = self._open_segment(speakable)
                self._active = source.chunks
                self._abort = source.abort
                carry = b""
                try:
                    async for chunk in source.chunks:
                        if self._stop.is_set():
                            return
                        if not chunk:
                            continue
                        pcm16 = carry + chunk
                        frame_length = len(pcm16) - (
                            len(pcm16) % PCM16_BYTES_PER_SAMPLE
                        )
                        frame_bytes = pcm16[:frame_length]
                        carry = pcm16[frame_length:]
                        if frame_bytes:
                            yield self._frame(frame_bytes)
                    if self._stop.is_set():
                        # Barge-in aborted this segment's read (its socket was shut),
                        # so the source ended early: any trailing partial-sample
                        # remainder is an artefact of the interruption, not a stream
                        # error. Stop cleanly rather than raising below.
                        return
                    if carry:
                        msg = "PCM16 byte stream ended with a partial sample"
                        raise ValueError(msg)
                finally:
                    # Close this segment's source before the next one (and on the
                    # GeneratorExit that cancel()/early-return raises here).
                    await self._aclose_active()
        finally:
            # Stop the segmenter's pump and release the input iterator on any exit
            # — clean end, early return, or the GeneratorExit from aclose.
            await self._segmenter.aclose()

    async def _aclose_active(self) -> None:
        """Close the active per-segment byte source, if any (idempotent).

        Also clears the barge-in ``abort`` for the segment just finished, so a
        later ``cancel()`` between segments does not poke a closed source.
        """
        active, self._active = self._active, None
        self._abort = _noop
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
