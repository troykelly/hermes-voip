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
    host). It also holds a `Voicemail PIN` (unused here). The item's **top-level
    `password`** is the **GDMS/WAVE web-app portal login**, NOT a SIP credential: a live
    RFC 7118 REGISTER returns `401` with it on both the SIP-TLS and the Secure-WebSocket
    edges. The **WSS/WebRTC edge authenticates with the SAME VoIP-section `Password`** as
    SIP-TLS (verified 2026-06-18, ADR-0042), so `HERMES_SIP_WS_PASSWORD` is left **unset**
    for this gateway (the documented fallback reuses `HERMES_SIP_PASSWORD`).
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
| STT | `HERMES_VOIP_STT_MODEL_DIR` | `STT_MODEL_MANIFEST` repo@revision | `encoder.onnx`, `decoder.onnx`, `joiner.onnx` (the three pinned `*-epoch-99-avg-1.onnx` weights, renamed from the HF repo filenames), `tokens.txt` |
| TTS | `HERMES_VOIP_TTS_MODEL` | `TTS_MODEL_MANIFEST` repo@revision | `model.onnx` (pinned), `voices.bin`, `tokens.txt`, `espeak-ng-data/` |
| Guard | `HERMES_VOIP_INJECTION_GUARD_MODEL_DIR` | `GUARD_MODEL_MANIFEST` repo@revision | `model.onnx` (pinned, the repo's `onnx/model.onnx`), `tokenizer.json`, `config.json` |
| VAD | `HERMES_VOIP_VAD_MODEL_DIR` | `snakers4/silero-vad` tag `v5.1.2`, file `src/silero_vad/data/silero_vad.onnx` (MIT) | `silero_vad.onnx` |

**STT model history** (the active pin is always `STT_MODEL_MANIFEST` in `manifest.py`):

| Date | HuggingFace repo | revision (40-hex) | Training data | Notes |
| --- | --- | --- | --- | --- |
| 2026-06-16 (current) | `csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-21` | `9a65b6ea94c311ca770c2bf895b30f456a22d703` | LibriSpeech + GigaSpeech | telephony WER 3.2%->1.9%; LESS->LEFT error eliminated at 20 dB SNR |
| 2026-06-14 (previous) | `csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26` | `672fbf1b30579d6585301139bb363f42a0ad4a24` | LibriSpeech only | original ADR-0006 default; degraded on noisy telephony audio |

Download script (reads the pins from `manifest.py`; verifies each pinned sha256 and aborts on
mismatch -- see the project history for the exact `huggingface_hub.hf_hub_download` +
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

**Once registered, a refresh failure self-heals (ADR-0055).** A periodic refresh `REGISTER`
that the registrar rejects (`4xx/5xx/6xx`) or never answers no longer silently de-registers
the extension. The adapter logs a WARNING on `hermes_voip.adapter`
(`SIP registration error on extension *NN: … — recovering`, the extension redacted to its
last two digits) and the manager re-`REGISTER`s with bounded exponential backoff (1 s → 30 s,
±20% jitter) until the registrar accepts again. Watch for that WARNING to spot a flapping
registrar; a single line that is not followed by recovery (a new `SIP registration
established` at INFO) means the registrar is persistently rejecting — check credentials/realm.

## 8. The test call — two-way audio

1. From any phone able to reach the gateway, **dial the registered extension** (the value of
   `HERMES_SIP_EXTENSION` for this item). Do **not** place an outbound call from the plugin.
2. The plugin answers the inbound `INVITE` (sends `200 OK` with an SDP answer), opens the RTP
   (or SDES-SRTP) media path, and starts the per-call loop. On answer it immediately speaks the
   configured opening greeting (`HERMES_VOIP_GREETING`, on by default) — this sends RTP **first**
   so the caller hears the opening at once and a gateway behind NAT latches onto our source tuple
   (symmetric RTP), opening the return media path. Look for the `greeting: synthesising N chars`
   and `greeting: first RTP sent` INFO lines.
3. **Speak.** On end-of-utterance (silero VAD endpointing), the transcript is screened by the
   injection guard, routed to the Hermes agent as a `VOICE` message, and the agent's reply is
   synthesised by the TTS provider and played back over RTP. (If you talk over the greeting, a
   speech onset barges in and cancels it.)
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
  Two mechanisms work together (both shipped, both on by default); option 3 is
  the fallback for gateways that route RTP strictly by the SDP address.
  1. **Outbound greeting on answer (shipped, on by default):** the plugin speaks
     the configured greeting (`HERMES_VOIP_GREETING`) the instant the call is
     answered, so we send RTP **first** — this opens the NAT pinhole and gives a
     symmetric-RTP gateway our source tuple to latch onto. Confirm the
     `greeting: first RTP sent` INFO line appears. Set `HERMES_VOIP_GREETING=`
     (empty) to disable it.
  2. **Symmetric-RTP (comedia) latching in the media engine (shipped, on by
     default):** the engine learns the peer's real source `(IP, port)` from the
     first **valid** inbound RTP packet and sends our RTP back to that tuple,
     ignoring a private/incorrect SDP address. This is OUR half of comedia — it
     makes two-way audio work even when the peer honours its own SDP literally
     and that SDP is a private/SBC-rewritten address. Vendor-neutral; survives
     NAT and SBC rewriting; a media-engine change, not signalling. Confirm the
     `rtp: latched to <ip>:<port>` INFO line (the gateway's media address — log
     it; it is operational, not PII). Anti-spoofing: only a datagram that parses
     as RTP with the negotiated audio payload type triggers a latch, and the
     latch fires once per call and then sticks. Set `HERMES_VOIP_RTP_SYMMETRIC=`
     `false` to disable it and always honour the SDP address.
  3. **Correct public address in the SDP** (rport/STUN/configured external IP) is
     the alternative for gateways that honour the SDP address literally.

### 8b. Verify which codec the call negotiated (G.722 wideband vs G.711)

The plugin negotiates the **best available** audio codec (ADR-0005/0022): it offers
**G.722 (16 kHz wideband) first**, then **G.711 PCMU/PCMA** (the universal fallback),
then `telephone-event` (DTMF). RFC 3264 negotiation honours the gateway's preference
order, so the call uses G.722 when the gateway offers it and G.711 otherwise — no
config knob, no per-gateway tuning. To confirm which one a given call used:

```bash
# The adapter logs the agreed codec set in the SDP-answer line (INFO).
grep -E "SDP answer built|codecs " /tmp/hermes-voip-live.log | tail -5
# expect e.g.:  SDP answer built — local RTP <ip>:<port>, codecs G722,telephone-event
#         (or)  ... codecs PCMU,telephone-event        when the gateway offered only G.711
```

```bash
# The first outbound RTP packet logs the RTP payload type actually on the wire:
#   pt=9  -> G.722 wideband     pt=0 -> PCMU (G.711 µ-law)     pt=8 -> PCMA (G.711 a-law)
grep -E "rtp tx: first packet" /tmp/hermes-voip-live.log | tail -3
```

What to expect end-to-end on a **G.722** call (the wideband win):

- The SDP answer's `m=audio` line leads with payload type `9` and carries
  `a=rtpmap:9 G722/8000` (the rtpmap clock is **8000 even though the audio is
  16 kHz** — RFC 3551 §4.5.2; this is correct, not a bug).
- Each 20 ms RTP packet is **160 octets** and the **RTP timestamp advances by 160**
  per packet (the 8 kHz clock), not 320 — the engine derives this from the codec
  descriptor, so it is automatic.
- STT runs on **native 16 kHz** audio (no upsample-from-8k) and TTS is requested at
  16 kHz (ElevenLabs natively; Kokoro's 24 kHz is downsampled 24→16, not 24→8) — so
  both transcription accuracy and playback quality improve.

If a gateway you expect to support G.722 still lands on G.711, check its own SDP
**offer** (the `SDP offer` INFO line lists the codecs it offered): we can only pick
G.722 if the gateway offers it. If the gateway offers an **unsupported** codec only
(no G.722 and no G.711), the call is **rejected with `488 Not Acceptable Here`** and
logged at `ERROR` — never answered-but-dead. Nothing about codec selection requires
touching the gateway from our side.

### 8c. Troubleshooting — the agent interrupts itself / cuts off mid-reply

Symptom: the agent starts a spoken reply, then the log shows
`agent.conversation_loop: Turn ended: reason=interrupted_during_api_call`, and the ASR
finalises one- or two-word fragments of the agent's OWN answer (e.g. `'IT'`, `'UPON'`,
`'NO'`) while it is speaking — even though the caller is silent. This is **gateway echo**:
the gateway/PSTN reflects the agent's rendered TTS back on the inbound path (a 2-wire hybrid
or a gateway without echo cancellation), the VAD transcribes it as the caller, and a barge-in
ends the agent's turn — a self-interruption loop.

Diagnose the echo source first (ADR-0023):

- Compare the `rtp tx: first packet -> <addr>` and `rtp rx: first packet <- <addr>` INFO
  lines. If RX is from the **gateway's** media address (the same address TX targets), the
  echo is external (gateway reflection) — the common case. If RX were from our **own** local
  RTP address, it would be a self-loopback instead (the engine already drops inbound RTP
  carrying our own SSRC `0xCAFEBABE` before it reaches the VAD/ASR — a `rtp rx: dropping
  inbound packet with our own SSRC … (self-loopback)` DEBUG line).

Fix (shipped, on by default — ADR-0023): **echo-robust barge-in**. While the agent's TTS is
playing (and for a short tail after), a barge-in counts only when the inbound speech is a
SUSTAINED voiced run, so short echo blips cannot interrupt but a genuine sustained
interruption still does. The same gate also **withholds the echoed audio from the STT** while
the agent is speaking and the run is unauthorised, so the echo is never transcribed and so
never handed to the agent as a caller turn — closing the second self-interruption route (the
live `asr: delivering turn 'NO'` → `interrupted_during_api_call` path) on both the endpointer
and any native-`end_of_turn` STT (e.g. Deepgram Flux). The first withheld echo frame logs
`pump: withholding echo audio from ASR at window N …` at DEBUG. Controls:

- `HERMES_VOIP_BARGE_IN_MODE` — `gated` (default), `full` (legacy immediate barge-in; only
  correct on an echo-cancelled gateway), or `off` (never barge in).
- `HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS` — minimum sustained voiced speech (ms) to interrupt /
  deliver a turn during playout (default `600`, above the longest observed echo burst ≈480 ms).
  Raise it if echo still slips through; lower it for snappier intentional barge-in on a clean
  line.
- `HERMES_VOIP_BARGE_IN_TAIL_MS` — how long after the agent's TTS ends the gate keeps
  requiring a sustained run (default `250`; echo lags the TTS via the jitter buffer /
  network). `0` disarms the instant TTS ends.
- `HERMES_VOIP_BARGE_IN_FADE_MS` — clean-stop fade (ADR-0028). When a barge-in is authorised
  the engine flushes the agent's already-queued near-end audio so it goes quiet within ~1 RTP
  packet (not after the buffered TTS drains), applying a short linear fade-out over this many
  ms on the final frames so the cut is click-free. Default `30`; `0` is an instant hard cut.
  The agent does NOT speak any interruption acknowledgment — the gateway's "Interrupting… I'll
  respond…" busy-ack (and its Queued/Steered/Subagent siblings) is dropped at the `send()`
  boundary (`notice_filter.is_interruption_ack`) so it is never synthesised.

If a gateway has its own echo cancellation and you want zero added barge-in latency, set
`HERMES_VOIP_BARGE_IN_MODE=full`. To stop the agent ever being interrupted, set `off`.

### 8d. Verify SRTP media (SDES) on a SIP-over-TLS call (ADR-0053 Stage 1)

When the gateway offers secured media (`m=audio … RTP/SAVP …` + an `a=crypto` line),
the plugin now answers SRTP instead of rejecting with 488 — it mints its **own**
answer key (RFC 4568 §6.1: we encrypt outbound with our key, decrypt inbound with the
offerer's). A plain `RTP/AVP` offer is still answered plain (opportunistic).

```sh
# In the gateway/extension config, enable SRTP (SDES) on the extension so its INVITE
# offers RTP/SAVP + a=crypto, then place the test call and inspect the 200 OK SDP.
# The adapter logs the answer; the 200 OK body must show RTP/SAVP + exactly one
# a=crypto line whose inline key is OURS (not the offerer's).
```

- 200 OK SDP answer carries `m=audio <port> RTP/SAVP …` and one
  `a=crypto:<tag> AES_CM_128_HMAC_SHA1_80 inline:<our-key>`. Confirm the inline key
  differs from the one in the offer (each direction uses the sender's key).
- The agreed-codec INFO log line is unchanged; audio is two-way as in step 8.
- **In-dialog re-offer continuity (ADR-0053 Stage 1).** During the secured call,
  trigger a hold then resume from the gateway/handset (or a peer-side re-INVITE).
  Every re-INVITE SDP — the offer we send AND the 200 OK we send — must still show
  `RTP/SAVP` + exactly one `a=crypto` line; it must **never** drop to `RTP/AVP`.
  Audio must remain audible (decrypted) after resume — a downgrade or a stale key
  would surface as silence/garble. The re-offer key is fresh per offer (its inline
  key differs from the prior offer's), echoing the same tag + suite.
- If the gateway only offers DTLS-SRTP (`UDP/TLS/RTP/SAVP` + `a=fingerprint`), that
  is **ADR-0053 Stage 2**. The **media capability is built** (`sdp.build_sip_dtls_answer`
  + `sdp.negotiate_media_security` + `media/sip_dtls_session.SipDtlsMediaSession` over a
  plain-UDP `_UdpDatagramPipe`, reusing `media/dtls.DtlsEndpoint`), but the **adapter
  wiring is a separate, named activation wave** (a `_setup_sip_dtls_call` path gated on
  `HERMES_VOIP_SIP_DTLS_SRTP`). Until that wave lands, an inbound DTLS-SRTP offer still
  falls through to the SDES/plain handler — so for now use SDES (`RTP/SAVP`) for this
  validation. See §8e once Stage 2 is activated.

### 8e. Verify DTLS-SRTP media on a SIP-over-TLS call (ADR-0053 Stage 2)

Applies **after the adapter-activation wave** wires `SipDtlsMediaSession` (see §8d).
When the gateway/extension offers DTLS-SRTP (`m=audio … UDP/TLS/RTP/SAVP …` with an
`a=fingerprint` and an `a=setup`), the plugin answers DTLS-SRTP: it advertises **our**
`a=fingerprint`/`a=setup` (default `active` — the DTLS client — for an `actpass` offer;
`HERMES_VOIP_SIP_DTLS_SETUP` ∈ `{auto,active,passive}` overrides for an `actpass` offer
only), sends the 200 OK first, then runs the DTLS handshake over the RTP UDP socket and
keys SRTP from the handshake (no `a=crypto` — the master key is never in the SDP).

```sh
# Enable DTLS-SRTP on the extension/profile so its INVITE offers UDP/TLS/RTP/SAVP +
# a=fingerprint + a=setup, then place the test call and inspect the 200 OK SDP.
# Rollback switch: HERMES_VOIP_SIP_DTLS_SRTP=0 makes a DTLS offer fall through to
# SDES/plain (no DTLS answer) without a code change.
```

- 200 OK SDP carries `m=audio <port> UDP/TLS/RTP/SAVP …`, an `a=fingerprint:sha-256 …`
  (ours), an `a=setup:active|passive`, `a=rtcp-mux`, the real `c=`/port — and **no**
  `a=crypto` and **no** ICE attributes.
- The adapter logs `sip-dtls: DTLS-SRTP keyed (setup=…)` once the handshake completes;
  audio is two-way as in step 8. A fingerprint mismatch or handshake timeout ends the
  call (it does **not** fall back to plaintext).
- NAT note: there is no ICE; the media pipe latches its send destination onto the
  peer's real source on the first inbound datagram (the comedia latch), so two-way
  media works even when the offered `c=`/port is behind NAT.

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
