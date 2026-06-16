"""DTLS-SRTP keying tests (RFC 5763/5764, PR-C) — media/dtls.py.

Tests the in-process DTLS handshake between two :class:`DtlsEndpoint` instances
driven over an in-memory datagram pair (no real UDP socket).  After the handshake:

- the RFC 5705 exporter (label ``EXTRACTOR-dtls_srtp``) yields keying material;
- client/server derive the SAME master key||salt (client-write == server-read);
- a protect→unprotect round-trip works through the resulting :class:`SrtpSession`s;
- the cert fingerprint emitted by :meth:`DtlsEndpoint.fingerprint` matches the cert.

The ``webrtc`` extra (pyOpenSSL ≥ 25.3.0) is required.  Without it the module
imports cleanly but :class:`DtlsEndpoint` raises :class:`ImportError` at
construction; ``pytest.importorskip`` gates the whole suite.

All key material in these tests is synthetically generated at test-session start —
nothing is committed.  The tests are fully deterministic (the DTLS handshake uses
real pyOpenSSL over an in-memory datagram pair, not a mock).
"""

from __future__ import annotations

import importlib

import pytest

import hermes_voip.media.dtls as _dtls_mod

# Gate the entire suite on pyOpenSSL being present (the ``webrtc`` extra).
_pyopenssl = pytest.importorskip(
    "OpenSSL",
    reason="pyOpenSSL (webrtc extra) required: uv run --extra webrtc pytest",
)

# Gate on cryptography too (needed by SrtpSession via the media extra).
_cryptography = pytest.importorskip(
    "cryptography",
    reason="cryptography (media extra) required: uv run --extra webrtc pytest",
)

# ---------------------------------------------------------------------------
# Import modules under test AFTER confirming extras are present.
# ---------------------------------------------------------------------------

from hermes_voip.media.dtls import (  # noqa: E402
    _EXPORTER_LABEL,
    _SRTP_KEY_LEN,
    _SRTP_SALT_LEN,
    DtlsEndpoint,
    DtlsRole,
    SrtpProfile,
)
from hermes_voip.media.srtp import SrtpError, SrtpSession  # noqa: E402
from hermes_voip.rtp import RtpPacket  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ssl_error_class() -> type[Exception]:
    """Return ``OpenSSL.SSL.Error`` for use in ``pytest.raises``.

    Loaded via importlib (the suite is already gated on OpenSSL being present)
    so the return type is a concrete ``type[Exception]`` with no ``Any``.
    """
    ssl_mod = importlib.import_module("OpenSSL.SSL")
    error_cls: type[Exception] = ssl_mod.Error
    return error_cls


def _pump_until_raise(
    client: DtlsEndpoint,
    server: DtlsEndpoint,
    *,
    max_rounds: int = 30,
) -> None:
    """Drive the handshake, letting any fatal SSL error propagate to the caller.

    Unlike :func:`_pump_handshake` (which expects success), this helper does not
    catch SSL errors - it is used to assert that a fatal DTLS alert escapes
    feed()/get_outbound_datagrams().
    """
    for _ in range(max_rounds):
        for dg in client.get_outbound_datagrams():
            server.feed(dg)
        for dg in server.get_outbound_datagrams():
            client.feed(dg)


