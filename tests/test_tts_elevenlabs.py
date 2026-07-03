"""ElevenLabs Flash v2.5 cloud-fallback TTS tests (ADR-0007), no live calls.

The HTTP transport is dependency-injected, so these tests drive a **recorded**
PCM response fixture rather than the network. They pin: the streaming PCM16 @ the
telephony-native **8 kHz** output (the ADR-0007 amendment that fixes the live
"very choppy" audio — no lossy 3:1 resample), the request shape (endpoint,
``eleven_flash_v2_5`` model id, ``output_format=pcm_8000``, ``xi-api-key`` auth),
barge-in ``cancel()`` closing the underlying byte stream, and that the API key
never leaks via ``repr``. A live network call is never made.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import threading
import time
import urllib.request
from collections.abc import AsyncIterator, Iterator

import pytest

from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.tts.elevenlabs import (
    DEFAULT_VOICE_SETTINGS,
    G711_NARROWBAND_RATE,
    V3_MODEL_ID,
    ElevenLabsRequest,
    ElevenLabsTTS,
    ElevenLabsVoiceSettings,
    HttpByteStream,
    HttpCancellation,
    _abort_blocked_read,
    _UrllibHttp,
    elevenlabs_pcm_format,
)

# The telephony-native G.711 wire rate (ADR-0007 amendment): ElevenLabs is asked
# for pcm_8000 so the media layer encodes G.711 with NO resample.
_OUTPUT_RATE = 8_000
_G711_WIRE_RATE = 8_000
_FLASH_V2_5 = "eleven_flash_v2_5"
_FAKE_KEY = "sk_fake_elevenlabs_key_for_tests"  # an obvious test fake, not a secret

# A short recorded PCM16-LE body, delivered as network-sized chunks (the exact
# sample rate is immaterial to these transport/framing assertions).
_RECORDED_PCM_CHUNKS: tuple[bytes, ...] = (
    bytes(range(0, 64)),
    bytes(range(64, 128)),
    bytes(range(128, 160)),
)


class _RecordedHttp:
    """An injected HTTP transport that replays a recorded PCM body.

    Captures the request it was given (so tests can assert the wire contract)
    and records whether the byte stream was closed (so cancel() is observable).
    """

    def __init__(self, chunks: tuple[bytes, ...] = _RECORDED_PCM_CHUNKS) -> None:
        self.request: ElevenLabsRequest | None = None
        self.closed = False
        self._chunks = chunks

    def open(
        self, request: ElevenLabsRequest, cancel: HttpCancellation
    ) -> Iterator[bytes]:
        self.request = request
        # A real transport arms the cancellation with the live response so a
        # barge-in closes the socket; this recorded fake completes promptly, so
        # it simply records teardown via the generator's finally below.
        return self._iter()

    def _iter(self) -> Iterator[bytes]:
        try:
            yield from self._chunks
        finally:
            # GeneratorExit on .close() (cancel) or normal exhaustion both land
            # here; record it so a cancelled stream is provably torn down.
            self.closed = True


async def _text(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


class _OpenEndedText:
    """Yields one partial (un-terminated) chunk, then stays open indefinitely.

    A real ``AsyncIterator[str]`` (not just iterable): after the partial chunk it
    sets ``consumed`` and parks on ``release`` so the input iterator stays open.
    Lets a test prove ``flush()`` forces the buffered partial text to synthesise
    *while the input iterator is still open*, not only when it ends.
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


class _BlockingHttp:
    """A transport that parks in its body read until cancellation releases it.

    Models ``_UrllibHttp`` stuck in ``response.read()``: it yields nothing and
    blocks on an internal event. ``cancel.arm(...)`` registers the releaser, so
    when the loop calls ``cancel.close()`` on barge-in the blocked read returns
    and the worker thread is freed — the exact teardown path the no-``on_cancel``
    bug left hanging until the join timeout.
    """

    def __init__(self) -> None:
        self.opened = threading.Event()
        self.aborted = threading.Event()
        self._unblock = threading.Event()

    def open(
        self, request: ElevenLabsRequest, cancel: HttpCancellation
    ) -> Iterator[bytes]:
        # Arm BEFORE blocking, exactly like the real transport arms the response
        # right after urlopen() and before the first read().
        cancel.arm(self._abort)
        return self._iter()

    def _abort(self) -> None:
        self.aborted.set()
        self._unblock.set()  # release the parked read (socket close => read returns)

    def _iter(self) -> Iterator[bytes]:
        self.opened.set()
        # Park as if inside a blocking response.read() with no data yet; once the
        # abort releases us, the body is empty (the read was interrupted).
        self._unblock.wait()
        yield from ()


class _OddThenBlockHttp:
    """Yields one odd-length chunk, then parks in its read until barge-in aborts.

    Models a real stream interrupted mid-partial-sample: the frame loop has already
    emitted an odd number of PCM16 bytes (one byte short of a whole sample) when the
    barge-in fires, so the segment source ends leaving a 1-byte remainder. cancel()
    must end the stream cleanly — NOT raise the "ended with a partial sample" error,
    which would escape a deliberate barge-in as an unhandled fault. The park here is
    an interruptible event, so this isolates the frame loop's cancel handling from
    the transport's socket-shutdown fix.
    """

    _ODD_CHUNK = bytes(101)  # 50 whole PCM16 samples + 1 trailing byte

    def __init__(self) -> None:
        self.framed = threading.Event()
        self._unblock = threading.Event()

    def open(
        self, request: ElevenLabsRequest, cancel: HttpCancellation
    ) -> Iterator[bytes]:
        cancel.arm(self._unblock.set)
        return self._iter()

    def _iter(self) -> Iterator[bytes]:
        yield self._ODD_CHUNK
        self.framed.set()
        # Park as if inside a blocking response.read(); the barge-in abort releases
        # us and the generator returns, ending the segment with a 1-byte carry.
        self._unblock.wait()


