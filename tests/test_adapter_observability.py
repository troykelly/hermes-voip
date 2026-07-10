"""Structured observability log-event tests for ``VoipAdapter``.

The adapter emits per-call lifecycle and RTCP call-quality records as plain
human-readable messages. For a log pipeline (journald/Loki/CloudWatch) to filter
and aggregate them, each record must ALSO carry machine-parseable ``extra={}``
fields — a stable ``event`` discriminator plus the call's ``call_id`` and the
event-specific outcome/quality fields. These tests assert those structured fields
are present on the emitted ``LogRecord`` (via ``caplog.records``), independent of
the unchanged human message text.

Run against the REAL hermes-agent ``BasePlatformAdapter`` (the adapter subclasses
it at runtime), so the module requires the optional ``hermes`` extra and skips
cleanly without it; the dedicated ``hermes-contract`` CI job installs the extra so
these run there (rule 26 — validate against the real deployment target).

All SIP/RTP/provider collaborators are fakes (no real network, no real ML);
credentials and hostnames are obvious fakes (``pbx.example.test`` / ext ``1000``
/ ``127.0.0.1``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The adapter imports the real Hermes base at module top; skip the whole module
# when the optional runtime is absent (it runs in the hermes-contract CI job).
pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_voip.call_end import CallEndReason
from hermes_voip.config import ExtensionConfig, load_media_config
from hermes_voip.manager import NewCall
from hermes_voip.media.engine import CallQuality
from hermes_voip.message import SipRequest, new_call_id, new_tag

if TYPE_CHECKING:
    from hermes_voip.adapter import VoipAdapter

pytestmark = pytest.mark.asyncio

_ADAPTER_LOGGER = "hermes_voip.adapter"


@pytest.fixture(autouse=True)
def _register_voip_platform() -> None:
    """Register a throwaway "voip" entry so ``Platform("voip")`` resolves."""
    if not platform_registry.is_registered("voip"):
        platform_registry.register(
            PlatformEntry(
                name="voip",
                label="VoIP",
                adapter_factory=lambda cfg: MagicMock(),
                check_fn=lambda: True,
                validate_config=lambda cfg: True,
                required_env=[],
                install_hint="",
                source="plugin",
            )
        )


# ---------------------------------------------------------------------------
# Fakes (mirrors tests/test_adapter_production_safety.py, kept local)
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Fake SipTransport + CallSignaling that captures sent messages."""

    def __init__(self, *, local_sent_by: str = "127.0.0.1:5061") -> None:
        self._local_sent_by = local_sent_by
        self.sent: list[str] = []
        self._calls: dict[str, object] = {}

    @property
    def local_sent_by(self) -> str:
        return self._local_sent_by

    def contact_uri(self, extension: str) -> str:
        return f"<sip:{extension}@{self._local_sent_by};transport=tls>"

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def connect(self) -> None:
        """No-op TLS connect for tests."""

    async def aclose(self) -> None:
        """No-op close for tests."""

    def bind_manager(self, manager: object) -> None:
        """No-op manager bind for tests."""

    def add_call(self, call_id: str, sink: object) -> None:
        self._calls[call_id] = sink

    def remove_call(self, call_id: str, sink: object | None = None) -> None:
        if sink is not None and self._calls.get(call_id) is not sink:
            return
        self._calls.pop(call_id, None)


class _FakeManager:
    """Minimal stand-in for RegistrationManager."""

    def __init__(self, *, is_up: bool = True) -> None:
        self._is_up = is_up
        self._calls: dict[tuple[str, str, str], object] = {}
        self.connected = False
        self.closed = False

    @property
    def is_up(self) -> bool:
        return self._is_up

    async def connect(self, *, timeout: float = 10.0) -> bool:
        self.connected = True
        return self._is_up

    async def aclose(self) -> None:
        self.closed = True

    def add_call(self, dialog_id: tuple[str, str, str], consumer: object) -> None:
        self._calls[dialog_id] = consumer

    def remove_call(self, dialog_id: tuple[str, str, str]) -> None:
        self._calls.pop(dialog_id, None)


class _FakeSession:
    """A stand-in CallSession exposing only what teardown touches."""

    def __init__(self, *, ended: bool = False, call_id: str = "c") -> None:
        self.ended = ended
        self.hang_up_calls = 0
        self.dialog_id = (call_id, "lt", "rt")
        self.guard = MagicMock()

    async def hang_up(self) -> None:
        self.hang_up_calls += 1
        self.ended = True


