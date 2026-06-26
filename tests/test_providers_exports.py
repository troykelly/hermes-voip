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


@pytest.mark.parametrize(
    ("name", "submodule"),
    ADR0004_PUBLIC_NAMES,
    ids=[name for name, _ in ADR0004_PUBLIC_NAMES],
)
def test_name_importable_from_providers_package(name: str, submodule: str) -> None:
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
