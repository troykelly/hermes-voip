"""Contract test: the real hermes-agent surface still matches our typed shim.

hermes-agent ships no ``py.typed``, so the plugin's adapter/loop code is typed
against ``hermes_voip.hermes_surface`` instead of importing hermes-agent's
classes directly (which would be ``Any`` under ``mypy --strict``). These tests
assert — reflectively, against the *installed* package — that the real classes
still have the shape the shim promises, so a hermes-agent version bump fails here
rather than at runtime.

By default they ``importorskip`` (so they skip cleanly when the optional
``hermes`` extra is not installed, e.g. local runs without it). The dedicated CI
job sets ``HERMES_CONTRACT_REQUIRED=1`` and installs the extra, turning a missing
or drifted surface into a hard CI failure instead of a silent skip.
"""

import importlib
import inspect
import os
from types import ModuleType

import pytest

from hermes_voip.hermes_surface import (
    BASE_ADAPTER_ABSTRACT_METHODS,
    REGISTER_PLATFORM_PARAMS,
    PluginContextProtocol,
)

_REQUIRED = os.environ.get("HERMES_CONTRACT_REQUIRED") == "1"


def _hermes(module_path: str) -> ModuleType:
    """Import a hermes-agent module — hard-failing in CI, skipping otherwise."""
    if _REQUIRED:
        return importlib.import_module(module_path)
    module = pytest.importorskip(module_path)
    assert isinstance(module, ModuleType)
    return module


def _ctor_params(cls: type) -> set[str]:
    return set(inspect.signature(cls).parameters)


def test_base_platform_adapter_abstract_methods_match_shim() -> None:
    base = _hermes("gateway.platforms.base")
    actual = frozenset(
        name
        for name, value in vars(base.BasePlatformAdapter).items()
        if getattr(value, "__isabstractmethod__", False)
    )
    assert actual == BASE_ADAPTER_ABSTRACT_METHODS


def test_base_platform_adapter_init_signature_matches_shim() -> None:
    base = _hermes("gateway.platforms.base")
    params = list(inspect.signature(base.BasePlatformAdapter.__init__).parameters)
    assert params == ["self", "config", "platform"]


def test_send_signature_matches_shim() -> None:
    base = _hermes("gateway.platforms.base")
    params = list(inspect.signature(base.BasePlatformAdapter.send).parameters)
    assert params[:3] == ["self", "chat_id", "content"]
    assert {"reply_to", "metadata"} <= set(params)


def test_register_platform_leading_params_match_shim() -> None:
    plugins = _hermes("hermes_cli.plugins")
    real = list(inspect.signature(plugins.PluginContext.register_platform).parameters)
    shim = list(inspect.signature(PluginContextProtocol.register_platform).parameters)
    expected = ["self", *REGISTER_PLATFORM_PARAMS]
    assert real[: len(expected)] == expected
    assert shim[: len(expected)] == expected


def test_send_result_and_message_event_have_promised_attributes() -> None:
    base = _hermes("gateway.platforms.base")
    assert {"success", "message_id", "error"} <= _ctor_params(base.SendResult)
    assert {"text", "media_urls"} <= _ctor_params(base.MessageEvent)


def test_runtime_value_types_are_exported() -> None:
    base = _hermes("gateway.platforms.base")
    config = _hermes("gateway.config")
    assert hasattr(base, "MessageType")
    assert hasattr(base.MessageType, "VOICE")  # the inbound voice-turn type we emit
    for name in ("Platform", "PlatformConfig"):
        assert hasattr(config, name), f"gateway.config.{name} missing"
