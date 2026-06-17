"""Tests for register(ctx): the Hermes plugin entry point.

TDD rule 18: all tests written before the implementation. Fake ctx captures
register_platform calls without any real Hermes runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from unittest.mock import MagicMock

import pytest

from hermes_voip.config import ConfigError
from hermes_voip.hermes_surface import (
    BasePlatformAdapterProtocol,
)

# ---------------------------------------------------------------------------
# Fake PluginContext
# ---------------------------------------------------------------------------


class _FakeCtx:
    """Fake PluginContextProtocol recording register_platform/tool/hook calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.tool_calls: list[dict[str, object]] = []
        self.hook_calls: list[dict[str, object]] = []

    def register_platform(  # noqa: PLR0913 — mirrors hermes-agent's register_platform arity exactly
        self,
        name: str,
        label: str,
        adapter_factory: Callable[[object], BasePlatformAdapterProtocol],
        check_fn: Callable[[], bool],
        validate_config: Callable[[object], None] | None = None,
        required_env: Sequence[str] | None = None,
        install_hint: str = "",
        **entry_kwargs: object,
    ) -> None:
        self.calls.append(
            {
                "name": name,
                "label": label,
                "adapter_factory": adapter_factory,
                "check_fn": check_fn,
                "validate_config": validate_config,
                "required_env": required_env,
                "install_hint": install_hint,
                **entry_kwargs,
            }
        )

    def register_tool(  # noqa: PLR0913 — mirrors hermes-agent's register_tool arity
        self,
        name: str,
        toolset: str,
        schema: dict[str, object],
        handler: object,
        check_fn: Callable[[], bool] | None = None,
        requires_env: Sequence[str] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
    ) -> None:
        self.tool_calls.append(
            {
                "name": name,
                "toolset": toolset,
                "schema": schema,
                "handler": handler,
                "is_async": is_async,
                "description": description,
            }
        )

    def register_hook(self, hook_name: str, callback: object) -> None:
        self.hook_calls.append({"hook_name": hook_name, "callback": callback})


# ---------------------------------------------------------------------------
# (a) register(ctx) calls register_platform exactly once with name="voip"
# ---------------------------------------------------------------------------


def test_register_calls_register_platform_once() -> None:
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    assert len(ctx.calls) == 1


def test_register_uses_name_voip() -> None:
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    assert ctx.calls[0]["name"] == "voip"


def test_register_supplies_all_required_params() -> None:
    """register_platform must supply name, label, adapter_factory, check_fn."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    call = ctx.calls[0]
    for param in ("name", "label", "adapter_factory", "check_fn"):
        assert param in call, f"register_platform missing param: {param!r}"
        assert call[param] is not None, f"register_platform param {param!r} is None"


def test_register_required_env_includes_hermes_sip_host() -> None:
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    required_env = ctx.calls[0]["required_env"]
    assert required_env is not None
    assert isinstance(required_env, (list, tuple, frozenset, set))
    assert "HERMES_SIP_HOST" in required_env


def test_register_adapter_factory_is_callable() -> None:
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    factory = ctx.calls[0]["adapter_factory"]
    assert callable(factory)


def test_register_check_fn_is_callable() -> None:
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    check_fn = ctx.calls[0]["check_fn"]
    assert callable(check_fn)


def test_register_validate_config_is_callable_or_none() -> None:
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    vc = ctx.calls[0]["validate_config"]
    assert vc is None or callable(vc)


# ---------------------------------------------------------------------------
# (b) validate_config: Hermes PlatformRegistry.create_adapter() treats a falsey
#     return as FAILURE, so success MUST return a truthy value (not None), and a
#     missing/invalid config must be falsey (raising ConfigError is caught by the
#     registry and treated as falsey too).
# ---------------------------------------------------------------------------


def test_validate_config_callback_truthy_on_valid_config() -> None:
    """The registered validate_config callback MUST return truthy on success.

    Hermes 0.16.0 ``PlatformRegistry.create_adapter`` aborts adapter creation
    when ``validate_config(config)`` is falsey — returning ``None`` (the previous
    behaviour) silently disables the platform even for a valid config.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    validate_raw = ctx.calls[0]["validate_config"]
    assert validate_raw is not None, "validate_config must be supplied"
    assert callable(validate_raw)
    validate: Callable[[object], bool] = validate_raw

    fake_config = MagicMock()
    fake_config.extra = {
        "HERMES_SIP_HOST": "pbx.example.test",
        "HERMES_SIP_EXTENSION": "1000",
        "HERMES_SIP_PASSWORD": "fake",
    }
    result = validate(fake_config)
    assert result is True, "validate_config(valid) must return True, not None/falsey"


