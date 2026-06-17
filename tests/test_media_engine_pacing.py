"""Outbound RTP pacing is steady regardless of per-frame encode cost (jitter fix).

The live G.722 defect: the first wideband call was audible but "ever so slightly
jittery — not clean", while the prior G.711 call was clean. Root cause is the
PACING MODEL, not the framing (320 samples/frame, RTP ts +160, 20 ms sleep are
all already correct and covered by ``test_media_engine_g722``):

``_transmit_frame`` historically did ``sendto`` and THEN ``await sleep(ptime)``,
so the realized inter-packet interval was ``ptime + (next frame's encode time)``.
G.722's pure-Python encoder costs ~1.3 ms per 20 ms frame and that cost VARIES
frame-to-frame, so the wire spacing drifted (~20.9-22.3 ms) instead of a constant
20.00 ms - audible jitter. G.711's audioop encode is ~microseconds, so its spacing
was already steady (why narrowband was clean).

The fix is deadline-based pacing: sleep until the next scheduled send time so the
encode cost (and scheduler jitter up to one ptime) is ABSORBED into the interval
rather than added on top. These tests pin a STEADY interval under a deterministic
fake clock where ``_encode`` consumes a variable, codec-like amount of wall time.

Deterministic: no real network, no wall-clock — an injected ``pace_clock`` models
time and an injected ``sleep`` advances it. Runs in the default (no-extra) gate.
"""

from __future__ import annotations

import math
import struct

import pytest

from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.providers.audio import PcmFrame

_PTIME_MS = 20
_PTIME_S = _PTIME_MS / 1000.0
_G722_SAMPLE_RATE = 16_000
_G711_SAMPLE_RATE = 8_000
# 20 ms at each codec's audio rate.
_G722_SAMPLES_PER_FRAME = (_G722_SAMPLE_RATE * _PTIME_MS) // 1000  # 320
_G711_SAMPLES_PER_FRAME = (_G711_SAMPLE_RATE * _PTIME_MS) // 1000  # 160

# A per-frame "encode cost" profile in milliseconds: deliberately VARIABLE, the
# way a pure-Python G.722 encode actually varies frame-to-frame (branch paths, GC).
# Min 0.9, max 2.3 ms — the realistic spread measured on this codec. A correct
# (deadline) pacer absorbs ALL of it; the old post-send-sleep pacer added it on.
_ENCODE_COST_MS: tuple[float, ...] = (
    1.0, 1.6, 1.1, 2.3, 0.9, 1.8, 1.2, 1.5, 1.0, 2.0, 1.3, 1.1,
)  # fmt: skip


class _PacingHarness:
    """A deterministic time model: a monotonic clock plus a sleep that advances it.

    ``pace_clock`` returns the current modelled time in seconds; ``sleep(secs)``
    advances it by ``secs`` (an ideal timer). ``charge(secs)`` advances it to model
    synchronous CPU work (an encode). ``send_times`` records the modelled clock at
    each ``sendto`` so the test can measure realized inter-packet spacing.
    """

    def __init__(self) -> None:
        self._t = 0.0
        self.send_times: list[float] = []

    def pace_clock(self) -> float:
        return self._t

    async def sleep(self, secs: float) -> None:
        # A real timer never travels backwards; a negative request waits zero.
        if secs > 0:
            self._t += secs

    def charge(self, secs: float) -> None:
        self._t += secs

    def record_send(self) -> None:
        self.send_times.append(self._t)


class _Recorder:
    """A minimal DatagramTransport stand-in that timestamps each ``sendto``."""

    def __init__(self, harness: _PacingHarness) -> None:
        self._harness = harness

    def sendto(self, data: bytes, addr: tuple[str, int] | None = None) -> None:
        assert addr is not None
        self._harness.record_send()

    def close(self) -> None:
        """No-op: owns no socket."""

    def is_closing(self) -> bool:
        return False


