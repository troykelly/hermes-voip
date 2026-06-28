"""Tests for the fail-loud optional-extras licence-gate guard.

The supply-chain workflow parses the optional-extras package set from
``uv export`` output (matching the ``# via hermes-voip`` provenance comment).  If
that parse yields an EMPTY package list while ``pyproject.toml`` still declares
``[project.optional-dependencies]`` with at least one package, the licence gate
would otherwise silently pass (a false green).  ``tools.check_optional_extras_guard``
is the small, fully-typed decision the workflow delegates to so the guard is
unit-testable rather than buried in inline YAML shell.

Three cases must be unambiguous:

* (a) no ``[project.optional-dependencies]`` section, OR every declared extra is
  empty -> legitimate SKIP (``decide`` -> ``SKIP``).
* (b) at least one declared extra contains at least one package BUT the parsed
  package list is empty -> FAIL LOUD (``decide`` raises ``GuardFailureError``).
* (c) declared extras with packages AND the parsed list is non-empty -> RUN the
  licence gate normally (``decide`` -> ``RUN``).
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest
from tools.check_optional_extras_guard import (
    Decision,
    GuardFailureError,
    count_declared_extra_packages,
    decide,
    main,
)

_PYPROJECT_NO_SECTION = """\
[project]
name = "hermes-voip"
version = "0.0.0"
"""

_PYPROJECT_ALL_EMPTY = """\
[project]
name = "hermes-voip"
version = "0.0.0"

[project.optional-dependencies]
webrtc = []
ml = []
"""

_PYPROJECT_WITH_PACKAGES = """\
[project]
name = "hermes-voip"
version = "0.0.0"

[project.optional-dependencies]
webrtc = ["aioice==0.10.2", "opuslib==3.0.1"]
ml = ["numpy==2.4.6"]
"""


def _write(
    tmp_path: pathlib.Path, pyproject: str, packages: str
) -> tuple[pathlib.Path, pathlib.Path]:
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(pyproject)
    packages_path = tmp_path / "optional-runtime-pkgs.txt"
    packages_path.write_text(packages)
    return pyproject_path, packages_path


# --- count_declared_extra_packages -----------------------------------------


def test_count_is_zero_when_no_section(tmp_path: pathlib.Path) -> None:
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(_PYPROJECT_NO_SECTION)
    assert count_declared_extra_packages(pyproject_path) == 0


def test_count_is_zero_when_all_extras_empty(tmp_path: pathlib.Path) -> None:
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(_PYPROJECT_ALL_EMPTY)
    assert count_declared_extra_packages(pyproject_path) == 0


def test_count_sums_packages_across_all_extras(tmp_path: pathlib.Path) -> None:
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(_PYPROJECT_WITH_PACKAGES)
    # webrtc (2) + ml (1) == 3 — counts packages, not the truthiness of values.
    assert count_declared_extra_packages(pyproject_path) == 3


# --- decide: case (a) legitimate SKIP --------------------------------------


def test_decide_skips_when_no_section(tmp_path: pathlib.Path) -> None:
    pyproject_path, packages_path = _write(tmp_path, _PYPROJECT_NO_SECTION, "")
    assert decide(pyproject_path, packages_path) is Decision.SKIP


def test_decide_skips_when_all_extras_empty(tmp_path: pathlib.Path) -> None:
    pyproject_path, packages_path = _write(tmp_path, _PYPROJECT_ALL_EMPTY, "")
    assert decide(pyproject_path, packages_path) is Decision.SKIP


# --- decide: case (b) FAIL LOUD --------------------------------------------


def test_decide_fails_loud_when_declared_but_parse_empty(
    tmp_path: pathlib.Path,
) -> None:
    pyproject_path, packages_path = _write(tmp_path, _PYPROJECT_WITH_PACKAGES, "")
    with pytest.raises(GuardFailureError):
        decide(pyproject_path, packages_path)


def test_decide_fails_loud_when_parse_file_only_blank_lines(
    tmp_path: pathlib.Path,
) -> None:
    # A file that is non-empty on disk but carries no package names is still an
    # empty parse and must fail loud, not be treated as "RUN".
    pyproject_path, packages_path = _write(
        tmp_path, _PYPROJECT_WITH_PACKAGES, "\n   \n\n"
    )
    with pytest.raises(GuardFailureError):
        decide(pyproject_path, packages_path)


def test_decide_fails_loud_when_parse_file_missing(
    tmp_path: pathlib.Path,
) -> None:
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(_PYPROJECT_WITH_PACKAGES)
    missing_packages = tmp_path / "does-not-exist.txt"
    with pytest.raises(GuardFailureError):
        decide(pyproject_path, missing_packages)


# --- decide: case (c) RUN the licence gate ---------------------------------


def test_decide_runs_when_declared_and_parse_non_empty(
    tmp_path: pathlib.Path,
) -> None:
    pyproject_path, packages_path = _write(
        tmp_path, _PYPROJECT_WITH_PACKAGES, "aioice\nnumpy\nopuslib\n"
    )
    assert decide(pyproject_path, packages_path) is Decision.RUN


# --- main / CLI contract ---------------------------------------------------


def test_main_emits_skip_and_exits_zero(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject_path, packages_path = _write(tmp_path, _PYPROJECT_NO_SECTION, "")
    exit_code = main(
        ["--pyproject", str(pyproject_path), "--packages", str(packages_path)]
    )
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == Decision.SKIP.value


def test_main_emits_run_and_exits_zero(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject_path, packages_path = _write(
        tmp_path, _PYPROJECT_WITH_PACKAGES, "aioice\n"
    )
    exit_code = main(
        ["--pyproject", str(pyproject_path), "--packages", str(packages_path)]
    )
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == Decision.RUN.value


def test_main_fails_loud_with_diagnostic_on_stderr(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject_path, packages_path = _write(tmp_path, _PYPROJECT_WITH_PACKAGES, "")
    exit_code = main(
        ["--pyproject", str(pyproject_path), "--packages", str(packages_path)]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    # Diagnostic goes to stderr; nothing actionable should be printed to stdout
    # (a RUN/SKIP token on stdout would mislead the workflow).
    assert captured.out.strip() == ""
    assert "optional-dependencies" in captured.err
    assert captured.err.strip() != ""


def test_script_invocable_as_module(tmp_path: pathlib.Path) -> None:
    """The guard runs as ``python -m tools.check_optional_extras_guard`` (the CI form).

    This exercises the real subprocess path the workflow uses, proving the
    module's ``__main__`` wiring and exit-code contract end to end.
    """
    pyproject_path, packages_path = _write(tmp_path, _PYPROJECT_WITH_PACKAGES, "")
    repo_root = pathlib.Path(__file__).parent.parent
    result = subprocess.run(  # noqa: S603 - fixed module invocation via the test interpreter.
        [
            sys.executable,
            "-m",
            "tools.check_optional_extras_guard",
            "--pyproject",
            str(pyproject_path),
            "--packages",
            str(packages_path),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "optional-dependencies" in result.stderr