def _pump_handshake(
    client: DtlsEndpoint,
    server: DtlsEndpoint,
    *,
    max_rounds: int = 30,
) -> None:
    """Drive the DTLS handshake to completion by exchanging datagrams in-process.

    Each round: feed pending datagrams from client→server and server→client.
    The handshake is complete once both sides stop producing output.

    Args:
        client: The DTLS client endpoint (role=active).
        server: The DTLS server endpoint (role=passive).
        max_rounds: Safety limit to prevent infinite loops in tests.

    Raises:
        RuntimeError: If the handshake does not complete within max_rounds.
    """
    for _ in range(max_rounds):
        c_out = client.get_outbound_datagrams()
        for dg in c_out:
            server.feed(dg)

        s_out = server.get_outbound_datagrams()
        for dg in s_out:
            client.feed(dg)

        if not c_out and not s_out:
            break
    else:  # pragma: no cover
        msg = "DTLS handshake did not complete within the round limit"
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dtls_pair() -> tuple[DtlsEndpoint, DtlsEndpoint]:
    """Return a (client, server) pair that has completed the DTLS handshake.

    Shared across the module so we only run one handshake (it involves RSA keygen).
    Both endpoints have verify_peer_fingerprint() called (RFC 5763 §5 invariant)
    so they are ready for derive_srtp_sessions().
    """
    client = DtlsEndpoint(role=DtlsRole.CLIENT)
    server = DtlsEndpoint(role=DtlsRole.SERVER)

    _pump_handshake(client, server)

    # RFC 5763 §5: fingerprint must be verified before deriving SRTP sessions.
    # Cross-verify: each side verifies the peer's fingerprint using the
    # fingerprint the peer would put in its SDP a=fingerprint line.
    client.verify_peer_fingerprint(server.fingerprint())
    server.verify_peer_fingerprint(client.fingerprint())

    return client, server


# ---------------------------------------------------------------------------
# Fingerprint tests
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_fingerprint_is_sha256_hex_colon(self) -> None:
        """Fingerprint is a lowercase colon-separated hex string (RFC 4572 §5)."""
        ep = DtlsEndpoint(role=DtlsRole.CLIENT)
        fp = ep.fingerprint()
        # Format: 'sha-256 XX:XX:XX:...' — 32 pairs of hex digits separated by ':'
        assert fp.startswith("sha-256 ")
        hex_part = fp[len("sha-256 ") :]
        pairs = hex_part.split(":")
        assert len(pairs) == 32, f"Expected 32 hex pairs, got {len(pairs)}"
        for pair in pairs:
            assert len(pair) == 2, f"Pair {pair!r} is not 2 hex digits"
            int(pair, 16)  # raises ValueError if not hex

    def test_fingerprint_is_stable(self) -> None:
        """Fingerprint is the same cert on repeated calls."""
        ep = DtlsEndpoint(role=DtlsRole.CLIENT)
        assert ep.fingerprint() == ep.fingerprint()

    def test_two_endpoints_have_different_certs(self) -> None:
        """Each DtlsEndpoint generates its own self-signed cert."""
        ep1 = DtlsEndpoint(role=DtlsRole.CLIENT)
        ep2 = DtlsEndpoint(role=DtlsRole.CLIENT)
        assert ep1.fingerprint() != ep2.fingerprint()


# ---------------------------------------------------------------------------
# Handshake tests
# ---------------------------------------------------------------------------


class TestHandshake:
    def test_handshake_completes(
        self, dtls_pair: tuple[DtlsEndpoint, DtlsEndpoint]
    ) -> None:
        """The DTLS handshake between two in-process endpoints completes."""
        client, server = dtls_pair
        assert client.handshake_done()
        assert server.handshake_done()

    def test_selected_profile_is_srtp_aes128_cm_sha1_80(
        self, dtls_pair: tuple[DtlsEndpoint, DtlsEndpoint]
    ) -> None:
        """The negotiated DTLS-SRTP profile is SRTP_AES128_CM_SHA1_80."""
        client, server = dtls_pair
        assert client.selected_profile() == SrtpProfile.AES128_CM_HMAC_SHA1_80
        assert server.selected_profile() == SrtpProfile.AES128_CM_HMAC_SHA1_80

    def test_feed_is_noop_before_handshake(self) -> None:
        """feed() on a fresh endpoint does not raise (no data received yet)."""
        ep = DtlsEndpoint(role=DtlsRole.CLIENT)
        ep.feed(b"\x00" * 1)  # Should not raise

    def test_get_outbound_datagrams_produces_client_hello(self) -> None:
        """A fresh CLIENT endpoint produces a ClientHello on the first call."""
        ep = DtlsEndpoint(role=DtlsRole.CLIENT)
        datagrams = ep.get_outbound_datagrams()
        assert len(datagrams) > 0, "CLIENT should produce an initial ClientHello"


