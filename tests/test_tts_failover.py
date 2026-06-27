"""Automatic TTS failover (cloud primary → self-host fallback) tests (ADR-0025).

The live incident: the ElevenLabs streaming request 400'd during the greeting
synth, the error rose out of the call's ``TaskGroup``, and the call died with NO
audio. ``FailoverTTS`` wraps a primary ``StreamingTTS`` with a lazily-built
fallback so a primary synthesis failure (HTTP 400, timeout, connection error, ANY
exception from the primary stream) recovers by synthesising via the fallback — the
call still gets audio, never silence.

These tests pin the contract with fakes only (no network, no ml weights):

* a primary failure makes the fallback produce frames (no exception escapes);
* the happy path NEVER constructs or invokes the fallback (zero added latency);
* after the first failure the wrapper LATCHES to the fallback for the rest of the
  call (no per-utterance flapping; no second primary attempt);
* ``reset_failover()`` clears the latch so a fresh call retries the primary;
* the per-call negotiated wire ``sample_rate`` is forwarded to whichever provider
  actually synthesises (codec-gated output, ADR-0022).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Iterator

import pytest

from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.tts.failover import (
    FailoverTTS,
    SupportsCallReset,
    reset_failover_if_supported,
)

_G711_RATE = 8_000
_G722_RATE = 16_000


async def _text(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


def _frame(rate: int, payload: bytes = b"\x01\x00\x02\x00") -> PcmFrame:
    return PcmFrame(samples=payload, sample_rate=rate, monotonic_ts_ns=0)


class _ListStream:
    """A minimal ``TtsStream`` that yields a fixed list of frames.

    Records the text it was asked to synthesise (so a test can prove the *same*
    utterance text reached the fallback) and whether it was cancelled/closed.
    """

    def __init__(self, frames: list[PcmFrame], *, spoken: list[str]) -> None:
        self._frames = list(frames)
        self._spoken = spoken
        self.cancelled = False
        self.closed = False

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def flush(self) -> None: ...

    async def cancel(self) -> None:
        self.cancelled = True

    async def aclose(self) -> None:
        self.closed = True


class _RaisingStream:
    """A ``TtsStream`` that raises on the first frame pull (models the 400 case).

    ElevenLabs' ``urlopen`` raises before any audio byte, so the failure surfaces
    on the consumer's first ``__anext__``. ``raise_after`` lets a test instead
    raise AFTER emitting some frames (the mid-utterance case).
    """

    def __init__(
        self,
        exc: BaseException,
        *,
        frames_before: list[PcmFrame] | None = None,
    ) -> None:
        self._exc = exc
        self._frames = list(frames_before or [])
        self.cancelled = False
        self.closed = False

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        if self._frames:
            return self._frames.pop(0)
        raise self._exc

    async def flush(self) -> None: ...

    async def cancel(self) -> None:
        self.cancelled = True

    async def aclose(self) -> None:
        self.closed = True


class _FakeTTS:
    """A fake ``StreamingTTS`` whose ``synthesize`` is fully scriptable.

    ``make_stream`` is called with the buffered text and the per-call sample rate,
    so a test controls whether this provider yields frames or raises, and can
    assert the exact text + rate it received.
    """

    def __init__(
        self,
        make_stream: Callable[[str, int | None], TtsStream],
        *,
        default_rate: int = _G711_RATE,
    ) -> None:
        self._make_stream = make_stream
        self._default_rate = default_rate
        self.calls: list[tuple[str, int | None]] = []
        self.spoken: list[str] = []

    @property
    def output_sample_rate(self) -> int:
        return self._default_rate

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        # Eagerly drain the (single-use) text iterator so the recorded utterance is
        # exactly what the wrapper passed — mirrors a real provider segmenting it.
        async def _collect() -> str:
            parts = [chunk async for chunk in text]
            return "".join(parts)

        # synthesize() is a SYNC factory; collect lazily inside the returned stream
        # by wrapping make_stream in a stream that first resolves the text.
        return _DeferredStream(_collect, voice, sample_rate, self)


class _SyncRaisingTTS:
    """A ``StreamingTTS`` whose ``synthesize`` raises SYNCHRONOUSLY (not on iterate).

    Models a real provider that validates eagerly in the synchronous factory — e.g.
    ``ElevenLabsTTS.synthesize`` raises ``ValueError`` for an unsupported per-call
    sample rate before any stream is iterated. The failover must recover from these
    too (the spec is "ANY primary failure"), not only from failures raised while
    iterating frames.
    """

    def __init__(self, exc: BaseException, *, default_rate: int = _G711_RATE) -> None:
        self._exc = exc
        self._default_rate = default_rate

    @property
    def output_sample_rate(self) -> int:
        return self._default_rate

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        raise self._exc


class _DeferredStream:
    """Resolves the (async) text on first pull, then delegates to the scripted stream.

    A real provider's ``synthesize`` is synchronous and the text is consumed during
    iteration; this fake mirrors that so the wrapper's text-buffering contract is
    exercised honestly.
    """

    def __init__(
        self,
        collect: Callable[[], object],
        voice: str,
        sample_rate: int | None,
        owner: _FakeTTS,
    ) -> None:
        self._collect = collect
        self._sample_rate = sample_rate
        self._owner = owner
        self._inner: TtsStream | None = None

    async def _ensure(self) -> TtsStream:
        if self._inner is None:
            text = await self._collect()  # type: ignore[misc]
            assert isinstance(text, str)
            self._owner.calls.append((text, self._sample_rate))
            self._inner = self._owner._make_stream(text, self._sample_rate)
        return self._inner

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        inner = await self._ensure()
        return await inner.__anext__()

    async def flush(self) -> None:
        inner = await self._ensure()
        await inner.flush()

    async def cancel(self) -> None:
        if self._inner is not None:
            await self._inner.cancel()

    async def aclose(self) -> None:
        if self._inner is not None:
            await self._inner.aclose()


async def _drain(stream: TtsStream) -> list[PcmFrame]:
    return [frame async for frame in stream]


# ---------------------------------------------------------------------------
# (0) Model-conditional audio tags across the failover seam (ADR-0027).
# ---------------------------------------------------------------------------
#
# The sharp edge: the primary is v3 (PRESERVES audio tags), but the Kokoro
# fallback CANNOT render them. When failover re-synthesises the utterance on the
# fallback, the tags must be STRIPPED — matching the provider that actually
# synthesises, not the primary's capability. These wire REAL providers (a v3
# ElevenLabs primary whose HTTP transport fails, a real Kokoro fallback with a
# recording fake backend) so the production composition is what is verified.


class _AlwaysRaiseHttp:
    """An ElevenLabs HTTP transport that raises before any byte (the 400 case).

    ``open`` raises on invocation; ``ElevenLabsTTS.synthesize`` defers the call to a
    worker thread (``stream_from_thread``), so the failure surfaces on the consumer's
    first ``__anext__`` — exactly when the real ``urlopen`` 400 surfaces, triggering
    failover before any audio frame.
    """

    def open(self, request: object, cancel: object) -> Iterator[bytes]:
        raise ConnectionError("elevenlabs upstream down")


class _RecordingSynth:
    """A Kokoro ``Synthesizer`` backend that records the text it synthesises."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def synthesize(self, text: str, stop: Callable[[], bool]) -> Iterator[bytes]:
        self.requested.append(text)
        yield b"\x01\x00\x02\x00"


