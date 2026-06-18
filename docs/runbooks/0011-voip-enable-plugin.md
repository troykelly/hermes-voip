# Runbook: enabling the hermes-voip plugin in Hermes

**What it is.** How an operator turns the `hermes-voip` plugin **on** in a Hermes runtime, and
why the obvious command needs a small directory install first. `hermes-voip` ships as a **pip /
entry-point** plugin (entry-point group `hermes_agent.plugins`, declared in
[`pyproject.toml`](../../pyproject.toml)) **and** carries a complete plugin manifest
(`plugin.yaml`, ADR-0036). Two things gate it:

1. **Activation gate (always applies).** The Hermes runtime only loads an entry-point plugin
   when its name is in the `plugins.enabled` list of Hermes' `config.yaml`. This is the
   mechanism that genuinely decides whether the plugin runs.
2. **CLI discoverability (the obvious command needs the manifest on disk).** `hermes plugins
   list` and `hermes plugins enable` scan only the **filesystem** plugin directories (bundled +
   `~/.hermes/plugins/` + project `./.hermes/plugins/`). They never consult importlib entry
   points (verified against hermes-agent 0.16.0: the entry-point scan builds a manifest with
   **empty** version/description/tool fields). So a pip-only install is invisible to them, and
   `hermes plugins enable hermes-voip` fails with **"Plugin 'hermes-voip' is not installed or
   bundled."** (exit 1) — even though the plugin *is* installed and *will* load once enabled.
   Installing the manifest as a directory plugin (below) makes the CLI see it.

> **Public repo — no secrets here.** This runbook contains no host/extension/password. The SIP
> credentials are a separate concern: see [`0001-sip-extension-credentials.md`](0001-sip-extension-credentials.md).

## Recommended: install the directory manifest, then enable

The repo ships the **complete plugin manifest** at
[`packaging/hermes-plugins/hermes-voip/plugin.yaml`](../../packaging/hermes-plugins/hermes-voip/plugin.yaml)
(name/version/description/author/`kind: platform`/`provides_tools`/`provides_hooks`/
`requires_env`) alongside an
[`__init__.py`](../../packaging/hermes-plugins/hermes-voip/__init__.py) that re-exports the
package's `register`. Copying this directory into the Hermes user-plugins directory makes the
CLI recognise the plugin, so the natural `hermes plugins enable` command works and the plugin
shows up in `hermes plugins list` with its version + description.

```bash
# 1. Install the directory manifest (one time):
mkdir -p ~/.hermes/plugins/hermes-voip
cp packaging/hermes-plugins/hermes-voip/plugin.yaml ~/.hermes/plugins/hermes-voip/plugin.yaml
cp packaging/hermes-plugins/hermes-voip/__init__.py ~/.hermes/plugins/hermes-voip/__init__.py

# 2. Enable the plugin (now succeeds; writes plugins.enabled in config.yaml):
hermes plugins enable hermes-voip
# → ✓ Plugin hermes-voip enabled. Takes effect on next session.

# 3. Start the gateway:
hermes gateway run
```

**No double-registration (verified).** When both the pip entry point and this directory are
present, the loader dedups by key (both resolve to `hermes-voip`) and the **entry point wins**,
so this directory's `__init__.py` is not re-imported and the `voip` platform registers
**exactly once** (verified live: `platform_registry.is_registered("voip")` is `True` with 9
tools + 1 hook, no duplicate). The directory copy's job is purely the CLI affordance — it makes
`hermes plugins list` show the plugin and lets `hermes plugins enable`/`disable hermes-voip`
edit `plugins.enabled` in `config.yaml`.

> **`hermes plugins list` vs `/plugins` — a runtime nuance.** `hermes plugins list` reads this
> directory `plugin.yaml`, so it shows `hermes-voip … 0.0.0 … <description> … user`. The
> in-session `/plugins` *loaded* view, however, shows the version from the manifest that **won
> the load dedup — the entry point's — which is empty**; its **tool/hook count (9 tools, 1
> hook) is computed from the real registration, not from `provides_tools`**. Both are correct;
> they read different sources. (`hermes plugins list` has no tool-count column at all.)

The manifest is the single source of truth: its `provides_tools` / `provides_hooks` are pinned
by `tests/test_plugin_manifest.py` to the tools/hooks the plugin actually registers, and its
`version` to [`pyproject.toml`](../../pyproject.toml) — so it cannot silently drift.

