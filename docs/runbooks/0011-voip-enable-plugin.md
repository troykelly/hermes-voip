# Runbook: enabling the hermes-voip plugin in Hermes

**What it is.** How an operator turns the `hermes-voip` plugin **on** in a Hermes runtime, and
why the obvious command needs a small directory install first. `hermes-voip` ships as a **pip /
entry-point** plugin (entry-point group `hermes_agent.plugins`, declared in
[`pyproject.toml`](../../pyproject.toml)) **and** carries a complete plugin manifest
(`plugin.yaml`, ADR-0037). Two things gate it:

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

## Installing the package (what makes it loadable)

The plugin's code must be **installed as the `hermes_voip` Python package** for Hermes to load
it (Hermes auto-discovers it via the `hermes_agent.plugins` entry point once installed):

```bash
pip install "hermes-voip[ml,media,webrtc] @ git+https://github.com/troykelly/hermes-voip"
# …or from a repo clone:  uv sync --frozen --all-extras
```

> **WARNING: Hermes imports from the pip-installed copy, not the directory clone.**
> The Hermes runtime loads `hermes_voip` from the pip-installed site-packages directory,
> not from any git-cloned `~/.hermes/plugins/hermes-voip/` directory. If you apply a
> local patch or debug change to the code, you must apply it to the site-packages copy
> (or to both), not just the cloned directory — otherwise the runtime won't see it.

> **Verified (rule 27): `hermes plugins install owner/repo` does NOT install this package.**
> The real `hermes plugins install <identifier>` (hermes-agent 0.16.0; `identifier` = a Git URL
> or `owner/repo` shorthand) **`git clone --depth 1`s** the repo into `~/.hermes/plugins/<name>/`
> and prompts for the **clone-root** `plugin.yaml`'s `requires_env` — it never runs `pip`. Because
> `hermes-voip` is a *src-layout package* (code under `src/hermes_voip/`, not at the repo root),
> a bare clone can't import it, and with no **root** `plugin.yaml` the installer even names the
> plugin after the repo directory and skips the env prompt (confirmed empirically against this
> repo via a `file://` install). So `hermes plugins install` is the right tool for *self-contained
> directory plugins* (code at the repo root) — **not** for a packaged plugin like this one.
> `pip install git+…` is the supported install. (`hermes plugins update`/`remove` likewise act on
> the cloned directory, not the pip package.)

## Recommended: install the directory manifest, then enable

The repo ships the **complete plugin manifest** at
[`packaging/hermes-plugins/hermes-voip/plugin.yaml`](../../packaging/hermes-plugins/hermes-voip/plugin.yaml)
(name/version/description/author/`kind: platform`/`provides_tools`/`provides_hooks`/
`requires_env`) alongside an
[`__init__.py`](../../packaging/hermes-plugins/hermes-voip/__init__.py) that re-exports the
package's `register`. Copying this directory into the Hermes user-plugins directory makes the
CLI recognise the plugin, so the natural `hermes plugins enable` command works and the plugin
shows up in `hermes plugins list` with its manifest-backed version + description from the shipped plugin metadata.

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
**exactly once** (verified live: `platform_registry.is_registered("voip")` is `True` with 10
tools + 1 hook, no duplicate). The directory copy's job is purely the CLI affordance — it makes
`hermes plugins list` show the plugin and lets `hermes plugins enable`/`disable hermes-voip`
edit `plugins.enabled` in `config.yaml`.

> **`hermes plugins list` vs `/plugins` — a runtime nuance.** `hermes plugins list` reads this
> directory `plugin.yaml`, so it shows `hermes-voip` with the directory manifest version + description
> from the shipped plugin metadata. The in-session `/plugins` *loaded* view, however, shows the
> entry point empty version. Its tool/hook count (10 tools, 1 hook) is
> computed from the real registration, not from `provides_tools`. Both views are correct; they
> read different sources. (`hermes plugins list` has no tool-count column at all.)

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

None of these checks touch a **running** gateway. One of them — `hermes plugins enable` —
**does write** `plugins.enabled` to `config.yaml`, so to avoid mutating a live `~/.hermes`,
point `HERMES_HOME` at a throwaway dir **first** and copy the directory manifest into it:

```bash
export HERMES_HOME=$(mktemp -d)
mkdir -p "$HERMES_HOME/plugins/hermes-voip"
cp packaging/hermes-plugins/hermes-voip/plugin.yaml "$HERMES_HOME/plugins/hermes-voip/"
cp packaging/hermes-plugins/hermes-voip/__init__.py "$HERMES_HOME/plugins/hermes-voip/"
```

