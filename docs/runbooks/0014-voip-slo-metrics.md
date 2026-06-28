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
| **Call setup success** | 99.5% | % (per attempt) | **MEASURABLE** — structured lifecycle events | See [Call setup success](#call-setup-success) |
| **Time to first audio** | < 2 s | s | **MEASURABLE** from logs | See [Time to first audio](#time-to-first-audio) |
| **Per-turn latency (p50)** | < 1.0 s | s | **MEASURABLE** from logs (rough) | See [Per-turn latency](#per-turn-latency) |
| **Per-turn latency (p99)** | < 3.0 s | s | **MEASURABLE** from logs (rough) | See [Per-turn latency](#per-turn-latency) |
| **RTP packet loss** | < 1% | % | **RTCP ACTIVE (plain RTP)** — teardown log; metrics sink TBD | See [Packet loss & jitter](#packet-loss--jitter) |
| **Media jitter** | < 100 ms | ms | **RTCP ACTIVE (plain RTP)** — teardown log; metrics sink TBD | See [Packet loss & jitter](#packet-loss--jitter) |
| **Round-trip time** | < 300 ms | ms | **RTCP ACTIVE (plain RTP)** — teardown log; metrics sink TBD | See [Packet loss & jitter](#packet-loss--jitter) |
| **One-way audio / no audio** | < 0.5% | % (per call) | **INFERABLE (RTCP, plain RTP)** — metrics sink TBD | See [Media quality](#media-quality) |
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

**How to observe today (structured events, ADR-0075).**

Each inbound call emits machine-parseable per-call lifecycle records via the stdlib
logger's `extra={}` (LOCAL-ONLY — no external sink). The human message text is unchanged;
the structured fields ride alongside it so a log pipeline (journald JSON, Loki, etc.) can
count by `event` without grepping prose. All carry `call_id` to correlate one call across
its lifecycle. The events:

| `event` | Emitted when | Key fields |
|---------|--------------|------------|
| `invite_received` | inbound INVITE arrives | `call_id`, `extension` |
| `call_rejected` | any pre-200-OK reject (486/603/488/422) | `call_id`, `outcome="rejected"`, `sip_code`, `reason` |
| `call_answered` | the inbound `200 OK` is sent | `call_id`, `outcome="answered"`, `sip_code=200` |
| `call_loop_started` | the conversational loop goes live | `call_id`, `direction` (`inbound`/`outbound`) |
| `call_released` | the admission slot is freed at teardown | `call_id`, `duration_s`, `active_calls` |

Setup success ≈ `count(call_answered) / (count(call_answered) + count(call_rejected))`.

With JSON-formatted logs (`jq`):

```bash
# Inbound INVITEs, answers, rejects (structured event field):
jq -c 'select(.event=="invite_received")' /path/to/hermes/log.jsonl | wc -l
jq -c 'select(.event=="call_answered")'  /path/to/hermes/log.jsonl | wc -l
jq -c 'select(.event=="call_rejected")'  /path/to/hermes/log.jsonl | wc -l
# Reject breakdown by SIP code + reason token:
jq -r 'select(.event=="call_rejected") | "\(.sip_code) \(.reason)"' /path/to/hermes/log.jsonl | sort | uniq -c
```

The human-readable text is still greppable for plain-text logs:

```bash
grep -c "INVITE received" /path/to/hermes/log
grep -c "CallLoop started" /path/to/hermes/log
grep -E "REJECTED [4-6][0-9][0-9]" /path/to/hermes/log | wc -l
```

**Typical rejection `reason` tokens (the `reason` field on `call_rejected`):**
- `at_capacity` (486) — concurrent-call cap reached.
- `caller_declined` (603) — caller-group deny match.
- `secure_media_mandate` / `no_common_codec` / `no_voice_codec` / `codec_not_carriable` /
  `codec_dependency_unavailable` / `cannot_build_answer` / `unparseable_sdp` /
  `no_audio_media` / `webrtc_missing_fingerprint_ice` / `sip_dtls_missing_fingerprint` (488).
- `session_interval_too_small` (422) — offered Session-Expires below Min-SE.

**Metrics sink still TBD** — a future lane maps these structured events to gauges/counters:
- `voip.calls.inbound_invite_received` — counter (`event=invite_received`).
- `voip.calls.setup_success` — counter (`event=call_answered`).
- `voip.calls.setup_rejected` — counter (`event=call_rejected`, label `sip_code`/`reason`).
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
- **Round-trip time:** < 300 ms (RTCP-derived; see below).

**In-process source (RTCP, ADR-0061).** The media engine now runs RTCP (RFC 3550 §6):
it computes per-call reception statistics from the inbound RTP stream and parses the peer's
RTCP reports. `RtpMediaTransport.call_quality` returns a `CallQuality` snapshot with:

- `local_fraction_lost` / `local_cumulative_lost` / `local_jitter_ms` — what WE received
  from the peer (our own measurement).
- `remote_fraction_lost` / `remote_cumulative_lost` / `remote_jitter_ms` — what the PEER
  reported it received from US (the far-end view of our outbound stream).
- `rtt_seconds` — round-trip time from the peer report block's LSR/DLSR.

**Adapter activation (live, ADR-0061).** The adapter activates RTCP for each inbound call
on the **cleartext plain-RTP path**: after `connect()` it calls
`engine.start_rtcp(mux=…, remote_rtcp_addr=…)`, which starts the periodic SR/RR sender,
demuxes inbound RTCP (RFC 5761 §4 on the muxed port, or a sibling socket on RTP-port+1 when
not muxed — RFC 3550 §11), and flushes a closing BYE on stop. At call teardown the adapter
logs the final `call_quality` snapshot (an INFO line: local/remote loss, jitter, RTT). That
line now ALSO carries a structured `extra={}` (ADR-0075): `event="rtcp_call_quality"`,
`call_id`, and the five numeric quality fields (`local_fraction_lost`, `local_jitter_ms`,
`remote_fraction_lost`, `remote_jitter_ms`, `rtt_seconds`) — so a log pipeline filters and
aggregates per-call quality without parsing the message. The record is gated on the call
having actually run RTCP (`_rtcp_active`), so a secured/disabled call emits nothing. RTCP
is on by default; the operator kill-switch is `HERMES_VOIP_RTCP_ENABLED=false`.

```bash
# Per-call quality from JSON logs:
jq -c 'select(.event=="rtcp_call_quality")
       | {call_id, loss: .local_fraction_lost, jitter: .local_jitter_ms, rtt: .rtt_seconds}' \
  /path/to/hermes/log.jsonl
```

**Secured paths (SDES / WebRTC): RTCP is dormant by default (opt-in via ADR-0066).**
ADR-0066 shipped `src/hermes_voip/media/srtcp.py` and wired it in `adapter.py`. However,
a live finding (2026-06-21) showed that sending SRTCP to a real gateway that had not
negotiated `a=rtcp-mux` muted the audio entirely. Secured-path RTCP is therefore
**opt-in, default off**: by default a SDES/WebRTC call stays **RTCP-dormant** (no sibling
SRTCP socket, no RTCP on the wire), which is the audio-working posture.

Set `HERMES_VOIP_SECURED_RTCP_ENABLED=true` (together with the master
`HERMES_VOIP_RTCP_ENABLED=true`) to activate SRTCP on a gateway validated to tolerate it.
See runbook 0002 §9c for the full opt-in procedure and pass criteria.

**Still TODO:** the teardown snapshot is logged, not yet pushed to a metrics sink (the
`voip.rtp.*` gauges below); wiring a metrics emitter is the remaining observability step.

**Workaround for test/validation (independent of the above):**
- Use external packet capture on the RTP port (5000 by default):
  ```bash
  # Capture 30 s of RTP and export to pcap:
  sudo tcpdump -i any -n "udp and port 5000" -c 1000 -w /tmp/rtp.pcap
  # Analyze with wireshark or a tool like tshark:
  tshark -r /tmp/rtp.pcap -Y "rtp" -T fields -e frame.time_epoch -e rtp.seq -e rtp.timestamp
  # RTCP SR/RR (incl. the loss/jitter/RTT fields) — muxed on the RTP port when
  # rtcp-mux was negotiated, else on RTP port + 1:
  tshark -r /tmp/rtp.pcap -Y "rtcp" -V
  ```

**Gateway-side metrics:**
- The SIP gateway logs RTP statistics per call (lost packets, jitter).
- After a call, check the gateway's call analytics for that call ID.

**To emit (adapter lane — the source exists in `call_quality`):**
- `voip.rtp.packet_loss_pct` — gauge (`local_fraction_lost` * 100).
- `voip.rtp.jitter_ms` — gauge (`local_jitter_ms`).
- `voip.rtp.rtt_ms` — gauge (`rtt_seconds` * 1000).
- `voip.rtp.packets_sent` / `voip.rtp.packets_received` — counters per call.

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

**One-way audio is now inferable from RTCP (ADR-0061):** if `call_quality` shows we send
packets but the peer's reports never arrive (no inbound RTCP / `rtt_seconds` stays `None`)
the OUTBOUND leg may be dead; if we receive no RTP (`local_*` all `None`) while sending, the
INBOUND leg may be dead. The adapter lane turns this into the counters below.

**To emit (adapter lane — RTCP source exists):**
- `voip.calls.one_way_audio` — counter (inbound or outbound dead, inferred from `call_quality`).
- `voip.calls.no_audio` — counter (both directions silent).
- `voip.calls.media_quality_issues` — counter.
- `voip.calls.media_quality_score` — gauge, 0–100 (100 = perfect; based on metrics above).

---

## Concurrent calls

**What it measures.** Maximum number of simultaneous active call loops the plugin can handle.

**Target:** 50 (design capacity; tested).

**How to observe:**

**Measurable today (structured gauge, ADR-0075).** Every `call_released` event carries
`active_calls` — the live admission-slot count AFTER that release — which is the
concurrency gauge sampled at each call end, plus `duration_s` for the released call:

```bash
# Live concurrency over time (the value AFTER each release) + the call's duration:
jq -r 'select(.event=="call_released") | "\(.call_id) dur=\(.duration_s)s active=\(.active_calls)"' \
  /path/to/hermes/log.jsonl
# Peak concurrency observed at release points:
jq -r 'select(.event=="call_released") | .active_calls' /path/to/hermes/log.jsonl | sort -n | tail -1
```

Plain-text fallback (correlate started vs released):

```bash
# From logs, count "CallLoop started" lines that have not yet seen a release or hung up:
grep -E "CallLoop started|call released|BYE|CANCELLED" /path/to/hermes/log | tail -200
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

**Metrics sink still TBD** (the structured `active_calls` / `duration_s` source exists):
- `voip.calls.active_count` — gauge (`active_calls` from `call_released`, or `call_loop_started`
  minus `call_released`).
- `voip.calls.started` — counter (`event=call_loop_started`).
- `voip.calls.ended` — counter (`event=call_released`).
- `voip.calls.duration_s` — histogram (`duration_s` from `call_released`).
- `voip.memory.bytes` — gauge.
- `voip.memory.per_call_avg` — gauge.

---

## Error handling

**What it measures.** Frequency that provider errors (API failures, 502 responses)
reach the caller. ADR-0063 shipped an intercept so raw errors are **not** spoken;
this SLO now measures residual leakage (should be 0%) and the instrumentation gap
(counters not yet wired to a metrics sink).

**Target:** 0% of turns (callers hear only the safe apology phrase, never a raw
error string).

**Shipped behaviour (ADR-0063):** when `is_provider_error()` detects a raw error in
the LLM reply, `adapter._deliver_content` replaces it with `resolve_error_apology()`
before calling `loop.speak()`. The raw error is logged at WARNING with structured
`event=provider_error_replaced` and `error_category`.

**How to observe:**

**Log inspection — intercepted errors (should be the ONLY form of error events):**

```bash
# Count intercepted provider errors (caller heard safe apology, not raw error):
jq 'select(.event=="provider_error_replaced") | .error_category' \
  /path/to/hermes/log.jsonl | sort | uniq -c
# Count total turns:
grep "asr: delivering turn" /path/to/hermes/log | wc -l
```

**Manual spot check:**

```bash
# Make test calls with a deliberately overloaded/down backend.
# The caller should hear a friendly apology phrase, NOT a raw error string such as:
# - "502 Bad Gateway"
# - "overloaded_error"
# If you hear raw errors, check provider_error.py patterns and adapter.py ~line 1435.
```

**NOT YET INSTRUMENTED (remaining gap):**
- `voip.errors.intercepted` — counter (provider errors intercepted; caller heard apology).
- `voip.turns.total` — counter.
- `voip.turns.with_error` — counter.

---

## Instrumentation roadmap

**Current state (as of 2026-06-26):**
- ✅ **STRUCTURED LOG EVENTS (ADR-0075):** per-call lifecycle (`invite_received`,
  `call_rejected`, `call_answered`, `call_loop_started`, `call_released`) and RTCP
  call-quality (`rtcp_call_quality`) emit machine-parseable `extra={}` fields — call setup
  success, RTP loss/jitter/RTT, and concurrency gauge are now countable from logs without
  prose-grepping. LOCAL-ONLY stdlib logging; no external sink.
- ✅ **MEASURABLE from logs:** registration events, call setup (INVITE → 200 OK), time to first audio,
  per-turn latency (rough), concurrent calls.
- 🟡 **SOURCE EXISTS, EMISSION TBD:** the structured events above are not yet PUSHED to a
  metrics sink (StatsD/Prometheus) — that mapping is the remaining step.
- ❌ **NOT YET INSTRUMENTED:** media quality score, error-to-caller rate (automated).

**Next code lane:** wire metric emission (likely via StatsD, Prometheus, or structured logging)
so that:
1. Each call emits its own metrics: setup latency, duration, call outcome, latency per turn.
2. Registration state changes emit gauge updates.
3. Media engine RTP stats are PUSHED: read `engine.call_quality` (loss/jitter/RTT already
   computed via RTCP, ADR-0061) periodically and emit the gauges/counters above. The adapter
   also calls `engine.ingest_rtcp(datagram)` on inbound RTCP and starts `engine.run_rtcp(...)`.
4. Provider calls emit latency + success/failure flags.
5. Errors are logged with structured fields, never spoken verbatim.

This allows production dashboards to auto-populate SLO signals without manual log parsing.

---

## Related runbooks

- [`0013-voip-incident-oncall.md`](0013-voip-incident-oncall.md) — incident diagnosis and
  on-call response.
- [`0002-voip-live-validation.md`](0002-voip-live-validation.md) — end-to-end setup and
  validation (includes manual capacity/latency spot checks).
