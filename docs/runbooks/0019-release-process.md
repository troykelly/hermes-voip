# Runbook: cut a `hermes-voip` release

**What it is.** The procedure to cut a versioned release of the `hermes-voip`
Hermes plugin: bump the single-sourced version, prove the version-sync gate green,
update the changelog, tag the commit, and push the tag — at which point the
`publish.yml` GitHub Actions workflow builds + verifies the artifacts, creates the
GitHub Release, and publishes to PyPI (see "Automated publish on tag" below). This
is the operational HOW; the WHY of the single-source decision is recorded inline in
`src/hermes_voip/__init__.py` and the version-sync tests in
`tests/test_plugin_manifest.py`.

> Public repo — no hosts/tokens/PII here. The version is a public package number.

## Version single-sourcing (read this first)

The package version has **one canonical home**: `pyproject.toml [project].version`.
Two derived copies track it and are pinned equal by the test suite, so they cannot
silently drift:

- `hermes_voip.__version__` derives at import time from the installed distribution
  metadata — `importlib.metadata.version("hermes-voip")` — which the build backend
  (hatchling) populates from `pyproject.toml [project].version`. It is NOT a
  hand-maintained literal. (Source-tree-without-install fallback: `0+unknown`, a
  PEP 440 local version that signals "no metadata"; never hit in an installed
  deployment — wheel, editable, or directory-install — all of which carry metadata.)
- `plugin.yaml version` (the Hermes manifest, shipped at
  `src/hermes_voip/plugin.yaml` and byte-identically at
  `packaging/hermes-plugins/hermes-voip/plugin.yaml`) is a literal string that the
  test suite asserts equals `pyproject.toml`.

The guards live in `tests/test_plugin_manifest.py`:

- `test_manifest_version_matches_pyproject` — `plugin.yaml` == `pyproject`.
- `test_package_version_matches_pyproject` — `__version__` == `pyproject`.
- `test_package_version_is_derived_from_installed_metadata` — `__version__` ==
  `importlib.metadata.version("hermes-voip")` (derivation is live).
- `test_init_source_single_sources_the_version` — `__init__.py` references
  `importlib.metadata` and assigns NO bare `__version__ = "X.Y.Z"` literal.

**Consequence: a release is a single edit** — bump `pyproject.toml`, then update
the two literal copies that the suite checks (`plugin.yaml` ×2). `__version__`
follows the install metadata automatically with no source edit.

## How the plugin is distributed (the real mechanism)

`hermes-voip` is a **pip / entry-point plugin** (entry-point group
`hermes_agent.plugins`, declared in `pyproject.toml` as
`[project.entry-points."hermes_agent.plugins"] hermes-voip = "hermes_voip"`). The
Hermes runtime scans that group at startup and calls `hermes_voip.register(ctx)`.
The current documented consumer install path is **from the Git checkout** via
`uv sync` (see README Step 1) — that is the install model the README and ADR-0037
describe. As of the automated publish flow below, pushing a `vX.Y.Z` tag ALSO
publishes the wheel + sdist to the **PyPI** project `hermes-voip` (so once the
first tag fires, `uv add hermes-voip` / `pip install hermes-voip` resolve it too).
Activation also needs the one-time manifest-directory copy under
`~/.hermes/plugins/hermes-voip/` so `hermes plugins enable hermes-voip` sees it —
see [0011-voip-enable-plugin.md](0011-voip-enable-plugin.md).

So a "release" is a **tagged, buildable Git commit** whose tag push drives the
`publish.yml` workflow: build + verify the artifacts, attach them to a GitHub
Release, and publish them to PyPI via OIDC Trusted Publishing (no stored token).

## Cut a release (step by step)

All commands run from a worktree lane (AGENTS.md rule 8), never the root checkout.
Replace `X.Y.Z` with the target version (e.g. `0.1.0`).

1. **Bump the canonical version** in `pyproject.toml`:

   ```toml
   [project]
   version = "X.Y.Z"
   ```

2. **Update the two literal manifest copies** to the same `X.Y.Z` (they must stay
   byte-identical — the suite checks both):

   ```bash
   # edit the version: line in BOTH files to "X.Y.Z"
   #   src/hermes_voip/plugin.yaml
   #   packaging/hermes-plugins/hermes-voip/plugin.yaml
   diff src/hermes_voip/plugin.yaml packaging/hermes-plugins/hermes-voip/plugin.yaml
   # → no output (identical)
   ```

   `hermes_voip.__version__` needs NO edit — it derives from install metadata.

