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

__all__ = [
    "find_name_addr",
    "name_addr_parts",
    "params_after_addr",
    "split_params",
    "tag_param",
]


def name_addr_parts(value: str) -> tuple[str, str, str] | None:
    """Split a name-addr at the ``<...>`` angle-addr outside quoted-string spans.

    Scans ``value`` left to right, skipping over any quoted-string display-name
    (honouring backslash quoted-pair escaping, RFC 3261 §25.1), and returns the
    first ``<...>`` span found outside a quote as
    ``(display-name, addr-spec, trailing)`` where ``display-name`` is everything
    before the opening ``<`` (verbatim, not stripped), ``addr-spec`` is the
    content between the brackets, and ``trailing`` is everything after the
    closing ``>`` (the header parameters). Returns ``None`` when there is no such
    angle-addr — either a bare addr-spec with no brackets, or an unterminated
    ``<`` — so the caller falls back to bare-addr-spec parsing.
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
            return value[:index], value[index + 1 : close], value[close + 1 :]
    return None


def find_name_addr(value: str) -> tuple[str, str] | None:
    """Return ``(addr-spec, trailing)`` of the angle-addr outside quoted spans.

    A thin ``(addr-spec, trailing)`` view of :func:`name_addr_parts` for callers
    that only need the URI and its header parameters (not the display-name).
    Returns ``None`` for a bare addr-spec or an unterminated ``<``.
    """
    parts = name_addr_parts(value)
    if parts is None:
        return None
    return parts[1], parts[2]


def split_params(trailing: str) -> list[str]:
    """Split a header-parameter string on ``;`` outside double-quoted spans.

    Splits on ``;`` that fall outside any ``"..."`` quoted span, honouring
    backslash quoted-pair escaping (RFC 3261 §25.1), so a quoted generic-param
    value that itself contains ``;`` (e.g. ``g=";tag=fake"``) stays one element.
    The quotes and their contents are preserved verbatim in the returned parts.
    """
    parts: list[str] = []
    buffer: list[str] = []
    in_quote = False
    escaped = False
    for char in trailing:
        if escaped:
            buffer.append(char)
            escaped = False
        elif in_quote and char == "\\":
            buffer.append(char)
            escaped = True
        elif char == '"':
            in_quote = not in_quote
            buffer.append(char)
        elif char == ";" and not in_quote:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return parts


def tag_param(trailing: str) -> str | None:
    """Return the value of the ``tag`` header parameter (quote-aware), or ``None``.

    Splits ``trailing`` with :func:`split_params` so a forged ``;tag=`` hidden
    inside a quoted generic-param value is never mistaken for the real dialog
    tag; the first top-level ``tag=`` wins.
    """
    for part in split_params(trailing):
        key, sep, raw = part.partition("=")
        if sep and key.strip().lower() == "tag":
            # A present-but-empty tag (``;tag=`` or ``;tag="  "``) is malformed
            # (RFC 3261 tag = token, 1+ chars); treat it as absent so it neither
            # counts as an in-dialog tag nor suppresses our own tag minting.
            return raw.strip() or None
    return None


def params_after_addr(value: str) -> str:
    """Header parameters after a bare/unterminated addr-spec's first top-level ``;``.

    The fallback for callers that have already tried :func:`find_name_addr` and got
    ``None`` (a bare ``addr-spec`` or a MALFORMED, unterminated ``<``). Returns
    everything after the first ``;`` located OUTSIDE any ``"..."`` quoted-string span
    or ``<...>`` angle span, so a ``;`` inside a quoted display-name (e.g. a forged
    ``"x;tag=fake"`` before an unterminated ``<``) is never mistaken for the parameter
    boundary. Returns ``""`` when there is no top-level ``;``. Lenient: an unterminated
    quote/angle just runs to the end (never raises).
    """
    in_quote = False
    angle_depth = 0
    for index, char in enumerate(value):
        if char == '"':
            in_quote = not in_quote
        elif char == "<" and not in_quote:
            angle_depth += 1
        elif char == ">" and not in_quote and angle_depth > 0:
            angle_depth -= 1
        elif char == ";" and not in_quote and angle_depth == 0:
            return value[index + 1 :]
    return ""