## Alternative: edit config.yaml directly (no directory manifest)

If you'd rather not add the directory manifest, enable the plugin by hand-editing Hermes'
config. The path is whatever `hermes config path` prints (usually `~/.hermes/config.yaml`):

```yaml
plugins:
  enabled:
    - hermes-voip
```

Then `hermes gateway run`. This sets the same `plugins.enabled` list `hermes plugins enable`
would. Without the directory manifest, `hermes plugins list` still won't show the plugin and
`hermes plugins enable hermes-voip` still fails — but the runtime honours the hand-edited list
regardless, so the plugin loads.

> **Do not use `hermes config set` for this.** `hermes config set plugins.enabled '["hermes-voip"]'`
> stores the value as a **string** (`plugins.enabled: '["hermes-voip"]'`), not a YAML list, so
> the runtime does not recognise it. Edit the YAML list by hand, or use the directory manifest +
> `hermes plugins enable`.

## Verify

All checks below run **read-only**; none of them touch a running gateway. To avoid disturbing a
live `~/.hermes`, you can point `HERMES_HOME` at a throwaway dir first
(`export HERMES_HOME=$(mktemp -d)` then copy the directory manifest into
`$HERMES_HOME/plugins/hermes-voip/`).

**The CLI sees it + enabling works (after the directory manifest is installed):**

```bash
hermes plugins list --plain | grep hermes-voip
# → not enabled  user  0.0.0  hermes-voip
hermes plugins enable hermes-voip
# → ✓ Plugin hermes-voip enabled. Takes effect on next session.
hermes plugins list --plain | grep hermes-voip
# → enabled      user  0.0.0  hermes-voip
```

`hermes plugins list` reads the directory `plugin.yaml`, so the **version + description** come
from the manifest. (`HERMES_PLUGINS_DEBUG=1 hermes plugins list` prints discovery/load detail at
startup — useful when the plugin is unexpectedly absent. It is a Hermes runtime variable, not
read by this plugin.)

**The plugin is recognised as enabled by the runtime** (the load-bearing check — independent of
the CLI cosmetics). With the `webrtc`/all extras synced so the runtime is importable:

```bash
uv run --all-extras python -c \
  "from hermes_cli.plugins import _get_enabled_plugins as g; e=g(); print(e); print('hermes-voip enabled:', bool(e) and 'hermes-voip' in e)"
# → {'hermes-voip'}        hermes-voip enabled: True
```

**The platform registers exactly once + the agent tools load** (this is what `/plugins` counts):

```bash
uv run --all-extras python - <<'PY'
from hermes_cli.plugins import PluginManager
mgr = PluginManager(); mgr.discover_and_load(force=True)
from gateway.platform_registry import platform_registry
from gateway.config import Platform
print("voip registered:", platform_registry.is_registered("voip"), "->", Platform("voip"))
lp = next(p for k, p in mgr._plugins.items() if "hermes-voip" in k)
print("tools:", len(lp.tools_registered), "hooks:", len(lp.hooks_registered))
PY
# → voip registered: True -> Platform.VOIP
# → tools: 9 hooks: 1
```

For the end-to-end "register on the gateway and place a test call" procedure, see
[`0002-voip-live-validation.md`](0002-voip-live-validation.md).

## Disable / remove / roll back

```bash
hermes plugins disable hermes-voip          # if the directory manifest is installed (removes it from plugins.enabled)
# …or hand-remove the "- hermes-voip" line under plugins.enabled in config.yaml.

rm -rf ~/.hermes/plugins/hermes-voip        # remove the directory manifest (reverts CLI discoverability)
```

Disabling leaves the pip package installed but unloaded; removing the directory manifest only
reverts the CLI affordance. Neither uninstalls the package (`uv` / `pip` owns that).

## Why not fix the CLI instead?

The `hermes plugins enable`/`list` filesystem-only behaviour is in the **Hermes runtime**
(`hermes_cli`), not this plugin — we can't change it from here. The directory manifest is the
supported way a pip plugin makes itself visible to that CLI. The plugin's own operator-facing
hint (the `install_hint` passed to `register_platform` in
[`src/hermes_voip/plugin.py`](../../src/hermes_voip/plugin.py)) therefore points at the
`plugins.enabled` mechanism, which works in every case.
