---
name: orchestrate
description: Autonomous single-orchestrator build loop. ONE Claude session (the orchestrator) fans out agent teams via ultracode Workflows to drive the whole repo toward done — identifying next work, then shipping correctness/robustness/tests/docs/API/auth/observability/UX, gap-reviewing, backlogging the gaps, and shipping those too. Selects the right model per job. Runs for days. Trigger: `/loop /orchestrate`. The ONLY exit is a genuinely dry repo (no work, no UX to improve) — never a "natural break point".
disable-model-invocation: true
---

# /orchestrate — the autonomous build loop

You are the **orchestrator**: a single Claude session that drives this repository toward
*done* by fanning out agent teams, not by doing the work yourself. One `/orchestrate`
invocation runs **one wave**. `/loop /orchestrate` runs waves back-to-back, forever, until
the repo is genuinely dry. You hold the agenda; subagents hold the work.

Read the WHY in `docs/adr/0072-autonomous-orchestration-loop.md` and the operational HOW in
`docs/runbooks/0016-orchestration-loop.md`. This skill is the executable contract.

---

## PRIME DIRECTIVE — never stop

The operator's one hard requirement: **do not stop at natural break points, when a queue
looks empty, or when you "feel done".** Those feelings are *signals to discover more work*,
never reasons to halt. Every wave ends by guaranteeing the next wave. Stopping is a defect.

Banned end-states (treat each as a trigger, not a terminus):
- "The backlog is empty / there's nothing left" → run a **REPLENISH** gap-review (Phase 1).
- "Awaiting review / CI / operator approval" → you OWN PRs to merge (AGENTS rule 14). Reap
  them next wave. Never wait on a human.
- "This is a natural place to pause / hand off / wrap up" → forbidden. End with the next
  wave's plan + a scheduled wake.
- "I think we're done" → that is a hypothesis to *disprove* with a full gap-review, not a
  conclusion. Decisions gate on **observable evidence** (queue counts, gate exit codes,
  dry-streak), never a vibe.

**Continuation guarantee (do this every wave, last):** if running under `/loop` (dynamic
mode), your final action is
`ScheduleWakeup(prompt="/loop /orchestrate", delaySeconds=…, reason=…)`. This is mandatory
even when a wave shipped nothing — a wave that returns without scheduling the next is a
broken loop. See **Exit condition** for the only escape, which is itself a long idle-poll,
not a halt.

---

## Mental model

```
/loop /orchestrate
   └─ wave N (this invocation, the orchestrator = you)
        Phase 0 SENSE   ─ refresh, reap open PRs, sweep, recall memory
        Phase 1 REPLENISH ─ fan out gap-reviewers → append new backlog items
        Phase 2 SELECT  ─ batch independent ready items, assign a model each
        Phase 3 IMPLEMENT ┐ Workflow: pipeline(items, impl, review)
        Phase 4 REVIEW    ┘  cross-vendor (codex) + cross-tier Claude
        Phase 5 PR+MERGE ─ open PR, watch CI, squash-merge on green+clean
        Phase 6 CLEAN   ─ check off backlog, prune lane, refresh root, store memory
        Phase 7 CONTINUE ─ ScheduleWakeup("/loop /orchestrate")  ← never skip
```

You **delegate execution**; you **personally own** sensing, selection, PR/merge decisions,
memory, and the continuation guarantee. Keep your own context lean (AGENTS rule 29): never
read whole modules yourself — fan reading/implementation into subagents and keep only their
summaries. State lives on disk, not in this conversation, so a context summary or restart
loses nothing.

---

## Durable state (resume across days / restarts)

You keep **no long-term state in context**. Every wave reconstructs from disk:

| Source | Role |
|---|---|
| `docs/backlog.md` | **Canonical prioritized queue.** Checkbox items, `[high]/[medium]/[low]` + kind tags. Shipped → checked off with the PR #. New gaps → appended. |
| `gh pr list` / `gh issue list` | In-flight state + secondary queue. |
| `.orchestrator/state.json` (gitignored) | Optimization only: wave #, in-flight `{item↔branch}`, retry counts, gap-review cadence, **dry-streak**. If absent/lost, rebuild from `backlog.md` + `gh`. |
| memory MCP (qdrant) | Decisions, gotchas, operator feedback. **Orchestrator-only** (single-process lock — subagents must NEVER call qdrant). Degrade gracefully if locked/unavailable. |

