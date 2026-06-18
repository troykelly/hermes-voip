"""Every ADR file's 4-digit numeric prefix must be unique.

A duplicate prefix (two `NNNN-*.md` files sharing the same `NNNN`) makes
"ADR-NNNN" references ambiguous and is a documentation defect. This test guards
the `docs/adr/` directory against re-introducing a collision.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

_ADR_DIR = Path(__file__).resolve().parent.parent / "docs" / "adr"
# A numbered ADR: four leading digits, a hyphen, a kebab title, `.md`.
_NUMBERED = re.compile(r"^(\d{4})-.+\.md$")


def _numbered_prefixes() -> list[str]:
    """Return the 4-digit prefix of every numbered ADR file (sorted)."""
    prefixes: list[str] = []
    for path in sorted(_ADR_DIR.glob("*.md")):
        match = _NUMBERED.match(path.name)
        if match is not None:
            prefixes.append(match.group(1))
    return prefixes


def test_adr_dir_exists() -> None:
    assert _ADR_DIR.is_dir(), f"ADR directory missing: {_ADR_DIR}"


def test_adr_numeric_prefixes_are_unique() -> None:
    prefixes = _numbered_prefixes()
    duplicates = sorted(num for num, count in Counter(prefixes).items() if count > 1)
    assert not duplicates, (
        f"duplicate ADR numeric prefixes (each NNNN must be unique): {duplicates}"
    )


def test_at_least_one_numbered_adr() -> None:
    # Guards the regex/glob against silently matching nothing (a vacuous pass).
    assert _numbered_prefixes(), "no numbered ADR files found under docs/adr/"
