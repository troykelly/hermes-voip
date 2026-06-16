# Runbook: VoIP live end-to-end validation (W16)

**What it is.** The exact procedure to stand up the `hermes-voip` plugin against a **real**
SIP-over-TLS gateway: a local Hermes runtime loading the `voip` platform plugin, an LLM agent
backend, the self-hosted STT/TTS/VAD/guard models, and the extension **REGISTERED** on the
gateway — so the operator can phone the extension and converse with the agent.

This runbook is the operational HOW. The WHY lives in the ADRs (ADR-0002 platform adapter,
ADR-0005 SIP/RTP, ADR-0006/0007/0008/0009 the conversational providers, ADR-0013 SDES-SRTP).

> **Public repo — secrets are NAMES only here.** This file never contains a host, extension
> number, password, token, or URL. All connection values come from 1Password via the `op`
> CLI (rule 34/41) and are referenced only by environment-variable **name** and 1Password
> **item title**. Never `echo`/`print`/log a fetched value; fetch into a shell variable and
> reference the variable.

## Prerequisites

- Devcontainer up; `op` CLI present (`op --version`) and `OP_SERVICE_ACCOUNT_TOKEN` in the env
  (`op whoami` succeeds).
- Outbound TCP to the gateway's SIP-TLS port is reachable from the runtime host.
- 1Password items (titles):
  - **SIP extension:** the gateway's SIP-extension item in vault `Aperim` (the operator
    knows its title — set `SIP_ITEM` to it; it embeds the extension number so it is not
    reproduced in this public runbook). Fields used: `Extension`
    (the extension number / SIP username), the **VoIP-section `Password`** (the SIP-TLS
    digest secret — *not* the top-level portal `password`), and the `website` URL (the SIP
    host). It also holds a `Voicemail PIN` (unused here).
  - **LLM backend:** `LLAP Hermes mbp018 Provider Key` (vault `Claude API Access`) — an
    OpenAI/OpenRouter-compatible LLM proxy (`credential` = bearer key, `hostname` = proxy
    host). The proxy serves OpenRouter-style `vendor/model` ids (e.g. `nousresearch/hermes-4-70b`).

## 1. Install the runtime + dependencies

```bash
# Hermes runtime + local-inference ML stack + SRTP crypto, from the committed lockfile.
uv sync --extra hermes --extra ml --extra media
# Sanity: the plugin imports and its entry point resolves without pulling the runtime.
uv run python -c "import hermes_voip; print(hermes_voip.__version__); \
  from importlib.metadata import entry_points; \
  print([e.name for e in entry_points(group='hermes_agent.plugins')])"
# expect: 0.0.0  and  ['hermes-voip']
```

## 2. Download + verify the self-hosted models

The pinned repos/revisions/sha256 are the single source of truth in
`src/hermes_voip/manifest.py`. Models are **not** vendored in git (rule 33); download them
locally and verify each pinned digest (rule 23). Default offline stack: sherpa-onnx streaming
STT + sherpa-Kokoro TTS + silero VAD + the DeBERTa ONNX injection guard.

Target a model root *inside the workspace or another gitignored path* (never commit weights):

```bash
export VOIP_MODELS_ROOT="$PWD/.models"   # gitignored; adjust as desired
mkdir -p "$VOIP_MODELS_ROOT"/{stt,tts,vad,guard}
```

Fetch + verify (the STT/TTS/guard pins live in `manifest.py`; the loader file-name layout is
fixed):

