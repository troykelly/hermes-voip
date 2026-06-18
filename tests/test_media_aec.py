"""In-process NLMS acoustic echo canceller — unit tests (ADR-0033).

The gateway reflects the agent's own TTS back on the inbound leg (a delayed,
attenuated, filtered copy of the outbound signal we already hold). The canceller
estimates that echo from the known outbound REFERENCE and subtracts it from the
inbound NEAR-END before the VAD/ASR see it, so:

* a known reflected reference is cancelled to near-silence (no false VAD onset),
* a real, uncorrelated near-end signal (the caller) survives the subtraction (a
  genuine barge-in still triggers), and double-talk does not let the filter eat
  the caller, and
* ``cancel`` adds NO algorithmic latency: it returns exactly the samples it was
  given (no extra frame of buffering).

All deterministic and pure-stdlib (no extra). Audio is synthesised as PCM16 so
energy assertions are exact.
"""

from __future__ import annotations

import math
import random
import struct
from collections.abc import Sequence

from hermes_voip.media.aec import EchoCanceller

_G711_RATE = 8_000
_G722_RATE = 16_000
_OPUS_WIRE_RATE = 48_000


# ---------------------------------------------------------------------------
# PCM helpers (pure stdlib; energy is exact integer math)
# ---------------------------------------------------------------------------


def _pack(samples: Sequence[int]) -> bytes:
    """Pack int samples to PCM16-LE, clamping to the int16 range."""
    clamped = [max(-32768, min(32767, int(s))) for s in samples]
    return struct.pack(f"<{len(clamped)}h", *clamped)


def _unpack(pcm16: bytes) -> tuple[int, ...]:
    return struct.unpack(f"<{len(pcm16) // 2}h", pcm16)


def _rms(pcm16: bytes) -> float:
    """Root-mean-square amplitude of a PCM16 buffer (0.0 for empty)."""
    vals = _unpack(pcm16)
    if not vals:
        return 0.0
    return math.sqrt(sum(v * v for v in vals) / len(vals))


def _sine(
    n: int, *, freq_hz: float, rate: int, amplitude: float, phase: float = 0.0
) -> list[int]:
    """A pure sine of ``n`` PCM16 samples at ``freq_hz`` and ``amplitude`` fraction."""
    peak = amplitude * 32767.0
    w = 2.0 * math.pi * freq_hz / rate
    return [int(peak * math.sin(w * i + phase)) for i in range(n)]


def _noise(n: int, *, amplitude: float, seed: int) -> list[int]:
    """``n`` samples of band-broad (white) PCM16 noise — a speech-like reference.

    A delay of a broadband signal is NOT representable by a couple of taps (unlike a
    pure tone), so cancelling a *delayed* broadband echo genuinely requires the filter
    to span the delay. Using a tone here would let a short filter cancel any delay and
    hide an under-sized echo-delay window (the real failure mode).
    """
    rng = random.Random(seed)  # noqa: S311 — test signal, not cryptographic
    peak = amplitude * 32767.0
    return [int(rng.uniform(-peak, peak)) for _ in range(n)]


def _delayed_echo(reference: Sequence[int], *, delay: int, gain: float) -> list[int]:
    """A pure delayed+attenuated copy: ``echo[n] = gain * reference[n - delay]``."""
    out = [0] * len(reference)
    for i in range(len(reference)):
        j = i - delay
        if 0 <= j < len(reference):
            out[i] = int(gain * reference[j])
    return out


def _echo_of(
    reference: Sequence[int], *, delay: int, gain: float, taps: Sequence[float]
) -> list[int]:
    """A deterministic echo: reference convolved with ``taps``, delayed, attenuated.

    Models a realistic echo path: a fixed bulk ``delay`` (samples), an overall
    ``gain`` attenuation, and a short FIR impulse response ``taps`` (the room /
    hybrid colouring). ``echo[n] = gain * sum_k taps[k] * reference[n - delay - k]``.
    """
    out = [0] * len(reference)
    for n in range(len(reference)):
        acc = 0.0
        for k, c in enumerate(taps):
            j = n - delay - k
            if 0 <= j < len(reference):
                acc += c * reference[j]
        out[n] = int(gain * acc)
    return out


# ---------------------------------------------------------------------------
# Convergence: a known reflected reference is cancelled toward silence
# ---------------------------------------------------------------------------


