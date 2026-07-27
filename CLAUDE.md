@AGENTS.md

`AGENTS.md` is canonical; `docs/standards/engineering.md` is the full contract. This file
adds only Claude-Code specifics.

- **Every task works in a lane.** `git worktree add --detach .worktrees/<lane> HEAD`, then
  absolute paths inside it — solo work and delegated subagents alike. The `worktree-lane`
  skill has the lifecycle; a PreToolUse hook blocks root-checkout edits (Edit/Write only —
  it cannot see Bash writes, so do not route around it).
- **Skills:** `adr` when a decision constrains future changes · `worktree-lane` when
  starting or integrating a lane · `memory` for recall/store · `graphify` for codebase
  questions when `graphify-out/graph.json` exists · `orchestrate` runs exactly ONE delivery
  cycle and then exits — never loop back, never `ScheduleWakeup` yourself (ADR-0114); the
  scheduler re-runs it in a fresh session via `.claude/skills/orchestrate/tick.sh`.
- **Subagents** may read anything and write only inside their own lane. They return
  conclusions, never transcripts or file dumps. Give each an independent file territory;
  two agents in one lane corrupt the branch. Route by cost: mechanical sweeps → Haiku 4.5,
  implementation and focused review → Sonnet 5, architecture and adjudication → Opus 5.
- **Memory MCP** (`memory`, local qdrant, data in the gitignored `.memory/`): recall with
  `qdrant-find` before non-trivial work, store what a future session needs. Never store the
  gateway host, extension or password. The store is single-process — one session per clone.
- **graphify:** for codebase questions prefer `graphify query "<q>"` / `explain` / `path`
  over broad greps; run `graphify update .` after changing code. It indexes source, not
  prose — read docs and instruction files directly.
- `.memory/`, `.env` and `.worktrees/` are gitignored. Never commit them.
