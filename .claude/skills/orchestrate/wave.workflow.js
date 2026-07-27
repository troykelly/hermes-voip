// wave.workflow.js — the canonical ultracode Workflow for ONE orchestration wave.
//
// Driven by the `/orchestrate` skill (see SKILL.md). Two phases, switched by `args.phase`:
//   - "gap-review": fan out one read-only reviewer per dimension → structured candidate items.
//   - "implement":  pipeline(selected items) through TDD implementation → cross-vendor +
//                   cross-tier adversarial review, returning verdicts for the orchestrator
//                   to PR/merge.
//
// The orchestrator (the main session) owns sensing, selection, PR creation, merging, memory,
// and the continuation guarantee. This script owns the parallel fan-out only.
//
// Plain JS only (no TS). `meta` is a pure literal. No Date.now()/Math.random()/new Date().
// Resumable via {scriptPath, resumeFromRunId}.

export const meta = {
  name: 'orchestrate-wave',
  description: 'One orchestration wave: fan out gap-review across dimensions, OR implement+review a batch of backlog items (TDD, full local gate, cross-vendor codex + cross-tier Claude review).',
  whenToUse: 'Invoked by the /orchestrate skill each wave. args.phase = "gap-review" | "implement".',
  phases: [
    { title: 'Gap review' },
    { title: 'Implement' },
    { title: 'Review' },
  ],
}

// ---------------------------------------------------------------------------
// Shared: the few things a subagent must know BEFORE it reads anything. Subagents
// get fresh context, so this cannot be empty — but it is deliberately not a copy
// of the contract. Everything else is a file they can open, and re-sending it on
// every prompt is what made this workflow's fixed floor larger than the diffs it
// reviewed. Full contract: docs/standards/engineering.md.
// ---------------------------------------------------------------------------
const RULES = [
  'PUBLIC REPO: never put the SIP host/extension/password/device-model or any PII in a tracked',
  'file, comment, test, fixture, doc, commit message, or log. Tests use fakes (pbx.example.test,',
  'ext 1000). Secrets live only in the gitignored .env / 1Password.',
  'LANE ONLY: edit inside your own worktree; never the root checkout.',
  'Do NOT use the memory MCP (qdrant) — it is single-process and owned by the orchestrator.',
  'GATES SCALE WITH BLAST RADIUS: run `uv run python -m tools.classify` and meet that class in',
  'docs/standards/engineering.md Part A. Read AGENTS.md for the invariants; read the standard',
  'for the gates. Do not assume every change owes every gate.',
  'Always: mypy --strict clean (no Any / unjustified ignore / laundering cast); errors propagate;',
  'never weaken, skip or delete a test; no new hosting/platform/SaaS without an operator-approved',
  'ADR. Conventional Commits + trailer: Co-Authored-By: Claude <noreply@anthropic.com>.',
].join(' ')

const GATE =
  'uv sync --frozen && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest'

// Extra CI jobs to mirror locally when the change touches these surfaces (see .github/workflows/gate.yml):
const EXTRA_GATES = [
  'adapter.py / voip_tools.py / tests/e2e → uv sync --frozen --extra hermes --extra webrtc --extra media (+ apt libopus0), then the hermes-contract mypy+pytest subset with HERMES_CONTRACT_REQUIRED=1',
  'providers/* or stt/* or tts/* → uv sync --frozen --extra ml, then uv run pytest -rs',
  'media/srtp|srtcp|dtls → uv sync --frozen --extra media, then uv run pytest -rs',
  'transport/ws_connection or media/opus|ice → uv sync --frozen --extra webrtc (+ apt libopus0), then uv run pytest -rs',
].join(' | ')

