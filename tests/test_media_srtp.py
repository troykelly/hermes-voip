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
    _packet_iv,
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

    def test_roc_wrap_up_is_32bit_masked(self) -> None:
        """_estimate_roc wrap-UP at ROC=0xFFFFFFFF must return 0, not 0x100000000.

        RFC 3711 §3.3.1: the rollover counter is a 32-bit value; incrementing past
        0xFFFFFFFF must wrap to 0x00000000.  Without the modulus the result would be
        a 33-bit integer (0x100000000), which diverges from the RFC and from the
        symmetric wrap-DOWN branch that already applies % _ROC_MOD.
        """
        crypto = _make_crypto()
        # Build a receiver already at ROC = 0xFFFFFFFF and seq_top in the lower half
        # so that the next packet (seq in the upper half, using a large seq value as
        # a back-reference across the boundary) can exercise the wrap-UP estimation.
        # We reach this state by directly manipulating roc; no 2^48-packet marathon.
        rx = SrtpSession(crypto)
        tx = SrtpSession(crypto)

        # Prime both sessions with a single packet so _seq_top is initialised.
        seed = _make_packet(seq=1)
        rx.unprotect(tx.protect(seed))
        assert tx.roc == 0
        assert rx.roc == 0

        # Force both sessions to the penultimate ROC.
        tx.roc = 0xFFFFFFFF
        rx.roc = 0xFFFFFFFF

        # Set seq_top to a small value so the estimation algorithm sees a seq in
        # the upper half (s_l < _SEQ_HALF, seq - s_l > _SEQ_HALF) — that triggers
        # the wrap-DOWN branch (roc - 1), NOT the wrap-UP branch we are testing.
        # We want s_l in the upper half and seq in the lower half to reach wrap-UP:
        #   s_l >= _SEQ_HALF AND s_l - seq > _SEQ_HALF  => v = roc + 1
        # Use s_l = 0xC000, seq = 0x0001 so s_l - seq = 0xBFFF > 0x7FFF = _SEQ_HALF.
        rx._seq_top = 0xC000  # test-only state injection

        # _estimate_roc must return 0 (== (0xFFFFFFFF + 1) % 2**32), not 0x100000000.
        # Access the private method directly to test just the estimation logic in
        # isolation without needing a valid SRTP packet at ROC=0xFFFFFFFF.
        estimated_roc = rx._estimate_roc(0x0001)
        assert estimated_roc == 0, (
            f"_estimate_roc wrap-UP at ROC=0xFFFFFFFF must yield 0 (32-bit wrap), "
            f"got {estimated_roc:#x}"
        )

    def test_sender_roc_increment_is_32bit_masked(self) -> None:
        """Sender ROC must wrap to 0x00000000 after 0xFFFFFFFF, not overflow.

        RFC 3711 §3.3.1: the ROC is a 32-bit integer; the sender-side increment on
        sequence-number wraparound must apply modulo 2**32 so that roc never escapes
        the 32-bit domain.
        """
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        # Manually place the sender at ROC = 0xFFFFFFFF and seq_top = 0xFFFF.
        tx.roc = 0xFFFFFFFF
        tx._seq_top = 0xFFFF  # test-only state injection

        # protect() with seq=0 triggers the wraparound branch (seq_top==SEQ_MAX, seq==0)
        # so roc += 1 fires; it must wrap to 0, not become 0x100000000.
        pkt = _make_packet(seq=0)
        tx.protect(pkt)
        assert tx.roc == 0, (
            f"Sender ROC must wrap to 0 after 0xFFFFFFFF, got {tx.roc:#x}"
        )


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


# ---------------------------------------------------------------------------
# Per-SSRC binding (RFC 3711 §3.2.3: cryptographic context is per-SSRC).
#
# A SrtpSession holds ONE rollover counter + replay window, which is only valid
# for a single SSRC.  The session binds to the first SSRC it sees and rejects
# any packet carrying a different SSRC, before mutating ROC/replay state.
# ---------------------------------------------------------------------------


