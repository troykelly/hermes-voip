---
name: memory
description: Store and recall persistent project memory via the local qdrant memory MCP (qdrant-store / qdrant-find). Recall when it would change what you do; store durable decisions, operator feedback, and hard-won lessons as you learn them.
---

# Project memory conventions

The `memory` MCP server (mcp-server-qdrant, configured in `.mcp.json`) provides two tools
backed by a local vector store under `.memory/`:

- `qdrant-find` — semantic search; phrase the query as a natural-language question.
- `qdrant-store` — persist one memory (`information` + optional `metadata` JSON).

## Recall

Run `qdrant-find` when recall would actually change what you do — e.g. before touching an
area with a history of decisions or gotchas ("decisions about the SIP transport", "gotchas
registering against the SIP gateway"). Skip it when nothing here is likely to be new.

## Store

Store durable findings at task end, not as a running trail: a non-trivial decision not
worth a full ADR, operator feedback or a correction, a gotcha that cost real time, or
milestone state a future session needs.

Entry format:

- `information`: 1–3 self-contained sentences, present tense, absolute dates (never
  "today"/"recently"). A future session sees only this text — include the why.
- `metadata`: `{"type": "project|feedback|user|reference", "topic": "<kebab-case>"}`.

## Do NOT store

- Secrets, tokens, keys — ever. The gateway host, extension number, device model and SIP
  password are sensitive; they live in the gitignored `.env` and the per-user agent memory,
  never in this store.
- Anything already canonical in the repo (`AGENTS.md`, `docs/standards/engineering.md`,
  docs/ figures, ADRs, code). Repo files are the source of truth; memory is for what the
  repo doesn't record.
- Conversation-local trivia with no future value.

## Operational notes

- Local after first run: embedded Qdrant DB + ONNX embedding model under `.memory/`
  (gitignored). The embedding model downloads from the HuggingFace Hub on first use only,
  then runs fully offline — no further network calls; nothing leaves the machine.
- Single-process lock: only one session per repo clone can use the store at a time. A
  second concurrent session's memory server fails to connect — that is the lock, not a
  corruption.
- If a memory turns out to be wrong, store a correcting entry stating both the old claim and
  the correction (the store has no delete tool).
