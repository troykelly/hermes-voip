"""Tests for hermes_voip.registration — the sans-IO SIP REGISTER flow.

The flow produces request wire text and consumes parsed responses; it owns no
socket or timer (the transport, ADR-0005, does). The Via transport/sent-by are
explicit transport inputs (never guessed). Fakes use ``pbx.example.test``, ext
``1000``, and RFC 5737 ``198.51.100.x``.
"""

import re

import pytest

import hermes_voip
from hermes_voip.digest import (
    DigestChallenge,
    DigestCredentials,
    build_authorization,
)
from hermes_voip.message import SipResponse
from hermes_voip.registration import (
    Challenged,
    Failed,
    Registered,
    RegistrationConfig,
    RegistrationFlow,
    Retry,
    ViaTransport,
)

# The canonical fixture registers over TLS, so its AOR uses the mandated ``sips:``
# scheme (ADR-0005/ADR-0080): a ``sip:`` AOR on TLS is now rejected at construction.
# The Contact keeps its own scheme (the bk231 gate is AOR-vs-transport only).
_CONFIG = RegistrationConfig(
    aor="sips:1000@pbx.example.test",
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


def _qopless_challenge(opaque: str | None = None) -> SipResponse:
    """A 401 with NO qop (RFC 2069 legacy MD5), optionally carrying an opaque.

    Every other fixture offers ``qop="auth"``; this exercises the registration↔
    digest seam on the qop-less path (no nc/cnonce, 32-hex response).
    """
    opaque_param = f', opaque="{opaque}"' if opaque is not None else ""
    return SipResponse.parse(
        "SIP/2.0 401 Unauthorized\r\n"
        f'WWW-Authenticate: Digest realm="pbx.example.test", nonce="abc123", '
        f"algorithm=md5{opaque_param}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _stale_challenge(nonce: str) -> SipResponse:
    """A second 401 advertising ``stale=true`` with a FRESH nonce (RFC 7616 §3.5)."""
    return SipResponse.parse(
        "SIP/2.0 401 Unauthorized\r\n"
        f'WWW-Authenticate: Digest realm="pbx.example.test", nonce="{nonce}", '
        'algorithm=md5, qop="auth", stale=true\r\n'
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


def _via_branch(msg: str) -> str:
    """Extract the ``branch`` token from the REGISTER's single Via header."""
    via = _h(msg, "Via")
    assert via is not None
    match = re.search(r";branch=([^;]+)", via)
    assert match is not None
    return match.group(1)


def test_start_builds_initial_register_with_explicit_via() -> None:
    req = RegistrationFlow(_CONFIG).start()
    assert req.startswith("REGISTER sips:pbx.example.test SIP/2.0\r\n")
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


def _multi_challenge(code: int = 401, header: str = "WWW-Authenticate") -> SipResponse:
    """A 401/407 offering MD5 *first*, then SHA-256 (RFC 8760 §2.4 back-compat).

    An on-path attacker can reorder or strip challenges (SIP digest has no header
    integrity), so listing the weaker algorithm first must NOT downgrade the
    client off the strongest algorithm it supports.
    """
    return SipResponse.parse(
        f"SIP/2.0 {code} Unauthorized\r\n"
        f'{header}: Digest realm="pbx.example.test", nonce="abc123", '
        'algorithm=MD5, qop="auth"\r\n'
        f'{header}: Digest realm="pbx.example.test", nonce="abc123", '
        'algorithm=SHA-256, qop="auth"\r\n'
        "Content-Length: 0\r\n\r\n"
    )


def _auth_param(auth: str, name: str) -> str | None:
    """Extract a single auth-param (quoted or bare) from a Digest header value."""
    match = re.search(rf'{name}=(?:"([^"]*)"|([^,\s]+))', auth)
    if match is None:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def test_multiple_challenges_select_strongest_not_first() -> None:
    """RFC 8760 §2.4: with MD5 *and* SHA-256 offered, authenticate with SHA-256.

    Guards against a silent SHA-256 -> MD5 downgrade when a gateway (or on-path
    attacker) lists MD5 first: the client must pick the strongest algorithm it
    supports, not whichever challenge happens to be parsed first.
    """
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    outcome = flow.handle(_multi_challenge())
    assert isinstance(outcome, Challenged)
    auth = _h(outcome.request, "Authorization")
    assert auth is not None
    # The chosen algorithm must be SHA-256, not the MD5 listed first.
    assert _auth_param(auth, "algorithm") == "SHA-256"
    # 64 hex digits is a SHA-256 digest; 32 would be MD5.
    response = _auth_param(auth, "response")
    assert response is not None
    assert re.fullmatch(r"[0-9a-f]{64}", response)
    # The digest must equal the independently-computed SHA-256 value (not MD5).
    cnonce = _auth_param(auth, "cnonce")
    assert cnonce is not None
    expected = build_authorization(
        DigestChallenge(
            realm="pbx.example.test",
            nonce="abc123",
            algorithm="SHA-256",
            qop=("auth",),
        ),
        DigestCredentials("1000", "s3cr3t"),
        method="REGISTER",
        uri="sips:pbx.example.test",
        cnonce=cnonce,
    )
    expected_response = _auth_param(expected, "response")
    assert expected_response is not None
    assert response == expected_response


def test_multiple_proxy_challenges_select_strongest_not_first() -> None:
    """A 407 with MD5-first / SHA-256 must also pick SHA-256 (handled symmetrically)."""
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    outcome = flow.handle(_multi_challenge(code=407, header="Proxy-Authenticate"))
    assert isinstance(outcome, Challenged)
    auth = _h(outcome.request, "Proxy-Authorization")
    assert auth is not None
    assert _auth_param(auth, "algorithm") == "SHA-256"
    response = _auth_param(auth, "response")
    assert response is not None
    assert re.fullmatch(r"[0-9a-f]{64}", response)


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


def test_granted_expiry_selects_our_contact_in_multi_binding_200() -> None:
    # RFC 3261 §10.3: a 200 OK to REGISTER echoes EVERY binding the registrar
    # holds for the AOR — other devices' Contacts too, each with its OWN expires.
    # The refresh timer must be armed from OUR binding's lifetime, not whichever
    # Contact comes first; arming off another device's (here much longer) expiry
    # would skip our own refresh and let our binding silently lapse.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    # Two other-device Contacts (one as a leading header, one comma-joined inside
    # our own Contact line) bracket ours — our binding is the config's contact URI
    # (host 198.51.100.7:5061), whose expires=120 is the value to arm from.
    multi = SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        f"CSeq: {_cseq_num(started)} REGISTER\r\n"
        "Contact: <sip:1000@198.51.100.9:5061;transport=tls>;expires=600\r\n"
        "Contact: <sip:1000@198.51.100.7:5061;transport=tls>;expires=120, "
        "<sip:1000@198.51.100.8:5062;transport=tls>;expires=900\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    outcome = flow.handle(multi)
    assert isinstance(outcome, Registered)
    assert outcome.expires == 120  # OUR binding, not 600/900 from other devices


def test_granted_expiry_falls_back_to_first_contact_when_ours_absent() -> None:
    # Defensive: if the registrar's echo omits our exact Contact URI (a rewritten
    # or normalised binding), fall back to the first Contact's expires so a refresh
    # is still armed rather than the flow crashing — a slightly-off refresh beats
    # none. A real registrar echoes our Contact verbatim; this is belt-and-braces.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    other = SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        f"CSeq: {_cseq_num(started)} REGISTER\r\n"
        "Contact: <sip:1000@198.51.100.99:5061;transport=tls>;expires=222\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    outcome = flow.handle(other)
    assert isinstance(outcome, Registered)
    assert outcome.expires == 222


def test_granted_expiry_matches_contact_differing_host_case_and_default_port() -> None:
    # RFC 3261 §19.1.4: two SIP-URIs are equivalent when scheme matches
    # case-insensitively, userinfo case-sensitively, host case-insensitively, and
    # an omitted port equals the scheme default (5060 sip / 5061 sips). A registrar
    # that echoes our binding with an UPPER-CASE host AND an explicit default port
    # (``sip:1000@PBX.EXAMPLE.TEST:5061``) when our Contact omitted the port and
    # lower-cased the host (``sip:1000@pbx.example.test``) is still OUR binding. With
    # raw string equality the match misses and the flow falls back to the FIRST
    # (other device's) binding — arming the wrong refresh timer and letting our
    # binding silently lapse. The selected lifetime must be OUR 120, not the 600.
    cfg = RegistrationConfig(
        aor="sips:1000@pbx.example.test",
        username="1000",
        password="s3cr3t",
        contact="<sip:1000@pbx.example.test;transport=tls>",
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
        expires=300,
    )
    flow = RegistrationFlow(cfg)
    started = flow.start()
    echoed = SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        f"CSeq: {_cseq_num(started)} REGISTER\r\n"
        "Contact: <sip:1000@198.51.100.9:5061;transport=tls>;expires=600\r\n"
        "Contact: <sip:1000@PBX.EXAMPLE.TEST:5061;transport=tls>;expires=120\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    outcome = flow.handle(echoed)
    assert isinstance(outcome, Registered)
    assert (
        outcome.expires == 120
    )  # OUR binding by §19.1.4 equality, not the 600 fallback


def test_granted_expiry_matches_sips_contact_with_explicit_default_port_5061() -> None:
    # §19.1.4 companion for the sips: scheme — default port 5061. Our Contact uses a
    # sips: addr-spec with no explicit port; the registrar echoes it with the explicit
    # default ``:5061``. The two URIs are equivalent, so OUR 90 is selected over the
    # leading other-device binding's 600.
    cfg = RegistrationConfig(
        aor="sips:1000@pbx.example.test",
        username="1000",
        password="s3cr3t",
        contact="<sips:1000@pbx.example.test;transport=tls>",
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
        expires=300,
    )
    flow = RegistrationFlow(cfg)
    started = flow.start()
    echoed = SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        f"CSeq: {_cseq_num(started)} REGISTER\r\n"
        "Contact: <sips:1000@198.51.100.9:5061;transport=tls>;expires=600\r\n"
        "Contact: <sips:1000@pbx.example.test:5061;transport=tls>;expires=90\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    outcome = flow.handle(echoed)
    assert isinstance(outcome, Registered)
    assert outcome.expires == 90  # OUR sips binding by §19.1.4 equality, not the 600


def test_200_granting_zero_expires_to_a_registration_is_failed_not_registered() -> None:
    # RFC 3261 §10.3: a registrar MAY grant a SHORTER lifetime than requested,
    # including 0 — which means it REMOVED our binding (a de-registration we did
    # not ask for). A 2xx that echoes OUR Contact with expires=0 must therefore
    # NOT be a Registered outcome (arming a 0-second refresh would busy-loop
    # re-REGISTERing a binding the registrar keeps tearing down). It is surfaced
    # as Failed so the manager treats it as an anomaly, not a live registration.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    outcome = flow.handle(_ok(_cseq_num(started), contact_expires=";expires=0"))
    assert isinstance(outcome, Failed), (
        "a non-positive granted lifetime is a binding removal, not a registration"
    )
    # The status is non-2xx (so a downstream RegistrationRejectedError is honest)
    # and the reason names the anomaly without leaking any SIP host/extension.
    assert outcome.status not in range(200, 300)
    assert "expires" in outcome.reason.lower()


def test_200_echoing_negative_expires_on_our_binding_is_failed_not_registered() -> None:
    # codex MUST-FIX 1: RFC 3261 §10.2/§10.3 — Expires is a non-negative
    # delta-seconds, so a NEGATIVE value on OUR returned Contact (e.g. ``expires=-1``)
    # is MALFORMED. The old expires regex matched only ``\d+``, so a negative token
    # did NOT parse and ``_granted_expires`` silently fell back to the positive
    # REQUESTED lifetime — yielding ``Registered`` and arming a refresh off a binding
    # the registrar effectively did not grant. Fail closed: a malformed/negative
    # expires on our binding is treated the SAME as a non-positive grant (Failed),
    # never a silent positive fallback.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    outcome = flow.handle(_ok(_cseq_num(started), contact_expires=";expires=-1"))
    assert isinstance(outcome, Failed), (
        "a negative (malformed) expires on our binding must be Failed, "
        "not Registered with a positive fallback"
    )
    assert outcome.status not in range(200, 300)
    assert "expires" in outcome.reason.lower()


def test_200_echoing_non_numeric_expires_on_our_binding_is_failed() -> None:
    # codex MUST-FIX 1: a non-numeric expires token (``expires=abc``) on our returned
    # binding is equally malformed. It must NOT silently fall back to the positive
    # requested lifetime; it is surfaced as Failed (fail-closed), mirroring the
    # negative-expires case.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    outcome = flow.handle(_ok(_cseq_num(started), contact_expires=";expires=abc"))
    assert isinstance(outcome, Failed), (
        "a non-numeric (malformed) expires on our binding must be Failed, "
        "not Registered with a positive fallback"
    )
    assert outcome.status not in range(200, 300)
    assert "expires" in outcome.reason.lower()


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
    assert outcome.reason == "Forbidden"


def test_second_challenge_in_transaction_fails_no_loop() -> None:
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    flow.handle(_challenge())  # -> Challenged
    outcome = flow.handle(_challenge())  # still 401 after auth, same transaction
    assert isinstance(outcome, Failed)
    assert outcome.status == 401
    assert outcome.reason == "Unauthorized"


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


def test_deregister_resets_registered_state() -> None:
    # After a successful registration, deregister() -> 200 OK sets _registered=False.
    # A second deregister() call must then raise RuntimeError (not registered),
    # distinct from test_deregister_before_registration_raises which tests the
    # initial state. This guards the idempotency guard: once unregistered, the
    # flow cannot be de-registered again without a fresh registration.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    flow.handle(_ok(_cseq_num(started)))  # -> Registered, sets _registered=True
    dereg = flow.deregister()  # -> 200 OK, should clear _registered
    flow.handle(_ok(_cseq_num(dereg)))  # accept the de-registration
    # Direct assertion: _registered must be False after the 200 OK is accepted.
    assert flow._registered is False
    # Indirect guard: a second deregister() must raise (idempotency guard).
    with pytest.raises(RuntimeError, match="not registered"):
        flow.deregister()


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


@pytest.mark.parametrize("transport", ["TLS", "WSS"])
def test_config_rejects_sip_aor_on_secure_transport(transport: ViaTransport) -> None:
    # bk231 / ADR-0080: ADR-0005 mandates SIP-over-TLS (SIPS). A bare ``sip:`` AOR
    # on a TLS/WSS transport is internally inconsistent — the registrar request-URI
    # and digest uri would advertise an insecure scheme over a secure leg with no
    # signal. Fail fast at construction, mirroring the bk226 AOR validation, rather
    # than surfacing later as a confusing gateway rejection.
    with pytest.raises(ValueError, match=r"sips"):
        RegistrationConfig(
            aor="sip:1000@pbx.example.test",
            username="1000",
            password="s3cr3t",
            contact="<sips:1000@198.51.100.7:5061>",
            local_sent_by="198.51.100.7:5061",
            transport=transport,
        )


def test_config_rejects_sip_aor_on_secure_transport_case_insensitive() -> None:
    # The transport token is normalised to uppercase in __post_init__ before the
    # secure-transport check runs: a lower-case ``tls`` input must trigger the
    # sip:-AOR rejection identically to ``TLS``, proving normalisation fires before
    # the check. The stored .transport normalisation itself is asserted separately
    # in test_config_normalises_lowercase_transport_to_uppercase.
    with pytest.raises(ValueError, match=r"sips"):
        RegistrationConfig(
            aor="sip:1000@pbx.example.test",
            username="1000",
            password="s3cr3t",
            contact="<sips:1000@198.51.100.7:5061>",
            local_sent_by="198.51.100.7:5061",
            transport="tls",  # type: ignore[arg-type]  # intentional: passes lowercase to test normalisation
        )


@pytest.mark.parametrize("transport", ["UDP", "TCP"])
def test_config_accepts_sip_aor_on_insecure_transport(transport: ViaTransport) -> None:
    # bk231 is TRANSPORT-GATED: UDP/TCP leave the AOR scheme to the deployer (the
    # SIPS mandate is an invariant only on the secure TLS/WSS transports). A ``sip:``
    # AOR on UDP/TCP is accepted and drives a ``sip:`` registrar request-URI.
    cfg = RegistrationConfig(
        aor="sip:1000@pbx.example.test",
        username="1000",
        password="s3cr3t",
        contact="<sip:1000@198.51.100.7:5060>",
        local_sent_by="198.51.100.7:5060",
        transport=transport,
    )
    assert (
        RegistrationFlow(cfg)
        .start()
        .startswith("REGISTER sip:pbx.example.test SIP/2.0\r\n")
    )


def test_config_accepts_sips_aor_on_insecure_transport() -> None:
    # The complement of the gate: a ``sips:`` AOR is accepted on ANY transport
    # (upgrading the scheme is never the inconsistency bk231 guards against).
    cfg = RegistrationConfig(
        aor="sips:1000@pbx.example.test",
        username="1000",
        password="s3cr3t",
        contact="<sips:1000@198.51.100.7:5061>",
        local_sent_by="198.51.100.7:5061",
        transport="UDP",
    )
    assert (
        RegistrationFlow(cfg)
        .start()
        .startswith("REGISTER sips:pbx.example.test SIP/2.0\r\n")
    )


def test_authed_register_pins_nc_00000001() -> None:
    # bk239 / ADR-0080: each REGISTER is a fresh transaction answering the challenge
    # it just received, with a fresh cnonce and nc=00000001 (RFC 7616 §3.4 — correct
    # for a purely-reactive flow with no reused nonce to count against). Pin nc so a
    # refactor cannot silently drift it; we deliberately do NOT thread an nc counter.
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    outcome = flow.handle(_challenge())
    assert isinstance(outcome, Challenged)
    auth = _h(outcome.request, "Authorization")
    assert auth is not None
    assert _auth_param(auth, "nc") == "00000001"


def test_via_branch_changes_between_initial_and_authed_register() -> None:
    # bk243 / RFC 3261 §8.1.1.7: the authed REGISTER is a new client transaction and
    # MUST carry a new Via branch, while Call-ID and From-tag stay stable (same
    # registration). ``_build`` calls new_branch() per send; pin it so a hoist into
    # __init__ cannot silently break transaction matching.
    flow = RegistrationFlow(_CONFIG)
    first = flow.start()
    authed = flow.handle(_challenge())
    assert isinstance(authed, Challenged)
    first_branch = _via_branch(first)
    authed_branch = _via_branch(authed.request)
    assert first_branch.startswith("z9hG4bK")
    assert authed_branch.startswith("z9hG4bK")
    assert first_branch != authed_branch
    # Call-ID and From-tag are stable across the re-authentication (same dialog).
    assert _h(first, "Call-ID") == _h(authed.request, "Call-ID")
    assert _h(first, "From") == _h(authed.request, "From")


def test_qop_less_rfc2069_digest_path_at_registration_level() -> None:
    # bk247 / ADR-0080: registration-level coverage of the RFC 2069 legacy MD5 path
    # (digest.py exists but was untested through the flow). A qop-less challenge must
    # yield an Authorization with NO nc/cnonce and a 32-hex (MD5) response.
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    outcome = flow.handle(_qopless_challenge())
    assert isinstance(outcome, Challenged)
    auth = _h(outcome.request, "Authorization")
    assert auth is not None
    assert _auth_param(auth, "qop") is None
    assert _auth_param(auth, "nc") is None
    assert _auth_param(auth, "cnonce") is None
    response = _auth_param(auth, "response")
    assert response is not None
    assert re.fullmatch(r"[0-9a-f]{32}", response)
    # The digest must equal the independently-computed RFC 2069 legacy MD5 value.
    expected = build_authorization(
        DigestChallenge(realm="pbx.example.test", nonce="abc123", algorithm="md5"),
        DigestCredentials("1000", "s3cr3t"),
        method="REGISTER",
        uri="sips:pbx.example.test",
    )
    expected_response = _auth_param(expected, "response")
    assert expected_response is not None
    assert response == expected_response


def test_opaque_is_echoed_through_the_flow() -> None:
    # bk247 / ADR-0080: an opaque sent in the challenge MUST be echoed back unchanged
    # in the Authorization (RFC 7616 §3.4) — pin the registration↔digest seam.
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    outcome = flow.handle(_qopless_challenge(opaque="op4que-XYZ"))
    assert isinstance(outcome, Challenged)
    auth = _h(outcome.request, "Authorization")
    assert auth is not None
    assert _auth_param(auth, "opaque") == "op4que-XYZ"


def test_stale_second_challenge_still_fails_recorded_limitation() -> None:
    # bk250 / ADR-0080: INTENTIONAL, RECORDED LIMITATION. A second 401 in a
    # transaction — even one advertising stale=true with a FRESH nonce — is treated
    # as Failed; the flow does NOT perform an in-transaction stale-nonce retry.
    # Recovery is the RegistrationManager's next refresh: a brand-new transaction
    # that answers the fresh nonce. This test pins that we fail on the second 401
    # even when it is stale, so the limitation cannot be silently "fixed" or broken.
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    first = flow.handle(_challenge(header="WWW-Authenticate"))
    assert isinstance(first, Challenged)  # answered the first challenge
    second = flow.handle(_stale_challenge(nonce="fresh-nonce-2"))
    assert isinstance(second, Failed)
    assert second.status == 401
    # And the documented recovery path works: a fresh refresh re-authenticates.
    refresh = flow.start()
    assert _cseq_num(refresh) > _cseq_num(first.request)
    assert isinstance(flow.handle(_challenge()), Challenged)


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


# ---- transport Literal validation (bk236) -----------------------------------


def test_config_rejects_unknown_transport() -> None:
    # bk236: transport is a Literal["TLS","WSS","UDP","TCP"]. An unrecognised
    # token such as "QUIC" would be injected verbatim into the Via header and
    # silently produce a malformed request. Reject at construction instead.
    with pytest.raises(ValueError, match="transport must be one of"):
        RegistrationConfig(
            aor="sip:1000@pbx.example.test",
            username="1000",
            password="s3cr3t",
            contact="<sip:1000@198.51.100.7:5060>",
            local_sent_by="198.51.100.7:5060",
            transport="QUIC",  # type: ignore[arg-type]  # intentionally wrong type
        )


def test_config_normalises_lowercase_transport_to_uppercase() -> None:
    # bk236 / MUST-FIX 1: RegistrationConfig accepts lowercase transport tokens
    # (e.g. "tls") by normalising them to the canonical uppercase ViaTransport
    # Literal form in __post_init__. The stored .transport must be "TLS", not "tls",
    # so the field always satisfies the Literal["TLS","WSS","UDP","TCP"] contract at
    # runtime (not just at static-type-check time).
    cfg = RegistrationConfig(
        aor="sips:1000@pbx.example.test",
        username="1000",
        password="s3cr3t",
        contact="<sip:1000@198.51.100.7:5061;transport=tls>",
        local_sent_by="198.51.100.7:5061",
        transport="tls",  # type: ignore[arg-type]  # intentional: passes lowercase to test normalisation
    )
    assert cfg.transport == "TLS"


# ---- expires non-negative validation (bk236) --------------------------------


def test_config_rejects_negative_expires() -> None:
    # bk236: a negative expires would produce "Expires: -1" on the wire, which
    # confuses registrars and is semantically invalid (RFC 3261 §10.2). Reject
    # at construction so the error surfaces early rather than mid-flow.
    with pytest.raises(ValueError, match=r"expires"):
        RegistrationConfig(
            aor="sips:1000@pbx.example.test",
            username="1000",
            password="s3cr3t",
            contact="<sip:1000@198.51.100.7:5061;transport=tls>",
            local_sent_by="198.51.100.7:5061",
            transport="TLS",
            expires=-1,
        )


def test_config_rejects_large_negative_expires() -> None:
    # Belt-and-braces: any negative value is rejected, not just -1.
    with pytest.raises(ValueError, match=r"expires"):
        RegistrationConfig(
            aor="sips:1000@pbx.example.test",
            username="1000",
            password="s3cr3t",
            contact="<sip:1000@198.51.100.7:5061;transport=tls>",
            local_sent_by="198.51.100.7:5061",
            transport="TLS",
            expires=-300,
        )


def test_config_accepts_zero_expires() -> None:
    # expires=0 is a legal de-registration request (RFC 3261 §10.2); it must
    # not be rejected by the negative-expires guard.
    cfg = RegistrationConfig(
        aor="sips:1000@pbx.example.test",
        username="1000",
        password="s3cr3t",
        contact="<sip:1000@198.51.100.7:5061;transport=tls>",
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
        expires=0,
    )
    assert cfg.expires == 0


# ---- 2xx success range (backlog 262) ----------------------------------------


def _2xx(cseq: int, code: int, reason: str = "Accepted") -> SipResponse:
    """A non-200 2xx response (e.g. 202 Accepted) to a REGISTER."""
    return SipResponse.parse(
        f"SIP/2.0 {code} {reason}\r\n"
        f"CSeq: {cseq} REGISTER\r\n"
        f"Contact: <sip:1000@198.51.100.7:5061;transport=tls>;expires=300\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def test_202_accepted_yields_registered() -> None:
    # backlog 262: any 2xx (200 <= status < 300) must be treated as success.
    # A 202 Accepted from some RFC-compliant registrar implementations must not
    # be mishandled as a failure (the old code compared status == 200 exactly).
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    outcome = flow.handle(_2xx(_cseq_num(started), 202))
    assert isinstance(outcome, Registered)


def test_204_no_content_yields_registered() -> None:
    # backlog 262: 204 is another non-200 2xx that must also yield Registered.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    outcome = flow.handle(_2xx(_cseq_num(started), 204, "No Content"))
    assert isinstance(outcome, Registered)


# ---- CSeq method validation (backlog 266) ------------------------------------


def _ok_with_cseq_method(cseq: int, method: str) -> SipResponse:
    """A 200 OK whose CSeq uses the given method (not necessarily REGISTER)."""
    return SipResponse.parse(
        f"SIP/2.0 200 OK\r\n"
        f"CSeq: {cseq} {method}\r\n"
        f"Contact: <sip:1000@198.51.100.7:5061;transport=tls>;expires=300\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def test_cseq_method_invite_raises() -> None:
    # backlog 266: a response whose CSeq number matches but whose method is NOT
    # REGISTER is a protocol error and must raise RuntimeError.  The old code
    # only checked the sequence number and silently accepted an INVITE/BYE/etc.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    cseq_num = _cseq_num(started)
    with pytest.raises(RuntimeError, match="CSeq"):
        flow.handle(_ok_with_cseq_method(cseq_num, "INVITE"))


def test_cseq_method_bye_raises() -> None:
    # backlog 266: same guard for BYE — any non-REGISTER method is rejected.
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    cseq_num = _cseq_num(started)
    with pytest.raises(RuntimeError, match="CSeq"):
        flow.handle(_ok_with_cseq_method(cseq_num, "BYE"))


# ---- missing/garbled CSeq behaviour (backlog 269) ---------------------------


def _ok_without_cseq(expires: int = 300) -> SipResponse:
    """A 200 OK with NO CSeq header (malformed/absent, as some broken proxies emit)."""
    return SipResponse.parse(
        f"SIP/2.0 200 OK\r\n"
        f"Contact: <sip:1000@198.51.100.7:5061;transport=tls>;expires={expires}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def test_missing_cseq_is_tolerated_and_yields_registered() -> None:
    # backlog 269: a response with NO CSeq header cannot be correlated by sequence
    # number, but we tolerate the omission (a real registrar always echoes CSeq; a
    # broken proxy may strip it).  Current behaviour: silently accept and return
    # Registered.  This test PINS that leniency so it cannot change unintentionally.
    flow = RegistrationFlow(_CONFIG)
    flow.start()
    outcome = flow.handle(_ok_without_cseq())
    assert isinstance(outcome, Registered)


# ---- Unicode-digit crash guard (isdigit → isascii+isdecimal) -----------------
#
# str.isdigit() returns True for Unicode superscript digits such as U+00B2 (²)
# but int(chr(0xB2)) raises ValueError.  The three isdigit() guards in
# registration.py must use isascii()+isdecimal() (the framing.py precedent) so
# a malformed expires carrying a Unicode digit does NOT propagate a bare
# ValueError through _granted_expires → _handle_success → handle, which would
# tear down the SIP connection on the transport async read loop.


def test_granted_expires_unicode_digit_contact_param_does_not_crash() -> None:
    # U+00B2 (²) passes str.isdigit() but raises ValueError from int(); a Contact
    # expires param carrying this codepoint must be treated as malformed (the same
    # as any other non-ASCII-digit value such as "abc") — fail-closed to Failed —
    # and the handler must NOT raise a bare ValueError.
    unicode_digit = chr(0xB2)
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    # Build a 200 OK whose Contact has expires=² (Unicode superscript two)
    response = SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        f"CSeq: {_cseq_num(started)} REGISTER\r\n"
        "Contact: <sip:1000@198.51.100.7:5061;transport=tls>"
        f";expires={unicode_digit}\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    # Must not raise; malformed expires on our binding → fail-closed → Failed
    outcome = flow.handle(response)
    assert isinstance(outcome, Failed)
    assert outcome.status not in range(200, 300)
    assert "expires" in outcome.reason.lower()


def test_granted_expires_unicode_digit_expires_header_does_not_crash() -> None:
    # U+00B2 (²) in the Expires header (no Contact expires param) must also be
    # treated as absent/invalid — the handler must NOT raise a bare ValueError and
    # must fall back to the configured expires, returning Registered.
    unicode_digit = chr(0xB2)
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    response = SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        f"CSeq: {_cseq_num(started)} REGISTER\r\n"
        f"Contact: <sip:1000@198.51.100.7:5061;transport=tls>\r\n"
        f"Expires: {unicode_digit}\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    # Must not raise; malformed Expires header → skipped → config expires (300)
    outcome = flow.handle(response)
    assert isinstance(outcome, Registered)
    assert outcome.expires == _CONFIG.expires


def test_min_expires_unicode_digit_does_not_crash() -> None:
    # U+00B2 (²) in a 423 Min-Expires header must be treated as absent/invalid —
    # _min_expires() must return None rather than raising ValueError, so the flow
    # falls back to a safe Failed outcome (no valid retry interval can be computed).
    unicode_digit = chr(0xB2)
    flow = RegistrationFlow(_CONFIG)
    started = flow.start()
    response = SipResponse.parse(
        "SIP/2.0 423 Interval Too Brief\r\n"
        f"CSeq: {_cseq_num(started)} REGISTER\r\n"
        f"Min-Expires: {unicode_digit}\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    # Must not raise; _min_expires returns None → flow treats 423 without a
    # valid Min-Expires as Failed (no valid retry interval can be computed).
    outcome = flow.handle(response)
    assert not isinstance(outcome, Registered)
