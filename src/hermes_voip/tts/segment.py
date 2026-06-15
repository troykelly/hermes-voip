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

from collections.abc import AsyncIterator

__all__ = ["DEFAULT_FIRST_SEGMENT_MAX_CHARS", "SentenceAggregator", "segment_stream"]

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
