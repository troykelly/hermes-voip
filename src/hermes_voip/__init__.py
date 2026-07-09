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
#
# config, providers.audio, and providers.build are dataclass/Protocol/function
# definitions with no ML or heavy IO imports, so they remain lightweight here.
from hermes_voip.call_context import (
    DiversionHop,
    HistoryInfoEntry,
    InboundCallContext,
    extract_call_context,
)
from hermes_voip.config import ConfigError, GatewayConfig, MediaConfig
from hermes_voip.plugin import register
from hermes_voip.providers.asr import StreamingASR
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers, build_providers
from hermes_voip.providers.guard import InjectionGuard
from hermes_voip.providers.transport import MediaTransport
from hermes_voip.providers.tts import StreamingTTS
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
    "ConfigError",
    "DiversionHop",
    "Failed",
    "GatewayConfig",
    "HistoryInfoEntry",
    "InboundCallContext",
    "InjectionGuard",
    "MediaConfig",
    "MediaTransport",
    "PcmFrame",
    "Providers",
    "Registered",
    "RegistrationConfig",
    "RegistrationFlow",
    "RegistrationOutcome",
    "Retry",
    "StreamingASR",
    "StreamingTTS",
    "__version__",
    "build_providers",
    "extract_call_context",
    "register",
    "sip_address_of_record",
]


def _resolve_version() -> str:
    """Single-source the package version from installed distribution metadata.

    The canonical version lives in ``pyproject.toml [project].version``; the build
    backend (hatchling) writes it into the distribution metadata, from which
    :func:`importlib.metadata.version` reads it for both wheel and editable
    installs. Deriving ``__version__`` here means a release is a SINGLE edit in
    ``pyproject.toml`` rather than three hand-maintained copies that can drift
    (the plugin.yaml manifest is pinned equal by the test suite).

    Fallback: when the package is imported from a source tree that was never
    installed — so no distribution metadata exists — there is no canonical version
    to report; ``"0+unknown"`` (PEP 440 local-version form) signals exactly that
    rather than fabricating a release number. This path is not hit in any installed
    deployment (wheel, editable, or directory-install), where metadata is present.
    """
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version("hermes-voip")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _resolve_version()
