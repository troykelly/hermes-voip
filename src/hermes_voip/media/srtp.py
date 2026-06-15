"""SDES-SRTP packet transform (RFC 3711) — AES-CM + HMAC-SHA1 (ADR-0013).

This module implements the two SRTP suites negotiated by SDES (RFC 4568):
``AES_CM_128_HMAC_SHA1_80`` and ``AES_CM_128_HMAC_SHA1_32``.

It is the *payload-level* transform only: the caller (the media transport)
owns the UDP socket, RTP packetisation (:class:`~hermes_voip.rtp.RtpPacket`),
and SDES key exchange; this module owns the key derivation, per-packet
encryption, authentication, replay protection, and ROC management.

**Dependency gating**: ``cryptography`` is an *optional* dependency declared
in the ``media`` extra (``pyproject.toml``).  The module-level import is
deferred via :func:`importlib.import_module` so that
``import hermes_voip.media.srtp`` succeeds in the default install.
:class:`SrtpSession` raises :class:`ImportError` at *construction* time when
``cryptography`` is absent — the error propagates naturally (AGENTS.md rule 37).

**Security invariants**:
- Key material (master key, session keys, salt) never appears in :func:`repr`,
  log messages, or exception text (only structural facts are reported).
- Auth-tag verification uses :func:`hmac.compare_digest` (constant-time).
- No hand-rolled AES; all block-cipher work goes through ``cryptography``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import struct
from dataclasses import dataclass, field
from typing import Protocol

from hermes_voip.rtp import RtpPacket
from hermes_voip.sdp import CryptoAttribute

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AES_CM_128_HMAC_SHA1_80 = "AES_CM_128_HMAC_SHA1_80"
_AES_CM_128_HMAC_SHA1_32 = "AES_CM_128_HMAC_SHA1_32"

# Auth-tag lengths in bytes (suite → bytes).
_AUTH_TAG_LEN: dict[str, int] = {
    _AES_CM_128_HMAC_SHA1_80: 10,  # 80 bits
    _AES_CM_128_HMAC_SHA1_32: 4,  # 32 bits
}

# SRTP session key lengths (RFC 3711 §4.3, Table 4).
_SESSION_ENCRYPT_KEY_LEN = 16  # 128 bits (AES-128)
_SESSION_AUTH_KEY_LEN = 20  # 160 bits (HMAC-SHA1)
_SESSION_SALT_LEN = 14  # 112 bits

# Master key/salt lengths (RFC 4568 §6.2, both supported suites share 30 bytes).
_MASTER_KEY_LEN = 16  # 128 bits
_MASTER_SALT_LEN = 14  # 112 bits

# KDF labels (RFC 3711 §4.3.1, Table 4).
_KDF_LABEL_CIPHER: int = 0x00
_KDF_LABEL_AUTH: int = 0x01
_KDF_LABEL_SALT: int = 0x02

# AES block size in bytes.
_AES_BLOCK = 16

# Replay window size (number of packets, must be a power of 2 for the bitmask).
_REPLAY_WINDOW = 64
_REPLAY_WINDOW_MASK = (1 << _REPLAY_WINDOW) - 1

# RTP fixed header size (V/P/X/CC/M/PT, seq, timestamp, SSRC), before any CSRC
# list or header extension (RFC 3550 §5.1).
_RTP_HEADER_LEN = 12

# RTP header-extension preamble length: 2-byte profile + 2-byte length-in-words.
_RTP_EXT_HEADER_LEN = 4

# One CSRC identifier / one extension word is 4 octets (RFC 3550 §5.1, §5.3.1).
_RTP_WORD = 4

# RTP first-byte bit masks (RFC 3550 §5.1).
_RTP_CSRC_COUNT_MASK = 0x0F  # CC: number of CSRC identifiers after the header
_RTP_EXTENSION_BIT = 0x10  # X: a header extension follows the CSRC list

# Minimum raw SRTP byte length: 12-byte RTP header + 4-byte minimum auth tag.
_MIN_SRTP_LEN = _RTP_HEADER_LEN + 4

# Highest 16-bit sequence number (wrap sentinel for ROC update).
_SEQ_MAX = 0xFFFF

# 16-bit sequence-number half-range for ROC estimation.
_SEQ_HALF: int = 1 << 15

# Inline key prefix (RFC 4568 §6.1).
_INLINE_PREFIX = "inline:"

# ROC modulus (32-bit unsigned).
_ROC_MOD: int = 2**32

# Label position shift: label * 2^64 in the 128-bit KDF IV.
# Verified against RFC 3711 Appendix B.3 test vectors.
_KDF_LABEL_SHIFT = 64


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class SrtpError(ValueError):
    """Raised when SRTP protect/unprotect fails (auth, replay, or format error)."""


# ---------------------------------------------------------------------------
# Narrow Protocol surface over the optional ``cryptography`` extra.
#
# ``cryptography`` is in the optional ``media`` extra, absent in the default
# install and its mypy gate.  Mirroring the ``ml``-extra lanes
# (``hermes_voip.media.vad`` / ``hermes_voip.stt.sherpa_onnx``): rather than
# suppress the resulting untyped-import errors with an escape hatch (AGENTS.md
# rules 17/39 ban them), we declare *narrow local Protocols* covering only the
# constructors and methods the keystream path uses.  A module attribute resolved
# via ``importlib.import_module`` is a ``ModuleType`` whose attribute access
# yields ``Any``; ``Any`` is assignable to these structural callables with NO
# cast, and every subsequent call is then fully type-checked — clean in BOTH the
# no-media gate and the media env, with zero ``# type: ignore``.
# ---------------------------------------------------------------------------


class _CipherContext(Protocol):
    """The subset of cryptography's ``CipherContext`` used here."""

    def update(self, data: bytes) -> bytes:
        """Feed plaintext (here: zero bytes) and return the keystream chunk."""
        ...

    def finalize(self) -> bytes:
        """Finalise the cipher and return any trailing keystream bytes."""
        ...


