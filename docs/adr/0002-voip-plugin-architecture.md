# ADR-0002: VoIP as a kind:platform plugin that owns the real-time media plane in-process

- **Date:** 2026-06-14
- **Status:** Accepted
- **Deciders:** agent session (VoIP architecture, post-research)

## Context

`hermes-voip` must give a Hermes agent two-way voice over telephony by registering as an extension on any RFC-compliant SIP-over-TLS or WebRTC gateway. AGENTS.md rule 40 binds this repository to be a Python *package loaded by the Hermes runtime* — not a deployed service — and forbids introducing any hosting platform, cloud, SaaS, or external media server without explicit operator approval recorded in an ADR. So the first architectural question is *what kind of Hermes extension this is* and *where the real-time audio actually runs*.

The Hermes plugin system (verified against `NousResearch/hermes-agent@main`) constrains the shape:

- Plugins are **in-process Python** loaded at startup: a directory with `plugin.yaml` (declaring `kind ∈ {standalone, backend, exclusive, platform, model-provider}` plus `requires_env`/`optional_env`) and an `__init__.py` exposing `register(ctx)`. Distribution is a pip entry point in group `hermes_agent.plugins`.
- The voice-channel seam is `ctx.register_platform(name, label, adapter_factory, check_fn, validate_config, required_env, ...)`. There is **no** `register_adapter`; a voice channel *is* a platform.
- `BasePlatformAdapter` (`gateway/platforms/base.py`) has exactly four abstract async methods: `connect() -> bool`, `disconnect() -> None`, `send(chat_id, content, reply_to, metadata) -> SendResult`, `get_chat_info(chat_id) -> dict`. Inbound is **one discrete `MessageEvent` per turn** via `self.handle_message(event)`; media rides as **file paths** in `media_urls`, never bytes or streams.
- The built-in STT (`agent/transcription_provider.py`) and TTS (`agent/tts_provider.py`) providers are **whole-file batch** (`transcribe(file_path) -> dict`, `synthesize(text, output_path) -> path`). A `stream()` hook exists on TTS but has **no consumer**. The streaming token tap (`agent.stream_delta_callback` / `MessageChunk`) is delivered by `GatewayStreamConsumer` as rate-limited chat-message *edits* — the wrong shape for audio.
- The adapter and the agent share **one process and one event loop** (a synchronous `queue.Queue` handoff). Any SIP/RTP media stack therefore *must* run in-process. The `plugins/google_meet/` plugin is the precedent: it runs a live-audio engine (OpenAI Realtime, pcm16/24k, with `cancel_response()` for barge-in) **out-of-band**, not as a platform adapter's batch path. The `plugins/platforms/irc/` adapter is the cleanest template for a `connect()`-owns-the-socket platform.
- Hermes' own "voice mode" is **half-duplex** and mic-deaf during TTS (no barge-in), so the batch STT/TTS path cannot meet the conversational latency bar (silence→first-audio target <~800 ms–1 s; everything must stream — see ADR-0003).
- Gating gotcha: a `kind:platform` plugin **auto-loads only when `source == bundled`**. A pip-installed plugin (us) remains gated by `config.yaml` `plugins.enabled`, so operators must explicitly enable it.

We must decide what kind of plugin this is, where the media plane lives, and how a phone call maps onto Hermes' session/chat model — without forking Hermes core (rule 28: minimal in-scope diffs) and without standing up infrastructure the operator has not approved (rule 40).

## Decision

`hermes-voip` ships as a **pip-distributed Hermes plugin** (entry point in group `hermes_agent.plugins`) whose `plugin.yaml` declares **`kind: platform`**, and whose `register(ctx)` calls **`ctx.register_platform(...)`** to register a single `BasePlatformAdapter` subclass under the platform name **`voip`**. The adapter is **only the control + finalized-text seam**; the **entire real-time media plane runs in-process inside the adapter**, invisible to Hermes core, mirroring the `plugins/google_meet` out-of-band precedent.

Concrete shape:

- **Package & entry point.** `src/hermes_voip/` is the package; `pyproject.toml` declares:

  ```toml
  [project.entry-points."hermes_agent.plugins"]
  hermes_voip = "hermes_voip:register"
  ```

  `plugin.yaml` (sibling of the package `__init__.py`):

  ```yaml
  kind: platform
  name: voip
  label: "VoIP (SIP/WebRTC telephony)"
  requires_env:
    - HERMES_SIP_HOST
    - HERMES_SIP_EXTENSION
    - HERMES_SIP_PASSWORD
  optional_env:
    - HERMES_SIP_PORT
    - HERMES_SIP_TRANSPORT   # "tls" | "wss"; default decided in ADR-0005
  ```

