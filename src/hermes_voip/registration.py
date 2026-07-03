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

__all__ = [
    "Challenged",
    "Failed",
    "Registered",
    "RegistrationConfig",
    "RegistrationFlow",
    "RegistrationOutcome",
    "Retry",
    "ViaTransport",
]

import re
from dataclasses import dataclass, field
from typing import Literal

from hermes_voip._decimal import _parse_decimal
from hermes_voip._header_list import split_header_list
from hermes_voip.digest import (
    DigestChallenge,
    DigestCredentials,
    build_authorization,
    pick_best_challenge,
)
from hermes_voip.message import (
    SipResponse,
    build_request,
    new_branch,
    new_call_id,
    new_tag,
)

#: The set of Via transport tokens accepted by :class:`RegistrationConfig`.
#: Extending this set requires a corresponding update to
#: ``_require_secure_scheme`` and ``_SECURE_TRANSPORTS`` (ADR-0005/ADR-0080).
type ViaTransport = Literal["TLS", "WSS", "UDP", "TCP"]

# Runtime counterpart of the ViaTransport Literal — kept in sync by hand.
# Used in __post_init__ to reject unknown transport tokens early (bk236).
_VALID_TRANSPORTS: frozenset[str] = frozenset({"TLS", "WSS", "UDP", "TCP"})

_UNAUTHORIZED = 401
_PROXY_AUTH_REQUIRED = 407
_INTERVAL_TOO_BRIEF = 423
_OK = 200
_3XX = 300  # first non-2xx status code; used in the 2xx success range check
# Synthetic (non-SIP) status for a 2xx whose grant for OUR binding is not a usable
# positive lifetime: the registrar accepted the message but REMOVED the binding
# (``expires=0`` is a de-registration we did not request — RFC 3261 §10.3 lets a
# registrar grant a shorter lifetime, including 0) OR echoed a MALFORMED expires
# (negative or non-numeric, which §10.2/§10.3 forbid). Either way it is reported as
# a Failed outcome so the manager never treats a removed/garbled binding as a live
# registration (which would arm a 0-delay refresh and busy-loop). 0 is outside the
# 1xx-6xx SIP range, so it never collides with a real status and unambiguously marks
# this anomaly.
_BINDING_REMOVED = 0
# The whole ``expires=`` parameter VALUE token on a Contact binding — captured
# verbatim (digits, a leading ``-``, or non-numeric garbage) so the flow can tell
# "a usable non-negative lifetime" apart from "an expires param is present but
# MALFORMED" apart from "no expires param at all". RFC 3261 §10.2/§10.3 define
# Expires as a non-negative delta-seconds, so anything that is not a run of digits
# is malformed and must fail closed rather than fall back to the requested lifetime
# (codex MUST-FIX 1). The narrower ``\d+`` form alone silently dropped a malformed
# token, hiding a registrar that did not actually grant our binding.
_EXPIRES_TOKEN = re.compile(r";\s*expires\s*=\s*([^;,\s]+)", re.IGNORECASE)
# The addr-spec inside a Contact name-addr's angle brackets (``<sip:...>``).
_ANGLE_ADDR = re.compile(r"<([^>]*)>")
# The SIP schemes an AOR may carry; sips is ADR-0005's SIP-over-TLS scheme.
_AOR_SCHEMES = frozenset({"sip", "sips"})
# The Via transports that carry signalling over an encrypted channel (lower-cased
# for case-insensitive comparison). On these, ADR-0005's SIP-over-TLS/SIPS mandate
# is an invariant, so a cleartext ``sip:`` AOR is rejected (ADR-0080). UDP/TCP are
# absent: their AOR scheme is the deployer's responsibility, not this invariant's.
_SECURE_TRANSPORTS = frozenset({"tls", "wss"})


def _split_contacts(header_value: str) -> list[str]:
    """Split a Contact header into its individual bindings (RFC 3261 §10.3).

    A REGISTER 200 OK may carry several bindings either as repeated ``Contact``
    headers or comma-separated within one header value. Delegates to the shared
    :func:`~hermes_voip._header_list.split_header_list`, which breaks only on a
    top-level comma (one outside a name-addr's ``<...>`` or a quoted display-name).
    """
    return split_header_list(header_value)


