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
    _NodeArg,  # private symbol; test-only
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


@pytest.mark.parametrize("tiny", [5e-324, 1e-300, 0.001, 0.0099])
def test_constructor_rejects_threshold_below_min(tiny: float) -> None:
    # A VAD probability threshold is realistically 0.1-0.9. A vanishingly small
    # one is not a usable cutoff AND breaks the derived default: the smallest
    # positive float (5e-324) times 0.5 underflows to 0.0, which can never be
    # crossed by a 0.0..1.0 probability. Require a normal positive probability
    # (threshold >= 0.01) so the derived exit is always positive/representable.
    with pytest.raises(ValueError, match="threshold"):
        VoiceActivityDetector(
            sample_rate_hz=_RATE_16K, threshold=tiny, model=_ScriptedModel([])
        )


@pytest.mark.parametrize("threshold", [0.1, 0.5, 0.9, 1.0, 0.01])
def test_normal_threshold_constructs_with_default_exit_in_open_interval(
    threshold: float,
) -> None:
    # Every realistic threshold (and the inclusive 0.01 lower bound and 1.0 upper
    # bound) constructs without an explicit exit_threshold and derives a default
    # cutoff strictly inside (0.0, threshold) -- always positive, never floored.
    vad = VoiceActivityDetector(
        sample_rate_hz=_RATE_16K, threshold=threshold, model=_ScriptedModel([])
    )
    assert 0.0 < vad._exit_threshold < threshold


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
    # threshold 0.5 -> default exit 0.25 (threshold * 0.5); 0.4 sits in the dead band
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
    # so speech would never end. Require exit_threshold > 0.0.
    with pytest.raises(ValueError, match="exit_threshold"):
        VoiceActivityDetector(
            sample_rate_hz=_RATE_16K,
            threshold=0.5,
            exit_threshold=-0.1,
            model=_ScriptedModel([]),
        )


def test_zero_exit_threshold_rejected() -> None:
    # exit_threshold == 0.0 silently breaks endpointing: the OFFSET test is
    # `probability < exit_threshold`, and probabilities are in [0.0, 1.0], so
    # `prob < 0.0` is NEVER true -> once speech starts it never ends. A zero
    # cutoff must be rejected at construction (require exit_threshold > 0.0),
    # exactly like a negative one.
    with pytest.raises(ValueError, match="exit_threshold"):
        VoiceActivityDetector(
            sample_rate_hz=_RATE_16K,
            threshold=0.5,
            exit_threshold=0.0,
            model=_ScriptedModel([]),
        )


def test_low_threshold_derives_crossable_exit_and_speech_ends() -> None:
    # For a LOW threshold the default exit_threshold must still be a real,
    # crossable cutoff strictly inside (0, threshold) -- never floored to 0,
    # which would leave speech stuck open forever. With threshold=0.1 and no
    # explicit exit_threshold, construction must succeed, derive an exit in
    # (0.0, 0.1), and silence (prob=0.0) must actually end the turn.
    vad = VoiceActivityDetector(
        sample_rate_hz=_RATE_16K,
        threshold=0.1,
        model=_ScriptedModel([0.9, 0.0]),
    )
    # read the resolved private cutoff to assert the derivation's bounds directly
    derived_exit = vad._exit_threshold
    assert 0.0 < derived_exit < 0.1
    # drive prob above threshold (ONSET) then prob == 0.0 silence (OFFSET fires)
    events = list(vad.feed(_frame(_silence(2, _RATE_16K), _RATE_16K)))
    assert _edges(iter(events)) == [SpeechEdge.ONSET, SpeechEdge.OFFSET]


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


class _FakeNodeArg:
    """A stand-in for ``onnxruntime.NodeArg`` exposing just ``.name`` and ``.shape``.

    Satisfies the ``_NodeArg`` Protocol from ``hermes_voip.media.vad`` so
    ``_FakeSession.get_inputs()`` can be typed as ``list[_NodeArg]``. ``shape``
    elements are ``int`` (fixed), ``None`` (unnamed dynamic), or ``str`` (a
    symbolic/named dynamic dim) — exactly what onnxruntime's ``NodeArg.shape``
    yields.
    """

    def __init__(self, name: str, shape: list[int | str | None]) -> None:
        self.name: str = name
        self.shape: list[int | str | None] = shape


