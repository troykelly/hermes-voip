"""The Hermes plugin entry point: ``register(ctx)`` + ``validate_voip_config``.

This module is the **light** half of the plugin surface — it imports *no*
hermes-agent runtime and *no* heavy media/ML dependency, so a bare
``import hermes_voip`` (which re-exports :func:`register` from here) stays cheap.
The Hermes-dependent adapter implementation lives in :mod:`hermes_voip.adapter`
and is imported lazily inside the factory, so it is loaded only when the Hermes
gateway actually instantiates the platform.

Lifecycle (verified against hermes-agent 0.16.0):

1. The gateway discovers the ``hermes-voip`` pip entry point in group
   ``hermes_agent.plugins`` and calls :func:`register` with a ``PluginContext``.
2. :func:`register` calls ``ctx.register_platform("voip", ...)``, which builds a
   ``PlatformEntry`` in the module-singleton ``platform_registry`` and makes the
   name ``"voip"`` resolvable via ``gateway.config.Platform("voip")``.
3. When a ``voip`` platform is configured + enabled, the gateway calls
   ``platform_registry.create_adapter("voip", config)``: it runs ``check_fn()``,
   then ``validate_config(config)`` (which **must return truthy** — a falsey
   return aborts adapter creation), then the factory, which builds a
   :class:`~hermes_voip.adapter.VoipAdapter`.
"""

from __future__ import annotations

import logging
import os
import ssl
from typing import TYPE_CHECKING

from hermes_voip.config import load_gateway_config

if TYPE_CHECKING:
    from hermes_voip.hermes_surface import (
        BasePlatformAdapterProtocol,
        PluginContextProtocol,
    )

__all__ = ["register", "validate_voip_config"]

_log = logging.getLogger(__name__)

# Install hint shown by ``hermes plugins list``.
_INSTALL_HINT = (
    "Set HERMES_SIP_HOST, HERMES_SIP_EXTENSION, and HERMES_SIP_PASSWORD "
    "(or indexed HERMES_SIP_EXTENSION_<n>/PASSWORD_<n> for multiple registrations). "
    "Run `hermes plugins enable hermes-voip` to activate the plugin."
)

# The required HERMES_SIP_* environment variable names.
_REQUIRED_ENV: tuple[str, ...] = (
    "HERMES_SIP_HOST",
    "HERMES_SIP_EXTENSION",
    "HERMES_SIP_PASSWORD",
)

# The env-var name prefixes the adapter reads its config from. The SIP scheme
# (``HERMES_SIP_*``) carries the gateway + registration credentials; the media
# scheme (``HERMES_VOIP_*``) carries the STT/TTS/VAD/guard + feature settings.
# These are the key prefixes ``env_enablement_fn`` copies from the process env
# into ``PlatformConfig.extra`` for the running gateway (see :func:`_env_enablement`).
_EXTRA_ENV_PREFIXES: tuple[str, ...] = ("HERMES_SIP_", "HERMES_VOIP_")

# Non-prefixed cloud-credential keys that ``load_media_config`` reads from the
# same ``extra`` mapping.  ``DEEPGRAM_API_KEY`` is required when
# ``HERMES_VOIP_STT_PROVIDER=deepgram``; ``ELEVENLABS_API_KEY`` is required when
# ``HERMES_VOIP_TTS_PROVIDER=elevenlabs``.  Neither carries one of the two
# prefixes above, so they must be copied by exact name alongside the prefix match.
_EXTRA_ENV_KEYS: frozenset[str] = frozenset({"DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"})


def validate_voip_config(config: object) -> bool:
    """Validate the Hermes ``PlatformConfig`` for the VoIP adapter.

    Calls :func:`~hermes_voip.config.load_gateway_config` against the config's
    ``extra`` mapping. Returns ``True`` when the SIP configuration is complete
    and well-formed.

    The Hermes gateway's ``PlatformRegistry.create_adapter`` treats a **falsey**
    return as a validation failure and refuses to build the adapter, so this
    deliberately returns ``True`` on success (never ``None``). A missing or
    malformed SIP env raises :class:`~hermes_voip.config.ConfigError`; the
    registry catches that and treats it as a rejection as well.

    Args:
        config: A Hermes ``PlatformConfig``-like object with an ``extra``
            attribute (``Mapping[str, str]``).

    Returns:
        ``True`` if the SIP configuration is valid.

    Raises:
        ConfigError: If a required SIP env var is absent or invalid.
    """
    extra = getattr(config, "extra", {})
    load_gateway_config(extra)
    return True


