"""Pinned model manifests and the per-family SPDX licence gate (ADR-0006/0007/0009).

Every conversational seam — streaming STT (ADR-0006), streaming TTS (ADR-0007),
and the prompt-injection guard (ADR-0009) — runs a *self-hosted* default model
whose engine and weights carry independent licences. Several otherwise-attractive
models ship under share-alike or non-commercial terms (the Kroko STT zipformer is
CC-BY-SA-4.0; the disqualified self-host TTS set — Coqui XTTS, F5-TTS,
Fish-Speech/OpenAudio, ChatTTS — is non-commercial / copyleft-viral). For a
PUBLIC repo operated commercially, those are traps: the pin must be *verified*,
not trusted (rule 35).

This module is the data + gate for that verification, and it is deliberately
**pure**: a :class:`ModelManifest` pins the exact artifact (source ``repo``, a
``revision`` commit SHA, and each file's ``sha256``), each pinned :class:`ModelFile`
records its declared SPDX licence id, and the gate checks those recorded strings
against a **per-family allow-list**. It performs **no** network access, model
download, or ONNX load, so an offline, reproducible CI (rule 33) can run it
deterministically.

The allow-list is *default-deny*: a licence is acceptable for a family **iff** it
is in that family's allow-list. STT and the guard accept Apache-2.0 only; TTS
additionally accepts MIT, CC0-1.0, and CC-BY-4.0 (the permissive Creative-Commons
weights its ADR cleared). Because the check is allow-list membership — not a
denylist — *any* licence outside the family's set is rejected, so a banned model
can never validate for its family even if it is one this code never enumerated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import assert_never

__all__ = [
    "GUARD_ALLOWED_SPDX",
    "STT_ALLOWED_SPDX",
    "TTS_ALLOWED_SPDX",
    "LicenceError",
    "ModelFamily",
    "ModelFile",
    "ModelManifest",
    "licence_ok",
    "validate_manifest",
]

# A model file's content digest: exactly 64 lowercase hex chars (SHA-256). A pin
# that is not a full digest is not a pin, so construction rejects it.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# A single SPDX licence id: letters/digits/dot/hyphen (incl. a `LicenseRef-`
# prefix). This deliberately excludes whitespace and the SPDX *expression*
# operators (OR/AND/WITH and a trailing `+`) — a pinned artifact must declare one
# concrete id, not an expression, so the allow-list gate compares like for like.
_SPDX_ID_RE = re.compile(r"[A-Za-z0-9.-]+")
# A pinned revision is a full 40-hex Git commit SHA (ADR-0006/0007/0009 each pin a
# revision, not a branch/tag) — a moving ref like "main" is not a reproducible pin.
_REVISION_RE = re.compile(r"[0-9a-f]{40}")


class LicenceError(ValueError):
    """A pinned model file declares an SPDX licence its family does not allow.

    Raised by :func:`validate_manifest`. The message names the offending file
    and its licence so the CI licence-gate log (and the audit trail) records
    exactly which artifact failed and why (rule 35).
    """


class ModelFamily(Enum):
    """The conversational-seam family a model serves; selects the allow-list.

    Each family's licence policy is fixed by its ADR: :data:`STT` (ADR-0006) and
    :data:`GUARD` (ADR-0009) are Apache-2.0 only; :data:`TTS` (ADR-0007) widens
    the set to the permissive Creative-Commons weights it cleared.
    """

    STT = "stt"  # streaming speech-to-text (ADR-0006)
    TTS = "tts"  # streaming text-to-speech (ADR-0007)
    GUARD = "guard"  # prompt-injection guard (ADR-0009)


# Per-family SPDX allow-lists (the only definition of "acceptable"; default-deny).
#
# STT (ADR-0006) and the guard (ADR-0009) require an Apache-2.0 engine AND model:
# the Kroko zipformer (CC-BY-SA-4.0) is the explicit trap this excludes.
STT_ALLOWED_SPDX: frozenset[str] = frozenset({"Apache-2.0"})
GUARD_ALLOWED_SPDX: frozenset[str] = frozenset({"Apache-2.0"})
# TTS (ADR-0007) additionally allows MIT (e.g. Piper's from-scratch libritts
# voice), CC0-1.0, and CC-BY-4.0 (e.g. Kyutai weights with CC0/CC-BY voices) —
# but still bans the non-commercial / copyleft-viral set (CC-BY-NC*, CPML, AGPL).
TTS_ALLOWED_SPDX: frozenset[str] = frozenset(
    {"Apache-2.0", "MIT", "CC0-1.0", "CC-BY-4.0"}
)


def _allowed_spdx(family: ModelFamily) -> frozenset[str]:
    """Return the SPDX allow-list for ``family`` (total over :class:`ModelFamily`).

    Exhaustive by construction: a new :class:`ModelFamily` member that is not
    given an allow-list here fails ``mypy`` at :func:`assert_never` rather than
    silently defaulting to "allow nothing" (or worse, allowing everything).
    """
    if family is ModelFamily.STT:
        return STT_ALLOWED_SPDX
    if family is ModelFamily.TTS:
        return TTS_ALLOWED_SPDX
    if family is ModelFamily.GUARD:
        return GUARD_ALLOWED_SPDX
    assert_never(family)


def licence_ok(family: ModelFamily, spdx: str) -> bool:
    """Return whether ``spdx`` is allowed for ``family`` (allow-list membership).

    This is default-deny: only licences in the family's allow-list pass, so every
    non-commercial / share-alike / unknown identifier is rejected.

    Args:
        family: The conversational-seam family the model serves.
        spdx: The model file's declared SPDX licence id (e.g. ``"Apache-2.0"``).

    Returns:
        True iff ``spdx`` is in ``family``'s allow-list.
    """
    return spdx in _allowed_spdx(family)


@dataclass(frozen=True, slots=True)
class ModelFile:
    """One pinned file of a model artifact, with its content digest and licence.

    Attributes:
        name: The file name within the source repo (e.g. ``encoder.onnx``).
        sha256: The file's SHA-256 content digest (64 lowercase hex chars).
        spdx: The file's declared SPDX licence id (e.g. ``Apache-2.0``).
    """

    name: str
    sha256: str
    spdx: str

    def __post_init__(self) -> None:
        """Reject a file that is not a real pin (no name, bad digest, no licence)."""
        if not self.name:
            msg = "ModelFile.name must not be empty"
            raise ValueError(msg)
        if _SHA256_RE.fullmatch(self.sha256) is None:
            msg = (
                f"ModelFile.sha256 must be 64 lowercase hex chars, "
                f"got {self.sha256!r} for {self.name!r}"
            )
            raise ValueError(msg)
        if _SPDX_ID_RE.fullmatch(self.spdx) is None:
            msg = (
                f"ModelFile.spdx must be a single SPDX licence id (no whitespace "
                f"or expression operators), got {self.spdx!r} for {self.name!r}"
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ModelManifest:
    """A pinned model artifact: a source repo at a revision, and its files.

    The manifest is the *exact* identity the licence gate verifies — not the
    generic model name. Pinning ``repo`` + ``revision`` + per-file ``sha256``
    means the gate (and any future fetch) checks the precise bytes a licence was
    asserted against (ADR-0006/0007/0009).

    Attributes:
        repo: The model source repository id (e.g. a public ``owner/name``).
        revision: The pinned commit SHA (40 lowercase hex chars), not a branch.
        files: The pinned files, each with its digest and SPDX licence.
    """

    repo: str
    revision: str
    files: tuple[ModelFile, ...]

    def __post_init__(self) -> None:
        """Enforce the pin invariants the type promises; validate, don't trust."""
        if not self.repo:
            msg = "ModelManifest.repo must not be empty"
            raise ValueError(msg)
        if _REVISION_RE.fullmatch(self.revision) is None:
            msg = (
                f"ModelManifest.revision must be a 40-hex commit SHA, "
                f"got {self.revision!r}"
            )
            raise ValueError(msg)
        if not self.files:
            msg = f"ModelManifest({self.repo!r}) must pin at least one file"
            raise ValueError(msg)
        names = [f.name for f in self.files]
        if len(set(names)) != len(names):
            msg = f"ModelManifest({self.repo!r}) has duplicate file names"
            raise ValueError(msg)


def validate_manifest(manifest: ModelManifest, family: ModelFamily) -> None:
    """Assert every pinned file in ``manifest`` is licence-allowed for ``family``.

    The CI licence gate (rule 35): a model may be a committed default for a
    conversational seam only if *all* of its pinned files declare a licence in
    that family's allow-list. The check is per-file, so one disallowed file among
    permissive ones still fails the whole manifest. Errors propagate (rule 37):
    the first offending file raises rather than being skipped.

    Args:
        manifest: The pinned model artifact to verify.
        family: The conversational-seam family the model would serve.

    Raises:
        LicenceError: If any pinned file's SPDX licence is not allowed for
            ``family``; the message names the file, its licence, and the family.
    """
    for file in manifest.files:
        if not licence_ok(family, file.spdx):
            msg = (
                f"{manifest.repo}@{manifest.revision}: file {file.name!r} "
                f"declares licence {file.spdx!r}, which is not allowed for the "
                f"{family.value} family (allowed: "
                f"{', '.join(sorted(_allowed_spdx(family)))})"
            )
            raise LicenceError(msg)
