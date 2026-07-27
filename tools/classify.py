"""Classify a change by blast radius.

Prints the change class (R0-R3) and the gates that class requires, per
``docs/standards/engineering.md``. The class is derived mechanically from the diff
against the merge base: an agent does not argue with it and cannot lower it.

Two inputs decide the class:

* a **path map** — the most severe pattern matched by any changed path wins;
* an **R0 content test** — R0 is a property of diff *content*, not of paths, so it is
  only emitted when every changed path is provably non-executable.

The map errs upward. Anything unmatched is R1, never R0, because under-classifying a
change is the only failure mode of this script that matters.

Usage::

    uv run python -m tools.classify              # against the merge base with main
    uv run python -m tools.classify --base HEAD~1
    uv run python -m tools.classify --paths a.py b.md   # classify an explicit list
"""

from __future__ import annotations

import argparse
import enum
import subprocess
import sys
from collections.abc import Sequence
from fnmatch import fnmatch

__all__ = ["Class", "classify", "classify_paths", "main"]


class Class(enum.IntEnum):
    """Blast-radius class. Ordered, so the most severe match wins."""

    R0 = 0
    R1 = 1
    R2 = 2
    R3 = 3

    def __str__(self) -> str:
        """Return the bare class name, e.g. ``R2``."""
        return self.name


# --- The path map -------------------------------------------------------------------
#
# Ordered most-severe first. The first list whose pattern matches a path decides that
# path's class; the highest class across all changed paths decides the change.

_R3_PATTERNS: tuple[str, ...] = (
    # Publishing: a v*.*.* tag uploads to PyPI and cuts a public GitHub Release.
    ".github/workflows/publish.yml",
    # Legal and licence content.
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    # Secret-scanning policy: weakening it is how a leak reaches a public repo.
    ".gitleaks.toml",
    # SIP digest authentication — credential construction and comparison.
    "src/hermes_voip/digest.py",
)

_R2_PATTERNS: tuple[str, ...] = (
    # --- Apply-trigger paths. A release tag publishes from these, so they carry the
    # blast radius of the publish, not of their own diff.
    "pyproject.toml",
    "uv.lock",
    "packaging/**",
    # --- Self-referential: the policy, its classifier, and the machinery that runs
    # them. Listed explicitly so a change cannot lower its own class.
    "AGENTS.md",
    "CLAUDE.md",
    "docs/standards/**",
    "tools/**",
    ".github/**",
    ".claude/**",
    ".pre-commit-config.yaml",
    "conftest.py",
    # --- Signalling, media and transport: the product surface. A defect here drops
    # calls, leaks audio, or strands a dialog.
    "src/hermes_voip/adapter.py",
    "src/hermes_voip/sip.py",
    "src/hermes_voip/sdp.py",
    "src/hermes_voip/rtp.py",
    "src/hermes_voip/rtcp.py",
    "src/hermes_voip/dialog.py",
    "src/hermes_voip/registration.py",
    "src/hermes_voip/call.py",
    "src/hermes_voip/call_context.py",
    "src/hermes_voip/call_end.py",
    "src/hermes_voip/session_timer.py",
    "src/hermes_voip/keepalive.py",
    "src/hermes_voip/refer.py",
    "src/hermes_voip/originate.py",
    "src/hermes_voip/message.py",
    "src/hermes_voip/incall.py",
    "src/hermes_voip/manager.py",
    "src/hermes_voip/dtmf*.py",
    "src/hermes_voip/transport/**",
    "src/hermes_voip/media/**",
    # --- Security guards: injection defence, caller identity, outbound authorisation.
    "src/hermes_voip/guard/**",
    "src/hermes_voip/caller_modes.py",
    "src/hermes_voip/outbound_allow.py",
    "src/hermes_voip/notice_filter.py",
    # --- Config and env-var surface. Drift across the three registries is a recorded
    # failure mode, and it is caught only by the full suite.
    "src/hermes_voip/config.py",
    "src/hermes_voip/plugin.py",
    "src/hermes_voip/plugin.yaml",
    "src/hermes_voip/manifest.py",
    ".env.example",
    # --- External providers: credentials and third-party service surface.
    "src/hermes_voip/providers/**",
    "src/hermes_voip/stt/**",
    "src/hermes_voip/tts/**",
    # --- Public API consumed by the Hermes runtime.
    "src/hermes_voip/voip_tools.py",
    "src/hermes_voip/hermes_surface.py",
    # --- Tests that assert a safety invariant rather than product behaviour. Changing
    # one is a policy change, so it is gated like one.
    "tests/test_no_vendor_identifiers.py",
    "tests/test_database_exposure_guard.py",
    "tests/test_plugin_manifest.py",
    "tests/test_register.py",
    "tests/test_backlog_wave10_checkoff.py",
    "tests/test_workflow_action_pins.py",
    "tests/test_supply_chain_*.py",
    "tests/test_extra_dep_ranges.py",
    "tests/test_pytest_hang_safety_net.py",
    "tests/test_wheel_packaging.py",
    "tests/test_packaging.py",
    "tests/test_register_skills.py",
    "tests/test_classify.py",
)

# Files with no executable or policy effect. A change touching only these is R0.
_R0_PATTERNS: tuple[str, ...] = (
    "docs/backlog.md",
    "CHANGELOG.md",
    "docs/plan/**",
)

