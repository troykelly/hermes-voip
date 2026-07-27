# Engineering standard

The full contract. `AGENTS.md` is the router and carries the invariants you need loaded at
all times; this document carries everything you look up when you are about to do something.

**Gates scale with blast radius, not with change count.** Safety controls are
unconditional — they are nearly free per change because CI and platform config enforce
them, not your labour. Ceremony scales down hard.

---

## Part A — the gate matrix

This table is the entire process contract. `green` means CI reported success on the head
commit.

| Gate | R0 | R1 | R2 | R3 |
| --- | --- | --- | --- | --- |
| Governing issue | no | reference an existing issue, backlog item or the user request | required | required |
| Plan document | no | no | only if multi-stage | required |
| Failing test first | no | behavioural changes only | required | required |
| Tests while working | none | focused test file only | focused test file only | focused test file only |
| Full local gate | CI only | CI only | before PR, and CI | before PR, and CI |
| Independent review | no | diff-only, single pass | scoped context, cross-vendor | scoped, cross-vendor, + specialist |
| Human review | no | no | required | specialist + dual control |
| Docs updated | no | only if the change makes existing docs wrong | required for behaviour change | required |
| ADR | no | no | only if it constrains future changes | required |
| Runbook | no | no | required when it provisions or changes a real resource | required |
| Merge | auto on green | agent merges on green | human approval | dual control + explicit approval |
| Batching | many per PR | related changes per PR | one logical change | one logical change |

The full local gate is one line:

```sh
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest
```

**Independent review, concretely.** R1 is a single diff-only pass:

```sh
git diff main...HEAD | codex exec "Review this diff for correctness, security and spec defects only."
```

Use the three-dot form. A two-dot diff on a branch that is behind `main` shows main's own
advances as if they were your changes and produces false blocks. If no cross-vendor
reviewer is available in the environment, say "independent review not performed" — never
let your own re-read stand in for it.

R2 and R3 add: the reviewer sees the diff, the modified files, and only the policy
documents matching the touched surface (media/transport → the relevant ADR; config or
env-var surface → `docs/stack.md` and both `plugin.yaml` copies; CI → this document). Never
the author's session history.

---

## Part B — classification is mechanical

`uv run python -m tools.classify` reads the diff against the merge base and prints the
class and its gates. It is the authority: you do not argue with it and you cannot lower it.
The path map lives in the script.

- **Every apply-trigger path is R2 minimum**, regardless of how the diff looks. A tag
  matching `v*.*.*` publishes to PyPI and cuts a public GitHub Release, so `pyproject.toml`
  (which carries the version), `uv.lock`, `packaging/**` and `.github/workflows/publish.yml`
  carry the blast radius of that publish, not of their own diff.
- `tools/classify.py`, `AGENTS.md`, `docs/standards/engineering.md`, `.github/workflows/**`
  and `.claude/**` are **R2 by definition** and appear explicitly in the map, so a change
  cannot lower its own class by editing the classifier. `.github/CODEOWNERS` names the
  operator on all of them. Note that CODEOWNERS is advisory here: this repository has no
  branch protection, so nothing mechanically blocks a merge without the owner's review.
- Credential, secret-handling and licence surfaces are R3.
- Default when nothing matches: **R1**.
- **R0 is content-decided, not path-decided.** The classifier emits R0 only when the diff
  touches nothing but an explicit allowlist of non-executable files (`docs/backlog.md`,
  `CHANGELOG.md`, `docs/plan/**`) or nothing but comment and blank-line changes in files
  that are otherwise R1. Anything it cannot prove non-executable is R1 or higher.

---

## Part C — safety invariants

Unconditional and class-independent. Each names where it is actually enforced. Where a
control is stated as convention only, that is said plainly rather than implied.

**Secrets and identity**

