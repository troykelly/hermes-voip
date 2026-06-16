"""Deterministic content tests for the bidirectional audio pipeline.

TDD RED-first (AGENTS.md rule 18). These tests prove the audio CONTENT pipeline
carries non-silent signal end-to-end -- no live call needed. Each test targets
one seam:

* TX content: PCM at TTS rate -> send_audio (8kHz resample + G.711 encode) ->
  wire bytes -> decoded back -> non-silent, full-duration, correct packet count.
* Greeting playout: CallLoop plays ALL frames of a multi-frame TTS stream, not
  only the first.
* RX content: 8kHz G.711 inbound -> engine decode -> 8k->16k upsample -> STT
  feed receives non-silent 16kHz PCM of the correct duration.
* Tone frames: generate_tone_frames produces 8kHz, non-silent, correct-length
  frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import struct
from collections.abc import AsyncIterator, Iterator

import pytest

from hermes_voip.media.audio import (
    G711_SAMPLE_RATE,
    decode_ulaw,
    encode_ulaw,
    frame_to_ulaw,
    generate_tone_frames,
    ulaw_to_frame,
)
from hermes_voip.media.call_loop import CallLoop
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.media.vad import VadModel, VoiceActivityDetector
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.providers.tts import TtsStream
from hermes_voip.rtp import RtpPacket
from hermes_voip.stt.resample import FrameUpsampler, pcm16_to_float32

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PTIME_MS = 20
_SAMPLES_PER_8K_FRAME = G711_SAMPLE_RATE * _PTIME_MS // 1000  # 160
_BYTES_PER_8K_FRAME = _SAMPLES_PER_8K_FRAME * 2  # 320 PCM16 bytes

_TTS_RATE = 24_000
_SAMPLES_PER_24K_FRAME = _TTS_RATE * _PTIME_MS // 1000  # 480

# Minimum non-silent RMS: 5% of int16 full-scale (~1638 out of 32767).
_MIN_RMS = 32767 * 0.05


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sine_pcm16(freq_hz: float, rate: int, n_samples: int) -> bytes:
    """Generate n_samples of a pure sine at freq_hz Hz as PCM16-LE mono."""
    samples = [
        int(32767 * 0.5 * math.sin(2 * math.pi * freq_hz * i / rate))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


def _rms(pcm16: bytes) -> float:
    """Compute the RMS amplitude of PCM16-LE mono bytes."""
    n = len(pcm16) // 2
    if n == 0:
        return 0.0
    vals = struct.unpack(f"<{n}h", pcm16)
    return math.sqrt(sum(v * v for v in vals) / n)


async def _no_sleep(_secs: float) -> None:
    """Pacing sleep replaced with a no-op for deterministic tests."""


class _SendRecorder(asyncio.DatagramTransport):
    """Records every sendto call (datagrams + dest) for content inspection."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(  # type: ignore[override]  # narrower addr type; engine always passes (host, port)
        self, data: bytes, addr: tuple[str, int] | None = None
    ) -> None:
        assert addr is not None
        self.sent.append((bytes(data), addr))

    def close(self) -> None:
        pass  # no socket -- the real one is closed by engine.stop()

    def is_closing(self) -> bool:
        return False


@contextlib.contextmanager
def _capture_sends(engine: RtpMediaTransport) -> Iterator[_SendRecorder]:
    """Intercept outbound sends; restore real transport afterwards (no leak)."""
    real = engine._transport
    recorder = _SendRecorder()
    engine._transport = recorder
    try:
        yield recorder
    finally:
        engine._transport = real


# ---------------------------------------------------------------------------
# Fake collaborators for the CallLoop playout test
# ---------------------------------------------------------------------------


