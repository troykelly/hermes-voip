# ADR-0004: Typed async provider interfaces (StreamingASR, StreamingTTS, InjectionGuard, MediaTransport)

- **Date:** 2026-06-14
- **Status:** Accepted
- **Deciders:** agent session (VoIP architecture, post-research)

## Context

The cascaded media path (ADR-0003) wires four swappable components into the core: a
streaming speech recogniser, a streaming synthesiser, a prompt-injection classifier, and the
SIP/WebRTC+RTP transport. Each has a research-selected default and at least one cloud
fallback (sherpa-onnx vs Deepgram Flux for STT in ADR-0006; sherpa-onnx/Kokoro vs
Cartesia/Deepgram/ElevenLabs for TTS in ADR-0007; an in-process DeBERTa ONNX guard in
ADR-0009; an in-process media engine — aiortc behind a thin SIP-over-TLS signalling layer as
the leading candidate, with PJSIP a first-class candidate — to be confirmed by the ADR-0005
spike). If the core imports any of those vendors by name, it stops being
gateway- and provider-agnostic and acquires exactly the lock-in that rule 40 forbids
introducing without an ADR. The choice that is "decided" must be a *seam*, not a vendor.

Three constraints bind the shape of that seam:

1. **Real-time, not batch.** The turn-taking budget (silence→first-audio under ~1 s) means
   every stage streams. Hermes' own provider hooks — `ctx.register_transcription_provider`
   (`transcribe(file_path) -> dict`) and `ctx.register_tts_provider`
   (`synthesize(text, output_path) -> path`) — are **whole-file batch** ABCs; the TTS
   `stream()` hook on Hermes' base class has no consumer. They cannot express a partial
   `Transcript` arriving mid-utterance or a `cancel()` that stops audio for barge-in. So the
   streaming shape Hermes lacks must be defined here, while the batch hooks are still
   registered for the non-realtime fallback (e.g. voicemail transcription, the
   `transcribe_audio` tool's 25 MB whole-file path).

2. **Audio representation must be uniform and codec-free at the boundary.** G.711 mu-law/a-law
   is the wire codec and the gateway runs at 8 kHz; STT/VAD want 16 kHz; TTS engines emit
   24 kHz or 16 kHz. If each provider negotiated its own codec and rate, the core would carry
   N×M conversion paths. Instead the *plugin's media layer* owns G.711 codec and 8↔16 kHz
   resampling (`audioop-lts`, since stdlib `audioop` is removed in Python 3.13), and every
   provider interface speaks one currency: linear PCM16 frames at a declared sample rate.

3. **Fully-typed, no escape hatches** (rules 17/39). The seam is the most-imported contract in
   the codebase; it must be clean under `mypy --strict` with no `Any`, and must let us prefer
   types over runtime checks (a graded `GuardVerdict`/`ToolRisk` discriminated by a typed
   field, not a dict probe).

The decision is *how* to express that seam, not *which vendor* — vendor selection lives in the
sibling ADRs and is reduced here to a config key.

## Decision

Every external, swappable component sits behind a typed async `Protocol` in
`src/hermes_voip/providers/`, resolved from config at startup by a registry; the core depends
only on the Protocols, never on a concrete vendor. Audio crossing any provider boundary is
**linear PCM16 framed at a declared sample rate** — codec (G.711) and resampling are the
plugin's media layer, not a provider concern.

### Shared audio currency

```python
# src/hermes_voip/providers/audio.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

PCM16_BYTES_PER_SAMPLE: Final[int] = 2


@dataclass(frozen=True, slots=True)
class PcmFrame:
    """A frame of signed 16-bit little-endian mono PCM at `sample_rate` Hz.

    `samples` length is `len(samples) // PCM16_BYTES_PER_SAMPLE` mono samples.
    Codec (G.711) and 8<->16 kHz resampling never appear here: by the time a
    frame reaches a provider it is already PCM16 at the provider's declared rate.
    `monotonic_ts_ns` is the de-jittered, gap-free presentation clock the media
    layer (ADR-0005) stamps on every frame so downstream stages (VAD/endpointing
    ADR-0008, A/V sync, barge-in timing) share one monotonic timebase. This is
    the single canonical `PcmFrame`; transport and provider ADRs import it and
    never redefine its fields.
    """

    samples: bytes
    sample_rate: int
    monotonic_ts_ns: int
```

### StreamingASR (see ADR-0006)

```python
# src/hermes_voip/providers/asr.py
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hermes_voip.providers.audio import PcmFrame


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    is_final: bool          # this hypothesis will not change
    end_of_turn: bool       # the speaker has yielded the floor (turn boundary)
    confidence: float       # 0.0..1.0


@runtime_checkable
class StreamingASR(Protocol):
    def stream(
        self, audio: AsyncIterator[PcmFrame]
    ) -> AsyncIterator[Transcript]:
        """Consume PCM16 frames, yield interim and final `Transcript`s.

        Drains `audio` until exhausted (caller closes on hang-up). Engines
        without native turn detection set `end_of_turn` from the VAD signal
        the media layer supplies (ADR-0008); fused engines (e.g. Deepgram Flux)
        set it natively.
        """
        ...

    @property
    def input_sample_rate(self) -> int:
        """Declared input rate; the media layer resamples to match (e.g. 16000)."""
        ...
```

### StreamingTTS (see ADR-0007)

```python
# src/hermes_voip/providers/tts.py
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from hermes_voip.providers.audio import PcmFrame


@runtime_checkable
class TtsStream(AsyncIterator[PcmFrame], Protocol):
    """Async iterator of PCM16 output frames with explicit lifecycle control."""

    async def flush(self) -> None:
        """Force synthesis of any buffered text and emit remaining frames."""
        ...

    async def cancel(self) -> None:
        """Stop synthesis NOW for barge-in: stop yielding and free the backend.

        Maps to the vendor primitive: Deepgram Aura `Clear`, Cartesia cancel,
        ElevenLabs websocket close_context, sherpa-onnx chunk callback return 0.
        """
        ...


@runtime_checkable
class StreamingTTS(Protocol):
    def synthesize(
        self, text: AsyncIterator[str], voice: str
    ) -> TtsStream:
        """Stream token/sentence text in, stream PCM16 frames out.

        `text` is the agent's incremental output; the engine begins emitting
        audio before `text` completes. `voice` is an opaque provider-scoped id.
        """
        ...

    @property
    def output_sample_rate(self) -> int:
        """Declared output rate; the media layer resamples to 8 kHz for G.711."""
        ...
```

**Calling convention (binding).** `StreamingASR.stream(...)` and `StreamingTTS.synthesize(...)`
are **synchronous factory methods** that return an `AsyncIterator` (a `TtsStream` for TTS) — they
are **not** `async def`; the caller iterates the returned async iterator, it does not `await` the
factory call. The concrete implementations in ADR-0006 (`StreamingASR`) and ADR-0007
(`StreamingTTS`/`TtsStream`) MUST conform to these exact signatures.

### InjectionGuard (see ADR-0009)

This is the single canonical prompt-injection contract; ADR-0009 imports it and
does not redefine `InjectionGuard`, `GuardResult`, or `GuardVerdict` with a
different shape. The verdict is **graded** (not a binary benign/injection label)
so the caller can degrade behaviour proportionally — proceed, clarify, restrict
the toolset, or refuse — and `degraded` records a fail-open guard so policy can
clamp the action surface even when classification was unavailable.

```python
# src/hermes_voip/providers/guard.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class GuardVerdict(Enum):
    """Graded screening outcome (ascending severity)."""

    ALLOW = "allow"        # benign turn; proceed normally
    CLARIFY = "clarify"    # ambiguous; ask a clarifying question, no tools this turn
    RESTRICT = "restrict"  # weak/medium signal; proceed least-privilege (read-only)
    REFUSE = "refuse"      # strong signal; refuse the instruction, flag, escalate


@dataclass(frozen=True, slots=True)
class GuardResult:
    verdict: GuardVerdict
    normalized_text: str            # after decode/normalize (base64/ROT13/homoglyph)
    reasons: tuple[str, ...]        # audit-log detail, never shown to the caller
    degraded: bool                  # True when the guard failed open (errored/unreachable)
    score: float                    # 0.0..1.0 raw detector probability


@runtime_checkable
class InjectionGuard(Protocol):
    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        """Screen one finalized, transcribed caller turn for prompt injection.

        `call_id` scopes per-session state (cumulative risk, rate of suspicious
        turns; ADR-0009). Returns a graded `GuardResult`, never a raw string.
        This is an early-warning LAYER, not the defense (ADR-0009): the caller
        decides policy on the typed verdict, and the enforceable control is the
        tool-policy gate below — not the classifier outcome alone.
        """
        ...
```

### Tool policy (the enforceable control — see ADR-0009)

The classifier has false negatives by construction, so the *enforceable* control
is a typed tool-policy gate, also canonical here. Every registered tool carries a
`ToolRisk`; per-session guard state carries the `degraded` flag (it follows the
session, set by any fail-open `GuardResult`); and a `pre_tool_call` hook **MUST**
gate every `irreversible` tool — requiring explicit human/DTMF confirmation
(ADR-0010) and hard-blocking while `degraded` — **regardless of the classifier
outcome**. A missed injection therefore still cannot reach an irreversible action.

```python
# src/hermes_voip/providers/policy.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ToolRisk(Enum):
    """Action risk class for a registered tool (ascending)."""

    SAFE = "safe"                  # read-only / no side effects
    ELEVATED = "elevated"          # mutating but reversible / low blast radius
    IRREVERSIBLE = "irreversible"  # payments, bookings, transfers, account mutation


@dataclass(slots=True)
class GuardSessionState:
    """Per-session guard state; lives for the call (ADR-0009)."""

    call_id: str
    degraded: bool = False                       # any fail-open screen sets this and it sticks
    flagged_turns: tuple[str, ...] = field(default_factory=tuple)

    def record(self, result: GuardResult) -> None:
        """Fold one screen into session state; degraded never un-sets within a call."""
        self.degraded = self.degraded or result.degraded


def gate_tool_call(risk: ToolRisk, state: GuardSessionState, *, confirmed: bool) -> bool:
    """`pre_tool_call` policy: may this tool run? Enforced regardless of verdict.

    An `IRREVERSIBLE` tool requires explicit confirmation (human/DTMF, ADR-0010)
    and is hard-blocked while the session is `degraded` — even if the classifier
    returned ALLOW (the miss case ADR-0009 tests for). Errors propagate; this
    never silently allows (rule 37).
    """
    if risk is ToolRisk.IRREVERSIBLE:
        return confirmed and not state.degraded
    if risk is ToolRisk.ELEVATED and state.degraded:
        return False
    return True
```

### MediaTransport (see ADR-0005)

```python
# src/hermes_voip/providers/transport.py
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from hermes_voip.providers.audio import PcmFrame


@runtime_checkable
class MediaTransport(Protocol):
    """The SIP/WebRTC signalling + RTP/SRTP media boundary.

    Hides G.711 codec, RTP packetisation, jitter buffering, and DTMF
    (RFC 4733, ADR-0010). Above this line everything is PCM16 frames.

    This is the single canonical media seam: there is exactly one media
    interface name (`MediaTransport`) with `inbound_audio()` / `send_audio()`
    and an `inbound_sample_rate` property. ADR-0005 implements this exact
    Protocol for its in-process engine; it does not define a second media
    protocol under a different name.
    """

    async def connect(self) -> bool:
        """Register the extension and establish signalling. Returns success."""
        ...

    async def disconnect(self) -> None:
        ...

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        """Far-end (caller) audio decoded to PCM16 at `inbound_sample_rate`."""
        ...

    async def send_audio(self, frame: PcmFrame) -> None:
        """Encode + packetise one near-end (agent) frame to the gateway."""
        ...

    @property
    def inbound_sample_rate(self) -> int:
        ...
```

### Selection and registration

Provider choice is config, never code. A registry maps a config key to a factory; the active
provider is named in plugin config and its credentials/endpoint come from `HERMES_SIP_*`,
`ELEVENLABS_API_KEY`, and peers — read at runtime from the gitignored `.env` (and 1Password
via the `op` CLI), never tracked. The Hermes integration in `plugin.yaml` declares those as
`requires_env`/`optional_env`.

```python
# src/hermes_voip/providers/registry.py  (sketch — fully typed, no Any)
from collections.abc import Callable, Mapping
from hermes_voip.providers.asr import StreamingASR

_ASR_FACTORIES: Mapping[str, Callable[[], StreamingASR]] = {
    "sherpa-onnx": _make_sherpa_asr,   # ADR-0006 default
    "deepgram-flux": _make_deepgram_asr,
}

def make_asr(name: str) -> StreamingASR:
    try:
        return _ASR_FACTORIES[name]()
    except KeyError as exc:                       # propagate, never swallow (rule 37)
        raise ValueError(f"unknown ASR provider: {name!r}") from exc
```

### Relationship to Hermes' batch hooks

We **keep** `ctx.register_transcription_provider` and `ctx.register_tts_provider`: the
plugin registers a thin batch adapter for non-realtime paths (voicemail/whole-file transcribe,
the `transcribe_audio` tool's ≤25 MB cap) by wrapping the same configured engine where it has a
file mode, or by selecting a separate batch engine. The streaming Protocols above are
**additive** — they express the partial-result / `cancel()` / mid-utterance shape the batch
ABCs structurally cannot, and they are what the live call loop (ADR-0003) consumes. The two
never overlap on the hot path.

## Consequences

- **Swapping a vendor is a config edit, not a code change.** Adding a provider is one factory
  plus a registry entry; the core, the call loop, and every test against the Protocols are
  untouched. This is what keeps rule 40 satisfied without per-vendor ADR churn.
- **One audio currency.** PCM16-at-declared-rate collapses the codec/resample matrix to a
  single media-layer responsibility (`audioop-lts`); providers never see G.711 or 8 kHz. Bugs
  in conversion live in one place with one test surface.
- **Testability is high.** Protocols are `runtime_checkable` and audio is plain frames, so a
  fake `StreamingASR` yielding scripted `Transcript`s, a fake `TtsStream` whose `cancel()`
  records that it fired, and a loopback `MediaTransport` give deterministic, vendor-free unit
  tests for the call loop (rule 18 TDD; fakes use `pbx.example.test`, ext `1000`).
- **We commit to maintaining the seam.** Any new provider capability (e.g. word-level
  timestamps, emotion tags) is a deliberate, typed extension to a Protocol, reviewed against
  every implementer — not an ad-hoc kwarg. A Protocol that drifts ahead of an implementer
  breaks `mypy`, which is the intended guard.
- **Latency is bounded by the interface, not hidden by it.** Because frames stream and
  `cancel()`/`flush()` are first-class, the budget in ADR-0003 (first-audio under ~1 s,
  TTS first-byte 100–300 ms) is *expressible*; all vendor numbers remain model-only until
  re-measured on our 8 kHz path (rules 23/24/26).
- **Minor cost:** two layers (streaming Protocol + batch Hermes hook) for transcription/TTS.
  Justified — they serve different shapes (live turn vs whole file) and the duplication is a
  thin adapter, not a second engine.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Hard-wire the chosen defaults (sherpa-onnx STT/TTS, aiortc) directly into the core | Bakes a vendor into a public, gateway-agnostic plugin — the exact lock-in rule 40 forbids introducing without an ADR. Every cloud-fallback decision (ADR-0006/0007) would become a core rewrite, and the test target's needs would leak into the core. |
| Reuse only Hermes' batch provider ABCs (`register_transcription_provider` / `register_tts_provider`) | They are whole-file batch: `transcribe(file_path)` / `synthesize(text, output_path)`. They cannot express interim `Transcript`s, `end_of_turn`, or a barge-in `cancel()`; the base `stream()` hook has no consumer. Real-time turn-taking is unrepresentable, so we add the streaming shape and keep the batch hooks only for the non-realtime fallback. |
| Pass file paths between stages (mirroring Hermes' `media_urls`) instead of PCM frames | A whole-file handoff serialises the pipeline — STT can't start until TTS-side audio is written and read back — destroying the sub-second budget and forcing disk I/O per turn. Frames let each stage begin on first audio. (The file-path shape is retained only at the Hermes adapter edge, ADR-0003, for non-realtime media.) |
| Let each provider negotiate its own codec/sample rate | Pushes an N×M conversion matrix into the core and couples it to vendor wire formats (some want native mu-law@8 k, others 24 kHz PCM). One PCM16 currency plus a single `audioop-lts` media layer is simpler, testable in one place, and keeps providers codec-agnostic. |
| One generic `Provider` Protocol with a `kind` field | Collapses four unrelated contracts into a stringly-typed mega-interface, defeating `mypy --strict` exhaustiveness and forcing runtime `kind` checks — the opposite of "prefer types over runtime checks" (rule 17). Four distinct Protocols give per-seam type safety. |

