"""Pure-Python licence gate for pinned model artifacts (ADR-0006/0007/0009, rule 35).

These tests are the CI licence assertion the three media ADRs each promise: the
default model on every conversational seam (STT, TTS, prompt-injection guard) is
pinned by ``repo`` + ``revision`` + per-file ``sha256``, and its declared SPDX
licence is checked against a *per-family* allow-list before the pin is accepted.
The pin is verified, not trusted.

The whole gate is static metadata only — **no network, no model download, no
ONNX load**. It reasons over recorded SPDX strings, which is exactly what an
offline, reproducible CI (rule 33) can do deterministically.

The load-bearing assertions:

* every SPDX in each family's allow-list passes ``licence_ok`` for that family
  (and the strictly-Apache STT/guard families reject the extra TTS licences);
* the licence traps the ADRs name by id are rejected for the family that would
  otherwise host that model — the Kroko STT zipformer is CC-BY-SA-4.0
  (ADR-0006), and the disqualified self-host TTS set (Coqui XTTS, F5-TTS,
  Fish-Speech/OpenAudio, ChatTTS) is non-commercial / copyleft-viral (ADR-0007);
* a manifest carrying a banned licence can **never** validate for its family —
  ``validate_manifest`` raises, naming the offending file, so a trap model can
  never be committed as a default.
"""

from __future__ import annotations

import pytest

from hermes_voip.manifest import (
    GUARD_ALLOWED_SPDX,
    STT_ALLOWED_SPDX,
    TTS_ALLOWED_SPDX,
    LicenceError,
    ModelFamily,
    ModelFile,
    ModelManifest,
    licence_ok,
    validate_manifest,
)

# A syntactically-valid pinned artifact coordinate using obvious public fakes:
# a 40-hex commit revision and a 64-hex SHA-256 (rule: fakes only, no real host
# or secret — model repo ids are public, but nothing here needs to be real).
_FAKE_REVISION = "0" * 40
_FAKE_SHA256 = "a" * 64

# SPDX identifiers that disqualify a model for ANY family — the exact traps the
# ADRs call out. CC-BY-SA-4.0 is the Kroko STT zipformer (ADR-0006, share-alike
# viral). The TTS bans (ADR-0007) are non-commercial / copyleft-viral weights:
# Coqui XTTS-v2 ships the Coqui Public Model License (no standard SPDX id, so the
# SPDX custom-licence reference form), F5-TTS base is CC-BY-NC-4.0, Fish-Speech /
# OpenAudio S1 is CC-BY-NC-SA-4.0, and ChatTTS is AGPL-3.0 code + CC-BY-NC-4.0
# weights (the weight licence is what disqualifies it).
_KROKO_STT_SPDX = "CC-BY-SA-4.0"
_DISALLOWED_TTS_SPDX = (
    "LicenseRef-CPML",  # Coqui XTTS-v2 (Coqui Public Model License, non-commercial)
    "CC-BY-NC-4.0",  # F5-TTS base weights
    "CC-BY-NC-SA-4.0",  # Fish-Speech / OpenAudio S1 weights
    "AGPL-3.0",  # ChatTTS code (with CC-BY-NC weights)
)
# Every banned SPDX, across every family.
_ALL_BANNED_SPDX = (_KROKO_STT_SPDX, *_DISALLOWED_TTS_SPDX)


def _manifest(repo: str, spdx: str) -> ModelManifest:
    """A one-file manifest whose single artifact declares ``spdx``."""
    return ModelManifest(
        repo=repo,
        revision=_FAKE_REVISION,
        files=(ModelFile(name="model.onnx", sha256=_FAKE_SHA256, spdx=spdx),),
    )


# --- the per-family allow-lists are exactly what the ADRs decided -------------


def test_stt_and_guard_allow_only_apache() -> None:
    """STT (ADR-0006) and guard (ADR-0009) default to Apache-2.0 only."""
    assert frozenset({"Apache-2.0"}) == STT_ALLOWED_SPDX
    assert frozenset({"Apache-2.0"}) == GUARD_ALLOWED_SPDX


def test_tts_additionally_allows_permissive_creative_commons() -> None:
    """TTS (ADR-0007) widens the allow-list to Apache/MIT/CC0/CC-BY-4.0."""
    assert frozenset({"Apache-2.0", "MIT", "CC0-1.0", "CC-BY-4.0"}) == TTS_ALLOWED_SPDX
    # The wider TTS list is a strict superset of the strict STT/guard list.
    assert STT_ALLOWED_SPDX < TTS_ALLOWED_SPDX


# --- licence_ok: every allowed SPDX passes; everything else fails -------------


@pytest.mark.parametrize("spdx", sorted(STT_ALLOWED_SPDX))
def test_licence_ok_accepts_every_allowed_stt_spdx(spdx: str) -> None:
    assert licence_ok(ModelFamily.STT, spdx) is True


@pytest.mark.parametrize("spdx", sorted(GUARD_ALLOWED_SPDX))
def test_licence_ok_accepts_every_allowed_guard_spdx(spdx: str) -> None:
    assert licence_ok(ModelFamily.GUARD, spdx) is True


@pytest.mark.parametrize("spdx", sorted(TTS_ALLOWED_SPDX))
def test_licence_ok_accepts_every_allowed_tts_spdx(spdx: str) -> None:
    assert licence_ok(ModelFamily.TTS, spdx) is True


