"""hermes-voip: a Hermes plugin for two-way voice over telephony.

The plugin registers as an extension on any RFC-compliant SIP-over-TLS or WebRTC
voice gateway. The package root re-exports the stable, gateway-agnostic public
surface — the sans-IO SIP REGISTER flow and its typed outcomes (ADR-0011) plus
the AOR helper — so callers and the Hermes runtime import them from
``hermes_voip`` rather than via deep module paths. The media plane and
conversational providers are designed on the record in ``docs/adr/``.

The Hermes plugin entry point :func:`register` is also re-exported here so the
``[project.entry-points."hermes_agent.plugins"]`` entry point
(``hermes-voip = "hermes_voip"``) resolves immediately without importing any
ML or transport heavy dependency (those are lazy-imported inside the adapter).
"""

# Lazy re-export: importing hermes_voip.adapter does not pull in the Hermes
# runtime (which is optional) — the heavy imports happen inside the factory
# and connect() — but ``register`` must be present at module level so the
# Hermes plugin loader can call it immediately after import.
from hermes_voip.adapter import register
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
    "register",
    "sip_address_of_record",
]
__version__ = "0.0.0"
