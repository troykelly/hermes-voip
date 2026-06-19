# Runbook: Supply-chain advisory audit (CI `audit` job)

**What it is.** The CI advisory + licence gate for this plugin's dependencies
(AGENTS.md rule 35), in the `audit` job of
`.github/workflows/supply-chain.yml`. It (1) installs the dependency surface and
runs `pip-audit` (PyPI Advisory DB / OSV — free/OSS), and (2) licence-checks the
production deps with `pip-licenses` against a permissive-OSS allowlist. The WHY is
ADR-0024. This runbook is the HOW.

> Public repo — no hosts/tokens/PII here; this gate touches only package metadata.

## What it does (current shape)

```yaml
- run: uv sync --frozen --all-extras        # install hermes+ml+media+webrtc too
- run: uv run pip-audit                     # audit the FULL installed surface, no suppression
# licence gate (unchanged): scoped to PRODUCTION deps via `uv export --no-dev`
```

`--all-extras` matters: `pip-audit` audits the *installed* environment, so a bare
`uv sync --frozen` (no extras → only `audioop-lts` installed) leaves every
extra-only transitive dependency uninstalled and reports a **false green**. The
licence gate is unaffected — it reads the lock via `uv export --frozen --no-dev`,
which ignores extras, so it stays scoped to production deps (today: just
`audioop-lts`).

## pyjwt history — fully resolved (ADR-0062)

`pyjwt 2.12.1` (transitive via the `hermes` extra) carried 5 advisories
(PYSEC-2026-175 … -179; HIGH + medium/low; fix: 2.13.0). Previously blocked
because `hermes-agent==0.16.0` hard-pinned `PyJWT[crypto]==2.12.1` and
`uv lock` reported "unsatisfiable" when forced to ≥2.13.0.

**Resolved 2026-06-19** via a uv resolver override in `pyproject.toml`
(`[tool.uv] override-dependencies = ["pyjwt[crypto]>=2.13.0"]`), which
supersedes the hermes-agent pin at the resolution layer. `uv lock` now resolves
cleanly to pyjwt 2.13.0; `pip-audit` reports no advisories. The five
`--ignore-vuln` lines that previously suppressed these CVEs have been removed
from `supply-chain.yml`. See ADR-0062 for the full rationale and verification
evidence.

**Revert path.** If a future hermes-agent version is incompatible with
pyjwt 2.13.x (i.e. `uv lock` fails "unsatisfiable" after a hermes-agent bump),
the override may need to be widened or removed. If hermes-agent itself ships a
native allow for pyjwt≥2.13.0, the override section in `pyproject.toml` becomes
a no-op but does no harm; it may be removed for cleanliness.

Re-check after any hermes-agent bump:

```bash
# from a worktree (NOT the root checkout).
uv lock                                     # must resolve without error
uv sync --frozen --all-extras
uv run pip-audit                            # must exit 0 with no vulnerabilities
uv run python -c "import jwt; print(jwt.__version__)"   # must print >=2.13.0
```

## Verify (locally, before relying on CI — rule 15)

Run from your worktree (never the root checkout / live-gateway venv). Each is a
separate command (exit code shown):

```bash
uv sync --frozen --all-extras                      # mirror the CI audit env
uv run pip-audit
# expect: "No known vulnerabilities found"  +  exit 0
```

Licence gate (must stay scoped to production deps):

```bash
uv export --frozen --no-dev --no-emit-project --no-hashes | sed 's/[<>=!~; ].*//' \
  | grep -vE '^$|^#|^-e'        # expect: audioop-lts  (extras NOT listed)
```

The local project (`hermes-voip`) is skipped by pip-audit ("not found on PyPI") —
that is expected, not a failure.

## Trigger

`supply-chain.yml` runs on push to `main` and on PRs that touch `pyproject.toml`,
`uv.lock`, or `supply-chain.yml` itself. To force a run, change one of those.

## When a NEW advisory fires (the gate goes red)

1. Read the `pip-audit` output: package, advisory ID, fix version.
2. If a fix version exists and resolves: bump the pin in `pyproject.toml`,
   `uv lock`, re-run the **Verify** block, open a PR.
3. If a resolver override is needed (package hard-pinned by a transitive dep):
   add `"pkg>=fix-version"` to `[tool.uv] override-dependencies` in
   `pyproject.toml`, run `uv lock`, verify the lock picks the patched version,
   run `uv run pip-audit`, write an ADR documenting the decision.
4. If truly blocked (e.g. override causes import failure or test breakage):
   confirm with test evidence, add a **per-ID** `--ignore-vuln` with an inline
   justification + an ADR entry — never a blanket package ignore.
5. Never widen the licence allowlist to copyleft to make the licence gate pass
   (rule 35); never disable the job.