class _FakeTtsStream:
    """TtsStream that yields n_frames of 8 kHz sine, then stops."""

    def __init__(self, n_frames: int) -> None:
        self._n_frames = n_frames
        self._count = 0
        self._cancelled = False
        pcm = _sine_pcm16(440.0, G711_SAMPLE_RATE, _SAMPLES_PER_8K_FRAME)
        self._frame = PcmFrame(
            samples=pcm, sample_rate=G711_SAMPLE_RATE, monotonic_ts_ns=0
        )

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        if self._cancelled or self._count >= self._n_frames:
            raise StopAsyncIteration
        self._count += 1
        return self._frame

    async def flush(self) -> None:
        pass

    async def cancel(self) -> None:
        self._cancelled = True

    async def aclose(self) -> None:
        self._cancelled = True


class _NeverIter:
    """AsyncIterator[PcmFrame] that blocks until cancelled and yields nothing.

    Used by _CountingTransport.inbound_audio() to simulate an open call whose
    inbound stream is never closed, so the CallLoop pump blocks indefinitely
    (until the TaskGroup is cancelled externally). Avoids the ``yield`` after
    ``return`` pattern that mypy flags as unreachable.
    """

    def __aiter__(self) -> _NeverIter:
        return self

    async def __anext__(self) -> PcmFrame:
        await asyncio.Event().wait()  # blocks until cancelled
        raise StopAsyncIteration


class _CountingTransport:
    """MediaTransport that counts send_audio calls; inbound never yields."""

    def __init__(self) -> None:
        self.inbound_sample_rate: int = G711_SAMPLE_RATE
        self.send_count: int = 0

    async def connect(self) -> bool:
        return True

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        return _NeverIter()

    async def send_audio(self, frame: PcmFrame) -> None:
        self.send_count += 1

    async def disconnect(self) -> None:
        pass


class _EmptyTranscriptIter:
    """AsyncIterator[Transcript] that yields nothing.

    Drains audio but emits no transcripts. Used by _DrainASR to satisfy the
    StreamingASR.stream return type while
    ensuring the audio is consumed (to avoid blocking the pump's audio_q).
    Avoids the ``yield`` after ``return`` anti-pattern that mypy flags as
    unreachable.
    """

    def __init__(self, audio: AsyncIterator[PcmFrame]) -> None:
        self._audio = audio
        self._drained = False

    def __aiter__(self) -> _EmptyTranscriptIter:
        return self

    async def __anext__(self) -> Transcript:
        if not self._drained:
            async for _ in self._audio:
                pass
            self._drained = True
        raise StopAsyncIteration


class _DrainASR:
    """ASR that drains audio without emitting transcripts."""

    input_sample_rate: int = 16_000

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        return _EmptyTranscriptIter(audio)


class _FixedStreamTTS:
    """TTS that always returns the provided _FakeTtsStream."""

    def __init__(self, stream: _FakeTtsStream) -> None:
        self._stream = stream

    @property
    def output_sample_rate(self) -> int:
        return G711_SAMPLE_RATE

    def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
        return self._stream


class _AlwaysAllowGuard:
    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            normalized_text=text,
            reasons=("allow",),
            degraded=False,
            score=0.0,
        )


class _AlwaysSilenceVadModel:
    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        return 0.0  # never detects speech -> no barge-in interference