@pytest.mark.asyncio
async def test_failover_to_kokoro_strips_audio_tags_v3_primary() -> None:
    """v3 primary fails → Kokoro fallback synthesises the utterance WITHOUT tags.

    The primary (eleven_v3) preserves tags, but the moment failover hands the
    replayed utterance to the Kokoro fallback, Kokoro must strip them — otherwise
    Kokoro would speak the literal word "breath". This proves the per-utterance
    sanitisation matches the provider actually synthesising.
    """
    from hermes_voip.tts.elevenlabs import V3_MODEL_ID, ElevenLabsTTS  # noqa: PLC0415
    from hermes_voip.tts.sherpa_kokoro import SherpaKokoroTTS  # noqa: PLC0415

    primary = ElevenLabsTTS(
        api_key="sk_fake_for_tests",
        voice="v",
        http=_AlwaysRaiseHttp(),
        model_id=V3_MODEL_ID,
    )
    backend = _RecordingSynth()
    fallback = SherpaKokoroTTS(synthesizer_factory=lambda: backend)
    tts = FailoverTTS(primary=primary, fallback_factory=lambda: fallback)

    frames = await _drain(
        tts.synthesize(_text("Hello [breath] there [laughs]. "), voice="v")
    )

    assert frames, "the fallback must still produce audio"
    synthesised = " ".join(backend.requested)
    assert synthesised, "the Kokoro fallback must have synthesised the utterance"
    assert "[breath]" not in synthesised
    assert "[laughs]" not in synthesised
    assert "breath" not in synthesised
    assert "laughs" not in synthesised
    assert "Hello" in synthesised
    assert "there" in synthesised