def _install(
    engine: RtpMediaTransport, harness: _PacingHarness, *, cost_ms: tuple[float, ...]
) -> None:
    """Swap the engine's transport for a recorder; make ``_encode`` cost wall time.

    Wrapping ``_encode`` so it advances the modelled clock by a per-call cost is
    how we model the codec's variable synchronous encode time deterministically —
    the exact thing that, under the old post-send-sleep pacer, leaked into the
    inter-packet interval. The real encode still runs (the bytes are real); only
    time is modelled.
    """
    harness_recorder = _Recorder(harness)
    engine._transport = harness_recorder  # type: ignore[assignment]  # recorder satisfies sendto/close/is_closing
    real_encode = engine._encode
    counter = {"i": 0}

    def _timed_encode(frame: PcmFrame) -> bytes:
        cost = cost_ms[counter["i"] % len(cost_ms)]
        counter["i"] += 1
        harness.charge(cost / 1000.0)
        return real_encode(frame)

    engine._encode = _timed_encode  # type: ignore[method-assign]  # test seam: model encode wall-time


@pytest.fixture
def harness() -> _PacingHarness:
    return _PacingHarness()


def _g722_frame(n_frames: int) -> PcmFrame:
    """``n_frames`` whole 20 ms wideband frames of a 1 kHz tone (16 kHz PCM16)."""
    n = n_frames * _G722_SAMPLES_PER_FRAME
    samples = [
        int(0.4 * 32767 * math.sin(2 * math.pi * 1000 * i / _G722_SAMPLE_RATE))
        for i in range(n)
    ]
    return PcmFrame(
        samples=struct.pack(f"<{n}h", *samples),
        sample_rate=_G722_SAMPLE_RATE,
        monotonic_ts_ns=0,
    )


def _gaps_ms(send_times: list[float]) -> list[float]:
    return [
        (send_times[i] - send_times[i - 1]) * 1000.0 for i in range(1, len(send_times))
    ]


def _new_engine(
    harness: _PacingHarness, codec: Codec, *, sample_rate: int
) -> RtpMediaTransport:
    del sample_rate  # codec fixes the wire rate; kept for call-site readability
    return RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=codec,
        sleep=harness.sleep,
        pace_clock=harness.pace_clock,
        initial_seq=0,
        initial_ts=0,
    )


@pytest.mark.asyncio
async def test_g722_outbound_pacing_is_steady_under_variable_encode_cost(
    harness: _PacingHarness,
) -> None:
    """G.722: every inter-packet gap is a steady 20.00 ms despite variable encode cost.

    RED on the old post-send-sleep pacer: the gap becomes ``20 ms + encode``, which
    varies 20.9-22.3 ms here (a ~1.4 ms spread) - the audible jitter. GREEN on the
    deadline pacer: the encode cost is absorbed, so every gap is exactly the ptime.
    """
    engine = _new_engine(harness, Codec.G722, sample_rate=_G722_SAMPLE_RATE)
    await engine.connect()
    _install(engine, harness, cost_ms=_ENCODE_COST_MS)
    # 12 whole frames delivered in one TTS-style burst.
    await engine.send_audio(_g722_frame(12))
    await engine.stop()

    gaps = _gaps_ms(harness.send_times)
    assert len(gaps) >= 10, f"expected >=10 inter-packet gaps, got {len(gaps)}"
    # Every gap is the ptime to well under a millisecond — no per-frame drift.
    for i, gap in enumerate(gaps):
        assert gap == pytest.approx(_PTIME_MS, abs=0.05), (
            f"gap {i} = {gap:.3f} ms, expected a steady {_PTIME_MS} ms; the spread "
            f"{max(gaps) - min(gaps):.3f} ms is the G.722 pacing jitter (the old "
            f"post-send sleep added the variable encode time onto each interval)"
        )


@pytest.mark.asyncio
async def test_g722_pacing_spread_is_negligible(harness: _PacingHarness) -> None:
    """The max-min inter-packet spread is ~0, not the ~1.4 ms of the old pacer."""
    engine = _new_engine(harness, Codec.G722, sample_rate=_G722_SAMPLE_RATE)
    await engine.connect()
    _install(engine, harness, cost_ms=_ENCODE_COST_MS)
    await engine.send_audio(_g722_frame(12))
    await engine.stop()

    gaps = _gaps_ms(harness.send_times)
    spread = max(gaps) - min(gaps)
    assert spread < 0.1, (
        f"inter-packet spread {spread:.3f} ms is jitter; a steady pacer keeps it ~0"
    )


