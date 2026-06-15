"""Tests for hermes_voip.media.vad — silero-vad edge detection (ADR-0008 Phase 1).

The pure per-frame edge state machine is tested against offline PCM fixtures with
the ONNX model **injected** as a fake speech-probability callable, so CI stays
model-free (rule: never download in CI). A separate real-model smoke test
``importorskip``s onnxruntime and skips when the silero model isn't cached.

silero-vad runs natively at 8 kHz (256-sample window) or 16 kHz (512-sample
window); the detector slices the incoming PCM into exactly those windows and runs
one inference per window, emitting ONSET/OFFSET edges with hysteresis.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator, Sequence

import pytest

from hermes_voip.media.vad import (
    SILERO_WINDOW_SAMPLES,
    SpeechEdge,
    VadEvent,
    VoiceActivityDetector,
    load_silero_model,
)
from hermes_voip.providers.audio import PcmFrame

_RATE_16K = 16_000
_RATE_8K = 8_000


def _window_bytes(rate: int) -> int:
    """Byte length of one silero window at ``rate`` (PCM16 mono)."""
    return SILERO_WINDOW_SAMPLES[rate] * 2


def _silence(n_windows: int, rate: int) -> bytes:
    """``n_windows`` worth of digital-silence PCM16 at ``rate``."""
    return b"\x00\x00" * (SILERO_WINDOW_SAMPLES[rate] * n_windows)


class _ScriptedModel:
    """A fake silero callable returning canned probabilities, one per window.

    Each call consumes the next scripted probability; the recorded ``windows``
    let a test assert the detector fed exactly one model window per inference and
    sized each window correctly.
    """

    def __init__(self, probabilities: list[float]) -> None:
        self._probs = iter(probabilities)
        self.windows: list[bytes] = []

    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        self.windows.append(window_pcm16)
        return next(self._probs)


def _frame(pcm16: bytes, rate: int, ts_ns: int = 0) -> PcmFrame:
    return PcmFrame(samples=pcm16, sample_rate=rate, monotonic_ts_ns=ts_ns)


# --- window slicing -------------------------------------------------------


def test_feed_runs_one_inference_per_silero_window_16k() -> None:
    model = _ScriptedModel([0.1, 0.1, 0.1])
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, model=model)
    # exactly three 512-sample windows of audio
    vad.feed(_frame(_silence(3, _RATE_16K), _RATE_16K))
    assert len(model.windows) == 3
    assert all(len(w) == _window_bytes(_RATE_16K) for w in model.windows)


def test_feed_buffers_partial_window_until_full() -> None:
    model = _ScriptedModel([0.1, 0.1])
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, model=model)
    win = _window_bytes(_RATE_16K)
    partial = 200  # bytes (100 samples), a sub-window remainder to be buffered
    # one full window plus a partial: exactly one inference, the partial held
    list(vad.feed(_frame(_silence(1, _RATE_16K) + b"\x00" * partial, _RATE_16K)))
    assert len(model.windows) == 1
    # feed exactly the bytes that complete the second window -> a 2nd inference
    list(vad.feed(_frame(b"\x00" * (win - partial), _RATE_16K)))
    assert len(model.windows) == 2


def test_feed_supports_native_8k_window() -> None:
    model = _ScriptedModel([0.1, 0.1])
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_8K, model=model)
    vad.feed(_frame(_silence(2, _RATE_8K), _RATE_8K))
    assert len(model.windows) == 2
    assert all(len(w) == _window_bytes(_RATE_8K) for w in model.windows)


def test_feed_rejects_frame_at_wrong_rate() -> None:
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, model=_ScriptedModel([]))
    with pytest.raises(ValueError, match="16000"):
        list(vad.feed(_frame(_silence(1, _RATE_8K), _RATE_8K)))


def test_constructor_rejects_unsupported_rate() -> None:
    with pytest.raises(ValueError, match="8000 or 16000"):
        VoiceActivityDetector(sample_rate_hz=44_100, model=_ScriptedModel([]))


# --- edge detection -------------------------------------------------------


def _edges(events: Iterator[VadEvent]) -> list[SpeechEdge]:
    return [ev.edge for ev in events]


def test_onset_fires_when_probability_crosses_threshold() -> None:
    # silence, silence, speech -> one ONSET on the third window
    model = _ScriptedModel([0.1, 0.2, 0.9])
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, threshold=0.5, model=model)
    events = list(vad.feed(_frame(_silence(3, _RATE_16K), _RATE_16K)))
    assert _edges(iter(events)) == [SpeechEdge.ONSET]
    assert events[0].frame_index == 2  # zero-based ordinal of the 3rd window
    assert events[0].probability == pytest.approx(0.9)


def test_offset_fires_when_probability_drops_below_exit() -> None:
    # rise into speech, then fall well below the exit threshold
    model = _ScriptedModel([0.9, 0.9, 0.05])
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, threshold=0.5, model=model)
    events = list(vad.feed(_frame(_silence(3, _RATE_16K), _RATE_16K)))
    assert _edges(iter(events)) == [SpeechEdge.ONSET, SpeechEdge.OFFSET]
    assert events[1].edge is SpeechEdge.OFFSET
    assert events[1].frame_index == 2


def test_no_spurious_edges_while_steady() -> None:
    # steady speech then steady silence -> exactly one ONSET and one OFFSET
    model = _ScriptedModel([0.9, 0.95, 0.92, 0.04, 0.03, 0.02])
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, threshold=0.5, model=model)
    events = list(vad.feed(_frame(_silence(6, _RATE_16K), _RATE_16K)))
    assert _edges(iter(events)) == [SpeechEdge.ONSET, SpeechEdge.OFFSET]


def test_hysteresis_holds_speech_in_the_dead_band() -> None:
    # once in speech, a probability in (exit, threshold) must NOT end the turn:
    # threshold 0.5 -> exit 0.35; 0.4 sits in the dead band
    model = _ScriptedModel([0.9, 0.4, 0.9])
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, threshold=0.5, model=model)
    events = list(vad.feed(_frame(_silence(3, _RATE_16K), _RATE_16K)))
    assert _edges(iter(events)) == [SpeechEdge.ONSET]


def test_dead_band_below_threshold_does_not_onset() -> None:
    # from silence, a probability in the dead band must NOT start speech
    model = _ScriptedModel([0.4, 0.45, 0.49])
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, threshold=0.5, model=model)
    events = list(vad.feed(_frame(_silence(3, _RATE_16K), _RATE_16K)))
    assert events == []


def test_frame_index_is_monotonic_across_feed_calls() -> None:
    model = _ScriptedModel([0.1, 0.9, 0.05])
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, threshold=0.5, model=model)
    e1 = list(vad.feed(_frame(_silence(1, _RATE_16K), _RATE_16K)))
    e2 = list(vad.feed(_frame(_silence(1, _RATE_16K), _RATE_16K)))
    e3 = list(vad.feed(_frame(_silence(1, _RATE_16K), _RATE_16K)))
    assert e1 == []
    assert [ev.frame_index for ev in e2] == [1]  # ONSET on window ordinal 1
    assert [ev.frame_index for ev in e3] == [2]  # OFFSET on window ordinal 2


def test_reset_clears_speech_state_and_buffer() -> None:
    model = _ScriptedModel([0.9, 0.9])
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, threshold=0.5, model=model)
    first = list(vad.feed(_frame(_silence(1, _RATE_16K), _RATE_16K)))
    assert _edges(iter(first)) == [SpeechEdge.ONSET]
    vad.reset()
    # after reset we are back in silence: the next high prob ONSETs again,
    # and the window ordinal restarts at 0
    second = list(vad.feed(_frame(_silence(1, _RATE_16K), _RATE_16K)))
    assert _edges(iter(second)) == [SpeechEdge.ONSET]
    assert second[0].frame_index == 0


def test_reset_discards_buffered_partial_window() -> None:
    model = _ScriptedModel([0.9])
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, threshold=0.5, model=model)
    half = _window_bytes(_RATE_16K) // 2
    list(vad.feed(_frame(b"\x00\x00" * (half // 2), _RATE_16K)))
    vad.reset()
    # the buffered half-window is gone: completing it does NOT run an inference
    list(vad.feed(_frame(b"\x00\x00" * (half // 2), _RATE_16K)))
    assert model.windows == []


def test_feed_rejects_odd_length_pcm() -> None:
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, model=_ScriptedModel([]))
    with pytest.raises(ValueError, match="16-bit samples"):
        list(vad.feed(_frame(b"\x00\x00\x00", _RATE_16K)))


def test_threshold_defaults_and_custom_exit() -> None:
    # explicit exit_threshold overrides the derived dead band
    model = _ScriptedModel([0.9, 0.6])
    vad = VoiceActivityDetector(
        sample_rate_hz=_RATE_16K,
        threshold=0.7,
        exit_threshold=0.65,
        model=model,
    )
    events = list(vad.feed(_frame(_silence(2, _RATE_16K), _RATE_16K)))
    # 0.6 < exit 0.65 -> OFFSET
    assert _edges(iter(events)) == [SpeechEdge.ONSET, SpeechEdge.OFFSET]


def test_invalid_exit_threshold_rejected() -> None:
    with pytest.raises(ValueError, match="exit_threshold"):
        VoiceActivityDetector(
            sample_rate_hz=_RATE_16K,
            threshold=0.5,
            exit_threshold=0.6,  # must be <= threshold
            model=_ScriptedModel([]),
        )


def test_nan_exit_threshold_rejected() -> None:
    # A NaN exit threshold silently breaks endpointing: `probability < nan` is
    # always False, so speech never ends. It must be rejected at construction,
    # not accepted because `nan > threshold` happens to be False.
    with pytest.raises(ValueError, match="exit_threshold"):
        VoiceActivityDetector(
            sample_rate_hz=_RATE_16K,
            threshold=0.5,
            exit_threshold=float("nan"),
            model=_ScriptedModel([]),
        )


def test_infinite_exit_threshold_rejected() -> None:
    # +inf would pass a naive `> threshold` check inverted but is meaningless; a
    # finite value in [0, threshold] is required.
    with pytest.raises(ValueError, match="exit_threshold"):
        VoiceActivityDetector(
            sample_rate_hz=_RATE_16K,
            threshold=0.5,
            exit_threshold=float("-inf"),
            model=_ScriptedModel([]),
        )


def test_negative_exit_threshold_rejected() -> None:
    # A negative exit threshold can never be crossed (probabilities are >= 0.0),
    # so speech would never end. Require exit_threshold >= 0.0.
    with pytest.raises(ValueError, match="exit_threshold"):
        VoiceActivityDetector(
            sample_rate_hz=_RATE_16K,
            threshold=0.5,
            exit_threshold=-0.1,
            model=_ScriptedModel([]),
        )


# --- a real PCM fixture exercises the same edges deterministically --------


def _ramp_window(rate: int, amplitude: int) -> bytes:
    """One window of a constant-amplitude square-ish tone (non-zero energy)."""
    n = SILERO_WINDOW_SAMPLES[rate]
    return struct.pack(f"<{n}h", *([amplitude, -amplitude] * (n // 2)))


def test_real_pcm_fixture_drives_edges_via_injected_model() -> None:
    # An offline PCM fixture (silence then a tone then silence). The injected
    # model maps energy->probability so the edges are deterministic without a
    # real neural net, proving feed() slices/marshals real bytes correctly.
    rate = _RATE_16K

    def energy_model(window_pcm16: bytes, sample_rate: int) -> float:
        n = len(window_pcm16) // 2
        peak = max(abs(v) for v in struct.unpack(f"<{n}h", window_pcm16))
        return 0.95 if peak > 1000 else 0.02

    vad = VoiceActivityDetector(sample_rate_hz=rate, threshold=0.5, model=energy_model)
    pcm = _silence(2, rate) + _ramp_window(rate, 8000) * 2 + _silence(2, rate)
    events = list(vad.feed(_frame(pcm, rate)))
    assert _edges(iter(events)) == [SpeechEdge.ONSET, SpeechEdge.OFFSET]
    onset, offset = events
    assert onset.frame_index == 2  # speech starts at the 3rd window
    assert offset.frame_index == 4  # silence resumes at the 5th window


# --- live ONNX wrapper contract (fakes; no ml extra) ----------------------
#
# These drive the private ``_SileroOnnxModel`` with fakes that implement EXACTLY
# the narrow ``_NumpyModule`` / ``_OrtSession`` Protocols it depends on — no
# wider numpy/ort surface. That proves two things offline (the ml extra is never
# needed here): the wrapper marshals each window into the silero inputs and
# threads the recurrent state correctly, AND the local Protocols are the real
# call contract (a fake missing a used method would fail to construct/run). If a
# future edit reaches outside the declared Protocol surface, this test breaks.


class _FakeArray:
    """A stand-in ndarray exposing only the ``_NdArray`` Protocol operations.

    It carries an opaque ``tag`` so the test can identify which array flowed
    where (e.g. that the next-state output is threaded into the following call),
    and a ``value`` used only when the wrapper coerces a scalar via ``float()``.
    """

    def __init__(self, tag: str, value: float = 0.0) -> None:
        self.tag = tag
        self.value = value

    def astype(self, dtype: object, /) -> _FakeArray:
        return self

    def reshape(self, *shape: int) -> _FakeArray:
        return self

    def __truediv__(self, divisor: float, /) -> _FakeArray:
        return self

    def __getitem__(self, index: int, /) -> _FakeArray:
        return self

    def __float__(self) -> float:
        return self.value


class _FakeNumpy:
    """A ``_NumpyModule`` whose factories return tagged :class:`_FakeArray`s."""

    # typed ``object`` to match the Protocol's opaque dtype members exactly
    # (mypy treats Protocol attribute types invariantly).
    float32: object = "float32"
    int64: object = "int64"

    def zeros(self, shape: object, *, dtype: object) -> _FakeArray:
        return _FakeArray("zero-state")

    def frombuffer(self, buffer: bytes, *, dtype: str) -> _FakeArray:
        return _FakeArray("samples")

    def array(self, obj: object, *, dtype: object) -> _FakeArray:
        return _FakeArray("sr")


class _FakeSession:
    """An ``_OrtSession`` returning scripted probabilities and a fresh next-state.

    Records each call's ``state`` input and the full input feed so the test can
    assert the recurrent state is threaded and the inputs are well-formed.
    """

    def __init__(self, probabilities: list[float]) -> None:
        self._probs = iter(probabilities)
        self.seen_states: list[object] = []
        self.feeds: list[dict[str, object]] = []

    def run(
        self, output_names: Sequence[str] | None, input_feed: dict[str, object], /
    ) -> Sequence[_FakeArray]:
        assert output_names is None
        self.seen_states.append(input_feed["state"])
        self.feeds.append(dict(input_feed))
        prob = next(self._probs)
        # output 0 = probability (coerced via float()); output 1 = next state,
        # uniquely tagged per call so state-threading is observable.
        next_state = _FakeArray(f"state-after-call-{len(self.feeds)}")
        return [_FakeArray("prob", value=prob), next_state]


def test_silero_onnx_wrapper_marshals_inputs_and_threads_state() -> None:
    from hermes_voip.media.vad import (  # noqa: PLC0415  (lazy: private symbol)
        _NumpyModule,
        _OrtSession,
        _SileroOnnxModel,
    )

    session = _FakeSession([0.8, 0.1])
    numpy = _FakeNumpy()
    # the fakes satisfy the runtime-checkable Protocols the wrapper relies on
    assert isinstance(session, _OrtSession)
    assert isinstance(numpy, _NumpyModule)

    model = _SileroOnnxModel(session, numpy)
    window = b"\x00\x00" * SILERO_WINDOW_SAMPLES[_RATE_16K]

    first = model(window, _RATE_16K)
    second = model(window, _RATE_16K)

    # the scripted probabilities are returned, coerced from output 0
    assert first == pytest.approx(0.8)
    assert second == pytest.approx(0.1)
    # every call feeds the three named silero inputs
    assert set(session.feeds[0]) == {"input", "state", "sr"}
    # the first call starts from the zero state; the second is threaded the
    # next-state output of the first (recurrent state carried across windows)
    assert isinstance(session.seen_states[0], _FakeArray)
    assert session.seen_states[0].tag == "zero-state"
    assert isinstance(session.seen_states[1], _FakeArray)
    assert session.seen_states[1].tag == "state-after-call-1"


def test_silero_onnx_wrapper_reset_restores_zero_state() -> None:
    from hermes_voip.media.vad import _SileroOnnxModel  # noqa: PLC0415

    session = _FakeSession([0.9, 0.9])
    model = _SileroOnnxModel(session, _FakeNumpy())
    window = b"\x00\x00" * SILERO_WINDOW_SAMPLES[_RATE_16K]

    model(window, _RATE_16K)  # advances the state away from zero
    model.reset()
    model(window, _RATE_16K)  # must start from the zero state again
    last_state = session.seen_states[-1]
    assert isinstance(last_state, _FakeArray)
    assert last_state.tag == "zero-state"


# --- real-model smoke (skipped in the model-free gate) --------------------


def test_real_silero_model_loads_if_cached() -> None:
    pytest.importorskip("onnxruntime")
    pytest.importorskip("numpy")
    try:
        model = load_silero_model(_RATE_16K)
    except FileNotFoundError:
        pytest.skip("silero-vad model not cached; CI never downloads it")
    # feeding silence to the real model yields low speech probability and no edge
    vad = VoiceActivityDetector(sample_rate_hz=_RATE_16K, model=model)
    events = list(vad.feed(_frame(_silence(10, _RATE_16K), _RATE_16K)))
    assert events == []
