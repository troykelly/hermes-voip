"""Sans-IO SIP REGISTER flow (RFC 3261 §10).

A :class:`RegistrationFlow` produces request wire text and consumes parsed
responses, returning a typed outcome; it owns no socket and no timer. The
transport (ADR-0005) drives it: send what ``start`` / ``deregister`` and a
``Challenged`` outcome return, feed every response to ``handle``, and use
``Registered.expires`` to schedule the refresh. The Via transport and sent-by
are explicit transport inputs (never guessed). Each REGISTER is one transaction
with its own challenge state, so a refresh after a successful registration can
re-authenticate. Keeping IO out makes the digest and state logic deterministic.
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
_EXPIRES_PARAM = re.compile(r";\s*expires\s*=\s*(\d+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RegistrationConfig:
    """Inputs for a REGISTER flow (credentials sourced from ``HERMES_SIP_*``).

    Attributes:
        aor: The address-of-record (``sip:user@domain``).
        username: The digest auth username.
        password: The digest auth password.
        contact: The Contact header value (``<sip:user@host:port;transport=...>``).
        local_sent_by: The Via ``sent-by`` (the transport's actual local
            host:port, or an ``.invalid`` host for WebSocket per RFC 7118).
        transport: The Via transport token (``TLS``, ``WSS``, ``UDP``, ``TCP``).
        expires: The requested registration lifetime in seconds.
        user_agent: The User-Agent header value.
    """

    aor: str
    username: str
    password: str
    contact: str
    local_sent_by: str
    transport: str = "TLS"
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


@dataclass(slots=True)
class _Transaction:
    """The single outstanding REGISTER request and its challenge state."""

    cseq: int
    requested_expires: int
    challenged: bool = False


def _registrar_uri(aor: str) -> str:
    """Derive the registrar request-URI (``sip:domain``) from an AOR."""
    scheme, _, rest = aor.partition(":")
    host = rest.split(";", 1)[0].split("@", 1)[-1]
    return f"{scheme}:{host}"


class RegistrationFlow:
    """Drives one extension's REGISTER lifecycle as a sans-IO state machine."""

    def __init__(self, config: RegistrationConfig) -> None:
        """Initialise the flow with a stable Call-ID and From-tag."""
        self._cfg = config
        self._registrar = _registrar_uri(config.aor)
        self._call_id = new_call_id()
        self._from_tag = new_tag()
        self._cseq = 0
        self._registered = False
        self._txn: _Transaction | None = None

    def start(self) -> str:
        """Begin a (re)registration transaction; returns the REGISTER request."""
        return self._begin(self._cfg.expires)

    def deregister(self) -> str:
        """Begin a de-registration transaction (``Expires: 0``).

        Raises:
            RuntimeError: If called before a successful registration.
        """
        if not self._registered:
            msg = "cannot de-register: not registered"
            raise RuntimeError(msg)
        return self._begin(0)

    def handle(self, response: SipResponse) -> RegistrationOutcome:
        """Consume the response to the outstanding request.

        Raises:
            RuntimeError: If there is no outstanding request, or the response's
                CSeq does not match it.
        """
        txn = self._txn
        if txn is None:
            msg = "no outstanding REGISTER request to handle"
            raise RuntimeError(msg)
        self._check_cseq(response, txn)
        status = response.status_code
        if status == _OK:
            self._registered = txn.requested_expires > 0
            self._txn = None
            return Registered(expires=self._granted_expires(response))
        if status in (_UNAUTHORIZED, _PROXY_AUTH_REQUIRED):
            if txn.challenged:
                self._txn = None  # already answered once; the credential is wrong
                return Failed(status, response.reason)
            return Challenged(request=self._reauthenticate(response, status, txn))
        self._txn = None
        return Failed(status, response.reason)

    def _begin(self, requested_expires: int) -> str:
        self._cseq += 1
        self._txn = _Transaction(cseq=self._cseq, requested_expires=requested_expires)
        return self._build(expires=requested_expires, auth=None)

    def _reauthenticate(
        self, response: SipResponse, status: int, txn: _Transaction
    ) -> str:
        if status == _UNAUTHORIZED:
            challenge_header, auth_header = "WWW-Authenticate", "Authorization"
        else:
            challenge_header, auth_header = "Proxy-Authenticate", "Proxy-Authorization"
        challenge = DigestChallenge.parse(response.header(challenge_header) or "")
        auth_value = build_authorization(
            challenge,
            DigestCredentials(self._cfg.username, self._cfg.password),
            method="REGISTER",
            uri=self._registrar,
        )
        self._cseq += 1
        txn.cseq = self._cseq
        txn.challenged = True
        return self._build(
            expires=txn.requested_expires, auth=(auth_header, auth_value)
        )

    def _check_cseq(self, response: SipResponse, txn: _Transaction) -> None:
        cseq = response.header("CSeq")
        if cseq is None:
            return  # a real registrar always echoes CSeq; nothing to correlate here
        number = cseq.split()[0] if cseq.split() else ""
        if not number.isdigit() or int(number) != txn.cseq:
            msg = f"response CSeq {cseq!r} does not match outstanding {txn.cseq}"
            raise RuntimeError(msg)

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

    def _build(self, *, expires: int, auth: tuple[str, str] | None) -> str:
        sent_by = self._cfg.local_sent_by
        via = f"SIP/2.0/{self._cfg.transport} {sent_by};branch={new_branch()};rport"
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
            headers.append(auth)
        return build_request("REGISTER", self._registrar, headers)
