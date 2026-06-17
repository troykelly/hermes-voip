"""Config → concrete provider instance wiring (W8, ADR-0004).

:func:`build_providers` maps a :class:`~hermes_voip.config.MediaConfig` to live
concrete provider instances for the three swappable families (streaming ASR,
streaming TTS, prompt-injection guard). It is the single call-site that turns the
config-selected provider tokens into objects the call loop uses.

Selection mechanism (ADR-0004 §"Selection and registration": *a registry maps a
config key to a factory*)
-----------------------------------------------------------------------------
Resolution is **direct dispatch over a typed factory map** — one
``Mapping[str, <Family>Factory]`` per family, where each factory is
``Callable[[MediaConfig], <Provider>]``. The selected provider is
``factories[token](config)``. There is no shared mutable state: dispatch is a pure
read of an immutable mapping, so concurrent :func:`build_providers` calls never
race (the earlier mutate-a-global-registry/restore approach did — fixed here).

The default maps (:data:`DEFAULT_ASR_FACTORIES`, :data:`DEFAULT_TTS_FACTORIES`,
:data:`DEFAULT_GUARD_FACTORIES`) wire every shipped token to its production
factory. Tests (and any embedder wanting a different wiring) inject alternative
maps through the explicit ``asr_factories`` / ``tts_factories`` /
``guard_factories`` keyword seam — never by mutating module state.

(The generic :class:`~hermes_voip.providers.registry.ProviderRegistry` — a
name→zero-arg-factory map with raise-on-duplicate — remains available for any
future *dynamic* external-provider registration, where a mutable registry with
collision detection is the right tool. W8's wiring is static and config-aware, so
it uses the simpler, thread-safe direct map; ``ProviderRegistry`` is intentionally
not used here.)

Design invariants
-----------------
- **Fail-fast on misconfiguration**: a self-host provider whose required model dir
  is ``None`` raises :class:`~hermes_voip.config.ConfigError` naming the missing
  env var, inside the factory, before any model load.
- **Licence gate (rule 35 / ADR-0006/0007/0009)**: every *self-host default* model
  factory runs :func:`~hermes_voip.manifest.validate_manifest` against the family's
  pinned :class:`~hermes_voip.manifest.ModelManifest` before constructing the
  provider — STT (:data:`~hermes_voip.manifest.STT_MODEL_MANIFEST`), TTS
  (:data:`~hermes_voip.manifest.TTS_MODEL_MANIFEST`), and the ONNX guard
  (:data:`~hermes_voip.manifest.GUARD_MODEL_MANIFEST`). Cloud providers
  (Deepgram, ElevenLabs) carry no committed model artifact and skip the gate.
  :class:`~hermes_voip.manifest.LicenceError` propagates uncaught (rule 37).
- **ML-free import**: importing this module never loads sherpa-onnx, onnxruntime,
  tokenizers, or websockets. Each factory lazy-imports the concrete class only when
  invoked.

Env keys named in :class:`~hermes_voip.config.ConfigError` messages
---------------------------------------------------------------------
- ``HERMES_VOIP_STT_MODEL_DIR`` — required for ``sherpa-onnx``
- ``HERMES_VOIP_TTS_MODEL`` — required for ``sherpa-kokoro``
- ``HERMES_VOIP_INJECTION_GUARD_MODEL_DIR`` — required for ``onnx`` guard
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from hermes_voip.config import ConfigError, MediaConfig
from hermes_voip.manifest import (
    GUARD_MODEL_MANIFEST,
    STT_MODEL_MANIFEST,
    TTS_MODEL_MANIFEST,
    ModelFamily,
    validate_manifest,
)
from hermes_voip.providers.asr import StreamingASR
from hermes_voip.providers.guard import InjectionGuard
from hermes_voip.providers.tts import StreamingTTS

__all__ = [
    "DEFAULT_ASR_FACTORIES",
    "DEFAULT_GUARD_FACTORIES",
    "DEFAULT_TTS_FACTORIES",
    "GUARD_MODEL_MANIFEST",
    "STT_MODEL_MANIFEST",
    "TTS_MODEL_MANIFEST",
    "AsrFactory",
    "GuardFactory",
    "Providers",
    "TtsFactory",
    "build_providers",
]

# A provider factory takes the full validated MediaConfig and returns one live
# provider. Config-aware (not zero-arg) so a factory reads exactly the fields its
# provider needs (model dir, api key, voice) without a global or a partial closure.
type AsrFactory = Callable[[MediaConfig], StreamingASR]
type TtsFactory = Callable[[MediaConfig], StreamingTTS]
type GuardFactory = Callable[[MediaConfig], InjectionGuard]


# ---------------------------------------------------------------------------
# Result type returned by build_providers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Providers:
    """The resolved, live provider instances for one plugin session.

    Attributes:
        asr: The active streaming speech-to-text provider.
        tts: The active streaming text-to-speech provider.
        guard: The active prompt-injection guard.
    """

    asr: StreamingASR
    tts: StreamingTTS
    guard: InjectionGuard


# ---------------------------------------------------------------------------
# Production factories — one per shipped token. Each is config-aware: it reads
# the fields its provider needs, fails fast on a missing required model dir,
# runs the licence gate for self-host defaults, and lazy-imports the concrete
# class so importing this module pulls no ml/cloud dependency.
# ---------------------------------------------------------------------------


def _make_sherpa_onnx_asr(config: MediaConfig) -> StreamingASR:
    """Build the self-host sherpa-onnx streaming zipformer recogniser (ADR-0006)."""
    model_dir = config.stt_model_dir
    if model_dir is None:
        msg = "stt_provider 'sherpa-onnx' requires HERMES_VOIP_STT_MODEL_DIR to be set"
        raise ConfigError(msg)
    # Licence gate (rule 35): the pinned default STT model must declare an
    # STT-family-allowed SPDX before we construct the provider.
    validate_manifest(STT_MODEL_MANIFEST, ModelFamily.STT)
    from hermes_voip.stt.sherpa_onnx import SherpaOnnxASR  # noqa: PLC0415

    return SherpaOnnxASR(model_dir)


def _make_deepgram_asr(config: MediaConfig) -> StreamingASR:
    """Build the Deepgram Flux cloud-fallback recogniser (ADR-0006, no model pin)."""
    # Cloud provider: no committed model artifact, so no licence gate. The key
    # presence is already enforced by MediaConfig.__post_init__ (fail-fast there).
    api_key = config.deepgram_api_key or ""
    from hermes_voip.stt.deepgram import DeepgramASR  # noqa: PLC0415

    return DeepgramASR(api_key)


def _make_sherpa_kokoro_tts(config: MediaConfig) -> StreamingTTS:
    """Build the self-host sherpa-onnx + Kokoro-82M synthesiser (ADR-0007)."""
    model_dir = config.tts_model
    if model_dir is None:
        msg = "tts_provider 'sherpa-kokoro' requires HERMES_VOIP_TTS_MODEL to be set"
        raise ConfigError(msg)
    # Licence gate (rule 35): the pinned default TTS model must declare a
    # TTS-family-allowed SPDX before we construct the provider.
    validate_manifest(TTS_MODEL_MANIFEST, ModelFamily.TTS)
    voice = config.tts_voice or ""
    from hermes_voip.tts.sherpa_kokoro import SherpaKokoroTTS  # noqa: PLC0415

    return SherpaKokoroTTS(model_dir=model_dir, voice=voice)


def _make_elevenlabs_tts(config: MediaConfig) -> StreamingTTS:
    """Build the ElevenLabs cloud synthesiser (ADR-0007); Flash v2.5 by default.

    Threads the optional ElevenLabs tuning knobs into the provider so the operator
    can A/B voice dynamism via env without a code change: ``tts_model`` is the model
    id (default Flash v2.5 — the only real-time-streaming, recommended voice-agent
    model), and the ``tts_*`` voice-settings knobs build the request's
    ``voice_settings``. Each unset knob falls back to the dynamic-but-stable
    :data:`DEFAULT_VOICE_SETTINGS` field (lower-than-flat stability), so a bare
    ElevenLabs install is already livelier than the API's monotone default.
    """
    # Cloud provider: no committed model artifact, so no licence gate. The key
    # presence is already enforced by MediaConfig.__post_init__ (fail-fast there).
    api_key = config.elevenlabs_api_key or ""
    voice = config.tts_voice or ""
    from hermes_voip.tts.elevenlabs import (  # noqa: PLC0415
        DEFAULT_VOICE_SETTINGS,
        FLASH_V2_5_MODEL_ID,
        G711_NARROWBAND_RATE,
        ElevenLabsTTS,
        ElevenLabsVoiceSettings,
    )

    # Per-field fallback to the dynamic default: an unset knob keeps the dynamic
    # value (not a flat one), a set knob overrides only that field. Ranges are
    # already validated by MediaConfig.__post_init__.
    default = DEFAULT_VOICE_SETTINGS
    voice_settings = ElevenLabsVoiceSettings(
        stability=default.stability
        if config.tts_stability is None
        else config.tts_stability,
        similarity_boost=default.similarity_boost
        if config.tts_similarity is None
        else config.tts_similarity,
        style=default.style if config.tts_style is None else config.tts_style,
        use_speaker_boost=default.use_speaker_boost
        if config.tts_speaker_boost is None
        else config.tts_speaker_boost,
    )
    # tts_model is the model id for ElevenLabs (it is a model DIRECTORY only for the
    # self-host sherpa-kokoro provider); unset keeps the Flash v2.5 default.
    model_id = config.tts_model or FLASH_V2_5_MODEL_ID

    # The provider is built ONCE per process, but the wire rate is per-call
    # (codec-derived). So the construction DEFAULT is the G.711 8 kHz case (no
    # resample — the "very choppy" fix, ADR-0007 amendment), and the call loop
    # passes the negotiated wire rate per call via synthesize(sample_rate=…)
    # (ADR-0022): a G.722 call requests pcm_16000 natively, a G.711 call keeps this
    # 8 kHz default. So this stays the narrowband default; the per-call override is
    # the codec→rate hook, not this line.
    return ElevenLabsTTS(
        api_key=api_key,
        voice=voice,
        model_id=model_id,
        voice_settings=voice_settings,
        optimize_streaming_latency=config.tts_streaming_latency,
        output_sample_rate=G711_NARROWBAND_RATE,
    )


def _make_onnx_guard(config: MediaConfig) -> InjectionGuard:
    """Build the in-process ONNX DeBERTa injection guard (ADR-0009)."""
    model_dir = config.injection_guard_model_dir
    if model_dir is None:
        msg = (
            "injection_guard 'onnx' requires "
            "HERMES_VOIP_INJECTION_GUARD_MODEL_DIR to be set"
        )
        raise ConfigError(msg)
    # Licence gate (rule 35 / ADR-0009): runs before any model load.
    validate_manifest(GUARD_MODEL_MANIFEST, ModelFamily.GUARD)
    from hermes_voip.guard.onnx import (  # noqa: PLC0415
        OnnxInjectionGuard,
        build_onnx_classifier,
    )

    classifier = build_onnx_classifier(model_dir)
    return OnnxInjectionGuard(classify=classifier)


# ---------------------------------------------------------------------------
# Default factory maps — the production wiring (ADR-0004 config-key→factory).
# Only providers whose concrete class is implemented are wired; selecting a
# config-valid-but-not-yet-implemented token (e.g. a deferred TTS engine) raises
# the unknown-provider ValueError from dispatch, which is correct fail-fast.
# ---------------------------------------------------------------------------

DEFAULT_ASR_FACTORIES: Mapping[str, AsrFactory] = MappingProxyType(
    {
        "sherpa-onnx": _make_sherpa_onnx_asr,
        "deepgram": _make_deepgram_asr,
    }
)

DEFAULT_TTS_FACTORIES: Mapping[str, TtsFactory] = MappingProxyType(
    {
        "sherpa-kokoro": _make_sherpa_kokoro_tts,
        "elevenlabs": _make_elevenlabs_tts,
    }
)

DEFAULT_GUARD_FACTORIES: Mapping[str, GuardFactory] = MappingProxyType(
    {
        "onnx": _make_onnx_guard,
    }
)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def build_providers(
    config: MediaConfig,
    *,
    asr_factories: Mapping[str, AsrFactory] = DEFAULT_ASR_FACTORIES,
    tts_factories: Mapping[str, TtsFactory] = DEFAULT_TTS_FACTORIES,
    guard_factories: Mapping[str, GuardFactory] = DEFAULT_GUARD_FACTORIES,
) -> Providers:
    """Resolve the config-selected providers and return live instances.

    Each family is resolved by direct dispatch: the provider token from ``config``
    indexes the family's factory map, and the factory is called with ``config``.
    The factory validates required config (raising
    :class:`~hermes_voip.config.ConfigError`), runs the licence gate for self-host
    defaults (raising :class:`~hermes_voip.manifest.LicenceError`), and constructs
    the provider. An unknown token (no map entry) raises ``ValueError``.

    Args:
        config: The validated media/provider config.
        asr_factories: Token→factory map for the ASR family. Defaults to the
            production wiring (:data:`DEFAULT_ASR_FACTORIES`); inject an alternative
            (e.g. fakes in tests) through this seam, never by mutating globals.
        tts_factories: Token→factory map for the TTS family.
        guard_factories: Token→factory map for the guard family.

    Returns:
        A frozen :class:`Providers` dataclass with the three live provider
        instances.

    Raises:
        ValueError: If a selected provider token has no factory in its map.
        ConfigError: If a self-host provider requires a model dir that is ``None``.
        LicenceError: If a self-host default model's pinned manifest fails its
            family licence gate.
    """
    asr = _dispatch("asr", asr_factories, config.stt_provider, config)
    tts = _resolve_tts(tts_factories, config)
    guard = _dispatch("guard", guard_factories, config.injection_guard, config)
    return Providers(asr=asr, tts=tts, guard=guard)


def _resolve_tts(
    tts_factories: Mapping[str, TtsFactory], config: MediaConfig
) -> StreamingTTS:
    """Build the primary TTS, wrapping it in failover when a fallback is configured.

    The primary is dispatched as usual. When ``config.tts_fallback`` is set
    (ADR-0025), the primary is wrapped in a
    :class:`~hermes_voip.tts.failover.FailoverTTS` whose ``fallback_factory`` builds
    the fallback provider via the SAME factory map — but **lazily** (only on the
    first failover), so the fallback's model is not loaded on the happy path. A
    primary synthesis failure then recovers by synthesising via the fallback so the
    call still gets audio instead of dropping silent.
    """
    primary = _dispatch("tts", tts_factories, config.tts_provider, config)
    fallback_token = config.tts_fallback
    if fallback_token is None:
        return primary
    from hermes_voip.tts.failover import FailoverTTS  # noqa: PLC0415 - lazy, no ml load

    def _build_fallback() -> StreamingTTS:
        # Built only on the first failover (and cached by FailoverTTS): the fallback
        # provider's licence gate / model load runs lazily, not on the happy path.
        return _dispatch("tts", tts_factories, fallback_token, config)

    return FailoverTTS(primary=primary, fallback_factory=_build_fallback)


def _dispatch[T](
    family: str,
    factories: Mapping[str, Callable[[MediaConfig], T]],
    token: str,
    config: MediaConfig,
) -> T:
    """Look up ``token`` in ``factories`` and invoke it with ``config``.

    Args:
        family: The family label for the error message (``"asr"`` etc.).
        factories: The token→factory map for this family.
        token: The config-selected provider token.
        config: The config to hand the resolved factory.

    Returns:
        The provider the factory builds.

    Raises:
        ValueError: If ``token`` has no entry in ``factories`` (unknown provider).
    """
    try:
        factory = factories[token]
    except KeyError as exc:
        msg = f"unknown {family} provider: {token!r}"
        raise ValueError(msg) from exc
    return factory(config)
