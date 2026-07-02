# Runbook: VoIP incident diagnosis and on-call response

**What it is.** Troubleshooting flowchart and response playbook for an on-call operator when the
`hermes-voip` plugin fails in production. This runbook covers the most common symptoms, the
diagnostics to prove root cause, and the immediate remediation steps. For preventive metrics and
SLO signals, see runbook [`0014-voip-slo-metrics.md`](0014-voip-slo-metrics.md).

This is a **present-tense operational HOW** (rule 27): it describes what IS, not aspirational
features. Instrument commands and log strings are verified against the current code and changed
only when the code changes.

> **Secrets are NAMES only.** Never write real host/extension/password/IP in a runbook — use
> env-var names and 1Password item titles only (rule 34).

## First response checklist (< 5 min)

1. **Is the process alive?**
   ```bash
   ps aux | grep -E "hermes gateway|hermes_voip" | grep -v grep
   # If no output → process is down; go to "Restart" section.
   ```

2. **Is the plugin registered on the gateway?** (SIP-over-TLS extension only)
   ```bash
   grep "SIP registration established" /path/to/hermes/log | tail -1
   # Expected: "SIP registration established (expires 300s)" (or similar TTL).
   # Missing or old timestamp (> 5 min) → registration may have expired; check below.
   ```

3. **Are there active errors in the last 100 log lines?**
   ```bash
   tail -100 /path/to/hermes/log | grep -E "ERROR|CRITICAL"
   # Common errors are covered below.
   ```

4. **Can users call the extension?** (manual test)
   - **Inbound:** try to dial the extension from any phone on the gateway.
     - Call rings / routes → probably OK (move to call-quality checks).
     - Call goes to voicemail or gets busy → possible registration loss or internal error.
   - **Outbound:** if enabled, agent tries `place_call` tool → check the error response below.

If the answer to #1 is "process is down," skip to **[Restart](#restart)** and do not
troubleshoot further until the process is back up.

---

## Symptom: Registration down / extension not registered

**Check these before restarting.**

### 1. Is the process actually running?

```bash
ps aux | grep hermes | grep -v grep
# If the process is present, skip to step 2.
# If the process is gone → go to Restart section.
```

### 2. Are SIP credentials loaded?

```bash
printenv | grep -c '^HERMES_SIP_'
# Expect: ≥ 3 (HOST, EXTENSION, PASSWORD at minimum).
# If 0 or <3 → env vars are not set; see "Rotate / restart" below.
```

### 3. Check the registration log

```bash
grep -E "SIP registration established|registered=|REGISTER|401|403|404" \
  /path/to/hermes/log | tail -20
```

**What each response means:**
- `SIP registration established (expires 300s)` → **registration succeeded** (good).
- Repeating `401` (digest failure) → wrong password OR wrong password field (see
  [`0001-sip-extension-credentials.md`](0001-sip-extension-credentials.md) VoIP-section note).
- `403` (Forbidden) → wrong realm or extension not enabled on gateway.
- `404` (Not Found) → extension does not exist on the gateway.
- `423` (Interval Too Brief) → registration lifetime too short; the plugin retries
  automatically with a longer interval.
- `Connection refused` / `unreachable` → SIP host or port is wrong, or firewall is blocking
  outbound to the gateway's SIP-TLS port (default 5061).

### 4. Check for TLS handshake errors

```bash
grep -E "ssl.SSLError|TLS|certificate|handshake|Connecting to voip" \
  /path/to/hermes/log | tail -10
```

**Common TLS failures:**
- `certificate verify failed` → gateway's TLS cert is self-signed or expired; the plugin
  verifies peer certs (rule 34). Confirm the cert with the gateway owner.
- `Connection refused` → the host or port is wrong, or the gateway is not listening.

### 5. Check extension & SIP config syntax

```bash
# Verify the plugin loaded the config without errors.
# This is a _fast_ check (no network, no gateway contact):
uv run python -c \
  "import os; from hermes_voip.config import load_gateway_config; \
   c=load_gateway_config(os.environ); \
   print('Loaded:', c.transport, 'to', c.host, ':', c.port, '| extensions:', [e.extension for e in c.extensions])"
# Expected: e.g. "Loaded: tls to pbx.example.test : 5061 | extensions: ['1137']"
# If ConfigError is raised → a required key is missing or malformed.
```

