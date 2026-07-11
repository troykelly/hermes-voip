"""Production-safety lifecycle tests for ``VoipAdapter`` (ADR-0059).

Four launch-readiness gaps, each driven here against the REAL hermes-agent
``BasePlatformAdapter`` (the adapter subclasses it at runtime). These tests
therefore require the optional ``hermes`` extra and skip cleanly without it; the
dedicated ``hermes-contract`` CI job installs the extra so they run there
(rule 26 — validate against the real deployment target).

1. **BYE on mid-call provider failure** — when the conversational pipeline fails
   mid-call, ``_teardown_call`` closes the SIP dialog with a BYE (no zombie
   dialog / dead air), distinct from a caller-hangup and a normal end.
2. **Graceful-shutdown drain** — ``disconnect()`` BYEs every live call (bounded
   by a configurable timeout) before tearing the transport down, rather than
   hard-dropping live callers.
3. **Admission-control cap** — at ``max_calls`` concurrent calls a new inbound
   INVITE is rejected ``486 Busy Here`` before any media is opened, and the live
   count never leaks across accept + teardown.
4. **Secret redaction in logs** — the unroutable-request log never emits the
   ``Authorization`` digest response or ``a=crypto`` SRTP key material.

All SIP/RTP/provider collaborators are fakes (no real network, no real ML);
credentials and hostnames are obvious fakes (``pbx.example.test`` / ext ``1000``
/ ``127.0.0.1``).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING, NamedTuple
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
from hermes_voip.manager import NewCall, Unroutable
from hermes_voip.message import SipRequest, SipResponse, new_call_id, new_tag

if TYPE_CHECKING:
    from collections.abc import Callable

    from hermes_voip.adapter import VoipAdapter

pytestmark = pytest.mark.asyncio


async def _until(
    predicate: Callable[[], bool], *, timeout: float = 3.0, step: float = 0.001
) -> None:
    """Poll ``predicate`` until true or the timeout elapses (no fixed sleeps)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            msg = "condition not met within the timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(step)


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
# Fakes (mirrors tests/test_adapter.py, kept local so this module is standalone)
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


class _FakeSession:
    """A stand-in CallSession exposing only what teardown/drain touch."""

    def __init__(self, *, ended: bool = False, call_id: str = "c") -> None:
        self.ended = ended
        self.hang_up_calls = 0
        self.dialog_id = (call_id, "lt", "rt")
        self.guard = MagicMock()

    async def hang_up(self) -> None:
        self.hang_up_calls += 1
        # Mirror the real CallSession.hang_up: idempotent + flips ended.
        self.ended = True


def _fake_engine() -> MagicMock:
    return MagicMock(
        stop=AsyncMock(return_value=None),
        media_timed_out=False,
        # RTCP (ADR-0061): inert so the teardown call-quality log is skipped for a
        # fake engine that never activated RTCP.
        _rtcp_active=False,
    )


# ===========================================================================
# Item 1 — BYE on mid-call provider failure
# ===========================================================================


