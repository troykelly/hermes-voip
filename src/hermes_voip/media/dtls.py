"""DTLS-SRTP keying module (RFC 5763/5764, ADR-0016) — media/dtls.py.

Implements the DTLS handshake and SRTP keying-material extraction for the
WebRTC media path.  After a DTLS handshake completes over the ICE-nominated
UDP socket, the RFC 5705 exporter (label ``EXTRACTOR-dtls_srtp``) yields a
block of raw bytes from which the client/server SRTP master key||salt pairs
are sliced (RFC 5764 §4.2).  Those raw bytes feed :class:`SrtpSession.from_raw_keys`
— the per-packet RFC 3711 AES-CM/HMAC-SHA1 transform is reused verbatim; only
the key source changes from SDES (``a=crypto`` inline) to DTLS.

**Transport seam.** The engine (or ICE agent) owns the real UDP socket.  This
module operates over a **memory BIO** (pyOpenSSL ``SSL.Connection(ctx, None)``)
so DTLS datagrams are exchanged by the caller:

- :meth:`DtlsEndpoint.feed` — deliver an inbound datagram from the peer.
- :meth:`DtlsEndpoint.get_outbound_datagrams` — retrieve datagrams the DTLS
  state machine wants to send; call after :meth:`feed` and to initiate the
  handshake on the CLIENT side.

The caller is responsible for demuxing DTLS datagrams from STUN/RTP/RTCP
(first byte 20-63 per RFC 7983) before passing them here.

**Dependency gating.** ``pyOpenSSL`` (the ``webrtc`` extra) is lazy-imported so
that ``import hermes_voip.media.dtls`` works in the default install.
:class:`DtlsEndpoint` raises :class:`ImportError` at construction time when the
extra is absent — the error propagates naturally (AGENTS.md rule 37).

**Security invariants.**
- SRTP key material is never logged, repr'd, or raised in exception text.
- Cert and private key are generated ephemerally at construction; nothing is
  stored to disk or committed (mirrors the TLS-loopback fixture lesson in
  memory ``voip-test-cert-no-committed-key-ephemeral-openssl``).
- Peer-cert fingerprint verification (:meth:`DtlsEndpoint.verify_peer_fingerprint`)
  is ENFORCED: :meth:`DtlsEndpoint.derive_srtp_sessions` raises
  :class:`RuntimeError` unless ``verify_peer_fingerprint`` succeeded first
  (RFC 5763 §5).  Both DTLS roles present and verify a certificate (the context
  uses ``VERIFY_PEER`` so the server requests a client cert).
- A fatal DTLS alert during the handshake is not swallowed:
  :meth:`DtlsEndpoint.feed` / :meth:`DtlsEndpoint.get_outbound_datagrams`
  re-raise the error and :meth:`DtlsEndpoint.handshake_failed` returns ``True``.
"""

from __future__ import annotations

import enum
import hashlib
import importlib
import time
from dataclasses import dataclass, field
from typing import Protocol

from hermes_voip._lazy_singleton import LazySingleton
from hermes_voip.media.srtcp import SrtcpSession
from hermes_voip.media.srtp import SrtpSession

# ---------------------------------------------------------------------------
# Public constants (tested)
# ---------------------------------------------------------------------------

#: RFC 5764 §4.2 keying-material exporter label.
_EXPORTER_LABEL: bytes = b"EXTRACTOR-dtls_srtp"

#: SRTP master key length in bytes (AES-128, 128 bits).
_SRTP_KEY_LEN: int = 16

#: SRTP master salt length in bytes (112 bits).
_SRTP_SALT_LEN: int = 14

#: Total exported bytes for SRTP_AES128_CM_HMAC_SHA1_80:
#: client_write_key(16) || server_write_key(16) ||
#: client_write_salt(14) || server_write_salt(14) = 60 bytes.
_EXPORT_LEN: int = 2 * (_SRTP_KEY_LEN + _SRTP_SALT_LEN)  # = 60

#: DTLS ``use_srtp`` extension profile list (highest priority first).
#: Profile name bytes as expected by pyOpenSSL ``set_tlsext_use_srtp``.
_SRTP_PROFILES: bytes = b"SRTP_AES128_CM_SHA1_80:SRTP_AES128_CM_SHA1_32"

#: Suite name for SRTP_AES128_CM_SHA1_80 (matching srtp.py _AES_CM_128_HMAC_SHA1_80).
_SUITE_80: str = "AES_CM_128_HMAC_SHA1_80"
#: Suite name for SRTP_AES128_CM_SHA1_32.
_SUITE_32: str = "AES_CM_128_HMAC_SHA1_32"