# ---------------------------------------------------------------------------
# (1) A primary failure recovers via the fallback — the call gets audio.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_failure_falls_back_to_fallback_audio() -> None:
    """A primary 400 (raises before any frame) yields the FALLBACK's frames.

    The incident: ElevenLabs 400'd on the greeting, the error escaped, the call
    died silent. With failover, the greeting must still produce audio — from the
    fallback — and no exception escapes the stream.
    """
    primary_spoken: list[str] = []
    fallback_spoken: list[str] = []

    def _primary_stream(text: str, rate: int | None) -> TtsStream:
        primary_spoken.append(text)
        return _RaisingStream(_http_400())

    def _fallback_stream(text: str, rate: int | None) -> TtsStream:
        fallback_spoken.append(text)
        return _ListStream([_frame(rate or _G711_RATE)] * 3, spoken=fallback_spoken)

    primary = _FakeTTS(_primary_stream)
    fallback = _FakeTTS(_fallback_stream)
    tts = FailoverTTS(primary=primary, fallback_factory=lambda: fallback)

    frames = await _drain(tts.synthesize(_text("Hello there. "), voice="v"))

    assert frames, "the call must still get audio from the fallback"
    # The SAME greeting text reached the fallback (replayed from the buffer).
    assert fallback.calls
    assert fallback.calls[0][0] == "Hello there. "


@pytest.mark.asyncio
async def test_primary_failure_does_not_escape_the_stream() -> None:
    """The primary's exception is recovered, never propagated out of the stream."""

    def _primary_stream(text: str, rate: int | None) -> TtsStream:
        return _RaisingStream(ConnectionError("upstream down"))

    def _fallback_stream(text: str, rate: int | None) -> TtsStream:
        return _ListStream([_frame(rate or _G711_RATE)], spoken=[])

    tts = FailoverTTS(
        primary=_FakeTTS(_primary_stream),
        fallback_factory=lambda: _FakeTTS(_fallback_stream),
    )
    # Must NOT raise ConnectionError — recovered into fallback audio.
    frames = await _drain(tts.synthesize(_text("Recover me. "), voice="v"))
    assert len(frames) == 1


@pytest.mark.asyncio
async def test_synchronous_primary_synthesize_failure_falls_back() -> None:
    """A primary whose synthesize() raises SYNCHRONOUSLY still falls back to audio.

    Real providers validate eagerly in the synchronous factory (e.g. ElevenLabs
    rejects an unsupported per-call sample rate in synthesize()). That failure must
    also trigger failover — the spec is "ANY primary failure", not only failures
    raised while iterating frames. Before the fix this exception escaped the wrapper.
    """
    fallback_calls: list[str] = []

    def _fallback_stream(text: str, rate: int | None) -> TtsStream:
        fallback_calls.append(text)
        return _ListStream([_frame(rate or _G711_RATE)], spoken=[])

    primary = _SyncRaisingTTS(ValueError("no native PCM output for 12345 Hz"))
    fallback = _FakeTTS(_fallback_stream)
    tts = FailoverTTS(primary=primary, fallback_factory=lambda: fallback)

    # Must NOT raise the ValueError — recovered into fallback audio.
    frames = await _drain(tts.synthesize(_text("Sync fail. "), voice="v"))
    assert len(frames) == 1
    assert fallback_calls == ["Sync fail. "]
    # And the wrapper latched (a sync failure is still a failure for this call).
    assert tts._latched is True