class _ScriptedResponse:
    """A minimal ``HTTPResponse`` stand-in for the real ``_UrllibHttp`` read loop.

    ``read`` replays a script of byte chunks and, where an ``OSError`` is scripted,
    raises it — modelling a TLS ``response.read()`` that RAISES (rather than EOFs)
    once the barge-in socket shutdown interrupts it. ``fp`` is ``None`` so the
    barge-in's socket lookup finds nothing and its abort is a no-op; the response is
    instead closed by the read loop's own cleanup after the scripted OSError.
    """

    def __init__(self, reads: list[bytes | OSError]) -> None:
        self._reads = list(reads)
        self.closed = False
        self.fp: object = None

    def read(self, _amt: int) -> bytes:
        item = self._reads.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class _RawWithSock:
    """The ``SocketIO`` layer of ``response.fp.raw`` — holds the real ``_sock``."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock


class _FpWithRaw:
    """The buffered ``response.fp`` layer, exposing ``.raw`` like ``BufferedReader``."""

    def __init__(self, sock: socket.socket) -> None:
        self.raw = _RawWithSock(sock)


class _SocketBackedResponse:
    """HTTPResponse stand-in whose ``fp.raw._sock`` is a REAL socket.

    Lets a test prove the transport arms the barge-in with a socket shutdown: after
    ``open`` primes (EOF), calling the cancellation must ``shutdown`` this socket so
    a recv parked on it returns — the exact mechanism that frees a blocked read.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.fp = _FpWithRaw(sock)
        self.closed = False

    def read(self, _amt: int) -> bytes:
        return b""  # EOF: prime the generator so it arms then returns

    def close(self) -> None:
        self.closed = True


def _fake_stream_request() -> ElevenLabsRequest:
    """A request aimed at an obvious fake host (never the real API — repo is public)."""
    return ElevenLabsRequest(
        voice_id="x",
        model_id=_FLASH_V2_5,
        voice_settings=DEFAULT_VOICE_SETTINGS,
        output_format="pcm_8000",
        url="http://pbx.example.test/v1/text-to-speech/x/stream?output_format=pcm_8000",
        headers={"xi-api-key": _FAKE_KEY, "Content-Type": "application/json"},
        text="hello",
    )


async def _drain(stream: TtsStream) -> list[PcmFrame]:
    return [frame async for frame in stream]


def _make(http: HttpByteStream, *, voice: str = "Rachel") -> ElevenLabsTTS:
    return ElevenLabsTTS(api_key=_FAKE_KEY, voice=voice, http=http)


# --- conformance + output ----------------------------------------------------


def test_elevenlabs_is_a_streaming_tts() -> None:
    """ElevenLabsTTS satisfies StreamingTTS and declares the 8 kHz wire rate.

    The declared ``output_sample_rate`` must be the G.711 telephony wire rate so
    the media layer encodes with NO resample (ADR-0007 amendment): a 24 kHz rate
    here is exactly what forced the lossy 3:1 downsample behind the "very choppy"
    audio.
    """
    tts: StreamingTTS = _make(_RecordedHttp())
    assert isinstance(tts, StreamingTTS)
    assert tts.output_sample_rate == _OUTPUT_RATE
    assert tts.output_sample_rate == _G711_WIRE_RATE


@pytest.mark.asyncio
async def test_streams_recorded_pcm_as_8k_wire_rate_frames() -> None:
    """The recorded PCM body is surfaced as PCM16 frames at the 8 kHz wire rate."""
    http = _RecordedHttp()
    tts = _make(http)
    frames = await _drain(tts.synthesize(_text("Hello from the cloud. "), voice="x"))
    assert frames
    assert all(f.sample_rate == _G711_WIRE_RATE for f in frames)
    # No audio is lost or invented: the concatenated frame bytes equal the body.
    body = b"".join(f.samples for f in frames)
    assert body == b"".join(_RECORDED_PCM_CHUNKS)
    assert len(body) % PCM16_BYTES_PER_SAMPLE == 0


@pytest.mark.asyncio
async def test_stream_realigns_odd_length_pcm_chunks_without_losing_bytes() -> None:
    """Short HTTP reads may split a PCM16 sample across chunk boundaries."""
    chunks = (
        bytes(value % 256 for value in range(161)),
        bytes(value % 256 for value in range(161, 320)),
    )
    http = _RecordedHttp(chunks=chunks)
    tts = _make(http)

    frames = await _drain(tts.synthesize(_text("Odd transport chunks. "), voice="x"))

    assert frames
    assert all(len(frame.samples) % PCM16_BYTES_PER_SAMPLE == 0 for frame in frames)
    assert b"".join(frame.samples for frame in frames) == b"".join(chunks)


# --- request shape (the wire contract) --------------------------------------


@pytest.mark.asyncio
async def test_request_targets_flash_v2_5_pcm_endpoint_with_auth() -> None:
    """The request uses the Flash v2.5 model, pcm_8000, and xi-api-key auth.

    ``output_format`` MUST be the telephony-native ``pcm_8000`` (not ``pcm_24000``)
    so no lossy 24->8 kHz resample runs in the media path (ADR-0007 amendment).
    """
    http = _RecordedHttp()
    tts = _make(http, voice="VoiceXYZ")
    await _drain(tts.synthesize(_text("Say this. "), voice="VoiceXYZ"))

    req = http.request
    assert req is not None
    assert req.voice_id == "VoiceXYZ"
    assert req.model_id == _FLASH_V2_5
    assert req.output_format == "pcm_8000"
    # And the rate is encoded in the stream URL query so the API emits 8 kHz.
    assert "output_format=pcm_8000" in req.url
    assert "/v1/text-to-speech/VoiceXYZ/stream" in req.url
    assert req.headers["xi-api-key"] == _FAKE_KEY
    assert req.headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_request_body_carries_the_segmented_text() -> None:
    """Each synthesised segment's text is what gets sent in the request body."""
    http = _RecordedHttp()
    tts = _make(http)
    await _drain(tts.synthesize(_text("One sentence only. "), voice="x"))
    req = http.request
    assert req is not None
    assert req.text == "One sentence only."


