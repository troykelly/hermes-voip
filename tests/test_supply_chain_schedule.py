"""Tests that the supply-chain CI workflow has a scheduled cron trigger.

A newly-disclosed CVE against an unchanged pinned dependency is invisible to CI
when the workflow only runs on pull_request/push path-filtered events.  A daily
schedule trigger closes this gap (see backlog.md, ADR-0062 background).

This test intentionally fails before the schedule trigger is added and passes
once it is present — TDD red -> green.
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

_WORKFLOW_PATH = (
    pathlib.Path(__file__).parent.parent / ".github" / "workflows" / "supply-chain.yml"
)


def _load_workflow() -> dict[str, Any]:
    with _WORKFLOW_PATH.open() as fh:
        result: dict[str, Any] = yaml.safe_load(fh)
    return result


def test_workflow_file_exists() -> None:
    """Baseline: the workflow file must exist before we check its content."""
    assert _WORKFLOW_PATH.exists(), f"Workflow file not found: {_WORKFLOW_PATH}"


def test_on_block_contains_schedule() -> None:
    """The `on:` block must contain a `schedule:` key with at least one entry."""
    workflow = _load_workflow()
    on_block: dict[str, Any] = workflow.get("on", {})
    assert "schedule" in on_block, (
        "supply-chain.yml has no `schedule:` trigger — a CVE against an unchanged "
        "pinned dependency will never be caught between dependency-bump PRs.  "
        "Add a daily `schedule:` cron entry to the workflow's `on:` block."
    )
    schedule_entries: list[Any] = on_block["schedule"]
    assert isinstance(schedule_entries, list), (
        "`schedule:` must be a list of `{cron: ...}` entries"
    )
    assert len(schedule_entries) >= 1, "`schedule:` must have at least one cron entry"


def test_schedule_has_cron_entry() -> None:
    """Each schedule entry must be a mapping with a `cron` key."""
    workflow = _load_workflow()
    on_block: dict[str, Any] = workflow.get("on", {})
    schedule_entries: list[Any] = on_block.get("schedule", [])
    assert schedule_entries, (
        "No schedule entries found (see test_on_block_contains_schedule)"
    )
    for entry in schedule_entries:
        assert isinstance(entry, dict), f"Schedule entry must be a dict, got: {entry!r}"
        assert "cron" in entry, f"Schedule entry is missing a `cron` key: {entry!r}"
        cron_value: str = entry["cron"]
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
    """The `audit` job must not be skipped on scheduled runs.

    Path-filtered `on.push/pull_request` events mean schedule events carry
    NO path context.  Any `if:` condition on the job that checks
    ``github.event_name`` and excludes ``schedule`` would silently skip the
    audit on cron runs -- defeating the purpose of the schedule trigger.

    Acceptable states:
    - No `if:` condition on the job at all (job always runs), OR
    - An `if:` that explicitly ALLOWS ``github.event_name == 'schedule'``.

    Unacceptable: an `if:` whose only branch is pull_request / push without
    a schedule arm.
    """
    workflow = _load_workflow()
    jobs: dict[str, Any] = workflow.get("jobs", {})
    assert "audit" in jobs, "No `audit` job found in supply-chain.yml"
    audit_job: dict[str, Any] = jobs["audit"]

    if_condition: str | None = audit_job.get("if")
    if if_condition is None:
        # No condition -- the job runs unconditionally on every trigger, including
        # schedule.  This is the correct (and currently expected) state.
        return

    # If there IS a condition, it must not be one that blocks scheduled runs.
    # The simplest safe check: the condition must mention 'schedule' when it
    # references event_name, so it is not accidentally excluded.
    if "event_name" in if_condition or "github.event" in if_condition:
        assert "schedule" in if_condition, (
            f"The `audit` job has an `if:` condition that references the event "
            f"type but does not include `schedule` -- the job will be skipped on "
            f"scheduled runs.  Condition: {if_condition!r}"
        )
