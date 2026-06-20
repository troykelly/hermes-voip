# ADR-0064: Call-progress detection — fax tones (CNG/CED), answering-machine detection, and the leave-message protocol

- **Date:** 2026-06-19
- **Status:** Accepted
- **Deciders:** operator request (2026-06-19, full scope) — agent session (call-progress lane)

## Context

The conversational media plane (ADR-0003/0005/0008) assumes the far end is a **human
caller**. Two real telephony events break that assumption and need handling before launch:

1. **A fax/modem is on the line.** Either a calling fax dials the agent's extension
   (inbound) or the agent's outbound call reaches a fax/modem (outbound). A fax tone fed
   into STT produces garbage transcripts and a fax that hears speech instead of an answer
   tone retrains repeatedly — both waste the call. ITU-T T.30 specifies two diagnostic
   tones: **CNG** (the *calling* fax, 1100 Hz, cadence 0.5 s on / 3 s off) and **CED** (the
   *answering* fax/modem, 2100 Hz, ~2.6–4 s continuous).
2. **An answering machine / voicemail answered an outbound call.** The agent should not
   talk over a greeting; it should wait for the record cue (beep) and then leave a message,
   or hang up. This is **answering-machine detection (AMD)** plus a **leave-message**
   protocol.

Constraints (AGENTS.md): sans-IO and deterministic so it is unit-testable with no sockets,
threads, or wall clock (the engine's existing injectable-clock discipline, ADR-0061);
fully-typed under `mypy --strict` with no escape hatch (17/39); O(n)-per-frame on the audio
hot path like the existing Goertzel DTMF detector (22); errors propagate (37); minimal
in-scope diff (28); the repo is PUBLIC so only public tone constants appear (34). The live
engine/adapter **wiring is a same-push follow-on** (rule 6 — named below, not deferred).

The existing DSP this builds on:

- `hermes_voip.dtmf._goertzel_power(samples, freq, sample_rate)` — a single-bin DFT power,
  O(n) with two multiplies per sample. Reused directly for every tone test here.
- `hermes_voip.dtmf.InbandDtmfDetector` — the false-positive-rejection discipline this
  mirrors: a tone counts only when it holds a high fraction of the frame's energy AND
  dominates its neighbour bins, with a consecutive-frame debounce. Speech spreads energy
  across a harmonic series and never sustains a single pure bin, so this rejects it.
- `hermes_voip.media.vad` (silero VAD, native 8 kHz or 16 kHz) + `media.endpoint`
  (end-of-turn by trailing silence). VAD emits `VadEvent(edge, frame_index, probability)`
  ONSET/OFFSET edges on a monotonic 32 ms window clock. The AMD classifier consumes the
  **speech/silence segment durations** derived from those edges.

The inbound pipeline runs at 16 kHz (resampled from the 8 kHz G.711 wire for the STT
zipformer, ADR-0017); the outbound/wire side is 8 kHz G.711. The detector is therefore
**sample-rate parameterized** — it never hardcodes 8000.

## Decision

Add a **sans-IO** module `src/hermes_voip/media/call_progress.py` that receives decoded
`PcmFrame`s and VAD edges and **returns typed events**. It opens no sockets and imports no
Hermes runtime. Three cooperating pieces behind one `CallProgressDetector` facade:

### 1. Fax tone detection (Goertzel, both directions)

Two single-tone detectors over decoded PCM, each reusing `_goertzel_power` and the
`InbandDtmfDetector` rejection discipline (energy floor + per-frame fraction + bin
dominance + a consecutive-frame run):

- **CNG — 1100 Hz**, the *calling* fax (relevant **inbound**: a fax dialed the agent). The
  ITU-T T.30 cadence is 0.5 s on / 3 s off; a single ~0.5 s burst of clean 1100 Hz is
  already strongly diagnostic, so the detector emits `FaxCng` once **one** burst of the
  expected on-duration (within a tolerance window) completes. (Requiring a full 3.5 s cadence
  cycle only adds latency; a sustained pure 1100 Hz tone is not something speech produces.)
- **CED — 2100 Hz**, the *answering* fax/modem (relevant **outbound**: the agent reached a
  fax). T.30 specifies ~2.6–4 s continuous. The detector emits `FaxCed` once a continuous
  2100 Hz run reaches a configurable minimum (default 1.0 s — long enough that voiced
  speech cannot masquerade as a sustained single tone, short enough to abort the call
  before the modem handshake). 2100 Hz also covers the answer-tone band (the ANS/ANSam tone
  used by V.8/V.25 modems sits at 2100 Hz); phase reversals in ANSam do not defeat a
  magnitude-only Goertzel.

Tone constants and thresholds are module-level `Final` with cited rationale. The fax
frequencies (1100, 2100 Hz) are public ITU-T T.30 constants.

### 2. Answering-machine detection (AMD) — a sans-IO state machine over VAD segments

Fed the **speech/silence segment durations** (seconds) the VAD stream produces. The
heuristic (the long-understood industry approach):

- **Human** ≈ a *short* greeting ("Hello?", < ~2 s of speech) followed by a *pause*
  awaiting a response (a trailing silence ≥ a response-gap threshold). Classified
  `LikelyHuman`.
- **Machine** ≈ a *long* greeting — continuous speech beyond a machine threshold
  (default 3.5 s) — and/or a detected beep. Classified `AnsweringMachine`.

The verdict fires on a **completed** VAD segment, never mid-segment: the detector is fed
only closed speech/silence runs (a speech run closes on its OFFSET edge, a silence run on
the next ONSET). A long enough opening greeting fires `AnsweringMachine` when that opening
speech run **closes** (the OFFSET edge) and its accumulated speech duration has reached the
machine threshold — so a machine that talks continuously without ever pausing yields no
verdict from this state machine until it first stops. Otherwise the verdict fires at the
**first** silence that follows the opening speech — if the opening speech was short and the
trailing silence reaches the response gap it is a human, and if it spoke for a while
(between the human and machine thresholds) then went quiet it is a machine. Internal pauses
inside a machine greeting ("Hi, you've reached
… *beat* … please leave a message") are handled by accumulating speech across short gaps (a
gap shorter than the response gap does **not** end the greeting).

The classifier is fed **only the VAD segment durations** — no STT transcript participates.
(An STT-corroboration input was considered and rejected for this lane: the facade has no
transcript-input path yet, and claiming STT-corroborated AMD that the code does not wire
would be aspirational, rule 27. The `Final` thresholds leave a clean seam to add it later.)

**Applicability:** AMD is **outbound-only**. On an *inbound* call the agent **is** the
answerer — there is no greeting to classify — so the facade gates AMD behind a per-call
`outbound` flag. Fax detection runs in **both** directions.

**Honesty (rule 27):** AMD is heuristic. Even commercial carrier-grade AMD lands roughly
**85–95 %** accurate and trades false-positives (a slow human classed as a machine) against
latency (waiting longer to be sure). The thresholds are exposed as tunable `Final`
constants and documented as such here and in the module docstring — this ADR does **not**
claim deterministic AMD.

### 3. Beep detection + the leave-message protocol

A machine's record cue is a sustained single tone, conventionally ~1000 Hz (commonly
1000–1100 Hz). A third Goertzel single-tone detector watches a ~1000 Hz band for a short
sustained burst (default ≥ 0.2 s). Two paths produce the record cue:

