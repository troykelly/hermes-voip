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
    "DEFAULT_GREETING",
    "ConfigError",
    "ExtensionConfig",
    "GatewayConfig",
    "MediaConfig",
    "load_gateway_config",
    "load_media_config",
]

#: The opening line the agent speaks the instant an inbound call is answered,
#: unless ``HERMES_VOIP_GREETING`` overrides it (ADR-0002 §"NAT / symmetric-RTP
#: latching"). Speaking on answer makes the plugin send RTP first, which both
#: lets the caller hear something immediately and gives a symmetric-RTP gateway
#: behind NAT a source tuple to latch onto so the return media path opens. An
#: explicitly-empty override disables the greeting entirely.
DEFAULT_GREETING = (
    "Hello, you're through to the Hermes voice assistant. How can I help?"
)

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

# ElevenLabs dynamic-voice tuning (ADR-0007 amendment, 2026-06-17). Optional knobs
# that let the operator A/B voice dynamism on live calls WITHOUT a code change:
# they map onto the ElevenLabs request's ``voice_settings`` object + the
# ``optimize_streaming_latency`` query param. Each is provider-agnostic at this
# layer — unset (``None``) means "the ElevenLabs provider applies its dynamic
# default" (a lower-than-flat stability), so the default install is already livelier
# than ElevenLabs' monotone 0.5; a self-host provider simply ignores them.
_TTS_STABILITY_KEY = "HERMES_VOIP_TTS_STABILITY"
_TTS_STYLE_KEY = "HERMES_VOIP_TTS_STYLE"
_TTS_SIMILARITY_KEY = "HERMES_VOIP_TTS_SIMILARITY"
_TTS_SPEAKER_BOOST_KEY = "HERMES_VOIP_TTS_SPEAKER_BOOST"
_TTS_STREAMING_LATENCY_KEY = "HERMES_VOIP_TTS_STREAMING_LATENCY"
# The ElevenLabs voice_settings floats are 0.0-1.0; optimize_streaming_latency is
# an int in [0, 4] (0 = none ... 4 = max, text-normaliser off).
_MIN_TTS_SETTING = 0.0
_MAX_TTS_SETTING = 1.0
_MIN_TTS_STREAMING_LATENCY = 0
_MAX_TTS_STREAMING_LATENCY = 4

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

# Opening greeting spoken on inbound-call answer (ADR-0002 NAT-latch). Absent →
# the friendly DEFAULT_GREETING; present-but-empty (or whitespace) → no greeting.
_GREETING_KEY = "HERMES_VOIP_GREETING"

# Echo-robust barge-in (ADR-0023). The gateway can reflect the agent's own TTS
# back on the inbound path (no echo cancellation), and the VAD/ASR transcribe it
# as the caller — a single ONSET then barged the agent in, ending its own turn (a
# self-interruption loop). Mode `gated` (default) requires a SUSTAINED voiced run
# while the agent's TTS plays (and for a short tail after) before a barge-in
# counts, so short echo blips never interrupt but a genuine interruption still
# does. `full` is the legacy immediate barge-in (for echo-cancelled gateways);
# `off` disables barge-in entirely.
_BARGE_IN_MODE_KEY = "HERMES_VOIP_BARGE_IN_MODE"
_BARGE_IN_MIN_SPEECH_MS_KEY = "HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS"
_BARGE_IN_TAIL_MS_KEY = "HERMES_VOIP_BARGE_IN_TAIL_MS"
_DEFAULT_BARGE_IN_MODE = "gated"
_BARGE_IN_MODES = frozenset({"off", "gated", "full"})
# 600 ms ≈ 19 VAD windows at 8 kHz — above the longest observed gateway-echo
# burst (~15 windows ≈ 480 ms in the live log), with margin, so echo never
# reaches the sustained-barge-in threshold while a real interruption (which
# sustains well beyond 600 ms) still does.
_DEFAULT_BARGE_IN_MIN_SPEECH_MS = 600
_DEFAULT_BARGE_IN_TAIL_MS = 250

# Symmetric-RTP (comedia) latching for NAT traversal (ADR-0005 §NAT). When on
# (the default) the media engine latches its outbound destination onto the peer's
# real RTP source — the source tuple of the first valid inbound RTP packet —
# instead of trusting the SDP c=/m= address (which under NAT may be a private or
# SBC-rewritten address the peer's media never comes from). Set false to always
# honour the SDP address (for gateways that route RTP by the negotiated address).
_RTP_SYMMETRIC_KEY = "HERMES_VOIP_RTP_SYMMETRIC"
_DEFAULT_RTP_SYMMETRIC = True

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

