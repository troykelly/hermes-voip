"""Tests for Wave-10 backlog check-off (docs/backlog.md, PRs #323-#332).

Verifies:
1. The eight PRs with clear backlog matches (#323-#327, #330-#332) are
   checked off with ``[x]`` and a ``(#NNN)`` provenance marker.
2. The ``transfer_attended`` comment in ``voip_tools.py`` no longer claims
   the tool is "deliberately NOT exposed" or "deferred" — both descriptions
   were stale after ADR-0048 shipped the full implementation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKLOG = Path(__file__).parent.parent / "docs" / "backlog.md"
_VOIP_TOOLS = Path(__file__).parent.parent / "src" / "hermes_voip" / "voip_tools.py"

# PRs with clear unchecked-backlog matches that must be checked off.
# PRs #328 and #329 are excluded: no single unchecked line had a sufficiently
# clear match (IPv6 hold and duplicate-CL items were not in the unchecked set).
_WAVE10_PRS = [323, 324, 325, 326, 327, 330, 331, 332]


def _backlog_text() -> str:
    return _BACKLOG.read_text(encoding="utf-8")


def _voip_tools_text() -> str:
    return _VOIP_TOOLS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Wave-10 PRs are checked off in backlog.md
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pr", _WAVE10_PRS)
def test_wave10_pr_is_checked_off(pr: int) -> None:
    """Each Wave-10 PR must appear on a ``[x]`` line with a ``(#NNN)`` marker."""
    text = _backlog_text()
    pattern = rf"^\- \[x\].*\(.*#{pr}(?!\d).*\)"
    assert re.search(pattern, text, re.MULTILINE), (
        f"PR #{pr} is not yet checked off in docs/backlog.md. "
        "Find its matching unchecked item and set [x] (#NNN)."
    )


# ---------------------------------------------------------------------------
# 2. Stale ``transfer_attended`` comment must be corrected (rule 27)
# ---------------------------------------------------------------------------


def test_transfer_attended_not_described_as_unexposed() -> None:
    """The ``transfer_attended`` section must not claim the tool is unexposed.

    ADR-0048 shipped the full consultative-transfer implementation; the old
    comment ``transfer_attended is deliberately NOT exposed — it needs a
    consult-leg Dialog the agent cannot originate (deferred, ADR-0031 §4)``
    was a rule-27 violation.  After the fix the comment must accurately
    describe the shipped reality.
    """
    src = _voip_tools_text()
    # Locate the paragraph that discusses transfer_attended (the module-level
    # docstring comment block above TRANSFER_BLIND_TOOL_NAME).
    # The stale claim used the exact phrase "deliberately NOT exposed".
    assert "deliberately NOT exposed" not in src, (
        "voip_tools.py still contains the stale 'deliberately NOT exposed' "
        "claim for transfer_attended.  Rewrite the comment to reflect that "
        "transfer_attended IS exposed and fully wired (ADR-0048, plugin.yaml)."
    )


def test_transfer_attended_not_described_as_deferred_in_comment() -> None:
    """The ``transfer_attended`` block must not claim a deferral.

    The word 'deferred' in the same sentence as 'transfer_attended' was
    aspirational / stale after ADR-0048 closed the deferral (2026-06-18).
    """
    src = _voip_tools_text()
    # Find the TRANSFER_BLIND_TOOL_NAME comment block (the two lines before
    # the constant definition).  We search for the exact stale phrase.
    assert "(deferred, ADR-0031 §4)" not in src, (
        "voip_tools.py still contains the stale deferral note "
        "'(deferred, ADR-0031 §4)' for transfer_attended.  "
        "Update the comment to state what is actually shipped."
    )
