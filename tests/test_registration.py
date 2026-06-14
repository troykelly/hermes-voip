"""Tests for hermes_voip.registration — the sans-IO SIP REGISTER flow.

The flow produces request wire text and consumes parsed responses; it owns no
socket or timer (the transport, ADR-0005, does). This keeps the digest/auth and
state logic deterministic. Fakes use ``pbx.example.test`` / ext ``1000``.
"""

import re

import pytest

from hermes_voip.message import SipResponse
from hermes_voip.registration import (
    Challenged,
    Failed,
    Registered,
    RegistrationConfig,
    RegistrationFlow,
)

_CONFIG = RegistrationConfig(
    aor="sip:1000@pbx.example.test",
    username="1000",
    password="s3cr3t",
    contact="<sip:1000@198.51.100.7:5061;transport=tls>",
    expires=300,
)


def _challenge_response() -> SipResponse:
    return SipResponse.parse(
        "SIP/2.0 401 Unauthorized\r\n"
        'WWW-Authenticate: Digest realm="pbx.example.test", nonce="abc123", '
        'algorithm=md5, qop="auth"\r\n'
        "Content-Length: 0\r\n\r\n"
    )


def _ok_response(expires: int = 300) -> SipResponse:
    return SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        f"Contact: <sip:1000@198.51.100.7:5061;transport=tls>;expires={expires}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _header(msg: str, name: str) -> str | None:
    for line in msg.split("\r\n"):
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return None


def test_start_builds_initial_register() -> None:
    flow = RegistrationFlow(_CONFIG)
    req = flow.start()
    assert req.startswith("REGISTER sip:pbx.example.test SIP/2.0\r\n")
    assert _header(req, "From") is not None
    assert _header(req, "To") is not None
    assert _header(req, "CSeq") == "1 REGISTER"
    assert _header(req, "Expires") == "300"
    assert "Authorization" not in req


def test_401_yields_authed_register_with_incremented_cseq() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    outcome = flow.handle(_challenge_response())
    assert isinstance(outcome, Challenged)
    authed = outcome.request
    assert _header(authed, "CSeq") == "2 REGISTER"
    auth = _header(authed, "Authorization")
    assert auth is not None
    assert 'username="1000"' in auth
    assert 'realm="pbx.example.test"' in auth
    assert re.search(r'response="[0-9a-f]{32}"', auth)


def test_call_id_and_from_tag_stable_across_requests() -> None:
    flow = RegistrationFlow(_CONFIG)
    first = flow.start()
    authed = flow.handle(_challenge_response())
    assert isinstance(authed, Challenged)
    assert _header(first, "Call-ID") == _header(authed.request, "Call-ID")
    assert _header(first, "From") == _header(authed.request, "From")  # tag stable


def test_200_yields_registered_with_granted_expiry() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    flow.handle(_challenge_response())
    outcome = flow.handle(_ok_response(expires=120))
    assert isinstance(outcome, Registered)
    assert outcome.expires == 120


def test_403_yields_failed() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    outcome = flow.handle(
        SipResponse.parse("SIP/2.0 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
    )
    assert isinstance(outcome, Failed)
    assert outcome.status == 403


def test_second_challenge_does_not_loop_forever() -> None:
    # a 401 answering an already-authed REGISTER must fail, not re-challenge again
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    flow.handle(_challenge_response())  # -> Challenged (authed)
    outcome = flow.handle(_challenge_response())  # still 401 after auth
    assert isinstance(outcome, Failed)
    assert outcome.status == 401


def test_deregister_sends_expires_zero() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    flow.handle(_challenge_response())
    flow.handle(_ok_response())
    dereg = flow.deregister()
    assert _header(dereg, "Expires") == "0"
    assert _header(dereg, "CSeq") == "3 REGISTER"


def test_deregister_before_registration_raises() -> None:
    flow = RegistrationFlow(_CONFIG)
    with pytest.raises(RuntimeError, match="not registered"):
        flow.deregister()
