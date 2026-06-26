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
| **0003** | Cascaded STT → Hermes → TTS (reject fused S2S) | **built** | `media/call_loop.py` ships the duplex `CallLoop`; `adapter.py` wires it into live calls; the loop runs inbound audio through VAD/ASR/guard before `deliver_turn`, and outbound agent text through TTS before `send_audio()`. | Real-gateway latency/WER evidence is still an operational validation task, not a missing code path. |
| **0004** | Typed async provider interfaces (the seam, not the vendor) | **built** | `providers/{audio,asr,tts,guard,policy,transport,registry}.py` + `media/audio.py`; mypy-strict clean; conformance proves the Protocol seams statically + at runtime. | No seam-level ADR gap remains here; follow-on ADRs extend concrete implementations over this surface. |
| **0005** | In-process media/transport (aiortc-behind-SIPS leading, audioop-lts) | **built** | `transport/connection.py` (`SipOverTlsTransport`), `transport/ws_connection.py` (`WssSipTransport`), `media/engine.py` (`RtpMediaTransport`), `media/webrtc_session.py` (`WebRtcMediaSession`), and `adapter.py` (`connect()`, `place_call()`, `abort_call()`) are all shipped and wired. | Real-gateway measurement is still pending, and WSS outbound abort parity is incomplete (`send_cancel` is a no-op there; `ring_timeout_secs` raises). |
| **0006** | Streaming STT (sherpa-onnx default + Deepgram Flux fallback) | **built** | `stt/sherpa_onnx.py` (`SherpaOnnxASR`), `stt/deepgram.py` (`DeepgramASR`), `providers/build.py`, and `adapter.py` (`build_providers`) ship the concrete STT path. | On-target WER/latency measurement is still a verification task. |
| **0007** | Streaming TTS (sherpa-onnx + Kokoro default; cloud fallbacks) | **built** | `tts/sherpa_kokoro.py` (`SherpaKokoroTTS`), `tts/elevenlabs.py` (`ElevenLabsTTS`), `providers/build.py`, and the live call loop's `TtsStream.cancel()` path are all shipped. | On-target TTS latency/voice-quality measurement is still a verification task. |
| **0008** | VAD + endpointing (silero-vad, Phase 1 accepted; full-duplex deferred) | **built** | `media/vad.py` ships `VoiceActivityDetector`/`load_silero_model`; `media/endpoint.py` wires endpointing; `call_loop.py` consumes both. The live model is the cached `silero_vad.onnx` loaded via `onnxruntime` under the optional `ml` extra, not a pinned `silero-vad` pip dep. | Phase-2/full-duplex work is governed by follow-on ADRs; it is not missing Phase-1 VAD/endpointing code. |
| **0009** | In-process offline prompt-injection guard + enforceable tool-policy gate | **built** | `guard/onnx.py` (`OnnxInjectionGuard`, `build_onnx_classifier`), `providers/build.py`, `providers/policy.py`, and `media/call_loop.py` (`guard.screen(...)`, `guard_state.record(...)`) ship the detector and the enforceable gate together. | On-target detector-quality evaluation remains a verification/reporting task. |
| **0010** | DTMF (RFC 4733 primary, SIP INFO fallback, in-band last resort) | **built** | `call.py` (`send_dtmf`, `send_dtmf_info`), `dtmf_config.py`, `media/engine.py` (`send_dtmf`, negotiated telephone-event handling), and `voip_tools.py` (`send_dtmf` tool registration/handler) are shipped. | Cross-gateway live validation of each negotiated backend remains a verification task. |

**One-sentence read:** the runtime stack is now broadly shipped end-to-end: adapter, call loop,
transport/media, VAD, STT, TTS, guard, and DTMF are in merged code; the remaining work is mainly
real-gateway validation/measurement plus specific parity gaps (notably WSS outbound CANCEL/ring-timeout).

---

## 2. End-to-end gap analysis (foundation → "polished two-way telephony")

