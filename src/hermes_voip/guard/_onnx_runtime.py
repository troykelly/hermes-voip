"""The untyped ONNX/tokenizer edge for the injection guard (ADR-0009 default).

This module is the **only** place the VoIP plugin touches ``onnxruntime`` and the
HuggingFace ``tokenizers`` runtime — both ship without type stubs, so they are
quarantined here behind one typed function, :func:`load_deberta_classifier`, which
returns the plain :data:`hermes_voip.guard.onnx.Classifier` (``str -> float``).
Keeping the untyped surface in this single file lets the rest of the guard stay
clean under ``mypy --strict`` with no project-wide ``Any`` (rule 39).

It loads ``protectai/deberta-v3-base-prompt-injection-v2`` from a **pinned, local**
directory (ADR-0009): an exported ONNX graph (``model.onnx``) plus the fast
tokenizer (``tokenizer.json``). Nothing is downloaded at run time, so no caller
text leaves the box. The model is a 2-class sequence classifier
(``{SAFE, INJECTION}``); the returned callable softmaxes the logits and reports
the INJECTION-class probability.

Import-time / load-time failure **propagates** (rule 37): a missing ``ml`` extra or
a missing artifact is a deployment error to surface, not something to degrade
silently — the guard's *runtime* fail-open (ADR-0009) covers inference errors on a
loaded model, not a model that was never present.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_voip.guard.onnx import Classifier

__all__ = ["load_deberta_classifier"]

# The pinned artifact's file names (ADR-0009 records repo/revision/sha in the
# manifest licence-gate; these are the file names that gate pins and we load).
_ONNX_FILE = "model.onnx"
_TOKENIZER_FILE = "tokenizer.json"
_CONFIG_FILE = "config.json"

# DeBERTa-v3-base accepts up to 512 tokens; we truncate longer turns (a caller
# turn is short, but a pasted/obfuscated payload can be long).
_MAX_TOKENS = 512


def load_deberta_classifier(model_dir: str) -> Classifier:
    """Load the pinned ONNX DeBERTa classifier from ``model_dir``.

    Args:
        model_dir: Directory holding ``model.onnx`` + ``tokenizer.json`` (+
            ``config.json`` for the label order).

    Returns:
        A synchronous ``Classifier`` mapping text to the INJECTION-class
        probability in ``0.0..1.0``.

    Raises:
        ImportError: The ``ml`` extra (onnxruntime / tokenizers) is not installed.
        FileNotFoundError: A required pinned artifact file is absent.
    """
    try:
        # onnxruntime / tokenizers ship no type stubs; this is the single file
        # where that untyped surface is allowed (rule 17/39 — justified, isolated).
        import onnxruntime  # type: ignore[import-untyped]  # noqa: PLC0415
        import tokenizers  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        msg = (
            "The injection guard's ONNX backend needs the 'ml' extra "
            "(onnxruntime + tokenizers); install it to use the default guard."
        )
        raise ImportError(msg) from exc

    base = Path(model_dir)
    onnx_path = base / _ONNX_FILE
    tokenizer_path = base / _TOKENIZER_FILE
    for required in (onnx_path, tokenizer_path):
        if not required.is_file():
            msg = f"injection-guard model artifact missing: {required}"
            raise FileNotFoundError(msg)

    injection_index = _injection_label_index(base / _CONFIG_FILE)

    tokenizer = tokenizers.Tokenizer.from_file(str(tokenizer_path))
    tokenizer.enable_truncation(max_length=_MAX_TOKENS)
    session = onnxruntime.InferenceSession(
        str(onnx_path), providers=["CPUExecutionProvider"]
    )
    input_names = {i.name for i in session.get_inputs()}

    def classify(text: str) -> float:
        encoding = tokenizer.encode(text)
        feeds: dict[str, list[list[int]]] = {"input_ids": [list(encoding.ids)]}
        if "attention_mask" in input_names:
            feeds["attention_mask"] = [list(encoding.attention_mask)]
        if "token_type_ids" in input_names:
            feeds["token_type_ids"] = [list(encoding.type_ids)]
        outputs = session.run(None, feeds)
        # ``outputs[0]`` is the logits array (untyped numpy at this edge); the
        # first row is this single sequence. Coerce to plain floats so the pure
        # softmax helper sees only ``list[float]``.
        logits = [float(value) for value in outputs[0][0]]
        return _softmax_index(logits, injection_index)

    return classify


def _injection_label_index(config_path: Path) -> int:
    """Return the logit index of the INJECTION class from ``config.json``.

    DeBERTa classifier configs carry ``id2label``; the injection class is the
    label whose name marks the positive/unsafe class. If the config is absent or
    unrecognised we default to index 1 (the conventional positive class for these
    2-class injection detectors).
    """
    if not config_path.is_file():
        return 1
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    id2label = raw.get("id2label")
    if not isinstance(id2label, dict):
        return 1
    for key, label in id2label.items():
        if isinstance(label, str) and label.strip().upper() in {
            "INJECTION",
            "UNSAFE",
            "LABEL_1",
            "MALICIOUS",
        }:
            return int(key)
    return 1


def _softmax_index(logits: list[float], index: int) -> float:
    """Softmax ``logits`` and return the probability mass at ``index``."""
    if not logits:
        return 0.0
    hi = max(logits)
    exps = [math.exp(v - hi) for v in logits]
    total = math.fsum(exps)
    if total <= 0.0:
        return 0.0
    safe_index = index if 0 <= index < len(exps) else len(exps) - 1
    return exps[safe_index] / total
