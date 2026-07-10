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
import json
import logging
import re
from collections.abc import Callable

import pytest

import hermes_voip.manager as manager_module
from hermes_voip.config import GatewayConfig, load_gateway_config
from hermes_voip.manager import (
    _MIN_REFRESH_DELAY,
    Cancel,
    InDialog,
    NewCall,
    RegistrationError,
    RegistrationFailureCategory,
    RegistrationManager,
    RegistrationRejectedError,
    RegistrationTimeoutError,
    Unroutable,
    registration_failure_category,
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


def _disable_refresh_floor(manager: RegistrationManager) -> RegistrationManager:
    """Private test seam: drop the production refresh floor for an immediate refresh.

    The PUBLIC ``min_refresh_delay`` knob hard-enforces ``> 0`` (ADR-0088 / codex
    MUST-FIX 2) — it can never be set to a guard-defeating ``0``. Tests that drive a
    refresh by hand and want it to fire immediately (``refresh_fraction=0.0`` makes
    the nominal delay ``0``, which the floor would otherwise lift to ``1 s``) reach
    past the public knob via the private ``_min_refresh_delay`` attribute. This
    preserves the original immediate-refresh intent of these tests without
    re-opening the public bypass the floor exists to close.
    """
    manager._min_refresh_delay = 0.0
    return manager


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


# Standard LogRecord attributes (Python 3.13). Subtracting this set from
# ``record.__dict__`` isolates the code-attached ``extra`` fields. A secret reaches the
# log ONLY via a code-controlled surface -- the rendered message, the ``%`` args, an
# ``extra`` field, a logged exception (``exc_text`` or the raw ``exc_info`` value), a
# captured traceback (``stack_info``), or a CUSTOM task name -- the scan covers those.
# The rest is framework metadata (timestamps, thread/process ids, source location, the
# default "Task-<n>" counter) that never carries a secret and whose nondeterministic
# numeric values would only false-trip a short-digit scan.
_STANDARD_LOGRECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
    }
)

#: FRAMEWORK-DEFAULT auto-names for asyncio tasks / threads / processes: fixed labels
#: (``MainThread``/``MainProcess``) or monotonic counters (``Task-<n>``/``Thread-<n>``)
#: that carry no user data but can coincidentally contain a short secret digit like
#: "1000". A ``taskName``/``threadName``/``processName`` that does NOT match is
#: application-set (e.g. ``create_task(name=f"dial-{ext}")``) and MAY carry a target.
_FRAMEWORK_AUTO_NAME = re.compile(
    r"MainThread|MainProcess"
    r"|(Task|Thread|Process|SpawnProcess|ForkProcess|SpawnPoolWorker"
    r"|ForkPoolWorker|ThreadPoolExecutor|Dummy|asyncio)[-_].*"
)


def _custom_auto_name(value: object) -> str:
    """The value of a LogRecord auto-name, scanned only when NOT a framework default.

    A custom task/thread/process name can carry a dial target; the framework default
    is a fixed label or a counter that would false-trip a short-digit scan.
    """
    return (
        value
        if isinstance(value, str) and _FRAMEWORK_AUTO_NAME.fullmatch(value) is None
        else ""
    )


def _assert_failure_log_is_secret_safe(record: logging.LogRecord) -> None:
    """Assert no fake secret leaks into any CODE-CONTROLLED surface of ``record``.

    Deterministic: scans only the surfaces a logging call populates -- code-attached
    ``extra`` fields, the rendered message, the ``%`` args, a logged exception rendered
    via ``formatException`` (``exc_text``/``exc_info``, incl. its ``__cause__`` chain)
    with its ``stack_info``, and any CUSTOM asyncio-task/thread/process name. It never
    scans framework metadata: numeric ids/timestamps, the source location, or the
    framework-DEFAULT auto-names (``MainThread`` / ``Task-<n>`` ...), whose fixed or
    counter values would coincidentally match a short digit like "1000" and flake. This
    package logs from the default main thread/process and unnamed tasks, never naming a
    thread/process with caller data; the one auto-name that CAN be user-set is scanned
    when custom, so such a leak is still caught. ``None`` auto-names carry nothing.
    """
    extra_fields = {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_LOGRECORD_ATTRS
    }
    # ``exc_text`` is populated only once a Formatter runs; on a raw captured record a
    # log with ``exc_info=True`` leaves ``exc_text`` None. Render ``exc_info`` and scan
    # it -- ``formatException`` follows the full ``__cause__`` / ``__context__`` /
    # ``ExceptionGroup`` chain (the real log output), so a secret in a chained or
    # wrapped exception is caught too, with no timestamps/counters to false-trip.
    formatted_exc = (
        logging.Formatter().formatException(record.exc_info)
        if isinstance(record.exc_info, tuple) and record.exc_info[1] is not None
        else ""
    )
    scanned_surfaces = {
        "extra fields": json.dumps(extra_fields, sort_keys=True, default=repr),
        "rendered message": record.getMessage(),
        "message args": json.dumps(record.args, default=repr) if record.args else "",
        "custom task name": _custom_auto_name(record.__dict__.get("taskName")),
        "custom thread name": _custom_auto_name(record.__dict__.get("threadName")),
        "custom process name": _custom_auto_name(record.__dict__.get("processName")),
        "exception text": record.exc_text or "",
        "exception value": formatted_exc,
        "stack info": record.stack_info or "",
    }
    for secret in ("pbx.example.test", "1000", "1001", "p1", "p2"):
        for surface_name, surface in scanned_surfaces.items():
            assert secret not in surface, (
                f"structured failure log leaked secret {secret!r} in {surface_name}"
            )


def _make_clean_record(**overrides: object) -> logging.LogRecord:
    """A WARNING failure LogRecord with NO secret in any surface, plus per-test knobs.

    ``overrides`` are written into ``record.__dict__`` (how logging attaches ``extra``
    fields and framework metadata), so a test can set ``taskName`` / ``relativeCreated``
    / an extra field without tripping attribute typing. Framework metadata is pinned to
    a deterministic baseline first, so async auto-population can't slip a real name in.
    """
    record = logging.LogRecord(
        name="hermes_voip.manager",
        level=logging.WARNING,
        pathname="manager.py",
        lineno=1,
        msg="SIP registration failed: rejected (status=503, attempt=1)",
        args=None,
        exc_info=None,
    )
    record.__dict__.update({"taskName": None, "relativeCreated": 0.0, **overrides})
    return record


async def test_secret_scan_ignores_framework_metadata() -> None:
    """No false-trip on framework metadata that coincidentally contains "1000".

    The framework-DEFAULT task/thread/process auto-names (``Task-<n>``/``Thread-<n>``/
    ``...PoolWorker-<n>``) are monotonic counters that climb past ``1000``/``1001``
    mid-suite, and numeric fields (``relativeCreated`` etc.) vary per run -- all are
    framework-set, never secrets, so none may trip the scan.
    """
    record = _make_clean_record(
        taskName="Task-1000",
        threadName="Thread-1000 (worker)",
        processName="SpawnPoolWorker-1001",
        relativeCreated=21000.5,
    )
    _assert_failure_log_is_secret_safe(record)  # must NOT raise


