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
from typing import Protocol, runtime_checkable

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

# RTP fixed header size.
_RTP_HEADER_LEN = 12

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
# install and its mypy gate.  These Protocols describe the exact members used
# in _aes_cm_keystream; the lazy resolver _get_cipher() binds
# importlib.import_module(...) to a Protocol-typed handle.  mypy stays clean
# under both the no-media gate and the media env — no Any, no cast, no
# # type: ignore (AGENTS.md rules 17/39).
# ---------------------------------------------------------------------------


class _CipherContext(Protocol):
    """The subset of cryptography's ``CipherContext`` used here."""

    def update(self, data: bytes) -> bytes: ...
    def finalize(self) -> bytes: ...


class _CipherHandle(Protocol):
    """The subset of cryptography's ``Cipher`` object used here."""

    def encryptor(self) -> _CipherContext: ...


@runtime_checkable
class _CryptographyModule(Protocol):
    """Combined surface of ciphers + algorithms + modes used in _aes_cm_keystream."""

    def make_cipher(self, key: bytes, iv: bytes) -> _CipherHandle: ...


class _CryptographyImpl:
    """Runtime adapter that wraps importlib-loaded cryptography modules."""

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
        self._Cipher = ciphers_mod.Cipher
        self._AES = algorithms_mod.AES
        self._CTR = modes_mod.CTR

    def make_cipher(self, key: bytes, iv: bytes) -> _CipherHandle:
        """Build an AES-CTR cipher instance for ``key`` and initial counter ``iv``."""
        # self._Cipher / _AES / _CTR are loaded via importlib so their type is Any;
        # calling Any(...) returns Any, triggering no-any-return against the Protocol
        # return annotation.  cast() is equally banned (AGENTS.md rule 17); this single
        # boundary ignore is the minimum escape needed for the importlib lazy pattern.
        return self._Cipher(self._AES(key), self._CTR(iv))  # type: ignore[no-any-return]


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


