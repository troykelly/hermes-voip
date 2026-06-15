"""The in-process, offline prompt-injection guard (ADR-0009 §2-5, default impl).

This is layer 1 of the defense-in-depth stack — the early-warning detector, *not*
the enforceable control (that is ``providers.policy.gate_tool_call``). It takes a
finalized caller turn, de-obfuscates it (:mod:`hermes_voip.guard.normalize`),
classifies the decoded text, and **grades** the raw score against a tuned
threshold plus two stateful signals — a per-call cumulative score and a
sliding-window rate of suspicious turns — so a caller who probes repeatedly
escalates even when no single turn crosses the high threshold.

The classifier is a dependency-injected callable ``Classifier`` (``str -> float``
in ``0.0..1.0``). That boundary is deliberate:

* it keeps the **untyped** ``onnxruntime`` / tokenizer stack out of the strictly
  typed core (rule 39: no ``Any``) — the ONNX session is built by
  :func:`build_onnx_classifier` and exposed only as the typed ``Classifier``; and
* it makes the guard **model-free to unit-test** (rule 18/33): the grade ladder,
  the fail-open path, and the stateful escalation are tested with a fake callable,
  with no weights and no network — exactly what an offline CI can run.

**Fail policy (ADR-0009).** Any inference failure — the callable raises, returns
NaN/inf, or returns a value outside ``0.0..1.0`` — *fails open* to
``GuardVerdict.RESTRICT`` with ``degraded=True``: the caller is never dropped, the
action surface is clamped read-only by policy, and the error is reported in
``reasons`` (rule 37: handled + logged, never silently swallowed, and **never** a
silent ``ALLOW``).
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from hermes_voip.guard.normalize import NormalizedText, normalize
from hermes_voip.providers.guard import GuardResult, GuardVerdict

__all__ = [
    "Classifier",
    "GuardConfig",
    "OnnxInjectionGuard",
    "build_onnx_classifier",
]

# A prompt-injection classifier: normalized text in, malicious-probability out
# (``0.0..1.0``). Synchronous — the guard awaits it off the event-loop thread.
Classifier = Callable[[str], float]


@dataclass(frozen=True, slots=True)
class GuardConfig:
    """Tuned thresholds for the grade ladder and the stateful escalation signals.

    All scores are malicious-probabilities in ``0.0..1.0``. The single-turn ladder
    is ``score >= refuse_threshold`` -> REFUSE, ``>= restrict_threshold`` ->
    RESTRICT, else ALLOW. Two cumulative signals can escalate an otherwise-low
    turn: the per-call running sum of scores crossing ``cumulative_threshold``, and
    the count of suspicious turns (``score >= suspicious_threshold``) within the
    last ``window`` turns reaching ``window_threshold``.

    Defaults are conservative starting points to be re-tuned against the eval
    harness (ADR-0009); they are not claimed as validated accuracy.

    Attributes:
        restrict_threshold: Single-turn score at/above which a turn is RESTRICT.
        refuse_threshold: Single-turn score at/above which a turn is REFUSE.
        suspicious_threshold: Score at/above which a turn counts toward the window.
        cumulative_threshold: Per-call running-sum of scores that forces escalation.
        window: Number of recent turns the sliding window considers.
        window_threshold: Suspicious-turn count in the window that forces escalation.
    """

    restrict_threshold: float = 0.5
    refuse_threshold: float = 0.85
    suspicious_threshold: float = 0.4
    cumulative_threshold: float = 1.5
    window: int = 5
    window_threshold: int = 3

    def __post_init__(self) -> None:
        """Reject a config whose thresholds are not coherent probabilities."""
        for name in (
            "restrict_threshold",
            "refuse_threshold",
            "suspicious_threshold",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                msg = f"GuardConfig.{name} must be in 0.0..1.0, got {value!r}"
                raise ValueError(msg)
        if self.restrict_threshold > self.refuse_threshold:
            msg = "GuardConfig.restrict_threshold must be <= refuse_threshold"
            raise ValueError(msg)
        if self.window < 1:
            msg = f"GuardConfig.window must be >= 1, got {self.window!r}"
            raise ValueError(msg)
        if self.window_threshold < 1:
            msg = (
                f"GuardConfig.window_threshold must be >= 1, "
                f"got {self.window_threshold!r}"
            )
            raise ValueError(msg)


@dataclass(slots=True)
class _CallState:
    """Per-call stateful signals for cumulative + sliding-window escalation."""

    cumulative: float = 0.0
    recent: deque[bool] = field(default_factory=deque)

    def observe(self, score: float, *, config: GuardConfig) -> None:
        """Fold one screened turn's score into the per-call signals."""
        self.cumulative += score
        if len(self.recent) == config.window:
            self.recent.popleft()
        self.recent.append(score >= config.suspicious_threshold)

    @property
    def suspicious_in_window(self) -> int:
        """Count of suspicious turns currently in the sliding window."""
        return sum(self.recent)


