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


def test_optional_extras_step_delegates_guard_to_extracted_module() -> None:
    """The fail-loud guard lives in a unit-tested module, not inline YAML shell.

    The empty-parse branch must call ``tools.check_optional_extras_guard`` rather
    than an inline ``python3 -c`` one-liner, so the skip/fail/run decision is
    exercised by ``tests/test_supply_chain_optional_extras_guard.py``.  Asserting
    the workflow delegates keeps the two in lock-step.
    """
    run_cmd = _optional_extras_run()

    assert "tools.check_optional_extras_guard" in run_cmd, (
        "The optional-extras step must delegate its skip/fail-loud decision to the "
        "tools.check_optional_extras_guard module (see "
        "tests/test_supply_chain_optional_extras_guard.py)."
    )
    # The brittle inline ``python3 -c`` guard must be gone — it was untestable and
    # checked value-truthiness rather than counting declared packages.
    assert "python3 -c" not in run_cmd
