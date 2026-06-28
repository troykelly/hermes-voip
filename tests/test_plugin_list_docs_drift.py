"""Guard against drift in the plugin-list version docs.

Rule 27: the operator-facing docs must agree on what ``hermes plugins list``
shows once the directory manifest is installed. The CLI reads the directory
``plugin.yaml`` and therefore surfaces the shipped manifest version +
description there; the loaded ``/plugins`` view is the one that still reflects
the entry point empty version.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README = _REPO_ROOT / "README.md"
_RUNBOOK = _REPO_ROOT / "docs" / "runbooks" / "0011-voip-enable-plugin.md"


def test_readme_plugin_list_sentence_has_no_stray_paren() -> None:
    """README keeps the loaded-view sentence punctuation clean."""
    content = _README.read_text(encoding="utf-8")

    assert "`10 tools, 1 hook`.)" not in content, (
        "README still has the stray closing paren after the loaded /plugins "
        "tool-count sentence"
    )
    assert (
        "shows the loaded view with its tool count — `10 tools, 1 hook`." in content
    ), "README must state the loaded /plugins count sentence without stray punctuation"


def test_runbook_0011_describes_plugin_list_and_loaded_versions_honestly() -> None:
    """Runbook 0011 must not contradict itself about plugin-list version output."""
    content = _RUNBOOK.read_text(encoding="utf-8")

    assert "0.0.0" not in content, (
        "runbook 0011 still claims hermes plugins list shows 0.0.0; the CLI reads "
        "the shipped directory manifest version instead"
    )
    assert "directory manifest version + description" in content, (
        "runbook 0011 must say hermes plugins list shows the directory manifest "
        "version + description"
    )
    assert "entry point empty version" in content, (
        "runbook 0011 must document that the loaded /plugins view still shows the "
        "entry point empty version"
    )
