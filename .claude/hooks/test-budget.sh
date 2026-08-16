#!/usr/bin/env bash
# estate-core v2026.08.1 — full-suite test budget (Claude Code PreToolUse:Bash hook).
# Fail-open: any error in this script must permit the command. Exit 2 = block.
set -u
BUDGET=2
command -v jq >/dev/null 2>&1 || exit 0
INPUT="$(cat 2>/dev/null || true)"; [ -n "$INPUT" ] || exit 0
CMD="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)" || exit 0
[ -n "$CMD" ] || exit 0
SID="$(printf '%s' "$INPUT" | jq -r '.session_id // "solo"' 2>/dev/null)" || SID=solo

# INSTALLER: replace with this repository's real bare full-suite command patterns.
# Bare suite invocations only — a targeted run (path/file/-k argument) must NOT match.
FULL_SUITE_RE='^[[:space:]]*(npm|pnpm|yarn)[[:space:]]+(run[[:space:]]+)?test[[:space:]]*$|^[[:space:]]*pytest[[:space:]]*$|^[[:space:]]*go[[:space:]]+test[[:space:]]+\./\.\.\.[[:space:]]*$|^[[:space:]]*cargo[[:space:]]+test[[:space:]]*$|^[[:space:]]*(npx|bunx|pnpm([[:space:]]+(exec|dlx))?|yarn[[:space:]]+exec)[[:space:]]+(vitest|jest)([[:space:]]+(--run|run))?[[:space:]]*$'

printf '%s' "$CMD" | grep -Eq "$FULL_SUITE_RE" || exit 0

D="${CLAUDE_PROJECT_DIR:-.}/.claude"; F="$D/.test-budget-$SID"
N="$(cat "$F" 2>/dev/null || echo 0)"; case "$N" in ''|*[!0-9]*) N=0;; esac
if [ "$N" -ge "$BUDGET" ]; then
  echo "estate-core: full-suite budget spent ($BUDGET/session). Run tests targeted at what you changed; CI runs the full suite at the PR gate." >&2
  exit 2
fi
{ mkdir -p "$D" && echo $((N+1)) > "$F"; } 2>/dev/null || true
exit 0
