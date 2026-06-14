# TypeScript / Node template head (remoteclip standardized devcontainer).
# Base ships Node 24; the `node` devcontainer feature layers Node 26 on top
# (see devcontainer.json). pnpm-only — npm is never used in our workflows.
# The shared baseline tooling layer is appended below at render time.
FROM mcr.microsoft.com/devcontainers/typescript-node:24-trixie
USER root
ARG DEBIAN_FRONTEND=noninteractive
# postgresql-client (psql / pg_isready) for the +timescale variant.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*
USER vscode

# ===========================================================================
# remoteclip standardized baseline tooling — IDENTICAL across every template.
# Appended verbatim after each language's Dockerfile.head. Installs the system-
# level, root-owned tools shared by all stacks; the per-user tools (pnpm,
# uv, claude, wrangler, Playwright/Puppeteer browsers) are installed by
# post-create.sh once the node feature has put Node 26 on PATH.
# ===========================================================================
USER root
ARG DEBIAN_FRONTEND=noninteractive
# BuildKit sets TARGETARCH (amd64/arm64). Fall back to dpkg if building without it.
ARG TARGETARCH

# Harden apt against the flaky Debian mirror. deb.debian.org / security.debian.org
# are fronted by a Fastly CDN; on plain :80 some transparent proxies/middleboxes
# reset the long ~74MB arm64 chromium download mid-stream ("Remote end closed
# connection"), and apt performs no retries by default, so the whole layer dies
# with exit 100. Retry every fetch, drop HTTP pipelining, and force the Debian
# mirrors to HTTPS (TLS/443 isn't subject to that middlebox inspection). The MCR
# base images already ship ca-certificates. Written to apt.conf.d so it persists.
RUN printf '%s\n' \
      'Acquire::Retries "8";' \
      'Acquire::http::Pipeline-Depth "0";' \
      'Acquire::http::Timeout "60";' \
      > /etc/apt/apt.conf.d/80-remoteclip-retries \
 && for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \
      [ -f "$f" ] && sed -i -E 's#http://(deb|security)\.debian\.org#https://\1.debian.org#g' "$f" || true; \
    done

# Standardize on the `vscode` user (uid 1000). The typescript-node base ships a
# `node` user instead of `vscode`; rename it so `USER vscode`, the `install -o
# vscode` below, devcontainer.json's remoteUser, and post-create.sh all line up.
# Idempotent: the python/rust bases already provide `vscode` and are left as-is.
RUN set -eux; \
    if ! id -u vscode >/dev/null 2>&1; then \
      groupmod -n vscode node; \
      usermod -l vscode -d /home/vscode -m node; \
      if [ -f /etc/sudoers.d/node ]; then \
        sed 's/\bnode\b/vscode/g' /etc/sudoers.d/node > /etc/sudoers.d/vscode; \
        rm -f /etc/sudoers.d/node; \
        chmod 0440 /etc/sudoers.d/vscode; \
      fi; \
    fi

# System packages + system Chromium. Installing the `chromium` apt package pulls
# in exactly the shared libraries Playwright's own bundled Chromium needs too, so
# we get a working browser stack on BOTH amd64 and arm64 WITHOUT Playwright's
# `--with-deps` (whose Debian-13/trixie detection is currently buggy) and without
# hand-maintaining the t64 library list.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl wget gnupg jq git unzip xz-utils \
      chromium \
      libxshmfence1 libdrm2 \
      fonts-liberation fonts-noto-color-emoji fonts-unifont \
 && rm -rf /var/lib/apt/lists/*

# 1Password CLI (op) via the official apt repo (the community devcontainer
# feature is unmaintained since 2023). Includes the debsig verification policy.
RUN install -d -m 0755 /usr/share/keyrings \
 && curl -fsSL https://downloads.1password.com/linux/keys/1password.asc \
      | gpg --dearmor -o /usr/share/keyrings/1password-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/$(dpkg --print-architecture) stable main" \
      > /etc/apt/sources.list.d/1password.list \
 && install -d -m 0755 /etc/debsig/policies/AC2D62742012EA22 /usr/share/debsig/keyrings/AC2D62742012EA22 \
 && curl -fsSL https://downloads.1password.com/linux/debian/debsig/1password.pol \
      -o /etc/debsig/policies/AC2D62742012EA22/1password.pol \
 && curl -fsSL https://downloads.1password.com/linux/keys/1password.asc \
      | gpg --dearmor -o /usr/share/debsig/keyrings/AC2D62742012EA22/debsig.gpg \
 && apt-get update \
 && apt-get install -y --no-install-recommends 1password-cli \
 && rm -rf /var/lib/apt/lists/*

# ripgrep — current release binary (Debian apt lags several majors behind).
ARG RIPGREP_VERSION=15.1.0
RUN set -eux; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64|x86_64)  rg_arch=x86_64-unknown-linux-musl ;; \
      arm64|aarch64) rg_arch=aarch64-unknown-linux-gnu ;; \
      *) echo "unsupported arch for ripgrep: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/BurntSushi/ripgrep/releases/download/${RIPGREP_VERSION}/ripgrep-${RIPGREP_VERSION}-${rg_arch}.tar.gz" -o /tmp/rg.tgz; \
    tar -xzf /tmp/rg.tgz -C /tmp; \
    install -m 0755 "/tmp/ripgrep-${RIPGREP_VERSION}-${rg_arch}/rg" /usr/local/bin/rg; \
    rm -rf /tmp/rg.tgz "/tmp/ripgrep-${RIPGREP_VERSION}-${rg_arch}"

# OpenAI Codex CLI — prebuilt static musl binary (no Node/npm dependency).
ARG CODEX_VERSION=0.137.0
RUN set -eux; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64|x86_64)  cx_arch=x86_64-unknown-linux-musl ;; \
      arm64|aarch64) cx_arch=aarch64-unknown-linux-musl ;; \
      *) echo "unsupported arch for codex: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${cx_arch}.tar.gz" -o /tmp/codex.tgz; \
    tar -xzf /tmp/codex.tgz -C /tmp; \
    install -m 0755 "/tmp/codex-${cx_arch}" /usr/local/bin/codex; \
    rm -f /tmp/codex.tgz "/tmp/codex-${cx_arch}"

# Browser wiring for Playwright + Puppeteer. There is still NO native arm64
# Chrome-for-Testing build, so Puppeteer always uses the system chromium; on
# arm64 Playwright uses its own (native) Chromium via the `chromium` channel.
ENV PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    PLAYWRIGHT_BROWSERS_PATH=/home/vscode/.cache/ms-playwright

# Pre-create the vscode-owned dirs that named volumes / per-user installs land
# in. When a fresh named volume mounts onto a path that already exists in the
# image, Docker copies that path's ownership into the empty volume — so the pnpm
# store comes up vscode-owned instead of root-owned (which would EACCES the
# first `pnpm add -g` in post-create.sh).
# NOTE: `install -d` applies -o/-g ONLY to the dirs named as operands; any leading
# parent it auto-creates (.local, .local/share, .cache) is left root-owned, which
# then EACCESes per-user installers that mkdir a sibling there — e.g. the claude
# installer's ~/.local/share/claude. So enumerate every parent explicitly.
RUN install -d -o vscode -g vscode -m 0755 \
      /home/vscode/.local \
      /home/vscode/.local/bin \
      /home/vscode/.local/share \
      /home/vscode/.local/share/pnpm \
      /home/vscode/.local/share/pnpm/store \
      /home/vscode/.cache \
      /home/vscode/.cache/ms-playwright

USER vscode
