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

import threading
import time
from collections.abc import Iterator
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
