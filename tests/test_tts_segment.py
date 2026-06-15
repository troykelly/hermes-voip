"""Sentence/clause segmentation of the agent's streaming text (ADR-0007).

The synthesiser works per sentence, so the agent's incremental token stream must
be cut into complete utterances as soon as a boundary is seen — and the *first*
segment kept short (clause boundaries count early) so first-audio and a
``cancel()`` at a boundary stay responsive (ADR-0007 "keep the first sentence(s)
short"). These tests pin the pure segmentation contract: ``SentenceAggregator``
(sync, stateful) and ``segment_stream`` (its async driver). No model, no numpy.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from hermes_voip.tts.segment import (
    FlushableSegmenter,
    SentenceAggregator,
    segment_stream,
)


def _drain(agg: SentenceAggregator, chunks: list[str]) -> list[str]:
    """Push every chunk then flush, collecting all emitted segments in order."""
    out: list[str] = []
    for chunk in chunks:
        out.extend(agg.push(chunk))
    tail = agg.flush()
    if tail is not None:
        out.append(tail)
    return out


# --- SentenceAggregator: boundary detection ---------------------------------


def test_complete_sentence_emits_on_terminator() -> None:
    """A full sentence is emitted as soon as its terminator + space arrives."""
    agg = SentenceAggregator()
    assert agg.push("Hello there. ") == ["Hello there."]


def test_partial_text_is_buffered_until_a_boundary() -> None:
    """Text with no boundary yet is held back, not emitted as a fragment."""
    agg = SentenceAggregator()
    assert agg.push("Hello ") == []
    assert agg.push("there") == []
    # Only on the terminator (here flushed at end) does the buffer release.
    assert agg.flush() == "Hello there"


def test_terminator_split_across_two_chunks() -> None:
    """A boundary that straddles a chunk boundary is still detected."""
    agg = SentenceAggregator()
    assert agg.push("How are you") == []
    # The '?' completes the sentence; the trailing space confirms the boundary.
    assert agg.push("? ") == ["How are you?"]


def test_multiple_sentences_in_one_chunk_split_in_order() -> None:
    """Several sentences in a single push come out individually, in order."""
    agg = SentenceAggregator()
    emitted = agg.push("One. Two! Three? ")
    assert emitted == ["One.", "Two!", "Three?"]


def test_trailing_quote_and_bracket_stay_with_the_sentence() -> None:
    """A closing quote/paren after the terminator is part of that sentence."""
    agg = SentenceAggregator()
    assert agg.push('She said "go". ') == ['She said "go".']
    assert agg.push("(done.) ") == ["(done.)"]


def test_decimal_number_is_not_split_mid_value() -> None:
    """A period between digits is a decimal point, not a sentence boundary."""
    agg = SentenceAggregator()
    assert agg.push("Pi is 3.14 today. ") == ["Pi is 3.14 today."]


def test_flush_returns_remainder_then_resets() -> None:
    """flush() yields the buffered tail once, then the aggregator is empty."""
    agg = SentenceAggregator()
    agg.push("dangling tail")
    assert agg.flush() == "dangling tail"
    # A second flush has nothing left to give.
    assert agg.flush() is None


def test_flush_on_empty_buffer_is_none() -> None:
    """Nothing buffered means flush() returns None, not an empty string."""
    assert SentenceAggregator().flush() is None


def test_emitted_segments_are_stripped_of_surrounding_whitespace() -> None:
    """Leading/trailing whitespace around a segment is trimmed, internal kept."""
    agg = SentenceAggregator()
    assert agg.push("  spaced   out  words.   ") == ["spaced   out  words."]


# --- first-segment-short: clause boundaries count early ----------------------


def test_first_segment_breaks_on_a_clause_boundary_for_low_latency() -> None:
    """The first segment is released at a clause break to cut first-audio time.

    Before any sentence terminator, a comma/semicolon/colon in the *opening*
    text is a valid early boundary so synthesis can start sooner (ADR-0007).
    """
    agg = SentenceAggregator(first_segment_max_chars=40)
    # The comma ends the first (short) segment even though the sentence runs on.
    emitted = agg.push("Sure, I can help with that today. ")
    assert emitted[0] == "Sure,"
    assert "I can help with that today." in emitted


def test_clause_break_does_not_apply_once_past_the_short_window() -> None:
    """After the first short segment, commas no longer force a split."""
    agg = SentenceAggregator(first_segment_max_chars=10)
    # First clause "Yes," releases early; the long remainder waits for a
    # terminator rather than splitting on every later comma.
    emitted = agg.push("Yes, here is a long winding clause, still going. ")
    assert emitted[0] == "Yes,"
    assert emitted[1] == "here is a long winding clause, still going."


def test_clause_boundary_disabled_when_window_is_zero() -> None:
    """first_segment_max_chars=0 disables early clause splitting entirely."""
    agg = SentenceAggregator(first_segment_max_chars=0)
    assert agg.push("Sure, I can help. ") == ["Sure, I can help."]


# --- segment_stream: the async driver over a token stream --------------------


async def _tokens(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


@pytest.mark.asyncio
async def test_segment_stream_emits_sentences_as_they_complete() -> None:
    """The async driver yields each completed sentence and flushes the tail."""
    out = [seg async for seg in segment_stream(_tokens("Hi the", "re. Bye", " now"))]
    assert out == ["Hi there.", "Bye now"]


@pytest.mark.asyncio
async def test_segment_stream_empty_input_yields_nothing() -> None:
    """An empty token stream produces no segments (no spurious empty flush)."""
    out = [seg async for seg in segment_stream(_tokens())]
    assert out == []


@pytest.mark.asyncio
async def test_segment_stream_whitespace_only_input_yields_nothing() -> None:
    """Whitespace-only text has no real segment to synthesise."""
    out = [seg async for seg in segment_stream(_tokens("   ", "\n\t "))]
    assert out == []


# --- FlushableSegmenter: out-of-band flush + error propagation ---------------


class _OpenEndedTokens:
    """A real ``AsyncIterator[str]``: yields one partial chunk, then stays open.

    Lets a test drive ``FlushableSegmenter.flush()`` while the input is still
    open (the call loop's end-of-turn lever), exactly as a live agent stream that
    has emitted a few un-terminated words and is still producing.
    """

    def __init__(self, partial: str) -> None:
        self.consumed = asyncio.Event()
        self.release = asyncio.Event()
        self._partial = partial
        self._yielded = False

    def __aiter__(self) -> AsyncIterator[str]:
        return self

    async def __anext__(self) -> str:
        if not self._yielded:
            self._yielded = True
            return self._partial
        self.consumed.set()
        await self.release.wait()
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_flushable_segmenter_flush_emits_buffer_while_input_open() -> None:
    """flush() drains the buffered partial text to a segment, input still open."""
    text = _OpenEndedTokens("partial unfinished words")
    seg = FlushableSegmenter(text)
    out: list[str] = []

    async def _consume() -> None:
        async for segment in seg:
            out.append(segment)

    consumer = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(text.consumed.wait(), timeout=1.0)
        await seg.flush()

        async def _has() -> None:
            while not out:
                await asyncio.sleep(0)

        await asyncio.wait_for(_has(), timeout=1.0)
        assert out == ["partial unfinished words"]
        assert not text.release.is_set()  # the input was never ended
    finally:
        await seg.aclose()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_flushable_segmenter_emits_completed_segments_then_flushes_tail() -> None:
    """It yields completed sentences as they arrive, then the trailing remainder."""
    seg = FlushableSegmenter(_tokens("First one. ", "tail without end"))
    out = [segment async for segment in seg]
    assert out == ["First one.", "tail without end"]


@pytest.mark.asyncio
async def test_flushable_segmenter_propagates_input_error() -> None:
    """An error from the input stream surfaces to the consumer (rule 37).

    The valid segment before the error is still delivered; the exception then
    re-raises rather than being swallowed.
    """

    async def _boom() -> AsyncIterator[str]:
        yield "Before the error. "
        msg = "input stream exploded"
        raise RuntimeError(msg)

    seg = FlushableSegmenter(_boom())
    seen: list[str] = []

    async def _consume() -> None:
        async for segment in seg:
            seen.append(segment)

    with pytest.raises(RuntimeError, match="input stream exploded"):
        await _consume()
    assert seen == ["Before the error."]
    await seg.aclose()
