# Runbook: Supply-chain advisory audit (CI `audit` job)

**What it is.** The CI advisory + licence gate for this plugin's dependencies
(AGENTS.md rule 35), in the `audit` job of
`.github/workflows/supply-chain.yml`. It (1) installs the dependency surface and
runs `pip-audit` (PyPI Advisory DB / OSV — free/OSS), (2) licence-checks the
default production deps with `pip-licenses` against a permissive-OSS allowlist,
and (3) separately reports the optional runtime extras surface with the same
allowlist. The WHY is ADR-0024. This runbook is the HOW.

> Public repo — no hosts/tokens/PII here; this gate touches only package metadata.

## What it does (current shape)

```yaml
- run: uv sync --frozen --all-extras        # install hermes+ml+media+webrtc too
- run: uv run pip-audit                     # audit the FULL installed surface, no suppression
- name: Licence allowlist (production deps) # default shipped surface only
- name: Licence allowlist (optional runtime extras) # additive extra-surface report
```

`--all-extras` matters twice. First, `pip-audit` audits the *installed*
environment, so a bare `uv sync --frozen` (no extras → only `audioop-lts`
installed) leaves every extra-only transitive dependency uninstalled and
reports a **false green**. Second, `uv export --frozen --no-dev` still lists only
the default production dependency set, so the licence gate now keeps that base
report and adds a separate `uv export --frozen --all-extras --no-dev
--no-emit-project --no-hashes` report for the optional runtime extras closure.
That additive report filters to the packages marked `# via hermes-voip` in the
export, i.e. the direct packages this repo declares in `[project.optional-
dependencies]`, so extra-only runtime packages cannot bypass the allowlist audit
while host/runtime transitive metadata stays outside this repo's declaration
boundary.

Today the base production export is still just `audioop-lts`; the optional
runtime-extras export is the direct hermes/ml/media/webrtc package set declared
by `hermes-voip`.

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

Licence gates (the base production report stays separate from the additive
optional-runtime-extras report):

```bash
uv export --frozen --no-dev --no-emit-project --no-hashes | sed 's/[<>=!~; ].*//' \
  | grep -vE '^$|^#|^-e'        # expect: audioop-lts  (extras NOT listed)

uv export --frozen --all-extras --no-dev --no-emit-project --no-hashes
# expect: a full requirements export whose direct hermes-voip declarations are
# the entries marked with `# via hermes-voip`
```

Both exports feed `pip-licenses --allow-only` in CI, but the second report first
extracts only the `# via hermes-voip` package names from the all-extras export.
That keeps the scope aligned with the backlog item: all declared optional runtime
extras are licence-gated, while transitive host/runtime dependencies are still
covered by `pip-audit` rather than a fragile string allowlist. The local project
(`hermes-voip`) is skipped by pip-audit ("not found on PyPI") — that is
expected, not a failure.

To mirror the two licence reports locally:

```bash
uv run pip-licenses --packages $(uv export --frozen --no-dev --no-emit-project --no-hashes \
  | sed 's/[<>=!~; ].*//' | grep -vE '^$|^#|^-e' | tr '\n' ' ') \
  --allow-only="MIT;MIT License;BSD License;BSD-2-Clause;BSD-3-Clause;Apache-2.0;Apache Software License;ISC;ISC License (ISCL);Python Software Foundation License;PSF-2.0;0BSD;BlueOak-1.0.0"

uv export --frozen --all-extras --no-dev --no-emit-project --no-hashes \
  | uv run python - <<'PY'
import sys

package_name: str | None = None
include_package = False
selected: list[str] = []


def flush() -> None:
    global package_name, include_package
    if package_name is not None and include_package:
        selected.append(package_name)


for raw_line in sys.stdin:
    line = raw_line.rstrip("\n")
    stripped = line.strip()
    if not stripped or stripped.startswith("# This file was autogenerated"):
        continue
    if not line.startswith(" "):
        flush()
        package_name = stripped
        include_package = False
        for separator in ("<", ">", "=", "!", "~", ";", " "):
            if separator in package_name:
                package_name = package_name.split(separator, 1)[0]
                break
        continue
    if "hermes-voip" in stripped:
        include_package = True

flush()
for package in sorted(set(selected)):
    print(package)
PY
# expect: aioice audioop-lts cryptography hermes-agent numpy onnxruntime opuslib
#         pyopenssl sherpa-onnx tokenizers websockets

uv run pip-licenses --packages aioice audioop-lts cryptography hermes-agent numpy \
  onnxruntime opuslib pyopenssl sherpa-onnx tokenizers websockets \
  --allow-only="MIT;MIT License;BSD License;BSD-2-Clause;BSD-3-Clause;Apache-2.0;Apache Software License;ISC;ISC License (ISCL);Python Software Foundation License;PSF-2.0;0BSD;BlueOak-1.0.0;Apache-2.0 OR BSD-3-Clause;BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0;Apache licensed, as found in the LICENSE file"
```

