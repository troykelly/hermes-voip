# ADR-0072: Autonomous orchestration loop — single orchestrator, fan-out agent teams

- **Date:** 2026-06-22
- **Status:** Accepted
- **Deciders:** operator (`troy@…`) + agent session (orchestration-loop lane). Composes with
  the worktree-lane discipline (AGENTS rules 8–13), the quality gates (rules 14–22), the
  no-new-platform constraint (rules 40–42), the `adr`/`memory`/`worktree-lane` skills, and the
  `docs/backlog.md` work register.

## Context

The repository is built and operated entirely by agent sessions under `AGENTS.md`. The
operator's recurring problem with autonomous operation is that sessions **stop** — at a
"natural break point", when a queue looks empty, or when the model "feels done". Two failure
modes dominate: (1) treating an empty backlog as completion, and (2) treating "awaiting
review/CI" as a parking state. Both halt progress that should continue.

The operator wants a **single orchestrator** (not many Claude Code terminals racing in
separate shells) that fans out work **as broadly as possible** via ultracode Workflows and
agent teams, **selects the right model per job** (intelligence vs speed/efficiency), and runs
**for days** until everything outstanding is shipped, gap-reviewed, the gaps are backlogged,
and those are shipped too — across next-work identification, correctness, documentation, the
provider/tool API, auth, observability/reporting/monitoring, and UX. The only acceptable exit
is a genuinely dry repo (no work to ship, no UX to investigate).

Constraints that bound the design:
- **Worktree lanes only** (rule 8); a PreToolUse hook blocks root-checkout edits. All work —
  including authoring this loop — lands via a lane → PR.
- **TDD + full local gate + adversarial cross-vendor review before merge** (rules 15/18/21).
  `codex` (OpenAI CLI) is installed, so true cross-vendor review is available — matching the
  repo's existing "codex review" history.
- **No new hosting/platform/SaaS/cost without an operator-approved ADR** (rule 40/41).
- **Public repo** — the gateway host/extension/password/device-model and all PII stay out of
  every tracked file (CLAUDE.md invariant).
- **Memory MCP (qdrant) is single-process** — only one consumer at a time.
- `/loop <prompt>` (built-in skill) runs a prompt on a cadence; with no interval it is
  *dynamic mode*, where the model guarantees continuation via `ScheduleWakeup`.

## Decision

Add a project skill `/orchestrate` and a canonical Workflow script that together implement an
autonomous, single-orchestrator, fan-out build loop, started by **`/loop /orchestrate`**.

### Topology — one brain, many hands
The **orchestrator** is one Claude session (the main loop). It never does feature work
itself; it **senses, selects, shepherds PRs, merges, records memory, and guarantees
continuation**. All execution fans out to subagents via the **Workflow tool**
(`parallel`/`pipeline`) and Agent teams — inside a single session, not multiple terminals.
One `/orchestrate` invocation = **one wave**; `/loop` runs waves back-to-back.

### The wave (eight phases)
0. **Sense & resume** — refresh root; reap open PRs (merge green+reviewed; fix red; review
   unreviewed); sweep orphaned worktrees; recall memory; load `.orchestrator/state.json`.
1. **Replenish** — when the ready queue is low or every third wave, fan out **one
   gap-reviewer per dimension** (correctness, robustness, security/auth, tests/mutation,
   docs/drift, API, performance, observability, UX, operability, packaging, product/features);
   dedup vs backlog+issues; append genuinely-new items via a docs lane.
2. **Select & plan** — batch independent ready items (≤ fleet width), priority- and
   dependency-ordered, non-overlapping file territory (rule 32); assign a **model tier** each.
3. **Implement** — a `pipeline` runs each item through a TDD implementation agent
   (`isolation: worktree`, assigned model): red test → green → full gate → push branch.
4. **Review** — each item is reviewed the moment its implementation is green by a
   **different-vendor** (`codex`) **and** a **different-tier Claude** reviewer, fresh-context,
   diff+spec+checklist only (rule 21); must-fix findings loop back to a fix agent.
5. **PR & merge** — the orchestrator re-verifies on current HEAD, opens the PR, watches CI,
   and **squash-merges on green CI + clean review** (operator-approved auto-merge). Slow CI
   never blocks the wave — the next wave's Phase 0 reaps the PR (idempotent shepherding).
6. **Integrate & clean** — check off the backlog item with the PR #; remove the lane; refresh
   root; store memory; reconcile drifted docs.
7. **Continue** — update the journal and **always** schedule the next wave
   (`ScheduleWakeup("/loop /orchestrate")`).

### Durable state (resume across restarts / days)
The orchestrator keeps **no long-term state in context**. Truth lives on disk:
`docs/backlog.md` (canonical queue), GitHub PRs/issues (in-flight), `.orchestrator/state.json`
(gitignored optimization — rebuildable from the former two), and the memory MCP
(orchestrator-only). A context summary or process restart loses nothing.

### Model-selection rubric ("the right agent for the job")
`opus` (`claude-opus-4-8`) for hard correctness/security, new subsystems, ADR design,
crypto/SRTP/digest/guard, ambiguous root-cause, synthesis, escalated reviews; `sonnet`
(`claude-sonnet-4-6`) for standard spec'd work and most lanes; `haiku` (`claude-haiku-4-5`)
for mechanical/bounded edits, triage, and the codex-review driver; `fable` (`claude-fable-5`)
as the fast high-capability peer and primary Claude cross-tier reviewer. Reviewer model is
always different from the author's (rule 21).