class _CipherHandle(Protocol):
    """The subset of cryptography's ``Cipher`` object used here."""

    def encryptor(self) -> _CipherContext:
        """Return an encryptor context for this cipher."""
        ...


class _Algorithm(Protocol):
    """Opaque handle for an ``algorithms.AES`` instance (passed to ``Cipher``)."""


class _Mode(Protocol):
    """Opaque handle for a ``modes.CTR`` instance (passed to ``Cipher``)."""


class _AesCtor(Protocol):
    """The ``algorithms.AES`` constructor surface (``AES(key) -> algorithm``)."""

    def __call__(self, key: bytes) -> _Algorithm:
        """Construct an AES algorithm bound to ``key``."""
        ...


class _CtrCtor(Protocol):
    """The ``modes.CTR`` constructor surface (``CTR(nonce) -> mode``)."""

    def __call__(self, nonce: bytes) -> _Mode:
        """Construct a CTR mode with initial counter ``nonce``."""
        ...


class _CipherCtor(Protocol):
    """The ``ciphers.Cipher`` constructor surface (``Cipher(algo, mode)``)."""

    def __call__(self, algorithm: _Algorithm, mode: _Mode) -> _CipherHandle:
        """Construct a cipher from an algorithm and a mode."""
        ...


class _CryptographyImpl:
    """Runtime adapter that wraps importlib-loaded cryptography modules.

    The three constructors are bound to the narrow ``_AesCtor`` / ``_CtrCtor`` /
    ``_CipherCtor`` Protocols.  Assigning the ``Any``-typed module attributes to
    these structural callables needs no cast, so :meth:`make_cipher` returns a
    fully-typed ``_CipherHandle`` with no escape hatch.
    """

    def __init__(self) -> None:
        try:
            ciphers_mod = importlib.import_module(
                "cryptography.hazmat.primitives.ciphers"
            )
            algorithms_mod = importlib.import_module(
                "cryptography.hazmat.primitives.ciphers.algorithms"
            )
            modes_mod = importlib.import_module(
                "cryptography.hazmat.primitives.ciphers.modes"
            )
        except ModuleNotFoundError as exc:
            msg = (
                "hermes_voip.media.srtp requires the 'media' optional extra: "
                "install with `uv sync --extra media` "
                "or `pip install hermes-voip[media]`"
            )
            raise ImportError(msg) from exc
        self._cipher: _CipherCtor = ciphers_mod.Cipher
        self._aes: _AesCtor = algorithms_mod.AES
        self._ctr: _CtrCtor = modes_mod.CTR

    def make_cipher(self, key: bytes, iv: bytes) -> _CipherHandle:
        """Build an AES-CTR cipher instance for ``key`` and initial counter ``iv``."""
        return self._cipher(self._aes(key), self._ctr(iv))


