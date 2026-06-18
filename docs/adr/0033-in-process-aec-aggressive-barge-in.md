# ADR-0033: In-process acoustic echo cancellation (NLMS) + aggressive barge-in (extends ADR-0023/0028)

- **Date:** 2026-06-17
- **Status:** Accepted
- **Deciders:** agent session (VoIP media)
- **Extends:** ADR-0023 (echo-robust barge-in), ADR-0028 (barge-in clean stop), ADR-0008 (the
  deferred Phase-2 AEC), ADR-0017/0022/0032 (the outbound/inbound rate paths)

## Context

ADR-0023 stopped the live self-interruption loop (the gateway reflects the agent's own TTS
back on the inbound leg; the VAD/ASR transcribe it as the caller and barge the agent in) with
a **temporal** discriminator: a barge-in counts, while the agent's TTS is playing, only once it
is a *sustained* voiced run of at least `HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS` (default **600 ms**,
≈ 19 VAD windows — above the longest observed echo burst). That works, but it makes barge-in
**sluggish**: a caller must talk continuously for 600 ms over the agent before they are heard.
ADR-0028 then made the *action* clean (flush + fade), and both ADRs recorded the same explicit
follow-up (rule 6): a full in-process **acoustic echo canceller (AEC)** so the gateway's
reflected TTS is cancelled *before* the VAD/ASR see it, letting the sustained threshold drop and
barge-in become responsive **without** echo false-positives. ADR-0008 had deferred exactly this,
pending an AEC-engine choice. This ADR makes that choice, builds it, wires it, and lowers the
threshold.

**The signal we can exploit.** The echo is a (delayed, filtered, attenuated) copy of an audio
signal **we already have in hand** — the outbound TTS PCM the engine is sending on the wire. AEC
is therefore a *reference-based* cancellation: estimate the echo from the known far-end
(outbound) reference and subtract it from the near-end (inbound) signal, leaving only the
caller's genuine speech (plus residual). This is the classic line-echo / acoustic-echo problem;
the standard tool is an **adaptive FIR filter** that continuously learns the echo path.

**Library survey (rule 35 — canonical registry, permissive licence, available wheel).** None of
the obvious permissively-licensed AEC libraries is cleanly installable on our pinned runtime
(CPython 3.13, `uv`, x86_64):

| Candidate | Licence | Why rejected |
| --- | --- | --- |
| `webrtc-audio-processing` 0.1.3 (the WebRTC APM) | BSD-3 | Ships **only** `cp27`/`cp36` `linux_armv7l` wheels — no `cp313`, no `x86_64`. Unusable on the pinned runtime; building from source pulls a large C++ tree + abseil. |
| `speexdsp` 0.1.1 | BSD-3 | **sdist-only** (no wheels at all); needs the system `libspeexdsp` dev headers + a C build at install — fails a frozen `uv sync` on a host without it, and adds a system-lib runtime dependency. |
| `speexdsp-python` | — | **Does not exist** on PyPI (404). |

Per the task's stated fallback ("if none is clean, implement a well-tested adaptive-filter
(NLMS) echo canceller in-package, fully typed"), and because an in-package canceller also
**removes FFI/C-build cost from the hot path** (rule 22), we implement the AEC ourselves.

## Decision

**Build a pure-stdlib NLMS (Normalised Least-Mean-Squares) adaptive-filter echo canceller in
`src/hermes_voip/media/aec.py` and run it at the engine RX, before the VAD/ASR — then lower the
barge-in sustained threshold when AEC is on.** No new dependency (the supply-chain surface is
unchanged): the filter math is `array('d', …)` (contiguous C-double storage) over the Python
stdlib only — the canceller lives in the `media`-extra engine path, which must **not** pull the
`ml` extra's numpy.

### The canceller (`EchoCanceller`)

It operates entirely at the inbound **analysis rate** (8 kHz G.711, 16 kHz G.722, 16 kHz for
Opus — the rate `inbound_audio()` delivers and the VAD/ASR consume). Two inputs:

