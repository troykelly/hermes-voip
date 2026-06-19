"""Tests for the RegistrationManager: N flows, demux, refresh (ADR-0011 PR7).

The manager owns one :class:`RegistrationFlow` per extension over a shared
``SipTransport`` and is the inbound demux hub: REGISTER responses route by
Call-ID, new INVITEs by Request-URI user-part (fallback To-AOR, then the default
registration), and in-dialog requests by dialog key. Logic is exercised against a
fake transport and a fake call consumer — no live socket (**invariant 2**).

Fakes only (``pbx.example.test``, ext ``1000``/``1001``, ``198.51.100.x``).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from hermes_voip.config import GatewayConfig, load_gateway_config
from hermes_voip.manager import (
    Cancel,
    InDialog,
    NewCall,
    RegistrationManager,
    Unroutable,
)
from hermes_voip.message import SipRequest, SipResponse

pytestmark = pytest.mark.asyncio


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    @property
    def local_sent_by(self) -> str:
        return "198.51.100.7:5061"

    def contact_uri(self, extension: str) -> str:
        return f"<sip:{extension}@198.51.100.7:5061;transport=tls>"


class _FakeConsumer:
    def __init__(self) -> None:
        self.received: list[SipRequest] = []

    async def handle_request(self, request: SipRequest) -> None:
        self.received.append(request)


def _gateway(**over: str) -> GatewayConfig:
    env = {
        "HERMES_SIP_HOST": "pbx.example.test",
        "HERMES_SIP_EXTENSION_1": "1000",
        "HERMES_SIP_PASSWORD_1": "p1",
        "HERMES_SIP_EXTENSION_2": "1001",
        "HERMES_SIP_PASSWORD_2": "p2",
    }
    env.update(over)
    return load_gateway_config(env)


def _ok_for(register_text: str, *, expires: int = 300) -> SipResponse:
    reg = SipRequest.parse(register_text)
    return SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        f"Call-ID: {reg.header('Call-ID')}\r\n"
        f"CSeq: {reg.header('CSeq')}\r\n"
        f"Contact: {reg.header('Contact')};expires={expires}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _challenge_for(register_text: str) -> SipResponse:
    reg = SipRequest.parse(register_text)
    return SipResponse.parse(
        "SIP/2.0 401 Unauthorized\r\n"
        f"Call-ID: {reg.header('Call-ID')}\r\n"
        f"CSeq: {reg.header('CSeq')}\r\n"
        'WWW-Authenticate: Digest realm="pbx.example.test", nonce="abc123", '
        'algorithm=md5, qop="auth"\r\n'
        "Content-Length: 0\r\n\r\n"
    )


def _invite(request_uri: str, to_uri: str) -> SipRequest:
    return SipRequest(
        method="INVITE",
        request_uri=request_uri,
        headers=(
            ("From", "<sip:caller@elsewhere.test>;tag=callertag"),
            ("To", f"<{to_uri}>"),
            ("Call-ID", "inbound-call-1"),
            ("CSeq", "1 INVITE"),
            ("Contact", "<sip:caller@198.51.100.200:5061>"),
        ),
        body="",
    )


def _bye(*, call_id: str, to_tag: str, from_tag: str) -> SipRequest:
    return SipRequest(
        method="BYE",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("From", f"<sip:caller@elsewhere.test>;tag={from_tag}"),
            ("To", f"<sip:1000@pbx.example.test>;tag={to_tag}"),
            ("Call-ID", call_id),
            ("CSeq", "2 BYE"),
        ),
        body="",
    )


# ---- construction + registration -------------------------------------------


async def test_builds_one_flow_per_extension() -> None:
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    assert len(manager.registration_call_ids) == 2
    snapshot = manager.snapshot()
    assert {s.extension for s in snapshot} == {"1000", "1001"}
    assert all(not s.registered for s in snapshot)
    assert manager.is_up is False


async def test_start_sends_one_register_per_extension() -> None:
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    await manager.start()
    assert len(transport.sent) == 2
    aors = "".join(transport.sent)
    assert "sip:1000@pbx.example.test" in aors
    assert "sip:1001@pbx.example.test" in aors


async def test_on_response_registers_and_marks_up() -> None:
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    await manager.start()
    await manager.on_response(_ok_for(transport.sent[0], expires=300))
    assert manager.is_up is True
    registered = [s for s in manager.snapshot() if s.registered]
    assert len(registered) == 1
    assert registered[0].expires == 300


async def test_on_response_logs_registration_established(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful REGISTER emits one INFO line on the manager logger.

    This is the operator-facing "it's working" signal (#30 gap #2): without it
    the gateway-login success is silent and runbooks have no log to point at. The
    line carries only the non-sensitive ``expires`` value — never the SIP host,
    extension, username, or password (rule 34).
    """
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    await manager.start()
    with caplog.at_level(logging.INFO, logger="hermes_voip.manager"):
        await manager.on_response(_ok_for(transport.sent[0], expires=299))

    records = [
        r
        for r in caplog.records
        if r.name == "hermes_voip.manager" and r.levelno == logging.INFO
    ]
    assert len(records) == 1, (
        "exactly one INFO registration-established line per successful REGISTER"
    )
    message = records[0].getMessage()
    # The expiry IS surfaced (it is not a secret) — operators read the refresh window.
    assert "299" in message
    # rule 34: the message must NOT leak any HERMES_SIP_* value. The fakes here are
    # the host, the extension/username, and the digest password from ``_gateway()``.
    for secret in ("pbx.example.test", "1000", "1001", "p1", "p2", "sip:"):
        assert secret not in message, f"registration log leaked {secret!r}"


