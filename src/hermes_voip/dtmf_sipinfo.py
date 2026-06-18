"""SIP INFO DTMF body codec (ADR-0010/0034).

SIP INFO carries a DTMF digit in an in-dialog ``INFO`` request body in one of two
gateway-dependent formats:

* ``application/dtmf-relay`` — a small key/value body, e.g.::

      Signal=5
      Duration=160

  (``Signal=`` is the digit; ``Duration=`` is advisory). Some gateways encode ``*``
  and ``#`` as the numeric event codes ``10`` and ``11``.
* ``application/dtmf`` — the bare digit alone in the body.

This module parses BOTH to a keypad digit (:func:`parse_dtmf_info`) and builds the
relay body for sending (:func:`build_dtmf_relay_body`). It owns no transport: the
:class:`~hermes_voip.call.CallSession` wraps the body in an in-dialog ``INFO`` (send)
and answers an inbound ``INFO`` ``200 OK`` then surfaces the digit (receive).
"""

from __future__ import annotations

from hermes_voip.dtmf import digit_to_event, event_to_digit

__all__ = [
    "DTMF_RELAY_CONTENT_TYPE",
    "DTMF_SIMPLE_CONTENT_TYPE",
    "build_dtmf_relay_body",
    "parse_dtmf_info",
]

#: The ``application/dtmf-relay`` content type (Signal=/Duration= body).
DTMF_RELAY_CONTENT_TYPE = "application/dtmf-relay"
#: The bare ``application/dtmf`` content type (the digit alone).
DTMF_SIMPLE_CONTENT_TYPE = "application/dtmf"

_KEYPAD = "0123456789*#ABCD"


def _content_subtype(content_type: str) -> str:
    """The lowercased media type of ``content_type``, parameters and casing stripped.

    ``"Application/DTMF-Relay; charset=utf-8"`` -> ``"application/dtmf-relay"``.
    """
    return content_type.split(";", 1)[0].strip().lower()


def _normalise_signal(raw: str) -> str | None:
    """Map a ``Signal=`` / bare-body token to a keypad digit, or ``None``.

    Accepts a single keypad character (case-insensitive) or a numeric RFC 4733 event
    code (``0``-``15``, so ``10``/``11`` for ``*``/``#``). Anything else is not a DTMF
    digit.
    """
    token = raw.strip()
    if len(token) == 1 and token.upper() in _KEYPAD:
        return token.upper()
    if token.isdigit():
        try:
            return event_to_digit(int(token))
        except ValueError:
            return None
    return None


def parse_dtmf_info(content_type: str, body: str) -> str | None:
    """Parse a SIP ``INFO`` body to a DTMF keypad digit, or ``None``.

    Returns ``None`` when the request is not a DTMF INFO (a different content type,
    or a malformed/absent signal) — the caller still acknowledges the INFO but
    surfaces no digit.

    Args:
        content_type: The request's ``Content-Type`` header value (may carry params).
        body: The request body.

    Returns:
        The single keypad digit, or ``None``.
    """
    subtype = _content_subtype(content_type)
    if subtype == DTMF_SIMPLE_CONTENT_TYPE:
        return _normalise_signal(body)
    if subtype != DTMF_RELAY_CONTENT_TYPE:
        return None
    for line in body.replace("\r\n", "\n").split("\n"):
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "signal":
            return _normalise_signal(value)
    return None  # a dtmf-relay body with no Signal= line is not a digit


def build_dtmf_relay_body(digit: str, *, duration_ms: int) -> str:
    """Build an ``application/dtmf-relay`` body for one ``digit``.

    Args:
        digit: The keypad character to send (``0-9``, ``*``, ``#``, ``A``-``D``;
            case-insensitive).
        duration_ms: The advisory tone duration in milliseconds (positive).

    Returns:
        The ``Signal=``/``Duration=`` body (CRLF line endings).

    Raises:
        ValueError: If ``digit`` is not a DTMF symbol, or ``duration_ms`` is not
            positive.
    """
    digit_to_event(digit)  # validate; raises ValueError on a non-DTMF char
    if duration_ms <= 0:
        msg = f"duration_ms must be positive, got {duration_ms}"
        raise ValueError(msg)
    return f"Signal={digit.upper()}\r\nDuration={duration_ms}\r\n"
