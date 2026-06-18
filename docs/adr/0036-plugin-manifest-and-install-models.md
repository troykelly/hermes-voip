# ADR-0036: Hermes plugin manifest (plugin.yaml) and the two install models

- **Date:** 2026-06-18
- **Status:** Accepted
- **Deciders:** agent session (plugin-manifest lane) — operator-directed
- **Amends:** ADR-0002 (plugin registration) — adds the manifest + install-model
  detail that the original ADR deferred to "a pip entry point". Supersedes the
  PR #109 description-only `plugin.yaml` stub.

## Context

The operator flagged (2026-06-18) that hermes-voip had **no proper plugin manifest**:
"where's our manifest?". The official Hermes plugin guide
(`hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin`) states that every
plugin ships a `plugin.yaml` carrying `name` / `version` / `description` / `author` /
`provides_tools` / `provides_hooks` / `requires_env`, plus `kind: platform` for gateway
adapters. We register a platform (`ctx.register_platform("voip", …)`), so we **are** a
gateway adapter. The #30 audit had checked the runtime source but not the guide; PR #109
shipped only a **description-only** stub (`name` / `kind` / `version` / `description`,
nothing else). The guide lists two supported install models:

- **(a) directory install** — `~/.hermes/plugins/<name>/` holding `plugin.yaml` +
  `__init__.py` with `register(ctx)`.
- **(b) pip + entry-point** — `[project.entry-points."hermes_agent.plugins"]` (what we
  already ship).

The binding constraint is a **rule-27** one: the manifest and the documented install
steps must describe what the runtime *actually does*, not what the guide prose implies.
Three facts, verified against the installed `hermes-agent==0.16.0` source
(`hermes_cli/plugins.py`, `hermes_cli/plugins_cmd.py`; the read files were confirmed
byte-identical to the runtime):

