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
        validate_config: Callable[[object], bool | None] | None = None,
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


def test_register_registers_the_primary_voip_platform_first() -> None:
    """The primary ``voip`` platform is registered first (ADR-0034).

    ADR-0034 adds the per-caller-group CHANNEL platforms (voip-unknown / voip-known /
    voip-operator / voip-intercom) as additional first-class registrations, so the
    plugin no longer registers exactly one platform. The PRIMARY ``voip`` platform
    (the one carrying the adapter factory + env-enablement + connection probe) is
    still registered, and is registered first.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    assert len(ctx.calls) >= 1
    assert ctx.calls[0]["name"] == "voip"


def test_register_uses_name_voip() -> None:
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    assert ctx.calls[0]["name"] == "voip"


def test_register_registers_the_four_channel_platforms() -> None:
    """ADR-0034: each caller-group channel is a first-class Hermes platform.

    Registering the channels as platforms is what lets the operator target a channel
    with per-platform tools_config / disabled_toolsets and makes the channel
    discoverable. The canonical operator set is voip-unknown / voip-known /
    voip-operator / voip-intercom (plus the primary voip).
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    names = {c["name"] for c in ctx.calls}
    for channel in ("voip-unknown", "voip-known", "voip-operator", "voip-intercom"):
        assert channel in names, f"channel platform {channel!r} was not registered"


def test_channel_platforms_share_the_adapter_factory_and_check_fn() -> None:
    """The channel platforms ALIAS the one voip adapter (no own transport).

    There is a single telephony endpoint; the channels are routing identities over
    the one adapter, so each channel registration reuses the primary platform's
    adapter_factory and check_fn rather than minting a second SIP/RTP transport.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    primary = next(c for c in ctx.calls if c["name"] == "voip")
    for channel in ("voip-unknown", "voip-known", "voip-operator", "voip-intercom"):
        entry = next(c for c in ctx.calls if c["name"] == channel)
        assert entry["adapter_factory"] is primary["adapter_factory"]
        assert entry["check_fn"] is primary["check_fn"]


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


def test_register_install_hint_names_the_working_enable_mechanism() -> None:
    """The install hint must point at the enable mechanism that actually works.

    Rule 27 (no aspirational docs): ``hermes plugins enable hermes-voip`` fails for
    a pip/entry-point plugin (the CLI's ``_plugin_exists`` is filesystem-only, so it
    prints "Plugin 'hermes-voip' is not installed or bundled." and exits 1) UNLESS a
    directory ``plugin.yaml`` stub is installed. The runtime gate that genuinely
    decides activation is ``plugins.enabled`` in ``config.yaml``. The operator-facing
    hint must therefore name ``plugins.enabled`` (the mechanism that always works),
    not direct the operator to a bare CLI command that fails out of the box.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    hint = ctx.calls[0]["install_hint"]
    assert isinstance(hint, str)
    # Names the working runtime enable key.
    assert "plugins.enabled" in hint
    # Does NOT instruct the bare CLI enable as THE activation step (it fails for an
    # entry-point plugin without the stub). Phrasing that mentions the command only
    # as the stub-enabled affordance is fine; the unconditional imperative is not.
    assert "Run `hermes plugins enable hermes-voip` to activate" not in hint


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


def test_register_still_registers_the_primary_platform() -> None:
    """Adding tool/hook + channel registration keeps the primary voip platform.

    (ADR-0034 made the platform set the primary ``voip`` plus the channel aliases;
    this asserts the primary is still registered first and the tool/hook wiring did
    not displace it.)
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
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
    # The primary voip platform plus the ADR-0034 channel aliases all register; the
    # primary is first. (register_tool/register_hook absence only skips tool wiring.)
    assert ctx.calls[0] == "voip"
    for channel in ("voip-unknown", "voip-known", "voip-operator", "voip-intercom"):
        assert channel in ctx.calls


# ---------------------------------------------------------------------------
# In-call control tools exposed through register(ctx) (ADR-0011 §3)
# ---------------------------------------------------------------------------


def test_register_registers_the_in_call_control_tools() -> None:
    """register(ctx) exposes hold_call / resume_call / list_registrations.

    These were built + unit-tested in tools.py but were dark (only hang_up was
    registered); this wires them to the runtime so the agent can call them.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    names = {c["name"] for c in ctx.tool_calls}
    for tool in ("hold_call", "resume_call", "list_registrations"):
        assert tool in names, f"the {tool!r} tool was not registered"