def _binding_uri(binding: str) -> str:
    """The bare addr-spec of one Contact binding (inside ``<...>`` if present)."""
    match = _ANGLE_ADDR.search(binding)
    if match is not None:
        return match.group(1).strip()
    # A bare URI form (no angle brackets): everything before the first ';' param.
    return binding.split(";", 1)[0].strip()


#: The default port for each SIP scheme (RFC 3261 §19.1.2). Used only as a
#: scheme-keyed fallback for pragmatic Contact-binding canonicalisation when the
#: caller supplies no transport-derived default. This is not strict RFC 3261
#: §19.1.4 URI equality: §19.1.4 treats an omitted port as different from an
#: explicitly present default port.
_DEFAULT_PORT: dict[str, int] = {"sip": 5060, "sips": 5061}

#: The default signalling port keyed by the active Via transport (ADR-0090). On a
#: secure leg (TLS/WSS, which carry SIP-over-TLS per ADR-0005) the wire default is
#: 5061, so a registrar that echoes our portless Contact with an explicit ``:5061``
#: — even on a ``sip:`` addr-spec whose ``transport=tls`` param ``_binding_uri``
#: strips — is binding the same endpoint. UDP/TCP keep the cleartext 5060 default.
_TRANSPORT_DEFAULT_PORT: dict[str, int] = {
    "TLS": 5061,
    "WSS": 5061,
    "UDP": 5060,
    "TCP": 5060,
}


def _split_host_port(hostport: str) -> tuple[str, str] | None:
    """Split a ``host[:port]`` (or bracketed IPv6 ``[..]:port``) into ``(host, port)``.

    Returns the host and the raw port token (``""`` when the port is omitted), or
    ``None`` for a malformed reference. An IPv6 literal is bracketed
    (``[2001:db8::1]:5061``), so the port-delimiting colon is only the one OUTSIDE
    the brackets — a plain ``rpartition(':')`` would wrongly split inside the
    address.
    """
    if hostport.startswith("["):
        close = hostport.find("]")
        if close == -1:
            return None
        host = hostport[: close + 1]
        remainder = hostport[close + 1 :]
        if remainder == "":
            return host, ""
        if remainder.startswith(":"):
            return host, remainder[1:]
        return None
    host, _, port_str = hostport.partition(":")
    return host, port_str


def _split_contact_binding_uri(uri: str) -> tuple[str, str, str, int | None] | None:
    """Decompose a Contact addr-spec for pragmatic binding canonicalisation.

    Operates on a Contact addr-spec (``scheme:[user@]host[:port]`` optionally
    followed by ``;uri-params``). ``_binding_uri`` strips the angle brackets but
    leaves ``;uri-params`` inside name-addrs; they are intentionally out of scope
    for this OUR-AOR binding match, so this parser splits userinfo at the LAST
    ``@`` first and then strips ``;uri-params`` from the HOSTPORT portion only.
    This preserves legal semicolons in userinfo, such as
    ``sip:alice;day=tuesday@host``. Returns ``None`` for anything that is not a
    ``scheme:[user@]host[:port]`` shape so the caller falls back to no match
    rather than mis-parsing.
    """
    scheme, sep, rest = uri.partition(":")
    if not sep or not scheme or not rest:
        return None
    userinfo, at, hostport_with_params = rest.rpartition("@")
    if not at:
        userinfo, hostport_with_params = "", rest
    # URI/header parameters are out of scope for this bounded comparison because
    # ``_binding_uri`` already trims params from bare Contact forms, and this
    # helper is used only to choose among Contact bindings echoed for our own AOR.
    # Strip them after userinfo parsing so semicolons inside userinfo survive.
    hostport = hostport_with_params.split(";", 1)[0]
    if not hostport:
        return None
    split = _split_host_port(hostport)
    if split is None:
        return None
    host, port_str = split
    if not host:
        return None
    if port_str == "":
        port: int | None = None
    else:
        port = _parse_decimal(port_str)
        if port is None:
            return None
    return scheme, userinfo, host, port


