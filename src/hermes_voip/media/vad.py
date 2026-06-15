"""Voice-activity detection with silero-vad (ADR-0008 Phase 1).

The cascaded pipeline (ADR-0003) needs to know **when the caller starts and stops
speaking**: telephony is a continuous stream with no message boundaries. This
module runs ``silero-vad`` per fixed-size window over the inbound :class:`PcmFrame`
stream and emits speech **ONSET** / **OFFSET** edge events, which the endpointer
(:mod:`hermes_voip.media.endpoint`) turns into end-of-turn marks.

Three properties matter:

* **Native 8 kHz or 16 kHz.** silero-vad runs at *either* rate (256 samples per
  window at 8 kHz, 512 at 16 kHz). We default to 16 kHz only to share one
  resampled stream with STT (ADR-0006) — not because 8 kHz is unsupported. The
  long-corrected record: 8 kHz is fully supported (older notes claiming otherwise
  were wrong).
* **Fixed windows.** silero requires an exact window size; ``feed`` buffers the
  incoming PCM and runs one inference per full window, carrying any partial window
  to the next call so a stream fed in arbitrary chunk sizes produces identical
  edges to one fed window-aligned.
* **Hysteresis.** A single ``threshold`` would chatter around the boundary, so we
  enter speech at ``threshold`` and only leave it once the probability drops below
  a lower ``exit_threshold`` (silero's recipe uses ``threshold - 0.15``). This is
  what gives the endpointer a clean offset to time silence from.

The model is **dependency-injected** as a :class:`VadModel` callable so the pure
edge state machine is unit-tested offline with a fake returning canned
probabilities — CI never loads or downloads a neural net (rule 18/33).
:func:`load_silero_model` builds the real onnxruntime-backed callable for the live
path and is exercised only by a skipped-when-uncached smoke test.

Off-loop bridging: the live detector runs inside the media engine's worker thread
and is bridged onto the event loop with :func:`hermes_voip.aio.stream_from_thread`
(ADR-0008), so the per-window onnxruntime call never blocks the loop. ``feed`` and
the state machine here are deliberately synchronous and allocation-light to fit
the per-frame CPU budget (rule 22).
"""

from __future__ import annotations

import importlib
import math
import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame

# numpy / onnxruntime are NOT statically imported here: the optional ``ml`` extra
# is absent in the default gate (and its mypy job), and onnxruntime ships no
# ``py.typed`` even when present — so a static ``import`` would error differently
# in each environment. Like ``hermes_voip.providers.onnx_compat``, the live path
# resolves them dynamically (``importlib.import_module``) inside the one factory
# that needs them, and every numpy/ort reference is held as ``object`` at the
# boundaries. The pure detector and state machine above carry no ML dependency.

__all__ = [
    "SILERO_WINDOW_SAMPLES",
    "SpeechEdge",
    "VadEvent",
    "VadModel",
    "VoiceActivityDetector",
    "load_silero_model",
]

#: silero-vad's required window size (mono samples) per native sample rate. The
#: model accepts *exactly* these lengths — 256 samples at 8 kHz, 512 at 16 kHz
#: (both 32 ms) — and rejects any other window. The detector slices to these.
SILERO_WINDOW_SAMPLES: Final[dict[int, int]] = {8_000: 256, 16_000: 512}

#: silero's recommended gap between the speech-entry and speech-exit thresholds,
#: giving the hysteresis dead band that stops edge chatter at the boundary.
_DEFAULT_HYSTERESIS_GAP: Final[float] = 0.15


class SpeechEdge(Enum):
    """A transition of the inbound stream's speech/silence state."""

    ONSET = auto()
    OFFSET = auto()


@dataclass(frozen=True, slots=True)
class VadEvent:
    """A speech onset/offset edge detected on one model window.

    Attributes:
        edge: Which transition occurred (ONSET = speech began, OFFSET = ended).
        frame_index: The window's ordinal on this detector's monotonic window
            clock (zero-based; one per model window, restarting at ``reset``).
        probability: The model's speech probability for the window, in
            ``0.0..1.0`` — carried so an endpointer/barge-in debounce can weigh
            confidence, not just the binary edge.
    """

    edge: SpeechEdge
    frame_index: int
    probability: float