### 6. Registrar granted a non-positive or malformed `Expires` (ADR-0088)

**Current behaviour:** RFC 3261 §10.3 lets a registrar grant a *shorter* lifetime than
requested, including `0` (an unrequested de-registration), and a malformed/negative
`Expires` on our echoed `Contact` is also possible. `RegistrationFlow._handle_success`
(`src/hermes_voip/registration.py:496`) treats either case as a registration **failure** —
never a silent `Registered(expires=0)` — so `RegistrationManager` never arms a 0-delay
refresh that would hot-loop the registrar. The failure routes through
`_on_registration_failed` (`src/hermes_voip/manager.py:590`), which emits the same
structured WARNING as any other registration failure — `event=sip_registration_failed`,
`outcome="rejected"`, `attempt=<1-based counter>` — but with a **synthetic**
`status_code=0` that can never collide with a real SIP status (1xx–6xx), unambiguously
marking this specific anomaly.

**To observe in logs:**

```bash
jq 'select(.event=="sip_registration_failed" and .status_code==0) | {outcome, attempt}' \
  /path/to/hermes/log.jsonl
# Plain-text log (no jq): the WARNING line reads
#   "SIP registration failed: rejected (status=0, attempt=N)"
```

**What it means:** the registrar answered our REGISTER with a `2xx` but the `Expires` on
OUR `Contact` binding was non-positive or unparseable — it did not actually keep the
binding alive, even though it did not send a real rejection status. This is a
**registrar-side** signal (a gateway/ACL/binding-policy issue), not a plugin defect; the
plugin never logs the registrar's free-text reason for this event (rule 34), so
cross-reference the gateway's own registration logs by timestamp, not by message text.

**Operator action:** if the extension was previously **established**, the existing
bounded-backoff re-REGISTER ramp (same recovery path as step 3 above) retries
automatically and usually self-heals once the registrar starts granting a positive
lifetime again. A **cold-start** non-positive/malformed grant is reported but not
auto-retried — fix the gateway-side binding/ACL issue for this extension, then restart
(see **Restart** below).

---

## Symptom: No inbound calls arriving

**Registration is UP (extension logs show `registered=True`), but no calls ring.**

### 1. Check the gateway's extension routing

- Log in to the gateway's admin UI and verify:
  - The extension is **enabled** (not suspended).
  - Inbound routes to the extension are active.
  - Call forwarding is not active (would route calls elsewhere).

### 2. Check transport mismatch

- **SIP-over-TLS:** the plugin registers on the TLS port (default 5061). The gateway must route
  inbound calls to the TLS transport, not WebSocket or plain UDP.
  - Log: `registered=True` in the adapter snapshot means the TLS registration worked.
  - If the gateway has per-transport call routing and routes calls to a **different** transport,
    the call will not arrive at the plugin.

- **WebRTC (WSS):** WSS signalling IS wired (ADR-0038). `WssSipTransport` is selected in
  `adapter.py` when `gateway_cfg.transport == "wss"`, and inbound INVITEs over WSS reach
  `_on_inbound_invite` identically to TLS (the `is_webrtc` branch handles the WebRTC-specific
  media path). If WSS inbound calls are not arriving, verify the gateway routes the INVITE to
  the WSS Contact registered by the plugin.

**Check what the gateway sees:** log in to the gateway and inspect the extension's transport
registration on the SIP/TLS edge. It must show the plugin's Contact address (the IP and port it
registered from).

### 3. Gateway behind NAT

If the gateway is behind a NAT (private network), verify:
- The gateway's **outbound** NAT rules allow the plugin's inbound INVITE to reach the plugin's
  private RTP address (comedia symmetric-RTP latching compensates for **inbound** media NAT,
  not **signalling** NAT). This is a gateway/network configuration issue.
- Confirm the gateway can route back to the plugin's SIP address (the registered Contact).

### 4. Callers get a busy signal under load (admission cap — ADR-0059)

