# ADR-0114: One cycle per session — the scheduler owns continuity, not the context window

- **Date:** 2026-07-27
- **Status:** Accepted
- **Supersedes:** ADR-0072 (autonomous orchestration loop — never-stop, in-context continuation)
- **Deciders:** operator (`@troykelly`) + agent session (token-burn lane). Composes with the
  worktree-lane discipline, the blast-radius gate matrix (`docs/standards/engineering.md`
  Part A) and the `docs/backlog.md` work register.

## Context

ADR-0072 made "never stop" the prime directive: every wave ended by guaranteeing the next
one in the **same session**, via `ScheduleWakeup` in dynamic mode or a `*/10 * * * *` cron
firing `/orchestrate` back into the live session with preserved context. That achieved its
goal — the loop did not stop — but it bought continuity with the most expensive resource
available.

Fleet measurement over 7 days and 1,589 sessions:

| Signal | Measurement |
| --- | --- |
| `cache_read` share of true cost | 59.5% |
| Sessions over 250 turns | 4% of sessions, **66% of spend** |
| Cost at 16–40 turns | $0.47/session |
| Cost at 251–600 turns | $15.68/session |
| Cost above 600 turns | $40.70/session |
| Bash share of tool-result bytes | 74.6% (3,179 calls vs 165 `Read`) |
| Instruction-file share of payload | 0.4% |

The cost curve is superlinear because a turn re-sends the entire prefix. Cost is therefore
`prefix_size × turn_count`, and a design whose defining feature is *unbounded turn count in
one session* sits on the wrong side of that multiplication. The longest observed session
reached 4,027 turns — not an accident but a configured outcome of the re-injection pattern.

The important correction: ADR-0072's *goal* was right. Progress should not halt at a natural
break point, an empty-looking queue, or a "feels done" judgement. Only its *mechanism* was
wrong. Continuity does not require a living session; it requires durable state that a fresh
session can read.

## Decision

**Continuity moves out of the context window and into GitHub. One cycle per session.**

1. `/orchestrate` runs exactly **one** delivery cycle — orient, close, dispatch, discover,
   record, report — and then **exits**. Ending with work outstanding is the correct outcome,
   not a defect.
2. The skill may never self-continue: no `ScheduleWakeup`, no loop-back to an earlier
   section, no Stop hook returning `decision: "block"`.
3. `.claude/skills/orchestrate/tick.sh` is the scheduler's entry point. It takes a lock,
   runs one cycle in a **fresh `claude` process**, and exits. Cadence belongs to cron or a
   supervisor, never to the agent.
4. Cross-cycle state lives in a GitHub issue labelled `loop-state` (owner lease, heartbeat,
   `consecutiveDryCycles`, the issue-creation ledger) plus `gh`/`git`/CI themselves.
5. Termination is **computed, not felt**. `lib/done-gate.mjs` decides DONE from counts;
   `lib/partition.mjs` decides wave membership from lane rules. Both are pure, tested, and
   fail safe — an unparseable input reads as NOT done, never as finished. `K` is floored at
   2 consecutive dry cycles and cannot be argued down by a caller.
6. A **materiality floor** guards the dry-cycle counter: only a confirmed defect, security
   or privacy problem, data loss, missing designed feature, wrong document, or user-visible
   issue resets it. Without it a sufficiently pedantic reviewer always finds something and
   the loop can never converge — the mirror-image failure of never stopping.

Parallelism is retained in full. `.claude/workflows/wave.workflow.js` still fans out one
agent per dimension or per item; subagent transcripts never enter the orchestrator's
context, only their final structured message. Fan-out was never the cost problem — a long
single session was.

## Consequences

**Accepted:**
- A cycle that would have continued in-context now waits for the next tick, so latency
  between cycles rises to the cron period. Throughput within a cycle is unchanged.
- State must be written to GitHub before exit. A cycle killed mid-flight loses only the
  work it had not yet recorded; the next tick rebuilds from `gh` and `git`.
- Two schedulers pointed at one repo would race, so `tick.sh` holds a pid lock and the
  skill exits on seeing a live foreign lease.

**Retained from ADR-0072 (unchanged, and still binding):**
- The loop self-merges **R0/R1/R2** on green CI plus clean cross-vendor review, per the
  standing exception in `docs/standards/engineering.md` Part H. It **never** merges R3 and
  **never** pushes a release tag.
- Worktree lanes only; the root checkout is never edited.
- The memory MCP is orchestrator-only — it is single-process and subagents must not call it.
- Public-repo secret hygiene applies to every artefact the loop produces, including issue
  and PR bodies.

**Rejected alternatives:**
- *Keep the long session, shrink the instruction files.* Instruction files are 0.4% of
  payload. This cannot move a number driven by turn count.
- *Cap turns inside the session and summarise.* Summarisation still re-sends a growing
  prefix and silently discards evidence; a fresh process starting from GitHub does not.
- *Drop autonomy and require a human to start each cycle.* Rejected: the operator's
  requirement that progress continue without prompting is unchanged. The scheduler, not a
  human, supplies the heartbeat.

## References

- `.claude/skills/orchestrate/SKILL.md` — the executable contract (one cycle, then exit).
- `.claude/skills/orchestrate/tick.sh` — scheduler entry point, one cycle per process.
- `.claude/skills/orchestrate/lib/{partition,done-gate}.mjs` + `lib/test.mjs` — the pure
  gates and their 22 tests.
- `.claude/workflows/wave.workflow.js` — the retained fan-out (relocated from the skill dir).
- `orchestrate.config.json` — lanes, caps, commands and dimensions for this repo.
- `docs/runbooks/0016-orchestration-loop.md` — how to arm, schedule, observe and stop it.
- `.claude/hooks/prefer-native-tools.mjs` — the companion burn control on the Bash/Read ratio.
