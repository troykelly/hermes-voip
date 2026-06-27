"""Tests that CI workflows pin third-party actions to full 40-char commit SHAs.

AGENTS.md rule 35 (supply-chain): every third-party ``uses:`` entry must be
pinned to a full 40-character commit SHA so that a tag reassignment or a
compromised action publisher cannot inject malicious code into CI runs.  The
pattern is established in ``.github/workflows/publish.yml``
(e.g. ``actions/checkout@11bd71...  # v4.2.2``).

This test intentionally FAILS before the SHA-pinning is applied to gate.yml,
gitleaks.yml, and supply-chain.yml, and PASSES once all ``@vTAG``-style refs
are replaced with ``@<40-char-sha>  # vX.Y.Z`` refs.

Covered files:
  - .github/workflows/gate.yml
  - .github/workflows/gitleaks.yml
  - .github/workflows/supply-chain.yml
"""

from __future__ import annotations

import pathlib
import re

import yaml

_WORKFLOWS_DIR = pathlib.Path(__file__).parent.parent / ".github" / "workflows"

# Files that must have all third-party actions pinned to full SHAs.
_TARGET_WORKFLOWS: list[str] = [
    "gate.yml",
    "gitleaks.yml",
    "supply-chain.yml",
]

# Pattern for a PINNED ref: exactly 40 hex chars.
_PINNED_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _collect_uses_refs(workflow_path: pathlib.Path) -> list[str]:
    """Return all ``uses:`` values from a workflow file.

    Covers step-level uses and job-level uses (reusable workflows).

    Returns a flat list of strings like ``"actions/checkout@v4"`` or
    ``"astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39"``.
    """
    with workflow_path.open() as fh:
        workflow: dict[str, object] = yaml.safe_load(fh)

    refs: list[str] = []

    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return refs

    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        # Top-level job `uses:` (reusable workflows)
        job_uses = job.get("uses")
        if isinstance(job_uses, str):
            refs.append(job_uses)
        # Step-level `uses:`
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_uses = step.get("uses")
            if isinstance(step_uses, str):
                refs.append(step_uses)

    return refs


def _is_unpinned(uses_ref: str) -> bool:
    """Return True if the ``uses:`` value is NOT pinned to a 40-char commit SHA.

    A pinned ref is exactly 40 lowercase hex chars after the ``@``.  Anything
    else (@v4, @v1.2.3, @main, @master, …) is unpinned.

    Docker-protocol (``docker://``) and local (``./``) refs are skipped; they
    use a different pinning model.
    """
    if uses_ref.startswith("docker://") or uses_ref.startswith("./"):
        return False  # not a third-party action; different pinning model
    if "@" not in uses_ref:
        return False  # malformed or local ref
    _, ref_part = uses_ref.rsplit("@", 1)
    # Be defensive: strip trailing whitespace (YAML values shouldn't include it,
    # but the helper is called with raw strings in some tests).
    ref_part = ref_part.split()[0] if ref_part.split() else ref_part
    return not bool(_PINNED_SHA_RE.match(ref_part))


def test_gate_yml_all_actions_pinned() -> None:
    """All third-party action refs in gate.yml must be pinned to 40-char SHAs."""
    workflow_path = _WORKFLOWS_DIR / "gate.yml"
    assert workflow_path.exists(), f"Workflow file not found: {workflow_path}"

    refs = _collect_uses_refs(workflow_path)
    assert refs, f"No `uses:` entries found in {workflow_path} — unexpected"

    unpinned = [r for r in refs if _is_unpinned(r)]
    assert not unpinned, (
        f"gate.yml has {len(unpinned)} unpinned action ref(s) — replace each "
        f"'@vTAG' with the corresponding 40-char commit SHA and keep the version "
        f"in a trailing '# vX.Y.Z' comment (AGENTS.md rule 35):\n"
        + "\n".join(f"  {r}" for r in unpinned)
    )


