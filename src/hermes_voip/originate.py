"""Sans-IO outbound INVITE builder (UAC originate, ADR-0019).

Provides :func:`build_outbound_invite` (the INVITE wire-text factory for an
out-of-dialog UAC call) and :class:`OutboundCallFailed` (the exception raised
when a call does not establish). Modelled on :mod:`hermes_voip.refer`'s
:func:`~hermes_voip.refer.build_triggered_invite`.
"""

from __future__ import annotations

import base64
import secrets

from hermes_voip.message import build_request, new_branch, new_call_id, new_tag
from hermes_voip.sdp import CryptoAttribute

__all__ = [
    "OutboundCallFailed",
    "build_outbound_invite",
    "build_srtp_crypto_attrs",
]

_DEFAULT_USER_AGENT = "hermes-voip/0"
_MAX_FORWARDS = "70"
_SDP_CONTENT_TYPE: tuple[str, str] = ("Content-Type", "application/sdp")
_AES_CM_128_HMAC_SHA1_80 = "AES_CM_128_HMAC_SHA1_80"
_AES_CM_128_HMAC_SHA1_32 = "AES_CM_128_HMAC_SHA1_32"
# RFC 4568: 16-octet AES-CM-128 key + 14-octet salt = 30 octets for both suites.
_KEY_SALT_BYTES = 30


class OutboundCallFailed(Exception):  # noqa: N818 — "Failed" suffix is intentional: SIP call failure ≠ programming error; ADR-0019 public API
    """The outbound INVITE did not result in an established call.

    Attributes:
        status: The SIP final response status code (e.g. 486, 503).
        reason: The SIP reason phrase.
    """

    def __init__(self, status: int, reason: str) -> None:
        """Initialise with the SIP final response status code and reason phrase."""
        self.status = status
        self.reason = reason
        super().__init__(f"{status} {reason}")


def build_srtp_crypto_attrs() -> tuple[CryptoAttribute, CryptoAttribute]:
    """Generate two fresh SDES crypto attributes (tag 1 = 80-bit, tag 2 = 32-bit).

    Returns:
        A pair of :class:`~hermes_voip.sdp.CryptoAttribute` objects — one for
        ``AES_CM_128_HMAC_SHA1_80`` (tag 1, preferred) and one for
        ``AES_CM_128_HMAC_SHA1_32`` (tag 2, fallback). Both carry freshly
        generated random key||salt material so each call to this function
        yields unique keying data.
    """
    key1 = base64.b64encode(secrets.token_bytes(_KEY_SALT_BYTES)).decode()
    key2 = base64.b64encode(secrets.token_bytes(_KEY_SALT_BYTES)).decode()
    return (
        CryptoAttribute(
            tag=1,
            suite=_AES_CM_128_HMAC_SHA1_80,
            key_params=f"inline:{key1}",
        ),
        CryptoAttribute(
            tag=2,
            suite=_AES_CM_128_HMAC_SHA1_32,
            key_params=f"inline:{key2}",
        ),
    )


def build_outbound_invite(  # noqa: PLR0913 — UAC INVITE needs the full local endpoint; all keyword-only
    *,
    target_uri: str,
    local_aor: str,
    local_contact: str,
    local_sent_by: str,
    transport: str,
    body: str = "",
    from_tag: str | None = None,
    call_id: str | None = None,
    cseq: int = 1,
    auth: tuple[str, str] | None = None,
    user_agent: str = _DEFAULT_USER_AGENT,
) -> tuple[str, str, str]:
    """Build an out-of-dialog INVITE (UAC originate).

    Modelled on :func:`hermes_voip.refer.build_triggered_invite`. Returns a
    three-tuple ``(invite_text, call_id, from_tag)``.

    When ``call_id``/``from_tag`` are provided (re-auth re-send), they are
    reused; otherwise fresh values are generated (initial send). The Via branch
    is always fresh (RFC 3261 §8.1.1.7 — a retransmission re-sends with the
    same branch, but a re-INVITE after a challenge is a new transaction and
    needs a new branch).

    Args:
        target_uri: The ``Request-URI`` and ``To`` AOR (e.g.
            ``sip:1000@pbx.example.test``).
        local_aor: Our ``From`` address-of-record.
        local_contact: Our ``Contact`` header value (the registrar-facing URI).
        local_sent_by: The Via ``sent-by`` (local socket ``host:port``).
        transport: The Via transport token (``TLS`` | ``WSS``).
        body: An optional SDP offer body. When non-empty, a ``Content-Type:
            application/sdp`` header is added automatically.
        from_tag: The ``From``-tag; a fresh random tag if omitted.
        call_id: The ``Call-ID``; a fresh random one if omitted.
        cseq: The ``CSeq`` sequence number (default ``1``; increment to ``2``
            on re-auth).
        auth: An optional ``(Authorization | Proxy-Authorization, value)``
            header pair — carried on the re-auth re-send.
        user_agent: The ``User-Agent`` header value.

    Returns:
        ``(invite_text, call_id, from_tag)`` where ``call_id`` and ``from_tag``
        are the values embedded in the INVITE (either the caller-supplied values
        or freshly generated ones).
    """
    actual_call_id = call_id if call_id is not None else new_call_id()
    actual_from_tag = from_tag if from_tag is not None else new_tag()
    via = f"SIP/2.0/{transport} {local_sent_by};branch={new_branch()};rport"
    headers: list[tuple[str, str]] = [
        ("Via", via),
        ("Max-Forwards", _MAX_FORWARDS),
        ("From", f"<{local_aor}>;tag={actual_from_tag}"),
        ("To", f"<{target_uri}>"),
        ("Call-ID", actual_call_id),
        ("CSeq", f"{cseq} INVITE"),
        ("Contact", local_contact),
        ("User-Agent", user_agent),
    ]
    if auth is not None:
        headers.append(auth)
    if body:
        headers.append(_SDP_CONTENT_TYPE)
    text = build_request("INVITE", target_uri, headers, body)
    return text, actual_call_id, actual_from_tag