_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
}
_FAKE_MEDIA_ENV: dict[str, str] = {
    "HERMES_VOIP_STT_PROVIDER": "sherpa-onnx",
    "HERMES_VOIP_STT_MODEL_DIR": "/fake/stt",
    "HERMES_VOIP_TTS_PROVIDER": "sherpa-kokoro",
    "HERMES_VOIP_TTS_MODEL": "/fake/tts",
    "HERMES_VOIP_INJECTION_GUARD": "onnx",
    "HERMES_VOIP_INJECTION_GUARD_MODEL_DIR": "/fake/guard",
}

# A minimal G.711 SDP offer (no a=crypto — plain RTP/AVP).
_FAKE_SDP_OFFER = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 20000 RTP/AVP 0 8\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=sendrecv\r\n"
)


def _platform_config(env: dict[str, str] | None = None) -> PlatformConfig:
    return PlatformConfig(enabled=True, extra=dict(env or {}))


def _make_invite(call_id: str | None = None, from_tag: str | None = None) -> str:
    cid = call_id or new_call_id()
    ftag = from_tag or new_tag()
    content_length = len(_FAKE_SDP_OFFER.encode("utf-8"))
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKfake\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:9999@pbx.example.test>;tag={ftag}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {cid}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:9999@127.0.0.1:60000;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{_FAKE_SDP_OFFER}"
    )


def _ext_config() -> ExtensionConfig:
    return ExtensionConfig(index=0, extension="1000", username="1000", password="fake")


