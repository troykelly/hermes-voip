# ADR-0047: Bundled call-scenario skills

- **Date:** 2026-06-18
- **Status:** Accepted (reverses the "no skill" conclusion of ADR-0033's research note; operator-directed)
- **Deciders:** agent session (bundled call-skills lane) — operator-directed

## Context

A live phone agent needs scenario-specific guidance: how to screen an unknown
inbound caller, how to take a message accurately, how to handle a delivery at a
door intercom, how to make a booking on an outbound call, and how to run a
price/availability enquiry. The always-on persona preambles (ADR-0020/0021/0031)
establish the persona, privilege ceiling, and untrusted-data fence, but they are
deliberately short — a full step-by-step playbook for every scenario would bloat
every turn's prompt for calls that do not need it.

An earlier research pass (recorded under the ADR-0033 rich-context work) concluded
"do not add a VoIP skill": Hermes plugin skills are opt-in and not in the
system-prompt index, so a latency-pressured voice agent might not reliably invoke
one. The **operator has overridden that conclusion** (2026-06-18) and asked for
bundled call-scenario skills. This ADR records the reversal and the design that
makes the opt-in model work in practice.

The Hermes *bundle-skills* model (verified against hermes-agent 0.16.0
`hermes_cli/plugins.py`): a plugin ships a `SKILL.md` file and registers it with
`ctx.register_skill(name, path, description="")`. The skill is qualified
`hermes-voip:<name>`, stored **read-only**, and is **not** placed in the system
prompt's `<available_skills>` index — the agent loads it on demand via the
`skill_view` tool. The real `register_skill` requires a `pathlib.Path` (it calls
`path.exists()` / `path.name`); a `str` raises inside the runtime.

## Decision

1. **Ship five bundled call-scenario skills**, each at
   `hermes_voip/skills/<name>/SKILL.md` (importable package data, declared as a
   wheel artifact so it resolves from a source checkout and an installed wheel):
   - **Inbound:** `reception` (screen an unknown caller), `take-message` (capture
     caller / callback / message and record it with `report_call_result`),
     `intercom-open-for-delivery` (handle a delivery at a door/gate intercom and
     open the entry with `open_entry` only for a genuine, expected delivery).
   - **Outbound:** `make-reservation` (book a table/appointment, confirm, report
     the outcome), `enquire-price-availability` (ask about price/stock/availability,
     confirm the figures, report back; do not commit to a purchase).
2. **Write each skill for a LIVE SPOKEN CALL.** Short sentences, one thing at a
   time, no markdown / URLs / emoji to be read aloud, and explicit "spell out names
   and numbers, read them back" guidance. Each names the in-call tools the agent
   actually has for that scenario (`open_entry`, `report_call_result`, `hang_up`)
   so the cue is real (rule 27), and reinforces the security posture (untrusted
   caller, disclose nothing private, do not open the door / commit under pressure).
3. **Register all five via `ctx.register_skill(name, path, description)`** in
   `register()`, in a `hermes_voip.skills` module guarded with
   `getattr(ctx, "register_skill", None)` — the same graceful-degrade pattern as the
   platform/tool registrations, so a runtime predating `register_skill` still
   registers the platform and tools. The path passed is a `pathlib.Path` (the real
   runtime contract); a missing `SKILL.md` for a declared skill **raises** (a
   packaging defect, never swallowed — rule 37).
4. **Point the personas at the relevant skills.** Because plugin skills are opt-in
   and not in the system-prompt index, the persona preambles (`caller_modes.py`)
   now name the matching skill(s) and tell the agent to load one with `skill_view`
   when the call matches: the receptionist persona points at `reception` /
   `take-message`, the intercom persona at `intercom-open-for-delivery`, and the
   outbound persona at `make-reservation` / `enquire-price-availability`. Each cue
   names only a skill the persona's privilege actually permits.

## Scope / deferred (rule 6)

- **No new manifest key.** The hermes-agent 0.16.0 manifest parser reads only
  `name`/`version`/`description`/`author`/`kind`/`provides_tools`/`provides_hooks`/
  `requires_env` (verified); it does **not** parse a `skills` key. Adding one would
  be inert (silently tolerated, never read), so `plugin.yaml` is left unchanged —
  the live `register_skill` calls are the real registration surface.
- **Skill *content* is the agent interface, not enforcement.** A skill is advisory
  guidance loaded into the agent's context; the privilege clamp (`gate_tool_call`)
  and the per-call `allowed_tools` sub-ceiling remain the enforced boundary. A skill
  can never widen what a call may do — e.g. the intercom skill describes using
  `open_entry`, but the gate still blocks it outside the intercom channel.
- **Not auto-loaded.** The opt-in model is deliberate; the persona cue is what makes
  the agent reach for the right skill. A future always-on summary line is possible
  but not built here.

## Consequences

- A voice agent has scenario playbooks it can pull in on demand without bloating
  every turn's prompt, and the personas tell it which one to load.
- The skills ship with the wheel and resolve via `importlib.resources`, so they work
  for both directory-install and pip/entry-point install.
- One corrected test contract: `register_skill` takes a `Path`, not a `str` (the
  real runtime calls `path.exists()`); `tests/test_register_skills.py` asserts every
  registered skill is passed a `pathlib.Path`.

## Alternatives considered

| Alternative | Rejected because |
| --- | --- |
| Put the playbooks in the persona preambles | Bloats every turn's prompt for calls that don't need them; the persona stays short and points at a skill instead. |
| Add a `skills:` key to `plugin.yaml` | The 0.16.0 manifest parser never reads it (verified) — it would be inert; the `register_skill` calls are the real surface. |
| Ship skills at repo-root `skills/` | Would not travel in the wheel without an artifact glob anyway; placing them under the package makes `importlib.resources` resolution uniform across install modes. |
| Auto-load a skill per channel | Skills are opt-in by Hermes design; forcing always-on load is not supported and would re-bloat the prompt. The persona cue is the supported lever. |
| Keep the ADR-0033 "no skill" conclusion | Operator explicitly overrode it (2026-06-18); the persona-cue design addresses the original "agent won't invoke it" concern. |
