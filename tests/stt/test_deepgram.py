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
* a transport/JSON error surfaces to the consumer (rule 37);
* ``stream()`` completes after audio ends even when the fake socket stays open
  indefinitely (CloseStream sentinel is sent so the receive loop drains then
  exits — no hang);
* a ``socket.send()`` failure surfaces promptly (no hang; the receive loop is
  closed and the error re-raised from the consumer iterator).
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import AsyncIterator

import pytest

from hermes_voip.media.audio import G711_SAMPLE_RATE, encode_ulaw
from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.stt.deepgram import _CLOSE_STREAM, DeepgramASR


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
    (bytes for audio, str for control messages), async iteration for inbound text
    frames, and ``close``. The replayed text frames are the recorded Flux JSON
    events, followed by an unbounded wait until ``close()`` is called OR a
    ``CloseStream`` control message is received via ``send`` — matching real
    Deepgram behaviour where the server keeps the socket open until it receives
    ``CloseStream`` (or the client closes the connection).
    """

    def __init__(self, events: tuple[str, ...]) -> None:
        self._events = events
        self.sent: list[bytes] = []
        self.sent_text_frames: list[str] = []
        self.closed = False
        self.opened = False
        self._close_event = asyncio.Event()

    async def __aenter__(self) -> _FakeFluxSocket:
        self.opened = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True
        self._close_event.set()

    async def send(self, data: bytes | str) -> None:
        if isinstance(data, bytes):
            self.sent.append(data)
        else:
            self.sent_text_frames.append(data)
            # A text control frame (e.g. CloseStream): signal that the server
            # should close the socket — mirroring real Deepgram behaviour where
            # CloseStream causes the server to flush + close its side.
            self._close_event.set()

    async def close(self) -> None:
        self.closed = True
        self._close_event.set()

    def __aiter__(self) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            for event in self._events:
                yield event
            # Simulate real Deepgram: socket stays open (waiting for more audio or
            # a CloseStream control) until the provider signals end-of-stream.
            await self._close_event.wait()

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
async def test_deepgram_asr_sends_exact_closestream_control_frame() -> None:
    """Shutdown sends the real CloseStream sentinel, not an arbitrary text frame."""
    socket = _FakeFluxSocket(())
    asr = _asr_with(socket)

    [_ async for _ in asr.stream(_frames(_frame(0)))]

    assert socket.sent_text_frames == [_CLOSE_STREAM]


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
        async def send(self, data: bytes | str) -> None:
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


@pytest.mark.asyncio
async def test_deepgram_asr_stream_completes_after_audio_ends() -> None:
    """``stream()`` completes + yields trailing finals even when socket stays open.

    Real Deepgram sockets stay open until the server receives a CloseStream
    control (or the client closes the connection).  The provider must send the
    CloseStream sentinel (or close the socket) after the audio iterator is
    exhausted so the receive loop drains remaining finals and then ends — no
    hang.
    """
    # The socket emits a trailing final AFTER all audio has been sent, then
    # blocks in ``__aiter__`` until closed.  The provider must trigger the close
    # so both the trailing final is collected AND the receive loop terminates.
    trailing_event = json.dumps(
        {"type": "EndOfTurn", "transcript": "trailing", "end_of_turn": True}
    )
    socket = _FakeFluxSocket((trailing_event,))
    asr = _asr_with(socket)

    # Must complete within a tight deadline (no hang).
    transcripts = await asyncio.wait_for(
        _collect(asr.stream(_frames(_frame(0)))),
        timeout=2.0,
    )

    assert any(t.text == "trailing" and t.is_final for t in transcripts)
    assert socket.closed is True


@pytest.mark.asyncio
async def test_deepgram_asr_send_error_surfaces_promptly() -> None:
    """A ``socket.send()`` failure surfaces to the consumer without hanging.

    If the sender task raises (e.g. the connection was reset mid-stream), the
    consumer must see the error promptly — the receive loop must not block
    indefinitely waiting for server events that will never arrive.

    This is tested by running the stream as an asyncio Task and checking that
    the task has *already failed* after a brief cooperative yield — no need to
    wait for an external timeout to cancel it.  With concurrent supervision
    (TaskGroup / gather), the sender failure propagates to the consumer in
    O(one event-loop iteration); without it, the consumer hangs until some
    external cancel arrives.
    """

    class _BrokenSendSocket(_FakeFluxSocket):
        """Send always raises; ``__aiter__`` blocks until the socket is closed."""

        async def send(self, data: bytes | str) -> None:
            msg = "send failed mid-stream"
            raise ConnectionResetError(msg)

    socket = _BrokenSendSocket(())
    asr = _asr_with(socket)

    task: asyncio.Task[list[Transcript]] = asyncio.create_task(
        _collect(asr.stream(_frames(_frame(0))))
    )
    # Yield control enough times for the concurrent supervision to detect the
    # sender error and propagate it through the cleanup path.  With TaskGroup
    # the error surfaces in O(few event-loop iterations); without concurrent
    # supervision the consumer never sees it (hangs forever).
    for _ in range(20):
        await asyncio.sleep(0)

    # With concurrent supervision the task should already be done (failed).
    assert task.done(), "task is still running — sender error not yet surfaced (bug)"
    task.cancel()  # no-op if already done; prevents ResourceWarning
    with pytest.raises(ConnectionResetError, match="send failed mid-stream"):
        await task


async def _collect(it: AsyncIterator[Transcript]) -> list[Transcript]:
    return [t async for t in it]


# ---------------------------------------------------------------------------
# Malformed-frame guard tests (robustness — a single bad frame must not kill
# the stream; rule 37: genuine errors still propagate).
# ---------------------------------------------------------------------------


def test_map_event_returns_none_for_non_json() -> None:
    """_map_event must return None for a non-JSON frame (not raise JSONDecodeError).

    A transient Deepgram hiccup or binary-misclassified-as-text frame must not
    propagate json.JSONDecodeError up through _receive → _generate → CallLoop.
    """
    from hermes_voip.stt.deepgram import _map_event  # noqa: PLC0415

    result = _map_event("this is not json at all!!!")
    assert result is None


def test_map_event_returns_none_for_valid_json_wrong_shape() -> None:
    """_map_event must return None for valid JSON that lacks the expected fields.

    A valid-JSON-but-wrong-shape Flux frame (e.g. unexpected schema variant)
    must not propagate KeyError or TypeError — it should be skipped silently.
    """
    from hermes_voip.stt.deepgram import _map_event  # noqa: PLC0415

    # A JSON object that has neither "transcript" nor "type" at the top level.
    result = _map_event(json.dumps({"totally": "unexpected", "keys": [1, 2, 3]}))
    # No "transcript" key means empty text → already returns None via existing
    # logic, so this also exercises that path is safe.
    assert result is None

    # A JSON primitive (not a dict) — event.get() would raise AttributeError.
    result = _map_event(json.dumps(["list", "not", "dict"]))
    assert result is None

    # A JSON dict with a non-string "type" — no TypeError must escape.
    result = _map_event(json.dumps({"type": 42, "transcript": "hello"}))
    assert result is None


@pytest.mark.asyncio
async def test_stream_survives_malformed_frame_between_valid_ones() -> None:
    """A malformed frame between valid Flux events must not kill the stream.

    The receiver must skip bad frames and continue delivering subsequent
    valid transcripts — the live call must not end due to a single bad frame.
    """
    # Interleave a non-JSON frame between two valid Update events.
    events = (
        json.dumps({"type": "Update", "transcript": "hello", "end_of_turn": False}),
        "<<<not json>>>",  # malformed — must be skipped, not fatal
        json.dumps(
            {"type": "EndOfTurn", "transcript": "hello world", "end_of_turn": True}
        ),
    )
    socket = _FakeFluxSocket(events)
    asr = _asr_with(socket)

    out = await asyncio.wait_for(
        _collect(asr.stream(_frames(_frame(0)))),
        timeout=2.0,
    )

    # Both valid transcripts must arrive; the malformed frame must be skipped.
    assert [(t.text, t.is_final) for t in out] == [
        ("hello", False),
        ("hello world", True),
    ]


def test_malformed_frame_warning_does_not_log_raw_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The dropped-frame warning must NOT contain any slice of raw frame bytes.

    Rule 34 (content-leak guard): Flux frames carry transcribed speech and call
    metadata — logging ANY byte of content (even a 1-byte prefix) would leak
    sensitive data to the operator log stream.  Only the frame length (a safe
    integer) is permitted.

    The sentinel ``Zorblax-private-utterance`` is distinctive enough that a
    1-character or 8-character prefix appearing in a log message is unambiguously
    a content leak, not a coincidence.  This test catches the prior regression
    where ``first_byte=%r`` logged ``raw[:1]`` — the existing full-string check
    would have missed that because ``"Z"`` is NOT ``"Zorblax-private-utterance"``.
    """
    from hermes_voip.stt.deepgram import _map_event  # noqa: PLC0415

    # Distinctive, low-entropy sentinel (recognisable words, no high-entropy run)
    # so the public-repo leak scanner (.gitleaks.toml, rule 34) does not flag this
    # fixture as a credential; the recognisable prefix still makes partial-slice
    # leaks (raw[:1] = "Z", raw[:8] = "Zorblax-") detectable.
    sensitive_content = "Zorblax-private-utterance caller said transfer to account"

    with caplog.at_level(logging.WARNING, logger="hermes_voip.stt.deepgram"):
        result = _map_event(sensitive_content)

    assert result is None  # still returns None (non-regression)

    # The warning MUST have been emitted — proves the guard path actually ran.
    assert len(caplog.records) >= 1, "expected at least one warning log record"

    # No log message may contain ANY slice of the raw frame content:
    #   - the full string (original assertion)
    #   - the first byte  raw[:1]  = "Z"        ← caught the prior first_byte=%r leak
    #   - an 8-char prefix raw[:8] = "Zorblax-" ← catches any short-prefix variant
    #   - the distinctive marker itself (subset of the full string, belt-and-braces)
    first_byte = sensitive_content[:1]  # "Z"
    first_eight = sensitive_content[:8]  # "Zorblax-"
    marker_phrase = "Zorblax-private-utterance"

    for record in caplog.records:
        msg = record.getMessage()
        assert sensitive_content not in msg, (
            f"full raw frame content leaked into log: {msg!r}"
        )
        assert first_byte not in msg, (
            f"first-byte prefix of raw frame leaked into log"
            f" (raw[:1]={first_byte!r}): {msg!r}"
        )
        assert first_eight not in msg, (
            f"8-char prefix of raw frame leaked into log"
            f" (raw[:8]={first_eight!r}): {msg!r}"
        )
        assert marker_phrase not in msg, f"distinctive marker leaked into log: {msg!r}"
