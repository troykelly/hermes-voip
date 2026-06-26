"""Top-level-comma splitter for comma-combined SIP header values (RFC 3261 §7.3.1).

Any header field that may appear multiple times can be combined into a single
field value whose entries are separated by commas (RFC 3261 §7.3.1) — e.g. a
multi-binding ``Contact`` on a REGISTER 200 OK, or a multi-proxy ``Record-Route``
on an INVITE/2xx. A comma inside a name-addr's ``<...>`` or a quoted display-name
is NOT a separator, so the split tracks angle-bracket and double-quote depth and
only breaks on a top-level comma. Empty entries are dropped and each is stripped.

This is the one splitter shared by :mod:`hermes_voip.registration` (Contact
bindings) and :mod:`hermes_voip.dialog` (Record-Route route-set entries), so both
treat a comma-combined header identically.
"""

from __future__ import annotations

__all__ = ["split_header_list"]


def split_header_list(header_value: str) -> list[str]:
    """Split a comma-combined SIP header value into its top-level entries.

    Splits ``header_value`` on commas that fall outside a name-addr's ``<...>``
    or a quoted display-name (the only commas that are field separators per
    RFC 3261 §7.3.1). Each returned entry is stripped; empty entries are dropped.
    """
    entries: list[str] = []
    current: list[str] = []
    in_angle = False
    in_quote = False
    escaped = False
    for char in header_value:
        if escaped:
            # The previous char was an unescaped backslash inside a quoted-string:
            # this char is a literal quoted-pair (RFC 3261 §25.1), never structural.
            current.append(char)
            escaped = False
            continue
        if in_quote and char == "\\":
            escaped = True
        elif char == '"' and not in_angle:
            in_quote = not in_quote
        elif char == "<" and not in_quote:
            in_angle = True
        elif char == ">" and not in_quote:
            in_angle = False
        elif char == "," and not in_angle and not in_quote:
            entries.append("".join(current))
            current = []
            continue
        current.append(char)
    entries.append("".join(current))
    return [entry.strip() for entry in entries if entry.strip()]
