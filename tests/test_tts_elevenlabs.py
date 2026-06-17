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
import threading
from collections.abc import AsyncIterator, Iterator

import pytest

from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.tts.elevenlabs import (
    G711_NARROWBAND_RATE,
    ElevenLabsRequest,
    ElevenLabsTTS,
    HttpByteStream,
    HttpCancellation,
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


def test_http_byte_stream_protocol_is_satisfied_by_the_fake() -> None:
    """The injected fake structurally matches the HttpByteStream seam."""
    http = _RecordedHttp()
    conforms: HttpByteStream = http  # fails to type-check unless it matches the seam
    assert conforms is http
    assert http.request is None  # exercise a concrete member so the bind is real