class TestPerSsrcBinding:
    def test_protect_rejects_second_ssrc(self) -> None:
        """protect() must reject a packet whose SSRC differs from the first seen."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        tx.protect(_make_packet(seq=1, ssrc=0x11111111))
        with pytest.raises(SrtpError, match="SSRC"):
            tx.protect(_make_packet(seq=2, ssrc=0x22222222))

    def test_unprotect_rejects_second_ssrc(self) -> None:
        """unprotect() must reject a packet whose SSRC differs from the first seen."""
        crypto = _make_crypto()
        tx_a = SrtpSession(crypto)
        tx_b = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        # First inbound packet binds the receiver to SSRC 0xAAAA0000.
        rx.unprotect(tx_a.protect(_make_packet(seq=1, ssrc=0xAAAA0000)))
        # A well-formed, correctly-authenticated packet for a DIFFERENT SSRC must
        # be rejected (its tag is valid for SSRC 0xBBBB0000 but not our context).
        other = tx_b.protect(_make_packet(seq=1, ssrc=0xBBBB0000))
        with pytest.raises(SrtpError, match="SSRC"):
            rx.unprotect(other)

    def test_protect_second_ssrc_does_not_mutate_state(self) -> None:
        """A rejected foreign-SSRC protect() must not advance ROC or seq state."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        tx.protect(_make_packet(seq=65535, ssrc=0x11111111))
        assert tx.roc == 0
        with pytest.raises(SrtpError, match="SSRC"):
            # A seq=0 for a foreign SSRC would, if it mutated state, wrap ROC->1.
            tx.protect(_make_packet(seq=0, ssrc=0x22222222))
        assert tx.roc == 0  # state untouched by the rejected packet

    def test_unprotect_second_ssrc_does_not_mutate_state(self) -> None:
        """A rejected foreign-SSRC unprotect() must not advance receiver state."""
        crypto = _make_crypto()
        tx_a = SrtpSession(crypto)
        tx_b = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        rx.unprotect(tx_a.protect(_make_packet(seq=10, ssrc=0xAAAA0000)))
        with pytest.raises(SrtpError, match="SSRC"):
            rx.unprotect(tx_b.protect(_make_packet(seq=11, ssrc=0xBBBB0000)))
        # The legitimate stream still advances normally; seq=10 is still a replay.
        with pytest.raises(SrtpError, match="replay"):
            rx.unprotect(tx_a.protect(_make_packet(seq=10, ssrc=0xAAAA0000)))
        # And a fresh in-order packet on the bound SSRC is still accepted.
        ok = rx.unprotect(tx_a.protect(_make_packet(seq=11, ssrc=0xAAAA0000)))
        assert ok.sequence_number == 11

    def test_explicit_ssrc_binding_at_construction(self) -> None:
        """An explicit ssrc= at construction binds the context up front."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto, ssrc=0x0BADC0DE)
        # A packet on the bound SSRC works.
        tx.protect(_make_packet(seq=1, ssrc=0x0BADC0DE))
        # A packet on any other SSRC is rejected.
        with pytest.raises(SrtpError, match="SSRC"):
            tx.protect(_make_packet(seq=2, ssrc=0x0BADBEEF))

    def test_forged_first_packet_does_not_bind_ssrc(self) -> None:
        """A forged (bad-auth) first packet must NOT bind the receiver's SSRC.

        Auth is verified before the SSRC binding, so an attacker who injects a
        packet with an arbitrary SSRC before the first legitimate one cannot
        wedge the session onto the wrong SSRC: the forged packet fails auth, the
        binding never happens, and the genuine stream still works.
        """
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        # Forge a packet for a spoofed SSRC, then corrupt its auth tag.
        forged = bytearray(tx.protect(_make_packet(seq=1, ssrc=0x5F00F000)))
        forged[-1] ^= 0xFF
        with pytest.raises(SrtpError, match="auth"):
            rx.unprotect(bytes(forged))
        # The receiver is still unbound: a genuine packet on a different SSRC is
        # accepted and recovers its payload (the forged SSRC did not stick).
        genuine_tx = SrtpSession(crypto)
        payload = bytes(range(160))
        genuine = genuine_tx.protect(
            _make_packet(seq=1, ssrc=0xABCD1234, payload=payload)
        )
        recovered = rx.unprotect(genuine)
        assert recovered.payload == payload
        assert recovered.ssrc == 0xABCD1234


# ---------------------------------------------------------------------------
# SDES session params (lifetime / MKI) rejection at construction.
#
# RFC 4568 allows an inline key to carry |lifetime|MKI:length.  We support
# neither a non-default lifetime nor an MKI (MKI changes the SRTP packet
# layout — it inserts MKI octets between the payload and the auth tag).  We
# therefore REJECT them at construction rather than silently dropping them.
# ---------------------------------------------------------------------------


def _crypto_with_params(session_params: str) -> CryptoAttribute:
    """Build a CryptoAttribute whose inline key carries extra |session params."""
    key_salt = bytes(range(16)) + bytes(range(14))
    b64 = base64.b64encode(key_salt).decode()
    return CryptoAttribute(
        tag=1,
        suite="AES_CM_128_HMAC_SHA1_80",
        key_params=f"inline:{b64}{session_params}",
    )


class TestSessionParamRejection:
    def test_mki_is_rejected(self) -> None:
        """An MKI (|...|MKI:length) in the inline key must be rejected."""
        crypto = _crypto_with_params("|2^20|1:4")
        with pytest.raises(SrtpError, match="MKI"):
            SrtpSession(crypto)

    def test_lifetime_is_rejected(self) -> None:
        """A non-default key lifetime (|2^n) in the inline key must be rejected."""
        crypto = _crypto_with_params("|2^20")
        with pytest.raises(SrtpError, match=r"lifetime|session parameter"):
            SrtpSession(crypto)

    def test_bare_inline_key_is_accepted(self) -> None:
        """An inline key with NO session params constructs cleanly."""
        crypto = _crypto_with_params("")
        session = SrtpSession(crypto)  # must not raise
        assert session.roc == 0

    def test_session_param_error_does_not_expose_key(self) -> None:
        """The MKI/lifetime rejection error must not leak key material."""
        key_salt = bytes(range(16)) + bytes(range(14))
        b64 = base64.b64encode(key_salt).decode()
        crypto = CryptoAttribute(
            tag=1,
            suite="AES_CM_128_HMAC_SHA1_80",
            key_params=f"inline:{b64}|2^20|1:4",
        )
        with pytest.raises(SrtpError) as exc_info:
            SrtpSession(crypto)
        assert b64 not in str(exc_info.value)


# ---------------------------------------------------------------------------
# RTP header variations: CSRC list and header extension.
#
# RFC 3711 §3.1/§3.3: SRTP encrypts ONLY the RTP payload; the full RTP header
# (incl. the CSRC list per CC and any X header extension) stays in the clear
# and is AUTHENTICATED.  A fixed 12-byte header assumption corrupts any packet
# that carries CSRCs or an extension.
# ---------------------------------------------------------------------------


def _rtp_wire_with_csrc(  # noqa: PLR0913 - RTP header fields are independent, all keyword-only
    *,
    payload_type: int,
    seq: int,
    timestamp: int,
    ssrc: int,
    csrcs: tuple[int, ...],
    payload: bytes,
) -> bytes:
    """Hand-build RTP wire bytes with a CSRC list (V=2, P=0, X=0)."""
    cc = len(csrcs)
    byte0 = (2 << 6) | cc
    byte1 = payload_type & 0x7F
    header = bytes([byte0, byte1])
    header += seq.to_bytes(2, "big") + timestamp.to_bytes(4, "big")
    header += ssrc.to_bytes(4, "big")
    for csrc in csrcs:
        header += csrc.to_bytes(4, "big")
    return header + payload


def _rtp_wire_with_extension(  # noqa: PLR0913 - RTP header fields are independent, all keyword-only
    *,
    payload_type: int,
    seq: int,
    timestamp: int,
    ssrc: int,
    ext_profile: int,
    ext_words: tuple[int, ...],
    payload: bytes,
) -> bytes:
    """Hand-build RTP wire bytes with a header extension (V=2, P=0, X=1, CC=0)."""
    byte0 = (2 << 6) | 0x10  # X bit set
    byte1 = payload_type & 0x7F
    header = bytes([byte0, byte1])
    header += seq.to_bytes(2, "big") + timestamp.to_bytes(4, "big")
    header += ssrc.to_bytes(4, "big")
    header += ext_profile.to_bytes(2, "big") + len(ext_words).to_bytes(2, "big")
    for word in ext_words:
        header += word.to_bytes(4, "big")
    return header + payload


class TestRtpHeaderVariations:
    def test_csrc_packet_round_trips(self) -> None:
        """A packet with a 2-entry CSRC list must round-trip (header authenticated)."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        payload = bytes(range(160))
        wire = _rtp_wire_with_csrc(
            payload_type=0,
            seq=42,
            timestamp=160,
            ssrc=0xDEADBEEF,
            csrcs=(0x11111111, 0x22222222),
            payload=payload,
        )
        srtp = tx.protect_wire(wire)
        recovered = rx.unprotect(srtp)
        assert recovered.payload == payload
        assert recovered.sequence_number == 42
        assert recovered.ssrc == 0xDEADBEEF

    def test_csrc_bytes_are_not_encrypted(self) -> None:
        """The CSRC list stays in the clear (only the payload is encrypted)."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        csrcs = (0x11111111, 0x22222222)
        wire = _rtp_wire_with_csrc(
            payload_type=0,
            seq=42,
            timestamp=160,
            ssrc=0xDEADBEEF,
            csrcs=csrcs,
            payload=bytes(range(160)),
        )
        srtp = tx.protect_wire(wire)
        # The 12-byte fixed header + 8 CSRC bytes (2 * 4) must be byte-identical.
        clear_header_len = 12 + len(csrcs) * 4
        assert srtp[:clear_header_len] == wire[:clear_header_len]

    def test_extension_packet_round_trips(self) -> None:
        """A packet with a header extension round-trips (extension authenticated)."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        rx = SrtpSession(crypto)
        payload = bytes(range(80))
        wire = _rtp_wire_with_extension(
            payload_type=0,
            seq=7,
            timestamp=80,
            ssrc=0x0BADF00D,
            ext_profile=0xBEDE,
            ext_words=(0xCAFEBABE, 0x12345678),
            payload=payload,
        )
        srtp = tx.protect_wire(wire)
        recovered = rx.unprotect(srtp)
        assert recovered.payload == payload
        assert recovered.sequence_number == 7
        assert recovered.ssrc == 0x0BADF00D

    def test_extension_bytes_are_not_encrypted(self) -> None:
        """The header extension stays in the clear (only the payload is encrypted)."""
        crypto = _make_crypto()
        tx = SrtpSession(crypto)
        ext_words = (0xCAFEBABE, 0x12345678)
        wire = _rtp_wire_with_extension(
            payload_type=0,
            seq=7,
            timestamp=80,
            ssrc=0x0BADF00D,
            ext_profile=0xBEDE,
            ext_words=ext_words,
            payload=bytes(range(80)),
        )
        srtp = tx.protect_wire(wire)
        # 12-byte fixed header + 4-byte ext header + 8 ext-data bytes stay clear.
        clear_header_len = 12 + 4 + len(ext_words) * 4
        assert srtp[:clear_header_len] == wire[:clear_header_len]


