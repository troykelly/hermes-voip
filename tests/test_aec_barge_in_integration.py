"""AEC + lowered barge-in threshold — composition integration (ADR-0033).

The point of the AEC is to let the barge-in sustained threshold drop (600 ms →
200 ms) WITHOUT re-opening the self-interruption loop: with the echo cancelled
before the VAD, even the responsive 200 ms threshold never fires on the gateway's
reflected TTS, yet a genuine caller interruption still fires it.

This composes the REAL pieces the pump wires together — the real
:class:`~hermes_voip.media.aec.EchoCanceller`, a real
:class:`~hermes_voip.media.vad.VoiceActivityDetector` driven by an energy-sensing
model (voiced iff the frame has real energy — exactly what AEC controls), and the
real :class:`~hermes_voip.media.call_loop.BargeInGate` at the AEC-lowered window
count — and asserts:

* reflected echo, cancelled, never reaches the gate's sustained threshold (no
  self-interruption at 200 ms), but
* a real caller talking over the agent DOES reach it and barges in.

Pure stdlib + the pure VAD state machine (a fake model), no network/threads.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence

from hermes_voip.media.aec import EchoCanceller
from hermes_voip.media.call_loop import BargeInGate, BargeInMode
from hermes_voip.media.vad import (
    VadModel,
    VoiceActivityDetector,
    windows_for_ms,
)
from hermes_voip.providers.audio import PcmFrame

_RATE = 16_000
_WINDOW = 512  # one 16 kHz silero window (32 ms)
# The AEC-lowered barge-in threshold (ADR-0033): 200 ms of sustained voicing.
_MIN_WINDOWS = windows_for_ms(200, _RATE)  # ceil(200/32) = 7
_TAIL_WINDOWS = windows_for_ms(250, _RATE)


class _EnergyModel:
    """A VAD model that reports speech iff the window has real RMS energy.

    This is the property the AEC controls: cancelled echo collapses to near
    silence (RMS below the floor → not voiced), while real caller audio keeps its
    energy (→ voiced). A fixed-probability fake could not distinguish the two, so
    it would not test the AEC at all. Signature matches
    :class:`~hermes_voip.media.vad.VadModel`: one full PCM16 window in, a
    probability out.
    """

    def __init__(self, *, rms_floor: float) -> None:
        self._floor = rms_floor

    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        # sample_rate is part of the VadModel surface; this energy model is
        # rate-agnostic, so it reads only the PCM.
        del sample_rate
        vals = struct.unpack(f"<{len(window_pcm16) // 2}h", window_pcm16)
        if not vals:
            return 0.0
        rms = math.sqrt(sum(v * v for v in vals) / len(vals))
        return 0.95 if rms > self._floor else 0.0


def _vad() -> VoiceActivityDetector:
    # Floor at ~1% full-scale (≈328 of int16): cancelled echo (well below) reads as
    # silence; a real caller (tens of % full-scale) reads as speech.
    model: VadModel = _EnergyModel(rms_floor=328.0)
    return VoiceActivityDetector(model=model, sample_rate_hz=_RATE, threshold=0.5)


def _frame(samples: Sequence[int]) -> PcmFrame:
    return PcmFrame(
        samples=struct.pack(f"<{len(samples)}h", *samples),
        sample_rate=_RATE,
        monotonic_ts_ns=0,
    )


def _sine(n: int, *, freq_hz: float, amplitude: float) -> list[int]:
    peak = amplitude * 32767.0
    w = 2.0 * math.pi * freq_hz / _RATE
    return [int(peak * math.sin(w * i)) for i in range(n)]


def _echo_of(reference: Sequence[int], *, delay: int, gain: float) -> list[int]:
    out = [0] * len(reference)
    for i in range(len(reference)):
        j = i - delay
        if 0 <= j < len(reference):
            out[i] = int(gain * reference[j])
    return out


def _pump_window(  # noqa: PLR0913 — mirrors the pump's per-window inputs (the three collaborators + the two signals + the tts-active flag); bundling them would only obscure the test
    aec: EchoCanceller,
    vad: VoiceActivityDetector,
    gate: BargeInGate,
    *,
    reference: Sequence[int],
    near_end: Sequence[int],
    tts_active: bool,
) -> bool:
    """Mirror one pump iteration over a VAD window; return True if barge-in fired.

    Pushes the outbound reference, cancels the inbound near-end, feeds the
    cancelled frame to the VAD, drives the gate from the edges + tts-active state,
    and asks ``should_barge_in`` — exactly the order the real pump uses.
    """
    aec.push_reference(
        struct.pack(f"<{len(reference)}h", *reference), sample_rate=_RATE
    )
    residual = aec.cancel(struct.pack(f"<{len(near_end)}h", *near_end))
    fired = False
    for ev in vad.feed(
        PcmFrame(samples=residual, sample_rate=_RATE, monotonic_ts_ns=0)
    ):
        gate.on_event(ev)
    latest = vad.window_index - 1
    gate.tts_active(tts_active)
    if gate.should_barge_in(latest):
        fired = True
    return fired


def test_cancelled_echo_does_not_self_interrupt_at_lowered_threshold() -> None:
    """Reflected echo, cancelled, never barges in even at the 200 ms threshold.

    The agent's TTS plays (tts_active) and the gateway reflects it back as the
    inbound near-end. After the AEC, the residual is below the VAD floor, so no
    sustained voiced run ever forms — the gate never fires, even though the
    threshold is the responsive 7 windows. This is the self-interruption the AEC
    removes.
    """
    aec = EchoCanceller(sample_rate=_RATE, filter_len=256, bulk_delay=0, mu=0.5)
    vad = _vad()
    gate = BargeInGate(
        mode=BargeInMode.GATED,
        min_voiced_windows=_MIN_WINDOWS,
        tail_windows=_TAIL_WINDOWS,
    )

    reference = _sine(_WINDOW * 80, freq_hz=420.0, amplitude=0.4)
    echo = _echo_of(reference, delay=10, gain=0.7)

    fired_any = False
    for w in range(80):
        ref = reference[w * _WINDOW : (w + 1) * _WINDOW]
        near = echo[w * _WINDOW : (w + 1) * _WINDOW]
        if _pump_window(aec, vad, gate, reference=ref, near_end=near, tts_active=True):
            fired_any = True
    assert not fired_any, "cancelled echo self-interrupted at the lowered threshold"


def test_real_caller_still_barges_in_at_lowered_threshold() -> None:
    """A genuine caller talking over the agent DOES barge in at 200 ms.

    Same setup, but the inbound near-end is the echo PLUS a real caller (a
    different frequency, uncorrelated). The caller survives the AEC, the VAD sees
    sustained energy, and the gate fires once the run reaches 7 windows —
    responsive barge-in, preserved.
    """
    aec = EchoCanceller(sample_rate=_RATE, filter_len=256, bulk_delay=0, mu=0.5)
    vad = _vad()
    gate = BargeInGate(
        mode=BargeInMode.GATED,
        min_voiced_windows=_MIN_WINDOWS,
        tail_windows=_TAIL_WINDOWS,
    )

    reference = _sine(_WINDOW * 80, freq_hz=420.0, amplitude=0.4)
    echo = _echo_of(reference, delay=10, gain=0.7)
    caller = _sine(_WINDOW * 80, freq_hz=1500.0, amplitude=0.4)
    near_full = [e + c for e, c in zip(echo, caller, strict=True)]

    fired_window: int | None = None
    for w in range(80):
        ref = reference[w * _WINDOW : (w + 1) * _WINDOW]
        near = near_full[w * _WINDOW : (w + 1) * _WINDOW]
        if _pump_window(aec, vad, gate, reference=ref, near_end=near, tts_active=True):
            fired_window = w
            break
    assert fired_window is not None, "a real caller failed to barge in with AEC on"
    # It fired promptly — within a few windows of the threshold, not after 600 ms.
    assert fired_window <= _MIN_WINDOWS + 3, (
        f"barge-in too slow: fired at window {fired_window} (threshold {_MIN_WINDOWS})"
    )