#: Mapping from DTLS profile bytes to srtp.py suite name.
_PROFILE_TO_SUITE: dict[bytes, str] = {
    b"SRTP_AES128_CM_SHA1_80": _SUITE_80,
    b"SRTP_AES128_CM_SHA1_32": _SUITE_32,
}

# ---------------------------------------------------------------------------
# Public enums
# ---------------------------------------------------------------------------


class DtlsRole(enum.Enum):
    """DTLS role, determined by the SDP ``a=setup`` negotiation (RFC 4145).

    The SDP ``a=setup:active`` party is the DTLS CLIENT (sends ClientHello);
    ``a=setup:passive`` is the DTLS SERVER.  RFC 5763 §5 maps setup values to
    roles; this enum makes the role explicit in Python rather than using raw
    strings.
    """

    CLIENT = "active"  # sends ClientHello; a=setup:active
    SERVER = "passive"  # waits for ClientHello; a=setup:passive


class SrtpProfile(enum.Enum):
    """DTLS-SRTP protection profile (RFC 5764 §4.1.2).

    Only the two AES-CM-128 profiles are in-scope for MVP (ADR-0016 §6).
    """

    AES128_CM_HMAC_SHA1_80 = "SRTP_AES128_CM_SHA1_80"
    AES128_CM_HMAC_SHA1_32 = "SRTP_AES128_CM_SHA1_32"


# ---------------------------------------------------------------------------
# Narrow Protocols over the optional ``pyOpenSSL`` / ``OpenSSL`` extra.
#
# pyOpenSSL ships no ``py.typed`` and no bundled stubs.  The same strategy as
# srtp.py (narrow Protocols over importlib-loaded modules) gives full mypy
# coverage in the default gate (no pyOpenSSL) and in the webrtc gate alike,
# with zero ``# type: ignore`` escape hatches.
# ---------------------------------------------------------------------------


class _PKey(Protocol):
    """Narrow surface of ``OpenSSL.crypto.PKey`` used here."""

    def generate_key(self, key_type: int, bits: int) -> None:
        """Generate a keypair of the given *key_type* and *bits* size."""
        ...


class _X509(Protocol):
    """Narrow surface of ``OpenSSL.crypto.X509`` used here."""

    def get_subject(self) -> _X509Name:
        """Return the subject X509Name object."""
        ...

    def set_serial_number(self, serial: int) -> None:
        """Set the certificate serial number."""
        ...

    def gmtime_adj_notBefore(self, amount: int) -> None:  # noqa: N802 — pyOpenSSL API
        """Adjust the notBefore field by *amount* seconds from now (GMT)."""
        ...

    def gmtime_adj_notAfter(self, amount: int) -> None:  # noqa: N802 — pyOpenSSL API
        """Adjust the notAfter field by *amount* seconds from now (GMT)."""
        ...

    def set_issuer(self, issuer: _X509Name) -> None:
        """Set the certificate issuer."""
        ...

    def set_pubkey(self, pkey: _PKey) -> None:
        """Set the certificate public key."""
        ...

    def sign(self, pkey: _PKey, digest: str) -> None:
        """Sign the certificate with *pkey* using *digest*."""
        ...


class _X509Name(Protocol):
    """Narrow surface of ``OpenSSL.crypto.X509Name`` used here."""

    CN: str


class _SSLContext(Protocol):
    """Narrow surface of ``OpenSSL.SSL.Context`` used here."""

    def set_tlsext_use_srtp(self, profiles: bytes) -> None:
        """Advertise the DTLS-SRTP extension with the given profile list."""
        ...

    def use_certificate(self, cert: _X509) -> None:
        """Set the certificate for this context."""
        ...

    def use_privatekey(self, pkey: _PKey) -> None:
        """Set the private key for this context."""
        ...

    def check_privatekey(self) -> None:
        """Verify that the private key matches the certificate."""
        ...

    def set_verify(self, mode: int, callback: _VerifyCallback) -> None:
        """Set the peer-certificate verification mode."""
        ...

    def set_cipher_list(self, ciphers: bytes) -> None:
        """Restrict the cipher suites offered/accepted to the given list."""
        ...


class _VerifyCallback(Protocol):
    """Type signature for the pyOpenSSL verify callback."""

    def __call__(
        self,
        conn: _SSLConnection,
        cert: _X509,
        errnum: int,
        depth: int,
        ok: bool,
    ) -> bool:
        """Return True to accept the peer certificate."""
        ...