def test_register_exposes_transfer_blind_but_not_attended() -> None:
    """``transfer_blind`` IS exposed (ADR-0010/0031); ``transfer_attended`` is not.

    The spoof-resistant ADR-0010 DTMF confirmation channel landed (PR #104
    ``ArmedConfirmation``), so ``transfer_blind`` is no longer an always-blocked
    no-op: it is registered and the REFER fires only on a real keypad confirm. The
    one transfer still deferred is ``transfer_attended`` — it needs a consultation
    Dialog the agent cannot originate (ADR-0031 §4), so registering it would be a
    lying stub (rule 6). It stays deferred-not-registered.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    names = {c["name"] for c in ctx.tool_calls}
    assert "transfer_blind" in names, "transfer_blind should now be registered"
    assert "transfer_attended" not in names, "transfer_attended must stay deferred"
    # transfer_blind is async and ships a model-readable schema with a target param.
    blind = next(c for c in ctx.tool_calls if c["name"] == "transfer_blind")
    assert blind["is_async"] is True
    schema = blind["schema"]
    assert isinstance(schema, dict)
    params = schema.get("parameters")
    assert isinstance(params, dict)
    props = params.get("properties")
    assert isinstance(props, dict)
    assert "target" in props


def test_registered_control_tools_are_async_with_schemas() -> None:
    """Each exposed control tool is async and ships a model-readable JSON schema."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    for tool_name in ("hold_call", "resume_call", "list_registrations"):
        tool = next(c for c in ctx.tool_calls if c["name"] == tool_name)
        assert tool["is_async"] is True, f"{tool_name} must be async"
        schema = tool["schema"]
        assert isinstance(schema, dict)
        assert schema.get("name") == tool_name
        assert schema.get("description")


def test_elevated_tools_are_not_registered_without_the_gate_hook() -> None:
    """FAIL CLOSED: no register_hook → the ELEVATED tools are NOT registered.

    With register_tool but NO register_hook, only SAFE hang_up registers.
    The ELEVATED tools' privilege clamp lives in the pre_tool_call hook. If a ctx
    could register tools but not the hook, registering hold/resume/list would leave
    them reachable UNGATED — a level-0 caller could hold/resume the call or
    enumerate the operator's registrations. So they must be skipped when the gate
    cannot be installed; hang_up (SAFE) needs no clamp and still registers.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    class _NoHookCtx:
        """A ctx that can register tools but has NO register_hook (no gate)."""

        def __init__(self) -> None:
            self.tool_calls: list[str] = []

        def register_platform(self, name: str, *a: object, **k: object) -> None:
            pass

        def register_tool(self, name: str, *a: object, **k: object) -> None:
            self.tool_calls.append(name)

        # NOTE: deliberately NO register_hook attribute.

    ctx = _NoHookCtx()
    register(ctx)  # must not raise
    # SAFE hang_up still registers (no clamp needed).
    assert "hang_up" in ctx.tool_calls
    # The ELEVATED tools are refused — they would be ungated without the hook.
    assert "hold_call" not in ctx.tool_calls
    assert "resume_call" not in ctx.tool_calls
    assert "list_registrations" not in ctx.tool_calls
    # place_call is IRREVERSIBLE — also refused without the gate (would be ungated).
    assert "place_call" not in ctx.tool_calls
    # report_call_result is SAFE — it still registers (no privilege clamp needed).
    assert "report_call_result" in ctx.tool_calls


# ---------------------------------------------------------------------------
# Agent-triggered outbound tools exposed through register(ctx) (ADR-0029)
# ---------------------------------------------------------------------------


def test_register_registers_the_outbound_tools() -> None:
    """register(ctx) exposes place_call (IRREVERSIBLE) + report_call_result (SAFE).

    These wire the agent-triggered outbound feature (ADR-0029) into the runtime: an
    agent can place a call to accomplish an objective and the call agent can record
    its outcome. Without this wiring the tools would be dark.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    names = {c["name"] for c in ctx.tool_calls}
    for tool in ("place_call", "report_call_result"):
        assert tool in names, f"the {tool!r} tool was not registered"


def test_registered_outbound_tools_are_async_with_schemas() -> None:
    """place_call / report_call_result are async and ship model-readable schemas.

    place_call's schema declares the number + objective params (so the model knows
    to supply both); report_call_result declares summary.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _FakeCtx()
    register(ctx)
    place = next(c for c in ctx.tool_calls if c["name"] == "place_call")
    assert place["is_async"] is True
    place_schema = place["schema"]
    assert isinstance(place_schema, dict)
    place_params = place_schema["parameters"]
    assert isinstance(place_params, dict)
    place_props = place_params["properties"]
    assert isinstance(place_props, dict)
    assert "number" in place_props
    assert "objective" in place_props

    report = next(c for c in ctx.tool_calls if c["name"] == "report_call_result")
    assert report["is_async"] is True
    report_schema = report["schema"]
    assert isinstance(report_schema, dict)
    assert report_schema.get("name") == "report_call_result"
    assert report_schema.get("description")
