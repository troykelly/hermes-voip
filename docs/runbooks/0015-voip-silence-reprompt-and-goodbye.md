# Runbook: VoIP caller-silence reprompt + spoken goodbye (ADR-0057, ADR-0106)

**What it is.** Two voice-UX behaviours in the call loop (`CallLoop`), both **ON by default**:

1. **Caller-silence reprompt / no-input handling.** A live-but-silent caller — inbound RTP
   still flowing (silence / comfort-noise) but no end-of-turn, so nothing reaches the agent —
   is reprompted ("Are you still there?") after a silence window, and after N unanswered
   reprompts the call is wound up gracefully. Without this the agent waits in dead air forever
   (the engine's RTP-inactivity watchdog only fires on **dead** media, not silent-but-live
   media).
2. **Spoken goodbye before BYE.** On that **loop-initiated graceful end** the agent speaks a
   short closing line ("Goodbye.") and lets its audio flush **before** the call drops — so the
   caller hears a goodbye instead of a silent cut. The goodbye is spoken **only** on this
   loop-initiated end, never on a caller-hangup / inbound-EOS / pipeline-error end (there is no
   live media path once the caller is gone or the pipeline has failed).

The WHY lives in **ADR-0057** (which also records the reply-streaming feasibility verdict —
see below). This runbook is the operational HOW.

> **Public repo.** No secrets here — these are booleans, integers, and generic spoken phrases.

## Current wiring (what IS, AGENTS rule 27)

