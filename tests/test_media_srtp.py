"""RFC 3711 SRTP known-answer tests and unit tests for media/srtp.py.

Known-answer test (KAT) vectors sourced from:

  RFC 3711 Appendix B (https://www.rfc-editor.org/rfc/rfc3711#appendix-B):
    B.2: AES-CM keystream test vector
    B.3: AES-CM key-derivation test vector

  NIST SP 800-38A Section F.5.1 (AES-128 CTR mode) confirms the B.2 keystream
  because RFC 3711 B.2 re-uses the same key/IV pair; the session salt shown in B.2
  (F8F9...) is the SDES wire value and the tested IV (F0F1...) is the derived one.

All key/salt/IV values below are synthetic test data from published standards and
are NOT real keys — they are reproduced here for correctness verification only.

The ``cryptography`` extra is required for these tests.  Without it, the srtp module
imports cleanly but constructing an SrtpSession raises ImportError; we use
``pytest.importorskip`` to gate the suite.
"""

from __future__ import annotations

import base64
import os

import pytest

# Gate the entire module on the ``cryptography`` extra.  Without it, the module
# imports cleanly but SrtpSession construction will raise ImportError.
_cryptography = pytest.importorskip(
    "cryptography",
    reason="cryptography extra required for SRTP tests (uv run --extra media pytest)",
)


# ---------------------------------------------------------------------------
# Import the modules under test AFTER confirming cryptography is present.
# ---------------------------------------------------------------------------
from hermes_voip.media.srtp import (  # noqa: E402
    SrtpError,
    SrtpSession,
    _aes_cm_keystream,
    _derive_session_keys,
)
from hermes_voip.rtp import RtpPacket  # noqa: E402
from hermes_voip.sdp import CryptoAttribute, SdpError  # noqa: E402

# ---------------------------------------------------------------------------
# RFC 3711 §B.2 — AES-CM keystream known-answer test vector
#
# Source: RFC 3711 Appendix B.2 / NIST SP 800-38A §F.5.1.
# These are published RFC/NIST test vectors and are NOT real keys.
#
# Session encryption key (128 bits):
#   2B7E151628AED2A6ABF7158809CF4F3C
#
# IV (128 bits, AES-CTR initial counter):
#   F0F1F2F3F4F5F6F7F8F9FAFBFCFD0000
#   (Derived from the RFC 3711 §4.1.1 IV formula with k_s=F0F1...FCFD as 14-byte
#    session salt and packet index 0, SSRC 0, ROC 0, SEQ 0;
#    confirmed by NIST SP 800-38A §F.5.1 which uses identical key+counter.)
#
# First 48 bytes of keystream (Table B-2):
#   E03EAD0935C95E80E166B16DD92B4EB4
#   D23513162B02D0F72A43A2FE4A5F97AB
#   41E95B3BB0A2E8DD477901E4FCA894C0
# ---------------------------------------------------------------------------

_RFC3711_B2_SESSION_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")

# The IV is a 128-bit AES-CTR initial counter constructed from the 14-byte session
# salt with packet index 0 (SSRC=0, ROC=0, SEQ=0): IV = k_s << 16 = k_s || 0x0000.
# k_s = F0F1F2F3F4F5F6F7F8F9FAFBFCFD (14 bytes), so IV = k_s || 0x0000 (16 bytes).
# This matches the NIST SP 800-38A F.5.1 counter value and reproduces the RFC keystream.
_RFC3711_B2_IV = bytes.fromhex("F0F1F2F3F4F5F6F7F8F9FAFBFCFD0000")

_RFC3711_B2_KEYSTREAM = bytes.fromhex(
    "E03EAD0935C95E80E166B16DD92B4EB4"
    "D23513162B02D0F72A43A2FE4A5F97AB"
    "41E95B3BB0A2E8DD477901E4FCA894C0"
)

# ---------------------------------------------------------------------------
# RFC 3711 §B.3 — AES-CM key-derivation known-answer test vector
#
# Source: RFC 3711 Appendix B.3 (https://www.rfc-editor.org/rfc/rfc3711#appendix-B.3).
# These are published RFC test vectors and are NOT real keys.
#
# Master Key  (128 bits): E1F97A0D3E018BE0D64FA32C06DE4139
# Master Salt (112 bits): 0EC675AD498AFEEBB6960B3AABE6
# key_derivation_rate = 0  → r = 0 for all labels
#
# Derived session keys (Table B-3):
#   label 0x00 → Cipher Key   (k_e, 128 bits): C61E7A93744F39EE10734AFE3FF7A087
#   label 0x01 → Auth Key     (k_a, 160 bits): CEBE321F6FF7716B6FD4AB49AF256A156D38BAA4
#   label 0x02 → Salting Key  (k_s, 112 bits): 30CBBC08863D8C85D49DB34A9AE1
#
# KDF IV formula (RFC 3711 §4.3.1, verified against the above vectors):
#   IV = (label * 2^64) XOR (master_salt || 0x0000)
# where master_salt || 0x0000 is the 14-byte salt right-padded to 16 bytes.
# ---------------------------------------------------------------------------

