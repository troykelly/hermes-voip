# Runbook: enabling the hermes-voip plugin in Hermes

**What it is.** How an operator turns the `hermes-voip` plugin **on** in a Hermes runtime, and
why the obvious command needs a small helper file first. `hermes-voip` ships as a **pip /
entry-point** plugin (entry-point group `hermes_agent.plugins`, declared in
[`pyproject.toml`](../../pyproject.toml)). Two things gate it:

1. **Activation gate (always applies).** The Hermes runtime only loads an entry-point plugin
   when its name is in the `plugins.enabled` list of Hermes' `config.yaml`. This is the
   mechanism that genuinely decides whether the plugin runs.
2. **CLI discoverability (cosmetic, but it breaks the obvious command).** `hermes plugins list`
   and `hermes plugins enable` scan only the **filesystem** plugin directories (bundled +
   `~/.hermes/plugins/` + project `./.hermes/plugins/`). They never consult importlib entry
   points. So out of the box a pip-installed plugin is invisible to them, and
   `hermes plugins enable hermes-voip` fails with **"Plugin 'hermes-voip' is not installed or
   bundled."** (exit 1) — even though the plugin *is* installed and *will* load once enabled.

> **Public repo — no secrets here.** This runbook contains no host/extension/password. The SIP
> credentials are a separate concern: see [`0001-sip-extension-credentials.md`](0001-sip-extension-credentials.md).

## Recommended: install the metadata stub, then enable

The repo ships a description-only metadata stub at
[`packaging/hermes-plugins/hermes-voip/plugin.yaml`](../../packaging/hermes-plugins/hermes-voip/plugin.yaml).
Copying it into the Hermes user-plugins directory makes the CLI recognise the plugin, so the
natural `hermes plugins enable` command works and the plugin shows up in `hermes plugins list`.

```bash
# 1. Install the stub (one time):
mkdir -p ~/.hermes/plugins/hermes-voip
cp packaging/hermes-plugins/hermes-voip/plugin.yaml ~/.hermes/plugins/hermes-voip/plugin.yaml

# 2. Enable the plugin (now succeeds; writes plugins.enabled in config.yaml):
hermes plugins enable hermes-voip
# → ✓ Plugin hermes-voip enabled. Takes effect on next session.

# 3. Start the gateway:
hermes gateway run
```

The stub carries **no `__init__.py` / `register()`**, so it is metadata only. The plugin's real
code is still loaded from its pip entry point, **exactly once** — there is no double
registration (verified: with both the entry point and the stub present, the `voip` platform is
registered a single time). The stub only:

- makes `hermes plugins list` show `hermes-voip` with the name/version/description from the
  stub, and
- lets `hermes plugins enable` / `disable hermes-voip` succeed (they edit `plugins.enabled` in
  `config.yaml`).

Keep the stub's `name` / `version` / `description` in sync with
[`pyproject.toml`](../../pyproject.toml).

## Alternative: edit config.yaml directly (no stub)

If you'd rather not add the stub, enable the plugin by hand-editing Hermes' config. The path is
whatever `hermes config path` prints (usually `~/.hermes/config.yaml`):

```yaml
plugins:
  enabled:
    - hermes-voip
```

Then `hermes gateway run`. This sets the same `plugins.enabled` list `hermes plugins enable`
would. Without the stub, `hermes plugins list` still won't show the plugin and
`hermes plugins enable hermes-voip` still fails — but the runtime honours the hand-edited list
regardless, so the plugin loads.

> **Do not use `hermes config set` for this.** `hermes config set plugins.enabled '["hermes-voip"]'`
> stores the value as a **string** (`plugins.enabled: '["hermes-voip"]'`), not a YAML list, so
> the runtime does not recognise it. Edit the YAML list by hand, or use the stub + `hermes
> plugins enable`.

## Verify

**The plugin is recognised as enabled by the runtime** (the load-bearing check — independent of
the CLI cosmetics). With the `webrtc`/all extras synced so the runtime is importable:

```bash
uv run --all-extras python -c \
  "from hermes_cli.plugins import _get_enabled_plugins as g; e=g(); print(e); print('hermes-voip enabled:', bool(e) and 'hermes-voip' in e)"
# → {'hermes-voip'}        hermes-voip enabled: True
```

**The platform actually registers when the runtime loads plugins:**

```bash
uv run --all-extras python - <<'PY'
from hermes_cli.plugins import PluginManager
PluginManager().discover_and_load()
from gateway.platform_registry import platform_registry
from gateway.config import Platform
print("voip registered:", platform_registry.is_registered("voip"), "->", Platform("voip"))
PY
# → voip registered: True -> Platform.VOIP
```

**The CLI sees it (only after the stub is installed):**

```bash
HERMES_PLUGINS_DEBUG=1 hermes plugins list   # hermes-voip appears with its description + version
```

`HERMES_PLUGINS_DEBUG=1` is a Hermes runtime variable (not read by this plugin) that prints
plugin discovery/load detail at startup — useful when the plugin is unexpectedly absent.

For the end-to-end "register on the gateway and place a test call" procedure, see
[`0002-voip-live-validation.md`](0002-voip-live-validation.md).

## Disable / remove / roll back

```bash
hermes plugins disable hermes-voip          # if the stub is installed (removes it from plugins.enabled)
# …or hand-remove the "- hermes-voip" line under plugins.enabled in config.yaml.

rm -rf ~/.hermes/plugins/hermes-voip        # remove the metadata stub (reverts CLI discoverability)
```

Disabling leaves the pip package installed but unloaded; removing the stub only reverts the CLI
affordance. Neither uninstalls the package (`uv` / `pip` owns that).

## Why not fix the CLI instead?

The `hermes plugins enable`/`list` filesystem-only behaviour is in the **Hermes runtime**
(`hermes_cli`), not this plugin — we can't change it from here. The stub is the supported way a
pip plugin makes itself visible to that CLI. The plugin's own operator-facing hint (the
`install_hint` passed to `register_platform` in [`src/hermes_voip/plugin.py`](../../src/hermes_voip/plugin.py))
therefore points at the `plugins.enabled` mechanism, which works in every case.
