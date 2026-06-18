"""A guarded lazy-singleton helper (ADR-0046).

The Hermes runtime documents ``plugins.plugin_utils.lazy_singleton`` as the helper
for a thread-safe, build-once lazy singleton. That module is a Hermes-*runtime*
module — it is NOT vendored into this repo's (default, no-hermes) test environment —
so a hard import would break the default ``mypy --strict`` + pytest gate. This module
mirrors the existing guarded ``from gateway...`` runtime imports in
:mod:`hermes_voip.adapter`: it uses the documented helper when the runtime provides
it, and a behaviourally-identical stdlib double-checked-lock fallback when it does
not. Both paths build the value at most once under a concurrent first-call stampede
and expose a ``reset()`` for test isolation.

The single, narrow surface the rest of the plugin uses is :class:`LazySingleton`:
``.get()`` returns the value (building it on first call), ``.reset()`` drops it so
the next ``.get()`` rebuilds.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class _RuntimeLazySingleton(Protocol):
    """The shape of the object ``plugins.plugin_utils.lazy_singleton`` returns.

    The documented helper takes a zero-arg factory and returns a handle whose
    ``get()`` builds-once-and-caches and whose ``reset()`` clears the cache. We
    consume only those two members, so this narrow Protocol is the full surface —
    no ``Any`` crosses the boundary even though the runtime module is untyped.
    """

    def get(self) -> object:
        """Return the singleton value, constructing it on first call."""
        ...

    def reset(self) -> None:
        """Drop the cached value so the next :meth:`get` rebuilds it."""
        ...


@runtime_checkable
class _LazySingletonFactory(Protocol):
    """The ``lazy_singleton(factory)`` callable surface (narrow, only our use)."""

    def __call__(self, factory: Callable[[], object]) -> _RuntimeLazySingleton:
        """Build a runtime lazy-singleton handle around ``factory``."""
        ...


class LazySingleton[T]:
    """A thread-safe build-once lazy singleton with a reset, over a guarded backend.

    Constructed with the value factory. On first :meth:`get` it builds the value via
    the documented ``plugins.plugin_utils.lazy_singleton`` helper when the Hermes
    runtime provides it, otherwise via a stdlib double-checked-lock fallback —
    behaviour is identical on both paths. :meth:`reset` clears the cached value
    (used by tests to force a rebuild).
    """

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._value: T | None = None
        # The runtime handle, if the documented helper is available. Resolved lazily
        # on first ``get`` so import cost is paid only when the singleton is used.
        self._runtime: _RuntimeLazySingleton | None = None
        self._runtime_resolved = False

    def _resolve_runtime_locked(self) -> _RuntimeLazySingleton | None:
        """Return the runtime lazy-singleton handle, or ``None`` for the fallback.

        Caller MUST hold ``self._lock``. Guarded exactly like the adapter's ``from
        gateway...`` imports: the helper is a Hermes-runtime module absent from the
        default test env, so ``ImportError`` selects the stdlib fallback (the path
        exercised by the test suite). Resolving under the lock is what makes the
        runtime handle build-once: a concurrent first-call stampede cannot create two
        handles (each of which would run the value factory once).
        """
        if self._runtime_resolved:
            return self._runtime
        self._runtime_resolved = True
        try:
            from plugins.plugin_utils import (  # noqa: PLC0415 — guarded runtime import
                lazy_singleton,
            )
        except ImportError:
            self._runtime = None
            return None
        # ``lazy_singleton`` is untyped at the boundary; bind it through the narrow
        # Protocol so no ``Any`` leaks. The factory passed to the runtime helper is a
        # side-effecting wrapper that records the value into ``self._value`` — so the
        # typed ``T`` value our own factory produced is captured WITHOUT trusting the
        # untyped runtime ``get()`` return (no escape-hatch cast needed). The runtime
        # handle's role is purely the build-once coordination.
        factory: _LazySingletonFactory = lazy_singleton

        def _build() -> object:
            built = self._factory()
            self._value = built
            return built

        self._runtime = factory(_build)
        return self._runtime

    def get(self) -> T:
        """Return the singleton value, constructing it at most once.

        The runtime-handle resolution, the runtime ``get()`` build-once drive, and the
        read of the resulting ``self._value`` ALL happen under ``self._lock`` (double-
        checked on the fast path), so a concurrent first-call stampede builds exactly
        one runtime handle and one value. The lock is uncontended once built.
        """
        # Fast path: already built — no lock needed (value is set-once before publish).
        value = self._value
        if value is not None:
            return value
        with self._lock:
            # Re-check under the lock (another thread may have built it meanwhile).
            value = self._value
            if value is not None:
                return value
            runtime = self._resolve_runtime_locked()
            if runtime is not None:
                # Drives the runtime's build-once coordination; the side-effecting
                # factory records the typed value into ``self._value``.
                runtime.get()
                built = self._value
                if built is None:  # pragma: no cover - the factory always sets it
                    built = self._factory()
                    self._value = built
                return built
            # Stdlib fallback (identical build-once semantics, same lock).
            self._value = self._factory()
            return self._value

    def reset(self) -> None:
        """Drop the cached value so the next :meth:`get` rebuilds it (test seam)."""
        with self._lock:
            self._value = None
            runtime = self._resolve_runtime_locked()
            if runtime is not None:
                runtime.reset()