```bash
grep "REJECTED 486 Busy Here — at concurrent-call cap" /path/to/hermes/log | tail
# Each line is a NEW inbound INVITE rejected because the line was already at the
# HERMES_SIP_MAX_CALLS concurrent-call cap (default 8). This is BY DESIGN — the cap
# protects the host from a per-call-pipeline (RTP+STT+TTS+AEC+VAD) resource
# exhaustion under burst/flood.
```

- If this is expected load, raise `HERMES_SIP_MAX_CALLS` to the host's pipeline
  budget (each call is one full media pipeline — size it to CPU/memory headroom) and
  restart.
- If it is a flood/abuse, the cap is doing its job; investigate the source at the
  gateway. The line stays up for legitimate calls as slots free.

---

## Symptom: Call answers but no audio / one-way audio

**The plugin's `200 OK` is sent, and the call connects, but no RTP is flowing either direction
or only one direction works.**

### 1. Check for RTP media startup

```bash
grep -E "rtp:|RTP|media engine|audio RTP" /path/to/hermes/log | tail -20
```

Look for these indicators of RTP stream startup:
- `SDP answer built — local RTP <addr>:<port>, codecs <list>` → the answer was built correctly.
- `rtp: latched to <ip>:<port>` → our side received the first inbound packet and learned the
  peer's real address (comedia symmetric-RTP, shipped on by default).
- Missing → RTP stack initialization failed (check for exceptions near the `200 OK` log line).

### 2. Check the SDP answer

```bash
grep "SDP answer built" /path/to/hermes/log | tail -1
```

**Troubleshoot each part:**

- **Local RTP address `127.0.0.1`?** → **loopback is advertised** (RTP cannot arrive).
  - Fixed in recent versions. If it recurs: the transport's local interface detection failed.
    Restart the process.

- **Codecs missing or mismatched?** → the gateway offered codecs the plugin doesn't support.
  - `codecs G722,telephone-event` → G.722 wideband (good; full quality).
  - `codecs PCMU,telephone-event` → G.711 µ-law (fallback; narrowband 8 kHz).
  - `codecs <something else>` → unsupported codec; the call should have been rejected with `488
    Not Acceptable Here`, not answered. Check the log for `REJECTED 488`.

### 3. Check the first RTP packet

```bash
grep -E "rtp tx: first packet|rtp rx: first packet" /path/to/hermes/log | tail -5
```

Expected output:
```
rtp tx: first packet pt=9 <info>  (pt=9 is G.722; pt=0/8 is G.711)
rtp rx: first packet <- <gateway-media-ip>:<port> (symmetric-RTP latch address)
```

- **Missing `rtp tx`?** → TTS synthesis failed or the greeting was disabled. Check the previous
  line for `greeting: failed` or `speak() failed` errors.
- **Missing `rtp rx`?** → the gateway is not sending media, or the media is not reaching our
  RTP port. Check:
  - The SDP answer advertises the correct RTP port (the `local RTP <addr>:<port>` line).
  - The gateway can reach that port (firewall, NAT).
  - The gateway's media address (the `rtp rx: first packet <- <ip>` source) matches the
    gateway's registered Contact address or the SDP offer's media address.

### 4. One-way audio: inbound only / outbound only