# --- requested rate follows the codec (not a hardcoded 8 kHz pin) ------------


def test_default_rate_is_g711_narrowband() -> None:
    """The DEFAULT requested rate is the G.711 narrowband 8 kHz (today's only lane).

    8 kHz is requested because the SDP codec menu is G.711-only — it is the
    narrowband CASE of a codec->rate map, the value `_make_elevenlabs_tts` passes.
    """
    assert G711_NARROWBAND_RATE == _G711_WIRE_RATE
    tts = _make(_RecordedHttp())
    assert tts.output_sample_rate == G711_NARROWBAND_RATE


def test_elevenlabs_pcm_format_maps_supported_rates() -> None:
    """The codec->format helper maps each supported wire rate to its pcm_<rate>."""
    assert elevenlabs_pcm_format(8_000) == "pcm_8000"
    assert elevenlabs_pcm_format(16_000) == "pcm_16000"  # G.722 wideband lane
    assert elevenlabs_pcm_format(24_000) == "pcm_24000"


def test_elevenlabs_pcm_format_rejects_unsupported_rate() -> None:
    """An unsupported wire rate fails loudly, not by silent lossy fallback."""
    with pytest.raises(ValueError, match="no native PCM output for 12345 Hz"):
        elevenlabs_pcm_format(12_345)


@pytest.mark.asyncio
async def test_wideband_rate_requests_matching_pcm_format() -> None:
    """A non-default (wideband) rate requests the MATCHING pcm format, not pcm_8000.

    Proves the request rate is NOT an unconditional 8 kHz pin: constructing the
    provider at 16 kHz (the G.722 lane the wideband work will negotiate) requests
    ``pcm_16000`` and emits 16 kHz frames — the rate follows the codec.
    """
    http = _RecordedHttp()
    tts = ElevenLabsTTS(
        api_key=_FAKE_KEY, voice="x", http=http, output_sample_rate=16_000
    )
    assert tts.output_sample_rate == 16_000
    frames = await _drain(tts.synthesize(_text("Wideband please. "), voice="x"))
    assert frames
    assert all(f.sample_rate == 16_000 for f in frames)
    req = http.request
    assert req is not None
    assert req.output_format == "pcm_16000"
    assert "output_format=pcm_16000" in req.url


@pytest.mark.asyncio
async def test_per_call_sample_rate_overrides_the_construction_default() -> None:
    """``synthesize(sample_rate=…)`` selects the rate PER CALL (ADR-0022).

    The provider is built once (process-wide) but the negotiated codec is
    per-call, so the call loop passes the negotiated wire rate into ``synthesize``.
    A G.722 call (16 kHz) must request ``pcm_16000`` and emit 16 kHz frames even
    though the provider's construction default is the 8 kHz G.711 case — the rate
    follows the negotiated codec, not the construction default.
    """
    http = _RecordedHttp()
    tts = ElevenLabsTTS(api_key=_FAKE_KEY, voice="x", http=http)  # default 8 kHz
    assert tts.output_sample_rate == _G711_WIRE_RATE
    frames = await _drain(
        tts.synthesize(_text("Wideband per call. "), voice="x", sample_rate=16_000)
    )
    assert frames
    assert all(f.sample_rate == 16_000 for f in frames)
    req = http.request
    assert req is not None
    assert req.output_format == "pcm_16000"
    assert "output_format=pcm_16000" in req.url


@pytest.mark.asyncio
async def test_per_call_sample_rate_none_uses_construction_default() -> None:
    # The G.711 case: no per-call override -> the 8 kHz construction default, so
    # the choppiness fix (no resample for G.711) is preserved.
    http = _RecordedHttp()
    tts = ElevenLabsTTS(api_key=_FAKE_KEY, voice="x", http=http)
    frames = await _drain(
        tts.synthesize(_text("Narrowband default. "), voice="x", sample_rate=None)
    )
    assert frames
    assert all(f.sample_rate == _G711_WIRE_RATE for f in frames)
    req = http.request
    assert req is not None
    assert req.output_format == "pcm_8000"


@pytest.mark.asyncio
async def test_per_call_unsupported_rate_rejected() -> None:
    # A per-call rate ElevenLabs cannot emit fails loudly (no silent fallback).
    http = _RecordedHttp()
    tts = ElevenLabsTTS(api_key=_FAKE_KEY, voice="x", http=http)
    with pytest.raises(ValueError, match="no native PCM output"):
        tts.synthesize(_text("Bad rate. "), voice="x", sample_rate=12_345)


def test_unsupported_output_rate_rejected_at_construction() -> None:
    """A wire rate ElevenLabs cannot emit fails fast at construction, not mid-call."""
    with pytest.raises(ValueError, match="no native PCM output"):
        ElevenLabsTTS(
            api_key=_FAKE_KEY,
            voice="x",
            http=_RecordedHttp(),
            output_sample_rate=11_025,
        )


# --- secret hygiene ----------------------------------------------------------


def test_api_key_is_not_exposed_in_repr() -> None:
    """The credential never appears in repr() (invariant: secrets never logged)."""
    tts = _make(_RecordedHttp())
    assert _FAKE_KEY not in repr(tts)


def test_empty_api_key_is_rejected() -> None:
    """A blank API key is a misconfiguration and fails fast at construction."""
    with pytest.raises(ValueError, match="api_key"):
        ElevenLabsTTS(api_key="", voice="x", http=_RecordedHttp())


# --- cancel / barge-in -------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_closes_the_byte_stream() -> None:
    """cancel() tears down the HTTP byte stream (stops audio + frees the socket)."""
    http = _RecordedHttp(chunks=tuple(bytes([b]) * 8 for b in range(50)))
    tts = _make(http)
    stream = tts.synthesize(_text("A long streamed reply to interrupt. "), voice="x")

    await anext(aiter(stream))  # start consuming
    await stream.cancel()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert http.closed is True


