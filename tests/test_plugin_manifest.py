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
  re-exports :func:`register` — the two canonical install models (ADR-0034).

The manifest schema (keys, ``requires_env`` rich format with ``secret:`` (NOT
``password:``), the ``kind`` values, and the fact that an ENTRY-POINT plugin never
surfaces this metadata so the DIRECTORY manifest is what ``hermes plugins list`` /
``/plugins`` read) is verified against the installed hermes-agent 0.16.0 source in
docs/runbooks/0011-voip-enable-plugin.md.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import yaml

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
    return cast("dict[str, object]", data)


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


def test_manifest_version_matches_pyproject() -> None:
    """The manifest version must track pyproject.toml (no silent drift)."""
    manifest = _load_manifest(_PACKAGING_MANIFEST)
    assert manifest.get("version") == _pyproject_version(), (
        "plugin.yaml version must match pyproject.toml [project].version"
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


def _env_entry_names(entries: object) -> list[str]:
    """Extract env-var names from a requires_env/optional_env list (str|dict form)."""
    assert isinstance(entries, list)
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            names.append(entry)
        else:
            assert isinstance(entry, dict), "env entry must be a str or a dict"
            name = entry.get("name")
            assert isinstance(name, str), f"rich env entry needs a string name: {entry}"
            names.append(name)
    return names


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

    Verified against hermes-agent 0.16.0: the rich entry field is ``secret`` (there
    is NO ``password`` field); ``secret: true`` drives the masked prompt.
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


def test_optional_env_advertises_transport_and_provider_keys() -> None:
    """optional_env documents the non-gating knobs (port/transport + provider keys).

    These are NOT in requires_env (they have safe defaults / are provider-conditional,
    so requiring them would wrongly disable the plugin), but the operator should see
    them in the install prompt. The cloud provider keys are marked secret.
    """
    manifest = _load_manifest(_PACKAGING_MANIFEST)
    optional = manifest.get("optional_env")
    assert optional is not None, "optional_env should advertise the non-gating knobs"
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


# ---------------------------------------------------------------------------
# (d) PUBLIC-repo invariant: the manifest contains env-var NAMES only — no real
#     host/extension/password/IP VALUES. A coarse but effective leak guard.
# ---------------------------------------------------------------------------


def test_manifest_leaks_no_secret_values() -> None:
    """No secret VALUES anywhere in the manifest — only fakes in descriptions."""
    raw = _PACKAGING_MANIFEST.read_text(encoding="utf-8")
    lowered = raw.lower()
    # Nothing that looks like a real internal host or the operator's real test
    # extension may appear. (The env-var NAMES themselves are always fine.)
    forbidden_markers = (
        ".internal",
        ".lan",
        ".corp",
        "sip:1137",  # the operator's real test extension must never appear
        "1137",
    )
    for marker in forbidden_markers:
        assert marker not in lowered, (
            f"manifest appears to leak a real identifier: {marker!r}"
        )


# ---------------------------------------------------------------------------
# (e) Two install models (ADR-0034):
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
    """plugin.yaml ships inside the hermes_voip wheel (importlib.resources).

    So a pip install can surface / extract the manifest, and the directory-install
    copy can be sourced from the installed package rather than the repo.
    """
    from importlib.resources import files  # noqa: PLC0415

    resource = files("hermes_voip").joinpath("plugin.yaml")
    assert resource.is_file(), (
        "plugin.yaml must ship as hermes_voip package data (wheel force-include)"
    )
    data = yaml.safe_load(resource.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("name") == "hermes-voip"


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