const DEFAULT_DIMENSIONS = [
  { key: 'correctness', model: 'opus', focus: 'logic bugs, contract violations, RFC compliance, off-by-ones, races' },
  { key: 'robustness', model: 'sonnet', focus: 'error handling and propagation, hostile input, edge cases, fail-closed behaviour' },
  { key: 'security-auth', model: 'opus', focus: 'injection guard, caller groups/modes, SIP digest, SRTP/DTLS, secret hygiene (public repo), supply-chain advisories + licences' },
  { key: 'tests-mutation', model: 'sonnet', focus: 'coverage gaps, weak/assertion-free tests, missing async tests, surviving mutants' },
  { key: 'docs-drift', model: 'sonnet', focus: 'aspirational or contradictory docs vs code; stale IMPLEMENTATION-PLAN.md / backlog.md preamble / README; runbook numbering collisions' },
  { key: 'api-ergonomics', model: 'sonnet', focus: '__all__, public exports, typed surfaces, import discoverability' },
  { key: 'performance', model: 'sonnet', focus: 'hot-path budgets, allocations (record concrete numbers)' },
  { key: 'observability', model: 'sonnet', focus: 'instrument runbook-0014 SLOs, RTCP metrics, structured logs — LOCAL-ONLY emission only (external sink/dashboard is propose-only)' },
  { key: 'ux-conversational', model: 'opus', focus: 'greeting, barge-in feel, silence/goodbye, error speech, multi-language, voice accessibility' },
  { key: 'operability', model: 'sonnet', focus: 'runbooks current, plugin enablement, graceful shutdown, config validation' },
  { key: 'packaging-release', model: 'haiku', focus: 'plugin manifest, entry points, version hygiene, dependency pinning' },
  { key: 'product-features', model: 'opus', focus: 'implied/designed features (agent-screened answering [designed in backlog], issue #64 video→vision, call transfer); infra-needing surfaces → Proposed ADR only' },
]

// ---------------------------------------------------------------------------
// Schemas (JSON Schema) for structured subagent output.
// ---------------------------------------------------------------------------
const GAP_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    items: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          title: { type: 'string', description: 'one-line actionable title' },
          dimension: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          kind: { type: 'string', description: 'correctness|robustness|security|test|docs|api|efficiency|observability|ux|operability|packaging|feature' },
          rationale: { type: 'string', description: 'why it matters + evidence (file:line where possible)' },
          files: { type: 'array', items: { type: 'string' } },
          size: { type: 'string', enum: ['small', 'medium', 'large'] },
          deps: { type: 'array', items: { type: 'string' } },
          needsInfra: { type: 'boolean', description: 'true if it requires new hosting/platform/SaaS/cost (→ propose-only)' },
        },
        required: ['title', 'dimension', 'severity', 'kind', 'rationale', 'size', 'needsInfra'],
      },
    },
  },
  required: ['items'],
}

const IMPL_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    failed: { type: 'boolean' },
    reason: { type: 'string', description: 'if failed: what blocked you' },
    branch: { type: 'string' },
    baseSha: { type: 'string' },
    redCommit: { type: 'string', description: 'sha of the standalone failing-test commit' },
    greenCommits: { type: 'array', items: { type: 'string' } },
    gate: { type: 'string', description: 'gate commands run + their exit codes (evidence)' },
    adr: { type: 'string', description: 'ADR file path if one was written' },
    runbook: { type: 'string', description: 'runbook file path if one was written/updated' },
    files: { type: 'array', items: { type: 'string' } },
    spec: { type: 'string', description: 'restate what this change does (for the reviewer)' },
    selfRisk: { type: 'string', description: 'your own honest risk statement' },
  },
  required: ['failed'],
}

const REVIEW_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    reviewer: { type: 'string', description: 'e.g. codex / opus / fable / sonnet, or codex-unavailable' },
    verdict: { type: 'string', enum: ['approve', 'block'] },
    mustFix: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          finding: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          location: { type: 'string' },
        },
        required: ['finding', 'severity'],
      },
    },
    noted: { type: 'array', items: { type: 'string' }, description: 'non-blocking observations' },
    riskStatement: { type: 'string', description: 'at least one substantive risk; rubber-stamping is a defect' },
  },
  required: ['verdict', 'mustFix', 'riskStatement'],
}

