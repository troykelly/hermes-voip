---
name: orchestrate
description: Run one autonomous delivery cycle — pick up what needs doing, dispatch it in parallel, merge what is ready, and record the result. Use when asked to orchestrate, run the loop, run a delivery cycle, work the backlog, or continue autonomous development. Repo-agnostic; reads orchestrate.config.json for local specifics.
---

# Orchestrate

One cycle. Then **stop and exit**.

## The rule everything else serves

**Continuity comes from GitHub, never from this context window.** You do one cycle and end the session. A scheduler starts the next one clean.

You must never: loop back to the top in-context; call `ScheduleWakeup` to continue yourself; return a "block" decision from a Stop hook; or keep working because it feels unfinished. Those all keep one session alive across thousands of turns, and every turn re-sends the whole transcript. That pattern is 4% of sessions and 66% of spend.

If the cycle ends with work outstanding, that is the correct outcome. Say so and exit.

## Config

Read `orchestrate.config.json` from the repo root. If absent, stop and ask for one — do not guess lanes.

```json
{
  "readyLabel": "status: ready",
  "blockedLabel": "status: blocked",
  "knownLanes": ["api", "db", "web", "ui", "docs", "infra"],
  "singleWriterLanes": ["api", "db", "infra"],
  "scopedLanes": ["ui"],
  "maxWave": 6,
  "maxOpenPRs": 3,
  "maxFilePerSweep": 10,
  "requiredDryCycles": 2,
  "commands": { "bootstrap": "./scripts/bootstrap", "verify": "./scripts/verify", "test": "./scripts/test" },
  "dimensions": ["spec-parity", "defects", "tests", "docs", "as-built", "security", "observability", "ux"]
}
```

## The cycle

### 1. Orient — bounded, and from ground truth only

Ground truth is `gh`, `git` and CI. Never a status document, never memory, never a previous cycle's say-so.

```sh
gh issue list --state open --label "<readyLabel>" --limit 100 --json number,title,labels,assignees
gh pr list --state open --author "@me" --json number,title,statusCheckRollup,mergeable
```

Read the loop-state issue (labelled `loop-state`) for `consecutiveDryCycles` and the ledger. **Read it by number**, not by search — list endpoints lag.

If a live owner holds the lease (recent `heartbeatAt`, different `ownerRunId`), **exit now**. Two orchestrators is worse than none.

Keep this step small. Use `--json` with named fields, never bare `gh` output. Never `cat` a file you could `Read`.

### 2. Close — serial, single Closer

You are the only thing that merges. Builders never merge; that rule exists because two PRs that were green alone landed red together.

For each PR reporting READY, one at a time: rebase onto fresh `main` → wait for a **fresh** CI pass on the rebased head → merge → delete branch → `git worktree prune`.

`Closes #n` must be plain text, never backticked, or GitHub ignores it. Never `gh pr merge --auto` before green — it merges immediately. `cancelled` is not a passing verdict.

### 3. Dispatch — parallel build, disjoint lanes

Classify each candidate into `{ number, lane, scope, blockedBy: [{ number, satisfied }] }`. Verify each blocker with `gh issue view <n> --json state` and `gh pr list --search "<n>"`; do not trust the label.

Then partition **in code**, not by judgement:

```sh
node .claude/skills/orchestrate/lib/partition.mjs   # via a tiny driver, or import it
```

Dispatch the returned wave with the Workflow tool: one agent per issue, `isolation: "worktree"`, a `schema` on every agent, and model per lane — cheap tier for mechanical lanes, top tier for anything on `singleWriterLanes` or `unknown`.

Every agent schema must cap its output. Require `file:line` evidence and **forbid file bodies in the return value**. N parallel agents returning file dumps is how the orchestrator's own context explodes.

Builders take an issue to CI-green and report READY. They do not merge.

Respect `maxOpenPRs`. If the cap is reached, skip dispatch this cycle and go to step 5.

### 4. Discover — only when the frontier is empty

An empty backlog is a trigger to look harder, not a reason to stop. Run one sweep across `dimensions`, one agent per dimension, in parallel. Each returns findings as `{ kind, confirmed, title, evidence }`.

The generic dimensions, which is what "complete" means here: does it do what was designed (`spec-parity`); is it broken (`defects`); is it tested (`tests`); is it documented (`docs`); do the docs match what was actually built (`as-built`); is it safe (`security`); can you see it running (`observability`); is it decent to use (`ux`).

File at most `maxFilePerSweep`. Anything beyond the cap goes into the cycle report as deferred with its title — **never silently dropped**; the next cycle files it.

Before creating an issue, append `{ key, title, status: "reserving" }` to the loop-state ledger and save. Then create. Then mark `created` with the number. `gh issue list` lags creates by around a minute, and a naive re-check duplicates epics.

**Materiality floor.** A finding only resets the dry-cycle counter if it is a real defect, a security or privacy problem, data loss, a missing designed feature, a wrong document, or something a user would notice. Nits get filed as chores and **do not** reset the counter. Without this floor a thorough reviewer always finds something and the loop can never converge.

### 5. Record

Update the loop-state issue: `ownerRunId`, `heartbeatAt`, `consecutiveDryCycles` (via `nextDryCycles`), the ledger, and a one-paragraph cycle summary. This is what the next session reads instead of your context.

### 6. Decide, report, exit

```sh
node .claude/skills/orchestrate/lib/done-gate.mjs   # via a driver: evaluateDone({...})
```

DONE is a computed fact. You may not declare it, override it, or argue `requiredDryCycles` below 2.

- **DONE** → write the `DONE` sentinel into the loop state dir, comment the final summary on the loop-state issue, exit.
- **NOT DONE** → print the cycle report and **exit**. The scheduler starts the next cycle in a fresh session.

Report exactly:

```
CYCLE:     <n>  owner=<runId>
MERGED:    <PR numbers, or none>
DISPATCHED:<issue numbers, or none>  deferred=<numbers + one-line reasons>
FILED:     <issue numbers, or none>  deferred=<titles>
DRY:       <consecutiveDryCycles>/<K>
VERDICT:   DONE | PARKED (blocked on human) | CONTINUE
```

## Never stop because

The cycle felt complete. You merged something. You hit a milestone or finished an epic. CI is green. The PR list is momentarily empty. A wave finished. These are transitions, not endings — the only endings are the DONE gate, the STOP sentinel, and a stop condition below.

## Never continue because

There is more to do. There is always more to do. **One cycle per session** is the invariant that keeps this affordable.

## Stop and escalate when

Credentials or authority are missing; a destructive action has no tested recovery path; a secret or production dataset appears; CI is red on `main`; the same item has failed twice — label it `blocked`, file a tracking issue, and move on.

## Operator

```sh
touch .claude/loop/ACTIVE        # arm
touch .claude/loop/STOP          # stop after the current cycle
rm    .claude/loop/DONE          # resume after convergence
./.claude/skills/orchestrate/tick.sh   # run one cycle now, in a fresh session
```

`tick.sh` is the scheduler's entry point. It takes a lock, runs exactly one cycle in a new `claude` process, and exits. Point cron at it. Do not replace it with anything that keeps a session alive.