@pytest.mark.asyncio
async def test_primary_failure_after_audio_truncates_and_latches() -> None:
    """A mid-utterance primary failure (after frames) latches without double-speak.

    If the primary emits some audio then fails, that audio is already on the wire.
    The wrapper must NOT replay the utterance (which would double-speak the start) —
    it ends the utterance (no exception escapes), latches, and a SUBSEQUENT utterance
    uses the fallback. So this utterance yields ONLY the primary's emitted frames and
    the fallback is never invoked for it.
    """
    primary_emitted = _frame(_G711_RATE, b"\x10\x00")
    fallback_calls: list[str] = []

    def _primary_stream(text: str, rate: int | None) -> TtsStream:
        # Emit one frame, then fail (models a dropped connection mid-stream).
        return _RaisingStream(_http_400(), frames_before=[primary_emitted])

    def _fallback_stream(text: str, rate: int | None) -> TtsStream:
        fallback_calls.append(text)
        return _ListStream([_frame(rate or _G711_RATE)], spoken=[])

    primary = _FakeTTS(_primary_stream)
    fallback = _FakeTTS(_fallback_stream)
    tts = FailoverTTS(primary=primary, fallback_factory=lambda: fallback)

    # Utterance 1: one primary frame, then failure -> truncate (no replay, no raise).
    frames = await _drain(tts.synthesize(_text("Partly spoken. "), voice="v"))
    assert frames == [primary_emitted]
    assert fallback_calls == [], "must NOT replay a partly-spoken utterance"

    # Utterance 2: latched -> the fallback now handles it.
    await _drain(tts.synthesize(_text("Next line. "), voice="v"))
    assert fallback_calls == ["Next line. "]


# ---------------------------------------------------------------------------
# (2) The happy path NEVER touches the fallback (no latency, no construction).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_never_constructs_or_invokes_fallback() -> None:
    """When the primary succeeds, the fallback factory is never even called.

    Zero added latency on the happy path: the fallback (a Kokoro model load) must
    not be constructed unless the primary fails.
    """
    factory_calls = 0

    def _primary_stream(text: str, rate: int | None) -> TtsStream:
        return _ListStream([_frame(rate or _G711_RATE)] * 2, spoken=[])

    def _fallback_factory() -> StreamingTTS:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeTTS(lambda t, r: _ListStream([], spoken=[]))

    tts = FailoverTTS(
        primary=_FakeTTS(_primary_stream), fallback_factory=_fallback_factory
    )
    frames = await _drain(tts.synthesize(_text("All good. "), voice="v"))
    assert len(frames) == 2
    assert factory_calls == 0, "the fallback must NOT be built on the happy path"


# ---------------------------------------------------------------------------
# (3) Latching: after one failure, later utterances skip the primary entirely.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latches_to_fallback_after_first_failure() -> None:
    """The second utterance after a failure uses the fallback WITHOUT retrying primary.

    Avoids mid-call voice flapping between the cloud and local synthesisers: once
    the primary has failed on this call, every later utterance goes straight to the
    fallback.
    """
    primary_calls: list[str] = []
    fallback_calls: list[str] = []

    def _primary_stream(text: str, rate: int | None) -> TtsStream:
        primary_calls.append(text)
        return _RaisingStream(_http_400())

    def _fallback_stream(text: str, rate: int | None) -> TtsStream:
        fallback_calls.append(text)
        return _ListStream([_frame(rate or _G711_RATE)], spoken=[])

    primary = _FakeTTS(_primary_stream)
    fallback = _FakeTTS(_fallback_stream)
    tts = FailoverTTS(primary=primary, fallback_factory=lambda: fallback)

    # Utterance 1: primary fails, fallback recovers.
    await _drain(tts.synthesize(_text("First. "), voice="v"))
    # Utterance 2: must go straight to the fallback (latched), no new primary call.
    await _drain(tts.synthesize(_text("Second. "), voice="v"))

    assert primary_calls == ["First. "], "primary must be tried ONCE then latched off"
    assert "First. " in fallback_calls
    assert "Second. " in fallback_calls
    # The fallback provider was built exactly once (cached), not per utterance.
    # (Asserted indirectly: both utterances hit the same `fallback` instance.)


@pytest.mark.asyncio
async def test_fallback_provider_is_built_once_and_cached() -> None:
    """The lazy fallback factory is invoked at most once across the call (cached)."""
    factory_calls = 0

    def _primary_stream(text: str, rate: int | None) -> TtsStream:
        return _RaisingStream(_http_400())

    def _fallback_factory() -> StreamingTTS:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeTTS(lambda t, r: _ListStream([_frame(_G711_RATE)], spoken=[]))

    tts = FailoverTTS(
        primary=_FakeTTS(_primary_stream), fallback_factory=_fallback_factory
    )
    await _drain(tts.synthesize(_text("One. "), voice="v"))
    await _drain(tts.synthesize(_text("Two. "), voice="v"))
    assert factory_calls == 1, "fallback built once, then reused"


