"""The untyped ONNX/tokenizer edge for the injection guard (ADR-0009 default).

This module is the **only** place the VoIP plugin touches ``onnxruntime`` and the
HuggingFace ``tokenizers`` runtime — both ship without type stubs, so they are
quarantined here behind one typed function, :func:`load_deberta_classifier`, which
returns the plain :data:`hermes_voip.guard.onnx.Classifier` (``str -> float``).
Keeping the untyped surface in this single file lets the rest of the guard stay
clean under ``mypy --strict`` with no project-wide ``Any`` (rule 39).

The two stub-less packages are loaded with :func:`importlib.import_module` rather
than a bare ``import`` (which would need a ``# type: ignore`` whose code differs
between the no-ml gate — ``import-not-found`` — and an ml env — ``import-untyped``,
where strict then flags the *other* ignore as unused). ``import_module`` returns a
typed :class:`~types.ModuleType`, so ``mypy --strict`` is clean in **both** envs
with no suppression: every value pulled across this edge is coerced to a concrete
type (``float``/``int``) before it leaves the module.

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

import importlib
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

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
    onnxruntime = _require("onnxruntime")
    tokenizers = _require("tokenizers")

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
    # ``i.name`` crosses the untyped onnxruntime edge; coerce to ``str`` so the
    # set is concretely ``set[str]``, not an implicit-``Any`` set.
    input_names: set[str] = {str(i.name) for i in session.get_inputs()}

    def classify(text: str) -> float:
        # Each per-token value crosses the untyped tokenizer edge; coerce to ``int``
        # so the feeds dict is concretely typed, never an implicit-``Any`` list.
        encoding = tokenizer.encode(text)
        feeds: dict[str, list[list[int]]] = {
            "input_ids": [[int(i) for i in encoding.ids]]
        }
        if "attention_mask" in input_names:
            feeds["attention_mask"] = [[int(m) for m in encoding.attention_mask]]
        if "token_type_ids" in input_names:
            feeds["token_type_ids"] = [[int(t) for t in encoding.type_ids]]
        outputs = session.run(None, feeds)
        # ``outputs[0]`` is the logits array (untyped numpy at this edge); the
        # first row is this single sequence. Coerce to plain floats so the pure
        # softmax helper sees only ``list[float]``.
        logits = [float(value) for value in outputs[0][0]]
        return _softmax_index(logits, injection_index)

    return classify


def _require(module_name: str) -> ModuleType:
    """Import a stub-less ml-extra module by name, or raise a clear ``ImportError``.

    Using :func:`importlib.import_module` (not a bare ``import``) is what keeps
    this edge free of a ``# type: ignore`` that would be correct in only one of
    the two gates (rule 39): the returned :class:`~types.ModuleType` is typed, so
    ``mypy --strict`` never tries to resolve the absent/untyped package. A missing
    ``ml`` extra **propagates** (rule 37) as a deployment error, named for the
    operator — it is not the guard's runtime fail-open (that covers inference
    errors on a *loaded* model, not a model backend that was never installed).
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        msg = (
            f"the injection guard's ONNX backend needs the 'ml' extra "
            f"(onnxruntime + tokenizers); {module_name!r} is not installed"
        )
        raise ImportError(msg) from exc


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