_CRYPTO: _CryptographyImpl | None = None


def _get_crypto() -> _CryptographyImpl:
    """Return the singleton :class:`_CryptographyImpl`, constructing it on first call.

    Raises:
        ImportError: If the ``media`` extra (``cryptography``) is not installed.
    """
    global _CRYPTO  # noqa: PLW0603 — module-level singleton, intentional
    if _CRYPTO is None:
        _CRYPTO = _CryptographyImpl()
    return _CRYPTO


# ---------------------------------------------------------------------------
# Low-level AES-CM primitives (module-private, exported for test KAT)
# ---------------------------------------------------------------------------


def _aes_cm_keystream(key: bytes, iv: bytes, length: int) -> bytes:
    """Generate ``length`` bytes of AES Counter Mode keystream (RFC 3711 §4.1.1).

    Uses Python's :mod:`cryptography` AES-CTR, which increments the full
    128-bit counter as a big-endian integer — identical to the RFC 3711 AES-CM
    counter behaviour (verified against RFC 3711 Appendix B.2 / NIST SP 800-38A
    §F.5.1).

    Args:
        key:    16-byte AES-128 session encryption key (``k_e``).
        iv:     16-byte initial counter (per-packet IV or KDF IV).
        length: Number of keystream bytes to return.

    Returns:
        Exactly ``length`` bytes of keystream (empty bytes if ``length == 0``).
    """
    if length == 0:
        return b""
    cipher = _get_crypto().make_cipher(key, iv)
    encryptor = cipher.encryptor()
    # Encrypt ``length`` zero bytes under AES-CTR to obtain the keystream.
    return encryptor.update(b"\x00" * length) + encryptor.finalize()


def _derive_session_keys(
    master_key: bytes,
    master_salt: bytes,
) -> tuple[bytes, bytes, bytes]:
    """Derive session keys from the SRTP master key and salt (RFC 3711 §4.3.1).

    Uses ``key_derivation_rate = 0`` (the SDES default), so ``r = 0`` for all
    labels and the KDF is called once per key type at session startup.

    KDF IV formula (verified against RFC 3711 Appendix B.3):
    ``IV = (label * 2^64) XOR (master_salt || 0x0000)``

    Args:
        master_key:  16-byte SRTP master key.
        master_salt: 14-byte SRTP master salt.

    Returns:
        ``(k_e, k_s, k_a)`` — session encryption key (16 bytes), session salt
        (14 bytes), and session auth key (20 bytes).
    """
    # Pad the 14-byte master salt to 16 bytes (right-pad with 0x0000).
    salt_padded_int = int.from_bytes(master_salt + b"\x00\x00", "big")

    def _kdf(label: int, out_len: int) -> bytes:
        # ``label * 2^64`` places the label byte at bit position [71..64]
        # of the 128-bit IV (verified: RFC 3711 Appendix B.3 matches).
        label_val = label * (2**_KDF_LABEL_SHIFT)
        iv_int = label_val ^ salt_padded_int
        iv = iv_int.to_bytes(_AES_BLOCK, "big")
        return _aes_cm_keystream(master_key, iv, out_len)

    k_e = _kdf(_KDF_LABEL_CIPHER, _SESSION_ENCRYPT_KEY_LEN)
    k_s = _kdf(_KDF_LABEL_SALT, _SESSION_SALT_LEN)
    k_a = _kdf(_KDF_LABEL_AUTH, _SESSION_AUTH_KEY_LEN)
    return k_e, k_s, k_a


# ---------------------------------------------------------------------------
# Per-packet IV and authentication
# ---------------------------------------------------------------------------


