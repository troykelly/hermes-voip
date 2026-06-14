#!/usr/bin/env bash
# Standardized devcontainer post-create (rendered by remoteclip init-devcontainer).
# Idempotent and re-run-safe — runs on every create/rebuild. Each step is
# independent and non-fatal: a failure is logged but never aborts the script.
# Installs the per-USER baseline tools (the system tools are baked into the
# image). pnpm-only — we never use npm.
set -uo pipefail

# CRLF self-heal (Windows checkouts can inject \r).
if head -1 "$0" | grep -q $'\r'; then sed -i 's/\r$//' "$0"; exec bash "$0" "$@"; fi

DEV_USER="${_REMOTE_USER:-${USER:-vscode}}"
DEV_HOME="$(eval echo "~${DEV_USER}")"
WORKSPACE_DIR="${CONTAINER_WORKSPACE_FOLDER:-$(pwd)}"

log() { printf '[post-create] %s\n' "$*"; }
step() { log "--- $1 ---"; }

export PNPM_HOME="${DEV_HOME}/.local/share/pnpm"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-${DEV_HOME}/.cache/ms-playwright}"
export PATH="${DEV_HOME}/.local/bin:${PNPM_HOME}:${PNPM_HOME}/bin:${PATH}"
mkdir -p "${DEV_HOME}/.local/bin" "${PNPM_HOME}/bin" "${PLAYWRIGHT_BROWSERS_PATH}"
# Belt-and-suspenders: the pnpm store is a named volume. The image pre-creates it
# vscode-owned, but an older root-owned volume from a prior build would EACCES the
# first `pnpm add -g`. Reclaim it (best-effort; no-op when already correct).
sudo chown -R "$(id -u):$(id -g)" "${PNPM_HOME}" 2>/dev/null || true

# Run fully non-interactively, even when invoked by hand from a TTY. pnpm's
# build-script approval prompt (and any installer prompt) reads from stdin; with
# our `>/dev/null` redirects the question is invisible, so on a TTY it blocks the
# script forever (observed: `pnpm add -g wrangler` hung indefinitely). Detaching
# stdin makes a manual `bash post-create.sh` behave like the devcontainer CLI run
# (which already has no TTY), so pnpm proceeds with its non-interactive defaults.
exec </dev/null

# --- pnpm (standalone installer; corepack was removed from Node 25+) ----------
step "pnpm"
if ! command -v pnpm >/dev/null 2>&1; then
  curl -fsSL https://get.pnpm.io/install.sh | env SHELL=bash PNPM_HOME="$PNPM_HOME" sh - \
    && log "pnpm $(pnpm --version 2>/dev/null || echo '?') installed" \
    || log "pnpm install FAILED (non-fatal)"
else
  log "pnpm present: $(pnpm --version 2>/dev/null)"
fi

# --- uv (+ uvx) ---------------------------------------------------------------
step "uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh \
    && log "uv installed" || log "uv install FAILED (non-fatal)"
else
  log "uv present: $(uv --version 2>/dev/null)"
fi

# --- Claude Code CLI (official installer -> ~/.local/bin/claude) ---------------
step "claude"
if command -v claude >/dev/null 2>&1; then
  claude update >/dev/null 2>&1 && log "claude up to date: $(claude --version 2>/dev/null)" \
    || log "claude present (self-update skipped): $(claude --version 2>/dev/null)"
else
  curl -fsSL https://claude.ai/install.sh | bash \
    && log "claude installed" || log "claude install FAILED (non-fatal)"
fi

# --- Cloudflare wrangler (pnpm global; never npm) -----------------------------
step "wrangler"
if ! command -v wrangler >/dev/null 2>&1; then
  pnpm add -g wrangler >/dev/null 2>&1 \
    && log "wrangler $(wrangler --version 2>/dev/null || echo '?') installed" \
    || log "wrangler install FAILED (non-fatal)"
else
  log "wrangler present: $(wrangler --version 2>/dev/null)"