| Family | Env var (model dir) | Source (HuggingFace, pinned in `manifest.py`) | Files placed in the dir |
| --- | --- | --- | --- |
| STT | `HERMES_VOIP_STT_MODEL_DIR` | `STT_MODEL_MANIFEST` repo@revision | `encoder.onnx`, `decoder.onnx`, `joiner.onnx` (the three pinned `*-chunk-16-left-64.onnx` weights, renamed), `tokens.txt` |
| TTS | `HERMES_VOIP_TTS_MODEL` | `TTS_MODEL_MANIFEST` repo@revision | `model.onnx` (pinned), `voices.bin`, `tokens.txt`, `espeak-ng-data/` |
| Guard | `HERMES_VOIP_INJECTION_GUARD_MODEL_DIR` | `GUARD_MODEL_MANIFEST` repo@revision | `model.onnx` (pinned, the repo's `onnx/model.onnx`), `tokenizer.json`, `config.json` |
| VAD | `HERMES_VOIP_VAD_MODEL_DIR` | `snakers4/silero-vad` tag `v5.1.2`, file `src/silero_vad/data/silero_vad.onnx` (MIT) | `silero_vad.onnx` |

Download script (reads the pins from `manifest.py`; verifies each pinned sha256 and aborts on
mismatch — see the project history for the exact `huggingface_hub.hf_hub_download` +
`snapshot_download` invocation used). The silero VAD is fetched from the official tagged
release and is *not* manifest-pinned (it is the VAD, not a licence-bearing conversational
model); record its sha256 when you cache it. After downloading, prove the stack actually
loads (not just that the files exist):

```bash
export HERMES_VOIP_STT_MODEL_DIR="$VOIP_MODELS_ROOT/stt"
export HERMES_VOIP_TTS_MODEL="$VOIP_MODELS_ROOT/tts"
export HERMES_VOIP_TTS_VOICE=0
export HERMES_VOIP_INJECTION_GUARD_MODEL_DIR="$VOIP_MODELS_ROOT/guard"
export HERMES_VOIP_VAD_MODEL_DIR="$VOIP_MODELS_ROOT/vad"

uv run python - <<'PY'
import os
from hermes_voip.config import load_media_config
from hermes_voip.providers.build import build_providers   # runs the licence gate
from hermes_voip.media.vad import load_silero_model
p = build_providers(load_media_config(os.environ))
print("ASR", type(p.asr).__name__, "TTS", type(p.tts).__name__, "GUARD", type(p.guard).__name__)
print("VAD silence prob", load_silero_model(16000)(b"\x00\x00"*512, 16000))
PY
# expect: ASR SherpaOnnxASR TTS SherpaKokoroTTS GUARD OnnxInjectionGuard  + a small VAD prob
```

The licence gate inside `build_providers` re-verifies each self-host default model's SPDX
against the per-family allow-list (`manifest.validate_manifest`); a banned/altered model
raises `LicenceError`.

## 3. Wire the LLM backend (the agent's brain)

The agent talks to an LLM through an OpenAI/OpenRouter-compatible proxy. The credential +
base URL go ONLY into Hermes' gitignored secrets file (`~/.hermes/.env`), fetched from the
session env or 1Password — never printed or committed. Choose the provider that matches the
proxy's model-id style:

- **OpenAI provider (`openai-api`)** — for a proxy serving bare OpenAI model ids (`gpt-5.5`,
  `gpt-4o`, …). The base URL **must include `/v1`** (this provider does not append it); key
  env `OPENAI_API_KEY`, base-URL env `OPENAI_BASE_URL`. This is the current configuration
  (model `gpt-5.5`), using the session's `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (which
  address the same proxy):

  ```bash
  # Values come from the session env; never printed. The base URL needs the /v1 suffix.
  mkdir -p ~/.hermes
  {
    echo "OPENAI_API_KEY=$ANTHROPIC_AUTH_TOKEN"
    echo "OPENAI_BASE_URL=${ANTHROPIC_BASE_URL%/}/v1"
  } >> ~/.hermes/.env
  chmod 600 ~/.hermes/.env
  uv run hermes config set provider openai-api
  uv run hermes config set model gpt-5.5
  # Optionally also pin model.base_url in config.yaml (the provider reads OPENAI_BASE_URL too).
  ```
  > Hermes' provider name for a custom OpenAI-compatible endpoint is **`openai-api`** (not
  > `openai`). Verify the resolution without printing the key:
  > `uv run python -c "import hermes_cli.runtime_provider as r; d=r.resolve_runtime_provider(requested='openai-api', target_model='gpt-5.5'); print(d['provider'], d['base_url'], bool(d['api_key']))"`
  > → `openai-api  https://…/v1  True`.