@runtime_checkable
class VadModel(Protocol):
    """A per-window speech-probability function (the injected silero engine).

    One call scores exactly one window of PCM16-LE mono at ``sample_rate`` Hz
    (length ``SILERO_WINDOW_SAMPLES[sample_rate] * 2`` bytes) and returns the
    speech probability in ``0.0..1.0``. Implementations are stateful across calls
    (silero carries an LSTM hidden state); :meth:`VoiceActivityDetector.reset`
    calls :meth:`reset` here when present so a new call starts clean.
    """

    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        """Return the speech probability for one full window of PCM16 mono."""
        ...


class VoiceActivityDetector:
    """Per-window speech detector over mono PCM (silero-vad, 8 kHz or 16 kHz).

    Feed it :class:`PcmFrame` chunks of any length at the configured rate; it
    slices them into silero windows, scores each via the injected
    :class:`VadModel`, and yields :class:`VadEvent` onset/offset edges with
    hysteresis. State (the partial-window buffer, the in-speech flag, the window
    ordinal, and the model's own state) persists across ``feed`` calls until
    :meth:`reset`.
    """

    def __init__(
        self,
        *,
        model: VadModel,
        sample_rate_hz: int = 16_000,
        threshold: float = 0.5,
        exit_threshold: float | None = None,
    ) -> None:
        """Create a detector.

        Args:
            model: The injected per-window speech-probability function. Inject a
                fake in tests; use :func:`load_silero_model` for the live path.
            sample_rate_hz: ``8000`` or ``16000`` — both native silero rates. The
                frames fed to :meth:`feed` must carry this exact rate.
            threshold: Speech-entry probability cutoff in ``[0.0, 1.0]`` (sourced
                from ``MediaConfig.vad_threshold``).
            exit_threshold: Speech-exit cutoff; speech ends once probability drops
                below it. Defaults to ``max(0.0, threshold - 0.15)`` (silero's
                hysteresis recipe). Must be finite and in ``[0.0, threshold]`` —
                a NaN, infinite, or negative cutoff can never be crossed and
                would mean speech never ends.

        Raises:
            ValueError: If the rate is not 8000/16000, ``threshold`` is outside
                ``[0.0, 1.0]``, or ``exit_threshold`` is not finite and within
                ``[0.0, threshold]``.
        """
        if sample_rate_hz not in SILERO_WINDOW_SAMPLES:
            msg = f"sample_rate_hz must be 8000 or 16000, got {sample_rate_hz}"
            raise ValueError(msg)
        if not 0.0 <= threshold <= 1.0:
            msg = f"threshold must be in [0.0, 1.0], got {threshold}"
            raise ValueError(msg)
        resolved_exit = (
            max(0.0, threshold - _DEFAULT_HYSTERESIS_GAP)
            if exit_threshold is None
            else exit_threshold
        )
        # exit_threshold must be a real, crossable cutoff. NaN (``p < nan`` is
        # always False) and negatives (probabilities are >= 0.0, never crossable)
        # would silently mean speech never ends; reject both, plus infinities and
        # anything above ``threshold`` (which would re-onset chatter). ``not (0.0
        # <= x <= threshold)`` also catches NaN, since every NaN comparison is
        # False; ``math.isfinite`` then keeps the message specific for infinities.
        if not math.isfinite(resolved_exit) or not 0.0 <= resolved_exit <= threshold:
            msg = (
                f"exit_threshold must be finite and in [0.0, threshold], "
                f"got {resolved_exit} (threshold {threshold})"
            )
            raise ValueError(msg)
        self._model = model
        self._sample_rate = sample_rate_hz
        self._threshold = threshold
        self._exit_threshold = resolved_exit
        self._window_bytes = SILERO_WINDOW_SAMPLES[sample_rate_hz] * (
            PCM16_BYTES_PER_SAMPLE
        )
        self._buffer = bytearray()
        self._in_speech = False
        self._next_index = 0

    def feed(self, frame: PcmFrame) -> Iterator[VadEvent]:
        """Push one PCM frame; yield any onset/offset edges it produced.

        The frame may be any length; its bytes are appended to the internal
        buffer and consumed one silero window at a time. A trailing partial
        window is retained for the next call (so chunk boundaries never change
        the result). The returned iterator is fully materialised before return —
        callers may collect or iterate it freely.

        Args:
            frame: PCM16-LE mono audio at this detector's configured rate.

        Returns:
            An iterator of :class:`VadEvent` edges, in window order.

        Raises:
            ValueError: If the frame's rate differs from the configured rate, or
                its payload is not a whole number of 16-bit samples.
        """
        if frame.sample_rate != self._sample_rate:
            msg = f"frame rate {frame.sample_rate} != detector rate {self._sample_rate}"
            raise ValueError(msg)
        if len(frame.samples) % PCM16_BYTES_PER_SAMPLE != 0:
            msg = (
                f"PCM16 frame must be whole 16-bit samples, "
                f"got {len(frame.samples)} bytes"
            )
            raise ValueError(msg)
        self._buffer.extend(frame.samples)
        events: list[VadEvent] = []
        while len(self._buffer) >= self._window_bytes:
            window = bytes(self._buffer[: self._window_bytes])
            del self._buffer[: self._window_bytes]
            edge = self._score_window(window)
            if edge is not None:
                events.append(edge)
            self._next_index += 1
        return iter(events)

    def _score_window(self, window: bytes) -> VadEvent | None:
        """Score one full window and return an edge if the state flipped."""
        probability = self._model(window, self._sample_rate)
        index = self._next_index
        if not self._in_speech and probability >= self._threshold:
            self._in_speech = True
            return VadEvent(SpeechEdge.ONSET, index, probability)
        if self._in_speech and probability < self._exit_threshold:
            self._in_speech = False
            return VadEvent(SpeechEdge.OFFSET, index, probability)
        return None

    def reset(self) -> None:
        """Drop all per-call state: buffer, speech flag, ordinal, model state.

        Call between calls so a new conversation starts at window ordinal 0 in
        the silence state with no leftover audio and a fresh model hidden state.
        """
        self._buffer.clear()
        self._in_speech = False
        self._next_index = 0
        model_reset = getattr(self._model, "reset", None)
        if callable(model_reset):
            model_reset()


