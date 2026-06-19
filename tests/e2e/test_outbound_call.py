"""End-to-end outbound call integration test (ADR-0019, RED phase).

Tests the full outbound UAC flow:
- Plugin sends INVITE
- Gateway challenges with 407
- Plugin re-sends with Proxy-Authorization
- Gateway sends 180 Ringing then 200 OK with SDP answer
- Plugin sends ACK
- RTP flows (plugin sends greeting audio)
- Gateway sends BYE
- Plugin tears down cleanly

Also tests error paths:
- 486 Busy Here -> OutboundCallFailed(486)
- CALL_ON_CONNECT env var fires place_call once after connect, not on reconnect

These tests fail until adapter.py + originate.py are implemented.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest

# The adapter imports the real Hermes base at module top; skip the whole module
# when the optional runtime is absent (it runs in the hermes-contract CI job).
pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_voip.media.engine import RtpMediaTransport
from hermes_voip.message import SipRequest, build_request, new_branch, new_tag
from hermes_voip.originate import OutboundCallFailed
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.tts import TtsStream
from hermes_voip.transport.connection import SipOverTlsTransport
from tests.e2e._fake_gateway import (
    FakeRtpEndpoint,
)
from tests.transport._loopback import LoopbackSipServer, client_ssl_context

_KOKORO_RATE = 24_000
_G711_RATE = 8_000
_PTIME_MS = 20
_SAMPLES_PER_FRAME_24K = (_KOKORO_RATE * _PTIME_MS) // 1000
_SAMPLES_PER_FRAME_8K = (_G711_RATE * _PTIME_MS) // 1000

_TO_USER = "1000"  # the extension the plugin registers as
_TARGET_EXT = "1001"  # the extension the plugin dials outbound

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.filterwarnings("error::RuntimeWarning"),
]


# ---------------------------------------------------------------------------
# Fake providers (same pattern as inbound tests)
# ---------------------------------------------------------------------------


class _RecordingVadModel:
    def __init__(self) -> None:
        self.sample_rates: list[int] = []

    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        self.sample_rates.append(sample_rate)
        return 0.9 if any(window_pcm16) else 0.0


class _FakeTtsStream:
    def __init__(
        self,
        frames: list[PcmFrame],
        text: AsyncIterator[str],
        recorded: list[str],
    ) -> None:
        self._frames = list(frames)
        self._index = 0
        self._cancelled = False
        self._text = text
        self._recorded = recorded
        self._text_drained = False

    def __aiter__(self) -> _FakeTtsStream:
        return self

    async def __anext__(self) -> PcmFrame:
        if not self._text_drained:
            self._text_drained = True
            chunks = [chunk async for chunk in self._text]
            self._recorded.append("".join(chunks))
        if self._cancelled or self._index >= len(self._frames):
            raise StopAsyncIteration
        frame = self._frames[self._index]
        self._index += 1
        return frame

    async def flush(self) -> None:
        pass

    async def cancel(self) -> None:
        self._cancelled = True

    async def aclose(self) -> None:
        self._cancelled = True


class _FakeTTS:
    output_sample_rate = _KOKORO_RATE

    def __init__(self) -> None:
        self.synth_texts: list[str] = []

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        sample = (4096).to_bytes(2, "little", signed=True)
        frame = PcmFrame(
            samples=sample * _SAMPLES_PER_FRAME_24K,
            sample_rate=_KOKORO_RATE,
            monotonic_ts_ns=0,
        )
        return _FakeTtsStream([frame, frame], text, self.synth_texts)


class _FakeASR:
    input_sample_rate = 16_000

    def __init__(self, *, transcript: str = "") -> None:
        self._transcript = transcript

    async def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        async for _ in audio:
            pass
        # Never emit a turn; satisfy the async generator contract.
        return
        yield  # type: ignore[unreachable]  # dead yield makes this an async generator


class _FakeGuard:
    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            normalized_text=text,
            reasons=(),
            degraded=False,
            score=0.0,
        )


# ---------------------------------------------------------------------------
# Gateway side: respond to outbound INVITE (UAS role)
# ---------------------------------------------------------------------------


class OutboundGateway:
    """Fake gateway that handles INVITE from the plugin (acting as UAS).

    Sequence:
    1. Plugin sends INVITE.
    2. Gateway sends 407 Proxy Auth Required.
    3. Plugin re-sends with Proxy-Authorization.
    4. Gateway sends 100 Trying + 180 Ringing + 200 OK with SDP answer.
    5. Test awaits ACK from plugin.
    6. Gateway sends BYE.
    """

    sip_host = "pbx.example.test"

    def __init__(self) -> None:
        self._server = LoopbackSipServer(self._respond)
        self.rtp = FakeRtpEndpoint()
        self._register_responder: (
            Callable[[SipRequest], Awaitable[list[str]]] | None
        ) = None
        self._sip_port = 0
        # Received SIP requests from plugin (UAC)
        self._received_invites: asyncio.Queue[SipRequest] = asyncio.Queue()
        self._received_acks: asyncio.Queue[SipRequest] = asyncio.Queue()
        self._received_byes: asyncio.Queue[SipRequest] = asyncio.Queue()
        # Track challenge state per Call-ID (0 = unseen, 1 = challenged, 2+ = auth'd)
        self._invite_challenges: dict[str, int] = {}

    async def start(self) -> None:
        await self._server.start()
        self._sip_port = self._server.port
        await self.rtp.start()

    async def stop(self) -> None:
        self.rtp.stop()
        await self._server.stop()

    @property
    def sip_port(self) -> int:
        return self._sip_port

    @property
    def received_sip(self) -> list[str]:
        return self._server.received

    def set_register_responder(
        self,
        *,
        realm: str = "pbx.example.test",
        expires: int = 120,
    ) -> None:
        """Install a REGISTER responder: 401 challenge first, then 200 OK."""
        from tests.e2e._fake_gateway import (  # noqa: PLC0415
            _register_challenge,
            _register_ok,
        )

        state = {"seen": 0}

        async def respond(request: SipRequest) -> list[str]:
            if request.method != "REGISTER":
                return []
            state["seen"] += 1
            if state["seen"] == 1:
                return [_register_challenge(request, realm=realm)]
            return [_register_ok(request, expires=expires)]

        self._register_responder = respond

    async def _respond(self, request: SipRequest) -> list[str]:
        """Handle any SIP request the plugin sends."""
        if request.method == "REGISTER" and self._register_responder is not None:
            return await self._register_responder(request)

        if request.method == "INVITE":
            return await self._handle_invite(request)

        if request.method == "ACK":
            await self._received_acks.put(request)
            return []

        if request.method == "BYE":
            await self._received_byes.put(request)
            return [self._build_200_ok_simple(request)]

        if request.method == "OPTIONS":
            return [self._build_options_ok(request)]

        return []

    async def _handle_invite(self, request: SipRequest) -> list[str]:
        await self._received_invites.put(request)
        call_id = request.header("Call-ID") or ""
        seen = self._invite_challenges.get(call_id, 0)
        self._invite_challenges[call_id] = seen + 1

        if seen == 0:
            # First INVITE: challenge with 407
            return [self._build_407(request)]

        # Re-INVITE with auth: send 100 Trying + 180 Ringing + 200 OK
        return [
            self._build_100(request),
            self._build_180(request),
            self._build_200_ok_with_sdp(request),
        ]

    def _build_407(self, req: SipRequest) -> str:
        """407 Proxy Authentication Required."""
        via = req.header("Via") or ""
        from_ = req.header("From") or ""
        to = req.header("To") or ""
        call_id = req.header("Call-ID") or ""
        cseq = req.header("CSeq") or ""
        return (
            "SIP/2.0 407 Proxy Authentication Required\r\n"
            f"Via: {via}\r\n"
            f"From: {from_}\r\n"
            f"To: {to};tag=srv-{new_tag()}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq}\r\n"
            f'Proxy-Authenticate: Digest realm="{self.sip_host}", '
            'nonce="obtest123", algorithm=MD5, qop="auth"\r\n'
            "Content-Length: 0\r\n\r\n"
        )

    def _build_100(self, req: SipRequest) -> str:
        via = req.header("Via") or ""
        from_ = req.header("From") or ""
        to = req.header("To") or ""
        call_id = req.header("Call-ID") or ""
        cseq = req.header("CSeq") or ""
        return (
            "SIP/2.0 100 Trying\r\n"
            f"Via: {via}\r\n"
            f"From: {from_}\r\n"
            f"To: {to}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq}\r\n"
            "Content-Length: 0\r\n\r\n"
        )

    def _build_180(self, req: SipRequest) -> str:
        via = req.header("Via") or ""
        from_ = req.header("From") or ""
        to = req.header("To") or ""
        call_id = req.header("Call-ID") or ""
        cseq = req.header("CSeq") or ""
        return (
            "SIP/2.0 180 Ringing\r\n"
            f"Via: {via}\r\n"
            f"From: {from_}\r\n"
            f"To: {to};tag=ringing-{new_tag()}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq}\r\n"
            "Content-Length: 0\r\n\r\n"
        )

    def _build_200_ok_with_sdp(self, req: SipRequest) -> str:
        """200 OK with SDP answer pointing to our RTP endpoint."""
        via = req.header("Via") or ""
        from_ = req.header("From") or ""
        to = req.header("To") or ""
        call_id = req.header("Call-ID") or ""
        cseq = req.header("CSeq") or ""
        answer_sdp = self._sdp_answer()
        body_bytes = answer_sdp.encode()
        return (
            "SIP/2.0 200 OK\r\n"
            f"Via: {via}\r\n"
            f"From: {from_}\r\n"
            f"To: {to};tag=ans-{new_tag()}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq}\r\n"
            f"Contact: <sip:{_TARGET_EXT}@127.0.0.1:{self.sip_port}"
            ";transport=tls>\r\n"
            "Content-Type: application/sdp\r\n"
            f"Content-Length: {len(body_bytes)}\r\n\r\n"
            f"{answer_sdp}"
        )

    def _build_200_ok_simple(self, req: SipRequest) -> str:
        """200 OK for BYE / non-SDP responses."""
        via = req.header("Via") or ""
        from_ = req.header("From") or ""
        to = req.header("To") or ""
        call_id = req.header("Call-ID") or ""
        cseq = req.header("CSeq") or ""
        return (
            "SIP/2.0 200 OK\r\n"
            f"Via: {via}\r\n"
            f"From: {from_}\r\n"
            f"To: {to}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq}\r\n"
            "Content-Length: 0\r\n\r\n"
        )

    def _build_options_ok(self, req: SipRequest) -> str:
        via = req.header("Via") or ""
        from_ = req.header("From") or ""
        to = req.header("To") or ""
        call_id = req.header("Call-ID") or ""
        cseq = req.header("CSeq") or ""
        return (
            "SIP/2.0 200 OK\r\n"
            f"Via: {via}\r\n"
            f"From: {from_}\r\n"
            f"To: {to}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq}\r\n"
            "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER, REFER, NOTIFY\r\n"
            "Content-Length: 0\r\n\r\n"
        )

    def _sdp_answer(self) -> str:
        """A G.711 RTP/AVP SDP answer pointing at our loopback RTP endpoint."""
        rtp_port = self.rtp.port
        return (
            "v=0\r\n"
            "o=- 8000 8000 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP 0 8 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=fmtp:101 0-16\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )

    async def await_invite_from_plugin(self, *, timeout: float = 5.0) -> SipRequest:
        """Wait for an INVITE from the plugin."""
        return await asyncio.wait_for(self._received_invites.get(), timeout)

    async def await_ack_from_plugin(self, *, timeout: float = 5.0) -> SipRequest:
        """Wait for an ACK from the plugin after our 200 OK."""
        return await asyncio.wait_for(self._received_acks.get(), timeout)


# ---------------------------------------------------------------------------
# Test wiring helpers
# ---------------------------------------------------------------------------

_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": _TO_USER,
    "HERMES_SIP_PASSWORD": "fake-password",
    "HERMES_SIP_EXPIRES": "120",
}


def _platform_config(extra: dict[str, str] | None = None) -> PlatformConfig:
    env = dict(_FAKE_ENV)
    if extra:
        env.update(extra)
    return PlatformConfig(enabled=True, extra=env)


async def _no_sleep(_seconds: float) -> None:
    """Instant sleep so the engine flushes outbound RTP without delay."""


@pytest.fixture(autouse=True)
def _register_voip_platform() -> None:
    if not platform_registry.is_registered("voip"):
        platform_registry.register(
            PlatformEntry(
                name="voip",
                label="VoIP",
                adapter_factory=lambda cfg: None,
                check_fn=lambda: True,
                validate_config=lambda cfg: True,
                required_env=[],
                install_hint="",
                source="plugin",
            )
        )


@asynccontextmanager
async def _real_adapter(
    gateway: OutboundGateway,
    *,
    providers: Providers,
    extra_env: dict[str, str] | None = None,
) -> AsyncIterator[object]:
    """Yield the real VoipAdapter wired to the outbound fake gateway."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    vad_model = _RecordingVadModel()

    def _transport_factory(**kwargs: object) -> SipOverTlsTransport:
        kwargs["ssl_context"] = client_ssl_context()
        kwargs["server_hostname"] = "pbx.example.test"
        kwargs["connect_address"] = "127.0.0.1"
        kwargs["port"] = gateway.sip_port
        return SipOverTlsTransport(**kwargs)  # type: ignore[arg-type]

    def _engine_factory(**kwargs: object) -> RtpMediaTransport:
        kwargs["sleep"] = _no_sleep
        return RtpMediaTransport(**kwargs)  # type: ignore[arg-type]

    with (
        patch("hermes_voip.adapter.build_providers", return_value=providers),
        patch("hermes_voip.adapter.load_silero_model", return_value=vad_model),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch(
            "hermes_voip.adapter.SipOverTlsTransport",
            side_effect=_transport_factory,
        ),
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            side_effect=_engine_factory,
        ),
    ):
        adapter = VoipAdapter(_platform_config(extra=extra_env))
        up = await adapter.connect()
        assert up is True, "adapter did not register"
        try:
            yield adapter
        finally:
            await adapter.disconnect()


