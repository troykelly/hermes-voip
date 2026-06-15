"""Config → concrete provider instance wiring (W8, ADR-0004).

This module owns the three module-level ``ProviderRegistry`` singletons for the
ASR, TTS, and injection-guard families and exposes :func:`build_providers` —
the single call-site that resolves the active token from a
:class:`~hermes_voip.config.MediaConfig` and returns the live :class:`Providers`
tuple.

Design invariants
-----------------
- **Registry is the source of truth**: :func:`build_providers` always resolves
  providers via the module-level registries. Tests inject fakes by overwriting
  registry entries; :func:`build_providers` installs config-aware production
  factories for the selected token, calls ``make()``, then restores the previous
  entry. An entry removed from the registry (simulating an unknown token) causes
  the registry to raise ``ValueError`` as designed.
- **Fail-fast on misconfiguration**: a self-host provider whose required model dir
  is ``None`` raises :class:`~hermes_voip.config.ConfigError` naming the missing
  env var *before* any registry lookup.
- **Licence gate**: the in-process ONNX guard runs
  :func:`~hermes_voip.manifest.validate_manifest` on
  :data:`~hermes_voip.manifest.GUARD_MODEL_MANIFEST` inside its factory;
  :class:`~hermes_voip.manifest.LicenceError` propagates uncaught (rule 37).
- **ML-free import**: importing this module never loads sherpa-onnx, onnxruntime,
  tokenizers, or websockets. Each factory closes over config fields and defers the
  heavy import to the concrete class constructor.
- **Idempotent calls**: calling :func:`build_providers` multiple times is safe.
  Each call installs a fresh config-aware factory, calls ``make()``, then restores
  the previous entry (or removes the entry if none existed before).

Env keys named in :class:`~hermes_voip.config.ConfigError` messages
---------------------------------------------------------------------
- ``HERMES_VOIP_STT_MODEL_DIR`` — required for ``sherpa-onnx``
- ``HERMES_VOIP_TTS_MODEL`` — required for ``sherpa-kokoro``
- ``HERMES_VOIP_INJECTION_GUARD_MODEL_DIR`` — required for ``onnx`` guard
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from hermes_voip.config import ConfigError, MediaConfig
from hermes_voip.manifest import (
    GUARD_MODEL_MANIFEST,
    ModelFamily,
    validate_manifest,
)
from hermes_voip.providers.asr import StreamingASR
from hermes_voip.providers.guard import InjectionGuard
from hermes_voip.providers.registry import ProviderRegistry
from hermes_voip.providers.tts import StreamingTTS

__all__ = [
    "Providers",
    "_asr_registry",
    "_guard_registry",
    "_tts_registry",
    "build_providers",
]

# ---------------------------------------------------------------------------
# Module-level registry singletons — one per provider family (ADR-0004).
# ---------------------------------------------------------------------------

_asr_registry: ProviderRegistry[StreamingASR] = ProviderRegistry("asr")
_tts_registry: ProviderRegistry[StreamingTTS] = ProviderRegistry("tts")
_guard_registry: ProviderRegistry[InjectionGuard] = ProviderRegistry("guard")


def _sentinel_factory_asr(name: str) -> Callable[[], StreamingASR]:
    """Return a placeholder ASR factory that raises; replaced at build time."""

    def _unconfigured() -> StreamingASR:
        msg = (
            f"asr provider {name!r} is registered but has no config-aware factory yet; "
            "call build_providers(config) to wire it"
        )
        raise RuntimeError(msg)

    return _unconfigured


def _sentinel_factory_tts(name: str) -> Callable[[], StreamingTTS]:
    """Return a placeholder TTS factory that raises; replaced at build time."""

    def _unconfigured() -> StreamingTTS:
        msg = (
            f"tts provider {name!r} is registered but has no config-aware factory yet; "
            "call build_providers(config) to wire it"
        )
        raise RuntimeError(msg)

    return _unconfigured


def _sentinel_factory_guard(name: str) -> Callable[[], InjectionGuard]:
    """Return a placeholder guard factory that raises; replaced at build time."""

    def _unconfigured() -> InjectionGuard:
        msg = (
            f"guard provider {name!r} is registered but has no"
            " config-aware factory yet; call build_providers(config) to wire it"
        )
        raise RuntimeError(msg)

    return _unconfigured


def _register_defaults() -> None:
    """Pre-populate the registries with sentinel factories for every known token.

    Called once at module import. Each sentinel raises ``RuntimeError`` if
    ``make()`` is called before ``build_providers`` replaces it with a
    config-aware factory. This ensures:

    - A token that IS in the registry (pre-populated) is recognised as known;
      :func:`_resolve` replaces it with a config-aware factory at call time.
    - A token that is NOT in the registry (e.g. removed by a test to simulate an
      unknown provider) causes the registry to raise ``ValueError`` on ``make()``.
    """
    for name in ("sherpa-onnx", "deepgram"):
        if name not in _asr_registry._factories:
            _asr_registry._factories[name] = _sentinel_factory_asr(name)
    for name in (
        "sherpa-kokoro",
        "piper",
        "kittentts",
        "kyutai",
        "cartesia",
        "aura2",
        "elevenlabs",
    ):
        if name not in _tts_registry._factories:
            _tts_registry._factories[name] = _sentinel_factory_tts(name)
    for name in ("onnx", "sidecar"):
        if name not in _guard_registry._factories:
            _guard_registry._factories[name] = _sentinel_factory_guard(name)


_register_defaults()


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
# Public entry point.
# ---------------------------------------------------------------------------


def build_providers(config: MediaConfig) -> Providers:
    """Resolve the config-selected providers and return live instances.

    For each provider family, :func:`build_providers`:

    1. Validates that required self-host model dirs are set (raises
       :class:`~hermes_voip.config.ConfigError` if not).
    2. Builds a config-aware factory closure.
    3. Installs it in the module-level registry (overwriting any previous entry,
       including test-injected fakes — except that if the entry was put there by
       a test, the test's ``_RegistryPatch`` context manager will restore it; our
       own restoration happens in the finally block).
    4. Calls ``registry.make(token)`` — which raises ``ValueError`` if the token
       is no longer in the registry (e.g. removed by a test to simulate an
       unknown token).
    5. Restores the previous registry entry (or removes the entry if none
       existed before this call).

    Args:
        config: The validated media/provider config.

    Returns:
        A frozen :class:`Providers` dataclass with the three live provider
        instances.

    Raises:
        ValueError: If the selected token is not registered in the registry at
            call time (the registry has no entry for it — removed by a test or
            genuinely absent).
        ConfigError: If a self-host provider requires a model dir that is
            ``None`` in ``config``.
        LicenceError: If the ONNX guard's pinned model manifest fails the
            per-family licence gate.
    """
    asr = _resolve(_asr_registry, config.stt_provider, _asr_factory(config))
    tts = _resolve(_tts_registry, config.tts_provider, _tts_factory(config))
    guard = _resolve(_guard_registry, config.injection_guard, _guard_factory(config))
    return Providers(asr=asr, tts=tts, guard=guard)


# ---------------------------------------------------------------------------
# Per-family factory builders — called by build_providers at resolve time.
# These raise ConfigError immediately if required config is missing, BEFORE
# any registry lookup.
# ---------------------------------------------------------------------------


def _asr_factory(config: MediaConfig) -> Callable[[], StreamingASR]:
    """Build a config-aware ASR factory for the selected provider name."""
    provider = config.stt_provider
    if provider == "sherpa-onnx":
        model_dir = config.stt_model_dir
        if model_dir is None:
            msg = (
                "stt_provider 'sherpa-onnx' requires "
                "HERMES_VOIP_STT_MODEL_DIR to be set"
            )
            raise ConfigError(msg)

        def _sherpa_onnx_asr() -> StreamingASR:
            from hermes_voip.stt.sherpa_onnx import (  # noqa: PLC0415
                SherpaOnnxASR,
            )

            return SherpaOnnxASR(model_dir)

        return _sherpa_onnx_asr

    if provider == "deepgram":
        api_key = config.deepgram_api_key or ""

        def _deepgram_asr() -> StreamingASR:
            from hermes_voip.stt.deepgram import DeepgramASR  # noqa: PLC0415

            return DeepgramASR(api_key)

        return _deepgram_asr

    # Unknown provider: return a factory that raises. The _resolve helper will
    # install this into the registry and call make(); the factory raises here.
    def _unknown_asr() -> StreamingASR:
        msg = f"unknown asr provider: {provider!r}"
        raise ValueError(msg)

    return _unknown_asr


def _tts_factory(config: MediaConfig) -> Callable[[], StreamingTTS]:
    """Build a config-aware TTS factory for the selected provider name."""
    provider = config.tts_provider
    if provider == "sherpa-kokoro":
        model_dir = config.tts_model
        if model_dir is None:
            msg = (
                "tts_provider 'sherpa-kokoro' requires HERMES_VOIP_TTS_MODEL to be set"
            )
            raise ConfigError(msg)
        voice = config.tts_voice or ""

        def _sherpa_kokoro_tts() -> StreamingTTS:
            from hermes_voip.tts.sherpa_kokoro import (  # noqa: PLC0415
                SherpaKokoroTTS,
            )

            return SherpaKokoroTTS(model_dir=model_dir, voice=voice)

        return _sherpa_kokoro_tts

    if provider == "elevenlabs":
        api_key = config.elevenlabs_api_key or ""
        voice = config.tts_voice or ""

        def _elevenlabs_tts() -> StreamingTTS:
            from hermes_voip.tts.elevenlabs import ElevenLabsTTS  # noqa: PLC0415

            return ElevenLabsTTS(api_key=api_key, voice=voice)

        return _elevenlabs_tts

    def _unknown_tts() -> StreamingTTS:
        msg = f"unknown tts provider: {provider!r}"
        raise ValueError(msg)

    return _unknown_tts


def _guard_factory(config: MediaConfig) -> Callable[[], InjectionGuard]:
    """Build a config-aware guard factory for the selected provider name."""
    provider = config.injection_guard
    if provider == "onnx":
        model_dir = config.injection_guard_model_dir
        if model_dir is None:
            msg = (
                "injection_guard 'onnx' requires "
                "HERMES_VOIP_INJECTION_GUARD_MODEL_DIR to be set"
            )
            raise ConfigError(msg)

        def _onnx_guard() -> InjectionGuard:
            # Licence gate: runs before any model load (rule 35 / ADR-0009).
            validate_manifest(GUARD_MODEL_MANIFEST, ModelFamily.GUARD)
            from hermes_voip.guard.onnx import (  # noqa: PLC0415
                OnnxInjectionGuard,
                build_onnx_classifier,
            )

            classifier = build_onnx_classifier(model_dir)
            return OnnxInjectionGuard(classify=classifier)

        return _onnx_guard

    def _unknown_guard() -> InjectionGuard:
        msg = f"unknown guard provider: {provider!r}"
        raise ValueError(msg)

    return _unknown_guard


# ---------------------------------------------------------------------------
# Registry resolution helper.
# ---------------------------------------------------------------------------


def _resolve[T](
    registry: ProviderRegistry[T],
    token: str,
    production_factory: Callable[[], T],
) -> T:
    """Replace the sentinel factory for ``token`` with ``production_factory``.

    ``_register_defaults()`` pre-populates the registry with sentinel factories
    for every known token. This function swaps the sentinel for a config-aware
    production factory, calls ``make()``, then restores the sentinel.

    If the entry has been replaced by a test-injected fake (the factory is NOT
    a sentinel — the test wrote a non-``RuntimeError``-raising callable), we do
    NOT overwrite it: the fake wins and we call ``make()`` directly.

    If the token has been REMOVED from the registry (e.g. a test popped it to
    simulate an unknown token), there is no entry to swap; we call ``make()``
    directly which raises ``ValueError`` (the registry's "unknown token" error).

    Args:
        registry: The family registry to resolve against.
        token: The provider name to look up.
        production_factory: The config-aware factory to install in place of the
            sentinel (ignored if a test fake is already in place).

    Returns:
        The provider instance returned by the winning factory.

    Raises:
        ValueError: If ``token`` is not in the registry at call time.
    """
    previous = registry._factories.get(token)
    if previous is None:
        # Token is absent (removed by a test); call make() to get the ValueError.
        return registry.make(token)

    # Swap in the production factory (sentinel → real; test fake → stays as-is
    # because we check the sentinel identity). We use a simple heuristic: if the
    # current entry's ``__qualname__`` contains ``_unconfigured``, it is our
    # sentinel and we replace it. Otherwise it is a test fake and we leave it.
    is_sentinel = getattr(previous, "__qualname__", "").endswith("_unconfigured")
    if is_sentinel:
        registry._factories[token] = production_factory
    try:
        return registry.make(token)
    finally:
        if is_sentinel:
            # Restore the sentinel so the next call can swap it again.
            registry._factories[token] = previous
