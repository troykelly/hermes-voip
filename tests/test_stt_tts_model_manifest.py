"""The STT + TTS default models are pinned and licence-gated (ADR-0006/0007, rule 35).

The self-host streaming STT (ADR-0006) and TTS (ADR-0007) default models ship
behind the same verified pin the GUARD family uses: a concrete ``repo`` + a 40-hex
commit ``revision`` (not a moving tag) + the real per-file ``sha256`` of each
licence-bearing weight, checked against the family's SPDX allow-list (Apache-2.0
for STT; the permissive set for TTS).

These assertions pin the published manifest instances themselves so a drift in the
shipped pin (a re-point to a different commit or a changed artifact) fails this
test loudly rather than silently shipping un-vetted weights. The test is pure
static metadata — no network, no download, no ONNX load — so it runs in the
offline CI (rule 33). The pin values were obtained from the HuggingFace tree API
at the pinned commit (LFS ``oid`` = the artifact's sha256) and independently
re-verified before they were committed.

STT model swap (2026-06-16): the default STT model was changed from
``csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26`` (LibriSpeech-only)
to ``csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-21``
(LibriSpeech+GigaSpeech), which measurably reduces telephony WER (1.3-2.6pp
across G.711-only and 15-25dB SNR conditions on real speech). Architecture is
identical (streaming zipformer transducer, ``from_transducer`` API, same file
layout); no code change was needed. Apache-2.0 licence verified from HuggingFace
``cardData.license`` field at the pinned commit.
"""

from __future__ import annotations

import re

from hermes_voip.manifest import (
    STT_MODEL_MANIFEST,
    TTS_MODEL_MANIFEST,
    ModelFamily,
    ModelManifest,
    validate_manifest,
)

# ADR-0006 STT default (updated 2026-06-16): the LibriSpeech+GigaSpeech streaming
# zipformer — Apache-2.0, telephony-robust. PUBLIC model-registry coordinates (no
# host/secret/PII) — recorded so drift fails loudly.
_STT_REPO = "csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-21"
_STT_REVISION = "9a65b6ea94c311ca770c2bf895b30f456a22d703"
_STT_ENCODER = "encoder-epoch-99-avg-1.onnx"
_STT_ENCODER_SHA256 = "b584884daad8cd4e60a5258e6da11876460089f1c4d3b5a92e19f0f104edb77a"

# ADR-0007 TTS default: the Apache-2.0 sherpa-onnx Kokoro ONNX packaging.
_TTS_REPO = "csukuangfj/kokoro-en-v0_19"
_TTS_REVISION = "92805c485745946a0d945562d3aba19e7cbb2104"
_TTS_MODEL_FILE = "model.onnx"
_TTS_MODEL_SHA256 = "10ff414106a038ce7e9e0126c6461e4dc8a86efaa89dc91d2009d69fe635e339"


def test_stt_manifest_is_the_adr0006_default_repo() -> None:
    """The pinned STT model is the ADR-0006 default sherpa-onnx zipformer repo."""
    assert isinstance(STT_MODEL_MANIFEST, ModelManifest)
    assert STT_MODEL_MANIFEST.repo == _STT_REPO


def test_stt_manifest_pins_an_immutable_commit_not_a_tag() -> None:
    """The STT revision is a full 40-hex commit SHA — a reproducible pin."""
    assert STT_MODEL_MANIFEST.revision == _STT_REVISION
    assert re.fullmatch(r"[0-9a-f]{40}", STT_MODEL_MANIFEST.revision) is not None


def test_stt_manifest_pins_the_real_encoder_sha256() -> None:
    """The STT encoder weight is pinned by its real content sha256 (verified)."""
    encoder = {f.name: f for f in STT_MODEL_MANIFEST.files}.get(_STT_ENCODER)
    assert encoder is not None, "the manifest must pin the encoder ONNX artifact"
    assert encoder.sha256 == _STT_ENCODER_SHA256
    assert encoder.spdx == "Apache-2.0"


def test_stt_manifest_pins_the_full_transducer_triplet() -> None:
    """The STT pin covers the encoder, decoder, and joiner (the whole model)."""
    names = {f.name for f in STT_MODEL_MANIFEST.files}
    assert _STT_ENCODER in names
    assert "decoder-epoch-99-avg-1.onnx" in names
    assert "joiner-epoch-99-avg-1.onnx" in names


def test_stt_manifest_validates_under_the_stt_family_gate() -> None:
    """The whole pinned STT manifest passes the STT (Apache-2.0-only) licence gate."""
    # Raises LicenceError on any non-allowed file; a clean return is the assertion.
    validate_manifest(STT_MODEL_MANIFEST, ModelFamily.STT)


def test_tts_manifest_is_the_adr0007_default_repo() -> None:
    """The pinned TTS model is the ADR-0007 default Kokoro sherpa-onnx repo."""
    assert isinstance(TTS_MODEL_MANIFEST, ModelManifest)
    assert TTS_MODEL_MANIFEST.repo == _TTS_REPO


def test_tts_manifest_pins_an_immutable_commit_not_a_tag() -> None:
    """The TTS revision is a full 40-hex commit SHA — a reproducible pin."""
    assert TTS_MODEL_MANIFEST.revision == _TTS_REVISION
    assert re.fullmatch(r"[0-9a-f]{40}", TTS_MODEL_MANIFEST.revision) is not None


def test_tts_manifest_pins_the_real_model_sha256() -> None:
    """The TTS model weight is pinned by its real content sha256 (verified)."""
    model = {f.name: f for f in TTS_MODEL_MANIFEST.files}.get(_TTS_MODEL_FILE)
    assert model is not None, "the manifest must pin the model.onnx artifact"
    assert model.sha256 == _TTS_MODEL_SHA256
    assert model.spdx == "Apache-2.0"


def test_tts_manifest_validates_under_the_tts_family_gate() -> None:
    """The whole pinned TTS manifest passes the TTS licence gate."""
    validate_manifest(TTS_MODEL_MANIFEST, ModelFamily.TTS)
