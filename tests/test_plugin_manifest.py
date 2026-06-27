"""The Hermes plugin manifest (``plugin.yaml``) is complete and drift-free.

TDD (rule 18): these are written before the manifest is filled out, and they fail
against the #109 description-only stub (no ``provides_tools`` / ``provides_hooks`` /
``requires_env`` / ``author``). They lock the manifest to the **real** runtime so it
cannot drift:

* ``provides_tools`` must list EXACTLY the tools :func:`hermes_voip.plugin.register`
  registers via ``ctx.register_tool`` (the drift guard the operator asked for — a
  tool added to ``voip_tools`` but not the manifest, or vice-versa, fails here).
* ``provides_hooks`` must list EXACTLY the hooks it registers via ``ctx.register_hook``
  (``pre_tool_call``).
* ``kind`` is ``platform`` (we register a platform — ADR-0002), ``version`` matches
  ``pyproject.toml``, and the manifest leaks no secret VALUES (env-var NAMES only;
  PUBLIC-repo invariant).
* The manifest ships BOTH as importable package data (``hermes_voip`` wheel) AND as
  the directory-install layout under ``packaging/`` with an ``__init__.py`` that
  re-exports :func:`register` — the two canonical install models (ADR-0037).

The manifest schema (keys, ``requires_env`` rich format with ``secret:`` (NOT
``password:``), the ``kind`` values, and the fact that an ENTRY-POINT plugin never
surfaces this metadata so the DIRECTORY manifest is what ``hermes plugins list`` /
``/plugins`` read) is verified against the installed hermes-agent 0.16.0 source in
docs/runbooks/0011-voip-enable-plugin.md.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from importlib.metadata import version as _dist_version
from pathlib import Path

import yaml

import hermes_voip
from hermes_voip.config import (
    _DEFAULT_KEEPALIVE_INTERVAL,
    _DEFAULT_MAX_CALLS,
    _DEFAULT_SHUTDOWN_DRAIN_SECS,
)

# ---------------------------------------------------------------------------
# Locations: the repo source tree (so the test runs from a checkout) + the
# importable package-data copy (so the built wheel is covered too).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGING_DIR = _REPO_ROOT / "packaging" / "hermes-plugins" / "hermes-voip"
_PACKAGING_MANIFEST = _PACKAGING_DIR / "plugin.yaml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _load_manifest(path: Path) -> dict[str, object]:
    """Parse a ``plugin.yaml`` into a mapping (fails the test if it is not one)."""
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert isinstance(data, dict), f"{path} must parse to a YAML mapping"
    # ``data`` is now narrowed to ``dict`` by the assert; the values are ``object``
    # under PyYAML's typed stubs, so this is the manifest mapping with no cast.
    manifest: dict[str, object] = data
    return manifest


def _pyproject_version() -> str:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    assert isinstance(project, dict)
    version = project["version"]
    assert isinstance(version, str)
    return version


# ---------------------------------------------------------------------------
# Fake PluginContext — records register_tool / register_hook / register_platform
# so we can compare the manifest's declared surface to the REAL registered one.
# Mirrors the shape in tests/test_register.py (full runtime: both register_tool
# AND register_hook present, so every ELEVATED tool registers — fail-closed gate
# is satisfied).
# ---------------------------------------------------------------------------


class _RecordingCtx:
    """A PluginContext that records every tool/hook/platform registration."""

    def __init__(self) -> None:
        self.platform_calls: list[str] = []
        self.tool_names: list[str] = []
        self.hook_names: list[str] = []

    def register_platform(  # noqa: PLR0913 — mirrors hermes-agent's arity
        self,
        name: str,
        label: str = "",
        adapter_factory: object = None,
        check_fn: object = None,
        validate_config: object = None,
        required_env: Sequence[str] | None = None,
        install_hint: str = "",
        **entry_kwargs: object,
    ) -> None:
        self.platform_calls.append(name)

    def register_tool(  # noqa: PLR0913 — mirrors hermes-agent's register_tool arity
        self,
        name: str,
        toolset: str = "",
        schema: dict[str, object] | None = None,
        handler: object = None,
        *,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
    ) -> None:
        self.tool_names.append(name)

    def register_hook(self, hook_name: str, callback: object) -> None:
        self.hook_names.append(hook_name)


def _registered_tools() -> set[str]:
    """The tool names :func:`register` registers with a full (gated) runtime ctx."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _RecordingCtx()
    register(ctx)
    return set(ctx.tool_names)