async def _until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
    step: float = 0.005,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            msg = "condition not met within the timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(step)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_outbound_call_auth_challenge_then_200() -> None:
    """Plugin sends INVITE, gets 407 challenge, re-sends with auth, gets 200.

    Sequence:
    1. adapter.connect() -> registered.
    2. adapter.place_call(TARGET_EXT) -> plugin sends INVITE.
    3. Gateway challenges with 407 Proxy Auth Required.
    4. Plugin re-sends INVITE with Proxy-Authorization.
    5. Gateway sends 180 Ringing then 200 OK with SDP answer.
    6. Plugin sends ACK.
    7. Plugin sends greeting RTP (at least 1 packet from the greeting).
    8. Gateway sends BYE.
    9. Call tears down cleanly (no engine leak, no uncancelled tasks).
    """
    gateway = OutboundGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(
        asr=_FakeASR(),
        tts=_FakeTTS(),
        guard=_FakeGuard(),
    )

    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)

            # place_call drives the full UAC flow; it returns when CallLoop runs
            place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))

            # 1st INVITE received by gateway -> gateway sends 407
            first_invite = await gateway.await_invite_from_plugin(timeout=5.0)
            assert first_invite.method == "INVITE"
            call_id = first_invite.header("Call-ID")
            assert call_id is not None

            # 2nd INVITE (re-auth) received by gateway -> gateway sends 200
            second_invite = await gateway.await_invite_from_plugin(timeout=5.0)
            assert second_invite.method == "INVITE"
            assert second_invite.header("Proxy-Authorization") is not None
            # Same Call-ID on re-auth
            assert second_invite.header("Call-ID") == call_id

            # ACK from plugin
            ack = await gateway.await_ack_from_plugin(timeout=5.0)
            assert ack.method == "ACK"

            # place_call returns the call_id once CallLoop is running
            returned_call_id = await asyncio.wait_for(place_task, timeout=5.0)
            assert returned_call_id == call_id

            # Greeting RTP should flow to the gateway's RTP endpoint
            await gateway.rtp.wait_for_frames(1, timeout=3.0)
            assert len(gateway.rtp.received_packets) >= 1

            # Wait for call to be tracked
            await _until(lambda: call_id in adapter._call_sessions, timeout=5.0)

            session = adapter._call_sessions.get(call_id)
            assert session is not None

            # Send BYE from the gateway side (UAS BYE) using the dialog
            dialog = session.dialog
            bye = build_request(
                "BYE",
                f"sip:{_TO_USER}@127.0.0.1:{gateway.sip_port};transport=tls",
                [
                    (
                        "Via",
                        f"SIP/2.0/TLS 127.0.0.1:{gateway.sip_port}"
                        f";branch={new_branch()};rport",
                    ),
                    ("Max-Forwards", "70"),
                    # UAS BYE: From = remote (callee), To = local (caller plugin)
                    ("From", f"<{dialog.remote_uri}>;tag={dialog.remote_tag}"),
                    ("To", f"<{dialog.local_uri}>;tag={dialog.local_tag}"),
                    ("Call-ID", call_id),
                    ("CSeq", "2 BYE"),
                ],
            )
            await gateway._server.push(bye)

            # Wait for call to be cleaned up
            await _until(lambda: call_id not in adapter._call_sessions, timeout=5.0)
            assert call_id not in adapter._call_sessions
            assert call_id not in adapter._call_loops

    finally:
        await gateway.stop()


