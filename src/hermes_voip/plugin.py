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
import re
import ssl
from typing import TYPE_CHECKING

from hermes_voip.caller_modes import CallerMode as _CallerMode
from hermes_voip.caller_modes import (
    canonical_channel_groups,
    channel_for_group,
    group_for_mode,
)
from hermes_voip.config import load_gateway_config, load_media_config

if TYPE_CHECKING:
    from hermes_voip.hermes_surface import (
        BasePlatformAdapterProtocol,
        PluginContextProtocol,
    )

__all__ = [
    "channel_env_enablement",
    "channel_is_never_independently_connected",
    "channel_platform_names",
    "register",
    "validate_voip_config",
]

_log = logging.getLogger(__name__)

# Install hint shown by ``hermes plugins list``.
#
# IMPORTANT (rule 27): ``hermes plugins enable hermes-voip`` does NOT work for this
# pip/entry-point plugin out of the box — the CLI's enable/list path is
# filesystem-only and never consults importlib entry points, so it prints
# "Plugin 'hermes-voip' is not installed or bundled." and exits 1. The mechanism
# the runtime actually honours is the ``plugins.enabled`` list in Hermes'
# ``config.yaml`` (the gateway loads the entry-point plugin when "hermes-voip" is
# in that list). The hint therefore names that working mechanism; the optional
# directory ``plugin.yaml`` stub (see docs/runbooks/0011) is what makes the
# ``hermes plugins enable`` CLI affordance work, and is documented there.
_INSTALL_HINT = (
    "Set HERMES_SIP_HOST, HERMES_SIP_EXTENSION, and HERMES_SIP_PASSWORD "
    "(or indexed HERMES_SIP_EXTENSION_<n>/PASSWORD_<n> for multiple registrations). "
    "Enable the plugin by adding 'hermes-voip' to plugins.enabled in Hermes' "
    "config.yaml (hermes config path), then run `hermes gateway run`. See the "
    "hermes-voip README for the full quickstart."
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

# The complete set of RECOGNISED env-var names — the plugin.yaml ``requires_env`` +
# ``optional_env`` registry, kept byte-for-byte in sync by the drift test
# ``test_known_env_keys_matches_manifest``. A key matching an ``_EXTRA_ENV_PREFIXES``
# prefix but absent from this set (and not an indexed ``HERMES_SIP_*_<n>`` form, below)
# is a likely operator TYPO: ``_env_enablement`` copies it into ``extra`` but nothing
# reads it, so the default is silently used and the enable gate still passes — hence the
# warning. Generated from the manifest; the drift test forbids divergence.
_KNOWN_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "HERMES_SIP_DEFAULT_EXTENSION",
        "HERMES_SIP_DTMF_INBAND_ENABLED",
        "HERMES_SIP_DTMF_INTERDIGIT_MS",
        "HERMES_SIP_DTMF_MODE",
        "HERMES_SIP_EXPIRES",
        "HERMES_SIP_EXTENSION",
        "HERMES_SIP_HOST",
        "HERMES_SIP_MAX_CALLS",
        "HERMES_SIP_PASSWORD",
        "HERMES_SIP_PORT",
        "HERMES_SIP_SHUTDOWN_DRAIN_SECS",
        "HERMES_SIP_TRANSPORT",
        "HERMES_SIP_USERNAME",
        "HERMES_SIP_USER_AGENT",
        "HERMES_SIP_WS_PASSWORD",
        "HERMES_SIP_WS_PATH",
        "HERMES_VOIP_AEC_BULK_DELAY_MS",
        "HERMES_VOIP_AEC_ENABLED",
        "HERMES_VOIP_AEC_FILTER_MS",
        "HERMES_VOIP_AEC_MU",
        "HERMES_VOIP_AMD",
        "HERMES_VOIP_AMD_HANGUP_ON_FAX",
        "HERMES_VOIP_BARGE_IN_FADE_MS",
        "HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS",
        "HERMES_VOIP_BARGE_IN_MODE",
        "HERMES_VOIP_BARGE_IN_TAIL_MS",
        "HERMES_VOIP_CALL_ON_CONNECT",
        "HERMES_VOIP_CALL_PROGRESS",
        "HERMES_VOIP_CARTESIA_API_KEY",
        "HERMES_VOIP_DECLINE_PHRASE",
        "HERMES_VOIP_DENY_MODE",
        "HERMES_VOIP_DUPLEX_MODE",
        "HERMES_VOIP_ENDPOINT_SILENCE_MS",
        "HERMES_VOIP_ERROR_APOLOGY",
        "HERMES_VOIP_GOODBYE",
        "HERMES_VOIP_GOODBYE_PHRASE",
        "HERMES_VOIP_GREETING",
        "HERMES_VOIP_HANGUP_DRAIN_SECS",
        "HERMES_VOIP_HANGUP_GRACE_SECS",
        "HERMES_VOIP_ICE_STUN_URLS",
        "HERMES_VOIP_ICE_TURN_PASSWORD",
        "HERMES_VOIP_ICE_TURN_URLS",
        "HERMES_VOIP_ICE_TURN_USERNAME",
        "HERMES_VOIP_ICE_USE_IPV4",
        "HERMES_VOIP_ICE_USE_IPV6",
        "HERMES_VOIP_INJECTION_GUARD",
        "HERMES_VOIP_INJECTION_GUARD_MODEL_DIR",
        "HERMES_VOIP_JITTER_MAX_DEPTH",
        "HERMES_VOIP_KEEPALIVE_INTERVAL",
        "HERMES_VOIP_LANGUAGE",
        "HERMES_VOIP_MAX_CONSECUTIVE_REFUSE",
        "HERMES_VOIP_MIN_SE",
        "HERMES_VOIP_NO_INPUT_MAX_REPROMPTS",
        "HERMES_VOIP_NO_INPUT_REPROMPT",
        "HERMES_VOIP_NO_INPUT_REPROMPT_PHRASES",
        "HERMES_VOIP_NO_INPUT_TIMEOUT_MS",
        "HERMES_VOIP_REFUSE_DECLINE_PHRASES",
        "HERMES_VOIP_REQUIRE_SECURE_MEDIA",
        "HERMES_VOIP_RING_TIMEOUT_SECS",
        "HERMES_VOIP_RTCP_ENABLED",
        "HERMES_VOIP_RTP_SYMMETRIC",
        "HERMES_VOIP_RTP_TIMEOUT_SECS",
        "HERMES_VOIP_SECURED_RTCP_ENABLED",
        "HERMES_VOIP_SESSION_EXPIRES",
        "HERMES_VOIP_SIP_DTLS_SETUP",
        "HERMES_VOIP_SIP_DTLS_SRTP",
        "HERMES_VOIP_SIP_SDES_OFFER",
        "HERMES_VOIP_STT_MODEL_DIR",
        "HERMES_VOIP_STT_PROVIDER",
        "HERMES_VOIP_TEST_TONE",
        "HERMES_VOIP_TRANSFER_OUTCOME_TIMEOUT_S",
        "HERMES_VOIP_TTS_COMFORT_FILLER",
        "HERMES_VOIP_TTS_COMFORT_FILLER_DELAY_MS",
        "HERMES_VOIP_TTS_COMFORT_FILLER_PHRASES",
        "HERMES_VOIP_TTS_COMFORT_FILLER_REPEAT_MS",
        "HERMES_VOIP_TTS_FALLBACK",
        "HERMES_VOIP_TTS_FALLBACK_MODEL",
        "HERMES_VOIP_TTS_MODEL",
        "HERMES_VOIP_TTS_PROVIDER",
        "HERMES_VOIP_TTS_SIMILARITY",
        "HERMES_VOIP_TTS_SPEAKER_BOOST",
        "HERMES_VOIP_TTS_STABILITY",
        "HERMES_VOIP_TTS_STREAMING_LATENCY",
        "HERMES_VOIP_TTS_STYLE",
        "HERMES_VOIP_TTS_VOICE",
        "HERMES_VOIP_VAD_MODEL_DIR",
        "HERMES_VOIP_VAD_THRESHOLD",
        "HERMES_VOIP_VIDEO_FPS",
        "HERMES_VOIP_VIDEO_SOURCE_PATH",
        "HERMES_VOIP_WEBRTC_DTLS_SETUP",
    }
)