1. **An entry-point plugin can NEVER surface manifest metadata.**
   `_scan_entry_points` builds `PluginManifest(name=ep.name, source="entrypoint",
   path=ep.value, key=ep.name)` — `version` / `description` / `author` /
   `provides_tools` take dataclass defaults (empty). No wheel `plugin.yaml`, `PKG-INFO`,
   or `importlib.metadata` version is ever read for the entry-point case. So a pip-only
   plugin shows a **blank** row (and in fact does not show at all — see #2).
2. **`hermes plugins list` and `hermes plugins enable` are filesystem-only.** `cmd_list`
   → `_discover_all_plugins` and `cmd_enable` → `_plugin_exists` scan only the bundled +
   `~/.hermes/plugins/` directories; neither consults entry points. A pip-only plugin
   therefore does not appear in `plugins list`, and `hermes plugins enable hermes-voip`
   exits 1 with "Plugin 'hermes-voip' is not installed or bundled." The runtime *load*
   gate is separate: `plugins.enabled` in `config.yaml`, which the entry-point loader
   honours.
3. **The rich `requires_env` entry field is `secret`, not `password`.** The install
   prompt (`plugins_cmd.py`) reads `name` / `description` / `url` / `secret`; there is no
   `password` field anywhere. Both the plain-string and rich-dict forms are accepted, and
   the prompt reads **only `requires_env`** — it does **not** prompt for `optional_env`.

## Decision

Ship one **canonical** `plugin.yaml` and reconcile both install models honestly.

**The manifest** (`packaging/hermes-plugins/hermes-voip/plugin.yaml`, mirrored as wheel
package data at `src/hermes_voip/plugin.yaml`):

- `name: hermes-voip`, `version` kept in lock-step with `pyproject.toml`, `description`,
  `author`, **`kind: platform`**.
- `provides_tools`: the nine tools `register()` registers via `ctx.register_tool` —
  `hang_up`, `hold_call`, `resume_call`, `list_registrations`, `place_call`,
  `report_call_result`, `send_dtmf`, `open_entry`, `transfer_blind`.
- `provides_hooks: [pre_tool_call]` — the per-call privilege-clamp gate.
- `requires_env` (gates loading; prompted by `hermes plugins install`): only the
  genuinely-required SIP credentials — `HERMES_SIP_HOST`, `HERMES_SIP_EXTENSION`,
  `HERMES_SIP_PASSWORD` (the password marked **`secret: true`**). Listing an
  optional/defaulted var here would wrongly disable the plugin when it is absent.
- `optional_env` (documentation only — the loader does not prompt for it): the
  non-gating knobs (`HERMES_SIP_PORT`/`TRANSPORT`, the model dirs, the provider
  selectors, and `ELEVENLABS_API_KEY` / `DEEPGRAM_API_KEY` marked `secret: true`, each
  required only when its provider is selected).

PUBLIC-repo invariant: the manifest carries env-var **names** and **fake** examples
(`pbx.example.test`, ext `1000`) only — never a real host/extension/password/IP/token.

**Both install models, canonically:**

- **pip / entry-point (the package source).** `pip install` / `uv sync` installs the
  code; the manifest ships inside the wheel as importable package data
  (`hatchling … artifacts = ["src/hermes_voip/plugin.yaml"]`) so the package is
  self-describing and the directory copy can be sourced from the install.
- **directory install (what the CLI reads).** `packaging/hermes-plugins/hermes-voip/`
  contains `plugin.yaml` **and** an `__init__.py` that does
  `from hermes_voip.plugin import register` (re-export only — no second
  implementation). Copying that directory to `~/.hermes/plugins/hermes-voip/` makes
  `hermes plugins list` / `/plugins` show `hermes-voip v0.0.0 (9 tools, 1 hooks)` and
  makes `hermes plugins enable hermes-voip` succeed; the plugin still loads exactly once
  (from the entry point — the directory `register` is the same callable).

A drift-guard test (`tests/test_plugin_manifest.py`) asserts `provides_tools` /
`provides_hooks` equal the registered set, version parity with `pyproject.toml`,
`secret: true` on the password, the byte-identity of the two manifest copies, and that
the manifest ships in the built wheel.

## Consequences

- **Operators get the canonical experience.** With the directory install, `hermes
  plugins list` / `enable` / `/plugins` work as the guide describes, including the tool
  count. Without it, the plugin still loads via `plugins.enabled` — documented as the
  no-stub path.
- **The directory install is canonical for CLI discoverability; pip is the code
  source.** This is forced by the runtime (facts #1/#2), not a preference — entry-point
  metadata is unreachable, so the directory manifest is the only thing the CLI can read.
- **Two physical copies of the manifest** (`src/…/plugin.yaml` and `packaging/…`), kept
  honest by a byte-identity test. Editing one without the other fails CI.
- **A drift between a registered tool and `provides_tools` fails CI** — the manifest can
  no longer silently misrepresent the tool surface (the operator's stated worry).
- **New dev dependency:** `types-pyyaml` (stubs for the manifest-parsing test) — dev
  group only, out of the runtime/licence/audit surface.
- **Maintenance:** bumping the package version, or adding/removing an agent tool, now
  also touches `plugin.yaml` (both copies) — enforced, not optional.

## Alternatives considered

| Alternative | Rejected because |
| ----------- | ---------------- |
| Ship the manifest as wheel package data ONLY (pip path), expecting `hermes plugins list` to surface it | Verified false (fact #1): `_scan_entry_points` never reads wheel/package metadata, so the pip path can't populate version/tools/`list` at all. |
| Keep the PR #109 description-only stub | Misses `provides_tools` / `provides_hooks` / `requires_env` / `author` and `kind` semantics — the operator's whole point; `/plugins` would show `(0 tools)`. |
| Single manifest file, symlinked between `packaging/` and `src/` | Brittle across `git`/`cp`/editable-vs-wheel installs; an operator `cp -r` of the dir would copy a dangling symlink. Two copies + a byte-identity test is simpler and robust. |
| Put `HERMES_SIP_PORT`/`TRANSPORT` + provider keys in `requires_env` (as the task brief listed them) | `requires_env` GATES loading; PORT/TRANSPORT have safe defaults and the provider keys are provider-conditional, so requiring them would disable the plugin in valid configs. They belong in `optional_env`. |
| Use `password: true` for the secret SIP field (per the guide prose) | Verified false (fact #3): the 0.16.0 parser reads `secret`, never `password`; `password: true` would silently fail to mask the prompt. |
| Add a `register()` implementation in the directory `__init__.py` | Would be a second copy of the registration logic that can drift / double-register. The re-export keeps one implementation, one registration. |
