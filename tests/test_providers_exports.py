"""Package-boundary import test for hermes_voip.providers (ADR-0004).

Asserts that every ADR-0004 public name is importable directly from
``hermes_voip.providers`` and is the same object as its home submodule's definition.
This is a boundary / API-surface test: it fails if __init__.py stops re-exporting a
name, or if a re-export points at a different object than the canonical definition.
"""

from __future__ import annotations

import importlib

import pytest

# ---------------------------------------------------------------------------
# Table-driven parametrize: (name, home_module)
# Each entry asserts that `hermes_voip.providers.<name>` is the same object as
# `hermes_voip.providers.<submodule>.<name>`.
# ---------------------------------------------------------------------------

ADR0004_PUBLIC_NAMES: list[tuple[str, str]] = [
    # build.py
    ("Providers", "build"),
    ("build_providers", "build"),
    ("AsrFactory", "build"),
    ("TtsFactory", "build"),
    ("GuardFactory", "build"),
    # asr.py
    ("StreamingASR", "asr"),
    ("Transcript", "asr"),
    # tts.py
    ("StreamingTTS", "tts"),
    ("TtsStream", "tts"),
    # guard.py
    ("InjectionGuard", "guard"),
    ("GuardResult", "guard"),
    ("GuardVerdict", "guard"),
    # audio.py
    ("PcmFrame", "audio"),
    # transport.py
    ("MediaTransport", "transport"),
]


@pytest.mark.parametrize("name", [name for name, _ in ADR0004_PUBLIC_NAMES])
def test_name_importable_from_providers_package(name: str) -> None:
    """Each ADR-0004 name is importable from hermes_voip.providers."""
    pkg = importlib.import_module("hermes_voip.providers")
    assert hasattr(pkg, name), (
        f"hermes_voip.providers is missing {name!r}; "
        f"add it to providers/__init__.py __all__ re-exports"
    )


@pytest.mark.parametrize(
    ("name", "submodule"),
    ADR0004_PUBLIC_NAMES,
    ids=[name for name, _ in ADR0004_PUBLIC_NAMES],
)
def test_name_is_same_object_as_deep_path(name: str, submodule: str) -> None:
    """Re-export from hermes_voip.providers equals its deep-path origin."""
    pkg = importlib.import_module("hermes_voip.providers")
    deep = importlib.import_module(f"hermes_voip.providers.{submodule}")
    pkg_obj = getattr(pkg, name, None)
    deep_obj = getattr(deep, name)
    assert pkg_obj is deep_obj, (
        f"hermes_voip.providers.{name} is not the same object as "
        f"hermes_voip.providers.{submodule}.{name}; "
        f"the re-export must use `from hermes_voip.providers.{submodule} import {name}`"
    )


def test_providers_all_contains_all_adr0004_names() -> None:
    """hermes_voip.providers.__all__ declares every ADR-0004 public name."""
    pkg = importlib.import_module("hermes_voip.providers")
    pkg_all: list[str] = getattr(pkg, "__all__", [])
    expected = {name for name, _ in ADR0004_PUBLIC_NAMES}
    missing = expected - set(pkg_all)
    assert not missing, (
        f"These ADR-0004 names are absent from hermes_voip.providers.__all__: "
        f"{sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# guard.py __all__ — star-import exposes exactly the public names
# ---------------------------------------------------------------------------

_GUARD_PUBLIC_NAMES = {"GuardVerdict", "GuardResult", "InjectionGuard"}


def test_guard_module_has_all() -> None:
    """hermes_voip.providers.guard defines __all__."""
    guard = importlib.import_module("hermes_voip.providers.guard")
    assert hasattr(guard, "__all__"), (
        "hermes_voip.providers.guard is missing __all__; "
        "add it listing the public names"
    )


def test_guard_all_contains_public_names() -> None:
    """guard.__all__ contains every expected public name."""
    guard = importlib.import_module("hermes_voip.providers.guard")
    declared: set[str] = set(guard.__all__)
    missing = _GUARD_PUBLIC_NAMES - declared
    assert not missing, f"guard.__all__ is missing: {sorted(missing)}"


def test_guard_all_contains_no_private_names() -> None:
    """guard.__all__ must not expose underscore-private names."""
    guard = importlib.import_module("hermes_voip.providers.guard")
    declared: set[str] = set(guard.__all__)
    private = {n for n in declared if n.startswith("_")}
    assert not private, f"guard.__all__ exposes private names: {sorted(private)}"


def test_guard_star_import_exposes_public_names() -> None:
    """Star-import from guard makes every public name available."""
    guard = importlib.import_module("hermes_voip.providers.guard")
    for name in _GUARD_PUBLIC_NAMES:
        assert hasattr(guard, name), f"guard is missing public name {name!r}"


# ---------------------------------------------------------------------------
# transport.py __all__ — star-import exposes exactly the public names
# ---------------------------------------------------------------------------

_TRANSPORT_PUBLIC_NAMES = {"MediaTransport"}


def test_transport_module_has_all() -> None:
    """hermes_voip.providers.transport defines __all__."""
    transport = importlib.import_module("hermes_voip.providers.transport")
    assert hasattr(transport, "__all__"), (
        "hermes_voip.providers.transport is missing __all__; "
        "add it listing the public names"
    )


def test_transport_all_contains_public_names() -> None:
    """transport.__all__ contains every expected public name."""
    transport = importlib.import_module("hermes_voip.providers.transport")
    declared: set[str] = set(transport.__all__)
    missing = _TRANSPORT_PUBLIC_NAMES - declared
    assert not missing, f"transport.__all__ is missing: {sorted(missing)}"


def test_transport_all_contains_no_private_names() -> None:
    """transport.__all__ must not expose underscore-private names."""
    transport = importlib.import_module("hermes_voip.providers.transport")
    declared: set[str] = set(transport.__all__)
    private = {n for n in declared if n.startswith("_")}
    assert not private, f"transport.__all__ exposes private names: {sorted(private)}"