def _packet_iv(session_salt: bytes, ssrc: int, roc: int, seq: int) -> bytes:
    """Build the 128-bit per-packet AES-CM IV (RFC 3711 §4.1.1).

    ``IV = (k_s * 2^16) XOR (SSRC * 2^64) XOR (i * 2^16)``
    where ``i = 2^16 * ROC + SEQ`` is the 48-bit SRTP packet index.

    Args:
        session_salt: 14-byte session salt (``k_s``).
        ssrc:         32-bit SSRC from the RTP header.
        roc:          Current rollover counter.
        seq:          16-bit RTP sequence number.

    Returns:
        16-byte AES-CM initial counter value.
    """
    # k_s shifted left 16 bits into 128-bit space = k_s || 0x0000
    salt_int = int.from_bytes(session_salt + b"\x00\x00", "big")
    # SSRC shifted left 64 bits
    ssrc_int = ssrc << _KDF_LABEL_SHIFT  # same 64-bit shift as KDF label
    # i = ROC * 2^16 + SEQ; i shifted left 16 bits
    i_int = ((roc << 16) | seq) << 16
    iv_int = salt_int ^ ssrc_int ^ i_int
    return iv_int.to_bytes(_AES_BLOCK, "big")


def _hmac_sha1(key: bytes, data: bytes) -> bytes:
    """Return the 20-byte HMAC-SHA1 digest of ``data`` under ``key``.

    HMAC-SHA1 is mandated by RFC 3711 §4.2 for the supported SRTP suites;
    this is not a hash used for collision resistance but as a MAC, so SHA-1
    is appropriate here.
    """
    return hmac.new(key, data, hashlib.sha1).digest()


def _auth_tag(
    auth_key: bytes,
    rtp_header: bytes,
    ciphertext: bytes,
    roc: int,
    tag_len: int,
) -> bytes:
    """Compute the SRTP authentication tag (RFC 3711 §4.2).

    ``tag = HMAC-SHA1(k_a, rtp_header || ciphertext || ROC_4bytes)[:tag_len]``

    Args:
        auth_key:   20-byte session authentication key (``k_a``).
        rtp_header: RTP header bytes (12 bytes for fixed header only).
        ciphertext: Encrypted RTP payload.
        roc:        Current rollover counter (unsigned 32-bit).
        tag_len:    Auth tag length in bytes (10 for _80, 4 for _32).

    Returns:
        ``tag_len`` bytes of authentication tag.
    """
    roc_bytes = struct.pack("!I", roc & 0xFFFFFFFF)
    digest = _hmac_sha1(auth_key, rtp_header + ciphertext + roc_bytes)
    return digest[:tag_len]


def _rtp_header_len(data: bytes) -> int:
    """Return the length of the clear RTP header in ``data`` (RFC 3550 §5.1).

    The clear (authenticated, never encrypted) header is the 12-byte fixed
    header plus the CSRC list (``CC`` * 4 octets) plus, when the ``X`` bit is
    set, the 4-byte extension preamble and its declared extension words. SRTP
    encrypts only what follows this boundary (RFC 3711 §3.1/§3.3), so the header
    length must be computed from the actual ``CC``/``X`` fields — a fixed
    12-byte assumption corrupts any packet carrying CSRCs or an extension.

    Args:
        data: The (decrypted-or-plaintext) RTP wire bytes — header onwards.

    Returns:
        The number of leading octets that form the clear RTP header.

    Raises:
        SrtpError: If ``data`` is too short for its declared CSRC count or
            extension.
    """
    if len(data) < _RTP_HEADER_LEN:
        msg = "SRTP packet too short for a fixed RTP header"
        raise SrtpError(msg)
    byte0 = data[0]
    offset = _RTP_HEADER_LEN + (byte0 & _RTP_CSRC_COUNT_MASK) * _RTP_WORD
    if len(data) < offset:
        msg = "SRTP packet too short for its CSRC count"
        raise SrtpError(msg)
    if byte0 & _RTP_EXTENSION_BIT:
        if len(data) < offset + _RTP_EXT_HEADER_LEN:
            msg = "SRTP packet too short for its extension header"
            raise SrtpError(msg)
        ext_words = int.from_bytes(data[offset + 2 : offset + 4], "big")
        offset += _RTP_EXT_HEADER_LEN + ext_words * _RTP_WORD
        if len(data) < offset:
            msg = "SRTP packet too short for its extension data"
            raise SrtpError(msg)
    return offset


