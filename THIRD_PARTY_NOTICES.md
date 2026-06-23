# Third-Party Notices

`hermes-voip` relies on the third-party components listed below. Each component is
governed by its own licence — the terms of those licences apply to each component
respectively. The licences of all components listed here are permissive (no copyleft or
non-commercial restriction); this is a hard constraint of the project (see ADR-0006,
ADR-0007, ADR-0009 in `docs/adr/`).

The SPDX licence identifiers in this file were verified against each package's
distribution metadata (`importlib.metadata`) or, where the metadata field is absent,
against the OSI classifier embedded in the package's PyPI record — both verified at the
pinned version committed in `uv.lock`. Model licences are verified against the ADR that
introduced the model and the pinned HuggingFace artifact (repo + revision) recorded in
`src/hermes_voip/manifest.py`.

---

## Python runtime dependencies

These packages are declared in `[project].dependencies` in `pyproject.toml` and are
installed in every environment.

| Component | Version | Licence (SPDX) | Used for | URL |
|-----------|---------|----------------|----------|-----|
| audioop-lts | 0.2.2 | PSF-2.0 | G.711 μ-law/A-law encode/decode and PCM rate conversion (Python 3.13-compatible fork of removed stdlib `audioop`) | <https://github.com/AbstractUmbra/audioop> |

---

## Optional extra: `ml`

Installed with `uv sync --extra ml`. Provides the local-inference stack for on-device
VAD, STT, TTS, and the prompt-injection guard.

| Component | Version | Licence (SPDX) | Used for | URL |
|-----------|---------|----------------|----------|-----|
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | PCM audio frame arithmetic | <https://numpy.org> |
| onnxruntime | 1.24.4 | MIT | ONNX model inference — Silero VAD, DeBERTa prompt-injection guard | <https://onnxruntime.ai> |
| sherpa-onnx | 1.13.2 | Apache-2.0 | On-device streaming STT (Zipformer) and TTS (Kokoro-82M) inference runtime | <https://github.com/k2-fsa/sherpa-onnx> |
| tokenizers | 0.23.1 | Apache-2.0 | SentencePiece tokenizer for the DeBERTa prompt-injection guard (loads `tokenizer.json` standalone) | <https://github.com/huggingface/tokenizers> |

> **Note on `tokenizers`:** the `License` metadata field is absent from the wheel; the
> licence is verified from the OSI classifier `License :: OSI Approved :: Apache Software
> License` embedded in the PyPI record at version 0.23.1.

---

## Optional extra: `media`

Installed with `uv sync --extra media`. Provides SDES-SRTP media encryption (RFC 3711).

| Component | Version | Licence (SPDX) | Used for | URL |
|-----------|---------|----------------|----------|-----|
| cryptography | 48.0.1 | Apache-2.0 OR BSD-3-Clause | AES-CM keystream and HMAC-SHA1 authentication for SDES-SRTP (RFC 3711) | <https://cryptography.io/> |

---

## Optional extra: `webrtc`

Installed with `uv sync --extra webrtc`. Provides WebRTC transport (ICE, DTLS-SRTP,
Opus, SIP-over-Secure-WebSocket).

| Component | Version | Licence (SPDX) | Used for | URL |
|-----------|---------|----------------|----------|-----|
| aioice | 0.10.2 | BSD-3-Clause | ICE agent for WebRTC connectivity negotiation (RFC 8445) | <https://github.com/aiortc/aioice> |
| opuslib | 3.0.1 | BSD-3-Clause | Pure-Python ctypes binding to the system `libopus` library for WebRTC/Opus audio encode/decode | <https://github.com/onbeep/opuslib> |
| pyOpenSSL | 26.2.0 | Apache-2.0 | DTLS handshake and RFC 5705 keying-material export for WebRTC DTLS-SRTP | <https://pyopenssl.org/> |
| websockets | 16.0 | BSD-3-Clause | asyncio WebSocket client for SIP-over-Secure-WebSocket (RFC 6455 / RFC 7118) | <https://github.com/python-websockets/websockets> |

**Notable transitive dependencies of `aioice`** (installed automatically):

| Component | Licence (SPDX) | URL |
|-----------|----------------|-----|
| dnspython | ISC | <https://www.dnspython.org/> |
| ifaddr | MIT | <https://github.com/pydron/ifaddr> |

---

## Optional extra: `hermes`

The Hermes runtime that loads this plugin. Declared as an optional extra so its large
transitive tree stays out of the default install (see `pyproject.toml` comment).

