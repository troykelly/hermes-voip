"""SRTCP packet transform (RFC 3711 §3.4) — AES-CM + HMAC-SHA1.

This module secures the RTCP control channel the same way
:mod:`hermes_voip.media.srtp` secures the RTP media stream: :meth:`protect`
turns an outbound compound RTCP datagram (built by :mod:`hermes_voip.rtcp`)
into an SRTCP packet, and :meth:`unprotect` turns an inbound SRTCP packet back
into the cleartext compound RTCP bytes.  It is the payload-level transform only
— the caller (the media transport) owns the UDP socket, the RTCP build/parse,
and the key exchange.

It supports the two suites SRTP supports — ``AES_CM_128_HMAC_SHA1_80`` and
``AES_CM_128_HMAC_SHA1_32`` — and shares SRTP's key/salt material and crypto
primitives, but with the three RFC 3711 §3.4 differences:

* **Different KDF labels.** SRTCP derives its session keys from the *same*
  master key/salt as SRTP, but with labels ``0x03`` (RTCP encryption key),
  ``0x04`` (RTCP auth key), ``0x05`` (RTCP salt) — distinct from SRTP's
  ``0x00``/``0x01``/``0x02``, so the two keystreams never collide.
* **Explicit 31-bit SRTCP index.** In place of SRTP's *implicit* ROC/SEQ
  index, every SRTCP packet carries a 31-bit index in a 4-byte trailer word
  whose most-significant bit is the **E (encrypt) flag**.  The index is the
  ``i`` in the AES-CM IV.  It starts at zero, increments by one (mod 2^31) per
  packet, and is never reset on re-key.
* **Encrypt from the ninth octet.** Only the RTCP *payload* — from octet 9 to
  the end of the compound packet — is encrypted; the first 8 octets (the first
  report's header through the sender SSRC) stay in the clear.  The auth tag
  covers the **entire** packet plus the E-flag + index word (after encryption),
  with **no** ROC appended (unlike SRTP §4.2).

**Authentication is mandatory** for SRTCP (RFC 3711 §3.4): on inbound the tag
is verified first (constant time) and decryption only runs once it passes;
replay protection uses a separate replay list keyed on the explicit index
(§3.3.2).  Auth failure raises :class:`SrtcpError` — it never silently returns
(AGENTS.md rule 37).

**Dependency gating** and the **narrow-Protocol crypto seam** are inherited
wholesale from :mod:`hermes_voip.media.srtp`: the AES-CM keystream, HMAC-SHA1,
and the lazily-imported ``cryptography`` backend are reused directly, so this
module adds no new escape hatch and no second copy of the cipher plumbing.

**Security invariants** (mirroring SRTP):

* Key material (master key, session keys, salt) never appears in :func:`repr`,
  log messages, or exception text.
* Auth-tag verification uses :func:`hmac.compare_digest` (constant time).
* No hand-rolled AES; all block-cipher work goes through ``cryptography``.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from dataclasses import dataclass, field

from hermes_voip.media.srtp import (
    _AES_BLOCK,
    _AUTH_TAG_LEN,
    _INLINE_PREFIX,
    _MASTER_KEY_LEN,
    _MASTER_SALT_LEN,
    _REPLAY_WINDOW,
    _REPLAY_WINDOW_MASK,
    _SESSION_AUTH_KEY_LEN,
    _SESSION_ENCRYPT_KEY_LEN,
    _SESSION_SALT_LEN,
    _aes_cm_keystream,
    _get_crypto,
    _hmac_sha1,
    _reject_session_params,
)
from hermes_voip.sdp import CryptoAttribute

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "SrtcpError",
    "SrtcpSession",
]  # Alphabetically sorted

# ---------------------------------------------------------------------------
# Constants (RFC 3711 §3.4 / §4.3.2)
# ---------------------------------------------------------------------------

# SRTCP KDF labels (RFC 3711 §4.3.2, Table 4): the SAME KDF as SRTP, but the
# RTCP-specific labels select a distinct keystream from the shared master key.
_KDF_LABEL_RTCP_CIPHER: int = 0x03
_KDF_LABEL_RTCP_AUTH: int = 0x04
_KDF_LABEL_RTCP_SALT: int = 0x05

# Label position shift in the 128-bit KDF IV: label * 2^64 (same as SRTP).
_KDF_LABEL_SHIFT = 64

# The clear (authenticated, never encrypted) prefix of an SRTCP packet: the
# 4-byte RTCP common header plus the 4-byte sender SSRC = octets 0..7. Encryption
# covers "from the ninth (9) octet to the end" (RFC 3711 §3.4).
_SRTCP_CLEAR_PREFIX = 8

# The mandatory E-flag + 31-bit SRTCP index trailer word (RFC 3711 §3.4).
_SRTCP_INDEX_LEN = 4
# Most-significant bit of the trailer word: the E (encrypt) flag.
_E_FLAG = 0x80000000
# The 31-bit index occupies the remaining bits.
_SRTCP_INDEX_MASK = 0x7FFFFFFF
# The highest 31-bit SRTCP index. The index space must NOT wrap under one master
# key: wrapping reuses the AES-CM IV/keystream (a two-time pad), so the sender
# raises on exhaustion instead (RFC 3711 §3.4 — re-key required).
_SRTCP_INDEX_MAX = 0x7FFFFFFF

# The SDES inline master key||salt is exactly key(16) + salt(14) = 30 octets for
# both supported suites (RFC 4568 §6.2).
_MASTER_KEY_SALT_LEN = _MASTER_KEY_LEN + _MASTER_SALT_LEN


class SrtcpError(ValueError):
    """Raised when SRTCP protect/unprotect fails (auth, replay, or format error)."""


# ---------------------------------------------------------------------------
# Key derivation and per-packet IV (RFC 3711 §4.3.2 / §4.1.1)
# ---------------------------------------------------------------------------


def _derive_srtcp_session_keys(
    master_key: bytes,
    master_salt: bytes,
) -> tuple[bytes, bytes, bytes]:
    """Derive the SRTCP session keys from the master key/salt (RFC 3711 §4.3.2).

    Identical KDF to :func:`hermes_voip.media.srtp._derive_session_keys`
    (``key_derivation_rate = 0``, ``IV = (label * 2^64) XOR (master_salt ||
    0x0000)``) but with the RTCP labels ``0x03`` / ``0x04`` / ``0x05`` so the
    derived keystream is independent of the SRTP session keys derived from the
    same master material.

    Args:
        master_key:  16-byte SRTP/SRTCP master key.
        master_salt: 14-byte SRTP/SRTCP master salt.

    Returns:
        ``(k_e, k_s, k_a)`` — the SRTCP encryption key (16 bytes), salt
        (14 bytes), and auth key (20 bytes).
    """
    # Pad the 14-byte master salt to 16 bytes (right-pad with 0x0000).
    salt_padded_int = int.from_bytes(master_salt + b"\x00\x00", "big")

    def _kdf(label: int, out_len: int) -> bytes:
        label_val = label * (2**_KDF_LABEL_SHIFT)
        iv_int = label_val ^ salt_padded_int
        iv = iv_int.to_bytes(_AES_BLOCK, "big")
        return _aes_cm_keystream(master_key, iv, out_len)

    k_e = _kdf(_KDF_LABEL_RTCP_CIPHER, _SESSION_ENCRYPT_KEY_LEN)
    k_s = _kdf(_KDF_LABEL_RTCP_SALT, _SESSION_SALT_LEN)
    k_a = _kdf(_KDF_LABEL_RTCP_AUTH, _SESSION_AUTH_KEY_LEN)
    return k_e, k_s, k_a


def _srtcp_packet_iv(session_salt: bytes, ssrc: int, index: int) -> bytes:
    """Build the 128-bit per-packet AES-CM IV for SRTCP (RFC 3711 §4.1.1).

    Same counter formula as SRTP but with the explicit 31-bit SRTCP index as the
    packet index ``i`` and the first compound header's SSRC:

    ``IV = (k_s || 0x0000) XOR (SSRC * 2^64) XOR (index * 2^16)``

    Args:
        session_salt: 14-byte SRTCP session salt (``k_s``).
        ssrc:         32-bit SSRC of the first packet in the compound.
        index:        31-bit SRTCP index for this packet.

    Returns:
        16-byte AES-CM initial counter value.
    """
    salt_int = int.from_bytes(session_salt + b"\x00\x00", "big")
    ssrc_int = ssrc << _KDF_LABEL_SHIFT  # SSRC occupies IV bits 64..95
    i_int = index << 16  # the index occupies bits 16..47
    iv_int = salt_int ^ ssrc_int ^ i_int
    return iv_int.to_bytes(_AES_BLOCK, "big")


def _decode_inline_key(b64: str) -> tuple[bytes, bytes]:
    """Decode an SDES ``inline:`` base64 master key||salt (RFC 4568 §6.1).

    Self-defending decode (codex r1, defence in depth): although
    :class:`~hermes_voip.sdp.CryptoAttribute` already validates the inline key on
    the normal construction path, this module does not *rely* on that invariant —
    it decodes with ``validate=True``, rejects non-base64 input, and requires the
    decoded master key||salt to be exactly :data:`_MASTER_KEY_SALT_LEN` octets.
    Any fault raises :class:`SrtcpError` (a typed error, never a raw
    ``binascii``/index error). The offending token is never echoed — it is (or
    carries) the SRTP master key (AGENTS.md secrets invariant).

    Args:
        b64: The base64 portion of an ``inline:<key||salt>`` key-params string
            (any ``|lifetime|MKI`` suffix already split off by the caller).

    Returns:
        ``(master_key, master_salt)`` — 16-byte key and 14-byte salt.

    Raises:
        SrtcpError: If ``b64`` is not valid base64, or the decoded length is not
            exactly :data:`_MASTER_KEY_SALT_LEN` octets.
    """
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        # Never echo the token: corrupt base64 is still (corrupt) key material.
        msg = "SRTCP inline key is not valid base64"
        raise SrtcpError(msg) from exc
    if len(raw) != _MASTER_KEY_SALT_LEN:
        msg = (
            f"SRTCP inline key||salt must be exactly {_MASTER_KEY_SALT_LEN} octets "
            f"(got {len(raw)})"
        )
        raise SrtcpError(msg)
    return raw[:_MASTER_KEY_LEN], raw[_MASTER_KEY_LEN:_MASTER_KEY_SALT_LEN]


# ---------------------------------------------------------------------------
# SrtcpSession
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SrtcpSession:
    """SRTCP session for ONE direction and ONE SSRC (RFC 3711 §3.4).

    Use one :class:`SrtcpSession` per direction (TX / RX): :meth:`protect` on the
    sender, :meth:`unprotect` on the receiver.  Two keying paths mirror
    :class:`hermes_voip.media.srtp.SrtpSession`:

    - **SDES (RFC 4568):** ``SrtcpSession(crypto_attr, ssrc=…)`` decodes the
      30-byte inline key||salt from the negotiated ``a=crypto`` attribute and
      derives the SRTCP session keys immediately.
    - **DTLS-SRTP (RFC 5764):** ``SrtcpSession.from_raw_keys(key, salt, suite=…)``
      uses the raw master key/salt from the RFC 5705 keying-material exporter.

    **Explicit index, per direction.** The sender maintains a monotonic 31-bit
    SRTCP index (starting at 1 for the first packet, incrementing mod 2^31); the
    receiver reads the index from each packet's trailer and enforces a replay
    window over it (RFC 3711 §3.3.2, separate from any SRTP replay state).

    **One SSRC per session (RFC 3711 §3.2.3).** The session binds to a single
    SSRC — fixed at construction (``ssrc=``) or captured from the first packet —
    and rejects any packet carrying a different SSRC *before* mutating index or
    replay state.

    **Key material is never stored in repr** — all key-bearing fields are
    ``field(repr=False)``.

    Raises:
        ImportError: At construction if the ``media`` extra is not installed.
        SrtcpError:  At construction on an unsupported suite or unsupported SDES
            session parameters; from :meth:`protect`/:meth:`unprotect` on an
            SSRC mismatch; from :meth:`unprotect` on auth failure, replay, or a
            malformed packet.
    """

    _crypto: CryptoAttribute | None = field(default=None, repr=False)
    #: SRTCP session encryption key (k_e), derived at construction.
    _k_e: bytes = field(init=False, repr=False)
    #: SRTCP session salt (k_s), derived at construction.
    _k_s: bytes = field(init=False, repr=False)
    #: SRTCP session authentication key (k_a), derived at construction.
    _k_a: bytes = field(init=False, repr=False)
    #: SRTCP auth-tag byte length (10 for _80, 4 for _32).
    _tag_len: int = field(init=False, repr=False)
    #: The SSRC this context is bound to, or None until the first packet binds it.
    _ssrc: int | None = field(default=None, init=False)
    #: Sender's last emitted SRTCP index (0 before the first packet → first is 1).
    index: int = field(default=0, init=False)
    #: Receiver's highest accepted SRTCP index, or -1 if none yet.
    _recv_top: int = field(default=-1, init=False, repr=False)
    #: Receiver replay-window bitmask over the SRTCP index (_REPLAY_WINDOW bits).
    _replay_mask: int = field(default=0, init=False, repr=False)

    def __init__(self, crypto: CryptoAttribute, *, ssrc: int | None = None) -> None:
        """Construct an SRTCP session from an SDES ``a=crypto`` attribute.

        **SDES keying path (RFC 4568).** Use :meth:`from_raw_keys` for
        DTLS-SRTP keying (RFC 5764).

        Args:
            crypto: The validated SDES ``a=crypto`` attribute. The 30-byte inline
                key||salt is decoded and the SRTCP session keys are derived
                immediately (RFC 3711 §4.3.2, KDR = 0).
            ssrc: Optionally bind this context to a known SSRC up front. When
                omitted, the session binds to the first packet's SSRC.

        Raises:
            ImportError: If the ``media`` extra (``cryptography``) is not installed.
            SrtcpError: If the suite is unsupported, or the inline key carries SDES
                session parameters we do not support (a non-default key lifetime or
                an MKI).
        """
        # Trigger lazy import early so the error is at construction, not first use.
        _get_crypto()

        self._crypto = crypto
        suite = crypto.suite

        if suite not in _AUTH_TAG_LEN:
            msg = f"unsupported SRTCP suite: {suite!r}"
            raise SrtcpError(msg)
        self._tag_len = _AUTH_TAG_LEN[suite]

        # Split inline:<base64 key||salt>[|lifetime][|MKI:len] (RFC 4568 §6.1).
        # An MKI/lifetime is rejected (never echoed — it carries the key).
        inline = crypto.key_params[len(_INLINE_PREFIX) :]
        segments = inline.split("|")
        b64 = segments[0]
        if len(segments) > 1:
            self._reject_params(segments[1:])

        master_key, master_salt = _decode_inline_key(b64)

        self._k_e, self._k_s, self._k_a = _derive_srtcp_session_keys(
            master_key, master_salt
        )

        self._ssrc = ssrc
        self.index = 0
        self._recv_top = -1
        self._replay_mask = 0

    @staticmethod
    def _reject_params(params: list[str]) -> None:
        """Reject unsupported SDES session params, re-raising as :class:`SrtcpError`.

        Reuses :func:`hermes_voip.media.srtp._reject_session_params` (the MKI /
        lifetime structural messages are identical and never echo the key) and
        re-raises its :class:`~hermes_voip.media.srtp.SrtpError` as the
        SRTCP-typed error so a caller catches one error type per module.
        """
        try:
            _reject_session_params(params)
        except ValueError as exc:  # SrtpError is a ValueError subclass
            raise SrtcpError(str(exc)) from exc

    @classmethod
    def from_raw_keys(
        cls,
        master_key: bytes,
        master_salt: bytes,
        *,
        suite: str,
        ssrc: int | None = None,
    ) -> SrtcpSession:
        """Construct an SRTCP session from raw DTLS-SRTP keying material (RFC 5764).

        **DTLS-SRTP keying path (RFC 5764 §4.2).** The same master key/salt that
        keys the call's :class:`~hermes_voip.media.srtp.SrtpSession` also keys
        SRTCP — only the KDF labels differ — so pass each direction's exported
        master key + salt here to build the SRTCP sessions.

        Key material is never logged or stored in ``repr``.

        Args:
            master_key:  Raw 16-byte master key (AES-128).
            master_salt: Raw 14-byte master salt (112-bit).
            suite: ``"AES_CM_128_HMAC_SHA1_80"`` or ``"AES_CM_128_HMAC_SHA1_32"``.
            ssrc: Optionally pre-bind the session to a known SSRC.

        Returns:
            A fully initialised :class:`SrtcpSession`.

        Raises:
            ImportError: If the ``media`` extra (``cryptography``) is not installed.
            SrtcpError: If the suite is unsupported, or the key/salt length is not
                exactly 16 / 14 bytes.
        """
        _get_crypto()  # raise ImportError early if cryptography is absent

        if len(master_key) != _MASTER_KEY_LEN:
            msg = (
                f"SRTCP master key must be exactly {_MASTER_KEY_LEN} bytes "
                f"(got {len(master_key)})"
            )
            raise SrtcpError(msg)
        if len(master_salt) != _MASTER_SALT_LEN:
            msg = (
                f"SRTCP master salt must be exactly {_MASTER_SALT_LEN} bytes "
                f"(got {len(master_salt)})"
            )
            raise SrtcpError(msg)
        if suite not in _AUTH_TAG_LEN:
            msg = f"unsupported SRTCP suite: {suite!r}"
            raise SrtcpError(msg)

        instance = object.__new__(cls)
        instance._crypto = None  # no SDES attribute on the DTLS keying path
        instance._tag_len = _AUTH_TAG_LEN[suite]
        instance._k_e, instance._k_s, instance._k_a = _derive_srtcp_session_keys(
            master_key, master_salt
        )
        instance._ssrc = ssrc
        instance.index = 0
        instance._recv_top = -1
        instance._replay_mask = 0
        return instance

    def _bind_ssrc(self, ssrc: int) -> None:
        """Bind the context to ``ssrc`` on first use, or reject a mismatch.

        Called before any index/replay mutation, so a rejected foreign-SSRC packet
        leaves the context untouched.

        Raises:
            SrtcpError: If ``ssrc`` differs from the SSRC this session is bound to.
        """
        if self._ssrc is None:
            self._ssrc = ssrc
        elif ssrc != self._ssrc:
            msg = (
                "SRTCP packet SSRC does not match this session's SSRC; "
                "one SrtcpSession serves a single SSRC (RFC 3711 §3.2.3)"
            )
            raise SrtcpError(msg)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def protect(self, rtcp_compound: bytes) -> bytes:
        """Encrypt and authenticate a compound RTCP datagram (RFC 3711 §3.4).

        Encrypts the RTCP payload from the ninth octet onwards (the first 8 octets
        — header + sender SSRC — stay in the clear), appends the E-flag + 31-bit
        SRTCP index trailer, and appends the auth tag computed over the whole
        SRTCP packet up to and including that trailer (no ROC).  The sender's
        SRTCP index advances by one per call; index 0 is reserved unused, so the
        first protected packet carries index 1 and the last carries
        :data:`_SRTCP_INDEX_MAX` (0x7fffffff).

        The index space MUST NOT wrap under a single master key: a wrapped index
        would reproduce a previous AES-CM IV and hence reuse the keystream (a
        catastrophic two-time pad).  When the index is exhausted, :meth:`protect`
        raises :class:`SrtcpError` rather than wrapping — the call must be re-keyed
        (a fresh master key / :class:`SrtcpSession`) to continue.  At one RTCP
        compound packet every few seconds (RFC 3550 §6.2) exhaustion is ~hundreds
        of years away, so in practice this is a safety assertion, not a live limit.

        Args:
            rtcp_compound: A complete compound RTCP datagram (e.g. from
                :func:`hermes_voip.rtcp.build_compound`).

        Returns:
            SRTCP wire bytes: clear-prefix || encrypted-payload || E+index || tag.

        Raises:
            SrtcpError: If the datagram is shorter than the 8-octet clear prefix;
                its SSRC differs from this session's bound SSRC; or the SRTCP index
                space is exhausted (a re-key is required — never wrap).
        """
        if len(rtcp_compound) < _SRTCP_CLEAR_PREFIX:
            msg = (
                "RTCP datagram too short for an SRTCP transform "
                f"(need at least {_SRTCP_CLEAR_PREFIX} octets, "
                f"got {len(rtcp_compound)})"
            )
            raise SrtcpError(msg)

        ssrc = int.from_bytes(rtcp_compound[4:_SRTCP_CLEAR_PREFIX], "big")
        # Bind/verify the SSRC BEFORE advancing the sender index.
        self._bind_ssrc(ssrc)

        # Advance the explicit SRTCP index (index 0 unused → first packet = 1).
        # Refuse to wrap past the 31-bit max: wrapping reuses the IV/keystream
        # under one master key (two-time pad). Exhaustion → hard error, re-key.
        if self.index >= _SRTCP_INDEX_MAX:
            msg = (
                "SRTCP index space exhausted (reached the 31-bit maximum); "
                "the session must be re-keyed — the index must not wrap "
                "(RFC 3711 §3.4: a wrapped index reuses the keystream)"
            )
            raise SrtcpError(msg)
        self.index += 1
        index = self.index

        clear = rtcp_compound[:_SRTCP_CLEAR_PREFIX]
        payload = rtcp_compound[_SRTCP_CLEAR_PREFIX:]

        # Encrypt the payload (octet 9..end) with the SRTCP keystream.
        iv = _srtcp_packet_iv(self._k_s, ssrc, index)
        ks = _aes_cm_keystream(self._k_e, iv, len(payload))
        ciphertext = bytes(p ^ k for p, k in zip(payload, ks, strict=True))

        # The 4-byte trailer: E flag set (this packet is encrypted) || 31-bit index.
        index_word = (_E_FLAG | index).to_bytes(_SRTCP_INDEX_LEN, "big")

        # Auth covers the entire equivalent RTCP packet + E + index (no ROC).
        authenticated = clear + ciphertext + index_word
        tag = _hmac_sha1(self._k_a, authenticated)[: self._tag_len]
        return authenticated + tag

    def unprotect(self, data: bytes) -> bytes:
        """Authenticate and decrypt an inbound SRTCP packet (RFC 3711 §3.4).

        Processing order (RFC 3711 §3.4): split off the auth tag and the E-flag +
        index trailer, verify the tag FIRST (constant time, over the whole packet
        through the index word), then enforce the per-SSRC binding, then the
        replay window over the explicit index, then — only if the E flag is set —
        decrypt the payload.  No receiver state (index window, SSRC binding) is
        mutated until auth has passed.

        Args:
            data: Raw SRTCP wire bytes.

        Returns:
            The decrypted compound RTCP datagram (header onwards).

        Raises:
            SrtcpError: On a malformed packet, an auth-tag mismatch, an SSRC
                mismatch, or replay detection.
        """
        tag_len = self._tag_len
        min_len = _SRTCP_CLEAR_PREFIX + _SRTCP_INDEX_LEN + tag_len
        if len(data) < min_len:
            msg = (
                "SRTCP packet too short for a header, index trailer, and auth tag "
                f"(need at least {min_len} octets, got {len(data)})"
            )
            raise SrtcpError(msg)

        # Layout: [authenticated = everything but the tag] || [tag].
        authenticated = data[: len(data) - tag_len]
        received_tag = data[len(data) - tag_len :]

        # Verify the auth tag FIRST (constant time). SRTCP appends NO ROC.
        expected_tag = _hmac_sha1(self._k_a, authenticated)[:tag_len]
        if not hmac.compare_digest(expected_tag, received_tag):
            msg = "SRTCP auth tag mismatch — packet rejected"
            raise SrtcpError(msg)

        # Split the authenticated region into [clear || ciphertext || index word].
        index_word = int.from_bytes(authenticated[-_SRTCP_INDEX_LEN:], "big")
        encrypted = bool(index_word & _E_FLAG)
        index = index_word & _SRTCP_INDEX_MASK

        clear = authenticated[:_SRTCP_CLEAR_PREFIX]
        ciphertext = authenticated[_SRTCP_CLEAR_PREFIX:-_SRTCP_INDEX_LEN]

        ssrc = int.from_bytes(clear[4:_SRTCP_CLEAR_PREFIX], "big")
        # Enforce the per-SSRC binding after auth, before any replay mutation.
        self._bind_ssrc(ssrc)

        # Replay check over the explicit SRTCP index (after auth).
        self._check_replay(index)

        if encrypted:
            iv = _srtcp_packet_iv(self._k_s, ssrc, index)
            ks = _aes_cm_keystream(self._k_e, iv, len(ciphertext))
            payload = bytes(c ^ k for c, k in zip(ciphertext, ks, strict=True))
        else:
            # E=0: the payload was sent in the clear (RFC 3711 §3.4 permits this).
            payload = ciphertext

        # Record the index in the replay window only after a full successful parse.
        self._update_replay(index)

        return clear + payload

    # ------------------------------------------------------------------
    # Replay-window management over the explicit SRTCP index
    # ------------------------------------------------------------------

    def _check_replay(self, index: int) -> None:
        """Check the replay window for ``index`` (RFC 3711 §3.3.2, §3.4).

        The window covers the last ``_REPLAY_WINDOW`` (64) SRTCP indices. An index
        at or below ``top - window`` is rejected as too old; an index already
        marked in the window bitmask is rejected as a replay.

        Args:
            index: The 31-bit SRTCP index carried in the packet trailer.

        Raises:
            SrtcpError: If the packet is a replay or too old.
        """
        if self._recv_top < 0:
            return  # no packet accepted yet

        diff = self._recv_top - index
        if diff >= _REPLAY_WINDOW:
            msg = "SRTCP replay detected: index is outside the replay window"
            raise SrtcpError(msg)
        if diff >= 0:
            bit = 1 << (diff % _REPLAY_WINDOW)
            if self._replay_mask & bit:
                msg = "SRTCP replay detected: index already received"
                raise SrtcpError(msg)

    def _update_replay(self, index: int) -> None:
        """Record ``index`` in the replay window after a successful unprotect.

        Args:
            index: The 31-bit SRTCP index just accepted.
        """
        if self._recv_top < 0 or index > self._recv_top:
            shift = index - self._recv_top if self._recv_top >= 0 else 0
            self._replay_mask = ((self._replay_mask << shift) | 1) & _REPLAY_WINDOW_MASK
            self._recv_top = index
        else:
            diff = self._recv_top - index
            self._replay_mask |= 1 << (diff % _REPLAY_WINDOW)
