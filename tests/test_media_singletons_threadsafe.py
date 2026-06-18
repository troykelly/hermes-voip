"""Thread-safety tests for the lazy singletons in media/srtp.py and media/dtls.py.

Both ``media/srtp._get_crypto`` and ``media/dtls._get_openssl`` are lazy
module-level singletons.  Hermes runs the agent generation on an uncapped
``ThreadPoolExecutor`` and forks background/self-improve workers, so these
getters can be entered concurrently from multiple threads.  A naive
``if _X is None: _X = build()`` has a time-of-check/time-of-use race: two
threads can both observe ``None`` and both run ``build()``, returning distinct
instances and doing the (here-cheap, but still wasteful) construction twice.

These tests make the race observable by stubbing the builder with a counter
that sleeps briefly inside construction, then hammering the getter from many
threads at once.  After double-checked locking, the builder must run EXACTLY
once and every caller must receive the SAME instance.

The build-once-under-concurrency guarantee now lives in
:class:`hermes_voip._lazy_singleton.LazySingleton` (ADR-0046), which backs both
getters. Each test swaps the module's ``LazySingleton`` for one wrapping a counting
stub factory, then resets it afterwards via the ``_reset_singletons`` fixture so the
suite does not leak state into the rest of pytest (which may legitimately build the
real singletons).

No optional extra is required: the builders are stubbed out entirely, so the
real ``cryptography`` / ``pyOpenSSL`` dependencies are never touched.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

import hermes_voip.media.dtls as dtls_mod
import hermes_voip.media.srtp as srtp_mod
from hermes_voip._lazy_singleton import LazySingleton

# Number of threads that race into the getter simultaneously.  Comfortably more
# than the host core count so the scheduler genuinely interleaves them.
_N_THREADS = 32

# How long the stub builder sleeps mid-construction.  Long enough that, under
# the naive pattern, multiple threads enter ``build()`` before the first
# assignment lands — i.e. the race is reliably observed RED before the fix.
_BUILD_SLEEP_S = 0.05


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Reset both media singletons before and after each test (ADR-0046)."""
    srtp_mod._reset_crypto_singleton()
    dtls_mod._reset_openssl_singleton()
    try:
        yield
    finally:
        srtp_mod._reset_crypto_singleton()
        dtls_mod._reset_openssl_singleton()


class _Counter:
    """Thread-safe call counter for the stubbed builders."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0

    def tick(self) -> None:
        with self._lock:
            self.count += 1


def _hammer(getter: object) -> list[object]:
    """Call ``getter`` from ``_N_THREADS`` threads at once; return every result.

    A barrier lines all worker threads up at the getter call so they contend on
    the (un)guarded ``if _X is None`` check at the same instant.
    """
    assert callable(getter)
    barrier = threading.Barrier(_N_THREADS)

    def worker() -> object:
        barrier.wait()
        return getter()

    with ThreadPoolExecutor(max_workers=_N_THREADS) as pool:
        futures = [pool.submit(worker) for _ in range(_N_THREADS)]
        return [f.result() for f in futures]


def test_get_crypto_builds_once_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_get_crypto`` builds one ``_CryptographyImpl`` under a thread stampede.

    Every concurrent caller must receive that single shared instance.
    """
    counter = _Counter()
    sentinel = object()

    def fake_impl() -> object:
        # Count the construction, then sleep so a second racing thread would
        # also pass the ``is None`` check under a naive (unlocked) pattern.
        counter.tick()
        time.sleep(_BUILD_SLEEP_S)
        return sentinel

    # ``_get_crypto`` builds via the module's LazySingleton — swap it for one
    # wrapping the counting stub so the real cryptography backend is never touched.
    monkeypatch.setattr(srtp_mod, "_CRYPTO_SINGLETON", LazySingleton(fake_impl))

    results = _hammer(srtp_mod._get_crypto)

    assert counter.count == 1, (
        f"crypto backend built {counter.count} times under "
        f"{_N_THREADS} concurrent callers; expected exactly 1 (TOCTOU race)"
    )
    assert all(r is sentinel for r in results)
    assert srtp_mod._get_crypto() is sentinel