_RFC3711_B3_MASTER_KEY = bytes.fromhex("E1F97A0D3E018BE0D64FA32C06DE4139")
_RFC3711_B3_MASTER_SALT = bytes.fromhex("0EC675AD498AFEEBB6960B3AABE6")

_RFC3711_B3_SESSION_ENCRYPTION_KEY = bytes.fromhex("C61E7A93744F39EE10734AFE3FF7A087")
_RFC3711_B3_SESSION_AUTH_KEY = bytes.fromhex("CEBE321F6FF7716B6FD4AB49AF256A156D38BAA4")
_RFC3711_B3_SESSION_SALT = bytes.fromhex("30CBBC08863D8C85D49DB34A9AE1")


# ---------------------------------------------------------------------------
# Helper: build a CryptoAttribute from raw bytes.
#
# We construct the base64 key||salt from bytes(range(...)) to avoid placing
# high-entropy base64 literals in the test file (which would trip gitleaks'
# generic-api-key rule — the entropy scanner fires on any base64 string that
# looks like a key).  bytes(range(N)) is obviously synthetic and non-secret.
# ---------------------------------------------------------------------------


def _make_crypto(
    suite: str = "AES_CM_128_HMAC_SHA1_80",
    master_key: bytes | None = None,
    master_salt: bytes | None = None,
) -> CryptoAttribute:
    """Build a CryptoAttribute from raw key/salt bytes."""
    if master_key is None:
        master_key = bytes(range(16))  # 0x00..0x0F — obviously synthetic
    if master_salt is None:
        master_salt = bytes(range(14))  # 0x00..0x0D — obviously synthetic
    key_salt = master_key + master_salt  # 30 bytes
    b64 = base64.b64encode(key_salt).decode()
    return CryptoAttribute(tag=1, suite=suite, key_params=f"inline:{b64}")


def _make_crypto_from_bytes(
    suite: str, master_key: bytes, master_salt: bytes
) -> CryptoAttribute:
    """Build a CryptoAttribute directly from raw key and salt bytes."""
    key_salt = master_key + master_salt
    b64 = base64.b64encode(key_salt).decode()
    return CryptoAttribute(tag=1, suite=suite, key_params=f"inline:{b64}")


# ---------------------------------------------------------------------------
# RFC 3711 §B.2 — AES-CM keystream KAT
# ---------------------------------------------------------------------------


class TestAesCmKeystream:
    def test_rfc3711_b2_first_48_bytes(self) -> None:
        """RFC 3711 §B.2 / NIST §F.5.1: AES-CM must produce the exact keystream."""
        keystream = _aes_cm_keystream(
            key=_RFC3711_B2_SESSION_KEY,
            iv=_RFC3711_B2_IV,
            length=48,
        )
        assert keystream == _RFC3711_B2_KEYSTREAM, (
            f"AES-CM keystream mismatch.\n"
            f"Expected: {_RFC3711_B2_KEYSTREAM.hex()}\n"
            f"Got:      {keystream.hex()}"
        )

    def test_length_one_block(self) -> None:
        """Requesting exactly 16 bytes returns one AES block of keystream."""
        ks = _aes_cm_keystream(
            key=_RFC3711_B2_SESSION_KEY,
            iv=_RFC3711_B2_IV,
            length=16,
        )
        assert ks == _RFC3711_B2_KEYSTREAM[:16]

    def test_length_zero(self) -> None:
        """Zero-length keystream is empty bytes."""
        ks = _aes_cm_keystream(
            key=_RFC3711_B2_SESSION_KEY,
            iv=_RFC3711_B2_IV,
            length=0,
        )
        assert ks == b""

    def test_length_non_block_aligned(self) -> None:
        """Non-block-aligned length returns exactly the requested byte count."""
        ks = _aes_cm_keystream(
            key=_RFC3711_B2_SESSION_KEY,
            iv=_RFC3711_B2_IV,
            length=17,
        )
        assert len(ks) == 17
        assert ks == _RFC3711_B2_KEYSTREAM[:17]


# ---------------------------------------------------------------------------
# RFC 3711 §B.3 — key-derivation KAT
# ---------------------------------------------------------------------------