// ---------------------------------------------------------------------------
// Prompt builders.
// ---------------------------------------------------------------------------
function gapPrompt(d) {
  return [
    `You are a GAP-REVIEW agent for the hermes-voip repo (a fully-typed Python Hermes plugin`,
    `for two-way voice over SIP/WebRTC). Your dimension: **${d.key}** — focus: ${d.focus}.`,
    ``,
    `Hunt for GENUINELY NEW, actionable work in YOUR dimension only. Read targeted: use rg and`,
    `read excerpts; do NOT open build/vendored dirs. Cross-check docs/backlog.md and`,
    `\`gh issue list\` and do NOT report anything already tracked there.`,
    ``,
    `For each finding give a concrete, shippable item with evidence (file:line) and an honest`,
    `severity/size. Set needsInfra=true if it would require new hosting/platform/SaaS/cost`,
    `(those are propose-only). Prefer high-signal items over volume; an empty list is a valid,`,
    `honest answer if your dimension is genuinely clean.`,
    ``,
    `Binding rules context: ${RULES}`,
    ``,
    `Return ONLY the structured object.`,
  ].join('\n')
}

function implPrompt(item, index) {
  const branchHint = branchFor(item, index)
  return [
    `You are an IMPLEMENTATION agent for the hermes-voip repo. You are in your OWN isolated git`,
    `worktree (a linked worktree of the same repo; \`origin\` is configured). Deliver ONE backlog`,
    `item end-to-end, TDD, and push a branch. Do NOT open a PR (the orchestrator does that).`,
    ``,
    `## The item`,
    `- title: ${item.title}`,
    `- severity/kind: ${item.severity || '?'} / ${item.kind || '?'}`,
    `- files (hint): ${(item.files || []).join(', ') || '(discover)'}`,
    `- spec/rationale: ${item.rationale || item.spec || item.title}`,
    ``,
    `## Procedure (every step is mandatory)`,
    `1. Create + switch to branch \`${branchHint}\`. Record the base sha (git rev-parse HEAD).`,
    `2. Install deps: \`uv sync --frozen\` (add the right --extra per the surface, see below).`,
    `3. TDD: write the FAILING test first; run it; capture the red output; commit the red test`,
    `   as its OWN commit (Conventional Commit, with the Co-Authored-By trailer).`,
    `4. Implement to GREEN without touching the test. If you make a non-trivial design decision,`,
    `   write an ADR (docs/adr/NNNN — next free number). If you touch infra/ops, write/update the`,
    `   runbook (docs/runbooks/) when it changes a real resource. No aspirational docs.`,
    `5. Run the FULL local gate and capture exit codes: \`${GATE}\`.`,
    `   Extra surface-specific gates when relevant: ${EXTRA_GATES}.`,
    `6. Commit the green implementation. Push: \`git push -u origin ${branchHint}\`.`,
    ``,
    `If you cannot reach green (blocked, flaky, design ambiguity), STOP and return {failed:true,`,
    `reason:"..."} with what you learned — do NOT weaken a test or ship a partial.`,
    ``,
    `Binding rules: ${RULES}`,
    ``,
    `Return ONLY the structured object (branch, redCommit, greenCommits, gate evidence, spec,`,
    `selfRisk, etc.).`,
  ].join('\n')
}

function codexReviewPrompt(impl, item) {
  return [
    `You are a REVIEW DRIVER. Obtain an INDEPENDENT CROSS-VENDOR review from the \`codex\` CLI`,
    `(OpenAI) of the change on branch \`${impl.branch}\`, then faithfully report codex's findings.`,
    ``,
    `Steps:`,
    `1. \`git fetch origin ${impl.branch}\` then compute the diff: \`git diff origin/main...origin/${impl.branch}\`.`,
    `2. Run codex non-interactively (check \`codex --help\`; typically \`codex exec "<prompt>"\`).`,
    `   Ask it to review the diff for CORRECTNESS, SECURITY, SPEC-CONFORMANCE, and GUARDRAIL`,
    `   defects ONLY (ignore cosmetics). Spec to check against: "${item.title}".`,
    `   Send codex the DIFF and the spec — not the repo tree, not whole files.`,
    `3. If codex is unavailable/unauthenticated, do NOT fail the wave: set reviewer`,
    `   "codex-unavailable", give your own careful read, and say so in riskStatement.`,
    ``,
    `Materiality: mustFix = correctness/security/spec/guardrail/blast-radius only. Give ≥1`,
    `substantive risk statement — rubber-stamping is a defect. Return ONLY the object.`,
  ].join('\n')
}

