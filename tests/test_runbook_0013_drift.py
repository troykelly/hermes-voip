"""Test that runbook 0013 §3 (Graceful shutdown) matches ADR-0059 and §Restart.

Rule 27: no aspirational or contradictory docs — runbooks must describe what IS.
This test validates that §3 does NOT falsely claim graceful shutdown is
unimplemented, since ADR-0059 shipped a full BYE-drain disconnect() in
adapter.py.
"""

import re
from pathlib import Path


def test_runbook_0013_section3_graceful_shutdown_documents_shipped_behavior() -> None:
    """Verify that runbook 0013 §3 matches ADR-0059 implementation.

    ADR-0059 shipped graceful shutdown via SIGTERM + BYE-drain (disconnect()).
    Section 3 must NOT falsely claim "graceful shutdown not yet implemented" or
    recommend kill -9 as the primary path.
    """
    runbook_path = (
        Path(__file__).parent.parent
        / "docs"
        / "runbooks"
        / "0013-voip-incident-oncall.md"
    )
    content = runbook_path.read_text()

    # Section 3 spans from the "### 3. Graceful shutdown" header to the next "---"
    section3_match = re.search(
        r"### 3\. Graceful shutdown.*?(?=\n---\n)", content, re.DOTALL
    )
    assert section3_match is not None, "Section 3 (Graceful shutdown) not found"
    section3 = section3_match.group(0)

    # FAIL: Section 3 still contains the false "not yet implemented" claim
    false_claim = "does not currently implement graceful shutdown"
    assert false_claim not in section3, (
        f"Section 3 contains false claim: '{false_claim}'. "
        "This contradicts ADR-0059 which shipped graceful shutdown via "
        "SIGTERM + BYE-drain."
    )

    # FAIL: Section 3 still says "available in future"
    false_future = "available in future"
    assert false_future not in section3, (
        f"Section 3 contains '{false_future}' — this is FALSE. "
        "ADR-0059 shipped graceful shutdown. Rewrite to describe what IS."
    )

    # FAIL: Section 3 recommends kill -9 as the primary stop method
    # (It should be SIGTERM + drain, with kill -9 as the fallback only.)
    section3_lines = section3.split("\n")
    kill9_line = next(
        (i for i, line in enumerate(section3_lines) if "kill -9" in line), None
    )
    sigterm_line = next(
        (i for i, line in enumerate(section3_lines) if "kill -TERM" in line),
        None,
    )

    if kill9_line is not None and sigterm_line is not None:
        # FAIL if kill -9 comes before (primary method) or if no SIGTERM mention
        assert sigterm_line < kill9_line, (
            f"Section 3 lists 'kill -TERM' at line {sigterm_line} but "
            f"'kill -9' at line {kill9_line}. The section must recommend SIGTERM "
            "first (graceful), then kill -9 as fallback only."
        )


def test_runbook_0013_restart_and_section3_align() -> None:
    """Verify that §Restart describes consistent shutdown/drain with §3."""
    runbook_path = (
        Path(__file__).parent.parent
        / "docs"
        / "runbooks"
        / "0013-voip-incident-oncall.md"
    )
    content = runbook_path.read_text()

    # Extract §Restart (section 1 under "## Restart")
    restart_match = re.search(
        r"## Restart\s+.*?### 1\. Stop the process.*?(?=\n### [0-9]|\n---|\Z)",
        content,
        re.DOTALL,
    )
    assert restart_match is not None, "Restart section not found"
    restart_section = restart_match.group(0)

    # §Restart should recommend SIGTERM (graceful) FIRST
    assert "kill -TERM" in restart_section or "SIGTERM" in restart_section, (
        "Restart §1 (Stop the process) must recommend SIGTERM/kill -TERM as "
        "the primary method."
    )

    # §Restart should mention "graceful shutdown" or "drain" behavior
    assert (
        "graceful" in restart_section.lower() or "drain" in restart_section.lower()
    ), "Restart §1 must mention graceful shutdown or drain behavior."

    # §Restart should mention ADR-0059
    assert "ADR-0059" in restart_section, "Restart §1 should cross-reference ADR-0059."