class TestKeyDerivation:
    def test_rfc3711_b3_encryption_key(self) -> None:
        """RFC 3711 §B.3: session encryption key must match the RFC test vector."""
        k_e, _k_s, _k_a = _derive_session_keys(
            master_key=_RFC3711_B3_MASTER_KEY,
            master_salt=_RFC3711_B3_MASTER_SALT,
        )
        assert k_e == _RFC3711_B3_SESSION_ENCRYPTION_KEY, (
            f"Session encryption key mismatch.\n"
            f"Expected: {_RFC3711_B3_SESSION_ENCRYPTION_KEY.hex()}\n"
            f"Got:      {k_e.hex()}"
        )

    def test_rfc3711_b3_salt_key(self) -> None:
        """RFC 3711 §B.3: session salt must match the RFC test vector."""
        _k_e, k_s, _k_a = _derive_session_keys(
            master_key=_RFC3711_B3_MASTER_KEY,
            master_salt=_RFC3711_B3_MASTER_SALT,
        )
        assert k_s == _RFC3711_B3_SESSION_SALT, (
            f"Session salt mismatch.\n"
            f"Expected: {_RFC3711_B3_SESSION_SALT.hex()}\n"
            f"Got:      {k_s.hex()}"
        )

    def test_rfc3711_b3_auth_key(self) -> None:
        """RFC 3711 §B.3: session auth key must match the RFC test vector."""
        _k_e, _k_s, k_a = _derive_session_keys(
            master_key=_RFC3711_B3_MASTER_KEY,
            master_salt=_RFC3711_B3_MASTER_SALT,
        )
        assert k_a == _RFC3711_B3_SESSION_AUTH_KEY, (
            f"Session auth key mismatch.\n"
            f"Expected: {_RFC3711_B3_SESSION_AUTH_KEY.hex()}\n"
            f"Got:      {k_a.hex()}"
        )


# ---------------------------------------------------------------------------
# SrtpSession: protect → unprotect round-trip
# ---------------------------------------------------------------------------


def _make_packet(
    seq: int = 1,
    ssrc: int = 0xDEADBEEF,
    timestamp: int = 160,
    payload: bytes = b"\xaa" * 160,
) -> RtpPacket:
    return RtpPacket(
        payload_type=0,
        sequence_number=seq,
        timestamp=timestamp,
        ssrc=ssrc,
        payload=payload,
    )


class TestSrtpSessionRoundTrip:
    def test_protect_unprotect_recovers_plaintext(self) -> None:
        """protect() then unprotect() must recover the original payload exactly."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        packet = _make_packet(payload=bytes(range(160)))
        ciphertext = tx.protect(packet)
        recovered = rx.unprotect(ciphertext)
        assert recovered.payload == packet.payload
        assert recovered.sequence_number == packet.sequence_number
        assert recovered.ssrc == packet.ssrc
        assert recovered.timestamp == packet.timestamp

    def test_protect_output_longer_than_rtp_pack(self) -> None:
        """protect() appends auth tag; SRTP output must be longer than plain RTP."""
        crypto = _make_crypto(suite="AES_CM_128_HMAC_SHA1_80")
        tx = SrtpSession(crypto)
        packet = _make_packet()
        srtp_bytes = tx.protect(packet)
        rtp_bytes = packet.pack()
        # AES_CM_128_HMAC_SHA1_80: 80-bit (10-byte) auth tag
        assert len(srtp_bytes) == len(rtp_bytes) + 10

    def test_protect_32bit_auth_tag_length(self) -> None:
        """AES_CM_128_HMAC_SHA1_32 must append a 4-byte (32-bit) auth tag."""
        crypto = _make_crypto(suite="AES_CM_128_HMAC_SHA1_32")
        tx = SrtpSession(crypto)
        packet = _make_packet()
        srtp_bytes = tx.protect(packet)
        rtp_bytes = packet.pack()
        assert len(srtp_bytes) == len(rtp_bytes) + 4

    def test_ciphertext_differs_from_plaintext_payload(self) -> None:
        """Encrypted payload bytes must differ from the original plaintext payload."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        payload = bytes(range(160))
        packet = _make_packet(payload=payload)
        srtp_bytes = tx.protect(packet)
        # Extract encrypted payload section (12-byte RTP header, then ciphertext)
        encrypted_payload = srtp_bytes[12 : len(srtp_bytes) - 10]
        assert encrypted_payload != payload

    def test_empty_payload_round_trip(self) -> None:
        """protect/unprotect must work for zero-length payloads (comfort noise)."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        packet = _make_packet(payload=b"")
        srtp_bytes = tx.protect(packet)
        recovered = rx.unprotect(srtp_bytes)
        assert recovered.payload == b""

    def test_large_payload_round_trip(self) -> None:
        """protect/unprotect must work for 1400-byte payloads (near MTU)."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        payload = bytes(range(256)) * 5 + bytes(range(168))  # 1448 bytes
        packet = _make_packet(payload=payload)
        srtp_bytes = tx.protect(packet)
        recovered = rx.unprotect(srtp_bytes)
        assert recovered.payload == payload


