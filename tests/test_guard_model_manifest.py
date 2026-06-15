"""The GUARD default model is pinned and licence-gated (ADR-0009, rule 35).

The in-process injection guard's default model (ADR-0009) is
``protectai/deberta-v3-base-prompt-injection-v2`` — Apache-2.0, ungated, served
offline via ONNX. A *default* model on a conversational seam may ship only behind
the same verified pin the STT/TTS families use: a concrete ``repo`` + a 40-hex
commit ``revision`` (not a moving tag) + the real per-file ``sha256``, checked
against the GUARD family's Apache-2.0-only allow-list.

These assertions pin the published manifest instance itself (not a fabricated
fixture — that is what ``test_model_licence_gate`` covers): the repo id is the
ADR-0009 default, the revision is a real immutable commit, the ONNX artifact's
digest is the one HuggingFace records for that commit, and the whole manifest
validates for :data:`ModelFamily.GUARD`. The test is pure static metadata — no
network, no download, no ONNX load — so it runs in the offline CI (rule 33).
"""

from __future__ import annotations

import re

from hermes_voip.manifest import (
    GUARD_MODEL_MANIFEST,
    ModelFamily,
    ModelManifest,
    validate_manifest,
)

# The ADR-0009 default model repository and its verified pin. These are PUBLIC
# model-registry coordinates (no host/secret/PII), recorded so a drift in the
# shipped manifest (a re-point to a different commit or a changed artifact) fails
# this test loudly rather than silently shipping un-vetted weights.
_GUARD_REPO = "protectai/deberta-v3-base-prompt-injection-v2"
_GUARD_REVISION = "e6535ca4ce3ba852083e75ec585d7c8aeb4be4c5"
# sha256 of onnx/model.onnx at the pinned revision (the Git-LFS pointer's
# `oid sha256:` — i.e. the content digest of the actual ONNX artifact).
_ONNX_SHA256 = "f0ea7f239f765aedbde7c9e163a7cb38a79c5b8853d3f76db5152172047b228c"


def test_guard_manifest_is_the_adr0009_default_repo() -> None:
    """The pinned GUARD model is the ADR-0009 default ProtectAI DeBERTa repo."""
    assert isinstance(GUARD_MODEL_MANIFEST, ModelManifest)
    assert GUARD_MODEL_MANIFEST.repo == _GUARD_REPO


def test_guard_manifest_pins_an_immutable_commit_not_a_tag() -> None:
    """The revision is a full 40-hex commit SHA — a reproducible, immutable pin."""
    assert GUARD_MODEL_MANIFEST.revision == _GUARD_REVISION
    assert re.fullmatch(r"[0-9a-f]{40}", GUARD_MODEL_MANIFEST.revision) is not None


def test_guard_manifest_pins_the_real_onnx_artifact_sha256() -> None:
    """The ONNX artifact is pinned by its real content sha256 (verified not trusted)."""
    onnx = {f.name: f for f in GUARD_MODEL_MANIFEST.files}.get("onnx/model.onnx")
    assert onnx is not None, "the manifest must pin the ONNX artifact it loads"
    assert onnx.sha256 == _ONNX_SHA256
    assert onnx.spdx == "Apache-2.0"


def test_guard_manifest_validates_under_the_guard_family_gate() -> None:
    """The whole pinned manifest passes the GUARD (Apache-2.0-only) licence gate."""
    # Raises LicenceError on any non-allowed file; a clean return is the assertion.
    validate_manifest(GUARD_MODEL_MANIFEST, ModelFamily.GUARD)