async def test_teardown_failure_sends_bye_to_close_dialog() -> None:
    """A FAILURE end with a live dialog must send a SIP BYE (no zombie dialog)."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    call_id = new_call_id()
    session = _FakeSession(ended=False, call_id=call_id)
    adapter._call_sessions[call_id] = session  # type: ignore[assignment]  # _FakeSession is a CallSession test double

    await adapter._teardown_call(
        call_id=call_id,
        engine=_fake_engine(),
        transport=transport,  # type: ignore[arg-type]  # fake transport double
        dialog_id=session.dialog_id,
        session=session,  # type: ignore[arg-type]  # _FakeSession double
        reason=CallEndReason.PIPELINE_FAILURE,
    )

    assert session.hang_up_calls == 1, (
        "a mid-call pipeline failure must tear the SIP dialog down with a BYE"
    )


async def test_teardown_normal_end_does_not_send_bye() -> None:
    """A NORMAL end (caller already BYE'd) must NOT send a second BYE."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    call_id = new_call_id()
    # Caller hung up: the peer BYE already set ended=True in the real _on_bye.
    session = _FakeSession(ended=True, call_id=call_id)
    adapter._call_sessions[call_id] = session  # type: ignore[assignment]  # _FakeSession is a CallSession test double

    await adapter._teardown_call(
        call_id=call_id,
        engine=_fake_engine(),
        transport=transport,  # type: ignore[arg-type]  # fake transport double
        dialog_id=session.dialog_id,
        session=session,  # type: ignore[arg-type]  # _FakeSession double
        reason=CallEndReason.REMOTE_BYE,
    )

    assert session.hang_up_calls == 0, (
        "a remote-BYE normal end must not send another BYE on the dead dialog"
    )


async def test_teardown_failure_on_already_ended_session_sends_no_bye() -> None:
    """A failure end whose dialog the peer already closed sends no BYE."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    call_id = new_call_id()
    session = _FakeSession(ended=True, call_id=call_id)
    adapter._call_sessions[call_id] = session  # type: ignore[assignment]  # _FakeSession is a CallSession test double

    await adapter._teardown_call(
        call_id=call_id,
        engine=_fake_engine(),
        transport=transport,  # type: ignore[arg-type]  # fake transport double
        dialog_id=session.dialog_id,
        session=session,  # type: ignore[arg-type]  # _FakeSession double
        reason=CallEndReason.PIPELINE_FAILURE,
    )

    assert session.hang_up_calls == 0, (
        "no BYE when the dialog is already closed (session.ended), even on failure"
    )


async def test_teardown_failure_bye_error_does_not_strand_engine_stop() -> None:
    """If the BYE raises, teardown still stops the engine (best-effort, rule 37)."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    call_id = new_call_id()

    class _BoomSession(_FakeSession):
        async def hang_up(self) -> None:
            self.hang_up_calls += 1
            raise RuntimeError("BYE send failed")

    session = _BoomSession(ended=False, call_id=call_id)
    adapter._call_sessions[call_id] = session  # type: ignore[assignment]  # _FakeSession is a CallSession test double
    engine = _fake_engine()

    await adapter._teardown_call(
        call_id=call_id,
        engine=engine,
        transport=transport,  # type: ignore[arg-type]  # fake transport double
        dialog_id=session.dialog_id,
        session=session,  # type: ignore[arg-type]  # _FakeSession double
        reason=CallEndReason.PIPELINE_FAILURE,
    )

    assert session.hang_up_calls == 1
    engine.stop.assert_awaited_once()


# ===========================================================================
# Item 2 — Graceful-shutdown drain
# ===========================================================================


async def test_disconnect_byes_live_calls_before_teardown() -> None:
    """disconnect() must BYE every live call before closing the transport."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    s1 = _FakeSession(ended=False, call_id="call-1")
    s2 = _FakeSession(ended=False, call_id="call-2")
    adapter._call_sessions["call-1"] = s1  # type: ignore[assignment]  # _FakeSession is a CallSession test double
    adapter._call_sessions["call-2"] = s2  # type: ignore[assignment]  # _FakeSession is a CallSession test double

    await adapter.disconnect()

    assert s1.hang_up_calls == 1, "shutdown must BYE live call 1, not hard-drop it"
    assert s2.hang_up_calls == 1, "shutdown must BYE live call 2, not hard-drop it"


async def test_disconnect_drain_is_bounded_by_timeout() -> None:
    """A call whose BYE far outlasts the drain timeout must not block shutdown.

    The BYE here sleeps far longer (30 s) than the 1 s drain timeout. The drain
    uses ``asyncio.wait(..., timeout=...)`` and then ``cancel()``s the still-pending
    BYE WITHOUT awaiting it, so ``disconnect()`` returns at ~the drain timeout, not
    at the BYE's duration — a slow/stuck BYE can never stall shutdown. The BYE is
    cancellable, so the cancelled task terminates cleanly (no orphan).
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(
        transport,
        _FakeManager(),
        env={"HERMES_SIP_SHUTDOWN_DRAIN_SECS": "1"},
    )

    class _SlowSession(_FakeSession):
        async def hang_up(self) -> None:
            self.hang_up_calls += 1
            # Far longer than the 1 s drain timeout; cancellable (terminates on the
            # cancel() the drain issues after the timeout — no orphaned task).
            await asyncio.sleep(30)

    slow = _SlowSession(ended=False, call_id="slow")
    adapter._call_sessions["slow"] = slow  # type: ignore[assignment]  # test double

    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.wait_for(adapter.disconnect(), timeout=10.0)
    elapsed = loop.time() - start

    assert slow.hang_up_calls == 1, "the slow call must have been sent a BYE"
    assert elapsed < 5.0, (
        f"shutdown must be bounded by the ~1s drain timeout (not the 30s BYE), "
        f"took {elapsed:.1f}s"
    )
    assert adapter._transport is None, "the transport must still be closed after drain"


async def test_disconnect_with_no_live_calls_is_clean() -> None:
    """Draining with no live calls is a no-op and still closes the transport."""
    transport = _FakeTransport()
    manager = _FakeManager()
    adapter = await _build_adapter(transport, manager)

    await adapter.disconnect()

    assert manager.closed is True
    assert adapter._transport is None


# ===========================================================================
# Item 3 — Admission-control cap
# ===========================================================================


async def test_inbound_invite_at_capacity_is_rejected_486() -> None:
    """At max_calls concurrent calls, a new INVITE is rejected 486 Busy Here."""
    transport = _FakeTransport()
    adapter = await _build_adapter(
        transport, _FakeManager(), env={"HERMES_SIP_MAX_CALLS": "1"}
    )
    # Saturate the single slot with an already-admitted call.
    adapter._admitted_calls.add("existing-call")

    call_id = new_call_id()
    invite_req = SipRequest.parse(_make_invite(call_id=call_id))
    new_call = NewCall(registration=_ext_config(), invite=invite_req)

    await adapter._handle_inbound_invite(new_call)

    busy = [m for m in transport.sent if m.startswith("SIP/2.0 486")]
    assert busy, f"a call at capacity must get 486 Busy Here, sent: {transport.sent!r}"
    # No 200 OK — the call was never answered / no media opened.
    assert not any(m.startswith("SIP/2.0 200") for m in transport.sent)


async def test_admission_slot_released_on_teardown_no_leak() -> None:
    """An admitted call's slot is released on teardown (no leak across calls)."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    call_id = new_call_id()
    adapter._admitted_calls.add(call_id)
    session = _FakeSession(ended=True, call_id=call_id)
    adapter._call_sessions[call_id] = session  # type: ignore[assignment]  # _FakeSession is a CallSession test double

    await adapter._teardown_call(
        call_id=call_id,
        engine=_fake_engine(),
        transport=transport,  # type: ignore[arg-type]  # fake transport double
        dialog_id=session.dialog_id,
        session=session,  # type: ignore[arg-type]  # _FakeSession double
        reason=CallEndReason.REMOTE_BYE,
    )

    assert call_id not in adapter._admitted_calls, (
        "the admission slot must be released on teardown so capacity does not leak"
    )


async def test_ended_call_info_retention_is_bounded_no_leak() -> None:
    """Ended ``_call_info`` entries are bounded (CWE-400), not retained forever.

    They are kept (flagged ``ended``) for ``send()`` late-reply suppression, but
    ``_teardown_call`` never removed them, so sequential calls (distinct forgeable
    Call-IDs) grew ``_call_info`` without bound over the long-lived adapter's lifetime.
    """
    from hermes_voip.adapter import _MAX_ENDED_CALL_INFO  # noqa: PLC0415

    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    for i in range(_MAX_ENDED_CALL_INFO + 100):
        call_id = f"seq-call-{i}"
        adapter._call_info[call_id] = {"name": "caller", "ended": True}
        await adapter._teardown_call(
            call_id=call_id,
            engine=_fake_engine(),
            transport=transport,  # type: ignore[arg-type]  # fake transport double
            dialog_id=(call_id, "lt", "rt"),
            reason=CallEndReason.REMOTE_BYE,
        )

    assert len(adapter._call_info) <= _MAX_ENDED_CALL_INFO


async def test_recent_ended_call_info_survives_for_late_reply_suppression() -> None:
    """A just-ended call's ``_call_info`` entry survives the bound for suppression.

    ``send()`` must still be able to drop a late reply to it — only entries older than
    the retention cap are reaped, never a recent one (or a still-live call's).
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    recent = "recent-call"
    adapter._call_info[recent] = {"name": "caller", "ended": True}
    await adapter._teardown_call(
        call_id=recent,
        engine=_fake_engine(),
        transport=transport,  # type: ignore[arg-type]  # fake transport double
        dialog_id=(recent, "lt", "rt"),
        reason=CallEndReason.REMOTE_BYE,
    )
    # A handful of later calls end; the recent one is well within the cap and survives.
    for i in range(10):
        cid = f"later-{i}"
        adapter._call_info[cid] = {"name": "x", "ended": True}
        await adapter._teardown_call(
            call_id=cid,
            engine=_fake_engine(),
            transport=transport,  # type: ignore[arg-type]  # fake transport double
            dialog_id=(cid, "lt", "rt"),
            reason=CallEndReason.REMOTE_BYE,
        )

    assert adapter._call_info.get(recent, {}).get("ended") is True


async def test_disconnect_clears_call_info_no_residual() -> None:
    """``disconnect()`` clears ``_call_info`` (it omitted it from the shutdown clear).

    It cleared 8 sibling per-call maps but not ``_call_info``, leaving the per-call
    metadata behind at shutdown.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    adapter._call_info["c1"] = {"name": "caller", "ended": True}

    await adapter.disconnect()

    assert adapter._call_info == {}


async def test_inbound_invite_under_capacity_is_admitted() -> None:
    """Below the cap, a new INVITE is admitted, answered 200 OK, and leaks no slot.

    The media collaborators (RTP engine / CallSession / CallLoop / VAD / endpointer)
    are mocked — like ``tests/test_adapter.py``'s inbound tests — so this exercises
    ADMISSION only, not the real silero VAD model load.

    The offer here is plain ``RTP/AVP`` (``_FAKE_SDP_OFFER``), and the ADR-0070
    secure-media mandate defaults ON, which would 488-reject a cleartext offer at
    the guard that sits BEFORE ``_admit_inbound`` — masking this admission test
    entirely (the INVITE would never reach admission, yet the old "no 486" /
    "no leaked slot" assertions would still hold for a 488). This test exercises the
    CLEARTEXT admission path, so the mandate is turned OFF for it; the mandate has
    its own end-to-end coverage in ``tests/test_adapter_secure_media.py``.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(
        transport, _FakeManager(), env={"HERMES_SIP_MAX_CALLS": "4"}
    )
    # Cleartext admission path: disable the secure-media mandate so the plain
    # RTP/AVP offer is admitted rather than 488'd before admission control runs.
    adapter._media_cfg = load_media_config(
        {"HERMES_VOIP_REQUIRE_SECURE_MEDIA": "false"}
    )

    call_id = new_call_id()
    invite_req = SipRequest.parse(_make_invite(call_id=call_id))
    new_call = NewCall(registration=_ext_config(), invite=invite_req)

    with (
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

    # Admission must have been REACHED and PASSED: the only way a 200 OK is sent is
    # for the INVITE to clear the 486 fast-path, the 488 mandate guard (off here) and
    # the codec preflight, then be admitted by ``_admit_inbound`` and answered. A 488
    # (the masking failure this test guards against) would emit no 200 OK.
    assert any(m.startswith("SIP/2.0 200") for m in transport.sent), (
        f"an under-capacity INVITE must be admitted and answered 200 OK, "
        f"sent: {transport.sent!r}"
    )
    assert not any(m.startswith("SIP/2.0 486") for m in transport.sent), (
        "a call under capacity must NOT be rejected 486"
    )
    assert not any(m.startswith("SIP/2.0 488") for m in transport.sent), (
        "the mandate is off here, so the cleartext offer must NOT be 488-rejected"
    )
    # The slot was reserved during the call and released by the teardown that runs
    # when the (mocked) CallLoop returns — no leak.
    assert call_id not in adapter._admitted_calls


class _InboundLeakCase(NamedTuple):
    """The mocked collaborators + ids for one inbound post-answer leak test."""

    adapter: VoipAdapter
    call_id: str
    engine: MagicMock
    session: MagicMock
    dialog_id: tuple[str, str, str]


async def _drive_inbound_to_post_answer(
    transport: _FakeTransport, manager: _FakeManager
) -> _InboundLeakCase:
    """Build an adapter + INVITE that clears admission and is answered 200 OK.

    The RTP engine / CallSession / GuardSessionState are mocked (the test patches
    them in), so the real _setup_sdes_call sends the 200 OK but no real pipeline is
    built; the test then injects a post-answer failure and asserts nothing leaks.
    """
    adapter = await _build_adapter(
        transport, manager, env={"HERMES_SIP_MAX_CALLS": "4"}
    )
    # Cleartext admission path (the plain RTP/AVP offer), like the admission test.
    adapter._media_cfg = load_media_config(
        {"HERMES_VOIP_REQUIRE_SECURE_MEDIA": "false"}
    )
    call_id = new_call_id()
    engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        start_rtcp=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=20002,
        inbound_sample_rate=8_000,
    )
    dialog_id = (call_id, "local-tag", "remote-tag")
    session = MagicMock(dialog_id=dialog_id, ended=False)
    return _InboundLeakCase(adapter, call_id, engine, session, dialog_id)


def _assert_answered_then_no_leak(
    transport: _FakeTransport, manager: _FakeManager, case: _InboundLeakCase
) -> None:
    """The answered call's setup failed, so it must release everything it allocated.

    Checks the exact set that leaked into a 486-Busy DoS before the leak-safe boundary
    (L1705): the admission slot, the RTP engine (socket), and both in-dialog routes.
    """
    assert any(m.startswith("SIP/2.0 200") for m in transport.sent), (
        f"the call must have been answered before the failure; sent: {transport.sent!r}"
    )
    assert case.call_id not in case.adapter._admitted_calls, (
        "admission slot leaked — repeat max_calls times for a 486-Busy DoS (L1705)"
    )
    case.engine.stop.assert_awaited_once()  # the bound RTP socket is released
    assert case.call_id not in transport._calls, "transport in-dialog route leaked"
    assert case.dialog_id not in manager._calls, "manager in-dialog route leaked"
    assert case.call_id not in case.adapter._call_sessions, "call-session mirror leaked"


async def test_inbound_post_answer_context_failure_releases_all_no_leak() -> None:
    """A forgeable header making extract_call_context raise post-answer is leak-safe.

    extract_call_context is documented "never raises" but a Unicode-digit
    History-Info index does; before the leak-safe boundary this ran outside any
    try/finally, so the raise skipped _teardown_call and permanently held the slot.
    """
    transport = _FakeTransport()
    manager = _FakeManager()
    case = await _drive_inbound_to_post_answer(transport, manager)
    invite_req = SipRequest.parse(_make_invite(call_id=case.call_id))
    new_call = NewCall(registration=_ext_config(), invite=invite_req)

    with (
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=case.engine),
        patch("hermes_voip.adapter.CallSession", return_value=case.session),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch(
            "hermes_voip.adapter.extract_call_context",
            MagicMock(side_effect=RuntimeError("forged INVITE header")),
        ),
        pytest.raises(RuntimeError, match="forged INVITE header"),
    ):
        await case.adapter._handle_inbound_invite(new_call)

    _assert_answered_then_no_leak(transport, manager, case)


async def test_inbound_post_answer_timer_failure_releases_all_no_leak() -> None:
    """A different post-answer step (_start_session_timer) raising is equally leak-safe.

    Proves the whole answered-setup region — not just extract_call_context — is inside
    the single leak-safe try/finally (L1705).
    """
    transport = _FakeTransport()
    manager = _FakeManager()
    case = await _drive_inbound_to_post_answer(transport, manager)
    invite_req = SipRequest.parse(_make_invite(call_id=case.call_id))
    new_call = NewCall(registration=_ext_config(), invite=invite_req)

    with (
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=case.engine),
        patch("hermes_voip.adapter.CallSession", return_value=case.session),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch.object(
            case.adapter,
            "_start_session_timer",
            side_effect=RuntimeError("timer boom"),
        ),
        pytest.raises(RuntimeError, match="timer boom"),
    ):
        await case.adapter._handle_inbound_invite(new_call)

    _assert_answered_then_no_leak(transport, manager, case)


