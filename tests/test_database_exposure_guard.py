"""Database exposure guardrail tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.check_database_exposure import Violation, main, scan_repository


def _write_file(root: Path, relative_path: str, content: str) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _rule_ids(violations: tuple[Violation, ...]) -> list[str]:
    return [violation.rule_id for violation in violations]


def test_rejects_stale_devcontainer_database_clients(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        ".devcontainer/Dockerfile",
        "RUN apt-get update && apt-get install -y postgresql-client\n",
    )
    _write_file(
        tmp_path,
        ".devcontainer/post-start.sh",
        "pg_isready -h timescale -q 2>/dev/null && break\n",
    )
    _write_file(
        tmp_path,
        ".devcontainer/devcontainer.json",
        '{"customizations":{"vscode":{"extensions":["ms-ossdata.vscode-pgsql"]}}}\n',
    )

    violations = scan_repository(tmp_path)

    assert _rule_ids(violations) == ["DB001", "DB001", "DB001"]
    assert {violation.path.as_posix() for violation in violations} == {
        ".devcontainer/Dockerfile",
        ".devcontainer/post-start.sh",
        ".devcontainer/devcontainer.json",
    }


@pytest.mark.parametrize(
    "compose_text",
    [
        """
services:
  db:
    image: postgres:18
    ports:
      - "5432:5432"
""",
        """
services:
  timescale:
    image: timescale/timescaledb:latest-pg18
    ports:
      - "0.0.0.0:5432:5432"
""",
        """
services:
  db:
    image: postgres:18
    ports:
      - "[::]:5432:5432"
""",
        """
services:
  db:
    image: postgres:18
    ports:
      - target: 5432
        published: 5432
""",
        """
services:
  db:
    image: postgres:18
    ports:
      - target: 5432
        published: 15432
""",
        """
services:
  db:
    image: postgres:18
    ports:
      - target: 5432
""",
        """
services:
  db:
    image: postgres:18
    ports:
      - "203.0.113.10:15432:5432"
""",
        """
services:
  db:
    image: postgres:18
    ports:
      - "[FE80::1]:15432:5432"
""",
        """
services:
  db:
    image: postgres:18
    ports:
      - "5432"
""",
        """
services:
  db:
    image: postgres:18
    ports:
      - 5432
""",
        """
services:
  db:
    image: postgres:18
    ports:
      - "0:5432"
""",
        """
services:
  db:
    image: postgres:18
    environment:
      SAFE_01: value
      SAFE_02: value
      SAFE_03: value
      SAFE_04: value
      SAFE_05: value
      SAFE_06: value
      SAFE_07: value
      SAFE_08: value
      SAFE_09: value
      SAFE_10: value
      SAFE_11: value
      SAFE_12: value
      SAFE_13: value
      SAFE_14: value
      SAFE_15: value
      SAFE_16: value
      SAFE_17: value
      SAFE_18: value
      SAFE_19: value
      SAFE_20: value
      SAFE_21: value
      SAFE_22: value
      SAFE_23: value
      SAFE_24: value
      SAFE_25: value
      SAFE_26: value
      SAFE_27: value
      SAFE_28: value
      SAFE_29: value
      SAFE_30: value
      SAFE_31: value
      SAFE_32: value
      SAFE_33: value
      SAFE_34: value
      SAFE_35: value
      SAFE_36: value
      SAFE_37: value
      SAFE_38: value
      SAFE_39: value
      SAFE_40: value
      SAFE_41: value
      SAFE_42: value
      SAFE_43: value
      SAFE_44: value
      SAFE_45: value
      SAFE_46: value
      SAFE_47: value
      SAFE_48: value
      SAFE_49: value
      SAFE_50: value
    ports:
      - "15432:5432"
""",
        """
services:
  api:
    image: example/api:latest
    ports:
      - "15432:5432"
    environment:
      DATABASE_URL: postgresql://user@db/app
