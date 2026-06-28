"""Tests for the supply-chain optional-extras licence gate.

The advisory gate already installs ``--all-extras`` so ``pip-audit`` sees the full
runtime surface, but the licence gate must also report the optional runtime extras
surface explicitly. Otherwise extra-only runtime packages can bypass the permissive
OSS allowlist report entirely.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import textwrap

import yaml

_WORKFLOW_PATH = (
    pathlib.Path(__file__).parent.parent / ".github" / "workflows" / "supply-chain.yml"
)
_OPTIONAL_EXTRAS_STEP_NAME = "Licence allowlist (optional runtime extras)"


def _load_workflow() -> dict[str, object]:
    with _WORKFLOW_PATH.open() as fh:
        result: dict[str, object] = yaml.safe_load(fh)
    return result


def _audit_steps() -> list[dict[str, object]]:
    workflow = _load_workflow()
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "No `jobs` mapping found in supply-chain.yml"
    assert "audit" in jobs, "No `audit` job found in supply-chain.yml"
    audit_job = jobs["audit"]
    assert isinstance(audit_job, dict), "`audit` job must be a mapping"

    steps = audit_job.get("steps", [])
    assert isinstance(steps, list), "`steps` in `audit` job must be a list"
    return [step for step in steps if isinstance(step, dict)]


def _optional_extras_run() -> str:
    for step in _audit_steps():
        if step.get("name") != _OPTIONAL_EXTRAS_STEP_NAME:
            continue
        run_cmd = step.get("run")
        assert isinstance(run_cmd, str), (
            "Optional-extras step must define a shell script"
        )
        return run_cmd
    raise AssertionError(
        "supply-chain.yml is missing the optional runtime extras licence step"
    )


def _optional_extras_python_script() -> str:
    run_cmd = _optional_extras_run()
    match = re.search(
        r"<<'PY'[^\n]*\n(?P<script>.*)\n\s*PY",
        run_cmd,
        flags=re.DOTALL,
    )
    assert match is not None, "Optional-extras step must embed a Python parser script"
    return textwrap.dedent(match.group("script"))


def test_optional_extras_licence_step_exports_all_extras_runtime_surface() -> None:
    """The audit job must export optional runtime extras for licence checking.

    ``uv export --no-dev`` without ``--all-extras`` only covers the default runtime
    dependency set. This repository's real runtime surface also includes optional
    extras (``hermes``, ``ml``, ``media``, ``webrtc``), so CI must generate a second
    licence report from ``uv export --all-extras --no-dev --no-emit-project
    --no-hashes``.
    """
    matching_runs: list[str] = []
    for step in _audit_steps():
        run_cmd = step.get("run")
        if not isinstance(run_cmd, str):
            continue
        if (
            "uv export" in run_cmd
            and "--all-extras" in run_cmd
            and "--no-dev" in run_cmd
            and "pip-licenses" in run_cmd
        ):
            matching_runs.append(run_cmd)

    assert matching_runs, (
        "supply-chain.yml does not have a licence-report step for optional runtime "
        "extras. Add a separate audit-job step that exports `uv export --frozen "
        "--all-extras --no-dev --no-emit-project --no-hashes` and runs "
        "`pip-licenses` over that package list."
    )


def test_production_and_optional_extras_licence_reports_are_separate() -> None:
    """CI must keep the base production gate and add a separate extras report.

    The existing production-only licence check is still needed because it captures the
    exact default shipped surface. The optional-extras report is additive, not a
    replacement.
    """
    production_steps = 0
    optional_extras_steps = 0
    for step in _audit_steps():
        run_cmd = step.get("run")
        if not isinstance(run_cmd, str) or "pip-licenses" not in run_cmd:
            continue
        if (
            "uv export" in run_cmd
            and "--no-dev" in run_cmd
            and "--all-extras" not in run_cmd
        ):
            production_steps += 1
        if (
            "uv export" in run_cmd
            and "--all-extras" in run_cmd
            and "--no-dev" in run_cmd
        ):
            optional_extras_steps += 1

    assert production_steps >= 1, (
        "Expected the existing production licence gate to remain present"
    )
    assert optional_extras_steps >= 1, (
        "Expected a separate optional-extras licence report in addition to the "
        "production-only gate"
    )


def test_optional_extras_licence_step_reads_named_export_file() -> None:
    """The parser must read a named export file, not a discarded pipe.

    ``python - <<'PY'`` consumes stdin for the script body itself, so piping
    ``uv export`` into that form silently drops the exported requirements and leaves
    the package list empty. The workflow must materialize the export first and pass
    the file path into the parser.
    """
    run_cmd = _optional_extras_run()

    assert "optional-runtime-export.txt" in run_cmd
    assert "sys.argv[1]" in run_cmd
    assert "| uv run python - <<'PY'" not in run_cmd


def test_optional_extras_parser_keeps_direct_extra_packages(
    tmp_path: pathlib.Path,
) -> None:
    """The embedded parser keeps every direct hermes-voip optional package.

    A representative ``uv export --all-extras`` stream must yield the direct package
    names declared by ``hermes-voip`` rather than an empty file.
    """
    export_path = tmp_path / "optional-runtime-export.txt"
    export_path.write_text(
        textwrap.dedent(
            """\
            # This file was autogenerated by uv via the following command:
            aioice==0.10.1
                # via hermes-voip
            audioop-lts==0.2.2
                # via hermes-voip
            cffi==1.17.1
                # via cryptography
            hermes-agent==0.16.0
                # via
                #   hermes-voip
            sherpa-onnx==1.10.59 ; sys_platform == 'linux'
                # via hermes-voip
            websockets==15.0.1
                # via aioice
            """
        )
    )

    result = subprocess.run(  # noqa: S603 - test runs the embedded workflow parser via the current interpreter.
        [sys.executable, "-", str(export_path)],
        input=_optional_extras_python_script(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "aioice",
        "audioop-lts",
        "hermes-agent",
        "sherpa-onnx",
    ]


def _extract_shell_guard_segment(run_cmd: str) -> str:
    """Return the portion of the shell script that runs after the Python parser.

    The fail-loud guard lives between ``PY`` (end of the heredoc) and the
    ``rm -f`` cleanup block.
    """
    # Strip the heredoc body; keep only the shell wrapper
    return run_cmd.split("PY", 2)[-1]  # text after the closing ``PY``


def test_optional_extras_guard_fails_loud_when_extras_declared_but_parse_empty(
    tmp_path: pathlib.Path,
) -> None:
    """Fail loudly when optional-extras are declared but the parser returns nothing.

    If the ``uv export`` comment format changes and the parser yields an empty
    ``optional-runtime-pkgs.txt`` while ``pyproject.toml`` still declares
    ``[project.optional-dependencies]``, the CI step MUST exit non-zero with a
    clear message — never silently pass (rule 37 / spec: fail-loud guard).

    The guard is embedded in the shell ``run:`` block of the
    ``Licence allowlist (optional runtime extras)`` step immediately after the
    ``PY`` heredoc closes. This test exercises it by:

    * Writing a fake ``pyproject.toml`` that declares one extra.
    * Writing an empty ``optional-runtime-pkgs.txt`` (simulates a broken parser).
    * Extracting and running the shell portion of the step inside ``tmp_path``.
    * Asserting exit code is non-zero and stderr/stdout carries a diagnostic.
    """
    # Fake pyproject.toml with a declared optional dependency
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "hermes-voip"
            version = "0.0.0"

            [project.optional-dependencies]
            webrtc = ["aioice==0.10.2"]
            """
        )
    )
    # Empty parsed package list (simulates broken/missing provenance comment)
    (tmp_path / "optional-runtime-pkgs.txt").write_text("")

    run_cmd = _optional_extras_run()
    shell_after_py = _extract_shell_guard_segment(run_cmd)

    result = subprocess.run(  # noqa: S603
        ["bash", "-euo", "pipefail", "-c", shell_after_py],  # noqa: S607 - bash is a known system shell.
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, (
        "Expected the gate to fail when optional-extras are declared in "
        "pyproject.toml but the parsed package list is empty. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert combined, (
        "Expected a diagnostic message explaining why the gate failed, got empty output"
    )


def test_optional_extras_guard_skips_when_genuinely_no_extras(
    tmp_path: pathlib.Path,
) -> None:
    """The guard must still allow skipping when no extras are declared.

    A project with an empty ``[project.optional-dependencies]`` section (or no
    section at all) has a genuinely empty optional package set.  The gate should
    print the existing "no extras" message and exit 0 — the skip path is
    legitimate and must not be broken by the fail-loud guard.
    """
    # pyproject.toml with NO optional-dependencies section
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "hermes-voip"
            version = "0.0.0"
            """
        )
    )
    # Empty parsed package list (correct: no extras → nothing to parse)
    (tmp_path / "optional-runtime-pkgs.txt").write_text("")

    run_cmd = _optional_extras_run()
    shell_after_py = _extract_shell_guard_segment(run_cmd)

    result = subprocess.run(  # noqa: S603
        ["bash", "-euo", "pipefail", "-c", shell_after_py],  # noqa: S607 - bash is a known system shell.
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        "Expected the gate to succeed (skip) when no optional-extras are declared. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