def test_strict_families_reject_the_extra_tts_licences() -> None:
    """MIT/CC0/CC-BY-4.0 are fine for TTS but NOT for the Apache-only families."""
    for spdx in ("MIT", "CC0-1.0", "CC-BY-4.0"):
        assert licence_ok(ModelFamily.TTS, spdx) is True
        assert licence_ok(ModelFamily.STT, spdx) is False
        assert licence_ok(ModelFamily.GUARD, spdx) is False


@pytest.mark.parametrize("family", list(ModelFamily))
@pytest.mark.parametrize("spdx", _ALL_BANNED_SPDX)
def test_licence_ok_rejects_every_banned_spdx_for_every_family(
    family: ModelFamily, spdx: str
) -> None:
    """No banned SPDX is acceptable for any family — not even widest TTS."""
    assert licence_ok(family, spdx) is False


def test_kroko_share_alike_is_rejected_for_stt() -> None:
    """The Kroko zipformer trap (CC-BY-SA-4.0) fails the STT gate (ADR-0006)."""
    assert licence_ok(ModelFamily.STT, _KROKO_STT_SPDX) is False


# --- validate_manifest: a banned-licence manifest can NEVER validate ----------


def test_validate_manifest_passes_for_allowed_default() -> None:
    """A pinned Apache-2.0 artifact validates cleanly for every family."""
    for family in ModelFamily:
        validate_manifest(_manifest("example/allowed-model", "Apache-2.0"), family)


def test_validate_manifest_rejects_kroko_for_stt() -> None:
    """A CC-BY-SA-4.0 STT manifest cannot be a default — it raises (ADR-0006)."""
    manifest = _manifest("example/kroko-streaming-zipformer", _KROKO_STT_SPDX)
    with pytest.raises(LicenceError) as excinfo:
        validate_manifest(manifest, ModelFamily.STT)
    # The error names the offending file and licence for the CI log / audit.
    assert "model.onnx" in str(excinfo.value)
    assert _KROKO_STT_SPDX in str(excinfo.value)


@pytest.mark.parametrize("spdx", _DISALLOWED_TTS_SPDX)
def test_validate_manifest_rejects_every_disqualified_tts_model(spdx: str) -> None:
    """No non-commercial / copyleft-viral TTS weight can validate (ADR-0007)."""
    manifest = _manifest("example/disqualified-tts", spdx)
    with pytest.raises(LicenceError):
        validate_manifest(manifest, ModelFamily.TTS)


@pytest.mark.parametrize("family", list(ModelFamily))
@pytest.mark.parametrize("spdx", _ALL_BANNED_SPDX)
def test_no_banned_licence_can_ever_validate_for_any_family(
    family: ModelFamily, spdx: str
) -> None:
    """The core invariant: a banned licence never validates, for any family."""
    manifest = _manifest("example/trap", spdx)
    with pytest.raises(LicenceError):
        validate_manifest(manifest, family)


def test_validate_manifest_rejects_a_mixed_manifest_on_one_bad_file() -> None:
    """One disallowed file among allowed ones still fails the whole manifest."""
    manifest = ModelManifest(
        repo="example/mostly-clean",
        revision=_FAKE_REVISION,
        files=(
            ModelFile(name="encoder.onnx", sha256="b" * 64, spdx="Apache-2.0"),
            ModelFile(name="decoder.onnx", sha256="c" * 64, spdx="CC-BY-SA-4.0"),
            ModelFile(name="joiner.onnx", sha256="d" * 64, spdx="Apache-2.0"),
        ),
    )
    with pytest.raises(LicenceError) as excinfo:
        validate_manifest(manifest, ModelFamily.STT)
    assert "decoder.onnx" in str(excinfo.value)


# --- the dataclasses are frozen, slotted, and validate their own shape --------


def test_model_file_and_manifest_are_frozen() -> None:
    """The manifest is immutable: a pinned artifact cannot be mutated in place."""
    f = ModelFile(name="model.onnx", sha256=_FAKE_SHA256, spdx="Apache-2.0")
    m = _manifest("example/allowed-model", "Apache-2.0")
    with pytest.raises(AttributeError):
        f.spdx = "MIT"  # type: ignore[misc]  # asserting frozen rejects assignment
    with pytest.raises(AttributeError):
        m.revision = "1" * 40  # type: ignore[misc]  # asserting frozen


def test_model_file_rejects_a_malformed_sha256() -> None:
    """A non-64-hex digest is not a valid pin and is rejected at construction."""
    with pytest.raises(ValueError, match="sha256"):
        ModelFile(name="model.onnx", sha256="deadbeef", spdx="Apache-2.0")


def test_model_manifest_rejects_a_malformed_revision() -> None:
    """A revision that is not a 40-hex commit SHA is not a real pin."""
    with pytest.raises(ValueError, match="revision"):
        ModelManifest(
            repo="example/model",
            revision="main",
            files=(ModelFile(name="m.onnx", sha256=_FAKE_SHA256, spdx="Apache-2.0"),),
        )


def test_model_manifest_rejects_empty_files() -> None:
    """A manifest with no pinned files gates nothing — reject it."""
    with pytest.raises(ValueError, match="file"):
        ModelManifest(repo="example/model", revision=_FAKE_REVISION, files=())