def _env_enablement() -> dict[str, str]:
    """Seed ``PlatformConfig.extra`` from the process env for the VoIP adapter.

    The Hermes gateway's registry-driven plugin-platform enable pass
    (``gateway.config._apply_env_overrides``) calls this hook to populate the
    platform's ``extra`` mapping *before* it builds the adapter — and
    :class:`~hermes_voip.adapter.VoipAdapter` reads its entire SIP + media config
    from ``config.extra`` (via ``load_gateway_config``/``load_media_config``).
    Without this seed the gateway enables ``voip`` with an empty ``extra`` and
    ``connect()`` raises :class:`~hermes_voip.config.ConfigError`.

    Returning the env keeps every secret (the SIP password, any cloud key) in the
    process environment only — never written to ``config.yaml`` (rule 34). Two
    classes of keys are copied:

    * Every key matching the ``HERMES_SIP_*`` / ``HERMES_VOIP_*`` prefixes
      (``_EXTRA_ENV_PREFIXES``) — the gateway and media/feature config.
    * The exact non-prefixed cloud-credential keys in ``_EXTRA_ENV_KEYS``
      (``DEEPGRAM_API_KEY``, ``ELEVENLABS_API_KEY``) — required by
      ``load_media_config`` when the matching cloud provider is selected.

    All other process env vars are excluded.

    Returns:
        A mapping of every relevant variable currently set in the process
        environment to its value (empty dict if none are set).
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key.startswith(_EXTRA_ENV_PREFIXES) or key in _EXTRA_ENV_KEYS
    }


def _is_connected(config: object) -> bool:
    """Return whether the SIP credentials are present for this platform.

    The gateway consults this gate before flipping the ``voip`` platform on, so a
    runtime without SIP env configured is left disabled (no noisy retry-forever
    connect attempts) instead of failing inside ``connect()``. It checks the
    probe config's ``extra`` — which the gateway has already seeded via
    :func:`_env_enablement` — for the required SIP keys, reusing the same
    validation as :func:`validate_voip_config` so "connected" means "would build".

    Args:
        config: A Hermes ``PlatformConfig``-like object with an ``extra`` mapping.

    Returns:
        ``True`` if ``extra`` holds a complete, valid SIP configuration.
    """
    extra = getattr(config, "extra", {})
    try:
        load_gateway_config(extra)
    except Exception:  # noqa: BLE001 — any config error means "not configured yet"
        return False
    return True


def _check_fn() -> bool:
    """Probe whether the runtime can build a TLS context (SIP-over-TLS needs it).

    This is the gateway's cheap pre-instantiation dependency check; the real SIP
    connect happens in ``VoipAdapter.connect()``.
    """
    try:
        ssl.create_default_context()
    except Exception:  # noqa: BLE001 — any TLS-context failure means "deps not met"
        return False
    return True


def _adapter_factory(config: object) -> BasePlatformAdapterProtocol:
    """Build the VoIP adapter for a configured platform.

    The real :class:`~hermes_voip.adapter.VoipAdapter` (which subclasses the
    hermes-agent ``BasePlatformAdapter`` and therefore imports the Hermes
    runtime at module top) is imported **here, lazily** — so a bare
    ``import hermes_voip`` never pulls in hermes-agent, but the adapter is a real
    ``BasePlatformAdapter`` subclass the moment the gateway instantiates it.
    """
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    return VoipAdapter(config)


def register(ctx: PluginContextProtocol) -> None:
    """Register the VoIP plugin with the Hermes runtime.

    Called by the Hermes plugin loader (``hermes_cli.plugins``) after the
    package is discovered via the ``hermes_agent.plugins`` entry point.

    Registration is unconditional (``check_fn`` does the runtime probe and
    ``validate_config`` checks the env); the Hermes gateway decides whether to
    activate the platform based on the operator's ``hermes plugins enable
    hermes-voip`` configuration.

    Args:
        ctx: The Hermes ``PluginContext`` — accepts ``register_platform`` calls.
    """
    register_platform = getattr(ctx, "register_platform", None)
    if register_platform is None:
        _log.warning("register(ctx): ctx has no register_platform — skipping")
        return

    register_platform(
        "voip",
        "VoIP (SIP/WebRTC telephony)",
        _adapter_factory,
        _check_fn,
        validate_config=validate_voip_config,
        required_env=list(_REQUIRED_ENV),
        install_hint=_INSTALL_HINT,
        # The gateway's registry-driven enable pass seeds PlatformConfig.extra
        # from env_enablement_fn() and gates enablement on is_connected(probe),
        # so `hermes gateway run` brings the platform up from the HERMES_SIP_*/
        # HERMES_VOIP_* process env (secrets stay in env, never config.yaml).
        env_enablement_fn=_env_enablement,
        is_connected=_is_connected,
    )
