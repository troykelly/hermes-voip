#!/usr/bin/env bash
# Standardized devcontainer post-start (rendered by remoteclip init-devcontainer).
# Runs on every container start — must stay idempotent and fast.
set -uo pipefail

if head -1 "$0" | grep -q $'\r'; then sed -i 's/\r$//' "$0"; exec bash "$0" "$@"; fi

WORKSPACE_DIR="${CONTAINER_WORKSPACE_FOLDER:-$(pwd)}"

# Refresh git safe.directory (needed after worktree additions / container restarts).
git config --global --add safe.directory "$WORKSPACE_DIR" 2>/dev/null || true

# If this stack has a database, give it a moment to accept connections so the
# first command an agent runs doesn't race a cold Postgres. Best-effort only.
if command -v pg_isready >/dev/null 2>&1; then
  for _ in $(seq 1 30); do
    pg_isready -h timescale -q 2>/dev/null && break
    sleep 1
  done
fi

exit 0
