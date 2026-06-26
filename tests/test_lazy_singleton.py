"""Tests for the runtime-helper SHAPE handling in :class:`LazySingleton` (issue #201).

The Hermes ``plugins.plugin_utils.lazy_singleton`` helper exists in the wild in
two shapes:

* a *handle* object exposing ``get()`` + ``reset()`` (the shape this repo's shim
  originally assumed); and
* a *callable accessor* exposing ``reset()`` but NO ``get()`` — you **call** the
  accessor to obtain the value.

The shim originally drove ``runtime.get()`` unconditionally, so under a runtime
exposing the callable-accessor shape every path through ``LazySingleton.get()``
raised ``AttributeError: 'function' object has no attribute 'get'`` (surfaced via
``media/srtp.py`` ``_get_crypto`` -> ``_CRYPTO_SINGLETON.get()``). These tests pin
both supported runtime shapes plus the stdlib fallback for an unrecognised shape.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator

import pytest

from hermes_voip._lazy_singleton import LazySingleton


def _install_fake_plugin_utils(lazy_singleton: object) -> Iterator[None]:
    """Install a fake ``plugins.plugin_utils`` exposing ``lazy_singleton``.

    Yields with the module installed, then restores ``sys.modules`` so the rest of
    the suite (which legitimately falls back to stdlib) is unaffected.
    """
    pkg = types.ModuleType("plugins")
    pkg.__path__ = []  # mark as a package so the submodule import resolves
    sub = types.ModuleType("plugins.plugin_utils")
    sub.lazy_singleton = lazy_singleton  # type: ignore[attr-defined]  # test stub attr
    added = {"plugins": pkg, "plugins.plugin_utils": sub}
    saved = {name: sys.modules.get(name) for name in added}
    sys.modules.update(added)
    try:
        yield
    finally:
        for name, prior in saved.items():
            if prior is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior


# ---------------------------------------------------------------------------
# Shape (b): a CALLABLE ACCESSOR with .reset() and NO .get() (issue #201).
# ---------------------------------------------------------------------------


class _CallableAccessor:
    """The callable-accessor runtime shape: call to build-once; ``reset()``; no ``get``.

    This mirrors the Hermes runtime in issue #201 — ``lazy_singleton(factory)``
    returns an object you **call** to obtain the value, exposing ``reset()`` but no
    ``get()`` method.
    """

    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._built = False
        self._value: object = None

    def __call__(self) -> object:
        if not self._built:
            self._value = self._factory()
            self._built = True
        return self._value

    def reset(self) -> None:
        self._built = False
        self._value = None


@pytest.fixture
def _callable_accessor_runtime() -> Iterator[None]:
    """Install a runtime whose ``lazy_singleton`` returns a callable accessor."""
    accessors: list[_CallableAccessor] = []

    def lazy_singleton(factory: Callable[[], object]) -> _CallableAccessor:
        accessor = _CallableAccessor(factory)
        accessors.append(accessor)
        return accessor

    yield from _install_fake_plugin_utils(lazy_singleton)


@pytest.mark.usefixtures("_callable_accessor_runtime")
def test_get_supports_callable_accessor_runtime_shape() -> None:
    """``get()`` works when the runtime helper returns a callable accessor (no get).

    Reproduces issue #201: before the fix this raised
    ``AttributeError: 'function' object has no attribute 'get'`` because the shim
    called ``runtime.get()`` unconditionally.
    """
    calls = 0
    sentinel = object()

    def factory() -> object:
        nonlocal calls
        calls += 1
        return sentinel

    singleton: LazySingleton[object] = LazySingleton(factory)

    assert singleton.get() is sentinel
    # Build-once: a second get must not rebuild.
    assert singleton.get() is sentinel
    assert calls == 1


@pytest.mark.usefixtures("_callable_accessor_runtime")
def test_reset_rebuilds_with_callable_accessor_runtime_shape() -> None:
    """``reset()`` drops the value so next ``get()`` rebuilds (callable-accessor)."""
    calls = 0

    def factory() -> object:
        nonlocal calls
        calls += 1
        return object()

    singleton: LazySingleton[object] = LazySingleton(factory)

    first = singleton.get()
    assert calls == 1
    singleton.reset()
    second = singleton.get()
    assert calls == 2
    assert second is not first


# ---------------------------------------------------------------------------
# Shape (a): the documented HANDLE with .get() + .reset() still works.
# ---------------------------------------------------------------------------


class _HandleRuntime:
    """The documented handle shape: ``get()`` build-once + ``reset()``."""

    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._built = False
        self._value: object = None

    def get(self) -> object:
        if not self._built:
            self._value = self._factory()
            self._built = True
        return self._value

    def reset(self) -> None:
        self._built = False
        self._value = None


@pytest.fixture
def _handle_runtime() -> Iterator[None]:
    def lazy_singleton(factory: Callable[[], object]) -> _HandleRuntime:
        return _HandleRuntime(factory)

    yield from _install_fake_plugin_utils(lazy_singleton)


@pytest.mark.usefixtures("_handle_runtime")
def test_get_supports_handle_runtime_shape() -> None:
    """``get()``/``reset()`` work with the documented ``.get()``+``.reset()`` handle."""
    calls = 0
    sentinel = object()

    def factory() -> object:
        nonlocal calls
        calls += 1
        return sentinel

    singleton: LazySingleton[object] = LazySingleton(factory)
    assert singleton.get() is sentinel
    assert singleton.get() is sentinel
    assert calls == 1
    singleton.reset()
    assert singleton.get() is sentinel
    assert calls == 2


# ---------------------------------------------------------------------------
# Shape (c): an UNRECOGNISED runtime shape falls back to the stdlib singleton.
# ---------------------------------------------------------------------------


@pytest.fixture
def _unrecognised_runtime() -> Iterator[None]:
    """A ``lazy_singleton`` returning an object matching neither supported shape.

    It is neither callable nor exposes ``get`` — only an unrelated member — so the
    shim must fall back to the stdlib local singleton rather than crash.
    """

    class _Bogus:
        def reset(self) -> None:  # has reset but is not callable and has no get
            return None

    def lazy_singleton(factory: Callable[[], object]) -> _Bogus:
        return _Bogus()

    yield from _install_fake_plugin_utils(lazy_singleton)


@pytest.mark.usefixtures("_unrecognised_runtime")
def test_get_falls_back_to_stdlib_for_unrecognised_shape() -> None:
    """An unrecognised runtime shape falls back to the stdlib build-once path."""
    calls = 0
    sentinel = object()

    def factory() -> object:
        nonlocal calls
        calls += 1
        return sentinel

    singleton: LazySingleton[object] = LazySingleton(factory)
    assert singleton.get() is sentinel
    assert singleton.get() is sentinel
    assert calls == 1
    singleton.reset()
    assert singleton.get() is sentinel
    assert calls == 2