What exists now is the live runtime stack itself: Hermes registration via `plugin.py` +
`adapter.py`; concrete signalling/media transports (`SipOverTlsTransport`, `WssSipTransport`,
`RtpMediaTransport`, `WebRtcMediaSession`); concrete providers (`SherpaOnnxASR`, `DeepgramASR`,
`SherpaKokoroTTS`, `ElevenLabsTTS`, `OnnxInjectionGuard`, silero VAD via `load_silero_model`);
and the shipped `CallLoop`/`CallSession`/tool wiring. A real call path exists in code today.

What remains to reach the *operationally verified* deliverable is narrower:

1. **Real-gateway measurement and parity verification** (rule 26). The code ships both the TLS and
   WSS/WebRTC stacks, but the docs still need honest separation between "implemented" and
   "measured on the target gateway". The de-risking spike's remaining value is runtime evidence:
   media-security/ICE/DTMF capability confirmation, latency/WER numbers, and gateway-specific
   parity checks.

2. **Transport-specific parity gaps** (ADR-0005/0069). The main shipped gap is outbound abort
   behaviour on WSS/WebRTC: `WssSipTransport.send_cancel()` is still a no-op and
   `place_call(..., ring_timeout_secs=...)` raises on WSS. Documenting and closing those parity
   gaps is distinct from claiming the whole transport/media stack is absent.

3. **Operational validation of the concrete ML providers** (ADR-0006/0007/0009). The concrete STT,
   TTS, guard, and VAD implementations are shipped and wired; what remains is on-target quality and
   latency evidence, not basic provider implementation.

4. **Follow-on enhancements, not missing foundations.** Full-duplex/AEC and other later ADR work
   remain explicit follow-ons. They should not be described as if Phase-1 VAD, the call loop, or
   the concrete transport/provider stack were still unimplemented.

5. **Hermes/runtime rollout verification.** The adapter and plugin entry point are shipped; the
   remaining work is validating plugin discovery/enablement and live end-to-end behaviour under the
   real runtime and gateway.

The integration risk now concentrates in verification and parity, not in the existence of the core
runtime stack.

The integration risk concentrates in two places: the **thread↔asyncio bridge** (items 2/3/4/5 all
run CPU-bound inference that must be off-loop) and the **barge-in state machine** (timing-sensitive;
a missed cancel = the agent talks over the caller). Both are unit-testable now against fakes; both
need on-target measurement to be called done.

---

## 3. Phased delivery record and remaining operational units

This section preserves the original phased delivery units for traceability. Most of the build work
below is now shipped; use the ADR status table in §1 and the current frontier in §5 for the
present-tense view. Where a phase still matters today, it is because of remaining
validation/measurement/parity work rather than because the core code path is absent.

Each unit was intended to ship end-to-end in the same push (rule 6 — no
scaffolding/partial-ship), TDD red-first (rule 18), in its own worktree lane (rule 8). PR sizing is
rough agent-effort. "Pinned artifacts" are the exact model/library identities to record in
committed manifests with revision SHA + sha256.

### Phase 0 — De-risk the seams (historical; shipped)

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

### Phase 1 — Concrete providers behind the built Protocols (historical delivery record; shipped)

The bullets below describe the concrete provider work that has since landed. The remaining work in
this area is measurement/quality validation, not initial provider implementation. Independent file
territories for follow-on validation still mainly sit around STT, TTS, and the guard; VAD is
already shipped.

- **P1.2 — Injection guard detector (ADR-0009).** `src/hermes_voip/guard/onnx.py`
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
  selected-but-misconfigured cloud provider (rule 37). *Size: small. Deps: the shipped Phase-1
  provider set above.*

### Phase 2 — The live media transport (historical build record; now mostly verification)

- **P2.1 — De-risking spike (ADR-0005).** The transport/media stack is now built, so the spike's
  remaining purpose is operational evidence: real inbound/outbound calls against the test gateway,
  capability-matrix confirmation (media-security profile, ICE mode, RTP profile, DTMF mode), and
  measured latency/jitter numbers (rule 26). Runbook written as-you-go (rule 42). *Size:
  medium/spike-shaped. The TLS path is live in code today; the WebRTC credential/TURN story still
  limits full WSS validation (see §4).*

