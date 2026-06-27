"""Tests for hermes_voip.providers.registry — config-keyed provider factories.

Provider choice is config, never code (ADR-0004): a registry maps a name to a
factory and resolves the active provider at startup, raising (never swallowing)
on an unknown name (rule 37).
"""

import pytest

from hermes_voip.providers.registry import ProviderRegistry


class _Fake:
    pass


def test_make_returns_factory_result() -> None:
    reg: ProviderRegistry[_Fake] = ProviderRegistry("thing")
    sentinel = _Fake()
    reg.register("x", lambda: sentinel)
    assert reg.make("x") is sentinel


def test_make_unknown_raises_valueerror() -> None:
    reg: ProviderRegistry[_Fake] = ProviderRegistry("thing")
    with pytest.raises(ValueError, match="unknown thing provider: 'nope'"):
        reg.make("nope")


def test_register_duplicate_raises() -> None:
    reg: ProviderRegistry[_Fake] = ProviderRegistry("thing")
    reg.register("x", _Fake)
    with pytest.raises(ValueError, match="already registered"):
        reg.register("x", _Fake)


def test_names_lists_registered_sorted() -> None:
    reg: ProviderRegistry[_Fake] = ProviderRegistry("thing")
    reg.register("b", _Fake)
    reg.register("a", _Fake)
    assert reg.names() == ("a", "b")


def test_contains_true_after_register() -> None:
    reg: ProviderRegistry[_Fake] = ProviderRegistry("thing")
    reg.register("x", _Fake)
    assert "x" in reg


def test_contains_false_before_register() -> None:
    reg: ProviderRegistry[_Fake] = ProviderRegistry("thing")
    assert "x" not in reg


def test_contains_false_after_different_register() -> None:
    reg: ProviderRegistry[_Fake] = ProviderRegistry("thing")
    reg.register("x", _Fake)
    assert "y" not in reg


def test_make_calls_factory_exactly_once_per_call() -> None:
    reg: ProviderRegistry[_Fake] = ProviderRegistry("thing")
    call_count = 0

    def factory() -> _Fake:
        nonlocal call_count
        call_count += 1
        return _Fake()

    reg.register("x", factory)
    assert call_count == 0
    reg.make("x")
    assert call_count == 1
    reg.make("x")
    assert call_count == 2


def test_make_returns_distinct_instances_per_call() -> None:
    """make() must return a fresh instance on every call (no memoisation/caching).

    This test would fail if make() ever cached the factory result and returned
    the same object identity on subsequent calls.
    """
    reg: ProviderRegistry[_Fake] = ProviderRegistry("thing")
    reg.register("x", _Fake)
    first = reg.make("x")
    second = reg.make("x")
    assert first is not second


def test_register_duplicate_error_includes_kind_and_name() -> None:
    reg: ProviderRegistry[_Fake] = ProviderRegistry("special-thing")
    reg.register("x", _Fake)
    with pytest.raises(ValueError, match=r"special-thing.*already registered.*'x'"):
        reg.register("x", _Fake)