async def _build_adapter(
    transport: _FakeTransport,
    manager: _FakeManager,
    *,
    env: dict[str, str] | None = None,
    connect: bool = True,
) -> VoipAdapter:
    """Construct a real ``VoipAdapter`` wired to fakes, optionally calling connect()."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    full_env = _FAKE_ENV | _FAKE_MEDIA_ENV | (env or {})
    config = _platform_config(full_env)

    with (
        patch(
            "hermes_voip.adapter.load_media_config",
            return_value=load_media_config({}),
        ),
        patch("hermes_voip.adapter.build_providers", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        if connect:
            await adapter.connect()
        return adapter


def _record_with_event(
    caplog: pytest.LogCaptureFixture, event: str
) -> logging.LogRecord:
    """Return the single LogRecord whose ``extra`` ``event`` field == ``event``."""
    matches = [rec for rec in caplog.records if getattr(rec, "event", None) == event]
    assert matches, (
        f"expected exactly one structured log record with event={event!r}; "
        f"events seen: {[getattr(r, 'event', None) for r in caplog.records]!r}"
    )
    assert len(matches) == 1, f"event={event!r} emitted {len(matches)} times"
    return matches[0]


# ===========================================================================
# (1) RTCP call-quality structured record (emitted when _rtcp_active is True)
# ===========================================================================


def _rtcp_active_engine() -> MagicMock:
    """A fake media engine that DID activate RTCP, with a populated CallQuality."""
    quality = MagicMock(
        local_fraction_lost=0.01,
        local_cumulative_lost=3,
        local_jitter_ms=4.5,
        remote_fraction_lost=0.02,
        remote_cumulative_lost=7,
        remote_jitter_ms=6.5,
        rtt_seconds=0.12,
    )
    return MagicMock(
        stop=AsyncMock(return_value=None),
        media_timed_out=False,
        _rtcp_active=True,
        call_quality=quality,
    )


async def test_teardown_emits_structured_rtcp_call_quality_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When RTCP was active, teardown emits a machine-parseable quality record.

    Every other adapter test sets ``_rtcp_active=False`` to skip this branch, so
    this is the first test that drives the RTCP-active path and asserts the
    ``extra={}`` quality fields land on the emitted LogRecord.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    call_id = new_call_id()
    session = _FakeSession(ended=True, call_id=call_id)
    adapter._call_sessions[call_id] = session  # type: ignore[assignment]  # _FakeSession is a CallSession test double

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        await adapter._teardown_call(
            call_id=call_id,
            engine=_rtcp_active_engine(),
            transport=transport,  # type: ignore[arg-type]  # fake transport double
            dialog_id=session.dialog_id,
            session=session,  # type: ignore[arg-type]  # _FakeSession double
            reason=CallEndReason.REMOTE_BYE,
        )

    rec = _record_with_event(caplog, "rtcp_call_quality")
    assert rec.call_id == call_id  # type: ignore[attr-defined]
    assert rec.local_fraction_lost == 0.01  # type: ignore[attr-defined]
    assert rec.local_cumulative_lost == 3  # type: ignore[attr-defined]
    assert rec.local_jitter_ms == 4.5  # type: ignore[attr-defined]
    assert rec.remote_fraction_lost == 0.02  # type: ignore[attr-defined]
    assert rec.remote_cumulative_lost == 7  # type: ignore[attr-defined]
    assert rec.remote_jitter_ms == 6.5  # type: ignore[attr-defined]
    assert rec.rtt_seconds == 0.12  # type: ignore[attr-defined]
    # Redaction (rule 34 / ADR-0084): the event's CUSTOM structured extras — vars(rec)
    # minus stdlib LogRecord metadata and the rendered `message` — must be EXACTLY the
    # fixed set (call_id + the numeric quality metrics, including the cumulative-loss
    # counters). Computing the custom set (not filtering to allowed keys) means a
    # leaked field (remote_host/caller/...) fails.
    standard = set(vars(logging.makeLogRecord({}))) | {"message"}
    custom = {k for k in vars(rec) if k not in standard}
    assert custom == {
        "event",
        "call_id",
        "local_fraction_lost",
        "local_cumulative_lost",
        "local_jitter_ms",
        "remote_fraction_lost",
        "remote_cumulative_lost",
        "remote_jitter_ms",
        "rtt_seconds",
    }, f"unexpected structured field(s) on the event: {custom}"
    # A healthy call emits the quality record but NO anomaly event (bk1295/ADR-0102).
    assert not [
        r
        for r in caplog.records
        if getattr(r, "event", None) in ("one_way_audio", "media_degraded")
    ], "a healthy call must emit no media-anomaly event"


def _rtcp_one_way_engine() -> MagicMock:
    """A fake engine whose CallQuality shows inbound-dead one-way audio (ADR-0102).

    ``local_*`` is None (we received NO inbound RTP) while the peer DID report on our
    stream (``remote_*`` present) — the disambiguated one-way-inbound-dead signal.
    """
    quality = CallQuality(
        local_fraction_lost=None,
        local_cumulative_lost=None,
        local_jitter_ms=None,
        remote_fraction_lost=0.0,
        remote_cumulative_lost=0,
        remote_jitter_ms=5.0,
        rtt_seconds=0.1,
    )
    return MagicMock(
        stop=AsyncMock(return_value=None),
        media_timed_out=False,
        _rtcp_active=True,
        call_quality=quality,
    )


async def test_teardown_emits_one_way_audio_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A one-way (inbound-dead) call emits a structured one_way_audio SLO event.

    ADR-0102: local=None (no inbound RTP) while the peer reported on our stream =>
    audio flowed only outbound. The event carries call_id + the fixed reason + numeric
    metrics only — never caller identity/address (rule 34 / ADR-0084; verified by the
    field set below).
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    call_id = new_call_id()
    session = _FakeSession(ended=True, call_id=call_id)
    adapter._call_sessions[call_id] = session  # type: ignore[assignment]  # _FakeSession double

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        await adapter._teardown_call(
            call_id=call_id,
            engine=_rtcp_one_way_engine(),
            transport=transport,  # type: ignore[arg-type]  # fake transport double
            dialog_id=session.dialog_id,
            session=session,  # type: ignore[arg-type]  # _FakeSession double
            reason=CallEndReason.REMOTE_BYE,
        )

    rec = _record_with_event(caplog, "one_way_audio")
    assert rec.call_id == call_id  # type: ignore[attr-defined]
    assert rec.reason == "no_inbound_rtp"  # type: ignore[attr-defined]
    assert rec.local_cumulative_lost is None  # type: ignore[attr-defined]
    assert rec.remote_cumulative_lost == 0  # type: ignore[attr-defined]
    # Redaction (rule 34 / ADR-0084): the event's CUSTOM structured extras — vars(rec)
    # minus stdlib LogRecord metadata and the rendered `message` — must be EXACTLY the
    # fixed set (call_id + reason + numeric metrics, including the cumulative-loss
    # counters). Computing the custom set (not filtering to allowed keys) means a
    # leaked field (remote_host/caller/...) fails.
    standard = set(vars(logging.makeLogRecord({}))) | {"message"}
    custom = {k for k in vars(rec) if k not in standard}
    assert custom == {
        "event",
        "call_id",
        "reason",
        "local_fraction_lost",
        "local_cumulative_lost",
        "remote_fraction_lost",
        "remote_cumulative_lost",
        "local_jitter_ms",
        "remote_jitter_ms",
    }, f"unexpected structured field(s) on the event: {custom}"


async def test_teardown_without_rtcp_emits_no_quality_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-RTCP call (the common case) emits NO rtcp_call_quality record."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    call_id = new_call_id()
    session = _FakeSession(ended=True, call_id=call_id)
    adapter._call_sessions[call_id] = session  # type: ignore[assignment]  # _FakeSession is a CallSession test double
    engine = MagicMock(
        stop=AsyncMock(return_value=None),
        media_timed_out=False,
        _rtcp_active=False,
    )

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        await adapter._teardown_call(
            call_id=call_id,
            engine=engine,
            transport=transport,  # type: ignore[arg-type]  # fake transport double
            dialog_id=session.dialog_id,
            session=session,  # type: ignore[arg-type]  # _FakeSession double
            reason=CallEndReason.REMOTE_BYE,
        )

    assert not [
        rec
        for rec in caplog.records
        if getattr(rec, "event", None) == "rtcp_call_quality"
    ], "a non-RTCP call must emit no rtcp_call_quality record"


def _rtcp_dormant_engine(*, dormant_reason: str | None) -> MagicMock:
    """A fake media engine that left RTCP dormant, carrying a dormant reason."""
    return MagicMock(
        stop=AsyncMock(return_value=None),
        media_timed_out=False,
        _rtcp_active=False,
        _rtcp_dormant_reason=dormant_reason,
    )


async def test_teardown_emits_structured_rtcp_dormant_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When RTCP stayed dormant, teardown emits a machine-parseable dormant record.

    Without this event, a dormant call (secured-without-SRTCP-opt-in, the RTCP
    kill-switch off, an unsupported answer profile, a muxed payload-type conflict,
    ...) left teardown SILENT — operators could not distinguish "no RTCP data
    because RTCP never activated" from "RTCP activated then failed to report".
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    call_id = new_call_id()
    session = _FakeSession(ended=True, call_id=call_id)
    adapter._call_sessions[call_id] = session  # type: ignore[assignment]  # _FakeSession is a CallSession test double

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        await adapter._teardown_call(
            call_id=call_id,
            engine=_rtcp_dormant_engine(dormant_reason="secured_rtcp_not_enabled"),
            transport=transport,  # type: ignore[arg-type]  # fake transport double
            dialog_id=session.dialog_id,
            session=session,  # type: ignore[arg-type]  # _FakeSession double
            reason=CallEndReason.REMOTE_BYE,
        )

    rec = _record_with_event(caplog, "rtcp_dormant")
    assert rec.call_id == call_id  # type: ignore[attr-defined]
    assert rec.dormant_reason == "secured_rtcp_not_enabled"  # type: ignore[attr-defined]


async def test_teardown_dormant_without_reason_logs_not_negotiated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dormant call with no reason set still logs a fixed code, never ``None``.

    A call torn down BEFORE any RTCP activation decision was reached
    (``_rtcp_dormant_reason`` still ``None`` — e.g. an early setup failure before SDP
    negotiation) must STILL emit a machine-parseable ``rtcp_dormant`` record with a
    fixed reason code, never ``dormant_reason=None``. The teardown log coalesces
    ``None`` to ``"not_negotiated"`` so a log pipeline can always key on the reason.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    call_id = new_call_id()
    session = _FakeSession(ended=True, call_id=call_id)
    adapter._call_sessions[call_id] = session  # type: ignore[assignment]  # _FakeSession is a CallSession test double

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        await adapter._teardown_call(
            call_id=call_id,
            engine=_rtcp_dormant_engine(dormant_reason=None),
            transport=transport,  # type: ignore[arg-type]  # fake transport double
            dialog_id=session.dialog_id,
            session=session,  # type: ignore[arg-type]  # _FakeSession double
            reason=CallEndReason.REMOTE_BYE,
        )

    rec = _record_with_event(caplog, "rtcp_dormant")
    assert rec.dormant_reason == "not_negotiated"  # type: ignore[attr-defined]


async def test_teardown_active_rtcp_emits_no_dormant_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An RTCP-active call must not ALSO emit the rtcp_dormant record."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    call_id = new_call_id()
    session = _FakeSession(ended=True, call_id=call_id)
    adapter._call_sessions[call_id] = session  # type: ignore[assignment]  # _FakeSession is a CallSession test double

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        await adapter._teardown_call(
            call_id=call_id,
            engine=_rtcp_active_engine(),
            transport=transport,  # type: ignore[arg-type]  # fake transport double
            dialog_id=session.dialog_id,
            session=session,  # type: ignore[arg-type]  # _FakeSession double
            reason=CallEndReason.REMOTE_BYE,
        )

    assert not [
        rec for rec in caplog.records if getattr(rec, "event", None) == "rtcp_dormant"
    ], "an RTCP-active call must emit no rtcp_dormant record"


# ===========================================================================
# (3) Admission concurrency gauge + per-call duration on release
# ===========================================================================


async def test_release_admission_emits_duration_and_concurrency_gauge(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Releasing an admitted slot logs a structured duration_s + concurrency gauge."""
    transport = _FakeTransport()
    adapter = await _build_adapter(
        transport, _FakeManager(), env={"HERMES_SIP_MAX_CALLS": "4"}
    )
    call_id = new_call_id()
    # Reserve via the real admission seam so the admission-start clock is recorded.
    assert adapter._admit_inbound(call_id, 4) is True
    # A second concurrent call holds a slot too, so the gauge at release is non-zero.
    other = new_call_id()
    assert adapter._admit_inbound(other, 4) is True

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        adapter._release_admission(call_id)

    rec = _record_with_event(caplog, "call_released")
    assert rec.call_id == call_id  # type: ignore[attr-defined]
    assert isinstance(rec.duration_s, float)  # type: ignore[attr-defined]
    assert rec.duration_s >= 0.0  # type: ignore[attr-defined]
    # One slot (``other``) remains admitted after this release → gauge == 1.
    assert rec.active_calls == 1  # type: ignore[attr-defined]