The second report is intentionally additive: it proves every direct optional
runtime extra declaration also satisfies the allowlist.

Current direct optional-runtime package set (verified from the all-extras export):

- `aioice`
- `audioop-lts`
- `cryptography`
- `hermes-agent`
- `numpy`
- `onnxruntime`
- `opuslib`
- `pyopenssl`
- `sherpa-onnx`
- `tokenizers`
- `websockets`

Current extra-report-only allowlist strings (all permissive metadata for the
packages above):

- `Apache-2.0 OR BSD-3-Clause`
- `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`
- `Apache licensed, as found in the LICENSE file`

If a dependency update changes any of those exact strings, re-run the local
mirror command above and update the workflow allowlist in the same change after
verifying the new string is still permissive-only.

## Trigger

`supply-chain.yml` runs on three event types:

1. **Push to `main`** — filtered to `pyproject.toml`, `uv.lock`,
   `.github/workflows/supply-chain.yml`.
2. **Pull requests** — same path filter.
3. **Daily schedule** — `cron: '7 6 * * *'` (06:07 UTC every day).
   The off-:00 minute avoids fleet pile-up on GitHub's shared runners.
   Schedule events carry no path context, so the path filters above do not apply;
   the `audit` job has no `if:` condition, so it runs unconditionally on every
   trigger including the daily cron.

**Why a schedule?** Path-filtered push/PR events only fire when a tracked file
changes.  A CVE disclosed against an unchanged pinned dependency (e.g. the
PYSEC-2026-175..179 / pyjwt class of issue) is invisible to CI until the next
dependency-bump PR.  The daily cron closes this gap: new advisories are surfaced
within 24 hours regardless of repo activity.

**To force an ad-hoc run** without changing a tracked file: trigger the workflow
manually from the GitHub Actions UI (Actions → supply-chain → Run workflow) or
push any change to one of the path-filtered files.

## Triaging a scheduled-run failure

When the `audit` job fails on a scheduled run (not triggered by a PR or push),
the failure appears in GitHub Actions → supply-chain → the failed run.  The
workflow run will show `schedule` as the trigger event.

Triage steps:

1. Open the failed run; read the `uv run pip-audit` step output to identify:
   - Package name
   - Advisory ID(s) (PYSEC-YYYY-NNN / CVE-YYYY-NNNNN)
   - Severity and fix version

2. Verify it is a real new advisory (not a re-scan of a known-suppressed issue):
   confirm the advisory ID is not already listed in a `--ignore-vuln` flag with a
   documented justification in `supply-chain.yml`.

3. Reproduce locally (from a worktree, not the root checkout):
   ```bash
   uv sync --frozen --all-extras
   uv run pip-audit
   ```

4. Follow the **When a NEW advisory fires** section below to resolve.

5. Once resolved and merged to `main`, the next scheduled run (or a manually
   triggered run) must exit green before the issue is considered closed.

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