- **Caller hears agent, agent hears nothing** (outbound dead):
  - Check `rtp tx: first packet` is logged. If missing → no TTS sent.
  - Check for STT / ASR errors in the loop logs.
  - Agent may be silent (ASR returned empty utterance) or provider (LLM) failed (see
    **[Provider unreachable](#symptom-provider-llmstttts-unreachable--502-storms)** section).

- **Agent hears caller, caller hears nothing** (inbound dead):
  - Likely a media-direction firewall issue (outbound UDP from RTP port is blocked).
  - The plugin receives inbound RTP from the gateway (`rtp rx:` logs) but cannot send back.
  - Check: the system's firewall allows outbound UDP on the RTP port to the gateway's media
    address. On Linux, `sudo iptables -L -n | grep -E "RTP|5000|media"`.

### 5. SRTP (encrypted media) issues

If the SDP offer carries `m=audio … RTP/SAVP` + an `a=crypto` line (SDES-SRTP), the plugin now
answers SRTP (ADR-0053 Stage 1). If the `SDP answer built` line shows:
- `RTP/SAVP` + `a=crypto:<tag> AES_CM_128_HMAC_SHA1_80 inline:<key>` → SRTP negotiated correctly.
- Still `RTP/AVP` (no SAVP, no crypto) → offer was plain RTP, not SRTP (gateway did not demand it).

**Encrypted audio but still silent or garbled?**
- The crypto keys must differ between our answer and the offer (each direction uses the
  sender's key). Confirm with:
  ```bash
  grep "a=crypto" /path/to/hermes/log | tail -2
  # Expect two different inline keys.
  ```
- If keys are identical or only one crypto line exists → SRTP keying failed. Restart the
  process and retry.

---

## Symptom: Call answers, agent replies normally, then conversation hangs

**Call setup looks good, first turn works, but then the agent goes silent or takes > 30 s to
respond.**

### 1. Check the conversation loop logs

```bash
grep -E "conversation_loop|Turn ended|interrupted|API call" /path/to/hermes/log | tail -20
```

Look for:
- `Turn ended: reason=interrupted_during_api_call` → **agent's answer was cut off by a barge-in
  or echo**. See **[Self-interruption / gateway echo](#symptom-self-interruption--gateway-echo)**
  below.
- `API call failed` / `502` / `timeout` → **provider failure**. See **[Provider
  unreachable](#symptom-provider-llmstttts-unreachable--502-storms)** below.
- `RuntimeError` / `OutboundCallFailed` → **outbound-call infrastructure error** (if the agent
  tried to place a call). See logs for the exact error.

### 2. Check for VAD / STT errors

```bash
grep -E "ASR|STT|VAD|endpointing|silence" /path/to/hermes/log | tail -20
```

- `No speech detected` → the caller is silent, or the VAD threshold is too high. Check the line
  `VAD silence probability: <0-1>` (lower = more speech; VAD rejects if > 0.5 by default).
- `ASR timeout` → speech was detected but transcription took too long. If this repeats, the STT
  provider is slow (see provider section).

### 3. Check the LLM response time

```bash
grep -E "LLM|provider|api|completion|took|latency|ms" /path/to/hermes/log | tail -20
```

- **First turn works, second turn times out?** → likely **LLM backend overload** or **session
  context too large**. The agent's context grows with each turn; after many turns, the LLM
  request becomes expensive. Check:
  - `OPENAI_BASE_URL` / `OPENROUTER_API_KEY` reachable (`curl -I` the base URL).
  - LLM provider status page (OpenAI, OpenRouter, Anthropic, etc.).
  - Session context size (each turn adds ~500 tokens). If the conversation has > 50 turns, the
    context is large. No setting currently trims it; long sessions are expensive.

---

## Symptom: Self-interruption / gateway echo

**Symptom: the agent starts to speak, then immediately gets cut off (agent replies: "Hello, this
is…" then silence). Log shows `Turn ended: reason=interrupted_during_api_call`.** The agent is
hearing its own voice reflected by the gateway and interrupting itself.

### 1. Diagnose the echo source

```bash
grep -E "rtp tx: first packet|rtp rx: first packet" /path/to/hermes/log | head -5
```

Compare the TX and RX addresses:
- Both from the **gateway's media address** → **external echo** (gateway/PSTN reflection).
- RX from **our own local RTP address** → **self-loopback** (plugin's own outbound reaching
  back in). Rare; the engine drops self-loopback at `rtp rx: dropping inbound packet with our
  own SSRC 0xCAFEBABE` (DEBUG level).

### 2. Echo-robust barge-in (shipped, on by default — ADR-0023)

The plugin already compensates for external echo via:
- **Gate:** sustained-speech requirement before interruption (default `HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS=600` ms). Echo bursts < 600 ms do not interrupt.
- **Audio withholding:** echoed audio is withheld from STT while the agent is speaking, so the
  echo is never transcribed. Log: `pump: withholding echo audio from ASR at window N …`
  (DEBUG).

### 3. If echo still interrupts

Check current barge-in settings:

```bash
grep -E "BARGE_IN|withholding|interrupted" /path/to/hermes/log | tail -10
```

**Adjust the echo gate (in order of effect):**

| Setting | Default | Increase if | Decrease if |
|---------|---------|-------------|-------------|
| `HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS` | 600 | echo still slips through | real barge-in is sluggish |
| `HERMES_VOIP_BARGE_IN_TAIL_MS` | 250 | late echo arrives after agent stops | unnecessary latency after TTS ends |
| `HERMES_VOIP_BARGE_IN_MODE` | `gated` | (not recommended) | `off` to disable barge-in entirely on echo-free lines |

Set these in the env before launching the gateway:

```bash
export HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS=800
export HERMES_VOIP_BARGE_IN_TAIL_MS=300
# Restart the gateway: kill the process and relaunch.
```

**If the gateway has its own echo cancellation,** disable our gate:

```bash
export HERMES_VOIP_BARGE_IN_MODE=full
# Restart.
```

---

## Symptom: Provider (LLM/STT/TTS) unreachable / 502 storms

**Call setup works, the agent is called, but the caller hears the apology
"Sorry, I'm having trouble right now. Please bear with me." instead of a real answer.**

Since ADR-0063 the plugin NEVER reads a raw backend error aloud: when an unrecoverable
provider error arrives as the agent's reply (an HTTP 502/503, a provider error class, a
stack trace), `VoipAdapter.send()` speaks a short safe apology
(`hermes_voip.provider_error.safe_error_reply`, language-aware) and logs the REAL error at
WARNING with secrets redacted. So the caller-facing symptom is the apology; the diagnosis
lives in the log, not in what the caller heard. (A caller hearing a literal "502 Bad Gateway"
means an OLD build predating ADR-0063 — upgrade.)

### 1. Check which provider failed

```bash
# The real provider error is logged at WARNING by the adapter (caller heard only the
# apology). The "real error:" tail carries the HTTP status / provider class, redacted.
grep -E "provider/runtime error reply|provider|502|overloaded|failed|timeout|unreachable|ConnectionError" \
  /path/to/hermes/log | tail -20
```

Providers in order of likelihood:
1. **LLM** (agent's brain) — `/v1/chat/completions` to `OPENAI_BASE_URL` / `OPENROUTER_API_KEY`.
2. **STT** (speech-to-text) — e.g., Deepgram, self-hosted sherpa-onnx.
3. **TTS** (text-to-speech) — e.g., ElevenLabs Cloud, self-hosted Kokoro.
4. **Guard** (injection filter) — ONNX model (self-hosted; always available if models were downloaded).

### 2. LLM backend check

```bash
# Substitute your actual OPENAI_BASE_URL or OPENROUTER_API_KEY endpoint.
# Never print the key; check reachability only.

# OpenAI/proxy example:
curl -sS -I "${ANTHROPIC_BASE_URL%/}/v1/chat/completions" \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "Content-Type: application/json"
# Expect: HTTP/2 200 or HTTP/1.1 200 (options request).
# If: 502, 503, Connection refused, timeout → provider is down or key is invalid.

# OpenRouter example:
curl -sS -I "https://openrouter.ai/api/v1/chat/completions" \
  -H "Authorization: Bearer ${OPENROUTER_API_KEY:?not set}"
# Expect: HTTP/1.1 200 or HTTP/2 200.
```

Check the LLM provider's **status page**:
- OpenAI: https://status.openai.com
- OpenRouter: https://openrouter.ai (check their API docs for status).
- Anthropic: https://status.anthropic.com

### 3. STT backend check (if self-hosted)

```bash
# Self-hosted Sherpa-ONNX (no network; loaded at startup).
# If the model files are missing, the plugin fails to load.
ls -la "$HERMES_VOIP_STT_MODEL_DIR"
# Expect: encoder.onnx, decoder.onnx, joiner.onnx, tokens.txt (all present).
# Missing files → see runbook 0002 §2 "Download + verify self-hosted models."
```

External STT (e.g., Deepgram):
```bash
# Check Deepgram reachability (substitute your key).
curl -sS -I "https://api.deepgram.com/v1/listen" \
  -H "Authorization: Token ${DEEPGRAM_API_KEY:?not set}"
# Expect: HTTP/1.1 400 or 200 (OPTIONS works).
# If: 502, timeout → Deepgram is down.
```

### 4. TTS backend check

```bash
# ElevenLabs Cloud:
curl -sS -I "https://api.elevenlabs.io/v1/text-to-speech" \
  -H "xi-api-key: ${ELEVENLABS_API_KEY:?not set}"
# Expect: 405 (POST required) or 200.
# If: 502, 429 (rate limit), timeout → ElevenLabs is down or key is invalid.

# Self-hosted Kokoro (no network; loaded at startup).
ls -la "$HERMES_VOIP_TTS_MODEL"
# Expect: model.onnx, voices.bin, tokens.txt, espeak-ng-data/ (all present).
# Missing → see runbook 0002 §2.
```

### 5. If the provider is down

**Immediate action:** are you in an SLA-critical period? If yes, escalate to the operator.

**Fallback:**
- **TTS has a fallback.** ElevenLabs Cloud primary is configured to fall back to self-hosted
  Kokoro on failure. If Kokoro is not configured, the agent stays silent.
  - Ensure `HERMES_VOIP_TTS_FALLBACK=sherpa-kokoro` and `HERMES_VOIP_TTS_FALLBACK_MODEL` is set to
    the Kokoro model root.
- **LLM has no fallback.** A provider outage is an outage.
- **STT has no fallback** (currently). If Deepgram is down, calls cannot transcribe speech.

### 6. Provider error handling — ADR-0063 (shipped)

**Current behaviour:** transient provider errors (e.g., `502 Bad Gateway`,
`overloaded_error`) are **NOT spoken to the caller**. ADR-0063 shipped
`src/hermes_voip/provider_error.py` and an intercept in `adapter.py`
(`_deliver_content`, ~line 1435): when `is_provider_error()` matches the LLM
reply, the adapter replaces it with a safe apology phrase
(`resolve_error_apology()`) before calling `loop.speak()`. The raw error is
logged at WARNING with structured `event=provider_error_replaced` (visible in
the log pipeline) but the caller hears only the apology.

The apology phrase is operator-overridable via `HERMES_VOIP_ERROR_APOLOGY`; a
per-language fallback is applied for non-English calls when a native phrase is
registered.

**To observe interception in logs:**

```bash
jq 'select(.event=="provider_error_replaced") | {call_id, error_category, language}' \
  /path/to/hermes/log.jsonl
```

**If you still see raw errors in calls** (should not happen): check that the
`provider_error.py` patterns cover the vendor error format. Widen `is_provider_error`
or set `HERMES_VOIP_ERROR_APOLOGY` to a fixed safe phrase and redeploy.

---

## Symptom: Wedged process / hung media or memory leak

**The process is running but not responding to calls, or memory is growing unbounded.**

### 1. Check for hung tasks

```bash
ps aux | grep hermes
# Look at the VSZ (virtual memory) and RSS (resident memory) columns.
# If RSS > 2 GB and growing → memory leak or unbounded buffer.
```

Concurrent calls + memory:
- Each active call holds ~10–50 MB (RTP engine, ASR buffers, STT context, agent session state).
- 10 concurrent calls ≈ 100–500 MB; 100 concurrent calls ≈ 1–5 GB.
- If fewer calls use much more memory → possible leak.

### 2. Check for stuck call loops

```bash
grep -E "CallLoop started|Turn ended|hung|deadlock|stuck" /path/to/hermes/log | tail -30
```

- **No "Turn ended" for a call after 30+ minutes** → the call's loop may be hung waiting on a
  provider response that never returns. No automatic timeout; the call accumulates memory.
- **Each turn says `Turn ended: reason=timeout` or `error`** but the next turn starts anyway →
  the loop is active and recovering (expected; the agent is resilient).

### 3. Graceful shutdown (BYE-drain via the host's `disconnect()` path — ADR-0059)

The plugin's `disconnect()` performs a graceful BYE-drain. It is a **library/plugin**, not a
standalone service (AGENTS rule 40) — it installs **no `SIGTERM` handler of its own**. The
**Hermes host runtime** invokes `disconnect()` on its shutdown / `aclose()` path (ADR-0059
calls this "the `aclose()`/SIGTERM path"); a typical host wires its `SIGTERM` to that
shutdown, so `kill -TERM <host-PID>` reaches `disconnect()` **only if the host is wired that
way**. When `disconnect()` runs, it:

1. **Stops accepting new INVITEs** — a racing inbound INVITE gets a `503 Service Unavailable`.
2. **Drains active calls** — sends a SIP BYE to every live call concurrently (up to
   `HERMES_SIP_SHUTDOWN_DRAIN_SECS`, default 5 s).
3. **Closes cleanly** — deregisters the extension (sends `Expires: 0`) and closes the transport.

**Recommended shutdown:**

```bash
# Use the host runtime's graceful stop (its SIGTERM path, if wired, calls disconnect()).
kill -TERM <PID>
# VERIFY the drain actually ran — look for this exact log line:
#   "graceful shutdown: draining N live call(s) with BYE (timeout 5.0s)"
# If that line does NOT appear, the host did NOT trigger disconnect() (unwired SIGTERM):
# live calls were NOT drained — treat it as a hard kill and escalate.
# Otherwise the process exits once the drain completes or times out (max 5 s + grace).
```

**If the process does not exit within ~10 seconds after SIGTERM:**

```bash
# Use hard kill as a fallback only; graceful drain has already tried.
kill -9 <PID>
# Wait 5 s, then restart.
```

A hard kill (without SIGTERM drain first) immediately terminates the process and drops all
live calls into dangling dialogs. Always use `kill -TERM` first so connected callers get a
clean in-dialog BYE.

---

## Restart

**When to restart:** process is down, registration has been dead > 5 min, or you've made a config
change.

### 1. Stop the process (if running)

```bash
ps aux | grep hermes | grep -v grep
# If found, prefer a graceful stop FIRST so live calls drain cleanly (ADR-0059):
kill -TERM <PID>
# The adapter stops accepting new INVITEs (a racing INVITE gets 503), sends a BYE
# to every live call, and waits up to HERMES_SIP_SHUTDOWN_DRAIN_SECS (default 5s)
# for the drain before deregistering and closing. Expect a log line:
#   "graceful shutdown: draining N live call(s) with BYE (timeout 5.0s)"
# Only if it does not exit within the drain window + a few seconds:
kill -9 <PID>
# Wait 5 s.
```

> **Graceful drain (ADR-0059).** A `SIGTERM`/`aclose()` no longer hard-drops live
> callers — they get an in-dialog BYE. A `kill -9` skips the drain and IS a hard
> drop, so use it only as the fallback.

### 2. Verify all env vars are set (SIP + LLM + models)

```bash
# SIP credentials (from 1Password or .env):
printenv | grep -E '^HERMES_SIP_' | wc -l
# Expect ≥ 3. If 0 → export them (see 0002 §5).

# LLM backend (from ~/.hermes/.env or session env):
printenv | grep -E '^(OPENAI_|OPENROUTER_)' | wc -l
# Expect ≥ 2. If 0 → configure LLM (see 0002 §3).

# Model directories (for self-hosted):
printenv | grep -E '^HERMES_VOIP_(STT|TTS|VAD|GUARD)' | wc -l
# Expect ≥ 3 if using self-host. If fewer and required → set them (see 0002 §2).
```

If any are missing, **do not restart** — resolve the missing config first.

### 3. Launch the gateway

```bash
# From the shell where all env vars above are exported:
nohup uv run hermes gateway run -vv > /tmp/hermes-voip.log 2>&1 &
sleep 3
ps aux | grep hermes | grep -v grep
# Confirm it is running.
```

### 4. Verify registration

```bash
sleep 5  # Give it time to register.
grep "SIP registration established" /tmp/hermes-voip.log | tail -1
# Expected: "SIP registration established (expires 300s)" or similar.
# If missing → registration failed; see **Symptom: Registration down** above.
```

### 5. Test an inbound call

- Dial the extension from any phone on the gateway.
- Confirm the agent answers and responds (first turn).
- Hang up.

If the test call works, the restart is **successful**. If it fails, go back to the relevant
symptom section above.

---

## Related runbooks

- [`0001-sip-extension-credentials.md`](0001-sip-extension-credentials.md) — SIP credential
  setup, password field disambiguation, and rotation.
- [`0002-voip-live-validation.md`](0002-voip-live-validation.md) — full end-to-end validation
  (one-time setup).
- [`0014-voip-slo-metrics.md`](0014-voip-slo-metrics.md) — SLO targets and signal definitions
  for production monitoring.
