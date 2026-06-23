# Runbook: cut a `hermes-voip` release

**What it is.** The procedure to cut a versioned release of the `hermes-voip`
Hermes plugin: bump the single-sourced version, prove the version-sync gate green,
update the changelog, tag the commit, build and verify the wheel, and make the
release available. This is the operational HOW; the WHY of the single-source
decision is recorded inline in `src/hermes_voip/__init__.py` and the version-sync
tests in `tests/test_plugin_manifest.py`.

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
There is **no published package registry** for this project today: installation is
**from the Git checkout** via `uv sync` (see README Step 1), not `pip install
hermes-voip` from PyPI. Activation also needs the one-time manifest-directory copy
under `~/.hermes/plugins/hermes-voip/` so `hermes plugins enable hermes-voip` sees
it — see [0011-voip-enable-plugin.md](0011-voip-enable-plugin.md).

So a "release" here is a **tagged, buildable Git commit** plus a verified wheel
artifact — consumers install that tag. Publishing the wheel to an external index
(PyPI) is **not** part of this process and is propose-only (see "Automated publish"
below); standing up that account/token is operator-gated infra (AGENTS.md rules
40/41).

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

3. **Run the version-sync gate** (and the full suite):

   ```bash
   uv sync --frozen   # re-installs editable so importlib.metadata sees X.Y.Z
   uv run pytest tests/test_plugin_manifest.py -q
   uv run pytest -q   # full suite
   ```

   The four version-sync guards listed above must pass. `uv sync` is required after
   the bump so the editable install's metadata reflects `X.Y.Z` (otherwise
   `__version__` reports the previously-installed number and the derivation guard
   fails — a useful tripwire, but run the sync).

4. **Update `CHANGELOG.md`**: move the `[Unreleased]` entries into a new
   `## [X.Y.Z] - YYYY-MM-DD` section, leave a fresh empty `[Unreleased]`, and fix
   the compare/tag links at the bottom.

5. **Commit** (Conventional Commit + the AI co-author trailer) on a worktree-lane
   branch, open a PR, run the full local gate (AGENTS.md rule 15), and merge.

6. **Tag the merged commit** `vX.Y.Z` and push the tag:

   ```bash
   git tag -a vX.Y.Z -m "hermes-voip X.Y.Z"
   git push origin vX.Y.Z
   ```

   (Use an annotated tag. The semver tag is what consumers pin.)

7. **Build the distribution artifacts** from the tagged commit, into a
   worktree-local `dist/` (AGENTS.md rule 10 — never `/tmp`):

   ```bash
   uv build --out-dir dist
   # → Successfully built dist/hermes_voip-X.Y.Z.tar.gz
   #   Successfully built dist/hermes_voip-X.Y.Z-py3-none-any.whl
   ```

8. **Verify the wheel** carries the right version, installs cleanly, imports, and
   ships the manifest as package data:

   ```bash
   uv venv /tmp/relcheck && WHL=$(ls dist/*.whl)
   uv pip install --python /tmp/relcheck/bin/python "$WHL"
   /tmp/relcheck/bin/python -c "
   import hermes_voip
   from importlib.resources import files
   print('version:', hermes_voip.__version__)               # → X.Y.Z (from wheel metadata)
   print('register:', callable(hermes_voip.register))       # → True
   print('manifest packaged:', files('hermes_voip').joinpath('plugin.yaml').is_file())  # → True
   "
   rm -rf /tmp/relcheck dist   # clean scratch (AGENTS.md rule 10)
   ```

   `version: X.Y.Z` here proves the single-source works for a REAL wheel install
   (not just the editable dev install): the wheel's `METADATA` `Version:` is read
   by `importlib.metadata`, with no source edit to `__init__.py`. `manifest
   packaged: True` proves `plugin.yaml` is in the wheel (hatchling `artifacts`
   declaration, ADR-0037).

9. **Make the release available.** Consumers install from the tag:

   ```bash
   uv add "hermes-voip @ git+https://github.com/troykelly/hermes-voip.git@vX.Y.Z"
   ```

   Optionally attach the built wheel + sdist to a GitHub Release for the tag
   (`gh release create vX.Y.Z dist/*` — does not require any external registry or
   token beyond the repo's own `gh` auth). Then follow the activation steps in
   [0011-voip-enable-plugin.md](0011-voip-enable-plugin.md).

## Verify a cut release

```bash
git tag --list 'v*'                       # the new vX.Y.Z is present
git show vX.Y.Z --stat | head             # points at the bumped commit
# in a clean venv with the wheel installed (step 8):
python -c "import hermes_voip; assert hermes_voip.__version__ == 'X.Y.Z'"
```

## Roll back / re-cut

A release is a tag + an artifact; nothing is provisioned. To roll back, do NOT
move a published tag (consumers may have pinned it) — instead cut a new patch
release (`X.Y.Z+1`) with the fix and a changelog entry. If a tag was pushed in
error and provably unconsumed, delete it locally and on the remote
(`git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z`) and re-cut. Build
artifacts under `dist/` are disposable — rebuild from the tag at any time with
`uv build`.

## Automated publish (PROPOSE-ONLY — not built)

Publishing the wheel to **PyPI** (or any external index) on tag would need an
external account and a scoped API token — infrastructure this repo does not stand
up without explicit operator approval recorded in an ADR (AGENTS.md rules 40/41).
It is therefore **not** part of this process today. If the operator wants it:

1. Record a Proposed ADR (`docs/adr/`) choosing the index (PyPI), the trusted-
   publishing mechanism (GitHub OIDC → PyPI, so no long-lived token in CI), and the
   scope.
2. On approval, add a tag-triggered GitHub Actions job that runs `uv build` and
   `uv publish` (or `pypa/gh-action-pypi-publish` via OIDC), and a runbook for the
   PyPI project + trusted-publisher binding (rule 42).

Until then, the manual steps above are the release process, and the backlog item
"automate release publish (PyPI trusted publishing)" tracks the proposal.
