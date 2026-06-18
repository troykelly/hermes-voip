"""Typed shim of the hermes-agent surface the VoIP plugin consumes (plan P0.1).

The plugin is loaded by the Hermes runtime, which provides ``gateway`` /
``hermes_cli`` at run time. That package ships no ``py.typed`` marker, so
importing its classes directly resolves to ``Any`` under this repo's
``mypy --strict`` + ``disallow_any_explicit`` (rules 17/39). This module is the
typed boundary the adapter and call-loop code compile against — a faithful
mirror of *only* the surface we use, verified against the real
``hermes-agent==0.16.0`` by ``tests/test_hermes_contract.py``. It is a typing
boundary over an untyped third-party runtime, not a stub of our own behaviour;
the running plugin uses the real classes.

Surface verified by introspection of hermes-agent 0.16.0:
- ``gateway.platforms.base.BasePlatformAdapter`` — ABC, four abstract async
  methods, ``__init__(config, platform)``; exports ``MessageEvent`` /
  ``SendResult`` / ``MessageType``.
- ``hermes_cli.plugins.PluginContext.register_platform(...)``.
- ``gateway.config`` — ``Platform`` / ``PlatformConfig``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, runtime_checkable

#: The exact abstract methods a platform adapter must implement (verified).
BASE_ADAPTER_ABSTRACT_METHODS: frozenset[str] = frozenset(
    {"connect", "disconnect", "send", "get_chat_info"}
)

#: The ordered ``register_platform`` parameter names (excluding ``self``).
REGISTER_PLATFORM_PARAMS: tuple[str, ...] = (
    "name",
    "label",
    "adapter_factory",
    "check_fn",
    "validate_config",
    "required_env",
    "install_hint",
)


@runtime_checkable
class SendResultProtocol(Protocol):
    """The result of an outbound send (``gateway.platforms.base.SendResult``)."""

    success: bool
    message_id: str | None
    error: str | None


@runtime_checkable
class MessageEventProtocol(Protocol):
    """The inbound turn the adapter hands to Hermes (read side we rely on).

    A VoIP adapter constructs one of these per finalised caller utterance with
    ``message_type`` = the runtime's ``MessageType.VOICE`` and the transcript in
    ``text`` (and optionally the captured audio path in ``media_urls``).
    """

    text: str
    media_urls: Sequence[str]


@runtime_checkable
class BasePlatformAdapterProtocol(Protocol):
    """The platform-adapter contract our ``VoipAdapter`` implements.

    Mirrors ``gateway.platforms.base.BasePlatformAdapter``'s four abstract
    methods. The real class also provides ``handle_message`` and connection
    helpers the subclass calls; those are runtime services, not part of this
    typed contract.
    """

    async def connect(self) -> bool:
        """Bring the platform up; return success."""
        ...

    async def disconnect(self) -> None:
        """Tear the platform down."""
        ...

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> SendResultProtocol:
        """Deliver the agent's reply text to ``chat_id``.

        ``metadata`` is deliberately typed ``Mapping[str, object] | None`` — a
        no-``Any`` narrowing of the runtime's ``Optional[Dict[str, Any]]``; our
        code reads no value out of it, so the narrower type is sound.
        """
        ...

    async def get_chat_info(self, chat_id: str) -> dict[str, object]:
        """Return chat metadata (at least ``name`` and ``type``)."""
        ...


@runtime_checkable
class PluginContextProtocol(Protocol):
    """The ``register(ctx)`` context surface the plugin uses.

    Only ``register_platform`` (the voice-channel seam) is mirrored here; other
    ``register_*`` methods are added to this Protocol when the plugin starts
    using them.
    """

    def register_platform(  # noqa: PLR0913 - mirrors hermes-agent's register_platform arity exactly
        self,
        name: str,
        label: str,
        adapter_factory: Callable[[object], BasePlatformAdapterProtocol],
        check_fn: Callable[[], bool],
        # The real registry treats a FALSEY return as a validation failure, so the
        # plugin's validator (validate_voip_config) returns True on success — the
        # return type must admit bool (not None-only).
        validate_config: Callable[[object], bool | None] | None = None,
        required_env: Sequence[str] | None = None,
        install_hint: str = "",
        **entry_kwargs: object,
    ) -> None:
        """Register a ``kind: platform`` adapter with the gateway."""
        ...