class _SSLConnection(Protocol):
    """Narrow surface of ``OpenSSL.SSL.Connection`` used here."""

    def set_accept_state(self) -> None:
        """Set the connection to server (accept) mode."""
        ...

    def set_connect_state(self) -> None:
        """Set the connection to client (connect) mode."""
        ...

    def do_handshake(self) -> None:
        """Perform or continue the DTLS handshake.

        Raises ``SSL.WantReadError`` when the handshake needs more inbound data.
        Returns without raising once the handshake is complete.
        """
        ...

    def bio_write(self, data: bytes) -> int:
        """Feed *data* into the memory BIO for the DTLS state machine to read."""
        ...

    def bio_read(self, bufsiz: int) -> bytes:
        """Read bytes the DTLS state machine wants to send.

        Raises ``SSL.Error`` when the outbound BIO is empty.
        """
        ...

    def get_peer_certificate(self) -> _X509 | None:
        """Return the peer's certificate after the handshake, or None."""
        ...

    def export_keying_material(
        self, label: bytes, olen: int, context: bytes | None
    ) -> bytes:
        """Export keying material via the RFC 5705 exporter."""
        ...

    def get_selected_srtp_profile(self) -> bytes:
        """Return the negotiated DTLS-SRTP profile name as bytes."""
        ...


# Constructor Protocols so we can assign importlib module attributes to typed vars.


class _ContextCtor(Protocol):
    """``OpenSSL.SSL.Context(method) -> _SSLContext``."""

    def __call__(self, method: int) -> _SSLContext:
        """Construct an SSL/DTLS context for the given protocol method."""
        ...


class _ConnectionCtor(Protocol):
    """``OpenSSL.SSL.Connection(ctx, sock_or_None) -> _SSLConnection``."""

    def __call__(self, context: _SSLContext, sock: None) -> _SSLConnection:
        """Construct a memory-BIO DTLS connection."""
        ...


class _PKeyCtor(Protocol):
    """``OpenSSL.crypto.PKey() -> _PKey``."""

    def __call__(self) -> _PKey:
        """Construct a new PKey."""
        ...


class _X509Ctor(Protocol):
    """``OpenSSL.crypto.X509() -> _X509``."""

    def __call__(self) -> _X509:
        """Construct a new X509 certificate."""
        ...


class _DumpCertFn(Protocol):
    """``OpenSSL.crypto.dump_certificate(type, cert) -> bytes``."""

    def __call__(self, cert_type: int, cert: _X509) -> bytes:
        """Serialise *cert* to DER (FILETYPE_ASN1) bytes."""
        ...


# ---------------------------------------------------------------------------
# pyOpenSSL lazy-import singleton
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PyOpenSSLImpl:
    """Runtime adapter for the lazy-loaded pyOpenSSL modules.

    Constructed once on first :class:`DtlsEndpoint` use.  All referenced
    constants and constructors are narrowly typed so mypy can verify call
    sites without escape hatches.
    """

    # SSL method constants.
    DTLS_CLIENT_METHOD: int
    DTLS_SERVER_METHOD: int
    VERIFY_PEER: int
    SSL_ERROR_CLASS: type[Exception]
    WANT_READ_ERROR_CLASS: type[Exception]

    # Typed constructors.
    Context: _ContextCtor = field(repr=False)
    Connection: _ConnectionCtor = field(repr=False)
    PKey: _PKeyCtor = field(repr=False)
    X509: _X509Ctor = field(repr=False)
    dump_certificate: _DumpCertFn = field(repr=False)

    # Crypto type constants.
    TYPE_RSA: int = field(default=0)
    FILETYPE_ASN1: int = field(default=0)

    @classmethod
    def load(cls) -> _PyOpenSSLImpl:
        """Import pyOpenSSL and build a ``_PyOpenSSLImpl``.

        Raises:
            ImportError: If the ``webrtc`` extra (``pyOpenSSL``) is not installed.
        """
        try:
            ssl_mod = importlib.import_module("OpenSSL.SSL")
            crypto_mod = importlib.import_module("OpenSSL.crypto")
        except ModuleNotFoundError as exc:
            msg = (
                "hermes_voip.media.dtls requires the 'webrtc' optional extra: "
                "install with `uv sync --extra webrtc` "
                "or `pip install hermes-voip[webrtc]`"
            )
            raise ImportError(msg) from exc

        return cls(
            DTLS_CLIENT_METHOD=ssl_mod.DTLS_CLIENT_METHOD,
            DTLS_SERVER_METHOD=ssl_mod.DTLS_SERVER_METHOD,
            VERIFY_PEER=ssl_mod.VERIFY_PEER,
            SSL_ERROR_CLASS=ssl_mod.Error,
            WANT_READ_ERROR_CLASS=ssl_mod.WantReadError,
            Context=ssl_mod.Context,
            Connection=ssl_mod.Connection,
            PKey=crypto_mod.PKey,
            X509=crypto_mod.X509,
            dump_certificate=crypto_mod.dump_certificate,
            TYPE_RSA=crypto_mod.TYPE_RSA,
            FILETYPE_ASN1=crypto_mod.FILETYPE_ASN1,
        )