@pytest.mark.asyncio
async def test_cancel_before_iteration_yields_nothing() -> None:
    """Cancelling before the first frame yields an empty stream."""
    http = _RecordedHttp()
    tts = _make(http)
    stream = tts.synthesize(_text("Unheard. "), voice="x")
    await stream.cancel()
    assert await _drain(stream) == []


@pytest.mark.asyncio
async def test_cancel_releases_a_worker_blocked_in_the_http_read() -> None:
    """Barge-in aborts an in-flight HTTP read and frees the worker promptly.

    A consumer is parked awaiting the next frame while the transport sits inside
    ``response.read()`` (no data yet). Barge-in (``cancel()``, on a different task)
    must close the response from the loop side so the read returns, the worker
    exits, and the consumer's iteration ends — all within the worker-join timeout.

    With no ``on_cancel`` wired into ``stream_from_thread`` the loop side cannot
    close the socket, so teardown blocks on the read until the 5s join timeout and
    then raises ``RuntimeError`` ("producer ignored cancellation"). The fix makes
    this complete promptly with the worker provably released.
    """
    http = _BlockingHttp()
    tts = _make(http)
    stream = tts.synthesize(_text("Interrupt me mid-fetch. "), voice="x")

    ended = asyncio.Event()

    async def _consume() -> None:
        try:
            async for _ in stream:
                pass
        finally:
            ended.set()

    consumer = asyncio.create_task(_consume())
    try:
        # The worker reaches the (blocked) read; nothing is yielded yet.
        await asyncio.to_thread(http.opened.wait, 1.0)
        assert http.opened.is_set()
        assert not ended.is_set()

        # Barge-in from this (separate) task. If cancel did not abort the blocked
        # read, the consumer would hang ~5s then surface the join-timeout error.
        await stream.cancel()
        await asyncio.wait_for(ended.wait(), timeout=2.0)
        assert http.aborted.is_set()  # the in-flight read was aborted from the loop
        # The worker thread is provably gone (not just the read returned).
        assert all(t.name != "hermes-voip-stream" for t in threading.enumerate())
    finally:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


@contextlib.contextmanager
def _stalling_http_server() -> Iterator[tuple[str, threading.Event]]:
    """A localhost server that sends 200 headers then stalls mid-body, held open.

    Yields ``(base_url, headers_sent)``. On exit it stops serving and closes the
    accepted socket, so a worker still parked in the body read is released even when
    the test under it failed (no leaked thread).
    """
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(10.0)
    host, port = server.getsockname()
    accepted: list[socket.socket] = []
    headers_sent = threading.Event()
    stop_serving = threading.Event()

    def _serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        accepted.append(conn)
        buf = b""
        while b"\r\n\r\n" not in buf:
            data = conn.recv(4096)
            if not data:
                return
            buf += data
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: audio/pcm\r\n"
            b"Content-Length: 1048576\r\n\r\n"
        )
        headers_sent.set()
        while not stop_serving.wait(0.05):  # stall: no body; hold open until torn down
            pass

    thread = threading.Thread(target=_serve, name="stalling-http-server", daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}", headers_sent
    finally:
        stop_serving.set()
        for conn in accepted:
            with contextlib.suppress(OSError):
                conn.close()
        server.close()
        thread.join(5.0)


def test_barge_in_interrupts_a_worker_blocked_in_a_real_socket_read() -> None:
    """Barge-in tears down a REAL urllib socket parked mid-body (whole-loop freeze).

    Reproduces the availability bug end to end: a worker thread blocks in the real
    ``_UrllibHttp`` ``response.read()`` against a server that sent headers then
    stalled mid-body; a barge-in ``cancel()`` on the event-loop thread must release
    BOTH the worker AND the loop. With the pre-fix code the loop thread blocks
    inside ``response.close()`` (waiting on the read lock the parked worker holds)
    and the worker never returns — a deadlock that freezes every concurrent call.

    The scenario runs on a daemon thread bounded by a join, because that pre-fix
    freeze happens synchronously on the loop thread (an ``asyncio`` timeout could
    not fire) — so the bug FAILS this test via the bounded join, never hanging the
    suite. ``_stalling_http_server`` tears down in ``finally``, releasing the parked
    worker even on the failing run.
    """
    released = threading.Event()

    with _stalling_http_server() as (base_url, headers_sent):

        async def _scenario() -> None:
            tts = ElevenLabsTTS(
                api_key=_FAKE_KEY, voice="x", http=_UrllibHttp(), base_url=base_url
            )
            stream = tts.synthesize(_text("Interrupt a real socket read. "), voice="x")
            ended = asyncio.Event()

            async def _consume() -> None:
                try:
                    async for _ in stream:
                        pass
                finally:
                    ended.set()

            consumer = asyncio.create_task(_consume())
            try:
                await asyncio.to_thread(headers_sent.wait, 5.0)
                await asyncio.sleep(0.2)  # let the worker enter the blocked read
                await stream.cancel()  # pre-fix: freezes here inside response.close()
                await asyncio.wait_for(ended.wait(), timeout=4.0)
            finally:
                consumer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consumer
            released.set()

        def _run() -> None:
            # A deadlock leaves ``released`` unset; suppress so the daemon thread does
            # not print a traceback — the bounded join + assertion is the signal.
            with contextlib.suppress(BaseException):
                asyncio.run(_scenario())

        scenario = threading.Thread(target=_run, name="scenario", daemon=True)
        scenario.start()
        scenario.join(6.0)
        assert released.is_set(), (
            "barge-in did not release the loop within 6s: a blocked response.read()"
            " deadlocked the event loop (cancel() never returned)"
        )
        assert all(t.name != "hermes-voip-stream" for t in threading.enumerate())


