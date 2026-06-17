# hermes-voip

A [Hermes](https://hermes-agent.nousresearch.com/) plugin that gives a Hermes agent **full
two-way conversational voice** over telephony. It registers as an extension on any
RFC-compliant **SIP-over-TLS** voice gateway, bridges live call audio through a
speech-to-text → agent → text-to-speech loop, and speaks back to the caller — inbound calls
and operator-placed outbound calls alike.

## What this is

A **Python package** (a Hermes plugin) — not a standalone service. The Hermes runtime loads
it (it registers a `voip` platform), so this repository makes **no hosting or platform
assumptions**. Gateway connection details (host, extension, password) are configuration,
supplied via `HERMES_SIP_*` environment variables and never committed — the repo is public.

What is built and working today:

- **SIP-over-TLS** registration (single or multiple extensions), inbound INVITE handling,
  and operator-placed outbound calls (RFC 3261 / 3550 / 4566).
- **Codec negotiation, best-available:** **G.722** 16 kHz wideband is offered first, with
  **G.711** (PCMU/PCMA) as the universal fallback; the STT/TTS sample rate follows the
  negotiated codec (ADR-0022).
- **Cascaded media:** streaming STT → the Hermes agent → streaming TTS, with the agent as
  the reasoner (ADR-0003). Audio is telephony-native and emoji-safe (spoken text is
  sanitised before synthesis).
- **Two selectable TTS providers** and **two selectable STT providers** — local self-host or
  cloud — chosen by config (see [Conversational media](#conversational-media-stt--tts)).
- **Caller trust tiers** (caller groups): an inbound caller or an outbound callee is
  **untrusted unless allow-listed**, with least-privilege tool gating
  (see [Caller groups](#caller-groups-trust-tiers)).

> **Roadmap (not yet wired — do not rely on these):** WebRTC (SIP-over-WSS) media transport,
> DTMF-send + intercom, and call-termination session signalling are in progress. The
> `wss` transport token and the WebRTC building blocks (the `webrtc` extra) exist, but the
> live media path runs over SIP-over-TLS today. Track these in [`docs/adr/`](docs/adr/).

## Install & run

The plugin core is light (only `audioop-lts`); the Hermes runtime, the local-inference ML
stack, and the media/transport libraries live in **extras**. Install all of them:

```bash
uv sync --frozen --all-extras   # hermes + ml + media + webrtc extras
```

The extras (declared in [`pyproject.toml`](pyproject.toml)):

| Extra     | What it provides                                                              |
| --------- | ---------------------------------------------------------------------------- |
| `hermes`  | The `hermes-agent` runtime + `hermes` CLI that loads the plugin              |
| `ml`      | `sherpa-onnx` STT/TTS, `onnxruntime` (VAD + injection guard), `tokenizers`   |
| `media`   | `cryptography` for SDES-SRTP media encryption (ADR-0013)                     |
| `webrtc`  | WebRTC building blocks (`aioice`, `pyopenssl`, `websockets`) — roadmap        |

Then enable the plugin and bring the gateway up — it registers from the `HERMES_SIP_*` /
`HERMES_VOIP_*` environment:

```bash
hermes plugins enable hermes-voip
hermes gateway run
```

## Configuration

All configuration is environment variables. Copy [`.env.example`](.env.example) to a
gitignored `.env` and fill in real values from 1Password. **Never commit real host /
extension / password values** — the examples below are fakes.

### Gateway connection (`HERMES_SIP_*`)

| Variable                    | Required | Default | Notes                                          |
| --------------------------- | -------- | ------- | ---------------------------------------------- |
| `HERMES_SIP_HOST`           | yes      | —       | Gateway FQDN (the SIP registrar), e.g. `pbx.example.test` |
| `HERMES_SIP_EXTENSION`      | yes      | —       | Extension / SIP user-part, e.g. `1000`         |
| `HERMES_SIP_PASSWORD`       | yes      | —       | Digest-auth password                           |
| `HERMES_SIP_USERNAME`       | no       | extension | Digest-auth username                         |
| `HERMES_SIP_PORT`           | no       | `5061` (tls) | Signalling port                           |
| `HERMES_SIP_TRANSPORT`      | no       | `tls`   | `tls` (working) or `wss` (roadmap)             |

For **multiple registrations**, use the indexed form `HERMES_SIP_EXTENSION_<n>` +
`HERMES_SIP_PASSWORD_<n>` (optional `HERMES_SIP_USERNAME_<n>`); `HERMES_SIP_DEFAULT_EXTENSION`
picks the inbound fallback. The single and indexed schemes must not be mixed.

### Conversational media (STT / TTS)

Every key has a safe default, so a bare install runs the **fully-offline self-host** path.
Selection is config-only ([`config.py`](src/hermes_voip/config.py),
[`providers/build.py`](src/hermes_voip/providers/build.py)).

**Text-to-speech** — `HERMES_VOIP_TTS_PROVIDER`:

| Value           | Provider                                            | Default | Credential          |
| --------------- | --------------------------------------------------- | ------- | ------------------- |
| `sherpa-kokoro` | Local Kokoro-82M via sherpa-onnx (self-host, free) | **yes** | none (needs `HERMES_VOIP_TTS_MODEL`) |
| `elevenlabs`    | ElevenLabs Flash v2.5 realtime WebSocket (cloud)   | no      | `ELEVENLABS_API_KEY` |

Both are first-class. `sherpa-kokoro` is the default (local, no API key). `elevenlabs`
streams Flash v2.5 and emits PCM natively at the negotiated wire rate (8 kHz for G.711,
16 kHz for G.722). Set the voice with `HERMES_VOIP_TTS_VOICE` and the Kokoro model directory
with `HERMES_VOIP_TTS_MODEL`.

**Speech-to-text** — `HERMES_VOIP_STT_PROVIDER`:

| Value         | Provider                                          | Default | Credential        |
| ------------- | ------------------------------------------------- | ------- | ----------------- |
| `sherpa-onnx` | Local streaming zipformer (self-host, free)       | **yes** | `HERMES_VOIP_STT_MODEL_DIR` |
| `deepgram`    | Deepgram streaming (cloud)                         | no      | `DEEPGRAM_API_KEY` |

A selected cloud provider must have its credential set, or startup fails fast. (Other tokens
are reserved in config but not yet wired; selecting one fails fast at provider build.)

Other media knobs (all optional, with safe defaults): `HERMES_VOIP_GREETING` (opening line;
empty disables it), `HERMES_VOIP_RTP_SYMMETRIC` (NAT comedia latching, on by default),
`HERMES_VOIP_VAD_THRESHOLD`, `HERMES_VOIP_ENDPOINT_SILENCE_MS`, and the `HERMES_SIP_DTMF_*`
DTMF-receive settings. See [`config.py`](src/hermes_voip/config.py) for the full surface.

### Caller groups (trust tiers)

The remote party on **any** call — an inbound caller **or** an outbound callee — is
**untrusted unless allow-listed**. Caller-ID is forgeable and is **not** authentication; a
caller group is a privilege **ceiling**, never a bypass (ADR-0020 / ADR-0021). Callers are
sorted into named trust tiers by `privilege_level`:

- **0 (receptionist)** — SAFE tools only; the default for any unmatched caller.
- **2 (trusted)** — adds ELEVATED tools (e.g. hold/resume).
- **3 (operator/assistant)** — adds IRREVERSIBLE tools (e.g. transfer), which **still**
  require per-action confirmation and a non-degraded session.

Caller numbers are PII, so they live in **gitignored JSON files** referenced by env **paths
only** — inline number lists are rejected. Use either the N-group file
`HERMES_VOIP_CALLER_GROUPS_FILE`, or the legacy 3-file scheme
(`HERMES_VOIP_CALLER_{ALLOW,DENY,GREY}_FILE`). An unmatched caller falls to the unprivileged
default; a privileged default is refused at startup. The full schema and operational steps
are in the runbook [`docs/runbooks/0003-voip-caller-modes.md`](docs/runbooks/0003-voip-caller-modes.md).

```jsonc
// example caller-list file (fakes only) — { "patterns": [...] }
// exact value OR a "*"-suffixed literal prefix
{ "patterns": ["+15555550100", "1000", "+1555550*"] }
```

## Development

Standardized devcontainer. Toolchain standards: [`docs/stack.md`](docs/stack.md). Working
rules every change follows: [`AGENTS.md`](AGENTS.md).

```bash
uv sync --all-extras     # install (CI: uv sync --frozen)
uv run ruff format .     # format        (check: uv run ruff format --check .)
uv run ruff check .      # lint
uv run mypy              # strict type-check
uv run pytest            # tests
```

- **Language/runtime:** Python ≥ 3.13, managed with **uv**. **Typing:** mypy strict, no
  escape hatches. **Lint/format:** ruff.
- **Secrets:** 1Password + a gitignored `.env`.

## Security

This repository is **public**. Never commit the gateway host, extension number, passwords,
internal hostnames, IPs, caller numbers, or any PII — they live only in the gitignored `.env`,
gitignored caller-list files, and 1Password. Secret scanning (gitleaks) and a dependency
vulnerability audit run in CI.

## Licence

Not yet specified (operator to choose).
