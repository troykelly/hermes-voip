"""On-disk content verification for pinned model files (rule 35, ADR-0006/0007/0009).

The licence gate (``validate_manifest``) checks only the *declared* SPDX string;
it never reads the actual model bytes. This module is the missing half: given a
materialised ``model_dir`` and the pinned :class:`ModelManifest`,
:func:`verify_model_files` streams every pinned file through SHA-256 and asserts
the digest matches the pin — so a swapped, truncated, or missing weight artifact
is caught before the provider loads it. The most security-critical case is the
prompt-injection GUARD model (ADR-0009): a silently-swapped guard neuters the
whole injection defence, and only a real on-disk hash check stops that.

These tests build a tiny synthetic ``model_dir`` with obvious fake bytes and pin
each file by its *real* computed SHA-256 (no real model data or PII — the repo is
public). The load-bearing assertions:

* bytes whose digest does NOT match the pin -> :class:`ModelIntegrityError`;
* bytes whose digest matches the pin -> passes silently;
* a pinned file absent from ``model_dir`` -> :class:`ModelIntegrityError`;
* a pinned file under a subdirectory (e.g. ``onnx/model.onnx``, the real GUARD
  layout) is resolved relative to ``model_dir`` and verified;
* the raised error names the file and a SHORT digest prefix only — it never
  echoes file bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hermes_voip.manifest import (
    ModelFile,
    ModelIntegrityError,
    ModelManifest,
    verify_model_files,
)

# A syntactically-valid pin coordinate using obvious public fakes: a 40-hex
# revision (model repo ids are public, but nothing here needs to be real).
_FAKE_REVISION = "0" * 40
_FAKE_REPO = "fake-owner/fake-model"


def _sha256_hex(data: bytes) -> str:
    """Return the real lowercase-hex SHA-256 of ``data`` (the pin the test asserts)."""
    return hashlib.sha256(data).hexdigest()


def _write(model_dir: Path, name: str, data: bytes) -> None:
    """Write ``data`` to ``model_dir/name``, creating any subdirectory in ``name``."""
    path = model_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_matching_bytes_pass(tmp_path: Path) -> None:
    """A model_dir whose file bytes hash to the pinned sha256 verifies silently."""
    data = b"synthetic-model-weights-A"
    manifest = ModelManifest(
        repo=_FAKE_REPO,
        revision=_FAKE_REVISION,
        files=(
            ModelFile(name="model.onnx", sha256=_sha256_hex(data), spdx="Apache-2.0"),
        ),
    )
    _write(tmp_path, "model.onnx", data)

    # The assertion is the ABSENCE of a raise: a matching digest verifies and
    # returns control to the caller (verify_model_files returns None).
    verify_model_files(manifest, str(tmp_path))


def test_mismatched_bytes_raise(tmp_path: Path) -> None:
    """Bytes whose digest differs from the pin raise ModelIntegrityError."""
    pinned = b"the-audited-bytes"
    tampered = b"a-DIFFERENT-swapped-artifact"
    assert _sha256_hex(pinned) != _sha256_hex(tampered)
    manifest = ModelManifest(
        repo=_FAKE_REPO,
        revision=_FAKE_REVISION,
        files=(
            ModelFile(name="model.onnx", sha256=_sha256_hex(pinned), spdx="Apache-2.0"),
        ),
    )
    _write(tmp_path, "model.onnx", tampered)

    with pytest.raises(ModelIntegrityError) as excinfo:
        verify_model_files(manifest, str(tmp_path))

    message = str(excinfo.value)
    # The error names the offending file so the audit trail records which artifact
    # failed...
    assert "model.onnx" in message
    # ...but never echoes the file bytes (neither the tampered nor pinned content).
    assert tampered.decode() not in message
    assert pinned.decode() not in message


def test_missing_file_raises(tmp_path: Path) -> None:
    """A pinned file absent from model_dir raises ModelIntegrityError, not silence."""
    data = b"synthetic-model-weights-B"
    manifest = ModelManifest(
        repo=_FAKE_REPO,
        revision=_FAKE_REVISION,
        files=(
            ModelFile(name="model.onnx", sha256=_sha256_hex(data), spdx="Apache-2.0"),
        ),
    )
    # Deliberately do NOT write model.onnx into tmp_path.

    with pytest.raises(ModelIntegrityError) as excinfo:
        verify_model_files(manifest, str(tmp_path))

    assert "model.onnx" in str(excinfo.value)


def test_pinned_file_in_subdirectory_is_resolved(tmp_path: Path) -> None:
    """A pinned name with a subdirectory (the GUARD onnx/ layout) is verified."""
    data = b"synthetic-guard-weights"
    manifest = ModelManifest(
        repo=_FAKE_REPO,
        revision=_FAKE_REVISION,
        files=(
            ModelFile(
                name="onnx/model.onnx", sha256=_sha256_hex(data), spdx="Apache-2.0"
            ),
        ),
    )
    _write(tmp_path, "onnx/model.onnx", data)

    # No raise == the subdirectory file was located and its digest matched.
    verify_model_files(manifest, str(tmp_path))


def test_first_mismatch_among_many_raises(tmp_path: Path) -> None:
    """With several pinned files, one bad digest fails the whole verification."""
    good = b"good-encoder"
    bad_pinned = b"good-decoder"
    bad_on_disk = b"swapped-decoder"
    manifest = ModelManifest(
        repo=_FAKE_REPO,
        revision=_FAKE_REVISION,
        files=(
            ModelFile(name="encoder.onnx", sha256=_sha256_hex(good), spdx="Apache-2.0"),
            ModelFile(
                name="decoder.onnx", sha256=_sha256_hex(bad_pinned), spdx="Apache-2.0"
            ),
        ),
    )
    _write(tmp_path, "encoder.onnx", good)
    _write(tmp_path, "decoder.onnx", bad_on_disk)

    with pytest.raises(ModelIntegrityError) as excinfo:
        verify_model_files(manifest, str(tmp_path))

    assert "decoder.onnx" in str(excinfo.value)