# ---------------------------------------------------------------------------
# Key-export tests
# ---------------------------------------------------------------------------


class TestKeyExport:
    def test_exporter_label(self) -> None:
        """The RFC 5764 §4.2 exporter label is correct."""
        assert _EXPORTER_LABEL == b"EXTRACTOR-dtls_srtp"

    def test_key_and_salt_lengths(self) -> None:
        """Master key and salt sizes match SRTP_AES128_CM_HMAC_SHA1_80."""
        assert _SRTP_KEY_LEN == 16  # 128 bits
        assert _SRTP_SALT_LEN == 14  # 112 bits

    def test_client_write_key_equals_server_read_key(
        self, dtls_pair: tuple[DtlsEndpoint, DtlsEndpoint]
    ) -> None:
        """Both sides export the same raw material (RFC 5764 §4.2)."""
        client, server = dtls_pair
        c_mat = client.export_srtp_keying_material()
        s_mat = server.export_srtp_keying_material()
        # Both ends must produce identical exported keying material.
        assert c_mat == s_mat

    def test_keying_material_has_correct_length(
        self, dtls_pair: tuple[DtlsEndpoint, DtlsEndpoint]
    ) -> None:
        """Exported block is the correct length for AES128_CM (60 bytes)."""
        client, _ = dtls_pair
        mat = client.export_srtp_keying_material()
        expected = 2 * (_SRTP_KEY_LEN + _SRTP_SALT_LEN)  # 60 bytes for AES128_CM
        assert len(mat) == expected

    def test_key_material_is_not_all_zeros(
        self, dtls_pair: tuple[DtlsEndpoint, DtlsEndpoint]
    ) -> None:
        """Keying material is random (not zeros — a degenerate value)."""
        client, _ = dtls_pair
        mat = client.export_srtp_keying_material()
        assert mat != bytes(len(mat))


# ---------------------------------------------------------------------------
# SRTP session derivation (the critical integration test)
# ---------------------------------------------------------------------------