- **OpenRouter provider (`openrouter`)** — for a proxy serving OpenRouter-style `vendor/model`
  ids (`nousresearch/hermes-4-70b`, `anthropic/…`). Key env `OPENROUTER_API_KEY`, base-URL
  override env `OPENROUTER_BASE_URL` (include `/v1`). The `LLAP Hermes mbp018 Provider Key`
  1Password item (vault `Claude API Access`) holds such a key (`credential` field) + `hostname`.

Smoke-test the chosen backend (a one-line completion returns `OK`, proving key + base URL):

```bash
# OpenAI-provider example (gpt-5.5 via the session proxy; token via VAR, never printed):
curl -sS -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","max_tokens":8,"messages":[{"role":"user","content":"Reply with OK"}]}' \
  "${ANTHROPIC_BASE_URL%/}/v1/chat/completions"
```

> **If no LLM key is available, that is a hard blocker** — the operator must supply an
> OpenAI/OpenRouter/Anthropic-compatible key; do not fabricate one.

## 4. Enable the plugin in the Hermes runtime

Hermes gates pip-installed (entry-point) plugins behind `config.yaml` `plugins.enabled`. The
`hermes plugins enable` CLI validates only filesystem/bundled plugins, so add the entry-point
plugin to the config directly:

```bash
uv run python - <<'PY'
import pathlib, yaml
p = pathlib.Path.home()/".hermes"/"config.yaml"
cfg = yaml.safe_load(p.read_text()) or {}
en = cfg.setdefault("plugins", {}).setdefault("enabled", [])
if "hermes-voip" not in en: en.append("hermes-voip")
cfg["plugins"].setdefault("disabled", [])
p.write_text(yaml.safe_dump(cfg, sort_keys=False))
print("plugins.enabled =", cfg["plugins"]["enabled"])
PY
```

Confirm the runtime discovers, loads, and registers the platform:

```bash
uv run python - <<'PY'
from hermes_cli.plugins import PluginManager
PluginManager().discover_and_load()
from gateway.platform_registry import platform_registry
from gateway.config import Platform
print("voip registered:", platform_registry.is_registered("voip"), "->", Platform("voip"))
PY
# expect: voip registered: True -> Platform.VOIP
```

## 5. Provide the SIP credentials (env)

Fetch the SIP values from 1Password into the process environment (NAMES below; values never
printed). `HERMES_SIP_PASSWORD` is the **VoIP-section `Password`** (the SIP-TLS digest secret),
selected by its field id to disambiguate it from the top-level portal `password`.

```bash
SIP_ITEM="${HERMES_SIP_OP_ITEM:?set to the 1Password SIP-extension item title}"
SIP_VAULT="${HERMES_SIP_OP_VAULT:-Aperim}"
export HERMES_SIP_HOST="$(op item get "$SIP_ITEM" --vault "$SIP_VAULT" --format=json \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print([u['href'] for u in d['urls'] \
    if u.get('label')=='website'][0].split('://')[1].rstrip('/'))")"
export HERMES_SIP_EXTENSION="$(op item get "$SIP_ITEM" --vault "$SIP_VAULT" --fields label=Extension)"
export HERMES_SIP_USERNAME="$HERMES_SIP_EXTENSION"
export HERMES_SIP_PASSWORD="$(op item get "$SIP_ITEM" --vault "$SIP_VAULT" \
  --fields 'VoIP.Password' --reveal)"   # the VoIP-section Password (the SIP-TLS digest secret;
                                        # 'VoIP.Password' = <section>.<label>, which disambiguates
                                        # it from the item's top-level portal `password` field)
export HERMES_SIP_PORT=5061
export HERMES_SIP_TRANSPORT=tls
```

Required keys (`src/hermes_voip/config.py`): `HERMES_SIP_HOST`, `HERMES_SIP_EXTENSION`,
`HERMES_SIP_PASSWORD` (+ optional `HERMES_SIP_USERNAME`, `HERMES_SIP_PORT`,
`HERMES_SIP_TRANSPORT`, `HERMES_SIP_EXPIRES`, `HERMES_SIP_USER_AGENT`). For multiple
simultaneous registrations use the indexed scheme `HERMES_SIP_EXTENSION_<n>` /
`HERMES_SIP_PASSWORD_<n>` and set `HERMES_SIP_DEFAULT_EXTENSION` for the inbound default.