# --- live silero-vad model (onnxruntime); never exercised by the CI gate ----

#: The pinned silero-vad ONNX model filename. The weights are NOT vendored (rule
#: 33: no model blobs in git); the live path resolves a locally-cached copy and
#: raises ``FileNotFoundError`` if absent (CI never downloads — it skips).
_SILERO_MODEL_FILENAME: Final[str] = "silero_vad.onnx"

#: Env var naming a directory holding ``silero_vad.onnx`` for the live path.
_SILERO_MODEL_DIR_ENV: Final[str] = "HERMES_VOIP_VAD_MODEL_DIR"


#: silero v5's recurrent state shape: a single (2, 1, 128) float32 tensor carried
#: between windows. Reset to zeros starts a fresh utterance.
_SILERO_STATE_SHAPE: Final[tuple[int, int, int]] = (2, 1, 128)

#: Full-scale divisor mapping PCM16 to the float32 [-1.0, 1.0) range silero wants.
_PCM16_FULL_SCALE: Final[float] = 32768.0


class _SileroOnnxModel:
    """A :class:`VadModel` backed by the silero-vad ONNX graph via onnxruntime.

    silero-vad is recurrent: each inference takes the window plus the carried LSTM
    state and returns the speech probability and the next state. We hold the state
    here and thread it through every call, resetting it on :meth:`reset` so a new
    utterance starts clean (matching the pure detector's ``reset``).

    numpy/onnxruntime are unresolved to the type checker (optional ``ml`` extra,
    absent in the gate), so the session, the numpy module, and the state tensor
    are held as ``object`` at the boundaries; the array math lives entirely in
    :meth:`__call__`. The factory :func:`load_silero_model` constructs this.
    """

    def __init__(self, session: object, numpy_module: object) -> None:
        self._session = session
        self._np = numpy_module
        self._state: object = self._zero_state()

    def _zero_state(self) -> object:
        return self._np.zeros(_SILERO_STATE_SHAPE, dtype=self._np.float32)  # type: ignore[attr-defined]

    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        np = self._np
        samples = (
            np.frombuffer(window_pcm16, dtype="<i2").astype(np.float32)  # type: ignore[attr-defined]
            / _PCM16_FULL_SCALE
        )
        inputs = {
            "input": samples.reshape(1, -1),
            "state": self._state,
            "sr": np.array(sample_rate, dtype=np.int64),  # type: ignore[attr-defined]
        }
        # onnxruntime's InferenceSession.run is dynamically typed; the silero
        # contract is fixed: output 0 = speech probability, output 1 = next state.
        out = self._session.run(None, inputs)  # type: ignore[attr-defined]
        self._state = out[1]
        return float(out[0].reshape(-1)[0])

    def reset(self) -> None:
        self._state = self._zero_state()