def test_gitleaks_yml_all_actions_pinned() -> None:
    """All action refs in gitleaks.yml must be pinned to 40-char SHAs."""
    workflow_path = _WORKFLOWS_DIR / "gitleaks.yml"
    assert workflow_path.exists(), f"Workflow file not found: {workflow_path}"

    refs = _collect_uses_refs(workflow_path)
    assert refs, f"No `uses:` entries found in {workflow_path} — unexpected"

    unpinned = [r for r in refs if _is_unpinned(r)]
    assert not unpinned, (
        f"gitleaks.yml has {len(unpinned)} unpinned action ref(s) — replace each "
        f"'@vTAG' with the corresponding 40-char commit SHA and keep the version "
        f"in a trailing '# vX.Y.Z' comment (AGENTS.md rule 35):\n"
        + "\n".join(f"  {r}" for r in unpinned)
    )


def test_supply_chain_yml_all_actions_pinned() -> None:
    """All action refs in supply-chain.yml must be pinned to 40-char SHAs."""
    workflow_path = _WORKFLOWS_DIR / "supply-chain.yml"
    assert workflow_path.exists(), f"Workflow file not found: {workflow_path}"

    refs = _collect_uses_refs(workflow_path)
    assert refs, f"No `uses:` entries found in {workflow_path} — unexpected"

    unpinned = [r for r in refs if _is_unpinned(r)]
    assert not unpinned, (
        f"supply-chain.yml has {len(unpinned)} unpinned action ref(s) — replace each "
        f"'@vTAG' with the corresponding 40-char commit SHA and keep the version "
        f"in a trailing '# vX.Y.Z' comment (AGENTS.md rule 35):\n"
        + "\n".join(f"  {r}" for r in unpinned)
    )


def test_pinned_refs_have_version_comment() -> None:
    """Every pinned action ref must have a trailing ``# vX.Y.Z`` comment.

    A SHA with no version comment is opaque — a reviewer cannot tell which tag
    the SHA corresponds to without querying the GitHub API.  Requiring a comment
    keeps the version human-readable alongside the pinned SHA.

    This test inspects the raw file text (not the parsed YAML, which strips
    comments) to check that every line containing a pinned SHA action ref also
    contains a ``# v``-prefixed trailing comment.
    """
    pinned_line_re = re.compile(r"uses:\s+\S+@[0-9a-f]{40}")
    version_comment_re = re.compile(r"#\s*v\d+")

    failures: list[str] = []
    for filename in _TARGET_WORKFLOWS:
        workflow_path = _WORKFLOWS_DIR / filename
        if not workflow_path.exists():
            continue
        raw = workflow_path.read_text(encoding="utf-8")
        for line in raw.splitlines():
            if pinned_line_re.search(line) and not version_comment_re.search(line):
                failures.append(f"{filename}: {line.strip()}")

    assert not failures, (
        "The following pinned action refs are missing a trailing '# vX.Y.Z' comment:\n"
        + "\n".join(f"  {f}" for f in failures)
    )


def test_no_target_workflow_has_version_tag_ref() -> None:
    """No target workflow must contain a third-party action ref by version tag.

    Belt-and-suspenders raw-text check that catches refs the YAML parser might
    not surface (e.g. inside multi-line run steps or anchors).  Patterns like
    ``uses: owner/repo@v4`` or ``uses: owner/repo@v1.2.3`` are matched.
    """
    # Match `uses: owner/repo@` followed by a version tag (starts with v or a
    # digit that looks like a semver), NOT followed by 40 hex chars.
    tag_ref_re = re.compile(
        r"uses:\s+\S+/\S+@(?!(?:[0-9a-f]{40})(?:\s|$|#))([vV]\d|\d+\.\d)"
    )

    failures: list[str] = []
    for filename in _TARGET_WORKFLOWS:
        workflow_path = _WORKFLOWS_DIR / filename
        if not workflow_path.exists():
            continue
        raw = workflow_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(raw.splitlines(), start=1):
            if tag_ref_re.search(line):
                failures.append(f"{filename}:{lineno}: {line.strip()}")

    assert not failures, (
        "The following lines use version-tag action refs (not SHA-pinned):\n"
        + "\n".join(f"  {f}" for f in failures)
    )
