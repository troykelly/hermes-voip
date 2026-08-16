#!/usr/bin/env bash
# estate-core v2026.08.2 — full-suite test budget (hook rev 2, atomic counter)
# (Claude Code PreToolUse:Bash hook).
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

# Atomic claim: each concurrent invocation races to mkdir a numbered slot dir
# (mkdir is atomic on a local filesystem — exactly one caller wins each slot,
# unlike a read-modify-write counter). Claiming any slot 1..BUDGET permits the
# run; failing to claim any of them blocks it. No shared counter file, no race.
D="${CLAUDE_PROJECT_DIR:-.}/.claude"
mkdir -p "$D" 2>/dev/null || exit 0
[ -w "$D" ] || exit 0

CLAIMED=0
i=1
while [ "$i" -le "$BUDGET" ]; do
  if mkdir "$D/.test-budget-$SID.slot$i" 2>/dev/null; then
    CLAIMED=1
    break
  fi
  i=$((i+1))
done

[ "$CLAIMED" -eq 1 ] && exit 0

echo "estate-core: full-suite budget spent ($BUDGET/session). Run tests targeted at what you changed; CI runs the full suite at the PR gate." >&2
exit 2
