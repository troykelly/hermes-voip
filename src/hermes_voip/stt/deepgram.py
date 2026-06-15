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


class _FluxSocket(Protocol):
    """The minimal async websocket surface the provider drives.

    Both the real ``websockets`` connection and the test fake satisfy this. Text
    frames yielded by async iteration are Deepgram Flux JSON events; ``send``
    takes the binary mu-law audio; ``close`` ends the session.
    """

    async def send(self, data: bytes) -> None: ...

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

        A sender task drains ``audio``, encodes each frame to mu-law, and sends it;
        the receive loop maps inbound Flux events to ``Transcript``. The socket is
        closed once the audio ends; any sender error surfaces to the consumer
        (rule 37).
        """

        async def _run() -> AsyncIterator[Transcript]:
            socket = self._connect()
            sender = asyncio.ensure_future(_send_frames(socket, audio))
            try:
                async for raw in socket:
                    transcript = _map_event(raw)
                    if transcript is not None:
                        yield transcript
                # Surface a sender failure that completed before the socket closed.
                await sender
            finally:
                if not sender.done():
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender
                elif (exc := _task_exception(sender)) is not None:
                    await socket.close()
                    raise exc
                await socket.close()

        return _run()

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


async def _send_frames(socket: _FluxSocket, audio: AsyncIterator[PcmFrame]) -> None:
    """Encode each inbound PCM16 frame to mu-law and send it to Flux."""
    async for frame in audio:
        await socket.send(encode_ulaw(frame.samples))


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


def _task_exception(task: asyncio.Task[None]) -> BaseException | None:
    """Return a finished task's exception, or ``None`` if it succeeded/cancelled."""
    if task.cancelled():
        return None
    return task.exception()


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

    async def send(self, data: bytes) -> None:
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

    async def send(self, data: bytes) -> None: ...

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
