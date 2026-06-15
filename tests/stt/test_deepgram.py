"""``DeepgramASR`` cloud-fallback against a recorded ws transcript fixture.

No live calls: the websocket is dependency-injected as a fake that (a) records
the audio frames the provider sends and (b) replays a recorded sequence of
Deepgram Flux JSON events back to the provider. This proves the provider's
framing + event-mapping without a Deepgram account or network (rule 26: the wire
*shape* is the recorded fixture; live interop is out of scope for a unit test).

Contract under test (ADR-0006):

* the provider declares an **8 kHz** input rate (Deepgram Flux ingests mu-law at
  8 kHz natively — no resample), and sends each PCM16 frame **G.711-encoded** to
  mu-law on the wire;
* Flux turn events map onto ``Transcript``: interim updates are
  ``is_final=False, end_of_turn=False``; an ``EndOfTurn`` (and the eager variant)
  sets ``end_of_turn=True`` *natively* (the engine owns the turn, unlike sherpa);
* the socket is closed when the inbound audio ends;
* a transport/JSON error surfaces to the consumer (rule 37).
"""

from __future__ import annotations

import json
import struct
from collections.abc import AsyncIterator

import pytest

from hermes_voip.media.audio import G711_SAMPLE_RATE, encode_ulaw
from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.stt.deepgram import DeepgramASR


def _frame(*samples: int, ts: int = 0) -> PcmFrame:
    return PcmFrame(
        samples=struct.pack(f"<{len(samples)}h", *samples),
        sample_rate=G711_SAMPLE_RATE,
        monotonic_ts_ns=ts,
    )


async def _frames(*items: PcmFrame) -> AsyncIterator[PcmFrame]:
    for item in items:
        yield item


# A recorded Deepgram Flux event stream (shapes per the verified API: a turn that
# grows through interim updates and finalises on EndOfTurn, then a second turn).
_RECORDED_EVENTS: tuple[str, ...] = (
    json.dumps({"type": "Update", "transcript": "what is", "end_of_turn": False}),
    json.dumps({"type": "Update", "transcript": "what is the", "end_of_turn": False}),
    json.dumps(
        {
            "type": "EndOfTurn",
            "transcript": "what is the balance",
            "end_of_turn": True,
        }
    ),
    json.dumps({"type": "Update", "transcript": "thanks", "end_of_turn": False}),
)


class _FakeFluxSocket:
    """A fake Deepgram websocket: records sent audio, replays recorded events.

    Mirrors the minimal ``async`` websocket surface the provider uses: ``send``
    (bytes), async iteration for inbound text frames, and ``close``. The replayed
    text frames are the recorded Flux JSON events.
    """

    def __init__(self, events: tuple[str, ...]) -> None:
        self._events = events
        self.sent: list[bytes] = []
        self.closed = False
        self.opened = False

    async def __aenter__(self) -> _FakeFluxSocket:
        self.opened = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            for event in self._events:
                yield event

        return _gen()


def _asr_with(socket: _FakeFluxSocket) -> DeepgramASR:
    """Build a DeepgramASR whose connection factory returns ``socket``."""
    return DeepgramASR(api_key="fake-key", connect=lambda: socket)


async def _run(asr: DeepgramASR, *frames: PcmFrame) -> list[Transcript]:
    """Drain ``asr.stream`` over ``frames`` into a list."""
    return [t async for t in asr.stream(_frames(*frames))]


def test_deepgram_asr_is_a_streaming_asr() -> None:
    """It conforms to the ADR-0004 ``StreamingASR`` Protocol."""
    asr: StreamingASR = _asr_with(_FakeFluxSocket(()))
    assert isinstance(asr, StreamingASR)


def test_deepgram_asr_declares_8k_input_rate() -> None:
    """Flux ingests mu-law @ 8 kHz natively; no resample to 16 kHz."""
    assert _asr_with(_FakeFluxSocket(())).input_sample_rate == G711_SAMPLE_RATE


@pytest.mark.asyncio
async def test_deepgram_asr_maps_flux_events_to_transcripts() -> None:
    """Interim Updates are non-final; EndOfTurn sets end_of_turn natively."""
    socket = _FakeFluxSocket(_RECORDED_EVENTS)
    asr = _asr_with(socket)
    out: list[Transcript] = [t async for t in asr.stream(_frames(_frame(0)))]

    assert [(t.text, t.is_final, t.end_of_turn) for t in out] == [
        ("what is", False, False),
        ("what is the", False, False),
        ("what is the balance", True, True),
        ("thanks", False, False),
    ]


@pytest.mark.asyncio
async def test_deepgram_asr_encodes_pcm16_to_mulaw_on_the_wire() -> None:
    """Each PCM16 frame is G.711 mu-law encoded before being sent to Flux."""
    socket = _FakeFluxSocket(())
    asr = _asr_with(socket)
    frame = _frame(0, 1000, -1000, 32767)
    [_ async for _ in asr.stream(_frames(frame))]

    assert socket.sent == [encode_ulaw(frame.samples)]


@pytest.mark.asyncio
async def test_deepgram_asr_closes_socket_when_audio_ends() -> None:
    """The websocket is closed once the inbound audio iterator is exhausted."""
    socket = _FakeFluxSocket(())
    asr = _asr_with(socket)
    [_ async for _ in asr.stream(_frames(_frame(0)))]
    assert socket.closed is True


@pytest.mark.asyncio
async def test_deepgram_asr_eager_end_of_turn_sets_end_of_turn() -> None:
    """An EagerEndOfTurn is treated as a (speculative) turn boundary too."""
    events = (
        json.dumps(
            {
                "type": "EagerEndOfTurn",
                "transcript": "cancel it",
                "end_of_turn": True,
            }
        ),
    )
    out = await _run(_asr_with(_FakeFluxSocket(events)), _frame(0))
    assert len(out) == 1
    assert out[0].end_of_turn is True


@pytest.mark.asyncio
async def test_deepgram_asr_propagates_transport_errors() -> None:
    """A websocket send error surfaces to the consumer (rule 37)."""

    class _BrokenSocket(_FakeFluxSocket):
        async def send(self, data: bytes) -> None:
            msg = "socket reset"
            raise ConnectionError(msg)

    asr = _asr_with(_BrokenSocket(()))
    with pytest.raises(ConnectionError, match="socket reset"):
        [_ async for _ in asr.stream(_frames(_frame(0)))]


@pytest.mark.asyncio
async def test_deepgram_asr_ignores_non_transcript_events() -> None:
    """Control/metadata events (no transcript) are skipped, not emitted blank."""
    events = (
        json.dumps({"type": "Connected"}),
        json.dumps({"type": "Update", "transcript": "", "end_of_turn": False}),
        json.dumps({"type": "Update", "transcript": "hello", "end_of_turn": False}),
    )
    out = await _run(_asr_with(_FakeFluxSocket(events)), _frame(0))
    assert [t.text for t in out] == ["hello"]