@pytest.mark.asyncio
async def test_barge_in_after_an_odd_partial_sample_ends_cleanly() -> None:
    """A barge-in that leaves a 1-byte remainder ends the stream, not raises.

    Once the socket-shutdown fix makes an interrupted read return promptly, the
    bytes delivered before the barge-in can be an odd count, leaving the frame loop
    holding a 1-byte carry. cancel() must surface that as a clean end of stream: the
    "ended with a partial sample" ValueError is a real-stream integrity check, not
    something a deliberate barge-in should raise into the call loop.
    """
    http = _OddThenBlockHttp()
    tts = _make(http)
    stream = tts.synthesize(_text("Cut me mid-sample. "), voice="x")

    frames: list[PcmFrame] = []
    ended = asyncio.Event()
    errors: list[BaseException] = []

    async def _consume() -> None:
        try:
            async for frame in stream:
                frames.append(frame)
        except BaseException as exc:  # noqa: BLE001 - the test asserts nothing escaped
            errors.append(exc)
        finally:
            ended.set()

    consumer = asyncio.create_task(_consume())
    try:
        # Wait until the odd chunk has been framed and the worker is parked in read.
        await asyncio.to_thread(http.framed.wait, 2.0)

        async def _has_frame() -> None:
            while not frames:
                await asyncio.sleep(0)

        await asyncio.wait_for(_has_frame(), timeout=2.0)

        await stream.cancel()
        await asyncio.wait_for(ended.wait(), timeout=2.0)
        assert errors == []  # a barge-in remainder must not surface as an error
        assert frames  # the whole-sample bytes before the cut were still delivered
    finally:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


# --- flush forces buffered text to synthesis (input iterator still open) -----


@pytest.mark.asyncio
async def test_flush_synthesises_buffered_text_while_input_is_still_open() -> None:
    """flush() pushes buffered partial text to a synthesis request mid-stream.

    The agent has emitted only un-terminated words and the input iterator is
    still open. flush() must force those words into a synthesis request now; we
    assert the request is made (and frames flow) before the iterator ever ends.
    """
    http = _RecordedHttp()
    tts = _make(http)
    text = _OpenEndedText("partial unfinished words")
    stream = tts.synthesize(text, voice="x")

    drained: list[PcmFrame] = []

    async def _consume() -> None:
        async for frame in stream:
            drained.append(frame)

    consumer = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(text.consumed.wait(), timeout=1.0)
        await stream.flush()

        async def _has_request() -> None:
            while http.request is None:
                await asyncio.sleep(0)

        await asyncio.wait_for(_has_request(), timeout=1.0)
        assert http.request is not None
        assert http.request.text == "partial unfinished words"
        assert not text.release.is_set()  # the input was never ended
    finally:
        await stream.cancel()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_flush_mid_tag_does_not_leak_fragment_on_flash() -> None:
    """A flush() mid-tag on Flash never sends an incomplete tag fragment (ADR-0027).

    Codex review repro: a streamed ``[breath]`` cut after the ``[`` leaves
    ``"Hello [bre"`` in the segmenter; flush() must not hand that to a non-v3 model
    (which would voice "bracket b-r-e"). The dangling tag-opening is stripped, so the
    request text is just ``"Hello"`` (and never contains the fragment).
    """
    http = _RecordedHttp()
    tts = _make(http)  # default Flash v2.5 — a non-tag model
    text = _OpenEndedText("Hello [bre")
    stream = tts.synthesize(text, voice="x")

    drained: list[PcmFrame] = []

    async def _consume() -> None:
        async for frame in stream:
            drained.append(frame)

    consumer = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(text.consumed.wait(), timeout=1.0)
        await stream.flush()

        async def _has_request() -> None:
            while http.request is None:
                await asyncio.sleep(0)

        await asyncio.wait_for(_has_request(), timeout=1.0)
        assert http.request is not None
        assert "[bre" not in http.request.text
        assert "[" not in http.request.text
        assert http.request.text == "Hello"
    finally:
        await stream.cancel()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


# --- errors propagate (rule 37) ---------------------------------------------


@pytest.mark.asyncio
async def test_http_error_propagates_to_the_consumer() -> None:
    """A transport error surfaces from the stream rather than being swallowed."""

    class _BoomHttp:
        def open(
            self, request: ElevenLabsRequest, cancel: HttpCancellation
        ) -> Iterator[bytes]:
            if request.text:  # always true here; keeps the yield reachable
                raise ConnectionError("upstream unavailable")
            yield b""  # pragma: no cover - empty-text branch makes this a generator

    tts = ElevenLabsTTS(api_key=_FAKE_KEY, voice="x", http=_BoomHttp())
    stream = tts.synthesize(_text("Trigger. "), voice="x")
    with pytest.raises(ConnectionError, match="upstream unavailable"):
        await _drain(stream)


# --- barge-in interrupts the blocking urllib transport (the deadlock fix) ----


def test_open_arms_a_barge_in_that_shuts_the_underlying_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel.close() shuts the response's socket, so a parked recv is interrupted.

    Proves the fix mechanism deterministically: the transport arms the barge-in
    with a shutdown of the underlying socket (not just ``response.close``, which
    cannot interrupt a concurrent read), so invoking it makes a recv blocked on
    that socket return at once.
    """
    sock_a, sock_b = socket.socketpair()
    try:
        response = _SocketBackedResponse(sock_a)
        monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: response)
        cancel = HttpCancellation()
        stream = _UrllibHttp().open(_fake_stream_request(), cancel)
        with contextlib.suppress(StopIteration):
            next(stream)  # prime: runs urlopen + arms the barge-in, then EOFs

        result: list[object] = []

        def _reader() -> None:
            try:
                result.append(sock_a.recv(4096))
            except OSError as exc:
                result.append(exc)

        reader = threading.Thread(target=_reader, name="probe-recv", daemon=True)
        reader.start()
        time.sleep(0.1)
        assert reader.is_alive()  # parked in the blocking recv

        cancel.close()  # barge-in: must shut sock_a so the parked recv returns
        reader.join(2.0)
        assert not reader.is_alive(), "shutdown did not interrupt the blocked recv"
        assert result == [b""]  # SHUT_RD => the recv returns EOF
    finally:
        sock_a.close()
        sock_b.close()


def test_abort_blocked_read_without_a_socket_is_a_logged_no_op(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the underlying socket is unreachable, the barge-in degrades to a NO-OP.

    Regression (codex review of the fix): the ``sock is None`` path must NOT fall back
    to a blocking loop-thread ``response.close()`` — that is exactly the whole-event-
    loop freeze the socket-shutdown mechanism exists to avoid (``close()`` cannot
    interrupt a parked read and blocks on the buffer lock it holds). The helper now
    takes ONLY the socket (so the unsafe close is impossible by construction) and
    logs the degraded path; the worker is reaped by the stream join timeout instead.
    """
    with caplog.at_level(logging.WARNING, logger="hermes_voip.tts.elevenlabs"):
        _abort_blocked_read(None)  # must be a no-op: no raise, no block, no close
    assert any(
        "could not reach the response socket" in rec.message for rec in caplog.records
    ), "the degraded (sock-None) barge-in path must log a warning"


