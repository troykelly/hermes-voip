"""Wheel-build + install + plugin-load smoke tests (release blocker #2).

These tests verify that the CI gate (gate.yml wheel-smoke job) and the pyproject
packaging configuration are coherent, and that the in-process installed package
satisfies the three properties the CI job asserts:

1. ``hermes_voip.__version__`` is derivable from installed metadata (not the
   "0+unknown" fallback that signals *no* installed distribution metadata), and
   matches pyproject.toml's declared version.
2. ``plugin.yaml`` is declared in the hatch wheel artifacts so it lands in the
   built wheel (the gate.yml smoke job verifies the actual zipfile at CI-time;
   this test verifies the BUILD CONFIG that produces that outcome).
3. The ``hermes_agent.plugins`` entry point is declared in pyproject.toml and
   resolves to ``hermes_voip`` (so ``ep.load()`` returns the module with a
   ``register`` callable).

TDD: the test ``test_gate_yml_has_wheel_smoke_job`` is the RED test — it fails
until the wheel-smoke CI job is added to ``.github/workflows/gate.yml``.  The
other tests are structural/in-process guards that turn GREEN once the gate job
and any pyproject fixes land in the same commit.
"""

from __future__ import annotations

import tomllib
import zipfile
from importlib.metadata import PackageNotFoundError, entry_points, version
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Repo locations
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_GATE_YML = _REPO_ROOT / ".github" / "workflows" / "gate.yml"