def _drive(
    aec: EchoCanceller,
    *,
    reference: Sequence[int],
    near_end: Sequence[int],
    rate: int,
    block: int,
) -> bytes:
    """Feed reference + near-end through the AEC in ``block``-sized frames.

    Mirrors the engine wiring: each outbound frame is pushed as the reference,
    then the matching inbound (near-end) frame is cancelled. Returns the
    concatenated residual PCM16.
    """
    residual = bytearray()
    for off in range(0, len(near_end), block):
        ref_chunk = reference[off : off + block]
        near_chunk = near_end[off : off + block]
        aec.push_reference(_pack(ref_chunk), sample_rate=rate)
        residual += aec.cancel(_pack(near_chunk))
    return bytes(residual)


def test_cancels_known_reflected_reference_toward_silence() -> None:
    """Pure echo (no caller) is driven well below the input echo level.

    The near-end is ONLY the echo of the reference. After the NLMS filter
    converges the residual energy must collapse — proving the canceller removes
    the known reflected signal so the VAD never sees it.
    """
    rate = _G722_RATE
    n = rate  # 1 second of audio
    # The outbound TTS reference: a couple of partials so the filter has spectral
    # content to lock onto (a single tone is a pathological easy case).
    reference = [
        a + b
        for a, b in zip(
            _sine(n, freq_hz=320.0, rate=rate, amplitude=0.30),
            _sine(n, freq_hz=850.0, rate=rate, amplitude=0.18),
            strict=True,
        )
    ]
    # The echo the gateway reflects back: delayed, attenuated, mildly filtered.
    echo = _echo_of(reference, delay=12, gain=0.6, taps=(1.0, 0.4, -0.2))

    aec = EchoCanceller(sample_rate=rate, filter_len=256, bulk_delay=0, mu=0.5)
    residual = _drive(aec, reference=reference, near_end=echo, rate=rate, block=320)

    echo_rms = _rms(_pack(echo))
    # Measure the residual on the SECOND half, after the filter has converged.
    tail = residual[len(residual) // 2 :]
    tail_rms = _rms(tail)
    assert echo_rms > 200.0, f"test echo too quiet to be meaningful: {echo_rms}"
    # At least ~12 dB of echo return loss enhancement on the converged tail.
    assert tail_rms < echo_rms * 0.25, (
        f"echo not cancelled: residual_rms={tail_rms:.1f} vs echo_rms={echo_rms:.1f}"
    )


def test_cancels_delayed_broadband_echo_when_window_spans_the_delay() -> None:
    """A broadband echo delayed by the round-trip is cancelled only by a wide window.

    The real failure mode (cross-vendor review): the gateway reflects the agent's
    SPEECH (broadband) back after a round-trip delay of tens of ms (our jitter buffer
    + gateway). A delay of a broadband signal is NOT representable by a couple of taps,
    so the adaptive filter must reach back PAST the delay to model it. This pins that a
    window spanning the 40 ms delay (the 64 ms-default tap count) drives the echo to
    near silence — and, as a guard, that a too-short 16 ms window does NOT (so the test
    actually exercises the delay reach, not a trivially-cancellable tone).
    """
    rate = _G711_RATE
    n = rate * 2  # 2 s
    reference = _noise(n, amplitude=0.3, seed=7)
    delay = (rate * 40) // 1000  # 40 ms round-trip echo delay
    echo = _delayed_echo(reference, delay=delay, gain=0.6)

    # Wide window (64 ms = 512 taps at 8 kHz — the default) spans the 40 ms delay.
    wide = EchoCanceller(sample_rate=rate, filter_len=512, bulk_delay=0, mu=0.5)
    wide_res = _drive(wide, reference=reference, near_end=echo, rate=rate, block=160)
    # Too-short window (16 ms = 128 taps) cannot reach the 40 ms-delayed echo.
    narrow = EchoCanceller(sample_rate=rate, filter_len=128, bulk_delay=0, mu=0.5)
    narrow_res = _drive(
        narrow, reference=reference, near_end=echo, rate=rate, block=160
    )

    echo_rms = _rms(_pack(echo))
    wide_rms = _rms(wide_res[len(wide_res) // 2 :])
    narrow_rms = _rms(narrow_res[len(narrow_res) // 2 :])
    assert echo_rms > 200.0
    # The wide (default) window cancels the delayed broadband echo.
    assert wide_rms < echo_rms * 0.2, (
        f"wide window failed to cancel a 40 ms-delayed broadband echo: "
        f"residual={wide_rms:.1f} echo={echo_rms:.1f}"
    )
    # The narrow window does NOT — proving the test exercises the delay reach (and
    # that the default must be wide enough, which is why it is 64 ms not 16 ms).
    assert narrow_rms > echo_rms * 0.5, (
        f"narrow window unexpectedly cancelled a 40 ms echo with a 16 ms reach: "
        f"residual={narrow_rms:.1f} echo={echo_rms:.1f}"
    )


def test_cancels_echo_when_inbound_starts_before_outbound() -> None:
    """Near-end arriving BEFORE the first reference does not break later cancellation.

    On a real call the inbound pump and the greeting playout start concurrently, so
    near-end (RTP / comfort noise) can arrive before the first outbound TTS frame.
    Those pre-roll samples have no echo (the agent has not spoken) and must pass
    through — and crucially the read cursor must NOT run off the end of the (empty)
    far-end FIFO, or when the greeting's echo finally arrives the window would point
    past the FIFO and never cancel (cross-vendor review: a 200 ms inbound pre-roll
    reproduced exactly this — the echo tail stayed at 100%).
    """
    rate = _G711_RATE
    block = 160
    pre_roll_frames = 12  # ~240 ms of inbound before any outbound
    n = rate * 2
    reference = _noise(n, amplitude=0.3, seed=21)
    delay = (rate * 30) // 1000  # 30 ms echo delay
    echo = _delayed_echo(reference, delay=delay, gain=0.6)

    aec = EchoCanceller(sample_rate=rate, filter_len=512, bulk_delay=0, mu=0.5)
    residual = bytearray()
    # Phase 1: inbound silence arrives with NO reference pushed yet (the pre-roll).
    silence = [0] * block
    for _ in range(pre_roll_frames):
        residual += aec.cancel(_pack(silence))
    # Phase 2: now the outbound starts; push reference + cancel the delayed echo 1:1.
    for off in range(0, n, block):
        aec.push_reference(_pack(reference[off : off + block]), sample_rate=rate)
        residual += aec.cancel(_pack(echo[off : off + block]))

    echo_rms = _rms(_pack(echo))
    # Measure the converged tail of the phase-2 residual (skip the pre-roll + ramp-up).
    tail = residual[len(residual) - n :]  # the last ~2 s (phase-2 region)
    tail_rms = _rms(tail[len(tail) // 2 :])
    assert echo_rms > 200.0
    assert tail_rms < echo_rms * 0.25, (
        f"echo not cancelled after an inbound pre-roll: "
        f"residual={tail_rms:.1f} echo={echo_rms:.1f}"
    )


def test_disabled_passthrough_does_not_cancel() -> None:
    """A divergence guard sanity check: with no reference pushed, near-end is intact.

    If nothing is pushed as the reference (e.g. the agent is silent), the filter
    has nothing to subtract, so a near-end signal passes through unchanged — the
    canceller never invents a subtraction that would damage caller-only audio.
    """
    rate = _G711_RATE
    n = rate // 2
    near = _sine(n, freq_hz=440.0, rate=rate, amplitude=0.3)
    aec = EchoCanceller(sample_rate=rate, filter_len=128, bulk_delay=0, mu=0.5)
    # No push_reference at all.
    out = bytearray()
    for off in range(0, n, 160):
        out += aec.cancel(_pack(near[off : off + 160]))
    assert _unpack(bytes(out)) == tuple(near), "caller-only audio must pass through"


# ---------------------------------------------------------------------------
# Double-talk: the caller survives; the echo is still removed
# ---------------------------------------------------------------------------


def test_uncorrelated_near_end_survives_cancellation() -> None:
    """A real caller talking OVER the echo is not cancelled away (barge-in safe).

    Near-end = caller speech (uncorrelated with the reference) + echo. After the
    canceller, the caller component must still dominate the residual — i.e. the
    residual stays close to the caller-only signal, NOT driven to silence. This is
    the property that lets a genuine barge-in still fire the VAD.
    """
    rate = _G722_RATE
    n = rate
    reference = _sine(n, freq_hz=300.0, rate=rate, amplitude=0.32)
    echo = _echo_of(reference, delay=10, gain=0.6, taps=(1.0, 0.3))
    # The caller: a different frequency, present in the SECOND half only (they cut
    # in partway). Uncorrelated with the reference.
    caller = [0] * n
    caller_half = _sine(n // 2, freq_hz=1700.0, rate=rate, amplitude=0.35)
    caller[n // 2 :] = caller_half
    near = [e + c for e, c in zip(echo, caller, strict=True)]

    aec = EchoCanceller(sample_rate=rate, filter_len=256, bulk_delay=0, mu=0.5)
    residual = _drive(aec, reference=reference, near_end=near, rate=rate, block=320)

    # Compare the residual's second half (caller present) to the caller-only signal.
    res_tail = _rms(residual[len(residual) // 2 :])
    caller_rms = _rms(_pack(caller_half))
    assert caller_rms > 200.0
    # The caller survives: residual energy where the caller talks is a large
    # fraction of the caller-only energy (not cancelled toward silence).
    assert res_tail > caller_rms * 0.6, (
        f"caller was cancelled away: residual={res_tail:.1f} caller={caller_rms:.1f}"
    )


# ---------------------------------------------------------------------------
# No added latency: cancel returns exactly the input length, frame-by-frame
# ---------------------------------------------------------------------------


def test_cancel_preserves_frame_length_no_buffering() -> None:
    """``cancel`` returns the same number of samples it was given (no delay).

    A buffering canceller (one that holds a frame to look ahead) would add a ptime
    of latency to the conversational path. Each ``cancel`` call must return exactly
    its input frame length so the inbound chain gains zero algorithmic delay.
    """
    rate = _G711_RATE
    aec = EchoCanceller(sample_rate=rate, filter_len=128, bulk_delay=0, mu=0.3)
    for _ in range(20):
        aec.push_reference(_pack([100] * 160), sample_rate=rate)
        out = aec.cancel(_pack([50] * 160))
        assert len(out) == 160 * 2, "cancel must return exactly the input frame length"


def test_cancel_empty_frame_is_empty() -> None:
    """An empty inbound frame yields an empty residual (no spurious samples)."""
    aec = EchoCanceller(sample_rate=_G711_RATE, filter_len=64, bulk_delay=0, mu=0.3)
    assert aec.cancel(b"") == b""


# ---------------------------------------------------------------------------
# Rate alignment: an Opus 48 kHz reference is downsampled to the 16 kHz analysis
# ---------------------------------------------------------------------------


def test_reference_downsampled_to_analysis_rate_for_opus() -> None:
    """A 48 kHz wire reference still cancels a 16 kHz-analysis-rate echo.

    On the Opus path the outbound reference is 48 kHz but the inbound analysis
    (and the echo) is 16 kHz. ``push_reference`` must downsample the reference to
    the analysis rate so the canceller's two inputs align — otherwise the filter
    could never converge.
    """
    analysis = 16_000
    n_wire = _OPUS_WIRE_RATE  # 1 s of 48 kHz reference
    n_near = analysis  # 1 s of 16 kHz near-end
    ref_wire = _sine(n_wire, freq_hz=300.0, rate=_OPUS_WIRE_RATE, amplitude=0.3)
    # The analysis-rate version of that reference (what the echo is derived from).
    ref_analysis = _sine(n_near, freq_hz=300.0, rate=analysis, amplitude=0.3)
    echo = _echo_of(ref_analysis, delay=8, gain=0.6, taps=(1.0, 0.3))

    aec = EchoCanceller(sample_rate=analysis, filter_len=256, bulk_delay=0, mu=0.5)
    # Push the 48 kHz reference in 20 ms wire frames (960 samples); cancel the
    # 16 kHz near-end in 20 ms analysis frames (320 samples). Same wall-clock rate.
    residual = bytearray()
    wire_block = (_OPUS_WIRE_RATE * 20) // 1000  # 960
    near_block = (analysis * 20) // 1000  # 320
    n_frames = n_near // near_block
    for f in range(n_frames):
        aec.push_reference(
            _pack(ref_wire[f * wire_block : (f + 1) * wire_block]),
            sample_rate=_OPUS_WIRE_RATE,
        )
        residual += aec.cancel(_pack(echo[f * near_block : (f + 1) * near_block]))

    echo_rms = _rms(_pack(echo))
    tail_rms = _rms(residual[len(residual) // 2 :])
    assert echo_rms > 200.0
    assert tail_rms < echo_rms * 0.35, (
        f"opus-rate echo not cancelled: residual={tail_rms:.1f} echo={echo_rms:.1f}"
    )
