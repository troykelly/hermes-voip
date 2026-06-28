"""Backlog check for cartesia manifest credential parity."""

from __future__ import annotations

from pathlib import Path

import yaml

PLUGIN_PATHS = (
    Path(
        "/workspaces/hermes-voip/.claude/worktrees/wf_f8629796-43c-4/src/hermes_voip/plugin.yaml"
    ),
    Path(
        "/workspaces/hermes-voip/.claude/worktrees/wf_f8629796-43c-4/packaging/hermes-plugins/hermes-voip/plugin.yaml"
    ),
)


def test_cartesia_api_key_is_listed_as_optional_env() -> None:
    for plugin_path in PLUGIN_PATHS:
        manifest = yaml.safe_load(plugin_path.read_text(encoding="utf-8"))
        optional_env = manifest["optional_env"]
        names = {entry["name"] for entry in optional_env}
        assert "HERMES_VOIP_TTS_PROVIDER" in names
        assert "HERMES_VOIP_CARTESIA_API_KEY" in names
