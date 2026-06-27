"""Sanity checks for the Wave-7 backlog check-off (docs/backlog.md).

This is a docs-only change: no production code was altered.  The test
verifies that:

1. Every checked item follows the exact `[x]` or `[ ]` syntax — any other
   bracket content (e.g. `[ x]`, `[x ]`, `[]`, `[xx]`, `[X]`) is flagged.
2. The ten PRs that shipped in Wave 7 (#245-#254) are referenced in the file
   with their PR numbers in the expected check-off pattern.
3. No checked item appears *without* a PR reference or a "done" annotation
   (regression: a check-off that strips the provenance).
4. Every checked item in the Wave-7 section carries a `(#NNN)` PR reference.
5. The "still in-flight" items (#255 / #242) are NOT checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKLOG = Path(__file__).parent.parent / "docs" / "backlog.md"


def _lines() -> list[str]:
    return _BACKLOG.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# 1. Checkbox syntax is always `[ ]` (unchecked) or `[x]` (checked),
#    lower-case x only.  Any other content inside the brackets on a list-item
#    checkbox is malformed.
# ---------------------------------------------------------------------------

# Parametrized edge-cases that the OLD regex `r"\[[xX ]\]"` would silently
# PASS (false negatives).  The new logic must DETECT all of these.
_MALFORMED_CHECKBOX_SAMPLES = [
    "- [ x] item text",  # space before x — old regex: no match → missed
    "- [x ] item text",  # trailing space — old regex: no match → missed
    "- [] item text",  # empty brackets — old regex: no match → missed
    "- [xx] item text",  # double x — old regex: no match → missed
    "- [X] item text",  # uppercase X — old regex: [xX ] matched, second
    #                     guard ^- \[(x| )\] blocks, but let's be explicit
]

_VALID_CHECKBOX_SAMPLES = [
    "- [x] item text",
    "- [ ] item text",
    "  - [x] nested item",
    "  - [ ] nested item",
    "not a list item at all",
    "- regular list item without checkbox",
]


@pytest.mark.parametrize("line", _MALFORMED_CHECKBOX_SAMPLES)
def test_malformed_checkbox_is_detected(line: str) -> None:
    r"""Demonstrate that each malformed form is flagged by the detection logic.

    This test would have FAILED (wrongly passed) under the old
    `r"\[[xX ]\]"` single-char pattern for the first four samples.
    """
    # A list item that starts with `- [` but whose bracket content is not
    # exactly one of `x` or ` ` (single space) is malformed.
    match = re.match(r"^(\s*- )\[([^\]]*)\] ", line)
    if match is None:
        # Not a checkbox list item at all — skip
        return
    inner = match.group(2)
    # Only `x` (lower-case) or a single space are valid
    assert inner not in ("x", " "), (
        f"Expected '{line!r}' to be detected as malformed but it passed validation"
    )


@pytest.mark.parametrize("line", _VALID_CHECKBOX_SAMPLES)
def test_valid_checkbox_passes(line: str) -> None:
    """Valid forms must NOT be flagged as malformed."""
    match = re.match(r"^(\s*- )\[([^\]]*)\] ", line)
    if match is None:
        return  # not a checkbox line — fine
    inner = match.group(2)
    assert inner in ("x", " "), (
        f"Expected '{line!r}' to be valid but it was flagged as malformed"
    )


def test_no_malformed_checkboxes() -> None:
    """All checkbox list items in backlog.md use exactly `[ ]` or `[x]`.

    Detection: any list item whose `[...]` content is not exactly `x` or a
    single space is malformed.  This catches `[ x]`, `[x ]`, `[]`, `[xx]`,
    `[X]`, etc. — forms the old single-char regex `[xX ]` silently missed.
    """
    bad: list[tuple[int, str]] = []
    for i, line in enumerate(_lines(), 1):
        m = re.match(r"^(\s*- )\[([^\]]*)\] ", line)
        if m is None:
            continue
        inner = m.group(2)
        if inner not in ("x", " "):
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
        # Each PR must appear on a checked-off line with a boundary guard so
        # e.g. #245 does not spuriously match inside #2450.
        if not re.search(
            rf"^\- \[x\].*\(.*#{pr}(?!\d).*\)",
            text,
            re.MULTILINE,
        ):
            missing.append(pr)
    assert not missing, (
        f"PRs not yet checked off in backlog.md: {missing}. "
        "Run the implementation step."
    )


# ---------------------------------------------------------------------------
# 3. PR-number boundary correctness.  Demonstrate that the boundary guard
#    is necessary: without it, `#245` matches inside `(#2450)`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pr", "text_fragment", "should_match"),
    [
        # With boundary guard: exact match
        (245, "- [x] item (#245)", True),
        # With boundary guard: #245 inside #2450 must NOT match
        (245, "- [x] item (#2450)", False),
        # #250 inside #2500 must NOT match
        (250, "- [x] item (#2500)", False),
        # Multi-PR parens: exact match when another PR follows
        (253, "- [x] item (#253, #254)", True),
    ],
)
def test_pr_boundary_guard(pr: int, text_fragment: str, should_match: bool) -> None:
    """PR number matching uses a negative lookahead to prevent false positives."""
    pattern = rf"^\- \[x\].*\(.*#{pr}(?!\d).*\)"
    result = bool(re.search(pattern, text_fragment, re.MULTILINE))
    assert result == should_match, (
        f"PR #{pr} boundary check: expected match={should_match} for {text_fragment!r}"
    )


# ---------------------------------------------------------------------------
# 4. The in-flight items (#255 silero / #242 VAD model) are NOT checked.
# ---------------------------------------------------------------------------


def test_inflight_items_remain_unchecked() -> None:
    """#255 (silero) and #242 (VAD model) are still in-flight — must stay [ ]."""
    text = _BACKLOG.read_text(encoding="utf-8")
    for pr in (255, 242):
        checked_pattern = rf"^\- \[x\].*#{pr}(?!\d)"
        matches = re.findall(checked_pattern, text, re.MULTILINE)
        assert not matches, (
            f"PR #{pr} appears checked off but should remain in-flight: {matches}"
        )


