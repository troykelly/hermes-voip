# Runbook: operating the autonomous delivery cycle

**What it is.** The operational HOW for the autonomous delivery loop — arm it, schedule it,
watch it, stop it, recover it, and bound its cost. The WHY is in
[`docs/adr/0114-one-cycle-per-session-orchestration.md`](../adr/0114-one-cycle-per-session-orchestration.md);
the executable contract is `.claude/skills/orchestrate/SKILL.md`; the repo-specific lanes,
caps and commands are in `orchestrate.config.json`.

This is a **present-tense operational HOW**: it describes what IS. Update it in the same
change whenever the loop's mechanics change.

> **Public-repo rule.** Never write a real host/extension/password/IP/PII into this runbook
> or anywhere the loop can commit — including issue and PR bodies. The loop and its
> subagents are told this; you must hold it too.

---

## What it does, in one paragraph

`tick.sh` starts a **fresh `claude` process** that runs the `/orchestrate` skill for exactly
**one cycle** — orient from `gh`/`git`/CI, close ready PRs one at a time, dispatch a wave of
disjoint lanes in parallel isolated worktrees, discover new work when the frontier is empty,
record state to the `loop-state` issue, print a cycle report — and then **exits**. Cadence
comes from cron, never from the agent. Ending a cycle with work outstanding is correct.

The predecessor design (ADR-0072) kept one session alive across waves. It is superseded:
cost is `prefix_size × turn_count`, and sessions over 250 turns measured 4% of sessions and
66% of spend. Fan-out was never the problem and is retained in full.

---

## Prerequisites (verify before arming)

```bash
# 1. Repo root, current main, clean tree.
cd /workspaces/hermes-voip
git status -sb && git rev-parse --abbrev-ref HEAD     # expect: ## main..., clean

# 2. GitHub CLI authenticated as the acting user (the loop opens and merges PRs).
gh auth status                                        # expect: Logged in

# 3. The cross-vendor reviewer is present.
codex --version                                       # expect: codex-cli x.y.z

# 4. The gate is green NOW. CI is never the first run.
uv sync --frozen && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest

# 5. The pure gates pass (22 assertions, no network, ~instant).
node .claude/skills/orchestrate/lib/test.mjs          # expect: 22/22 passed

# 6. The memory MCP is free (single-process lock): no OTHER session has this clone open.
```

If any check fails, resolve it first — do **not** arm the loop on a red gate or an
unauthenticated `gh`.

### One-time setup

The skill reads GitHub, so the labels it queries must exist:

```bash
gh label create "status: ready"   --description "Loop may dispatch this" --color 0E8A16
gh label create "status: blocked" --description "Needs a human or an unmet dependency" --color B60205
gh label create "loop-state"      --description "Cross-cycle orchestrator state (one issue)" --color 5319E7
```

Then open a single issue labelled `loop-state` holding a JSON body with `ownerRunId`,
`heartbeatAt`, `consecutiveDryCycles` and the issue-creation ledger. **Record its number
here once created** — the skill reads it by number, because list endpoints lag creates by
around a minute.

---

## The queue: `docs/backlog.md` and GitHub issues

`docs/backlog.md` remains this repo's canonical, human-readable work register. The skill
orients from GitHub, so the two are bridged explicitly:

- A backlog item becomes dispatchable by being filed as an issue labelled `status: ready`
  with a lane name (see below). Discovery sweeps file issues directly.
- A merged PR checks its backlog item off with the PR number, exactly as before.
- Anything only in `backlog.md` and never filed is **not** visible to a cycle. That is
  deliberate: the frontier is what has been triaged, not everything ever written down.

---

## Lanes (from `orchestrate.config.json`)

Partitioning is computed by `lib/partition.mjs`, never judged by the model. An issue's lane
is declared when it is filed; an unrecognised lane coerces to `unknown`, which is treated as
single-writer — fail safe.

| Lane | Surface | Concurrency |
| --- | --- | --- |
| `sip` | `adapter.py`, `registration.py`, `dialog.py`, `refer.py`, `message.py`, `transport/` | **single-writer** — `adapter.py` is the hot shared file |
| `media` | `media/**` (RTP/RTCP, SRTP/DTLS, Opus, jitter, AEC, `call_loop.py`) | **single-writer** — `call_loop.py` is the hot shared file |
| `core` | `config.py`, `plugin.py`, `manifest.py`, `plugin.yaml`, `guard/`, caller modes | **single-writer** — the env-var registry trio must move together |
| `ci` | `.github/**`, `packaging/**`, `pyproject.toml`, `uv.lock`, `tools/**` | **single-writer** — packaging and deps |
| `providers` | `providers/**`, `stt/**`, `tts/**` | scoped — one item per provider module |
| `tests` | `tests/**` | scoped — one item per test module |
| `docs` | `docs/**`, `README.md` | scoped — one item per file, which serialises `docs/backlog.md` |

