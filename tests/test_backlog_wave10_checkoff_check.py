"""Backlog check for cartesia manifest credential parity."""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_MANIFEST = _REPO_ROOT / "src" / "hermes_voip" / "plugin.yaml"
_PACKAGING_MANIFEST = (
    _REPO_ROOT / "packaging" / "hermes-plugins" / "hermes-voip" / "plugin.yaml"
)
_MANIFEST_PATHS = (_SOURCE_MANIFEST, _PACKAGING_MANIFEST)
_CARTESIA_API_KEY = "HERMES_VOIP_CARTESIA_API_KEY"
_TTS_PROVIDER = "HERMES_VOIP_TTS_PROVIDER"


def _optional_env_entries(manifest_path: Path) -> dict[str, dict[str, object]]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict), f"{manifest_path} must parse to a mapping"
    optional_env = manifest.get("optional_env")
    assert isinstance(optional_env, list), (
        f"{manifest_path} optional_env must be a list"
    )

    entries: dict[str, dict[str, object]] = {}
    for entry in optional_env:
        assert isinstance(entry, dict), (
            f"{manifest_path} optional_env entries must be mappings"
        )
        name = entry.get("name")
        assert isinstance(name, str), (
            f"{manifest_path} optional_env entry needs a string name"
        )
        entries[name] = entry
    return entries


def test_cartesia_api_key_is_listed_as_optional_env() -> None:
    cartesia_entries: dict[Path, dict[str, object]] = {}

    for manifest_path in _MANIFEST_PATHS:
        optional_env = _optional_env_entries(manifest_path)
        assert _TTS_PROVIDER in optional_env, (
            f"{manifest_path} optional_env must advertise {_TTS_PROVIDER}"
        )
        assert _CARTESIA_API_KEY in optional_env, (
            f"{manifest_path} optional_env must advertise {_CARTESIA_API_KEY}"
        )
        cartesia_entry = optional_env[_CARTESIA_API_KEY]
        assert cartesia_entry.get("secret") is True, (
            f"{manifest_path} {_CARTESIA_API_KEY} must be secret: true"
        )
        cartesia_entries[manifest_path] = cartesia_entry

    assert (
        cartesia_entries[_SOURCE_MANIFEST] == cartesia_entries[_PACKAGING_MANIFEST]
    ), (
        "src/hermes_voip/plugin.yaml and packaging/hermes-plugins/hermes-voip/"
        "plugin.yaml must keep the Cartesia optional_env entry identical"
    )
