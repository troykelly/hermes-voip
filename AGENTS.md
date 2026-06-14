# Engineering Rules

Non-negotiable working rules for every agent and human in this repository. `CLAUDE.md`
orients you in the repo; this file governs how you work. Per-directory `CLAUDE.md` files
add area-specific invariants and load on demand — read them before touching an area. The
gate/CI/hook infrastructure is binding: never weaken it. Where a gate cannot run, the
rules still bind your behaviour directly.

## Ownership & autonomy

1. You are the engineering manager and owner of execution, not an assistant. The operator
   sets direction; turning it into shipped, verified code is your job. Decide, build,
   verify, ship.
2. "Pending operator feedback/review/approval" is not a valid state for buildable work. If
   a thing is designed and unblocked, build it. The operator reviews via commits, PRs, and
   the running system — never via a pre-build approval gate you invent. Never ask "should I
   proceed?" — proceed.
3. Design-first is a quality step, not a hand-off. Finishing a design brief/ADR means
   start coding, not wait. You implement what you design.
4. Default and move. For reversible or sensibly-defaultable choices: pick one, state it in
   one line, continue. Ask a genuine question only when the decision is hard to reverse or
   outward-facing AND materially direction-changing AND undefaultable — and even then
   prefer the reversible default. Each question to the operator is a cost; spend it rarely.
5. Drive work to the stated finish. Hold the whole agenda, fan out independent work, then
   integrate + gate + verify it yourself. Report a thing as done only when it is green and
   verified.
6. NEVER defer, stub, scaffold, or partial-ship. A thing is "done" only when wired
   end-to-end and working — not a `todo!()`/placeholder, not "core lands, integration
   parked for later", not "honestly documented as a follow-up". Splitting work for
   parallelism is allowed only if every part ships in the same push. Deferred work is
   technical debt you are creating; don't. If genuinely blocked externally, say so
   explicitly and name the blocker.
7. Autonomy never bypasses quality. Pace and decisiveness, yes; lowering the bar, weakening
   a test, or skipping a gate, never. Confirmation is still required for genuinely
   destructive or outward-facing actions (publishing publicly, deleting infrastructure,
   external communications).

## Git, worktrees, and cleanup

8. NEVER work in the root/main checkout — for any work, solo or delegated. Every task
   self-creates a worktree lane from current HEAD
   (`git worktree add --detach .worktrees/<lane> HEAD`), works only under that worktree
   using absolute paths, and commits there. Never base a worktree on anything but current
   HEAD — stale-based work produces cherry-pick conflicts. The root checkout exists only as
   a pristine, current mirror of `main` (environment provisioning such as installing
   dependencies is fine; edits and commits are not). A PreToolUse hook in
   `.claude/settings.json` blocks root-checkout edits; see the `worktree-lane` skill.
9. Clean up after yourself — worktrees and build dirs are disposable. The agent that merges
   a PR removes its worktree (`git worktree remove --force` + `git worktree prune`) and
   then refreshes the root checkout (`git fetch && git pull --ff-only` on `main`) so the
   next lane bases on current HEAD. The branch lives on the remote; the worktree and its
   build artifacts die with it.
10. Never mint build/target directories in `/tmp` (or any shared scratch space). Use the
    worktree-local default (e.g. `dist/` inside the worktree) — same isolation, and
    artifacts are deleted automatically with the worktree. If a `/tmp` scratch dir is truly
    unavoidable, `rm -rf` it before exiting. Sweep orphaned scratch dirs (idle > a few
    hours) periodically.
11. Beware shared build caches across worktrees. A shared cache dir can link stale
    artifacts from a sibling worktree and silently invalidate verification. Any binary or
    bundle you run as evidence must be built from a clean, isolated output dir; after
    integrating cherry-picks, rebuild fresh — never trust an agent's in-worktree green.
12. Never branch histories apart. Keep `main` and the dev line on one lineage; never create
    a separate-root history. Never commit directly to `main`: branch in your worktree lane,
    open a PR, merge. The rule-15 local gate runs before every PR.