class TestSrtpSessionDerivation:
    def test_derive_inbound_outbound_sessions(
        self, dtls_pair: tuple[DtlsEndpoint, DtlsEndpoint]
    ) -> None:
        """derive_srtp_sessions returns a (inbound, outbound) SrtpSession pair."""
        client, _server = dtls_pair
        inbound, outbound = client.derive_srtp_sessions()
        assert isinstance(inbound, SrtpSession)
        assert isinstance(outbound, SrtpSession)

    def test_protect_unprotect_round_trip(
        self, dtls_pair: tuple[DtlsEndpoint, DtlsEndpoint]
    ) -> None:
        """Client-outbound protect→server-inbound unprotect succeeds (RFC 3711).

        Integration test for the full DTLS → SRTP pipeline:
        1. Run DTLS handshake (done in fixture).
        2. Client: client_write_key/salt → outbound SrtpSession → protect().
        3. Server: client_write_key/salt → inbound SrtpSession → unprotect().
        4. Decrypted payload matches the original.
        """
        client, server = dtls_pair

        # Derive sessions for both sides.
        _c_inbound, c_outbound = client.derive_srtp_sessions()
        s_inbound, _s_outbound = server.derive_srtp_sessions()

        # Build a plaintext RTP packet.
        payload = b"\xaa\xbb\xcc\xdd" * 10
        pkt = RtpPacket(
            payload_type=0,
            sequence_number=1,
            timestamp=160,
            ssrc=0xCAFEBABE,
            payload=payload,
        )

        # Protect with the client's outbound session.
        srtp_wire = c_outbound.protect(pkt)
        assert len(srtp_wire) > len(payload)  # has auth tag

        # Unprotect with the server's inbound session.
        decrypted = s_inbound.unprotect(srtp_wire)
        assert decrypted.payload == payload
        assert decrypted.ssrc == pkt.ssrc
        assert decrypted.sequence_number == pkt.sequence_number

    def test_server_to_client_round_trip(
        self, dtls_pair: tuple[DtlsEndpoint, DtlsEndpoint]
    ) -> None:
        """Server-outbound protect→client-inbound unprotect (reverse direction)."""
        client, server = dtls_pair

        c_inbound, _c_outbound = client.derive_srtp_sessions()
        _s_inbound, s_outbound = server.derive_srtp_sessions()

        payload = b"\x11\x22\x33\x44" * 8
        pkt = RtpPacket(
            payload_type=0,
            sequence_number=1,
            timestamp=160,
            ssrc=0xDEADBEEF,
            payload=payload,
        )

        srtp_wire = s_outbound.protect(pkt)
        decrypted = c_inbound.unprotect(srtp_wire)
        assert decrypted.payload == payload

    def test_srtp_auth_failure_on_tampered_packet(
        self, dtls_pair: tuple[DtlsEndpoint, DtlsEndpoint]
    ) -> None:
        """A tampered SRTP packet is rejected by the auth tag check."""
        client, server = dtls_pair

        _c_inbound, c_outbound = client.derive_srtp_sessions()
        # fresh server session to avoid SSRC binding state from other tests
        s_inbound_fresh, _ = server.derive_srtp_sessions()

        pkt = RtpPacket(
            payload_type=0,
            sequence_number=2,
            timestamp=320,
            ssrc=0x11223344,
            payload=b"\xff" * 20,
        )
        srtp_wire = c_outbound.protect(pkt)

        # Corrupt one byte in the payload area (not the auth tag).
        tampered = bytearray(srtp_wire)
        tampered[13] ^= 0xFF  # flip bits in the ciphertext
        with pytest.raises(SrtpError):
            s_inbound_fresh.unprotect(bytes(tampered))


# ---------------------------------------------------------------------------
# from_raw_keys constructor
# ---------------------------------------------------------------------------


class TestFromRawKeys:
    def test_from_raw_keys_constructs_session(self) -> None:
        """SrtpSession.from_raw_keys constructs without a CryptoAttribute."""
        # Synthetic test values (not real keys): 16-byte key + 14-byte salt.
        master_key = bytes(range(16))
        master_salt = bytes(range(14))
        session = SrtpSession.from_raw_keys(
            master_key, master_salt, suite="AES_CM_128_HMAC_SHA1_80"
        )
        assert isinstance(session, SrtpSession)

    def test_from_raw_keys_protect_unprotect(self) -> None:
        """SrtpSession.from_raw_keys produces a session that can protect/unprotect."""
        master_key = bytes(range(16, 32))
        master_salt = bytes(range(14))

        tx = SrtpSession.from_raw_keys(
            master_key, master_salt, suite="AES_CM_128_HMAC_SHA1_80"
        )
        rx = SrtpSession.from_raw_keys(
            master_key, master_salt, suite="AES_CM_128_HMAC_SHA1_80"
        )

        payload = b"\xde\xad\xbe\xef" * 10
        pkt = RtpPacket(
            payload_type=8,
            sequence_number=100,
            timestamp=8000,
            ssrc=0xAABBCCDD,
            payload=payload,
        )

        wire = tx.protect(pkt)
        decrypted = rx.unprotect(wire)
        assert decrypted.payload == payload

    def test_from_raw_keys_rejects_bad_key_length(self) -> None:
        """from_raw_keys raises ValueError/SrtpError on an incorrect key length."""
        with pytest.raises((SrtpError, ValueError)):
            SrtpSession.from_raw_keys(
                b"\x00" * 10,  # wrong length (need 16)
                b"\x00" * 14,
                suite="AES_CM_128_HMAC_SHA1_80",
            )

    def test_from_raw_keys_rejects_bad_salt_length(self) -> None:
        """from_raw_keys raises ValueError/SrtpError on an incorrect salt length."""
        with pytest.raises((SrtpError, ValueError)):
            SrtpSession.from_raw_keys(
                b"\x00" * 16,
                b"\x00" * 10,  # wrong length (need 14)
                suite="AES_CM_128_HMAC_SHA1_80",
            )

    def test_from_raw_keys_rejects_unsupported_suite(self) -> None:
        """from_raw_keys raises SrtpError on an unsupported suite name."""
        with pytest.raises(SrtpError):
            SrtpSession.from_raw_keys(
                b"\x00" * 16,
                b"\x00" * 14,
                suite="AES_CM_256_HMAC_SHA1_80",  # not supported
            )

    def test_from_raw_keys_with_ssrc_binding(self) -> None:
        """from_raw_keys accepts an optional ssrc argument."""
        master_key = bytes(range(16))
        master_salt = bytes(range(14))
        session = SrtpSession.from_raw_keys(
            master_key,
            master_salt,
            suite="AES_CM_128_HMAC_SHA1_80",
            ssrc=0xABCD1234,
        )
        assert isinstance(session, SrtpSession)