# The pyOpenSSL backend is a build-once lazy singleton (ADR-0046): Hermes runs the
# agent generation on an uncapped thread pool and forks background workers, so the
# getter can be entered concurrently. :class:`LazySingleton` loads it at most once
# via the documented ``plugins.plugin_utils.lazy_singleton`` helper when the runtime
# provides it, with a behaviourally-identical stdlib double-checked-lock fallback in
# the default (no-hermes) environment.
_OPENSSL_SINGLETON: LazySingleton[_PyOpenSSLImpl] = LazySingleton(_PyOpenSSLImpl.load)


def _get_openssl() -> _PyOpenSSLImpl:
    """Return the singleton :class:`_PyOpenSSLImpl`, loading pyOpenSSL on first call.

    Raises:
        ImportError: If the ``webrtc`` extra is not installed.
    """
    return _OPENSSL_SINGLETON.get()


def _reset_openssl_singleton() -> None:
    """Drop the cached pyOpenSSL backend so the next :func:`_get_openssl` rebuilds it.

    Test-isolation seam (ADR-0046): a test that needs a fresh load calls this so a
    later test sees a clean singleton.
    """
    _OPENSSL_SINGLETON.reset()


# ---------------------------------------------------------------------------
# Certificate helpers
# ---------------------------------------------------------------------------


def _generate_self_signed(ossl: _PyOpenSSLImpl) -> tuple[_PKey, _X509]:
    """Generate an ephemeral self-signed RSA-2048 certificate.

    The certificate is valid for 30 days and carries a minimal subject.
    It is generated ephemerally at :class:`DtlsEndpoint` construction; nothing
    is persisted to disk.

    Args:
        ossl: The loaded :class:`_PyOpenSSLImpl` singleton.

    Returns:
        ``(pkey, cert)`` — the RSA private key and the signed X.509 certificate.
    """
    pkey: _PKey = ossl.PKey()
    pkey.generate_key(ossl.TYPE_RSA, 2048)

    cert: _X509 = ossl.X509()
    cert.get_subject().CN = "hermes-voip"
    # Use a time-based serial to avoid collisions between endpoints.
    cert.set_serial_number(int(time.monotonic_ns()))
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(30 * 24 * 60 * 60)  # 30 days
    cert.set_issuer(cert.get_subject())
    cert.set_pubkey(pkey)
    cert.sign(pkey, "sha256")

    return pkey, cert


def _cert_fingerprint(ossl: _PyOpenSSLImpl, cert: _X509) -> str:
    """Compute the SHA-256 fingerprint of *cert* in SDP ``a=fingerprint`` format.

    The fingerprint is the SHA-256 digest of the DER-encoded certificate
    (RFC 4572 §5), formatted as ``sha-256 XX:XX:XX:…`` with uppercase hex
    pairs separated by colons.

    Args:
        ossl: The loaded :class:`_PyOpenSSLImpl` singleton.
        cert: The X.509 certificate to fingerprint.

    Returns:
        A string of the form ``"sha-256 XX:XX:XX:…"`` (32 uppercase hex pairs).
    """
    der: bytes = ossl.dump_certificate(ossl.FILETYPE_ASN1, cert)
    digest: str = hashlib.sha256(der).hexdigest().upper()
    hex_pairs: str = ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
    return f"sha-256 {hex_pairs}"


