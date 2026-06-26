"""Reject tracked PostgreSQL/Timescale exposure footguns."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Violation:
    """A database exposure guardrail violation."""

    rule_id: str
    path: Path
    line: int
    message: str


_SCAN_EXACT_NAMES = frozenset(
    {
        ".pre-commit-config.yaml",
        "pyproject.toml",
    }
)
_SCAN_SUFFIXES = (
    ".conf",
    ".dockerfile",
    ".env.example",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
)
_SKIP_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
    }
)
_INTENTIONAL_FIXTURE_PATHS = frozenset({Path("tests/test_database_exposure_guard.py")})
_INSECURE_POSTGRES_DEFAULTS = frozenset(
    {
        "",
        "changeme",
        "default",
        "example",
        "password",
        "postgres",
        "test",
    }
)
_DB_CONTEXT_RE = re.compile(
    r"\b(database_url|postgres(?:ql)?|timescale(?:db)?)\b", re.IGNORECASE
)
_STALE_DEVCONTAINER_RE = re.compile(
    r"\bpostgresql-client\b|\bpg_isready\s+-h\s+timescale\b|ms-ossdata\.vscode-pgsql",
    re.IGNORECASE,
)
_FORWARD_PORT_RE = re.compile(r"\b(?:forwardPorts|appPort)\b", re.IGNORECASE)
_POSTGRES_ENV_RE = re.compile(
    r"[\"']?\b(?P<name>POSTGRES_(?:USER|DB|PASSWORD|HOST_AUTH_METHOD))\b[\"']?\s*(?::|=)\s*[\"']?(?P<value>[^\s,#\]}\"']+)",
    re.IGNORECASE,
)
_LISTEN_ALL_RE = re.compile(
    r"\blisten_addresses\b\s*(?:=|:)\s*[\"']?(?:\*|0\.0\.0\.0|::)[\"']?",
    re.IGNORECASE,
)
_PG_HBA_PUBLIC_RE = re.compile(
    r"^\s*host(?:ssl|nossl)?\s+\S+\s+\S+\s+(?:0\.0\.0\.0/0|::/0)\s+(?:trust|md5|password|scram-sha-256)\b",
    re.IGNORECASE,
)
_SHORT_PORT_RE = re.compile(
    r"(?:^|[\s\"'])"
    r"(?:(?:\[[0-9a-f:.]+\]|(?:\d{1,3}\.){3}\d{1,3}):)?"
    r"(?P<published>\d+):(?P<target>5432)(?:[/\"'\s]|$)"
)
_PUBLISHED_RE = re.compile(
    r"\bpublished\s*:\s*[\"']?(?P<port>\d+)[\"']?", re.IGNORECASE
)
_TARGET_RE = re.compile(r"\btarget\s*:\s*[\"']?5432[\"']?", re.IGNORECASE)
_POSTGRES_PORT = 5432
_NUMERIC_5432_RE = re.compile(r"(?<!\d)5432(?!\d)")
_COMPOSE_FILE_RE = re.compile(r"(?:^|/)(?:docker-)?compose[^/]*\.ya?ml$", re.IGNORECASE)
_ENV_FILE_RE = re.compile(r"(?:^|/)\.env(?:\..+)?$")


def scan_repository(root: Path) -> tuple[Violation, ...]:
    """Scan repository files under ``root`` for database exposure footguns."""
    root = root.resolve()
    violations: list[Violation] = []
    for path in _candidate_paths(root):
        relative_path = path.relative_to(root)
        if _is_skipped(relative_path) or not _is_scanned_path(relative_path):
            continue
        if relative_path in _INTENTIONAL_FIXTURE_PATHS:
            continue
        text = _read_text(path)
        if text is None:
            continue
        lines = text.splitlines()
        violations.extend(_scan_lines(relative_path, lines))
    return tuple(violations)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the guardrail scanner."""
    args = tuple(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path.cwd()
    violations = scan_repository(root)
    for violation in violations:
        sys.stderr.write(
            f"{violation.path.as_posix()}:{violation.line}: "
            f"{violation.rule_id}: {violation.message}\n"
        )
    return 1 if violations else 0


def _candidate_paths(root: Path) -> Iterator[Path]:
    git_files = _git_tracked_paths(root)
    if git_files is not None:
        yield from git_files
        return
    yield from (path for path in root.rglob("*") if path.is_file())


def _git_tracked_paths(root: Path) -> tuple[Path, ...] | None:
    git_path = shutil.which("git")
    if git_path is None:
        return None
    try:
        # Fixed git subcommand over a local repo root; no shell or dynamic flags.
        result = subprocess.run(  # noqa: S603
            (git_path, "-C", str(root), "ls-files", "-z"),
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    decoded = result.stdout.decode("utf-8", errors="surrogateescape")
    return tuple(root / name for name in decoded.split("\0") if name)


def _is_skipped(relative_path: Path) -> bool:
    parts = set(relative_path.parts)
    if parts & _SKIP_PARTS:
        return True
    return relative_path.name == ".env" or relative_path.as_posix().endswith("/.env")


def _is_scanned_path(relative_path: Path) -> bool:
    path_text = relative_path.as_posix()
    if _ENV_FILE_RE.search(path_text):
        return True
    if path_text.startswith((".devcontainer/", ".github/workflows/", "tools/")):
        return True
    if _COMPOSE_FILE_RE.search(path_text):
        return True
    if relative_path.name in _SCAN_EXACT_NAMES:
        return True
    if relative_path.name == "Dockerfile" or relative_path.name.startswith(
        "Dockerfile."
    ):
        return True
    return path_text.endswith(_SCAN_SUFFIXES)


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def _scan_lines(relative_path: Path, lines: Sequence[str]) -> Iterator[Violation]:
    yield from _scan_stale_devcontainer(relative_path, lines)
    yield from _scan_public_ports(relative_path, lines)
    yield from _scan_postgres_defaults(relative_path, lines)
    yield from _scan_public_auth_config(relative_path, lines)


def _scan_stale_devcontainer(
    relative_path: Path, lines: Sequence[str]
) -> Iterator[Violation]:
    if not relative_path.as_posix().startswith(".devcontainer/"):
        return
    for line_number, line in _numbered(lines):
        if _STALE_DEVCONTAINER_RE.search(line):
            yield Violation(
                "DB001",
                relative_path,
                line_number,
                "remove stale PostgreSQL/Timescale devcontainer affordance",
            )


def _scan_public_ports(
    relative_path: Path, lines: Sequence[str]
) -> Iterator[Violation]:
    for line_number, line in _numbered(lines):
        if _nearby_forward_port_key(lines, line_number) and _NUMERIC_5432_RE.search(
            line
        ):
            yield from _forwarded_port_violations(relative_path, line_number, line)
        if _line_publishes_postgres(line) and _has_database_context(lines, line_number):
            yield Violation(
                "DB002",
                relative_path,
                line_number,
                "do not publish PostgreSQL port 5432 from tracked config",
            )
        if _published_postgres_target(lines, line_number):
            yield Violation(
                "DB002",
                relative_path,
                line_number,
                "do not publish PostgreSQL port 5432 from tracked config",
            )


def _forwarded_port_violations(
    relative_path: Path, line_number: int, line: str
) -> Iterator[Violation]:
    reported_endpoints: set[str] = set()
    for match in _NUMERIC_5432_RE.finditer(line):
        endpoint = _forwarded_endpoint(line, match.start())
        if endpoint in reported_endpoints:
            continue
        reported_endpoints.add(endpoint)
        yield Violation(
            "DB002",
            relative_path,
            line_number,
            "do not auto-forward PostgreSQL port 5432",
        )


def _forwarded_endpoint(line: str, match_start: int) -> str:
    start = match_start
    while start > 0 and line[start - 1] not in "[, ":
        start -= 1
    end = match_start
    while end < len(line) and line[end] not in ",] ":
        end += 1
    return line[start:end]


def _line_publishes_postgres(line: str) -> bool:
    match = _SHORT_PORT_RE.search(line)
    if match is None:
        return False
    published = int(match.group("published"))
    return published != 0


def _published_postgres_target(lines: Sequence[str], line_number: int) -> bool:
    line = lines[line_number - 1]
    if _PUBLISHED_RE.search(line) is None:
        return False
    return _has_nearby_match(
        lines, line_number, _TARGET_RE, radius=3
    ) and _has_database_context(lines, line_number)


def _nearby_forward_port_key(lines: Sequence[str], line_number: int) -> bool:
    return _has_nearby_match(lines, line_number, _FORWARD_PORT_RE, radius=3)


def _scan_postgres_defaults(
    relative_path: Path, lines: Sequence[str]
) -> Iterator[Violation]:
    for line_number, line in _numbered(lines):
        normalized_line = line.lstrip(" -\t\"'")
        match = _POSTGRES_ENV_RE.search(normalized_line)
        if match is None:
            continue
        name = match.group("name").upper()
        value = _normalize_value(match.group("value"))
        if name == "POSTGRES_HOST_AUTH_METHOD" and value == "trust":
            yield Violation(
                "DB003",
                relative_path,
                line_number,
                "do not use unsafe PostgreSQL authentication",
            )
        elif name in {"POSTGRES_USER", "POSTGRES_DB"} and value == "postgres":
            yield Violation(
                "DB003",
                relative_path,
                line_number,
                f"do not use default {name} value",
            )
        elif name == "POSTGRES_PASSWORD" and _is_insecure_password_value(value):
            yield Violation(
                "DB003",
                relative_path,
                line_number,
                "do not use default PostgreSQL password values",
            )


def _scan_public_auth_config(
    relative_path: Path, lines: Sequence[str]
) -> Iterator[Violation]:
    for line_number, line in _numbered(lines):
        if _LISTEN_ALL_RE.search(line):
            yield Violation(
                "DB004",
                relative_path,
                line_number,
                "do not configure PostgreSQL to listen on all interfaces",
            )
        if _PG_HBA_PUBLIC_RE.search(line.strip("\"'")):
            yield Violation(
                "DB004",
                relative_path,
                line_number,
                "do not allow public PostgreSQL password pg_hba entries",
            )


def _has_database_context(lines: Sequence[str], line_number: int) -> bool:
    return _has_nearby_match(lines, line_number, _DB_CONTEXT_RE, radius=40)


def _has_nearby_match(
    lines: Sequence[str], line_number: int, pattern: re.Pattern[str], *, radius: int
) -> bool:
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    return any(pattern.search(lines[index - 1]) for index in range(start, end + 1))


def _is_insecure_password_value(value: str) -> bool:
    if value.startswith("${") and ":-" not in value and "-" not in value:
        return False
    if value.startswith("${"):
        default = value.rsplit("-", maxsplit=1)[-1].rstrip("}").strip().lower()
        return default in _INSECURE_POSTGRES_DEFAULTS
    return value in _INSECURE_POSTGRES_DEFAULTS


def _normalize_value(value: str) -> str:
    return value.strip().strip(",]}\"'").lower()


def _numbered(lines: Iterable[str]) -> Iterator[tuple[int, str]]:
    yield from enumerate(lines, start=1)


if __name__ == "__main__":
    raise SystemExit(main())
