"""Tests for hermes_voip.aio — the blocking-iterator -> async bridge (plan P0.2).

The streaming STT/TTS/VAD/guard providers wrap synchronous engines (sherpa-onnx,
onnxruntime); ``stream_from_thread`` runs their blocking iterator on a worker
thread and yields its items on the event loop, with bounded back-pressure,
exception propagation, and clean shutdown that joins the thread (no leak/hang).
"""

import asyncio
import threading
from collections.abc import Iterator
from contextlib import aclosing

import pytest

from hermes_voip.aio import stream_from_thread


@pytest.mark.asyncio
async def test_yields_all_items_in_order() -> None:
    out = [item async for item in stream_from_thread(lambda: iter([1, 2, 3]))]
    assert out == [1, 2, 3]


@pytest.mark.asyncio
async def test_empty_producer_yields_nothing() -> None:
    out = [item async for item in stream_from_thread(lambda: iter([]))]
    assert out == []


@pytest.mark.asyncio
async def test_delivers_more_items_than_the_buffer() -> None:
    # back-pressure must not drop items: a producer larger than max_buffer
    out = [
        item
        async for item in stream_from_thread(lambda: iter(range(100)), max_buffer=4)
    ]
    assert out == list(range(100))


@pytest.mark.asyncio
async def test_propagates_worker_exception() -> None:
    def make_iter() -> Iterator[int]:
        yield 1
        msg = "boom in the worker"
        raise ValueError(msg)

    seen: list[int] = []

    async def _collect() -> None:
        async for item in stream_from_thread(make_iter):
            seen.append(item)

    with pytest.raises(ValueError, match="boom in the worker"):
        await _collect()
    assert seen == [1]


@pytest.mark.asyncio
async def test_early_close_stops_and_joins_the_worker() -> None:
    closed = threading.Event()
    started = threading.Event()

    def make_iter() -> Iterator[int]:
        started.set()
        try:
            value = 0
            while True:  # an unbounded producer
                yield value
                value += 1
        finally:
            closed.set()  # the worker must close us on early shutdown

    threads_before = threading.active_count()
    async with aclosing(stream_from_thread(make_iter, max_buffer=1)) as stream:
        out: list[int] = []
        async for item in stream:
            out.append(item)
            if len(out) == 3:
                break

    assert out == [0, 1, 2]
    assert started.is_set()
    assert closed.is_set()  # the producer was closed, not abandoned
    # the worker thread terminated (no leak)
    await asyncio.sleep(0)
    assert threading.active_count() <= threads_before