async def test_secret_scan_catches_leak_in_rendered_message() -> None:
    """A secret rendered into the message (via ``%`` args) is still caught."""
    record = _make_clean_record(msg="dialing %s failed", args=("1000",))
    with pytest.raises(AssertionError, match=r"leaked secret '1000'"):
        _assert_failure_log_is_secret_safe(record)


async def test_secret_scan_catches_leak_in_extra_field() -> None:
    """A secret in a code-attached ``extra`` field is still caught."""
    record = _make_clean_record(extension="1000")
    with pytest.raises(AssertionError, match=r"leaked secret '1000'"):
        _assert_failure_log_is_secret_safe(record)


async def test_secret_scan_catches_leak_in_custom_task_name() -> None:
    """A secret leaked ONLY via a CUSTOM asyncio task name is caught (codex concern)."""
    record = _make_clean_record(taskName="dial-1000")
    with pytest.raises(AssertionError, match=r"leaked secret '1000'"):
        _assert_failure_log_is_secret_safe(record)


async def test_secret_scan_catches_leak_in_custom_thread_or_process_name() -> None:
    """A secret in a CUSTOM thread or process name is caught (codex must-fix).

    ``threadName``/``processName`` are application-settable (``Thread(name=...)`` /
    ``multiprocessing`` process names) just like ``taskName``; a custom value carrying a
    dial target must be caught while the framework defaults stay excluded.
    """
    with pytest.raises(
        AssertionError, match=r"leaked secret '1000' in custom thread name"
    ):
        _assert_failure_log_is_secret_safe(_make_clean_record(threadName="dial-1000"))
    with pytest.raises(
        AssertionError, match=r"leaked secret '1001' in custom process name"
    ):
        _assert_failure_log_is_secret_safe(_make_clean_record(processName="dial-1001"))


async def test_secret_scan_catches_leak_in_exception_text() -> None:
    """A secret in a logged exception's rendered text is caught (codex must-fix).

    A failure log that adds ``exc_info=True`` renders the exception into
    ``record.exc_text``; a registrar- or dial-target string captured there is a
    genuine leak, so it must be scanned like the message, args, and extra fields.
    """
    record = _make_clean_record(
        exc_text="Traceback: ConnectionError contacting pbx.example.test:5061",
    )
    with pytest.raises(
        AssertionError, match=r"leaked secret 'pbx\.example\.test' in exception text"
    ):
        _assert_failure_log_is_secret_safe(record)


async def test_secret_scan_catches_leak_in_stack_info() -> None:
    """A secret captured in ``stack_info`` (``stack_info=True``) is caught."""
    record = _make_clean_record(
        stack_info='Stack (most recent call last):\n  dialing "1001"',
    )
    with pytest.raises(AssertionError, match=r"leaked secret '1001' in stack info"):
        _assert_failure_log_is_secret_safe(record)


async def test_secret_scan_catches_leak_in_unformatted_exception() -> None:
    """A logged exception's message is caught even before ``exc_text`` is formatted.

    ``caplog`` captures RAW records: with ``exc_info=True`` the record carries an
    ``exc_info`` tuple but ``exc_text`` stays ``None`` until a Formatter runs, so an
    ``exc_text``-only scan would miss it. The exception is rendered via
    ``Formatter().formatException`` and scanned too.
    """
    exc = ConnectionError("cannot reach pbx.example.test:5061")
    record = _make_clean_record(exc_info=(type(exc), exc, exc.__traceback__))
    assert record.exc_text is None  # precondition: exception not yet formatted
    with pytest.raises(
        AssertionError, match=r"leaked secret 'pbx\.example\.test' in exception value"
    ):
        _assert_failure_log_is_secret_safe(record)


def _chained_secret_exception() -> RuntimeError:
    """A ``RuntimeError('registration failed')`` whose ``__cause__`` carries a secret.

    Equivalent to ``raise RuntimeError(...) from ConnectionError('...secret...')`` but
    built without a live ``raise`` -- the wrapper's own message is sanitized, so only a
    scan that follows ``__cause__`` sees the secret.
    """
    wrapper = RuntimeError("registration failed")
    wrapper.__cause__ = ConnectionError("cannot reach pbx.example.test:5061")
    return wrapper


async def test_secret_scan_catches_leak_in_chained_exception() -> None:
    """A secret in a CHAINED exception's cause is caught (codex must-fix).

    A sanitized wrapper logged with ``exc_info=True`` still renders its ``__cause__``
    into the formatted traceback, so a secret in the cause is a genuine leak. Scanning
    only the top-level exception value would miss it; ``Formatter().formatException``
    follows the ``__cause__`` chain, matching the real log output.
    """
    exc = _chained_secret_exception()
    assert "pbx.example.test" not in str(exc)  # the wrapper itself is sanitized
    record = _make_clean_record(exc_info=(type(exc), exc, exc.__traceback__))
    with pytest.raises(
        AssertionError, match=r"leaked secret 'pbx\.example\.test' in exception value"
    ):
        _assert_failure_log_is_secret_safe(record)


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


@pytest.mark.parametrize("bad_floor", [0.0, -0.5, -1.0, float("nan")])
async def test_min_refresh_delay_must_be_positive(bad_floor: float) -> None:
    # codex MUST-FIX 2: the refresh floor is the guard that stops a tiny/zero granted
    # lifetime arming a near-zero-delay refresh that hot-loops the registrar
    # (ADR-0088). A 0 (or negative) ``min_refresh_delay`` DEFEATS that guard, so the
    # public knob must hard-enforce ``> 0`` at construction — it can never be set to
    # a guard-defeating value. Tests that want an immediate hand-driven refresh use a
    # PRIVATE seam (``_disable_refresh_floor``), not this public knob.
    #
    # codex follow-up: NaN must ALSO be rejected. ``nan <= 0`` is False, so a naive
    # ``<= 0`` check would let NaN slip the positive-floor contract and later poison
    # ``max(nan, x)`` / ``asyncio.sleep(nan)`` in the scheduler. The validation is
    # fail-closed (``not (min_refresh_delay > 0)``), which catches NaN, 0, and
    # negatives alike (``nan > 0`` is False).
    with pytest.raises(ValueError, match=r"min_refresh_delay"):
        RegistrationManager(_gateway(), _FakeTransport(), min_refresh_delay=bad_floor)