# Indexed multi-registration keys (``config.py``): ``HERMES_SIP_EXTENSION_<n>`` /
# ``HERMES_SIP_PASSWORD_<n>`` / ``HERMES_SIP_USERNAME_<n>`` for each registration. These
# are valid but dynamic, so they are matched by pattern rather than enumerated above.
_INDEXED_SIP_ENV_RE = re.compile(r"HERMES_SIP_(?:EXTENSION|PASSWORD|USERNAME)_[0-9]+")

# The primary platform name (the one connecting adapter). ADR-0035 adds the caller-
# group CHANNEL platforms as routing aliases of this one.
_PLATFORM_NAME = "voip"

# The Hermes ``platform_hint`` injected into the agent's context for every VoIP
# platform (primary + channel aliases). Telephony is a live, audio-only channel:
# replies are read aloud by TTS, so markdown / code blocks / URLs / emoji are
# actively harmful (spoken literally or dropped). The hint tells the model it is on
# a phone call and to reply in short, spoken-friendly prose (ADR-0046).
_PLATFORM_HINT = (
    "You are speaking on a live phone call. Replies are read aloud, so keep them "
    "short, conversational, and free of markdown, code blocks, URLs, or emoji. "
    "Spell out anything that must be heard."
)


def channel_platform_names() -> tuple[str, ...]:
    """The caller-group CHANNEL platform names this plugin registers (ADR-0035).

    The operator's four canonical channels (``voip-unknown`` / ``voip-known`` /
    ``voip-operator`` / ``voip-intercom``) plus the channels the legacy ADR-0020 modes
    and the outbound group resolve to (``voip-receptionist`` / ``voip-blocked`` /
    ``voip-outbound``) — every channel an inbound or outbound call may route to under
    the default/canonical groups. Registering these as platforms makes
    ``gateway.config.Platform(<channel>)`` resolve (its ``_missing_`` hook only
    accepts registered plugin platforms) and lets the operator target a channel with
    per-platform ``tools_config`` / ``disabled_toolsets``. Custom channels named in a
    groups file are registered on demand by
    :func:`hermes_voip.adapter.ensure_channel_registered`.

    Ordered + de-duplicated; the primary ``voip`` platform is NOT included (it is the
    connecting adapter, registered separately).
    """
    names: list[str] = [channel_for_group(g) for g in canonical_channel_groups()]
    # The legacy ADR-0020 default modes (ALLOW/GREY/OUTBOUND) and their channels.
    for mode in (_CallerMode.ALLOW, _CallerMode.GREY, _CallerMode.OUTBOUND):
        names.append(channel_for_group(group_for_mode(mode)))
    # The blocked group is declined at SIP and never reaches a turn, but include its
    # channel for completeness/audit symmetry (it costs one inert registry entry).
    names.append("voip-blocked")
    # De-duplicate, preserve first-seen order, drop the primary platform if present.
    seen: dict[str, None] = {}
    for name in names:
        if name and name != _PLATFORM_NAME:
            seen.setdefault(name, None)
    return tuple(seen)


