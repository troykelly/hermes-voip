---
name: worktree-lane
description: Create, work in, integrate, and clean up an isolated git worktree lane. ALL work happens in a lane — solo tasks and delegated subagents alike; the root checkout is never edited. Use at the start of any task that changes files.
---

# Worktree lane lifecycle

Implements AGENTS.md rules 8–13. A "lane" is one unit of work: one worktree, one file
territory, one deliverable (a PR for solo work; a returned commit SHA for delegated work).
The root checkout is a pristine mirror of `main` — never edit or commit there. A PreToolUse
hook (`.claude/hooks/enforce-worktree.mjs`) blocks Edit/Write against the root checkout as
defence in depth.

## 1. Create — always from current HEAD

```bash
LANE=<short-task-name>
git worktree add --detach ".worktrees/$LANE" HEAD
# Solo (PR-bound) lanes name their branch immediately:
git -C ".worktrees/$LANE" switch -c <type>/<topic>
```

- `--detach` from `HEAD`, never from a branch name or older SHA — stale bases produce
  conflicts at integration. Record the base SHA: `git rev-parse HEAD`.
- `.worktrees/` is gitignored; never place worktrees in `/tmp`.
- Do not launch a standalone session from inside `.worktrees/` expecting the memory MCP —
  the embedded store's single-process lock belongs to the root session, and `.mcp.json`
  points at the root checkout's `.memory/`.

## 2. Work — only inside the lane, absolute paths

- Every file the lane touches lives under `.worktrees/$LANE/...`. Never edit the root
  checkout from a lane.
- Install dependencies in the lane before testing — lanes do not share the `.venv`. (uv's
  global cache is shared, so `uv sync` in a lane is fast.)
- Build output stays worktree-local (`dist/` etc. inside the lane). Do not share build
  caches across lanes — a shared cache can link a sibling's stale artifacts and fake a
  green run.
- Commit in the lane with Conventional Commits + the AI co-author trailer. Failing tests
  are committed on their own before the implementation commit.
- Git hooks caveat: `.git/hooks` is shared across all worktrees; some hook managers pin the
  dependency path of whichever checkout installed last. Keep the root checkout's
  dependencies installed so hooks keep working; CI is the authoritative gate either way.

## 3. Deliver

- **Solo lane:** push the branch, open the PR, and own it to merge (rule 14).
- **Delegated lane:** report the final commit SHA (and base SHA) to the integrator. A lane
  that didn't commit produced nothing integrable.

## 4. Integrate delegated lanes — pick by pick, in the integrator's lane

```bash
git -C ".worktrees/$LANE" log --oneline <base-sha>..HEAD   # find ALL commits
git -C ".worktrees/<integrator-lane>" cherry-pick <sha>     # one at a time
```

- List `base..HEAD` first — lanes sometimes make more commits than they report.
- Individual single-commit picks, not multi-commit ranges.
- After integrating, re-verify from a clean build in the integrating lane. An agent's
  in-worktree green is not integration evidence.

## 5. Clean up — the lane dies with its work, and root catches up

```bash
git worktree remove --force ".worktrees/$LANE"
git worktree prune
# After a PR merge, the merging agent refreshes the root mirror (name the explicit
# remote/branch so it works even before upstream tracking is configured):
git fetch origin && git pull --ff-only origin main
```

Run cleanup as soon as the lane's work is merged/integrated. Sweep any orphaned worktrees
or scratch dirs idle for more than a few hours: `git worktree list`.
