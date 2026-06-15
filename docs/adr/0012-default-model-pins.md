# ADR-0012: Pinned default-model artifacts for STT and TTS

- **Date:** 2026-06-15
- **Status:** Accepted
- **Deciders:** agent session

## Context

ADR-0006 (streaming STT) and ADR-0007 (streaming TTS) each name an Apache-2.0
self-host default model but explicitly defer the concrete artifact pin — "the
exact revision + checksums are recorded at implementation" — to the implementing
commit. ADR-0009 already recorded its pin (`GUARD_MODEL_MANIFEST`) and rule 35
requires every default model on a conversational seam to be licence-gated against
its family's SPDX allow-list before it is constructed.

The provider-wiring step (W8, `providers/build.py`) is where each default model is
instantiated, so it is where the licence gate must fire. A cross-vendor review of
PR #40 found the self-host STT/TTS factories were constructing their models with
**no** licence gate (only the ONNX guard was gated). Closing that gap requires the
two deferred pins to exist as `ModelManifest` data the gate can check.

The models are **operator-supplied at runtime** — the operator points
`HERMES_VOIP_STT_MODEL_DIR` / `HERMES_VOIP_TTS_MODEL` at their own download. The
manifest is therefore a *licence/identity assertion*, not a loader: it records the
canonical artifact whose declared SPDX licence the default selection relies on, so
CI fails loudly if the recorded licence is ever changed to a banned one. It is not
a runtime checksum of the operator's local files (that is a separate, future
download-verification concern).

## Decision

Pin the two deferred default-model artifacts as `ModelManifest` constants in
`hermes_voip.manifest`, mirroring `GUARD_MODEL_MANIFEST` exactly (PUBLIC
model-registry coordinates: `repo` + 40-hex commit `revision` + per-file
`sha256` + declared SPDX), and call `validate_manifest(..., ModelFamily.STT|TTS)`
inside the default `sherpa-onnx` / `sherpa-kokoro` factories in
`providers/build.py` before constructing the provider.

**STT** — `STT_MODEL_MANIFEST` (ADR-0006 default, `ModelFamily.STT`, Apache-2.0):

- repo `csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26`
- revision `672fbf1b30579d6585301139bb363f42a0ad4a24`
- files (the `left-64` full-precision transducer triplet, each Apache-2.0):
  - `encoder-epoch-99-avg-1-chunk-16-left-64.onnx`
    sha256 `b67600b0eaf19069867f109a5b6ad78db10efba67ec8e781ea719c956d20261f`
  - `decoder-epoch-99-avg-1-chunk-16-left-64.onnx`
    sha256 `7bf787f90b194b307e5a4ad6a34fadb4e748304c35f78a8d66358a05b13ee6ef`
  - `joiner-epoch-99-avg-1-chunk-16-left-64.onnx`
    sha256 `210591f72b3c56b8364f85f345dca240bc2b4c00632848f4aa923630d5639d3b`

**TTS** — `TTS_MODEL_MANIFEST` (ADR-0007 default, `ModelFamily.TTS`, Apache-2.0):

- repo `csukuangfj/kokoro-en-v0_19` (the sherpa-onnx ONNX packaging of Kokoro-82M;
  the upstream `hexgrad/Kokoro-82M` ships PyTorch, not the ONNX `model.onnx` sherpa
  loads)
- revision `92805c485745946a0d945562d3aba19e7cbb2104`
- file `model.onnx`
  sha256 `10ff414106a038ce7e9e0126c6461e4dc8a86efaa89dc91d2009d69fe635e339`

The sha256 of each LFS-tracked weight is the HuggingFace tree-API `lfs.oid` at the
pinned commit; the values were independently re-verified before commit. Only the
licence-bearing weights are pinned — the small non-LFS `tokens.txt` vocab and
`voices.bin` voice data carry no separate licence the gate must assert.

The TTS repo declares **no** HuggingFace `cardData.license`; its Apache-2.0 licence
is verified from the in-repo `LICENSE` file at the pinned commit (git blob
`d645695673349e3947e8e5ae42332d0ac3164cd7` — the canonical Apache-2.0 text). The
manifest records `spdx="Apache-2.0"` on that basis.

## Consequences

- Every default self-host model on a conversational seam is now licence-gated
  before construction (STT, TTS, and guard), closing the PR #40 gap (rule 35).
- `test_stt_tts_model_manifest.py` locks the pins, so a silent re-point to a
  different commit or a changed declared licence fails CI loudly.
- The pins must be refreshed if ADR-0006/0007 ever change their default model. A
  refresh is: obtain the new repo's commit SHA + per-file `lfs.oid` from the HF
  tree API, re-verify, update both `manifest.py` and the drift test.
- The manifest asserts the *canonical* artifact's licence, not the operator's local
  bytes; a runtime content-verification of the operator-supplied model dir against
  these digests is a possible future enhancement, not built here.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Gate only the guard (status quo before this ADR) | Leaves the self-host STT/TTS default weights un-vetted — the exact rule-35 gap the review flagged. |
| Pin the upstream `hexgrad/Kokoro-82M` repo for TTS | That repo ships PyTorch weights, not the ONNX `model.onnx` the sherpa-onnx provider loads; the gate must pin the artifact actually used. |
| Pin `tokens.txt` / `voices.bin` too | They are not LFS-tracked (no canonical `sha256` via `lfs.oid`) and carry no separate licence; pinning them adds fragility (computed digests) without strengthening the licence gate. |
| Skip the gate because the model is operator-supplied | The *default selection* still asserts an Apache-2.0 model; rule 35 gates the default a commit ships, regardless of where the bytes are fetched. |
