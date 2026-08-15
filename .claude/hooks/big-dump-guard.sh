#!/usr/bin/env bash
# estate-core v2026.08 — big-dump guard (Claude Code PreToolUse:Bash hook).
# Blocks exactly one pattern: a bare `cat` of one file over LIMIT_BYTES with no
# pipe or redirect. Everything subtler is handled by output caps and context
# rules, not hooks. Fail-open: any error here must permit the command. Exit 2 = block.
set -u
LIMIT_BYTES=262144
command -v jq >/dev/null 2>&1 || exit 0
INPUT="$(cat 2>/dev/null || true)"; [ -n "$INPUT" ] || exit 0
CMD="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)" || exit 0
[ -n "$CMD" ] || exit 0
case "$CMD" in (*'|'*|*'>'*) exit 0 ;; esac   # piped/redirected output is bounded by its consumer
FILE="$(printf '%s' "$CMD" | sed -n -E 's/^[[:space:]]*cat[[:space:]]+(-[[:alnum:]]+[[:space:]]+)*([^[:space:]]+)[[:space:]]*$/\2/p')"
[ -n "$FILE" ] || exit 0
FILE="${FILE%\"}"; FILE="${FILE#\"}"; FILE="${FILE%\'}"; FILE="${FILE#\'}"
[ -f "$FILE" ] || exit 0
SZ="$(stat -c %s "$FILE" 2>/dev/null || stat -f %z "$FILE" 2>/dev/null || echo 0)"
[ "$SZ" -gt "$LIMIT_BYTES" ] 2>/dev/null || exit 0
echo "estate-core: $FILE is $((SZ/1024)) KiB — a full dump into context is almost never needed. Use rg -n '<pattern>' $FILE, or sed -n '<start>,<end>p' $FILE, or head -c 4000 $FILE. (Bare cat blocked above $((LIMIT_BYTES/1024)) KiB.)" >&2
exit 2
