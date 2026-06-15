"""Tests for hermes_voip.registration — the sans-IO SIP REGISTER flow.

The flow produces request wire text and consumes parsed responses; it owns no
socket or timer (the transport, ADR-0005, does). The Via transport/sent-by are
explicit transport inputs (never guessed). Fakes use ``pbx.example.test``, ext
``1000``, and RFC 5737 ``198.51.100.x``.
"""

import re

import pytest

import hermes_voip
from hermes_voip.message import SipResponse
from hermes_voip.registration import (
    Challenged,
    Failed,
    Registered,
    RegistrationConfig,
    RegistrationFlow,
    Retry,
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


def _too_brief(cseq: int, min_expires: str | None = "3600") -> SipResponse:
    min_line = f"Min-Expires: {min_expires}\r\n" if min_expires is not None else ""
    return SipResponse.parse(
        "SIP/2.0 423 Interval Too Brief\r\n"
        f"CSeq: {cseq} REGISTER\r\n"
        f"{min_line}"
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


def test_call_id_matches_the_wire_and_is_stable() -> None:
    # The manager demuxes REGISTER responses by Call-ID (ADR-0011), so the
    # property must equal the Call-ID actually used on the wire.
    flow = RegistrationFlow(_CONFIG)
    req = flow.start()
    assert flow.call_id == _h(req, "Call-ID")
    # Stable across re-authentication and re-registration (same registration).
    challenged = flow.handle(_challenge())
    assert isinstance(challenged, Challenged)
    assert _h(challenged.request, "Call-ID") == flow.call_id
    refresh = flow.start()
    assert _h(refresh, "Call-ID") == flow.call_id


def test_distinct_flows_have_distinct_call_ids() -> None:
    # Each extension's registration is an independent transaction space.
    assert RegistrationFlow(_CONFIG).call_id != RegistrationFlow(_CONFIG).call_id


def test_423_retries_with_min_expires() -> None:
    # A registrar enforcing a minimum interval returns 423 + Min-Expires; the
    # flow must re-issue REGISTER with the larger interval (a new transaction),
    # not give up — a silent total outage otherwise.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    outcome = flow.handle(_too_brief(_cseq_num(started), min_expires="3600"))
    assert isinstance(outcome, Retry)
    assert _h(outcome.request, "Expires") == "3600"  # bumped to Min-Expires
    assert _cseq_num(outcome.request) > _cseq_num(started)  # fresh transaction
    # the retried REGISTER then completes normally
    assert isinstance(flow.handle(_ok(_cseq_num(outcome.request))), Registered)


def test_423_keeps_our_interval_when_min_expires_is_smaller() -> None:
    # max(requested, min_expires): a Min-Expires below what we already asked for
    # cannot be the reason for the 423, so there is nothing larger to retry with.
    flow = RegistrationFlow(_CONFIG)  # requests 300
    started = flow.start()
    outcome = flow.handle(_too_brief(_cseq_num(started), min_expires="60"))
    assert isinstance(outcome, Failed)
    assert outcome.status == 423


def test_423_without_min_expires_fails() -> None:
    # 423 with no Min-Expires gives nothing to comply with -> Failed, not a loop.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    outcome = flow.handle(_too_brief(_cseq_num(started), min_expires=None))
    assert isinstance(outcome, Failed)
    assert outcome.status == 423


def test_second_423_after_retry_fails_no_loop() -> None:
    # A registrar that 423s again after we honoured Min-Expires must not loop us.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    retry = flow.handle(_too_brief(_cseq_num(started), min_expires="3600"))
    assert isinstance(retry, Retry)
    again = flow.handle(_too_brief(_cseq_num(retry.request), min_expires="7200"))
    assert isinstance(again, Failed)
    assert again.status == 423


def test_deregister_423_fails_rather_than_extending() -> None:
    # A 423 on a de-registration (Expires: 0) must not be "fixed" by bumping the
    # interval, which would silently turn a de-register into a registration.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    flow.handle(_ok(_cseq_num(started)))
    dereg = flow.deregister()
    outcome = flow.handle(_too_brief(_cseq_num(dereg), min_expires="3600"))
    assert isinstance(outcome, Failed)
    assert outcome.status == 423


@pytest.mark.parametrize(
    "aor",
    [
        "",
        ":",
        "1000@pbx.example.test",
        "sip:",
        "sip:1000@",
        "ftp:1000@pbx.example.test",
    ],
)
def test_config_rejects_malformed_aor(aor: str) -> None:
    # A malformed AOR would corrupt the request-URI and the digest uri and only
    # surface much later as a confusing gateway rejection; fail fast instead.
    with pytest.raises(ValueError, match=r"aor|scheme|host"):
        RegistrationConfig(
            aor=aor,
            username="1000",
            password="s3cr3t",
            contact="<sip:1000@198.51.100.7:5061;transport=tls>",
            local_sent_by="198.51.100.7:5061",
        )


def test_config_accepts_sips_aor() -> None:
    # sips: is valid (ADR-0005's SIP-over-TLS mandate); the registrar URI keeps
    # the scheme and drops the user/params.
    cfg = RegistrationConfig(
        aor="sips:1000@pbx.example.test;transport=tls",
        username="1000",
        password="s3cr3t",
        contact="<sips:1000@198.51.100.7:5061>",
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
    )
    req = RegistrationFlow(cfg).start()
    assert req.startswith("REGISTER sips:pbx.example.test SIP/2.0\r\n")


def test_registration_public_api_is_importable_from_package() -> None:
    # The plugin's reason to exist is registration; its API must be reachable
    # from the package root, not only via the deep module path (ADR-0011).
    for name in (
        "RegistrationFlow",
        "RegistrationConfig",
        "Challenged",
        "Registered",
        "Failed",
        "Retry",
    ):
        assert hasattr(hermes_voip, name), name
        assert name in hermes_voip.__all__
