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
    out = [item async for item in stream_from_thread(lambda: iter(range(0)))]
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

    async with aclosing(stream_from_thread(make_iter, max_buffer=1)) as stream:
        out: list[int] = []
        async for item in stream:
            out.append(item)
            if len(out) == 3:
                break

    assert out == [0, 1, 2]
    assert started.is_set()
    assert closed.is_set()  # the producer was closed, not abandoned
    # the named worker thread was joined (checking active_count would also count
    # asyncio.to_thread's executor-pool thread, so target the worker by name)
    assert all(t.name != "hermes-voip-stream" for t in threading.enumerate())


# --- hardening per cross-vendor concurrency review ---


@pytest.mark.asyncio
async def test_factory_exception_propagates_without_hang() -> None:
    def make_iter() -> Iterator[int]:
        msg = "factory boom"
        raise RuntimeError(msg)

    async def _collect() -> None:
        async for _ in stream_from_thread(make_iter):
            pass

    # must raise the producer error, not hang the consumer on queue.get()
    with pytest.raises(RuntimeError, match="factory boom"):
        await asyncio.wait_for(_collect(), timeout=3.0)


@pytest.mark.asyncio
async def test_on_cancel_unblocks_a_blocked_producer() -> None:
    release = threading.Event()
    cancelled = threading.Event()

    def make_iter() -> Iterator[int]:
        yield 0
        release.wait(timeout=5.0)  # blocks until on_cancel releases it

    def on_cancel() -> None:
        cancelled.set()
        release.set()

    async with aclosing(
        stream_from_thread(make_iter, max_buffer=4, on_cancel=on_cancel)
    ) as stream:
        async for _ in stream:
            break  # consume item 0, then close early while the producer is blocked

    assert cancelled.is_set()
    assert all(t.name != "hermes-voip-stream" for t in threading.enumerate())


@pytest.mark.asyncio
async def test_stuck_producer_raises_on_shutdown_not_silent() -> None:
    block = threading.Event()

    def make_iter() -> Iterator[int]:
        yield 0
        block.wait()  # blocks indefinitely; no on_cancel to release it

    async def run() -> None:
        async with aclosing(
            stream_from_thread(make_iter, max_buffer=4, shutdown_timeout=0.2)
        ) as stream:
            async for _ in stream:
                break

    try:
        with pytest.raises(RuntimeError, match="did not terminate"):
            await run()
    finally:
        block.set()  # release the leaked daemon thread so the test process is clean