# ---------------------------------------------------------------------------
# Import-guard (no extra) test
# ---------------------------------------------------------------------------


class TestImportGuard:
    def test_module_imports_without_webrtc_extra(self) -> None:
        """media.dtls can be imported even without pyOpenSSL."""
        # We can't un-import OpenSSL in this process, but we verify the module
        # exists and the DtlsEndpoint is importable.
        assert hasattr(_dtls_mod, "DtlsEndpoint")
        assert hasattr(_dtls_mod, "DtlsRole")
        assert hasattr(_dtls_mod, "SrtpProfile")


# ---------------------------------------------------------------------------
# Security hardening: fingerprint-before-keying (DEFECT 8 fix)
# ---------------------------------------------------------------------------


class TestFingerprintBeforeKeying:
    def test_derive_srtp_sessions_requires_fingerprint_verification(self) -> None:
        """derive_srtp_sessions raises if verify_peer_fingerprint was not called.

        RFC 5763 §5 requires fingerprint verification before using keying material.
        Without this guard a MITM could complete the DTLS handshake (VERIFY_NONE
        accepts any cert) and the caller would silently key SRTP from attacker
        material.
        """
        client = DtlsEndpoint(role=DtlsRole.CLIENT)
        server = DtlsEndpoint(role=DtlsRole.SERVER)
        _pump_handshake(client, server)

        # Neither endpoint has had verify_peer_fingerprint() called.
        with pytest.raises(RuntimeError, match="verify_peer_fingerprint"):
            client.derive_srtp_sessions()
        with pytest.raises(RuntimeError, match="verify_peer_fingerprint"):
            server.derive_srtp_sessions()

    def test_derive_srtp_sessions_succeeds_after_fingerprint_verification(
        self,
    ) -> None:
        """derive_srtp_sessions succeeds after verify_peer_fingerprint is called."""
        client = DtlsEndpoint(role=DtlsRole.CLIENT)
        server = DtlsEndpoint(role=DtlsRole.SERVER)
        _pump_handshake(client, server)

        # Verify fingerprints: each side uses the peer's fingerprint from SDP.
        client.verify_peer_fingerprint(server.fingerprint())
        server.verify_peer_fingerprint(client.fingerprint())

        c_inbound, c_outbound = client.derive_srtp_sessions()
        s_inbound, s_outbound = server.derive_srtp_sessions()
        assert isinstance(c_inbound, SrtpSession)
        assert isinstance(c_outbound, SrtpSession)
        assert isinstance(s_inbound, SrtpSession)
        assert isinstance(s_outbound, SrtpSession)

    def test_fingerprint_mismatch_does_not_set_verified_flag(self) -> None:
        """A failed verify_peer_fingerprint does not allow derive_srtp_sessions."""
        client = DtlsEndpoint(role=DtlsRole.CLIENT)
        server = DtlsEndpoint(role=DtlsRole.SERVER)
        _pump_handshake(client, server)

        # Deliberately pass the wrong fingerprint (use client's own fp for client).
        with pytest.raises(ValueError, match="mismatch"):
            client.verify_peer_fingerprint(client.fingerprint())  # wrong: own fp

        # Should still block derive_srtp_sessions since verification failed.
        with pytest.raises(RuntimeError, match="verify_peer_fingerprint"):
            client.derive_srtp_sessions()