def test_validate_config_callback_falsey_on_missing_host() -> None:
    """A config with no HERMES_SIP_HOST must be rejected (raise ConfigError).

    The registry catches the raise and treats it as falsey, so either a falsey
    return or a ``ConfigError`` is a correct rejection; we assert the explicit
    ``ConfigError`` the validator raises.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    validate_raw = ctx.calls[0]["validate_config"]
    assert validate_raw is not None
    assert callable(validate_raw)
    validate: Callable[[object], bool] = validate_raw

    fake_config = MagicMock()
    fake_config.extra = {}
    with pytest.raises(ConfigError):
        validate(fake_config)


# ---------------------------------------------------------------------------
# (c) install_hint is a non-empty string
# ---------------------------------------------------------------------------


def test_register_install_hint_is_non_empty() -> None:
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    hint = ctx.calls[0]["install_hint"]
    assert isinstance(hint, str)
    assert hint  # non-empty


# ---------------------------------------------------------------------------
# (d) register is exported from hermes_voip.__init__
# ---------------------------------------------------------------------------


def test_register_exported_from_package_root() -> None:
    import hermes_voip  # noqa: PLC0415

    assert hasattr(hermes_voip, "register"), (
        "register() must be re-exported from hermes_voip.__init__"
    )
    assert callable(hermes_voip.register)


# ---------------------------------------------------------------------------
# (e) env_enablement_fn + is_connected: the gateway seeds PlatformConfig.extra
#     from the HERMES_SIP_*/HERMES_VOIP_* PROCESS ENV (never config.yaml — the
#     SIP password is a secret). Without these hooks the gateway enables the
#     "voip" platform but with an EMPTY extra, so VoipAdapter.connect() fails
#     with ConfigError (no HERMES_SIP_HOST in extra). The registry-driven enable
#     pass in gateway.config calls env_enablement_fn() to seed extra and consults
#     is_connected(probe_cfg) to gate enablement.
# ---------------------------------------------------------------------------


def test_register_supplies_env_enablement_fn() -> None:
    """register_platform must pass a zero-arg env_enablement_fn."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    fn = ctx.calls[0].get("env_enablement_fn")
    assert fn is not None, "register_platform must supply env_enablement_fn"
    assert callable(fn)


def test_env_enablement_fn_seeds_sip_and_voip_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env_enablement_fn() returns the HERMES_SIP_*/HERMES_VOIP_* env as a dict.

    The gateway merges this dict into PlatformConfig.extra, which the adapter
    reads. It must pick up every HERMES_SIP_*/HERMES_VOIP_* var present in the
    process environment, and nothing unrelated.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    monkeypatch.setenv("HERMES_SIP_HOST", "pbx.example.test")
    monkeypatch.setenv("HERMES_SIP_EXTENSION", "1000")
    monkeypatch.setenv("HERMES_SIP_PASSWORD", "fake-password")
    monkeypatch.setenv("HERMES_VOIP_STT_MODEL_DIR", "/models/stt")
    monkeypatch.setenv("UNRELATED_VAR", "ignore-me")

    ctx = _FakeCtx()
    register(ctx)
    fn_raw = ctx.calls[0].get("env_enablement_fn")
    assert fn_raw is not None
    assert callable(fn_raw)
    fn: Callable[[], dict[str, str] | None] = fn_raw

    seed = fn()
    assert isinstance(seed, dict)
    assert seed.get("HERMES_SIP_HOST") == "pbx.example.test"
    assert seed.get("HERMES_SIP_EXTENSION") == "1000"
    assert seed.get("HERMES_SIP_PASSWORD") == "fake-password"
    assert seed.get("HERMES_VOIP_STT_MODEL_DIR") == "/models/stt"
    assert "UNRELATED_VAR" not in seed


