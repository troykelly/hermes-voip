"""RFC 2617/7616/8760 HTTP/SIP digest authentication (``auth`` quality of protection).

SIP (RFC 3261 §22) reuses HTTP digest. A registrar/proxy answers a request with
``401``/``407`` carrying a ``WWW-Authenticate``/``Proxy-Authenticate`` challenge;
the client re-sends the request with an ``Authorization``/``Proxy-Authorization``
header whose ``response`` is keyed by the shared password. This module parses the
challenge and builds that header.

Supported algorithms (RFC 8760 preference order, strongest first):

- ``SHA-256-sess`` — SHA-256 with session binding (HA1 mixes nonce+cnonce)
- ``SHA-256``      — SHA-256 plain (RFC 7616 / RFC 8760)
- ``MD5-sess``     — MD5 with session binding
- ``MD5``          — plain MD5 (RFC 2617; wire-format mandatory baseline)

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
from collections.abc import Sequence
from dataclasses import dataclass, field

from hermes_voip._chars import contains_control

# An auth-param is ``name=token`` or ``name="quoted-string"``. The quoted
# alternative is escape-aware (RFC 2617 quoted-pair): ``\"`` is a literal quote
# and ``\\`` a literal backslash, so the value spans to the matching unescaped
# ``"`` instead of stopping at the first inner quote. The bare (unquoted)
# alternative is an RFC 3261/2617 ``token``, which excludes the separators
# ``,``, ``;`` and whitespace — so it must stop at ``;`` rather than running on
# and swallowing semicolon-delimited trailing content into the value.
_PARAM = re.compile(r'(\w[\w-]*)=(?:"((?:[^"\\]|\\.)*)"|([^,;\s]+))')
# A quoted-pair: a backslash followed by any single character it escapes.
_QUOTED_PAIR = re.compile(r"\\(.)")

# Supported algorithm tokens (case-insensitive).  All four variants in the
# RFC 8760 preference order — SHA-256-sess > SHA-256 > MD5-sess > MD5.
# Any other token is rejected rather than silently mis-signed.
_SUPPORTED_ALGORITHMS = frozenset({"md5", "md5-sess", "sha-256", "sha-256-sess"})

# RFC 8760 §3 preference order: index 0 = most preferred.
_ALGORITHM_PREFERENCE: tuple[str, ...] = (
    "sha-256-sess",
    "sha-256",
    "md5-sess",
    "md5",
)

# RFC 2617/7616 nonce-count is exactly 8 lowercase hex digits on the wire.
_NC_MIN = 1
_NC_MAX = 0xFFFFFFFF


def _unescape(quoted: str) -> str:
    r"""Resolve RFC 2617 quoted-pair escapes (``\X`` -> ``X``) in a parsed value.

    The inverse of :func:`_quoted`'s escaping, applied to the body of a
    quoted-string so realm/nonce/opaque reach HA1/HA2 in their literal form.
    """
    return _QUOTED_PAIR.sub(r"\1", quoted)


def _md5_hex(value: str) -> str:
    """Return the lowercase hex MD5 digest of ``value`` (UTF-8 encoded)."""
    return hashlib.md5(value.encode("utf-8")).hexdigest()  # noqa: S324 - digest scheme mandates MD5


def _sha256_hex(value: str) -> str:
    """Return the lowercase hex SHA-256 digest of ``value`` (UTF-8 encoded)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _quoted(value: str) -> str:
    """Render ``value`` as an RFC 2617 quoted-string body (without the quotes).

    Rejects control characters (which a hostile or garbled peer could use to
    inject extra SIP headers once this lands in an ``Authorization`` line) and
    escapes the backslash and double-quote characters per the quoted-pair rule.

    Raises:
        ValueError: If ``value`` contains a control character.
    """
    if contains_control(value):
        msg = "auth-param value contains a control character"
        raise ValueError(msg)
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _hash_hex(algo_lower: str, value: str) -> str:
    """Hash ``value`` with the base algorithm (stripping ``-sess`` suffix).

    Args:
        algo_lower: The algorithm token in lower-case (e.g. ``"sha-256-sess"``).
        value: The UTF-8 string to hash.

    Returns:
        Lowercase hex digest.
    """
    base = algo_lower.removesuffix("-sess")
    if base == "sha-256":
        return _sha256_hex(value)
    # base == "md5" — the only other supported base
    return _md5_hex(value)