**The CLI sees it (read-only listing) + enabling works (this step writes config.yaml):**

```bash
hermes plugins list --plain | grep hermes-voip      # read-only
# → not enabled  user  <version>  hermes-voip
hermes plugins enable hermes-voip                    # WRITES plugins.enabled to config.yaml
# → ✓ Plugin hermes-voip enabled. Takes effect on next session.
hermes plugins list --plain | grep hermes-voip       # read-only
# → enabled      user  <version>  hermes-voip
```

`hermes plugins list` is a **filesystem listing** — it reads the directory `plugin.yaml`
(so the **version + description** come from the shipped plugin manifest) but does **not** load the plugin
or run `register()`, and it does **not** consult `HERMES_PLUGINS_DEBUG`. To see actual
discovery/load detail set `HERMES_PLUGINS_DEBUG=1` when you start the **gateway**
(`hermes gateway run`) — that is the path that loads plugins. The load-bearing checks below
are what actually prove the plugin loads + registers.

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
# → tools: 10 hooks: 1
```

For the end-to-end "register on the gateway and place a test call" procedure, see
[`0002-voip-live-validation.md`](0002-voip-live-validation.md).

## Config preflight: provider misconfiguration is rejected at enable time

When the gateway builds the `voip` adapter it runs the plugin's `validate_config`
(`validate_voip_config`) **before** `connect()`. As well as checking the SIP/media env
**shape** (`load_gateway_config` / `load_media_config`), that gate now **preflights the
provider wiring** (`check_providers_buildable` in
[`src/hermes_voip/providers/build.py`](../../src/hermes_voip/providers/build.py)), so the
most common provider misconfiguration is rejected up front — with a clear `ConfigError`
naming the offending setting (never the SIP password) — instead of surfacing one step later
as a `connect()`-time crash. It rejects:

- **An unimplemented provider token.** `HERMES_VOIP_STT_PROVIDER` / `HERMES_VOIP_TTS_PROVIDER`
  / `HERMES_VOIP_INJECTION_GUARD` accept a wider config *vocabulary* than the set of *wired*
  providers; selecting a valid-but-not-yet-implemented token (e.g. a deferred TTS engine) is
  rejected here rather than at connect().
- **A missing or mis-pathed self-host model directory.** When a self-host provider is
  selected (the defaults are `sherpa-onnx` STT, `sherpa-kokoro` TTS, `onnx` guard), its model
  directory env var — `HERMES_VOIP_STT_MODEL_DIR` / `HERMES_VOIP_TTS_MODEL` /
  `HERMES_VOIP_INJECTION_GUARD_MODEL_DIR` (and `HERMES_VOIP_TTS_FALLBACK_MODEL` for a
  model-backed fallback) — must be set **and** point at an existing directory.

The preflight is deliberately **shallow** (no model load): the model-integrity (SHA-256) and
SPDX licence gates, and per-call Opus availability, still run at `connect()` — so Opus is
never required to enable the plugin (a host without the `webrtc` extra keeps the
G.711/G.722 SIP path). A config that passes the preflight can therefore still fail at
`connect()` (a swapped or truncated weight, a disallowed licence). To fix a preflight
rejection, set the named env var to a valid, provisioned value; for downloading + verifying
the self-host models, see [`0002-voip-live-validation.md`](0002-voip-live-validation.md).

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
(`hermes_cli`), not this plugin — we can't change it from here. The CLI is hardcoded to scan
the filesystem plugin directories (bundled + `~/.hermes/plugins/` + project `./.hermes/plugins/`)
and never consults `importlib.metadata` entry points, so a pip entry-point plugin like this one
is invisible to it — this upstream limitation is tracked as [NousResearch/hermes-agent#23802](https://github.com/NousResearch/hermes-agent/issues/23802).
Watch that issue for when the CLI gains entry-point awareness; at that point, the directory
manifest copy-to-`~/.hermes/plugins` workaround can be retired. For now, the directory manifest
is the supported way a pip plugin makes itself visible to that CLI. The plugin's own
operator-facing hint (the `install_hint` passed to `register_platform` in
[`src/hermes_voip/plugin.py`](../../src/hermes_voip/plugin.py)) therefore points at the
`plugins.enabled` mechanism, which works in every case.
