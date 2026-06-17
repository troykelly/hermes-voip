"""Tests for hermes_voip.providers.build — config-to-provider wiring (W8).

All tests are model-free (no ml weights, no network): concrete provider types
are verified without triggering their constructors' ml-load paths by passing fake
factory maps through the explicit ``*_factories`` dependency seam of
:func:`build_providers` (NOT by mutating any global state — the redesign after the
codex review dropped the mutate/restore registry pattern for direct dispatch).

TDD (rule 18): the new DI-seam tests are committed red before the redesign lands.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import fields

import pytest

import hermes_voip.providers.build as build_mod
from hermes_voip.config import ConfigError, MediaConfig
from hermes_voip.manifest import (
    STT_MODEL_MANIFEST,
    TTS_MODEL_MANIFEST,
    LicenceError,
    ModelFamily,
    ModelFile,
    ModelManifest,
    validate_manifest,
)
from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import (
    AsrFactory,
    GuardFactory,
    Providers,
    TtsFactory,
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

    async def aclose(self) -> None: ...


class _FakeTTS:
    @property
    def output_sample_rate(self) -> int:
        return 24_000

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
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
# Fake factory maps — passed through the build_providers DI seam.
# Each factory is Callable[[MediaConfig], <Provider>], mirroring the production
# signature so tests exercise the real dispatch path with no ml load.
# ---------------------------------------------------------------------------


def _fake_asr_factories() -> Mapping[str, AsrFactory]:
    return {"sherpa-onnx": lambda _config: _FakeASR()}


def _fake_tts_factories() -> Mapping[str, TtsFactory]:
    return {"sherpa-kokoro": lambda _config: _FakeTTS()}


def _fake_guard_factories() -> Mapping[str, GuardFactory]:
    return {"onnx": lambda _config: _FakeGuard()}


def _build_with_fakes(
    config: MediaConfig,
    *,
    asr_factories: Mapping[str, AsrFactory] | None = None,
    tts_factories: Mapping[str, TtsFactory] | None = None,
    guard_factories: Mapping[str, GuardFactory] | None = None,
) -> Providers:
    """Call build_providers with all-fake factory maps unless overridden."""
    return build_providers(
        config,
        asr_factories=asr_factories
        if asr_factories is not None
        else _fake_asr_factories(),
        tts_factories=tts_factories
        if tts_factories is not None
        else _fake_tts_factories(),
        guard_factories=guard_factories
        if guard_factories is not None
        else _fake_guard_factories(),
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
        "greeting": "",
        "rtp_symmetric": True,
        "barge_in_mode": "gated",
        "barge_in_min_speech_ms": 400,
        "barge_in_tail_ms": 250,
        "injection_guard": "onnx",
        "injection_guard_model_dir": "/models/guard",
        "dtmf_mode": "auto",
        "dtmf_interdigit_ms": None,
        "dtmf_inband_enabled": True,
        "tone_secs": 0.0,
    }
    defaults.update(overrides)
    return MediaConfig(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# (a) Default MediaConfig → Providers with the expected concrete types.
# ---------------------------------------------------------------------------


def test_build_providers_returns_providers_dataclass() -> None:
    """build_providers returns a Providers dataclass with asr/tts/guard fields."""
    result = _build_with_fakes(_media_config())
    assert isinstance(result, Providers)
    field_names = {f.name for f in fields(result)}
    assert {"asr", "tts", "guard"} <= field_names


def test_build_providers_wires_correct_instances() -> None:
    """build_providers resolves the selected token to the injected factory."""
    fake_asr = _FakeASR()
    fake_tts = _FakeTTS()
    fake_guard = _FakeGuard()
    result = _build_with_fakes(
        _media_config(),
        asr_factories={"sherpa-onnx": lambda _c: fake_asr},
        tts_factories={"sherpa-kokoro": lambda _c: fake_tts},
        guard_factories={"onnx": lambda _c: fake_guard},
    )
    assert result.asr is fake_asr
    assert result.tts is fake_tts
    assert result.guard is fake_guard


def test_build_providers_passes_config_to_factory() -> None:
    """The selected factory receives the same MediaConfig instance (DI contract)."""
    seen: list[MediaConfig] = []

    def _capturing_asr(config: MediaConfig) -> StreamingASR:
        seen.append(config)
        return _FakeASR()

    cfg = _media_config()
    _build_with_fakes(cfg, asr_factories={"sherpa-onnx": _capturing_asr})
    assert seen == [cfg]


def test_providers_fields_satisfy_protocols_statically() -> None:
    """Providers.asr/tts/guard are typed as the ADR-0004 Protocol types."""
    result = _build_with_fakes(_media_config())
    # Static: mypy verifies these assignments at analysis time.
    asr: StreamingASR = result.asr
    tts: StreamingTTS = result.tts
    guard: InjectionGuard = result.guard
    assert asr.input_sample_rate == 16_000
    assert tts.output_sample_rate == 24_000
    assert isinstance(guard, InjectionGuard)


# ---------------------------------------------------------------------------
# (b) Unknown token → ValueError from direct dispatch.
# ---------------------------------------------------------------------------


def test_build_providers_unknown_stt_raises_valueerror() -> None:
    """An stt_provider with no factory entry raises ValueError naming the token."""
    # An empty ASR factory map has no entry for 'sherpa-onnx'.
    with pytest.raises(ValueError, match="sherpa-onnx"):
        _build_with_fakes(_media_config(), asr_factories={})


def test_build_providers_unknown_tts_raises_valueerror() -> None:
    """A tts_provider with no factory entry raises ValueError naming the token."""
    with pytest.raises(ValueError, match="sherpa-kokoro"):
        _build_with_fakes(_media_config(), tts_factories={})


def test_build_providers_unknown_guard_raises_valueerror() -> None:
    """An injection_guard with no factory entry raises ValueError naming the token."""
    with pytest.raises(ValueError, match="onnx"):
        _build_with_fakes(_media_config(), guard_factories={})


# ---------------------------------------------------------------------------
# (c) Self-host token with model_dir=None → ConfigError naming the env key.
# These use the REAL default factory maps (no fake override for the family under
# test) so the production model-dir validation path is exercised.
# ---------------------------------------------------------------------------


def test_build_providers_sherpa_onnx_no_model_dir_raises_config_error() -> None:
    """sherpa-onnx STT without model_dir raises ConfigError naming the env key."""
    # Use the real ASR factory map (default) so the production guard fires; fake
    # the other two families so only the STT path matters.
    with pytest.raises(ConfigError, match="HERMES_VOIP_STT_MODEL_DIR"):
        build_providers(
            _media_config(stt_model_dir=None),
            tts_factories=_fake_tts_factories(),
            guard_factories=_fake_guard_factories(),
        )


def test_build_providers_sherpa_kokoro_no_model_dir_raises_config_error() -> None:
    """sherpa-kokoro TTS without tts_model raises ConfigError naming the env key."""
    with pytest.raises(ConfigError, match="HERMES_VOIP_TTS_MODEL"):
        build_providers(
            _media_config(tts_model=None),
            asr_factories=_fake_asr_factories(),
            guard_factories=_fake_guard_factories(),
        )


def test_build_providers_onnx_guard_no_model_dir_raises_config_error() -> None:
    """ONNX guard without injection_guard_model_dir raises ConfigError."""
    with pytest.raises(ConfigError, match="HERMES_VOIP_INJECTION_GUARD_MODEL_DIR"):
        build_providers(
            _media_config(injection_guard_model_dir=None),
            asr_factories=_fake_asr_factories(),
            tts_factories=_fake_tts_factories(),
        )


# ---------------------------------------------------------------------------
# (d) Disallowed-SPDX manifest → LicenceError surfaces.
# ---------------------------------------------------------------------------


def test_validate_manifest_bad_spdx_raises_licence_error() -> None:
    """A manifest with a disallowed SPDX licence raises LicenceError (guard family)."""
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
    """build_providers surfaces a LicenceError raised inside a guard factory."""
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

    def _bad_guard_factory(_config: MediaConfig) -> InjectionGuard:
        validate_manifest(bad_manifest, ModelFamily.GUARD)
        return _FakeGuard()  # unreachable after the licence gate raises

    with pytest.raises(LicenceError, match=re.escape("GPL-3.0")):
        _build_with_fakes(
            _media_config(),
            guard_factories={"onnx": _bad_guard_factory},
        )


# ---------------------------------------------------------------------------
# (d.STT / d.TTS) The default STT and TTS self-host factories run the licence
# gate before constructing. A disallowed-SPDX manifest for those families must
# surface a LicenceError too — proving the self-host weights are gated, not just
# the guard (codex finding #3).
# ---------------------------------------------------------------------------


def test_validate_manifest_stt_bad_spdx_raises_licence_error() -> None:
    """A non-Apache-2.0 STT manifest is rejected by the STT family gate."""
    bad_stt = ModelManifest(
        repo="example/stt",
        revision="e" * 40,
        files=(
            ModelFile(
                name="encoder.onnx",
                sha256="f" * 64,
                spdx="CC-BY-SA-4.0",  # the Kroko trap — share-alike, banned for STT
            ),
        ),
    )
    with pytest.raises(LicenceError, match=re.escape("CC-BY-SA-4.0")):
        validate_manifest(bad_stt, ModelFamily.STT)


def test_validate_manifest_tts_bad_spdx_raises_licence_error() -> None:
    """A non-allow-listed TTS manifest is rejected by the TTS family gate."""
    bad_tts = ModelManifest(
        repo="example/tts",
        revision="1" * 40,
        files=(
            ModelFile(
                name="model.onnx",
                sha256="2" * 64,
                spdx="CC-BY-NC-4.0",  # non-commercial — banned for TTS
            ),
        ),
    )
    with pytest.raises(LicenceError, match=re.escape("CC-BY-NC-4.0")):
        validate_manifest(bad_tts, ModelFamily.TTS)


def test_default_stt_manifest_passes_its_family_gate() -> None:
    """The shipped STT_MODEL_MANIFEST validates under the STT (Apache-2.0) gate."""
    # A clean return is the assertion; raises LicenceError on any non-allowed file.
    validate_manifest(STT_MODEL_MANIFEST, ModelFamily.STT)


def test_default_tts_manifest_passes_its_family_gate() -> None:
    """The shipped TTS_MODEL_MANIFEST validates under the TTS gate."""
    validate_manifest(TTS_MODEL_MANIFEST, ModelFamily.TTS)


def test_build_providers_stt_factory_runs_licence_gate() -> None:
    """The DEFAULT sherpa-onnx STT factory runs the licence gate before building.

    Patching the STT manifest to a disallowed SPDX must make the default factory
    raise LicenceError — i.e. the self-host STT weights are gated (codex #3). We
    monkeypatch the manifest module's STT manifest to a bad one and use the REAL
    default ASR factory map.
    """
    bad_stt = ModelManifest(
        repo="example/stt-bad",
        revision="3" * 40,
        files=(ModelFile(name="encoder.onnx", sha256="4" * 64, spdx="GPL-3.0"),),
    )
    original = build_mod.STT_MODEL_MANIFEST
    build_mod.STT_MODEL_MANIFEST = bad_stt
    try:
        with pytest.raises(LicenceError, match=re.escape("GPL-3.0")):
            build_providers(
                _media_config(),
                tts_factories=_fake_tts_factories(),
                guard_factories=_fake_guard_factories(),
            )
    finally:
        build_mod.STT_MODEL_MANIFEST = original


def test_build_providers_tts_factory_runs_licence_gate() -> None:
    """The DEFAULT sherpa-kokoro TTS factory runs the licence gate before building."""
    bad_tts = ModelManifest(
        repo="example/tts-bad",
        revision="5" * 40,
        files=(ModelFile(name="model.onnx", sha256="6" * 64, spdx="CC-BY-NC-4.0"),),
    )
    original = build_mod.TTS_MODEL_MANIFEST
    build_mod.TTS_MODEL_MANIFEST = bad_tts
    try:
        with pytest.raises(LicenceError, match=re.escape("CC-BY-NC-4.0")):
            build_providers(
                _media_config(),
                asr_factories=_fake_asr_factories(),
                guard_factories=_fake_guard_factories(),
            )
    finally:
        build_mod.TTS_MODEL_MANIFEST = original


# ---------------------------------------------------------------------------
# (e) build_providers is safe to call repeatedly and concurrently.
# ---------------------------------------------------------------------------


def test_build_providers_repeated_calls_are_independent() -> None:
    """build_providers can be called many times; direct dispatch has no shared state."""
    cfg = _media_config()
    result1 = _build_with_fakes(cfg)
    result2 = _build_with_fakes(cfg)
    assert isinstance(result1, Providers)
    assert isinstance(result2, Providers)
    # Distinct instances each call (the fake factories build fresh objects).
    assert result1.asr is not result2.asr


def test_build_providers_uses_real_default_factory_maps() -> None:
    """Calling build_providers with no factory args uses the production maps.

    The default maps must contain the known tokens; we assert resolution does NOT
    raise the unknown-token ValueError for the default config. The sherpa-onnx STT
    factory would try to load ml, so we only assert the maps recognise the tokens
    by checking the module-level default maps directly (no construction).
    """
    assert "sherpa-onnx" in build_mod.DEFAULT_ASR_FACTORIES
    assert "deepgram" in build_mod.DEFAULT_ASR_FACTORIES
    assert "sherpa-kokoro" in build_mod.DEFAULT_TTS_FACTORIES
    assert "elevenlabs" in build_mod.DEFAULT_TTS_FACTORIES
    assert "onnx" in build_mod.DEFAULT_GUARD_FACTORIES
