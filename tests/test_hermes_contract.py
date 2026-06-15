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


# ---------------------------------------------------------------------------
# The VoipAdapter must work against the REAL base — these are the checks the
# unit tests (which used fakes) could not make. They run only with the hermes
# extra installed; the dedicated CI job makes a missing surface a hard failure.
# ---------------------------------------------------------------------------


def test_voip_adapter_is_real_base_platform_adapter_subclass() -> None:
    """VoipAdapter must subclass the real ``BasePlatformAdapter`` at runtime.

    The gateway relies on ``isinstance(adapter, BasePlatformAdapter)`` for
    handle_message/build_source/set_message_handler/send-retry wiring; a duck
    type that merely has the four methods does not get any of that.
    """
    base = _hermes("gateway.platforms.base")
    _hermes("gateway.config")  # ensure the optional runtime is importable
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    assert issubclass(VoipAdapter, base.BasePlatformAdapter)


def test_validate_voip_config_is_truthy_on_valid_config() -> None:
    """``validate_voip_config`` must return truthy for a valid config.

    ``PlatformRegistry.create_adapter`` treats a falsey return as a validation
    failure and refuses to build the adapter.
    """
    config_mod = _hermes("gateway.config")
    from hermes_voip.plugin import validate_voip_config  # noqa: PLC0415

    cfg = config_mod.PlatformConfig(
        enabled=True,
        extra={
            "HERMES_SIP_HOST": "pbx.example.test",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "fake",
        },
    )
    assert validate_voip_config(cfg) is True


def test_platform_registry_create_adapter_builds_voip_adapter() -> None:
    """The real ``PlatformRegistry.create_adapter`` path must produce an adapter.

    This exercises the exact gateway flow: ``register(ctx)`` registers the
    platform, then ``create_adapter("voip", config)`` runs check_fn +
    validate_config + the factory and must return a live ``VoipAdapter``.
    """
    base = _hermes("gateway.platforms.base")
    config_mod = _hermes("gateway.config")
    registry_mod = _hermes("gateway.platform_registry")
    plugins_mod = _hermes("hermes_cli.plugins")
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415
    from hermes_voip.plugin import register  # noqa: PLC0415

    # A minimal PluginContext-like ctx is the real one; build it from a manifest.
    manifest = plugins_mod.PluginManifest(name="hermes-voip", source="entrypoint")
    manager = plugins_mod.PluginManager()
    ctx = plugins_mod.PluginContext(manifest, manager)
    register(ctx)

    assert registry_mod.platform_registry.is_registered("voip")

    cfg = config_mod.PlatformConfig(
        enabled=True,
        extra={
            "HERMES_SIP_HOST": "pbx.example.test",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "fake",
        },
    )
    adapter = registry_mod.platform_registry.create_adapter("voip", cfg)
    assert adapter is not None, "create_adapter returned None for a valid config"
    assert isinstance(adapter, base.BasePlatformAdapter)
    assert isinstance(adapter, VoipAdapter)