- **`register(ctx)` is the only public surface.** It is fully typed and calls exactly one seam:

  ```python
  from hermes_cli.plugins import PluginContext  # type: PluginContext

  def register(ctx: PluginContext) -> None:
      ctx.register_platform(
          name="voip",
          label="VoIP (SIP/WebRTC telephony)",
          adapter_factory=create_voip_adapter,   # (PlatformConfig) -> VoipAdapter
          check_fn=check_voip_ready,              # () -> bool, cheap readiness probe
          validate_config=validate_voip_config,   # (PlatformConfig) -> None, raises on bad config
          required_env=("HERMES_SIP_HOST", "HERMES_SIP_EXTENSION", "HERMES_SIP_PASSWORD"),
      )
  ```

  No `register_tts_provider` / `register_transcription_provider` call is made for the live path — those are batch and are deliberately bypassed (see Consequences). Tools, hooks, and the injection guard register separately (ADR-0009) but do not change this platform seam.

- **The adapter owns the socket and the media plane.** `VoipAdapter(BasePlatformAdapter)` implements the four abstract methods. `connect()` brings up SIP-over-TLS/WebRTC signalling and the SRTP/RTP media stack (ADR-0005) and registers the extension; it returns `True` only once registered. Inside `connect()` (and the per-call tasks it spawns on the shared loop) live VAD/endpointing (ADR-0008), streaming STT (ADR-0006), streaming TTS (ADR-0007), barge-in, and DTMF (ADR-0010). None of this is visible to Hermes core. Off-loop callbacks from the media stack are marshalled back onto the agent loop via `agent.async_utils.safe_schedule_threadsafe` (exact signature to be verified in implementation, rule 23).

- **The adapter↔core contract is text only.**
  - *Inbound:* when an utterance is finalized, the adapter builds one discrete `MessageEvent` of `MessageType.VOICE` carrying the **transcript** as `text=...` and (optionally) the captured-audio **file path** in `media_urls`, then calls `self.handle_message(event)`. Because the transcript is already populated, Hermes' auto-STT need not re-run; the file path is retained only for tooling/forensics under the 25 MB cap.
  - *Outbound:* Hermes calls `adapter.send(chat_id, content, reply_to, metadata)` with the agent's reply **text**; the adapter renders it to speech via its in-process streaming TTS and returns a `SendResult`. The batch `synthesize(...)`/`play_tts` override path is *not* used for the live conversation.

- **A call is a Hermes session.** The adapter maps one phone call to one session/chat:
  - `chat_id` ← SIP `Call-ID` (one call = one chat = one session).
  - `user_id` ← caller identity (e.g. `From` URI user-part / P-Asserted-Identity), normalized.
  - `chat_type` = `"dm"` (a call is one-to-one).
  - The registered `PlatformEntry` sets `pii_safe=True` so caller identifiers are handled as PII.
  - `Platform._missing_` lets the `"voip"` platform name resolve without editing the `Platform` enum in core.

- **Secrets via config, never in git.** SIP credentials are read at runtime from `HERMES_SIP_*` environment variables (sourced from the gitignored `.env` and 1Password via the `op` CLI) and seeded into `PlatformConfig.extra`; the adapter reads them only from `PlatformConfig`, never from a tracked file. Tests and examples use obvious fakes (host `pbx.example.test`, extension `1000`). No real host, extension, password, or vault/item name appears anywhere in the package, tests, or docs (CLAUDE.md secrecy invariant, rule 34).

- **Operator enablement is documented, not assumed.** Because we are pip-installed (not bundled), `kind: platform` does **not** auto-load us; the operator must enable the plugin in `config.yaml` `plugins.enabled` (`hermes plugins enable hermes-voip`). This is a one-line operational step captured in the runbook (rule 42), not in this ADR.

This ADR fixes *the seam and the process model*. The cascaded STT→Hermes→TTS pipeline that rides this seam is ADR-0003; the provider abstraction that keeps STT/TTS/VAD swappable is ADR-0004; the SIP/WebRTC transport and SRTP/RTP media stack are ADR-0005.

