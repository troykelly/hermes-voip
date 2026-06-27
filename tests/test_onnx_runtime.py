"""Tests for hermes_voip.guard._onnx_runtime — loading and error handling.

These tests exercise the untyped ONNX/tokenizer edge (ADR-0009), particularly
error handling in the config loader. The module is only loaded when the optional
``ml`` extra is installed; tests that import it skip otherwise.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("onnxruntime", reason="ml extra not installed")

from hermes_voip.guard._onnx_runtime import _injection_label_index


def test_corrupt_config_json_raises_value_error_with_path() -> None:
    """ValueError with path when config.json is corrupt; preserves JSONDecodeError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "config.json"
        # Write invalid JSON to simulate corruption.
        config_path.write_text("not json{", encoding="utf-8")

        # Expect ValueError with the path in the message.
        with pytest.raises(ValueError, match=r"corrupt config\.json") as exc_info:
            _injection_label_index(config_path)

        # The error message contains the path.
        assert str(config_path) in str(exc_info.value)
        # The original json.JSONDecodeError is preserved as __cause__.
        assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)
