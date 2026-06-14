"""Tests for hermes_voip.registration — the sans-IO SIP REGISTER flow.

The flow produces request wire text and consumes parsed responses; it owns no
socket or timer (the transport, ADR-0005, does). The Via transport/sent-by are
explicit transport inputs (never guessed). Fakes use ``pbx.example.test``, ext
``1000``, and RFC 5737 ``198.51.100.x``.
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
    local_sent_by="198.51.100.7:5061",
    transport="TLS",
    expires=300,
)


def _challenge(code: int = 401, header: str = "WWW-Authenticate") -> SipResponse:
    return SipResponse.parse(
        f"SIP/2.0 {code} Unauthorized\r\n"
        f'{header}: Digest realm="pbx.example.test", nonce="abc123", '
        'algorithm=md5, qop="auth"\r\n'
        "Content-Length: 0\r\n\r\n"
    )


def _ok(cseq: int, contact_expires: str = ";expires=300") -> SipResponse:
    return SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        f"CSeq: {cseq} REGISTER\r\n"
        f"Contact: <sip:1000@198.51.100.7:5061;transport=tls>{contact_expires}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _h(msg: str, name: str) -> str | None:
    for line in msg.split("\r\n"):
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return None


def _cseq_num(msg: str) -> int:
    cseq = _h(msg, "CSeq")
    assert cseq is not None
    return int(cseq.split()[0])


def test_start_builds_initial_register_with_explicit_via() -> None:
    req = RegistrationFlow(_CONFIG).start()
    assert req.startswith("REGISTER sip:pbx.example.test SIP/2.0\r\n")
    assert "SIP/2.0/TLS 198.51.100.7:5061;branch=" in req
    assert _h(req, "CSeq") == "1 REGISTER"
    assert _h(req, "Expires") == "300"
    assert "Authorization" not in req


def test_401_yields_authed_register_with_incremented_cseq() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    outcome = flow.handle(_challenge())
    assert isinstance(outcome, Challenged)
    assert _h(outcome.request, "CSeq") == "2 REGISTER"
    auth = _h(outcome.request, "Authorization")
    assert auth is not None
    assert 'username="1000"' in auth
    assert re.search(r'response="[0-9a-f]{32}"', auth)


def test_407_uses_proxy_authorization() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    outcome = flow.handle(_challenge(code=407, header="Proxy-Authenticate"))
    assert isinstance(outcome, Challenged)
    assert _h(outcome.request, "Proxy-Authorization") is not None
    assert _h(outcome.request, "Authorization") is None  # 407 -> Proxy-Authorization


def test_call_id_and_from_tag_stable_across_requests() -> None:
    flow = RegistrationFlow(_CONFIG)
    first = flow.start()
    authed = flow.handle(_challenge())
    assert isinstance(authed, Challenged)
    assert _h(first, "Call-ID") == _h(authed.request, "Call-ID")
    assert _h(first, "From") == _h(authed.request, "From")


def test_200_yields_registered_with_granted_expiry() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    authed = flow.handle(_challenge())
    assert isinstance(authed, Challenged)
    outcome = flow.handle(
        _ok(_cseq_num(authed.request), contact_expires=";expires=120")
    )
    assert isinstance(outcome, Registered)
    assert outcome.expires == 120


def test_granted_expiry_param_is_case_insensitive() -> None:
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    outcome = flow.handle(_ok(_cseq_num(started), contact_expires=";Expires=90"))
    assert isinstance(outcome, Registered)
    assert outcome.expires == 90


def test_403_yields_failed() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    outcome = flow.handle(
        SipResponse.parse(
            "SIP/2.0 403 Forbidden\r\nCSeq: 1 REGISTER\r\nContent-Length: 0\r\n\r\n"
        )
    )
    assert isinstance(outcome, Failed)
    assert outcome.status == 403


def test_second_challenge_in_transaction_fails_no_loop() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    flow.handle(_challenge())  # -> Challenged
    outcome = flow.handle(_challenge())  # still 401 after auth, same transaction
    assert isinstance(outcome, Failed)
    assert outcome.status == 401


def test_refresh_after_registration_can_reauthenticate() -> None:
    # the regression the review caught: a later refresh must be able to re-auth
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    authed = flow.handle(_challenge())
    assert isinstance(authed, Challenged)
    assert isinstance(flow.handle(_ok(_cseq_num(authed.request))), Registered)
    # refresh: a brand-new REGISTER transaction
    refresh = flow.start()
    assert _cseq_num(refresh) > _cseq_num(authed.request)  # CSeq keeps climbing
    again = flow.handle(_challenge())
    assert isinstance(again, Challenged)  # not Failed — challenge state was per-txn


def test_challenged_deregister_keeps_expires_zero() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    a = flow.handle(_challenge())
    assert isinstance(a, Challenged)
    flow.handle(_ok(_cseq_num(a.request)))
    flow.deregister()
    challenged = flow.handle(_challenge())  # de-register gets challenged
    assert isinstance(challenged, Challenged)
    assert _h(challenged.request, "Expires") == "0"  # stays a de-registration


def test_handle_before_start_raises() -> None:
    flow = RegistrationFlow(_CONFIG)
    with pytest.raises(RuntimeError, match="no outstanding"):
        flow.handle(_ok(1))


def test_response_with_wrong_cseq_raises() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()  # CSeq 1 outstanding
    with pytest.raises(RuntimeError, match="CSeq"):
        flow.handle(_ok(99))  # response to some other transaction


def test_deregister_before_registration_raises() -> None:
    with pytest.raises(RuntimeError, match="not registered"):
        RegistrationFlow(_CONFIG).deregister()
