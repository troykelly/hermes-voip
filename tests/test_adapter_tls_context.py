"""TLS-context hardening for the SOLE client TLS context (ADR-0089, ADR-0005).

``_make_tls_context`` builds the only client TLS context for BOTH the
SIP-over-TLS and WSS signalling legs — the legs that carry the SIP digest
password (Authorization HA1/response) and the SDES ``a=crypto`` inline master
key and salt. A bare ``ssl.create_default_context()`` leaves
``minimum_version`` at ``TLSVersion.MINIMUM_SUPPORTED`` on Python 3.13, so the
host OpenSSL policy could negotiate TLS 1.0/1.1 on those secret-bearing legs.
This module pins the TLS 1.2 floor and locks the inherited verification posture
(``check_hostname`` / ``CERT_REQUIRED``) so a future weakening is caught.

The adapter imports the real Hermes base at module top, so importing
``_make_tls_context`` needs the optional ``hermes`` extra; the module skips
cleanly without it and runs in the ``hermes-contract`` CI job (rule 26). The
test asserts context PROPERTIES only — no live connection, no real host or
certificate (public repo).
"""

from __future__ import annotations

import ssl

import pytest

# The adapter imports the real Hermes base at module top; skip the whole module
# when the optional runtime is absent (it runs in the hermes-contract CI job).
pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from hermes_voip.adapter import _make_tls_context


def test_make_tls_context_pins_min_version_and_verification() -> None:
    """The client TLS context floors at TLS 1.2 and keeps verification on."""
    ctx = _make_tls_context("pbx.example.test")

    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2, (
        "the client TLS context must floor at TLS 1.2 to prevent downgrade to "
        "TLS 1.0/1.1 on the leg carrying the digest password and SDES key"
    )
    assert ctx.check_hostname is True, (
        "server-certificate hostname verification must stay enabled "
        "(downgrade-hardening is not a verification bypass)"
    )
    assert ctx.verify_mode == ssl.CERT_REQUIRED, (
        "server-certificate verification must stay required"
    )
