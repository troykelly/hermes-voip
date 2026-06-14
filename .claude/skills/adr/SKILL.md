---
name: adr
description: Record an architecture decision as an ADR in docs/adr/. Use whenever a non-trivial design or tooling decision is made (technology choice, data model, protocol, security posture) — before or alongside the implementing commit.
---

# Writing an ADR

Implements AGENTS.md rule 30: non-trivial decisions are recorded, not implied by code.

## Procedure

1. Find the next number: `ls docs/adr/` — files are `NNNN-kebab-title.md`, zero-padded to
   four digits. `0000-template.md` is reserved for the template.
2. Copy `docs/adr/0000-template.md` to `docs/adr/NNNN-<kebab-title>.md`.
3. Fill every section. "Alternatives considered" must name real alternatives and the
   specific reason each was rejected — "didn't fit" is not a reason.
4. Status starts at `Accepted` (we record decisions when made, not proposals). If a later
   ADR reverses it, edit the old one's status to `Superseded by ADR-NNNN` in the same commit
   that adds the new one.
5. Commit the ADR with the work it justifies, or as its own `docs(adr):` commit if the
   decision precedes implementation.

## Quality bar

- Present tense, factual, self-contained — a reader gets the full picture without the chat
  transcript that produced it.
- Numbers and names, not vibes: versions, benchmarks, prices, URLs.
- An ADR that describes behaviour the repo doesn't have yet is aspirational documentation
  (rule 27) — write it when the decision is real.