Both behaviours are implemented in `src/hermes_voip/media/call_loop.py` as `CallLoop`
constructor kwargs with built-in English defaults. The adapter (`adapter._run_call_loop`,
~line 5104) **passes all six kwargs explicitly** from `MediaConfig`, and `config.py`
(~lines 343–374, PR #188) exposes each as a `HERMES_VOIP_*` env var. All six knobs are
fully wired end-to-end: env var → `MediaConfig` field → `_run_call_loop` kwarg → `CallLoop`.

The feature is **live on every call** with the defaults shown below; set the env vars to
override (see the env-var table after the kwarg table).

## The knobs (`CallLoop` kwargs)

| Kwarg | Type | Default | Meaning |
| --- | --- | --- | --- |
| `no_input_reprompt` | bool | `True` (**ON**) | Caller-silence watchdog master switch. Off ⇒ no watchdog task is created (the prior behaviour exactly). |
| `no_input_timeout_ms` | int ms, `> 0` | `10000` | Silence window: no caller end-of-turn for this long before a reprompt. |
| `no_input_max_reprompts` | int, `>= 0` | `2` | Unanswered reprompts before the graceful end. `0` ⇒ straight to goodbye + end on the first silent window (no reprompt). |
| `no_input_reprompt_phrases` | tuple[str, …], non-empty | `("Are you still there?", "Hello, are you still there?", "Sorry, I can't hear anything. Are you still there?")` | Reprompt set; one chosen at random per fire, never the immediately-previous one. |
| `goodbye` | bool | `True` (**ON**) | Speak a closing line on the loop-initiated graceful end. Off ⇒ the end still happens, silently. |
| `goodbye_phrase` | str | `"Goodbye."` | The closing line spoken pre-BYE. |

The silence window and reprompt phrasing reuse the comfort-filler RNG and the same injected
`sleep` seam, so the loop has one deterministic time source for tests.

## Env vars (all six knobs are wired via `MediaConfig`)

| Env var | Maps to kwarg | Default | Notes |
| --- | --- | --- | --- |
| `HERMES_VOIP_NO_INPUT_REPROMPT` | `no_input_reprompt` | `true` | `false` disables the watchdog entirely. |
| `HERMES_VOIP_NO_INPUT_TIMEOUT_MS` | `no_input_timeout_ms` | `10000` | Must be `> 0`. |
| `HERMES_VOIP_NO_INPUT_MAX_REPROMPTS` | `no_input_max_reprompts` | `2` | `>= 0`; `0` goes straight to goodbye. |
| `HERMES_VOIP_NO_INPUT_REPROMPT_PHRASES` | `no_input_reprompt_phrases` | (built-in English set) | Newline-separated list; must be non-empty. |
| `HERMES_VOIP_GOODBYE` | `goodbye` | `true` | `false` → call ends silently. |
| `HERMES_VOIP_GOODBYE_PHRASE` | `goodbye_phrase` | `"Goodbye."` | The closing line spoken pre-BYE. |

All six are read at `connect()` time and validated by `MediaConfig._validate_no_input()`.

## How it behaves (the guarantees)

- **Reprompt only on genuine dead air.** Each silence window, the watchdog reprompts only if
  (a) the caller showed no life during the window **and** (b) no agent audio is on the wire
  right now (it never speaks over a reply / greeting / a prior reprompt — it skips that window
  and re-checks, without spending the reprompt budget).
- **Resets the instant the caller engages.** A delivered turn (the caller finished speaking)
  **or** a barge-in (the caller started speaking) resets the silence window and clears the
  reprompt count — a caller who is talking is plainly still there. A reprompt already *playing*
  is cancelled by the barge-in like any agent audio, so a caller who answers mid-reprompt is
  heard at once.
- **Graceful end is a CLEAN end.** After the reprompt budget is spent on a still-silent caller,
  the loop speaks the goodbye (if enabled), lets it fully flush, then signals the pump to wind
  up so `run()` returns **cleanly** — the adapter classifies a normal end (`REMOTE_BYE`), not a
  `/stop`. On a real silent-but-live call inbound RTP keeps flowing so the pump observes the
  end within ~one frame; a truly dead line is ended by the engine's RTP watchdog instead.
- **Goodbye has a live media path.** It is spoken **before** `run()` returns; the adapter stops
  the media engine only after `run()` returns, so the goodbye reaches the wire. It is the last
  audio on the call.
- **Flushable + echo-gate-safe (ADR-0023/0028).** Reprompt and goodbye route through the same
  `speak()`/TTS/`send_audio` path as a reply, so they arm the echo gate (the gateway reflecting
  them back cannot self-interrupt the call) and are flushable. The watchdog task is cancelled
  and joined at call teardown — it never leaks past the call, even mid-playout.
- **Best-effort (AGENTS rule 37).** A reprompt/goodbye synthesis or send failure is logged at
  warning (`no-input: reprompt synthesis/send failed (call continues)` /
  `no-input watchdog failed (call continues)`) and is never fatal — the call survives it.
- **Model-appropriate text (ADR-0027).** The default phrases read naturally on every TTS model;
  emoji/markdown/URLs are stripped and the per-segment audio-tag strip applies, exactly like a
  reply. Do not set a bare-bracket-tag-only phrase as a default on a non-v3 deployment (it would
  strip to empty — no audio).

## Reply streaming (ADR-0057 §3) — what to know operationally

The third conversational-UX item investigated was reply first-audio latency. **Finding:** the
Hermes 0.16.0 runtime hands the plugin the agent reply as **one complete string** (there is no
per-sentence text callback wired through the gateway platform path), so the plugin **cannot**
start TTS until the whole reply string arrives — true plugin-side streaming is **runtime-
blocked**. The best available mitigation is already in place: once the string arrives, the TTS
layer (`tts/segment.py` + `tts/_stream.py`) splits it into sentences and streams each sentence's
audio before synthesising the next, so first audio starts after ~**one sentence**, not the whole
reply; the **comfort filler** (runbook 0006) covers the LLM think-time wait before the string
arrives. **No operator action and no call-loop change are required for §3.** If a future
hermes-agent release exposes a per-sentence text callback to platform plugins, revisit ADR-0057.

## Agent-hangup farewell drain (#1297, ADR-0106)

The **loop-initiated** goodbye above drains inline (it is spoken and flushed before `run()`
returns). Its symmetric counterpart is the **agent-initiated** hangup: when the agent speaks
a goodbye **and** calls the `hang_up` tool in the **same turn**, the adapter must not send BYE
+ stop media until that in-flight farewell has reached the wire, or the closing line is
truncated (media stop mid-playout) or dropped (post-teardown send-suppression).

**Wiring (what IS):** `CallLoop` counts agent replies in flight in `self._active_replies`
(incremented in `speak()` only — the comfort filler and the loop goodbye go through
`_speak_text` directly and are deliberately **not** counted). `CallLoop.drain_agent_speech(*,
timeout, grace)` polls that counter using **only** `asyncio.sleep` (no Event/Lock/Future/task/
async-generator — leak-free by construction): a short arrival grace catches a same-turn
farewell that is dispatched a scheduling beat after the tool call, then it waits for every
in-flight reply to finish, bounded by `timeout`. `adapter.hang_up_call` awaits it **between**
`_mark_agent_hangup` and `session.hang_up()`, so `info["ended"]` is still `False` throughout —
the farewell `send()` is neither suppressed nor its frames cut. A caller barge-in **releases**
the drain early (cancelling the reply decrements the counter) but does **not** abort the hangup
— the tool call is an already-committed decision.

| Env var | Maps to `MediaConfig` field | Default | Notes |
| --- | --- | --- | --- |
| `HERMES_VOIP_HANGUP_DRAIN_SECS` | `agent_hangup_drain_secs` | `5.0` | Total bound (s); positive finite. The drain returns after this even if a farewell is still playing (then the closing line may clip — the caller is not left waiting). |
| `HERMES_VOIP_HANGUP_GRACE_SECS` | `agent_hangup_grace_secs` | `0.5` | Arrival window (s) for a same-turn farewell to **begin**; positive finite and **must be `<= drain`** (enforced in `MediaConfig.__post_init__`). A bare hangup pays only this before BYE. |

Both are read at `connect()` time and validated by `MediaConfig._validate_agent_hangup_drain()`.

**Tuning:** raise `HANGUP_DRAIN_SECS` if the agent's farewells are long/multi-sentence and get
clipped; lower `HANGUP_GRACE_SECS` toward 0 to effectively disable the same-turn arrival guard
(fixing only the mid-playout case) if you see a perceptible pre-BYE beat on bare hangups.
**Residual (documented, not a bug):** a goodbye the model emits in a *genuinely separate later
message* (after it has already seen the `hang_up` tool result, past the grace) arrives after
media stop and is correctly dropped — closing that would require dead air on every hangup.

## How to verify

0. **Agent-hangup drain (deterministic, no real waiting beyond the sub-second bounds):**

   ```
   uv run pytest tests/test_call_loop.py -k drain_agent_speech
   uv run pytest tests/test_config.py -k agent_hangup
   ```

   and, in the `hermes-contract` env (real `VoipAdapter`):

   ```
   uv run pytest tests/test_adapter.py -k "drains_farewell_before_bye or proceeds_when_no_farewell or proceeds_after_drain_timeout"
   ```

   proves: the drain waits for an in-flight farewell then returns True; a same-turn farewell
   arriving during the grace is caught and fully drained; a bare hangup returns on the grace; a
   wedged playout is bounded (returns False, never hangs); the comfort filler is ignored;
   barge-in releases it; and at the adapter seam every farewell frame reaches the wire **before**
   the session BYE.

1. **Behaviour (covered by the test suite, deterministic — injected sleep seam, no real
   waiting):**

   ```
   uv run pytest tests/test_call_loop.py -k "no_input or goodbye or reply_streams"
   ```

   proves: a reprompt fires after the silence window on a silent caller; the window resets when
   the caller speaks (delivered turn) or barges in; after N unanswered reprompts the loop ends
   itself cleanly; the goodbye is spoken + flushed before the end on the loop-initiated end and
   is **not** spoken on a caller-hangup/EOS end; the off path emits nothing and never self-ends;
   and the reply is streamed to TTS sentence-by-sentence (the first sentence's audio reaches the
   wire before the second is synthesised).

2. **Live (default-on):** place a call and go silent after the greeting. After ~10 s the
   operator log shows `no-input: reprompt 1/2 on caller silence: 'Are you still there?'`; stay
   silent and a second reprompt follows, then `no-input: caller silent after 2 reprompt(s);
   ending the call`, `no-input: speaking goodbye before end: 'Goodbye.'`, and the call drops
   after the goodbye plays. Answer a reprompt (speak) and the cycle resets (no further reprompt,
   no end). (Live validation is pending the operator's redeploy; the gateway was not touched.)

## Tuning guidance

- **`no_input_timeout_ms`** (default 10 s): lower signals concern sooner but risks reprompting a
  caller who is just thinking; higher tolerates longer natural pauses. 10 s is a common IVR
  no-input window.
- **`no_input_max_reprompts`** (default 2): how many "are you there?" prompts before giving up.
  `0` ends immediately on silence (with a goodbye) — useful for a line that should never sit
  open unattended.
- **Phrases:** keep each short and natural; a varied reprompt set keeps the random,
  no-immediate-repeat selection from sounding cyclic. Multi-language follows the comfort-filler
  mechanism once the config plumbing lands.

## Rollback

Construct `CallLoop` with `no_input_reprompt=False` (and/or `goodbye=False`) — the loop returns
to the prior behaviour (no watchdog task; no spoken goodbye). Once the `config.py` env plumbing
lands, this becomes an `HERMES_VOIP_*` env var + redeploy (this runbook will be updated in that
commit, rule 42).