def _contact_binding_matches(
    a: str, b: str, *, default_port: int | None = None
) -> bool:
    """Whether two Contact addr-specs identify the same pragmatic binding.

    This is a deliberate, bounded canonicalisation for matching a registrar's
    200-OK echo of OUR OWN Contact, not generic RFC 3261 §19.1.4 SIP-URI
    equality. Strict §19.1.4 does not equate an omitted port with an explicitly
    present default port and also has parameter/percent-encoding rules this
    helper does not implement. For this flow, the useful canonicalisations are:
    scheme case-folding, host case-folding, case-sensitive userinfo, and eliding
    an omitted port to the active transport's signalling default. That recognises
    a registrar echo such as ``sip:1000@PBX.EXAMPLE.TEST:5061`` for our portless
    ``sip:1000@pbx.example.test`` while remaining scoped to Contact bindings for
    our own AOR.

    ``default_port`` is the elided port — the active transport's signalling
    default (ADR-0090): on a TLS/WSS leg every binding is reached over 5061, so an
    explicit ``:5061`` echoed against our omitted port is treated as the same
    binding even when the addr-spec scheme is bare ``sip`` (its
    ``transport=tls`` param is stripped before comparison). When ``None`` the
    per-scheme RFC 3261 §19.1.2 default is used (5060 ``sip`` / 5061 ``sips``).

    URI parameters, headers, and percent-encoding equivalence are out of scope:
    our minted Contact carries no percent-encoding, and :func:`_binding_uri`
    already strips parameters from bare Contact forms before this OUR-AOR-scoped
    comparison. If either side is not a parseable SIP addr-spec, this returns
    ``False`` so a malformed echo can never spuriously match.
    """
    if a == b:
        return True
    parsed_a = _split_contact_binding_uri(a)
    parsed_b = _split_contact_binding_uri(b)
    if parsed_a is None or parsed_b is None:
        return False
    scheme_a, user_a, host_a, port_a = parsed_a
    scheme_b, user_b, host_b, port_b = parsed_b
    scheme_a, scheme_b = scheme_a.lower(), scheme_b.lower()
    if scheme_a != scheme_b or user_a != user_b:
        return False
    if host_a.lower() != host_b.lower():
        return False
    elided = default_port if default_port is not None else _DEFAULT_PORT.get(scheme_a)
    effective_a = port_a if port_a is not None else elided
    effective_b = port_b if port_b is not None else elided
    return effective_a == effective_b


def _split_aor(aor: str) -> tuple[str, str]:
    """Split an AOR into its ``(scheme, host)`` for the registrar request-URI.

    The host is the AOR host, dropping any ``user@`` and trailing ``;params``.

    Raises:
        ValueError: If the AOR has no ``sip``/``sips`` scheme or an empty host.
    """
    scheme, sep, rest = aor.partition(":")
    if not sep or scheme.lower() not in _AOR_SCHEMES:
        msg = f"aor must use a sip(s): scheme: {aor!r}"
        raise ValueError(msg)
    host = rest.split(";", 1)[0].split("@", 1)[-1]
    if not host:
        msg = f"aor has no host: {aor!r}"
        raise ValueError(msg)
    return scheme, host


def _require_secure_scheme(aor: str, transport: str) -> None:
    """Reject a cleartext ``sip:`` AOR on a secure (TLS/WSS) transport (ADR-0080).

    ADR-0005 mandates SIP-over-TLS (SIPS) on the encrypted transports, so a bare
    ``sip:`` AOR there is internally inconsistent: the registrar request-URI and
    the digest ``uri`` would advertise an insecure scheme over a secure leg with
    no signal. The check is **transport-gated** — UDP/TCP leave the scheme to the
    deployer — and a ``sips:`` AOR is accepted on any transport. The comparison is
    case-insensitive (``tls`` and ``TLS`` are the same transport).

    Raises:
        ValueError: If ``aor`` uses the ``sip`` scheme on a TLS/WSS transport.
    """
    scheme, _ = _split_aor(aor)
    if scheme.lower() == "sip" and transport.lower() in _SECURE_TRANSPORTS:
        msg = (
            f"aor must use the sips: scheme on a secure transport "
            f"(transport={transport!r}, ADR-0005/ADR-0080): {aor!r}"
        )
        raise ValueError(msg)


def _min_expires(response: SipResponse) -> int | None:
    """Return the ``Min-Expires`` value from a 423, or ``None`` if absent/malformed."""
    raw = response.header("Min-Expires")
    if raw is None:
        return None
    return _parse_decimal(raw.strip())