# ===========================================================================
# Item 4 — Secret redaction in unroutable logs
# ===========================================================================

_SECRET_AUTH = (
    'Digest username="1000", realm="pbx.example.test", '
    'nonce="abc123", uri="sip:1000@pbx.example.test", '
    'response="DEADBEEFDEADBEEFDEADBEEFDEADBEEF", algorithm=MD5'
)
# Synthetic SRTP key computed at runtime (30 bytes -> 40-char base64) so no
# key-shaped literal lives in a tracked file and trips the gitleaks all-refs
# scan (the allowlist is reserved for tests/test_sdp.py + g722_kat_vectors.py).
_SECRET_CRYPTO_KEY = base64.b64encode(bytes(range(30))).decode()


def _unroutable_with_secrets(call_id: str) -> Unroutable:
    raw = (
        f"INVITE sip:9999@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKx\r\n"
        f"From: <sip:9999@pbx.example.test>;tag=ft\r\n"
        f"To: <sip:9999@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Authorization: {_SECRET_AUTH}\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
        f"v=0\r\n"
        f"m=audio 20000 RTP/SAVP 0\r\n"
        f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_SECRET_CRYPTO_KEY}\r\n"
    )
    return Unroutable(request=SipRequest.parse(raw), reason="no matching registration")