> Verify the env loaded **without** printing values: `printenv | grep -c '^HERMES_SIP_'`
> (expect ≥ 3). Never `printenv HERMES_SIP_PASSWORD`.

## 6. Launch + REGISTER on the gateway

With every variable from steps 2/3/5 exported in the same shell, launch the gateway. The
plugin's `register(ctx)` supplies an `env_enablement_fn` that seeds `PlatformConfig.extra`
from the `HERMES_SIP_*` / `HERMES_VOIP_*` process env (secrets stay in env, never
config.yaml), and an `is_connected` gate, so the gateway enables `voip`, instantiates the
adapter, and calls `connect()` (TLS handshake + REGISTER) on its own — no `platforms:` block
in config.yaml is required.

**Detached launch (persists across turns; the inbound call arrives asynchronously):**

```bash
# All of steps 2/3/5's exports must be live in THIS shell first.
nohup uv run hermes gateway run -vv > /tmp/hermes-voip-live.log 2>&1 &
echo "gateway PID=$!  log=/tmp/hermes-voip-live.log"
```

Confirm in the log that the `voip` platform loaded and the extension REGISTERED:

```bash
grep -E "voip|REGISTER|Connecting to voip|registered" /tmp/hermes-voip-live.log | tail -20
```

**Registration-only check (no full gateway)** — drive the adapter directly:

```bash
uv run python - <<'PY'
import asyncio, os, logging
logging.basicConfig(level=logging.INFO)
import hermes_voip
from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry


```bash
uv run python - <<'PY'
import asyncio, os, logging
logging.basicConfig(level=logging.INFO)
import hermes_voip
from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

class _Ctx:
    def register_platform(self, name, label, fac, chk, validate_config=None,
                          required_env=None, install_hint="", **k):
        platform_registry.register(PlatformEntry(
            name=name, label=label, adapter_factory=fac, check_fn=chk,
            validate_config=validate_config, required_env=required_env or [],
            install_hint=install_hint, source="plugin", plugin_name="hermes-voip", **k))
hermes_voip.register(_Ctx())

extra = {k: v for k, v in os.environ.items()
         if k.startswith(("HERMES_SIP_", "HERMES_VOIP_"))}
adapter = platform_registry.create_adapter("voip", PlatformConfig(enabled=True, extra=extra))

async def main():
    up = await adapter.connect()          # TLS handshake + REGISTER (401 -> digest -> 200 OK)
    for s in adapter._manager.snapshot():
        print(f"ext={s.extension} registered={s.registered} expires={s.expires}s")
    print("REGISTERED" if up else "NOT REGISTERED — see logs for the SIP/TLS error")
    if up:
        print("Listening for an inbound INVITE. Dial the extension now. Ctrl-C to stop.")
        try:
            await asyncio.Event().wait()
        finally:
            await adapter.disconnect()