- **Beep heard:** once `AnsweringMachine` has been classified, a detected beep emits
  `ReadyToLeaveMessage(beep_at_s=<t>)`.
- **Fallback (no beep):** many machines emit no beep or one outside the band. Once
  `AnsweringMachine` is classified, the fallback emits `ReadyToLeaveMessage(beep_at_s=None)`
  after the post-greeting silence has lasted the response gap. **This fallback is driven by
  the audio/sample clock in `on_audio_frame`, not by a VAD edge** — a machine that has
  stopped talking to record sends no further VAD ONSET/OFFSET, so waiting for an edge would
  hang forever. An OFFSET stamps the silence start **from the edge's own window time** (its
  ordinal × the 32 ms window duration, the same call-elapsed timeline the sample clock uses)
  and an ONSET clears it (resetting the timer if the machine resumes speaking before the gap
  elapses); each fed audio frame then checks whether the gap has been reached. Stamping from
  the edge's own time (not from "samples seen when `on_vad_event` was called") makes the
  timer independent of how the caller interleaves `on_audio_frame` and `on_vad_event`. So the
  agent is never stuck waiting for a beep — or an edge — that never comes.

The detector only ever **emits the cue**. The hang-up-vs-speak decision belongs to the
**Hermes agent**, which already has the call-control tools (ADR-0009/0010/0031) to hang up
or to speak a message. The wiring step surfaces these events to the agent; the detector
holds no policy.

### Event model — a typed discriminated union

Frozen, slotted dataclasses, each tagged with a `Literal` `kind`, unioned as
`CallProgressEvent`. Each carries `elapsed_s` (seconds since detector start, computed as the
accumulated audio sample count divided by the sample rate — exact under variable or
zero-length frames):

- `FaxCng(kind="fax_cng", elapsed_s)`
- `FaxCed(kind="fax_ced", elapsed_s)`
- `AnsweringMachine(kind="answering_machine", elapsed_s, beep_at_s: float | None, why: str)`
- `LikelyHuman(kind="likely_human", elapsed_s, why: str)`
- `ReadyToLeaveMessage(kind="ready_to_leave_message", elapsed_s, beep_at_s: float | None)`