fi

# --- Playwright + Puppeteer (arch-aware) --------------------------------------
# Puppeteer uses the system chromium (env baked into the image). Playwright uses
# its own Chromium engine (native on arm64) — `chromium` channel only. No
# --with-deps: the apt `chromium` package already provides the shared libs.
step "playwright + puppeteer"
pnpm add -g playwright @playwright/test puppeteer >/dev/null 2>&1 \
  && log "playwright + puppeteer (global) installed" \
  || log "pnpm add playwright/puppeteer FAILED (non-fatal)"
pnpm dlx playwright install chromium >/dev/null 2>&1 \
  && log "playwright chromium engine installed" \
  || log "playwright install chromium FAILED (non-fatal)"

# --- Claude Code container config (non-interactive) ---------------------------
step "claude config"
mkdir -p "${DEV_HOME}/.claude"
settings="${DEV_HOME}/.claude/settings.json"
desired='{"permissions":{"defaultMode":"bypassPermissions"}}'
if [ -f "$settings" ]; then
  tmp="$(mktemp)"
  jq -s '.[0] * .[1]' "$settings" <(printf '%s' "$desired") > "$tmp" 2>/dev/null && mv "$tmp" "$settings" || rm -f "$tmp"
else
  printf '%s\n' "$desired" | jq '.' > "$settings" 2>/dev/null || true
fi
prefs="${DEV_HOME}/.claude.json"
prefs_desired='{"hasCompletedOnboarding":true,"hasAcknowledgedCostThreshold":true}'
if [ -f "$prefs" ]; then
  tmp="$(mktemp)"
  jq -s '.[0] * .[1]' "$prefs" <(printf '%s' "$prefs_desired") > "$tmp" 2>/dev/null && mv "$tmp" "$prefs" || rm -f "$tmp"
else
  printf '%s\n' "$prefs_desired" | jq '.' > "$prefs" 2>/dev/null || true
fi

# --- git safe.directory -------------------------------------------------------
git config --global --add safe.directory "$WORKSPACE_DIR" 2>/dev/null || true

# --- Shell rc block: make the baseline tools resolvable in non-login shells ----
# remoteclip attaches via `docker exec /bin/zsh` (a non-login shell), so the
# tool PATHs must be exported from ~/.zshrc / ~/.bashrc, not just /etc/profile.
step "shell rc"
BEGIN_MARK="# >>> remoteclip-devcontainer >>>"
END_MARK="# <<< remoteclip-devcontainer <<<"
read -r -d '' RC_BLOCK <<'EOF' || true
# >>> remoteclip-devcontainer >>>
# Node (defense-in-depth: the node feature also wires nvm into the system rc
# files, but make the agent's non-login shell self-sufficient regardless).
export NVM_DIR="${NVM_DIR:-/usr/local/share/nvm}"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
case ":$PATH:" in *":$NVM_DIR/current/bin:"*) ;; *) [ -d "$NVM_DIR/current/bin" ] && export PATH="$NVM_DIR/current/bin:$PATH";; esac
export PNPM_HOME="$HOME/.local/share/pnpm"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH";; esac
case ":$PATH:" in *":$PNPM_HOME:"*) ;; *) export PATH="$PNPM_HOME:$PNPM_HOME/bin:$PATH";; esac
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
export PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright"
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  export GITHUB_TOKEN="$(gh auth token 2>/dev/null)"
fi
# <<< remoteclip-devcontainer <<<
EOF
for rc in "${DEV_HOME}/.zshrc" "${DEV_HOME}/.bashrc"; do
  [ -f "$rc" ] || touch "$rc"
  if grep -qF "$BEGIN_MARK" "$rc" 2>/dev/null; then
    sed -i "/$BEGIN_MARK/,/$END_MARK/d" "$rc"
  fi
  printf '%s\n' "$RC_BLOCK" >> "$rc"
done

log "post-create complete"
