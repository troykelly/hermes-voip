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
ML or transport heavy dependency — and, crucially, without importing the
hermes-agent runtime itself. ``register`` lives in the light
:mod:`hermes_voip.plugin` module; the real ``VoipAdapter`` (which subclasses
the hermes-agent ``BasePlatformAdapter`` and so imports the runtime) is
imported lazily inside the registration factory, only when the gateway
instantiates the platform.
"""

# ``register`` comes from the light plugin module — it imports neither the
# hermes-agent runtime nor any heavy media/ML dependency, so a bare
# ``import hermes_voip`` stays cheap. The Hermes plugin loader calls
# ``hermes_voip.register(ctx)`` immediately after importing the package.
from hermes_voip.plugin import register
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