def test_urllib_transport_treats_a_cancelled_read_error_as_end_of_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that RAISES after a barge-in is the abort — surfaced as a clean end.

    The socket shutdown that frees a parked TLS read makes ``response.read()`` raise
    ``OSError`` (empirically ``BrokenPipeError``) rather than return ``b''``. Once
    cancelled, ``_UrllibHttp`` ends the aborted stream instead of letting that error
    escape and crash the barge-in.
    """
    response = _ScriptedResponse([b"AB", OSError("connection shut down")])
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: response)
    cancel = HttpCancellation()
    cancel.close()  # a barge-in that fired before the read raised

    chunks = list(_UrllibHttp().open(_fake_stream_request(), cancel))

    assert chunks == [b"AB"]
    assert response.closed


def test_urllib_transport_propagates_a_read_error_without_a_barge_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no barge-in, a read error is a real fault and still propagates (rule 37)."""
    response = _ScriptedResponse([b"AB", OSError("upstream reset")])
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: response)
    cancel = HttpCancellation()

    with pytest.raises(OSError, match="upstream reset"):
        list(_UrllibHttp().open(_fake_stream_request(), cancel))
    assert response.closed


def test_http_byte_stream_protocol_is_satisfied_by_the_fake() -> None:
    """The injected fake structurally matches the HttpByteStream seam."""
    http = _RecordedHttp()
    conforms: HttpByteStream = http  # fails to type-check unless it matches the seam
    assert conforms is http
    assert http.request is None  # exercise a concrete member so the bind is real


# --- dynamic voice_settings (the "flat voice" fix) ---------------------------
#
# Today the request body carries ONLY {text, model_id}; ElevenLabs then applies
# its flat default voice_settings (stability 0.5), which is what the operator
# hears as "flat". These tests pin that the provider now sends a `voice_settings`
# object and that its DEFAULT is the dynamic-but-stable starting point
# (lower stability for broader emotional range), so the audio is more dynamic out
# of the box — while every field stays operator-tunable.


def _body_json(req: ElevenLabsRequest) -> dict[str, object]:
    """Decode the JSON request body the transport would POST."""
    import json  # noqa: PLC0415 - test-local decode helper

    decoded: dict[str, object] = json.loads(req.body().decode("utf-8"))
    return decoded


def test_default_voice_settings_are_dynamic_not_flat() -> None:
    """The shipped default voice_settings favour a dynamic (not flat) delivery.

    ElevenLabs' own default ``stability`` is 0.5 ("can result in a monotonous
    voice"); our default must be LOWER so the voice has broader emotional range —
    this is the single biggest dynamism lever and the fix for the "flat" report.
    The other fields stay at sensible conversational values.
    """
    s = DEFAULT_VOICE_SETTINGS
    assert isinstance(s, ElevenLabsVoiceSettings)
    # Lower than ElevenLabs' flat 0.5 default -> broader emotional range, but not
    # so low it becomes inconsistent (the documented dynamic-but-stable band).
    assert 0.25 <= s.stability < 0.5
    assert 0.0 <= s.similarity_boost <= 1.0
    assert 0.0 <= s.style <= 1.0
    assert s.use_speaker_boost is True


@pytest.mark.asyncio
async def test_request_body_includes_default_voice_settings() -> None:
    """A bare provider sends voice_settings in the body, defaulting to the dynamic set.

    This is the regression that would FAIL under the old flat-default behaviour:
    the body used to be ``{text, model_id}`` with no ``voice_settings`` at all, so
    ElevenLabs applied its monotone 0.5-stability default.
    """
    http = _RecordedHttp()
    tts = _make(http)
    await _drain(tts.synthesize(_text("Be expressive. "), voice="x"))
    req = http.request
    assert req is not None
    body = _body_json(req)
    assert "voice_settings" in body
    vs = body["voice_settings"]
    assert isinstance(vs, dict)
    # The exact ElevenLabs request-body field names (API reference).
    assert vs["stability"] == DEFAULT_VOICE_SETTINGS.stability
    assert vs["similarity_boost"] == DEFAULT_VOICE_SETTINGS.similarity_boost
    assert vs["style"] == DEFAULT_VOICE_SETTINGS.style
    assert vs["use_speaker_boost"] == DEFAULT_VOICE_SETTINGS.use_speaker_boost
    # And the default settings are the dynamic ones (not flat 0.5 stability).
    assert vs["stability"] < 0.5


