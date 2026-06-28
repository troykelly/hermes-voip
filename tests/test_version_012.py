"""Version-pin test for v0.1.2 release.

TDD red commit: these tests assert that the package has been bumped to 0.1.2
and that the CHANGELOG [Unreleased] section has been replaced with a proper
[0.1.2] section.  They fail against the as-yet-unchanged 0.1.1 tree and must
turn green once pyproject.toml, plugin.yaml x2, and CHANGELOG.md are updated.

No implementation is changed by this test file.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_SOURCE_MANIFEST = _REPO_ROOT / "src" / "hermes_voip" / "plugin.yaml"
_PACKAGING_MANIFEST = (
    _REPO_ROOT / "packaging" / "hermes-plugins" / "hermes-voip" / "plugin.yaml"
)

_EXPECTED_VERSION = "0.1.2"


def _pyproject_version() -> str:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    assert isinstance(project, dict)
    version = project["version"]
    assert isinstance(version, str)
    return version


def test_pyproject_version_is_012() -> None:
    """pyproject.toml [project].version must be 0.1.2 after the release bump."""
    got = _pyproject_version()
    assert got == _EXPECTED_VERSION, (
        f"pyproject.toml version is {got!r}; expected {_EXPECTED_VERSION!r}. "
        "Bump [project].version to 0.1.2."
    )


def test_source_plugin_yaml_version_is_012() -> None:
    """src/hermes_voip/plugin.yaml version must be 0.1.2 after the release bump."""
    import yaml  # noqa: PLC0415

    data = yaml.safe_load(_SOURCE_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    manifest_version = data.get("version")
    assert manifest_version == _EXPECTED_VERSION, (
        f"src/hermes_voip/plugin.yaml version is {manifest_version!r}; "
        f"expected {_EXPECTED_VERSION!r}. Update the version: field."
    )


def test_packaging_plugin_yaml_version_is_012() -> None:
    """packaging/.../plugin.yaml version must be 0.1.2 after the release bump."""
    import yaml  # noqa: PLC0415

    data = yaml.safe_load(_PACKAGING_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    manifest_version = data.get("version")
    assert manifest_version == _EXPECTED_VERSION, (
        f"packaging/.../plugin.yaml version is {manifest_version!r}; "
        f"expected {_EXPECTED_VERSION!r}. Update the version: field."
    )


def test_changelog_has_012_section() -> None:
    """CHANGELOG.md must contain a ## [0.1.2] release section."""
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    assert "## [0.1.2]" in changelog, (
        "CHANGELOG.md has no ## [0.1.2] section. "
        "Move [Unreleased] entries into a new [0.1.2] section with today's date."
    )


def test_changelog_012_section_is_not_empty() -> None:
    """The ## [0.1.2] section must contain at least one entry."""
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    idx = changelog.find("## [0.1.2]")
    assert idx != -1, "## [0.1.2] section not found in CHANGELOG.md"
    # Slice from the section header to the next ## header or end of file.
    rest = changelog[idx + len("## [0.1.2]") :]
    next_section = rest.find("\n## [")
    section_body = rest[:next_section] if next_section != -1 else rest
    # Must have at least one ### subsection (Added / Changed / Fixed / Security)
    assert "### " in section_body, (
        "## [0.1.2] section has no ### subsections "
        "(Added/Changed/Fixed/Security). Populate it with real entries."
    )


def test_changelog_unreleased_link_points_to_012() -> None:
    """[Unreleased] compare link at the bottom must point to v0.1.2...HEAD."""
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    # The keep-a-changelog convention places a link like:
    #   [Unreleased]: https://github.com/.../compare/v0.1.2...HEAD
    assert "v0.1.2...HEAD" in changelog, (
        "The [Unreleased] compare link at the bottom of CHANGELOG.md must point "
        "to v0.1.2...HEAD after the 0.1.2 release.  Update the link section."
    )


def test_changelog_has_012_compare_link() -> None:
    """CHANGELOG.md must have a [0.1.2] compare link at the bottom."""
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    # E.g.: [0.1.2]: https://github.com/.../compare/v0.1.1...v0.1.2
    assert "[0.1.2]:" in changelog, (
        "CHANGELOG.md has no [0.1.2]: link at the bottom. "
        "Add a compare link from v0.1.1...v0.1.2."
    )
