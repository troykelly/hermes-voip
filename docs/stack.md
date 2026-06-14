# Platform & toolchain standards

Binding standards for this repository (AGENTS.md rules 38–42). Every entry is verified
against a primary source; cite it and date the verification.

- **Language/runtime:** Python (>= 3.13, pinned in `.python-version`). Full type annotations,
  checked with `mypy --strict` (`disallow_any_explicit`); no escape hatches (rule 17).
- **Package/project manager:** `uv` only — never bare `pip`/`poetry`/`conda`. Dependencies in
  `pyproject.toml`, fully locked in the committed `uv.lock`. Frozen install: `uv sync
  --frozen`. Dev tools are exact-pinned (rule 33).
- **Lint & format:** `ruff` (format + a strong curated lint rule set — see `pyproject.toml`).
- **Tests:** `pytest`, TDD (rule 18). **Build backend:** `hatchling`, `src/` layout
  (`src/hermes_voip`). **Local gate:** `pre-commit` (`repo: local` hooks calling `uv run`).
- **Hosting / deployment:** NONE assumed. This repo is a Python package (a Hermes plugin)
  loaded by the Hermes runtime; it is not a deployed service and pins no cloud/platform. The
  questions of where/how the plugin runs, the SIP-over-TLS/WebRTC media transport, and the
  STT/TTS conversational provider are deliberately deferred to future ADRs (rule 40) — never
  defaulted from devcontainer tooling.
- **Secret manager:** 1Password. The `op` CLI is baked into the devcontainer image and an
  `OP_SERVICE_ACCOUNT_TOKEN` is provided. Gateway/SIP credentials live ONLY in the gitignored
  `.env` (keys `HERMES_SIP_*`) and 1Password — never in a tracked file (the repo is public).
- **CI (GitHub Actions):** `gate` (ruff format check / ruff lint / mypy / pytest),
  `supply-chain` (`pip-audit` + a production-deps licence allowlist, on dependency changes),
  `gitleaks` (pinned, checksum-verified binary). All tooling is free/OSS (rule 36).

Verified against primary sources (devcontainer config, installed tool versions): 2026-06-14.
