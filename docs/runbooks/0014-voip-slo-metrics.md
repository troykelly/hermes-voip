# Runbook: VoIP SLO definitions and service metrics

**What it is.** Service-level objectives (SLOs) and signal definitions for a production `hermes-voip`
deployment. Each signal describes: what it measures, the target range, how to observe it today
(from logs), and which signals are NOT YET INSTRUMENTED (requiring a code lane for emission).

This runbook is the **operational HOW** — which signals to watch and what ranges indicate health
or degradation. The **WHY** (design decisions on latency budgets, capacity, etc.) lives in ADRs.

> **Secrets are NAMES only.** Runbooks never contain real credentials or hostnames — reference
> env-var keys only (rule 34).

---

## SLO signals

| Signal | Target | Unit | Observable today? | Notes |
|--------|--------|------|-------------------|-------|
| **Registration uptime** | 99.9% | % (monthly) | **MEASURABLE** from logs | See [Registration uptime](#registration-uptime) |
| **Call setup success** | 99.5% | % (per attempt) | **NOT YET** — requires instrumentation | See [Call setup success](#call-setup-success) |
| **Time to first audio** | < 2 s | s | **MEASURABLE** from logs | See [Time to first audio](#time-to-first-audio) |
| **Per-turn latency (p50)** | < 1.0 s | s | **MEASURABLE** from logs (rough) | See [Per-turn latency](#per-turn-latency) |
| **Per-turn latency (p99)** | < 3.0 s | s | **MEASURABLE** from logs (rough) | See [Per-turn latency](#per-turn-latency) |
| **RTP packet loss** | < 1% | % | **NOT YET** — requires instrumentation | See [Packet loss & jitter](#packet-loss--jitter) |
| **Media jitter** | < 100 ms | ms | **NOT YET** — requires instrumentation | See [Packet loss & jitter](#packet-loss--jitter) |
| **One-way audio / no audio** | < 0.5% | % (per call) | **NOT YET** — requires instrumentation | See [Media quality](#media-quality) |
| **Concurrent call capacity** | 50 | calls | **MEASURABLE** from memory/load | See [Concurrent calls](#concurrent-calls) |
| **Error spoken to caller** | < 5% | % (per turn) | **MEASURABLE** via manual spot checks | See [Error handling](#error-handling) |

---

## Registration uptime

**What it measures.** Fraction of time the plugin's SIP extension is registered and reachable on
the gateway.

**Target:** 99.9% monthly (≤ 43 minutes of downtime per month).

**How to observe:**

1. **Check the log for registration events:**
   ```bash
   grep "SIP registration established" /path/to/hermes/log
   # Each line is a successful registration with its `expires` TTL.
   # Example: "2026-06-19T10:05:30 SIP registration established (expires 300s)"
   ```

2. **Estimate uptime from restart frequency:**
   - Count the number of registration events in a time window.
   - Each event indicates a restart or re-registration.
   - Long gaps (> 1 hour) without a registration event = the plugin was down.

3. **Manual spot check:**
   ```bash
   # Right now: attempt a call to the extension.
   # If the call rings → registered.
   # If it goes to voicemail or gets a fast busy → not registered.
   ```

**Metrics to emit (future):**
- `voip.registration.uptime` — gauge, 0–1 (0 = not registered, 1 = registered).
- `voip.registration.expires` — gauge, seconds until next re-register.
- `voip.registration.attempts` — counter, total REGISTER sent (includes failures).
- `voip.registration.failures` — counter, SIP 401/403/404 responses.

---

## Call setup success

**What it measures.** Fraction of inbound INVITE requests that result in a successful `200 OK`
and active media (not rejected with 4xx/5xx).

**Target:** 99.5% (1 failure per 200 calls is acceptable).

**How to observe today:**

Requires **manual counting** from logs (not yet automated):

```bash
# Count inbound INVITEs:
grep -c "^.*INVITE.*received" /path/to/hermes/log
# Count successful answers (200 OK with media engine active):
grep -c "CallLoop started" /path/to/hermes/log
# Count rejections (488, 486, etc.):
grep -E "REJECTED [4-5][0-9][0-9]" /path/to/hermes/log | wc -l
```

**Typical rejection causes:**
- `488 Not Acceptable Here` — codec negotiation failed (gateway offered no G.722 or G.711).
- `480 Temporarily Unavailable` — adapter overloaded or no call loop capacity.
- `500 Server Internal Error` — SDP parsing or media engine setup failed.

**NOT YET INSTRUMENTED** — a future code lane should emit:
- `voip.calls.inbound_invite_received` — counter.
- `voip.calls.setup_success` — counter (200 OK + media started).
- `voip.calls.setup_rejected` — counter (any rejection).
- `voip.calls.setup_success_pct` — gauge, `success / (success + rejected)`.

---

## Time to first audio

**What it measures.** Wall-clock seconds from receiving the inbound INVITE until the first RTP
packet is transmitted (the opening greeting or the first STT data).

**Target:** < 2.0 s (user perceives immediate response).

**How to observe:**

Manual measurement from logs:

```bash
# Find an INVITE line and the first rtp tx line in the same call.
grep "INVITE [0-9a-f]" /path/to/hermes/log | head -1
# → "2026-06-19T10:05:30.123 INVITE 1234567890abcdef received …"

grep "rtp tx: first packet" /path/to/hermes/log | head -1
# → "2026-06-19T10:05:32.456 rtp tx: first packet …"

# Difference: 2.456 - 0.123 = 2.333 s (too slow; typically < 1.5 s is good).
```

**Breakdown by phase:**
- SIP INVITE received → 200 OK sent: < 500 ms (SDP negotiation + media engine init).
- 200 OK sent → first RTP sent: < 1.5 s (TTS synthesis of the greeting + RTP startup).
  - If > 2 s: check for TTS provider latency (ElevenLabs API calls or Kokoro synthesis).

**NOT YET INSTRUMENTED:**
- `voip.media.first_audio_latency_ms` — histogram, wall-clock time INVITE → first RTP tx.

---

## Per-turn latency

**What it measures.** Time for one round trip: caller speaks → STT processes → LLM replies → TTS
synthesizes → first audio played back.

**Targets:**
- p50 (median): < 1.0 s
- p99: < 3.0 s

**How to observe:**

Manual estimate from logs (rough — timestamps are logged, but not all events appear):

```bash
# Find a turn: speech detection → ASR finalizes
grep "asr: delivering turn" /path/to/hermes/log | head -1
# → timestamp A: speech was transcribed.

# Find the LLM response:
grep "LLM response:" /path/to/hermes/log | head -1
# → timestamp B (if logged).

# Find TTS output sent to RTP:
grep "rtp tx: first packet" /path/to/hermes/log | head -3 | tail -1
# → timestamp C (if it's the response audio, not the greeting).

# Latency ≈ C - A (rough; actual timestamp precision varies by provider).
```

**Breakdown by component:**
| Phase | Budget | Provider |
|-------|--------|----------|
| **STT (speech-to-text)** | < 500 ms | Deepgram or self-hosted Sherpa |
| **LLM (agent thinking)** | < 1000 ms | OpenAI, OpenRouter, Anthropic proxy |
| **TTS (synthesis)** | < 800 ms | ElevenLabs Cloud or self-hosted Kokoro |
| **RTP play-out** | < 200 ms | local RTP scheduling |
| **Total** | < 2500 ms | sum of above |

**Latency spikes:**
- Spike in one component (STT, LLM, TTS) bubbles up as a turn-latency spike.
- If STT is slow: Deepgram API latency or network delay.
- If LLM is slow: backend overload, session context too large (grows with each turn).
- If TTS is slow: Cloud provider congestion or streaming response chunking.

**NOT YET INSTRUMENTED:**
- `voip.turn.latency_ms` — histogram (STT finalizes → RTP starts).
- `voip.stts.latency_ms` — histogram (STT provider latency).
- `voip.llm.latency_ms` — histogram (LLM provider latency).
- `voip.tts.latency_ms` — histogram (TTS provider latency).

---

## Packet loss & jitter

**What it measures.** RTP media reliability and timing consistency.

**Targets:**
- **Packet loss:** < 1% (< 1 lost packet per 100).
- **Jitter:** < 100 ms (< 100 ms variance in inter-packet arrival time).

**How to observe today:**

**NOT YET INSTRUMENTED.** The RTP media engine has no builtin stats emission. Observation requires
external packet capture or gateway-side metrics.

**Workaround for test/validation:**
- Use external packet capture on the RTP port (5000 by default):
  ```bash
  # Capture 30 s of RTP and export to pcap:
  sudo tcpdump -i any -n "udp and port 5000" -c 1000 -w /tmp/rtp.pcap
  # Analyze with wireshark or a tool like tshark:
  tshark -r /tmp/rtp.pcap -Y "rtp" -T fields -e frame.time_epoch -e rtp.seq -e rtp.timestamp
  ```

**Gateway-side metrics:**
- The SIP gateway logs RTP statistics per call (lost packets, jitter).
- After a call, check the gateway's call analytics for that call ID.

**NOT YET INSTRUMENTED:**
- `voip.rtp.packet_loss_pct` — gauge.
- `voip.rtp.jitter_ms` — gauge.
- `voip.rtp.packets_sent` — counter per call.
- `voip.rtp.packets_received` — counter per call.

---

## Media quality

**What it measures.** Fraction of calls with degraded or missing media (one-way audio, no audio,
garbled audio).

**Target:** < 0.5% of calls (1 bad call per 200).

**How to observe:**

**NOT YET INSTRUMENTED.** Currently requires manual inspection or caller feedback.

**Manual test:**
```bash
# Dial the extension and listen for quality issues:
# - Dead silence on both directions (call setup failed; see 0013-voip-incident-oncall.md).
# - Agent hears you but you hear nothing (outbound RTP issue).
# - You hear agent but agent hears nothing (inbound RTP issue).
# - Garbled / robotic audio (SRTP keying mismatch or low bit rate).
# - Echo of your own voice (gateway echo; see 0013 for mitigation).
```

**Log indicators of audio issues:**
```bash
# Look for these ERROR/WARNING lines:
grep -E "media engine.*failed|RTP.*error|rtp rx:.*error|rtp tx:.*error" /path/to/hermes/log
grep "one-way" /path/to/hermes/log
grep -i "garbled\|decode\|sync" /path/to/hermes/log
```

**NOT YET INSTRUMENTED:**
- `voip.calls.one_way_audio` — counter (inbound or outbound dead).
- `voip.calls.no_audio` — counter (both directions silent).
- `voip.calls.media_quality_issues` — counter.
- `voip.calls.media_quality_score` — gauge, 0–100 (100 = perfect; based on metrics above).

---

## Concurrent calls

**What it measures.** Maximum number of simultaneous active call loops the plugin can handle.

**Target:** 50 (design capacity; tested).

**How to observe:**

**Measurable today** (but manual):

```bash
# From logs, count "CallLoop started" lines that have not yet seen "Call ended" or hung up:
grep -E "CallLoop started|Call ended|BYE|CANCELLED" /path/to/hermes/log | tail -200
# Count live calls (started but not ended).

# From system metrics:
ps aux | grep hermes
# Note the RSS (resident memory).
# Each call ≈ 10–50 MB.
# 50 calls ≈ 500 MB–2.5 GB.
# If RSS > 4 GB with < 50 calls → possible memory leak.

# Network monitoring:
ss -tn | grep -c "5061\|5000" # SIP and RTP connections
```

**Capacity test procedure (not routine, but for validation):**

```bash
# Simulate 50 simultaneous calls (external load test).
# 1. Start the plugin.
# 2. From N client phones (or a SIP test client), dial the extension rapidly (< 5 s apart).
# 3. Hold each call open for at least 60 s.
# 4. Monitor:
#    - Plugin memory (should stay < 3 GB).
#    - RTP stream count (should reach 50).
#    - Call setup latency (should not spike).
# 5. Hang up and verify all calls end cleanly (no hung processes).
```

**NOT YET INSTRUMENTED:**
- `voip.calls.active_count` — gauge (real-time call count).
- `voip.calls.started` — counter (lifetime call count).
- `voip.calls.ended` — counter.
- `voip.memory.bytes` — gauge.
- `voip.memory.per_call_avg` — gauge.

---

## Error handling

**What it measures.** Frequency that provider errors (API failures, 502 responses) are spoken
to the caller verbatim (a current known leak — Task #26).

**Target:** < 5% of turns (or 0% if fixed).

**How to observe:**

**Manual spot check:**

```bash
# Make test calls and have the agent speak for multiple turns.
# Listen for error messages like:
# - "API call failed"
# - "502 Bad Gateway"
# - "overloaded_error"
# If you hear these, the error is being spoken (current behavior).
```

**Log inspection:**

```bash
# Count error-speech events:
grep -E "502|overloaded|API call failed|provider.*error" /path/to/hermes/log | wc -l
# Count total turns:
grep "asr: delivering turn" /path/to/hermes/log | wc -l
# Error pct ≈ (errors / turns) * 100
```

**Current status:** NOT FIXED. A future code lane will:
1. Detect provider errors in the `send()` / `speak()` path.
2. Synthesize a safe, friendly fallback response (e.g., "One moment…" or silence).
3. Log the raw error (for debugging) without speaking it.

See ADR backlog Task #26 and the `llm-502-spoken-error-finding-verified` memory entry.

**NOT YET INSTRUMENTED:**
- `voip.errors.spoken_to_caller` — counter (LLM/STT/TTS failures spoken).
- `voip.turns.total` — counter.
- `voip.turns.with_error` — counter.

---

## Instrumentation roadmap

**Current state (as of 2026-06-19):**
- ✅ **MEASURABLE from logs:** registration events, call setup (INVITE → 200 OK), time to first audio,
  per-turn latency (rough), concurrent calls (rough).
- ❌ **NOT YET INSTRUMENTED:** call setup success rate (automated), packet loss / jitter, media
  quality score, error-to-caller rate (automated).

**Next code lane:** wire metric emission (likely via StatsD, Prometheus, or structured logging)
so that:
1. Each call emits its own metrics: setup latency, duration, call outcome, latency per turn.
2. Registration state changes emit gauge updates.
3. Media engine emits RTP stats: sent/received packets, loss, jitter.
4. Provider calls emit latency + success/failure flags.
5. Errors are logged with structured fields, never spoken verbatim.

This allows production dashboards to auto-populate SLO signals without manual log parsing.

---

## Related runbooks

- [`0013-voip-incident-oncall.md`](0013-voip-incident-oncall.md) — incident diagnosis and
  on-call response.
- [`0002-voip-live-validation.md`](0002-voip-live-validation.md) — end-to-end setup and
  validation (includes manual capacity/latency spot checks).
