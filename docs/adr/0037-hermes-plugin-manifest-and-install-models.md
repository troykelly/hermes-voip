# ADR-0037: Hermes plugin manifest (`plugin.yaml`) + the two install models

- **Date:** 2026-06-18
- **Status:** Accepted
- **Deciders:** operator (`troy@…`) + agent session
- **Note:** numbered 0037 because ADR-0036 was concurrently taken by the DTMF
  lane (`0036-dtmf-sip-info-and-in-band.md`); both landed the same day.

## Context

The operator flagged (2026-06-18) that `hermes-voip` had **no proper Hermes plugin
manifest**: "where's our manifest?". The official Hermes plugin guide
(<https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin>) states that
every plugin ships a `plugin.yaml` manifest declaring
`name`/`version`/`description`/`author`/`provides_tools`/`provides_hooks`/`requires_env`,
and **`kind: platform`** for gateway adapters. We register a platform
(`ctx.register_platform("voip", …)` — ADR-0002), so we **are** a platform adapter.

What we actually shipped before this ADR:

- A pip / entry-point plugin (`[project.entry-points."hermes_agent.plugins"]
  hermes-voip = "hermes_voip"`) — the code-loading path.
- A **description-only stub** `plugin.yaml` (PR #109): `name` + `kind: platform` +
  `version` + `description`, with **no `provides_tools`, no `provides_hooks`, no
  `requires_env`, no `author`**.

The earlier #30 audit checked the runtime *source* but never the *guide*, so the manifest
gap went unnoticed. This ADR records the decision to ship a complete manifest and to
reconcile the install story to the guide's two canonical models — grounded in what the
**installed hermes-agent 0.16.0 runtime actually does** (rule 23/26/27), not what the guide
prose implies.

### Verified runtime behaviour (hermes-agent 0.16.0)

Established by `inspect.getsource` of the installed `hermes_cli.plugins` /
`hermes_cli.plugins_cmd` and by live `discover_and_load` / `hermes plugins …` runs against a
throwaway `HERMES_HOME` (never the live gateway). Captured in
[`docs/runbooks/0011-voip-enable-plugin.md`](../runbooks/0011-voip-enable-plugin.md):

1. **Two discovery sources, two metadata fates.** A **directory** plugin
   (`~/.hermes/plugins/<name>/plugin.yaml`) is parsed in full — `version`, `description`,
   `author`, `requires_env`, `provides_tools`, `provides_hooks`, `kind`. An **entry-point**
   plugin is registered as `PluginManifest(name=ep.name, source="entrypoint", …)` with
   **every metadata field empty** — the loader never reads a wheel-shipped `plugin.yaml` or
   package metadata. So a pip-only plugin can **never** surface its version / tool list in
   `hermes plugins list`, and `hermes plugins enable`/`list` (a filesystem-only scan) don't
   see it at all → `hermes plugins enable hermes-voip` fails "not installed or bundled".
2. **`kind: platform` is a recognised kind**
   (`{backend, exclusive, model-provider, platform, standalone}`), but the auto-load that
   `kind: platform` grants applies **only to *bundled* platforms**; a *user-directory*
   platform plugin is still gated by `plugins.enabled` (so it still needs
   `hermes plugins enable`).
3. **`requires_env` is a hard load gate _and_ an install prompt.** A missing `requires_env`
   var disables the plugin; `hermes plugins install` prompts for the missing ones and saves
   them to `.env`. The rich-entry fields the runtime reads are `name` / `description` /
   `url` / **`secret`** (the guide's `password:` is doc drift — the install path reads
   `secret`). `optional_env` is **not read by any 0.16.0 loader path** (no prompt, no gate).
4. **`provides_tools` is declarative only.** The in-session `/plugins` tool count is computed
   from the **actual `register(ctx)` result**, not from `provides_tools`; `hermes plugins
   list` has no tool-count column at all. Nothing in the runtime enforces that
   `provides_tools` matches reality — so a **drift-guard test** is the only thing that keeps
   it honest.

## Decision

1. **Ship a complete `plugin.yaml`.** `name: hermes-voip`, `version` (kept equal to
   `pyproject.toml`), `description`, `author`, `kind: platform`, `provides_tools` (the nine
   agent tools `register_voip_tools` registers), `provides_hooks: [pre_tool_call]`, and
   `requires_env` in the rich format.

2. **`requires_env` = only the genuinely-required SIP credentials**
   (`HERMES_SIP_HOST`, `HERMES_SIP_EXTENSION`, `HERMES_SIP_PASSWORD` — exactly
   `hermes_voip.plugin._REQUIRED_ENV`), with `secret: true` on the password. Because
   `requires_env` *gates loading*, putting an optional/defaulted var (`HERMES_SIP_PORT`,
   `HERMES_SIP_TRANSPORT`) or a provider-conditional key (`ELEVENLABS_API_KEY`,
   `DEEPGRAM_API_KEY`) there would **wrongly disable the plugin** for a valid default-port,
   TLS-only, or local-models-only configuration. Those go in `optional_env` (documentary)
   and are documented for real in the README + runbooks.

3. **One canonical manifest, shipped two ways** (the guide's two install models):
   - **pip / entry-point** (code source): the manifest also ships as **importable package
     data** — it physically lives at `src/hermes_voip/plugin.yaml` (so
     `importlib.resources.files("hermes_voip")/"plugin.yaml"` resolves it in editable *and*
     wheel installs; `hatch` declares it via `artifacts`). This makes the wheel
     self-describing and lets the directory copy be sourced from the installed package.
   - **directory install** (metadata + CLI affordance):
     `packaging/hermes-plugins/hermes-voip/` holds a **byte-identical** copy of the manifest
     plus an `__init__.py` that does `from hermes_voip.plugin import register`. A test
     asserts the two copies are identical (no drift). Copied to
     `~/.hermes/plugins/hermes-voip/`, this is what makes `hermes plugins list` show the
     plugin with its version + description and lets `hermes plugins enable hermes-voip`
     succeed.

4. **No double-registration.** When both the pip entry point and the directory plugin are
   present, the loader dedups by key (`manifest.key or manifest.name`, both `"hermes-voip"`)
   and the entry point wins, so the directory `__init__.py` is **not** re-imported — the
   platform registers **exactly once** (verified live: `voip` registered once, 9 tools, 1
   hook). The directory `__init__.py` does `from hermes_voip.plugin import register`, so it
   is the canonical guide model (a) layout — but note the **`hermes_voip` package must still
   be installed** (`pip install` / `uv sync`) for that import to resolve: this plugin's code
   ships in the wheel, not in the directory, so the directory is the metadata + enable
   affordance over the pip-installed package, **not** a package-less install. (There is no
   code-bearing copy in the directory by design — that would be a second implementation that
   could drift from the package; see the alternatives.)

5. **Drift guard in CI.** `tests/test_plugin_manifest.py` asserts (a) the manifest parses
   and ships as package data, (b) `provides_tools`/`provides_hooks` **exactly** match the
   tools/hooks `register(ctx)` actually registers, (c) `version` matches `pyproject.toml`,
   (d) `requires_env` equals `_REQUIRED_ENV` with `secret: true` on the password and no
   stray `password:` field, and (e) the manifest leaks no real host / extension identifiers
   (PUBLIC-repo invariant). This is the enforcement the runtime does not provide.

## Consequences

- **Operators get the canonical guide experience**: `hermes plugins list` shows
  `hermes-voip` with its version + description; `hermes plugins enable hermes-voip` works and
  writes `plugins.enabled`; the loaded `/plugins` view shows `9 tools, 1 hook`. The README +
  runbook 0011 are corrected to these **verified** commands (rule 27), keeping the
  hand-edit-`config.yaml` fallback as the no-stub alternative.
- **A documented runtime nuance remains** (it is a Hermes-runtime property, not ours, so we
  document rather than "fix"): the in-session `/plugins` *loaded* view shows the **entry
  point's empty version** (because the entry point wins the load dedup), while
  `hermes plugins list` shows the directory manifest's version. Both are surfaced honestly.
- **Manifest honesty is test-enforced**, not hoped for: adding a tool to `voip_tools`
  without updating `provides_tools` (or vice-versa) fails CI.
- **`optional_env` is documentary** under 0.16.0 (no prompt/gate); if a future Hermes starts
  reading it, the data is already correct. The README/runbooks are the operative source for
  the optional knobs today.

## Alternatives considered

- **Make `provides_tools` populate `/plugins` automatically** — impossible: the count comes
  from the real registration, and entry-point manifests carry no metadata. Rejected;
  drift-guard test instead.
- **Put PORT/TRANSPORT/provider keys in `requires_env`** so `hermes plugins install` prompts
  for them — rejected: `requires_env` gates loading, so this disables the plugin for valid
  configs that omit them. `optional_env` + README is the honest place.
- **Single manifest file only (no `src/` copy)** — rejected: editable installs resolve
  `importlib.resources` against `src/hermes_voip/`, so the package-data copy must physically
  live there; the directory-install copy is a separate path. The two-copy + identity-test
  approach keeps a single source of truth without breaking either install model.
- **Drop the directory `__init__.py` (metadata-only stub, as in #109)** — rejected: the task
  calls for the complete guide-model-(a) directory layout (`plugin.yaml` + `__init__.py`).
  The `__init__.py` re-imports the installed package's `register` (it is not a package-less
  install — the wheel must be present), and because the entry point wins the load dedup it is
  harmless alongside pip (no double registration). A metadata-only stub would diverge from
  the guide's stated directory shape for no benefit.