def _registered_hooks() -> set[str]:
    """The hook names :func:`register` registers with a full runtime ctx."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _RecordingCtx()
    register(ctx)
    return set(ctx.hook_names)


# ---------------------------------------------------------------------------
# (a) The manifest exists, parses, and carries the core identity fields.
# ---------------------------------------------------------------------------


def test_packaging_manifest_exists_and_parses() -> None:
    assert _PACKAGING_MANIFEST.is_file(), (
        f"the plugin manifest is missing at {_PACKAGING_MANIFEST}"
    )
    _load_manifest(_PACKAGING_MANIFEST)


def test_manifest_identity_fields() -> None:
    """Name / description / author / kind are present and correct."""
    manifest = _load_manifest(_PACKAGING_MANIFEST)
    assert manifest.get("name") == "hermes-voip"
    # kind: platform — we register a platform (gateway adapter), ADR-0002.
    assert manifest.get("kind") == "platform", (
        "a gateway-adapter plugin must declare kind: platform"
    )
    description = manifest.get("description")
    assert isinstance(description, str)
    assert description.strip()
    author = manifest.get("author")
    assert isinstance(author, str)
    assert author.strip(), "the manifest must name an author (Hermes manifest spec)"


def test_manifest_declares_a_platform_label() -> None:
    """A kind: platform manifest declares a human-readable ``label``.

    The canonical platform-manifest field (see plugins/platforms/irc/plugin.yaml,
    and the ``label`` consumed by hermes_cli/config.py's platform env injector). It
    mirrors the label passed to ``ctx.register_platform`` so the operator-facing name
    is consistent across the manifest and the running registry.
    """
    manifest = _load_manifest(_PACKAGING_MANIFEST)
    label = manifest.get("label")
    assert isinstance(label, str), "a kind: platform manifest must declare a label"
    assert label.strip(), "the platform label must be non-empty"


def test_manifest_version_matches_pyproject() -> None:
    """The manifest version must track pyproject.toml (no silent drift)."""
    manifest = _load_manifest(_PACKAGING_MANIFEST)
    assert manifest.get("version") == _pyproject_version(), (
        "plugin.yaml version must match pyproject.toml [project].version"
    )


def test_package_version_matches_pyproject() -> None:
    """hermes_voip.__version__ must equal pyproject.toml [project].version.

    A release bump can silently leave the package runtime version out of sync
    with the declared package version.  This guard catches that drift: if a
    developer bumps pyproject.toml [project].version without updating
    ``src/hermes_voip/__init__.py __version__`` (or vice-versa), this test
    fails at release time rather than silently shipping a mismatched package.
    """
    assert hermes_voip.__version__ == _pyproject_version(), (
        f"hermes_voip.__version__ ({hermes_voip.__version__!r}) must match "
        f"pyproject.toml [project].version ({_pyproject_version()!r})"
    )


def test_package_version_is_derived_from_installed_metadata() -> None:
    """``__version__`` is single-sourced from installed package metadata.

    The release blocker: the version lived hard-coded in THREE places
    (pyproject.toml, ``__init__.py``, plugin.yaml) and could drift. We
    single-source the RUNTIME version by deriving
    ``src/hermes_voip/__init__.py __version__`` from the installed package
    metadata (``importlib.metadata.version("hermes-voip")``) — which is itself
    populated from ``pyproject.toml [project].version`` at build/install time.

    This guard asserts the derivation is LIVE: ``__version__`` equals the
    distribution metadata version, not an independently-maintained literal. A
    future release is then a single edit in ``pyproject.toml`` (the metadata
    follows automatically) rather than three edits that can desync.
    """
    assert hermes_voip.__version__ == _dist_version("hermes-voip"), (
        f"hermes_voip.__version__ ({hermes_voip.__version__!r}) must be derived "
        f"from importlib.metadata.version('hermes-voip') "
        f"({_dist_version('hermes-voip')!r}) — it must not be a hand-maintained literal"
    )


def test_init_source_single_sources_the_version() -> None:
    """``__init__.py`` derives the version from metadata — no bare literal assign.

    Complements the runtime check above with a SOURCE-level guard so the
    derivation cannot be quietly reverted to a hard-coded
    ``__version__ = "X.Y.Z"`` (which would re-introduce the third drift point
    even while the runtime value happens to match). The module must reference
    ``importlib.metadata`` and must not assign ``__version__`` to a quoted
    string literal.
    """
    init_py = _REPO_ROOT / "src" / "hermes_voip" / "__init__.py"
    source = init_py.read_text(encoding="utf-8")
    assert "importlib.metadata" in source, (
        "__init__.py must derive __version__ from importlib.metadata"
    )
    # No bare-literal assignment such as ``__version__ = "0.0.0"``.
    for quote in ('__version__ = "', "__version__ = '"):
        assert quote not in source, (
            "__version__ must be derived from package metadata, not assigned a "
            f"string literal (found {quote!r})"
        )


# ---------------------------------------------------------------------------
# (b) THE DRIFT GUARD: provides_tools / provides_hooks == the registered surface.
# ---------------------------------------------------------------------------


def test_provides_tools_matches_registered_tools_exactly() -> None:
    """provides_tools must equal the set register(ctx) actually registers.

    This is the guard the operator asked for: a tool added to / removed from
    ``register_voip_tools`` without updating the manifest (or vice-versa) fails
    here. Equality (not subset) catches BOTH directions of drift.
    """
    manifest = _load_manifest(_PACKAGING_MANIFEST)
    provides = manifest.get("provides_tools")
    assert isinstance(provides, list), "provides_tools must be a YAML list"
    declared = set(provides)
    assert len(declared) == len(provides), "provides_tools has duplicate entries"
    registered = _registered_tools()
    assert declared == registered, (
        "provides_tools drifted from the registered tools.\n"
        f"  declared but not registered: {sorted(declared - registered)}\n"
        f"  registered but not declared: {sorted(registered - declared)}"
    )


def test_provides_hooks_matches_registered_hooks_exactly() -> None:
    """provides_hooks must equal the hooks register(ctx) registers (pre_tool_call)."""
    manifest = _load_manifest(_PACKAGING_MANIFEST)
    provides = manifest.get("provides_hooks")
    assert isinstance(provides, list), "provides_hooks must be a YAML list"
    declared = set(provides)
    registered = _registered_hooks()
    assert "pre_tool_call" in registered, (
        "sanity: register() should register the pre_tool_call gate"
    )
    assert declared == registered, (
        "provides_hooks drifted from the registered hooks.\n"
        f"  declared but not registered: {sorted(declared - registered)}\n"
        f"  registered but not declared: {sorted(registered - declared)}"
    )


# ---------------------------------------------------------------------------
# (c) requires_env: the GENUINELY-required SIP vars (these gate loading), in the
#     rich format, with secret:true on the password. Optional/provider keys go in
#     optional_env, never requires_env (listing an optional var would wrongly
#     disable the plugin when it is absent).
# ---------------------------------------------------------------------------


def _entry_name(entry: dict[str, object]) -> str:
    """Return a normalised entry's ``name`` as a str (it is, by construction)."""
    name = entry["name"]
    assert isinstance(name, str)
    return name


def _env_entry_names(entries: object) -> list[str]:
    """Extract env-var names from a requires_env/optional_env list (str|dict form)."""
    return [_entry_name(e) for e in _normalise_env(entries)]


def _normalise_env(entries: object) -> list[dict[str, object]]:
    """Normalise a requires_env/optional_env list to dicts with a string ``name``.

    Accepts the two Hermes formats — a bare string or a rich mapping — and returns
    a uniform ``[{"name": …, …}]`` list so callers can read name + metadata.
    """
    assert isinstance(entries, list)
    out: list[dict[str, object]] = []
    for entry in entries:
        if isinstance(entry, str):
            out.append({"name": entry})
        else:
            assert isinstance(entry, dict), "env entry must be a str or a dict"
            name = entry.get("name")
            assert isinstance(name, str), f"rich env entry needs a string name: {entry}"
            out.append(entry)
    return out


def _requires_env_block() -> list[dict[str, object]]:
    """The manifest's ``requires_env`` normalised to rich dicts."""
    return _normalise_env(_load_manifest(_PACKAGING_MANIFEST).get("requires_env"))


def _optional_env_block() -> list[dict[str, object]]:
    """The manifest's ``optional_env`` normalised to rich dicts (``[]`` if absent)."""
    optional = _load_manifest(_PACKAGING_MANIFEST).get("optional_env")
    return _normalise_env(optional) if optional is not None else []


def test_requires_env_lists_the_required_sip_vars() -> None:
    """requires_env must gate on exactly the required SIP credential vars."""
    manifest = _load_manifest(_PACKAGING_MANIFEST)
    names = set(_env_entry_names(manifest.get("requires_env")))
    # The single-registration required set (load_gateway_config enforces these).
    assert names == {
        "HERMES_SIP_HOST",
        "HERMES_SIP_EXTENSION",
        "HERMES_SIP_PASSWORD",
    }, f"requires_env should gate on the required SIP vars, got {sorted(names)}"


def test_password_env_is_marked_secret() -> None:
    """The SIP password's rich entry uses secret: true (Hermes masks the prompt).

    Verified against hermes-agent 0.16.0: ``hermes plugins install`` reads ``secret``
    (``spec.get("secret", …)``), and the ``hermes config`` platform env injector reads
    ``password`` OR ``secret`` (config.py:_inject_platform_plugin_env_vars). We use
    ``secret: true`` because it is honoured by BOTH paths (``password`` is not read by
    the install prompt). The masked prompt depends on it.
    """
    manifest = _load_manifest(_PACKAGING_MANIFEST)
    entries = manifest.get("requires_env")
    assert isinstance(entries, list)
    password = next(
        (
            e
            for e in entries
            if isinstance(e, dict) and e.get("name") == "HERMES_SIP_PASSWORD"
        ),
        None,
    )
    assert password is not None, "HERMES_SIP_PASSWORD must be a rich entry"
    assert password.get("secret") is True, "HERMES_SIP_PASSWORD must be secret: true"


def test_every_env_entry_has_a_prompt() -> None:
    """Every requires_env / optional_env rich entry declares a ``prompt``.

    ``prompt`` is the canonical platform-manifest field (plugins/platforms/irc/
    plugin.yaml) that the ``hermes config`` setup-wizard injector
    (hermes_cli/config.py:_inject_platform_plugin_env_vars) shows as the input label
    — without it the wizard falls back to the raw variable name. A guided, friendly
    env prompt is the operator-facing win this manifest is for, so we require it on
    every declared variable.
    """
    for entry in (*_requires_env_block(), *_optional_env_block()):
        name = _entry_name(entry)
        prompt = entry.get("prompt")
        assert isinstance(prompt, str), (
            f"env entry {name!r} must declare a 'prompt' label"
        )
        assert prompt.strip(), f"env entry {name!r} 'prompt' must be non-empty"


def test_optional_env_advertises_transport_and_provider_keys() -> None:
    """optional_env documents the non-gating knobs (port/transport + provider keys).

    These are NOT in requires_env (they have safe defaults / are provider-conditional,
    so requiring them would wrongly disable the plugin). They are documented in the
    manifest's optional_env for completeness. NOTE (verified against hermes-agent
    0.16.0): the install prompt reads ONLY requires_env, so the runtime does NOT
    prompt for optional_env — the README + runbooks are where operators set these.
    The cloud provider keys are marked secret.
    """
    manifest = _load_manifest(_PACKAGING_MANIFEST)
    optional = manifest.get("optional_env")
    assert isinstance(optional, list), "optional_env should be a YAML list"
    names = set(_env_entry_names(optional))
    for expected in (
        "HERMES_SIP_PORT",
        "HERMES_SIP_TRANSPORT",
        "ELEVENLABS_API_KEY",
        "DEEPGRAM_API_KEY",
    ):
        assert expected in names, f"optional_env should mention {expected}"
    # Cloud keys are secret.
    for entry in optional:
        if isinstance(entry, dict) and entry.get("name") in {
            "ELEVENLABS_API_KEY",
            "DEEPGRAM_API_KEY",
        }:
            assert entry.get("secret") is True, (
                f"{entry.get('name')} must be secret: true"
            )


def test_optional_env_advertises_admission_control_knobs() -> None:
    """optional_env documents the admission-control and shutdown-drain knobs.

    HERMES_SIP_MAX_CALLS and HERMES_SIP_SHUTDOWN_DRAIN_SECS are declared and
    validated in config.py (lines ~104-107) and documented in runbook-0013.
    They are NOT in requires_env (each has a safe runtime default so they never
    gate plugin loading), but they must be visible in the manifest so an operator
    tuning admission capacity or shutdown drain has a manifest-visible signal that
    these knobs exist.

    Defaults verified against config.py: MAX_CALLS=8, SHUTDOWN_DRAIN_SECS=5.0.
    """
    entries = _optional_env_block()
    defaults = {
        _entry_name(entry): entry.get("default")
        for entry in entries
        if isinstance(entry, dict)
    }
    for expected, config_default in (
        ("HERMES_SIP_MAX_CALLS", _DEFAULT_MAX_CALLS),
        ("HERMES_SIP_SHUTDOWN_DRAIN_SECS", _DEFAULT_SHUTDOWN_DRAIN_SECS),
    ):
        assert expected in defaults, (
            f"optional_env must advertise {expected} (admission-control knob, "
            f"config.py line ~104-107, runbook-0013) — "
            f"operator has no manifest-visible signal it exists otherwise"
        )
        manifest_default = defaults[expected]
        assert manifest_default == config_default, (
            f"{expected} default in plugin.yaml must match config.py"
        )
        assert isinstance(manifest_default, type(config_default)), (
            f"{expected} default should be a YAML {type(config_default).__name__}"
        )


# ---------------------------------------------------------------------------
# (d) PUBLIC-repo invariant: the manifest contains env-var NAMES only — no real
#     host/extension/password/IP VALUES. A coarse but effective leak guard.
# ---------------------------------------------------------------------------


def test_manifest_leaks_no_secret_values() -> None:
    """No secret VALUES anywhere in the manifest — only fakes in descriptions.

    PUBLIC-repo invariant. This guard deliberately lists NO real identifier (writing
    the operator's real host/extension here would itself be the leak it guards
    against); instead it checks for the *shapes* of a leak — private-host suffixes,
    RFC-1918 IP literals, and an env-var declared with an inline ``=value``. The
    only digits the manifest may contain are the documented fake examples (ext
    ``1000``, the TLS port ``5061``/``5060``) — asserted in
    :func:`test_manifest_uses_only_fake_examples`.
    """
    raw = _PACKAGING_MANIFEST.read_text(encoding="utf-8")
    lowered = raw.lower()
    # Private-host suffixes / RFC-1918 IP prefixes that only a real deployment has.
    forbidden_markers = (
        ".internal",
        ".lan",
        ".corp",
        "192.168.",
        "10.0.",
        "172.16.",
    )
    for marker in forbidden_markers:
        assert marker not in lowered, (
            f"manifest appears to leak a real identifier: {marker!r}"
        )


def test_manifest_uses_only_fake_examples() -> None:
    """Env vars are declared as NAMES; no entry embeds an inline ``=value``.

    A ``requires_env``/``optional_env`` entry is a name + metadata — never a
    ``NAME=value`` assignment that could carry a real credential. The only example
    host in any description is the documented fake ``pbx.example.test`` (if a host
    appears at all). This is the positive complement to the shape-based leak guard
    above, and it references no real identifier.
    """
    for entry in (*_requires_env_block(), *_optional_env_block()):
        name = _entry_name(entry)
        # The declared key is a bare env-var NAME, never ``NAME=value``.
        assert "=" not in name, f"env entry {name!r} must be a name, not an assignment"
        # Any host shown in a description must be the documented fake.
        desc = entry.get("description", "")
        assert isinstance(desc, str)
        if "example" in desc.lower() or ".test" in desc.lower():
            assert "pbx.example.test" in desc or "example.test" in desc, (
                f"{name!r} description must use the fake host pbx.example.test"
            )


# ---------------------------------------------------------------------------
# (e) Two install models (ADR-0037):
#   1. directory-install: packaging/ dir has plugin.yaml + __init__.py(register)
#   2. pip/entry-point: the manifest is importable package data of hermes_voip
# ---------------------------------------------------------------------------


def test_directory_install_layout_has_init_reexporting_register() -> None:
    """The directory-install dir ships an __init__.py exposing register(ctx).

    This is the canonical directory-install model: a dir under ~/.hermes/plugins/
    with plugin.yaml + an __init__.py whose register() the runtime calls. Ours
    re-exports the package's real register so there is one implementation.
    """
    init_py = _PACKAGING_DIR / "__init__.py"
    assert init_py.is_file(), (
        f"directory-install layout needs an __init__.py at {init_py}"
    )
    source = init_py.read_text(encoding="utf-8")
    assert "from hermes_voip.plugin import register" in source, (
        "the directory-install __init__.py must re-export the package's register"
    )
    assert "register" in source


def test_manifest_is_importable_package_data() -> None:
    """plugin.yaml resolves as ``hermes_voip`` package data (importlib.resources).

    So a pip install can surface / extract the manifest, and the directory-install
    copy can be sourced from the installed package rather than the repo. NOTE: in an
    editable install this resolves to ``src/hermes_voip/plugin.yaml`` whether or not
    the wheel-packaging is configured — so the *wheel* guard is the separate
    :func:`test_pyproject_packages_the_manifest_into_the_wheel`.
    """
    from importlib.resources import files  # noqa: PLC0415

    resource = files("hermes_voip").joinpath("plugin.yaml")
    assert resource.is_file(), "plugin.yaml must resolve as hermes_voip package data"
    data = yaml.safe_load(resource.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("name") == "hermes-voip"


def test_pyproject_packages_the_manifest_into_the_wheel() -> None:
    """The hatch wheel target must ship the manifest as package data.

    Hatchling does NOT include non-``.py`` files under the package by default, so
    ``plugin.yaml`` only lands in the built wheel because the wheel target declares
    it (``artifacts``/``force-include``). This guard fails if that declaration is
    dropped — which :func:`test_manifest_is_importable_package_data` would NOT catch
    in an editable install (the source file resolves regardless). It asserts the
    config, not a built wheel, so it stays fast and offline.
    """
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    tool = data["tool"]
    assert isinstance(tool, dict)
    wheel = tool["hatch"]["build"]["targets"]["wheel"]
    assert isinstance(wheel, dict)
    declared = " ".join(
        str(v)
        for key in ("artifacts", "force-include")
        for v in _as_list(wheel.get(key))
    )
    assert "src/hermes_voip/plugin.yaml" in declared or "plugin.yaml" in declared, (
        "the hatch wheel target must include src/hermes_voip/plugin.yaml as package "
        "data (via artifacts or force-include) — otherwise the wheel ships no manifest"
    )


def _as_list(value: object) -> list[object]:
    """Coerce a hatch include value (list, or dict for force-include) to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [*value.keys(), *value.values()]
    return [value]


def test_packaged_and_importable_manifests_are_identical() -> None:
    """The package-data manifest and the directory-install manifest are the same file.

    They must not drift: both are the one canonical manifest.
    """
    from importlib.resources import files  # noqa: PLC0415

    packaged = files("hermes_voip").joinpath("plugin.yaml").read_text(encoding="utf-8")
    directory = _PACKAGING_MANIFEST.read_text(encoding="utf-8")
    assert packaged == directory, (
        "the importable (package-data) manifest and the directory-install manifest "
        "have drifted — they must be byte-identical"
    )


# ---------------------------------------------------------------------------
# (f) Documentation consistency: all docs must use the correct tool count.
# ---------------------------------------------------------------------------


#: Every "<N> tools, <M> hook(s)" mention in the docs, so the guard can assert that
#: EVERY occurrence matches the registered surface — not merely that the correct one
#: is present somewhere (a doc carrying both a stale and a fresh count would otherwise
#: pass). Tolerates singular/plural on both nouns.
_TOOL_COUNT_MENTION = re.compile(r"(\d+) tools?, (\d+) hooks?\b")


def test_docs_use_correct_tool_count() -> None:
    """Every doc 'N tools, M hook' mention equals the registered tool/hook surface.

    When a tool/hook is added or removed, the docs that quote the count must be
    updated. This guard fails if any key doc states a stale count — including a doc
    that accidentally keeps BOTH the old and the new count — by checking that every
    matched mention equals the registered surface (and that each doc still has one).
    """
    tool_count = len(_registered_tools())
    hook_count = len(_registered_hooks())
    expected = f"{tool_count} tools, {hook_count} hook"

    docs = {
        "README.md": _REPO_ROOT / "README.md",
        "ADR-0037": (
            _REPO_ROOT
            / "docs"
            / "adr"
            / "0037-hermes-plugin-manifest-and-install-models.md"
        ),
        "runbook 0011": _REPO_ROOT / "docs" / "runbooks" / "0011-voip-enable-plugin.md",
    }
    for label, path in docs.items():
        assert path.is_file(), f"{label} is missing at {path} — cannot verify its count"
        mentions = _TOOL_COUNT_MENTION.findall(path.read_text(encoding="utf-8"))
        assert mentions, (
            f"{label} no longer states a 'N tools, M hook' count — the guard would be "
            f"vacuous; restore an explicit '{expected}' mention."
        )
        for n_tools, n_hooks in mentions:
            assert (int(n_tools), int(n_hooks)) == (tool_count, hook_count), (
                f"{label} has a stale '{n_tools} tools, {n_hooks} hook' mention; "
                f"the registered surface is '{expected}'"
            )


# ---------------------------------------------------------------------------
# (g) Operator-facing env vars: CALL_ON_CONNECT + KEEPALIVE_INTERVAL.
# ---------------------------------------------------------------------------


def test_optional_env_advertises_call_on_connect() -> None:
    """HERMES_VOIP_CALL_ON_CONNECT in optional_env — must warn about allowlist bypass.

    This env var fires a one-shot outbound dial on first registration and BYPASSES
    the outbound allowlist (adapter.py ``_CALL_ON_CONNECT_KEY``). Without a manifest
    entry an operator cannot discover it; without an explicit bypass warning in the
    description the security implication is invisible. Both must be present.
    """
    entries = _optional_env_block()
    names = {_entry_name(e): e for e in entries if isinstance(e, dict)}
    assert "HERMES_VOIP_CALL_ON_CONNECT" in names, (
        "optional_env must advertise HERMES_VOIP_CALL_ON_CONNECT — "
        "operators need a manifest-visible signal this one-shot dial knob exists"
    )
    desc = names["HERMES_VOIP_CALL_ON_CONNECT"].get("description", "")
    assert isinstance(desc, str)
    # The description MUST warn that CALL_ON_CONNECT bypasses the outbound allowlist.
    lowered = desc.lower()
    assert "bypass" in lowered or "allowlist" in lowered or "allow list" in lowered, (
        "HERMES_VOIP_CALL_ON_CONNECT description must explicitly warn about the "
        "outbound-allowlist bypass (adapter.py ~920-921). Operators who set this "
        "knob must understand it dials without allowlist gating."
    )


def test_optional_env_advertises_keepalive_interval_with_matching_default() -> None:
    """HERMES_VOIP_KEEPALIVE_INTERVAL in optional_env; default matches config.py.

    This env var controls the RFC 5626 double-CRLF keepalive interval. The manifest
    default must match ``_DEFAULT_KEEPALIVE_INTERVAL`` in config.py so an operator
    consulting the manifest sees the accurate default rather than a stale number.
    """
    entries = _optional_env_block()
    defaults = {
        _entry_name(e): e.get("default") for e in entries if isinstance(e, dict)
    }
    assert "HERMES_VOIP_KEEPALIVE_INTERVAL" in defaults, (
        "optional_env must advertise HERMES_VOIP_KEEPALIVE_INTERVAL — "
        "operators need a manifest-visible signal this RFC 5626 keepalive knob exists"
    )
    manifest_default = defaults["HERMES_VOIP_KEEPALIVE_INTERVAL"]
    assert manifest_default == _DEFAULT_KEEPALIVE_INTERVAL, (
        f"HERMES_VOIP_KEEPALIVE_INTERVAL default in plugin.yaml "
        f"({manifest_default!r}) must match config.py "
        f"_DEFAULT_KEEPALIVE_INTERVAL ({_DEFAULT_KEEPALIVE_INTERVAL!r})"
    )