class _FakeSession:
    """An ``_OrtSession`` returning scripted probabilities and a fresh next-state.

    Records each call's ``state`` input and the full input feed so the test can
    assert the recurrent state is threaded and the inputs are well-formed.

    ``state_shape`` configures what ``get_inputs()`` reports for the state input.
    The default mirrors what the REAL silero v5 ONNX reports — ``[2, None, 128]``:
    the batch dim (index 1) is **dynamic** (onnxruntime returns ``None`` or a
    symbolic ``str`` for a dynamic dim), and only dim0 (``2``) and dim2 (``128``)
    are fixed. The incompatible silero v4 mirror (``deepghs/silero-vad-onnx``)
    instead reports dim2 ``64`` (e.g. ``[2, None, 64]`` / ``[2, 1, 64]``).
    Pass ``state_shape=None`` to drop the ``state`` input entirely (not a silero
    model at all).
    """

    #: Sentinel distinguishing "default v5 shape" from "no state input at all".
    _NO_STATE_INPUT: object = object()

    def __init__(
        self,
        probabilities: list[float],
        state_shape: list[int | str | None] | object = _NO_STATE_INPUT,
    ) -> None:
        self._probs = iter(probabilities)
        self.seen_states: list[object] = []
        self.feeds: list[dict[str, object]] = []
        # Default mirrors the real v5 model: dynamic batch dim (None), fixed
        # dim0=2 and dim2=128. ``None`` would mean "no state input"; the default
        # sentinel keeps that case explicit (state_shape=None drops the input).
        self._state_shape: list[int | str | None] | None
        if state_shape is _FakeSession._NO_STATE_INPUT:
            self._state_shape = [2, None, 128]
        elif state_shape is None:
            self._state_shape = None
        else:
            assert isinstance(state_shape, list)
            self._state_shape = state_shape

    def get_inputs(self) -> list[_NodeArg]:
        """Return the silero ONNX inputs (input, sr, and state unless dropped)."""
        # _FakeNodeArg satisfies the _NodeArg Protocol (name/shape attributes).
        inputs: list[_NodeArg] = [
            _FakeNodeArg("input", [None, None]),
            _FakeNodeArg("sr", []),
        ]
        if self._state_shape is not None:
            inputs.append(_FakeNodeArg("state", self._state_shape))
        return inputs

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


# --- silero v5 shape guard (offline; no ml extra needed) -----------------
#
# An operator who grabs the ``deepghs/silero-vad-onnx`` HuggingFace mirror
# gets a silero v4 model whose recurrent state's last dim is ``64`` rather than
# the ``128`` that v5 needs.  Without a load-time guard the failure surfaces as
# an opaque onnxruntime shape mismatch on the FIRST inference call — too late
# and too cryptic to debug.  _SileroOnnxModel.__init__ must validate
# ``session.get_inputs()`` immediately and raise a human-readable ``ValueError``
# so the operator can act before the call stack is live.
#
# CRITICAL: the REAL silero v5 ONNX reports its ``state`` input shape as
# ``[2, None, 128]`` — the **batch dim (index 1) is DYNAMIC** (onnxruntime
# returns ``None`` or a symbolic ``str`` for a dynamic dim, NOT a fixed ``1``).
# The guard must therefore compare only the LOAD-BEARING fixed dims (dim0==2,
# dim2==128) and treat dim1 as dynamic, otherwise it WRONGLY REJECTS the genuine
# v5 model (the opposite of its purpose).  v4 is identified by dim2==64.


def test_silero_v5_dynamic_batch_dim_constructs_without_error() -> None:
    """The REAL v5 shape ``[2, None, 128]`` (dynamic batch) must PASS the guard.

    onnxruntime reports the silero v5 ``state`` input as ``[2, None, 128]`` — the
    batch dim is dynamic (``None``), not a fixed ``1``. An EXACT match against
    ``(2, 1, 128)`` would wrongly reject the genuine model; the guard must accept
    a length-3 shape whose dim0==2 and dim2==128, treating dim1 as dynamic.
    """
    from hermes_voip.media.vad import _SileroOnnxModel  # noqa: PLC0415

    v5_session = _FakeSession([], state_shape=[2, None, 128])
    # must not raise
    _SileroOnnxModel(v5_session, _FakeNumpy())


