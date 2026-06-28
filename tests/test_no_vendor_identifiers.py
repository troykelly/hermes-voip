"""Privacy invariant guard against gateway vendor/model/brand identifiers.

CLAUDE.md hard invariant + AGENTS.md rule 34 ban device/vendor identifiers in
ANY tracked file of this PUBLIC repo. The operator's real test-gateway vendor,
model, brand, and product name leaked across the tree once before (a regression
of the PR-103 scrub). This test scans the actual git-tracked tree so any
re-introduction fails CI immediately.

The references are kept vendor-neutral (e.g. "a real RFC-compliant SIP gateway")
so the interop WHY of each is preserved without naming the device.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# This guard test legitimately must SPELL the banned tokens to match against
# them. To avoid the test itself being a tracked occurrence that the scan would
# flag, each banned token is assembled from fragments at runtime and the scan
# explicitly excludes this file from the tracked-tree walk.
_BANNED_CASE_INSENSITIVE: tuple[str, ...] = (
    "grand" + "stream",
    "u" + "cm6304",
    "gd" + "ms",
)
# Word-boundary token (the vendor product name) — matched case-sensitively as a
# whole word so the unrelated orchestration "wave" concept is not flagged.
_BANNED_WORD: str = "WA" + "VE"

_THIS_FILE = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()


def _tracked_files() -> list[str]:
    git = shutil.which("git")
    assert git is not None, "git executable not found on PATH"
    out = subprocess.run(  # noqa: S603 — git is resolved from PATH via shutil.which; args are fixed literals
        [git, "ls-files"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line and line != _THIS_FILE]


def _scan(matches: object) -> list[str]:
    assert isinstance(matches, re.Pattern)
    hits: list[str] = []
    for rel in _tracked_files():
        path = _REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if matches.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def test_no_vendor_model_brand_in_tracked_tree() -> None:
    pattern = re.compile("|".join(_BANNED_CASE_INSENSITIVE), re.IGNORECASE)
    hits = _scan(pattern)
    assert not hits, (
        "Banned vendor/model/brand identifier in tracked tree:\n" + "\n".join(hits)
    )


def test_no_vendor_product_word_in_tracked_tree() -> None:
    pattern = re.compile(r"\b" + _BANNED_WORD + r"\b")
    hits = _scan(pattern)
    assert not hits, "Banned vendor product name in tracked tree:\n" + "\n".join(hits)