- The gateway host, extension, password, device/model names and any PII never enter a
  tracked file. *Enforced:* `gitleaks` (CI, full history) · `tests/test_no_vendor_identifiers.py`
  scans every tracked file · `tests/test_plugin_manifest.py` asserts the manifests use only
  fake examples and leak no secret values.
- `.env` is gitignored and read-denied; secrets live in 1Password. *Enforced:* `.gitignore`,
  `.claude/settings.json` deny rules, root `conftest.py` (keeps pytest from reading it).
  *Convention only:* nothing stops `printenv`.
- Credential env vars are marked `secret: true` in both `plugin.yaml` copies. *Enforced:*
  `tests/test_plugin_manifest.py`, `tests/test_backlog_wave10_checkoff.py`.
- No default-credential or publicly-bound database configuration, in code or in prose.
  *Enforced:* `tools/check_database_exposure.py` at pre-commit and in CI.

**Data and config**

- The env-var registry is consistent across `src/hermes_voip/plugin.yaml`,
  `packaging/hermes-plugins/hermes-voip/plugin.yaml` and `_KNOWN_ENV_KEYS` in `plugin.py`.
  *Enforced:* `tests/test_register.py::test_known_env_keys_matches_manifest` and
  `tests/test_plugin_manifest.py` — in the full suite only.
- Documented env-var names and values match what the code actually reads. *Enforced:*
  the canonical-name tests in `tests/test_runbook_0013_drift.py` and
  `tests/test_runbook_rule27_fixes.py`.

**Agent boundaries**

- An agent works only in its own lane and never edits the root checkout. *Enforced:*
  `.claude/hooks/enforce-worktree.mjs` (hard block, exit 2). *Gap:* the hook sees
  Edit/Write/NotebookEdit only — a Bash write into the root checkout is unenforced. Do not
  route around it.
- An agent may not grant itself permissions, widen credentials, or edit the policy that
  governs it without that change being classified R2 and owner-reviewed. *Enforced:* the
  classifier path map + CODEOWNERS (advisory — see Part B).
- Tool and gateway output is untrusted until validated; a caller-supplied SIP field is
  attacker-controlled. *Enforced:* the injection-guard and caller-identity tests.

**Supply chain**

- Dependencies are locked and installed frozen (`uv sync --frozen`); `uv` is the only
  package manager. *Enforced:* CI uses `--frozen` everywhere; `uv.lock` is committed.
- Every third-party action is pinned to a full 40-character commit SHA with a version
  comment. *Enforced:* `tests/test_workflow_action_pins.py`. *Gap:* the repository's Actions
  setting does not require SHA pinning, so the test is the only thing holding this.
- Dependency changes are licence- and advisory-gated. *Enforced:* `supply-chain.yml`
  (`pip-audit` + `pip-licenses`), path-filtered to dependency files, plus a daily schedule
  so an advisory against an unchanged pin is still caught. Its integrity is itself tested by
  `tests/test_supply_chain_schedule.py` and `tests/test_supply_chain_optional_extras_licence.py`.
- Security floors on dependencies are asserted semantically, not by pin equality.
  *Enforced:* `tests/test_extra_dep_ranges.py`.
- An agent may not downgrade or suppress a security finding.

**Operations**

- Publishing is triggered only by a `v*.*.*` tag, serialised by a concurrency group, and
  authenticated by OIDC Trusted Publishing with no stored token. *Enforced:* `publish.yml`.
- **Gap, and it is the significant one:** the `pypi` environment has no protection rules and
  no deployment-branch policy, and the repository has no branch, tag or ruleset protection.
  Any `v*.*.*` tag, at any commit — including one never merged and never gated — publishes
  to PyPI and cuts a public Release, and the publish `build` job does not run ruff, mypy or
  pytest. Treat a release as R3 and verify the tagged commit is an ancestor of `main` and
  green, because nothing else will.
- The local gate must not hang silently. *Enforced:* `tests/test_pytest_hang_safety_net.py`
  requires a `faulthandler_timeout`.

---

## Part D — evidence rules