def _reject_session_params(params: list[str]) -> None:
    """Reject unsupported SDES inline session parameters (RFC 4568 §6.1).

    The inline key may carry ``|lifetime`` and/or ``|MKI:length`` after the
    base64 key||salt. We implement neither: a non-default key lifetime is not
    honoured, and an MKI changes the SRTP packet layout (MKI octets sit between
    the payload and the auth tag) which this transform does not parse. Reject
    them at construction rather than silently dropping them.

    The offending tokens are never echoed — they sit alongside the master key in
    the same attribute and a copy in a log line or traceback is a leak.

    Args:
        params: The ``|``-split segments that follow the base64 key||salt.

    Raises:
        SrtpError: Always — an MKI segment (``contains ':'``) is named as such;
            anything else is reported as an unsupported lifetime/session param.
    """
    if any(":" in segment for segment in params):
        msg = (
            "SRTP MKI is not supported (it changes the SRTP packet layout); "
            "offer an inline key without an MKI"
        )
        raise SrtpError(msg)
    msg = (
        "unsupported SDES session parameter (non-default key lifetime); "
        "offer an inline key without session parameters"
    )
    raise SrtpError(msg)


# ---------------------------------------------------------------------------
# SrtpSession
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SrtpSession:
    """SRTP session for ONE direction and ONE SSRC (RFC 3711).

    Use one :class:`SrtpSession` per direction (TX / RX): :meth:`protect` on the
    sender, :meth:`unprotect` on the receiver.  Instantiate with the
    :class:`~hermes_voip.sdp.CryptoAttribute` from the negotiated SDES
    ``a=crypto`` line.

    **One SSRC per session (RFC 3711 §3.2.3).** The rollover counter and replay
    window form a per-SSRC cryptographic context, so a session is bound to a
    single SSRC. The SSRC is fixed either at construction (``ssrc=``) or captured
    from the first packet processed; every later packet must carry that same
    SSRC, or it is rejected (``SrtpError``) *before* any ROC/replay state is
    mutated. A second media source needs its own :class:`SrtpSession`. (For
    telephony there is one SSRC per stream direction, so this is the normal
    case, not a limitation.)

    **Key material is never stored in repr** — all key-bearing fields are
    ``field(repr=False)`` so that ``repr(session)`` and tracebacks never expose
    SRTP key material.

    Raises:
        ImportError: At construction time if the ``media`` extra is not installed.
        SrtpError:   At construction on an unsupported suite or unsupported SDES
            session parameters (a non-default key lifetime or an MKI); from
            :meth:`protect`/:meth:`unprotect` on an SSRC mismatch; from
            :meth:`unprotect` on auth failure, replay, or a malformed packet.
    """

    _crypto: CryptoAttribute = field(repr=False)
    #: Session encryption key (k_e), derived at construction.
    _k_e: bytes = field(init=False, repr=False)
    #: Session salt (k_s), derived at construction.
    _k_s: bytes = field(init=False, repr=False)
    #: Session authentication key (k_a), derived at construction.
    _k_a: bytes = field(init=False, repr=False)
    #: SRTP auth-tag byte length (10 for _80, 4 for _32).
    _tag_len: int = field(init=False, repr=False)
    #: The SSRC this context is bound to, or None until the first packet binds it.
    _ssrc: int | None = field(default=None, init=False)
    #: Current rollover counter (sender or receiver).
    roc: int = field(default=0, init=False)
    #: Highest sequence number seen (receiver) or sent (sender), or -1 if none.
    _seq_top: int = field(default=-1, init=False, repr=False)
    #: Receiver replay-window bitmask (_REPLAY_WINDOW bits).
    _replay_mask: int = field(default=0, init=False, repr=False)

    def __init__(self, crypto: CryptoAttribute, *, ssrc: int | None = None) -> None:
        """Construct an SRTP session and derive session keys from the CryptoAttribute.

        Args:
            crypto: The validated SDES ``a=crypto`` attribute (from
                :class:`~hermes_voip.sdp.CryptoAttribute`).  The 30-byte inline
                key||salt is decoded and the session keys are derived immediately
                (RFC 3711 §4.3.1, KDR = 0).
            ssrc: Optionally bind this context to a known SSRC up front. When
                omitted, the session binds to the SSRC of the first packet it
                processes.

        Raises:
            ImportError: If the ``media`` extra (``cryptography``) is not installed.
            SrtpError: If the suite is unsupported, or the inline key carries SDES
                session parameters we do not support (a non-default key lifetime
                or an MKI — an MKI would change the SRTP packet layout).
        """
        # Trigger lazy import early so the error is at construction, not first use.
        _get_crypto()

        self._crypto = crypto
        suite = crypto.suite

        if suite not in _AUTH_TAG_LEN:
            msg = f"unsupported SRTP suite: {suite!r}"
            raise SrtpError(msg)
        self._tag_len = _AUTH_TAG_LEN[suite]

        # Split the inline key-params: inline:<base64 key||salt>[|lifetime][|MKI:len]
        # (RFC 4568 §6.1). We support neither a non-default lifetime nor an MKI, and
        # an MKI in particular changes the SRTP packet layout (it inserts MKI octets
        # between the payload and the auth tag), so reject either rather than
        # silently dropping it. Never echo the offending token — it carries the key.
        inline = crypto.key_params[len(_INLINE_PREFIX) :]
        segments = inline.split("|")
        b64 = segments[0]
        if len(segments) > 1:
            _reject_session_params(segments[1:])

        raw = base64.b64decode(b64)
        master_key = raw[:_MASTER_KEY_LEN]
        master_salt = raw[_MASTER_KEY_LEN : _MASTER_KEY_LEN + _MASTER_SALT_LEN]

        # Derive the three session keys (KDR=0, r=0 for all labels).
        self._k_e, self._k_s, self._k_a = _derive_session_keys(master_key, master_salt)

        # Per-SSRC context state.
        self._ssrc = ssrc
        self.roc = 0
        self._seq_top = -1
        self._replay_mask = 0

    def _bind_ssrc(self, ssrc: int) -> None:
        """Bind the context to ``ssrc`` on first use, or reject a mismatch.

        Called before any ROC/replay mutation, so a rejected foreign-SSRC packet
        leaves the context untouched.

        Raises:
            SrtpError: If ``ssrc`` differs from the SSRC this session is bound to.
        """
        if self._ssrc is None:
            self._ssrc = ssrc
        elif ssrc != self._ssrc:
            msg = (
                "SRTP packet SSRC does not match this session's SSRC; "
                "one SrtpSession serves a single SSRC (RFC 3711 §3.2.3)"
            )
            raise SrtpError(msg)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def protect(self, packet: RtpPacket) -> bytes:
        """Encrypt and authenticate an outbound RTP packet (RFC 3711 §3.3).

        Equivalent to ``protect_wire(packet.pack())``. Because
        :meth:`~hermes_voip.rtp.RtpPacket.pack` emits only the 12-byte fixed
        header (no CSRC/extension), this is the common sender path; use
        :meth:`protect_wire` to protect a pre-built RTP datagram that carries a
        CSRC list or a header extension.

        Args:
            packet: Plaintext :class:`~hermes_voip.rtp.RtpPacket` to protect.

        Returns:
            SRTP wire bytes (clear RTP header || encrypted payload || auth tag).

        Raises:
            SrtpError: If ``packet.ssrc`` differs from this session's bound SSRC.
        """
        return self.protect_wire(packet.pack())

    def protect_wire(self, rtp_wire: bytes) -> bytes:
        """Encrypt and authenticate a complete RTP datagram (RFC 3711 §3.3).

        Encrypts only the RTP *payload*; the full clear RTP header (the fixed
        header plus any CSRC list and header extension) is left in the clear and
        covered by the authentication tag (header || ciphertext || ROC). Updates
        the sender ROC when the 16-bit sequence number wraps.

        Args:
            rtp_wire: A complete RTP datagram (header onwards), e.g. from
                :meth:`~hermes_voip.rtp.RtpPacket.pack` or a relayed packet.

        Returns:
            SRTP wire bytes (clear RTP header || encrypted payload || auth tag).

        Raises:
            SrtpError: If the datagram is malformed, or its SSRC differs from
                this session's bound SSRC.
        """
        header_len = _rtp_header_len(rtp_wire)
        try:
            seq = int.from_bytes(rtp_wire[2:4], "big")
            ssrc = int.from_bytes(rtp_wire[8:12], "big")
        except (IndexError, ValueError) as exc:  # pragma: no cover - len-guarded above
            msg = "RTP datagram header is malformed"
            raise SrtpError(msg) from exc

        # Bind/verify the SSRC BEFORE mutating any ROC/seq state.
        self._bind_ssrc(ssrc)

        header = rtp_wire[:header_len]
        payload = rtp_wire[header_len:]

        # Update sender ROC on sequence-number wraparound.
        if self._seq_top == _SEQ_MAX and seq == 0:
            self.roc += 1
        self._seq_top = seq

        # Encrypt only the payload (the header stays clear, authenticated).
        iv = _packet_iv(self._k_s, ssrc, self.roc, seq)
        ks = _aes_cm_keystream(self._k_e, iv, len(payload))
        ciphertext = bytes(p ^ k for p, k in zip(payload, ks, strict=True))

        # Compute and append the authentication tag over the clear header.
        tag = _auth_tag(self._k_a, header, ciphertext, self.roc, self._tag_len)
        return header + ciphertext + tag

    def unprotect(self, data: bytes) -> RtpPacket:
        """Authenticate and decrypt an inbound SRTP packet (RFC 3711 §3.3).

        Processing order (RFC 3711 §3.3): estimate the index, verify the auth tag
        FIRST (constant-time over the full clear header + ciphertext + ROC), then
        enforce the per-SSRC binding, then the replay window, then decrypt only
        the payload. The clear header (including any CSRC list / header
        extension) is preserved verbatim.

        Auth precedes the SSRC binding deliberately: the session auth key derives
        from the master key alone (it does not depend on the SSRC), and on the
        first packet the index estimate is SSRC-independent — so a *forged* first
        packet bearing an arbitrary SSRC fails authentication and never binds the
        context. Only an authentic packet can bind it. No receiver state (ROC,
        replay window, SSRC binding) is mutated until auth has passed.

        Args:
            data: Raw SRTP wire bytes.

        Returns:
            Decrypted :class:`~hermes_voip.rtp.RtpPacket`.

        Raises:
            SrtpError: On a malformed packet, an auth-tag mismatch, an SSRC
                mismatch, or replay detection.
        """
        tag_len = self._tag_len
        if len(data) < _RTP_HEADER_LEN + tag_len:
            msg = "SRTP packet too short to contain a valid RTP header and auth tag"
            raise SrtpError(msg)

        # Split off the auth tag, then locate the clear-header boundary.
        srtp_body = data[: len(data) - tag_len]
        received_tag = data[len(data) - tag_len :]
        header_len = _rtp_header_len(srtp_body)

        header = srtp_body[:header_len]
        ciphertext = srtp_body[header_len:]

        # Parse SEQ and SSRC from the fixed RTP header (always the first 12 bytes).
        seq = int.from_bytes(header[2:4], "big")
        ssrc = int.from_bytes(header[8:12], "big")

        # Compute the extended sequence index using the current ROC estimate
        # (RFC 3711 §3.3.1).
        roc_for_packet = self._estimate_roc(seq)

        # Verify the authentication tag FIRST (constant-time). This is computed
        # over the clear header + ciphertext + ROC; the auth key is SSRC-agnostic,
        # so a forged SSRC cannot forge a tag.
        expected_tag = _auth_tag(self._k_a, header, ciphertext, roc_for_packet, tag_len)
        if not hmac.compare_digest(expected_tag, received_tag):
            msg = "SRTP auth tag mismatch — packet rejected"
            raise SrtpError(msg)

        # Enforce the per-SSRC binding only after auth, and before any replay or
        # ROC state mutation, so neither a forged nor a foreign-SSRC packet ever
        # binds the context or advances receiver state.
        self._bind_ssrc(ssrc)

        # Replay-window check (after auth, to prevent DoS via replay oracle).
        packet_index = (roc_for_packet << 16) | seq
        self._check_replay(packet_index)

        # Decrypt only the payload.
        iv = _packet_iv(self._k_s, ssrc, roc_for_packet, seq)
        ks = _aes_cm_keystream(self._k_e, iv, len(ciphertext))
        plaintext = bytes(c ^ k for c, k in zip(ciphertext, ks, strict=True))

        # Update receiver ROC and replay window.
        self._update_state(seq, roc_for_packet, packet_index)

        # Reconstruct the RTP packet from the clear header + decrypted payload.
        plain_wire = header + plaintext
        return RtpPacket.parse(plain_wire)

    # ------------------------------------------------------------------
    # Internal receiver state management
    # ------------------------------------------------------------------

    def _estimate_roc(self, seq: int) -> int:
        """Estimate the ROC for an inbound packet (RFC 3711 §3.3.1).

        Follows the index estimation algorithm from the RFC: if the receiver
        has not yet seen any packet (``_seq_top == -1``), assume ROC = 0.
        Otherwise, choose the ROC value that makes the inbound packet index
        closest to the top of the receiver window.

        Args:
            seq: The 16-bit sequence number from the inbound packet header.

        Returns:
            Estimated ROC for the packet.
        """
        if self._seq_top < 0:
            return 0

        v = self.roc
        s_l = self._seq_top  # highest seq seen in the current epoch

        # Standard RFC 3711 §3.3.1 estimation (simplified for KDR=0).
        if s_l < _SEQ_HALF:
            if seq - s_l > _SEQ_HALF:
                v = (self.roc - 1) % _ROC_MOD
        elif s_l - seq > _SEQ_HALF:
            v = self.roc + 1
        return v

    def _check_replay(self, packet_index: int) -> None:
        """Check the replay window for ``packet_index`` (RFC 3711 §3.3).

        The replay window covers the last ``_REPLAY_WINDOW`` (64) packet
        indices.  Any index at or below ``top - window`` is rejected as too
        old.  Any index already in the window bitmask is rejected as a replay.

        Args:
            packet_index: The 48-bit SRTP packet index (ROC<<16 | SEQ).

        Raises:
            SrtpError: If the packet is a replay or too old.
        """
        top = self._replay_top()
        if top < 0:
            return  # no packet received yet

        diff = top - packet_index
        if diff >= _REPLAY_WINDOW:
            msg = "SRTP replay detected: packet index is outside the replay window"
            raise SrtpError(msg)
        if diff >= 0:
            bit = 1 << (diff % _REPLAY_WINDOW)
            if self._replay_mask & bit:
                msg = "SRTP replay detected: packet index already received"
                raise SrtpError(msg)

    def _update_state(self, seq: int, roc: int, packet_index: int) -> None:
        """Update the receiver's ROC, seq_top, and replay-window state.

        Args:
            seq:          16-bit sequence number from the authenticated packet.
            roc:          The ROC used for this packet (from ``_estimate_roc``).
            packet_index: The 48-bit SRTP packet index (roc<<16 | seq).
        """
        top = self._replay_top()

        if top < 0 or packet_index > top:
            # New highest packet: shift the window forward.
            shift = packet_index - top if top >= 0 else 0
            self._replay_mask = ((self._replay_mask << shift) | 1) & _REPLAY_WINDOW_MASK
            self._seq_top = seq
            self.roc = roc
        else:
            # Packet within window: mark its bit.
            diff = top - packet_index
            bit = 1 << (diff % _REPLAY_WINDOW)
            self._replay_mask |= bit

    def _replay_top(self) -> int:
        """Return the current highest received packet index, or -1 if none."""
        if self._seq_top < 0:
            return -1
        return (self.roc << 16) | self._seq_top