| Component | Version | Licence (SPDX) | Used for | URL |
|-----------|---------|----------------|----------|-----|
| hermes-agent | 0.16.0 | MIT | The Hermes plugin runtime that loads and orchestrates this plugin | <https://hermes-agent.nousresearch.com/> |

---

## Vendored code (no runtime package dependency)

This code is embedded directly in the source tree; it introduces no additional Python
package dependency.

| Component | Licence | Source | Location | Used for |
|-----------|---------|--------|----------|----------|
| G.722 codec (public-domain port) | Public Domain (Steve Underwood) + "completely unrestricted" (CMU 1993) | Ported from the public-domain G.722 reference by Steve Underwood, based on the CMU 1993 single-channel G.722 codec; as packaged by the `sippy/libg722` project (Public-Domain + BSD-2-Clause). The ITU-T STL conformance vectors (modified GPL) are deliberately NOT included. See ADR-0022. | `src/hermes_voip/media/g722.py` | Pure-Python G.722 wideband codec encode/decode (16 kHz, 64 kbit/s, RTP mode 1) |

---

## Models and voices

These are AI model weights / voice data the plugin loads at runtime from
operator-supplied local directories (the plugin does not download model weights itself).
Licences are verified against the ADR that introduced each model and against the pinned
HuggingFace artifact recorded in `src/hermes_voip/manifest.py`.

### Default offline models (always required for the default self-hosted path)

| Component | Licence (SPDX) | Introduced | Used for | URL |
|-----------|----------------|------------|----------|-----|
| sherpa-onnx Zipformer STT model (`csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26`) | Apache-2.0 | ADR-0006, ADR-0012 | Default streaming speech-to-text (k2/icefall model, pinned revision `672fbf1b`) | <https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26> |
| Kokoro-82M ONNX TTS model (`csukuangfj/kokoro-en-v0_19`) | Apache-2.0 | ADR-0007, ADR-0012 | Default streaming text-to-speech voice, pinned revision `92805c48`; Apache-2.0 verified from in-repo LICENSE at that commit | <https://huggingface.co/csukuangfj/kokoro-en-v0_19> |
| Silero VAD model | MIT | ADR-0008 | Voice-activity detection — detects when callers start and stop speaking; required on every call regardless of TTS/STT provider | <https://github.com/snakers4/silero-vad> |
| DeBERTa prompt-injection guard (`protectai/deberta-v3-base-prompt-injection-v2`) | Apache-2.0 | ADR-0009 | On-device prompt-injection guard that screens every caller utterance before it reaches the agent; required on every call | <https://huggingface.co/protectai/deberta-v3-base-prompt-injection-v2> |

### Optional / alternative self-hosted TTS models

These are operator-selected alternatives to the default Kokoro voice (see ADR-0007 and
`HERMES_VOIP_TTS_PROVIDER`). None are downloaded by default.

| Component | Licence (SPDX) | Notes | Used for | URL |
|-----------|----------------|-------|----------|-----|
| Piper TTS engine | MIT | Engine licence only | Alternative self-hosted TTS engine for high-concurrency / no-GPU deployments | <https://github.com/rhasspy/piper> |
| Piper `en_US-libritts/high` voice | MIT | The libritts-trained voice; the `lessac`/Blizzard-derived voice is non-commercial and is NOT used — see ADR-0007 | Default Piper voice when `HERMES_VOIP_TTS_PROVIDER=piper` | <https://huggingface.co/rhasspy/piper-voices> |
| KittenTTS | Apache-2.0 | Alternative to Piper for concurrency tier | Alternative self-hosted TTS when `HERMES_VOIP_TTS_PROVIDER=kittentts` | (verify) |
| Kyutai TTS model weights | CC-BY-4.0 | Only CC0/CC-BY voice packs are permitted; Expresso voices (CC-BY-NC) are explicitly disallowed in committed config — see ADR-0007 | Premium GPU-tier TTS when `HERMES_VOIP_TTS_PROVIDER=kyutai` | <https://huggingface.co/kyutai> |

---

## System libraries (not Python packages)

These are OS-level shared libraries the plugin dlopen's at runtime. They must be
provided by the host system (the devcontainer ships them; see the Quickstart).

| Component | Licence | Used for | Install |
|-----------|---------|----------|---------|
| libopus | BSD-3-Clause | Opus audio codec for WebRTC calls (loaded by `opuslib` via ctypes) | `apt-get install libopus0` |

---

*This file was last updated 2026-06-23 against the dependency versions pinned in
`uv.lock` at that date.*
