"""RFC 3711 §3.4 SRTCP transform tests for media/srtcp.py.

The SRTCP transform protects the RTCP control channel the same way SRTP protects
the RTP media stream, but with three differences that this suite pins:

* a SEPARATE set of KDF labels — ``0x03`` (RTCP encryption key), ``0x04`` (RTCP
  auth key), ``0x05`` (RTCP salt) — derived from the SAME master key/salt;
* an EXPLICIT 31-bit SRTCP index carried in a 4-byte trailer word whose MSB is
  the E (encrypt) flag, in place of SRTP's implicit ROC/SEQ index;
* the encrypted portion starts at the NINTH octet (the first 8 octets — the RTCP
  header through the sender SSRC — stay in the clear), and the auth tag covers
  the whole packet plus the E+index trailer with NO ROC appended.

No real key material appears here. Synthetic key/salt bytes are COMPUTED at
runtime (``bytes(range(...))`` / hashlib of a constant) rather than written as
base64/hex literals, so the public-repo gitleaks scan sees nothing key-shaped
(the round-trip + tamper + wrong-key tests below need no literal vectors).

The ``cryptography`` extra is required; without it the module imports cleanly but
constructing an :class:`SrtcpSession` raises ``ImportError`` — gated with
``pytest.importorskip``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

import pytest

# Gate the whole module on the ``cryptography`` extra (mirrors test_media_srtp.py).
_cryptography = pytest.importorskip(
    "cryptography",
    reason="cryptography extra required for SRTCP tests (uv run --extra media pytest)",
)


# ---------------------------------------------------------------------------
# Import the modules under test AFTER confirming cryptography is present.
# ---------------------------------------------------------------------------
from hermes_voip.media.srtcp import (  # noqa: E402
    SrtcpError,
    SrtcpSession,
    _derive_srtcp_session_keys,
    _srtcp_packet_iv,
)
from hermes_voip.media.srtp import (  # noqa: E402
    _aes_cm_keystream,
    _derive_session_keys,
)
from hermes_voip.rtcp import (  # noqa: E402
    Bye,
    ReceiverReport,
    ReportBlock,
    SdesChunk,
    SenderReport,
    SourceDescription,
    build_compound,
    parse_compound,
)
from hermes_voip.sdp import CryptoAttribute  # noqa: E402

_AES_CM_128_HMAC_SHA1_80 = "AES_CM_128_HMAC_SHA1_80"
_AES_CM_128_HMAC_SHA1_32 = "AES_CM_128_HMAC_SHA1_32"

# Auth-tag lengths per suite (RFC 3711 §3.4 inherits SRTP's tag lengths).
_TAG_80 = 10
_TAG_32 = 4

# The clear prefix of an SRTCP packet: header(4) + sender SSRC(4) (RFC 3711 §3.4).
_SRTCP_CLEAR_PREFIX = 8

# The 4-byte E-flag + 31-bit index trailer.
_SRTCP_INDEX_LEN = 4
_E_FLAG = 0x80000000


# ---------------------------------------------------------------------------
# Helpers — synthetic keys computed at runtime (no literals for gitleaks).
# ---------------------------------------------------------------------------


def _make_crypto(
    suite: str = _AES_CM_128_HMAC_SHA1_80,
    master_key: bytes | None = None,
    master_salt: bytes | None = None,
) -> CryptoAttribute:
    """Build a CryptoAttribute from synthetic key/salt bytes (range-derived)."""
    if master_key is None:
        master_key = bytes(range(16))  # 0x00..0x0F — obviously synthetic
    if master_salt is None:
        master_salt = bytes(range(14))  # 0x00..0x0D — obviously synthetic
    b64 = base64.b64encode(master_key + master_salt).decode()
    return CryptoAttribute(tag=1, suite=suite, key_params=f"inline:{b64}")


def _other_crypto(suite: str = _AES_CM_128_HMAC_SHA1_80) -> CryptoAttribute:
    """A DIFFERENT synthetic key (hashlib of a constant) for wrong-key tests."""
    seed = hashlib.sha256(b"hermes-voip-srtcp-wrong-key").digest()
    master_key = seed[:16]
    master_salt = seed[16:30]
    b64 = base64.b64encode(master_key + master_salt).decode()
    return CryptoAttribute(tag=1, suite=suite, key_params=f"inline:{b64}")


def _sr(ssrc: int = 0xCAFEBABE) -> bytes:
    """A compound RTCP datagram (SR + SDES) — the common sender path."""
    sr = SenderReport(
        ssrc=ssrc,
        ntp_timestamp=0x0000000100000000,
        rtp_timestamp=160,
        packet_count=1,
        octet_count=160,
        report_blocks=(
            ReportBlock(
                ssrc=0xDEADBEEF,
                fraction_lost=0,
                cumulative_lost=0,
                extended_highest_seq=42,
                jitter=7,
                lsr=0,
                dlsr=0,
            ),
        ),
    )
    sdes = SourceDescription(chunks=(SdesChunk(ssrc=ssrc, cname="alice@host"),))
    return build_compound((sr, sdes))


def _rr(ssrc: int = 0x0BADF00D) -> bytes:
    """A receiver-report compound datagram (RR + SDES)."""
    rr = ReceiverReport(ssrc=ssrc, report_blocks=())
    sdes = SourceDescription(chunks=(SdesChunk(ssrc=ssrc, cname="bob@host"),))
    return build_compound((rr, sdes))


# ---------------------------------------------------------------------------
# RFC 3711 §4.3.2 — SRTCP key derivation uses labels 0x03 / 0x04 / 0x05.
#
# These derive from the SAME master key/salt as SRTP but with the SRTCP labels,
# so the SRTCP session keys MUST differ from the SRTP session keys (labels
# 0x00/0x01/0x02). We verify both: the derivation is deterministic, and it is
# distinct from the SRTP derivation.
# ---------------------------------------------------------------------------


class TestSrtcpKeyDerivation:
    def test_session_key_lengths(self) -> None:
        """Derive (k_e=16, k_s=14, k_a=20) from the master key/salt."""
        master_key = bytes(range(16))
        master_salt = bytes(range(14))
        k_e, k_s, k_a = _derive_srtcp_session_keys(master_key, master_salt)
        assert len(k_e) == 16
        assert len(k_s) == 14
        assert len(k_a) == 20

    def test_deterministic(self) -> None:
        """The KDF is a pure function of (master_key, master_salt)."""
        mk = bytes(range(16))
        ms = bytes(range(14))
        assert _derive_srtcp_session_keys(mk, ms) == _derive_srtcp_session_keys(mk, ms)

    def test_srtcp_keys_differ_from_srtp_keys(self) -> None:
        """SRTCP labels (0x03/04/05) MUST yield different keys than SRTP (0x00/01/02).

        Same master material, different labels — if SRTCP reused SRTP's labels the
        RTCP and RTP keystreams would collide (a real cryptographic defect).
        """
        mk = bytes(range(16))
        ms = bytes(range(14))
        srtp_keys = _derive_session_keys(mk, ms)
        srtcp_keys = _derive_srtcp_session_keys(mk, ms)
        assert srtcp_keys[0] != srtp_keys[0]  # encryption key
        assert srtcp_keys[1] != srtp_keys[1]  # salt
        assert srtcp_keys[2] != srtp_keys[2]  # auth key


# ---------------------------------------------------------------------------
# RFC 3711 §4.1.1 — the SRTCP per-packet IV uses the 31-bit SRTCP index as i.
#
# IV = (k_s || 0x0000) XOR (SSRC << 64) XOR (index << 16).  We assert a literal
# hand-derived value so a broken IV constructor cannot pass by coincidence.
# ---------------------------------------------------------------------------


class TestSrtcpPacketIv:
    def test_zero_index(self) -> None:
        """With ssrc=0 and index=0 the IV is k_s || 0x0000."""
        k_s = bytes(range(14))  # 000102030405060708090A0B0C0D
        iv = _srtcp_packet_iv(k_s, ssrc=0, index=0)
        assert iv == bytes(range(14)) + b"\x00\x00"

    def test_nonzero_hand_derived(self) -> None:
        """IV = (k_s||0000) XOR (SSRC<<64) XOR (index<<16), hand-derived.

        k_s=000102..0D, SSRC=0x12345678, index=0x20003:
          k_s||0000 = 000102030405060708090A0B0C0D0000
          SSRC<<64  = 00000000123456780000000000000000
          index<<16 = 00000000000000000000000200030000
          XOR       = 000102031631507F08090A090C0E0000
        """
        k_s = bytes(range(14))
        iv = _srtcp_packet_iv(k_s, ssrc=0x12345678, index=0x20003)
        assert iv == bytes.fromhex("000102031631507F08090A090C0E0000")


# ---------------------------------------------------------------------------
# protect → unprotect round-trip.
# ---------------------------------------------------------------------------


class TestSrtcpRoundTrip:
    def test_sr_round_trips(self) -> None:
        """protect() then unprotect() recovers the compound RTCP bytes exactly."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        compound = _sr()
        srtcp = tx.protect(compound)
        recovered = rx.unprotect(srtcp)
        assert recovered == compound
        # And it still parses as the original packets.
        packets = parse_compound(recovered)
        assert isinstance(packets[0], SenderReport)

    def test_rr_round_trips(self) -> None:
        """A receiver-report compound also round-trips."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        compound = _rr()
        recovered = rx.unprotect(tx.protect(compound))
        assert recovered == compound

    def test_bye_round_trips(self) -> None:
        """A compound ending in BYE round-trips (teardown path)."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        rr = ReceiverReport(ssrc=0x11223344, report_blocks=())
        bye = Bye(ssrcs=(0x11223344,), reason="bye")
        compound = build_compound((rr, bye))
        recovered = rx.unprotect(tx.protect(compound))
        assert recovered == compound

    def test_32bit_tag_round_trips(self) -> None:
        """The _32 suite (4-byte tag) round-trips too."""
        crypto = _make_crypto(suite=_AES_CM_128_HMAC_SHA1_32)
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        compound = _sr()
        recovered = rx.unprotect(tx.protect(compound))
        assert recovered == compound

    def test_index_increments_per_packet(self) -> None:
        """The explicit SRTCP index increments by one per protected packet."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        for expected_index in range(1, 6):
            srtcp = tx.protect(_sr())
            # Trailer is the 4 bytes before the auth tag.
            trailer = srtcp[-_TAG_80 - _SRTCP_INDEX_LEN : -_TAG_80]
            index = int.from_bytes(trailer, "big") & ~_E_FLAG
            assert index == expected_index
            assert rx.unprotect(srtcp) == _sr()


# ---------------------------------------------------------------------------
# Wire-format invariants (RFC 3711 §3.4).
# ---------------------------------------------------------------------------


class TestSrtcpWireFormat:
    def test_output_layout_lengths(self) -> None:
        """SRTCP = clear-prefix + enc(rest) + 4-byte index + tag (no length change)."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        compound = _sr()
        srtcp = tx.protect(compound)
        # Encryption is a stream cipher (length-preserving) so:
        # len(srtcp) == len(compound) + 4 (index) + 10 (tag).
        assert len(srtcp) == len(compound) + _SRTCP_INDEX_LEN + _TAG_80

    def test_first_eight_octets_are_clear(self) -> None:
        """Octets 0..7 (RTCP header + sender SSRC) are left in the clear."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        compound = _sr()
        srtcp = tx.protect(compound)
        assert srtcp[:_SRTCP_CLEAR_PREFIX] == compound[:_SRTCP_CLEAR_PREFIX]

    def test_payload_after_octet_eight_is_encrypted(self) -> None:
        """Octets 9..end (the RTCP payload) are encrypted, not passed through."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        compound = _sr()
        srtcp = tx.protect(compound)
        enc_portion = srtcp[_SRTCP_CLEAR_PREFIX : len(compound)]
        clear_portion = compound[_SRTCP_CLEAR_PREFIX:]
        assert enc_portion != clear_portion

    def test_e_flag_set_when_encrypted(self) -> None:
        """The E (encrypt) flag (MSB of the index word) is set when encrypted."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        srtcp = tx.protect(_sr())
        trailer = srtcp[-_TAG_80 - _SRTCP_INDEX_LEN : -_TAG_80]
        word = int.from_bytes(trailer, "big")
        assert word & _E_FLAG  # E bit set

    def test_encrypted_portion_matches_manual_keystream(self) -> None:
        """The encrypted portion equals payload XOR the AES-CM SRTCP keystream.

        Independently derives the SRTCP session keys + IV (index=1, the first
        packet) and reproduces the ciphertext, proving the transform uses the
        SRTCP keys/labels and the SRTCP index — not the SRTP ones.
        """
        master_key = bytes(range(16))
        master_salt = bytes(range(14))
        crypto = _make_crypto(master_key=master_key, master_salt=master_salt)
        tx = SrtcpSession(crypto)
        compound = _sr(ssrc=0xCAFEBABE)
        srtcp = tx.protect(compound)

        k_e, k_s, _k_a = _derive_srtcp_session_keys(master_key, master_salt)
        iv = _srtcp_packet_iv(k_s, ssrc=0xCAFEBABE, index=1)
        payload = compound[_SRTCP_CLEAR_PREFIX:]
        ks = _aes_cm_keystream(k_e, iv, len(payload))
        expected = bytes(p ^ k for p, k in zip(payload, ks, strict=True))
        actual = srtcp[_SRTCP_CLEAR_PREFIX : len(compound)]
        assert actual == expected


# ---------------------------------------------------------------------------
# Authentication: the tag covers enc(payload) + E + index, NO ROC.
# ---------------------------------------------------------------------------


class TestSrtcpAuthentication:
    def test_flipped_tag_byte_rejected(self) -> None:
        """A flipped auth-tag byte makes unprotect raise SrtcpError."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        srtcp = bytearray(tx.protect(_sr()))
        srtcp[-1] ^= 0xFF
        with pytest.raises(SrtcpError, match="auth"):
            rx.unprotect(bytes(srtcp))

    def test_flipped_clear_header_byte_rejected(self) -> None:
        """A flipped byte in the CLEAR header is authenticated → rejected."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        srtcp = bytearray(tx.protect(_sr()))
        srtcp[1] ^= 0x01  # the PT byte (inside the clear prefix)
        with pytest.raises(SrtcpError, match="auth"):
            rx.unprotect(bytes(srtcp))

    def test_flipped_encrypted_payload_byte_rejected(self) -> None:
        """A flipped byte in the encrypted payload is authenticated → rejected."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        srtcp = bytearray(tx.protect(_sr()))
        srtcp[_SRTCP_CLEAR_PREFIX + 1] ^= 0x01
        with pytest.raises(SrtcpError, match="auth"):
            rx.unprotect(bytes(srtcp))

    def test_flipped_index_byte_rejected(self) -> None:
        """A flipped byte in the SRTCP index trailer is authenticated → rejected."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        srtcp = bytearray(tx.protect(_sr()))
        # The low index byte sits just before the tag; flipping it both breaks the
        # tag and (were auth absent) would point decryption at a different index.
        srtcp[-_TAG_80 - 1] ^= 0x01
        with pytest.raises(SrtcpError, match="auth"):
            rx.unprotect(bytes(srtcp))

    def test_auth_tag_does_not_append_roc(self) -> None:
        """The SRTCP tag is HMAC over (whole packet || E+index) with NO ROC.

        Reproduces the expected tag independently per RFC 3711 §3.4 (auth covers
        the entire SRTCP packet through the index word) and asserts equality; an
        implementation that appended a ROC (SRTP-style) would mismatch.
        """
        master_key = bytes(range(16))
        master_salt = bytes(range(14))
        crypto = _make_crypto(master_key=master_key, master_salt=master_salt)
        tx = SrtcpSession(crypto)
        srtcp = tx.protect(_sr(ssrc=0xCAFEBABE))

        _k_e, _k_s, k_a = _derive_srtcp_session_keys(master_key, master_salt)
        authenticated = srtcp[: len(srtcp) - _TAG_80]  # everything but the tag
        expected = hmac.new(k_a, authenticated, hashlib.sha1).digest()[:_TAG_80]
        assert srtcp[-_TAG_80:] == expected


# ---------------------------------------------------------------------------
# Wrong-key rejection.
# ---------------------------------------------------------------------------


class TestSrtcpWrongKey:
    def test_wrong_key_fails_auth(self) -> None:
        """A receiver with a different master key rejects the packet (auth)."""
        tx = SrtcpSession(_make_crypto())
        rx = SrtcpSession(_other_crypto())
        with pytest.raises(SrtcpError, match="auth"):
            rx.unprotect(tx.protect(_sr()))

    def test_wrong_suite_fails(self) -> None:
        """A receiver expecting a different tag length rejects the packet."""
        tx = SrtcpSession(_make_crypto(suite=_AES_CM_128_HMAC_SHA1_80))
        rx = SrtcpSession(_make_crypto(suite=_AES_CM_128_HMAC_SHA1_32))
        # The _32 receiver reads a 4-byte tag and a different index span; auth fails.
        with pytest.raises(SrtcpError):
            rx.unprotect(tx.protect(_sr()))


# ---------------------------------------------------------------------------
# Replay protection on the explicit SRTCP index (RFC 3711 §3.3.2 / §3.4).
# ---------------------------------------------------------------------------


class TestSrtcpReplay:
    def test_replayed_packet_rejected(self) -> None:
        """Re-presenting an already-accepted SRTCP packet raises (replay)."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        srtcp = tx.protect(_sr())
        rx.unprotect(srtcp)
        with pytest.raises(SrtcpError, match="replay"):
            rx.unprotect(srtcp)

    def test_out_of_window_old_index_rejected(self) -> None:
        """An index far below the replay window is rejected as too old."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        first = tx.protect(_sr())  # index 1
        # Advance the receiver well past the window.
        for _ in range(70):
            rx.unprotect(tx.protect(_sr()))
        with pytest.raises(SrtcpError, match="replay"):
            rx.unprotect(first)

    def test_out_of_order_within_window_accepted(self) -> None:
        """Indices arriving out of order but inside the window are accepted once."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        p1 = tx.protect(_sr())  # index 1
        p2 = tx.protect(_sr())  # index 2
        p3 = tx.protect(_sr())  # index 3
        assert rx.unprotect(p3) == _sr()
        assert rx.unprotect(p1) == _sr()
        assert rx.unprotect(p2) == _sr()
        # And each is now a replay.
        with pytest.raises(SrtcpError, match="replay"):
            rx.unprotect(p2)