def channel_is_never_independently_connected(_config: object) -> bool:
    """A channel alias never connects on its own (the primary ``voip`` does).

    Returning ``False`` keeps the gateway from enabling a second connecting platform
    for a routing alias — the channels exist for session routing + per-platform tool
    config, not their own SIP/RTP transport. (The alias is still *registered*, so
    ``Platform(channel)`` resolves; registration, not enablement, is what ``_missing_``
    checks.) The ``_config`` probe is part of the ``is_connected`` callback contract but
    is unused — the answer is unconditionally "no".

    Public (not ``_``-prefixed) so the adapter's ``ensure_channel_registered`` — which
    builds the on-demand alias ``PlatformEntry`` in the hermes-importing module — can
    reuse the exact same inert-enablement callback as :func:`register`.
    """
    return False


def channel_env_enablement() -> dict[str, str]:
    """A channel alias seeds no env of its own (it does not independently connect).

    Public for the same reason as :func:`channel_is_never_independently_connected`.
    """
    return {}


def validate_voip_config(config: object) -> bool:
    """Validate the Hermes ``PlatformConfig`` for the VoIP adapter.

    Calls :func:`~hermes_voip.config.load_gateway_config` and
    :func:`~hermes_voip.config.load_media_config` against the config's ``extra``
    mapping, then :func:`~hermes_voip.providers.build.check_providers_buildable`
    against the parsed media config. The env loaders check env-var *shape*; the
    provider preflight closes the gap between the (wider) config *vocabulary* and the
    (narrower) set of *wired* providers — so an unimplemented provider token or a
    missing/mis-pathed self-host model directory is rejected HERE, at the enable gate,
    rather than one step later inside ``adapter.connect()`` where ``build_providers``
    runs. The preflight is shallow (no model load): a swapped/corrupt weight or a bad
    licence still surfaces at connect(). Returns ``True`` when the configuration is
    complete and well-formed.

    The Hermes gateway's ``PlatformRegistry.create_adapter`` treats a **falsey**
    return as a validation failure and refuses to build the adapter, so this
    deliberately returns ``True`` on success (never ``None``). A missing or
    malformed SIP/media env — or an unbuildable provider selection — raises
    :class:`~hermes_voip.config.ConfigError`; the registry catches that and treats it
    as a rejection as well.

    Args:
        config: A Hermes ``PlatformConfig``-like object with an ``extra``
            attribute (``Mapping[str, str]``).

    Returns:
        ``True`` if the SIP and media configuration is valid.

    Raises:
        ConfigError: If a required VoIP env var is absent or invalid, a selected
            provider token has no implementation, or a selected self-host provider's
            model directory is unset or absent on disk.
    """
    extra = getattr(config, "extra", {})
    load_gateway_config(extra)
    media = load_media_config(extra)
    # Preflight the provider wiring so a misconfigured provider is rejected at this
    # dedicated gate, not inside connect(). Imported lazily (like the adapter factory)
    # so a bare ``import hermes_voip`` stays light; the import chain is ML-free.
    from hermes_voip.providers.build import check_providers_buildable  # noqa: PLC0415

    check_providers_buildable(media)
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
    seed = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(_EXTRA_ENV_PREFIXES) or key in _EXTRA_ENV_KEYS
    }
    _warn_unknown_env_keys(seed)
    return seed