_GATES: dict[Class, tuple[str, ...]] = {
    Class.R0: (
        "CI only. Batch freely.",
        "No issue, no plan, no test-first, no review, no ADR.",
    ),
    Class.R1: (
        "Focused test file while working; CI runs the rest.",
        "One diff-only independent review pass.",
        "Failing test first for behavioural changes.",
        "Agent merges on green. Related changes may share a PR.",
    ),
    Class.R2: (
        "Governing issue, backlog item or user request.",
        "Failing test first.",
        "Full local gate before the PR, and CI.",
        "Cross-vendor review with context scoped to the touched surface.",
        "Human approval. Docs updated if behaviour changed.",
        "ADR if it constrains future changes; runbook if it changes a real resource.",
        "One logical change per PR.",
    ),
    Class.R3: (
        "Everything R2 requires, plus:",
        "Specialist review and dual control.",
        "A rehearsed recovery path.",
        "Explicit operator approval before the action.",
    ),
}


def _matches(path: str, patterns: Sequence[str]) -> bool:
    """Return True if ``path`` matches any glob in ``patterns``.

    ``**`` is treated as a prefix match so ``a/**`` covers ``a/b/c.py``, which
    :func:`fnmatch.fnmatch` alone does not do.
    """
    for pattern in patterns:
        if pattern.endswith("/**"):
            if path == pattern[:-3] or path.startswith(pattern[:-2]):
                return True
        elif fnmatch(path, pattern):
            return True
    return False


def classify_paths(paths: Sequence[str]) -> Class:
    """Classify a change from its changed paths alone, ignoring diff content.

    Returns the most severe class matched by any path. An empty change is R0; an
    unmatched path is R1.

    Raises:
        ValueError: if a path contains a newline. ``git diff --name-only`` never
            emits one, so it means several paths arrived as a single argument —
            usually an unquoted shell expansion under a shell that does not
            word-split. That would match no pattern and silently classify a real
            change as R1, so it fails loudly instead.
    """
    if not paths:
        return Class.R0
    worst = Class.R0
    for path in paths:
        if "\n" in path:
            message = (
                f"path contains a newline, so several paths were passed as one "
                f"argument and would silently under-classify: {path!r}"
            )
            raise ValueError(message)
        if _matches(path, _R3_PATTERNS):
            per_path = Class.R3
        elif _matches(path, _R2_PATTERNS):
            per_path = Class.R2
        elif _matches(path, _R0_PATTERNS):
            per_path = Class.R0
        else:
            per_path = Class.R1
        worst = max(worst, per_path)
    return worst


def _is_non_executable_line(line: str) -> bool:
    """Return True if a changed diff line provably has no executable effect.

    Only blank lines and whole-line ``#`` comments qualify. A line that merely
    *contains* a ``#`` does not: it may be trailing-comment-on-code, or a ``#``
    inside a string.
    """
    body = line[1:].strip()
    return body == "" or body.startswith("#")


def classify(paths: Sequence[str], diff_lines: Sequence[str] = ()) -> Class:
    """Classify a change from its paths and, for the R0 test, its diff content.

    ``diff_lines`` are the raw ``git diff`` lines. They are consulted only to
    downgrade an otherwise-R1 change to R0 when every changed line is a blank line
    or a whole-line comment in a Python file. Anything that cannot be proven
    non-executable keeps its path-derived class.
    """
    by_path = classify_paths(paths)
    if by_path is not Class.R1 or not diff_lines:
        return by_path
    if not all(path.endswith(".py") for path in paths):
        return by_path
    changed = [
        line
        for line in diff_lines
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    ]
    if not changed:
        return by_path
    if all(_is_non_executable_line(line) for line in changed):
        return Class.R0
    return by_path


def _git(args: Sequence[str]) -> str:
    """Run a git command and return its stdout, raising on a non-zero exit."""
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607 - git is resolved from PATH by design
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8", errors="surrogateescape")


def _merge_base(base: str | None) -> str:
    """Resolve the ref to diff against.

    Uses ``base`` when given. Otherwise finds the merge base with ``origin/main``,
    falling back to ``main`` for a repository without that remote ref.
    """
    if base is not None:
        return base
    for ref in ("origin/main", "main"):
        try:
            return _git(["merge-base", "HEAD", ref]).strip()
        except subprocess.CalledProcessError:
            continue
    message = "cannot resolve a merge base against origin/main or main; pass --base"
    raise SystemExit(message)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the class and its gates for the current change. Returns the exit code."""
    parser = argparse.ArgumentParser(
        prog="tools.classify",
        description="Classify a change by blast radius (see docs/standards/"
        "engineering.md).",
    )
    parser.add_argument("--base", help="ref to diff against (default: merge base)")
    parser.add_argument(
        "--paths",
        nargs="*",
        help="classify this explicit path list instead of reading the diff",
    )
    args = parser.parse_args(argv)

    if args.paths is not None:
        change = classify_paths(args.paths)
        paths: list[str] = list(args.paths)
    else:
        base = _merge_base(args.base)
        paths = [p for p in _git(["diff", "--name-only", base]).splitlines() if p]
        diff_lines = _git(["diff", "--unified=0", base]).splitlines()
        change = classify(paths, diff_lines)

    out = sys.stdout
    out.write(f"{change}\n\n")
    for gate in _GATES[change]:
        out.write(f"  - {gate}\n")
    out.write(f"\n{len(paths)} changed path(s)\n")
    for path in sorted(paths):
        out.write(f"  {classify_paths([path])}  {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