# ---------------------------------------------------------------------------
# _packet_iv literal known-answer tests (RFC 3711 §4.1.1).
#
# The B.2 keystream KAT exercises _aes_cm_keystream with a literal IV, NOT the
# IV constructor.  These assert _packet_iv itself against literals: the B.2
# zero-index case (IV == k_s || 0x0000) and a hand-derived non-zero case, so a
# broken IV constructor cannot pass the round-trip + B.2 tests by coincidence.
# ---------------------------------------------------------------------------


class TestPacketIvKnownAnswers:
    def test_packet_iv_b2_zero_index(self) -> None:
        """RFC 3711 §B.2: with ssrc=roc=seq=0, IV = k_s || 0x0000.

        k_s = F0F1F2F3F4F5F6F7F8F9FAFBFCFD (14 bytes) -> IV = k_s || 0x0000.
        """
        k_s = bytes.fromhex("F0F1F2F3F4F5F6F7F8F9FAFBFCFD")
        iv = _packet_iv(k_s, ssrc=0, roc=0, seq=0)
        assert iv == bytes.fromhex("F0F1F2F3F4F5F6F7F8F9FAFBFCFD0000")

    def test_packet_iv_nonzero_hand_derived(self) -> None:
        """RFC 3711 §4.1.1 IV = (k_s||0000) XOR (SSRC<<64) XOR (i<<16), i=ROC*2^16+SEQ.

        Hand-derived for k_s=000102..0D, SSRC=0x12345678, ROC=2, SEQ=3:
          k_s||0000 = 000102030405060708090A0B0C0D0000
          SSRC<<64  = 00000000123456780000000000000000
          i<<16     = 00000000000000000000000200030000  (i = 0x20003)
          XOR       = 000102031631507F08090A090C0E0000
        """
        k_s = bytes(range(14))  # 000102030405060708090A0B0C0D
        iv = _packet_iv(k_s, ssrc=0x12345678, roc=2, seq=3)
        assert iv == bytes.fromhex("000102031631507F08090A090C0E0000")


