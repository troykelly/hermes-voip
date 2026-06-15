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
    """Fake PluginContextProtocol that records register_platform calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

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