async def test_outbound_call_runs_unprivileged_with_callee_identity() -> None:
    """An established outbound call is OUTBOUND-TASK (ADR-0020 §3, amended).

    The callee is UNTRUSTED (operator mandate): the agent pursues only the
    operator's task with no privileged tool, so the call's GuardSessionState is
    ``privileged=False`` — the credit-card attack fails by construction on an
    outbound call exactly as it does inbound. The callee identity is recorded in
    ``_call_info`` so the agent knows who it called (fixes "I don't know you").
    """
    gateway = OutboundGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())

    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)

            place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))

            first_invite = await gateway.await_invite_from_plugin(timeout=5.0)
            call_id = first_invite.header("Call-ID")
            assert call_id is not None
            await gateway.await_invite_from_plugin(timeout=5.0)  # re-auth INVITE
            await gateway.await_ack_from_plugin(timeout=5.0)

            returned_call_id = await asyncio.wait_for(place_task, timeout=5.0)
            assert returned_call_id == call_id

            await _until(lambda: call_id in adapter._call_sessions, timeout=5.0)
            session = adapter._call_sessions.get(call_id)
            assert session is not None

            # The security spine: the outbound (untrusted-callee) session is NOT
            # privileged, so ELEVATED/IRREVERSIBLE tools are structurally blocked.
            assert session.guard.privileged is False

            # The agent knows the callee it dialled (not "unknown caller").
            from hermes_voip.caller_modes import CallerMode  # noqa: PLC0415

            info = adapter._call_info[call_id]
            assert info["name"] == _TARGET_EXT
            assert info["mode"] is CallerMode.OUTBOUND
    finally:
        await gateway.stop()