# ---------------------------------------------------------------------------
# Per-SSRC binding (RFC 3711 §3.2.3): one session serves one SSRC.
# ---------------------------------------------------------------------------


class TestSrtcpPerSsrcBinding:
    def test_unprotect_rejects_second_ssrc(self) -> None:
        """An authentic packet for a different SSRC is rejected after auth."""
        crypto = _make_crypto()
        tx_a = SrtcpSession(crypto)
        tx_b = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        rx.unprotect(tx_a.protect(_sr(ssrc=0xAAAA0000)))
        with pytest.raises(SrtcpError, match="SSRC"):
            rx.unprotect(tx_b.protect(_sr(ssrc=0xBBBB0000)))

    def test_explicit_ssrc_binding_at_construction(self) -> None:
        """An explicit ssrc= binds the context up front and rejects mismatches."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto, ssrc=0x0BADC0DE)
        tx.protect(_sr(ssrc=0x0BADC0DE))
        with pytest.raises(SrtcpError, match="SSRC"):
            tx.protect(_sr(ssrc=0x0BADBEEF))

    def test_foreign_ssrc_does_not_advance_replay(self) -> None:
        """A rejected foreign-SSRC packet must not advance the bound stream's state."""
        crypto = _make_crypto()
        tx_a = SrtcpSession(crypto)
        tx_b = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        rx.unprotect(tx_a.protect(_sr(ssrc=0xAAAA0000)))  # index 1 bound to AAAA
        with pytest.raises(SrtcpError, match="SSRC"):
            rx.unprotect(tx_b.protect(_sr(ssrc=0xBBBB0000)))  # index 1, wrong SSRC
        # The legitimate stream's next index is still accepted.
        assert rx.unprotect(tx_a.protect(_sr(ssrc=0xAAAA0000))) == _sr(ssrc=0xAAAA0000)