def test_register_supplies_is_connected() -> None:
    """register_platform must pass an is_connected gate callable."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    fn = ctx.calls[0].get("is_connected")
    assert fn is not None, "register_platform must supply is_connected"
    assert callable(fn)


def test_is_connected_true_when_required_sip_in_extra() -> None:
    """is_connected(probe) is True when the required SIP keys are in extra."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    fn_raw = ctx.calls[0].get("is_connected")
    assert fn_raw is not None
    assert callable(fn_raw)
    fn: Callable[[object], bool] = fn_raw

    probe = MagicMock()
    probe.extra = {
        "HERMES_SIP_HOST": "pbx.example.test",
        "HERMES_SIP_EXTENSION": "1000",
        "HERMES_SIP_PASSWORD": "fake",
    }
    assert fn(probe) is True


def test_is_connected_false_when_sip_env_absent() -> None:
    """is_connected(probe) is False when the SIP config is missing from extra."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    fn_raw = ctx.calls[0].get("is_connected")
    assert fn_raw is not None
    assert callable(fn_raw)
    fn: Callable[[object], bool] = fn_raw

    probe = MagicMock()
    probe.extra = {}
    assert fn(probe) is False


# ---------------------------------------------------------------------------
# (f) _env_enablement copies DEEPGRAM_API_KEY / ELEVENLABS_API_KEY so that
#     load_media_config(extra) succeeds when a cloud provider is selected.
#     These two keys have no HERMES_SIP_*/HERMES_VOIP_* prefix, so a prefix-
#     only filter would drop them — causing ConfigError inside connect().
# ---------------------------------------------------------------------------


def test_env_enablement_includes_deepgram_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env_enablement_fn() must include DEEPGRAM_API_KEY when set.

    Selecting stt_provider=deepgram requires DEEPGRAM_API_KEY in extra;
    that key has no HERMES_SIP_*/HERMES_VOIP_* prefix so a prefix-only
    filter silently drops it, causing load_media_config(extra) to raise
    ConfigError with "stt_provider 'deepgram' requires DEEPGRAM_API_KEY".
    """
    from hermes_voip.plugin import _env_enablement  # noqa: PLC0415

    monkeypatch.setenv("HERMES_SIP_HOST", "pbx.example.test")
    monkeypatch.setenv("HERMES_SIP_EXTENSION", "1000")
    monkeypatch.setenv("HERMES_SIP_PASSWORD", "fake-password")
    monkeypatch.setenv("HERMES_VOIP_STT_PROVIDER", "deepgram")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-fake-key-for-test")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    seed = _env_enablement()
    assert "DEEPGRAM_API_KEY" in seed, (
        "_env_enablement() must include DEEPGRAM_API_KEY so load_media_config "
        "does not raise ConfigError when stt_provider=deepgram"
    )
    assert seed["DEEPGRAM_API_KEY"] == "dg-fake-key-for-test"


def test_env_enablement_deepgram_key_survives_load_media_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEEPGRAM_API_KEY in env_enablement output keeps load_media_config from raising.

    This is the end-to-end contract: the seeded extra dict must be accepted
    by load_media_config without ConfigError when stt_provider=deepgram.
    """
    from hermes_voip.config import load_media_config  # noqa: PLC0415
    from hermes_voip.plugin import _env_enablement  # noqa: PLC0415

    monkeypatch.setenv("HERMES_SIP_HOST", "pbx.example.test")
    monkeypatch.setenv("HERMES_SIP_EXTENSION", "1000")
    monkeypatch.setenv("HERMES_SIP_PASSWORD", "fake-password")
    monkeypatch.setenv("HERMES_VOIP_STT_PROVIDER", "deepgram")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-fake-key-for-test")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    extra = _env_enablement()
    # Must not raise ConfigError about missing DEEPGRAM_API_KEY.
    media = load_media_config(extra)
    assert media.deepgram_api_key == "dg-fake-key-for-test"


def test_env_enablement_includes_elevenlabs_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env_enablement_fn() must include ELEVENLABS_API_KEY when set.

    Selecting tts_provider=elevenlabs requires ELEVENLABS_API_KEY in extra;
    that key has no HERMES_SIP_*/HERMES_VOIP_* prefix so a prefix-only
    filter silently drops it, causing load_media_config(extra) to raise
    ConfigError with "tts_provider 'elevenlabs' requires ELEVENLABS_API_KEY".
    """
    from hermes_voip.plugin import _env_enablement  # noqa: PLC0415

    monkeypatch.setenv("HERMES_SIP_HOST", "pbx.example.test")
    monkeypatch.setenv("HERMES_SIP_EXTENSION", "1000")
    monkeypatch.setenv("HERMES_SIP_PASSWORD", "fake-password")
    monkeypatch.setenv("HERMES_VOIP_TTS_PROVIDER", "elevenlabs")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-fake-key-for-test")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)

    seed = _env_enablement()
    assert "ELEVENLABS_API_KEY" in seed, (
        "_env_enablement() must include ELEVENLABS_API_KEY so load_media_config "
        "does not raise ConfigError when tts_provider=elevenlabs"
    )
    assert seed["ELEVENLABS_API_KEY"] == "el-fake-key-for-test"