""",
    ],
)
def test_rejects_public_postgres_port_publish(
    tmp_path: Path, compose_text: str
) -> None:
    _write_file(tmp_path, "docker-compose.yml", compose_text)

    violations = scan_repository(tmp_path)

    assert "DB002" in _rule_ids(violations)


def test_rejects_devcontainer_forwarded_postgres_port(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        ".devcontainer/devcontainer.json",
        """
{
  "forwardPorts": [
    5432
  ],
  "appPort": [
    "127.0.0.1:5432:5432"
  ]
}
""",
    )

    violations = scan_repository(tmp_path)

    assert _rule_ids(violations) == ["DB002", "DB002"]


def test_rejects_devcontainer_forwarded_postgres_port_after_long_array(
    tmp_path: Path,
) -> None:
    _write_file(
        tmp_path,
        ".devcontainer/devcontainer.json",
        """
{
  "forwardPorts": [
    8001,
    8002,
    8003,
    8004,
    8005,
    8006,
    8007,
    8008,
    8009,
    8010,
    8011,
    8012,
    8013,
    8014,
    8015,
    8016,
    8017,
    8018,
    8019,
    8020,
    8021,
    8022,
    8023,
    8024,
    8025,
    5432
  ]
}
""",
    )

    violations = scan_repository(tmp_path)

    assert _rule_ids(violations) == ["DB002"]


@pytest.mark.parametrize(
    "config_text",
    [
        "POSTGRES_USER=postgres\n",
        "POSTGRES_DB: postgres\n",
        "POSTGRES_PASSWORD=postgres\n",
        "POSTGRES_PASSWORD: password\n",
        '{"POSTGRES_PASSWORD": "postgres"}\n',
        '{"POSTGRES_PASSWORD": ""}\n',
        "- POSTGRES_PASSWORD=changeme\n",
        "POSTGRES_HOST_AUTH_METHOD=trust\n",
        "POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-postgres}\n",
    ],
)
def test_rejects_default_postgres_credentials(tmp_path: Path, config_text: str) -> None:
    _write_file(tmp_path, "compose.yml", config_text)

    violations = scan_repository(tmp_path)

    assert "DB003" in _rule_ids(violations)


@pytest.mark.parametrize(
    "config_text",
    [
        "listen_addresses = '*'\n",
        "command: ['postgres', '-c', 'listen_addresses=*']\n",
        "host all all 0.0.0.0/0 trust\n",
        "hostssl all all ::/0 md5\n",
        "host all all 0.0.0.0/0 password\n",
        "host all all 0.0.0.0/0 scram-sha-256\n",
        '- "host all all 0.0.0.0/0 scram-sha-256"\n',
    ],
)
def test_rejects_public_postgres_auth_config(tmp_path: Path, config_text: str) -> None:
    _write_file(tmp_path, "docker-compose.yml", config_text)

    violations = scan_repository(tmp_path)

    assert "DB004" in _rule_ids(violations)


def test_allows_bare_5432_without_ports_or_database_context(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "config.yml",
        "retry_after_ms: 5432\nbatch:\n  - 5432\n  - 8080\n",
    )

    assert scan_repository(tmp_path) == ()


def test_rejects_tracked_postgres_conf_public_auth(tmp_path: Path) -> None:
    _write_file(tmp_path, "pg_hba.conf", "host all all 0.0.0.0/0 scram-sha-256\n")

    violations = scan_repository(tmp_path)

    assert _rule_ids(violations) == ["DB004"]


def test_allows_safe_private_and_non_database_literals(tmp_path: Path) -> None:
    _write_file(
        tmp_path,
        "docker-compose.yml",
        """
services:
  app:
    image: example/app:latest
    ports:
      - "8080:8080"
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
""",
    )
    _write_file(
        tmp_path,
        "tests/test_media_ice.py",
        "candidate = '203.0.113.5 54321 typ srflx raddr 192.0.2.1 rport 54320'\n",
    )
    _write_file(
        tmp_path,
        "docs/example.md",
        "The fake gateway is pbx.example.test and no database is configured.\n",
    )

    assert scan_repository(tmp_path) == ()


def test_rejects_tracked_env_variant_defaults(tmp_path: Path) -> None:
    _write_file(tmp_path, ".env.ci", "POSTGRES_PASSWORD=postgres\n")

    violations = scan_repository(tmp_path)

    assert _rule_ids(violations) == ["DB003"]


def test_ignores_untracked_secret_env_files(tmp_path: Path) -> None:
    _write_file(tmp_path, ".env", "POSTGRES_PASSWORD=postgres\n")
    _write_file(tmp_path, ".devcontainer/.env", "POSTGRES_HOST_AUTH_METHOD=trust\n")

    assert scan_repository(tmp_path) == ()


def test_scan_is_robust_to_inherited_git_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Git hooks (pre-commit/pre-push) export GIT_DIR/GIT_WORK_TREE into the
    # environment. A naive ``git -C <root> ls-files`` is hijacked by those vars
    # and lists the hook's repo instead of <root>, so the scanner would silently
    # examine the wrong tree and miss real violations. The scan must depend only
    # on its ``root`` argument, never on inherited git context.
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("GIT_DIR", str(repo_root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo_root))
    _write_file(tmp_path, ".env.ci", "POSTGRES_PASSWORD=postgres\n")

    violations = scan_repository(tmp_path)

    assert _rule_ids(violations) == ["DB003"]


def test_cli_reports_relative_paths_line_numbers_and_rule_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_file(tmp_path, "compose.yml", "POSTGRES_HOST_AUTH_METHOD=trust\n")

    exit_code = main([str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "compose.yml:1: DB003" in captured.err
    assert "trust" not in captured.err.lower()


def test_cli_exits_zero_for_safe_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_file(tmp_path, "compose.yml", "services: {}\n")

    exit_code = main([str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