async def test_on_response_refresh_does_not_re_log_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A REGISTER refresh of an already-up extension does not emit a 2nd INFO line.

    The "established" line marks the transition to registered; periodic refreshes
    (which also yield a ``Registered`` outcome) would otherwise spam INFO every
    half-expiry, so they log at DEBUG instead.
    """
    transport = _FakeTransport()
    # refresh_fraction=0.0 schedules the refresh REGISTER immediately after the
    # first registration, so we can answer that *new* REGISTER (a real refresh,
    # with its own CSeq) rather than replaying the first response.
    manager = RegistrationManager(_gateway(), transport, refresh_fraction=0.0)
    await manager.start()
    first_register = transport.sent[0]
    call_id = SipRequest.parse(first_register).header("Call-ID")
    with caplog.at_level(logging.DEBUG, logger="hermes_voip.manager"):
        # First REGISTER: the transition to up -> one INFO line.
        await manager.on_response(_ok_for(first_register, expires=300))
        # The refresh REGISTER fires immediately (refresh_fraction=0.0); answer it.
        await asyncio.sleep(0.05)
        refresh_register = next(
            m
            for m in transport.sent[1:]
            if SipRequest.parse(m).header("Call-ID") == call_id
        )
        await manager.on_response(_ok_for(refresh_register, expires=300))
    await manager.aclose()

    info = [
        r
        for r in caplog.records
        if r.name == "hermes_voip.manager" and r.levelno == logging.INFO
    ]
    debug = [
        r
        for r in caplog.records
        if r.name == "hermes_voip.manager" and r.levelno == logging.DEBUG
    ]
    assert len(info) == 1, "only the initial registration logs at INFO, not refreshes"
    assert len(debug) == 1, "the refresh is logged at DEBUG"


async def test_on_response_challenge_resends_authenticated() -> None:
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    await manager.start()
    sent_before = len(transport.sent)
    await manager.on_response(_challenge_for(transport.sent[0]))
    assert len(transport.sent) == sent_before + 1
    assert "Authorization:" in transport.sent[-1]


async def test_on_response_423_retries_with_min_expires() -> None:
    # A 423 Interval Too Brief yields a Retry outcome; the manager must resend the
    # retry REGISTER (else the registration silently never completes).
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    await manager.start()
    reg = SipRequest.parse(transport.sent[0])
    too_brief = SipResponse.parse(
        "SIP/2.0 423 Interval Too Brief\r\n"
        f"Call-ID: {reg.header('Call-ID')}\r\n"
        f"CSeq: {reg.header('CSeq')}\r\n"
        "Min-Expires: 3600\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    sent_before = len(transport.sent)
    await manager.on_response(too_brief)
    assert len(transport.sent) == sent_before + 1
    assert "Expires: 3600" in transport.sent[-1]  # retried with the server's minimum


async def test_on_response_unknown_call_id_raises() -> None:
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    await manager.start()
    stranger = SipResponse.parse(
        "SIP/2.0 200 OK\r\nCall-ID: not-ours\r\nCSeq: 1 REGISTER\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    with pytest.raises(KeyError):
        await manager.on_response(stranger)


async def test_connect_true_when_at_least_one_registers() -> None:
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    task = asyncio.create_task(manager.connect(timeout=2.0))
    await asyncio.sleep(0)  # let start() send the REGISTERs
    await manager.on_response(_ok_for(transport.sent[0]))
    assert await task is True


async def test_connect_false_on_timeout_with_no_registration() -> None:
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    assert await manager.connect(timeout=0.05) is False


# ---- inbound request demux (invariant 2) -----------------------------------


async def test_route_invite_by_request_uri_user_part() -> None:
    manager = RegistrationManager(_gateway(), _FakeTransport())
    routing = manager.route_request(
        _invite("sip:1001@198.51.100.7:5061", "sip:1001@pbx.example.test")
    )
    assert isinstance(routing, NewCall)
    assert routing.registration.extension == "1001"


async def test_route_invite_falls_back_to_to_aor_user_part() -> None:
    manager = RegistrationManager(_gateway(), _FakeTransport())
    # Request-URI has no user-part; the To AOR names extension 1001.
    routing = manager.route_request(
        _invite("sip:pbx.example.test", "sip:1001@pbx.example.test")
    )
    assert isinstance(routing, NewCall)
    assert routing.registration.extension == "1001"


async def test_route_invite_unknown_user_defaults_to_default_registration() -> None:
    manager = RegistrationManager(_gateway(), _FakeTransport())
    routing = manager.route_request(
        _invite("sip:9999@198.51.100.7:5061", "sip:9999@pbx.example.test")
    )
    assert isinstance(routing, NewCall)
    assert routing.registration.extension == "1000"  # the default (lowest index)


async def test_route_in_dialog_to_registered_call() -> None:
    manager = RegistrationManager(_gateway(), _FakeTransport())
    consumer = _FakeConsumer()
    dialog_id = ("call-7", "our-tag", "their-tag")
    manager.add_call(dialog_id, consumer)
    routing = manager.route_request(
        _bye(call_id="call-7", to_tag="our-tag", from_tag="their-tag")
    )
    assert isinstance(routing, InDialog)
    assert routing.consumer is consumer


async def test_route_in_dialog_without_session_is_unroutable() -> None:
    manager = RegistrationManager(_gateway(), _FakeTransport())
    routing = manager.route_request(_bye(call_id="ghost", to_tag="x", from_tag="y"))
    assert isinstance(routing, Unroutable)


def _cancel(*, call_id: str, branch: str, cseq_num: int = 1) -> SipRequest:
    # RFC 3261 §9.1: a CANCEL copies the INVITE's Request-URI, Call-ID, From, To
    # (no To-tag — it targets the pre-dialog INVITE transaction) and the top Via
    # branch, with the same CSeq number but method CANCEL.
    return SipRequest(
        method="CANCEL",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Via", f"SIP/2.0/TLS 198.51.100.200:5061;branch={branch};rport"),
            ("From", "<sip:caller@elsewhere.test>;tag=callertag"),
            ("To", "<sip:1000@pbx.example.test>"),
            ("Call-ID", call_id),
            ("CSeq", f"{cseq_num} CANCEL"),
        ),
        body="",
    )


async def test_route_out_of_dialog_cancel_is_classified_as_cancel() -> None:
    # RFC 3261 §9.2: a CANCEL targets a pending INVITE server transaction — it is
    # neither a NewCall nor an in-dialog request nor Unroutable. The manager must
    # classify it as Cancel so the transport can 200 the CANCEL + 487 the INVITE.
    manager = RegistrationManager(_gateway(), _FakeTransport())
    routing = manager.route_request(
        _cancel(call_id="inbound-call-1", branch="z9hG4bKc")
    )
    assert isinstance(routing, Cancel)
    assert routing.request.method == "CANCEL"


async def test_route_out_of_dialog_non_invite_is_unroutable() -> None:
    manager = RegistrationManager(_gateway(), _FakeTransport())
    options = SipRequest(
        method="OPTIONS",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("From", "<sip:probe@elsewhere.test>;tag=p"),
            ("To", "<sip:1000@pbx.example.test>"),
            ("Call-ID", "probe-1"),
            ("CSeq", "1 OPTIONS"),
        ),
        body="",
    )
    assert isinstance(manager.route_request(options), Unroutable)


async def test_remove_call_makes_in_dialog_unroutable() -> None:
    manager = RegistrationManager(_gateway(), _FakeTransport())
    dialog_id = ("call-7", "our-tag", "their-tag")
    manager.add_call(dialog_id, _FakeConsumer())
    manager.remove_call(dialog_id)
    routing = manager.route_request(
        _bye(call_id="call-7", to_tag="our-tag", from_tag="their-tag")
    )
    assert isinstance(routing, Unroutable)


# ---- refresh + shutdown ----------------------------------------------------


async def test_refresh_resends_register() -> None:
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport, refresh_fraction=0.0)
    await manager.start()
    first_register = transport.sent[0]
    call_id = SipRequest.parse(first_register).header("Call-ID")
    await manager.on_response(_ok_for(first_register))
    # refresh_fraction=0.0 schedules the refresh immediately; let it run.
    await asyncio.sleep(0.05)
    refreshes = [
        m for m in transport.sent if SipRequest.parse(m).header("Call-ID") == call_id
    ]
    assert len(refreshes) >= 2  # the initial REGISTER plus the refresh
    await manager.aclose()


class _FlakyTransport(_FakeTransport):
    """A transport whose ``send`` fails once it has sent ``fail_after`` messages."""

    def __init__(self, *, fail_after: int) -> None:
        super().__init__()
        self._fail_after = fail_after

    async def send(self, message: str) -> None:
        if len(self.sent) >= self._fail_after:
            msg = "transport down"
            raise RuntimeError(msg)
        self.sent.append(message)


async def test_refresh_failure_marks_down_and_is_reported() -> None:
    # A refresh that fails to send must not be swallowed (rule 37): the
    # registration is marked down and the error surfaced, never left as a lost
    # background-task exception (codex HIGH).
    errors: list[tuple[str, BaseException]] = []
    transport = _FlakyTransport(fail_after=2)  # 2 initial REGISTERs ok; refresh fails
    manager = RegistrationManager(
        _gateway(),
        transport,
        refresh_fraction=0.0,
        on_registration_error=lambda ext, exc: errors.append((ext, exc)),
    )
    await manager.start()
    await manager.on_response(_ok_for(transport.sent[0]))
    await asyncio.sleep(0.05)  # the refresh fires and its send() raises
    assert errors
    extension, error = errors[0]
    assert extension == "1000"
    assert isinstance(error, RuntimeError)
    down = next(s for s in manager.snapshot() if s.extension == "1000")
    assert down.registered is False
    await manager.aclose()


async def test_aclose_cancels_refresh_tasks() -> None:
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport, refresh_fraction=0.5)
    await manager.start()
    await manager.on_response(_ok_for(transport.sent[0]))
    await manager.aclose()  # must not raise
    assert manager.is_up is True


# ---- recovery: a registrar-rejected refresh (item 1) -----------------------


def _failed_for(register_text: str, *, status: int, reason: str) -> SipResponse:
    """Build a 4xx/5xx/6xx final to the REGISTER in ``register_text``."""
    reg = SipRequest.parse(register_text)
    return SipResponse.parse(
        f"SIP/2.0 {status} {reason}\r\n"
        f"Call-ID: {reg.header('Call-ID')}\r\n"
        f"CSeq: {reg.header('CSeq')}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


async def test_failed_refresh_reports_and_schedules_reregister() -> None:
    # RFC 3261 §10: a registrar that REJECTS a periodic refresh (503/403/...) must
    # NOT silently take the extension down forever. The manager reports the failure
    # via on_registration_error AND schedules a bounded-backoff re-REGISTER, so the
    # binding recovers instead of dead-ending (the audit's terminal-silent gap).
    errors: list[tuple[str, BaseException]] = []
    transport = _FakeTransport()
    manager = RegistrationManager(
        _gateway(),
        transport,
        refresh_fraction=0.0,  # refresh fires immediately after registration
        retry_backoff=0.0,  # and the recovery re-REGISTER fires immediately too
        on_registration_error=lambda ext, exc: errors.append((ext, exc)),
    )
    await manager.start()
    first_register = transport.sent[0]
    call_id = SipRequest.parse(first_register).header("Call-ID")
    await manager.on_response(_ok_for(first_register))  # 1000 is now up
    # The refresh REGISTER fires (refresh_fraction=0.0); reject it with a 503.
    await asyncio.sleep(0.05)
    refresh = next(
        m
        for m in transport.sent[1:]
        if SipRequest.parse(m).header("Call-ID") == call_id
    )
    sent_before = len(transport.sent)
    await manager.on_response(
        _failed_for(refresh, status=503, reason="Service Unavailable")
    )
    # The failure is surfaced (never swallowed, rule 37) ...
    assert errors, "a rejected refresh must be reported via on_registration_error"
    ext, exc = errors[0]
    assert ext == "1000"
    assert isinstance(exc, Exception)
    # ... the extension is marked down ...
    down = next(s for s in manager.snapshot() if s.extension == "1000")
    assert down.registered is False
    # ... and a recovery re-REGISTER is scheduled and sent (not a dead-end).
    await asyncio.sleep(0.05)
    recovery = [
        m
        for m in transport.sent[sent_before:]
        if SipRequest.parse(m).header("Call-ID") == call_id
        and SipRequest.parse(m).method == "REGISTER"
    ]
    assert recovery, "a rejected refresh must schedule a re-REGISTER (bounded backoff)"
    await manager.aclose()


async def test_failed_refresh_recovers_back_to_registered() -> None:
    # The recovery re-REGISTER, once the registrar accepts it again, brings the
    # extension back up — end-to-end proof the dead-end is gone, not just that a
    # retry was emitted.
    transport = _FakeTransport()
    manager = RegistrationManager(
        _gateway(),
        transport,
        refresh_fraction=0.0,
        retry_backoff=0.0,
    )
    await manager.start()
    first_register = transport.sent[0]
    call_id = SipRequest.parse(first_register).header("Call-ID")
    await manager.on_response(_ok_for(first_register))
    await asyncio.sleep(0.05)
    refresh = next(
        m
        for m in transport.sent[1:]
        if SipRequest.parse(m).header("Call-ID") == call_id
    )
    sent_before = len(transport.sent)
    await manager.on_response(
        _failed_for(refresh, status=500, reason="Server Internal Error")
    )
    await asyncio.sleep(0.05)
    recovery = next(
        m
        for m in transport.sent[sent_before:]
        if SipRequest.parse(m).header("Call-ID") == call_id
        and SipRequest.parse(m).method == "REGISTER"
    )
    # The registrar now accepts the recovery REGISTER.
    await manager.on_response(_ok_for(recovery))
    up = next(s for s in manager.snapshot() if s.extension == "1000")
    assert up.registered is True
    await manager.aclose()


# ---- recovery: a refresh that gets NO response (item 2, Timer-F/B) ----------


async def test_refresh_with_no_response_times_out_and_reregisters() -> None:
    # RFC 3261 Timer F/B: a refresh REGISTER that gets NO response at all must not
    # leave the binding marked 'registered' forever. After a bounded response
    # timeout the manager marks the extension down, reports it, and re-registers —
    # rather than trusting a binding that may already have lapsed at the registrar.
    errors: list[tuple[str, BaseException]] = []
    transport = _FakeTransport()
    manager = RegistrationManager(
        _gateway(),
        transport,
        refresh_fraction=0.0,  # refresh fires immediately
        refresh_timeout=0.05,  # ... and times out fast (no response is fed)
        retry_backoff=0.0,
        on_registration_error=lambda ext, exc: errors.append((ext, exc)),
    )
    await manager.start()
    first_register = transport.sent[0]
    call_id = SipRequest.parse(first_register).header("Call-ID")
    await manager.on_response(_ok_for(first_register))  # up
    assert next(s for s in manager.snapshot() if s.extension == "1000").registered
    sent_before = len(transport.sent)
    # Deliberately feed NO response to the refresh; the timeout must fire.
    await asyncio.sleep(0.2)
    # The stale 'registered' is cleared ...
    down = next(s for s in manager.snapshot() if s.extension == "1000")
    assert down.registered is False, "a refresh with no response must not stay 'up'"
    # ... the timeout is surfaced ...
    assert errors, "a refresh response-timeout must be reported, never swallowed"
    assert errors[0][0] == "1000"
    # ... and a fresh REGISTER is scheduled (recovery, not permanent stale-down).
    recovery = [
        m
        for m in transport.sent[sent_before:]
        if SipRequest.parse(m).header("Call-ID") == call_id
        and SipRequest.parse(m).method == "REGISTER"
    ]
    assert recovery, "a refresh response-timeout must trigger a re-REGISTER"
    await manager.aclose()


async def test_refresh_response_cancels_the_timeout() -> None:
    # The Timer-F/B response timeout must NOT fire when the refresh DOES get a
    # timely 200 OK — otherwise every healthy refresh would spuriously flap the
    # registration down. With refresh_fraction=0.0 a fresh refresh fires the
    # instant the previous one is answered, so this models a healthy registrar
    # that ALWAYS answers promptly: every REGISTER it emits in the window gets an
    # immediate 200 OK. The Timer-F/B deadline must therefore never fire — no
    # error, still registered — proving a timely response disarms it.
    errors: list[tuple[str, BaseException]] = []
    transport = _FakeTransport()
    manager = RegistrationManager(
        _gateway(),
        transport,
        refresh_fraction=0.0,
        refresh_timeout=0.05,
        retry_backoff=0.0,
        on_registration_error=lambda ext, exc: errors.append((ext, exc)),
    )
    await manager.start()
    first_register = transport.sent[0]
    call_id = SipRequest.parse(first_register).header("Call-ID")
    await manager.on_response(_ok_for(first_register))
    seen = 1  # index 0 is the initial REGISTER, answered on the line above
    # Promptly answer every refresh for a window several Timer-F/B periods long —
    # a healthy, always-answering registrar. Walk the send log by index so a
    # REGISTER appended while we answer the previous one is not skipped.
    deadline = asyncio.get_running_loop().time() + 0.3
    while asyncio.get_running_loop().time() < deadline:
        while seen < len(transport.sent):
            message = transport.sent[seen]
            seen += 1
            parsed = SipRequest.parse(message)
            if parsed.method == "REGISTER" and parsed.header("Call-ID") == call_id:
                await manager.on_response(_ok_for(message))
        await asyncio.sleep(0.01)
    assert seen >= 2, "the refresh cascade must have fired (sanity)"
    assert not errors, "a refresh that got a 200 OK must not also time out"
    up = next(s for s in manager.snapshot() if s.extension == "1000")
    assert up.registered is True
    await manager.aclose()