3. **Sync the lockfile, then run the version-sync gate**:

   ```bash
   uv lock            # bumps hermes-voip's OWN version in uv.lock to X.Y.Z
   uv sync --frozen   # re-installs editable so importlib.metadata sees X.Y.Z
   uv run pytest tests/test_plugin_manifest.py -q
   ```

   `uv lock` is required after the pyproject bump: `uv.lock` pins the project's own
   version, so `uv sync --frozen` otherwise fails with "lockfile out of date". The
   diff must be version-only — `git diff uv.lock` shows just the `hermes-voip`
   package version changing, no dependency churn. `uv sync` then makes the editable
   install's metadata reflect `X.Y.Z` (otherwise `__version__` reports the
   previously-installed number and the derivation guard fails — a useful tripwire).
   The four version-sync guards must pass. Run the FULL suite (`uv run pytest -q`)
   only AFTER step 4 — the CHANGELOG and the version-pin ratchet test (below) must be
   updated first, or the full suite fails.

4. **Update `CHANGELOG.md`**: move the `[Unreleased]` entries into a new
   `## [X.Y.Z] - YYYY-MM-DD` section, leave a fresh empty `[Unreleased]`, and fix
   the compare/tag links at the bottom (`[Unreleased]: …/compare/vX.Y.Z...HEAD` plus
   a new `[X.Y.Z]: …/compare/vPREV...vX.Y.Z`).

   **Version-pin ratchet test.** `tests/test_version_<prev>.py` is a per-release
   ratchet that hard-asserts the NEW version across `pyproject.toml`, both
   `plugin.yaml` copies, and the CHANGELOG `## [X.Y.Z]` section + compare links.
   Update it in lockstep: rename it to the new version (e.g.
   `test_version_013.py` → `test_version_020.py`), bump `_EXPECTED_VERSION` and the
   `_0XY` test-function names, and update the changelog assertions. It fails red on
   the pre-bump tree and turns green once steps 1–2 and this step are complete. (A
   backlog item tracks generalising it to derive the version from `pyproject.toml`,
   which would drop this rename.)

5. **Commit** (Conventional Commit + the AI co-author trailer) on a worktree-lane
   branch, open a PR, run the full local gate (AGENTS.md rule 15), and merge.