# ---------------------------------------------------------------------------
# DtlsEndpoint
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DtlsEndpoint:
    """Memory-BIO DTLS endpoint for DTLS-SRTP keying (RFC 5763/5764, ADR-0016).

    Each endpoint owns one ephemeral self-signed certificate (generated at
    construction) and one DTLS SSL connection over a memory BIO.  DTLS
    datagrams are exchanged by the caller:

    - Call :meth:`get_outbound_datagrams` to retrieve datagrams the state
      machine wants to send (and to initiate the CLIENT's first ClientHello).
    - Call :meth:`feed` to deliver inbound datagrams from the peer.

    After the handshake is complete (:meth:`handshake_done` returns ``True``),
    call :meth:`verify_peer_fingerprint` with the peer's SDP ``a=fingerprint``
    value, THEN :meth:`derive_srtp_sessions` to get a ``(inbound, outbound)``
    :class:`~hermes_voip.media.srtp.SrtpSession` pair.  ``derive_srtp_sessions``
    raises :class:`RuntimeError` if fingerprint verification has not succeeded
    (RFC 5763 §5).  A fatal DTLS alert during the handshake re-raises from
    :meth:`feed` / :meth:`get_outbound_datagrams` and sets
    :meth:`handshake_failed`.

    **One DtlsEndpoint per call direction.** Construct a fresh endpoint for
    each call; do not re-use an endpoint across calls.

    Raises:
        ImportError: At construction time if the ``webrtc`` extra is not
            installed (pyOpenSSL absent).
    """

    role: DtlsRole
    #: Optional OpenSSL cipher list (RFC 4346 cipher string) restricting the DTLS
    #: ciphers offered/accepted.  ``None`` (the default) leaves the OpenSSL default
    #: list in place.  Set to e.g. ``b"ECDHE-RSA-AES128-GCM-SHA256"`` to pin AEAD
    #: ciphers as a security-hardening policy.
    cipher_list: bytes | None = None
    _ossl: _PyOpenSSLImpl = field(init=False, repr=False)
    _pkey: _PKey = field(init=False, repr=False)
    _cert: _X509 = field(init=False, repr=False)
    _conn: _SSLConnection = field(init=False, repr=False)
    _fp: str = field(init=False, repr=False)
    _done: bool = field(default=False, init=False, repr=False)
    _failed: bool = field(default=False, init=False, repr=False)
    _fatal_error: Exception | None = field(default=None, init=False, repr=False)
    _fingerprint_verified: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialise the DTLS connection and ephemeral certificate."""
        ossl = _get_openssl()
        self._ossl = ossl

        pkey, cert = _generate_self_signed(ossl)
        self._pkey = pkey
        self._cert = cert
        self._fp = _cert_fingerprint(ossl, cert)

        method = (
            ossl.DTLS_CLIENT_METHOD
            if self.role is DtlsRole.CLIENT
            else ossl.DTLS_SERVER_METHOD
        )
        ctx: _SSLContext = ossl.Context(method)
        ctx.set_tlsext_use_srtp(_SRTP_PROFILES)
        if self.cipher_list is not None:
            ctx.set_cipher_list(self.cipher_list)
        ctx.use_certificate(cert)
        ctx.use_privatekey(pkey)
        ctx.check_privatekey()
        # RFC 5763 §5: both sides MUST present and verify each other's certificate
        # fingerprint.  VERIFY_PEER makes the server request a client certificate
        # (mutual auth over memory BIO) so get_peer_certificate() returns the peer
        # cert on BOTH sides after the handshake.  The callback always returns True
        # (skip PKI chain validation); fingerprint comparison is done separately via
        # verify_peer_fingerprint().
        ctx.set_verify(ossl.VERIFY_PEER, _accept_any_cert)

        conn: _SSLConnection = ossl.Connection(ctx, None)  # memory BIO
        if self.role is DtlsRole.CLIENT:
            conn.set_connect_state()
        else:
            conn.set_accept_state()
        self._conn = conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fingerprint(self) -> str:
        """Return the SDP ``a=fingerprint`` value for our certificate (RFC 4572 §5).

        Format: ``"sha-256 XX:XX:XX:…"`` (32 uppercase hex pairs joined by colons).

        The fingerprint is stable for the lifetime of this endpoint — the
        certificate is generated once at construction.
        """
        return self._fp

    def feed(self, datagram: bytes) -> None:
        """Feed an inbound DTLS datagram into the state machine.

        The caller must demux DTLS datagrams (first byte 20-63 per RFC 7983)
        from STUN and SRTP/SRTCP before passing them here.

        After calling :meth:`feed`, call :meth:`get_outbound_datagrams` to
        retrieve any response datagrams the handshake produced.

        Args:
            datagram: A single DTLS datagram from the peer.

        Raises:
            OpenSSL.SSL.Error: If the handshake has already failed fatally — the
                stored error is re-raised before touching the dead connection.
        """
        if self._fatal_error is not None:
            raise self._fatal_error
        self._conn.bio_write(datagram)
        self._step_handshake()

    def get_outbound_datagrams(self) -> list[bytes]:
        """Return datagrams the DTLS state machine wants to send, draining the BIO.

        On the CLIENT side this also steps the handshake on the first call,
        producing the initial ClientHello.  Call after :meth:`feed` to send any
        responses the handshake generated.

        Returns:
            A list of raw datagram bytes to transmit to the peer (may be empty).
        """
        self._step_handshake()
        return self._drain_outbound()

    def handshake_done(self) -> bool:
        """Return ``True`` once the DTLS handshake has completed successfully."""
        return self._done

    def handshake_failed(self) -> bool:
        """Return ``True`` if the DTLS handshake encountered a fatal TLS alert.

        A fatal alert (e.g. ``certificate_unknown``, ``protocol_version``, or
        ``handshake_failure``) terminates the handshake permanently.  Once this
        returns ``True``, neither :meth:`feed` nor :meth:`get_outbound_datagrams`
        will make progress; the endpoint must be discarded.

        Note that ``_failed`` and ``_done`` are mutually exclusive: a successful
        handshake sets ``_done``; a fatal error sets ``_failed``.
        """
        return self._failed

    def selected_profile(self) -> SrtpProfile:
        """Return the negotiated DTLS-SRTP profile.

        Must be called after the handshake is complete.

        Raises:
            RuntimeError: If called before the handshake completes, or if the
                negotiated profile is not one of the supported suites.
            ImportError: If the ``webrtc`` extra is not installed.
        """
        if not self._done:
            msg = "DTLS handshake has not completed; call after handshake_done()"
            raise RuntimeError(msg)
        raw: bytes = self._conn.get_selected_srtp_profile()
        try:
            return SrtpProfile(raw.decode())
        except ValueError:
            msg = f"unsupported DTLS-SRTP profile negotiated: {raw!r}"
            raise RuntimeError(msg)  # noqa: B904 — no cause needed (own error)

    def export_srtp_keying_material(self) -> bytes:
        """Export the RFC 5705 keying material (label ``EXTRACTOR-dtls_srtp``).

        Returns the raw 60-byte block (for ``SRTP_AES128_CM_SHA1_80``):
        ``client_write_key(16) || server_write_key(16) ||
        client_write_salt(14) || server_write_salt(14)``

        Both ends of the handshake export an IDENTICAL block (RFC 5705 §4).
        The caller slices it per role using :meth:`derive_srtp_sessions`.

        Key material is never logged or repr'd.

        Raises:
            RuntimeError: If called before the handshake completes.
        """
        if not self._done:
            msg = (
                "DTLS handshake has not completed; "
                "call export_srtp_keying_material() after handshake_done()"
            )
            raise RuntimeError(msg)
        return self._conn.export_keying_material(_EXPORTER_LABEL, _EXPORT_LEN, None)

    def derive_srtp_sessions(self) -> tuple[SrtpSession, SrtpSession]:
        """Derive inbound and outbound :class:`SrtpSession` pairs from the handshake.

        Slices the RFC 5764 §4.2 key block and constructs two SRTP sessions:

        - ``inbound``:  decrypt packets received from the peer.
        - ``outbound``: encrypt packets sent to the peer.

        For a DTLS CLIENT (``a=setup:active``), the client uses the
        ``client_write_key/salt`` for outbound and ``server_write_key/salt``
        for inbound.  For a DTLS SERVER (``a=setup:passive``), the roles
        are mirrored.

        Returns:
            ``(inbound, outbound)`` — a pair of :class:`SrtpSession` instances
            ready for :meth:`~SrtpSession.unprotect` / :meth:`~SrtpSession.protect`.

        Raises:
            RuntimeError: If called before the handshake completes, or if
                :meth:`verify_peer_fingerprint` has not been called first
                (RFC 5763 §5 requires fingerprint verification before keying).
        """
        if not self._fingerprint_verified:
            msg = (
                "verify_peer_fingerprint() must be called before derive_srtp_sessions()"
                " — RFC 5763 §5 requires fingerprint verification before keying"
            )
            raise RuntimeError(msg)
        material: bytes = self.export_srtp_keying_material()
        suite: str = _PROFILE_TO_SUITE.get(
            self._conn.get_selected_srtp_profile(), _SUITE_80
        )

        # RFC 5764 §4.2 layout:
        # offset 0   client_write_key  (16 bytes)
        # offset 16  server_write_key  (16 bytes)
        # offset 32  client_write_salt (14 bytes)
        # offset 46  server_write_salt (14 bytes)
        cwk: bytes = material[0:_SRTP_KEY_LEN]
        swk: bytes = material[_SRTP_KEY_LEN : 2 * _SRTP_KEY_LEN]
        cws: bytes = material[2 * _SRTP_KEY_LEN : 2 * _SRTP_KEY_LEN + _SRTP_SALT_LEN]
        sws: bytes = material[2 * _SRTP_KEY_LEN + _SRTP_SALT_LEN : _EXPORT_LEN]

        if self.role is DtlsRole.CLIENT:
            # CLIENT outbound → client_write; CLIENT inbound → server_write
            outbound = SrtpSession.from_raw_keys(cwk, cws, suite=suite)
            inbound = SrtpSession.from_raw_keys(swk, sws, suite=suite)
        else:
            # SERVER outbound → server_write; SERVER inbound → client_write
            outbound = SrtpSession.from_raw_keys(swk, sws, suite=suite)
            inbound = SrtpSession.from_raw_keys(cwk, cws, suite=suite)

        return inbound, outbound

    def derive_srtcp_sessions(self) -> tuple[SrtcpSession, SrtcpSession]:
        """Derive inbound + outbound :class:`SrtcpSession` pairs (RFC 3711 §3.4).

        Secured-path RTCP rides SRTCP, keyed from the IDENTICAL RFC 5764 §4.2 export
        that keys SRTP (:meth:`derive_srtp_sessions`) — only the KDF labels differ
        (0x03/0x04/0x05 vs 0x00/0x01/0x02), so the SRTCP keystream is independent of the
        SRTP keystream derived from the same write key/salt. The role→direction mapping
        is identical to SRTP (CLIENT uses client_write outbound / server_write inbound;
        SERVER mirrors), so the SRTP and SRTCP pairs agree on which master material is
        ours vs the peer's. The adapter wires the returned pair onto the engine's
        ``srtcp_inbound``/``srtcp_outbound`` alongside the SRTP pair (ADR-0066).

        Returns:
            ``(inbound, outbound)`` — a pair of :class:`SrtcpSession` instances ready
            for :meth:`~SrtcpSession.unprotect` / :meth:`~SrtcpSession.protect`.

        Raises:
            RuntimeError: If called before the handshake completes, or before
                :meth:`verify_peer_fingerprint` (RFC 5763 §5 — fingerprint verification
                precedes keying), mirroring :meth:`derive_srtp_sessions`.
        """
        if not self._fingerprint_verified:
            msg = (
                "verify_peer_fingerprint() must be called before "
                "derive_srtcp_sessions() — RFC 5763 §5 requires fingerprint "
                "verification before keying"
            )
            raise RuntimeError(msg)
        material: bytes = self.export_srtp_keying_material()
        suite: str = _PROFILE_TO_SUITE.get(
            self._conn.get_selected_srtp_profile(), _SUITE_80
        )
        # RFC 5764 §4.2 layout (identical to derive_srtp_sessions).
        cwk: bytes = material[0:_SRTP_KEY_LEN]
        swk: bytes = material[_SRTP_KEY_LEN : 2 * _SRTP_KEY_LEN]
        cws: bytes = material[2 * _SRTP_KEY_LEN : 2 * _SRTP_KEY_LEN + _SRTP_SALT_LEN]
        sws: bytes = material[2 * _SRTP_KEY_LEN + _SRTP_SALT_LEN : _EXPORT_LEN]

        if self.role is DtlsRole.CLIENT:
            outbound = SrtcpSession.from_raw_keys(cwk, cws, suite=suite)
            inbound = SrtcpSession.from_raw_keys(swk, sws, suite=suite)
        else:
            outbound = SrtcpSession.from_raw_keys(swk, sws, suite=suite)
            inbound = SrtcpSession.from_raw_keys(cwk, cws, suite=suite)

        return inbound, outbound

    def derive_outbound_srtp_session(self, *, ssrc: int) -> SrtpSession:
        """Derive an ADDITIONAL outbound SRTP session bound to ``ssrc`` (ADR-0044).

        WebRTC BUNDLE multiplexes several RTP streams (audio + video) onto one
        DTLS handshake but distinct SSRCs; an :class:`SrtpSession` is bound to a
        single SSRC (one ROC + replay state). So the video stream needs its OWN
        outbound session, keyed from the **same** RFC 5764 export as audio but
        pre-bound to the video SSRC. The per-packet IV is ``salt ^ ssrc ^ index``
        (RFC 3711 §4.1.1), so distinct SSRCs yield distinct keystreams safely.

        Args:
            ssrc: The 32-bit SSRC to bind the new outbound session to.

        Returns:
            A new outbound :class:`SrtpSession` pre-bound to ``ssrc``.

        Raises:
            RuntimeError: If called before the handshake completes / before
                :meth:`verify_peer_fingerprint` (same precondition as
                :meth:`derive_srtp_sessions`).
        """
        if not self._fingerprint_verified:
            msg = (
                "verify_peer_fingerprint() must be called before "
                "derive_outbound_srtp_session() — RFC 5763 §5"
            )
            raise RuntimeError(msg)
        material: bytes = self.export_srtp_keying_material()
        suite: str = _PROFILE_TO_SUITE.get(
            self._conn.get_selected_srtp_profile(), _SUITE_80
        )
        cwk: bytes = material[0:_SRTP_KEY_LEN]
        swk: bytes = material[_SRTP_KEY_LEN : 2 * _SRTP_KEY_LEN]
        cws: bytes = material[2 * _SRTP_KEY_LEN : 2 * _SRTP_KEY_LEN + _SRTP_SALT_LEN]
        sws: bytes = material[2 * _SRTP_KEY_LEN + _SRTP_SALT_LEN : _EXPORT_LEN]
        # Outbound uses the local write key: client_write for a CLIENT, else
        # server_write (mirrors derive_srtp_sessions).
        if self.role is DtlsRole.CLIENT:
            return SrtpSession.from_raw_keys(cwk, cws, suite=suite, ssrc=ssrc)
        return SrtpSession.from_raw_keys(swk, sws, suite=suite, ssrc=ssrc)

    def verify_peer_fingerprint(self, expected_fingerprint: str) -> None:
        """Verify the peer's certificate against the SDP ``a=fingerprint`` value.

        Must be called after the handshake completes and BEFORE deriving SRTP
        sessions.  A mismatch means the peer is not who the SDP claims — abort
        the call.

        Args:
            expected_fingerprint: The ``a=fingerprint:sha-256 XX:XX:…`` value
                from the peer's SDP (case-insensitive hex, with or without the
                ``sha-256 `` prefix).

        Raises:
            RuntimeError: If no peer certificate is available (handshake not
                done, or anonymous peer).
            ValueError: If the fingerprint does not match.
        """
        peer_cert: _X509 | None = self._conn.get_peer_certificate()
        if peer_cert is None:
            msg = "no peer certificate — DTLS handshake may not be complete"
            raise RuntimeError(msg)
        actual: str = _cert_fingerprint(self._ossl, peer_cert)

        # Normalise: strip optional prefix, uppercase, compare.
        def _normalise(fp: str) -> str:
            if fp.lower().startswith("sha-256 "):
                fp = fp[len("sha-256 ") :]
            return fp.upper().replace(" ", "")

        if _normalise(actual) != _normalise(expected_fingerprint):
            msg = "DTLS peer certificate fingerprint mismatch — call rejected"
            raise ValueError(msg)
        self._fingerprint_verified = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _step_handshake(self) -> None:
        """Advance the DTLS handshake state machine by one step.

        Calls ``do_handshake()`` once; catches only ``SSL.WantReadError`` (the
        normal "need more inbound data" signal in a memory-BIO DTLS session).
        Any other ``SSL.Error`` subclass is a fatal TLS alert; the endpoint sets
        ``_failed = True``, stores the exception, and re-raises so the caller sees
        the failure immediately (AGENTS.md rule 37 — errors propagate, never
        swallowed).  Once failed, every subsequent call re-raises the SAME stored
        error rather than silently no-opping — a failed endpoint keeps surfacing
        its failure deterministically.

        On successful completion (no exception), sets ``_done = True``.
        """
        if self._done:
            return
        if self._fatal_error is not None:
            # Re-surface the original fatal error on every subsequent call so a
            # failed endpoint never silently no-ops (rule 37).  Checking
            # _fatal_error (not _failed) also narrows the type for the raise.
            raise self._fatal_error
        ossl = self._ossl
        try:
            self._conn.do_handshake()
            self._done = True
        except ossl.WANT_READ_ERROR_CLASS:
            # WantReadError is the normal "need more inbound data" signal —
            # the handshake is still in progress.  Drain the outbound BIO on
            # the next get_outbound_datagrams call.
            pass
        except ossl.SSL_ERROR_CLASS as exc:
            # Any SSL.Error that is NOT WantReadError is a fatal TLS alert
            # (e.g. certificate_unknown, handshake_failure, protocol_version,
            # or "no shared cipher").  The handshake is permanently broken;
            # store the error, mark failed, and re-raise.
            self._failed = True
            self._fatal_error = exc
            raise

    def _drain_outbound(self) -> list[bytes]:
        """Read and return all pending outbound DTLS datagrams from the memory BIO.

        ``bio_read`` raises ``SSL.Error`` when the BIO is empty; that is the
        normal termination condition, not an error.

        Returns:
            List of datagram bytes (empty list if nothing is pending).
        """
        datagrams: list[bytes] = []
        ossl = self._ossl
        while True:
            try:
                chunk: bytes = self._conn.bio_read(65536)
                datagrams.append(chunk)
            except ossl.SSL_ERROR_CLASS:
                break
        return datagrams


# ---------------------------------------------------------------------------
# Verify callback (module-level so it is picklable and has a stable reference)
# ---------------------------------------------------------------------------


def _accept_any_cert(
    conn: _SSLConnection,  # noqa: ARG001 — required by pyOpenSSL callback API
    cert: _X509,  # noqa: ARG001 — required by pyOpenSSL callback API
    errnum: int,  # noqa: ARG001 — required by pyOpenSSL callback API
    depth: int,  # noqa: ARG001 — required by pyOpenSSL callback API
    ok: bool,  # noqa: ARG001 — required by pyOpenSSL callback API
) -> bool:
    """Accept any peer certificate; fingerprint verification is done separately.

    RFC 5763 §5 mandates that the peer's certificate fingerprint be verified
    against the SDP ``a=fingerprint`` attribute after the handshake.  This
    callback defers that check to :meth:`DtlsEndpoint.verify_peer_fingerprint`
    so that the DTLS handshake completes even for self-signed certs (which is
    the normal case for WebRTC peers).

    Returns:
        Always ``True`` — fingerprint verification is the caller's responsibility.
    """
    return True