- **P2.2 — Concrete `MediaTransport` engine (Path A, leading).** This implementation work is shipped:
  the repo already contains the concrete signalling/media engine (`SipOverTlsTransport`,
  `WssSipTransport`, `RtpMediaTransport`, `WebRtcMediaSession`) over the existing
  `registration.py`/`message.py`/`sdp.py`/`rtp.py` substrate. The remaining work is parity
  validation and closing documented gaps such as WSS outbound CANCEL/ring-timeout behaviour.

### Phase 3 — Hermes integration + the orchestration loop (historical build record; shipped core)

- **P3.1 — `VoipAdapter` + plugin entry point (ADR-0002).** This core implementation is shipped:
  `register(ctx)` wires the platform registration; `plugin.yaml` and the plugin entry point ship;
  `VoipAdapter` implements the adapter surface over the typed shim; creds stay in runtime config.

- **P3.2 — `CallSession` orchestration loop (ADR-0002/0003).** This core implementation is shipped:
  the per-call loop ties inbound audio, VAD/endpointing, ASR, the guard, Hermes message delivery,
  outbound TTS/audio send, and barge-in cancellation into a live runtime path. The remaining work is
  live measurement/verification, not first-time loop construction.

- **P3.3 — DTMF backends + tool + controller routing (ADR-0010).** The core implementation is shipped:
  DTMF negotiation/runtime handling and the agent-facing `send_dtmf` surface exist in merged code.
  The remaining work is cross-gateway validation of each backend and negotiated mode.
  
- **P3.4 — Live validation + measurement.** Local Hermes install: verify plugin discovery/enablement
  (`config.yaml plugins.enabled` — pip-installed `kind:platform` is **not** auto-loaded),
  `MessageEvent → run_conversation → send()` round-trip, `Platform._missing_` resolving "voip",
  `pre_tool_call` firing. Then the real-gateway end-to-end call, reporting on-target numbers (WER,
  silence→first-audio, jitter, barge-in latency) — rule 26 done-definition. *Size: medium. Deps: all.*

**Dependency summary (historical build order):** P0 preceded the concrete provider work; the
transport/media build and Hermes/call-loop integration then landed on top of that foundation. In the
current repo state, the remaining dependency chain is operational: gateway reachability and WebRTC
credentials/TURN data bound the scope of live parity measurement, while plugin enablement and
end-to-end runtime checks bound rollout verification.

---

## 4. Blocker register (honest)

| Blocker | Severity | Affects | Reality | Resolution |
|---------|----------|---------|---------|------------|
| **model-download** | **non-blocking** (feasibility verified) | P1.2–P1.4, P1.2 licence gate | API ground truth + exact artifacts are **verified**: the cached silero ONNX model loaded via `onnxruntime` under the optional `ml` extra, sherpa zipformer 2023-06-26 (Apache-2.0), Kokoro v0_19 (Apache-2.0), DeBERTa v2 (Apache-2.0, self-quantize to INT8). No artifact is gated/credentialed. | Fetch + checksum-verify into the model dir; pin (repo, revision SHA, filenames, sha256) in committed manifests; not committed to git except the ~83 MB INT8 guard (git-LFS, default-and-move). Resolve the exact DeBERTa revision SHA at implementation. |
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

**The current unblocked frontier is runtime verification/parity work, not missing VAD or adapter
implementation.** The concrete provider stack is shipped; the remaining parallelisable lanes are
quality/measurement work around the guard, STT, TTS, and real-gateway transport behaviour.
P2.1 is now primarily a measurement/parity exercise on a reachable gateway (the SIPS endpoint with
existing digest creds is the leading path — see §4).

The next highest-leverage unblocked lane is **real-gateway parity validation** (especially the WSS
outbound abort gap) or **provider-quality measurement** for the guard / STT / TTS stack.
