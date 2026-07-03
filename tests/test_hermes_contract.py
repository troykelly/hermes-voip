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
from pathlib import Path
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


def test_validate_voip_config_is_truthy_on_valid_config(tmp_path: Path) -> None:
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
            # The self-host default providers each need a present model dir now that
            # validate_voip_config preflights the provider wiring; an existing dir
            # keeps this a complete, buildable config so the truthy contract holds.
            "HERMES_VOIP_STT_MODEL_DIR": str(tmp_path),
            "HERMES_VOIP_TTS_MODEL": str(tmp_path),
            "HERMES_VOIP_INJECTION_GUARD_MODEL_DIR": str(tmp_path),
        },
    )
    assert validate_voip_config(cfg) is True


def test_platform_registry_create_adapter_builds_voip_adapter(tmp_path: Path) -> None:
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
            # create_adapter runs validate_config, which now preflights the provider
            # wiring; the self-host defaults each need a present model dir, so point
            # them at an existing directory to keep this a buildable config.
            "HERMES_VOIP_STT_MODEL_DIR": str(tmp_path),
            "HERMES_VOIP_TTS_MODEL": str(tmp_path),
            "HERMES_VOIP_INJECTION_GUARD_MODEL_DIR": str(tmp_path),
        },
    )
    adapter = registry_mod.platform_registry.create_adapter("voip", cfg)
    assert adapter is not None, "create_adapter returned None for a valid config"
    assert isinstance(adapter, base.BasePlatformAdapter)
    assert isinstance(adapter, VoipAdapter)


# ---------------------------------------------------------------------------
# Call-termination signal mechanism (ADR-0026). The plugin signals call-end to
# the Hermes session by injecting a MessageEvent: a FAILURE end injects the
# gateway control command ``/stop`` (a hard stop), a NORMAL end injects a plain
# content note the gateway REPLAYS as the next turn. These contract tests pin
# both halves against the REAL gateway command/interrupt machinery so a version
# bump that changes how ``/stop`` is recognised (or makes the note look like a
# control interrupt) fails here, not silently in production.
# ---------------------------------------------------------------------------


def test_stop_command_is_recognised_by_the_real_gateway() -> None:
    """A FAILURE end injects ``/stop``; the gateway must recognise it as a command.

    ``/stop`` must be (a) parsed as the ``stop`` command from MessageEvent.text and
    (b) in the gateway's active-session-bypass command set, so injecting it while a
    turn is in flight actually stops the session (a hard stop) rather than being
    delivered to the agent as content.
    """
    base = _hermes("gateway.platforms.base")
    commands = _hermes("hermes_cli.commands")
    from hermes_voip.call_end import STOP_COMMAND  # noqa: PLC0415

    event = base.MessageEvent(
        text=STOP_COMMAND,
        message_type=base.MessageType.VOICE,
        source=None,
    )
    assert event.is_command() is True
    assert event.get_command() == "stop"
    # The bypass set is what lets /stop interrupt an in-flight session (a hard
    # stop), which is the whole point of injecting it for a FAILURE end.
    assert "stop" in commands.ACTIVE_SESSION_BYPASS_COMMANDS


def test_normal_end_note_replays_and_is_not_a_control_interrupt() -> None:
    """A NORMAL end injects a content note the gateway REPLAYS (not a hard stop).

    The note must be neither a slash command (so the gateway does not treat it as
    /stop /new /reset) nor one of the gateway's ``_CONTROL_INTERRUPT_MESSAGES``
    (which are dropped with no replay). Failing either, Hermes would not get the
    turn and could not decide stop-vs-followup — the operator's design intent.
    """
    base = _hermes("gateway.platforms.base")
    run = _hermes("gateway.run")
    from hermes_voip.call_end import NORMAL_END_NOTE  # noqa: PLC0415

    event = base.MessageEvent(
        text=NORMAL_END_NOTE,
        message_type=base.MessageType.VOICE,
        source=None,
    )
    # Not a command → delivered to the agent as a turn, not parsed as /stop etc.
    assert event.is_command() is False
    # Not a control-interrupt reason → replayed as the next user turn, not dropped.
    assert run._is_control_interrupt_message(NORMAL_END_NOTE) is False