class OnnxInjectionGuard:
    """Graded, stateful prompt-injection guard (ADR-0009 default, in-process).

    Implements ADR-0004's ``InjectionGuard`` Protocol. Construct with a
    ``Classifier`` callable (inject a fake in tests; use
    :func:`build_onnx_classifier` in production). Per-call state is keyed by
    ``call_id`` and lives for the process; it escalates a repeat prober and is the
    input to the fail-open ``degraded`` flag policy reads.
    """

    __slots__ = ("_classify", "_config", "_states")

    def __init__(
        self, *, classify: Classifier, config: GuardConfig | None = None
    ) -> None:
        """Create a guard over a classifier callable.

        Args:
            classify: The injection classifier (normalized text -> probability).
            config: Tuned thresholds; the conservative default if omitted.
        """
        self._classify = classify
        self._config = config if config is not None else GuardConfig()
        self._states: dict[str, _CallState] = {}

    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        """Screen one finalized caller turn (ADR-0004 ``InjectionGuard``).

        Normalises ``text``, classifies it off the event-loop thread, grades the
        score against the single-turn ladder and the per-call cumulative +
        sliding-window signals, and returns a graded :class:`GuardResult`. On any
        inference failure it fails open to ``RESTRICT`` + ``degraded`` (never a
        silent ``ALLOW``).

        Args:
            text: The finalized, transcribed caller turn.
            call_id: Scopes the per-call cumulative / window state.

        Returns:
            The graded outcome for this turn.
        """
        normalized = normalize(text)
        try:
            raw = await asyncio.to_thread(self._classify, normalized.screened_text)
        except Exception as exc:  # noqa: BLE001 -- fail-open is the policy (ADR-0009)
            # The error is handled + reported (not swallowed): degrade the session
            # so policy clamps the action surface, and NEVER silently ALLOW.
            return self._fail_open(normalized, reason=f"classifier-error: {exc}")

        if not _is_valid_probability(raw):
            return self._fail_open(
                normalized, reason=f"classifier-invalid-score: {raw!r}"
            )

        return self._grade(raw, normalized, call_id=call_id)

    def _grade(
        self, score: float, normalized: NormalizedText, *, call_id: str
    ) -> GuardResult:
        """Map a valid score + per-call state to a graded verdict."""
        config = self._config
        state = self._states.setdefault(call_id, _CallState())
        state.observe(score, config=config)

        reasons: list[str] = [*normalized.reasons, f"score={score:.4f}"]

        cumulative_hit = state.cumulative >= config.cumulative_threshold
        window_hit = state.suspicious_in_window >= config.window_threshold
        if cumulative_hit:
            reasons.append(f"cumulative={state.cumulative:.4f}")
        if window_hit:
            reasons.append(f"window-suspicious={state.suspicious_in_window}")

        verdict = self._verdict(
            score, cumulative_hit=cumulative_hit, window_hit=window_hit
        )
        return GuardResult(
            verdict=verdict,
            # The audit field carries the full de-obfuscated screening text (all
            # candidates), so a decoded payload is visible to the audit log, not
            # just the surface form.
            normalized_text=normalized.screened_text,
            reasons=tuple(reasons),
            degraded=False,
            score=score,
        )

    def _verdict(
        self, score: float, *, cumulative_hit: bool, window_hit: bool
    ) -> GuardVerdict:
        """The grade ladder: single-turn score, escalated by the stateful signals."""
        config = self._config
        if score >= config.refuse_threshold:
            return GuardVerdict.REFUSE
        # Repeated probing escalates a still-borderline caller: once either
        # cumulative or window signal trips, a sub-REFUSE turn is pushed up.
        if cumulative_hit or window_hit:
            if score >= config.restrict_threshold:
                return GuardVerdict.REFUSE
            return GuardVerdict.RESTRICT
        if score >= config.restrict_threshold:
            return GuardVerdict.RESTRICT
        return GuardVerdict.ALLOW

    def _fail_open(self, normalized: NormalizedText, *, reason: str) -> GuardResult:
        """Build the fail-open result: RESTRICT + degraded, never ALLOW (ADR-0009)."""
        return GuardResult(
            verdict=GuardVerdict.RESTRICT,
            normalized_text=normalized.canonical,
            reasons=(*normalized.reasons, reason, "fail-open-degraded"),
            degraded=True,
            score=0.0,
        )


def _is_valid_probability(value: float) -> bool:
    """True iff ``value`` is a finite probability in ``0.0..1.0`` (not NaN/inf)."""
    return math.isfinite(value) and 0.0 <= value <= 1.0


def build_onnx_classifier(model_dir: str) -> Classifier:
    """Build the production :data:`Classifier` from a pinned ONNX DeBERTa artifact.

    Loads the ``protectai/deberta-v3-base-prompt-injection-v2`` ONNX model and its
    tokenizer from ``model_dir`` (ADR-0009: a pinned, cached artifact — no runtime
    download, so no egress of caller text), and returns a synchronous callable that
    maps normalized text to the model's malicious-class probability.

    This is the **only** place the untyped ``onnxruntime`` / tokenizer stack is
    touched; everything it returns is the typed ``Classifier`` the guard consumes,
    so ``mypy --strict`` (no ``Any``) holds for the rest of the module. The ``ml``
    extra (``onnxruntime``) must be installed and the artifact present; both are
    asserted by raising on import/load failure (rule 37), never by degrading here.

    Args:
        model_dir: Filesystem directory holding the pinned ONNX model + tokenizer.

    Returns:
        A :data:`Classifier` over the loaded model.
    """
    # Deferred so importing this module (and the guard package) never pulls the
    # untyped onnxruntime/tokenizers edge — only constructing the real classifier
    # does, which is when the ``ml`` extra is required.
    from hermes_voip.guard._onnx_runtime import (  # noqa: PLC0415
        load_deberta_classifier,
    )

    return load_deberta_classifier(model_dir)