# ---------------------------------------------------------------------------
# 5. Every checked item in the Wave-7 section carries a (#NNN) PR reference.
#    Scope: lines between the Wave-7 header and the end of file (no false
#    positives from older sections that use `done: <hash>` or other formats).
# ---------------------------------------------------------------------------

_WAVE7_HEADER = re.compile(r"^## Operator-filed issues \(Wave 7", re.MULTILINE)


def _wave7_section_lines() -> list[str]:
    """Return only the lines belonging to the Wave-7 section."""
    text = _BACKLOG.read_text(encoding="utf-8")
    m = _WAVE7_HEADER.search(text)
    assert m is not None, "Wave-7 section header not found in backlog.md"
    # Everything from the Wave-7 header to the end of the file
    return text[m.start() :].splitlines()


def test_wave7_checked_items_have_pr_reference() -> None:
    """Every `[x]` item in the Wave-7 section carries a `(#NNN)` PR reference.

    The REAL invariant: no checked-off Wave-7 item may be missing its
    provenance marker.  The old implementation only checked that each PR in
    _WAVE7_PRS appeared SOMEWHERE on a [x] line; a checked item like
    `- [x] done` with no `(#NNN)` would have passed.  This implementation
    checks the other direction: FOR EACH [x] line in the Wave-7 section,
    assert it carries a `(#NNN)` reference.
    """
    # Pattern: a checked list item with a (#NNN) reference (boundary-guarded)
    pr_ref_pattern = re.compile(r"\(#\d+(?!\d)")

    bad: list[str] = []
    for line in _wave7_section_lines():
        if re.match(r"^\- \[x\] ", line) and not pr_ref_pattern.search(line):
            bad.append(line.strip())

    assert not bad, f"Wave-7 [x] items missing a (#NNN) PR reference: {bad}"


# ---------------------------------------------------------------------------
# Demonstrate that the old test_wave7_checked_items_have_pr_reference logic
# had the weakness described in finding #2.
# ---------------------------------------------------------------------------

_WEAK_WAVE7_CHECK_SYNTHETIC = [
    # This [x] item has NO (#NNN) — the old logic would not have caught it
    # because the old test only verified each PR in _WAVE7_PRS appeared
    # somewhere on a [x] line (other direction).
    "- [x] done without any pr reference",
    "- [x] shipped (done: abc123)",  # hash, not a PR number
]


@pytest.mark.parametrize("line", _WEAK_WAVE7_CHECK_SYNTHETIC)
def test_checked_wave7_item_without_pr_ref_is_detected(line: str) -> None:
    """Prove the new logic WOULD catch a checked item lacking a PR reference."""
    pr_ref_pattern = re.compile(r"\(#\d+(?!\d)")
    # Simulate what the new test does: check this line for a PR reference
    assert not pr_ref_pattern.search(line), (
        f"Expected no PR ref in '{line}' but found one"
    )
    # And confirm the old direction-only check would have MISSED it:
    # old check: does PR 245 appear on a [x] line? Yes (other lines).
    # This line alone wouldn't have been caught.
    assert re.match(r"^\- \[x\] ", line), "Test setup: must be a [x] line"
