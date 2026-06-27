"""Tests for hermes_voip.guard._onnx_runtime — config loading and error handling.

``_injection_label_index`` is a pure-Python helper that reads and parses
``config.json``; it does **not** require onnxruntime, so these tests run in the
default pytest suite without any importorskip guard.  The full model smoke test
(which does need the pinned ONNX artifact) lives in ``test_guard_onnx_model.py``
and uses its own importorskip there.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

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


def test_non_object_json_list_returns_default_index() -> None:
    """Valid JSON that is a list (not an object) must default to index 1.

    Calling .get() on a list raises AttributeError; the docstring says an
    "unrecognised config defaults to 1" so a non-dict JSON value must follow
    the same code path as a missing or unrecognised config, not crash.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "config.json"
        config_path.write_text(json.dumps([]), encoding="utf-8")
        # Must NOT raise AttributeError; must return the default.
        assert _injection_label_index(config_path) == 1


def test_non_object_json_string_returns_default_index() -> None:
    """Valid JSON that is a string (not an object) must default to index 1."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "config.json"
        config_path.write_text(json.dumps("hello"), encoding="utf-8")
        assert _injection_label_index(config_path) == 1


def test_non_object_json_number_returns_default_index() -> None:
    """Valid JSON that is a number (not an object) must default to index 1."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "config.json"
        config_path.write_text(json.dumps(42), encoding="utf-8")
        assert _injection_label_index(config_path) == 1


def test_invalid_utf8_bytes_raises_value_error_with_path() -> None:
    """Binary/garbage file raises ValueError with path; wraps UnicodeDecodeError.

    read_text(encoding='utf-8') raises UnicodeDecodeError on a non-UTF-8 file.
    The docstring claims "corrupt/unparseable" configs are handled as ValueError;
    UnicodeDecodeError is a corrupt-config situation and must be wrapped the same
    way as JSONDecodeError — not left unwrapped as a bare UnicodeDecodeError.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "config.json"
        # Bytes that are definitively not valid UTF-8.
        config_path.write_bytes(b"\xff\xfe\x00")

        with pytest.raises(ValueError, match=r"corrupt config\.json") as exc_info:
            _injection_label_index(config_path)

        assert str(config_path) in str(exc_info.value)
        # The original UnicodeDecodeError is preserved as __cause__.
        assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)