`maxWave` 6, `maxOpenPRs` 3, `maxFilePerSweep` 10, `requiredDryCycles` 2.

---

## Arm and schedule

```bash
touch .claude/loop/ACTIVE                 # arm; tick.sh is a no-op without this
./.claude/skills/orchestrate/tick.sh      # run one cycle now, in a fresh session
```

`.claude/loop/` is gitignored: arming on one clone never arms anyone else's.

Pick **one** scheduler and point it at `tick.sh`. It self-locks, so an overlapping fire
exits immediately rather than racing.

```bash
# cron — every 15 minutes
*/15 * * * * cd /workspaces/hermes-voip && ./.claude/skills/orchestrate/tick.sh >>.claude/loop/tick.log 2>&1

# or, for a foreground/devcontainer session
watch -n 900 ./.claude/skills/orchestrate/tick.sh
```

**Cadence for this repo: every 15 minutes.** A cycle here is dominated by CI (the `gate` job
plus the slower `hermes-contract` extras job), so a shorter period mostly produces ticks that
find the same PR still pending and exit. `ORCH_MAX_SECONDS` (default 3600) caps a single
cycle; on timeout the process is terminated and the next tick resumes from GitHub.

There is deliberately **no** always-on supervisor and **no** self-scheduling from inside the
skill. If you find either, it is a regression against ADR-0114.

---

## Observe it

| Signal | Command / where |
|---|---|
| Cycle history | `.claude/loop/tick.log` (each line stamped UTC) |
| Live fan-out within a cycle | `/workflows` |
| Cross-cycle state | the `loop-state` issue (owner, heartbeat, dry-cycle count, ledger) |
| PRs in flight / merged | `gh pr list --state open` · `gh pr list --state merged --limit 20` |
| Frontier | `gh issue list --state open --label "status: ready"` |
| Work register | `docs/backlog.md` |
| Active lanes | `git worktree list` |

Healthy over time: open PRs cycle to merged, backlog items get checked off, new issues
appear, and `git worktree list` does not accumulate stale lanes.

---

## Stop, resume, converge

```bash
touch .claude/loop/STOP    # stop after the current cycle; leave the cron in place
rm    .claude/loop/STOP    # resume
rm    .claude/loop/ACTIVE  # disarm entirely
rm    .claude/loop/DONE    # resume after the loop converged and wrote DONE itself
```

Stopping is safe at any time — all durable state is in GitHub and git. A half-finished lane
is just an unmerged branch: delete it (`git worktree remove --force …`) or let the next cycle
rebase and continue it. In-flight PRs are unaffected; the next cycle's step 2 reaps them.

The loop writes `DONE` itself only when `lib/done-gate.mjs` computes it: no buildable issues,
no open orchestrator PRs, and `requiredDryCycles` consecutive cycles with no **material**
finding. DONE is a computed fact — it cannot be declared by the model, and `K` cannot be
argued below 2.

---

## Failure recovery

