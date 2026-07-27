#!/usr/bin/env bash
# One orchestration cycle, in a FRESH Claude Code session, then exit.
#
# This is the whole anti-burn mechanism. Continuity comes from GitHub, not from
# a context window. A re-injecting Stop hook keeps one session alive for
# thousands of turns and re-sends the entire transcript on every one of them;
# measured, that is 4% of sessions consuming 66% of spend.
#
# Run me from cron, a supervisor, or by hand. Every invocation is a clean slate.
#
#   ./.claude/skills/orchestrate/tick.sh            # one cycle
#   watch -n 900 ./.claude/skills/orchestrate/tick.sh
#   */15 * * * * cd /path/to/repo && ./.claude/skills/orchestrate/tick.sh >>.claude/loop/tick.log 2>&1

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

STATE_DIR="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.claude/loop"
mkdir -p "$STATE_DIR"
LOCK="$STATE_DIR/tick.lock"
MAX_SECONDS="${ORCH_MAX_SECONDS:-3600}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

[[ -f "$STATE_DIR/STOP" ]] && { log "STOP sentinel present — not ticking."; exit 0; }
[[ -f "$STATE_DIR/DONE" ]] && { log "DONE sentinel present — loop converged. Remove it to resume."; exit 0; }
[[ -f "$STATE_DIR/ACTIVE" ]] || { log "not armed (no ACTIVE sentinel) — nothing to do."; exit 0; }

# Single owner. Stale lock from a dead pid is reclaimed; a live one is respected.
if ! (set -o noclobber; printf '%s' "$$" >"$LOCK") 2>/dev/null; then
  holder="$(cat "$LOCK" 2>/dev/null || echo '')"
  if [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
    log "cycle already running (pid $holder) — exiting."; exit 0
  fi
  log "reclaiming stale lock from dead pid ${holder:-unknown}"
  rm -f "$LOCK"; (set -o noclobber; printf '%s' "$$" >"$LOCK") 2>/dev/null || { log "lock race lost"; exit 0; }
fi
trap 'rm -f "$LOCK"' EXIT

command -v claude >/dev/null || { log "claude CLI not on PATH"; exit 1; }

log "cycle start (max ${MAX_SECONDS}s)"
set +e
timeout --signal=TERM --kill-after=60 "$MAX_SECONDS" \
  claude -p "Run one /orchestrate cycle. Follow .claude/skills/orchestrate/SKILL.md exactly. Do exactly one cycle, then stop — do not loop, do not schedule yourself, do not continue after the cycle report." \
  ${ORCH_CLAUDE_ARGS:-}
rc=$?
set -e

case $rc in
  0)   log "cycle complete" ;;
  124) log "cycle hit the ${MAX_SECONDS}s ceiling and was terminated — state is in GitHub, next tick resumes" ;;
  *)   log "cycle exited rc=$rc — next tick resumes from GitHub" ;;
esac

# Never self-schedule. The scheduler owns cadence; this script owns one cycle.
exit 0
