"""hermes-voip: a Hermes plugin for two-way voice over telephony.

The plugin registers as an extension on any RFC-compliant SIP-over-TLS or WebRTC
voice gateway. The package root re-exports the stable, gateway-agnostic public
surface — the sans-IO SIP REGISTER flow and its typed outcomes (ADR-0011) plus
the AOR helper — so callers and the Hermes runtime import them from
``hermes_voip`` rather than via deep module paths. The media plane and
conversational providers are designed on the record in ``docs/adr/``.
"""

from hermes_voip.registration import (
    Challenged,
    Failed,
    Registered,
    RegistrationConfig,
    RegistrationFlow,
    RegistrationOutcome,
    Retry,
)
from hermes_voip.sip import sip_address_of_record

__all__ = [
    "Challenged",
    "Failed",
    "Registered",
    "RegistrationConfig",
    "RegistrationFlow",
    "RegistrationOutcome",
    "Retry",
    "__version__",
    "sip_address_of_record",
]
__version__ = "0.0.0"
