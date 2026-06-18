# ADR-0046: Hermes plugin best-practices alignment (handler resilience, fail-soft tool registration, platform_hint, documented singleton helper)

- **Date:** 2026-06-18
- **Status:** Accepted
- **Deciders:** agent session (best-practices-alignment lane) — operator-directed

## Context

The VoIP plugin is loaded and run by the Hermes runtime as a `kind: platform`
plugin (ADR-0011 control plane, ADR-0026 call control, ADR-0035 channel routing).
Five details of how the plugin meets the documented Hermes plugin conventions had
drifted from — or never adopted — the runtime's stated contracts. None is a
correctness bug today, but each is a place where the plugin behaves differently
from what the Hermes plugin guidance documents, which is a maintenance and
robustness liability:

1. **Tool-handler exception surface.** The Hermes tool-handler contract is "a
   handler never raises; it returns a JSON tool result (an `{"error": ...}` object
   on failure)" — the model-facing JSON IS the surfaced error. Our nine registered
   handlers return clear `{"error": ...}` JSON for *anticipated* failures (no
   adapter, stale call, bad arg, refused transfer …) but an *unanticipated*
   exception from a host call (a bug, an unexpected runtime error) would propagate
   out of the handler. At the handler boundary that is the wrong shape: the runtime
   turns a raised exception into an opaque tool failure (or, worse, an unhandled
   error in the agent turn) instead of a clean error the model can react to.

2. **Tool registration is not fail-soft.** `register_voip_tools` already guards the
   *presence* of `register_tool` / `register_hook` with `getattr`, but the
   per-spec `register_tool()` call itself is unguarded: if the runtime rejected one
   tool (e.g. a name collision with another plugin), the exception would abort the
   whole loop and the remaining tools (and on an early failure, nothing) would be
   registered. The platform would still come up, but the agent would silently lose
   call-control tools.

3. **No `platform_hint`.** Hermes lets a platform declare a `platform_hint` — a
   short instruction injected into the agent's context for that platform.
   Telephony is a live, audio-only channel: replies are read aloud by TTS, so
   markdown, code blocks, URLs, and emoji are actively harmful (they get spoken
   character-by-character or dropped). We register the platform with no hint, so the
   model is not told it is on a phone call and tends to reply with text-surface
   formatting.

4. **Hand-rolled singletons vs the documented helper.** `srtp._get_crypto` and
   `dtls._get_openssl` each hand-roll a double-checked-lock lazy singleton with a
   module global + `threading.Lock`. Hermes ships `plugins.plugin_utils.lazy_singleton`
   as the documented helper for exactly this pattern. Using the documented helper
   reduces bespoke concurrency code. BUT `plugin_utils` is a Hermes-*runtime* module
   not vendored into this repo's test environment (the installed `plugins` package
   ships no `plugin_utils` submodule), so a hard import would break the default
   (no-hermes) `mypy --strict` + pytest gate.

5. **`cron_deliver_env_var` omission is undocumented.** The primary
   `register_platform` deliberately omits `cron_deliver_env_var` because telephony
   has no persistent home channel — every call is an ephemeral session — and
   `notice_filter.py` suppresses the home-channel / cron "no home channel" notices
   that would otherwise be spoken to a caller (ADR-0026 §notice handling). That this
   omission is intentional was not recorded at the call site.

## Decision

1. **Outermost log-and-return guard on every handler.** Each of the nine registered
   tool handlers gets an OUTERMOST `except Exception as exc:` that logs the
   exception (with traceback context, via `_log.exception`) and returns
   `json.dumps({"error": "<tool> failed: <exc>"})`. All existing specific-error
   returns stay INSIDE the guard (unchanged) — the guard only catches what they did
   not. This reconciles with rule 37 (errors propagate, never swallowed): at the
   *tool-handler boundary* the model-facing JSON error IS the surfaced error, and it
   is also logged with full context, so nothing is silently dropped — it is
   *translated* into the contract's error channel. A code comment records this
   rationale. `noqa: BLE001` (broad-except) carries that justification inline.

   **Redacted variant for the two secret-bearing handlers.** `send_dtmf` and
   `open_entry` carry secrets (the DTMF digits / the opening secret), which the
   handlers already keep out of the success result and log. An unanticipated `exc`
   from deep in the send path can embed those very digits in its message, so these
   two use `_tool_failure_redacted` instead: it returns a FIXED generic message
   (`"<tool> failed (internal error)"`) and logs ONLY the tool name and the
   exception's TYPE name — never `str(exc)` and never `exc_info` (a rendered
   traceback re-embeds the exception repr, hence the digits). The failure is still
   surfaced (rule 37) — a typed error line to the operator and an error result to the
   model — just without the secret-bearing message text. The other seven handlers
   keep echoing `<exc>`.

2. **Fail-soft per-tool registration.** The per-spec `register_tool()` call in
   `register_voip_tools`' loop is wrapped in `try/except Exception` that logs a
   warning naming the colliding/failing tool and `continue`s — so one bad tool can
   no longer prevent the others from registering. This mirrors the existing
   `getattr` presence guards (best-effort, resilient).

3. **`platform_hint` on every VoIP platform.** A module constant `_PLATFORM_HINT`
   ("You are speaking on a live phone call. Replies are read aloud, so keep them
   short, conversational, and free of markdown, code blocks, URLs, or emoji. Spell
   out anything that must be heard.") is passed to BOTH `register_platform` call
   sites — the primary `voip` platform and the channel-alias loop — so the model is
   told it is on a phone call regardless of which channel routes the session. The
   primary platform also declares `emoji="☎️"`.

4. **Guarded adoption of `lazy_singleton`.** `srtp._get_crypto` and
   `dtls._get_openssl` are reimplemented on top of `plugins.plugin_utils.lazy_singleton`
   via a GUARDED import (`try: from plugins.plugin_utils import lazy_singleton /
   except ImportError:` → the existing stdlib double-checked-lock fallback). This
   mirrors the existing guarded `from gateway...` runtime imports in `adapter.py`:
   the plugin uses the documented helper when the Hermes runtime provides it, and a
   behaviourally-identical stdlib fallback (kept correct and tested) when it does
   not. Each module exposes a `_reset_*_singleton()` for test isolation. Behaviour
   is identical on both paths (build-at-most-once under a concurrent first-call
   stampede). The `LazySingleton` wrapper performs BOTH the runtime-handle
   resolution and the `self._value` read UNDER `self._lock` (double-checked on the
   fast path), so the build-once guarantee holds on the runtime path too — a
   stampede cannot resolve two runtime handles (each of which would run the value
   factory once).

5. **Documented `cron_deliver_env_var` omission.** A one-line comment at the primary
   `register_platform` records that `cron_deliver_env_var` is intentionally omitted
   (telephony has no persistent home channel; `notice_filter.py` suppresses the
   home-channel / cron notices) referencing `notice_filter.py` and this ADR.

## Consequences

- Handlers can no longer leak an unanticipated exception to the runtime as an opaque
  failure; the model always sees a well-formed `{"error": ...}` and the operator
  always sees a logged traceback.
- A tool-name collision with another plugin degrades gracefully (the colliding tool
  is skipped, the rest register) instead of aborting registration.
- The agent is told it is on a phone call and stops emitting TTS-hostile formatting.
- The two media singletons use the documented Hermes helper when available, with a
  tested stdlib fallback — no behavioural change, less bespoke concurrency code.
- The intentional `cron_deliver_env_var` omission is documented at the call site.

## Out of scope

- No change to the privilege gate, the tool schemas/semantics, or the SRTP/DTLS
  crypto behaviour. No new tools, no transport changes.