# ---------------------------------------------------------------------------
# TX content test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tx_content_non_silent_rms_full_duration() -> None:
    """send_audio carries non-silent signal across the full TTS-rate->G.711 path.

    Drive: 1 s of 1 kHz sine at 24 kHz (50 frames x 480 samples), run through
    the REAL send_audio path (24k resample -> 8k -> G.711 encode -> wire).

    Assert:
    (a) packet count ~= 50 (catches a STALL -- only 1-2 packets sent);
    (b) each decoded G.711 payload has RMS > threshold (catches silence);
    (c) total decoded bytes ~= 1 s x 8000 Hz x 2 bytes (correct duration).
    """
    n_frames = 50  # 1 second at 50 pps
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        sleep=_no_sleep,
    )
    await engine.connect()

    pcm_1s = _sine_pcm16(1000.0, _TTS_RATE, n_frames * _SAMPLES_PER_24K_FRAME)

    with _capture_sends(engine) as recorder:
        for i in range(n_frames):
            lo = i * _SAMPLES_PER_24K_FRAME * 2
            hi = (i + 1) * _SAMPLES_PER_24K_FRAME * 2
            chunk = pcm_1s[lo:hi]
            frame = PcmFrame(samples=chunk, sample_rate=_TTS_RATE, monotonic_ts_ns=0)
            await engine.send_audio(frame)

    await engine.stop()

    # (a) Packet count -- a stall would produce far fewer packets than n_frames.
    actual_pkts = len(recorder.sent)
    assert actual_pkts >= n_frames - 2, (
        f"TX stall detected: expected ~{n_frames} packets, got {actual_pkts}"
    )

    # (b) + (c) Decode every G.711 payload and check RMS and total duration.
    total_pcm = b""
    silent_pkts = 0
    for wire, _dest in recorder.sent:
        pkt = RtpPacket.parse(wire)
        decoded = decode_ulaw(pkt.payload)
        total_pcm += decoded
        if _rms(decoded) < _MIN_RMS:
            silent_pkts += 1

    assert silent_pkts == 0, (
        f"TX produces silence: {silent_pkts}/{actual_pkts} packets below RMS "
        f"threshold {_MIN_RMS:.0f} (decoded from G.711)"
    )

    expected_bytes = n_frames * _SAMPLES_PER_8K_FRAME * 2
    assert abs(len(total_pcm) - expected_bytes) <= _BYTES_PER_8K_FRAME * 2, (
        f"TX duration wrong: expected ~{expected_bytes} PCM bytes, got {len(total_pcm)}"
    )


# ---------------------------------------------------------------------------
# Greeting playout test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_greeting_play_sends_all_frames_not_just_first() -> None:
    """CallLoop._play drains ALL frames from a multi-frame TTS stream.

    A bug where _play exits after the first frame (e.g. early return, barge-in
    triggered by a race, lock contention) would cause only 1 send_audio call
    instead of all n_frames.  This test drives _play directly with a fake
    TtsStream carrying known PCM frames.
    """
    n_frames = 20  # 400 ms of audio -- should all arrive
    vad_model: VadModel = _AlwaysSilenceVadModel()
    vad = VoiceActivityDetector(model=vad_model, sample_rate_hz=G711_SAMPLE_RATE)
    endpointer = Endpointer(silence_ms=500, sample_rate_hz=G711_SAMPLE_RATE)
    transport = _CountingTransport()
    fake_tts_stream = _FakeTtsStream(n_frames)

    async def _deliver(_text: str) -> None:
        pass

    call_loop = CallLoop(
        transport=transport,
        asr=_DrainASR(),
        tts=_FixedStreamTTS(fake_tts_stream),
        guard=_AlwaysAllowGuard(),
        vad=vad,
        endpointer=endpointer,
        guard_state=GuardSessionState("test-call"),
        deliver_turn=_deliver,
        voice="",
        call_id="test-call",
        greeting="",  # no greeting -- we test _play directly below
    )

    # Drive _play with the fake stream directly (same path as _play_greeting).
    await call_loop._play(fake_tts_stream, on_first_frame=None)

    assert transport.send_count == n_frames, (
        f"_play stall: expected {n_frames} send_audio calls for "
        f"{n_frames} TTS frames, got {transport.send_count}"
    )


# ---------------------------------------------------------------------------
# RX content test
# ---------------------------------------------------------------------------