async def test_start_sends_one_register_per_extension() -> None:
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    await manager.start()
    assert len(transport.sent) == 2
    aors = "".join(transport.sent)
    # ADR-0080: the AOR uses the mandated ``sips:`` scheme on the TLS transport.
    assert "sips:1000@pbx.example.test" in aors
    assert "sips:1001@pbx.example.test" in aors


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
    assert records[0].__dict__["event"] == "sip_registration_established"
    assert records[0].__dict__["expires_s"] == 299
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
    # _disable_refresh_floor drops the production floor so this deliberately-immediate
    # test refresh is not lifted to 1 s (the floor guards a tiny granted lifetime, not
    # a test that drives the refresh by hand) — the private seam, not the public knob.
    manager = _disable_refresh_floor(
        RegistrationManager(_gateway(), transport, refresh_fraction=0.0)
    )
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
    assert debug[0].__dict__["event"] == "sip_registration_refreshed"
    assert debug[0].__dict__["expires_s"] == 300


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
    # _disable_refresh_floor: see the note in test_on_response_refresh_does_not_re_log
    # — the production floor guards a tiny grant, not this hand-driven refresh.
    manager = _disable_refresh_floor(
        RegistrationManager(_gateway(), transport, refresh_fraction=0.0)
    )
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
    manager = _disable_refresh_floor(  # private seam: immediate hand-driven refresh
        RegistrationManager(
            _gateway(),
            transport,
            refresh_fraction=0.0,
            on_registration_error=lambda ext, exc: errors.append((ext, exc)),
        )
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
    manager = _disable_refresh_floor(  # private seam: immediate hand-driven refresh
        RegistrationManager(
            _gateway(),
            transport,
            refresh_fraction=0.0,  # refresh fires immediately after registration
            retry_backoff=0.0,  # and the recovery re-REGISTER fires immediately too
            on_registration_error=lambda ext, exc: errors.append((ext, exc)),
        )
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
    manager = _disable_refresh_floor(  # private seam: immediate hand-driven refresh
        RegistrationManager(
            _gateway(),
            transport,
            refresh_fraction=0.0,
            retry_backoff=0.0,
        )
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


def _single_gateway() -> GatewayConfig:
    return load_gateway_config(
        {
            "HERMES_SIP_HOST": "pbx.example.test",
            "HERMES_SIP_EXTENSION": "1000",
            "HERMES_SIP_PASSWORD": "p1",
        }
    )


async def test_is_up_false_after_sole_extensions_refresh_fails() -> None:
    # is_up must reflect live registration state: when the only extension's
    # refresh is rejected, the manager is no longer up (codex review — guards a
    # future refactor that might back is_up by the one-shot connect() event).
    transport = _FakeTransport()
    manager = _disable_refresh_floor(  # private seam: immediate hand-driven refresh
        RegistrationManager(
            _single_gateway(),
            transport,
            refresh_fraction=0.0,
            retry_backoff=10.0,  # keep recovery from re-registering during the assert
        )
    )
    await manager.start()
    first = transport.sent[0]
    call_id = SipRequest.parse(first).header("Call-ID")
    await manager.on_response(_ok_for(first))
    up_after_register = manager.is_up
    assert up_after_register is True
    await asyncio.sleep(0.02)
    refresh = next(
        m
        for m in transport.sent[1:]
        if SipRequest.parse(m).header("Call-ID") == call_id
    )
    await manager.on_response(_failed_for(refresh, status=403, reason="Forbidden"))
    up_after_failure = manager.is_up
    assert up_after_failure is False, "the manager is down once its sole binding fails"
    await manager.aclose()


class _SendFailsOnceTransport(_FakeTransport):
    """A transport whose ``send`` raises on the nth message, then works again.

    Used to prove a recovery re-REGISTER whose *send* fails still schedules
    another bounded-backoff attempt (it must not dead-end — codex review).
    """

    def __init__(self, *, fail_on_index: int) -> None:
        super().__init__()
        self._fail_on_index = fail_on_index
        self.attempts = 0

    async def send(self, message: str) -> None:
        self.attempts += 1
        if self.attempts == self._fail_on_index:
            msg = "transient transport send failure"
            raise RuntimeError(msg)
        self.sent.append(message)


async def test_recovery_send_failure_reschedules_another_attempt() -> None:
    # A recovery re-REGISTER whose transport.send() raises must not dead-end: the
    # manager must schedule a further bounded-backoff attempt (codex finding). The
    # 3rd send (initial=1, refresh=2, first recovery=3) fails; a later send proves
    # recovery continued.
    errors: list[tuple[str, BaseException]] = []
    transport = _SendFailsOnceTransport(fail_on_index=3)
    manager = _disable_refresh_floor(  # private seam: immediate hand-driven refresh
        RegistrationManager(
            _single_gateway(),
            transport,
            refresh_fraction=0.0,
            retry_backoff=0.0,
            on_registration_error=lambda ext, exc: errors.append((ext, exc)),
        )
    )
    await manager.start()  # send #1 (initial REGISTER)
    first = transport.sent[0]
    call_id = SipRequest.parse(first).header("Call-ID")
    await manager.on_response(_ok_for(first))
    await asyncio.sleep(0.02)  # send #2 = the refresh
    refresh = next(
        m
        for m in transport.sent[1:]
        if SipRequest.parse(m).header("Call-ID") == call_id
    )
    sent_before = transport.attempts
    # Reject the refresh -> recovery scheduled. Its send (#3) raises; recovery must
    # reschedule and the next attempt (#4) succeeds in reaching the wire.
    await manager.on_response(_failed_for(refresh, status=500, reason="Server Error"))
    await asyncio.sleep(0.2)
    assert transport.attempts > sent_before + 1, (
        "a recovery send failure must trigger a further re-REGISTER attempt, "
        "not dead-end the registration"
    )
    # The error reporting fired for both the rejection and the failed send.
    assert len(errors) >= 2
    await manager.aclose()


# ---- a registrar that grants a non-positive (0) lifetime --------------------


async def test_zero_granted_expires_does_not_arm_a_busyloop_refresh() -> None:
    # RFC 3261 §10.3: a registrar MAY echo OUR binding with expires=0 in a 200 OK,
    # which means it REMOVED the binding. Treating that as a live Registered would
    # arm a refresh whose delay = 0 * fraction = 0s, firing an immediate
    # re-REGISTER -> the registrar grants 0 again -> a TIGHT re-REGISTER loop that
    # floods the gateway and never keeps the binding up. The manager must instead
    # treat the non-positive grant as a FAILURE, never arm a <=0-delay refresh, and
    # surface the anomaly (rule 37) — proven by: no flood of REGISTERs, the
    # extension is NOT registered, and the failure is reported.
    errors: list[tuple[str, BaseException]] = []
    transport = _FakeTransport()
    manager = RegistrationManager(
        _single_gateway(),
        transport,
        refresh_fraction=0.5,
        retry_backoff=10.0,  # keep cold-start/recovery from re-REGISTERing in-window
        on_registration_error=lambda ext, exc: errors.append((ext, exc)),
    )
    await manager.start()
    first = transport.sent[0]
    sent_after_initial = len(transport.sent)
    # The registrar grants 0 — our binding was removed, not registered.
    await manager.on_response(_ok_for(first, expires=0))
    # No live registration: the manager must not report itself up off a removed
    # binding ...
    assert manager.is_up is False, "a 0-expires grant is not a live registration"
    down = next(s for s in manager.snapshot() if s.extension == "1000")
    assert down.registered is False
    # ... the anomaly is surfaced, never swallowed ...
    assert errors, "a non-positive granted lifetime must be reported (rule 37)"
    assert errors[0][0] == "1000"
    # ... and NO refresh task is armed at all (DETERMINISTIC: assert the scheduling
    # DECISION, not a wall-clock window). A 0-grant must route through Failed, so
    # _schedule_refresh is never called and no <=0-delay refresh task exists — the
    # stronger, race-free proof that no busy-loop can fire. (This is a cold-start
    # failure, so recovery is not scheduled either; recovery is connect()'s concern.)
    state = manager._by_extension["1000"]
    assert state.refresh_task is None, (
        "a 0-expires grant must NOT arm a refresh task (no 0-delay re-REGISTER loop)"
    )
    assert state.recovery_task is None, (
        "a cold-start 0-grant failure must not schedule recovery here"
    )
    # Drain the event loop once; with no refresh/recovery armed, no further REGISTER
    # can have been emitted (belt-and-braces over the scheduling-decision assertions).
    await asyncio.sleep(0)
    sent_after = [
        m
        for m in transport.sent[sent_after_initial:]
        if SipRequest.parse(m).method == "REGISTER"
    ]
    assert not sent_after, (
        f"a 0-expires grant must NOT emit any further REGISTER; saw {len(sent_after)}"
    )
    await manager.aclose()


async def test_tiny_positive_grant_refresh_is_clamped_to_a_positive_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defence-in-depth for _schedule_refresh: even a *positive* but tiny granted
    # lifetime (e.g. 1s) must not arm a sub-second refresh. The scheduler clamps the
    # refresh delay to a sane positive minimum, so a tiny grant does NOT produce a
    # near-immediate re-REGISTER. The flow stays registered (a positive grant IS a
    # registration); only the refresh cadence is floored.
    #
    # codex MUST-FIX 3: assert the COMPUTED refresh delay directly rather than racing
    # wall-clock time (the old test slept 0.7s hoping an unclamped 0.5s refresh would
    # have fired — flaky under CI load). A capturing stub records the exact delay
    # _schedule_refresh passes to _refresh_after; we assert it equals the production
    # floor, deterministically and with no real sleeping.
    captured: list[float] = []

    async def _capture_delay(state: object, delay: float) -> None:
        captured.append(delay)

    transport = _FakeTransport()
    manager = RegistrationManager(
        _single_gateway(),
        transport,
        refresh_fraction=0.5,  # 1s grant -> 0.5s nominal, below the positive floor
    )
    # Replace the refresh body with the capturing stub (private seam): _schedule_refresh
    # still computes and passes the clamped delay, but no REGISTER is sent and nothing
    # sleeps, so the assertion is on the value, not the clock.
    monkeypatch.setattr(manager, "_refresh_after", _capture_delay)
    await manager.start()
    first = transport.sent[0]
    await manager.on_response(_ok_for(first, expires=1))
    up = next(s for s in manager.snapshot() if s.extension == "1000")
    assert up.registered is True, "a positive grant is a live registration"
    await asyncio.sleep(0)  # let the scheduled (no-op) refresh task run
    # 1s * 0.5 = 0.5s nominal is below the floor, so the scheduled delay is the floor
    # itself — NOT the sub-second nominal value that would hot-loop the registrar.
    assert captured == [_MIN_REFRESH_DELAY], (
        "a tiny positive grant must be clamped to the positive refresh floor, "
        f"not its sub-second nominal delay; scheduled {captured}"
    )
    assert _MIN_REFRESH_DELAY > 0.5, "the floor must exceed the tiny nominal delay"
    await manager.aclose()


# ---- recovery: a refresh that gets NO response (item 2, Timer-F/B) ----------


async def test_refresh_with_no_response_times_out_and_reregisters() -> None:
    # RFC 3261 Timer F/B: a refresh REGISTER that gets NO response at all must not
    # leave the binding marked 'registered' forever. After a bounded response
    # timeout the manager marks the extension down, reports it, and re-registers —
    # rather than trusting a binding that may already have lapsed at the registrar.
    errors: list[tuple[str, BaseException]] = []
    transport = _FakeTransport()
    manager = _disable_refresh_floor(  # private seam: immediate hand-driven refresh
        RegistrationManager(
            _gateway(),
            transport,
            refresh_fraction=0.0,  # refresh fires immediately
            refresh_timeout=0.05,  # ... and times out fast (no response is fed)
            retry_backoff=0.0,
            on_registration_error=lambda ext, exc: errors.append((ext, exc)),
        )
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
    manager = _disable_refresh_floor(  # private seam: immediate hand-driven refresh
        RegistrationManager(
            _gateway(),
            transport,
            refresh_fraction=0.0,
            refresh_timeout=0.05,
            retry_backoff=0.0,
            on_registration_error=lambda ext, exc: errors.append((ext, exc)),
        )
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


# ---- structured log events on registration failure (observability) ----------


async def test_rejected_refresh_emits_structured_failure_log_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 4xx/5xx REGISTER rejection emits a structured WARNING with typed extra fields.

    The event ``sip_registration_failed`` must carry:
    - ``outcome`` — the string ``"rejected"``
    - ``status_code`` — the integer HTTP/SIP status (e.g. 503)
    - ``attempt`` — the integer recovery attempt count (>= 1 on first failure)

    SECRET-SAFE: the emitted LogRecord must NOT contain the fake SIP host
    (``pbx.example.test``), extension/username (``1000``/``1001``), or password
    (``p1``/``p2``) in any serialized structured field or in the formatted
    message — rule 34 applies to structured log events the same as to free-text
    log lines.
    """
    transport = _FakeTransport()
    manager = _disable_refresh_floor(
        RegistrationManager(
            _gateway(),
            transport,
            refresh_fraction=0.0,
            retry_backoff=10.0,  # suppress recovery send to keep the test focused
        )
    )
    await manager.start()
    first_register = transport.sent[0]
    call_id = SipRequest.parse(first_register).header("Call-ID")
    with caplog.at_level(logging.WARNING, logger="hermes_voip.manager"):
        await manager.on_response(_ok_for(first_register))  # extension 1000 now up
        await asyncio.sleep(0.05)
    refresh = next(
        m
        for m in transport.sent[1:]
        if SipRequest.parse(m).header("Call-ID") == call_id
    )
    with caplog.at_level(logging.WARNING, logger="hermes_voip.manager"):
        await manager.on_response(
            _failed_for(refresh, status=503, reason="Service Unavailable")
        )
    await manager.aclose()

    records = [
        r
        for r in caplog.records
        if r.name == "hermes_voip.manager"
        and getattr(r, "event", None) == "sip_registration_failed"
    ]
    assert records, (
        "a rejected REGISTER refresh must emit a structured log record with "
        "event='sip_registration_failed'"
    )
    rec = records[0]
    assert rec.levelno == logging.WARNING, (
        "registration failure must log at WARNING level"
    )
    assert getattr(rec, "outcome", None) == "rejected", (
        "structured field 'outcome' must be 'rejected' for a 4xx/5xx response"
    )
    assert getattr(rec, "status_code", None) == 503, (
        "structured field 'status_code' must be the SIP status integer"
    )
    assert isinstance(getattr(rec, "attempt", None), int), (
        "structured field 'attempt' must be an integer"
    )
    assert getattr(rec, "attempt", 0) >= 1, (
        "structured field 'attempt' must be >= 1 on the first failure"
    )
    # rule 34: never log secrets in the structured event or any structured fields
    _assert_failure_log_is_secret_safe(rec)


async def test_refresh_send_failure_emits_transport_failed_log_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refresh transport failure is logged distinctly from timeout/rejection.

    Operators need to distinguish a registrar response from a local transport send
    failure, but the log must remain secret-safe.
    """
    transport = _FlakyTransport(fail_after=2)
    manager = _disable_refresh_floor(
        RegistrationManager(
            _gateway(),
            transport,
            refresh_fraction=0.0,
            retry_backoff=10.0,
        )
    )
    await manager.start()
    with caplog.at_level(logging.WARNING, logger="hermes_voip.manager"):
        await manager.on_response(_ok_for(transport.sent[0]))
        await asyncio.sleep(0.05)
    await manager.aclose()

    records = [
        r
        for r in caplog.records
        if r.name == "hermes_voip.manager"
        and getattr(r, "event", None) == "sip_registration_failed"
    ]
    assert records, (
        "a refresh transport-send failure must emit a structured log record with "
        "event='sip_registration_failed'"
    )
    rec = records[0]
    assert rec.levelno == logging.WARNING, (
        "registration transport failure must log at WARNING level"
    )
    assert getattr(rec, "outcome", None) == "transport_failed", (
        "structured field 'outcome' must distinguish transport-send failures"
    )
    assert getattr(rec, "status_code", object()) is None, (
        "structured field 'status_code' must be None when no response exists"
    )
    assert isinstance(getattr(rec, "attempt", None), int), (
        "structured field 'attempt' must be an integer"
    )
    assert getattr(rec, "attempt", 0) >= 1, (
        "structured field 'attempt' must be >= 1 on the first failure"
    )
    _assert_failure_log_is_secret_safe(rec)


async def test_timeout_refresh_emits_structured_failure_log_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A response-timeout on a refresh emits a structured WARNING with typed fields.

    The event ``sip_registration_failed`` must carry:
    - ``outcome`` — the string ``"timeout"``
    - ``status_code`` — ``None`` (no SIP response was received)
    - ``attempt`` — the integer recovery attempt count (>= 1 on first failure)

    SECRET-SAFE: same rule-34 constraints as the rejection variant, including
    every serialized structured field.
    """
    transport = _FakeTransport()
    manager = _disable_refresh_floor(
        RegistrationManager(
            _gateway(),
            transport,
            refresh_fraction=0.0,
            refresh_timeout=0.05,  # short enough that no-response fires fast
            retry_backoff=10.0,  # suppress recovery send to keep the test focused
        )
    )
    await manager.start()
    first_register = transport.sent[0]
    with caplog.at_level(logging.WARNING, logger="hermes_voip.manager"):
        await manager.on_response(_ok_for(first_register))  # extension 1000 now up
        # Deliberately do NOT feed a response to the refresh — the Timer-F/B fires.
        await asyncio.sleep(0.2)
    await manager.aclose()

    records = [
        r
        for r in caplog.records
        if r.name == "hermes_voip.manager"
        and getattr(r, "event", None) == "sip_registration_failed"
    ]
    assert records, (
        "a refresh response-timeout must emit a structured log record with "
        "event='sip_registration_failed'"
    )
    rec = records[0]
    assert rec.levelno == logging.WARNING, (
        "registration timeout failure must log at WARNING level"
    )
    assert getattr(rec, "outcome", None) == "timeout", (
        "structured field 'outcome' must be 'timeout' for a Timer-F/B expiry"
    )
    assert getattr(rec, "status_code", object()) is None, (
        "structured field 'status_code' must be None when no response was received"
    )
    assert isinstance(getattr(rec, "attempt", None), int), (
        "structured field 'attempt' must be an integer"
    )
    assert getattr(rec, "attempt", 0) >= 1, (
        "structured field 'attempt' must be >= 1 on the first failure"
    )
    _assert_failure_log_is_secret_safe(rec)


# Distinctive registrar-controlled free text. A real registrar (or an attacker who
# influences it) can put ARBITRARY bytes in the SIP response reason-phrase; this
# sentinel stands in for that text. It must NEVER reach an on_registration_error
# consumer via the *default* string form of the error, because operators commonly
# wire that callback straight to a logger/telemetry sink (a path the #348
# structured-log guard does not cover).
_REGISTRAR_REASON_SENTINEL = "ATTACKER-CONTROLLED-REASON-7f3a91"


async def test_callback_error_str_omits_registrar_reason() -> None:
    # SECURITY (codex #351 follow-up): the on_registration_error callback receives
    # the raw exception. RegistrationRejectedError historically baked the
    # registrar-controlled reason-phrase into its Exception message, so a consumer
    # logging str(error)/repr(error) forwarded attacker-influenced free text to its
    # sink. The default string form of the error handed to the callback MUST be
    # safe — it may carry the SIP status code and a sanitized category, but NOT the
    # registrar reason text.
    errors: list[tuple[str, BaseException]] = []
    transport = _FakeTransport()
    manager = _disable_refresh_floor(
        RegistrationManager(
            _gateway(),
            transport,
            refresh_fraction=0.0,
            retry_backoff=0.0,
            on_registration_error=lambda ext, exc: errors.append((ext, exc)),
        )
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
    # The registrar rejects the refresh with attacker-influenced reason text.
    await manager.on_response(
        _failed_for(refresh, status=403, reason=_REGISTRAR_REASON_SENTINEL)
    )
    assert errors, "a rejected refresh must be reported via on_registration_error"
    _ext, exc = errors[0]
    rendered_str = str(exc)
    rendered_repr = repr(exc)
    # THE LEAK: the registrar-controlled text must not appear in the default
    # string/repr a consumer would log.
    assert _REGISTRAR_REASON_SENTINEL not in rendered_str, (
        "registrar-controlled reason leaked via str(error) to the "
        f"on_registration_error consumer: {rendered_str!r}"
    )
    assert _REGISTRAR_REASON_SENTINEL not in rendered_repr, (
        "registrar-controlled reason leaked via repr(error) to the "
        f"on_registration_error consumer: {rendered_repr!r}"
    )
    # str(exc.args) is the other casually-logged form (logging renders args): it
    # must be clean too, so the reason cannot live in the exception args tuple.
    assert _REGISTRAR_REASON_SENTINEL not in str(exc.args), (
        "registrar-controlled reason leaked via exc.args to the "
        f"on_registration_error consumer: {exc.args!r}"
    )
    # The default form is still useful: the SIP status code remains visible, and
    # the error exposes a typed status for status-aware consumers.
    assert "403" in rendered_str, (
        "the sanitized error string should still carry the SIP status code"
    )
    assert getattr(exc, "status", None) == 403, (
        "RegistrationRejectedError must expose the SIP status for consumers"
    )
    await manager.aclose()


async def test_registration_failure_category_classifies_without_string_form() -> None:
    # The sanitized category is the safe discriminator for an on_registration_error
    # consumer: it exposes no registrar text and no SIP host/extension.
    rejected = RegistrationRejectedError(403, _REGISTRAR_REASON_SENTINEL)
    timeout = RegistrationTimeoutError()
    other = RuntimeError("transport went away")
    assert registration_failure_category(rejected) is (
        RegistrationFailureCategory.REJECTED
    )
    assert registration_failure_category(timeout) is (
        RegistrationFailureCategory.TIMEOUT
    )
    # A non-RegistrationError (e.g. an unexpected transport/task failure) is
    # classified as TRANSPORT_FAILED, never crashing the consumer.
    assert registration_failure_category(other) is (
        RegistrationFailureCategory.TRANSPORT_FAILED
    )
    # The error answers its own category too.
    assert rejected.category is RegistrationFailureCategory.REJECTED
    assert timeout.category is RegistrationFailureCategory.TIMEOUT
    # No category value carries registrar text or an identifier.
    for member in RegistrationFailureCategory:
        assert _REGISTRAR_REASON_SENTINEL not in member.value
        assert "1000" not in member.value


async def test_registration_rejected_raw_reason_is_explicit_opt_in() -> None:
    # The registrar reason is reachable ONLY via the explicit, documented-untrusted
    # raw_reason opt-in — never via the default string/repr/args form.
    rejected = RegistrationRejectedError(403, _REGISTRAR_REASON_SENTINEL)
    assert rejected.raw_reason == _REGISTRAR_REASON_SENTINEL, (
        "a consumer that explicitly opts in must still be able to read the reason"
    )
    assert _REGISTRAR_REASON_SENTINEL not in str(rejected)
    assert _REGISTRAR_REASON_SENTINEL not in repr(rejected)
    assert _REGISTRAR_REASON_SENTINEL not in str(rejected.args)


async def test_registration_rejected_reason_attribute_retained_for_compat() -> None:
    # COMPAT (codex #351 follow-up BLOCK): original main exposed a PUBLIC
    # ``RegistrationRejectedError.reason`` attribute. An external operator callback
    # wired to ``on_registration_error`` may read ``error.reason`` directly. The
    # sanitization fix must NOT drop that public accessor (that would break such a
    # caller) — ``.reason`` must keep returning the registrar reason verbatim, the
    # same untrusted opt-in contract as ``raw_reason``. This makes the change
    # genuinely non-breaking: ``error.reason`` keeps working exactly as before.
    rejected = RegistrationRejectedError(403, _REGISTRAR_REASON_SENTINEL)
    # The pre-existing public read accessor still works (the compat guarantee).
    assert rejected.reason == _REGISTRAR_REASON_SENTINEL, (
        "RegistrationRejectedError.reason must remain a public accessor so an "
        "external operator callback reading error.reason does not break"
    )
    # ``.reason`` is the explicit, opt-in untrusted accessor — it is excluded from
    # the sanitized default rendering, so a consumer that merely logs the exception
    # still cannot leak the registrar text via str/repr/args.
    assert _REGISTRAR_REASON_SENTINEL not in str(rejected)
    assert _REGISTRAR_REASON_SENTINEL not in repr(rejected)
    assert _REGISTRAR_REASON_SENTINEL not in str(rejected.args)


# A factory per CONCRETE RegistrationError subclass, each constructed with a
# reason-like string argument (the sentinel) wherever the subclass takes registrar
# text. A subclass that carries no reason (e.g. the timeout) simply ignores it; the
# point is that EVERY concrete subclass is exercised. The completeness assertion
# below pins this map to the actual subclass set, so a future subclass is forced to
# register here AND prove its sanitization — a subclass that forwarded the registrar
# reason into its message/args would then trip the leak assertion.
_REGISTRATION_ERROR_FACTORIES: dict[
    type[RegistrationError], Callable[[str], RegistrationError]
] = {
    RegistrationRejectedError: lambda reason: RegistrationRejectedError(403, reason),
    RegistrationTimeoutError: lambda _reason: RegistrationTimeoutError(),
}


def _concrete_registration_error_subclasses() -> set[type[RegistrationError]]:
    """Every concrete (non-abstract) ``RegistrationError`` subclass, recursively."""
    discovered: set[type[RegistrationError]] = set()
    pending: list[type[RegistrationError]] = list(RegistrationError.__subclasses__())
    while pending:
        cls = pending.pop()
        discovered.add(cls)
        pending.extend(cls.__subclasses__())
    return discovered


async def test_registration_error_subclasses_never_leak_reason_in_default_form() -> (
    None
):
    """INVARIANT: no concrete ``RegistrationError`` subclass renders registrar text.

    Locks the secret-safety contract against a future-subclass regression (a
    reviewer's substantive risk): for every concrete subclass constructible with a
    reason-like string, a passed-in sentinel must NOT appear in ``str(e)``,
    ``repr(e)``, or ``str(e.args)``. Passes today (the current subclasses are
    safe); the moment a new subclass forwards registrar/gateway free text into its
    message or ``args`` it trips this test.
    """
    concrete = _concrete_registration_error_subclasses()
    # Completeness: the factory map must cover exactly the concrete subclass set, so
    # a newly-added subclass cannot silently escape this invariant.
    assert concrete == set(_REGISTRATION_ERROR_FACTORIES), (
        "every concrete RegistrationError subclass must be registered in "
        "_REGISTRATION_ERROR_FACTORIES so the no-leak invariant covers it; "
        f"uncovered={concrete - set(_REGISTRATION_ERROR_FACTORIES)}, "
        f"stale={set(_REGISTRATION_ERROR_FACTORIES) - concrete}"
    )
    for cls, make in _REGISTRATION_ERROR_FACTORIES.items():
        error = make(_REGISTRAR_REASON_SENTINEL)
        for rendered, form in (
            (str(error), "str"),
            (repr(error), "repr"),
            (str(error.args), "args"),
        ):
            assert _REGISTRAR_REASON_SENTINEL not in rendered, (
                f"{cls.__name__} leaked registrar reason via {form}(): {rendered!r}"
            )


# ---- on_response must never propagate: fail-open DoS (ADR-0081) --------------
#
# on_response wraps flow.handle() with NO guard and runs OUTSIDE the reader loop's
# parse-only ``except ValueError`` (transport/connection.py + ws_connection.py). So
# an exception escaping handle() unwinds _read_loop → on_connection_lost and tears
# down the WHOLE shared TLS/WSS signalling connection — every active call AND every
# registration. Two reachable triggers must be contained here so on_response RETURNS
# normally (the connection survives), per ADR-0081's "one malformed message must not
# be a DoS against unrelated calls":
#   (1) an UNANSWERABLE first challenge (digest ValueError) → the flow fails CLOSED to
#       Failed, which on_response reports via _on_registration_failed; and
#   (2) a DUPLICATE/late or SUPERSEDED (stale-CSeq) REGISTER final (handle() raises
#       RuntimeError because the registration Call-ID is stable and routes the stray
#       final back to a flow with no matching outstanding transaction) → IGNORED, so a
#       harmless retransmit neither crashes on_response nor flaps the live registration.


def _unanswerable_challenge_for(register_text: str) -> SipResponse:
    """A 401 for ``register_text`` offering ONLY an unsupported algorithm.

    Routes to the owning flow by Call-ID (like ``_challenge_for``) but is
    UNANSWERABLE: SHA-512-256 is a legitimate RFC 8760 algorithm this flow does not
    implement, so the digest layer raises ``ValueError`` inside ``handle()``.
    """
    reg = SipRequest.parse(register_text)
    return SipResponse.parse(
        "SIP/2.0 401 Unauthorized\r\n"
        f"Call-ID: {reg.header('Call-ID')}\r\n"
        f"CSeq: {reg.header('CSeq')}\r\n"
        'WWW-Authenticate: Digest realm="pbx.example.test", nonce="abc123", '
        'algorithm=SHA-512-256, qop="auth"\r\n'
        "Content-Length: 0\r\n\r\n"
    )


async def test_on_response_unanswerable_challenge_does_not_propagate() -> None:
    errors: list[tuple[str, BaseException]] = []
    transport = _FakeTransport()
    manager = RegistrationManager(
        _gateway(),
        transport,
        on_registration_error=lambda ext, exc: errors.append((ext, exc)),
    )
    await manager.start()
    # MUST NOT raise: an escaping ValueError here is the DoS — it would unwind the
    # reader task and drop every call + registration on the shared connection.
    await manager.on_response(_unanswerable_challenge_for(transport.sent[0]))
    # The failure is reported (never swallowed, rule 37) and nothing came up.
    assert errors, "an unanswerable challenge must be reported, not crash on_response"
    assert manager.is_up is False
    await manager.aclose()


async def test_on_response_duplicate_final_does_not_propagate_or_flap() -> None:
    errors: list[tuple[str, BaseException]] = []
    transport = _FakeTransport()
    manager = RegistrationManager(
        _gateway(),
        transport,
        on_registration_error=lambda ext, exc: errors.append((ext, exc)),
    )
    await manager.start()
    first_register = transport.sent[0]
    # Drive a full, successful registration: 401 → authed REGISTER → 200 OK.
    await manager.on_response(_challenge_for(first_register))
    authed = transport.sent[-1]
    await manager.on_response(_ok_for(authed))
    up = [s for s in manager.snapshot() if s.registered]
    assert len(up) == 1  # the extension is registered
    assert up[0].expires == 300

    # A DUPLICATE 200 OK re-enters handle() after the transaction closed (_txn is
    # None) → handle() raises RuntimeError. on_response MUST ignore it: no crash, no
    # flap, no operator alarm.
    await manager.on_response(_ok_for(authed))
    still_up = [s for s in manager.snapshot() if s.registered]
    assert len(still_up) == 1, "a duplicate 200 OK must not flap the live registration"
    assert not errors, "a harmless duplicate final must not alarm the operator"
    await manager.aclose()


async def test_on_response_stale_cseq_does_not_propagate() -> None:
    errors: list[tuple[str, BaseException]] = []
    transport = _FakeTransport()
    manager = RegistrationManager(
        _gateway(),
        transport,
        on_registration_error=lambda ext, exc: errors.append((ext, exc)),
    )
    await manager.start()
    reg = SipRequest.parse(transport.sent[0])
    # A 200 OK whose CSeq does NOT match the outstanding REGISTER (1): _check_cseq
    # raises RuntimeError. A superseded/stale final routing back by the stable
    # Call-ID must be ignored, never propagate out of on_response.
    stale = SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        f"Call-ID: {reg.header('Call-ID')}\r\n"
        "CSeq: 99 REGISTER\r\n"
        f"Contact: {reg.header('Contact')};expires=300\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    await manager.on_response(stale)  # MUST NOT raise
    assert manager.is_up is False  # the stale final did not spuriously register us
    assert not errors
    await manager.aclose()


async def test_on_response_stale_cseq_preserves_outstanding_response_timeout() -> None:
    # Ignoring an uncorrelated (stale-CSeq) final must NOT disarm the LIVE REGISTER's
    # Timer-F/B deadline. on_response used to cancel the response timeout BEFORE
    # handle(), so a stale/superseded final left the outstanding REGISTER with no
    # timeout-driven recovery — a fail-stuck registration (the same registration-layer
    # DoS, merely shifted from the shared-connection teardown this fix closes). The
    # deadline must be disarmed ONLY for a response the registrar actually answered
    # THIS REGISTER with (a correlated outcome).
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)  # default refresh_timeout > 0
    await manager.start()
    # Answer the initial REGISTER with a 401 → the authed REGISTER (CSeq 2) is sent and
    # its Timer-F/B response timeout is armed while we await the authed 200.
    await manager.on_response(_challenge_for(transport.sent[0]))
    state = manager._by_extension["1000"]
    assert state.response_timeout_task is not None  # armed, awaiting the authed 200

    reg = SipRequest.parse(transport.sent[0])
    stale = SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        f"Call-ID: {reg.header('Call-ID')}\r\n"
        "CSeq: 1 REGISTER\r\n"  # stale: the outstanding REGISTER is CSeq 2 (authed)
        f"Contact: {reg.header('Contact')};expires=300\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    await manager.on_response(stale)  # MUST NOT raise, MUST NOT disarm the live timeout
    assert state.response_timeout_task is not None, (
        "a stale/uncorrelated final must not cancel the outstanding REGISTER's timeout"
    )
    await manager.aclose()


async def test_aclose_deregisters_registered_flow() -> None:
    """Graceful shutdown de-registers a live binding, Expires:0 (#97, ADR-0059).

    Without it the gateway keeps routing inbound calls to the just-closed contact
    until the binding expires (an inbound black-hole after every restart). aclose()
    must send the Expires:0 REGISTER (``RegistrationFlow.deregister``) while the
    transport is still open, before it tears the flow down.
    """
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    await manager.start()
    # Register ONLY the flow whose REGISTER is sent[0]; the other stays unregistered.
    await manager.on_response(_ok_for(transport.sent[0], expires=300))
    sent_before = len(transport.sent)

    await manager.aclose()

    deregs = [
        m
        for m in transport.sent[sent_before:]
        if m.startswith("REGISTER ") and SipRequest.parse(m).header("Expires") == "0"
    ]
    assert len(deregs) == 1, (
        "aclose must send exactly one Expires:0 de-register for the one registered "
        f"flow; new messages after registration: {transport.sent[sent_before:]!r}"
    )


async def test_aclose_does_not_deregister_unregistered_flow() -> None:
    """De-register ONLY live bindings on aclose, never an unregistered flow (#97)."""
    transport = _FakeTransport()
    manager = RegistrationManager(_gateway(), transport)
    await manager.start()  # 2 REGISTERs sent; no 200 fed, so NEITHER flow is registered
    sent_before = len(transport.sent)

    await manager.aclose()

    assert transport.sent[sent_before:] == [], (
        "aclose must send no de-register for a flow that never registered"
    )


async def test_aclose_deregister_send_failure_does_not_strand_shutdown() -> None:
    """A failed de-register send must not strand aclose on shutdown (#97, rule 37)."""

    class _FailingSendTransport(_FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.fail = False

        async def send(self, message: str) -> None:
            if self.fail:
                raise ConnectionResetError("transport gone during shutdown de-register")
            await super().send(message)

    transport = _FailingSendTransport()
    manager = RegistrationManager(_gateway(), transport)
    await manager.start()
    await manager.on_response(_ok_for(transport.sent[0], expires=300))
    await manager.on_response(_ok_for(transport.sent[1], expires=300))
    transport.fail = True  # every subsequent send (the de-registers) now raises

    await manager.aclose()  # MUST NOT raise despite the failed de-registers

    # Shutdown still completed for BOTH flows: their tasks are cancelled + cleared.
    for extension in ("1000", "1001"):
        state = manager._by_extension[extension]
        assert state.refresh_task is None
        assert state.recovery_task is None
        assert state.response_timeout_task is None


async def test_aclose_cancels_refresh_before_sending_deregister() -> None:
    """The Expires:0 de-register is sent only AFTER refresh tasks are cancelled (#97).

    Otherwise a still-live refresh task could send a normal REGISTER after the
    de-register and recreate the binding at the gateway — the inbound black-hole
    would persist (codex #435). So aclose must cancel + drain the per-flow tasks
    before it de-registers.
    """
    snapshots: list[tuple[str, bool]] = []  # (message, any refresh task still live)

    class _OrderTransport(_FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.manager: RegistrationManager | None = None

        async def send(self, message: str) -> None:
            mgr = self.manager
            live = mgr is not None and any(
                s.refresh_task is not None for s in mgr._flows.values()
            )
            snapshots.append((message, live))
            await super().send(message)

    transport = _OrderTransport()
    manager = RegistrationManager(_gateway(), transport)
    transport.manager = manager
    await manager.start()
    # A successful REGISTER arms this flow's refresh task; it must be cancelled
    # before the de-register is sent.
    await manager.on_response(_ok_for(transport.sent[0], expires=300))

    await manager.aclose()

    deregs = [
        (m, live)
        for (m, live) in snapshots
        if m.startswith("REGISTER ") and SipRequest.parse(m).header("Expires") == "0"
    ]
    assert deregs, "aclose must send a de-register"
    assert all(not live for (_, live) in deregs), (
        "the de-register must be sent AFTER refresh tasks are cancelled — no live "
        "refresh may follow it and recreate the binding"
    )


async def test_aclose_bounds_a_hanging_deregister_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A HANGING (not just failing) de-register send must not strand aclose.

    (#435, rule 37) aclose() wraps the shutdown Expires:0 de-register send in
    ``asyncio.wait_for(self._transport.send(...), _DEREGISTER_SEND_TIMEOUT_SECS)``
    because a dead/slow transport must never strand shutdown. The only existing
    negative-path test
    (``test_aclose_deregister_send_failure_does_not_strand_shutdown``) uses a
    transport whose ``send()`` RAISES immediately — an ``await`` of that
    raising coroutine returns (with an exception) exactly as fast as an
    ``await`` of the whole ``wait_for`` wrapper, so it cannot tell the two
    apart. A mutant that deletes the ``wait_for`` wrapper and awaits
    ``send()`` directly would still pass that test yet block ``aclose()``
    indefinitely in production against a transport whose ``send()`` never
    returns. This test uses a transport whose ``send()`` awaits an
    ``asyncio.Event`` that is never set — the only way it can complete is via
    the ``wait_for`` bound's cancellation of the pending send.
    """
    # Deterministic and fast: shrink the production bound rather than sleeping
    # for it, so the test proves the SAME wait_for behaviour without being slow.
    monkeypatch.setattr(manager_module, "_DEREGISTER_SEND_TIMEOUT_SECS", 0.05)

    class _HangingSendTransport(_FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.hang = False
            self._never = asyncio.Event()  # deliberately never set

        async def send(self, message: str) -> None:
            if self.hang:
                await self._never.wait()
                return
            await super().send(message)

    transport = _HangingSendTransport()
    manager = RegistrationManager(_gateway(), transport)
    await manager.start()
    await manager.on_response(_ok_for(transport.sent[0], expires=300))
    await manager.on_response(_ok_for(transport.sent[1], expires=300))
    transport.hang = True  # every subsequent send (the de-registers) now hangs forever

    # An outer bound generous relative to the (shrunk) production timeout: if the
    # wait_for wrapper were removed, aclose() would hang forever and THIS assertion
    # is what turns that hang into a deterministic test failure instead of a wedged
    # test run.
    await asyncio.wait_for(manager.aclose(), timeout=2.0)

    # Shutdown still completed for BOTH flows despite the hung sends: their tasks
    # are cancelled + cleared, exactly as the failing-send negative path asserts.
    for extension in ("1000", "1001"):
        state = manager._by_extension[extension]
        assert state.refresh_task is None
        assert state.recovery_task is None
        assert state.response_timeout_task is None


async def test_addr_spec_and_tag_survive_bracketed_quoted_display_name() -> None:
    # RFC 3261 §25.1 permits BOTH ``<`` and ``>`` inside a quoted display-name.
    # A To value whose quoted display-name contains a ``<...>`` span AND a
    # ``;tag=`` must not have that span mistaken for the real <addr-spec>: the
    # naive ``<([^>]*)>`` search extracts ``<Y>`` (garbage) and the split-on-'>'
    # tag scan latches the quoted ``;tag=fake`` instead of the real header tag.
    value = '"X <Y>;tag=fake" <sip:2000@pbx.example.test>;tag=realtag99'
    assert manager_module._addr_spec(value) == "sip:2000@pbx.example.test"
    assert manager_module._tag(value) == "realtag99"


async def test_addr_spec_and_tag_plain_and_bare_unchanged() -> None:
    # A plain name-addr (no brackets in the display-name) and a bare addr-spec
    # (no ``<...>`` at all) keep extracting the addr-spec and tag exactly as before.
    plain = '"Agent" <sip:2000@pbx.example.test>;tag=ttag'
    assert manager_module._addr_spec(plain) == "sip:2000@pbx.example.test"
    assert manager_module._tag(plain) == "ttag"

    bare = "sip:2000@pbx.example.test;tag=baretag"
    assert manager_module._addr_spec(bare) == "sip:2000@pbx.example.test"
    assert manager_module._tag(bare) == "baretag"