async def test_unroutable_log_redacts_authorization_and_crypto(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The unroutable log must not leak the digest response or SRTP key material."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())

    with caplog.at_level(logging.DEBUG, logger="hermes_voip.adapter"):
        adapter._on_unroutable(_unroutable_with_secrets(new_call_id()))

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "DEADBEEFDEADBEEFDEADBEEFDEADBEEF" not in blob, (
        "the Authorization digest response must be redacted from the log"
    )
    assert _SECRET_CRYPTO_KEY not in blob, (
        "the a=crypto SRTP key material must be redacted from the log"
    )


async def test_unroutable_log_redacts_response_authorization(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unroutable RESPONSE carrying an Authorization echo is also redacted."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, _FakeManager())
    raw = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKx\r\n"
        "From: <sip:1000@pbx.example.test>;tag=ft\r\n"
        "To: <sip:1000@pbx.example.test>;tag=tt\r\n"
        "Call-ID: stale-call\r\n"
        "CSeq: 1 REGISTER\r\n"
        f"Authorization: {_SECRET_AUTH}\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )
    response = SipResponse.parse(raw)

    with caplog.at_level(logging.DEBUG, logger="hermes_voip.adapter"):
        adapter._on_unroutable(response)

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "DEADBEEFDEADBEEFDEADBEEFDEADBEEF" not in blob, (
        "an unroutable response's Authorization must be redacted too"
    )