async def test_outbound_call_486_busy() -> None:
    """Plugin sends INVITE, gateway responds 486 Busy Here -> OutboundCallFailed.

    After the failure, the engine is stopped (no RTP socket leak).
    """

    class _BusyGateway(OutboundGateway):
        """Gateway that always rejects with 486 Busy Here (no auth challenge)."""

        async def _handle_invite(self, request: SipRequest) -> list[str]:
            await self._received_invites.put(request)
            via = request.header("Via") or ""
            from_ = request.header("From") or ""
            to = request.header("To") or ""
            call_id = request.header("Call-ID") or ""
            cseq = request.header("CSeq") or ""
            return [
                "SIP/2.0 486 Busy Here\r\n"
                f"Via: {via}\r\n"
                f"From: {from_}\r\n"
                f"To: {to};tag=busy\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq}\r\n"
                "Content-Length: 0\r\n\r\n"
            ]

    gateway = _BusyGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(
        asr=_FakeASR(),
        tts=_FakeTTS(),
        guard=_FakeGuard(),
    )

    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)

            with pytest.raises(OutboundCallFailed) as exc_info:
                await adapter.place_call(_TARGET_EXT)

            assert exc_info.value.status == 486
            assert "486" in str(exc_info.value)

            # Give a moment for async cleanup
            await asyncio.sleep(0.05)
            # No call should be tracked after failure
            assert not adapter._call_sessions
            assert not adapter._call_loops

    finally:
        await gateway.stop()


