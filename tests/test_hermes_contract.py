"""Contract test: the real hermes-agent surface still matches our typed shim.

hermes-agent ships no ``py.typed``, so the plugin's adapter/loop code is typed
against ``hermes_voip.hermes_surface`` instead of importing hermes-agent's
classes directly (which would be ``Any`` under ``mypy --strict``). These tests
assert — reflectively, against the *installed* package — that the real classes
still have the shape the shim promises, so a hermes-agent version bump fails here
rather than at runtime. They ``importorskip`` so they skip cleanly when the
optional ``hermes`` extra is not installed (e.g. default CI) and run locally /
in a job where it is.
"""

import inspect

import pytest

from hermes_voip.hermes_surface import (
    BASE_ADAPTER_ABSTRACT_METHODS,
    REGISTER_PLATFORM_PARAMS,
    PluginContextProtocol,
)


def test_base_platform_adapter_abstract_methods_match_shim() -> None:
    base = pytest.importorskip("gateway.platforms.base")
    adapter = base.BasePlatformAdapter
    actual = frozenset(
        name
        for name, value in vars(adapter).items()
        if getattr(value, "__isabstractmethod__", False)
    )
    assert actual == BASE_ADAPTER_ABSTRACT_METHODS


def test_base_platform_adapter_init_signature_matches_shim() -> None:
    base = pytest.importorskip("gateway.platforms.base")
    params = list(inspect.signature(base.BasePlatformAdapter.__init__).parameters)
    assert params == ["self", "config", "platform"]


def test_register_platform_leading_params_match_shim() -> None:
    plugins = pytest.importorskip("hermes_cli.plugins")
    real = list(inspect.signature(plugins.PluginContext.register_platform).parameters)
    shim = list(inspect.signature(PluginContextProtocol.register_platform).parameters)
    expected = ["self", *REGISTER_PLATFORM_PARAMS]
    assert real[: len(expected)] == expected
    assert shim[: len(expected)] == expected


def test_runtime_value_types_are_exported() -> None:
    base = pytest.importorskip("gateway.platforms.base")
    config = pytest.importorskip("gateway.config")
    for name in ("MessageEvent", "SendResult", "MessageType"):
        assert hasattr(base, name), f"gateway.platforms.base.{name} missing"
    for name in ("Platform", "PlatformConfig"):
        assert hasattr(config, name), f"gateway.config.{name} missing"
    assert hasattr(base.MessageType, "VOICE")  # the inbound voice-turn type we emit
