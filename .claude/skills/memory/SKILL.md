---
name: memory
description: Store and recall persistent project memory via the local qdrant memory MCP (qdrant-store / qdrant-find). Recall at the start of non-trivial tasks; store non-obvious decisions, operator feedback, and hard-won lessons when you learn them.
---

# Project memory conventions

The `memory` MCP server (mcp-server-qdrant, configured in `.mcp.json`) provides two tools
backed by a local vector store under `.memory/`:

- `qdrant-find` — semantic search; phrase the query as a natural-language question.
- `qdrant-store` — persist one memory (`information` + optional `metadata` JSON).

## Recall

At the start of any non-trivial task, run one or two `qdrant-find` queries about the area
you're touching (e.g. "decisions about the SIP transport", "gotchas registering against the
UCM"). Do this before re-deriving anything a past session may have settled.

## Store

Store when you (a) make a non-trivial decision not worth a full ADR, (b) receive operator
feedback or a correction, (c) discover a gotcha that cost real time, or (d) finish a
milestone whose state a future session needs.

Entry format:

- `information`: 1–3 self-contained sentences, present tense, absolute dates (never
  "today"/"recently"). A future session sees only this text — include the why.
- `metadata`: `{"type": "project|feedback|user|reference", "topic": "<kebab-case>"}`.

## Do NOT store

- Secrets, tokens, keys — ever. The gateway host, extension number, device model and SIP
  password are sensitive; they live in the gitignored `.env` and the per-user agent memory,
  never in this store.
- Anything already canonical in the repo (AGENTS.md rules, docs/ figures, ADRs, code). Repo
  files are the source of truth; memory is for what the repo doesn't record.
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