def _compute_ha1(  # noqa: PLR0913 - HA1 inputs are irreducible (algo+credentials+nonce+cnonce)
    algo_lower: str,
    username: str,
    realm: str,
    password: str,
    nonce: str,
    cnonce: str,
) -> str:
    """Compute HA1 per RFC 7616 §3.4 / RFC 2617 §3.2.2.

    For plain algorithms: ``HA1 = H(username:realm:password)``.
    For ``-sess`` variants: ``HA1 = H(H(username:realm:password):nonce:cnonce)``.

    Args:
        algo_lower: Algorithm token in lower-case (e.g. ``"md5-sess"``).
        username: The digest username.
        realm: The server protection realm.
        password: The shared secret.
        nonce: The server nonce (used only for ``-sess`` variants).
        cnonce: The client nonce (used only for ``-sess`` variants).

    Returns:
        HA1 hex string.
    """
    h1_base = _hash_hex(algo_lower, f"{username}:{realm}:{password}")
    if algo_lower.endswith("-sess"):
        return _hash_hex(algo_lower, f"{h1_base}:{nonce}:{cnonce}")
    return h1_base


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
            # Quoted values carry quoted-pair escapes; bare tokens never do.
            value = _unescape(quoted) if quoted is not None else bare
            params[match.group(1).lower()] = value
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


