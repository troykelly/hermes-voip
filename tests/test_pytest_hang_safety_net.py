"""Guard the global pytest hang safety-net configuration.

``pyproject.toml`` previously set no per-test timeout in
``[tool.pytest.ini_options]``, so a genuinely-hung test (e.g. an asyncio
socket ``await`` that never resolves) could block the local gate AND CI
indefinitely with no diagnostic -- observed 2026-07-03, filed in
``docs/backlog.md`` under "Test / CI robustness".

pytest's built-in ``faulthandler_timeout`` option closes this gap with no new
dependency (rule 33 -- deterministic builds, no floating/extra deps): once a
single test has been running longer than the bound, pytest dumps the stacks
of every running thread to stderr, naming the hung test, but does NOT fail a
legitimately slow-but-progressing test that completes under the bound.

This test intentionally fails before ``faulthandler_timeout`` is added to
``pyproject.toml`` and passes once it is present -- TDD red -> green.
"""

from __future__ import annotations

import pathlib
import tomllib

_PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pytest_ini_options() -> dict[str, object]:
    """Return the ``[tool.pytest.ini_options]`` table from pyproject.toml."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    tool = data.get("tool", {})
    result: dict[str, object] = tool.get("pytest", {}).get("ini_options", {})
    return result


def test_faulthandler_timeout_is_configured() -> None:
    """``faulthandler_timeout`` must be set so a hung test is diagnosable.

    Without this, a hung test blocks the local gate and CI indefinitely with
    no diagnostic (see docs/backlog.md, "Test / CI robustness").
    """
    options = _pytest_ini_options()
    assert "faulthandler_timeout" in options, (
        "[tool.pytest.ini_options] has no `faulthandler_timeout` -- a hung "
        "test can block the local gate and CI indefinitely with no "
        "diagnostic. Add a generous per-test bound (pytest's built-in "
        "faulthandler support; no new dependency -- rule 33)."
    )


def test_faulthandler_timeout_is_a_generous_positive_int() -> None:
    """``faulthandler_timeout`` must be a generous positive int (seconds).

    A plain positive int is required -- a bool, float, or string would not
    behave as pytest's ``faulthandler_timeout`` expects. A floor of 60s
    guards against a degenerate near-zero value that would defeat the
    safety-net's purpose (firing stack dumps on ordinary, legitimately-slow
    tests instead of only genuine hangs).
    """
    options = _pytest_ini_options()
    timeout = options.get("faulthandler_timeout")
    assert isinstance(timeout, int), (
        f"faulthandler_timeout must be a plain int (seconds), got: {timeout!r}"
    )
    assert not isinstance(timeout, bool), (
        f"faulthandler_timeout must be a plain int, not a bool: {timeout!r}"
    )
    assert timeout >= 60, (
        f"faulthandler_timeout={timeout!r} is too tight to be a 'generous' "
        "backstop -- it must be well clear of any legitimate single test's "
        "runtime (e.g. 300s), or it risks misdiagnosing ordinary slow tests "
        "as hangs."
    )
