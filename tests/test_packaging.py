"""Packaging metadata tests (PEP 427, PEP 561).

Asserts that the wheel declares proper PyPI trove classifiers and ships py.typed
to advertise full type coverage (PEP 561).
"""

import importlib.metadata
from pathlib import Path

import hermes_voip


def test_distribution_advertises_typing_typed_classifier() -> None:
    """Test that the distribution metadata includes 'Typing :: Typed' classifier."""
    dist = importlib.metadata.distribution("hermes-voip")
    classifiers = dist.metadata.get_all("Classifier") or []
    assert any("Typing :: Typed" in c for c in classifiers), (
        f"'Typing :: Typed' not in classifiers: {classifiers}"
    )


def test_distribution_advertises_python_3_13_classifier() -> None:
    """Test that the distribution metadata includes Python 3.13 classifier."""
    dist = importlib.metadata.distribution("hermes-voip")
    classifiers = dist.metadata.get_all("Classifier") or []
    assert any("Programming Language :: Python :: 3.13" in c for c in classifiers), (
        f"'Programming Language :: Python :: 3.13' not in classifiers: {classifiers}"
    )


def test_py_typed_file_exists_in_package() -> None:
    """Test that py.typed marker file exists in the hermes_voip package."""
    pkg_root = Path(hermes_voip.__file__).parent
    py_typed = pkg_root / "py.typed"
    assert py_typed.exists(), f"py.typed marker file not found at {py_typed}"
