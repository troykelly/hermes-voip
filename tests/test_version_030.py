"""Version-pin test for the v0.3.0 release.

These tests assert that the package has been bumped to 0.3.0 and that the
CHANGELOG [Unreleased] section has been replaced with a proper [0.3.0] section
(with entries) plus the matching compare links. They fail against a tree still on
the previous version and turn green once pyproject.toml, plugin.yaml x2, and
CHANGELOG.md are updated for the release.

This is the per-release version ratchet — see the "Version-pin ratchet test" note
in docs/runbooks/0019-release-process.md. No implementation is changed here. A
follow-up (docs/backlog.md) tracks generalising this so the expected version is
derived from pyproject.toml and the file need not be renamed+rewritten each cut.
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

_EXPECTED_VERSION = "0.3.0"


def _pyproject_version() -> str:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    assert isinstance(project, dict)
    version = project["version"]
    assert isinstance(version, str)
    return version


def test_pyproject_version_is_030() -> None:
    """pyproject.toml [project].version must be 0.3.0 after the release bump."""
    got = _pyproject_version()
    assert got == _EXPECTED_VERSION, (
        f"pyproject.toml version is {got!r}; expected {_EXPECTED_VERSION!r}. "
        "Bump [project].version to 0.3.0."
    )


def test_source_plugin_yaml_version_is_030() -> None:
    """src/hermes_voip/plugin.yaml version must be 0.3.0 after the release bump."""
    import yaml  # noqa: PLC0415

    data = yaml.safe_load(_SOURCE_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    manifest_version = data.get("version")
    assert manifest_version == _EXPECTED_VERSION, (
        f"src/hermes_voip/plugin.yaml version is {manifest_version!r}; "
        f"expected {_EXPECTED_VERSION!r}. Update the version: field."
    )


def test_packaging_plugin_yaml_version_is_030() -> None:
    """packaging/.../plugin.yaml version must be 0.3.0 after the release bump."""
    import yaml  # noqa: PLC0415

    data = yaml.safe_load(_PACKAGING_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    manifest_version = data.get("version")
    assert manifest_version == _EXPECTED_VERSION, (
        f"packaging/.../plugin.yaml version is {manifest_version!r}; "
        f"expected {_EXPECTED_VERSION!r}. Update the version: field."
    )


def test_changelog_has_030_section() -> None:
    """CHANGELOG.md must contain a ## [0.3.0] release section."""
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    assert f"## [{_EXPECTED_VERSION}]" in changelog, (
        f"CHANGELOG.md has no ## [{_EXPECTED_VERSION}] section. "
        "Move [Unreleased] entries into a new [0.3.0] section with today's date."
    )


def test_changelog_030_section_is_not_empty() -> None:
    """The ## [0.3.0] section must contain at least one entry."""
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    header = f"## [{_EXPECTED_VERSION}]"
    idx = changelog.find(header)
    assert idx != -1, f"{header} section not found in CHANGELOG.md"
    # Slice from the section header to the next ## header or end of file.
    rest = changelog[idx + len(header) :]
    next_section = rest.find("\n## [")
    section_body = rest[:next_section] if next_section != -1 else rest
    # Must have at least one ### subsection (Added / Changed / Fixed / Security)
    assert "### " in section_body, (
        f"{header} section has no ### subsections "
        "(Added/Changed/Fixed/Security). Populate it with real entries."
    )


def test_changelog_unreleased_link_points_to_030() -> None:
    """[Unreleased] compare link at the bottom must point to v0.3.0...HEAD."""
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    # The keep-a-changelog convention places a link like:
    #   [Unreleased]: https://github.com/.../compare/v0.3.0...HEAD
    assert f"v{_EXPECTED_VERSION}...HEAD" in changelog, (
        "The [Unreleased] compare link at the bottom of CHANGELOG.md must point "
        "to v0.3.0...HEAD after the 0.3.0 release. Update the link section."
    )


def test_changelog_has_030_compare_link() -> None:
    """CHANGELOG.md must have a [0.3.0] compare link at the bottom."""
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    # E.g.: [0.3.0]: https://github.com/.../compare/v0.2.0...v0.3.0
    assert f"[{_EXPECTED_VERSION}]:" in changelog, (
        "CHANGELOG.md has no [0.3.0]: link at the bottom. "
        "Add a compare link from v0.2.0...v0.3.0."
    )