def _warn_unknown_env_keys(env: dict[str, str]) -> None:
    """Warn (key-only) for each prefixed env var that is not a recognised setting.

    A ``HERMES_SIP_*`` / ``HERMES_VOIP_*`` key not in :data:`_KNOWN_ENV_KEYS` and not
    an indexed ``HERMES_SIP_*_<n>`` form is a likely operator typo — it is copied into
    ``extra`` but no consumer reads it, so the default is silently used and the enable
    gate still passes. The message names the KEY only, never its value (rule 34). The
    non-prefixed cloud keys (``_EXTRA_ENV_KEYS``) are matched by exact name, so a typo
    of one never reaches ``extra`` at all — nothing to warn about there.
    """
    for key in env:
        if not key.startswith(_EXTRA_ENV_PREFIXES):
            continue
        if key in _KNOWN_ENV_KEYS or _INDEXED_SIP_ENV_RE.fullmatch(key):
            continue
        _log.warning(
            "ignoring unrecognised VoIP env var %r: it matches the "
            "HERMES_SIP_/HERMES_VOIP_ prefix but is not a known setting — check for a "
            "typo (its default is used and the platform still enables)",
            key,
        )


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

    ``config`` is typed ``object`` at the hermes-surface boundary (the gateway
    passes an opaque object through the ``Callable[[object], …]`` adapter-factory
    contract in :mod:`hermes_voip.hermes_surface`).  The gateway always supplies a
    real ``PlatformConfig`` at runtime; ``VoipAdapter`` reads the settings it needs
    via ``config.extra``.  No ``isinstance`` check is performed here — the
    ``gateway.config`` module is not imported in this file (doing so would break
    the no-hermes default mypy gate; see ADR-0014).
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
        # Tell the model it is on a live, audio-only phone call (ADR-0046) so it
        # replies in short spoken-friendly prose, not TTS-hostile markdown/URLs/emoji.
        platform_hint=_PLATFORM_HINT,
        emoji="☎️",  # ☎️ telephone — the platform's glyph in the runtime
        # ``cron_deliver_env_var`` is INTENTIONALLY omitted: telephony has no
        # persistent home channel (every call is an ephemeral session), so there is
        # no channel for the runtime to deliver cron/kanban output to — and
        # ``notice_filter.py`` suppresses the home-channel / cron "no home channel"
        # notices that would otherwise be SPOKEN to a caller (ADR-0026 §notices,
        # ADR-0046). Wiring cron delivery here would reintroduce that leak.
    )

    # ADR-0035: register each caller-group CHANNEL as a first-class platform aliasing
    # the one voip adapter (voip channel routing — one Hermes, many VoIP
    # channels). This makes the channel name resolve via Platform(<channel>) and lets
    # the operator scope per-platform tools_config / disabled_toolsets to a channel.
    # The aliases share the primary adapter_factory + check_fn (one telephony
    # endpoint, many routing identities) and never connect independently
    # (is_connected → False), so no second SIP/RTP transport is brought up.
    _register_channel_platforms(ctx)

    # Register the agent-facing VoIP tools (the hang_up tool) + the pre_tool_call
    # gate (ADR-0026). Without this the agent has no way to end a call. Imported
    # lazily so a bare ``import hermes_voip.plugin`` stays light; the helper itself
    # imports no hermes runtime and is resilient to a ctx that lacks
    # register_tool/register_hook (older hermes-agent) — the platform still registers.
    from hermes_voip.voip_tools import register_voip_tools  # noqa: PLC0415

    register_voip_tools(ctx)

    # ADR-0047: register the bundled call-scenario skills (reception, take-message,
    # intercom-open-for-delivery, make-reservation, enquire-price-availability). Each
    # is a read-only, opt-in SKILL.md the agent loads on demand via skill_view; the
    # persona preambles point the agent at the relevant ones. Imported lazily (keeps a
    # bare import light) and guarded against a ctx without register_skill (older
    # hermes-agent) inside the helper — the platform still registers either way.
    from hermes_voip.skills import register_skills  # noqa: PLC0415

    register_skills(ctx)


def _register_channel_platforms(ctx: PluginContextProtocol) -> None:
    """Register each caller-group CHANNEL as a routing-alias platform (ADR-0035).

    One ``register_platform`` call per channel in :func:`channel_platform_names`,
    each reusing the primary ``voip`` adapter factory + ``check_fn`` so a channel is
    a routing identity over the one adapter, not a second connecting platform. The
    aliases declare ``is_connected`` → ``False`` and an empty env seed so the gateway
    enables only the primary ``voip``; the aliases exist so ``Platform(<channel>)``
    resolves and per-platform tool config can target a channel.
    """
    for channel in channel_platform_names():
        ctx.register_platform(
            channel,
            f"VoIP channel: {channel}",
            _adapter_factory,
            _check_fn,
            validate_config=validate_voip_config,
            required_env=list(_REQUIRED_ENV),
            install_hint=_INSTALL_HINT,
            env_enablement_fn=channel_env_enablement,
            is_connected=channel_is_never_independently_connected,
            # Same live-phone-call hint as the primary platform (ADR-0046): a session
            # routed to a channel alias is still a spoken call, so the model needs the
            # same TTS-friendly guidance regardless of which channel it lands on.
            platform_hint=_PLATFORM_HINT,
        )