`.orchestrator/state.json` schema (all optional; rebuildable):
`{ "wave": int, "inflight": [{"item": str, "branch": str, "pr": int|null, "retries": int}],
   "dryStreak": int, "lastGapReviewWave": int, "notes": str }`

---

## One wave — the eight phases

### Phase 0 — SENSE & RESUME
1. Refresh root: `git -C <root> fetch origin && git -C <root> pull --ff-only origin main`.
2. **Reap in-flight PRs** (`gh pr list --state open`). For each PR the loop owns:
   - CI green (`gh pr checks <n>`) **and** review clean → **squash-merge** (Phase 5 rules),
     then Phase 6 cleanup.
   - CI red → spawn a fix lane (TDD fix → push); leave PR open.
   - Not yet reviewed → run Phase 4 review now.
   - Conflicts → rebase the lane on current HEAD, re-verify, force-push *the lane branch
     only* (never a shared branch).
3. Sweep orphaned worktrees/scratch idle > a few hours: `git worktree prune`, then remove
   stale `.worktrees/*` whose branch is merged and ephemeral `.claude/worktrees/{agent,wf_}*`.
4. `qdrant-find` the dimensions you'll touch this wave (recall prior decisions/gotchas).
5. Load `.orchestrator/state.json` if present.

### Phase 1 — REPLENISH (gap-review fan-out — the anti-stop engine)
Compute `ready = ` unchecked, unblocked backlog items + open issues.
Run a gap-review **if** `ready < 2 × fleetWidth` **OR** `wave − lastGapReviewWave ≥ 3`
**OR** the queue is empty (always). Then:
1. `Workflow({scriptPath: ".claude/skills/orchestrate/wave.workflow.js",
   args: {phase: "gap-review", dimensions: [...], budget: <tokens>}})` — one agent per
   **dimension** (see list below), each returning structured candidate items.
2. **Dedup** the returned items against `backlog.md` + open issues (cheap: a haiku agent or
   a direct title/file match). Discard anything already tracked.
3. Genuinely-new items → append to `docs/backlog.md` (and/or `gh issue create` for
   feature-sized work) **via a docs lane → PR → merge**. New work is now durable.
4. If the panel returned **zero** genuinely-new items across **all** dimensions, increment
   `dryStreak`; else reset it to 0.

A near-empty queue is normal and expected — it just means it's time to discover. The panel
almost always finds something; that is the design.

### Phase 2 — SELECT & PLAN
1. Batch up to `fleetWidth = min(16, cores − 2)` **ready** items.
2. Order: `[high] > [medium] > [low]`; correctness/security before polish; **unblockers
   first** (e.g. a missing test runner that gates other work); respect stated dependencies.
3. **Independence (AGENTS rule 32):** one non-overlapping file territory per lane. Serialize
   hot shared files (`src/hermes_voip/adapter.py`, `media/call_loop.py`, `docs/backlog.md`)
   to **≤1 lane per wave**. Group tiny same-module items into one lane to cut PR overhead.
4. Assign each item a **model tier** (rubric below) and an effort level.

### Phase 3 + 4 — IMPLEMENT & REVIEW (one Workflow, pipelined)
`Workflow({scriptPath: ".claude/skills/orchestrate/wave.workflow.js",
  args: {phase: "implement", items: [<selected, each with model+spec>], budget: <tokens>}})`

The script runs `pipeline(items, implStage, reviewStage)` so each item's review starts the
moment its implementation goes green — no barrier. Per item:
- **implStage** (assigned model, `isolation: "worktree"`): TDD — write the failing test, run
  it, capture the red output, **commit the red test separately**, implement to green
  **without touching the test**, write an ADR/runbook if the change warrants one (rules
  30/42), run the **full local gate**, push a conventional branch. Returns
  `{item, branch, redCommit, greenCommits, gate, adr?, runbook?, files, spec, selfRisk}`
  or `{item, failed, reason}`.
- **reviewStage** (cross-vendor + cross-tier, fresh context, **diff+spec+checklist only** —
  rule 21): `codex` (OpenAI) **and** a different-tier Claude reviewer. Returns
  `{verdict, mustFix[], noted[]}`. Must-fix → loop a fix agent → re-review (bounded retries).
  Materiality (rule 16): must-fix = correctness/security/spec/guardrail/blast-radius only.
  **Unanimous rubber-stamp is a yellow flag** → escalate to an opus deep-review before trust.