Consumers match exhaustively on `kind` with no catch-all default (rule 17); `why` carries a
short human-readable rationale for logging/observability, not control flow.

### Facade entry points (what the same-push wiring calls)

```python
class CallProgressDetector:
    def __init__(self, *, sample_rate: int, outbound: bool) -> None: ...
    def on_audio_frame(self, frame: PcmFrame) -> CallProgressEvent | None: ...
    def on_vad_event(self, event: VadEvent) -> CallProgressEvent | None: ...
    def reset(self) -> None: ...
```

The thresholds are module-level `Final` constants (not constructor arguments) so tuning is
a single reviewable edit. `on_audio_frame` advances the sample clock, runs the three
Goertzel tone detectors (CNG/CED/beep), and fires the no-beep record-cue fallback once the
post-greeting silence reaches the response gap. `on_vad_event` segments the VAD edge stream
(ONSET→OFFSET = a speech segment; OFFSET→next ONSET = a silence segment), converts window
ordinals to seconds with the silero 32 ms window duration, drives the AMD state machine, and
stamps/clears the silence boundary the fallback times against. Both are O(n) in the frame
length and allocation-light, matching the per-frame CPU
budget (rule 22). The inner `_FaxToneDetector` and `_AnsweringMachineDetector` are
independently unit-testable (raw PCM frames / raw segment durations) without the facade.

### Wiring (the same-push follow-on, #43)

The detector is wired into the **`CallLoop` pump** (`media/call_loop.py`), not split across
`engine.py` + `adapter.py`. The pump is the single component that holds **both** the decoded
inbound `PcmFrame` **and** its VAD `frame_events` on one shared call-elapsed timeline — which
is exactly what the detector's two input streams need (the no-beep record-cue fallback is
interleaving-independent only because both the audio sample clock and the VAD window-ordinal
clock derive from the same audio; feeding them from two different objects/tasks would
reintroduce the ordering coupling the codex round-2 fix removed). The engine's `__init__` also
does not know the call direction, and bridging audio-path events from `engine._inbound_gen` (a
different `asyncio` task) to the loop would need a cross-task queue — more coupling for no gain.

* `CallLoop` gains two keyword-only constructor params: `call_progress_detector:
  CallProgressDetector | None` and `call_progress_callback: Callable[[CallProgressEvent],
  Awaitable[None]] | None`. Both `None` ⇒ the feature is OFF and the pump skips the feed (zero
  cost). The pump feeds **every** decoded frame to `on_audio_frame` and **every** VAD edge to
  `on_vad_event`, *unconditionally* — independent of the ADR-0023 echo-suppression branch, and
  continuing through post-greeting silence (the audio-clock fallback fires when no further VAD
  edge arrives). Each emitted event is surfaced through the callback as a tracked best-effort
  task off the hot path (cancelled+joined at loop teardown, like the DTMF delivery tasks).
* `adapter.py` `_run_call_loop` takes an explicit `outbound: bool` (inbound call site `False`,
  the two outbound UAC sites `True`) and, when `MediaConfig.enable_call_progress`, constructs
  `CallProgressDetector(sample_rate=engine.inbound_sample_rate, outbound=(outbound and
  enable_amd))` — so AMD/record-cue are inert unless the call is outbound **and** AMD is
  enabled, while fax detection (direction-independent) still runs.
* `_handle_call_progress` is the callback. It ADVISES the agent and acts: `FaxCng`/`FaxCed`
  inject a system turn and (when `amd_hang_up_on_fax`, the default) auto hang up via the soft
  `hang_up_call` path; `AnsweringMachine` injects a voicemail-context system turn so the agent
  decides hang-up-vs-wait; `ReadyToLeaveMessage` injects the record cue; `LikelyHuman` is
  advisory only (no turn, no hangup). System turns go through the same `_call_source` +
  `internal=True` `MessageEvent` path as the objective/context seeds (ADR-0029/0052).
* Three new `MediaConfig` switches, all safe-by-default: `enable_call_progress`
  (`HERMES_VOIP_CALL_PROGRESS`, default **off** — the whole feature), `enable_amd`
  (`HERMES_VOIP_AMD`, default **off**), `amd_hang_up_on_fax` (`HERMES_VOIP_AMD_HANGUP_ON_FAX`,
  default **on**).

## Consequences

- **Easier:** the engine/adapter can route a fax-tone call away from STT (hang up or hand
  to a fax path) and the agent can run a real voicemail flow instead of talking to a beep.
  Fax detection is reliable (pure tones); AMD gives a useful, honestly-bounded signal.