def test_rx_content_g711_to_16k_pcm_non_silent() -> None:
    """Inbound G.711 -> decode -> 8k->16k upsample produces non-silent 16 kHz PCM.

    Simulates the RX path the STT _feed sees: take a known non-silent 8 kHz
    G.711 payload (as the gateway sends), decode to PCM via ulaw_to_frame,
    upsample 8k->16k via FrameUpsampler, then assert the 16kHz PCM is non-silent
    and the correct duration.

    Catches: (a) silent G.711 payloads despite genuine inbound speech, (b)
    upsample destroying amplitude, (c) wrong sample count.
    """
    n_frames = 50  # 1 second at 20ms/frame
    pcm_8k = _sine_pcm16(1000.0, G711_SAMPLE_RATE, n_frames * _SAMPLES_PER_8K_FRAME)
    ulaw_payload = encode_ulaw(pcm_8k)

    upsampler = FrameUpsampler()
    pcm_16k_total = b""

    for i in range(n_frames):
        lo = i * _SAMPLES_PER_8K_FRAME
        hi = (i + 1) * _SAMPLES_PER_8K_FRAME
        chunk = ulaw_payload[lo:hi]
        frame_8k = ulaw_to_frame(chunk, monotonic_ts_ns=0)
        frame_16k = upsampler.upsample(frame_8k)
        pcm_16k_total += frame_16k.samples

    # Duration: 1 s x 16000 Hz x 2 bytes/sample = 32000 bytes.
    expected_bytes = n_frames * _SAMPLES_PER_8K_FRAME * 2 * 2  # doubled for 16k
    assert abs(len(pcm_16k_total) - expected_bytes) <= 64, (
        f"RX upsample duration wrong: expected ~{expected_bytes} bytes, "
        f"got {len(pcm_16k_total)}"
    )

    rms_16k = _rms(pcm_16k_total)
    assert rms_16k >= _MIN_RMS, (
        f"RX path produces silence after G.711-decode + 8k->16k upsample: "
        f"RMS={rms_16k:.1f} < threshold {_MIN_RMS:.0f}"
    )

    # Verify the float32 the STT model receives is non-zero.
    float32_arr = pcm16_to_float32(pcm_16k_total)
    raw_bytes = float32_arr.tobytes()
    assert any(b != 0 for b in raw_bytes), (
        "pcm16_to_float32 produced all-zero float32 from non-silent PCM: "
        "STT would receive silence"
    )


# ---------------------------------------------------------------------------
# Tone frames tests
# ---------------------------------------------------------------------------


def test_generate_tone_frames_properties() -> None:
    """generate_tone_frames produces 8 kHz, non-silent, correctly-sized frames.

    The tone diagnostic path (HERMES_VOIP_TEST_TONE) generates frames at
    G711_SAMPLE_RATE directly, bypassing TTS + resample, so they go straight into
    send_audio -> G.711 encode -> RTP with no conversion.
    """
    duration_secs = 1.0
    freq_hz = 440.0
    frames = list(generate_tone_frames(duration_secs=duration_secs, freq_hz=freq_hz))

    expected_n = int(duration_secs * 1000 / _PTIME_MS)  # 50 frames for 1 s
    assert len(frames) == expected_n, (
        f"generate_tone_frames: expected {expected_n} frames for "
        f"{duration_secs}s at {_PTIME_MS}ms/frame, got {len(frames)}"
    )

    for i, frame in enumerate(frames):
        assert frame.sample_rate == G711_SAMPLE_RATE, (
            f"frame {i}: sample_rate={frame.sample_rate}, expected {G711_SAMPLE_RATE}"
        )
        assert len(frame.samples) == _BYTES_PER_8K_FRAME, (
            f"frame {i}: {len(frame.samples)} bytes, expected {_BYTES_PER_8K_FRAME}"
        )
        rms = _rms(frame.samples)
        assert rms >= _MIN_RMS, (
            f"frame {i}: RMS={rms:.1f} below threshold {_MIN_RMS:.0f} (silent)"
        )


def test_generate_tone_frames_g711_roundtrip_non_silent() -> None:
    """Tone frames survive G.711 encode/decode with non-silent output.

    Since tone frames are already 8 kHz, send_audio takes the fast path (no
    resample); this test proves the G.711 encode/decode preserves the signal.
    """
    frames = list(generate_tone_frames(duration_secs=0.2, freq_hz=1000.0))
    for frame in frames:
        ulaw = frame_to_ulaw(frame)
        decoded = ulaw_to_frame(ulaw, monotonic_ts_ns=0)
        rms = _rms(decoded.samples)
        assert rms >= _MIN_RMS, (
            f"Tone frame G.711 round-trip produces silence: RMS={rms:.1f}"
        )