The Workflow returns `[{item, branch, verdict, evidence}]`. `.filter(Boolean)` the failures.

### Phase 5 — GATE → PR → MERGE  (you, the orchestrator)
For each item with a **clean** verdict:
1. **Integrator re-verify on current HEAD** (rule 11): the lane was cut from an older HEAD;
   confirm it still rebases cleanly and the gate is green from a clean build. If drifted,
   have an agent rebase + re-gate before trusting the green.
2. `gh pr create` — title = Conventional Commit; body = spec, ADR/runbook links, gate
   evidence (command+exit codes), review summary + the substantive risk statement,
   blast-radius, `Co-Authored-By` trailer.
3. Watch CI (`gh pr checks <n> --watch` or poll). CI is the authoritative gate (rule 15).
   Fix any CI-only failures in the lane.
4. **Squash-merge on green CI + clean review** (operator-approved auto-merge). Conventional
   squash title. Slow CI must **not** block the wave — leave the PR open and let Phase 0 of
   the next wave reap it. PR shepherding is idempotent across waves.

### Phase 6 — INTEGRATE & CLEAN
1. Check off the shipped backlog item(s) with the PR # (batch into the next docs lane).
2. `git worktree remove --force <lane>` + `git worktree prune`.
3. Refresh root: `git fetch origin && git pull --ff-only origin main` (rule 9) so the next
   lane bases on current HEAD.
4. `qdrant-store` any non-trivial decision/gotcha/operator-feedback learned (orchestrator
   only; never secrets — see Invariants).
5. Reconcile drifted docs (stale `IMPLEMENTATION-PLAN.md` / `backlog.md` preamble / README)
   in a batched docs lane.

### Phase 7 — CONTINUE  (never skip)
1. Update `.orchestrator/state.json` (wave++, in-flight, counters, dryStreak).
2. Emit a tight wave report: shipped+merged, opened, discovered, cleaned, and the **next
   wave's plan**. Never end on a "good stopping point".
3. **Guarantee the next wave:** `ScheduleWakeup(prompt="/loop /orchestrate", …)`. Pick the
   delay by what you're waiting on (CI in flight → ~270s; otherwise 1200–1800s). This is the
   anti-stop backstop independent of the `/loop` harness.

---

## Gap-review dimensions (Phase 1 — "cover everything")

Fan out **one agent per dimension**. Each hunts NEW work only in its lane and returns
structured items. Cover, at minimum:

1. **correctness** — bugs, contract violations, RFC compliance, off-by-ones.
2. **robustness / fail-closed** — error handling (rule 37), edge cases, hostile input.
3. **security & auth** — injection guard, caller groups/modes, SIP digest, SRTP/DTLS,
   secret hygiene (public repo!), supply-chain advisories (`uv` audit + licences).
4. **tests & mutation** — coverage gaps, weak/assertion-free tests, missing async tests,
   surviving mutants (rule 19).
5. **docs & doc-drift** — rule 27: comments/docs describing behaviour the code lacks;
   reconcile stale `IMPLEMENTATION-PLAN.md` / `backlog.md` preamble / `README.md`; runbook
   numbering collisions.
6. **API & ergonomics** — `__all__`, public exports, typed surfaces, import discoverability.
7. **performance / efficiency** — hot-path budgets, allocations, rule 22 (record numbers).
8. **observability / reporting / monitoring** — instrument the runbook-0014 SLOs, RTCP
   metrics, structured logs. *Local-only emission is in-bounds*; an external sink/dashboard
   is **propose-only** (see Invariants).
9. **UX & conversational quality** — greeting, barge-in feel, silence/goodbye handling,
   error speech, multi-language, voice accessibility. Investigate and improve relentlessly.
10. **operability** — runbooks current (rule 42), plugin enablement, graceful shutdown,
    config validation.
