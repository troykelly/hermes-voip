# hermes-voip implementation plan

This plan takes the project from the merged **foundation** (sans-IO SIP/RTP signalling +
provider-interface Protocols + media-codec layer) to the documented deliverable: **polished
two-way telephony** — a Hermes `kind:platform` plugin that registers an extension on an
RFC-compliant SIP-over-TLS / WebRTC gateway and runs a full duplex conversation
(STT → Hermes agent → TTS) with VAD/endpointing, barge-in, DTMF, and an in-process
prompt-injection guard.

It is grounded in the per-ADR gap analysis and the implementation research already done
(sherpa-onnx / Kokoro / silero-vad / DeBERTa-ONNX API ground truth and pinned artifacts, and a
**verified** read of `hermes-agent==0.16.0`'s real adapter surface). Where the research found a
blocker, it is carried through honestly in §4.

---

## 1. ADR status table (0002–0010)

Status legend: **built** = the ADR's decided artifact is merged and tested; **partial** = the
foundation/seam the ADR sits on is built but the ADR's own deliverable is not; **not-started** =
none of the ADR's decided surface exists.

| ADR | Title (short) | Status | Evidence in merged code | What's missing (one line) |
|-----|---------------|--------|--------------------------|----------------------------|
| **0002** | VoIP `kind:platform` plugin owning the in-process media plane | **built** | `plugin.py:register(ctx)` (line 289) wired to `ctx.register_platform`; `plugin.yaml` shipped; `adapter.py:VoipAdapter(BasePlatformAdapter)` (line 833); `media/vad.py` (VAD + endpoint timer); `call.py`, `manager.py`, `tools.py` all shipped and wired; `hermes_surface.py` typed shim; `pytest-asyncio` async runner in place. | Live measurement on target (rule 26); the de-risking spike (P2.1) still gates on-target latency numbers. |
| **0003** | Cascaded STT → Hermes → TTS (reject fused S2S) | **partial** | The one code artifact it directly demands — streaming, cancellable `StreamingASR`/`StreamingTTS` Protocols over `PcmFrame` — is built (`providers/asr.py`, `providers/tts.py`, `providers/audio.py`) and conformance-tested. | The cascade orchestration, the text agent boundary, concrete providers, the latency mitigations as measured behaviour. |
| **0004** | Typed async provider interfaces (the seam, not the vendor) | **built** | `providers/{audio,asr,tts,guard,policy,transport,registry}.py` + `media/audio.py`; mypy-strict clean (10 files), 29 tests green; conformance proves fakes satisfy all five Protocols statically + at runtime. | Concrete provider factories; the Hermes batch-hook adapter + `plugin.yaml`; a live call-loop consumer. |
| **0005** | In-process media/transport (aiortc-behind-SIPS leading, audioop-lts) | **partial** | `audioop-lts==0.2.2` pinned; `media/audio.py` (G.711 + stateful `Resampler`); `rtp.py` (pack/parse + reordering `JitterBuffer` + `Lost` signal); `sdp.py` (parse/build/negotiate, `a=crypto` detect); sans-IO `registration.py` (REGISTER only). | The concrete `MediaTransport` engine; transport-lib choice (aiortc/pjsua2); the de-risking spike; TLS signalling I/O; SRTP/DTLS/ICE keying; Opus; PLC; `NarrowbandCodec`. |
| **0006** | Streaming STT (sherpa-onnx default + Deepgram Flux fallback) | **partial** | `StreamingASR`/`Transcript` seam, `PcmFrame`, the `audioop-lts` G.711+resample glue (incl. the continuity test), and the generic `ProviderRegistry`. | Both recognizer implementations, env selection, the model-licence gate, the media-plane wiring, the on-target WER/latency measurement. |
| **0007** | Streaming TTS (sherpa-onnx + Kokoro default; cloud fallbacks) | **partial** | `StreamingTTS`/`TtsStream` (sync factory; `flush()`/`cancel()`), `PcmFrame`, generic `Resampler`+G.711 encode, generic registry, structural conformance. | Every concrete provider (default + cloud), sentence segmentation, env selection, the model-licence gate, the TDD streaming/flush/cancel behaviour tests. |
| **0008** | VAD + endpointing (silero-vad, Phase 1 accepted; full-duplex deferred) | **built** | `media/vad.py` shipped (`VoiceActivityDetector`, `SpeechEdge`, `VadEvent`, onset/offset state machine, configurable silence timer); `media/endpoint.py` wires the endpointing logic; silero-vad is a pinned dep. Phase 2 (full-duplex/AEC) is explicitly deferred by the ADR. | Phase 2 full-duplex/AEC (out of scope per ADR). |
| **0009** | In-process offline prompt-injection guard + enforceable tool-policy gate | **partial** | The **load-bearing enforceable control is built and tested**: `providers/policy.py` (`ToolRisk`, `GuardSessionState`, `gate_tool_call` with `assert_never`; degraded blocks IRREVERSIBLE even when confirmed) + the canonical `InjectionGuard`/`GuardResult`/`GuardVerdict` seam (`providers/guard.py`). | The actual detector (DeBERTa-ONNX), normalize/decode pipeline, grading + stateful risk, fail-open wiring, the model artifact + licence gate, the seam-level classifier-miss test, the eval harness, the sidecar. |
| **0010** | DTMF (RFC 4733 primary, SIP INFO fallback, in-band last resort) | **partial** | The RFC 4733 codec slice is built: `dtmf.py` (`DtmfEvent.encode/decode`, digit↔event map, `event_payloads` per-press train, `DtmfReceiver` dedup) + `sdp.py` telephone-event parse/negotiate. | The mode-negotiation helper, the `DtmfDetector`/`DtmfDigit` seam, SIP-INFO + in-band backends, `send_dtmf` tool, controller routing, env config, RTP wiring, the `dtmf/` package layout. |

**One-sentence read:** ADRs 0002 and 0004 are fully built; ADR-0008 Phase 1 is built (Phase 2
explicitly deferred); ADRs 0003/0005/0006/0007/0009/0010 have their **seam/foundation** merged
but not their full behaviour (concrete providers, live transport, on-target measurement).

---

## 2. End-to-end gap analysis (foundation → "polished two-way telephony")

What exists is a **sans-IO, gateway-agnostic, standards-only substrate** plus the **typed provider
seam** and the **enforceable tool gate** — all mypy-strict-clean and unit-tested with fakes. What
does NOT exist is anything that touches a socket, an event loop, a model, or the Hermes runtime.
A real call cannot be made or answered today.

To reach the deliverable, six things must be built and integrated (none optional for "polished
two-way telephony"):

1. **A live media transport** (ADR-0005). The `MediaTransport` Protocol has exactly one
   implementation anywhere — `FakeTransport` in a test. There is no SIPS/INVITE dialog, no
   RTP/SRTP socket, no Opus, no PLC, no `NarrowbandCodec`. The transport library
   (aiortc vs pjsua2) is **undecided** and is resolved by the de-risking spike against the real
   gateway — which also fills the media-security/ICE/DTMF capability matrix and produces every
   latency number (rule 26).

2. **A concrete streaming STT** (ADR-0006): in-process sherpa-onnx streaming zipformer (default)
   plus the Deepgram Flux cloud fallback, behind the built `StreamingASR` Protocol, fed the 16 kHz
   stream produced by the existing `Resampler`. The thread↔asyncio bridge is the load-bearing
   design risk (a blocking C++ engine must never run on the shared loop).

3. **A concrete streaming TTS** (ADR-0007): sherpa-onnx + Kokoro (default), with sentence
   segmentation and the chunk-callback-returns-0 barge-in primitive wired to `TtsStream.cancel()`,
   plus at least one cloud fallback (ElevenLabs Flash v2.5 is buildable *and live-testable now* —
   the Pro key is already in 1Password).

4. **VAD + endpointing** (ADR-0008 Phase 1): `media/vad.py` wrapping silero-vad (the model ships
   *inside* the MIT pip wheel, so it is offline by construction), emitting onset/offset edges and
   driving the ~500 ms silence endpoint timer + the half-duplex turn-taking gate. (Phase 2
   full-duplex/AEC is explicitly deferred by the ADR — out of scope until the cancellation spike +
   follow-up ADR.)

5. **The injection guard** (ADR-0009): the in-process DeBERTa-ONNX detector + normalize/decode
   pipeline + graded/stateful verdict + fail-open wiring, behind the already-built `InjectionGuard`
   seam. **The enforceable half (`gate_tool_call`) is already done and tested**, so the guard is
   a detection layer over a control that already holds.

6. **The Hermes adapter + the per-call orchestration loop** (ADR-0002/0003): `register(ctx)` →
   `register_platform`; `VoipAdapter(BasePlatformAdapter)` implementing the 4 abstract methods; and
   `CallSession`, the per-call asyncio loop that ties items 1–5 together
   (`inbound_audio → resample → tee{VAD, ASR} → guard → MessageEvent → handle_message`; and
   `send() → sentence-chunk → TTS → resample → send_audio`; with barge-in cancel).

The integration risk concentrates in two places: the **thread↔asyncio bridge** (items 2/3/4/5 all
run CPU-bound inference that must be off-loop) and the **barge-in state machine** (timing-sensitive;
a missed cancel = the agent talks over the caller). Both are unit-testable now against fakes; both
need on-target measurement to be called done.

---

## 3. Phased, ordered plan for the remaining units

Each unit ships end-to-end in the same push (rule 6 — no scaffolding/partial-ship), TDD red-first
(rule 18), in its own worktree lane (rule 8). PR sizing is rough agent-effort. "Pinned artifacts"
are the exact model/library identities to record in committed manifests with revision SHA + sha256.

### Phase 0 — De-risk the seams (parallelisable, no runtime, no credentials)

These unblock everything else and remove the largest unknowns. Fully fake-testable.

- **P0.1 — Hermes typed surface shim + contract test.** `src/hermes_voip/hermes_surface.py`: a
  fully-typed Protocol/ABC shim of *only* the consumed `hermes-agent` surface (verified real:
  `PluginContext.register_platform(name,label,adapter_factory,check_fn,validate_config,required_env,
  install_hint,**entry_kwargs)`; `BasePlatformAdapter(ABC)` with the 4 abstractmethods
  `connect/disconnect/send/get_chat_info` and `__init__(config: PlatformConfig, platform: Platform)`;
  `MessageEvent`/`MessageType.VOICE`/`SendResult`/`PlatformConfig`/`Platform._missing_`;
  `agent.async_utils.safe_schedule_threadsafe(coro, loop, *, logger, log_message, log_level)`).
  Add `hermes-agent==0.16.0` as an **optional** dep group. Add `tests/test_hermes_contract.py`
  using `pytest.importorskip` to reflectively assert the real classes match the shim (so a Hermes
  bump fails CI, not production). **Why a shim:** `hermes-agent` ships **no `py.typed`** and uses
  unnamespaced top-level packages, so direct imports become `Any` under this repo's
  `mypy --strict` + `disallow_any_explicit` (rule 17/39 violation). This is a typing boundary over
  an untyped third-party dep, not a stub of our own code. *Size: small. Deps: none blocking.*

- **P0.2 — Thread↔asyncio bridge + provider model manifest + model-licence-gate test.** A reusable
  `bridge` (worker thread ↔ `loop.call_soon_threadsafe` ↔ `asyncio.Queue`, sentinel-terminated,
  errors re-raised on the loop side per rule 37, `cancel()` joins the thread) that STT/TTS/VAD/guard
  all reuse; a frozen-dataclass manifest type (repo, revision SHA, files=[(name, sha256, spdx)]);
  and the pure-Python CI licence gate asserting each pinned artifact's SPDX is in the allow-list
  (Apache-2.0 for STT; +MIT/CC0/CC-BY-4.0 for TTS) and the disqualified set can never be the default.
  *Size: small. Deps: none blocking.*

- **P0.3 — Async test runner.** Add+pin `pytest-asyncio` (or `anyio`), set `asyncio_mode = "auto"`,
  regenerate `uv.lock`, convert the conformance suite to await every async member. (This is the
  cross-cutting blocker that makes P1+ behaviour testable.) *Size: small. Deps: none.*

### Phase 1 — Concrete providers behind the built Protocols (parallel after P0)

Independent file territories (STT, TTS, VAD, guard) — fan out, each red-first.

- **P1.1 — VAD + endpointing (ADR-0008 Phase 1).** `src/hermes_voip/media/vad.py`:
  `VoiceActivityDetector(sample_rate_hz=16000, threshold=0.5)`, `SpeechEdge`, frozen-slots
  `VadEvent(edge, frame_index, probability)`, `feed(pcm16_frame) -> Iterator[VadEvent]`, `reset()`.
  Wrap silero-vad's raw per-frame ONNX call (not `VADIterator`) so we own the onset/offset state
  machine + the config-driven silence timer; enforce the exact 512(@16k)/256(@8k) sample window;
  read `HERMES_VOIP_VAD_THRESHOLD` / `HERMES_VOIP_ENDPOINT_SILENCE_MS` / `HERMES_VOIP_DUPLEX_MODE=half`
  at runtime. Tests use deterministic synthetic PCM (hermetic). **Pinned:** `silero-vad` (MIT) +
  `onnxruntime` (pin explicitly — silero made it optional in 6.2.1; pin opset 16 for 8 kHz). The
  ~2 MB model ships *inside* the wheel → offline by construction. *Size: small.*

- **P1.2 — Injection guard detector (ADR-0009).** `src/hermes_voip/providers/onnx_guard.py`
  implementing `InjectionGuard.screen`: normalize/decode (strip controls, NFKC, homoglyph fold,
  reverse base64/ROT13/leetspeak — pure functions, model-free tests) → tokenize → onnxruntime
  classify → grade against a tuned threshold + per-call cumulative risk + sliding-window suspicious
  rate → graded `GuardResult`; fail-open (`RESTRICT` + `degraded=True`, logged) on any inference
  error (rule 37). **The mandatory classifier-miss test:** force `screen()` to ALLOW an injection
  that requests an IRREVERSIBLE tool and assert `gate_tool_call(...)` still blocks — this passes
  *already* at the policy unit level; the new test asserts it at the seam. **Pinned:**
  `protectai/deberta-v3-base-prompt-injection-v2` (Apache-2.0, ungated), **self-quantized to INT8
  ONNX (~83 MB)** from the 739 MB fp32 upstream (the fp32 is too big for a public repo and ~600 ms
  CPU — over the ≤150 ms budget); tokenizer via `tokenizers` (avoid dragging in torch). **Decision
  (default-and-move):** produce the INT8 artifact ourselves and commit via git-LFS so CI is offline;
  if the operator forbids LFS, fall back to a checksum-pinned build-time quantize. *Size: medium.*

- **P1.3 — sherpa-onnx StreamingASR default + Deepgram Flux fallback (ADR-0006).** Default:
  `OnlineRecognizer.from_transducer(...)`, one stream per call, `accept_waveform(16000, float32)`
  (**convert PcmFrame int16-bytes → float32/32768 — a units bug here looks like a model-quality
  problem; cover with a direct test**), `is_endpoint`→`end_of_turn`, off-loop via the P0.2 bridge.
  Fallback: Deepgram Flux ws (`encoding=mulaw&sample_rate=8000` native — no client resample),
  mapping `EndOfTurn`/`EagerEndOfTurn` natively. **Pinned:** `sherpa-onnx` (Apache-2.0) + `numpy` +
  `websockets` (BSD-3); model `csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26`
  (Apache-2.0, ~310 MB / ~149 MB ORT mirror), int8 variants. *Size: medium.*

- **P1.4 — sherpa-Kokoro StreamingTTS default + cloud fallback(s) (ADR-0007).** Default: `OfflineTts`
  + Kokoro, per-sentence `generate(..., callback=cb)`, **`cb` returning 0 = barge-in** wired to
  `TtsStream.cancel()`; sentence segmentation (plain Python, first sentence short for fast first-audio
  + responsive barge-in); 24 kHz float32 → PcmFrame; off-loop via the bridge. Cloud: ElevenLabs Flash
  v2.5 multi-context ws (`output_format=ulaw_8000` native; `flush:true`→`flush()`,
  `close_context:true`→`cancel()`) — **live-testable now (Pro key in 1Password)**. TDD behaviour
  tests: streaming order, flush end-of-utterance framing, mid-stream cancel emits no further frames.
  **Pinned:** sherpa-onnx `kokoro-en-v0_19` (Apache-2.0, ~686 MB incl. espeak-ng-data). *Size: medium.*

- **P1.5 — Provider env-selection + registry wiring.** Per-family `ProviderRegistry` instances +
  self-registering factories keyed by `HERMES_VOIP_STT_PROVIDER` / `HERMES_VOIP_TTS_PROVIDER` /
  `HERMES_VOIP_INJECTION_GUARD` (defaults: `sherpa-onnx` / `sherpa-kokoro` / `onnx`), fail-fast on a
  selected-but-misconfigured cloud provider (rule 37). *Size: small. Deps: P1.1–P1.4.*

### Phase 2 — The live media transport (the spike gate)

- **P2.1 — De-risking spike (ADR-0005).** One real inbound SIPS call from the test gateway,
  half-duplex, audio→STT→TTS→caller; fill the capability matrix (media-security profile, ICE mode,
  RTP profile, DTMF mode); **select aiortc vs pjsua2**; **measure every latency number** (rule 26).
  Runbook written as-you-go (rule 42). *Size: medium/spike-shaped. **Blocked on the webrtc/gateway
  credential** (see §4).*

- **P2.2 — Concrete `MediaTransport` engine (Path A, leading).** Behind the built Protocol: thin
  asyncio-TLS SIPS signalling client reusing `registration.py`/`message.py`/`sdp.py`/`rtp.py` for
  REGISTER + INVITE/re-INVITE/BYE/ACK + RFC 3264 offer/answer; media via the spike-selected library
  (DTLS-SRTP/SDES-SRTP/ICE/Opus+G.711); the 20 ms packetisation clock stamping `monotonic_ts_ns`;
  the adaptive de-jitter + PLC generation (extending `rtp.py`'s `Lost` signal); the `NarrowbandCodec`
  enum. *Size: large. Deps: P2.1.*

### Phase 3 — Hermes integration + the orchestration loop (the deliverable)

- **P3.1 — `VoipAdapter` + plugin entry point (ADR-0002).** `register(ctx)` → one
  `ctx.register_platform(name="voip", …, required_env=HERMES_SIP_*, pii_safe=True)`; `plugin.yaml`
  (`kind: platform`); `pyproject` `[project.entry-points."hermes_agent.plugins"]`; the 4 abstract
  methods against the P0.1 shim; `create_voip_adapter`/`check_voip_ready`/`validate_voip_config`;
  creds from `PlatformConfig.extra` only. Tested against a fake `PluginContext`. *Size: small.
  Deps: P0.1.*

- **P3.2 — `CallSession` orchestration loop (ADR-0002/0003).** The per-call asyncio state machine:
  `inbound_audio → Resampler(8k→16k) → tee{VAD/endpoint, StreamingASR} → InjectionGuard.screen →
  GuardSessionState → MessageEvent(MessageType.VOICE, text=transcript, media_urls=[…]) →
  handle_message`; outbound `send(content) → sentence-chunk → StreamingTTS → Resampler(→8k) →
  send_audio`; **barge-in** (VAD speech-start during AGENT_SPEAKING → `TtsStream.cancel()` + stop the
  send task) with the IDLE/CALLER_SPEAKING/THINKING/AGENT_SPEAKING transitions; chat_id←Call-ID,
  user_id←normalized From/PAI, chat_type="dm"; off-loop callbacks via `safe_schedule_threadsafe`;
  BYE→teardown. **Fully fake-backed end-to-end test** (FakeTransport/FakeASR/FakeTTS/FakeGuard) proves
  the whole loop incl. barge-in deterministically with zero network. *Size: medium. Deps: P3.1, P1.x.*

- **P3.3 — DTMF backends + tool + controller routing (ADR-0010).** Mode-negotiation helper +
  `DtmfMode`/`DtmfDigit`/`DtmfDetector` seam wrapping the built `DtmfReceiver`; SIP-INFO + in-band
  Goertzel backends; `send_dtmf` exposed via `ctx.register_tool`; controller routing of inbound digits
  to resolve the ADR-0009 confirmation gate directly (spoof-resistant) or a `[DTMF] …` `MessageEvent`;
  migrate `dtmf.py` → `dtmf/` package. *Size: medium. Deps: P2.2 (RTP/SIP wiring), P3.2.*

- **P3.4 — Live validation + measurement.** Local Hermes install: verify plugin discovery/enablement
  (`config.yaml plugins.enabled` — pip-installed `kind:platform` is **not** auto-loaded),
  `MessageEvent → run_conversation → send()` round-trip, `Platform._missing_` resolving "voip",
  `pre_tool_call` firing. Then the real-gateway end-to-end call, reporting on-target numbers (WER,
  silence→first-audio, jitter, barge-in latency) — rule 26 done-definition. *Size: medium. Deps: all.*

**Dependency summary:** P0 (no deps) → P1 (needs P0) ‖ P2.1 spike (credential-blocked) → P2.2 (needs
spike) → P3.1 (needs P0.1) → P3.2 (needs P1+P3.1) → P3.3/P3.4 (need P2.2+P3.2). P1 and P2.1 run in
parallel with each other and with P3.1.

---

## 4. Blocker register (honest)

| Blocker | Severity | Affects | Reality | Resolution |
|---------|----------|---------|---------|------------|
| **model-download** | **non-blocking** (feasibility verified) | P1.1–P1.4, P1.2 licence gate | API ground truth + exact artifacts are **verified**: silero-vad (in-wheel, MIT), sherpa zipformer 2023-06-26 (Apache-2.0), Kokoro v0_19 (Apache-2.0), DeBERTa v2 (Apache-2.0, self-quantize to INT8). No artifact is gated/credentialed. | Fetch + checksum-verify into the model dir; pin (repo, revision SHA, filenames, sha256) in committed manifests; not committed to git except the ~83 MB INT8 guard (git-LFS, default-and-move). Resolve the exact DeBERTa revision SHA at implementation. |
| **hermes-runtime** | **real, mitigated** | P0.1, P3.x | `hermes-agent==0.16.0` is on PyPI (MIT, `>=3.11,<3.14` — 3.13 intersects). All consumed symbols **verified present** (`register_platform`, `BasePlatformAdapter` 4 methods, `MessageType.VOICE`, `safe_schedule_threadsafe`, `Platform._missing_`). **But it ships no `py.typed`** → direct imports are `Any` under our `mypy --strict` (rule 17/39). | **A typed shim IS needed** (`hermes_surface.py`) + a reflection contract test (P0.1). hermes-agent is an *optional* dep, pinned `==0.16.0`. Our adapter is written complete against the shim — not deferred. Caveat: our `>=3.13` only overlaps Hermes's `<3.14` at 3.13; pin and document. |
| **webrtc-credential** | **real, external** | P2.1 spike, Path-B (WSS/WebRTC) | We have SIP digest creds (`HERMES_SIP_*`) but **no verified WebRTC token / TURN(ICE) credential flow** for the test gateway. The spike (which selects the transport and produces all latency numbers) needs a reachable real gateway. | **Path A (SIP-over-TLS) is the primary live path** so the project is not blocked on WebRTC. The spike runs against the SIPS endpoint with the existing digest creds. Path-B stays designed-not-live until the operator supplies a token/TURN story — named explicitly, not defaulted. |
| **transport choice (design)** | **deferred-by-design** | P2.2 | aiortc (loop-native, DTLS-SRTP/ICE/Opus) vs pjsua2 (native SIPS/SDES-SRTP/DTMF, needs a native build + thread bridge) is **undecided** — ADR-0005 routes it to the spike. | Resolved by P2.1 against the real gateway. It's a registry/config change, not a rewrite (both implement the same `MediaTransport` Protocol). |
| **.env fixes already found** | **resolved/known** | all live work | The foundation already honours `HERMES_SIP_*` and uses fakes (`pbx.example.test`, ext `1000`) in tests; secrets stay in gitignored `.env` / 1Password. The dev ElevenLabs Pro key is in 1Password (field `key`). | No action beyond reading creds from `PlatformConfig.extra` at runtime (P3.1) and never logging/committing them (rules 34/41). The ElevenLabs path is live-testable now. |
| **operator enablement** | **process** | P3.4 | A pip-installed `kind:platform` plugin is **not** auto-loaded — silent-fail-prone. | Verified runbook step (`config.yaml plugins.enabled` / `hermes plugins enable`) under `docs/runbooks/` (rule 42), validated against the local runtime in P3.4. |

Phase 2 full-duplex barge-in + AEC (ADR-0008 Phase 2) is **explicitly deferred** by its ADR (gated on
the unverified `AIAgent.run_conversation` mid-generation cancellation + an AEC choice in a follow-up
ADR) and is **out of scope** for this plan — do not build it now (rule 6).

Durable call-events + recordings analytics export (AWS S3 Tables / Iceberg) is researched and designed
in ADR-0060 (Proposed/Deferred) as a post-Wave-2 nice-to-have; it is OUT OF SCOPE for the P0–P3 phases
here and is gated on operator cost approval + an ADR flip to Accepted + a runbook (rule 40).

---

## 5. Smallest next shippable step ("get back to shipping")

**P0.1 and P0.3 are shipped.** `hermes_surface.py`, `tests/test_hermes_contract.py`,
`pytest-asyncio` (pinned, `asyncio_mode="auto"`), and `hermes-agent==0.16.0` as an optional dep
group all landed in an earlier PR. The full P0 phase is complete.

**The current unblocked frontier is P1 (concrete providers) and P2.1 (the de-risking spike).**
P1 items (VAD, guard detector, STT, TTS, provider registry) are parallelisable and credential-free.
P2.1 is gated on a reachable gateway (the SIPS endpoint with existing digest creds is the leading
path — see §4). P3 (the Hermes adapter + call loop) depends on P1 + P2.1.

The next highest-leverage unblocked lane is **P1.2 (injection guard detector, ADR-0009)** or
**P1.3 (sherpa-onnx StreamingASR, ADR-0006)** — both are credential-free and unblock the
call-loop integration.