def test_silero_v5_symbolic_batch_dim_constructs_without_error() -> None:
    """A v5 ``state`` with a SYMBOLIC (string) dynamic batch dim must PASS.

    onnxruntime can report a dynamic dim as a named symbol (a ``str``, e.g.
    ``"batch"``) rather than ``None``. The guard must treat any non-int dim1 as
    dynamic and still accept the model when dim0==2 and dim2==128.
    """
    from hermes_voip.media.vad import _SileroOnnxModel  # noqa: PLC0415

    v5_session = _FakeSession([], state_shape=[2, "batch_size", 128])
    # must not raise
    _SileroOnnxModel(v5_session, _FakeNumpy())


def test_silero_v5_fixed_batch_dim_constructs_without_error() -> None:
    """A v5 ``state`` reported with a fixed batch dim ``[2, 1, 128]`` must PASS.

    Some export toolchains pin the batch dim to a literal ``1``. dim1 is not
    load-bearing for the version check, so a fixed ``1`` is accepted just like a
    dynamic dim — only dim0==2 and dim2==128 matter.
    """
    from hermes_voip.media.vad import _SileroOnnxModel  # noqa: PLC0415

    v5_session = _FakeSession([], state_shape=[2, 1, 128])
    # must not raise
    _SileroOnnxModel(v5_session, _FakeNumpy())


def test_silero_v4_dynamic_batch_dim_raises_clear_error_at_model_init() -> None:
    """A v4 model with the realistic dynamic batch shape ``[2, None, 64]`` is rejected.

    A v4 model's state last dim is ``64`` (v5 needs ``128``). With a dynamic
    batch dim it reports ``[2, None, 64]``. ``_SileroOnnxModel.__init__`` must
    detect this via ``session.get_inputs()`` and raise a ``ValueError`` that
    names the loaded shape and the incompatible ``deepghs/silero-vad-onnx``
    mirror, at INIT time (not at the first ``__call__``).
    """
    from hermes_voip.media.vad import _SileroOnnxModel  # noqa: PLC0415

    v4_session = _FakeSession([], state_shape=[2, None, 64])
    with pytest.raises(ValueError, match="64") as excinfo:
        _SileroOnnxModel(v4_session, _FakeNumpy())
    # the message must be actionable: name v4, the mirror, and runbook 0002
    message = str(excinfo.value)
    assert "deepghs/silero-vad-onnx" in message
    assert "runbook 0002" in message


def test_silero_v4_fixed_batch_dim_raises_clear_error_at_model_init() -> None:
    """A v4 model reported with a literal batch dim ``[2, 1, 64]`` is also rejected.

    Whether v4 reports a fixed or dynamic batch dim, the last dim ``64`` marks it
    as v4 and the guard must reject it with the same clear, actionable error.
    """
    from hermes_voip.media.vad import _SileroOnnxModel  # noqa: PLC0415

    v4_session = _FakeSession([], state_shape=[2, 1, 64])
    with pytest.raises(ValueError, match="64"):
        _SileroOnnxModel(v4_session, _FakeNumpy())


def test_missing_state_input_raises_clear_error_at_model_init() -> None:
    """An ONNX with no ``state`` input at all is rejected with a clear ValueError.

    A model lacking the recurrent ``state`` input is not a recognisable silero
    model; the guard must say so at init rather than fail cryptically on the
    first inference (which would ``KeyError`` on ``inputs["state"]``).
    """
    from hermes_voip.media.vad import _SileroOnnxModel  # noqa: PLC0415

    no_state_session = _FakeSession([], state_shape=None)
    with pytest.raises(ValueError, match="no 'state' input"):
        _SileroOnnxModel(no_state_session, _FakeNumpy())


def test_silero_state_with_unexpected_rank_raises_clear_error() -> None:
    """A ``state`` whose shape is not rank-3 is rejected (not silently accepted).

    The v5 state is rank-3 ``[2, ?, 128]``. A shape of the wrong length (e.g. a
    flattened ``[256]``) is not a v5 state and must raise at init rather than
    being indexed out of bounds or accepted.
    """
    from hermes_voip.media.vad import _SileroOnnxModel  # noqa: PLC0415

    odd_session = _FakeSession([], state_shape=[256])
    with pytest.raises(ValueError, match="state shape"):
        _SileroOnnxModel(odd_session, _FakeNumpy())


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
