"""Tests for hermes_voip.providers.build — config-to-provider wiring (W8).

All tests are model-free (no ml weights, no network): concrete provider types
are verified without triggering their constructors' ml-load paths by injecting
fake factories into the module-level registries before each test.

TDD (rule 18): this test file is committed red before the implementation lands.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import fields

import pytest

from hermes_voip.config import ConfigError, MediaConfig
from hermes_voip.manifest import (
    LicenceError,
    ModelFamily,
    ModelFile,
    ModelManifest,
    validate_manifest,
)
from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import (  # type: ignore[import]  # module not yet present — RED
    Providers,
    _asr_registry,
    _guard_registry,
    _tts_registry,
    build_providers,
)
from hermes_voip.providers.guard import GuardResult, GuardVerdict, InjectionGuard
from hermes_voip.providers.tts import StreamingTTS, TtsStream

# ---------------------------------------------------------------------------
# Minimal fakes satisfying ADR-0004 Protocol seams without any ml load.
# ---------------------------------------------------------------------------


async def _no_transcripts() -> AsyncIterator[Transcript]:
    return
    yield  # pragma: no cover — makes this an async generator


async def _no_frames() -> AsyncIterator[PcmFrame]:
    return
    yield  # pragma: no cover


class _FakeASR:
    @property
    def input_sample_rate(self) -> int:
        return 16_000

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        return _no_transcripts()


class _FakeTtsStream:
    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        raise StopAsyncIteration

    async def flush(self) -> None: ...

    async def cancel(self) -> None: ...


class _FakeTTS:
    @property
    def output_sample_rate(self) -> int:
        return 24_000

    def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
        return _FakeTtsStream()


class _FakeGuard:
    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            normalized_text=text,
            reasons=(),
            degraded=False,
            score=0.0,
        )


# ---------------------------------------------------------------------------
# Helper: inject fake factories and restore originals as a context manager.
# ---------------------------------------------------------------------------


class _RegistryPatch:
    """Temporarily override specific factory entries in the build registries."""

    def __init__(
        self,
        asr_overrides: dict[str, object] | None = None,
        tts_overrides: dict[str, object] | None = None,
        guard_overrides: dict[str, object] | None = None,
    ) -> None:
        self._asr_overrides = asr_overrides or {}
        self._tts_overrides = tts_overrides or {}
        self._guard_overrides = guard_overrides or {}
        self._saved_asr: dict[str, object] = {}
        self._saved_tts: dict[str, object] = {}
        self._saved_guard: dict[str, object] = {}

    def __enter__(self) -> _RegistryPatch:
        for key, factory in self._asr_overrides.items():
            self._saved_asr[key] = _asr_registry._factories.get(key)
            _asr_registry._factories[key] = factory  # type: ignore[assignment]
        for key, factory in self._tts_overrides.items():
            self._saved_tts[key] = _tts_registry._factories.get(key)
            _tts_registry._factories[key] = factory  # type: ignore[assignment]
        for key, factory in self._guard_overrides.items():
            self._saved_guard[key] = _guard_registry._factories.get(key)
            _guard_registry._factories[key] = factory  # type: ignore[assignment]
        return self

    def __exit__(self, *_: object) -> None:
        for key, orig in self._saved_asr.items():
            if orig is None:
                _asr_registry._factories.pop(key, None)
            else:
                _asr_registry._factories[key] = orig  # type: ignore[assignment]
        for key, orig in self._saved_tts.items():
            if orig is None:
                _tts_registry._factories.pop(key, None)
            else:
                _tts_registry._factories[key] = orig  # type: ignore[assignment]
        for key, orig in self._saved_guard.items():
            if orig is None:
                _guard_registry._factories.pop(key, None)
            else:
                _guard_registry._factories[key] = orig  # type: ignore[assignment]


def _all_fake_patch() -> _RegistryPatch:
    """Patch all three registries to return fresh fakes."""
    return _RegistryPatch(
        asr_overrides={"sherpa-onnx": _FakeASR},
        tts_overrides={"sherpa-kokoro": _FakeTTS},
        guard_overrides={"onnx": _FakeGuard},
    )


# ---------------------------------------------------------------------------
# Shared config builder.
# ---------------------------------------------------------------------------


def _media_config(**overrides: object) -> MediaConfig:
    """Build a valid default MediaConfig, applying keyword overrides."""
    defaults: dict[str, object] = {
        "stt_provider": "sherpa-onnx",
        "stt_model_dir": "/models/stt",
        "tts_provider": "sherpa-kokoro",
        "tts_model": "/models/tts",
        "tts_voice": None,
        "elevenlabs_api_key": None,
        "deepgram_api_key": None,
        "cartesia_api_key": None,
        "vad_threshold": 0.5,
        "endpoint_silence_ms": 500,
        "duplex_mode": "half",
        "injection_guard": "onnx",
        "injection_guard_model_dir": "/models/guard",
        "dtmf_mode": "auto",
        "dtmf_interdigit_ms": None,
        "dtmf_inband_enabled": True,
    }
    defaults.update(overrides)
    return MediaConfig(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# (a) Default MediaConfig → Providers with the expected concrete types.
# ---------------------------------------------------------------------------


def test_build_providers_returns_providers_dataclass() -> None:
    """build_providers returns a Providers dataclass with asr/tts/guard fields."""
    with _all_fake_patch():
        result = build_providers(_media_config())
    assert isinstance(result, Providers)
    field_names = {f.name for f in fields(result)}
    assert {"asr", "tts", "guard"} <= field_names


def test_build_providers_wires_correct_instances() -> None:
    """build_providers resolves the selected token to the registered factory."""
    fake_asr = _FakeASR()
    fake_tts = _FakeTTS()
    fake_guard = _FakeGuard()
    with _RegistryPatch(
        asr_overrides={"sherpa-onnx": lambda: fake_asr},
        tts_overrides={"sherpa-kokoro": lambda: fake_tts},
        guard_overrides={"onnx": lambda: fake_guard},
    ):
        result = build_providers(_media_config())
    assert result.asr is fake_asr
    assert result.tts is fake_tts
    assert result.guard is fake_guard


def test_providers_fields_satisfy_protocols_statically() -> None:
    """Providers.asr/tts/guard are typed as the ADR-0004 Protocol types."""
    fake_asr = _FakeASR()
    fake_tts = _FakeTTS()
    fake_guard = _FakeGuard()
    with _RegistryPatch(
        asr_overrides={"sherpa-onnx": lambda: fake_asr},
        tts_overrides={"sherpa-kokoro": lambda: fake_tts},
        guard_overrides={"onnx": lambda: fake_guard},
    ):
        result = build_providers(_media_config())
    # Static: mypy verifies these assignments at analysis time.
    asr: StreamingASR = result.asr
    tts: StreamingTTS = result.tts
    guard: InjectionGuard = result.guard
    assert asr.input_sample_rate == 16_000
    assert tts.output_sample_rate == 24_000
    assert isinstance(guard, InjectionGuard)


# ---------------------------------------------------------------------------
# (b) Unknown token → ValueError propagated from the registry.
# ---------------------------------------------------------------------------


def test_build_providers_unknown_stt_raises_valueerror() -> None:
    """Unknown stt_provider raises ValueError naming the bad token."""
    # Remove 'sherpa-onnx' from the ASR registry so the registry raises.
    saved = _asr_registry._factories.pop("sherpa-onnx", None)
    try:
        with pytest.raises(ValueError, match="sherpa-onnx"):
            build_providers(_media_config())
    finally:
        if saved is not None:
            _asr_registry._factories["sherpa-onnx"] = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# (c) Self-host token with model_dir=None → ConfigError naming the env key.
# ---------------------------------------------------------------------------


def test_build_providers_sherpa_onnx_no_model_dir_raises_config_error() -> None:
    """sherpa-onnx STT without model_dir raises ConfigError naming the env key."""
    with pytest.raises(ConfigError, match="HERMES_VOIP_STT_MODEL_DIR"):
        build_providers(_media_config(stt_model_dir=None))


def test_build_providers_sherpa_kokoro_no_model_dir_raises_config_error() -> None:
    """sherpa-kokoro TTS without tts_model raises ConfigError naming the env key."""
    with (
        _RegistryPatch(asr_overrides={"sherpa-onnx": _FakeASR}),
        pytest.raises(ConfigError, match="HERMES_VOIP_TTS_MODEL"),
    ):
        build_providers(_media_config(tts_model=None))


def test_build_providers_onnx_guard_no_model_dir_raises_config_error() -> None:
    """ONNX guard without injection_guard_model_dir raises ConfigError."""
    patch = _RegistryPatch(
        asr_overrides={"sherpa-onnx": _FakeASR},
        tts_overrides={"sherpa-kokoro": _FakeTTS},
    )
    with (
        patch,
        pytest.raises(ConfigError, match="HERMES_VOIP_INJECTION_GUARD_MODEL_DIR"),
    ):
        build_providers(_media_config(injection_guard_model_dir=None))


# ---------------------------------------------------------------------------
# (d) Disallowed-SPDX manifest → LicenceError surfaces.
# ---------------------------------------------------------------------------


def test_validate_manifest_bad_spdx_raises_licence_error() -> None:
    """A manifest with a disallowed SPDX licence raises LicenceError (guard family).

    This exercises the licence gate that build_providers calls for ONNX guard
    providers that expose a manifest.
    """
    bad_manifest = ModelManifest(
        repo="example/model",
        revision="a" * 40,
        files=(
            ModelFile(
                name="model.onnx",
                sha256="b" * 64,
                spdx="CC-BY-NC-4.0",  # non-commercial — not in the allow-list
            ),
        ),
    )
    with pytest.raises(LicenceError, match=re.escape("CC-BY-NC-4.0")):
        validate_manifest(bad_manifest, ModelFamily.GUARD)


def test_build_providers_licence_gate_blocks_bad_guard_manifest() -> None:
    """build_providers rejects a guard provider whose manifest fails the licence gate.

    The factory injects the licence check before returning; LicenceError propagates.
    """
    bad_manifest = ModelManifest(
        repo="example/bad-model",
        revision="c" * 40,
        files=(
            ModelFile(
                name="model.onnx",
                sha256="d" * 64,
                spdx="GPL-3.0",  # rejected by the guard family allow-list
            ),
        ),
    )

    def _bad_guard_factory() -> _FakeGuard:
        validate_manifest(bad_manifest, ModelFamily.GUARD)
        return _FakeGuard()  # unreachable after the licence gate raises

    patch = _RegistryPatch(
        asr_overrides={"sherpa-onnx": _FakeASR},
        tts_overrides={"sherpa-kokoro": _FakeTTS},
        guard_overrides={"onnx": _bad_guard_factory},
    )
    with patch, pytest.raises(LicenceError, match=re.escape("GPL-3.0")):
        build_providers(_media_config())


# ---------------------------------------------------------------------------
# (e) build_providers is safe to call twice (idempotent registration).
# ---------------------------------------------------------------------------


def test_build_providers_idempotent_registration() -> None:
    """build_providers can be called multiple times without errors.

    _register_defaults() guards against double-registration; calling build_providers
    twice must not raise ValueError from a duplicate register attempt.
    """
    with _all_fake_patch():
        result1 = build_providers(_media_config())
        result2 = build_providers(_media_config())
    assert isinstance(result1, Providers)
    assert isinstance(result2, Providers)
