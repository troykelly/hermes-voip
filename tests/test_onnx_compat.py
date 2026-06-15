"""Tests for the onnxruntime/sherpa-onnx shared-library compatibility shim.

These run only when the optional ``ml`` extra is installed (``uv sync --extra
ml``); under the default gate (no ``ml``) every check skips. They prove the shim
makes the pip ``sherpa-onnx`` wheel actually loadable against the pinned
``onnxruntime``.
"""

from __future__ import annotations

import importlib
import importlib.util
import pathlib

import pytest

from hermes_voip.providers.onnx_compat import ensure_sherpa_loadable


def _ml_extra_present() -> bool:
    return (
        importlib.util.find_spec("onnxruntime") is not None
        and importlib.util.find_spec("sherpa_onnx") is not None
    )


def test_shim_is_a_no_op_without_the_ml_extra() -> None:
    # The shim must never raise just because the optional deps are absent.
    if _ml_extra_present():
        pytest.skip("ml extra installed; the no-op path is not exercised here")
    ensure_sherpa_loadable()  # returns cleanly with nothing installed


def test_shim_makes_sherpa_onnx_importable() -> None:
    if not _ml_extra_present():
        pytest.skip("ml extra not installed")
    ensure_sherpa_loadable()
    sherpa = importlib.import_module("sherpa_onnx")
    # The native module loaded -> the onnxruntime linkage is resolved.
    assert hasattr(sherpa, "OfflineRecognizer")
    assert hasattr(sherpa, "OfflineTts")


def test_shim_is_idempotent() -> None:
    if not _ml_extra_present():
        pytest.skip("ml extra not installed")
    ensure_sherpa_loadable()
    ensure_sherpa_loadable()  # second call must not raise (symlink already present)
    assert importlib.import_module("sherpa_onnx") is not None


def test_shim_repairs_a_broken_symlink() -> None:
    # A dangling libonnxruntime.so left by a prior install must be repaired, not
    # treated as already-present (codex MEDIUM).
    if not _ml_extra_present():
        pytest.skip("ml extra not installed")
    spec = importlib.util.find_spec("sherpa_onnx")
    assert spec is not None
    assert spec.origin is not None
    link = pathlib.Path(spec.origin).parent / "lib" / "libonnxruntime.so"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(link.parent / "does-not-exist-onnxruntime.so")
    assert link.is_symlink()  # confirmed broken
    assert not link.exists()

    ensure_sherpa_loadable()

    assert link.exists()  # repaired: resolves to a real library now
    assert link.resolve().name.startswith("libonnxruntime.so.")
