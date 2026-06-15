"""The live SIP-over-TLS signalling transport (ADR-0005).

This package is the IO layer beneath the merged sans-IO SIP control plane: an
``asyncio`` TLS client that frames the byte stream into whole SIP messages,
parses them, runs the INVITE transaction state machine (RFC 3261 §17), and
demuxes inbound traffic to the :class:`~hermes_voip.manager.RegistrationManager`
and the owning ``CallSession``s. It implements the two seams the control plane
defines — :class:`~hermes_voip.manager.SipTransport` and
:class:`~hermes_voip.call.CallSignaling` — and owns the transaction concern those
seams delegate here: request transmission and the non-2xx ACK for a failed
INVITE.
"""

from __future__ import annotations

from hermes_voip.transport.connection import SipOverTlsTransport
from hermes_voip.transport.framing import FramingError, SipMessageFramer
from hermes_voip.transport.transaction import (
    InviteClientTransaction,
    InviteServerTransaction,
    TransactionState,
)

__all__ = [
    "FramingError",
    "InviteClientTransaction",
    "InviteServerTransaction",
    "SipMessageFramer",
    "SipOverTlsTransport",
    "TransactionState",
]