async def test_outbound_engine_uses_negotiated_codec_pcma() -> None:
    """When the 2xx answer selects PCMA, the engine must use PCMA(8), not PCMU(0).

    RED test (TDD): before the fix the engine is always initialised with PCMU
    and the codec is never updated from the negotiated SDP answer, so this test
    fails on the unfixed code.
    """

    class _PcmaOnlyGateway(OutboundGateway):
        """Gateway that answers with PCMA-only SDP (no PCMU in the answer)."""

        def _sdp_answer(self) -> str:
            rtp_port = self.rtp.port
            return (
                "v=0\r\n"
                "o=- 8000 8000 IN IP4 127.0.0.1\r\n"
                "s=-\r\n"
                "c=IN IP4 127.0.0.1\r\n"
                "t=0 0\r\n"
                f"m=audio {rtp_port} RTP/AVP 8 101\r\n"
                "a=rtpmap:8 PCMA/8000\r\n"
                "a=rtpmap:101 telephone-event/8000\r\n"
                "a=fmtp:101 0-16\r\n"
                "a=ptime:20\r\n"
                "a=sendrecv\r\n"
            )

    from hermes_voip.media.engine import Codec  # noqa: PLC0415

    gateway = _PcmaOnlyGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(
        asr=_FakeASR(),
        tts=_FakeTTS(),
        guard=_FakeGuard(),
    )

    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)

            place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))

            # Consume both INVITEs (challenge + re-auth)
            await gateway.await_invite_from_plugin(timeout=5.0)
            await gateway.await_invite_from_plugin(timeout=5.0)

            # Wait for ACK (confirms 200 OK processed)
            await gateway.await_ack_from_plugin(timeout=5.0)

            returned_call_id = await asyncio.wait_for(place_task, timeout=5.0)

            # Engine must have been updated to PCMA (payload type 8), not PCMU (0).
            await _until(
                lambda: returned_call_id in adapter._call_sessions, timeout=5.0
            )
            session = adapter._call_sessions[returned_call_id]
            engine = session._media
            assert isinstance(engine, RtpMediaTransport), (
                f"expected RtpMediaTransport, got {type(engine)}"
            )
            assert engine._codec is Codec.PCMA, (
                f"engine codec is {engine._codec!r} (payload type "
                f"{engine._codec.value}); expected Codec.PCMA (payload type 8). "
                "The outbound engine was not updated after codec negotiation."
            )

            # Tear down the call cleanly.
            dialog = session.dialog
            bye = build_request(
                "BYE",
                f"sip:{_TO_USER}@127.0.0.1:{gateway.sip_port};transport=tls",
                [
                    (
                        "Via",
                        f"SIP/2.0/TLS 127.0.0.1:{gateway.sip_port}"
                        f";branch={new_branch()};rport",
                    ),
                    ("Max-Forwards", "70"),
                    ("From", f"<{dialog.remote_uri}>;tag={dialog.remote_tag}"),
                    ("To", f"<{dialog.local_uri}>;tag={dialog.local_tag}"),
                    ("Call-ID", returned_call_id),
                    ("CSeq", "2 BYE"),
                ],
            )
            await gateway._server.push(bye)
            await _until(
                lambda: returned_call_id not in adapter._call_sessions, timeout=5.0
            )
    finally:
        await gateway.stop()