def load_silero_model(
    sample_rate_hz: int = 16_000, *, model_dir: Path | str | None = None
) -> VadModel:
    """Build the live silero-vad :class:`VadModel` from a locally-cached graph.

    Resolves ``silero_vad.onnx`` from (in order) the ``model_dir`` argument, then
    the ``HERMES_VOIP_VAD_MODEL_DIR`` environment variable. The weights are never
    vendored or downloaded here (rule 33); a missing file raises
    ``FileNotFoundError`` so the offline smoke test skips rather than fetching.

    Args:
        sample_rate_hz: The native rate the returned model will score at.
        model_dir: Optional explicit directory holding ``silero_vad.onnx``.

    Returns:
        A stateful :class:`VadModel` running the ONNX graph via onnxruntime.

    Raises:
        ValueError: If ``sample_rate_hz`` is not a native silero rate.
        FileNotFoundError: If the model file cannot be located.
        ImportError: If the optional ``ml`` extra (onnxruntime/numpy) is absent.
    """
    if sample_rate_hz not in SILERO_WINDOW_SAMPLES:
        msg = f"sample_rate_hz must be 8000 or 16000, got {sample_rate_hz}"
        raise ValueError(msg)
    model_path = _resolve_model_path(model_dir)
    # Resolved dynamically so neither the no-ml gate nor the (py.typed-less)
    # onnxruntime stub-check trips the type checker; raises ImportError if absent.
    numpy_module = importlib.import_module("numpy")
    ort = importlib.import_module("onnxruntime")
    options = ort.SessionOptions()
    # Single-threaded: the media engine bridges this onto its own worker thread
    # (ADR-0008) and runs one window at a time, so extra ORT threads only add
    # contention and scheduling jitter on the per-frame budget (rule 22).
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(
        str(model_path),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    return _SileroOnnxModel(session, numpy_module)


def _resolve_model_path(model_dir: Path | str | None) -> Path:
    """Locate ``silero_vad.onnx`` or raise ``FileNotFoundError`` (no download)."""
    search: list[Path] = []
    if model_dir is not None:
        search.append(Path(model_dir))
    env_dir = os.environ.get(_SILERO_MODEL_DIR_ENV)
    if env_dir:
        search.append(Path(env_dir))
    for directory in search:
        candidate = directory / _SILERO_MODEL_FILENAME
        if candidate.is_file():
            return candidate
    locations = ", ".join(str(d) for d in search) or "(no model dir configured)"
    msg = (
        f"{_SILERO_MODEL_FILENAME} not found in {locations}; set "
        f"{_SILERO_MODEL_DIR_ENV} to a directory holding the cached silero-vad "
        f"model (it is never downloaded here)"
    )
    raise FileNotFoundError(msg)