# ---------------------------------------------------------------------------
# __all__ export
# ---------------------------------------------------------------------------


import hermes_voip.media.srtp as _srtp_mod  # noqa: E402


class TestSrtpModuleExports:
    """Verify that srtp.py defines __all__ with the correct public names."""

    def test_module_defines_all(self) -> None:
        """The srtp module must define __all__."""
        assert hasattr(_srtp_mod, "__all__"), "srtp module must define __all__"

    def test_all_contains_correct_public_names(self) -> None:
        """__all__ must list the exact public names intended for star-import."""
        expected = {"SrtpError", "SrtpSession", "crypto_suite_strength"}
        assert set(_srtp_mod.__all__) == expected

    def test_all_names_are_importable(self) -> None:
        """Every name in __all__ must be importable from the module."""
        all_names = _srtp_mod.__all__
        for name in all_names:
            assert hasattr(_srtp_mod, name), (
                f"{name} must be importable from srtp module"
            )
            assert not name.startswith("_"), (
                f"{name} must not be private (no leading _)"
            )

    def test_no_private_names_in_all(self) -> None:
        """__all__ must not include any names starting with underscore."""
        all_names = _srtp_mod.__all__
        private_names = [name for name in all_names if name.startswith("_")]
        assert not private_names, f"Private names in __all__: {private_names}"