# ---------------------------------------------------------------------------
# Auth tag manipulation → rejection
# ---------------------------------------------------------------------------


class TestAuthTagRejection:
    def test_flipped_auth_tag_byte_is_rejected(self) -> None:
        """A flipped byte in the auth tag must cause unprotect to raise SrtpError."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        packet = _make_packet()
        srtp_bytes = bytearray(tx.protect(packet))
        srtp_bytes[-1] ^= 0xFF
        with pytest.raises(SrtpError, match="auth"):
            rx.unprotect(bytes(srtp_bytes))

    def test_flipped_payload_byte_is_rejected(self) -> None:
        """A flipped byte in the encrypted payload must raise SrtpError."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        packet = _make_packet(payload=b"\x00" * 160)
        srtp_bytes = bytearray(tx.protect(packet))
        # Flip a byte in the encrypted payload (after 12-byte RTP header)
        srtp_bytes[13] ^= 0x01
        with pytest.raises(SrtpError, match="auth"):
            rx.unprotect(bytes(srtp_bytes))

    def test_flipped_rtp_header_byte_is_rejected(self) -> None:
        """A flipped byte in the RTP header must cause unprotect to raise SrtpError."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        packet = _make_packet()
        srtp_bytes = bytearray(tx.protect(packet))
        # Flip a byte in the RTP header (marker/payload-type byte)
        srtp_bytes[1] ^= 0x01
        with pytest.raises(SrtpError, match="auth"):
            rx.unprotect(bytes(srtp_bytes))


# ---------------------------------------------------------------------------
# Replay-window check
# ---------------------------------------------------------------------------


class TestReplayWindow:
    def test_replayed_sequence_is_dropped(self) -> None:
        """A packet with a sequence number already received must be rejected."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        packet = _make_packet(seq=10)
        srtp_bytes = tx.protect(packet)
        rx.unprotect(srtp_bytes)
        with pytest.raises(SrtpError, match="replay"):
            rx.unprotect(srtp_bytes)

    def test_out_of_window_old_packet_is_dropped(self) -> None:
        """A packet far behind the receiver replay window must be rejected."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        # Advance the receiver window by receiving 65 sequential packets
        for seq in range(100, 165):
            pkt = _make_packet(seq=seq % 65536)
            srtp_pkt = tx.protect(pkt)
            rx.unprotect(srtp_pkt)
        # A packet at seq=10 is well outside the 64-packet sliding window
        old_pkt = _make_packet(seq=10)
        old_srtp = tx.protect(old_pkt)
        with pytest.raises(SrtpError, match="replay"):
            rx.unprotect(old_srtp)

    def test_out_of_order_within_window_is_accepted(self) -> None:
        """Packets received out of order but within the window must be accepted."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        p1 = tx.protect(_make_packet(seq=1))
        p2 = tx.protect(_make_packet(seq=2))
        p3 = tx.protect(_make_packet(seq=3))
        r3 = rx.unprotect(p3)
        r2 = rx.unprotect(p2)
        r1 = rx.unprotect(p1)
        assert r3.sequence_number == 3
        assert r2.sequence_number == 2
        assert r1.sequence_number == 1


# ---------------------------------------------------------------------------
# ROC (Rollover Counter) management
# ---------------------------------------------------------------------------


class TestRolloverCounter:
    def test_roc_increments_on_sequence_wrap(self) -> None:
        """After the sequence number wraps 65535→0, the receiver ROC must increment."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)

        near_wrap = _make_packet(seq=65534)
        rx.unprotect(tx.protect(near_wrap))
        assert rx.roc == 0

        wrap_pkt = _make_packet(seq=65535)
        rx.unprotect(tx.protect(wrap_pkt))
        assert rx.roc == 0  # ROC is still 0 at 65535

        # The first seq=0 after the wrap must bump the receiver ROC to 1
        zero_pkt = _make_packet(seq=0)
        rx.unprotect(tx.protect(zero_pkt))
        assert rx.roc == 1

    def test_two_sessions_agree_across_wrap(self) -> None:
        """TX and RX must agree on all payloads through a full 16-bit sequence wrap."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)

        # 65536 + 5 packets completes one full ROC cycle
        for seq in range(65536 + 5):
            pkt = _make_packet(seq=seq % 65536)
            srtp_bytes = tx.protect(pkt)
            recovered = rx.unprotect(srtp_bytes)
            assert recovered.payload == pkt.payload

        assert tx.roc == 1
        assert rx.roc == 1