async def test_call_on_connect_trigger() -> None:
    """HERMES_VOIP_CALL_ON_CONNECT fires place_call once after registration.

    The _call_on_connect_fired flag prevents re-firing on reconnect.
    """

    class _DirectOkGateway(OutboundGateway):
        """Gateway that answers INVITE immediately with 200 OK (no challenge)."""

        async def _handle_invite(self, request: SipRequest) -> list[str]:
            await self._received_invites.put(request)
            return [
                self._build_100(request),
                self._build_200_ok_with_sdp(request),
            ]

    gateway = _DirectOkGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(
        asr=_FakeASR(),
        tts=_FakeTTS(),
        guard=_FakeGuard(),
    )

    try:
        extra = {"HERMES_VOIP_CALL_ON_CONNECT": _TARGET_EXT}
        async with _real_adapter(
            gateway, providers=providers, extra_env=extra
        ) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)

            # adapter.connect() should have fired place_call after registration.
            invite = await gateway.await_invite_from_plugin(timeout=5.0)
            assert invite.method == "INVITE"

            # _call_on_connect_fired prevents re-firing on reconnect
            assert adapter._call_on_connect_fired is True

            # Wait for ACK
            ack = await gateway.await_ack_from_plugin(timeout=5.0)
            assert ack.method == "ACK"

            call_id = invite.header("Call-ID") or ""
            await _until(lambda: call_id in adapter._call_sessions, timeout=5.0)

            # Clean up: send BYE to end the call
            session = adapter._call_sessions.get(call_id)
            if session is not None:
                dialog = session.dialog
                bye = build_request(
                    "BYE",
                    f"sip:{_TO_USER}@127.0.0.1:{gateway.sip_port};transport=tls",
                    [
                        (
                            "Via",
                            f"SIP/2.0/TLS 127.0.0.1:{gateway.sip_port}"
                            f";branch={new_branch()};rport",
                        ),
                        ("Max-Forwards", "70"),
                        ("From", f"<{dialog.remote_uri}>;tag={dialog.remote_tag}"),
                        ("To", f"<{dialog.local_uri}>;tag={dialog.local_tag}"),
                        ("Call-ID", call_id),
                        ("CSeq", "2 BYE"),
                    ],
                )
                await gateway._server.push(bye)
                await _until(lambda: call_id not in adapter._call_sessions, timeout=5.0)

    finally:
        await gateway.stop()


