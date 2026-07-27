---
name: adr
description: Record an architecture decision as an ADR in docs/adr/. Required at R3, and at R2 only when the decision constrains future changes (technology choice, data model, protocol, security posture) — not for every design choice. Write it before or alongside the implementing commit.
---

# Writing an ADR

An ADR records a decision that constrains future changes — not every non-trivial choice.
Required unconditionally at R3 (credentials, publishing, licence/legal); at R2, only when
the decision itself will bind what future work can do. R0/R1 changes don't need one. See
`docs/standards/engineering.md` (the gate matrix, and Part G) for the full trigger; run
`uv run python -m tools.classify` if you're unsure of the class.

## Procedure

1. Find the next number: `ls docs/adr/` — files are `NNNN-kebab-title.md`, zero-padded to
   four digits. `0000-template.md` is reserved for the template.
2. Copy `docs/adr/0000-template.md` to `docs/adr/NNNN-<kebab-title>.md` as a starting point
   for Context, Decision, Consequences and Alternatives considered.
3. Status starts at `Accepted` (we record decisions when made, not proposals). If a later
   ADR reverses it, edit the old one's status to `Superseded by ADR-NNNN` in the same commit
   that adds the new one.
4. Commit the ADR with the work it justifies, or as its own `docs(adr):` commit if the
   decision precedes implementation.

## What a good ADR contains

- Present tense, factual, self-contained — a reader gets the full picture without the chat
  transcript that produced it. Numbers and names, not vibes: versions, benchmarks, prices,
  URLs.
- "Alternatives considered" names real alternatives and the specific reason each was
  rejected — "didn't fit" is not a reason.
- Nothing describing behaviour the repo doesn't have yet — that's aspirational
  documentation (`AGENTS.md`'s "no aspirational docs" invariant). Write the ADR when the
  decision is real, not ahead of it.
