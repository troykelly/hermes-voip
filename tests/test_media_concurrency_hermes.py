"""Adapter-level concurrent-INVITE isolation test (requires hermes extra).

This test file is excluded from the default mypy gate (no hermes extra installed
in the base environment) and the hermes-contract CI job type-checks it via its
own mypy run. The test is skipped via ``pytest.importorskip`` if the hermes
runtime is absent.

Root-cause scenario: three concurrent INVITEs with the same Call-ID arrive (the
gateway retry/fork pattern). With the un-fixed adapter, task_1's teardown removes
task_2's and task_3's _call_loops/_call_sessions entries from the shared dicts
(unconditional pop by Call-ID). The fix adds identity checks so only THIS task's
entries are removed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from hermes_voip.media.audio import G711_SAMPLE_RATE
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.tts import TtsStream

# ---------------------------------------------------------------------------
# Minimal fakes (only what the adapter integration test needs)
# ---------------------------------------------------------------------------


class _DrainIterator:
    """AsyncIterator that drains PcmFrame source and emits no Transcripts."""

    def __init__(self, audio: AsyncIterator[PcmFrame]) -> None:
        self._audio = audio
        self._exhausted = False

    def __aiter__(self) -> AsyncIterator[Transcript]:
        return self

    async def __anext__(self) -> Transcript:
        if self._exhausted:
            raise StopAsyncIteration
        try:
            _ = await self._audio.__anext__()
        except StopAsyncIteration:
            self._exhausted = True
            raise
        raise StopAsyncIteration


class _ImmediateASR:
    """StreamingASR fake: drains audio without blocking; emits no transcripts."""

    @property
    def input_sample_rate(self) -> int:
        return G711_SAMPLE_RATE

    def stream(
        self,
        audio: AsyncIterator[PcmFrame],
    ) -> AsyncIterator[Transcript]:
        return _DrainIterator(audio)


class _NullTtsStream:
    """TtsStream fake: yields no frames (empty utterance)."""

    def __init__(self) -> None:
        self._done = False

    async def flush(self) -> None:
        pass

    async def cancel(self) -> None:
        self._done = True

    async def aclose(self) -> None:
        self._done = True

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self

    async def __anext__(self) -> PcmFrame:
        raise StopAsyncIteration


class _NullTTS:
    """StreamingTTS fake: produces an empty stream on every synthesize() call."""

    @property
    def output_sample_rate(self) -> int:
        return G711_SAMPLE_RATE

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        return _NullTtsStream()


class _NullGuard:
    """InjectionGuard fake: always allows."""

    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            score=0.0,
            degraded=False,
            normalized_text=text,
            reasons=(),
        )


# ---------------------------------------------------------------------------
# Adapter isolation test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_concurrent_same_call_id_teardown_isolation() -> None:  # noqa: PLR0915 — adapter integration test requires full SIP/media/loop mock stack; extraction would only scatter the complexity
    """Adapter: teardown of first INVITE task must not stop sibling call engines.

    With same Call-ID (gateway retry), _call_tasks, _call_loops, and
    _call_sessions are all keyed by the same string. When task_1 completes first
    and _teardown_call runs, it pops _call_loops[call_id] (REMOVING task_2's or
    task_3's loop) and calls engine_1.stop(). The test verifies:
    (a) engine_1.stop() does NOT affect engine_2 or engine_3.
    (b) After task_1's teardown, task_2's call_loop is STILL callable (not removed
        from _call_loops / _call_sessions by task_1's teardown).

    This requires the hermes extra (BasePlatformAdapter). Skipped if absent.
    """
    pytest.importorskip("gateway.platforms.base")
    pytest.importorskip("gateway.config")

    from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415

    from gateway.config import PlatformConfig  # noqa: PLC0415
    from gateway.platform_registry import (  # noqa: PLC0415
        PlatformEntry,
        platform_registry,
    )

    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415
    from hermes_voip.config import ExtensionConfig, GatewayConfig  # noqa: PLC0415
    from hermes_voip.manager import NewCall  # noqa: PLC0415
    from hermes_voip.message import SipRequest, new_call_id, new_tag  # noqa: PLC0415

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

    fake_sdp_offer = (
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

    shared_call_id = new_call_id()

    def _make_invite_raw() -> str:
        content_length = len(fake_sdp_offer.encode("utf-8"))
        return (
            f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
            f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKfake\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:9999@pbx.example.test>;tag={new_tag()}\r\n"
            "To: <sip:1000@pbx.example.test>\r\n"
            f"Call-ID: {shared_call_id}\r\n"
            "CSeq: 1 INVITE\r\n"
            "Contact: <sip:9999@127.0.0.1:60000;transport=tls>\r\n"
            "Content-Type: application/sdp\r\n"
            f"Content-Length: {content_length}\r\n"
            f"\r\n"
            f"{fake_sdp_offer}"
        )

    # Track which engine instances were created and which were stopped.
    created_engines: list[MagicMock] = []
    stopped_engine_ids: set[int] = set()

    def _make_engine(*a: object, **kw: object) -> MagicMock:
        engine = MagicMock()
        engine.connect = AsyncMock(return_value=True)
        engine.stop = AsyncMock(side_effect=lambda: stopped_engine_ids.add(id(engine)))
        # RTCP activation (ADR-0061): the inbound plain-RTP path starts RTCP after
        # connect(); model the awaitable method + the inert _rtcp_active flag.
        engine.start_rtcp = AsyncMock(return_value=None)
        engine._rtcp_active = False
        engine.local_port = 20000 + len(created_engines) * 2
        engine.inbound_sample_rate = G711_SAMPLE_RATE
        created_engines.append(engine)
        return engine

    # Three call loops: task_1 completes immediately; task_2 and task_3 block.
    # We use asyncio.Event to control when each loop returns.
    loop_gates: list[asyncio.Event] = [
        asyncio.Event(),  # gate for loop_1 (unblocked → completes fast)
        asyncio.Event(),  # gate for loop_2 (blocked until we assert)
        asyncio.Event(),  # gate for loop_3 (blocked until we assert)
    ]
    loop_index = 0
    created_loops: list[MagicMock] = []

    def _make_loop(*a: object, **kw: object) -> MagicMock:
        nonlocal loop_index
        idx = loop_index
        loop_index += 1
        gate = loop_gates[min(idx, len(loop_gates) - 1)]

        async def _blocking_run() -> None:
            await gate.wait()

        mock_loop = MagicMock()
        mock_loop.run = _blocking_run
        created_loops.append(mock_loop)
        return mock_loop

    class _FakeSipTransport:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._calls: dict[str, object] = {}

        @property
        def local_sent_by(self) -> str:
            return "127.0.0.1:5061"

        def contact_uri(self, ext: str) -> str:
            return f"<sip:{ext}@127.0.0.1:5061;transport=tls>"

        async def send(self, message: str) -> None:
            self.sent.append(message)

        async def connect(self) -> None:
            pass

        async def aclose(self) -> None:
            pass

        def bind_manager(self, manager: object) -> None:
            pass

        def add_call(self, call_id: str, sink: object) -> None:
            self._calls[call_id] = sink

        def remove_call(self, call_id: str, sink: object | None = None) -> None:
            if sink is not None and self._calls.get(call_id) is not sink:
                return
            self._calls.pop(call_id, None)

    class _FakeMgr:
        def __init__(self) -> None:
            self._calls: dict[object, object] = {}
            self.connected = False

        @property
        def is_up(self) -> bool:
            return True

        async def connect(self, *, timeout: float = 10.0) -> bool:
            self.connected = True
            return True

        async def aclose(self) -> None:
            pass

        def add_call(self, dialog_id: object, consumer: object) -> None:
            self._calls[dialog_id] = consumer

        def remove_call(self, dialog_id: object) -> None:
            self._calls.pop(dialog_id, None)

    sip_transport = _FakeSipTransport()
    mgr = _FakeMgr()

    gateway_cfg = GatewayConfig(
        host="pbx.example.test",
        port=5061,
        transport="tls",
        expires=300,
        user_agent="hermes-voip/0",
        extensions=(
            ExtensionConfig(index=0, extension="1000", username="1000", password="x"),
        ),
        default_index=0,
    )

    config = PlatformConfig(enabled=True, extra={})

    with (
        patch("hermes_voip.adapter.load_gateway_config", return_value=gateway_cfg),
        patch(
            "hermes_voip.adapter.load_media_config",
            return_value=MagicMock(
                greeting="",
                rtp_symmetric=True,
                vad_threshold=0.5,
                vad_model_dir="/fake/vad",
                endpoint_silence_ms=500,
                barge_in_mode="gated",
                barge_in_min_speech_ms=400,
                barge_in_tail_ms=250,
                barge_in_fade_ms=30,
                # Plain RTP/AVP concurrency test — the secure-media mandate (ADR-0070)
                # is off so the cleartext offers are admitted, not 488'd.
                require_secure_media=False,
                # Real ints so the RFC 4028 session-timer negotiation (ADR-0071)
                # compares them, not TypeError on a MagicMock.
                session_expires=600,
                min_se=90,
            ),
        ),
        patch(
            "hermes_voip.adapter.build_providers",
            return_value=MagicMock(
                asr=_ImmediateASR(),
                tts=_NullTTS(),
                guard=_NullGuard(),
            ),
        ),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=sip_transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=mgr),
        patch("hermes_voip.adapter.RtpMediaTransport", side_effect=_make_engine),
        patch("hermes_voip.adapter.CallLoop", side_effect=_make_loop),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()

        ext_cfg = ExtensionConfig(
            index=0, extension="1000", username="1000", password="x"
        )

        # Send 3 overlapping INVITEs with the SAME Call-ID (gateway retry).
        for _ in range(3):
            invite = SipRequest.parse(_make_invite_raw())
            adapter._on_inbound_invite(NewCall(registration=ext_cfg, invite=invite))

        # Let all three tasks start (reach their _blocking_run gate).
        for _ in range(20):
            await asyncio.sleep(0)

        # Three engines must have been created (one per INVITE).
        assert len(created_engines) == 3, (
            f"expected 3 engines for 3 INVITEs, got {len(created_engines)}"
        )

        # Unblock loop_1 (task_1 completes → _teardown_call for engine_1 runs).
        loop_gates[0].set()
        for _ in range(20):
            await asyncio.sleep(0)

        # engine_1 (index 0) must be stopped, engine_2 and engine_3 must NOT be.
        engine_1_id = id(created_engines[0])
        engine_2_id = id(created_engines[1])
        engine_3_id = id(created_engines[2])

        assert engine_1_id in stopped_engine_ids, (
            "engine_1 should have been stopped when task_1 completed"
        )
        # The key assertion for the isolation bug:
        assert engine_2_id not in stopped_engine_ids, (
            "engine_2 was stopped by task_1's teardown — cross-engine stop (the bug)"
        )
        assert engine_3_id not in stopped_engine_ids, (
            "engine_3 was stopped by task_1's teardown — cross-engine stop (the bug)"
        )

        # --- The dict-clobber invariant (the literal same-Call-ID isolation bug) -
        # All three INVITEs share one Call-ID, so _call_loops / _call_sessions /
        # the transport response sink are all keyed by that single string and a
        # later task's registration OVERWRITES an earlier one's. When task_1's
        # teardown ran above it must NOT have evicted the still-live entry that now
        # belongs to a LATER task (task_2 or task_3). Without the identity checks in
        # _teardown_call this is exactly what breaks: task_1's unconditional
        # pop(call_id) removes the surviving task's CallLoop / CallSession / sink,
        # silently killing in-dialog routing and speak() delivery for a live call.
        #
        # The registered entry must (a) still be present and (b) belong to a
        # surviving task — never task_1's own (now-torn-down) objects.
        surviving_loops = {id(loop) for loop in created_loops[1:]}
        assert shared_call_id in adapter._call_loops, (
            "task_1's teardown evicted the shared-Call-ID CallLoop that belongs to "
            "a still-live task (the same-Call-ID dict-clobber bug)"
        )
        assert id(adapter._call_loops[shared_call_id]) in surviving_loops, (
            "the registered CallLoop for the shared Call-ID is task_1's own (torn "
            "down) loop, not a surviving task's — teardown clobbered live state"
        )
        assert id(adapter._call_loops[shared_call_id]) != id(created_loops[0]), (
            "task_1's CallLoop is still registered after its own teardown"
        )
        assert shared_call_id in adapter._call_sessions, (
            "task_1's teardown evicted the shared-Call-ID CallSession that belongs "
            "to a still-live task (the same-Call-ID dict-clobber bug)"
        )
        assert shared_call_id in sip_transport._calls, (
            "task_1's teardown evicted the shared-Call-ID transport response sink "
            "that belongs to a still-live task (the same-Call-ID dict-clobber bug)"
        )
        # The live call must NOT have been flagged ended by task_1's teardown.
        info = adapter._call_info.get(shared_call_id, {})
        assert info.get("ended") is not True, (
            "task_1's teardown flagged the shared-Call-ID call ended while a later "
            "task is still live (the _call_info clobber)"
        )

        # Unblock all remaining loops so the test ends cleanly.
        for gate in loop_gates:
            gate.set()
        # Let tasks finish cleanly.
        for _ in range(20):
            await asyncio.sleep(0)

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_concurrent_same_call_id_all_tasks_tracked_and_cancelled() -> None:  # noqa: PLR0915 — full SIP/media/loop mock stack required; extraction only scatters it
    """disconnect() must cancel EVERY concurrent same-Call-ID task, not just one.

    Reproduces codex BLOCK-2: ``_call_tasks`` is keyed by raw Call-ID, so 3
    overlapping INVITEs with the SAME Call-ID overwrite each other — only the LAST
    task is tracked. On ``disconnect()`` the earlier 2 tasks are orphaned: never
    cancelled, their engines never stopped (a media/thread leak per missed call).

    The fix tracks all concurrent tasks per Call-ID; disconnect cancels them all.
    This test blocks every call loop forever (so none completes on its own), then
    asserts that after ``disconnect()`` every started task is done (cancelled) and
    every engine was stopped.
    """
    pytest.importorskip("gateway.platforms.base")
    pytest.importorskip("gateway.config")

    from unittest.mock import AsyncMock, MagicMock, patch  # noqa: PLC0415

    from gateway.config import PlatformConfig  # noqa: PLC0415
    from gateway.platform_registry import (  # noqa: PLC0415
        PlatformEntry,
        platform_registry,
    )

    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415
    from hermes_voip.config import ExtensionConfig, GatewayConfig  # noqa: PLC0415
    from hermes_voip.manager import NewCall  # noqa: PLC0415
    from hermes_voip.message import SipRequest, new_call_id, new_tag  # noqa: PLC0415

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

    fake_sdp_offer = (
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

    shared_call_id = new_call_id()

    def _make_invite_raw() -> str:
        content_length = len(fake_sdp_offer.encode("utf-8"))
        return (
            f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
            f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bK{new_tag()}\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:9999@pbx.example.test>;tag={new_tag()}\r\n"
            "To: <sip:1000@pbx.example.test>\r\n"
            f"Call-ID: {shared_call_id}\r\n"
            "CSeq: 1 INVITE\r\n"
            "Contact: <sip:9999@127.0.0.1:60000;transport=tls>\r\n"
            "Content-Type: application/sdp\r\n"
            f"Content-Length: {content_length}\r\n"
            f"\r\n"
            f"{fake_sdp_offer}"
        )

    created_engines: list[MagicMock] = []
    stopped_engine_ids: set[int] = set()

    def _make_engine(*a: object, **kw: object) -> MagicMock:
        engine = MagicMock()
        engine.connect = AsyncMock(return_value=True)
        engine.stop = AsyncMock(side_effect=lambda: stopped_engine_ids.add(id(engine)))
        # RTCP activation (ADR-0061): the inbound plain-RTP path starts RTCP after
        # connect(); model the awaitable method + the inert _rtcp_active flag.
        engine.start_rtcp = AsyncMock(return_value=None)
        engine._rtcp_active = False
        engine.local_port = 20000 + len(created_engines) * 2
        engine.inbound_sample_rate = G711_SAMPLE_RATE
        created_engines.append(engine)
        return engine

    # Every call loop blocks forever — only disconnect() can end them.
    block_forever = asyncio.Event()
    created_loops: list[MagicMock] = []

    def _make_loop(*a: object, **kw: object) -> MagicMock:
        async def _blocking_run() -> None:
            await block_forever.wait()

        mock_loop = MagicMock()
        mock_loop.run = _blocking_run
        created_loops.append(mock_loop)
        return mock_loop

    class _FakeSipTransport:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.calls: dict[str, object] = {}

        @property
        def local_sent_by(self) -> str:
            return "127.0.0.1:5061"

        def contact_uri(self, ext: str) -> str:
            return f"<sip:{ext}@127.0.0.1:5061;transport=tls>"

        async def send(self, message: str) -> None:
            self.sent.append(message)

        async def connect(self) -> None:
            pass

        async def aclose(self) -> None:
            pass

        def bind_manager(self, manager: object) -> None:
            pass

        def add_call(self, call_id: str, sink: object) -> None:
            self.calls[call_id] = sink

        def remove_call(self, call_id: str, sink: object | None = None) -> None:
            if sink is not None and self.calls.get(call_id) is not sink:
                return
            self.calls.pop(call_id, None)

    class _FakeMgr:
        def __init__(self) -> None:
            self.calls: dict[object, object] = {}

        @property
        def is_up(self) -> bool:
            return True

        async def connect(self, *, timeout: float = 10.0) -> bool:
            return True

        async def aclose(self) -> None:
            pass

        def add_call(self, dialog_id: object, consumer: object) -> None:
            self.calls[dialog_id] = consumer

        def remove_call(self, dialog_id: object) -> None:
            self.calls.pop(dialog_id, None)

    sip_transport = _FakeSipTransport()
    mgr = _FakeMgr()

    gateway_cfg = GatewayConfig(
        host="pbx.example.test",
        port=5061,
        transport="tls",
        expires=300,
        user_agent="hermes-voip/0",
        extensions=(
            ExtensionConfig(index=0, extension="1000", username="1000", password="x"),
        ),
        default_index=0,
    )

    config = PlatformConfig(enabled=True, extra={})

    with (
        patch("hermes_voip.adapter.load_gateway_config", return_value=gateway_cfg),
        patch(
            "hermes_voip.adapter.load_media_config",
            return_value=MagicMock(
                greeting="",
                rtp_symmetric=True,
                vad_threshold=0.5,
                vad_model_dir="/fake/vad",
                endpoint_silence_ms=500,
                barge_in_mode="gated",
                barge_in_min_speech_ms=400,
                barge_in_tail_ms=250,
                barge_in_fade_ms=30,
                # Plain RTP/AVP concurrency test — the secure-media mandate (ADR-0070)
                # is off so the cleartext offers are admitted, not 488'd.
                require_secure_media=False,
                # Real ints so the RFC 4028 session-timer negotiation (ADR-0071)
                # compares them, not TypeError on a MagicMock.
                session_expires=600,
                min_se=90,
            ),
        ),
        patch(
            "hermes_voip.adapter.build_providers",
            return_value=MagicMock(
                asr=_ImmediateASR(),
                tts=_NullTTS(),
                guard=_NullGuard(),
            ),
        ),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=sip_transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=mgr),
        patch("hermes_voip.adapter.RtpMediaTransport", side_effect=_make_engine),
        patch("hermes_voip.adapter.CallLoop", side_effect=_make_loop),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()

        ext_cfg = ExtensionConfig(
            index=0, extension="1000", username="1000", password="x"
        )

        # 3 overlapping INVITEs with the SAME Call-ID; capture the actual handler
        # task objects created (so we can assert they all get cancelled, not just
        # one) by diffing the live task set across each _on_inbound_invite call.
        started_tasks: list[asyncio.Task[object]] = []
        for _ in range(3):
            before = asyncio.all_tasks()
            invite = SipRequest.parse(_make_invite_raw())
            adapter._on_inbound_invite(NewCall(registration=ext_cfg, invite=invite))
            new_tasks = asyncio.all_tasks() - before
            started_tasks.extend(new_tasks)

        # Let all three handler tasks reach their blocking run.
        for _ in range(30):
            await asyncio.sleep(0)

        # Three engines created, three call loops started, three tasks captured.
        assert len(created_engines) == 3, f"engines: {len(created_engines)}"
        assert len(created_loops) == 3, f"loops: {len(created_loops)}"
        assert len(started_tasks) == 3, f"tasks: {len(started_tasks)}"

        # disconnect() must cancel EVERY task (not just the last same-Call-ID one)
        # and stop EVERY engine. Before the fix, _call_tasks held only the last
        # task, so the first two were orphaned and their engines never stopped.
        await adapter.disconnect()

        for _ in range(30):
            await asyncio.sleep(0)

        # Snapshot the outcome BEFORE any cleanup, so the assertions below describe
        # exactly what disconnect() did.
        not_done = [t for t in started_tasks if not t.done()]
        unstopped = [id(e) for e in created_engines if id(e) not in stopped_engine_ids]

    # Clean up any orphaned tasks the (pre-fix) bug left pending, so a RED run
    # does not leak tasks into the rest of the suite. (When GREEN this is a no-op:
    # disconnect() already cancelled them.)
    block_forever.set()
    for task in started_tasks:
        task.cancel()
    await asyncio.gather(*started_tasks, return_exceptions=True)

    assert not_done == [], (
        f"{len(not_done)} same-Call-ID task(s) were NOT cancelled by "
        "disconnect() — orphaned (the _call_tasks clobber bug)"
    )
    assert unstopped == [], (
        f"{len(unstopped)} engine(s) were never stopped after disconnect() — "
        "orphaned same-Call-ID call engines leaked"
    )