# ---------------------------------------------------------------------------
# Key-size / suite validation
# ---------------------------------------------------------------------------


class TestCryptoValidation:
    def test_wrong_key_length_rejected_by_crypto_attribute(self) -> None:
        """CryptoAttribute rejects a 29-byte key||salt (one short of required 30)."""
        bad = base64.b64encode(bytes(29)).decode()
        with pytest.raises(SdpError):
            CryptoAttribute(
                tag=1, suite="AES_CM_128_HMAC_SHA1_80", key_params=f"inline:{bad}"
            )

    def test_unsupported_suite_rejected_by_crypto_attribute(self) -> None:
        """CryptoAttribute rejects a suite not in the allowed set."""
        good_key = base64.b64encode(bytes(30)).decode()
        with pytest.raises(SdpError):
            CryptoAttribute(
                tag=1,
                suite="AES_256_CM_HMAC_SHA1_80",
                key_params=f"inline:{good_key}",
            )


# ---------------------------------------------------------------------------
# Malformed SRTP packet → SrtpError
# ---------------------------------------------------------------------------


class TestMalformedSrtpPacket:
    def test_too_short_raises(self) -> None:
        """Data too short for a minimal RTP header + auth tag must raise SrtpError."""
        crypto = _make_crypto()
        rx = SrtpSession(crypto)
        with pytest.raises(SrtpError):
            rx.unprotect(b"\x80\x00\x00\x01" + b"\x00" * 4)  # 8 bytes — too short

    def test_truncated_auth_tag_raises(self) -> None:
        """SRTP data missing the auth tag must raise SrtpError."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        srtp = tx.protect(_make_packet())
        # Strip the 10-byte auth tag (AES_CM_128_HMAC_SHA1_80)
        with pytest.raises(SrtpError):
            rx.unprotect(srtp[: len(srtp) - 10])


# ---------------------------------------------------------------------------
# Property-based round-trip test (random payloads)
# ---------------------------------------------------------------------------


class TestPropertyRoundTrip:
    def test_random_payload_round_trips(self) -> None:
        """protect→unprotect correctly recovers 20 independently random payloads."""
        crypto = _make_crypto()
        for _ in range(20):
            payload = os.urandom(160)
            tx = SrtpSession(crypto)
            rx = SrtpSession(crypto)
            srtp_bytes = tx.protect(_make_packet(payload=payload))
            recovered = rx.unprotect(srtp_bytes)
            assert recovered.payload == payload

    def test_sequential_packets_round_trip(self) -> None:
        """200 sequential random-payload packets all round-trip correctly."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        for seq in range(200):
            payload = os.urandom(160)
            pkt = _make_packet(seq=seq, payload=payload)
            srtp_bytes = tx.protect(pkt)
            recovered = rx.unprotect(srtp_bytes)
            assert recovered.payload == payload


# ---------------------------------------------------------------------------
# Key material must not appear in SrtpSession repr or exception messages
# ---------------------------------------------------------------------------


class TestKeyMaterialRedaction:
    def test_repr_does_not_expose_key(self) -> None:
        """SrtpSession repr must not contain the base64 key or raw key/salt bytes."""
        master_key = bytes(range(16))
        master_salt = bytes(range(14))
        key_b64 = base64.b64encode(master_key + master_salt).decode()
        crypto = _make_crypto_from_bytes(
            "AES_CM_128_HMAC_SHA1_80", master_key, master_salt
        )
        session = SrtpSession(crypto)
        r = repr(session)
        assert key_b64 not in r
        assert master_key.hex() not in r
        assert master_salt.hex() not in r

    def test_srtp_error_does_not_expose_key(self) -> None:
        """SrtpError raised on auth failure must not include key material in message."""
        master_key = bytes(range(16))
        master_salt = bytes(range(14))
        key_b64 = base64.b64encode(master_key + master_salt).decode()
        crypto = _make_crypto_from_bytes(
            "AES_CM_128_HMAC_SHA1_80", master_key, master_salt
        )
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        srtp = bytearray(tx.protect(_make_packet()))
        srtp[-1] ^= 0xFF
        with pytest.raises(SrtpError) as exc_info:
            rx.unprotect(bytes(srtp))
        exc_msg = str(exc_info.value)
        assert key_b64 not in exc_msg
        assert master_key.hex() not in exc_msg