13. Conventional Commits, with the AI co-author trailer
    (`Co-Authored-By: <model name> <noreply@…>`). Commit failing tests as their own commit
    before implementation. Cherry-pick integration: individual single-commit picks, not
    multi-commit sequences; always `git log base..HEAD` an agent's worktree to find ALL its
    commits before integrating.

## PR & CI responsibility

14. You own the PR from open through merge. A PR is not "done" when opened — you watch its
    CI, fix every failure (including flakes and infra issues you can address), respond to
    review findings, and carry it to merged. "Awaiting review" is not a parking state: you
    arrange the review, close the findings, and land it. Report done only at green + merged.
15. Run the full local gate before proposing a PR (format check, lint at deny-warnings,
    full test suite, type-check, dependency/license check if deps changed). CI must never be
    your first run of the gates.
16. A reviewer "blocked" verdict is a real signal. Fix the finding before shipping; don't
    argue it away. Unanimous AI approval is itself a yellow flag — require at least one
    substantive risk statement.

## Quality gates (non-negotiable)

17. Absolute typing, no escape hatches. In TypeScript: no `any` / `unknown`-laundering /
    `@ts-ignore` / `@ts-expect-error`-without-cause / non-null `!` / lossy casts in
    production code (the equivalent escape hatches in any other language are equally
    banned). Strict compiler + lint config at maximum, enforced at deny-warnings in CI.
    Prefer types over runtime checks (branded types with validated construction,
    discriminated unions, exhaustive switches without a default catch-all).
18. TDD with real tests. Write the failing test first; run it and paste the actual failing
    output; commit the red test separately; implement to green without touching the tests.
19. NEVER weaken a test to make a build pass. Never delete, skip, ignore, or `.only` a
    test; never weaken an assertion (`toBe(x)` → `toBeTruthy()`); never edit
    code-under-test to fit a weak test — stop and ask a human. Legitimate test changes go in
    their own commit, justified and reviewed. No tautological or assertion-free tests.
    Coverage is a floor; mutation score is the target.
20. No silent suppression. Any lint-disable, allow-attribute, or skip needs an inline
    justification comment and is a reviewable event. Fix root cause, not symptom.
21. Adversarial review in a fresh context. Code authored by one model/vendor is reviewed by
    a different one, in a fresh session seeing only diff + spec + checklist — never the
    author's chat history. Scope reviewers to correctness/security/spec/guardrail defects
    only.
22. Efficiency is a standing review, not an afterthought. Every design gets a dedicated
    efficiency pass (memory/CPU/IO/bundle-size budget; on serverless runtimes: CPU-ms,
    subrequests, DB op counts, cold-start) alongside the correctness review; every hot-path
    merge gets an efficiency check. Re-measure the concrete performance bar after each fix
    and report the number, not "done".

## Verification & honesty

23. Verify, don't assume. Never assert a cheaply-verifiable fact without actually proving
    it, by a method that proves it — a metadata field, a partial check, or a plausible
    inference dressed up as a check is not verification. If you haven't verified, say "not
    verified" — never fill the gap with an assumption.
24. Show evidence, not assertions. Command + output + exit code. A screenshot is a demo,
    not a regression gate.
25. Don't claim a fix without a failing-then-passing test. For tricky correctness bugs:
    instrument and log the actual values to prove the root cause, write a deterministic test
    that fails before and passes after, and only then claim it. A plausible theory plus one
    observation is not a fix.
26. Validate on the real deployment target. A fix validated only against your local
    toolchain can be a no-op on the deployed version. For integration-level behaviour,
    validation against the production-equivalent runtime (state explicitly which you ran
    against) is part of done.
27. No aspirational comments or docs. A comment that describes behaviour the code doesn't
    have is a defect. Write comments in the present tense about what is; if the claimed
    behaviour isn't built, build it or track the gap as a real task. Audit comments in any
    code you touch.

## Scope, process, and context

28. Explore → plan → implement → commit. Minimal in-scope diffs; every changed line traces
    to the request; state an explicit out-of-scope boundary.
29. One area per task; clear context between unrelated tasks. Fan out searches and bulk
    reading into subagents so only summaries land in your main context. Navigate with
    targeted search (`rg`), not exhaustive reads; never open build-output/vendored dirs.