- **`push_reference(pcm16, *, sample_rate)`** — the engine taps the outbound wire-rate PCM the
  instant it goes on the wire: the paced path (`_transmit_frame`) and the inline teardown/fade burst
  (`_emit_inline_frames`). The one frame *not* tapped is the deadline-pacer's parked
  `_inflight_wire` re-sent by `_flush_tx_tail` on a stop-race — its source PCM was discarded after
  encode (only the RTP bytes survive), so it cannot be pushed; it is a single end-of-call frame, so
  the missed reference is immaterial (the call is tearing down). When the wire rate differs from the
  analysis rate (Opus: 48 kHz wire → 16 kHz analysis) the reference is downsampled to the analysis
  rate by an internal state-carrying `Resampler` (the same conversion the inbound Opus path uses),
  so the canceller's far-end and near-end are always the same rate. The samples append to a bounded
  far-end FIFO consumed by `cancel` through a sample-synchronous read cursor (next paragraph).
- **`cancel(pcm16) -> bytes`** — the engine passes **every decoded inbound analysis-rate frame**
  through this before yielding it. For each near-end sample it forms the echo estimate as the dot
  product of the `filter_len` adaptive taps with the aligned far-end window, subtracts it, and
  returns the residual. **It returns exactly the samples it was given — no added frame of
  buffering, no extra latency** (the property the task's "must not add perceptible delay"
  requires).

**NLMS update.** After each sample the taps `w` are nudged toward the far-end window `x` that
produced the residual `e`: `w += (mu * e / (||x||² + eps)) * x`. The normalisation by the
reference window energy makes convergence speed independent of the signal level (the live TTS
amplitude varies), and `eps` (a small regulariser) bounds the step when the reference is near
silent. `mu` (the step size, 0 < mu < 2) trades convergence speed against steady-state residual.

**Double-talk hold (the barge-in-preserving guard).** A plain NLMS that keeps adapting *while the
caller is talking over the echo* would (a) try to model the caller's uncorrelated speech as echo
and **diverge**, and (b) partially **cancel the caller** — destroying the very barge-in we are
trying to make responsive. So adaptation is **frozen** for a frame whenever the near-end energy
substantially exceeds the current echo estimate's energy (the standard Geigel-style double-talk
indicator): during double-talk the filter stops learning but **keeps subtracting** its
last-converged estimate (it still cancels the echo component; the caller's speech passes through
as residual). When only echo is present (no caller), adaptation runs and the filter converges.

**Why this cancels echo but not the caller.** The echo is **correlated** with the known reference
(it *is* the reference, delayed+filtered), so the adaptive filter learns it and the subtraction
removes it. The caller's speech is **uncorrelated** with the reference, so the filter cannot model
it and it survives the subtraction — and the double-talk hold stops the filter from trying.

**Sample-synchronous alignment (not lockstep).** The far-end (the tapped outbound) and the near-end
(the decoded inbound) are **sample-synchronous streams** — both advance one sample per
sample-period from the call start. The engine does **not** interleave a push 1:1 with a cancel: the
TX (`push_reference`) runs **ahead** of the RX (`cancel`) by the system delay (the greeting plays
before any inbound; the jitter buffer holds the near-end; async scheduling batches I/O). The
canceller therefore consumes the far-end FIFO through an **independent read cursor** that advances
exactly one far-end sample per near-end sample, keeping the two time-locked; the adaptive taps then
span the echo round-trip delay forward from the cursor. Anchoring the window on the *newest* pushed
sample instead — the naive choice — points it at *future* TTS uncorrelated with the current echo,
which **diverges** the filter and **mis-fires the double-talk guard** (this was found and fixed in
cross-vendor review by an engine-level test where TX leads RX by the jitter buffer + scheduling; a
unit test with 1:1 interleave does not exercise it). The cursor is also **clamped to the available
far-end** (`min(read+n, len(x))`): the *other* live ordering is inbound arriving **before** the
first outbound (the pump and greeting start concurrently, and TTS has synthesis latency) — those
pre-roll near-end samples have no echo and pass through, and the cursor must not run off the end of
the still-empty FIFO, or the greeting's later echo would land past the FIFO and never cancel (a
second cross-vendor finding, reproduced with a 200 ms inbound pre-roll; pinned + regression-tested).

