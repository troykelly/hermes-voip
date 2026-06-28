"""Verify that plugin extras use compatible ranges, not exact == pins.

A Hermes plugin is a LIBRARY co-installed with the host runtime and other
plugins.  Exact ``==`` pins in ``[project.optional-dependencies]`` create
unsatisfiable conflicts when the host env pins a different version.  The
three extras relaxed by #239/#240/#241 must now express compatible ranges:

  * ``websockets``  (webrtc extra)  — ``>=15.0,<17``
  * ``cryptography`` (media extra)  — ``>=46.0.7,<49``
  * ``onnxruntime``  (ml extra)     — ``==1.24.4`` with a platform marker
    restricting it to non-macOS (``; sys_platform != 'darwin'``)

This module is a pure static-analysis test: it parses ``pyproject.toml``
(no build tooling or imports needed) so it runs in the default install
(no extras required).

Assertions use ``packaging.requirements.Requirement`` / ``SpecifierSet`` /
``packaging.markers.Marker`` for semantic correctness — substring heuristics
are not sufficient to prove range semantics.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
_UV_LOCK = Path(__file__).resolve().parent.parent / "uv.lock"


def _optional_deps() -> dict[str, list[str]]:
    """Return the optional-dependencies table from pyproject.toml."""
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    result: dict[str, list[str]] = data.get("project", {}).get(
        "optional-dependencies", {}
    )
    return result


def _get_req(extra: str, pkg_prefix: str) -> Requirement:
    """Parse and return the first Requirement matching pkg_prefix in the extra."""
    deps = _optional_deps()
    entries = [d for d in deps.get(extra, []) if d.lower().startswith(pkg_prefix)]
    assert entries, f"{pkg_prefix!r} not found in [{extra}] extra"
    return Requirement(entries[0])


# ---------------------------------------------------------------------------
# #240 — websockets must be a compatible range, not an exact pin
# ---------------------------------------------------------------------------


def test_websockets_is_not_exact_pin() -> None:
    """The websockets extra must NOT use an exact == pin."""
    req = _get_req("webrtc", "websockets")
    # An exact pin would be SpecifierSet("==16.0") with no other specifiers.
    # A compatible range has >=, so it must contain multiple version operators.
    specs = req.specifier
    ops = {s.operator for s in specs}
    assert "==" not in ops or ">=" in ops, (
        f"websockets still uses exact == pin with no >= lower bound: {req}"
    )
    assert ">=" in ops, f"websockets must use a compatible range (>=), got: {req}"


def test_websockets_accepts_16_0() -> None:
    """SpecifierSet must accept websockets 16.0 (the locked resolution)."""
    req = _get_req("webrtc", "websockets")
    assert req.specifier.contains("16.0", prereleases=False), (
        f"websockets specifier {req.specifier!r} must accept 16.0"
    )


def test_websockets_accepts_15_0_1() -> None:
    """SpecifierSet must accept websockets 15.0.1 (Hermes transitive dep)."""
    req = _get_req("webrtc", "websockets")
    assert req.specifier.contains("15.0.1", prereleases=False), (
        f"websockets specifier {req.specifier!r} must accept 15.0.1 "
        "(Hermes transitive websockets==15.0.1 via uvicorn[standard])"
    )


def test_websockets_rejects_17_0() -> None:
    """SpecifierSet must reject websockets 17.0 (next major, gated by <17)."""
    req = _get_req("webrtc", "websockets")
    assert not req.specifier.contains("17.0", prereleases=False), (
        f"websockets specifier {req.specifier!r} must reject 17.0 (<17 upper bound)"
    )


def test_websockets_rejects_14_0() -> None:
    """SpecifierSet must reject websockets 14.0 (below lower bound >=15.0)."""
    req = _get_req("webrtc", "websockets")
    assert not req.specifier.contains("14.0", prereleases=False), (
        f"websockets specifier {req.specifier!r} must reject 14.0 (below >=15.0)"
    )


# ---------------------------------------------------------------------------
# #241 — cryptography must be a compatible range with security floor >=46.0.7
# ---------------------------------------------------------------------------


def test_cryptography_is_not_exact_pin() -> None:
    """The cryptography extra must NOT use an exact == pin."""
    req = _get_req("media", "cryptography")
    ops = {s.operator for s in req.specifier}
    assert ">=" in ops, f"cryptography must use a compatible range (>=), got: {req}"


def test_cryptography_lower_bound_is_security_floor() -> None:
    """Lower bound must be >=46.0.7 — the CVE-2026-39892 security floor."""
    req = _get_req("media", "cryptography")
    # The floor is 46.0.7; verify by checking that 46.0.6 is rejected.
    assert not req.specifier.contains("46.0.6", prereleases=False), (
        f"cryptography specifier {req.specifier!r} must reject 46.0.6 "
        "(CVE-2026-39892 was not fixed until 46.0.7 — floor must not be lowered)"
    )


def test_cryptography_accepts_48_0_1() -> None:
    """SpecifierSet must accept cryptography 48.0.1 (the locked resolution)."""
    req = _get_req("media", "cryptography")
    assert req.specifier.contains("48.0.1", prereleases=False), (
        f"cryptography specifier {req.specifier!r} must accept 48.0.1"
    )


def test_cryptography_accepts_46_0_7() -> None:
    """SpecifierSet must accept cryptography 46.0.7 (the CVE security floor)."""
    req = _get_req("media", "cryptography")
    assert req.specifier.contains("46.0.7", prereleases=False), (
        f"cryptography specifier {req.specifier!r} must accept 46.0.7 "
        "(the security floor itself)"
    )


def test_cryptography_rejects_46_0_6() -> None:
    """SpecifierSet must reject cryptography 46.0.6 (CVE-2026-39892 not fixed)."""
    req = _get_req("media", "cryptography")
    assert not req.specifier.contains("46.0.6", prereleases=False), (
        f"cryptography specifier {req.specifier!r} must reject 46.0.6 "
        "(below security floor — CVE-2026-39892)"
    )


def test_cryptography_rejects_49() -> None:
    """SpecifierSet must reject cryptography 49.0 (pyopenssl==26.2.0 requires <49)."""
    req = _get_req("media", "cryptography")
    assert not req.specifier.contains("49.0", prereleases=False), (
        f"cryptography specifier {req.specifier!r} must reject 49.0 "
        "(pyopenssl==26.2.0 requires cryptography<49)"
    )


def test_cryptography_upper_bound_excludes_49() -> None:
    """Upper bound must be <49 (pyopenssl==26.2.0 requires cryptography<49)."""
    req = _get_req("media", "cryptography")
    ops = {s.operator for s in req.specifier}
    assert "<" in ops, f"cryptography must have an < upper bound, got: {req}"
    upper_specs = [s for s in req.specifier if s.operator == "<"]
    for spec in upper_specs:
        upper = tuple(int(x) for x in spec.version.split("."))
        assert upper <= (49,), (
            f"cryptography upper bound must be <49, got <{spec.version}"
        )


# ---------------------------------------------------------------------------
# #239 — onnxruntime must carry a sys_platform != 'darwin' marker
# ---------------------------------------------------------------------------


def test_onnxruntime_has_platform_marker() -> None:
    """The onnxruntime entry must carry a sys_platform != 'darwin' marker."""
    req = _get_req("ml", "onnxruntime")
    assert req.marker is not None, (
        f"onnxruntime must carry a sys_platform marker, got no marker: {req}"
    )


def test_onnxruntime_marker_evaluates_true_on_linux() -> None:
    """Marker must be True for sys_platform='linux' (non-macOS installs run it)."""
    req = _get_req("ml", "onnxruntime")
    assert req.marker is not None, "onnxruntime must carry a marker"
    # packaging.markers.Marker.evaluate() takes an environment dict
    marker = Marker(str(req.marker))
    result = marker.evaluate({"sys_platform": "linux"})
    assert result is True, (
        f"onnxruntime marker {req.marker!r} must be True for sys_platform='linux' "
        "(proving sys_platform != 'darwin' semantically, not by substring)"
    )


def test_onnxruntime_marker_evaluates_false_on_darwin() -> None:
    """Marker must evaluate False for sys_platform='darwin' (macOS skips the wheel)."""
    req = _get_req("ml", "onnxruntime")
    assert req.marker is not None, "onnxruntime must carry a marker"
    marker = Marker(str(req.marker))
    result = marker.evaluate({"sys_platform": "darwin"})
    assert result is False, (
        f"onnxruntime marker {req.marker!r} must be False for sys_platform='darwin' "
        "(no macOS wheel for onnxruntime 1.24.4 on py3.13)"
    )


# ---------------------------------------------------------------------------
# Lockfile resolution — assert the repo currently resolves to the expected
# versions so future uv.lock drift is caught immediately.
# ---------------------------------------------------------------------------


def _locked_version(package: str) -> str:
    """Parse uv.lock and return the resolved version of a package."""
    content = _UV_LOCK.read_text(encoding="utf-8")
    # uv.lock TOML format: each package block starts with [[package]] then
    # name = "..." / version = "..." on subsequent lines.
    pattern = re.compile(
        r'\[\[package\]\]\s+name\s*=\s*"'
        + re.escape(package)
        + r'"\s+version\s*=\s*"([^"]+)"',
        re.MULTILINE,
    )
    m = pattern.search(content)
    assert m is not None, f"Package {package!r} not found in uv.lock"
    return m.group(1)


def test_lockfile_resolves_websockets_15_0_1() -> None:
    """uv.lock must resolve websockets to 15.0.1 (drift detection)."""
    version = _locked_version("websockets")
    assert version == "15.0.1", (
        f"uv.lock resolved websockets to {version!r}, expected 15.0.1. "
        "If intentional, update this test alongside the lockfile bump."
    )


def test_lockfile_resolves_cryptography_48_0_1() -> None:
    """uv.lock must resolve cryptography to 48.0.1 (drift detection)."""
    version = _locked_version("cryptography")
    assert version == "48.0.1", (
        f"uv.lock resolved cryptography to {version!r}, expected 48.0.1. "
        "If intentional, update this test alongside the lockfile bump."
    )


def test_lockfile_resolves_onnxruntime_1_24_4() -> None:
    """uv.lock must resolve onnxruntime to 1.24.4 (drift detection)."""
    version = _locked_version("onnxruntime")
    assert version == "1.24.4", (
        f"uv.lock resolved onnxruntime to {version!r}, expected 1.24.4. "
        "If intentional, update this test alongside the lockfile bump (and re-run "
        "the sherpa-onnx ABI compatibility check)."
    )
