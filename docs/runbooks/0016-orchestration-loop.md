# Runbook: operating the autonomous orchestration loop

**What it is.** The operational HOW for the single-orchestrator autonomous build loop — start
it, watch it, pause/stop/resume it, recover it, and bound its cost. The WHY and the
architecture are in [`docs/adr/0072-autonomous-orchestration-loop.md`](../adr/0072-autonomous-orchestration-loop.md);
the executable contract the orchestrator follows is `.claude/skills/orchestrate/SKILL.md`.

This is a **present-tense operational HOW** (rule 27): it describes what IS. Update it in the
same change whenever the loop's mechanics change.

> **Public-repo rule.** Never write a real host/extension/password/IP/PII into this runbook or
> anywhere the loop can commit. The loop and its subagents are told this; you must hold it too.

---

## What it does, in one paragraph

`/loop /orchestrate` runs the `/orchestrate` skill wave after wave. Each **wave** senses repo
state, replenishes the backlog by fanning out per-dimension gap-reviewers, selects a batch of
independent ready items (assigning the right model to each), implements them in parallel
isolated worktrees with TDD, reviews each with a cross-vendor (`codex`) + cross-tier Claude
reviewer, opens PRs, and **squash-merges on green CI + clean review** — then cleans up and
**schedules the next wave**. It does not stop at "natural break points"; the only exit is a
deliberately near-impossible dry steady-state (see the skill's *Exit condition*).

---

## Prerequisites (verify before starting)

```bash
# 1. You are in the repo root on a current main, clean tree.
cd /workspaces/hermes-voip
git status -sb && git rev-parse --abbrev-ref HEAD     # expect: ## main..., clean

# 2. GitHub CLI is authenticated (the loop opens/merges PRs).
gh auth status                                        # expect: Logged in

# 3. The cross-vendor reviewer is present (rule 21).
codex --version                                       # expect: codex-cli x.y.z

# 4. The toolchain is installed and the gate is green NOW (CI is never the first run).
uv sync --frozen && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest

# 5. The memory MCP is free (single-process lock): no OTHER session has this clone open.
```

If any check fails, resolve it first — do **not** start the loop on a red gate or an
unauthenticated `gh`.

---

## Start it

```text
/loop /orchestrate
```

That's the whole trigger. `/loop` with no interval runs in **dynamic mode** — the orchestrator
paces itself and guarantees the next wave with `ScheduleWakeup`. You can walk away.

To preview a single wave without committing to the loop, run `/orchestrate` once on its own.

---

## Observe it

| Signal | Command / where |
|---|---|
| Live fan-out (agents per wave) | `/workflows` (watch the gap-review / implement / review groups) |
| Wave reports | The orchestrator prints a tight summary at the end of every wave (shipped, merged, opened, discovered, cleaned, next plan). |
| PRs in flight / merged | `gh pr list --state open` · `gh pr list --state merged --limit 20` |
| Work register | `docs/backlog.md` (items get checked off with their PR #; new gaps appended) |
| Wave journal (local) | `.orchestrator/state.json` (gitignored) |
| Active lanes | `git worktree list` |

The loop is healthy when, over time: open PRs cycle to merged, backlog items get checked off,
new gaps appear, and `git worktree list` does not accumulate stale lanes.

---

## The `.orchestrator/` journal (local, gitignored)

A per-clone optimization, **not** a source of truth — if deleted, the next wave rebuilds it
from `docs/backlog.md` + `gh`.

```json
{
  "wave": 42,
  "inflight": [{ "item": "digest quoted-pair escapes", "branch": "fix/digest-quoted-pair", "pr": 173, "retries": 0 }],
  "dryStreak": 0,
  "lastGapReviewWave": 40,
  "notes": "free-form"
}
```

---

## Pause / stop

The loop continues via `ScheduleWakeup` (and, with an interval, the `/loop` cadence). To stop:

```text
/loop                       # invoking the loop skill again lets you cancel/stop the active loop
```

or interrupt the session (Esc) and clear scheduled wakeups:

- List scheduled jobs and cancel the orchestration wake-up (the skill schedules with reason
  "next orchestration wave"). Use the loop skill's own stop/cancel path, or remove the job via
  the scheduler.
- In-flight PRs are unaffected — they sit green+reviewed until you merge them or restart the
  loop (the next wave's Phase 0 reaps them).

Stopping is safe at any time: all durable state is in `docs/backlog.md` + GitHub + git. A
half-finished lane is just an unmerged branch; delete it (`git worktree remove --force …`) or
let the next run rebase and continue it.

---

## Resume (including after a restart or days later)

```text
/loop /orchestrate
```

Phase 0 reconstructs everything: it refreshes root, lists open PRs (reaps the green ones),
reads `docs/backlog.md`, and loads `.orchestrator/state.json` if present. An empty
`.orchestrator/` is fine — state is rebuilt from the backlog + `gh`.

### The 7-day `/loop` expiry (long runs)

`/loop` (dynamic mode) auto-expires after ~7 days. For runs longer than a week, **re-arm** by
re-issuing `/loop /orchestrate` before/after expiry. Nothing is lost at expiry — it only stops
the heartbeat; the backlog, PRs, and journal persist, so a re-issue continues seamlessly. Note
the expiry on your calendar if you intend a multi-week unattended run.

---

## Failure recovery

| Symptom | What it means / what to do |
|---|---|
| A lane can't reach green | The impl agent returns `{failed, reason}`; the orchestrator re-queues it with a bounded retry, or files a blocker in `docs/backlog.md`. One poison item never wedges the loop. |
| A PR's CI is red | Phase 0 of the next wave spawns a fix lane (TDD fix → push). It is not merged until green. |
| Merge conflict on an old lane | Lanes branch from HEAD and root is refreshed after every merge; a drifted lane is rebased on current HEAD and re-gated before merge. |
| `codex` unavailable/unauthenticated | The review driver reports `codex-unavailable` and falls back to the cross-tier Claude reviewer, noting the gap honestly — it does not silently pass. Restore codex auth to regain true cross-vendor review (rule 21). |
| Memory MCP locked | Another session has the clone open. Close it (one session per clone). The loop degrades gracefully without memory but loses recall continuity. |
| Orphaned worktrees accumulate | Phase 0 sweeps them (`git worktree prune` + remove merged/ephemeral dirs). To sweep manually: `git worktree prune && git worktree list`. |
| The loop seems to have stopped | It must not. Check `/workflows` and scheduled wake-ups; re-issue `/loop /orchestrate`. Report the transcript context — a stop is a defect to fix in the skill, per ADR-0072. |

---

## Cost controls

- **Fan-out width** is capped by the Workflow concurrency limit (`min(16, cores−2)`); the wave
  batch size is bounded; the script is `budget`-aware (`+<tokens>` directives scale depth).
- **Model tiering** keeps mechanical work on `haiku`/`fable` and reserves `opus` for hard
  correctness/security and synthesis — see the rubric in the skill.
- To run a cheaper pass, start the session at a lower effort and/or pass a token budget; to
  pause spend, stop the loop (above). Auto-merge means work lands without you paying for idle
  supervision.

---

## Changing scope (dimensions, models, propose-only boundary)

- **Add/adjust gap-review dimensions** or **model assignments**: edit
  `.claude/skills/orchestrate/wave.workflow.js` (`DEFAULT_DIMENSIONS`, `pickReviewer`) and the
  rubric in `SKILL.md` — via a lane → PR (the loop can do this to itself).
- **Approve a new infra surface** (web/API/metrics sink/recordings): the loop will have filed a
  *Proposed* ADR + backlog item. Flip the ADR to `Accepted` (operator decision, rule 40) and
  the loop will then build it. Until then it stays propose-only.

---

## Related

- [`docs/adr/0072-autonomous-orchestration-loop.md`](../adr/0072-autonomous-orchestration-loop.md) — the WHY + architecture.
- `.claude/skills/orchestrate/SKILL.md` — the executable operating contract.
- [`docs/backlog.md`](../backlog.md) — the canonical work register the loop drains and refills.
- `AGENTS.md` rules 8–13 (lanes), 14–22 (gates/review), 40–42 (no-new-platform, runbooks).
