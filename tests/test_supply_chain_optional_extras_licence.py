"""Tests for the supply-chain optional-extras licence gate.

The advisory gate already installs ``--all-extras`` so ``pip-audit`` sees the full
runtime surface, but the licence gate must also report the optional runtime extras
surface explicitly. Otherwise extra-only runtime packages can bypass the permissive
OSS allowlist report entirely.
"""

from __future__ import annotations

import pathlib

import yaml

_WORKFLOW_PATH = (
    pathlib.Path(__file__).parent.parent / ".github" / "workflows" / "supply-chain.yml"
)


def _load_workflow() -> dict[str, object]:
    with _WORKFLOW_PATH.open() as fh:
        result: dict[str, object] = yaml.safe_load(fh)
    return result


def test_optional_extras_licence_step_exports_all_extras_runtime_surface() -> None:
    """The audit job must export optional runtime extras for licence checking.

    ``uv export --no-dev`` without ``--all-extras`` only covers the default runtime
    dependency set. This repository's real runtime surface also includes optional
    extras (``hermes``, ``ml``, ``media``, ``webrtc``), so CI must generate a second
    licence report from ``uv export --all-extras --no-dev --no-emit-project
    --no-hashes``.
    """
    workflow = _load_workflow()
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "No `jobs` mapping found in supply-chain.yml"
    assert "audit" in jobs, "No `audit` job found in supply-chain.yml"
    audit_job = jobs["audit"]
    assert isinstance(audit_job, dict), "`audit` job must be a mapping"

    steps = audit_job.get("steps", [])
    assert isinstance(steps, list), "`steps` in `audit` job must be a list"

    matching_runs = [
        step.get("run", "")
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("run"), str)
        and "uv export" in step["run"]
        and "--all-extras" in step["run"]
        and "--no-dev" in step["run"]
        and "pip-licenses" in step["run"]
    ]

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
    workflow = _load_workflow()
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "No `jobs` mapping found in supply-chain.yml"
    audit_job = jobs["audit"]
    assert isinstance(audit_job, dict), "`audit` job must be a mapping"

    steps = audit_job.get("steps", [])
    assert isinstance(steps, list), "`steps` in `audit` job must be a list"

    production_steps = 0
    optional_extras_steps = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
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