**Convergence window (the residual risk, honestly stated).** A fresh per-call canceller starts with
zeroed taps, so for the first few hundred ms of the greeting — before the filter has learned the
echo path — the greeting's echo is **not yet cancelled**. The `gated` barge-in mode is the backstop
during this window: it requires a **sustained continuous** voiced run (200 ms ≈ 7 windows) to fire,
and the echo arrives as **short, broken** bursts (ADR-0023's evidence), which do not sustain 7
continuous windows before the filter converges (NLMS at `mu=0.30` converges within ~100–300 ms on a
loud reference). So the lowered threshold is safe in combination with `gated`; an operator on a
gateway with an unusually loud/sustained early echo can raise `HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS`
rather than disable AEC. The 200 ms default applies only when AEC is enabled and that key is unset.

### Wiring (engine-internal — no change to the call-loop data flow)

The canceller is owned by `RtpMediaTransport`: constructed (when enabled) in `connect()` at the
codec's analysis rate, reset per call. The RX tap is one line in `_decode`'s caller
(`_inbound_gen`): `frame = aec.cancel_frame(frame)` before `yield`. The TX tap is one call in
each outbound send site. Because the canceller sits **inside** the engine, `call_loop.py`'s
pump → VAD → ASR chain is untouched; it simply receives already-cancelled frames, so the existing
`BargeInGate` (ADR-0023) and flush/fade (ADR-0028) work unchanged on a clean inbound signal.

### Lowering the threshold (the aggressive barge-in)

With AEC removing the echo before the VAD, the 600 ms sustained guard is no longer needed to
reject echo blips. When AEC is enabled the **default** `HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS` drops
to **200 ms** (≈ 7 windows at 8 kHz / 7 at 16 kHz) — responsive, while still long enough that a
single spurious VAD blip (a click, a residual transient) does not barge in. The `gated` mode and
its tail stay as defense-in-depth (AEC residual is small but non-zero), so even a brief leak below
200 ms cannot self-interrupt. An operator who sets `HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS` explicitly
keeps their value; the AEC-aware default applies only when the key is unset.

### Config surface

| Env var | `MediaConfig` field | Default | Meaning |
| --- | --- | --- | --- |
| `HERMES_VOIP_AEC_ENABLED` | `aec_enabled` | `true` | Master switch for the in-process echo canceller. `false` = the ADR-0023 sustained-gate-only behaviour (no canceller). |
| `HERMES_VOIP_AEC_FILTER_MS` | `aec_filter_ms` | `16` | Adaptive-filter length in ms (taps = `ms × analysis_rate / 1000`). Spans the room/hybrid impulse response; longer = more echo paths modelled, more CPU/sample. 16 ms captures the dominant echo energy within the pure-Python per-frame budget (see *Consequences*); 32 ms is available for a longer path at ~2× the CPU. |
| `HERMES_VOIP_AEC_BULK_DELAY_MS` | `aec_bulk_delay_ms` | `0` | A fixed reference delay (ms) skipped before the adaptive window, for a gateway with a large constant echo-return delay. `0` lets the adaptive taps cover the delay directly. |
| `HERMES_VOIP_AEC_MU` | `aec_mu` | `0.30` | NLMS step size in `(0, 2)`. Higher converges faster, higher steady-state residual. |
| `HERMES_VOIP_BARGE_IN_MIN_SPEECH_MS` | `barge_in_min_speech_ms` | **`200` when AEC on, else `600`** | Sustained voiced run (ms) to barge in during playout/tail (ADR-0023). The AEC-aware default. |

All parsed in `config.py` into `MediaConfig`, threaded through the adapter into
`RtpMediaTransport` (the canceller params) and `CallLoop` (the derived window count), with the
same fakes-only, fully-typed discipline as the rest of the engine.

## Consequences

- **Barge-in is responsive.** The sustained threshold drops from 600 ms to 200 ms with AEC on,
  so a caller is heard within ~200 ms of talking over the agent — without re-opening the
  self-interruption loop, because the echo is cancelled before the VAD/ASR see it. Verified by
  deterministic tests: a known reflected reference is cancelled below the VAD floor (no false
  ONSET), a real uncorrelated near-end signal survives (ONSET still fires), and the lowered
  threshold does not self-interrupt on echo through the full engine + `BargeInGate`.
- **No added latency on the hot path (rule 22).** `cancel()` is per-sample, in-place,
  buffer-free — it returns the same frame length with no extra ptime of delay (no look-ahead). The
  added CPU is `O(filter_len)` multiply-accumulates per sample, in `array('d')` via sliced/`zip`'d
  inner loops. The default filter is **64 ms** so the adaptive window spans the realistic
  echo-RETURN delay (the round-trip — our jitter buffer + gateway — not just the impulse response;
  a too-short window measurably leaves a delayed broadband echo uncancelled), and the engine **caps
  the tap count at 512** (`_AEC_MAX_TAPS`) for the CPU budget. **Measured** per-20-ms-frame cost at
  the shipped defaults: **~6.9 ms at 8 kHz G.711 (≈ 34 % of the 20 ms ptime, a full 64 ms window),
  ~13.8 ms at 16 kHz G.722/Opus (≈ 69 %, the 512-tap cap = ~32 ms window)** — both under budget. The
  pacing path is unchanged (the TX reference tap is a cheap list append). Only the inbound RX runs
  `cancel`; the outbound is the cheap tap. A longer 16 kHz echo needs `aec_bulk_delay_ms` tuning
  (the wideband path trades echo-delay reach for the per-frame CPU ceiling).
- **No new dependency.** Pure stdlib; the `uv.lock` / licence surface is unchanged (rule 35), and
  the canceller does not pull numpy into the `media`-extra engine path.
- **AEC is a no-op when disabled** (`HERMES_VOIP_AEC_ENABLED=false`) — the ADR-0023 path is
  preserved exactly, and the threshold default reverts to 600 ms — and a no-op on a gateway with
  its own echo cancellation (there is no echo to model, the filter stays near-zero, the reference
  is uncorrelated with the clean caller audio so nothing is subtracted).
- **Double-talk is handled, not assumed.** The hold guard keeps a genuine interruption intact; a
  diverging filter cannot eat the caller. This is the property that lets the threshold drop
  safely.
- **Live validation is pending the operator's redeploy** (the live gateway must not be touched
  from this lane); the deterministic tests prove the cancellation and the integration, and the
  measured per-frame cost proves the latency budget.