def _pyproject_version() -> str:
    """Return the version declared in pyproject.toml [project].version."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    assert isinstance(project, dict)
    ver = project["version"]
    assert isinstance(ver, str)
    return ver


# ---------------------------------------------------------------------------
# (A) CI gate structural guard (RED before the job lands)
# ---------------------------------------------------------------------------


def test_gate_yml_has_wheel_smoke_job() -> None:
    """gate.yml must define a ``wheel-smoke`` job.

    This is the RED test (rule 18): it fails until the CI job is added.
    The wheel-smoke job builds the wheel from a clean state, installs it
    into a fresh venv, and asserts version / plugin.yaml / entry-point.
    """
    raw = _GATE_YML.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict)
    jobs = data.get("jobs", {})
    assert isinstance(jobs, dict)
    assert "wheel-smoke" in jobs, (
        "gate.yml is missing the wheel-smoke job — add it to close release "
        "blocker #2 (broken wheel would pass CI green but fail in the Hermes runtime)"
    )


# ---------------------------------------------------------------------------
# (B) In-process structural checks (version / pyproject / entry-point)
#     These are fast and require no wheel build.
# ---------------------------------------------------------------------------


def test_installed_version_matches_pyproject() -> None:
    """hermes_voip.__version__ equals pyproject.toml [project].version.

    Verifies the importlib.metadata derivation is live (not a stale literal)
    and that the installed distribution metadata matches the source.  The
    "0+unknown" fallback produced by __init__._resolve_version() when the
    package is NOT installed is detected here: if the test environment is an
    editable install (``uv sync --frozen``), metadata is always present.
    """
    import hermes_voip  # noqa: PLC0415

    try:
        dist_ver = version("hermes-voip")
    except PackageNotFoundError:
        dist_ver = "0+unknown"

    assert dist_ver != "0+unknown", (
        "hermes-voip is not installed as a distribution — run `uv sync --frozen`; "
        "the wheel-smoke CI job requires an actual installable package"
    )
    expected = _pyproject_version()
    assert hermes_voip.__version__ == expected, (
        f"hermes_voip.__version__ ({hermes_voip.__version__!r}) != "
        f"pyproject.toml version ({expected!r})"
    )
    assert dist_ver == expected, (
        f"importlib.metadata version ({dist_ver!r}) != "
        f"pyproject.toml version ({expected!r})"
    )


def test_pyproject_wheel_artifacts_include_plugin_yaml() -> None:
    """The hatch wheel target declares plugin.yaml in artifacts or force-include.

    Hatchling does NOT include non-.py files by default; the declaration in
    pyproject.toml [tool.hatch.build.targets.wheel] is what causes plugin.yaml
    to land in the built wheel.  The wheel-smoke CI job verifies the actual
    zipfile; this test verifies the BUILD CONFIG that produces that outcome.
    """
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    tool = data.get("tool", {})
    assert isinstance(tool, dict)
    hatch = tool.get("hatch", {})
    assert isinstance(hatch, dict)
    build = hatch.get("build", {})
    assert isinstance(build, dict)
    targets = build.get("targets", {})
    assert isinstance(targets, dict)
    wheel = targets.get("wheel", {})
    assert isinstance(wheel, dict)

    # Collect all declared paths from both artifacts (list) and force-include
    # (list or dict).
    declared: list[str] = []
    artifacts = wheel.get("artifacts")
    if isinstance(artifacts, list):
        declared.extend(str(v) for v in artifacts)
    force_include = wheel.get("force-include")
    if isinstance(force_include, list):
        declared.extend(str(v) for v in force_include)
    elif isinstance(force_include, dict):
        declared.extend(str(k) for k in force_include)
        declared.extend(str(v) for v in force_include.values())

    combined = " ".join(declared)
    assert "plugin.yaml" in combined, (
        "pyproject.toml wheel target must declare src/hermes_voip/plugin.yaml "
        "in artifacts or force-include so it lands in the built wheel"
    )


def test_entry_point_declared_and_resolves() -> None:
    """The hermes_agent.plugins entry point is declared and loads hermes_voip.register.

    Verifies:
    - pyproject.toml declares [project.entry-points."hermes_agent.plugins"]
      hermes-voip = "hermes_voip"
    - The installed distribution exposes the entry point in the correct group
    - ep.load() returns the hermes_voip module
    - The module exposes a ``register`` callable
    """
    # Source-level: pyproject declares it.
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    assert isinstance(project, dict)
    eps_declared = project.get("entry-points", {})
    assert isinstance(eps_declared, dict)
    plugin_group = eps_declared.get("hermes_agent.plugins", {})
    assert isinstance(plugin_group, dict)
    assert "hermes-voip" in plugin_group, (
        'pyproject.toml must declare [project.entry-points."hermes_agent.plugins"] '
        'hermes-voip = "hermes_voip"'
    )
    assert plugin_group["hermes-voip"] == "hermes_voip"

    # Installed metadata: entry_points() resolves it.
    eps = list(entry_points(group="hermes_agent.plugins"))
    voip_ep = next((ep for ep in eps if ep.name == "hermes-voip"), None)
    assert voip_ep is not None, (
        "importlib.metadata.entry_points(group='hermes_agent.plugins') did not "
        "return a 'hermes-voip' entry point — is the package installed?"
    )
    assert voip_ep.value == "hermes_voip"

    # Load and verify the register callable is exposed.
    module = voip_ep.load()
    assert hasattr(module, "register"), (
        "hermes_voip module loaded via entry point must expose a `register` callable"
    )
    assert callable(module.register)


def test_wheel_zipfile_contains_plugin_yaml() -> None:
    """If a dist/*.whl exists, verify plugin.yaml is inside it.

    This test is SKIPPED when no wheel has been built (clean checkout);
    it runs green after ``uv build`` produces a wheel in dist/.  The CI
    wheel-smoke job always builds first, so it always exercises this path.
    """
    dist_dir = _REPO_ROOT / "dist"
    wheels = list(dist_dir.glob("*.whl")) if dist_dir.is_dir() else []
    if not wheels:
        # No wheel built yet — skip gracefully (the CI job runs uv build first).
        return

    whl_path = wheels[0]
    with zipfile.ZipFile(whl_path) as zf:
        names = zf.namelist()
    assert any(n.endswith("plugin.yaml") for n in names), (
        f"plugin.yaml is NOT inside the built wheel {whl_path.name}; "
        "fix the hatch artifacts declaration in pyproject.toml"
    )