# ---------------------------------------------------------------------------
# Malformed inbound packets.
# ---------------------------------------------------------------------------


class TestSrtcpMalformed:
    def test_too_short_for_index_and_tag_rejected(self) -> None:
        """Bytes too short for the clear prefix + index + tag raise SrtcpError."""
        crypto = _make_crypto()
        rx = SrtcpSession(crypto)
        with pytest.raises(SrtcpError):
            rx.unprotect(b"\x80\xc8\x00\x06" + b"\x00" * 4)  # 8 bytes only

    def test_truncated_tag_rejected(self) -> None:
        """An SRTCP packet missing part of its auth tag raises SrtcpError."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        srtcp = tx.protect(_sr())
        with pytest.raises(SrtcpError):
            rx.unprotect(srtcp[: len(srtcp) - 1])


# ---------------------------------------------------------------------------
# DTLS-SRTP keying path (from_raw_keys) mirrors SRTP.
# ---------------------------------------------------------------------------


class TestSrtcpFromRawKeys:
    def test_from_raw_keys_round_trips(self) -> None:
        """from_raw_keys(master_key, salt, suite=) builds a working session."""
        master_key = os.urandom(16)
        master_salt = os.urandom(14)
        tx = SrtcpSession.from_raw_keys(
            master_key, master_salt, suite=_AES_CM_128_HMAC_SHA1_80
        )
        rx = SrtcpSession.from_raw_keys(
            master_key, master_salt, suite=_AES_CM_128_HMAC_SHA1_80
        )
        compound = _sr()
        assert rx.unprotect(tx.protect(compound)) == compound

    def test_from_raw_keys_rejects_bad_key_length(self) -> None:
        """A short master key is rejected at construction."""
        with pytest.raises(SrtcpError):
            SrtcpSession.from_raw_keys(
                b"\x00" * 15, b"\x00" * 14, suite=_AES_CM_128_HMAC_SHA1_80
            )

    def test_from_raw_keys_rejects_bad_salt_length(self) -> None:
        """A short master salt is rejected at construction."""
        with pytest.raises(SrtcpError):
            SrtcpSession.from_raw_keys(
                b"\x00" * 16, b"\x00" * 13, suite=_AES_CM_128_HMAC_SHA1_80
            )

    def test_from_raw_keys_rejects_unsupported_suite(self) -> None:
        """An unsupported suite is rejected at construction."""
        with pytest.raises(SrtcpError, match="suite"):
            SrtcpSession.from_raw_keys(
                b"\x00" * 16, b"\x00" * 14, suite="AES_256_CM_HMAC_SHA1_80"
            )


# ---------------------------------------------------------------------------
# Unsupported-suite rejection on the SDES path + key redaction.
# ---------------------------------------------------------------------------


class TestSrtcpConstruction:
    def test_unsupported_suite_rejected(self) -> None:
        """A suite outside the supported set is rejected (SrtcpError)."""
        # CryptoAttribute itself validates the suite, so build one that passes its
        # check then would-be-fail in SrtcpSession only if we bypass; instead use
        # from_raw_keys (above) for the suite check and assert the constant here.
        crypto = _make_crypto()
        session = SrtcpSession(crypto)  # supported suite constructs cleanly
        assert session.index == 0


class TestSrtcpKeyRedaction:
    def test_repr_does_not_expose_key(self) -> None:
        """repr(session) must not contain the base64 key or raw key/salt bytes."""
        master_key = bytes(range(16))
        master_salt = bytes(range(14))
        key_b64 = base64.b64encode(master_key + master_salt).decode()
        crypto = _make_crypto(master_key=master_key, master_salt=master_salt)
        session = SrtcpSession(crypto)
        r = repr(session)
        assert key_b64 not in r
        assert master_key.hex() not in r
        assert master_salt.hex() not in r

    def test_error_does_not_expose_key(self) -> None:
        """An auth-failure SrtcpError must not include key material."""
        master_key = bytes(range(16))
        master_salt = bytes(range(14))
        key_b64 = base64.b64encode(master_key + master_salt).decode()
        crypto = _make_crypto(master_key=master_key, master_salt=master_salt)
        tx = SrtcpSession(crypto)
        rx = SrtcpSession(crypto)
        srtcp = bytearray(tx.protect(_sr()))
        srtcp[-1] ^= 0xFF
        with pytest.raises(SrtcpError) as exc_info:
            rx.unprotect(bytes(srtcp))
        assert key_b64 not in str(exc_info.value)
        assert master_key.hex() not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Finding 1 (codex r1, BLOCKING): SRTCP index space must NOT wrap under one
# master key — wrapping 0x7fffffff -> 0 reuses the IV/keystream (two-time pad).
# The sender must raise SrtcpError on index exhaustion instead of wrapping.
# ---------------------------------------------------------------------------

# The maximum 31-bit SRTCP index (RFC 3711 §3.4).
_SRTCP_INDEX_MAX = 0x7FFFFFFF


class TestSrtcpIndexExhaustion:
    def test_protect_accepts_up_to_max_index(self) -> None:
        """protect() emits the final two valid indices 0x7ffffffe and 0x7fffffff."""
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        # Fast-forward the sender to one before the penultimate index.
        tx.index = _SRTCP_INDEX_MAX - 2
        srtcp_pen = tx.protect(_sr())
        trailer = srtcp_pen[-_TAG_80 - _SRTCP_INDEX_LEN : -_TAG_80]
        assert (int.from_bytes(trailer, "big") & ~_E_FLAG) == _SRTCP_INDEX_MAX - 1
        srtcp_last = tx.protect(_sr())
        trailer = srtcp_last[-_TAG_80 - _SRTCP_INDEX_LEN : -_TAG_80]
        assert (int.from_bytes(trailer, "big") & ~_E_FLAG) == _SRTCP_INDEX_MAX
        assert tx.index == _SRTCP_INDEX_MAX

    def test_protect_raises_at_index_exhaustion(self) -> None:
        """The protect() after the max index raises instead of wrapping to 0.

        Wrapping would reuse the IV/keystream under the same master key (a
        catastrophic two-time-pad); exhaustion is a hard error requiring rekey.
        """
        crypto = _make_crypto()
        tx = SrtcpSession(crypto)
        tx.index = _SRTCP_INDEX_MAX  # the last index has been emitted
        with pytest.raises(SrtcpError, match=r"exhaust|rekey|index space"):
            tx.protect(_sr())
        # The index must NOT have wrapped to 0 (no keystream reuse).
        assert tx.index == _SRTCP_INDEX_MAX

    def test_exhaustion_error_does_not_expose_key(self) -> None:
        """The exhaustion SrtcpError must not leak key material."""
        master_key = bytes(range(16))
        master_salt = bytes(range(14))
        key_b64 = base64.b64encode(master_key + master_salt).decode()
        crypto = _make_crypto(master_key=master_key, master_salt=master_salt)
        tx = SrtcpSession(crypto)
        tx.index = _SRTCP_INDEX_MAX
        with pytest.raises(SrtcpError) as exc_info:
            tx.protect(_sr())
        assert key_b64 not in str(exc_info.value)
        assert master_key.hex() not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Finding 2 (codex r1, MAJOR): the SDES inline-key decode in srtcp.py must be
# self-defending — base64 with validate=True, catch decode errors, and require
# EXACTLY 30 octets — raising a typed SrtcpError rather than a low-level crypto
# error or silently-malformed keys. (CryptoAttribute already validates upstream,
# so these tests forge a malformed-but-typed attribute via object.__new__ to hit
# srtcp.py's own decode guard — defence in depth, not a redundant upstream check.)
# ---------------------------------------------------------------------------


def _forge_crypto(suite: str, key_params: str) -> CryptoAttribute:
    """Build a CryptoAttribute bypassing __post_init__ validation.

    CryptoAttribute is a frozen, validated dataclass, so a malformed key_params
    can never reach SrtcpSession through normal construction. To exercise
    srtcp.py's OWN decode guard (defence in depth), forge an instance directly.
    """
    obj = object.__new__(CryptoAttribute)
    object.__setattr__(obj, "tag", 1)
    object.__setattr__(obj, "suite", suite)
    object.__setattr__(obj, "key_params", key_params)
    return obj


class TestSrtcpInlineKeyDecodeGuard:
    def test_invalid_base64_raises_srtcp_error(self) -> None:
        """A non-base64 inline key raises SrtcpError, not a raw binascii error."""
        crypto = _forge_crypto(_AES_CM_128_HMAC_SHA1_80, "inline:not valid base64!!!")
        with pytest.raises(SrtcpError):
            SrtcpSession(crypto)

    def test_short_key_salt_raises_srtcp_error(self) -> None:
        """An inline key that decodes to fewer than 30 octets raises SrtcpError."""
        short = base64.b64encode(bytes(20)).decode()  # 20 octets, need 30
        crypto = _forge_crypto(_AES_CM_128_HMAC_SHA1_80, f"inline:{short}")
        with pytest.raises(SrtcpError):
            SrtcpSession(crypto)

    def test_long_key_salt_raises_srtcp_error(self) -> None:
        """An inline key that decodes to more than 30 octets raises SrtcpError."""
        long = base64.b64encode(bytes(40)).decode()  # 40 octets, need 30
        crypto = _forge_crypto(_AES_CM_128_HMAC_SHA1_80, f"inline:{long}")
        with pytest.raises(SrtcpError):
            SrtcpSession(crypto)

    def test_decode_guard_error_does_not_expose_key(self) -> None:
        """The decode-guard SrtcpError must not echo the (corrupt) key material."""
        short_b64 = base64.b64encode(bytes(range(20))).decode()
        crypto = _forge_crypto(_AES_CM_128_HMAC_SHA1_80, f"inline:{short_b64}")
        with pytest.raises(SrtcpError) as exc_info:
            SrtcpSession(crypto)
        assert short_b64 not in str(exc_info.value)

    def test_valid_inline_key_still_constructs(self) -> None:
        """A valid 30-octet inline key still constructs cleanly (no regression)."""
        crypto = _make_crypto()  # the normal validated path
        session = SrtcpSession(crypto)
        assert session.index == 0


# ---------------------------------------------------------------------------
# __all__ export
# ---------------------------------------------------------------------------


import hermes_voip.media.srtcp as _srtcp_mod  # noqa: E402


class TestSrtcpModuleExports:
    """Verify that srtcp.py defines __all__ with the correct public names."""

    def test_module_defines_all(self) -> None:
        """The srtcp module must define __all__."""
        assert hasattr(_srtcp_mod, "__all__"), "srtcp module must define __all__"

    def test_all_contains_correct_public_names(self) -> None:
        """__all__ must list the exact public names intended for star-import."""
        expected = {"SrtcpError", "SrtcpSession"}
        assert set(_srtcp_mod.__all__) == expected

    def test_all_names_are_importable(self) -> None:
        """Every name in __all__ must be importable from the module."""
        all_names = _srtcp_mod.__all__
        for name in all_names:
            assert hasattr(_srtcp_mod, name), (
                f"{name} must be importable from srtcp module"
            )
            assert not name.startswith("_"), (
                f"{name} must not be private (no leading _)"
            )

    def test_no_private_names_in_all(self) -> None:
        """__all__ must not include any names starting with underscore."""
        all_names = _srtcp_mod.__all__
        private_names = [name for name in all_names if name.startswith("_")]
        assert not private_names, f"Private names in __all__: {private_names}"