# ---------------------------------------------------------------------------
# ADR-0066: outbound SDES-SRTP offering on place_call.
#
# Today the outbound INVITE offers PLAIN RTP/AVP (srtp_*=None + build_audio_offer
# with no crypto=), while the inbound answer path negotiates SDES — an asymmetry.
# These RED tests assert the opt-in offer (HERMES_VOIP_SIP_SDES_OFFER) makes the
# outbound INVITE offer RTP/SAVP + a=crypto, that a secured answer brings the engine
# up secured (RFC 4568 §6.1 sender-keying), and that a PLAIN answer to our SAVP offer
# FAILS the call (fail-closed — never a silent plaintext downgrade).
#
# The fake SDES key the gateway answers with is computed at runtime (sequential bytes
# 0..29 → base64) so no high-entropy key literal appears in this file (the gitleaks
# allowlist is path-scoped to tests/test_sdp.py).
# ---------------------------------------------------------------------------

_GATEWAY_ANSWER_SDES_KEY = base64.b64encode(bytes(range(30))).decode("ascii")


def _crypto_line_of(invite: SipRequest) -> str | None:
    """Return the single ``a=crypto:`` line body of an INVITE SDP offer, or None."""
    for line in (invite.body or "").splitlines():
        if line.startswith("a=crypto:"):
            return line[len("a=crypto:") :]
    return None


class _SrtpAnsweringGateway(OutboundGateway):
    """Gateway that answers the plugin's INVITE with an SDES ``RTP/SAVP`` SDP.

    Echoes the offered crypto's tag/suite but with the gateway's OWN key (RFC 4568
    §6.1 — each side keys with its own key). The plugin must encrypt outbound with
    its own offer key and decrypt inbound with THIS answer key.
    """

    def _sdp_answer(self) -> str:
        rtp_port = self.rtp.port
        return (
            "v=0\r\n"
            "o=- 8000 8000 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            f"m=audio {rtp_port} RTP/SAVP 0 8 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=fmtp:101 0-16\r\n"
            f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_GATEWAY_ANSWER_SDES_KEY}\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )


async def test_outbound_offer_is_plain_rtp_avp_by_default() -> None:
    """Default (flag unset): the outbound INVITE offers PLAIN RTP/AVP, no a=crypto.

    Regression guard for the safe default — turning SDES offering ON is opt-in
    (ADR-0066), so an unconfigured deployment keeps offering cleartext exactly as
    today (the live-validated default). This already passes on main; it locks the
    default so the GREEN change cannot silently flip it.
    """
    gateway = OutboundGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)
            place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))

            first_invite = await gateway.await_invite_from_plugin(timeout=5.0)
            assert "m=audio" in (first_invite.body or "")
            assert "RTP/AVP" in (first_invite.body or ""), (
                f"default offer is not plain RTP/AVP; body:\n{first_invite.body}"
            )
            assert "RTP/SAVP" not in (first_invite.body or ""), (
                f"default offer wrongly advertises SAVP; body:\n{first_invite.body}"
            )
            assert _crypto_line_of(first_invite) is None, (
                f"default offer wrongly carries a=crypto; body:\n{first_invite.body}"
            )

            # Drain the flow so the adapter tears down cleanly.
            await gateway.await_invite_from_plugin(timeout=5.0)  # re-auth INVITE
            await gateway.await_ack_from_plugin(timeout=5.0)
            call_id = await asyncio.wait_for(place_task, timeout=5.0)
            await _until(lambda: call_id in adapter._call_sessions, timeout=5.0)
    finally:
        await gateway.stop()