- **Committed to maintain:** a small DSP module and a heuristic whose thresholds will need
  live re-measurement against real machines/faxes (the thresholds are `Final` constants with
  cited defaults precisely so that tuning is a one-line, reviewable change). The honesty bar
  (rule 27) means we never present AMD as exact.
- **Performance:** three extra single-bin Goertzel passes per audio frame on top of the
  existing VAD/STT/DTMF work. Each is O(n) (n = 160 samples per 20 ms G.711 frame; ~480 at
  16 kHz), the same order as one `InbandDtmfDetector` low+high+harmonic pass already on the
  path. No FFT, no allocation beyond the per-frame sample list the detectors already build.
- **Wiring shipped (#43):** the detector is live in the `CallLoop` pump and surfaced to the
  agent by `adapter._handle_call_progress`, gated by the three `MediaConfig` switches above
  (see **Wiring** under Decision). It went in as `media/call_loop.py` + `adapter.py` +
  `config.py`, NOT `engine.py` — the pump, not the engine, is where both detector input
  streams meet on one timeline. The module (#149) and the wiring (#43) shipped across the
  same launch push (rule 6).
- **No new dependency:** the detector uses stdlib `math`/`struct`/`array` + the existing
  `_goertzel_power`; the wiring adds no import beyond `media.call_progress`. No
  `ml`/`media`/`webrtc` extra is required to import or test the detector + the `CallLoop`
  wiring (the adapter surfacing test runs in the hermes-contract job, like the other adapter
  tests).

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| FFT-based spectral analysis for fax tones | A full FFT is O(n log n) and allocates per frame; we need only three single bins (1100, 2100, ~1000 Hz), which Goertzel computes in O(n) with two multiplies per sample — the pattern already proven in `InbandDtmfDetector`. |
| Require the full CNG 0.5 s-on/3 s-off cadence before emitting `FaxCng` | Adds ≥ 3.5 s of latency for no real gain: a sustained pure 1100 Hz burst is already not something voiced speech produces, and the energy-fraction + bin-dominance tests reject speech without needing the off-period. |
| A learned (ML) AMD classifier | New model weights + an `onnxruntime` dependency on the hot path for a feature whose heuristic form is industry-standard and good enough; rule 40 bars defaulting in new infra, and the honest accuracy ceiling (~85–95 %) is roughly the same. The `Final` thresholds leave a clean seam to swap one in later (mirroring the Smart-Turn-v2 seam behind the endpointer). |
| Run AMD on inbound calls too | On an inbound call the agent **is** the answerer; there is no far-end greeting to classify, so inbound AMD would only mis-classify the caller's first utterance. AMD is gated outbound-only; fax detection stays bidirectional. |
| Let the detector decide hang-up vs leave-message | Policy belongs to the Hermes agent, which already owns the call-control tools (ADR-0009/0010/0031). The detector emitting a `ReadyToLeaveMessage` cue and leaving the decision to the agent keeps the detector sans-IO and policy-free. |
| Put the AMD timing on a wall clock | Non-deterministic and untestable; the VAD already stamps a monotonic 32 ms window ordinal (ADR-0008), so converting ordinals→seconds gives an exact, offline-testable timer with no clock drift (same discipline as `media.endpoint`). |
| Fire the no-beep record-cue fallback from a VAD edge (the next ONSET closing the silence) | Dead in the common case: a machine that has stopped talking to record emits **no further VAD edge**, so the cue would never fire and the agent would wait forever. The fallback is driven by the audio/sample clock in `on_audio_frame` instead — an OFFSET stamps the silence start, an ONSET clears it, and each fed frame checks the gap. (Caught by codex review of PR #149.) |
| Stamp the no-beep silence start from `samples_seen` at the moment `on_vad_event` is called | Couples the timer to caller interleaving: an OFFSET delivered before the matching audio has advanced the sample clock starts the timer early (can fire while greeting audio still feeds); after, late. Stamp from the **edge's own window time** instead (its ordinal × the window duration, the same call-elapsed timeline) so the fallback is interleaving-independent. (Caught by codex round-2 review of PR #149.) |
| Compute `elapsed_s` / tone-run lengths from a frame **count** × frame duration | Wrong under variable or zero-length frames (real on the wire): the count drifts from true elapsed time the moment frame sizes vary. `elapsed_s` is the accumulated sample count ÷ rate, and each tone run accumulates **seconds** (per-frame duration), so both stay exact. (Caught by codex review of PR #149.) |
| Feed an optional STT greeting-text length into AMD as machine corroboration | The facade has no transcript-input path, so the constructor would advertise a parameter the engine/adapter never supplies — aspirational (rule 27). Dropped for this lane; the `Final` thresholds leave a clean seam to add a real transcript input (with its own test) later. |