def test_get_openssl_builds_once_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_get_openssl`` loads one ``_PyOpenSSLImpl`` under a thread stampede.

    Every concurrent caller must receive that single shared instance.
    """
    counter = _Counter()
    sentinel = object()

    def fake_load() -> object:
        counter.tick()
        time.sleep(_BUILD_SLEEP_S)
        return sentinel

    # ``_get_openssl`` builds via the module's LazySingleton — swap it for one
    # wrapping the counting stub so the real pyOpenSSL backend is never loaded.
    monkeypatch.setattr(dtls_mod, "_OPENSSL_SINGLETON", LazySingleton(fake_load))

    results = _hammer(dtls_mod._get_openssl)

    assert counter.count == 1, (
        f"pyOpenSSL backend loaded {counter.count} times under "
        f"{_N_THREADS} concurrent callers; expected exactly 1 (TOCTOU race)"
    )
    assert all(r is sentinel for r in results)
    assert dtls_mod._get_openssl() is sentinel


# ---------------------------------------------------------------------------
# LazySingleton's RUNTIME-helper path is build-once under concurrency too.
# ---------------------------------------------------------------------------


class _CorrectRuntimeHandle:
    """A faithful ``plugins.plugin_utils.lazy_singleton`` handle.

    Builds its wrapped factory at most once, under its own lock — exactly what a
    correct runtime helper guarantees. With a CORRECT runtime handle, the only way
    the user factory runs more than once is if ``LazySingleton`` itself builds more
    than one handle (its ``_resolve_runtime`` racing) — which is the bug under test.
    """

    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._built = False
        self._value: object = None

    def get(self) -> object:
        if not self._built:
            with self._lock:
                if not self._built:
                    self._value = self._factory()
                    self._built = True
        return self._value

    def reset(self) -> None:
        with self._lock:
            self._built = False
            self._value = None


@pytest.fixture
def _fake_runtime_lazy_singleton() -> Iterator[None]:
    """Install a fake ``plugins.plugin_utils`` exposing a correct ``lazy_singleton``.

    LazySingleton's ``_resolve_runtime`` imports
    ``plugins.plugin_utils.lazy_singleton``; absent in the default test env it falls
    back to stdlib. Injecting a real module here drives the RUNTIME path instead.
    """
    handles: list[_CorrectRuntimeHandle] = []

    def lazy_singleton(factory: Callable[[], object]) -> _CorrectRuntimeHandle:
        # Sleep WHILE building the runtime handle to widen the window in which
        # ``_runtime_resolved`` is True but ``_runtime`` is not yet assigned — the
        # exact interleaving in which an unlocked ``_resolve_runtime`` lets a second
        # thread fall through to the stdlib path and build the value a second time.
        time.sleep(_BUILD_SLEEP_S)
        handle = _CorrectRuntimeHandle(factory)
        handles.append(handle)
        return handle

    pkg = types.ModuleType("plugins")
    pkg.__path__ = []  # mark as a package so the submodule import resolves
    sub = types.ModuleType("plugins.plugin_utils")
    sub.lazy_singleton = lazy_singleton  # type: ignore[attr-defined]  # test stub attr
    monkey_added = {"plugins": pkg, "plugins.plugin_utils": sub}
    saved = {name: sys.modules.get(name) for name in monkey_added}
    sys.modules.update(monkey_added)
    try:
        yield
    finally:
        for name, prior in saved.items():
            if prior is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior


@pytest.mark.usefixtures("_fake_runtime_lazy_singleton")
def test_lazy_singleton_runtime_path_builds_once_under_concurrency() -> None:
    """LazySingleton over the RUNTIME helper builds the value once under a stampede.

    A concurrent first-call stampede must not let ``_resolve_runtime`` build two
    runtime handles (each of which would run the user factory once) — the value
    factory must run EXACTLY once and every caller gets the same instance.
    """
    counter = _Counter()
    sentinel = object()

    def factory() -> object:
        counter.tick()
        time.sleep(_BUILD_SLEEP_S)
        return sentinel

    singleton: LazySingleton[object] = LazySingleton(factory)
    results = _hammer(singleton.get)

    assert counter.count == 1, (
        f"runtime-path factory ran {counter.count} times under {_N_THREADS} "
        f"concurrent callers; expected exactly 1 (resolve_runtime race)"
    )
    assert all(r is sentinel for r in results)
    assert singleton.get() is sentinel