# ---------------------------------------------------------------------------
# (4) reset_failover() un-latches so a fresh call retries the primary.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_failover_retries_primary_on_a_fresh_call() -> None:
    """After ``reset_failover()`` the primary is attempted again (new call)."""
    primary_calls: list[str] = []

    def _primary_stream(text: str, rate: int | None) -> TtsStream:
        primary_calls.append(text)
        # Fail the first call's utterance, succeed the second call's.
        if len(primary_calls) == 1:
            return _RaisingStream(_http_400())
        return _ListStream([_frame(rate or _G711_RATE)], spoken=[])

    def _fallback_stream(text: str, rate: int | None) -> TtsStream:
        return _ListStream([_frame(rate or _G711_RATE)], spoken=[])

    tts = FailoverTTS(
        primary=_FakeTTS(_primary_stream),
        fallback_factory=lambda: _FakeTTS(_fallback_stream),
    )
    # Call A: primary fails -> latched to fallback.
    await _drain(tts.synthesize(_text("Call A. "), voice="v"))
    # New call: reset the latch.
    tts.reset_failover()
    # Call B: primary must be attempted again.
    await _drain(tts.synthesize(_text("Call B. "), voice="v"))
    assert primary_calls == ["Call A. ", "Call B. "]


def test_failover_tts_supports_call_reset_protocol() -> None:
    """FailoverTTS structurally satisfies SupportsCallReset (typed reset hook)."""
    tts = FailoverTTS(
        primary=_FakeTTS(lambda t, r: _ListStream([], spoken=[])),
        fallback_factory=lambda: _FakeTTS(lambda t, r: _ListStream([], spoken=[])),
    )
    assert isinstance(tts, SupportsCallReset)


def test_reset_failover_if_supported_is_a_noop_for_plain_providers() -> None:
    """The reset helper does nothing for a provider without reset_failover()."""
    plain = _FakeTTS(lambda t, r: _ListStream([], spoken=[]))
    # Must not raise even though _FakeTTS has no reset_failover().
    assert not isinstance(plain, SupportsCallReset)
    reset_failover_if_supported(plain)


def test_reset_failover_if_supported_calls_the_hook() -> None:
    """The reset helper invokes reset_failover() on a supporting provider."""
    tts = FailoverTTS(
        primary=_FakeTTS(lambda t, r: _ListStream([], spoken=[])),
        fallback_factory=lambda: _FakeTTS(lambda t, r: _ListStream([], spoken=[])),
    )
    # Latch it, then reset via the helper, then prove the latch cleared by a retry.
    tts._latched = True  # set the latch directly to observe the helper clearing it
    reset_failover_if_supported(tts)
    assert tts._latched is False


# ---------------------------------------------------------------------------
# (5) The per-call wire rate reaches whichever provider synthesises (ADR-0022).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sample_rate_forwarded_to_primary_on_happy_path() -> None:
    """The negotiated wire rate is passed through to the primary (codec-gated)."""

    def _primary_stream(text: str, rate: int | None) -> TtsStream:
        return _ListStream([_frame(rate or _G711_RATE)], spoken=[])

    primary = _FakeTTS(_primary_stream)
    tts = FailoverTTS(
        primary=primary,
        fallback_factory=lambda: _FakeTTS(lambda t, r: _ListStream([], spoken=[])),
    )
    await _drain(tts.synthesize(_text("Wideband. "), voice="v", sample_rate=_G722_RATE))
    assert primary.calls[0][1] == _G722_RATE


@pytest.mark.asyncio
async def test_sample_rate_forwarded_to_fallback_on_failover() -> None:
    """On failover the SAME negotiated wire rate reaches the fallback (G.722 16k)."""

    def _primary_stream(text: str, rate: int | None) -> TtsStream:
        return _RaisingStream(_http_400())

    fallback = _FakeTTS(lambda t, r: _ListStream([_frame(r or _G711_RATE)], spoken=[]))
    tts = FailoverTTS(
        primary=_FakeTTS(_primary_stream), fallback_factory=lambda: fallback
    )
    frames = await _drain(
        tts.synthesize(_text("Wideband fallback. "), voice="v", sample_rate=_G722_RATE)
    )
    assert fallback.calls[0][1] == _G722_RATE
    assert all(f.sample_rate == _G722_RATE for f in frames)


# ---------------------------------------------------------------------------
# (6) FailoverTTS is a StreamingTTS; output_sample_rate mirrors the primary.
# ---------------------------------------------------------------------------


