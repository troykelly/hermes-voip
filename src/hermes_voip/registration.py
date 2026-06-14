"""Sans-IO SIP REGISTER flow (RFC 3261 §10).

A :class:`RegistrationFlow` produces request wire text and consumes parsed
responses, returning a typed outcome; it owns no socket and no timer. The
transport (ADR-0005) drives it: send what ``start``/``deregister`` and a
``Challenged`` outcome return, feed every response to ``handle``, and use the
``Registered.expires`` to schedule the refresh. Keeping IO out makes the digest
and state logic deterministic and unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hermes_voip.digest import DigestChallenge, DigestCredentials, build_authorization
from hermes_voip.message import (
    SipResponse,
    build_request,
    new_branch,
    new_call_id,
    new_tag,
)

_UNAUTHORIZED = 401
_PROXY_AUTH_REQUIRED = 407
_OK = 200
_EXPIRES_PARAM = re.compile(r";expires=(\d+)")


@dataclass(frozen=True, slots=True)
class RegistrationConfig:
    """Inputs for a REGISTER flow (credentials sourced from ``HERMES_SIP_*``).

    Attributes:
        aor: The address-of-record (``sip:user@domain``).
        username: The digest auth username.
        password: The digest auth password.
        contact: The Contact header value (``<sip:user@host:port;transport=...>``).
        expires: The requested registration lifetime in seconds.
        user_agent: The User-Agent header value.
    """

    aor: str
    username: str
    password: str
    contact: str
    expires: int = 300
    user_agent: str = "hermes-voip/0"


@dataclass(frozen=True, slots=True)
class Challenged:
    """The registrar demanded auth; ``request`` is the authenticated REGISTER."""

    request: str


@dataclass(frozen=True, slots=True)
class Registered:
    """Registration succeeded; refresh before ``expires`` seconds elapse."""

    expires: int


@dataclass(frozen=True, slots=True)
class Failed:
    """Registration failed with the given final status."""

    status: int
    reason: str


type RegistrationOutcome = Challenged | Registered | Failed


def _registrar_uri(aor: str) -> str:
    """Derive the registrar request-URI (``sip:domain``) from an AOR."""
    scheme, _, rest = aor.partition(":")
    host = rest.split(";", 1)[0].split("@", 1)[-1]
    return f"{scheme}:{host}"


def _contact_via(contact: str) -> tuple[str, str]:
    """Return the Via ``sent-by`` and transport token derived from the Contact."""
    inner = contact.strip().removeprefix("<").removesuffix(">")
    addr_part = inner.split("@", 1)[-1]
    sent_by = addr_part.split(";", 1)[0]
    transport = "UDP"
    for param in addr_part.split(";")[1:]:
        if param.lower().startswith("transport="):
            transport = param.split("=", 1)[1].upper()
    return sent_by, transport


class RegistrationFlow:
    """Drives one extension's REGISTER lifecycle as a sans-IO state machine."""

    def __init__(self, config: RegistrationConfig) -> None:
        """Initialise the flow with a stable Call-ID and From-tag."""
        self._cfg = config
        self._registrar = _registrar_uri(config.aor)
        self._sent_by, self._via_transport = _contact_via(config.contact)
        self._call_id = new_call_id()
        self._from_tag = new_tag()
        self._cseq = 0
        self._challenged = False
        self._registered = False

    def start(self) -> str:
        """Return the initial (unauthenticated) REGISTER request."""
        return self._build(expires=self._cfg.expires, auth=None)

    def handle(self, response: SipResponse) -> RegistrationOutcome:
        """Consume a response and return the next action / terminal outcome."""
        status = response.status_code
        if status == _OK:
            self._registered = True
            return Registered(expires=self._granted_expires(response))
        if status in (_UNAUTHORIZED, _PROXY_AUTH_REQUIRED):
            if self._challenged:
                return Failed(status, response.reason)  # auth rejected; do not loop
            self._challenged = True
            return Challenged(request=self._authenticated_register(response, status))
        return Failed(status, response.reason)

    def deregister(self) -> str:
        """Return a REGISTER with ``Expires: 0`` to drop the binding.

        Raises:
            RuntimeError: If called before a successful registration.
        """
        if not self._registered:
            msg = "cannot de-register: not registered"
            raise RuntimeError(msg)
        self._challenged = False  # the de-register may be challenged afresh
        return self._build(expires=0, auth=None)

    def _authenticated_register(self, response: SipResponse, status: int) -> str:
        header = "WWW-Authenticate" if status == _UNAUTHORIZED else "Proxy-Authenticate"
        challenge = DigestChallenge.parse(response.header(header) or "")
        auth = build_authorization(
            challenge,
            DigestCredentials(self._cfg.username, self._cfg.password),
            method="REGISTER",
            uri=self._registrar,
        )
        return self._build(expires=self._cfg.expires, auth=auth)

    def _granted_expires(self, response: SipResponse) -> int:
        contact = response.header("Contact")
        if contact is not None:
            match = _EXPIRES_PARAM.search(contact)
            if match is not None:
                return int(match.group(1))
        expires = response.header("Expires")
        if expires is not None and expires.strip().isdigit():
            return int(expires.strip())
        return self._cfg.expires

    def _build(self, *, expires: int, auth: str | None) -> str:
        self._cseq += 1
        sent_by = self._sent_by
        via = f"SIP/2.0/{self._via_transport} {sent_by};branch={new_branch()};rport"
        headers = [
            ("Via", via),
            ("Max-Forwards", "70"),
            ("From", f"<{self._cfg.aor}>;tag={self._from_tag}"),
            ("To", f"<{self._cfg.aor}>"),
            ("Call-ID", self._call_id),
            ("CSeq", f"{self._cseq} REGISTER"),
            ("Contact", self._cfg.contact),
            ("Expires", str(expires)),
            ("User-Agent", self._cfg.user_agent),
        ]
        if auth is not None:
            headers.append(("Authorization", auth))
        return build_request("REGISTER", self._registrar, headers)
