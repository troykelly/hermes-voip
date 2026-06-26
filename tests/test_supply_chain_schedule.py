"""Tests that the supply-chain CI workflow has a scheduled cron trigger.

A newly-disclosed CVE against an unchanged pinned dependency is invisible to CI
when the workflow only runs on pull_request/push path-filtered events.  A daily
schedule trigger closes this gap (see backlog.md, ADR-0062 background).

This test intentionally fails before the schedule trigger is added and passes
once it is present -- TDD red -> green.
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


def _get_on_block(workflow: dict[str, object]) -> dict[str, object]:
    """Return the ``on:`` block from a parsed GitHub Actions workflow dict.

    PyYAML's safe_load converts the bare YAML key ``on`` to the Python boolean
    ``True`` (YAML 1.1 bool), so the dict key is ``True``, not the string
    ``"on"``.  This helper normalises both spellings so callers do not need
    to know about the YAML 1.1 quirk.
    """
    # Prefer the boolean key (what safe_load produces from bare ``on:``); fall
    # back to the string key (what would happen if the workflow quoted ``"on":``).
    on_block = workflow.get(True) or workflow.get("on")  # type: ignore[call-overload]  # YAML 1.1: bare `on` -> bool True
    if on_block is None:
        return {}
    assert isinstance(on_block, dict), (
        f"Expected `on:` to be a mapping, got {type(on_block)}"
    )
    result: dict[str, object] = on_block
    return result


def test_workflow_file_exists() -> None:
    """Baseline: the workflow file must exist before we check its content."""
    assert _WORKFLOW_PATH.exists(), f"Workflow file not found: {_WORKFLOW_PATH}"


def test_on_block_contains_schedule() -> None:
    """The ``on:`` block must contain a ``schedule:`` key with at least one entry."""
    workflow = _load_workflow()
    on_block = _get_on_block(workflow)
    assert "schedule" in on_block, (
        "supply-chain.yml has no `schedule:` trigger -- a CVE against an unchanged "
        "pinned dependency will never be caught between dependency-bump PRs.  "
        "Add a daily `schedule:` cron entry to the workflow's `on:` block."
    )
    schedule_entries = on_block["schedule"]
    assert isinstance(schedule_entries, list), (
        "`schedule:` must be a list of `{cron: ...}` entries"
    )
    assert len(schedule_entries) >= 1, "`schedule:` must have at least one cron entry"


def test_schedule_has_cron_entry() -> None:
    """Each schedule entry must be a mapping with a ``cron`` key."""
    workflow = _load_workflow()
    on_block = _get_on_block(workflow)
    schedule_entries = on_block.get("schedule", [])
    assert isinstance(schedule_entries, list), "`schedule:` must be a list"
    assert schedule_entries, (
        "No schedule entries found (see test_on_block_contains_schedule)"
    )
    for entry in schedule_entries:
        assert isinstance(entry, dict), f"Schedule entry must be a dict, got: {entry!r}"
        assert "cron" in entry, f"Schedule entry is missing a `cron` key: {entry!r}"
        cron_value = entry["cron"]
        assert isinstance(cron_value, str), (
            f"cron value must be a string, got: {cron_value!r}"
        )
        assert cron_value.strip(), f"cron value must be non-empty, got: {cron_value!r}"
        # Validate the cron string has 5 fields (standard GitHub Actions cron)
        fields = cron_value.strip().split()
        assert len(fields) == 5, (
            f"cron expression must have 5 fields (min hour dom month dow), "
            f"got {len(fields)} in: {cron_value!r}"
        )


def test_audit_job_runs_on_schedule() -> None:
    """The ``audit`` job must not be skipped on scheduled runs.

    Path-filtered ``on.push``/``pull_request`` events mean schedule events carry
    NO path context.  Any ``if:`` condition on the job that checks
    ``github.event_name`` and excludes ``schedule`` would silently skip the
    audit on cron runs -- defeating the purpose of the schedule trigger.

    Acceptable states:
    - No ``if:`` condition on the job at all (job always runs), OR
    - An ``if:`` that explicitly ALLOWS ``github.event_name == 'schedule'``.

    Unacceptable: an ``if:`` whose only branch is pull_request / push without
    a schedule arm.
    """
    workflow = _load_workflow()
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "No `jobs` mapping found in supply-chain.yml"
    assert "audit" in jobs, "No `audit` job found in supply-chain.yml"
    audit_job = jobs["audit"]
    assert isinstance(audit_job, dict), "`audit` job must be a mapping"

    if_condition = audit_job.get("if")
    if if_condition is None:
        # No condition -- the job runs unconditionally on every trigger, including
        # schedule.  This is the correct (and currently expected) state.
        return

    # If there IS a condition, it must not be one that blocks scheduled runs.
    # The simplest safe check: the condition must mention 'schedule' when it
    # references event_name, so it is not accidentally excluded.
    assert isinstance(if_condition, str), (
        f"`if:` must be a string, got {type(if_condition)}"
    )
    if "event_name" in if_condition or "github.event" in if_condition:
        assert "schedule" in if_condition, (
            f"The `audit` job has an `if:` condition that references the event "
            f"type but does not include `schedule` -- the job will be skipped on "
            f"scheduled runs.  Condition: {if_condition!r}"
        )


def test_on_block_contains_workflow_dispatch() -> None:
    """The ``on:`` block must contain a ``workflow_dispatch:`` key.

    The runbook (docs/runbooks/0003-supply-chain-audit.md) instructs operators to
    trigger an ad-hoc re-scan via the GitHub Actions UI "Run workflow" button
    (Actions → supply-chain → Run workflow).  That button only renders when the
    workflow declares ``workflow_dispatch:`` in its ``on:`` block.  Without it the
    documented manual trigger does not exist, violating AGENTS.md rule 27
    (no aspirational docs).
    """
    workflow = _load_workflow()
    on_block = _get_on_block(workflow)
    assert "workflow_dispatch" in on_block, (
        "supply-chain.yml has no `workflow_dispatch:` trigger -- the GitHub Actions "
        "'Run workflow' button will not appear, making the runbook's ad-hoc re-scan "
        "instruction aspirational rather than true.  Add `workflow_dispatch:` to the "
        "workflow's `on:` block (AGENTS.md rule 27)."
    )


def test_pip_audit_step_not_gated_from_schedule() -> None:
    """No pip-audit step in the ``audit`` job may carry a schedule-excluding ``if:``.

    A pip-audit step with ``if: github.event_name != 'schedule'`` (or equivalent) would
    silently skip the advisory scan on every scheduled run -- the job itself passes
    but no audit is performed.  The job-level ``if:`` test
    (``test_audit_job_runs_on_schedule``) does not cover this gap because a step-level
    gate is evaluated after the job starts.

    A step-level ``if: github.event_name != 'schedule'`` (or equivalent) would
    silently skip the advisory scan on every scheduled run -- the job itself passes
    but no audit is performed.  The job-level ``if:`` test
    (``test_audit_job_runs_on_schedule``) does not cover this gap because a step-level
    gate is evaluated after the job starts.

    This test inspects every step whose ``run:`` command contains ``pip-audit`` and
    asserts that none of them carry an ``if:`` referencing ``event_name`` /
    ``github.event`` without also including ``schedule``.
    """
    workflow = _load_workflow()
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "No `jobs` mapping found in supply-chain.yml"
    assert "audit" in jobs, "No `audit` job found in supply-chain.yml"
    audit_job = jobs["audit"]
    assert isinstance(audit_job, dict), "`audit` job must be a mapping"

    steps = audit_job.get("steps", [])
    assert isinstance(steps, list), "`steps` in `audit` job must be a list"

    for i, step in enumerate(steps):
        assert isinstance(step, dict), f"Step {i} must be a mapping, got {step!r}"
        run_cmd = step.get("run", "")
        if not isinstance(run_cmd, str) or "pip-audit" not in run_cmd:
            continue  # not a pip-audit step -- skip

        step_if = step.get("if")
        if step_if is None:
            continue  # no condition -- step always runs, which is correct

        assert isinstance(step_if, str), (
            f"pip-audit step {i} `if:` must be a string, got {type(step_if)}"
        )
        # If the condition references the event type it must allow 'schedule' through.
        if "event_name" in step_if or "github.event" in step_if:
            assert "schedule" in step_if, (
                f"pip-audit step {i} has an `if:` condition that references the event "
                f"type but does not include `schedule` -- the advisory scan will be "
                f"silently skipped on every scheduled run.  Condition: {step_if!r}"
            )