### Anti-stop design
"Empty backlog", "awaiting review", "natural break point", and "feeling done" are each
redefined as *triggers* (replenish / reap / continue / deeper gap-review), never as
terminuses. Decisions gate on **observable evidence** (queue counts, gate exit codes, a
`dryStreak` counter), never a vibe. The only exit is a deliberately near-impossible
steady-state (3+ consecutive fully-dry gap-review waves, zero open PRs/lanes, green gate, and
an empty UX/feature-discovery pass), and even that is a long idle-poll, not a halt.

### Scope boundary (rule 40/41)
The loop ships everything in-bounds (correctness, security/auth, tests, docs, local-only
observability instrumentation, the designed agent-screened-answering feature, …) and
**auto-merges** it. Anything needing new infrastructure or cost (a website, an HTTP API, an
S3 call-events/recordings store, an external metrics sink) is **propose-only**: the loop
writes a *Proposed* ADR + a backlog item and waits for operator approval — it never silently
stands up infrastructure.

### Files
- `.claude/skills/orchestrate/SKILL.md` — the operating manual + invariants.
- `.claude/skills/orchestrate/wave.workflow.js` — the canonical wave Workflow (gap-review +
  implement/review fan-out).
- `docs/runbooks/0016-orchestration-loop.md` — how to run/observe/pause/resume it.
- `.orchestrator/` — gitignored per-clone wave journal.

## Consequences

- **Progress no longer stalls.** The loop self-replenishes and self-continues; "done-feeling"
  becomes a gap-review, not a stop. The operator starts it with one command and walks away.
- **Quality is preserved, not traded for autonomy** (rule 7): every change is TDD'd, fully
  gated, and cross-vendor reviewed before an auto-merge; the loop obeys — never weakens — the
  existing gates and hooks.
- **Auto-merge on a public repo** is the committed posture (operator decision). Each merge
  publishes; the cross-vendor review + green CI are the guardrails. The operator can impose
  branch protection at any time to convert auto-merge into open-and-await without code change.
- **Cost** scales with fan-out. The wave is bounded by the Workflow concurrency cap
  (`min(16, cores−2)`) and a per-wave batch size, and is `budget`-aware. `/loop` expires
  after ~7 days — the runbook documents re-arming for longer runs.
- **Resumability** is a maintained invariant: state on disk, lean orchestrator context. This
  must not regress — a future change that parks state in the conversation breaks multi-day runs.
- **One ongoing commitment:** the gap-review dimension list and model rubric are living config
  in the skill; as the product grows (e.g. an approved web surface) they are extended there.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Many Claude Code terminals in parallel | The operator explicitly wants one orchestrator; independent sessions contend on the single-process memory lock, duplicate work, and have no shared selection/merge authority. |
| The `ralph-loop` plugin (bare repeat-prompt) | No model-tiering, no structured fan-out, no anti-stop discipline, no cross-vendor review/merge ownership — it would re-introduce the stopping failure modes. |
| The existing `/goal`+`ship-it` flow | Good ancestor, but it *leaves PRs for a human* and *stops after N turns* — the opposite of the multi-day, auto-merging, never-stop requirement. Its principles (state in git, evidence-backed, materiality gate) are absorbed here. |
| Leave PRs open for manual merge | Later lanes base on HEAD; unmerged PRs stall dependents and pile up conflicts over days — incompatible with unattended operation. Operator chose auto-merge. |
| Build new surfaces (web/API/metrics sink) autonomously | Violates rule 40 (no new hosting/platform/cost without an operator-approved ADR). Resolved as *propose-only* for infra; *build* for local-only surfaces. |
| Keep loop state in the conversation | Cannot survive context summarization or restart across days; defeats the multi-day goal. State is put on disk instead. |

## References

- AGENTS.md rules 1–7 (autonomy/never-defer), 8–13 (worktree lanes), 14–22 (PR/CI + quality
  gates + adversarial review), 29 (lean context, fan-out reading), 30/42 (ADRs/runbooks),
  40/41 (no new platform/cost without approval).
- `.claude/skills/orchestrate/SKILL.md`, `.claude/skills/orchestrate/wave.workflow.js`,
  `docs/runbooks/0016-orchestration-loop.md`.
- `docs/backlog.md` (the work register), `docs/plan/IMPLEMENTATION-PLAN.md` (phase plan).
- Built-in `/loop` skill (dynamic mode + `ScheduleWakeup`); the Workflow tool
  (`parallel`/`pipeline`, `isolation: worktree`, per-agent `model`/`effort`); `codex` CLI
  (cross-vendor review, v0.141).

## Update 2026-06-23 — harness pacing reality

Three verified gaps between this ADR's assumptions and the Claude Code harness as deployed:
(1) **Continuation is cron, not ScheduleWakeup.** `/loop /orchestrate` runs under a fixed
cron (`*/10 * * * *`); `ScheduleWakeup` is a no-op in this mode (returns "dynamic gate is
off"). The cron is the heartbeat — deleting it breaks the loop. (2) **The
`.orchestrator/state.json` journal is unwritable.** The `enforce-worktree` PreToolUse hook
blocks all root-checkout writes including gitignored paths; the orchestrator runs stateless
and rebuilds wave state from `docs/backlog.md` + `gh` + memory each wave. (3) **No
required-status-check branch protection.** `gh pr merge --auto` merges immediately; for any
code change, poll `gh pr checks` to green before merging explicitly. The SKILL.md and
runbook 0016 are updated to match. See `docs/runbooks/0016-orchestration-loop.md` for
operational detail.
