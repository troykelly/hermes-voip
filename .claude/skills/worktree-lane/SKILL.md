---
name: worktree-lane
description: Create, work in, integrate, and clean up an isolated git worktree lane. ALL work happens in a lane — solo tasks and delegated subagents alike; the root checkout is never edited. Use at the start of any task that changes files.
---

# Worktree lane lifecycle

Worktree isolation is unconditional (`AGENTS.md`): each task gets one lane, one file
territory and one deliverable (a solo PR or a delegated commit SHA). The root checkout
stays a pristine `main` mirror. `.claude/hooks/enforce-worktree.mjs` blocks Edit/Write
there as defence in depth; it cannot see Bash writes, so do not route around it.

Ceremony inside the lane scales by R0–R3; use `docs/standards/engineering.md` Part A and
classify with `uv run python -m tools.classify`.

## 1. Create — always from current HEAD

```bash
LANE=<short-task-name>
git worktree add --detach ".worktrees/$LANE" HEAD
# Solo (PR-bound) lanes name their branch immediately:
git -C ".worktrees/$LANE" switch -c <type>/<topic>
```

- Use `--detach` from `HEAD`, never a branch name or older SHA — stale bases produce
  integration conflicts. Record the base SHA: `git rev-parse HEAD`.
- `.worktrees/` is gitignored; never place worktrees in `/tmp`.
- A standalone session launched inside `.worktrees/` cannot use the memory MCP: the root
  session owns the embedded store's single-process lock, and `.mcp.json` points to the
  root checkout's `.memory/`.

## 2. Work — only inside the lane, with absolute paths

- Every touched file lives under `.worktrees/$LANE/...`; never edit the root checkout.
- Install dependencies in the lane before testing. Lanes do not share `.venv`, but uv's
  global cache makes `uv sync` fast.
- Keep build output (`dist/`, etc.) worktree-local. Shared build caches can link stale
  sibling artefacts and fake a green run.
- Use Conventional Commits and the AI co-author trailer. Commit the failing test separately
  before implementation for behavioural R1 changes and all R2/R3 changes; R0 does not use
  test-first.
- `.git/hooks` is shared across every worktree, so a commit in any lane runs through
  whichever checkout's dependency path the hook manager pinned when it last installed —
  normally the root checkout's `.venv`. Keep the root checkout's dependencies installed so
  hooks keep working; CI remains the authoritative gate either way.

## 3. Deliver

- **Solo lane:** push and open the PR. Run the full local gate before the PR only for
  R2/R3; R0/R1 use CI. Carry the PR to landing under its class gates: agent merge on green
  for R0/R1, human approval for R2, dual control for R3.
- **Delegated lane:** report the final commit SHA and base SHA to the integrator. A lane
  without a commit produced nothing integrable.

## 4. Integrate delegated lanes — pick by pick, in the integrator's lane

```bash
git -C ".worktrees/$LANE" log --oneline <base-sha>..HEAD   # find ALL commits
git -C ".worktrees/<integrator-lane>" cherry-pick <sha>     # one at a time
```

- List `base..HEAD` first — lanes sometimes make more commits than they report.
- Cherry-pick individual commits, never multi-commit ranges.
- Re-verify from a clean build in the integrating lane. An agent's in-worktree green is
  not integration evidence.

## 5. Clean up — the lane dies with its work, and root catches up

```bash
git worktree remove --force ".worktrees/$LANE"
git worktree prune
# After a PR merge, the merging agent refreshes the root mirror (name the explicit
# remote/branch so it works even before upstream tracking is configured):
git fetch origin && git pull --ff-only origin main
```

Clean up as soon as the work is merged/integrated. Find orphaned worktrees or scratch dirs
idle for more than a few hours with `git worktree list`.
