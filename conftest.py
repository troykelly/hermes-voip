"""Repository-root pytest configuration.

Excludes the gitignored, read-denied ``.env`` (AGENTS.md rule 34) from collection
so pytest's rootdir walk never calls ``stat`` on it. A ``.env`` provisioned by the
operator/runtime is typically root-owned and unreadable to the test user, so the
default ``pytest_ignore_collect`` probe (``collection_path.is_dir()``) would raise
``PermissionError`` and abort collection. Tests themselves live under ``tests/``
(see ``[tool.pytest.ini_options] testpaths``); nothing at the repo root is a test.
"""

from __future__ import annotations

import pathlib

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_ignore_collect(collection_path: pathlib.Path) -> bool | None:
    """Ignore the read-denied ``.env`` before pytest stats it (rootdir walk)."""
    if collection_path.name == ".env":
        return True
    return None