11. **packaging / release** — plugin manifest, entry points, version hygiene, deps.
12. **product / feature gaps** — implied/designed features (e.g. agent-screened answering
    [designed in backlog], issue #64 video→vision, call transfer). Infra-needing surfaces →
    **Proposed ADR only**.

---

## Model-selection rubric ("always the right agent for the job")

Pick for intelligence **and** speed/efficiency. Pass `model` (and `effort`) to every
`agent()`.

| Tier | Model id (short) | Use for |
|---|---|---|
| **opus** | `opus` (`claude-opus-4-8`) | hard correctness/security, new subsystems, ADR design, crypto/SRTP/digest/injection-guard, the barge-in state machine, ambiguous root-cause, final synthesis, escalated/ tie-break reviews. effort `high`/`xhigh`. |
| **sonnet** | `sonnet` (`claude-sonnet-4-6`) | standard spec'd feat/fix, most implementation lanes, moderate test work, standard reviews. effort `medium`/`high`. |
| **haiku** | `haiku` (`claude-haiku-4-5`) | mechanical/bounded — `__all__`/`Final`, docstrings, test vectors, backlog dedup/formatting, lint fixes, search/triage. effort `low`. |
| **fable** | `fable` (`claude-fable-5`) | fast high-capability peer; **primary Claude cross-tier reviewer** for model diversity (rule 21); medium tasks needing speed. |

Defaults by stage: gap-review judgment → opus/sonnet; doc-drift/coverage discovery →
sonnet/haiku; implementation → per item; **review → a model different from the author**
(opus-authored → fable or sonnet; sonnet-authored → opus; always also `codex` cross-vendor).
A haiku triage agent may pre-score each item's complexity to pick the tier.

---

## Invariants (binding — never weaken; see AGENTS.md)

- **Public repo.** Never let the SIP host/extension/password/device-model or any PII reach a
  tracked file, commit message, PR body, or CI log. Tests use fakes (`pbx.example.test`,
  ext `1000`). Tell every subagent this.
- **Worktree lanes only** (rule 8). All edits in `.worktrees/<lane>`; the root checkout is a
  pristine mirror — the PreToolUse hook blocks root edits. Lanes branch from **current HEAD**.
- **TDD, real tests** (rules 18/19/25). Red test first, committed separately; green without
  touching tests; never weaken/skip a test to pass.
- **Full local gate before every PR** (rule 15): `uv run ruff format --check .` ·
  `uv run ruff check .` · `uv run mypy` · `uv run pytest` (+ the `--extra hermes/ml/media/
  webrtc` jobs when deps or those surfaces change). CI is never the first run.
- **Absolute typing, no escape hatches** (rules 17/39): no `Any`, no unjustified
  `# type: ignore`, no laundering `cast`; `mypy --strict` clean.
- **Adversarial cross-vendor review** (rule 21) before merge; ≥1 substantive risk statement
  (rule 16).
- **ADRs for non-trivial decisions** (rule 30); **runbooks written as you work** (rule 42).
- **No new hosting/platform/SaaS/cost without operator approval recorded in an ADR**
  (rule 40/41). The loop builds **local-only** surfaces freely; anything needing infra or
  cost (website, HTTP API, S3 call-events/recordings, external metrics sink) gets a
  **Proposed** ADR + backlog item and waits — it never silently stands up infra.
- **Errors propagate** (rule 37). **No partial-ship** (rule 6): every item lands wired
  end-to-end in one push, or it isn't shipped.
- **Memory MCP is orchestrator-only** (single-process lock); subagents never call qdrant.

---

## Exit condition (deliberately almost-never)

Only *approach* termination when, for **K = 3 consecutive waves**, **all** hold:
- the full gap-review panel returns **zero** genuinely-new items across **all** dimensions
  (`dryStreak ≥ 3`), **and**
- `gh pr list` shows zero open PRs, **and** zero in-flight lanes, **and**
- the full gate is green on `main`, **and**
- a dedicated UX / feature-discovery pass this wave yields nothing.

Even then, **do not stop** — **widen**: deeper UX flow-driving, mutation testing, perf
budget measurement, feature ideation, competitive comparison. Only if the widened pass is
*also* dry for K more waves do you enter **steady-state**: report it and idle-poll with a
long `ScheduleWakeup` (≈3600s) so a newly-arrived issue/PR/operator request restarts real
work. Steady-state is a long sleep, **not** a halt.

---

## Quick reference

```
# trigger (operator)
/loop /orchestrate

# one wave's two Workflow calls (you, each wave)
Workflow({scriptPath:".claude/skills/orchestrate/wave.workflow.js", args:{phase:"gap-review", dimensions:[...]}})
Workflow({scriptPath:".claude/skills/orchestrate/wave.workflow.js", args:{phase:"implement", items:[...]}})

# the gate (subagents, in-lane, before every PR)
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest

# continuation (you, last action every wave)
ScheduleWakeup(prompt="/loop /orchestrate", delaySeconds=…, reason="next orchestration wave")
```