@pytest.mark.asyncio
async def test_configured_voice_settings_override_the_default() -> None:
    """Operator-supplied voice_settings are sent verbatim (tunable without code)."""
    settings = ElevenLabsVoiceSettings(
        stability=0.2,
        similarity_boost=0.9,
        style=0.4,
        use_speaker_boost=False,
    )
    http = _RecordedHttp()
    tts = ElevenLabsTTS(
        api_key=_FAKE_KEY, voice="x", http=http, voice_settings=settings
    )
    await _drain(tts.synthesize(_text("Tuned voice. "), voice="x"))
    req = http.request
    assert req is not None
    vs = _body_json(req)["voice_settings"]
    assert isinstance(vs, dict)
    assert vs == {
        "stability": 0.2,
        "similarity_boost": 0.9,
        "style": 0.4,
        "use_speaker_boost": False,
    }


@pytest.mark.parametrize("bad", [-0.1, 1.1, float("nan"), float("inf")])
def test_voice_settings_reject_out_of_range_floats(bad: float) -> None:
    """Each float field is constrained to [0.0, 1.0] (fail-fast at construction)."""
    with pytest.raises(ValueError, match="stability"):
        ElevenLabsVoiceSettings(stability=bad)
    with pytest.raises(ValueError, match="similarity_boost"):
        ElevenLabsVoiceSettings(similarity_boost=bad)
    with pytest.raises(ValueError, match="style"):
        ElevenLabsVoiceSettings(style=bad)


def test_voice_settings_payload_has_only_the_api_fields() -> None:
    """The serialised settings carry exactly the four documented API keys."""
    payload = DEFAULT_VOICE_SETTINGS.payload()
    assert set(payload) == {
        "stability",
        "similarity_boost",
        "style",
        "use_speaker_boost",
    }


# --- configurable model id ---------------------------------------------------


@pytest.mark.asyncio
async def test_model_id_is_configurable() -> None:
    """The model id is operator-selectable (body field), still defaulting to Flash."""
    http = _RecordedHttp()
    tts = ElevenLabsTTS(
        api_key=_FAKE_KEY, voice="x", http=http, model_id="eleven_multilingual_v2"
    )
    await _drain(tts.synthesize(_text("Pick a model. "), voice="x"))
    req = http.request
    assert req is not None
    assert req.model_id == "eleven_multilingual_v2"
    assert _body_json(req)["model_id"] == "eleven_multilingual_v2"


@pytest.mark.asyncio
async def test_default_model_id_is_flash_v2_5() -> None:
    """The default model stays Flash v2.5 (real-time streaming; v3 cannot stream)."""
    http = _RecordedHttp()
    tts = _make(http)
    await _drain(tts.synthesize(_text("Default model. "), voice="x"))
    req = http.request
    assert req is not None
    assert req.model_id == _FLASH_V2_5


# --- model-conditional audio tags (ADR-0027) ---------------------------------
#
# ElevenLabs v3 renders inline audio tags ([laughs]/[breath]/...) as performance
# cues; Flash/Turbo/Multilingual speak the bracketed word literally. The provider
# therefore PRESERVES tags only on a v3-family model and STRIPS them otherwise.
# The synthesised request body's ``text`` is what reaches ElevenLabs, so these
# assert on it. Emoji/markdown/URL stripping is the call-loop layer's job, not
# the provider's, so these inputs carry only tags.


def test_v3_model_preserves_audio_tags_capability() -> None:
    """A v3-family model declares it preserves audio tags; Flash does not."""
    v3 = ElevenLabsTTS(
        api_key=_FAKE_KEY, voice="x", http=_RecordedHttp(), model_id=V3_MODEL_ID
    )
    flash = _make(_RecordedHttp())
    assert v3.preserves_audio_tags is True
    assert flash.preserves_audio_tags is False


@pytest.mark.asyncio
async def test_v3_model_keeps_audio_tags_in_request() -> None:
    """On eleven_v3 the audio tags reach the API intact (v3 renders them)."""
    http = _RecordedHttp()
    tts = ElevenLabsTTS(api_key=_FAKE_KEY, voice="x", http=http, model_id=V3_MODEL_ID)
    await _drain(tts.synthesize(_text("Hello [breath] there [laughs]. "), voice="x"))
    req = http.request
    assert req is not None
    assert "[breath]" in req.text
    assert "[laughs]" in req.text


@pytest.mark.asyncio
async def test_flash_model_strips_audio_tags_from_request() -> None:
    """On Flash the audio tags are removed before synthesis (never spoken aloud)."""
    http = _RecordedHttp()
    tts = _make(http)  # default Flash v2.5
    await _drain(tts.synthesize(_text("Hello [breath] there [laughs]. "), voice="x"))
    req = http.request
    assert req is not None
    assert "[breath]" not in req.text
    assert "[laughs]" not in req.text
    # The bracketed words must not survive as literal speakable text.
    assert "breath" not in req.text
    assert "laughs" not in req.text
    assert "Hello" in req.text
    assert "there" in req.text


@pytest.mark.asyncio
async def test_multilingual_model_strips_audio_tags() -> None:
    """A non-v3 streaming model (multilingual v2) also strips tags."""
    http = _RecordedHttp()
    tts = ElevenLabsTTS(
        api_key=_FAKE_KEY, voice="x", http=http, model_id="eleven_multilingual_v2"
    )
    await _drain(tts.synthesize(_text("Okay [sighs] fine. "), voice="x"))
    req = http.request
    assert req is not None
    assert "[sighs]" not in req.text
    assert "sighs" not in req.text


# --- optimize_streaming_latency (opt-in query param) -------------------------


@pytest.mark.asyncio
async def test_streaming_latency_unset_by_default() -> None:
    """No optimize_streaming_latency is sent by default (it is deprecated/opt-in).

    Low first-audio latency already comes from Flash + pcm_8000; the deprecated
    ``optimize_streaming_latency`` is only sent when the operator opts in, so the
    default URL must NOT carry it.
    """
    http = _RecordedHttp()
    tts = _make(http)
    await _drain(tts.synthesize(_text("No latency hint. "), voice="x"))
    req = http.request
    assert req is not None
    assert "optimize_streaming_latency" not in req.url