@dataclass(frozen=True, slots=True)
class RegistrationConfig:
    """Inputs for a REGISTER flow (credentials sourced from ``HERMES_SIP_*``).

    Attributes:
        aor: The address-of-record (``sip:user@domain`` or ``sips:user@domain``).
            On a TLS/WSS transport it must use ``sips:`` (ADR-0005/ADR-0080),
            enforced in ``__post_init__``.
        username: The digest auth username.
        password: The digest auth password. A **secret** — repr-suppressed so it
            never reaches a log line (it carries the SIP or, on the WSS transport,
            the ``HERMES_SIP_WS_PASSWORD`` credential; ADR-0038 / rule 34).
        contact: The Contact header value (``<sip:user@host:port;transport=...>``).
        local_sent_by: The Via ``sent-by`` (the transport's actual local
            host:port, or an ``.invalid`` host for WebSocket per RFC 7118).
        transport: The Via transport token (``TLS``, ``WSS``, ``UDP``, ``TCP``).
        expires: The requested registration lifetime in seconds.
        user_agent: The User-Agent header value.
    """

    aor: str
    username: str
    password: str = field(repr=False)
    contact: str
    local_sent_by: str
    transport: ViaTransport = "TLS"
    expires: int = 300
    user_agent: str = "hermes-voip/0"

    def __post_init__(self) -> None:
        """Reject a malformed or insecure-scheme AOR at construction (fail fast).

        Validates the AOR scheme/host (bk226), enforces ADR-0005's
        SIP-over-TLS/SIPS mandate on the secure transports (bk231/ADR-0080): a
        ``sip:`` AOR on TLS/WSS is rejected, and rejects a negative ``expires``
        (bk236): ``expires < 0`` would produce ``Expires: -1`` on the wire,
        which is semantically invalid (RFC 3261 §10.2). All checks fail fast at
        construction rather than surfacing mid-flow as a confusing gateway
        rejection.

        ``transport`` is normalised to uppercase before validation so a caller
        who passes a lowercase token (e.g. ``"tls"``) gets the canonical
        ``ViaTransport`` Literal value stored (``"TLS"``). The dataclass is
        frozen, so normalisation uses ``object.__setattr__`` (the only safe
        mutation path for a frozen dataclass in ``__post_init__``).

        Raises:
            ValueError: If ``aor`` has no ``sip``/``sips`` scheme, an empty host,
                uses the ``sip`` scheme on a TLS/WSS transport, or ``expires < 0``,
                or ``transport`` is not a recognised token (bk236).
        """
        # Normalise to uppercase first so the stored field always satisfies the
        # Literal["TLS","WSS","UDP","TCP"] contract at runtime; must precede the
        # membership check so the check and _require_secure_scheme both see the
        # canonical form.
        normalised = self.transport.upper()
        object.__setattr__(self, "transport", normalised)
        if normalised not in _VALID_TRANSPORTS:
            allowed = ", ".join(sorted(_VALID_TRANSPORTS))
            msg = f"transport must be one of {allowed}; got {self.transport!r}"
            raise ValueError(msg)
        _split_aor(self.aor)
        _require_secure_scheme(self.aor, self.transport)
        if self.expires < 0:
            msg = f"expires must be >= 0, got {self.expires}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Challenged:
    """The registrar demanded auth; ``request`` is the authenticated REGISTER."""

    request: str


@dataclass(frozen=True, slots=True)
class Retry:
    """The registrar rejected the interval; ``request`` re-registers with a longer one.

    Returned on a ``423 Interval Too Brief``: the transport sends ``request``
    (a fresh REGISTER transaction whose ``Expires`` is at least the registrar's
    ``Min-Expires``) exactly as it would send a :class:`Challenged` request.
    """

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


type RegistrationOutcome = Challenged | Retry | Registered | Failed


@dataclass(slots=True)
class _Transaction:
    """The single outstanding REGISTER request and its challenge state."""

    cseq: int
    requested_expires: int
    challenged: bool = False


def _registrar_uri(aor: str) -> str:
    """Derive the registrar request-URI (``sip:domain`` / ``sips:domain``) from an AOR.

    Raises:
        ValueError: If the AOR has no ``sip``/``sips`` scheme or an empty host.
    """
    scheme, host = _split_aor(aor)
    return f"{scheme}:{host}"


