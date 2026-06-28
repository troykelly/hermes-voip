"""Fail-loud guard for the optional-extras licence gate (AGENTS.md rule 37).

The ``supply-chain`` workflow parses the optional-extras runtime package set out
of ``uv export --all-extras`` output by matching the ``# via hermes-voip``
provenance comment.  If that provenance comment format ever changes, the parser
silently yields an EMPTY package list — and the licence gate would then pass
without checking a single optional-extra licence (a false green).

This module is the single, unit-tested decision the workflow delegates to.  It
compares two facts:

* whether ``pyproject.toml`` declares any ``[project.optional-dependencies]``
  package (counted across every extra — not the truthiness of the value lists),
  and
* whether the parsed package list the workflow produced is non-empty.

From those it returns one of three outcomes:

* ``Decision.SKIP`` — no declared extra contains a package, so there is
  genuinely nothing to licence-check.  Legitimate skip.
* ``Decision.RUN`` — declared extras with packages AND a non-empty parsed list,
  so the workflow runs ``pip-licenses`` over the parsed list.
* ``GuardFailureError`` (raised) — declared extras WITH packages but the parsed list
  is empty.  The parser is broken; the gate must fail loudly rather than
  silently pass.

The workflow invokes ``main`` as ``python -m tools.check_optional_extras_guard
--pyproject pyproject.toml --packages optional-runtime-pkgs.txt`` and acts on the
``RUN``/``SKIP`` token printed to stdout (or the non-zero exit on failure).
"""

from __future__ import annotations

import argparse
import enum
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path


class Decision(enum.Enum):
    """The action the workflow should take after the optional-extras parse."""

    # Run ``pip-licenses`` over the parsed optional-extras package list.
    RUN = "RUN"
    # No declared optional-extra package exists; nothing to licence-check.
    SKIP = "SKIP"


class GuardFailureError(Exception):
    """Raised when declared optional-extras exist but the parsed list is empty.

    This is the false-green condition: ``pyproject.toml`` declares at least one
    optional-extra package, yet the workflow's parser produced an empty package
    list.  Treated as a hard failure (rule 37) rather than a silent pass.
    """


def count_declared_extra_packages(pyproject_path: Path) -> int:
    """Count packages declared across all ``[project.optional-dependencies]``.

    Counts the total number of package requirement strings summed over every
    declared extra.  Returns ``0`` when there is no ``[project]`` table, no
    ``optional-dependencies`` sub-table, or every declared extra is an empty
    list.  Counting packages (rather than testing the truthiness of the value
    lists) is what makes "extras ARE declared" robust: an extra present but empty
    must not count as a declared package.
    """
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data.get("project")
    if not isinstance(project, dict):
        return 0
    optional = project.get("optional-dependencies")
    if not isinstance(optional, dict):
        return 0
    total = 0
    for packages in optional.values():
        if isinstance(packages, list):
            total += sum(1 for package in packages if isinstance(package, str))
    return total


def _parsed_package_count(packages_path: Path) -> int:
    """Count non-blank package lines in the workflow's parsed package file.

    A missing file, an empty file, or a file containing only blank/whitespace
    lines all count as zero packages — each is an empty parse.
    """
    try:
        text = packages_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    return sum(1 for line in text.splitlines() if line.strip())


def decide(pyproject_path: Path, packages_path: Path) -> Decision:
    """Decide whether to run, skip, or fail the optional-extras licence gate.

    Raises ``GuardFailureError`` when optional-extras are declared with packages but
    the parsed package list is empty (the false-green condition).
    """
    declared = count_declared_extra_packages(pyproject_path)
    if declared == 0:
        return Decision.SKIP
    if _parsed_package_count(packages_path) == 0:
        raise GuardFailureError(
            f"pyproject.toml declares {declared} package(s) in "
            "[project.optional-dependencies] but the parsed optional-extras list "
            f"({packages_path.name}) is empty. The uv export provenance comment "
            "('# via hermes-voip') likely changed format — fix the parser in the "
            "supply-chain workflow to match. Never silently pass (rule 37)."
        )
    return Decision.RUN


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Decide RUN/SKIP for the optional-extras licence gate, or fail loud "
            "when declared extras exist but the parsed package list is empty."
        )
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        help="Path to pyproject.toml (default: ./pyproject.toml).",
    )
    parser.add_argument(
        "--packages",
        type=Path,
        required=True,
        help="Path to the workflow's parsed optional-extras package list.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the guard, printing the decision token or a diagnostic.

    On success prints ``RUN`` or ``SKIP`` to stdout and returns ``0``.  On the
    false-green condition prints the diagnostic to stderr (leaving stdout empty
    so the workflow never mistakes it for a decision token) and returns ``1``.
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    pyproject_path: Path = args.pyproject
    packages_path: Path = args.packages
    try:
        decision = decide(pyproject_path, packages_path)
    except GuardFailureError as failure:
        sys.stderr.write(f"ERROR: {failure}\n")
        return 1
    sys.stdout.write(f"{decision.value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