def test_failover_is_a_streaming_tts() -> None:
    """FailoverTTS satisfies the StreamingTTS seam and mirrors the primary's rate."""
    primary = _FakeTTS(lambda t, r: _ListStream([], spoken=[]), default_rate=_G711_RATE)
    tts: StreamingTTS = FailoverTTS(
        primary=primary,
        fallback_factory=lambda: _FakeTTS(lambda t, r: _ListStream([], spoken=[])),
    )
    assert isinstance(tts, StreamingTTS)
    assert tts.output_sample_rate == _G711_RATE


def test_failover_preserves_audio_tags_mirrors_primary() -> None:
    """The wrapper reports the PRIMARY's tag capability (a v3 primary preserves).

    A real call's tag fate is decided per-utterance inside whichever provider
    actually synthesises (the fallback always strips); this property reflects the
    primary so the configured capability is observable on the wrapper too.
    """
    from hermes_voip.tts.elevenlabs import V3_MODEL_ID, ElevenLabsTTS  # noqa: PLC0415

    v3_primary = ElevenLabsTTS(
        api_key="sk_fake_for_tests",
        voice="v",
        http=_AlwaysRaiseHttp(),
        model_id=V3_MODEL_ID,
    )
    tts = FailoverTTS(
        primary=v3_primary,
        fallback_factory=lambda: _FakeTTS(lambda t, r: _ListStream([], spoken=[])),
    )
    assert tts.preserves_audio_tags is True


# ---------------------------------------------------------------------------
# (7) TTS primary failover WARNING logs include structured extra fields.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_failure_before_audio_logs_structured_failover_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pre-audio primary failure logs a WARNING with structured extra fields.

    The failover event includes:
    - event: "tts_primary_failover"
    - primary_emitted_audio: whether any frame was emitted before the failure
    """

    def _primary_stream(text: str, rate: int | None) -> TtsStream:
        return _RaisingStream(_http_400())

    def _fallback_stream(text: str, rate: int | None) -> TtsStream:
        return _ListStream([_frame(rate or _G711_RATE)], spoken=[])

    tts = FailoverTTS(
        primary=_FakeTTS(_primary_stream),
        fallback_factory=lambda: _FakeTTS(_fallback_stream),
    )

    with caplog.at_level(logging.WARNING, logger="hermes_voip.tts.failover"):
        frames = await _drain(tts.synthesize(_text("Hello there. "), voice="v"))

    assert frames, "the fallback must produce frames"
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    assert "TTS primary synthesis failed before any audio" in record.message
    assert record.__dict__.get("event") == "tts_primary_failover"
    assert record.__dict__.get("primary_emitted_audio") is False


@pytest.mark.asyncio
async def test_primary_failure_after_audio_logs_structured_failover_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mid-utterance primary failure logs a WARNING with structured extra fields.

    The failover event includes:
    - event: "tts_primary_failover"
    - primary_emitted_audio: whether audio was emitted before failure (True here)
    """

    def _primary_stream(text: str, rate: int | None) -> TtsStream:
        return _RaisingStream(_http_400(), frames_before=[_frame(rate or _G711_RATE)])

    def _fallback_stream(text: str, rate: int | None) -> TtsStream:
        return _ListStream([_frame(rate or _G711_RATE)], spoken=[])

    tts = FailoverTTS(
        primary=_FakeTTS(_primary_stream),
        fallback_factory=lambda: _FakeTTS(_fallback_stream),
    )

    with caplog.at_level(logging.WARNING, logger="hermes_voip.tts.failover"):
        frames = await _drain(tts.synthesize(_text("Partly spoken. "), voice="v"))

    assert frames, "must yield the emitted frame"
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    assert "TTS primary failed mid-utterance after audio" in record.message
    assert record.__dict__.get("event") == "tts_primary_failover"
    assert record.__dict__.get("primary_emitted_audio") is True


# ---------------------------------------------------------------------------
# Helpers modelling the live error shapes.
# ---------------------------------------------------------------------------


def _http_400() -> Exception:
    """An exception standing in for the ElevenLabs urlopen HTTP 400.

    The real transport raises ``urllib.error.HTTPError``; the wrapper recovers on
    ANY exception, so a representative exception suffices for the contract test.
    """
    import urllib.error  # noqa: PLC0415 - test-local

    return urllib.error.HTTPError(
        url="https://api.elevenlabs.io/v1/text-to-speech/x/stream",
        code=400,
        msg="Bad Request",
        hdrs=None,  # type: ignore[arg-type]  # test fake; headers unused by the wrapper
        fp=None,
    )
