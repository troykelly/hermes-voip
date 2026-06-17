# ADR-0024: Audit the full optional-extra surface; pyjwt bump blocked upstream

- **Date:** 2026-06-17
- **Status:** Accepted
- **Deciders:** agent session

## Context

`pip-audit` flagged **8 advisories (PYSEC-2026-175 … -179, five distinct IDs)**
in **pyjwt 2.12.1**, all fixed in **pyjwt 2.13.0**. pyjwt is not a direct
dependency: it arrives as `pyjwt[crypto]`, a transitive dependency of
`hermes-agent==0.16.0` (the Hermes runtime, declared as the optional `hermes`
extra in `pyproject.toml`).

Two facts forced this ADR:

1. **The CI `audit` job did not catch it.** The `audit` job in
   `.github/workflows/supply-chain.yml` ran `uv sync --frozen` with **no extras**,
   so pyjwt — and every other extra-only transitive dependency (the `ml`, `media`,
   `webrtc` stacks too) — was never installed in the audit environment. pip-audit
   audits the *installed* environment, so it reported a false green. The advisories
   were visible only as Dependabot alerts on the default branch.

2. **The fix is blocked upstream.** hermes-agent 0.16.0 **hard-pins**
   `PyJWT[crypto]==2.12.1` (strict `==`, not a range). Verified two ways
   (rule 23):
   - its published PyPI `Requires-Dist` contains exactly `PyJWT[crypto]==2.12.1`;
   - forcing `pyjwt>=2.13.0` and running `uv lock` fails with
     `No solution found … hermes-agent==0.16.0 depends on pyjwt[crypto]==2.12.1 …
     your project's requirements are unsatisfiable`.

   We therefore **cannot** raise pyjwt to 2.13.0 without an incompatible
   resolution. Rule 6 forbids shipping a broken resolution; rule 40 forbids
   vendoring/forking the runtime to route around it. The real fix lives in
   hermes-agent (relax the pin to `>=2.12.1` / `>=2.13.0`); this repo cannot make
   it here.

## Decision

**1. Audit the full extra surface.** The `audit` job now runs
`uv sync --frozen --all-extras` before `pip-audit`, so the advisory gate sees the
complete transitive dependency surface this plugin can pull in (hermes, ml, media,
webrtc), not just the empty default runtime set. The licence gate in the same job
is unchanged and stays scoped to **production** deps: it reads the lock via
`uv export --frozen --no-dev` (not the installed venv), so widening the *install*
does not widen the licence-obligation surface (verified: it still resolves to the
single production dep `audioop-lts`).

**2. Suppress only the blocked-upstream pyjwt advisories, by ID.** Because the
pyjwt fix is unreachable, `pip-audit` is invoked with
`--ignore-vuln PYSEC-2026-175 … -179` (the five IDs). This keeps the gate
**actionable**: it is green today, but any *other* advisory — a sixth pyjwt one,
or a new vuln in any extra — still fails CI red (verified: omitting one ID
restores a non-zero exit). The suppression carries an inline justification in the
workflow naming the upstream blocker (rule 20). These ignores are **removed the
moment hermes-agent relaxes its pin**.

**3. Make the workflow self-triggering.** `supply-chain.yml` now also triggers on
changes to itself (added to the `paths` filter), so audit-job changes are
exercised by the PR that makes them.

The operational HOW (commands, how to re-check, how to drop the ignores) is
`docs/runbooks/0003-supply-chain-audit.md`. The upstream blocker is to be raised
as a hermes-agent issue requesting the pin be relaxed.

## Consequences

- The supply-chain audit now covers the real attack surface; this class of
  extra-only transitive vuln is caught going forward instead of hiding behind a
  false green.
- We carry a small, explicit, ID-scoped suppression list with a documented expiry
  condition (hermes-agent relaxes its pin). It must be revisited on every
  hermes-agent bump — the runbook spells out the check.
- The live gateway still runs pyjwt 2.12.1 until hermes-agent ships a fix; the
  exposure is unchanged by this ADR (it neither adds nor removes the vuln — it
  makes it *visible and gated*). A redeploy picks up any future pyjwt bump via the
  normal `uv sync` path once the pin is relaxed.
- pip-audit and pip-licenses remain free/OSS (rule 36); no new tooling or paid
  service is introduced.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Force `pyjwt>=2.13.0` via a direct constraint + `uv lock` | `uv lock` proves it unsatisfiable against hermes-agent's `==2.12.1` pin; would not resolve (rule 6 — never ship a broken build). |
| Override the transitive pin with `[tool.uv] override-dependencies` | Forces pyjwt 2.13.0 into hermes-agent against its declared `==2.12.1` constraint — an untested combination of the runtime the live gateway depends on; substitutes an unverified compatibility risk for a known-and-contained advisory. The correct fix is upstream. |
| Vendor/fork hermes-agent to patch the pin | Rule 40 (no vendor lock-in / forking the runtime) and a large maintenance burden for a transitive patch. |
| Keep `--all-extras` audit but DON'T suppress (let it fail red) | A permanently-red gate that nothing can fix trains reviewers to ignore it; it would also block every future supply-chain PR. The ID-scoped ignore keeps the gate meaningful. |
| Blanket-ignore pyjwt (e.g. by package, not by ID) | Would hide a *future* pyjwt advisory too. Per-ID suppression fails on any new finding. |
| Leave the audit at `uv sync --frozen` (status quo) and only file the upstream issue | Leaves the audit blind to all extra-only transitive vulns indefinitely — the exact gap that hid these advisories. |