- A finding blocks a merge only with a reproduction, a failing test, or a cited line
  demonstrably violating a stated invariant. **Unreproducible findings are advisory, not
  blocking.**
- A review binds to the **changed hunks**, not the base commit. Base movement invalidates a
  review only where the moved files intersect the diff.
- After a fix, re-review **the delta** — not the whole change again.
- Self-reported testing is not evidence. Show the command, its output and its exit code.
  Confidence, eloquence and consensus are not evidence; unanimous approval is a yellow flag.
- For a tricky correctness bug: prove the root cause with actual logged values, write a test
  that fails before and passes after, and only then claim the fix.
- Local green is not runtime green. Behaviour that depends on the Hermes runtime or a live
  gateway is validated against that target, and you state which target you ran against.
- Report failures and skips honestly. A green summary over a skipped suite is a defect.

---

## Part E — parallelism and delegation

- When work decomposes into two or more independent units, **run them concurrently** in
  separate lanes. Serial execution of independent work is a defect, not caution.
- Batch independent tool calls into a single message.
- Delegate wide reading and mechanical edits to subagents; keep their conclusions, not their
  transcripts. Read-only delegation is reliable here; multi-file implementation is more
  reliable in the main loop.
- Scope each lane to an independent file territory. Serialise work contending on a hot
  shared file under a single owner, and let the integrator re-verify every integrated commit
  independently — an agent's in-lane green is not integration evidence.
- Model routing: mechanical and high-volume (inventories, link checks, lint fixes, renames)
  → **Haiku 4.5**; implementation, ordinary fixes, focused review, docs → **Sonnet 5**;
  architecture, R2/R3 design, cross-cutting refactors, orchestration and adjudicating
  findings → **Opus 5** (effort `high`; `xhigh` only for R3).

---

## Part F — context discipline

Finishing the work remains mandatory. Running low on context is not a reason to deliver
partially and never appears in a status report as an excuse. But minimal sufficient context
is a **quality** requirement, not a cost concession: oversized context buries the
instructions that matter, dilutes review attention and increases disclosure. Read a file
because a specific question requires it, not as precaution. Delegate breadth to subagents
and keep their conclusions. Keep durable state outside the window — issues, PRs, commits,
ADRs — so a compaction boundary costs nothing.

Concretely: `docs/adr/` is over a megabyte and `docs/runbooks/` a third of one. "Read the
design docs first" means read *the* relevant ADR, found by search, not the archive.

---

## Part G — documentation proportionality

- Update docs in the same PR **when the change makes existing documentation wrong.** Not
  otherwise.
- Write an ADR when a decision constrains future changes. Not for every change.
- Write or update a runbook in the same commit that provisions or changes a real resource —
  what it is and why, the exact command used, its identifier, how to verify it, and how to
  rotate, recreate or roll it back. Runbooks are the operational how; ADRs are the why.
- Plans are checklists for multi-stage R2/R3 work, not templates with mandatory sections.
- Record durable findings at task end. No running provenance metadata for ordinary work.
- Every doc you write states what *is*. A doc describing behaviour that does not exist is a
  defect, not a roadmap.

---

## Part H — exceptions

The operator accepts risk. An exception is recorded in the PR that relies on it, names what
is being accepted and why, and expires — at the next release, or on a stated date. An
exception may never conceal a failed check, stand in for a review, or be self-granted by the
agent that benefits from it. If a gate cannot run, say so; do not write the evidence it
would have produced.

---

## Working hazards

Things that have actually gone wrong here.

- **Never trust an in-lane green.** A build cache shared across worktrees can link stale
  artefacts from a sibling lane. Build evidence from a clean, isolated output dir inside the
  lane; rebuild fresh after integrating cherry-picks.
- **Two agents in one lane corrupt the branch.** If a fresh lane already holds commits you
  did not author, stop and surface it.
