"""Parse the ``HERMES_SIP_*`` environment scheme into a typed gateway config.

The plugin registers one or more extensions on a single SIP-over-TLS / WebRTC
gateway (ADR-0011). Connection details live only in the gitignored ``.env`` and
are read by the runtime into a mapping; this module is the **pure** parser that
turns that mapping into a validated :class:`GatewayConfig` plus a tuple of
per-extension :class:`ExtensionConfig`. It reads no process environment itself —
callers pass ``os.environ`` (or ``PlatformConfig.extra``) explicitly — so the
parse is deterministic and unit-testable against fakes.

Two extension schemes are supported, and they MUST NOT be mixed:

* **Single (back-compatible):** ``HERMES_SIP_EXTENSION`` + ``HERMES_SIP_PASSWORD``
  (optional ``HERMES_SIP_USERNAME``). This is index ``0``.
* **Indexed (multiple registrations):** ``HERMES_SIP_EXTENSION_<n>`` +
  ``HERMES_SIP_PASSWORD_<n>`` (optional ``HERMES_SIP_USERNAME_<n>``) for each
  non-negative integer ``<n>``.

Shared gateway settings: ``HERMES_SIP_HOST`` (required), ``HERMES_SIP_PORT``,
``HERMES_SIP_TRANSPORT`` (``tls`` | ``wss``), ``HERMES_SIP_EXPIRES``,
``HERMES_SIP_USER_AGENT``, and ``HERMES_SIP_DEFAULT_EXTENSION`` (the inbound
fallback registration; defaults to the lowest-index extension).

A :class:`GatewayConfig` carries everything env can supply; the transport-derived
``Contact`` and Via ``sent-by`` are not knowable until the socket is up, so
:meth:`GatewayConfig.registration_config` completes a per-extension
:class:`~hermes_voip.registration.RegistrationConfig` from those live inputs.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from hermes_voip.registration import RegistrationConfig

__all__ = [
    "ConfigError",
    "ExtensionConfig",
    "GatewayConfig",
    "MediaConfig",
    "load_gateway_config",
    "load_media_config",
]

# Scheme tokens accepted for HERMES_SIP_TRANSPORT, mapped to their Via transport
# tokens (RFC 3261 §7.1 / RFC 7118). Only the two sanctioned transports.
_VIA_TRANSPORT: dict[str, str] = {"tls": "TLS", "wss": "WSS"}
_DEFAULT_PORT: dict[str, str] = {"tls": "5061", "wss": "443"}

_DEFAULT_TRANSPORT = "tls"
_DEFAULT_EXPIRES = 300
_DEFAULT_USER_AGENT = "hermes-voip/0"

_MIN_PORT = 1
_MAX_PORT = 65535

_HOST_KEY = "HERMES_SIP_HOST"
_PORT_KEY = "HERMES_SIP_PORT"
_TRANSPORT_KEY = "HERMES_SIP_TRANSPORT"
_EXPIRES_KEY = "HERMES_SIP_EXPIRES"
_USER_AGENT_KEY = "HERMES_SIP_USER_AGENT"
_DEFAULT_EXTENSION_KEY = "HERMES_SIP_DEFAULT_EXTENSION"

_BARE_EXTENSION = "HERMES_SIP_EXTENSION"
_BARE_PASSWORD = "HERMES_SIP_PASSWORD"  # noqa: S105 — env var name, not a secret
_BARE_USERNAME = "HERMES_SIP_USERNAME"

_EXTENSION_PREFIX = "HERMES_SIP_EXTENSION_"
_PASSWORD_PREFIX = "HERMES_SIP_PASSWORD_"  # noqa: S105 — env var name, not a secret
_USERNAME_PREFIX = "HERMES_SIP_USERNAME_"

_INDEX_RE = re.compile(r"[0-9]+")

# --- media / provider / feature scheme (ADR-0006..0010) ---------------------
#
# A second env scheme, parsed independently of the gateway/extension scheme
# above. Selection is config-only: every key has a safe default so a bare
# install runs the fully-offline self-host path (sherpa-onnx STT, Kokoro TTS,
# in-process ONNX injection guard). Cloud provider API keys are read *by
# reference only* and never logged (their dataclass fields are repr-suppressed).

# STT (ADR-0006 §"Configuration surface").
_STT_PROVIDER_KEY = "HERMES_VOIP_STT_PROVIDER"
_STT_MODEL_DIR_KEY = "HERMES_VOIP_STT_MODEL_DIR"
_DEFAULT_STT_PROVIDER = "sherpa-onnx"
_STT_PROVIDERS = frozenset({"sherpa-onnx", "deepgram"})

# TTS (ADR-0007 §"Configuration surface").
_TTS_PROVIDER_KEY = "HERMES_VOIP_TTS_PROVIDER"
_TTS_MODEL_KEY = "HERMES_VOIP_TTS_MODEL"
_TTS_VOICE_KEY = "HERMES_VOIP_TTS_VOICE"
_DEFAULT_TTS_PROVIDER = "sherpa-kokoro"
_TTS_PROVIDERS = frozenset(
    {"sherpa-kokoro", "piper", "kittentts", "kyutai", "cartesia", "aura2", "elevenlabs"}
)

# Cloud credentials, consumed by the cloud providers when selected. These are
# the env-var *names* (not secrets); the values are read by reference only and
# never logged (see MediaConfig repr-suppressed fields).
_ELEVENLABS_API_KEY = "ELEVENLABS_API_KEY"
_DEEPGRAM_API_KEY = "DEEPGRAM_API_KEY"
_CARTESIA_API_KEY = "HERMES_VOIP_CARTESIA_API_KEY"
# A selected cloud provider must have its key set (fail-fast, ADR-0006/0007).
_STT_REQUIRED_KEY = {"deepgram": _DEEPGRAM_API_KEY}
_TTS_REQUIRED_KEY = {
    "elevenlabs": _ELEVENLABS_API_KEY,
    "cartesia": _CARTESIA_API_KEY,
    "aura2": _DEEPGRAM_API_KEY,
}

# VAD / endpointing / duplex (ADR-0008). Full-duplex barge-in is a deferred
# Phase-2 design; the enum still accepts the token so config can opt in once the
# capability lands, but the default is the shipped Phase-1 half-duplex path.
_VAD_THRESHOLD_KEY = "HERMES_VOIP_VAD_THRESHOLD"
_ENDPOINT_SILENCE_MS_KEY = "HERMES_VOIP_ENDPOINT_SILENCE_MS"
_DUPLEX_MODE_KEY = "HERMES_VOIP_DUPLEX_MODE"
_DEFAULT_VAD_THRESHOLD = 0.5
_DEFAULT_ENDPOINT_SILENCE_MS = 500
_DEFAULT_DUPLEX_MODE = "half"
_DUPLEX_MODES = frozenset({"half", "full"})
_MIN_VAD_THRESHOLD = 0.0
_MAX_VAD_THRESHOLD = 1.0

# Prompt-injection guard (ADR-0009). Default is the in-process ONNX classifier;
# the optional loopback sidecar is opt-in (and out of this parser's scope).
_INJECTION_GUARD_KEY = "HERMES_VOIP_INJECTION_GUARD"
_INJECTION_GUARD_MODEL_DIR_KEY = "HERMES_VOIP_INJECTION_GUARD_MODEL_DIR"
_DEFAULT_INJECTION_GUARD = "onnx"
_INJECTION_GUARDS = frozenset({"onnx", "sidecar"})

# DTMF (ADR-0010). Default `auto` negotiates RFC 4733 and falls back per offer.
_DTMF_MODE_KEY = "HERMES_SIP_DTMF_MODE"
_DTMF_INTERDIGIT_MS_KEY = "HERMES_SIP_DTMF_INTERDIGIT_MS"
_DTMF_INBAND_ENABLED_KEY = "HERMES_SIP_DTMF_INBAND_ENABLED"
_DEFAULT_DTMF_MODE = "auto"
_DTMF_MODES = frozenset({"auto", "rfc4733", "sip_info", "inband"})
_DEFAULT_DTMF_INBAND_ENABLED = True

# Boolean spellings accepted for the env booleans (case-insensitive, trimmed).
_TRUE_TOKENS = frozenset({"true", "1", "yes", "on"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "off"})


class ConfigError(ValueError):
    """The ``HERMES_SIP_*`` environment is missing, ambiguous, or malformed."""


@dataclass(frozen=True, slots=True)
class ExtensionConfig:
    """One registrable extension, sourced from ``HERMES_SIP_*``.

    Attributes:
        index: The scheme index (``0`` for the back-compatible single form).
        extension: The extension number / SIP user-part (e.g. ``1000``).
        username: The digest auth username (defaults to ``extension``).
        password: The digest auth password.
    """

    index: int
    extension: str
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """The shared SIP gateway plus its registrable extensions.

    Attributes:
        host: The gateway FQDN (the SIP domain / registrar).
        port: The signalling port (transport default unless overridden).
        transport: The scheme token (``tls`` | ``wss``).
        expires: The requested registration lifetime in seconds.
        user_agent: The ``User-Agent`` header value for every registration.
        extensions: All configured extensions, ordered by ``index`` ascending.
        default_index: The ``index`` of the inbound-fallback registration.
    """

    host: str
    port: int
    transport: str
    expires: int
    user_agent: str
    extensions: tuple[ExtensionConfig, ...]
    default_index: int

    def __post_init__(self) -> None:
        """Enforce the invariants the type promises, not just the parser.

        ``GatewayConfig`` is public, so a caller can construct one directly;
        the dataclass validates itself rather than trusting
        :func:`load_gateway_config` to have done so (the ``default_extension``
        lookup and the demux logic depend on these holding).
        """
        if not self.extensions:
            msg = "GatewayConfig requires at least one extension"
            raise ConfigError(msg)
        indices = [ext.index for ext in self.extensions]
        if len(set(indices)) != len(indices):
            msg = "GatewayConfig extension indices must be unique"
            raise ConfigError(msg)
        numbers = [ext.extension for ext in self.extensions]
        if len(set(numbers)) != len(numbers):
            msg = "GatewayConfig extension numbers must be unique"
            raise ConfigError(msg)
        if self.default_index not in indices:
            msg = f"default_index {self.default_index} is not a configured index"
            raise ConfigError(msg)

    @property
    def via_transport(self) -> str:
        """The Via transport token (``TLS`` | ``WSS``) for this gateway."""
        return _VIA_TRANSPORT[self.transport]

    @property
    def default_extension(self) -> ExtensionConfig:
        """The registration that owns inbound calls with no better match."""
        # __post_init__ guarantees exactly one match for default_index.
        return next(ext for ext in self.extensions if ext.index == self.default_index)

    def registration_config(
        self,
        ext: ExtensionConfig,
        *,
        contact: str,
        local_sent_by: str,
    ) -> RegistrationConfig:
        """Complete a :class:`RegistrationConfig` from transport-derived inputs.

        ``contact`` and ``local_sent_by`` are knowable only once the transport
        socket is up (the local host:port, or an ``.invalid`` host for WebSocket
        per RFC 7118), so they are supplied by the caller; everything else comes
        from this env-sourced config. ``ext`` must be one of this gateway's
        configured extensions.
        """
        if ext not in self.extensions:
            msg = f"extension {ext.extension!r} is not configured on this gateway"
            raise ConfigError(msg)
        return RegistrationConfig(
            aor=f"sip:{ext.extension}@{self.host}",
            username=ext.username,
            password=ext.password,
            contact=contact,
            local_sent_by=local_sent_by,
            transport=self.via_transport,
            expires=self.expires,
            user_agent=self.user_agent,
        )


@dataclass(frozen=True, slots=True)
class MediaConfig:
    """The conversational-media + DTMF feature config (ADR-0006..0010).

    Sourced from the ``HERMES_VOIP_*`` / ``HERMES_SIP_DTMF_*`` env scheme, parsed
    independently of :class:`GatewayConfig`. Every field has a default so a bare
    install runs the fully-offline self-host path; cloud API keys are read by
    reference only and are **repr-suppressed** so a secret never reaches a log
    line (invariant: secrets never logged).

    Attributes:
        stt_provider: Streaming-STT provider token (``sherpa-onnx`` default).
        stt_model_dir: Filesystem path to the pinned STT model dir, or ``None``.
        tts_provider: Streaming-TTS provider token (``sherpa-kokoro`` default).
        tts_model: Provider-specific model id / voice-pack, or ``None``.
        tts_voice: Provider-specific voice id, or ``None``.
        elevenlabs_api_key: ElevenLabs credential (by reference; never logged).
        deepgram_api_key: Deepgram credential (by reference; never logged).
        vad_threshold: Voice-activity probability cut-off in ``[0.0, 1.0]``.
        endpoint_silence_ms: Trailing silence (ms) that ends a caller turn.
        duplex_mode: ``half`` (shipped) or ``full`` (deferred Phase-2 barge-in).
        injection_guard: Prompt-injection guard token (``onnx`` in-process default).
        injection_guard_model_dir: Path to the guard's ONNX model dir, or ``None``.
        dtmf_mode: ``auto`` | ``rfc4733`` | ``sip_info`` | ``inband``.
        dtmf_interdigit_ms: Inter-digit gap (ms) for digit aggregation, or ``None``.
        dtmf_inband_enabled: Whether the in-band Goertzel detector is armed.
    """

    stt_provider: str
    stt_model_dir: str | None
    tts_provider: str
    tts_model: str | None
    tts_voice: str | None
    elevenlabs_api_key: str | None = field(repr=False)
    deepgram_api_key: str | None = field(repr=False)
    cartesia_api_key: str | None = field(repr=False)
    vad_threshold: float
    endpoint_silence_ms: int
    duplex_mode: str
    injection_guard: str
    injection_guard_model_dir: str | None
    dtmf_mode: str
    dtmf_interdigit_ms: int | None
    dtmf_inband_enabled: bool

    def __post_init__(self) -> None:
        """Enforce the value invariants the type promises.

        :class:`MediaConfig` is public, so a caller can construct one directly;
        it validates the bounded/enumerated fields itself rather than trusting
        :func:`load_media_config` to have done so.
        """
        if not _finite_in_range(
            self.vad_threshold, _MIN_VAD_THRESHOLD, _MAX_VAD_THRESHOLD
        ):
            msg = (
                f"vad_threshold must be a finite value in "
                f"[{_MIN_VAD_THRESHOLD}, {_MAX_VAD_THRESHOLD}], "
                f"got {self.vad_threshold!r}"
            )
            raise ConfigError(msg)
        if self.endpoint_silence_ms <= 0:
            msg = (
                f"endpoint_silence_ms must be positive, got {self.endpoint_silence_ms}"
            )
            raise ConfigError(msg)
        if self.dtmf_interdigit_ms is not None and self.dtmf_interdigit_ms <= 0:
            msg = (
                f"dtmf_interdigit_ms must be positive when set, "
                f"got {self.dtmf_interdigit_ms}"
            )
            raise ConfigError(msg)
        if self.duplex_mode not in _DUPLEX_MODES:
            allowed = ", ".join(sorted(_DUPLEX_MODES))
            msg = f"duplex_mode must be one of {{{allowed}}}, got {self.duplex_mode!r}"
            raise ConfigError(msg)
        if self.dtmf_mode not in _DTMF_MODES:
            allowed = ", ".join(sorted(_DTMF_MODES))
            msg = f"dtmf_mode must be one of {{{allowed}}}, got {self.dtmf_mode!r}"
            raise ConfigError(msg)
        _require_enum("stt_provider", self.stt_provider, _STT_PROVIDERS)
        _require_enum("tts_provider", self.tts_provider, _TTS_PROVIDERS)
        _require_enum("injection_guard", self.injection_guard, _INJECTION_GUARDS)
        self._require_cloud_keys()

    def _require_cloud_keys(self) -> None:
        """A selected cloud provider must have its credential set (fail-fast)."""
        if (
            key := _STT_REQUIRED_KEY.get(self.stt_provider)
        ) and not self.deepgram_api_key:
            msg = f"stt_provider {self.stt_provider!r} requires {key} to be set"
            raise ConfigError(msg)
        tts_key_env = _TTS_REQUIRED_KEY.get(self.tts_provider)
        if tts_key_env is not None:
            held = {
                _ELEVENLABS_API_KEY: self.elevenlabs_api_key,
                _CARTESIA_API_KEY: self.cartesia_api_key,
                _DEEPGRAM_API_KEY: self.deepgram_api_key,
            }[tts_key_env]
            if not held:
                msg = (
                    f"tts_provider {self.tts_provider!r} requires "
                    f"{tts_key_env} to be set"
                )
                raise ConfigError(msg)


def _require_enum(name: str, value: str, allowed: frozenset[str]) -> None:
    """Raise ConfigError unless ``value`` is one of ``allowed`` (fail-fast)."""
    if value not in allowed:
        opts = ", ".join(sorted(allowed))
        msg = f"{name} must be one of {{{opts}}}, got {value!r}"
        raise ConfigError(msg)


def load_media_config(env: Mapping[str, str]) -> MediaConfig:
    """Parse the media/feature env scheme into a validated :class:`MediaConfig`.

    Additive to :func:`load_gateway_config` and a pure function of ``env`` (no
    process environment is read). Every key is optional and defaults to the
    fully-offline self-host path. Free-form provider/model/voice strings are
    taken as-is (trimmed); enumerated tokens are lower-cased then checked.

    Raises:
        ConfigError: if an enum value is unknown, a numeric value is malformed or
            out of range, or a boolean value is unrecognised.
    """
    return MediaConfig(
        stt_provider=_value_lower(env, _STT_PROVIDER_KEY) or _DEFAULT_STT_PROVIDER,
        stt_model_dir=_optional(env, _STT_MODEL_DIR_KEY),
        tts_provider=_value_lower(env, _TTS_PROVIDER_KEY) or _DEFAULT_TTS_PROVIDER,
        tts_model=_optional(env, _TTS_MODEL_KEY),
        tts_voice=_optional(env, _TTS_VOICE_KEY),
        elevenlabs_api_key=_optional(env, _ELEVENLABS_API_KEY),
        deepgram_api_key=_optional(env, _DEEPGRAM_API_KEY),
        cartesia_api_key=_optional(env, _CARTESIA_API_KEY),
        vad_threshold=_parse_vad_threshold(env),
        endpoint_silence_ms=_parse_positive_int(
            env, _ENDPOINT_SILENCE_MS_KEY, _DEFAULT_ENDPOINT_SILENCE_MS
        ),
        duplex_mode=_parse_enum(
            env, _DUPLEX_MODE_KEY, _DUPLEX_MODES, _DEFAULT_DUPLEX_MODE
        ),
        injection_guard=_value_lower(env, _INJECTION_GUARD_KEY)
        or _DEFAULT_INJECTION_GUARD,
        injection_guard_model_dir=_optional(env, _INJECTION_GUARD_MODEL_DIR_KEY),
        dtmf_mode=_parse_enum(env, _DTMF_MODE_KEY, _DTMF_MODES, _DEFAULT_DTMF_MODE),
        dtmf_interdigit_ms=_parse_optional_positive_int(env, _DTMF_INTERDIGIT_MS_KEY),
        dtmf_inband_enabled=_parse_bool(
            env, _DTMF_INBAND_ENABLED_KEY, _DEFAULT_DTMF_INBAND_ENABLED
        ),
    )


def load_gateway_config(env: Mapping[str, str]) -> GatewayConfig:
    """Parse the ``HERMES_SIP_*`` mapping into a validated :class:`GatewayConfig`.

    Raises:
        ConfigError: if a required value is missing, a value is malformed, the
            single and indexed schemes are mixed, or extension numbers collide.
    """
    host = _require(env, _HOST_KEY)
    transport = _parse_transport(env)
    port = _parse_port(env, transport)
    expires = _parse_expires(env)
    user_agent = _value(env, _USER_AGENT_KEY) or _DEFAULT_USER_AGENT

    extensions = _parse_extensions(env)
    default_index = _resolve_default_index(env, extensions)

    return GatewayConfig(
        host=host,
        port=port,
        transport=transport,
        expires=expires,
        user_agent=user_agent,
        extensions=extensions,
        default_index=default_index,
    )


# --- shared field parsing ---------------------------------------------------


def _value(env: Mapping[str, str], key: str) -> str:
    """Return the trimmed value for ``key``, or ``""`` if unset/blank."""
    raw = env.get(key)
    return raw.strip() if raw is not None else ""


def _require(env: Mapping[str, str], key: str) -> str:
    value = _value(env, key)
    if not value:
        msg = f"{key} is required"
        raise ConfigError(msg)
    return value


def _parse_transport(env: Mapping[str, str]) -> str:
    token = _value(env, _TRANSPORT_KEY).lower() or _DEFAULT_TRANSPORT
    if token not in _VIA_TRANSPORT:
        allowed = ", ".join(sorted(_VIA_TRANSPORT))
        msg = f"{_TRANSPORT_KEY} must be one of {{{allowed}}}, got {token!r}"
        raise ConfigError(msg)
    return token


def _parse_port(env: Mapping[str, str], transport: str) -> int:
    raw = _value(env, _PORT_KEY) or _DEFAULT_PORT[transport]
    port = _parse_int(raw, _PORT_KEY)
    if not _MIN_PORT <= port <= _MAX_PORT:
        msg = f"{_PORT_KEY} must be in [{_MIN_PORT}, {_MAX_PORT}], got {port}"
        raise ConfigError(msg)
    return port


def _parse_expires(env: Mapping[str, str]) -> int:
    raw = _value(env, _EXPIRES_KEY)
    if not raw:
        return _DEFAULT_EXPIRES
    expires = _parse_int(raw, _EXPIRES_KEY)
    if expires <= 0:
        msg = f"{_EXPIRES_KEY} must be positive, got {expires}"
        raise ConfigError(msg)
    return expires


def _parse_int(raw: str, key: str) -> int:
    if not _INDEX_RE.fullmatch(raw):
        msg = f"{key} must be a non-negative integer, got {raw!r}"
        raise ConfigError(msg)
    return int(raw)


# --- media / feature field parsing ------------------------------------------


def _optional(env: Mapping[str, str], key: str) -> str | None:
    """Return the trimmed value for ``key``, or ``None`` if unset/blank.

    A present-but-blank value (``""`` or whitespace) collapses to ``None`` so an
    accidentally-empty override reads as "unset" rather than an empty string.
    """
    value = _value(env, key)
    return value or None


def _value_lower(env: Mapping[str, str], key: str) -> str:
    """Return the trimmed, lower-cased value for ``key``, or ``""`` if unset."""
    return _value(env, key).lower()


def _parse_enum(
    env: Mapping[str, str],
    key: str,
    allowed: frozenset[str],
    default: str,
) -> str:
    """Parse ``key`` as a lower-cased token constrained to ``allowed``."""
    token = _value_lower(env, key) or default
    if token not in allowed:
        choices = ", ".join(sorted(allowed))
        msg = f"{key} must be one of {{{choices}}}, got {token!r}"
        raise ConfigError(msg)
    return token


def _parse_positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    """Parse ``key`` as a strictly-positive integer, defaulting when unset."""
    raw = _value(env, key)
    if not raw:
        return default
    value = _parse_int(raw, key)
    if value <= 0:
        msg = f"{key} must be positive, got {value}"
        raise ConfigError(msg)
    return value


def _parse_optional_positive_int(env: Mapping[str, str], key: str) -> int | None:
    """Parse ``key`` as a strictly-positive integer, or ``None`` if unset."""
    raw = _value(env, key)
    if not raw:
        return None
    value = _parse_int(raw, key)
    if value <= 0:
        msg = f"{key} must be positive, got {value}"
        raise ConfigError(msg)
    return value


def _parse_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    """Parse ``key`` as a boolean from common spellings, defaulting when unset."""
    raw = _value_lower(env, key)
    if not raw:
        return default
    if raw in _TRUE_TOKENS:
        return True
    if raw in _FALSE_TOKENS:
        return False
    truthy = ", ".join(sorted(_TRUE_TOKENS))
    falsy = ", ".join(sorted(_FALSE_TOKENS))
    msg = f"{key} must be a boolean ({truthy} / {falsy}), got {raw!r}"
    raise ConfigError(msg)


def _finite_in_range(value: float, lo: float, hi: float) -> bool:
    """True iff ``value`` is finite (not NaN/inf) and within ``[lo, hi]``."""
    return math.isfinite(value) and lo <= value <= hi


def _parse_vad_threshold(env: Mapping[str, str]) -> float:
    """Parse the VAD threshold as a finite float in ``[0.0, 1.0]``."""
    raw = _value(env, _VAD_THRESHOLD_KEY)
    if not raw:
        return _DEFAULT_VAD_THRESHOLD
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{_VAD_THRESHOLD_KEY} must be a number, got {raw!r}"
        raise ConfigError(msg) from exc
    if not _finite_in_range(value, _MIN_VAD_THRESHOLD, _MAX_VAD_THRESHOLD):
        msg = (
            f"{_VAD_THRESHOLD_KEY} must be a finite value in "
            f"[{_MIN_VAD_THRESHOLD}, {_MAX_VAD_THRESHOLD}], got {raw!r}"
        )
        raise ConfigError(msg)
    return value


# --- extension parsing ------------------------------------------------------


def _parse_extensions(env: Mapping[str, str]) -> tuple[ExtensionConfig, ...]:
    # Any bare credential key — not just the extension — signals the single
    # scheme, so a stray HERMES_SIP_PASSWORD/USERNAME beside the indexed scheme
    # is caught as a mix (a likely typo) rather than silently ignored.
    has_bare = any(
        key in env for key in (_BARE_EXTENSION, _BARE_PASSWORD, _BARE_USERNAME)
    )
    indexed_indices = _indexed_indices(env)

    if has_bare and indexed_indices:
        msg = (
            f"{_BARE_EXTENSION}/{_BARE_PASSWORD} (single) and "
            f"{_EXTENSION_PREFIX}<n> (indexed) schemes must not be combined; "
            "use one"
        )
        raise ConfigError(msg)

    extensions: tuple[ExtensionConfig, ...]
    if has_bare:
        extensions = (_parse_bare_extension(env),)
    else:
        extensions = _parse_indexed_extensions(env, indexed_indices)

    if not extensions:
        msg = (
            f"no extension configured: set {_BARE_EXTENSION} (+{_BARE_PASSWORD}) "
            f"or {_EXTENSION_PREFIX}<n> (+{_PASSWORD_PREFIX}<n>)"
        )
        raise ConfigError(msg)

    _reject_duplicate_numbers(extensions)
    return extensions


def _parse_bare_extension(env: Mapping[str, str]) -> ExtensionConfig:
    extension = _require(env, _BARE_EXTENSION)
    password = _require(env, _BARE_PASSWORD)
    username = _value(env, _BARE_USERNAME) or extension
    return ExtensionConfig(
        index=0, extension=extension, username=username, password=password
    )


def _indexed_indices(env: Mapping[str, str]) -> tuple[int, ...]:
    """Collect and validate the integer indices from ``HERMES_SIP_EXTENSION_<n>``.

    A non-integer suffix is malformed; an indexed ``PASSWORD``/``USERNAME``
    without a matching ``EXTENSION`` is an orphan. Both raise.
    """
    ext_indices = _suffix_indices(env, _EXTENSION_PREFIX)
    pwd_indices = _suffix_indices(env, _PASSWORD_PREFIX)
    user_indices = _suffix_indices(env, _USERNAME_PREFIX)

    orphans = (pwd_indices | user_indices) - ext_indices
    if orphans:
        joined = ", ".join(str(i) for i in sorted(orphans))
        msg = (
            f"indexed {_PASSWORD_PREFIX}/{_USERNAME_PREFIX} without a matching "
            f"{_EXTENSION_PREFIX} for index(es): {joined}"
        )
        raise ConfigError(msg)
    return tuple(sorted(ext_indices))


def _suffix_indices(env: Mapping[str, str], prefix: str) -> set[int]:
    indices: set[int] = set()
    for key in env:
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        if not _INDEX_RE.fullmatch(suffix):
            msg = f"{key}: index suffix must be a non-negative integer"
            raise ConfigError(msg)
        index = int(suffix)
        if index in indices:
            msg = f"duplicate index {index} from {prefix}<n> keys"
            raise ConfigError(msg)
        indices.add(index)
    return indices


def _parse_indexed_extensions(
    env: Mapping[str, str], indices: tuple[int, ...]
) -> tuple[ExtensionConfig, ...]:
    configs: list[ExtensionConfig] = []
    for index in indices:
        extension = _require(env, f"{_EXTENSION_PREFIX}{index}")
        password = _require(env, f"{_PASSWORD_PREFIX}{index}")
        username = _value(env, f"{_USERNAME_PREFIX}{index}") or extension
        configs.append(
            ExtensionConfig(
                index=index,
                extension=extension,
                username=username,
                password=password,
            )
        )
    return tuple(configs)


def _reject_duplicate_numbers(extensions: tuple[ExtensionConfig, ...]) -> None:
    seen: set[str] = set()
    for ext in extensions:
        if ext.extension in seen:
            msg = f"duplicate extension number {ext.extension!r}"
            raise ConfigError(msg)
        seen.add(ext.extension)


def _resolve_default_index(
    env: Mapping[str, str], extensions: tuple[ExtensionConfig, ...]
) -> int:
    chosen = _value(env, _DEFAULT_EXTENSION_KEY)
    if not chosen:
        # Lowest index wins; extensions are sorted ascending.
        return extensions[0].index
    for ext in extensions:
        if ext.extension == chosen:
            return ext.index
    available = ", ".join(ext.extension for ext in extensions)
    msg = (
        f"{_DEFAULT_EXTENSION_KEY}={chosen!r} is not a configured extension "
        f"(have: {available})"
    )
    raise ConfigError(msg)
