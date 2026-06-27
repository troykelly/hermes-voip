"""Assert utility modules define ``__all__`` and star-imports stay clean.

TDD: this test file is committed RED before the implementation commit.
Covers: call_context, hermes_surface, notice_filter, provider_error.
"""

from __future__ import annotations

import importlib
import types


def _star_names(module: types.ModuleType) -> list[str]:
    """Names that ``from <module> import *`` would bind."""
    all_attr = getattr(module, "__all__", None)
    if all_attr is not None:
        return list(all_attr)
    return [n for n in dir(module) if not n.startswith("_")]


_UTILITY_MODULES = [
    "hermes_voip.call_context",
    "hermes_voip.hermes_surface",
    "hermes_voip.notice_filter",
    "hermes_voip.provider_error",
]


def test_all_defined() -> None:
    """Every utility module exports an explicit ``__all__``."""
    for mod_name in _UTILITY_MODULES:
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "__all__"), (
            f"{mod_name} does not define __all__; star-imports leak private names"
        )


def test_no_private_names_in_star_import() -> None:
    """``import *`` from each module must not bind any underscore-private name."""
    for mod_name in _UTILITY_MODULES:
        mod = importlib.import_module(mod_name)
        leaked = [n for n in _star_names(mod) if n.startswith("_")]
        assert not leaked, f"{mod_name}.__all__ leaks private names: {leaked}"


def test_all_is_list_or_tuple_of_strings() -> None:
    """``__all__`` must be a list or tuple whose every element is a str."""
    for mod_name in _UTILITY_MODULES:
        mod = importlib.import_module(mod_name)
        all_attr = getattr(mod, "__all__", None)
        if all_attr is None:
            continue  # caught by test_all_defined
        assert isinstance(all_attr, (list, tuple)), (
            f"{mod_name}.__all__ must be list or tuple, got {type(all_attr)}"
        )
        for name in all_attr:
            assert isinstance(name, str), (
                f"{mod_name}.__all__ contains non-str element: {name!r}"
            )


def test_all_names_actually_exist() -> None:
    """Every name in ``__all__`` must be a real attribute of the module."""
    for mod_name in _UTILITY_MODULES:
        mod = importlib.import_module(mod_name)
        all_attr = getattr(mod, "__all__", None)
        if all_attr is None:
            continue  # caught by test_all_defined
        for name in all_attr:
            assert hasattr(mod, name), (
                f"{mod_name}.__all__ references non-existent name: {name!r}"
            )
