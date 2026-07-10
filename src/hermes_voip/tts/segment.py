"""Sentence/clause segmentation of the agent's streaming text (ADR-0007).

The synthesiser works per sentence (ADR-0007): the agent's incremental token
stream is cut into complete utterances as soon as a boundary is seen, so audio
starts before the whole reply is written and a ``cancel()`` at a sentence
boundary stays responsive. The *first* segment is additionally allowed to break
on a clause boundary (comma/semicolon/colon) while it is still short, because the
opening words are on the critical path to first-audio — "keep the first
sentence(s) short" (ADR-0007).

This module is **pure** — no model, no audio, no I/O. :class:`SentenceAggregator`
holds the buffer state and emits segments synchronously; :func:`segment_stream`
drives it over an async text stream for the TTS providers.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

__all__ = [
    "DEFAULT_FIRST_SEGMENT_MAX_CHARS",
    "FlushableSegmenter",
    "SentenceAggregator",
    "segment_stream",
]

# Characters that end a sentence; a run of them (e.g. "?!") ends one sentence.
_TERMINATORS = frozenset(".!?")
# Closing punctuation that belongs to the sentence it trails (so the boundary is
# after the quote/bracket, not before it): `She said "go".` -> `She said "go".`.
# The curly closers (»"') are intentional Unicode closing punctuation, so RUF001's
# ambiguous-character warning is suppressed for this literal (they are the point).
_CLOSERS = frozenset("\"')]}»”’")  # noqa: RUF001 - curly closers are intentional
# Clause punctuation that may end the *first* short segment early, for latency.
_CLAUSE = frozenset(",;:")
# Whitespace confirms a boundary mid-stream (text follows the punctuation).
_WS = frozenset(" \t\r\n\f\v")

#: Default cap (chars) on how long the first segment may grow before a clause
#: boundary stops counting — keeps first-audio + first barge-in point early
#: without chopping every later comma. 0 disables early clause splitting.
DEFAULT_FIRST_SEGMENT_MAX_CHARS = 48


class SentenceAggregator:
    """Accumulates streamed text and releases complete segments at boundaries.

    Feed text with :meth:`push` (returns any segments newly completed by that
    text) and end the stream with :meth:`flush` (returns the trailing remainder).
    Emitted segments are stripped of surrounding whitespace; internal spacing is
    preserved. The instance is single-stream and stateful — one per synthesis.
    """

    def __init__(
        self, first_segment_max_chars: int = DEFAULT_FIRST_SEGMENT_MAX_CHARS
    ) -> None:
        """Create an empty aggregator.

        Args:
            first_segment_max_chars: The first segment may break on a clause
                boundary (``,;:``) only while no longer than this many characters,
                trading a shorter opening utterance for lower first-audio latency.
                ``0`` disables early clause splitting (sentence boundaries only).

        Raises:
            ValueError: If ``first_segment_max_chars`` is negative.
        """
        if first_segment_max_chars < 0:
            msg = f"first_segment_max_chars must be >= 0, got {first_segment_max_chars}"
            raise ValueError(msg)
        self._first_segment_max_chars = first_segment_max_chars
        self._buffer = ""
        self._emitted_first = False

    def push(self, text: str) -> list[str]:
        """Append ``text`` and return any segments it completes, in order."""
        self._buffer += text
        segments: list[str] = []
        while (cut := self._next_boundary()) is not None:
            # Front-slice the emitted head off the buffer. This copies the buffer
            # remainder per emitted segment (same shape as the #310 RTP _tx_buffer
            # slice), but it is a COLD path: text arrives at LLM-chunk rate and the
            # buffer holds at most a sentence or two, so the copy costs a few
            # microseconds per SENTENCE. An offset cursor would add complexity for no
            # measurable gain, so the plain slice is kept (item 1687; rule 22 — do
            # not "optimise" this cold path).
            head, self._buffer = self._buffer[:cut], self._buffer[cut:]
            stripped = head.strip()
            if stripped:
                segments.append(stripped)
                self._emitted_first = True
        return segments

    def flush(self) -> str | None:
        """Return the buffered remainder (a final, unterminated segment), once.

        After the call the buffer is empty, so a second ``flush`` returns ``None``.
        A buffer that is empty or whitespace-only yields ``None`` rather than an
        empty segment (nothing to synthesise).
        """
        remainder = self._buffer.strip()
        self._buffer = ""
        if not remainder:
            return None
        self._emitted_first = True
        return remainder

    def _next_boundary(self) -> int | None:
        """Index one past the next boundary in the buffer, or ``None`` if none.

        A boundary is a sentence terminator run (plus any trailing closers)
        confirmed by following whitespace, or — only for a still-short first
        segment — a clause character confirmed by following whitespace. Requiring
        a following character is what makes detection safe across chunk splits: a
        terminator at the very end of the buffer waits for the next push (or
        ``flush``) rather than firing on a maybe-incomplete token.
        """
        buffer = self._buffer
        clause_ok = not self._emitted_first and self._first_segment_max_chars > 0
        for index, char in enumerate(buffer):
            if char in _TERMINATORS:
                end = self._terminator_end(index)
                if end is not None:
                    return end
            elif (
                clause_ok
                and char in _CLAUSE
                and index < self._first_segment_max_chars
                and self._followed_by_ws(index)
            ):
                return index + 1
        return None

    def _terminator_end(self, index: int) -> int | None:
        """Boundary index just past a terminator at ``index``, or ``None``.

        Skips a decimal point between digits (``3.14`` is one token), consumes a
        run of terminators (``?!``) and trailing closers, and requires whitespace
        (or, at ``flush`` time only, end-of-buffer) to follow.
        """
        buffer = self._buffer
        if self._is_decimal_point(index):
            return None
        end = index + 1
        while end < len(buffer) and buffer[end] in _TERMINATORS:
            end += 1
        while end < len(buffer) and buffer[end] in _CLOSERS:
            end += 1
        # A boundary mid-stream needs a following char (whitespace) to confirm it;
        # at end-of-buffer we cannot yet tell, so defer to flush().
        if end < len(buffer) and buffer[end] in _WS:
            return end
        return None

    def _is_decimal_point(self, index: int) -> bool:
        """True if ``buffer[index]`` is a ``.`` flanked by digits (a decimal)."""
        buffer = self._buffer
        return (
            buffer[index] == "."
            and index > 0
            and buffer[index - 1].isdigit()
            and index + 1 < len(buffer)
            and buffer[index + 1].isdigit()
        )

    def _followed_by_ws(self, index: int) -> bool:
        """True if the character after ``index`` exists and is whitespace."""
        return index + 1 < len(self._buffer) and self._buffer[index + 1] in _WS


async def segment_stream(chunks: AsyncIterator[str]) -> AsyncIterator[str]:
    """Segment an async text stream into complete utterances (ADR-0007).

    Drives a :class:`SentenceAggregator` over ``chunks`` (the agent's incremental
    output), yielding each segment the moment it completes and the trailing
    remainder when the stream ends. Whitespace-only input yields nothing.
    """
    aggregator = SentenceAggregator()
    async for chunk in chunks:
        for segment in aggregator.push(chunk):
            yield segment
    tail = aggregator.flush()
    if tail is not None:
        yield tail


@dataclass(frozen=True, slots=True)
class _End:
    """Queue sentinel marking the end of the segment stream.

    ``error`` carries an upstream exception so it re-raises on the consumer side
    (rule 37: input errors propagate, never swallowed); ``None`` is a clean end.
    """

    error: BaseException | None


class FlushableSegmenter:
    """Segment an async text stream, with an out-of-band :meth:`flush`.

    Like :func:`segment_stream` it cuts the agent's incremental text into complete
    utterances (ADR-0007) and yields them as they complete — but it additionally
    lets the call loop force synthesis of whatever is buffered *while the input
    iterator is still open*, via :meth:`flush`. That is the difference the no-op
    ``TtsStream.flush()`` was missing: without it, partial un-terminated text only
    reaches synthesis when the upstream iterator ends.

    A background task pumps the input through a :class:`SentenceAggregator` and
    enqueues each completed segment; :meth:`flush` drains the aggregator's current
    buffer into the same queue out of band. A lock serialises the two writers (the
    aggregator is single-stream and not re-entrant). The queue is unbounded, which
    is safe here: at most a handful of short segment *strings* are ever in flight
    (the consumer fully synthesises each segment before pulling the next, and
    audio back-pressure lives downstream in ``stream_from_thread``). This is the
    single shared segmenter both TTS backends drive (via ``PcmFrameStream``).
    """

    def __init__(
        self,
        chunks: AsyncIterator[str],
        *,
        first_segment_max_chars: int = DEFAULT_FIRST_SEGMENT_MAX_CHARS,
    ) -> None:
        """Wrap ``chunks`` (the agent's incremental text) in a flushable segmenter.

        Args:
            chunks: The agent's incremental output stream.
            first_segment_max_chars: Passed to :class:`SentenceAggregator` — the
                first segment may break early on a clause boundary while no longer
                than this, trading opening length for first-audio latency.
        """
        self._chunks = chunks
        self._aggregator = SentenceAggregator(first_segment_max_chars)
        self._queue: asyncio.Queue[str | _End] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._pump: asyncio.Task[None] | None = None
        self._closed = False

    def __aiter__(self) -> AsyncIterator[str]:
        """Return self; the segmenter is its own segment iterator."""
        return self

    async def __anext__(self) -> str:
        """Yield the next completed (or flushed) segment; end with the input."""
        self._ensure_pump()
        item = await self._queue.get()
        if isinstance(item, _End):
            if item.error is not None:
                raise item.error
            raise StopAsyncIteration
        return item

    async def flush(self) -> None:
        """Force the buffered (un-terminated) text out as a segment, now.

        Drains the aggregator's current buffer into the segment queue out of band,
        so a consumer iterating segments receives it immediately rather than only
        when the input iterator ends. A no-op when nothing is buffered or after
        the segmenter is closed.
        """
        if self._closed:
            return
        async with self._lock:
            tail = self._aggregator.flush()
            if tail is not None:
                self._queue.put_nowait(tail)

    async def aclose(self) -> None:
        """Stop the pump and release the input iterator (idempotent).

        Cancels the background pump and closes the upstream iterator if it is an
        async generator, so no task or generator is left running on barge-in or a
        clean end. Safe to call more than once and before iteration ever started.
        """
        self._closed = True
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            self._pump = None
        aclose = getattr(self._chunks, "aclose", None)
        if aclose is not None:
            await aclose()

    def _ensure_pump(self) -> None:
        """Start the input-draining pump task once (lazily, on first pull)."""
        if self._pump is None and not self._closed:
            self._pump = asyncio.ensure_future(self._run_pump())

    async def _run_pump(self) -> None:
        """Drain the input into segments; end (or surface an error) on the queue.

        Runs as a background task. ``CancelledError`` (from :meth:`aclose`)
        propagates out and ends the task without enqueuing an ``_End`` — teardown,
        not a stream error. Any other exception from the input is forwarded so it
        re-raises on the consumer side (rule 37).
        """
        try:
            async for chunk in self._chunks:
                async with self._lock:
                    for segment in self._aggregator.push(chunk):
                        self._queue.put_nowait(segment)
            async with self._lock:
                tail = self._aggregator.flush()
                if tail is not None:
                    self._queue.put_nowait(tail)
            self._queue.put_nowait(_End(None))
        except Exception as exc:  # noqa: BLE001 - forwarded to the consumer (rule 37)
            self._queue.put_nowait(_End(exc))