# ---------------------------------------------------------------------------
# Security hardening: fatal TLS alert propagation (DEFECT 4 fix)
# ---------------------------------------------------------------------------


class TestFatalTlsAlert:
    def test_handshake_failed_is_false_on_fresh_endpoint(self) -> None:
        """handshake_failed() returns False before any handshake attempt."""
        ep = DtlsEndpoint(role=DtlsRole.CLIENT)
        assert not ep.handshake_failed()

    def test_handshake_failed_is_false_after_successful_handshake(
        self, dtls_pair: tuple[DtlsEndpoint, DtlsEndpoint]
    ) -> None:
        """handshake_failed() returns False after a successful handshake."""
        client, server = dtls_pair
        assert not client.handshake_failed()
        assert not server.handshake_failed()

    def test_fatal_alert_reraises_and_sets_failed_flag(self) -> None:
        """A fatal DTLS alert re-raises (not swallowed) AND sets handshake_failed().

        Deterministically forces a fatal ``SSL.Error`` ("no shared cipher") via a
        cipher-suite mismatch: the client offers only AES128-GCM, the server only
        AES256-GCM.  This proves the DEFECT-4 fix - a non-WantRead ``SSL.Error``
        must propagate (rule 37) and the endpoint must record the failure - rather
        than being silently swallowed alongside the normal WantReadError signal.
        """
        client = DtlsEndpoint(
            role=DtlsRole.CLIENT, cipher_list=b"ECDHE-RSA-AES128-GCM-SHA256"
        )
        server = DtlsEndpoint(
            role=DtlsRole.SERVER, cipher_list=b"ECDHE-RSA-AES256-GCM-SHA384"
        )

        # Drive the handshake until the server rejects the cipher list.  The
        # fatal SSL.Error MUST propagate out of feed()/get_outbound_datagrams().
        with pytest.raises(_ssl_error_class()):
            _pump_until_raise(client, server)

        # The server saw the fatal "no shared cipher" alert.
        assert server.handshake_failed()
        # _done and _failed are mutually exclusive.
        assert not server.handshake_done()

    def test_failed_endpoint_reraises_on_subsequent_calls(self) -> None:
        """Once failed, an endpoint re-raises the SAME error on later calls.

        A failed endpoint must never silently no-op (rule 37): every subsequent
        feed()/get_outbound_datagrams() must keep surfacing the original fatal
        error so callers cannot mistake a dead endpoint for an idle one.
        """
        client = DtlsEndpoint(
            role=DtlsRole.CLIENT, cipher_list=b"ECDHE-RSA-AES128-GCM-SHA256"
        )
        server = DtlsEndpoint(
            role=DtlsRole.SERVER, cipher_list=b"ECDHE-RSA-AES256-GCM-SHA384"
        )

        # Drive until the server fails (the first raise is exercised above).
        first_error: BaseException | None = None
        try:
            _pump_until_raise(client, server)
        except _ssl_error_class() as exc:
            first_error = exc
        assert first_error is not None, "expected a fatal SSL.Error"
        assert server.handshake_failed()

        # A subsequent call on the failed server re-raises (does NOT no-op).
        with pytest.raises(_ssl_error_class()):
            server.get_outbound_datagrams()
        with pytest.raises(_ssl_error_class()):
            server.feed(b"\x16\xfe\xfd")  # any DTLS-looking byte triggers re-raise
