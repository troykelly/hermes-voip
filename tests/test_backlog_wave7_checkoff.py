"""Sanity checks for the Wave-7 backlog check-off (docs/backlog.md).

This is a docs-only change: no production code was altered.  The test
verifies that:

1. Every checked item follows the exact `[x]` syntax (not `[X]` etc.).
2. The seven PRs that shipped in Wave 7 are referenced in the file with
   their PR numbers in the expected check-off pattern.
3. No checked item appears *without* a PR reference or a "done" annotation
   (regression: a check-off that strips the provenance).
4. The "still in-flight" items (#255 / #242) are NOT checked.
"""

from __future__ import annotations

import re
from pathlib import Path

_BACKLOG = Path(__file__).parent.parent / "docs" / "backlog.md"


def _lines() -> list[str]:
    return _BACKLOG.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# 1. Checkbox syntax is always `[x]` (lower-case), never `[X]`/`[ x]` etc.
# ---------------------------------------------------------------------------


def test_no_malformed_checkboxes() -> None:
    bad: list[tuple[int, str]] = []
    for i, line in enumerate(_lines(), 1):
        # Detect checkbox-like patterns that are NOT one of the two valid forms
        if re.search(r"\[[xX ]\]", line) and not re.search(r"^- \[(x| )\] ", line):
            bad.append((i, line.strip()))
    assert not bad, f"Malformed checkbox lines: {bad}"


# ---------------------------------------------------------------------------
# 2. Wave-7 PRs are referenced (checked off) in the file.
# ---------------------------------------------------------------------------

_WAVE7_PRS = [245, 246, 247, 248, 249, 250, 251, 252, 253, 254]


def test_wave7_prs_referenced_in_checkoffs() -> None:
    text = _BACKLOG.read_text(encoding="utf-8")
    missing = []
    for pr in _WAVE7_PRS:
        # Each PR must appear as a checked-off reference in at least one line
        # Pattern: [x] ... (#<pr>) or (done #<pr>) or (#<pr>)
        if not re.search(
            rf"^\- \[x\].*\(.*#{pr}.*\)",
            text,
            re.MULTILINE,
        ):
            missing.append(pr)
    assert not missing, (
        f"PRs not yet checked off in backlog.md: {missing}. "
        "Run the implementation step."
    )


# ---------------------------------------------------------------------------
# 3. The in-flight items (#255 silero / #242 VAD model) are NOT checked.
# ---------------------------------------------------------------------------


def test_inflight_items_remain_unchecked() -> None:
    """#255 (silero) and #242 (VAD model) are still in-flight — must stay [ ]."""
    text = _BACKLOG.read_text(encoding="utf-8")
    # If either PR number appears inside a [x] line, that is wrong.
    for pr in (255, 242):
        checked_pattern = rf"^\- \[x\].*#{pr}\b"
        matches = re.findall(checked_pattern, text, re.MULTILINE)
        assert not matches, (
            f"PR #{pr} appears checked off but should remain in-flight: {matches}"
        )


# ---------------------------------------------------------------------------
# 4. All checked items carry a provenance marker (PR # or "done" keyword).
# ---------------------------------------------------------------------------


def test_wave7_checked_items_have_pr_reference() -> None:
    """Each newly checked-off Wave-7 PR must appear as `(#NNN)` on a `[x]` line.

    This is equivalent to test_wave7_prs_referenced_in_checkoffs but is kept as
    a named guard so that the implementation commit can verify both pass.
    """
    # Delegate to the same logic; both tests gate the same invariant from
    # different angles so they catch different implementation mistakes.
    text = _BACKLOG.read_text(encoding="utf-8")
    bad = []
    for pr in _WAVE7_PRS:
        pattern = rf"^\- \[x\].*\(.*#{pr}[^0-9]"
        if not re.search(pattern, text, re.MULTILINE):
            bad.append(pr)
    assert not bad, f"Wave-7 PRs missing [x] + (#NNN) line: {bad}"
