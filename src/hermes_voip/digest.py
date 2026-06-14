"""RFC 2617 HTTP/SIP digest authentication (the ``auth`` quality of protection).

SIP (RFC 3261 §22) reuses HTTP digest. A registrar/proxy answers a request with
``401``/``407`` carrying a ``WWW-Authenticate``/``Proxy-Authenticate`` challenge;
the client re-sends the request with an ``Authorization``/``Proxy-Authorization``
header whose ``response`` is keyed by the shared password. This module parses the
challenge and builds that header.

Standards-only and dependency-free: no transport, runtime, or gateway specifics
live here. Credentials are passed in by the caller (sourced from ``HERMES_SIP_*``
env at runtime) and never hard-coded — the repo is public.

MD5 is mandated by the digest scheme; its use here is a wire-format requirement,
not a security control (hence the ``S324`` suppressions on the hashing calls).
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field

_PARAM = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]+))')

# Only plain MD5 is implemented; MD5-sess and SHA-* are rejected rather than
# silently mis-signed (a wrong response would just fail against the registrar).
_SUPPORTED_ALGORITHMS = frozenset({"md5"})

# Forbidden control characters: the ASCII C0 range (code points below this) and DEL.
_C0_END = 0x20
_DEL = 0x7F


def _md5_hex(value: str) -> str:
    """Return the lowercase hex MD5 digest of ``value`` (UTF-8 encoded)."""
    return hashlib.md5(value.encode("utf-8")).hexdigest()  # noqa: S324 - digest scheme mandates MD5


def _quoted(value: str) -> str:
    """Render ``value`` as an RFC 2617 quoted-string body (without the quotes).

    Rejects control characters (which a hostile or garbled peer could use to
    inject extra SIP headers once this lands in an ``Authorization`` line) and
    escapes the backslash and double-quote characters per the quoted-pair rule.

    Raises:
        ValueError: If ``value`` contains a control character.
    """
    if any(ord(char) < _C0_END or ord(char) == _DEL for char in value):
        msg = "auth-param value contains a control character"
        raise ValueError(msg)
    return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True, slots=True)
class DigestChallenge:
    """A parsed digest ``WWW-Authenticate`` / ``Proxy-Authenticate`` challenge.

    Attributes:
        realm: The protection realm the server hashes the credential against.
        nonce: The server-issued nonce (single-use on many gateways).
        algorithm: The digest algorithm token, echoed verbatim; defaults to ``MD5``.
        qop: The offered quality-of-protection tokens (e.g. ``("auth",)``).
        opaque: An opaque value to echo back unchanged, if the server sent one.
    """

    realm: str
    nonce: str
    algorithm: str = "MD5"
    qop: tuple[str, ...] = ()
    opaque: str | None = None

    @classmethod
    def parse(cls, header_value: str) -> DigestChallenge:
        """Parse a challenge header value into a :class:`DigestChallenge`.

        Args:
            header_value: The header value, optionally including the leading
                ``Digest`` scheme token.

        Returns:
            The parsed challenge.

        Raises:
            ValueError: If the challenge carries no ``nonce`` (auth is impossible).
        """
        params: dict[str, str] = {}
        for match in _PARAM.finditer(header_value):
            quoted, bare = match.group(2), match.group(3)
            params[match.group(1).lower()] = quoted if quoted is not None else bare
        nonce = params.get("nonce")
        if not nonce:
            msg = "digest challenge is missing a nonce"
            raise ValueError(msg)
        qop_raw = params.get("qop", "")
        qop = tuple(token.strip() for token in qop_raw.split(",") if token.strip())
        return cls(
            realm=params.get("realm", ""),
            nonce=nonce,
            algorithm=params.get("algorithm", "MD5"),
            qop=qop,
            opaque=params.get("opaque"),
        )


@dataclass(frozen=True, slots=True)
class DigestCredentials:
    """The shared-secret credential used to answer a digest challenge."""

    username: str
    password: str = field(repr=False)


def build_authorization(  # noqa: PLR0913 - digest inputs are irreducible; 4 are keyword-only
    challenge: DigestChallenge,
    credentials: DigestCredentials,
    *,
    method: str,
    uri: str,
    cnonce: str | None = None,
    nc: int = 1,
) -> str:
    """Build an ``Authorization`` / ``Proxy-Authorization`` header value.

    Implements RFC 2617 ``qop=auth`` when the challenge offers it, and falls back
    to the RFC 2069 construction (``response = MD5(HA1:nonce:HA2)``) when it does
    not. ``HA1`` bakes in the realm, so the challenge realm must match the realm
    the server validates against.

    Args:
        challenge: The parsed server challenge.
        credentials: The username/password to authenticate with.
        method: The SIP method of the request being authorized (e.g. ``REGISTER``).
        uri: The digest URI — for SIP this is the request URI (e.g. ``sip:host``).
        cnonce: The client nonce; a random one is generated when omitted.
        nc: The nonce-count for this cnonce (rendered as 8 hex digits).

    Returns:
        The full header value, beginning with ``Digest ``.

    Raises:
        ValueError: If the challenge algorithm is unsupported, if it offers
            ``qop`` but not ``auth``, or if any rendered auth-param value
            contains a control character.
    """
    if challenge.algorithm.lower() not in _SUPPORTED_ALGORITHMS:
        msg = f"unsupported digest algorithm: {challenge.algorithm!r}"
        raise ValueError(msg)
    if challenge.qop and "auth" not in challenge.qop:
        msg = f"challenge offers qop without 'auth': {challenge.qop!r}"
        raise ValueError(msg)

    ha1 = _md5_hex(f"{credentials.username}:{challenge.realm}:{credentials.password}")
    ha2 = _md5_hex(f"{method}:{uri}")

    use_auth_qop = "auth" in challenge.qop
    params: list[tuple[str, str, bool]] = [
        ("username", credentials.username, True),
        ("realm", challenge.realm, True),
        ("nonce", challenge.nonce, True),
        ("uri", uri, True),
        ("algorithm", challenge.algorithm, False),
    ]

    if use_auth_qop:
        client_nonce = cnonce if cnonce is not None else secrets.token_hex(8)
        nc_hex = f"{nc:08x}"
        response = _md5_hex(
            f"{ha1}:{challenge.nonce}:{nc_hex}:{client_nonce}:auth:{ha2}"
        )
        params += [
            ("qop", "auth", False),
            ("nc", nc_hex, False),
            ("cnonce", client_nonce, True),
            ("response", response, True),
        ]
    else:
        params.append(("response", _md5_hex(f"{ha1}:{challenge.nonce}:{ha2}"), True))

    if challenge.opaque is not None:
        params.append(("opaque", challenge.opaque, True))

    rendered = ", ".join(
        f'{name}="{_quoted(value)}"' if quoted else f"{name}={value}"
        for name, value, quoted in params
    )
    return f"Digest {rendered}"