def pick_best_challenge(challenges: Sequence[DigestChallenge]) -> DigestChallenge:
    """Return the strongest-algorithm challenge from ``challenges``.

    Applies the RFC 8760 §3 preference order: SHA-256-sess > SHA-256 >
    MD5-sess > MD5.  Challenges with an unsupported or unknown algorithm
    are skipped; the highest-ranked supported challenge wins.  If multiple
    challenges share the same algorithm, the first one in iteration order is
    returned.

    Args:
        challenges: One or more challenges offered by the server.

    Returns:
        The preferred :class:`DigestChallenge`.

    Raises:
        ValueError: If ``challenges`` is empty or contains no supported algorithm.
    """
    if not challenges:
        msg = "empty challenge list: at least one challenge is required"
        raise ValueError(msg)

    best: DigestChallenge | None = None
    best_rank = len(_ALGORITHM_PREFERENCE)  # sentinel: higher than any valid rank

    for ch in challenges:
        algo = ch.algorithm.lower()
        try:
            rank = _ALGORITHM_PREFERENCE.index(algo)
        except ValueError:
            continue  # unsupported algorithm — skip, don't raise yet
        if rank < best_rank:
            best_rank = rank
            best = ch

    if best is None:
        algos = [ch.algorithm for ch in challenges]
        msg = f"no supported digest algorithm in challenges: {algos!r}"
        raise ValueError(msg)

    return best


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

    Implements RFC 7616 ``qop=auth`` for SHA-256 / SHA-256-sess and RFC 2617
    ``qop=auth`` for MD5 / MD5-sess.  ``HA1`` bakes in the realm, so the
    challenge realm must match the realm the server validates against.

    The ``-sess`` variants (MD5-sess, SHA-256-sess) bind the session key to the
    nonce and cnonce: ``HA1 = H(H(user:realm:pass):nonce:cnonce)``.

    The RFC 2069 legacy no-qop construction (``response = H(HA1:nonce:HA2)``) is
    used only for plain ``MD5`` when the challenge omits ``qop`` (ancient-server
    back-compat).  SHA-256, SHA-256-sess, and MD5-sess have no legacy form (RFC
    7616 §3.4.1, RFC 8760 §2.6) and -sess cannot send the cnonce its HA1 needs
    without qop (RFC 2617 §3.2.2); a no-qop challenge for any of these raises.

    Args:
        challenge: The parsed server challenge.
        credentials: The username/password to authenticate with.
        method: The SIP method of the request being authorized (e.g. ``REGISTER``).
        uri: The digest URI — for SIP this is the request URI (e.g. ``sip:host``).
        cnonce: The client nonce; a random one is generated when omitted.
        nc: The nonce-count for this cnonce (must be 1..0xffffffff and
            renders as 8 hex digits).

    Returns:
        The full header value, beginning with ``Digest ``.

    Raises:
        ValueError: If the challenge algorithm is unsupported, if it offers
            ``qop`` but not ``auth``, if a non-MD5 algorithm omits ``qop``, or
            if any rendered auth-param value contains a control character.
    """
    algo_lower = challenge.algorithm.lower()
    if algo_lower not in _SUPPORTED_ALGORITHMS:
        msg = f"unsupported digest algorithm: {challenge.algorithm!r}"
        raise ValueError(msg)
    if challenge.qop and "auth" not in challenge.qop:
        msg = f"challenge offers qop without 'auth': {challenge.qop!r}"
        raise ValueError(msg)

    if not _NC_MIN <= nc <= _NC_MAX:
        msg = "nonce-count must be in the range 1..0xffffffff"
        raise ValueError(msg)

    use_auth_qop = "auth" in challenge.qop
    # The RFC 2069 legacy no-qop construction (response = H(HA1:nonce:HA2)) is
    # valid ONLY for plain MD5. RFC 7616 §3.4.1 / RFC 8760 §2.6 define no legacy
    # form for SHA-256(/-sess), and RFC 2617 §3.2.2 forbids sending the cnonce a
    # -sess HA1 requires when the server offered no qop. So any algorithm other
    # than plain MD5 MUST be answered with qop=auth — reject if it was omitted,
    # rather than mis-signing with a construction the server cannot verify.
    if not use_auth_qop and algo_lower != "md5":
        msg = (
            f"algorithm {challenge.algorithm!r} requires a qop=auth challenge; "
            "the no-qop (RFC 2069 legacy) form is only valid for plain MD5"
        )
        raise ValueError(msg)

    # A cnonce is needed only on the qop=auth path (the only path reachable for
    # -sess after the guard above). The legacy MD5 no-qop path uses none.
    client_nonce = (cnonce or secrets.token_hex(8)) if use_auth_qop else ""

    ha1 = _compute_ha1(
        algo_lower,
        credentials.username,
        challenge.realm,
        credentials.password,
        challenge.nonce,
        client_nonce,
    )
    ha2 = _hash_hex(algo_lower, f"{method}:{uri}")

    params: list[tuple[str, str, bool]] = [
        ("username", credentials.username, True),
        ("realm", challenge.realm, True),
        ("nonce", challenge.nonce, True),
        ("uri", uri, True),
        ("algorithm", challenge.algorithm, False),
    ]

    if use_auth_qop:
        nc_hex = f"{nc:08x}"
        response = _hash_hex(
            algo_lower,
            f"{ha1}:{challenge.nonce}:{nc_hex}:{client_nonce}:auth:{ha2}",
        )
        params += [
            ("qop", "auth", False),
            ("nc", nc_hex, False),
            ("cnonce", client_nonce, True),
            ("response", response, True),
        ]
    else:
        # Plain MD5 only (guarded above): RFC 2069 legacy form, no qop/nc/cnonce.
        params.append(
            ("response", _hash_hex(algo_lower, f"{ha1}:{challenge.nonce}:{ha2}"), True)
        )

    if challenge.opaque is not None:
        params.append(("opaque", challenge.opaque, True))

    rendered = ", ".join(
        f'{name}="{_quoted(value)}"' if quoted else f"{name}={value}"
        for name, value, quoted in params
    )
    return f"Digest {rendered}"
