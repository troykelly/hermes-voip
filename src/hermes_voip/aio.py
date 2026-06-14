"""Bridge a blocking iterator onto the event loop (plan P0.2).

The streaming providers (ADR-0006/0007/0008/0009) wrap synchronous engines —
sherpa-onnx, onnxruntime — whose recognisers/synthesisers yield from blocking
calls. ``stream_from_thread`` runs such an iterator on a dedicated worker thread
and yields its items on the running event loop, so the loop is never blocked.

Guarantees:
- **Ordered, lossless** delivery of every produced item.
- **Bounded back-pressure**: the worker blocks once ``max_buffer`` items are
  unconsumed (it never builds an unbounded backlog).
- **Exceptions propagate** to the consumer (rule 37): an error raised in the
  producer is re-raised from the async iterator.
- **Clean shutdown**: closing the async iterator early stops the producer
  (closing it if it is a generator) and joins the worker thread — no leak, no
  hang, even if the worker was blocked mid-hand-off.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator, Callable, Iterator
from concurrent.futures import CancelledError
from dataclasses import dataclass

_JOIN_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class _Item[T]:
    value: T


@dataclass(frozen=True, slots=True)
class _Error:
    exc: BaseException


@dataclass(frozen=True, slots=True)
class _Done:
    pass


type _Message[T] = _Item[T] | _Error | _Done


async def stream_from_thread[T](
    make_iterator: Callable[[], Iterator[T]],
    *,
    max_buffer: int = 8,
) -> AsyncGenerator[T]:
    """Run ``make_iterator()`` on a worker thread; yield its items on the loop.

    Args:
        make_iterator: A zero-arg factory called once *inside the worker thread*
            to create the blocking iterator (so its construction does not block
            the loop either).
        max_buffer: Maximum number of produced-but-unconsumed items before the
            worker blocks (back-pressure). Must be >= 1.

    Yields:
        Each item produced by the iterator, in order.

    Raises:
        ValueError: If ``max_buffer`` < 1.
        BaseException: Whatever the producer raised (re-raised on the loop side).
    """
    if max_buffer < 1:
        msg = f"max_buffer must be >= 1, got {max_buffer}"
        raise ValueError(msg)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[_Message[T]] = asyncio.Queue(maxsize=max_buffer)
    stop = threading.Event()

    def _put(message: _Message[T]) -> bool:
        """Hand a message to the loop's queue; block for space (back-pressure)."""
        if stop.is_set():
            return False
        future = asyncio.run_coroutine_threadsafe(queue.put(message), loop)
        try:
            future.result()
        except (RuntimeError, CancelledError):  # loop stopped / put cancelled
            return False
        return True

    def _worker() -> None:
        iterator = make_iterator()
        try:
            for item in iterator:
                if stop.is_set() or not _put(_Item(item)):
                    return
            _put(_Done())
        except BaseException as exc:  # noqa: BLE001 - surfaced to the consumer (rule 37)
            _put(_Error(exc))
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()  # deterministically run a generator's finally

    thread = threading.Thread(target=_worker, name="hermes-voip-stream", daemon=True)
    thread.start()
    try:
        while True:
            message = await queue.get()
            if isinstance(message, _Done):
                return
            if isinstance(message, _Error):
                raise message.exc
            yield message.value
    finally:
        stop.set()
        # free any slot the worker is blocked on so its pending put() completes
        while not queue.empty():
            queue.get_nowait()
        await asyncio.to_thread(thread.join, _JOIN_TIMEOUT_S)
