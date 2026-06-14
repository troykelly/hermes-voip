# hermes-voip

A [Hermes](https://hermes-agent.nousresearch.com/) plugin that gives a Hermes agent **full
two-way voice** over telephony. It registers as an extension on any RFC-compliant
**SIP-over-TLS** or **WebRTC** voice gateway, bridges live call audio through a
speech-to-text → agent → text-to-speech loop, and speaks back to the caller.

> **Status: bootstrapping.** This repository currently holds the engineering-governance and
> tooling framework plus a minimal Python package skeleton. The SIP/WebRTC client, the media
> path, the conversational provider, and how/where the plugin runs are open questions —
> decided on the record in [`docs/adr/`](docs/adr/) in later sessions, **not assumed here**.

## What this is

A **Python package** (a Hermes plugin) — not a standalone service. It is loaded and run by
the Hermes runtime, so this repository makes **no hosting or platform assumptions**. Gateway
connection details (host, extension, password) are configuration, supplied via `HERMES_SIP_*`
environment variables and never committed — the repo is public.

## Development

Standardized devcontainer. Toolchain standards: [`docs/stack.md`](docs/stack.md). Working
rules every change follows: [`AGENTS.md`](AGENTS.md).

```bash
uv sync                  # install (CI: uv sync --frozen)
uv run ruff format .     # format        (check: uv run ruff format --check .)
uv run ruff check .      # lint
uv run mypy              # strict type-check
uv run pytest            # tests
```

- **Language/runtime:** Python ≥ 3.13, managed with **uv**. **Typing:** mypy strict, no
  escape hatches. **Lint/format:** ruff.
- **Secrets:** 1Password + a gitignored `.env`.

## Security

This repository is **public**. Never commit the gateway host, extension number, passwords,
internal hostnames, IPs, or any PII — they live only in the gitignored `.env` and 1Password.
Secret scanning (gitleaks) and a dependency vulnerability audit run in CI.

## Licence

Not yet specified (operator to choose).
