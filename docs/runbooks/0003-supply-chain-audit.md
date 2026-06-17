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
- run: uv run pip-audit \                    # audit the FULL installed surface
    --ignore-vuln PYSEC-2026-175 \           # pyjwt 2.12.1 advisories,
    --ignore-vuln PYSEC-2026-176 \           # fixed in 2.13.0 but BLOCKED upstream
    --ignore-vuln PYSEC-2026-177 \           # (hermes-agent==0.16.0 hard-pins
    --ignore-vuln PYSEC-2026-178 \           #  PyJWT[crypto]==2.12.1). Per-ID so
    --ignore-vuln PYSEC-2026-179             #  any OTHER advisory still fails red.
# licence gate (unchanged): scoped to PRODUCTION deps via `uv export --no-dev`
```

`--all-extras` matters: `pip-audit` audits the *installed* environment, so a bare
`uv sync --frozen` (no extras → only `audioop-lts` installed) leaves every
extra-only transitive dependency uninstalled and reports a **false green**. The
licence gate is unaffected — it reads the lock via `uv export --frozen --no-dev`,
which ignores extras, so it stays scoped to production deps (today: just
`audioop-lts`).

## The pyjwt suppression — why, and when it goes away

`pyjwt 2.12.1` (transitive via the `hermes` extra) carries 5 advisories
(PYSEC-2026-175 … -179), fixed in 2.13.0. We **cannot** bump it:
`hermes-agent==0.16.0` hard-pins `PyJWT[crypto]==2.12.1`, and forcing
`pyjwt>=2.13.0` makes `uv lock` unsatisfiable. The fix is upstream
(hermes-agent must relax the pin). Until then the 5 IDs are ignored **by ID** so
the gate stays green-but-actionable.

**Drop the ignores when hermes-agent relaxes the pin.** On every hermes-agent
bump, re-check:

```bash
# from a worktree (NOT the root checkout). Does the new hermes-agent still pin ==2.12.1?
uv sync --frozen --all-extras
uv run python -c "import importlib.metadata as m; print([r for r in (m.requires('hermes-agent') or []) if 'jwt' in r.lower()])"
```

If that no longer prints `==2.12.1`, add `"pyjwt>=2.13.0"` to the `hermes` extra
in `pyproject.toml`, run `uv lock`, confirm the lock moves pyjwt to ≥2.13.0, and
**delete the five `--ignore-vuln` lines** from the workflow + the suppression note
in ADR-0024.

## Verify (locally, before relying on CI — rule 15)

Run from your worktree (never the root checkout / live-gateway venv). Each is a
separate command (exit code shown):

```bash
uv sync --frozen --all-extras                      # mirror the CI audit env
uv run pip-audit \
  --ignore-vuln PYSEC-2026-175 --ignore-vuln PYSEC-2026-176 \
  --ignore-vuln PYSEC-2026-177 --ignore-vuln PYSEC-2026-178 \
  --ignore-vuln PYSEC-2026-179
# expect: "No known vulnerabilities found, 8 ignored"  +  exit 0
```

Prove the ignore is **scoped** (not a blanket mute) — omit one ID and it must fail:

```bash
uv run pip-audit \
  --ignore-vuln PYSEC-2026-175 --ignore-vuln PYSEC-2026-176 \
  --ignore-vuln PYSEC-2026-177 --ignore-vuln PYSEC-2026-178
# expect: "Found ... known vulnerabilities"  +  exit 1
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
3. If blocked upstream (like pyjwt): confirm with `uv lock` that the bump is
   unsatisfiable, file/locate the upstream issue, and add a **per-ID**
   `--ignore-vuln` with an inline justification + an entry in ADR-0024's
   suppression note — never a blanket package ignore.
4. Never widen the licence allowlist to copyleft to make the licence gate pass
   (rule 35); never disable the job.