6. **(Optional) Verify the build locally** before tagging — the workflow runs the
   identical checks, but this catches a bad build without spending a tag. Build into
   a worktree-local `dist/` (AGENTS.md rule 10 — never `/tmp`) and smoke-install:

   ```bash
   uv build --out-dir dist
   uv venv dist/.relcheck && WHL=$(ls dist/*.whl)
   uv pip install --python dist/.relcheck/bin/python "$WHL"
   dist/.relcheck/bin/python -c "
   import hermes_voip
   from importlib.resources import files
   print('version:', hermes_voip.__version__)               # → X.Y.Z (from wheel metadata)
   print('register:', callable(hermes_voip.register))       # → True
   print('manifest packaged:', files('hermes_voip').joinpath('plugin.yaml').is_file())  # → True
   "
   rm -rf dist   # clean scratch (AGENTS.md rule 10)
   ```

   `version: X.Y.Z` proves the single-source works for a REAL wheel install (the
   wheel's `METADATA` `Version:` read by `importlib.metadata`); `manifest packaged:
   True` proves `plugin.yaml` is in the wheel (hatchling `artifacts`, ADR-0037).
   These are the same three assertions the `publish.yml` build job runs.

7. **Tag the merged commit** `vX.Y.Z` and push the tag — this is what triggers the
   automated publish:

   ```bash
   git tag -a vX.Y.Z -m "hermes-voip X.Y.Z"
   git push origin vX.Y.Z
   ```

   Use an annotated tag. The semver tag is what consumers pin AND the
   `publish.yml` trigger (`on.push.tags: v[0-9]+.[0-9]+.[0-9]+`, plus a `-*`
   suffix pattern for pre-releases like `v1.2.3-rc1`). The tag's version (minus the
   leading `v`) MUST equal `pyproject.toml [project].version` or the build job's
   guard fails the run (see below). For a pre-release, append a `-<suffix>` so the
   GitHub Release is marked `--prerelease`.

8. **Watch the publish run** to green (`gh run watch` or the Actions tab). The
   three jobs are detailed in "Automated publish on tag" below; "Verify a publish"
   confirms the GitHub Release + PyPI artifact landed.

## Automated publish on tag

The workflow `.github/workflows/publish.yml` runs on every pushed tag matching
`v[0-9]+.[0-9]+.[0-9]+` (and the `-*` pre-release variant). It uses **no stored
secrets**: the GitHub Release uses the automatic `GITHUB_TOKEN`; PyPI uses GitHub
**OIDC Trusted Publishing** (no token at all). A `concurrency` group
(`publish-${{ github.ref }}`, no cancel-in-progress) serializes two tag pushes so
they cannot race the publish steps. Top-level permissions are read-only; each job
elevates only what it needs.

Three jobs:

1. **`build`** (`ubuntu-latest`, default read-only token):
   - checks out the pushed tag, installs `uv` (pinned) + Python from
     `.python-version`, `uv sync --frozen`;
   - **tag↔version guard:** strips the leading `v` and asserts it EXACTLY equals
     `pyproject.toml [project].version` — a mismatch fails the run with a clear
     message (you cannot publish a tag that disagrees with the source version);
   - `uv build --out-dir dist` (wheel + sdist);
   - **wheel-smoke** (mirrors the `wheel-smoke` job in `gate.yml`): installs the
     freshly built wheel into a clean venv and asserts `hermes_voip.__version__` ==
     pyproject version and != `0+unknown`, that `plugin.yaml` is inside the wheel,
     and that the `hermes_agent.plugins` entry point resolves to a callable
     `register`;
   - generates `dist/SHA256SUMS` over the wheel + sdist, extracts this version's
     `## [X.Y.Z]` section from `CHANGELOG.md` into `dist/RELEASE_NOTES.md`, and
     uploads `dist/` as the `dist` artifact.
2. **`github-release`** (`needs: build`; `permissions: contents: write`):
   downloads the artifact and runs
   `gh release create <tag> <wheel> <sdist> dist/SHA256SUMS --title <tag>
   --notes-file dist/RELEASE_NOTES.md`, adding `--prerelease` when the tag contains
   a `-` (e.g. `v1.2.3-rc1`). Authenticated by the automatic `GITHUB_TOKEN`.
3. **`pypi-publish`** (`needs: build`; `environment: pypi`;
   `permissions: id-token: write`): downloads the artifact, moves ONLY the wheel +
   sdist into a clean dir (so `SHA256SUMS`/`RELEASE_NOTES.md` are never uploaded),
   and publishes via `pypa/gh-action-pypi-publish` with `attestations: true` and
   **no `password:`** — the action mints a short-lived credential from the GitHub
   OIDC identity. This job is INDEPENDENT of `github-release` (both only
   `needs: build`), so a not-yet-configured PyPI Trusted Publisher fails ONLY this
   job and still leaves the GitHub Release published.

**Why `pypa/gh-action-pypi-publish` and not `uv publish`?** `uv publish` does
support OIDC Trusted Publishing, but it does **not** generate PEP 740 build-
provenance attestations; the PyPA action generates and uploads them by default
(`attestations: true` makes it explicit). We keep `uv` as the only *builder*
(`uv build` in the `build` job — AGENTS.md rule 38) and use the PyPA action solely
to *upload* the artifacts with attestations. Revisit if/when `uv publish` gains
attestation generation.

### Tag↔version guard (the exact assertion)

```bash
TAG_VERSION="${TAG#v}"   # TAG = github.ref_name, e.g. v0.1.0 → 0.1.0
PYPROJECT_VERSION=$(uv run python -c \
  "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
[ "$TAG_VERSION" = "$PYPROJECT_VERSION" ] || { echo "FAIL: tag != pyproject"; exit 1; }
```

## PyPI Trusted Publisher (one-time setup)

Trusted Publishing binds the GitHub repo + workflow + environment to the PyPI
project, so CI uploads with a short-lived OIDC credential and **no API token ever
exists** (rules 34/35: nothing to store, leak, or rotate). The operator has
configured this on PyPI with the EXACT values below — they MUST match the workflow
or PyPI rejects the upload:

| Field             | Value          |
| ----------------- | -------------- |
| PyPI project name | `hermes-voip`  |
| Owner             | `troykelly`    |
| Repository        | `hermes-voip`  |
| Workflow filename | `publish.yml`  |
| Environment name  | `pypi`         |

Because the PyPI project does not exist yet (nothing has been published), this was
registered as a **pending publisher** (PyPI → *Your projects* → *Publishing* →
*Add a pending publisher*, or *Manage* → *Publishing* on an existing project). The
first successful `pypi-publish` run **creates** the `hermes-voip` project on PyPI
and converts the pending publisher into the project's permanent Trusted Publisher.
No secrets are added to the GitHub repo or the `pypi` environment — the only
GitHub-side requirement is the `pypi` environment existing (the workflow's
`environment: pypi` plus the operator-side Trusted Publisher binding); the
`id-token: write` permission is granted in the workflow, not configured on GitHub.

If any of the five values above changes (e.g. the workflow is renamed or the
environment changes), the Trusted Publisher entry on PyPI must be updated to match,
or publishing fails with an OIDC trust error.

## Verify a publish

```bash
git tag --list 'v*'                       # the new vX.Y.Z is present
git show vX.Y.Z --stat | head             # points at the bumped commit
gh run list --workflow publish.yml -L 5   # the tag's run is green (all 3 jobs)

# GitHub Release exists with the three assets + (for an rc tag) prerelease flag:
gh release view vX.Y.Z --json tagName,isPrerelease,assets \
  --jq '{tag:.tagName, prerelease:.isPrerelease, assets:[.assets[].name]}'
# → assets include hermes_voip-X.Y.Z-py3-none-any.whl, .tar.gz, SHA256SUMS

# PyPI has the version (once pypi-publish is green):
curl -fsSL https://pypi.org/pypi/hermes-voip/X.Y.Z/json | \
  python -c "import sys,json; d=json.load(sys.stdin); print(d['info']['version'])"
# → X.Y.Z

# A clean install from PyPI imports and reports the right version:
uv venv dist/.verify && uv pip install --python dist/.verify/bin/python "hermes-voip==X.Y.Z"
dist/.verify/bin/python -c "import hermes_voip; assert hermes_voip.__version__ == 'X.Y.Z'"
rm -rf dist
```

## Roll back / re-cut

A release is a tag + a GitHub Release + a PyPI artifact. **PyPI is immutable** — a
published version can NEVER be re-uploaded or un-published, only **yanked** (it
stays installable by exact pin but is hidden from new resolutions). So the primary
roll-back is forward: cut a new patch release (`X.Y.Z+1`) with the fix and a
changelog entry, and `pip`-yank the bad version if it is actively harmful:

```bash
# Yank a bad PyPI release (reversible: `--unyank` restores it). PyPI has no CLI for
# this; do it in the project's *Manage* → *Releases* UI, or:
uvx twine yank hermes-voip X.Y.Z       # if twine is available; else use the PyPI UI
```

To delete a GitHub Release + its tag (e.g. a tag pushed in error, before anyone
pinned it):

```bash
gh release delete vX.Y.Z --yes --cleanup-tag     # removes the Release AND the tag
# or, if the Release was not created: git push origin :refs/tags/vX.Y.Z
```

Deleting the GitHub Release does **not** remove the PyPI artifact — if the
`pypi-publish` job already ran, yank it on PyPI as above. Build artifacts under
`dist/` are disposable; the workflow rebuilds them from the tag on any re-push of a
*new* tag (a tag already consumed by a green run will refuse to re-publish to PyPI
because the version already exists — which is the correct, safe behaviour).

## Re-running / recovering a failed publish

- **`build` failed (guard or wheel-smoke):** fix the cause on `main`, then move the
  tag is NOT allowed (consumers may have pinned it). Instead bump to the next
  version and tag that.
- **`github-release` failed but `pypi-publish` succeeded (or vice versa):** the
  jobs are independent. Re-run just the failed job from the Actions UI
  (`gh run rerun <run-id> --job <job-id>`). `pypi-publish` is idempotent in the
  safe direction — it errors if the version is already on PyPI, so a re-run after a
  successful PyPI upload is a no-op failure, not a double-publish.