@pytest.mark.asyncio
async def test_streaming_latency_added_as_query_when_configured() -> None:
    """When set, optimize_streaming_latency rides as a query param (not the body)."""
    http = _RecordedHttp()
    tts = ElevenLabsTTS(
        api_key=_FAKE_KEY, voice="x", http=http, optimize_streaming_latency=1
    )
    await _drain(tts.synthesize(_text("Latency 1. "), voice="x"))
    req = http.request
    assert req is not None
    assert "optimize_streaming_latency=1" in req.url
    # It is a query param, never a body field.
    assert "optimize_streaming_latency" not in _body_json(req)


@pytest.mark.parametrize("bad", [-1, 5, 99])
def test_streaming_latency_out_of_range_rejected(bad: int) -> None:
    """optimize_streaming_latency must be in [0, 4] (ElevenLabs' allowed values)."""
    with pytest.raises(ValueError, match="optimize_streaming_latency"):
        ElevenLabsTTS(
            api_key=_FAKE_KEY,
            voice="x",
            http=_RecordedHttp(),
            optimize_streaming_latency=bad,
        )


def test_voice_settings_not_exposed_in_repr_does_not_leak_key() -> None:
    """Adding settings/model to repr still never leaks the API key."""
    tts = ElevenLabsTTS(
        api_key=_FAKE_KEY,
        voice="x",
        http=_RecordedHttp(),
        voice_settings=ElevenLabsVoiceSettings(stability=0.3),
        model_id="eleven_flash_v2_5",
    )
    assert _FAKE_KEY not in repr(tts)


# --- model_id guard: the live HTTP 400 root cause (ADR-0025) -----------------
#
# The live incident: a G.722 call 400'd during the greeting synth and the call
# died silent. Reproduced against the live API: the request the plugin builds is
# WELL-FORMED (HTTP 200) for ``model_id=eleven_flash_v2_5``, but the shared
# ``HERMES_VOIP_TTS_MODEL`` knob is a model DIRECTORY for sherpa-kokoro and the
# model ID for ElevenLabs — so a Kokoro dir leaking into ``model_id`` is sent
# verbatim and ElevenLabs rejects it with HTTP 400 ``invalid_uid``. The guard
# turns that foot-gun into a fail-fast ValueError at construction (a startup
# ConfigError) instead of a per-call 400 that kills the call. These tests would
# FAIL before the guard (the bad model_id was accepted, then 400'd live).


def test_model_id_that_looks_like_a_path_is_rejected() -> None:
    """A filesystem-path ``model_id`` (the Kokoro-dir foot-gun) fails fast.

    ``HERMES_VOIP_TTS_MODEL`` is a model DIRECTORY for sherpa-kokoro; if it leaks
    into the ElevenLabs ``model_id`` the live API returns HTTP 400 ``invalid_uid``.
    The guard rejects a slash-bearing model id at construction, naming the env var,
    so the misconfiguration surfaces at startup — not as a dead call.
    """
    with pytest.raises(ValueError, match="HERMES_VOIP_TTS_MODEL"):
        ElevenLabsTTS(
            api_key=_FAKE_KEY,
            voice="x",
            http=_RecordedHttp(),
            model_id="/opt/models/kokoro",
        )


def test_model_id_with_backslash_is_rejected() -> None:
    """A backslash path (Windows-style) model id is also rejected as path-shaped."""
    with pytest.raises(ValueError, match="HERMES_VOIP_TTS_MODEL"):
        ElevenLabsTTS(
            api_key=_FAKE_KEY,
            voice="x",
            http=_RecordedHttp(),
            model_id="models\\kokoro",
        )


def test_blank_model_id_is_rejected() -> None:
    """An empty/whitespace ``model_id`` (live: 400 'No ID for voice') fails fast."""
    with pytest.raises(ValueError, match="model_id"):
        ElevenLabsTTS(
            api_key=_FAKE_KEY,
            voice="x",
            http=_RecordedHttp(),
            model_id="   ",
        )


def test_valid_elevenlabs_model_id_is_accepted() -> None:
    """A legitimate ElevenLabs model id (no slash) passes the guard untouched."""
    for good in ("eleven_flash_v2_5", "eleven_multilingual_v2", "eleven_turbo_v2_5"):
        tts = ElevenLabsTTS(
            api_key=_FAKE_KEY, voice="x", http=_RecordedHttp(), model_id=good
        )
        assert tts.model_id == good


@pytest.mark.asyncio
async def test_g722_16k_request_is_well_formed_per_contract() -> None:
    """The request the plugin builds for a 16 kHz G.722 call matches the accepted shape.

    A regression guard for the live 400: with the default (valid) model id and the
    dynamic default voice_settings, the request for a per-call 16 kHz rate carries a
    valid (non-path, non-empty) ``model_id``, ``output_format=pcm_16000``, and the
    exact ElevenLabs body field names — i.e. the shape that returns HTTP 200 live.
    """
    http = _RecordedHttp()
    tts = ElevenLabsTTS(api_key=_FAKE_KEY, voice="River", http=http)
    await _drain(
        tts.synthesize(
            _text("Hello there from Hermes. "), voice="River", sample_rate=16_000
        )
    )
    req = http.request
    assert req is not None
    # model_id is a valid ElevenLabs id — NOT a path, NOT empty (the 400 trigger).
    assert req.model_id == _FLASH_V2_5
    assert "/" not in req.model_id
    assert req.model_id.strip() != ""
    # 16 kHz G.722 -> pcm_16000 in URL and as the request format.
    assert req.output_format == "pcm_16000"
    assert "output_format=pcm_16000" in req.url
    # The body carries exactly the accepted ElevenLabs fields.
    body = _body_json(req)
    assert set(body) == {"text", "model_id", "voice_settings"}
    vs = body["voice_settings"]
    assert isinstance(vs, dict)
    assert set(vs) == {"stability", "similarity_boost", "style", "use_speaker_boost"}
    # Keep the dynamic-voice intent (stability 0.35 etc.) intact through the fix.
    assert vs["stability"] == DEFAULT_VOICE_SETTINGS.stability