asyncio.run(main())
PY
```

## 7. Verify — registration succeeded

**Success looks like:** `connect()` returns `True`, and `snapshot()` prints
`registered=True` with a non-zero `expires` (e.g. `expires=299s`) for the extension. That
proves, live: the TLS handshake to the gateway, the `REGISTER` transaction, and the digest
auth challenge/response (`401` → `200 OK`) all work. The process then waits for an inbound
`INVITE`.

**If registration FAILS, capture the exact signal** (most valuable for debugging):
- A `RuntimeError`/`ssl.SSLError` at `connect()` → TLS/connectivity problem (wrong host/port,
  cert chain, firewall). Record the exception text.
- `registered=False` after the timeout → the registrar rejected the `REGISTER`. Raise the
  transport/manager loggers to `DEBUG`
  (`logging.getLogger("hermes_voip.transport.connection").setLevel(logging.DEBUG)` and
  `"hermes_voip.manager"`) and record the **SIP response code** (`401` repeating = bad digest;
  `403` = forbidden / wrong realm; `404` = unknown AOR; `423` = interval too brief, handled
  automatically). The code + reason phrase is the debugging key.

## 8. The test call — two-way audio

1. From any phone able to reach the gateway, **dial the registered extension** (the value of
   `HERMES_SIP_EXTENSION` for this item). Do **not** place an outbound call from the plugin.
2. The plugin answers the inbound `INVITE` (sends `200 OK` with an SDP answer), opens the RTP
   (or SDES-SRTP) media path, and starts the per-call loop.
3. **Speak.** On end-of-utterance (silero VAD endpointing), the transcript is screened by the
   injection guard, routed to the Hermes agent as a `VOICE` message, and the agent's reply is
   synthesised by the TTS provider and played back over RTP.
4. **Success = a real two-way conversation:** you hear the agent's spoken reply to what you
   said, and follow-up turns work. Hang up to end the call; the loop tears down the RTP engine
   and in-dialog routes (no leaked socket).

### 8a. Troubleshooting — call answers but there is no audio

The plugin now logs the full inbound path at `INFO` (`hermes_voip.adapter`):
`INVITE received` → `SDP offer` → `SDP answer built` → `200 OK sent (To-tag …)` →
`CallSession registered (dialog_id …)` → `CallLoop started`. A fire-and-forget
handler failure is logged at `ERROR` **with its traceback**. Read these first.

- **The caller's ACK/BYE log as `out-of-dialog`:** the `200 OK` is missing its
  dialog `To`-tag (RFC 3261 §12.1.1). Fixed — the answer now carries
  `to_tag=local_tag`, matching the registered `dialog_id`. If it recurs, confirm
  the `200 OK sent (To-tag …)` line shows a non-empty tag.
- **The `SDP answer built` line shows `127.0.0.1`:** the answer is advertising
  loopback and no RTP can arrive. Fixed — the RTP host is derived from the
  transport's local interface (same host as the SIP `Contact`).
- **The runtime is behind NAT (private interface) and the SDP advertises a
  private RTP address:** a public gateway cannot reach a private address unaided.
  This is a media-reachability concern beyond the loopback fix. Resolve in this
  order:
  1. **Symmetric-RTP latching (preferred, vendor-neutral):** learn the peer's
     real source `(IP, port)` from the first inbound RTP packet and send our RTP
     back to that tuple, ignoring a private/incorrect SDP address. Survives NAT
     and SBC rewriting; a media-engine change, not signalling.
  2. **Outbound greeting on answer (complementary):** speak a short greeting
     immediately after the `200 OK` so we send RTP first — opens the NAT pinhole
     and gives a symmetric-RTP gateway our address to latch onto. Helps, but is
     **not sufficient alone** if the gateway honours the SDP address literally.
  3. **Correct public address in the SDP** (rport/STUN/configured external IP) is
     the alternative; latching is the more robust default.

## 9. Teardown

- Stop the validation process / gateway with `Ctrl-C` (the driver above calls
  `adapter.disconnect()`, which cancels call loops, closes the manager and the TLS transport).
- The registration expires on the gateway after the `expires` window if the process dies
  without a clean un-register.
- Unset the SIP/LLM env vars from the shell (`unset HERMES_SIP_PASSWORD ...`). The cached model
  files under `$VOIP_MODELS_ROOT` are gitignored and safe to keep or delete.

## Rotate / recreate credentials

- **SIP secret:** change the extension's secret in the gateway admin UI → update the VoIP-section
  `Password` field of that SIP-extension 1Password item → re-export
  `HERMES_SIP_PASSWORD` (and the repo-root `.env` if used) → re-run step 6. See runbook
  `0001-sip-extension-credentials.md`.
- **LLM proxy key:** mint a replacement LLAP key, update the `credential` field of the
  `LLAP Hermes mbp018 Provider Key` item, rewrite `OPENROUTER_API_KEY` in `~/.hermes/.env`,
  then revoke the old key (rule 41).
- Never echo, log, or commit any rotated value.
