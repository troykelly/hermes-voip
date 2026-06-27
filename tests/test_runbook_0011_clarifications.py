"""Runbook 0011 must include required clarifications about plugin discovery and install.

TDD (rule 18): This test documents two required clarifications to
docs/runbooks/0011-voip-enable-plugin.md:

1. Cross-reference to the upstream discovery bug (NousResearch/hermes-agent#23802)
   in the "Why not fix the CLI instead?" section, explaining that the CLI is
   filesystem-only and never consults importlib.metadata entry-points.

2. A WARNING block under the install section clarifying that Hermes imports
   hermes_voip from the pip-installed site-packages copy, not from any
   git-cloned ~/.hermes/plugins/ directory.

These are documentation-only clarifications per the spec (#244, #243).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNBOOK = _REPO_ROOT / "docs" / "runbooks" / "0011-voip-enable-plugin.md"


def test_runbook_0011_exists() -> None:
    """The runbook file exists at the expected location."""
    assert _RUNBOOK.is_file(), f"runbook 0011 missing at {_RUNBOOK}"


def test_runbook_0011_mentions_upstream_issue_23802() -> None:
    """Reference to NousResearch/hermes-agent#23802 in 'Why not fix CLI' section.

    Explains that hermes plugins enable/list CLI is filesystem-only and never
    consults importlib.metadata entry-points, making pip entry-point plugins
    invisible to it. The issue tracker link lets operators watch for CLI
    entry-point awareness.
    """
    content = _RUNBOOK.read_text(encoding="utf-8")
    assert "NousResearch/hermes-agent#23802" in content or (
        "hermes-agent#23802" in content
    ), (
        "runbook 0011 'Why not fix the CLI instead?' section must cross-reference "
        "the upstream issue NousResearch/hermes-agent#23802 explaining why the CLI "
        "is filesystem-only and never consults entry-points"
    )


def test_runbook_0011_install_section_has_warning_about_site_packages() -> None:
    """WARNING in install section about Hermes importing from site-packages copy.

    Clarifies that local patch must be applied to site-packages copy (or to
    both), not just git-cloned ~/.hermes/plugins/ directory. Runtime imports
    from pip-installed site-packages location, not directory-install copy.
    """
    content = _RUNBOOK.read_text(encoding="utf-8")
    # Look for a WARNING or alert block somewhere in the install-related section
    # that explains the site-packages copy behavior.
    has_warning = "WARNING" in content or "warning" in content
    assert has_warning, (
        "runbook 0011 install section must include a WARNING block clarifying that "
        "Hermes imports from the pip-installed site-packages copy, not from any "
        "git-cloned ~/.hermes/plugins/ directory"
    )
    # Verify the warning mentions the site-packages / pip install concept
    assert (
        "site-packages" in content.lower()
        or "pip-installed" in content.lower()
        or "pip install" in content.lower()
    ), "the WARNING must clarify that the runtime uses the pip-installed package copy"