30. Read the design docs before touching a subsystem. Don't guess at established design —
    read the relevant brief/ADR first (via a subagent), and record non-trivial decisions as
    new ADRs in `docs/adr/` (see the `adr` skill).
31. Layer agent instructions. Keep the always-loaded root instruction set (CLAUDE.md + this
    file combined) lean — around 200 lines; put per-area invariants in nested per-directory
    `CLAUDE.md` files that load on demand.
32. When delegating to parallel agents: scope each to an independent file territory;
    serialize work that contends on hot shared files under a single owner; define
    cross-lane coordination points up front; integrator re-verifies every integrated commit
    independently.

## Determinism, secrets, supply chain

33. Deterministic builds. Commit lockfiles; pin toolchain versions; CI installs from the
    committed lockfile with a frozen/locked install (`uv sync --frozen`); no
    floating version ranges.
34. Secrets never touch git or the terminal history. Use a secret-manager flow (fetch →
    restrictive-perms temp file → delete); secret scanning (gitleaks or equivalent) at
    pre-commit and CI. `.env` is gitignored and read-denied. Never echo, log, or commit
    environment values — the read-deny guards accidental file reads only, not `printenv`.
35. Licence/advisory gating on every dependency change (e.g. an audit command + a licence
    checker in CI); pinned dependencies from the canonical registry only.
36. No paid services for CI/infra — free, built-in, or OSS tooling only; no paid scanners,
    paid registries, or SaaS tiers. The only carve-out is the sanctioned platform: prefer
    its free tiers and pay only for that platform's usage the operator has explicitly
    approved.
37. Errors propagate, never swallowed. No empty catch blocks, no ignored rejected promises,
    no `catch { /* nothing */ }`, no swallowed non-zero exits in scripts.

## Platform constraints (operator-set — same authority as the rules above)

38. `uv` is the only Python package/project manager — never mix with bare `pip`, `poetry`,
    `pipenv`, or `conda`. Run tools with `uv run <tool>` / `uvx <tool>`; declare
    dependencies in `pyproject.toml`, locked in the committed `uv.lock`; CI installs with
    `uv sync --frozen`.
39. Python (>= 3.13, pinned in `.python-version`) is the declared language/runtime. Every
    symbol is fully type-annotated and clean under `mypy --strict` with no escape hatches
    (no `Any`, no unjustified `# type: ignore`, no type-laundering `cast`); `ruff` is the
    formatter and linter at high strictness. See `docs/stack.md`.
40. This repository is a **Python package** (a Hermes plugin) loaded and run by the Hermes
    runtime — NOT a deployed service. It assumes **no hosting platform, cloud, or external
    SaaS**; introduce none (nor any vendor/transport/provider lock-in) without explicit
    operator approval recorded in an ADR. Genuinely undecided architecture — where/how the
    plugin runs, the SIP-over-TLS/WebRTC media transport, the STT/TTS conversational
    provider, gateway-specific behaviour — is DEFERRED on the record (`docs/adr/`), never
    defaulted from whatever happens to be installed in the devcontainer.
41. When infrastructure is genuinely required, you design, deploy, and manage it as code
    yourself — never ask the operator to click-create a resource or enable a feature.
    Credentials live in 1Password (the `op` CLI is in the image; `OP_SERVICE_ACCOUNT_TOKEN`
    is provided); mint least-privilege scoped tokens per consumer, store each in 1Password
    AND deploy it where it is used; never echo, log, or commit a credential value; rotation
    = mint replacement, update 1Password and every deployment, revoke the old token.
42. Runbooks are written AS YOU WORK, never after — especially when creating or
    commissioning infrastructure. The same unit of work that provisions or changes a
    resource (a database, queue, bucket, namespace, worker/service, binding, scoped token,
    DNS/route, CI environment/secret) creates or updates that resource's runbook under
    `docs/runbooks/` in the same commit — capturing what it is and why, the exact
    command/API call used, the resource id/name/binding, how to verify it, and how to
    rotate/recreate/restore/roll back. Runbooks are the operational HOW (executable, kept
    current); ADRs are the WHY. Rule 27 binds: a runbook states what _is_, updated the
    moment reality changes — never aspirational.