@pytest.mark.asyncio
async def test_g711_outbound_pacing_is_steady(harness: _PacingHarness) -> None:
    """Regression: G.711 pacing stays a steady 20.00 ms (it was already clean).

    Same fake encode-cost profile so the test is apples-to-apples with G.722; the
    deadline pacer must keep the narrowband interval rock-steady too — the fix must
    not regress the clean narrowband path.
    """
    engine = _new_engine(harness, Codec.PCMU, sample_rate=_G711_SAMPLE_RATE)
    await engine.connect()
    _install(engine, harness, cost_ms=_ENCODE_COST_MS)
    # 12 whole 8 kHz frames (160 samples each).
    pcm = b"\x00" * (_G711_SAMPLES_PER_FRAME * 2 * 12)
    await engine.send_audio(
        PcmFrame(samples=pcm, sample_rate=_G711_SAMPLE_RATE, monotonic_ts_ns=0)
    )
    await engine.stop()

    gaps = _gaps_ms(harness.send_times)
    assert len(gaps) >= 10, f"expected >=10 gaps, got {len(gaps)}"
    for i, gap in enumerate(gaps):
        assert gap == pytest.approx(_PTIME_MS, abs=0.05), (
            f"G.711 gap {i} = {gap:.3f} ms drifted from {_PTIME_MS} ms — the fix "
            f"regressed the clean narrowband path"
        )


@pytest.mark.asyncio
async def test_pacing_does_not_busy_spin_when_behind_schedule(
    harness: _PacingHarness,
) -> None:
    """When a frame's encode overruns the ptime, the pacer never sleeps negative.

    If the synchronous encode for a frame costs MORE than one ptime (a slow host),
    the deadline is already in the past: the pacer must clamp the wait to >= 0 (send
    immediately, do not sleep a negative duration) and then resynchronise — never
    sleep a negative time and never spin. We model a pathological 30 ms encode and
    assert no negative sleep was requested and the stream still advances.
    """
    sleeps: list[float] = []
    base_sleep = harness.sleep

    async def _recording_sleep(secs: float) -> None:
        sleeps.append(secs)
        await base_sleep(secs)

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.G722,
        sleep=_recording_sleep,
        pace_clock=harness.pace_clock,
        initial_seq=0,
        initial_ts=0,
    )
    await engine.connect()
    # Every frame "encodes" in 30 ms (> the 20 ms ptime): the schedule can never be
    # met, so each wait should clamp to 0, not go negative.
    _install(engine, harness, cost_ms=(30.0,))
    await engine.send_audio(_g722_frame(5))
    await engine.stop()

    assert all(s >= 0.0 for s in sleeps), (
        f"pacer requested a negative sleep {[s for s in sleeps if s < 0]} — it must "
        f"clamp the wait to >= 0 when the encode overruns the ptime"
    )
    # The stream still produced its 5 frames (progress, not a stall).
    assert len(harness.send_times) == 5, (
        f"expected 5 frames sent, got {len(harness.send_times)}"
    )


@pytest.mark.asyncio
async def test_pacing_across_separate_send_audio_calls_holds_the_schedule(
    harness: _PacingHarness,
) -> None:
    """The deadline carries ACROSS send_audio calls within a continuous stream.

    A streaming TTS hands send_audio many small chunks; the schedule must be one
    continuous timeline, not reset per call (a per-call reset would re-introduce a
    boundary discontinuity). Feed 6 single-frame chunks as 6 separate send_audio
    calls and assert the inter-packet spacing stays a steady ptime across the call
    boundaries.
    """
    engine = _new_engine(harness, Codec.G722, sample_rate=_G722_SAMPLE_RATE)
    await engine.connect()
    _install(engine, harness, cost_ms=_ENCODE_COST_MS)
    for _ in range(6):
        await engine.send_audio(_g722_frame(1))
    await engine.stop()

    gaps = _gaps_ms(harness.send_times)
    assert len(gaps) == 5, (
        f"expected 5 gaps across 6 single-frame sends, got {len(gaps)}"
    )
    for i, gap in enumerate(gaps):
        assert gap == pytest.approx(_PTIME_MS, abs=0.05), (
            f"cross-call gap {i} = {gap:.3f} ms drifted from {_PTIME_MS} ms - the "
            f"pacing schedule reset at a send_audio boundary"
        )