async def test_release_admission_idempotent_no_event_for_unknown_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Releasing a call that holds no slot is a silent no-op (no spurious record)."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        adapter._release_admission("never-admitted")

    assert not [
        rec for rec in caplog.records if getattr(rec, "event", None) == "call_released"
    ], "releasing an unknown call must emit no call_released record"


# ===========================================================================
# (2) Per-call lifecycle events: invite_received, call_rejected,
#     call_answered, call_loop_started
# ===========================================================================


async def test_inbound_invite_at_capacity_emits_call_rejected_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An INVITE rejected 486 at capacity emits a structured call_rejected record."""
    transport = _FakeTransport()
    adapter = await _build_adapter(
        transport, _FakeManager(), env={"HERMES_SIP_MAX_CALLS": "1"}
    )
    adapter._admitted_calls.add("existing-call")

    call_id = new_call_id()
    invite_req = SipRequest.parse(_make_invite(call_id=call_id))
    new_call = NewCall(registration=_ext_config(), invite=invite_req)

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        await adapter._handle_inbound_invite(new_call)

    # invite_received must precede the rejection on the same call.
    received = _record_with_event(caplog, "invite_received")
    assert received.call_id == call_id  # type: ignore[attr-defined]

    rejected = _record_with_event(caplog, "call_rejected")
    assert rejected.call_id == call_id  # type: ignore[attr-defined]
    assert rejected.outcome == "rejected"  # type: ignore[attr-defined]
    assert rejected.sip_code == 486  # type: ignore[attr-defined]


async def test_inbound_invite_admitted_emits_answered_and_loop_started_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fully-admitted INVITE emits answered + loop-started structured records.

    Mocks the media collaborators (RTP engine / CallSession / CallLoop / VAD /
    endpointer) like the admission test, so this exercises the lifecycle log
    seams, not the real ML pipeline. The secure-media mandate is off so the
    cleartext offer is admitted and answered.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(
        transport, _FakeManager(), env={"HERMES_SIP_MAX_CALLS": "4"}
    )
    adapter._media_cfg = load_media_config(
        {"HERMES_VOIP_REQUIRE_SECURE_MEDIA": "false"}
    )

    call_id = new_call_id()
    invite_req = SipRequest.parse(_make_invite(call_id=call_id))
    new_call = NewCall(registration=_ext_config(), invite=invite_req)

    with (
        caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER),
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                start_rtcp=AsyncMock(return_value=None),
                _rtcp_active=False,
                local_port=20002,
                inbound_sample_rate=8_000,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallSession",
            return_value=MagicMock(
                dialog_id=(call_id, "local-tag", "remote-tag"),
                ended=False,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        await adapter._handle_inbound_invite(new_call)

    received = _record_with_event(caplog, "invite_received")
    assert received.call_id == call_id  # type: ignore[attr-defined]

    answered = _record_with_event(caplog, "call_answered")
    assert answered.call_id == call_id  # type: ignore[attr-defined]
    assert answered.sip_code == 200  # type: ignore[attr-defined]
    assert answered.outcome == "answered"  # type: ignore[attr-defined]

    loop_started = _record_with_event(caplog, "call_loop_started")
    assert loop_started.call_id == call_id  # type: ignore[attr-defined]
