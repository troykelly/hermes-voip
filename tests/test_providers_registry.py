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
