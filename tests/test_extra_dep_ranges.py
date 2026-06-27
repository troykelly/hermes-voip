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
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _optional_deps() -> dict[str, list[str]]:
    """Return the optional-dependencies table from pyproject.toml."""
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    result: dict[str, list[str]] = data.get("project", {}).get(
        "optional-dependencies", {}
    )
    return result


# ---------------------------------------------------------------------------
# #240 — websockets must be a compatible range, not an exact pin
# ---------------------------------------------------------------------------


def test_websockets_is_not_exact_pin() -> None:
    """The websockets extra must NOT use an exact == pin."""
    deps = _optional_deps()
    webrtc = deps.get("webrtc", [])
    ws_entries = [d for d in webrtc if d.lower().startswith("websockets")]
    assert ws_entries, "websockets not found in [webrtc] extra"
    for entry in ws_entries:
        assert "==" not in entry or "!=" in entry or ">=" in entry, (
            f"websockets still uses exact == pin: {entry!r}"
        )
        # Must not be a bare == without >= (i.e. exact pin with no lower bound)
        # The check: must contain >= (compatible range lower bound)
        assert ">=" in entry, (
            f"websockets must use a compatible range (>=), got: {entry!r}"
        )


def test_websockets_range_covers_fifteen_zero() -> None:
    """Lower bound must be <=15.0 so it covers Hermes's transitive 15.0.1."""
    deps = _optional_deps()
    webrtc = deps.get("webrtc", [])
    ws_entries = [d for d in webrtc if d.lower().startswith("websockets")]
    for entry in ws_entries:
        # Extract the >= bound
        m = re.search(r">=\s*([\d.]+)", entry)
        assert m is not None, f"no >= bound found in {entry!r}"
        lower = tuple(int(x) for x in m.group(1).split("."))
        assert lower <= (15, 0), (
            f"websockets lower bound must be <=15.0 (to cover 15.0.1), got {lower}"
        )


def test_websockets_range_excludes_seventeen() -> None:
    """Upper bound must be <17 (gate against the next major)."""
    deps = _optional_deps()
    webrtc = deps.get("webrtc", [])
    ws_entries = [d for d in webrtc if d.lower().startswith("websockets")]
    for entry in ws_entries:
        m = re.search(r"<\s*([\d.]+)", entry)
        assert m is not None, f"no < upper bound found in {entry!r}"
        upper = tuple(int(x) for x in m.group(1).split("."))
        assert upper == (17,) or upper[0] <= 16, (
            f"websockets upper bound must be <17, got <{m.group(1)}"
        )


# ---------------------------------------------------------------------------
# #241 — cryptography must be a compatible range with security floor >=46.0.7
# ---------------------------------------------------------------------------


def test_cryptography_is_not_exact_pin() -> None:
    """The cryptography extra must NOT use an exact == pin."""
    deps = _optional_deps()
    media = deps.get("media", [])
    crypto_entries = [d for d in media if d.lower().startswith("cryptography")]
    assert crypto_entries, "cryptography not found in [media] extra"
    for entry in crypto_entries:
        assert ">=" in entry, (
            f"cryptography must use a compatible range (>=), got: {entry!r}"
        )


def test_cryptography_lower_bound_is_security_floor() -> None:
    """Lower bound must be >=46.0.7 — the CVE-2026-39892 security floor."""
    deps = _optional_deps()
    media = deps.get("media", [])
    crypto_entries = [d for d in media if d.lower().startswith("cryptography")]
    for entry in crypto_entries:
        m = re.search(r">=\s*([\d.]+)", entry)
        assert m is not None, f"no >= bound found in {entry!r}"
        lower = tuple(int(x) for x in m.group(1).split("."))
        # Lower bound must be exactly 46.0.7 (the CVE security floor).
        # Lowering this floor violates the security invariant.
        assert lower >= (46, 0, 7), (
            f"cryptography lower bound must be >=46.0.7 (CVE-2026-39892 floor), "
            f"got {m.group(1)}"
        )


def test_cryptography_upper_bound_excludes_49() -> None:
    """Upper bound must be <49 (pyopenssl==26.2.0 requires cryptography<49)."""
    deps = _optional_deps()
    media = deps.get("media", [])
    crypto_entries = [d for d in media if d.lower().startswith("cryptography")]
    for entry in crypto_entries:
        m = re.search(r"<\s*([\d.]+)", entry)
        assert m is not None, f"no < upper bound found in {entry!r}"
        upper = tuple(int(x) for x in m.group(1).split("."))
        assert upper == (49,) or upper[0] < 49, (
            f"cryptography upper bound must be <49, got <{m.group(1)}"
        )


# ---------------------------------------------------------------------------
# #239 — onnxruntime must carry a sys_platform != 'darwin' marker
# ---------------------------------------------------------------------------


def test_onnxruntime_has_platform_marker() -> None:
    """The onnxruntime entry must carry a sys_platform != 'darwin' marker."""
    deps = _optional_deps()
    ml = deps.get("ml", [])
    onnx_entries = [d for d in ml if d.lower().startswith("onnxruntime")]
    assert onnx_entries, "onnxruntime not found in [ml] extra"
    for entry in onnx_entries:
        assert "sys_platform" in entry, (
            f"onnxruntime must carry a sys_platform marker, got: {entry!r}"
        )
        assert "darwin" in entry, (
            f"onnxruntime marker must exclude darwin (macOS), got: {entry!r}"
        )
        assert "!=" in entry, (
            f"onnxruntime marker must be sys_platform != 'darwin', got: {entry!r}"
        )
