"""Shared fail-closed protocol decimal parsing."""

from __future__ import annotations


def _parse_decimal(token: str, *, max_exclusive: int | None = None) -> int | None:
    """Parse an ASCII decimal token, returning ``None`` on every malformed value.

    SIP/SDP decimal fields are wire ASCII. ``str.isdigit()`` is too broad for this
    job: it accepts Unicode digit characters (for example U+00B2 superscript two)
    that ``int()`` rejects. Conversely, a token can be all ASCII decimal but still
    exceed CPython's integer-string-conversion limit; catch that ``ValueError`` and
    fail closed rather than letting an inbound message unwind a reader task. Leading
    zeros are deliberately accepted and normalised by ``int()`` (SIP CSeq is
    ``1*DIGIT`` and compares by numeric value).
    """
    if not token or not (token.isascii() and token.isdecimal()):
        return None
    try:
        value = int(token)
    except ValueError:
        return None
    if max_exclusive is not None and value >= max_exclusive:
        return None
    return value
