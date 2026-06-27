"""PEP 561 py.typed + trove-classifiers packaging assertions.

These tests verify the packaging configuration is correct for:

1. ``py.typed`` marker (PEP 561): the empty sentinel file MUST be shipped
   inside the built wheel so downstream type-checkers know this package
   exports type information.  The hatchling build backend does NOT include
   non-.py files by default — a missing artifacts/force-include declaration
   causes ``py.typed`` to silently disappear from the wheel even though it
   exists in the source tree (the prior lane's tautology bug).

2. Required trove classifiers in pyproject.toml [project].classifiers:
   - ``Typing :: Typed`` — signals PEP 561 typed package on PyPI
   - ``Programming Language :: Python :: 3.13`` — documents min Python
   - ``Topic :: Communications :: Telephony`` — domain classification
   - ``License :: OSI Approved :: Apache Software License`` — licence

The tests are wheel-build assertions (NOT source-tree tautologies): they
build the wheel into a temp dir inside this worktree and inspect the zipfile
content and the METADATA file it embeds.  A missing ``py.typed`` inside the
wheel is detected even if the file exists on disk; a missing classifier is
detected even if it appears in pyproject.toml (because building verifies that
hatchling actually picks it up).

These tests run as part of the full pytest gate (they are slow: ~5-15 s for
the uv build subprocess) but produce definitive wheel-level evidence.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Repo locations
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DIST_DIR = _REPO_ROOT / "dist_test_packaging"

# Required classifiers — each must appear verbatim in the wheel METADATA.
_REQUIRED_CLASSIFIERS: list[str] = [
    "Typing :: Typed",
    "Programming Language :: Python :: 3.13",
    "Topic :: Communications :: Telephony",
    "License :: OSI Approved :: Apache Software License",
]


# ---------------------------------------------------------------------------
# Fixture: build wheel once per session into a worktree-local dist dir
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def built_wheel() -> Path:
    """Build the wheel into ``dist_test_packaging/`` and return its path.

    Uses ``uv build --wheel`` so the same toolchain that CI uses is exercised.
    The output dir is worktree-local (not /tmp) per AGENTS.md rule 10.
    """
    _DIST_DIR.mkdir(exist_ok=True)
    # Remove any stale wheels so we always verify a fresh build.
    for old in _DIST_DIR.glob("*.whl"):
        old.unlink()

    uv_bin = shutil.which("uv")
    assert uv_bin is not None, "uv binary not found on PATH"
    result = subprocess.run(  # noqa: S603 — uv_bin is resolved from PATH via shutil.which
        [uv_bin, "build", "--wheel", "--out-dir", str(_DIST_DIR)],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"uv build --wheel failed (exit {result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    wheels = list(_DIST_DIR.glob("*.whl"))
    assert wheels, "uv build succeeded but produced no .whl file"
    return wheels[0]


# ---------------------------------------------------------------------------
# (1) py.typed must be present inside the wheel zipfile
# ---------------------------------------------------------------------------


def test_wheel_contains_py_typed(built_wheel: Path) -> None:
    """``hermes_voip/py.typed`` must be present inside the built wheel.

    This test is NOT a source-tree tautology: it inspects the zipfile.
    If hatchling's artifacts list does not include ``py.typed``, the file
    is present in ``src/hermes_voip/`` but ABSENT from the wheel — and this
    test will correctly FAIL even though the file exists on disk.
    """
    with zipfile.ZipFile(built_wheel) as zf:
        names = zf.namelist()

    # The wheel path is ``hermes_voip/py.typed`` (hatchling strips the src/ prefix).
    has_py_typed = any(n.endswith("hermes_voip/py.typed") for n in names)
    assert has_py_typed, (
        f"py.typed is NOT inside the built wheel {built_wheel.name}.\n"
        f"Wheel contents: {[n for n in names if 'hermes_voip' in n]}\n"
        "Fix: add 'src/hermes_voip/py.typed' to "
        "[tool.hatch.build.targets.wheel] artifacts in pyproject.toml"
    )


# ---------------------------------------------------------------------------
# (2) Required classifiers must appear in the wheel METADATA
# ---------------------------------------------------------------------------


def _read_wheel_metadata(wheel: Path) -> str:
    """Return the METADATA file content from inside the wheel zipfile."""
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        metadata_paths = [n for n in names if n.endswith("/METADATA")]
        assert metadata_paths, f"No METADATA found in wheel; entries: {names[:20]}"
        # There should be exactly one METADATA per wheel.
        with zf.open(metadata_paths[0]) as fh:
            return fh.read().decode("utf-8")


@pytest.mark.parametrize("classifier", _REQUIRED_CLASSIFIERS)
def test_wheel_metadata_has_classifier(built_wheel: Path, classifier: str) -> None:
    """The wheel METADATA must declare each required trove classifier.

    Reads the METADATA file embedded in the wheel zipfile — NOT pyproject.toml
    directly — so this catches classifier declarations that hatchling omits
    from the built artifact (a build-system bug, not a source typo).
    """
    metadata = _read_wheel_metadata(built_wheel)
    # METADATA format: ``Classifier: <value>`` one per line (PEP 566 / 643).
    classifier_lines = [
        line[len("Classifier: ") :].strip()
        for line in metadata.splitlines()
        if line.startswith("Classifier: ")
    ]
    assert classifier in classifier_lines, (
        f"Required classifier {classifier!r} is NOT in the wheel METADATA.\n"
        f"Classifiers found: {classifier_lines}\n"
        f"Fix: add {classifier!r} to [project] classifiers in pyproject.toml"
    )
