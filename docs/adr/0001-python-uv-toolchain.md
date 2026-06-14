# ADR-0001: Python + uv as the language and toolchain; architecture deferred

- **Date:** 2026-06-14
- **Status:** Accepted
- **Deciders:** operator direction; agent session (bootstrap)

## Context

The governance bootstrap needs a concrete language/runtime and toolchain so the CI gate and
local gate are real (AGENTS.md rules 38–39). Two facts bound the choice: Hermes plugins are
typically **Python** (operator), and the devcontainer ships Python 3.13 plus the `uv`
toolchain (and the `op` 1Password CLI). An earlier draft mis-inferred TypeScript/Node and a
Cloudflare hosting platform from devcontainer tooling (`wrangler`); the operator corrected
both. Defaulting architecture from "what happens to be installed" is exactly the failure mode
this ADR rejects.

## Decision

The implementation language is **Python (>= 3.13)**, managed end-to-end with **uv**:

- Dependencies in `pyproject.toml`, locked in `uv.lock`; CI installs with `uv sync --frozen`.
- **ruff** for format + lint (strong curated rule set), **mypy --strict** for typing (no
  escape hatches), **pytest** for tests, **hatchling** build backend with a `src/` layout.
- Local gate via **pre-commit** (`repo: local` hooks calling `uv run`); CI mirrors it.

Explicitly **NOT decided here** — deferred to later ADRs, when the work is actually designed:

- **Hosting / runtime location** — the plugin is a package loaded by the Hermes runtime; this
  repo assumes no cloud or platform (rule 40).
- **SIP-over-TLS / WebRTC media transport** — how the plugin holds the persistent
  registration and real-time media.
- **Conversational provider** — the STT ↔ agent ↔ TTS path (e.g. an external speech service).
- **Gateway specifics** — the plugin targets any RFC-compliant gateway; the specific test
  gateway is recorded only in the gitignored `.env` / 1Password, never in a tracked file.

## Consequences

- The gate (ruff/mypy/pytest) and CI are concrete and green from day one.
- No platform/vendor lock-in is introduced; the architecture stays open and is decided on the
  record when researched, not guessed from the environment.
- Reversing the language would be expensive, but it is operator-confirmed, not inferred.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| TypeScript / Node (the devcontainer's nominal image) | Hermes plugins are typically Python; the TS inference came from the devcontainer label, not the project. Operator-corrected. |
| Cloudflare as a hosting platform | The plugin is not a deployed service; it runs inside the Hermes runtime. Inferring a cloud from `wrangler` being installed was the over-assumption to avoid (rule 40). |
| poetry / pip-tools / pdm instead of uv | uv is already in the image, is fast, and unifies venv + lock + tool-runner; no second tool needed. |
| black + flake8 instead of ruff | ruff replaces both with one fast tool and a far larger rule set. |