class RegistrationFlow:
    """Drives one extension's REGISTER lifecycle as a sans-IO state machine."""

    def __init__(self, config: RegistrationConfig) -> None:
        """Initialise the flow with a stable Call-ID and From-tag."""
        self._cfg = config
        self._registrar = _registrar_uri(config.aor)
        # Our own binding's bare addr-spec, used to pick OUR Contact (and its
        # expires) out of a multi-binding 200 OK echo (RFC 3261 §10.3).
        self._contact_uri = _binding_uri(config.contact)
        self._call_id = new_call_id()
        self._from_tag = new_tag()
        self._cseq = 0
        self._registered = False
        self._txn: _Transaction | None = None
        self._interval_retried = False

    @property
    def call_id(self) -> str:
        """The stable ``Call-ID`` of this registration's REGISTER dialog.

        The :class:`RegistrationManager` demuxes REGISTER responses to the owning
        flow by this value (ADR-0011). It is distinct from any call dialog's
        ``Call-ID`` — registration and call have independent transaction spaces
        (invariant 2).
        """
        return self._call_id

    def start(self) -> str:
        """Begin a (re)registration transaction; returns the REGISTER request."""
        self._interval_retried = False
        return self._begin(self._cfg.expires)

    def deregister(self) -> str:
        """Begin a de-registration transaction (``Expires: 0``).

        Raises:
            RuntimeError: If called before a successful registration.
        """
        if not self._registered:
            msg = "cannot de-register: not registered"
            raise RuntimeError(msg)
        self._interval_retried = False
        return self._begin(0)

    def handle(self, response: SipResponse) -> RegistrationOutcome:
        """Consume the response to the outstanding request.

        An UNANSWERABLE 401/407 challenge (only an unsupported algorithm, ``qop``
        without ``auth``, a missing/absent nonce or realm) fails CLOSED to a
        :class:`Failed` outcome rather than raising: the digest layer's
        ``ValueError`` is consumed here so it can never propagate out to the shared
        signalling reader loop and tear down unrelated calls (ADR-0081).

        Raises:
            RuntimeError: If there is no outstanding request, or the response's
                CSeq does not match it (a duplicate/late/superseded final routed
                back by the stable registration Call-ID). The
                :class:`RegistrationManager` consumer treats this as an ignorable
                stray response — it, too, never propagates to the reader loop.
        """
        txn = self._txn
        if txn is None:
            msg = "no outstanding REGISTER request to handle"
            raise RuntimeError(msg)
        self._check_cseq(response, txn)
        status = response.status_code
        if _OK <= status < _3XX:
            # Accept any 2xx as success (RFC 3261 §21.2 — 200 OK is the most common,
            # but 202 Accepted is also a valid response to REGISTER on some gateways).
            return self._handle_success(response, txn)
        if status in (_UNAUTHORIZED, _PROXY_AUTH_REQUIRED):
            return self._handle_challenge(response, status, txn)
        if status == _INTERVAL_TOO_BRIEF:
            retry = self._retry_interval(response, txn)
            if retry is not None:
                return retry
            self._txn = None
            return Failed(status, response.reason)
        self._txn = None
        return Failed(status, response.reason)

    def _handle_challenge(
        self, response: SipResponse, status: int, txn: _Transaction
    ) -> Challenged | Failed:
        """Answer a 401/407 challenge, or fail CLOSED if it is unanswerable.

        A SECOND challenge in the same transaction is Failed (the credential is
        wrong; recovery is the manager's next refresh — ADR-0080). A FIRST challenge
        the digest layer cannot answer raises ``ValueError`` out of
        ``_reauthenticate`` — from ``pick_best_challenge`` (only an unsupported
        algorithm, e.g. a legitimate RFC 8760 SHA-512-256), ``build_authorization``
        (``qop`` without ``auth``, e.g. auth-int), or ``DigestChallenge.parse`` (a
        missing/garbled nonce or realm, including a 401/407 with NO challenge header,
        where ``_strongest_challenge`` parses ``""``). That ``ValueError`` is
        CONSUMED here and mapped to :class:`Failed` — exactly like the second-challenge
        path and every other non-answerable branch of :meth:`handle` — so the manager
        reports it and recovers via a fresh REGISTER (bounded backoff).

        It must NEVER propagate: ``on_response`` has no guard and runs OUTSIDE the
        signalling reader loop's parse-only ``except ValueError``, so an escaping
        ``ValueError`` would unwind ``_read_loop`` and tear down the whole shared
        connection — every active call and every registration (ADR-0081: one
        malformed message must not be a DoS against unrelated calls). The digest layer
        keeps its raising contract; registration fails closed on it.
        """
        if txn.challenged:
            self._txn = None  # already answered once; the credential is wrong
            return Failed(status, response.reason)
        try:
            request = self._reauthenticate(response, status, txn)
        except ValueError:
            # Unanswerable challenge (see the method docstring): the digest layer's
            # ValueError is consumed and mapped to a fail-closed Failed, never raised
            # out to on_response / the shared reader loop (ADR-0081 DoS invariant).
            self._txn = None
            return Failed(status, response.reason)
        return Challenged(request=request)

    def _handle_success(
        self, response: SipResponse, txn: _Transaction
    ) -> Registered | Failed:
        """Turn a 2xx into a Registered or, on a non-usable grant, a Failed.

        A registrar MAY grant a shorter lifetime than requested, including 0 (RFC
        3261 §10.3). For OUR binding to a *registration* request
        (``requested_expires > 0``), a granted lifetime that is not a positive
        delta-seconds means the registrar did not actually keep the binding alive:

        * ``0`` (or a value parsed as ``<= 0``) is an unrequested de-registration —
          it REMOVED the binding;
        * a MALFORMED echo (negative or non-numeric ``expires`` on our Contact,
          signalled by ``_granted_expires`` returning ``None``) is garbage the old
          digits-only parse silently dropped, falling back to the positive requested
          lifetime and hiding the anomaly (codex MUST-FIX 1).

        Both are surfaced as ``Failed`` — never ``Registered`` — so the manager
        treats them as a registration failure and never arms a refresh (a positive
        fallback would re-REGISTER into a tight loop against a binding the registrar
        is not honouring). A 2xx to a *de-registration* (``requested_expires == 0``)
        keeps its existing meaning (a clean unbind, not registered); a malformed
        expires there maps to ``0`` (there is no live lifetime to arm anyway).
        """
        granted = self._granted_expires(response)
        self._txn = None
        if txn.requested_expires > 0 and (granted is None or granted <= 0):
            self._registered = False
            detail = (
                "malformed/negative" if granted is None else f"non-positive ({granted})"
            )
            return Failed(
                _BINDING_REMOVED,
                f"registrar granted a {detail} expires for our binding; removed",
            )
        self._registered = txn.requested_expires > 0
        return Registered(expires=granted if granted is not None else 0)

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
        challenge = self._strongest_challenge(response, challenge_header)
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

    def _strongest_challenge(
        self, response: SipResponse, challenge_header: str
    ) -> DigestChallenge:
        """Select the strongest digest challenge the registrar offered (RFC 8760 §2.4).

        A registrar that supports SHA-256 sends MULTIPLE
        ``WWW-Authenticate``/``Proxy-Authenticate`` challenges (e.g. SHA-256 AND
        MD5 for back-compat), and the client MUST authenticate with the
        most-preferred algorithm it supports. Reading only the first header would
        let a gateway — or an on-path attacker who reorders headers (SIP digest
        has no header integrity) — silently downgrade us from SHA-256 to MD5.

        Every challenge header is parsed; ``pick_best_challenge`` then applies the
        RFC 8760 §3 preference order (SHA-256-sess > SHA-256 > MD5-sess > MD5) and
        skips any unsupported/unknown algorithm. A single-MD5 challenge (the
        legacy case) is returned unchanged.

        Raises:
            ValueError: If the response carries no challenge header, or no offered
                challenge uses a supported algorithm.
        """
        raw_challenges = response.headers_all(challenge_header)
        if not raw_challenges:
            # No header at all: parse("") raises the same "missing nonce" error as
            # before, keeping the no-challenge failure path identical.
            return DigestChallenge.parse("")
        challenges = [DigestChallenge.parse(raw) for raw in raw_challenges]
        return pick_best_challenge(challenges)

    def _retry_interval(self, response: SipResponse, txn: _Transaction) -> Retry | None:
        """Re-issue REGISTER honouring ``Min-Expires`` after a 423, or ``None``.

        Returns ``None`` (the caller then fails) when there is nothing to comply
        with: a de-registration, an already-retried attempt (no loops), or a
        ``Min-Expires`` that is missing, malformed, or not larger than what we
        already requested.
        """
        if self._interval_retried or txn.requested_expires <= 0:
            return None
        min_expires = _min_expires(response)
        if min_expires is None or min_expires <= txn.requested_expires:
            return None
        self._interval_retried = True
        return Retry(request=self._begin(min_expires))

    def _check_cseq(self, response: SipResponse, txn: _Transaction) -> None:
        cseq = response.header("CSeq")
        if cseq is None:
            return  # a real registrar always echoes CSeq; nothing to correlate here
        parts = cseq.split()
        number = parts[0] if parts else ""
        method = parts[1] if len(parts) > 1 else ""
        # RFC 3261 §8.1.3.5: the CSeq sequence number must match and the method
        # must be REGISTER (a mismatched method is a protocol error — a response
        # to a different transaction routed to this flow by mistake).
        # A non-decimal or over-long CSeq number falls into this RuntimeError mismatch
        # path (which on_response catches and ignores) instead of raising a bare
        # ValueError that would escape to the reader loop and tear the connection down
        # (ADR-0081).
        if _parse_decimal(number) != txn.cseq or method != "REGISTER":
            msg = (
                f"response CSeq {cseq!r} does not match outstanding {txn.cseq} REGISTER"
            )
            raise RuntimeError(msg)

    def _granted_expires(self, response: SipResponse) -> int | None:
        """The granted lifetime for OUR binding, or ``None`` if it is malformed.

        A 200 OK to REGISTER echoes EVERY binding the registrar holds for the
        AOR, each with its own ``expires`` — so the refresh window must be read
        from OUR Contact, not whichever binding comes first (another device's
        lifetime would arm the wrong timer and let our binding lapse). All
        ``Contact`` headers are flattened into individual bindings; the one
        whose addr-spec pragmatically canonicalises to our Contact URI supplies
        the value: scheme/host are case-insensitive, userinfo is case-sensitive,
        and an omitted port elides to the active transport's signalling default
        (ADR-0090). This is deliberately not strict RFC 3261 §19.1.4 URI
        equality, which would miss a registrar that echoes our portless binding
        with a differing host case or an explicit default port. Failing that
        bounded OUR-AOR match, the first binding's ``expires`` is the fallback,
        then the ``Expires`` header, then our requested lifetime.

        When the chosen binding carries an ``expires`` parameter whose value is
        present but MALFORMED — negative or non-numeric, which RFC 3261 §10.2/§10.3
        forbid — this returns ``None`` (fail-closed) rather than discarding the
        garbled token and falling back to the positive requested lifetime. The
        caller (:meth:`_handle_success`) treats ``None`` the same as a non-positive
        grant, so a registrar that does not actually grant our binding can never be
        mistaken for a live registration (codex MUST-FIX 1). A binding with NO
        ``expires`` parameter is not malformed: it falls through to the ``Expires``
        header / requested-lifetime fallbacks unchanged.
        """
        bindings = [
            binding
            for header in response.headers_all("Contact")
            for binding in _split_contacts(header)
        ]
        # The elided default port is the active transport's signalling default
        # (5061 on the secure TLS/WSS legs, 5060 on UDP/TCP) — ADR-0090.
        default_port = _TRANSPORT_DEFAULT_PORT[self._cfg.transport]
        ours = next(
            (
                b
                for b in bindings
                if _contact_binding_matches(
                    _binding_uri(b), self._contact_uri, default_port=default_port
                )
            ),
            None,
        )
        chosen = ours if ours is not None else (bindings[0] if bindings else None)
        if chosen is not None:
            token = _EXPIRES_TOKEN.search(chosen)
            if token is not None:
                value = token.group(1)
                # Present but not a valid ASCII decimal (e.g. ``-1`` / ``abc`` /
                # U+00B2 ² / thousands of digits) is malformed: fail closed,
                # never the positive fallback.
                return _parse_decimal(value)
        expires = response.header("Expires")
        if expires is not None:
            stripped = expires.strip()
            # Same fail-closed parser so malformed Expires never propagates a
            # bare ValueError.
            parsed = _parse_decimal(stripped)
            if parsed is not None:
                return parsed
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