| Symptom | What it means / what to do |
|---|---|
| A lane can't reach green | The builder returns a failure with a reason; the item is left open. After the **same item fails twice**, label it `status: blocked`, file a tracking issue and move on. One poison item never wedges the loop. |
| A PR's CI is red | The next cycle spawns a fix lane (TDD fix → push). Never merged until green. `cancelled` is not a passing verdict. |
| Merge conflict on an old lane | Lanes branch from current HEAD and root is refreshed after every merge; a drifted lane is rebased and re-gated on the rebased head before merge. |
| A PR merged but the issue stayed open | `Closes #n` must be **plain text** — GitHub ignores it inside backticks. Close by hand and fix the PR template usage. |
| `codex` unavailable/unauthenticated | The review driver reports `codex-unavailable` and falls back to the cross-tier Claude reviewer, noting the gap honestly — it does not silently pass. Restore codex auth to regain true cross-vendor review. Feed codex `git diff main...HEAD` (three-dot) or rebase first; a two-dot diff on a behind branch causes false BLOCKs on main's own advances. |
| Memory MCP locked | Another session has the clone open (one session per clone). The loop degrades gracefully without memory. Subagents must **never** call qdrant — it is single-process and orchestrator-owned. |
| Orphaned worktrees accumulate | `git worktree prune && git worktree list`; remove merged `.worktrees/*` and ephemeral `.claude/worktrees/{agent,wf_}*`. |
| `tick.sh` says "cycle already running" | A live pid holds `.claude/loop/tick.lock`. A stale lock from a dead pid is reclaimed automatically; do not delete a live one. |
| Two orchestrators appear to run | The skill exits on seeing a recent foreign `heartbeatAt`/`ownerRunId` in the loop-state issue. Confirm only one scheduler is armed across all clones. |
| The loop "seems to have stopped" | Unlike ADR-0072, **this is not automatically a defect**. Check for `STOP`/`DONE` sentinels, then `.claude/loop/tick.log` and the cron. A cycle that ends with work outstanding is correct behaviour. |
| `pre-push` hook hangs on pytest | Push with `--no-verify` and let CI re-gate — do not skip the run itself. |

---

## Cost controls

The loop's cost model, and why this design exists:

- **Turn count is the multiplier.** `cache_read` is 59.5% of true spend and equals prefix
  size × turns. One cycle per session is the primary control; everything else is secondary.
- **Fan-out is cheap, long sessions are not.** Subagent transcripts never enter the
  orchestrator's context — only their final structured message. Keep every agent schema
  capped, require `file:line` evidence, and forbid file bodies in return values.
- **Bash is 74.6% of tool-result bytes.** `.claude/hooks/prefer-native-tools.mjs` blocks
  file reads routed through Bash. Batch shell calls; keep output small.
- **Model tiering:** mechanical lanes on `haiku`, implementation and focused review on
  `sonnet`, architecture and adjudication on `opus`. Reviewer must differ from the author.
- **Caps:** `maxWave` bounds a wave, `maxOpenPRs` bounds work in flight, `maxFilePerSweep`
  bounds discovery, `ORCH_MAX_SECONDS` bounds a single cycle.

To pause spend entirely, `touch .claude/loop/STOP`.

---

## The R3 stop-line (unchanged, binding)

Arming the loop is the operator's standing approval for it to self-merge **R0/R1/R2** work
on green CI plus a clean cross-vendor review — recorded in
`docs/standards/engineering.md` Part H. It does **not** extend to **R3**: credentials, secret
handling, publishing to PyPI or a public Release, licence or legal content. An R3 item is
prepared, pushed and left **open** with its class in the PR body, and the cycle moves on. The
loop never merges R3 and never pushes a release tag. Classify with
`uv run python -m tools.classify`.

---

## Changing scope

- **Lanes, caps, commands, dimensions:** edit `orchestrate.config.json` via a lane → PR. It
  is R2 (it can widen a single-writer lane, which is how two agents end up in one file).
  Every lane in `singleWriterLanes`/`scopedLanes` must also appear in `knownLanes`.
- **Per-dimension focus and model assignments:** edit
  `.claude/workflows/wave.workflow.js` (`DEFAULT_DIMENSIONS`, `pickReviewer`).
- **Partition or DONE semantics:** edit `lib/partition.mjs` / `lib/done-gate.mjs` and extend
  `lib/test.mjs` in the same change. These are safety logic — never loosen an assertion to
  make a cycle proceed.
- **Approve a new infra surface** (web/API/metrics sink/recordings): the loop will have filed
  a *Proposed* ADR + backlog item. Flip the ADR to `Accepted` (operator decision) and the
  loop will build it. Until then it stays propose-only.

---

## Related

- [`docs/adr/0114-one-cycle-per-session-orchestration.md`](../adr/0114-one-cycle-per-session-orchestration.md) — the WHY.
- [`docs/adr/0072-autonomous-orchestration-loop.md`](../adr/0072-autonomous-orchestration-loop.md) — superseded predecessor, kept as history.
- `.claude/skills/orchestrate/SKILL.md` — the executable operating contract.
- `.claude/skills/orchestrate/tick.sh` — scheduler entry point, one cycle per process.
- `.claude/workflows/wave.workflow.js` — the retained parallel fan-out.
- [`docs/backlog.md`](../backlog.md) — the canonical work register.