async def test_outbound_sdes_offer_advertises_savp_and_crypto() -> None:
    """Flag ON: the outbound INVITE offers RTP/SAVP with exactly one a=crypto.

    RED on main (the offer is always plain RTP/AVP — build_audio_offer is called with
    no crypto=). After ADR-0066 the opt-in flag makes the INVITE offer SDES-SRTP with
    a fresh per-call AES_CM_128_HMAC_SHA1_80 key.
    """
    gateway = _SrtpAnsweringGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    extra = {"HERMES_VOIP_SIP_SDES_OFFER": "true"}
    try:
        async with _real_adapter(
            gateway, providers=providers, extra_env=extra
        ) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)
            place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))

            first_invite = await gateway.await_invite_from_plugin(timeout=5.0)
            body = first_invite.body or ""
            assert "m=audio" in body, f"offer has no audio m-line; body:\n{body}"
            assert "RTP/SAVP" in body, (
                f"opt-in outbound offer is not RTP/SAVP; body:\n{body}"
            )
            assert body.count("a=crypto:") == 1, (
                f"expected exactly one a=crypto in the offer; body:\n{body}"
            )
            crypto = _crypto_line_of(first_invite)
            assert crypto is not None
            assert "AES_CM_128_HMAC_SHA1_80 inline:" in crypto, (
                f"offer crypto is not the preferred suite; line: {crypto}"
            )
            # Our offer key must be OUR OWN, never the gateway's answer key.
            assert _GATEWAY_ANSWER_SDES_KEY not in crypto, (
                "outbound offer leaked the gateway's answer key into our offer"
            )

            # The re-auth re-send MUST carry the SAME offer body (same a=crypto), so
            # the key we keyed the engine with matches what the gateway sees.
            second_invite = await gateway.await_invite_from_plugin(timeout=5.0)
            assert _crypto_line_of(second_invite) == crypto, (
                "re-auth INVITE offered a different a=crypto than the first INVITE"
            )

            await gateway.await_ack_from_plugin(timeout=5.0)
            call_id = await asyncio.wait_for(place_task, timeout=5.0)
            await _until(lambda: call_id in adapter._call_sessions, timeout=5.0)
    finally:
        await gateway.stop()


async def test_outbound_sdes_secured_answer_brings_engine_up_secured() -> None:
    """Flag ON + SRTP answer: the engine comes up SECURED in BOTH directions.

    RFC 4568 §6.1: we encrypt outbound with our offer key and decrypt inbound with the
    gateway's answer key, so both _srtp_out and _srtp_in are set. RED on main (no
    a=crypto offered, the 2xx a=crypto is ignored, the engine stays cleartext).
    """
    gateway = _SrtpAnsweringGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    extra = {"HERMES_VOIP_SIP_SDES_OFFER": "true"}
    try:
        async with _real_adapter(
            gateway, providers=providers, extra_env=extra
        ) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)
            place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))

            await gateway.await_invite_from_plugin(timeout=5.0)  # challenge
            await gateway.await_invite_from_plugin(timeout=5.0)  # re-auth
            await gateway.await_ack_from_plugin(timeout=5.0)
            call_id = await asyncio.wait_for(place_task, timeout=5.0)

            await _until(lambda: call_id in adapter._call_sessions, timeout=5.0)
            session = adapter._call_sessions[call_id]
            engine = session._media
            assert isinstance(engine, RtpMediaTransport)
            # SECURED both ways: outbound encrypt + inbound decrypt SRTP sessions.
            assert engine._srtp_out is not None, (
                "outbound SRTP not keyed — the call streams cleartext despite the "
                "SAVP offer/answer"
            )
            assert engine._srtp_in is not None, (
                "inbound SRTP not keyed — we cannot decrypt the gateway's media"
            )
    finally:
        await gateway.stop()


async def test_outbound_sdes_plain_answer_fails_closed() -> None:
    """Flag ON + the peer answers PLAIN RTP/AVP to our SAVP offer → the call FAILS.

    Fail-closed (ADR-0066): accepting a cleartext answer to an encrypted offer would
    silently stream plaintext for a call we asked to protect — a bid-down. The call
    must raise OutboundCallFailed and leave NO secured/cleartext call running. RED on
    main (the answer's profile/crypto is ignored, so a plain answer is accepted and the
    engine streams cleartext).
    """
    # The base OutboundGateway answers plain RTP/AVP; with the flag on we OFFERED SAVP,
    # so this plain answer is the downgrade case.
    gateway = OutboundGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    extra = {"HERMES_VOIP_SIP_SDES_OFFER": "true"}
    try:
        async with _real_adapter(
            gateway, providers=providers, extra_env=extra
        ) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)

            with pytest.raises(OutboundCallFailed) as exc_info:
                await adapter.place_call(_TARGET_EXT)

            # 488 Not Acceptable Here — we cannot accept a plaintext answer to a
            # secured offer (mirrors the codec-mismatch 488 in the same handler).
            assert exc_info.value.status == 488

            # No call may be running — neither secured nor (worse) cleartext.
            await asyncio.sleep(0.05)
            assert not adapter._call_sessions, (
                "a downgraded (plaintext) call was left running after the SAVP offer "
                "was answered plain"
            )
            assert not adapter._call_loops
    finally:
        await gateway.stop()
