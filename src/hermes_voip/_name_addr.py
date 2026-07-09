"""Quote-aware angle-addr locator for SIP name-addr values (RFC 3261 §25.1).

A SIP ``From``/``To``/``Contact``/``Refer-To``/``Referred-By`` value is either a
bare ``addr-spec`` or a ``name-addr`` — an optional display-name followed by the
addr-spec wrapped in ``<...>``. The display-name may be a ``quoted-string`` that
legitimately contains ``<`` and ``>`` (RFC 3261 §25.1 ``qdtext``/``quoted-pair``),
e.g. ``"Support <Team>" <sip:agent@pbx.example.test>;tag=xyz``.

A naive ``<([^>]*)>`` search matches the FIRST ``<...>`` span — the bracketed
display text — instead of the real addr-spec, corrupting the extracted URI and
desyncing the trailing header parameters (the ``;tag`` is lost). This locator
tracks quoted-string spans (honouring backslash quoted-pair escaping) and only
recognises the ``<...>`` angle-addr that lies OUTSIDE any quoted display-name, so both
:mod:`hermes_voip.dialog` and :mod:`hermes_voip.refer` extract the addr-spec and
its trailing parameters from the correct bracket.
"""

from __future__ import annotations

__all__ = ["find_name_addr"]


def find_name_addr(value: str) -> tuple[str, str] | None:
    """Locate the ``<...>`` angle-addr that lies outside quoted-string spans.

    Scans ``value`` left to right, skipping over any quoted-string display-name
    (honouring backslash quoted-pair escaping, RFC 3261 §25.1), and returns the
    first ``<...>`` span found outside a quote as ``(addr-spec, trailing)`` where
    ``addr-spec`` is the content between the brackets and ``trailing`` is
    everything after the closing ``>`` (the header parameters). Returns ``None``
    when there is no such angle-addr — either a bare addr-spec with no brackets,
    or an unterminated ``<`` — so the caller falls back to bare-addr-spec parsing.
    """
    in_quote = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            # The previous char was an unescaped backslash inside a quoted-string:
            # this char is a literal quoted-pair, never structural (RFC 3261 §25.1).
            escaped = False
        elif in_quote:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_quote = False
        elif char == '"':
            in_quote = True
        elif char == "<":
            close = value.find(">", index + 1)
            if close == -1:
                return None
            return value[index + 1 : close], value[close + 1 :]
    return None