def test_env_enablement_elevenlabs_key_survives_load_media_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ELEVENLABS_API_KEY in env_enablement output keeps load_media_config from raising.

    End-to-end contract: the seeded extra dict must be accepted by
    load_media_config without ConfigError when tts_provider=elevenlabs.
    """
    from hermes_voip.config import load_media_config  # noqa: PLC0415
    from hermes_voip.plugin import _env_enablement  # noqa: PLC0415

    monkeypatch.setenv("HERMES_SIP_HOST", "pbx.example.test")
    monkeypatch.setenv("HERMES_SIP_EXTENSION", "1000")
    monkeypatch.setenv("HERMES_SIP_PASSWORD", "fake-password")
    monkeypatch.setenv("HERMES_VOIP_TTS_PROVIDER", "elevenlabs")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-fake-key-for-test")
    # Disable TTS failover so this test stays focused on the key surviving (a cloud
    # primary otherwise requires HERMES_VOIP_TTS_FALLBACK_MODEL for its Kokoro
    # fallback — ADR-0025 — which is exercised by the dedicated config tests).
    monkeypatch.setenv("HERMES_VOIP_TTS_FALLBACK", "none")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)

    extra = _env_enablement()
    # Must not raise ConfigError about missing ELEVENLABS_API_KEY.
    media = load_media_config(extra)
    assert media.elevenlabs_api_key == "el-fake-key-for-test"


# ---------------------------------------------------------------------------
# (z) register(ctx) wires the agent hang_up tool through the pre_tool_call gate
#     (ADR-0026). The plugin previously registered ONLY the platform, so the agent
#     had no way to end a call. The tool is registered with a pre_tool_call hook
#     that gates it through gate_voip_tool.
# ---------------------------------------------------------------------------


def test_register_registers_the_hang_up_tool() -> None:
    """register(ctx) must register an agent-facing hang_up/end_call tool."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    names = {c["name"] for c in ctx.tool_calls}
    assert "hang_up" in names, "the agent hang_up tool was not registered"


def test_hang_up_tool_is_async_with_a_schema() -> None:
    """The hang_up tool is async and ships a JSON schema (so the model can call it)."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    tool = next(c for c in ctx.tool_calls if c["name"] == "hang_up")
    assert tool["is_async"] is True
    schema = tool["schema"]
    assert isinstance(schema, dict)
    # A tool schema the model reads must at least name the tool + describe it.
    assert schema.get("name") == "hang_up"
    assert schema.get("description")


def test_register_registers_a_pre_tool_call_gate() -> None:
    """register(ctx) must register a pre_tool_call hook (the tool-policy gate)."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    hook_names = {h["hook_name"] for h in ctx.hook_calls}
    assert "pre_tool_call" in hook_names, "no pre_tool_call gate was registered"


def test_register_still_registers_the_platform_once() -> None:
    """Adding tool/hook registration must not disturb the single platform register."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    assert len(ctx.calls) == 1
    assert ctx.calls[0]["name"] == "voip"


def test_register_is_resilient_to_a_ctx_without_register_tool() -> None:
    """An older ctx lacking register_tool/register_hook still registers the platform.

    The tool/hook wiring is best-effort (guarded by getattr like register_platform),
    so a runtime that predates register_tool does not break plugin load.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    class _PlatformOnlyCtx:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def register_platform(self, name: str, *args: object, **kwargs: object) -> None:
            self.calls.append(name)

    ctx = _PlatformOnlyCtx()
    register(ctx)  # must not raise even though register_tool/register_hook are absent
    assert ctx.calls == ["voip"]
