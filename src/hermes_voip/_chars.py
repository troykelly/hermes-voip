"""Private character-class helpers shared across SIP text modules."""

from __future__ import annotations

_C0_END = 0x20
_DEL = 0x7F


def contains_control(value: str) -> bool:
    """Return whether ``value`` contains an ASCII C0 control or DEL."""
    return any(ord(char) < _C0_END or ord(char) == _DEL for char in value)
