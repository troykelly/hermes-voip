"""End-to-end integration tests: a complete inbound call against the real stack.

These drive the **real** plugin stack — ``VoipAdapter``, the real
``SipOverTlsTransport``, real ``RegistrationManager`` / ``Dialog`` /
``CallSession`` / ``CallLoop`` / ``RtpMediaTransport``, and real SDP — with only
the far-end **gateway** and the LLM **agent** faked, at REAL sample rates at every
seam.

The reusable loopback fake gateway lives in :mod:`tests.e2e._fake_gateway`.
"""
