@AGENTS.md

# hermes-voip

A plugin for [Hermes](https://hermes-agent.nousresearch.com/) that gives a Hermes agent
full two-way voice communication over telephony, by registering as an extension on **any
RFC-compliant SIP-over-TLS or WebRTC voice gateway**. The first **test** target is a
specific gateway recorded only in the gitignored `.env` / 1Password — not a design
assumption; the plugin must stay gateway-agnostic (no vendor-specific quirks in the core). The conversational media path
(STT ↔ agent ↔ TTS) and the exact runtime transport are open design questions resolved on
the record in `docs/adr/` before implementation. Owned by the operator (`troy@…`); built
and operated by agent sessions under the rules in `AGENTS.md`.

## Repository map

| Path             | What it is                                                       |
| ---------------- | --------------------------------------------------------------- |
| `src/hermes_voip/` | The Hermes VoIP plugin (Python package)                        |
| `tests/`           | Tests (`pytest`, TDD per AGENTS.md rule 18)                    |
| `docs/`          | Canonical strategy docs, platform standards, ADRs, runbooks      |
| `.devcontainer/` | Standardized devcontainer (operator-owned baseline tooling)     |
| `.claude/`       | Committed settings, hooks, and skills                            |
| `.memory/`       | Local memory MCP database — gitignored, never commit            |

Each app/area gets its own `CLAUDE.md` as it grows (read it before working there).
Commands (root): `uv run ruff format --check .` · `uv run ruff check .` · `uv run mypy` ·
`uv run pytest`. Versions are pinned (`pyproject.toml`, `uv.lock`, `.python-version`);
platform constraints are AGENTS.md rules 38–42 with detail in `docs/stack.md`.

## Invariants every change must respect

- **The repo is PUBLIC.** Never put the SIP host, extension number, internal hostnames,
  IPs, URLs, device names, tokens, or any PII into a tracked file — code, comments, tests,
  fixtures, docs, commit messages, or CI logs. Connection details live ONLY in the
  gitignored `.env` (and 1Password / per-user agent memory). Code reads them from
  `HERMES_SIP_*` env vars; tests use obvious fakes (`pbx.example.test`, ext `1000`).
- **Fully-typed Python, no escape hatches** (AGENTS.md rules 39, 17): every symbol annotated
  and clean under `mypy --strict`; no `Any`, no `# type: ignore` without a justification
  comment, no type-laundering `cast`. Errors propagate, never swallowed (rule 37).

## Persistent memory (MCP)

A fully-local vector-RAG memory server (`memory`, mcp-server-qdrant) is configured in
`.mcp.json`; data lives under `.memory/`. Use `qdrant-find` at the start of non-trivial
tasks to recall prior decisions; `qdrant-store` what future sessions need. Never store
secrets there (the SIP host/extension/password are sensitive — keep them out of the store).
Conventions: the `memory` skill. The store is single-process — one session per repo clone
at a time.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