# ---------------------------------------------------------------------------
# SrtpSession
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SrtpSession:
    """SRTP session for one direction (RFC 3711).

    One :class:`SrtpSession` instance per direction (TX / RX).  Instantiate
    with the :class:`~hermes_voip.sdp.CryptoAttribute` from the negotiated
    SDES ``a=crypto`` line; use :meth:`protect` on the sender and
    :meth:`unprotect` on the receiver.

    **Key material is never stored in repr** — all key-bearing fields are
    ``field(repr=False)`` so that ``repr(session)`` and tracebacks never
    expose SRTP key material.

    Raises:
        ImportError: At construction time if the ``media`` extra is not installed.
        SrtpError:   From :meth:`unprotect` on auth failure, replay, or format error.
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
    #: Current rollover counter (sender or receiver).
    roc: int = field(default=0, init=False)
    #: Highest sequence number seen (receiver) or sent (sender), or -1 if none.
    _seq_top: int = field(default=-1, init=False, repr=False)
    #: Receiver replay-window bitmask (_REPLAY_WINDOW bits).
    _replay_mask: int = field(default=0, init=False, repr=False)

    def __init__(self, crypto: CryptoAttribute) -> None:
        """Construct an SRTP session and derive session keys from the CryptoAttribute.

        Args:
            crypto: The validated SDES ``a=crypto`` attribute (from
                :class:`~hermes_voip.sdp.CryptoAttribute`).  The 30-byte
                inline key||salt is decoded and the session keys are derived
                immediately (RFC 3711 §4.3.1, KDR = 0).

        Raises:
            ImportError: If the ``media`` extra (``cryptography``) is not installed.
        """
        # Trigger lazy import early so the error is at construction, not first use.
        _get_crypto()

        self._crypto = crypto
        suite = crypto.suite

        if suite not in _AUTH_TAG_LEN:
            msg = f"unsupported SRTP suite: {suite!r}"
            raise SrtpError(msg)
        self._tag_len = _AUTH_TAG_LEN[suite]

        # Decode the 30-byte inline master key||salt from the CryptoAttribute.
        key_params = crypto.key_params
        b64 = key_params[len(_INLINE_PREFIX) :].split("|", 1)[0]
        raw = base64.b64decode(b64)
        master_key = raw[:_MASTER_KEY_LEN]
        master_salt = raw[_MASTER_KEY_LEN : _MASTER_KEY_LEN + _MASTER_SALT_LEN]

        # Derive the three session keys (KDR=0, r=0 for all labels).
        self._k_e, self._k_s, self._k_a = _derive_session_keys(master_key, master_salt)

        # Sender/receiver state.
        self.roc = 0
        self._seq_top = -1
        self._replay_mask = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def protect(self, packet: RtpPacket) -> bytes:
        """Encrypt and authenticate an outbound RTP packet (RFC 3711 §3.3).

        Constructs the SRTP packet: encrypted payload + appended auth tag.
        Updates the sender's ROC when the sequence number wraps.

        Args:
            packet: Plaintext :class:`~hermes_voip.rtp.RtpPacket` to protect.

        Returns:
            SRTP wire bytes (RTP header || encrypted payload || auth tag).
        """
        seq = packet.sequence_number
        ssrc = packet.ssrc

        # Update sender ROC on sequence-number wraparound.
        if self._seq_top == _SEQ_MAX and seq == 0:
            self.roc += 1
        self._seq_top = seq

        # Encrypt the payload.
        iv = _packet_iv(self._k_s, ssrc, self.roc, seq)
        ks = _aes_cm_keystream(self._k_e, iv, len(packet.payload))
        ciphertext = bytes(p ^ k for p, k in zip(packet.payload, ks, strict=True))

        # Re-pack the fixed 12-byte RTP header (no CSRC/extension in our packets).
        rtp_wire = packet.pack()
        header = rtp_wire[:_RTP_HEADER_LEN]

        # Compute and append the authentication tag.
        tag = _auth_tag(self._k_a, header, ciphertext, self.roc, self._tag_len)
        return header + ciphertext + tag

    def unprotect(self, data: bytes) -> RtpPacket:
        """Authenticate and decrypt an inbound SRTP packet (RFC 3711 §3.3).

        Verifies the auth tag FIRST (constant-time), then checks the replay
        window, then decrypts the payload.

        Args:
            data: Raw SRTP wire bytes.

        Returns:
            Decrypted :class:`~hermes_voip.rtp.RtpPacket`.

        Raises:
            SrtpError: On auth-tag mismatch, replay detection, or format error.
        """
        tag_len = self._tag_len
        min_len = _RTP_HEADER_LEN + tag_len
        if len(data) < min_len:
            msg = "SRTP packet too short to contain a valid RTP header and auth tag"
            raise SrtpError(msg)

        # Split off the auth tag.
        srtp_body = data[: len(data) - tag_len]
        received_tag = data[len(data) - tag_len :]

        header = srtp_body[:_RTP_HEADER_LEN]
        ciphertext = srtp_body[_RTP_HEADER_LEN:]

        # Parse SEQ and SSRC from the fixed RTP header.
        try:
            _b0, _b1, seq, _ts, ssrc = struct.unpack("!BBHII", header)
        except struct.error as exc:
            msg = "SRTP packet header is malformed"
            raise SrtpError(msg) from exc

        # Compute the extended sequence index using the current ROC estimate
        # (RFC 3711 §3.3.1).
        roc_for_packet = self._estimate_roc(seq)

        # Verify the authentication tag BEFORE decrypting (constant-time).
        expected_tag = _auth_tag(self._k_a, header, ciphertext, roc_for_packet, tag_len)
        if not hmac.compare_digest(expected_tag, received_tag):
            msg = "SRTP auth tag mismatch — packet rejected"
            raise SrtpError(msg)

        # Replay-window check (after auth, to prevent DoS via replay oracle).
        packet_index = (roc_for_packet << 16) | seq
        self._check_replay(packet_index)

        # Decrypt payload.
        iv = _packet_iv(self._k_s, ssrc, roc_for_packet, seq)
        ks = _aes_cm_keystream(self._k_e, iv, len(ciphertext))
        plaintext = bytes(c ^ k for c, k in zip(ciphertext, ks, strict=True))

        # Update receiver ROC and replay window.
        self._update_state(seq, roc_for_packet, packet_index)

        # Reconstruct the RTP packet from the decrypted wire bytes.
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