- **Integration is single-commit cherry-picks**, never multi-commit sequences. Always
  `git log base..HEAD` a lane first — agents routinely leave more commits than they report.
- **A pre-commit lint failure aborts the commit** while the surrounding output can still
  read as passing. Confirm `git log -1` advanced before reporting work as committed.
- **The `pre-push` pytest hook can hang**, stalling a lane as committed-but-unpushed. Push
  with `--no-verify` and let CI re-gate; do not skip the test run itself.
- **A new `HERMES_SIP_*`/`HERMES_VOIP_*` var needs three registries in step** — both
  `plugin.yaml` copies and `_KNOWN_ENV_KEYS`. Only the full `uv run pytest` catches drift.
- **Wrapping a secret-bearing exception uses `raise ... from None`**, so the credential
  cannot leak through the traceback cause chain.
- Commits are Conventional Commits with the AI co-author trailer
  (`Co-Authored-By: <model name> <noreply@…>`).

---

## Where the previous 42 rules went

`AGENTS.md` used to be 42 numbered rules, and roughly 550 places in this repository still
cite them by number. The numbering is retired but the identifiers remain resolvable:

| Rules | Where the content lives now |
| --- | --- |
| 1–5 | Ownership: `AGENTS.md` preamble. You own execution; the operator sets direction and reviews via commits, PRs and the running system. Default and move on reversible choices. |
| 6 | Retired as blanket policy — scope is now the deliverable, and Part A sets what "done" means per class. Never partial-ship *within* an agreed scope. |
| 7 | Part H, plus `AGENTS.md` — confirmation still required for destructive or outward-facing actions. |
| 8–9 | `AGENTS.md` (lane rule) + `worktree-lane` skill + `.claude/hooks/enforce-worktree.mjs`. |
| 10–11 | *Working hazards* above (isolated output dirs, stale shared caches). |
| 12 | `AGENTS.md` — never commit directly to `main`. |
| 13 | *Working hazards* above (commit convention, cherry-pick integration). |
| 14 | Part A — you carry a PR to merged; "awaiting review" is not a parking state. |
| 15 | Part A, scaled by class: the full local gate is an R2/R3 gate, not an R0/R1 one. |
| 16 | Part D — a blocked verdict is a real signal; unanimous approval is a yellow flag. |
| 17 | `AGENTS.md` (typing) + `docs/stack.md` (the strict config that enforces it). |
| 18 | Part A — failing test first, scaled by class. |
| 19 | `AGENTS.md` — never weaken a test. Unconditional. |
| 20 | `AGENTS.md` — suppressions need inline justification. |
| 21 | Part A — independent cross-vendor review, scoped by class. |
| 22 | `docs/stack.md` — efficiency budgets for the real-time media path. |
| 23–25 | Part D — evidence rules. |
| 26 | Part D — validate on the real target. |
| 27 | `AGENTS.md` + Part G — no aspirational comments or docs. |
| 28 | Part A batching + minimal in-scope diffs. |
| 29 | Part E and Part F — delegation and context discipline. |
| 30 | Part F and Part G — read *the* relevant ADR; write one when it constrains the future. |
| 31 | This restructure is its outcome; the router/standard split replaces the line budget. |
| 32 | Part E — lane territories, serialised hot files, integrator re-verification. |
| 33 | Part C supply chain — frozen installs, committed lockfile. |
| 34 | `AGENTS.md` + Part C secrets. |
| 35 | Part C supply chain — licence and advisory gating. |
| 36 | `AGENTS.md` — no paid services. |
| 37 | `AGENTS.md` — errors propagate. |
| 38–39 | `AGENTS.md` (uv, typing) + `docs/stack.md`. |
| 40 | `AGENTS.md` — no assumed platform; undecided architecture stays deferred in `docs/adr/`. |
| 41 | `AGENTS.md` — infrastructure as code, 1Password, scoped tokens, rotation. |
| 42 | Part G — runbooks in the same commit as the change that provisions a resource. |