## Alternatives considered

- **Keep the 600 ms sustained gate only (ADR-0023, no AEC).** The shipped behaviour; correct but
  sluggish — the operator asked for *aggressive* barge-in, which the gate alone cannot give
  without re-admitting echo. Superseded here, kept as the `HERMES_VOIP_AEC_ENABLED=false` path.
- **A C/wheel AEC (WebRTC APM / speexdsp).** Stronger DSP, but no clean `cp313` wheel exists
  (table above), it adds a system-lib/C-build dependency and FFI cost on the hot path, and it
  brings far more than we need. Rejected on availability + rule 22; revisit if a clean wheel
  appears.
- **Frequency-domain (block) adaptive filter (FDAF/PBFDAF).** Lower asymptotic CPU for long
  filters, but it needs an FFT (numpy — banned in this path) and introduces a **block of
  algorithmic latency** (it processes a block at a time), violating the no-perceptible-delay
  requirement. The time-domain sample-by-sample NLMS adds zero algorithmic delay; our filter is
  short (16 ms default) so its per-sample cost is acceptable (measured in *Consequences*).
- **Energy/spectral subtraction without a reference.** Cannot distinguish the agent's echo from
  the caller — exactly the failure ADR-0023 documented for a level-only gate. Reference-based
  cancellation is the robust, gateway-agnostic answer.