## Consequences

What gets easier:

- **Zero Hermes-core changes.** We use only the published `register_platform` seam and the four-method `BasePlatformAdapter` contract; Hermes upgrades that preserve that contract leave us working. No fork to rebase, no patch to maintain (rule 28).
- **No new infrastructure.** The media plane is a library running inside the Hermes process, so we introduce no hosting platform, cloud, media server, or SaaS — staying inside rule 40 without an operator approval gate. We are governed by Hermes' own deployment, lifecycle, and resource envelope.
- **Hermes stays the brain.** Inbound transcripts and outbound replies flow through the normal session/agent pipeline, so memory, tools, hooks, and the injection guard (ADR-0009) all apply unchanged. A call behaves like any other DM session.
- **Clean swap surface.** Because the live path bypasses the batch providers and hides everything behind the adapter, the STT/TTS/VAD/transport choices (ADR-0004…0008) can change without touching the core seam.

What gets harder / what we commit to maintaining:

- **We own a real-time media stack inside a shared event loop.** All SIP/RTP/SRTP, jitter buffering, VAD, streaming STT/TTS, barge-in, and DTMF run on the agent's loop; CPU-bound or blocking work *will* stall the agent and must be offloaded to threads/processes and bridged back via `safe_schedule_threadsafe`. This is a standing efficiency obligation (rule 22), and every vendor latency figure from research is model-only and must be re-measured on our 8 kHz telephony path on the real gateway (rules 23/26).
- **`MessageType.VOICE` semantics are repurposed.** We emit `VOICE` events whose `text` is already the transcript, so Hermes' auto-STT is intentionally a no-op for us; we must keep this assumption true as Hermes evolves (rule 27 — no aspirational behaviour).
- **Operator enablement is a required step.** The pip-install gating means a deploy is silent-fail-prone if the operator forgets to enable the plugin; the runbook must make the `plugins.enabled` step explicit and verifiable.
- **One process, one media stack.** Sharing the agent loop means we cannot horizontally scale media independently of Hermes; concurrency limits (max simultaneous calls) are bounded by the host running Hermes. This is accepted for the first test target and revisited only if call volume forces an out-of-process media server — which would itself require an operator-approved ADR (rule 40).
- **Lock-in posture:** none introduced here. The seam is Hermes' own published interface; the gateway stays vendor-neutral (the first test gateway is only a test target, not a design assumption). Cost is compute only (no per-call SaaS at this layer); provider-level cost/lock-in lives in ADR-0006/0007.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Fork/patch Hermes core to add first-class real-time audio primitives (duplex media, streaming sink) | Violates rule 28 (minimal in-scope diffs) and creates a permanent rebase/maintenance burden; the published `register_platform` + `BasePlatformAdapter` seam plus the `google_meet` out-of-band precedent already make an in-process media plane possible without touching core. |
| Run the media plane in an **out-of-process media server** bridging to a thin in-process adapter | Introduces external infrastructure forbidden by rule 40 without explicit operator approval recorded in an ADR; also adds an extra network hop and serialization on the latency-critical path (ADR-0003 budget). Kept on the table only as an operator-gated future option if single-process concurrency becomes the bottleneck. |
| Rely solely on Hermes' **batch STT/TTS + `MessageType.VOICE`** (built-in voice mode) | The built-in path is whole-file batch and half-duplex (mic-deaf during TTS, no barge-in); the TTS `stream()` hook has no consumer and the token tap delivers as rate-limited message edits — structurally unable to meet the streaming, sub-second, barge-in-capable conversational bar (ADR-0003/0008). |
| Register as a non-platform kind (e.g. `standalone`/`backend`) or via a (nonexistent) `register_adapter` | There is no `register_adapter`; a voice channel *is* a platform in Hermes, and only `register_platform` wires the inbound `handle_message` / outbound `send` channel seam we need. Other kinds do not get the chat/session channel. |
| Model a call as something **other than a Hermes session** (e.g. a tool invocation, a background task, or a long-lived multi-call chat) | Breaks the natural one-call/one-conversation mapping and bypasses session lifecycle, memory, and PII handling. Mapping `Call-ID → chat_id`, caller → `user_id`, `chat_type='dm'`, `pii_safe=True` reuses Hermes' session machinery exactly as intended and keeps each call's context isolated. |

