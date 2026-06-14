"""Parse the ``HERMES_SIP_*`` environment scheme into a typed gateway config.

The plugin registers one or more extensions on a single SIP-over-TLS / WebRTC
gateway (ADR-0011). Connection details live only in the gitignored ``.env`` and
are read by the runtime into a mapping; this module is the **pure** parser that
turns that mapping into a validated :class:`GatewayConfig` plus a tuple of
per-extension :class:`ExtensionConfig`. It reads no process environment itself —
callers pass ``os.environ`` (or ``PlatformConfig.extra``) explicitly — so the
parse is deterministic and unit-testable against fakes.

Two extension schemes are supported, and they MUST NOT be mixed:

* **Single (back-compatible):** ``HERMES_SIP_EXTENSION`` + ``HERMES_SIP_PASSWORD``
  (optional ``HERMES_SIP_USERNAME``). This is index ``0``.
* **Indexed (multiple registrations):** ``HERMES_SIP_EXTENSION_<n>`` +
  ``HERMES_SIP_PASSWORD_<n>`` (optional ``HERMES_SIP_USERNAME_<n>``) for each
  non-negative integer ``<n>``.

Shared gateway settings: ``HERMES_SIP_HOST`` (required), ``HERMES_SIP_PORT``,
``HERMES_SIP_TRANSPORT`` (``tls`` | ``wss``), ``HERMES_SIP_EXPIRES``,
``HERMES_SIP_USER_AGENT``, and ``HERMES_SIP_DEFAULT_EXTENSION`` (the inbound
fallback registration; defaults to the lowest-index extension).

A :class:`GatewayConfig` carries everything env can supply; the transport-derived
``Contact`` and Via ``sent-by`` are not knowable until the socket is up, so
:meth:`GatewayConfig.registration_config` completes a per-extension
:class:`~hermes_voip.registration.RegistrationConfig` from those live inputs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from hermes_voip.registration import RegistrationConfig

__all__ = [
    "ConfigError",
    "ExtensionConfig",
    "GatewayConfig",
    "load_gateway_config",
]

# Scheme tokens accepted for HERMES_SIP_TRANSPORT, mapped to their Via transport
# tokens (RFC 3261 §7.1 / RFC 7118). Only the two sanctioned transports.
_VIA_TRANSPORT: dict[str, str] = {"tls": "TLS", "wss": "WSS"}
_DEFAULT_PORT: dict[str, str] = {"tls": "5061", "wss": "443"}

_DEFAULT_TRANSPORT = "tls"
_DEFAULT_EXPIRES = 300
_DEFAULT_USER_AGENT = "hermes-voip/0"

_MIN_PORT = 1
_MAX_PORT = 65535

_HOST_KEY = "HERMES_SIP_HOST"
_PORT_KEY = "HERMES_SIP_PORT"
_TRANSPORT_KEY = "HERMES_SIP_TRANSPORT"
_EXPIRES_KEY = "HERMES_SIP_EXPIRES"
_USER_AGENT_KEY = "HERMES_SIP_USER_AGENT"
_DEFAULT_EXTENSION_KEY = "HERMES_SIP_DEFAULT_EXTENSION"

_BARE_EXTENSION = "HERMES_SIP_EXTENSION"
_BARE_PASSWORD = "HERMES_SIP_PASSWORD"  # noqa: S105 — env var name, not a secret
_BARE_USERNAME = "HERMES_SIP_USERNAME"

_EXTENSION_PREFIX = "HERMES_SIP_EXTENSION_"
_PASSWORD_PREFIX = "HERMES_SIP_PASSWORD_"  # noqa: S105 — env var name, not a secret
_USERNAME_PREFIX = "HERMES_SIP_USERNAME_"

_INDEX_RE = re.compile(r"[0-9]+")


class ConfigError(ValueError):
    """The ``HERMES_SIP_*`` environment is missing, ambiguous, or malformed."""


@dataclass(frozen=True, slots=True)
class ExtensionConfig:
    """One registrable extension, sourced from ``HERMES_SIP_*``.

    Attributes:
        index: The scheme index (``0`` for the back-compatible single form).
        extension: The extension number / SIP user-part (e.g. ``1000``).
        username: The digest auth username (defaults to ``extension``).
        password: The digest auth password.
    """

    index: int
    extension: str
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """The shared SIP gateway plus its registrable extensions.

    Attributes:
        host: The gateway FQDN (the SIP domain / registrar).
        port: The signalling port (transport default unless overridden).
        transport: The scheme token (``tls`` | ``wss``).
        expires: The requested registration lifetime in seconds.
        user_agent: The ``User-Agent`` header value for every registration.
        extensions: All configured extensions, ordered by ``index`` ascending.
        default_index: The ``index`` of the inbound-fallback registration.
    """

    host: str
    port: int
    transport: str
    expires: int
    user_agent: str
    extensions: tuple[ExtensionConfig, ...]
    default_index: int

    def __post_init__(self) -> None:
        """Enforce the invariants the type promises, not just the parser.

        ``GatewayConfig`` is public, so a caller can construct one directly;
        the dataclass validates itself rather than trusting
        :func:`load_gateway_config` to have done so (the ``default_extension``
        lookup and the demux logic depend on these holding).
        """
        if not self.extensions:
            msg = "GatewayConfig requires at least one extension"
            raise ConfigError(msg)
        indices = [ext.index for ext in self.extensions]
        if len(set(indices)) != len(indices):
            msg = "GatewayConfig extension indices must be unique"
            raise ConfigError(msg)
        numbers = [ext.extension for ext in self.extensions]
        if len(set(numbers)) != len(numbers):
            msg = "GatewayConfig extension numbers must be unique"
            raise ConfigError(msg)
        if self.default_index not in indices:
            msg = f"default_index {self.default_index} is not a configured index"
            raise ConfigError(msg)

    @property
    def via_transport(self) -> str:
        """The Via transport token (``TLS`` | ``WSS``) for this gateway."""
        return _VIA_TRANSPORT[self.transport]

    @property
    def default_extension(self) -> ExtensionConfig:
        """The registration that owns inbound calls with no better match."""
        # __post_init__ guarantees exactly one match for default_index.
        return next(ext for ext in self.extensions if ext.index == self.default_index)

    def registration_config(
        self,
        ext: ExtensionConfig,
        *,
        contact: str,
        local_sent_by: str,
    ) -> RegistrationConfig:
        """Complete a :class:`RegistrationConfig` from transport-derived inputs.

        ``contact`` and ``local_sent_by`` are knowable only once the transport
        socket is up (the local host:port, or an ``.invalid`` host for WebSocket
        per RFC 7118), so they are supplied by the caller; everything else comes
        from this env-sourced config. ``ext`` must be one of this gateway's
        configured extensions.
        """
        if ext not in self.extensions:
            msg = f"extension {ext.extension!r} is not configured on this gateway"
            raise ConfigError(msg)
        return RegistrationConfig(
            aor=f"sip:{ext.extension}@{self.host}",
            username=ext.username,
            password=ext.password,
            contact=contact,
            local_sent_by=local_sent_by,
            transport=self.via_transport,
            expires=self.expires,
            user_agent=self.user_agent,
        )


def load_gateway_config(env: Mapping[str, str]) -> GatewayConfig:
    """Parse the ``HERMES_SIP_*`` mapping into a validated :class:`GatewayConfig`.

    Raises:
        ConfigError: if a required value is missing, a value is malformed, the
            single and indexed schemes are mixed, or extension numbers collide.
    """
    host = _require(env, _HOST_KEY)
    transport = _parse_transport(env)
    port = _parse_port(env, transport)
    expires = _parse_expires(env)
    user_agent = _value(env, _USER_AGENT_KEY) or _DEFAULT_USER_AGENT

    extensions = _parse_extensions(env)
    default_index = _resolve_default_index(env, extensions)

    return GatewayConfig(
        host=host,
        port=port,
        transport=transport,
        expires=expires,
        user_agent=user_agent,
        extensions=extensions,
        default_index=default_index,
    )


# --- shared field parsing ---------------------------------------------------


def _value(env: Mapping[str, str], key: str) -> str:
    """Return the trimmed value for ``key``, or ``""`` if unset/blank."""
    raw = env.get(key)
    return raw.strip() if raw is not None else ""


def _require(env: Mapping[str, str], key: str) -> str:
    value = _value(env, key)
    if not value:
        msg = f"{key} is required"
        raise ConfigError(msg)
    return value


def _parse_transport(env: Mapping[str, str]) -> str:
    token = _value(env, _TRANSPORT_KEY).lower() or _DEFAULT_TRANSPORT
    if token not in _VIA_TRANSPORT:
        allowed = ", ".join(sorted(_VIA_TRANSPORT))
        msg = f"{_TRANSPORT_KEY} must be one of {{{allowed}}}, got {token!r}"
        raise ConfigError(msg)
    return token


def _parse_port(env: Mapping[str, str], transport: str) -> int:
    raw = _value(env, _PORT_KEY) or _DEFAULT_PORT[transport]
    port = _parse_int(raw, _PORT_KEY)
    if not _MIN_PORT <= port <= _MAX_PORT:
        msg = f"{_PORT_KEY} must be in [{_MIN_PORT}, {_MAX_PORT}], got {port}"
        raise ConfigError(msg)
    return port


def _parse_expires(env: Mapping[str, str]) -> int:
    raw = _value(env, _EXPIRES_KEY)
    if not raw:
        return _DEFAULT_EXPIRES
    expires = _parse_int(raw, _EXPIRES_KEY)
    if expires <= 0:
        msg = f"{_EXPIRES_KEY} must be positive, got {expires}"
        raise ConfigError(msg)
    return expires


def _parse_int(raw: str, key: str) -> int:
    if not _INDEX_RE.fullmatch(raw):
        msg = f"{key} must be a non-negative integer, got {raw!r}"
        raise ConfigError(msg)
    return int(raw)


# --- extension parsing ------------------------------------------------------


def _parse_extensions(env: Mapping[str, str]) -> tuple[ExtensionConfig, ...]:
    # Any bare credential key — not just the extension — signals the single
    # scheme, so a stray HERMES_SIP_PASSWORD/USERNAME beside the indexed scheme
    # is caught as a mix (a likely typo) rather than silently ignored.
    has_bare = any(
        key in env for key in (_BARE_EXTENSION, _BARE_PASSWORD, _BARE_USERNAME)
    )
    indexed_indices = _indexed_indices(env)

    if has_bare and indexed_indices:
        msg = (
            f"{_BARE_EXTENSION}/{_BARE_PASSWORD} (single) and "
            f"{_EXTENSION_PREFIX}<n> (indexed) schemes must not be combined; "
            "use one"
        )
        raise ConfigError(msg)

    extensions: tuple[ExtensionConfig, ...]
    if has_bare:
        extensions = (_parse_bare_extension(env),)
    else:
        extensions = _parse_indexed_extensions(env, indexed_indices)

    if not extensions:
        msg = (
            f"no extension configured: set {_BARE_EXTENSION} (+{_BARE_PASSWORD}) "
            f"or {_EXTENSION_PREFIX}<n> (+{_PASSWORD_PREFIX}<n>)"
        )
        raise ConfigError(msg)

    _reject_duplicate_numbers(extensions)
    return extensions


def _parse_bare_extension(env: Mapping[str, str]) -> ExtensionConfig:
    extension = _require(env, _BARE_EXTENSION)
    password = _require(env, _BARE_PASSWORD)
    username = _value(env, _BARE_USERNAME) or extension
    return ExtensionConfig(
        index=0, extension=extension, username=username, password=password
    )


def _indexed_indices(env: Mapping[str, str]) -> tuple[int, ...]:
    """Collect and validate the integer indices from ``HERMES_SIP_EXTENSION_<n>``.

    A non-integer suffix is malformed; an indexed ``PASSWORD``/``USERNAME``
    without a matching ``EXTENSION`` is an orphan. Both raise.
    """
    ext_indices = _suffix_indices(env, _EXTENSION_PREFIX)
    pwd_indices = _suffix_indices(env, _PASSWORD_PREFIX)
    user_indices = _suffix_indices(env, _USERNAME_PREFIX)

    orphans = (pwd_indices | user_indices) - ext_indices
    if orphans:
        joined = ", ".join(str(i) for i in sorted(orphans))
        msg = (
            f"indexed {_PASSWORD_PREFIX}/{_USERNAME_PREFIX} without a matching "
            f"{_EXTENSION_PREFIX} for index(es): {joined}"
        )
        raise ConfigError(msg)
    return tuple(sorted(ext_indices))


def _suffix_indices(env: Mapping[str, str], prefix: str) -> set[int]:
    indices: set[int] = set()
    for key in env:
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        if not _INDEX_RE.fullmatch(suffix):
            msg = f"{key}: index suffix must be a non-negative integer"
            raise ConfigError(msg)
        index = int(suffix)
        if index in indices:
            msg = f"duplicate index {index} from {prefix}<n> keys"
            raise ConfigError(msg)
        indices.add(index)
    return indices


def _parse_indexed_extensions(
    env: Mapping[str, str], indices: tuple[int, ...]
) -> tuple[ExtensionConfig, ...]:
    configs: list[ExtensionConfig] = []
    for index in indices:
        extension = _require(env, f"{_EXTENSION_PREFIX}{index}")
        password = _require(env, f"{_PASSWORD_PREFIX}{index}")
        username = _value(env, f"{_USERNAME_PREFIX}{index}") or extension
        configs.append(
            ExtensionConfig(
                index=index,
                extension=extension,
                username=username,
                password=password,
            )
        )
    return tuple(configs)


def _reject_duplicate_numbers(extensions: tuple[ExtensionConfig, ...]) -> None:
    seen: set[str] = set()
    for ext in extensions:
        if ext.extension in seen:
            msg = f"duplicate extension number {ext.extension!r}"
            raise ConfigError(msg)
        seen.add(ext.extension)


def _resolve_default_index(
    env: Mapping[str, str], extensions: tuple[ExtensionConfig, ...]
) -> int:
    chosen = _value(env, _DEFAULT_EXTENSION_KEY)
    if not chosen:
        # Lowest index wins; extensions are sorted ascending.
        return extensions[0].index
    for ext in extensions:
        if ext.extension == chosen:
            return ext.index
    available = ", ".join(ext.extension for ext in extensions)
    msg = (
        f"{_DEFAULT_EXTENSION_KEY}={chosen!r} is not a configured extension "
        f"(have: {available})"
    )
    raise ConfigError(msg)
