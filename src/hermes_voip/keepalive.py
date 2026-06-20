"""Sans-IO UA-liveness responses: out-of-dialog OPTIONS / unsolicited NOTIFY.

A gateway that has a registered contact periodically *qualifies* it: it sends an
out-of-dialog ``OPTIONS`` ping and expects a ``200 OK``. If the UA never answers,
the registrar marks the endpoint UNREACHABLE and routes inbound calls to
voicemail **without ever sending an INVITE** (observed live against a real
RFC-compliant SIP/UCM gateway — zero INVITEs reached the plugin). RFC 3261 §11 makes
answering out-of-dialog ``OPTIONS`` mandatory, so keeping the registration
*qualified* is a UA/transport concern, not the adapter's.

This module is the sans-IO half: it builds those ``200 OK`` responses as plain
wire text via :func:`~hermes_voip.message.build_response` — it owns no socket and
no dialog state. The transport
(:class:`~hermes_voip.transport.connection.SipOverTlsTransport`) calls these for
an out-of-dialog request that is not a new INVITE and writes the result.

* :func:`build_options_ok` — a ``200 OK`` to an out-of-dialog ``OPTIONS`` carrying
  an :data:`ALLOW_METHODS` ``Allow`` header so the gateway knows the UA can be
  rung and transferred.
* :func:`build_keepalive_ok` — a bare ``200 OK`` that acknowledges an unsolicited
  ``NOTIFY`` (e.g. ``Event: message-summary`` MWI) without processing its body.

Both echo the request's ``Via``/``From``/``To``/``Call-ID``/``CSeq`` and add a
``To``-tag (the response is a new transaction's far end), with ``Content-Length:
0`` and no body.
"""

from __future__ import annotations

from hermes_voip.message import SipRequest, build_response

__all__ = [
    "ALLOW_METHODS",
    "build_keepalive_ok",
    "build_options_ok",
]

# The methods this UA supports, advertised in the OPTIONS 200 OK ``Allow`` header.
# INVITE/ACK/CANCEL/BYE/OPTIONS are the in-call core; REFER and NOTIFY are handled
# by the ADR-0011 call-control layer (transfer + its progress subscription), so we
# advertise them too. Tuple = ordered + immutable (this is fixed UA policy).
ALLOW_METHODS: tuple[str, ...] = (
    "INVITE",
    "ACK",
    "CANCEL",
    "BYE",
    "OPTIONS",
    "REFER",
    "NOTIFY",
)

_ALLOW_HEADER: tuple[str, str] = ("Allow", ", ".join(ALLOW_METHODS))
# RFC 4028 §8: a UA that supports session timers advertises ``Supported: timer`` so a
# querying peer/proxy knows it can engage them (ADR-0071). Advertised in the OPTIONS
# capability response alongside the Allow methods.
_SUPPORTED_HEADER: tuple[str, str] = ("Supported", "timer")


def build_options_ok(request: SipRequest, *, to_tag: str) -> str:
    """Build a ``200 OK`` to an out-of-dialog ``OPTIONS`` (RFC 3261 §11).

    The response echoes the ping's ``Via``/``From``/``To``/``Call-ID``/``CSeq``,
    appends ``to_tag`` to ``To`` (no body, ``Content-Length: 0``), advertises
    :data:`ALLOW_METHODS` in an ``Allow`` header so the registrar keeps the
    contact *qualified* and rings it for inbound calls, and advertises
    ``Supported: timer`` (RFC 4028 §8) so a peer/proxy knows we engage session
    timers (ADR-0071).

    Args:
        request: The inbound ``OPTIONS`` request being answered.
        to_tag: The dialog tag to add to ``To`` (the UA's local tag).

    Returns:
        The full ``200 OK`` response as wire text.

    Raises:
        ValueError: if ``request`` is missing a mandatory header to echo
            (propagated from :func:`~hermes_voip.message.build_response`).
    """
    return build_response(
        request,
        200,
        "OK",
        to_tag=to_tag,
        extra_headers=(_ALLOW_HEADER, _SUPPORTED_HEADER),
    )


def build_keepalive_ok(request: SipRequest, *, to_tag: str) -> str:
    """Build a bare ``200 OK`` acknowledging an unsolicited request (e.g. NOTIFY).

    Used for an unsolicited ``NOTIFY`` (such as ``Event: message-summary`` MWI)
    the UA does not process: acknowledging it keeps the gateway happy. Echoes
    ``Via``/``From``/``To``/``Call-ID``/``CSeq``, adds ``to_tag`` to ``To``, and
    carries no body (``Content-Length: 0``).

    Args:
        request: The inbound request being acknowledged.
        to_tag: The dialog tag to add to ``To`` (the UA's local tag).

    Returns:
        The full ``200 OK`` response as wire text.

    Raises:
        ValueError: if ``request`` is missing a mandatory header to echo
            (propagated from :func:`~hermes_voip.message.build_response`).
    """
    return build_response(request, 200, "OK", to_tag=to_tag)