# Tone diagnostic (operator-use only).  When set to a positive number of
# seconds the call opening plays a generated 440 Hz sine tone directly at
# 8 kHz (bypassing TTS + resample) instead of the TTS greeting.  This lets
# the operator confirm the RTP transport and G.711 codec are working before
# implicating the TTS/resample layers.  Unset / 0 = normal operation.
_TEST_TONE_KEY = "HERMES_VOIP_TEST_TONE"
_DEFAULT_TEST_TONE_SECS: float = 0.0

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
        tts_model: Provider-specific model id / voice-pack, or ``None``. For
            sherpa-kokoro this is the model *directory*; for ElevenLabs it is the
            synthesis model id (``None`` → the provider's Flash v2.5 default).
        tts_voice: Provider-specific voice id, or ``None``.
        elevenlabs_api_key: ElevenLabs credential (by reference; never logged).
        deepgram_api_key: Deepgram credential (by reference; never logged).
        vad_threshold: Voice-activity probability cut-off in ``[0.0, 1.0]``.
        endpoint_silence_ms: Trailing silence (ms) that ends a caller turn.
        duplex_mode: ``half`` (shipped) or ``full`` (deferred Phase-2 barge-in).
        greeting: Opening line spoken the instant an inbound call is answered
            (``DEFAULT_GREETING`` when unset; ``""`` disables it). Speaking on
            answer sends RTP first — the caller hears it immediately and a
            symmetric-RTP gateway behind NAT latches onto our source tuple.
        rtp_symmetric: Whether the media engine latches its outbound RTP onto the
            peer's real source tuple (the first valid inbound RTP packet) for NAT
            traversal — ``True`` by default. ``False`` always honours the SDP
            ``c=``/``m=`` address.
        barge_in_mode: Echo-robust barge-in mode (ADR-0023): ``gated`` (default —
            require a sustained voiced run while TTS plays), ``full`` (legacy
            immediate barge-in on any onset), or ``off`` (never barge in).
        barge_in_min_speech_ms: In ``gated`` mode, the minimum sustained voiced
            run (ms) required to barge in while the agent's TTS is playing (or in
            the tail after). Must be positive. Short echo blips never reach it.
        barge_in_tail_ms: How long (ms) after the agent's TTS ends the gate keeps
            requiring a sustained run (echo lags the TTS via jitter/network).
            ``0`` disarms the instant TTS ends; must be non-negative.
        injection_guard: Prompt-injection guard token (``onnx`` in-process default).
        injection_guard_model_dir: Path to the guard's ONNX model dir, or ``None``.
        dtmf_mode: ``auto`` | ``rfc4733`` | ``sip_info`` | ``inband``.
        dtmf_interdigit_ms: Inter-digit gap (ms) for digit aggregation, or ``None``.
        dtmf_inband_enabled: Whether the in-band Goertzel detector is armed.
        tone_secs: When positive, the call opening plays a generated 440 Hz sine
            tone for this many seconds at 8 kHz (bypassing TTS + resample) so the
            operator can isolate the RTP transport layer from TTS issues.
            ``0.0`` (the default) means normal operation (TTS greeting).
        tts_stability: ElevenLabs ``voice_settings.stability`` in ``[0.0, 1.0]``, or
            ``None`` to use the provider's dynamic default. *Lower* = more
            expressive/varied (the main dynamism dial); too low = inconsistent.
        tts_style: ElevenLabs ``voice_settings.style`` in ``[0.0, 1.0]``, or
            ``None`` for the provider default (``0.0``). Above 0 adds expression but
            costs stability and may add latency — raise deliberately.
        tts_similarity: ElevenLabs ``voice_settings.similarity_boost`` in
            ``[0.0, 1.0]``, or ``None`` for the provider default.
        tts_speaker_boost: ElevenLabs ``voice_settings.use_speaker_boost``, or
            ``None`` for the provider default (``True``).
        tts_streaming_latency: ElevenLabs ``optimize_streaming_latency`` query value
            (int in ``[0, 4]``), or ``None`` to send nothing (the default —
            deprecated param; ``4`` disables number/date normalisation).
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
    greeting: str
    rtp_symmetric: bool
    barge_in_mode: str
    barge_in_min_speech_ms: int
    barge_in_tail_ms: int
    injection_guard: str
    injection_guard_model_dir: str | None
    dtmf_mode: str
    dtmf_interdigit_ms: int | None
    dtmf_inband_enabled: bool
    tone_secs: float
    # ElevenLabs dynamic-voice tuning. Defaulted to None so existing direct
    # constructions stay valid and an unset knob means "provider default" (a
    # dynamic-but-stable voice), not a flat override.
    tts_stability: float | None = None
    tts_style: float | None = None
    tts_similarity: float | None = None
    tts_speaker_boost: bool | None = None
    tts_streaming_latency: int | None = None

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
        if self.barge_in_mode not in _BARGE_IN_MODES:
            allowed = ", ".join(sorted(_BARGE_IN_MODES))
            msg = (
                f"barge_in_mode must be one of {{{allowed}}}, "
                f"got {self.barge_in_mode!r}"
            )
            raise ConfigError(msg)
        if self.barge_in_min_speech_ms <= 0:
            msg = (
                f"barge_in_min_speech_ms must be positive, "
                f"got {self.barge_in_min_speech_ms}"
            )
            raise ConfigError(msg)
        if self.barge_in_tail_ms < 0:
            msg = f"barge_in_tail_ms must be non-negative, got {self.barge_in_tail_ms}"
            raise ConfigError(msg)
        if self.dtmf_mode not in _DTMF_MODES:
            allowed = ", ".join(sorted(_DTMF_MODES))
            msg = f"dtmf_mode must be one of {{{allowed}}}, got {self.dtmf_mode!r}"
            raise ConfigError(msg)
        _require_enum("stt_provider", self.stt_provider, _STT_PROVIDERS)
        _require_enum("tts_provider", self.tts_provider, _TTS_PROVIDERS)
        _require_enum("injection_guard", self.injection_guard, _INJECTION_GUARDS)
        if not math.isfinite(self.tone_secs) or self.tone_secs < 0:
            msg = (
                "tone_secs must be a non-negative finite number, "
                f"got {self.tone_secs!r}"
            )
            raise ConfigError(msg)
        self._validate_tts_tuning()
        self._require_cloud_keys()

    def _validate_tts_tuning(self) -> None:
        """Validate the optional ElevenLabs voice-tuning knobs (when set).

        Each float must be finite and within ``[0.0, 1.0]``; the streaming-latency
        int must be within ``[0, 4]``. ``None`` (unset) is always valid — the
        provider then applies its dynamic default for that field.
        """
        for name, value in (
            ("tts_stability", self.tts_stability),
            ("tts_style", self.tts_style),
            ("tts_similarity", self.tts_similarity),
        ):
            if value is not None and not _finite_in_range(
                value, _MIN_TTS_SETTING, _MAX_TTS_SETTING
            ):
                msg = (
                    f"{name} must be a finite value in "
                    f"[{_MIN_TTS_SETTING}, {_MAX_TTS_SETTING}], got {value!r}"
                )
                raise ConfigError(msg)
        if self.tts_streaming_latency is not None and not (
            _MIN_TTS_STREAMING_LATENCY
            <= self.tts_streaming_latency
            <= _MAX_TTS_STREAMING_LATENCY
        ):
            msg = (
                f"tts_streaming_latency must be in "
                f"[{_MIN_TTS_STREAMING_LATENCY}, {_MAX_TTS_STREAMING_LATENCY}], "
                f"got {self.tts_streaming_latency}"
            )
            raise ConfigError(msg)

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
        greeting=_parse_greeting(env),
        rtp_symmetric=_parse_bool(env, _RTP_SYMMETRIC_KEY, _DEFAULT_RTP_SYMMETRIC),
        barge_in_mode=_parse_enum(
            env, _BARGE_IN_MODE_KEY, _BARGE_IN_MODES, _DEFAULT_BARGE_IN_MODE
        ),
        barge_in_min_speech_ms=_parse_positive_int(
            env, _BARGE_IN_MIN_SPEECH_MS_KEY, _DEFAULT_BARGE_IN_MIN_SPEECH_MS
        ),
        barge_in_tail_ms=_parse_non_negative_int(
            env, _BARGE_IN_TAIL_MS_KEY, _DEFAULT_BARGE_IN_TAIL_MS
        ),
        injection_guard=_value_lower(env, _INJECTION_GUARD_KEY)
        or _DEFAULT_INJECTION_GUARD,
        injection_guard_model_dir=_optional(env, _INJECTION_GUARD_MODEL_DIR_KEY),
        dtmf_mode=_parse_enum(env, _DTMF_MODE_KEY, _DTMF_MODES, _DEFAULT_DTMF_MODE),
        dtmf_interdigit_ms=_parse_optional_positive_int(env, _DTMF_INTERDIGIT_MS_KEY),
        dtmf_inband_enabled=_parse_bool(
            env, _DTMF_INBAND_ENABLED_KEY, _DEFAULT_DTMF_INBAND_ENABLED
        ),
        tone_secs=_parse_tone_secs(env),
        tts_stability=_parse_optional_unit_float(env, _TTS_STABILITY_KEY),
        tts_style=_parse_optional_unit_float(env, _TTS_STYLE_KEY),
        tts_similarity=_parse_optional_unit_float(env, _TTS_SIMILARITY_KEY),
        tts_speaker_boost=_parse_optional_bool(env, _TTS_SPEAKER_BOOST_KEY),
        tts_streaming_latency=_parse_optional_bounded_int(
            env,
            _TTS_STREAMING_LATENCY_KEY,
            _MIN_TTS_STREAMING_LATENCY,
            _MAX_TTS_STREAMING_LATENCY,
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


def _parse_non_negative_int(env: Mapping[str, str], key: str, default: int) -> int:
    """Parse ``key`` as a ``>= 0`` integer, defaulting when unset.

    Unlike :func:`_parse_positive_int`, ``0`` is accepted (e.g. a barge-in tail of
    0 ms means "disarm the instant TTS ends"). The shared ``_parse_int`` already
    rejects negatives (its regex matches only non-negative integers), so a
    malformed or negative value raises :class:`ConfigError`.
    """
    raw = _value(env, key)
    if not raw:
        return default
    return _parse_int(raw, key)


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


def _parse_optional_bool(env: Mapping[str, str], key: str) -> bool | None:
    """Parse ``key`` as a boolean, or ``None`` when unset (no default applied).

    Unlike :func:`_parse_bool` there is no fallback value: an unset knob stays
    ``None`` so a downstream provider can supply its own default. A present-but-
    unrecognised spelling still raises.
    """
    if not _value(env, key):
        return None
    return _parse_bool(env, key, default=False)


def _parse_optional_unit_float(env: Mapping[str, str], key: str) -> float | None:
    """Parse ``key`` as a float in ``[0.0, 1.0]``, or ``None`` when unset.

    NaN/inf and out-of-range values raise (NaN slips past a naive ``lo <= x <= hi``
    test, so :func:`_finite_in_range` rejects it explicitly). Used by the ElevenLabs
    voice-tuning knobs, whose ``voice_settings`` floats are 0.0-1.0.

    Raises:
        ConfigError: If the value is non-numeric, NaN/inf, or outside ``[0, 1]``.
    """
    raw = _value(env, key)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{key} must be a number in [0.0, 1.0], got {raw!r}"
        raise ConfigError(msg) from exc
    if not _finite_in_range(value, _MIN_TTS_SETTING, _MAX_TTS_SETTING):
        msg = (
            f"{key} must be a finite value in "
            f"[{_MIN_TTS_SETTING}, {_MAX_TTS_SETTING}], got {raw!r}"
        )
        raise ConfigError(msg)
    return value


def _parse_optional_bounded_int(
    env: Mapping[str, str], key: str, lo: int, hi: int
) -> int | None:
    """Parse ``key`` as an int within ``[lo, hi]``, or ``None`` when unset.

    Raises:
        ConfigError: If the value is not an integer or is outside ``[lo, hi]``.
    """
    raw = _value(env, key)
    if not raw:
        return None
    value = _parse_int(raw, key)
    if not lo <= value <= hi:
        msg = f"{key} must be in [{lo}, {hi}], got {value}"
        raise ConfigError(msg)
    return value


def _finite_in_range(value: float, lo: float, hi: float) -> bool:
    """True iff ``value`` is finite (not NaN/inf) and within ``[lo, hi]``."""
    return math.isfinite(value) and lo <= value <= hi


def _parse_greeting(env: Mapping[str, str]) -> str:
    """Parse the opening greeting, distinguishing 'unset' from 'explicitly empty'.

    Unlike :func:`_optional` (which collapses a blank value to ``None``), the
    greeting must tell apart two intents: *unset* → use the friendly
    :data:`DEFAULT_GREETING`; *present-but-empty* (``""`` or whitespace) → opt
    out of any greeting (returns ``""``). A set value is trimmed.
    """
    raw = env.get(_GREETING_KEY)
    if raw is None:  # key absent → friendly default
        return DEFAULT_GREETING
    return raw.strip()  # present (incl. empty/whitespace) → verbatim, trimmed


def _parse_tone_secs(env: Mapping[str, str]) -> float:
    """Parse ``HERMES_VOIP_TEST_TONE`` as a non-negative float (seconds).

    Absent or ``"0"`` → ``0.0`` (tone disabled, normal operation). A positive
    value enables the diagnostic tone path for that many seconds.

    Raises:
        ConfigError: If the value is set but not a valid non-negative number.
    """
    raw = _value(env, _TEST_TONE_KEY)
    if not raw:
        return _DEFAULT_TEST_TONE_SECS
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{_TEST_TONE_KEY} must be a number of seconds, got {raw!r}"
        raise ConfigError(msg) from exc
    if not math.isfinite(value) or value < 0:
        msg = f"{_TEST_TONE_KEY} must be a non-negative number, got {raw!r}"
        raise ConfigError(msg)
    return value


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
