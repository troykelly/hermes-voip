"""Deepgram Flux cloud-fallback recogniser (StreamingASR, ADR-0006, opt-in).

:class:`DeepgramASR` is the **operator-gated, off-by-default** cloud fallback
(ADR-0006, rule 40): selecting it requires ``DEEPGRAM_API_KEY`` (config enforces
that, fail-fast). It is chosen for accuracy- or multilingual-critical
deployments, accepting the recorded caller-audio egress trade-off.

Deepgram Flux ingests **G.711 mu-law at 8 kHz natively** (no resample to 16 kHz),
so this provider declares an 8 kHz input rate and the media layer hands it 8 kHz
PCM16 frames; each frame is G.711-encoded to mu-law (the canonical codec in
:mod:`hermes_voip.media.audio`) before it goes on the wire. Flux's fused turn
detection emits ``EndOfTurn`` / ``EagerEndOfTurn`` events, so unlike the
self-host engine this provider sets ``Transcript.end_of_turn`` **natively** from
the event (ADR-0006 — when Deepgram is selected its turn signal is preferred).

The websocket is hidden behind the :class:`_FluxSocket` Protocol and supplied by
an injected ``connect`` factory, so the event-mapping is tested against a recorded
fixture with no network or account; the production factory opens a real
``websockets`` connection (the optional ``deepgram`` extra).

Async-lifecycle invariants
--------------------------
Sender and receiver run as concurrent :class:`asyncio.Task` objects so that
either task's failure immediately cancels the other (rule 37 — errors propagate,
never swallowed):

* The **sender** drains ``audio``, encodes each frame to mu-law, sends it, then
  sends a ``CloseStream`` control message so Deepgram flushes its buffers, emits
  any trailing finals, and closes the server side of the socket.
* The **receiver** maps inbound Flux JSON events to :class:`Transcript` and
  enqueues them; it exits naturally when the server closes (after receiving
  ``CloseStream``).

The two tasks communicate through a shared :class:`asyncio.Queue`; this async
generator drains the queue and yields each transcript to the consumer.  A shared
:class:`asyncio.Event` (``done``) signals when the receiver has exited (success
or failure) so the generator knows when there is nothing more to drain.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Final, Protocol

from hermes_voip.media.audio import G711_SAMPLE_RATE, encode_ulaw
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame

__all__ = ["DeepgramASR"]

# Deepgram Flux WebSocket endpoint + native telephony framing (mu-law @ 8 kHz).
_FLUX_ENDPOINT: Final[str] = (
    "wss://api.deepgram.com/v2/listen?encoding=mulaw&sample_rate=8000"
)

# Flux event "type" values that carry a transcript hypothesis. EndOfTurn and its
# speculative EagerEndOfTurn variant are turn boundaries; Update is interim.
_UPDATE: Final[str] = "Update"
_END_OF_TURN: Final[frozenset[str]] = frozenset({"EndOfTurn", "EagerEndOfTurn"})

# Flux does not surface a per-hypothesis confidence on this path; report full
# confidence (the value is informational — the turn decision is the event type).
_FULL_CONFIDENCE: Final[float] = 1.0

# Deepgram CloseStream control message: signals the server to flush its buffers,
# emit any remaining finals, and close the websocket from the server side.  Sent
# as a text frame (str) after all audio frames are exhausted.
_CLOSE_STREAM: Final[str] = json.dumps({"type": "CloseStream"})


class _FluxSocket(Protocol):
    """The minimal async websocket surface the provider drives.

    Both the real ``websockets`` connection and the test fake satisfy this. Text
    frames yielded by async iteration are Deepgram Flux JSON events; ``send``
    accepts bytes (mu-law audio) OR str (control messages); ``close`` ends the
    session.
    """

    async def send(self, data: bytes | str) -> None: ...

    async def close(self) -> None: ...

    def __aiter__(self) -> AsyncIterator[str]: ...


# Factory that opens a fresh Flux socket; injected so tests supply a fake and the
# production path opens a real websocket lazily (optional `deepgram` extra).
type _Connect = Callable[[], _FluxSocket]


class DeepgramASR:
    """Deepgram Flux streaming recogniser implementing ``StreamingASR``."""

    def __init__(self, api_key: str, *, connect: _Connect | None = None) -> None:
        """Create the fallback recogniser.

        Args:
            api_key: The Deepgram credential (read by reference from the env /
                1Password by the caller; never logged — rule 34). Required and
                non-empty: selecting this provider without a key is a config error
                surfaced fail-fast (ADR-0006).
            connect: Factory returning a fresh :class:`_FluxSocket`. Defaults to a
                real ``websockets`` connection to Flux; injected as a fake in tests.

        Raises:
            ValueError: If ``api_key`` is empty (fail-fast, rule 37).
        """
        if not api_key:
            msg = "DeepgramASR requires a non-empty api_key"
            raise ValueError(msg)
        self._api_key = api_key
        self._connect: _Connect = connect or self._default_connect

    @property
    def input_sample_rate(self) -> int:
        """8 kHz: Flux ingests mu-law @ 8 kHz natively (no resample)."""
        return G711_SAMPLE_RATE

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        """Stream PCM16 frames to Flux as mu-law; yield mapped transcripts.

        Sender and receiver run as concurrent tasks so that either task's failure
        immediately cancels the other (rule 37).  The sender sends a
        ``CloseStream`` control after the last audio frame so the server flushes,
        emits trailing finals, then closes; the receiver drains those events and
        exits naturally.  Transcripts flow through an :class:`asyncio.Queue` and
        are yielded to the consumer as they arrive.
        """
        return _generate(self._connect, audio)

    def _default_connect(self) -> _FluxSocket:
        """Open a real Flux websocket (production path; runtime-only ``websockets``).

        ``websockets`` is loaded lazily via :func:`importlib.import_module` — bound
        to the :class:`_WebsocketClientModule` Protocol that describes only
        ``connect`` — so ``import hermes_voip.stt`` works with zero cloud deps
        installed and ``mypy --strict`` stays clean without a ``# type: ignore``.
        The resulting connection is adapted to :class:`_FluxSocket`.
        """
        client: _WebsocketClientModule = importlib.import_module(
            "websockets.asyncio.client"
        )
        opener = client.connect(
            _FLUX_ENDPOINT,
            additional_headers={"Authorization": f"Token {self._api_key}"},
        )
        return _FluxConnection(opener)


async def _generate(
    connect: _Connect, audio: AsyncIterator[PcmFrame]
) -> AsyncIterator[Transcript]:
    """Run sender + receiver concurrently; yield transcripts as they arrive.

    The transcript queue carries :class:`Transcript` items from the receiver;
    the generator drains it until both tasks are done.  If either task raises,
    the other is cancelled and the exception is re-raised (rule 37).

    Sender and receiver are supervised via a shared :class:`asyncio.Event`
    (``error_event``) so the generator wakes up immediately when either task
    fails — no event-loop polling lag.
    """
    socket = connect()
    # Unbounded queue: network events must not be dropped.
    out: asyncio.Queue[Transcript] = asyncio.Queue()
    # Signalled as soon as either background task raises an exception.
    error_event = asyncio.Event()

    async def _guarded_sender() -> None:
        try:
            await _send_frames(socket, audio)
        except BaseException:
            error_event.set()
            raise

    async def _guarded_receiver() -> None:
        try:
            await _receive(socket, out)
        except BaseException:
            error_event.set()
            raise

    sender_task = asyncio.create_task(_guarded_sender())
    receiver_task = asyncio.create_task(_guarded_receiver())

    # ``error_task`` acts as a waitable for "something went wrong"; it is
    # cancelled once both background tasks are done.
    # mypy infers Event.wait() as Coroutine[Any,Any,Literal[True]] while
    # create_task[None] expects Coroutine[Any,Any,None]; the return value is
    # intentionally discarded (we only care that the event fired), so the cast
    # is sound.  No type-laundering: Task[None] vs Task[Literal[True]] is a
    # covariant read-only annotation difference with no runtime impact.
    error_task: asyncio.Task[None] = asyncio.create_task(error_event.wait())  # type: ignore[arg-type]

    try:
        # Drain the queue while the receiver is still running; surface any error
        # from either task as soon as it lands (rule 37 — no silent hold).
        while not receiver_task.done():
            # Wait for the next queue item OR for a task to finish / raise.
            get_task: asyncio.Task[Transcript] = asyncio.create_task(out.get())
            wait_set: set[asyncio.Task[object]] = {
                sender_task,
                receiver_task,
                get_task,
                error_task,
            }
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
            if get_task in done:
                # A transcript arrived; cancel the pending error watch for now.
                yield get_task.result()
            else:
                # A background task finished or errored; discard the queue-get.
                get_task.cancel()
            # If the error event fired, surface the exception immediately.
            if error_task in done or error_event.is_set():
                _raise_task_exception(sender_task)
                _raise_task_exception(receiver_task)
        # Receiver done normally; drain any transcripts already in the queue.
        while not out.empty():
            yield out.get_nowait()
        # Await sender to propagate any late error and mark it retrieved.
        await sender_task
    except BaseException:
        sender_task.cancel()
        receiver_task.cancel()
        for t in (sender_task, receiver_task):
            with contextlib.suppress(BaseException):
                await t
        raise
    finally:
        error_task.cancel()
        await socket.close()


def _raise_task_exception(task: asyncio.Task[object]) -> None:
    """Re-raise ``task``'s exception if it has one; no-op otherwise.

    Factored out so ``raise t.exception()`` (which ruff's TRY301 flags as an
    abstract-raise candidate) lives in a dedicated function rather than inside
    a conditional branch.
    """
    if task.done() and not task.cancelled():
        exc = task.exception()
        if exc is not None:
            raise exc


async def _send_frames(socket: _FluxSocket, audio: AsyncIterator[PcmFrame]) -> None:
    """Encode each PCM16 frame to mu-law, send it, then send ``CloseStream``.

    ``CloseStream`` is a Deepgram control message (text frame) that signals the
    server to flush its internal buffers, emit any remaining finals, and close
    the websocket from the server side.  This is the correct end-of-audio signal
    (not relying on a server-side timeout) so the receive loop drains all finals
    and exits naturally rather than hanging.
    """
    async for frame in audio:
        await socket.send(encode_ulaw(frame.samples))
    # Signal end-of-audio to Deepgram so it finalises and closes the socket.
    await socket.send(_CLOSE_STREAM)


async def _receive(socket: _FluxSocket, out: asyncio.Queue[Transcript]) -> None:
    """Drain inbound Flux events into ``out``; exits when the socket closes."""
    async for raw in socket:
        transcript = _map_event(raw)
        if transcript is not None:
            await out.put(transcript)


def _map_event(raw: str) -> Transcript | None:
    """Map one Flux JSON event to a ``Transcript``, or ``None`` to skip it.

    Control/metadata events (and empty hypotheses) yield ``None``; ``Update`` is
    interim; ``EndOfTurn`` / ``EagerEndOfTurn`` finalise the turn natively.
    """
    event = json.loads(raw)
    text = event.get("transcript", "")
    if not text:
        return None
    event_type = event.get("type", "")
    is_turn_end = event_type in _END_OF_TURN
    if event_type != _UPDATE and not is_turn_end:
        return None
    return Transcript(
        text=text,
        is_final=is_turn_end,
        end_of_turn=is_turn_end,
        confidence=_FULL_CONFIDENCE,
    )


class _FluxConnection:
    """Adapt a ``websockets`` async connection to :class:`_FluxSocket`.

    ``websockets.asyncio.client.connect(...)`` returns an async context manager;
    this opens it on first use and forwards ``send`` / iteration / ``close`` so the
    provider drives the uniform :class:`_FluxSocket` surface. ``__aiter__`` is a
    regular method returning an async generator (it must not be a coroutine), so
    the connection is awaited inside that generator on first iteration.
    """

    def __init__(self, opener: _WebsocketOpener) -> None:
        self._opener = opener
        self._conn: _WebsocketConnection | None = None

    async def _connection(self) -> _WebsocketConnection:
        if self._conn is None:
            self._conn = await self._opener.__aenter__()
        return self._conn

    async def send(self, data: bytes | str) -> None:
        conn = await self._connection()
        await conn.send(data)

    async def close(self) -> None:
        if self._conn is not None:
            await self._opener.__aexit__(None, None, None)
            self._conn = None

    def __aiter__(self) -> AsyncIterator[str]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[str]:
        conn = await self._connection()
        async for message in conn:
            yield message


class _WebsocketConnection(Protocol):
    """Structural view of a ``websockets`` connection's methods we call."""

    async def send(self, data: bytes | str) -> None: ...

    def __aiter__(self) -> AsyncIterator[str]: ...


class _WebsocketOpener(Protocol):
    """Structural view of the ``connect(...)`` async context manager."""

    async def __aenter__(self) -> _WebsocketConnection: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...


class _ConnectFn(Protocol):
    """Structural view of ``websockets.asyncio.client.connect`` (the parts we call)."""

    def __call__(
        self, uri: str, *, additional_headers: dict[str, str]
    ) -> _WebsocketOpener: ...


class _WebsocketClientModule(Protocol):
    """The single ``websockets.asyncio.client`` attribute this provider uses."""

    connect: _ConnectFn