function claudeReviewPrompt(impl, item, reviewerModel) {
  return [
    `You are an ADVERSARIAL REVIEWER (model: ${reviewerModel}) in a FRESH context. You see only`,
    `the diff + spec + this checklist — never the author's reasoning. Try to REFUTE the`,
    `change's correctness.`,
    ``,
    `1. \`git fetch origin ${impl.branch}\`; review \`git diff origin/main...origin/${impl.branch}\`.`,
    `2. Spec: "${item.title}" — ${item.rationale || item.spec || ''}.`,
    `3. Check: does the failing-test-first discipline hold (a real red→green)? Are there`,
    `   correctness/security/spec/guardrail defects? Typing escape hatches (Any / ignore / cast)?`,
    `   Swallowed errors? Public-repo secret leakage? A half-wired change?`,
    ``,
    `Materiality gate: mustFix = correctness/security/spec/guardrail/blast-radius only;`,
    `note cosmetics without blocking. An unreproducible finding is advisory, not blocking.`,
    `Give ≥1 substantive risk statement — unanimous approval is a yellow flag.`,
    `Contract if you need it: docs/standards/engineering.md (Part D covers evidence).`,
  ].join('\n')
}

function slugify(s) {
  return (
    String(s || '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 48) || 'item'
  )
}

// A unique, conventional branch name per item. The `-${index}` suffix guarantees
// uniqueness within a wave even when two item titles slugify identically (gap-review
// items carry no branch/id), so parallel implementation agents never collide on one
// remote branch. The orchestrator may pre-assign item.branch to override this.
function branchFor(item, index) {
  if (item.branch) return item.branch
  const typeByKind = {
    test: 'test',
    docs: 'docs',
    correctness: 'fix',
    robustness: 'fix',
    security: 'fix',
    efficiency: 'perf',
    observability: 'feat',
    ux: 'feat',
    api: 'feat',
    operability: 'chore',
    packaging: 'chore',
    feature: 'feat',
  }
  const type = item.type || typeByKind[item.kind] || 'chore'
  return `${type}/${slugify(item.topic || item.id || item.title)}-${index}`
}

function pickReviewer(authorModel) {
  switch (authorModel) {
    case 'opus':
      return 'fable'
    case 'fable':
      return 'opus'
    case 'sonnet':
      return 'opus'
    case 'haiku':
      return 'sonnet'
    default:
      return 'opus'
  }
}

// ---------------------------------------------------------------------------
// Wave body.
// ---------------------------------------------------------------------------
const phaseArg = (args && args.phase) || 'gap-review'

if (phaseArg === 'gap-review') {
  phase('Gap review')
  const requested = args && Array.isArray(args.dimensions) && args.dimensions.length ? args.dimensions : null
  const dims = requested
    ? DEFAULT_DIMENSIONS.filter((d) => requested.includes(d.key)).concat(
        // allow ad-hoc dimension keys not in the default set
        requested
          .filter((k) => !DEFAULT_DIMENSIONS.some((d) => d.key === k))
          .map((k) => ({ key: k, model: 'sonnet', focus: 'operator-requested dimension' })),
      )
    : DEFAULT_DIMENSIONS

  log(`gap-review: fanning out ${dims.length} dimensions`)
  const results = await parallel(
    dims.map((d) => () =>
      agent(gapPrompt(d), {
        label: `gap:${d.key}`,
        phase: 'Gap review',
        model: d.model || 'sonnet',
        effort: 'high',
        schema: GAP_SCHEMA,
      }),
    ),
  )
  const items = results
    .filter(Boolean)
    .flatMap((r) => (r.items || []))
  log(`gap-review: ${items.length} candidate items discovered`)
  return { phase: 'gap-review', dimensions: dims.map((d) => d.key), count: items.length, items }
}

if (phaseArg === 'implement') {
  const items = (args && Array.isArray(args.items) ? args.items : []).slice(0, 64)
  if (!items.length) {
    log('implement: no items provided — nothing to do this wave')
    return { phase: 'implement', results: [] }
  }
  log(`implement: ${items.length} items through TDD → cross-vendor review`)

  // pipeline() awaits each stage's return value (incl. Promise-returning stages) before
  // passing it to the next — the canonical Workflow pattern is exactly a stage that returns
  // `parallel(...).then(...)` (see the Workflow tool docs), and items pipeline independently
  // so review starts the moment each impl is green. So stage 2 may return a Promise.
  const results = await pipeline(
    items,
    // Stage 1: implement (TDD + full gate + push), isolated worktree, model per item.
    // Stage callbacks receive (prevResult, originalItem, index); for stage 1 the first arg
    // is the item itself and the third is its index (used for a unique branch name).
    (item, _orig, index) =>
      agent(implPrompt(item, index), {
        label: `impl:${item.id || item.title}`,
        phase: 'Implement',
        model: item.model || 'sonnet',
        effort: item.effort || 'high',
        isolation: 'worktree',
        schema: IMPL_SCHEMA,
      }),
    // Stage 2: adversarial review (codex cross-vendor + cross-tier Claude), in parallel.
    (impl, item) => {
      // Treat a success without a pushed branch as a failure: IMPL_SCHEMA only requires
      // `failed`, so a malformed `{failed:false}` (no branch) must NOT reach review — the
      // reviewers diff `origin/<branch>`, and `origin/undefined` would fake an empty review.
      if (!impl || impl.failed || !impl.branch) {
        const reason = !impl
          ? 'implementation returned null'
          : impl.failed
            ? impl.reason || 'implementation reported failure'
            : 'implementation reported success but pushed no branch'
        return { item: item.title, branch: null, clean: false, failed: true, reason }
      }
      const reviewerModel = pickReviewer(item.model || 'sonnet')
      return parallel([
        () =>
          agent(codexReviewPrompt(impl, item), {
            label: `review:codex:${item.id || item.title}`,
            phase: 'Review',
            model: 'haiku',
            effort: 'medium',
            schema: REVIEW_SCHEMA,
          }),
        () =>
          agent(claudeReviewPrompt(impl, item, reviewerModel), {
            label: `review:${reviewerModel}:${item.id || item.title}`,
            phase: 'Review',
            model: reviewerModel,
            effort: 'high',
            schema: REVIEW_SCHEMA,
          }),
      ]).then((verdicts) => {
        const vs = verdicts.filter(Boolean)
        const mustFix = vs.flatMap((v) => v.mustFix || [])
        const clean = vs.length > 0 && mustFix.length === 0 && vs.every((v) => v.verdict === 'approve')
        // A rubber-stamp = a clean pass where NO reviewer offered a substantive risk
        // statement or observation (unanimous low-effort approval is a yellow flag
        // the orchestrator must escalate). Trigger on absent signal, not on a missing field.
        const substantive = (v) =>
          (Array.isArray(v.noted) && v.noted.length > 0) ||
          (typeof v.riskStatement === 'string' && v.riskStatement.trim().length >= 40)
        const rubberStamp = clean && !vs.some(substantive)
        return {
          item: item.title,
          branch: impl.branch,
          impl,
          verdicts: vs,
          mustFix,
          clean,
          rubberStamp,
          failed: false,
        }
      })
    },
  )

  return { phase: 'implement', results: results.filter(Boolean) }
}

log(`orchestrate-wave: unknown phase '${phaseArg}' (expected "gap-review" or "implement")`)
return { phase: phaseArg, error: 'unknown phase' }
