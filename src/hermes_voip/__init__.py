"""hermes-voip: a Hermes plugin for two-way voice over telephony.

The plugin registers as an extension on any RFC-compliant SIP-over-TLS or WebRTC
voice gateway. This package is currently a minimal, generic scaffold: the
SIP/WebRTC client, the media path, and the conversational provider are designed
on the record in ``docs/adr/`` and built in later sessions — nothing about that
architecture is assumed here.
"""

from hermes_voip.sip import sip_address_of_record

__all__ = ["__version__", "sip_address_of_record"]
__version__ = "0.0.0"
