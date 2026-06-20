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
import logging
import re
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
from hermes_voip.sdp import CryptoAttribute
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

    async def await_bye_from_plugin(self, *, timeout: float = 5.0) -> SipRequest:
        """Wait for an in-dialog BYE from the plugin (e.g. a refused-2xx teardown)."""
        return await asyncio.wait_for(self._received_byes.get(), timeout)


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
# ADR-0067: outbound SDES-SRTP offering on place_call.
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
# A SECOND distinct fake key (bytes 30..59) for the multiple-a=crypto answer; built
# at runtime so no high-entropy literal sits in this file (gitleaks path-scoping).
_GATEWAY_ANSWER_SDES_KEY_2 = base64.b64encode(bytes(range(30, 60))).decode("ascii")

# Every fake inline key||salt that appears in an SDP in this file. A failure-assertion
# message must NEVER echo one (rule 34: the repo is PUBLIC and a CI failure log is too),
# so _redact_sdp scrubs them; this list keeps the redactor in lockstep with the keys.
_FAKE_SDES_KEYS = (_GATEWAY_ANSWER_SDES_KEY, _GATEWAY_ANSWER_SDES_KEY_2)


def _redact_sdp(text: str | None) -> str:
    """Redact SDES inline key material from an SDP before it enters a log/assert msg.

    Rule 34: an ``a=crypto`` line carries the inline master key||salt, which must never
    reach a log line — including a CI **failure** message that echoes the body. Replaces
    each known fake key with ``<redacted>`` AND, defensively, any ``inline:`` token (so
    an unexpected key the plugin generated is scrubbed too). Used in every assertion
    message in this file that references an SDP body or crypto line.
    """
    redacted = text or ""
    for key in _FAKE_SDES_KEYS:
        redacted = redacted.replace(key, "<redacted>")
    # Defensive catch-all: scrub any remaining inline:<...> token (to end-of-token).
    return re.sub(r"inline:[^\s|]+", "inline:<redacted>", redacted)


def _crypto_lines_of(invite: SipRequest) -> list[str]:
    """Return all ``a=crypto:`` line bodies of an INVITE/answer SDP, in order."""
    return [
        line[len("a=crypto:") :]
        for line in (invite.body or "").splitlines()
        if line.startswith("a=crypto:")
    ]


def _crypto_line_of(invite: SipRequest) -> str | None:
    """Return the single ``a=crypto:`` line body of an INVITE SDP offer, or None."""
    lines = _crypto_lines_of(invite)
    return lines[0] if lines else None


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


class _SrtpWrongTagGateway(_SrtpAnsweringGateway):
    """Answers RTP/SAVP but with a DIFFERENT crypto tag than the offer (tag 9).

    RFC 4568 §6.1: the answerer MUST select by the offered tag. A tag the plugin
    never offered must be rejected, not keyed.
    """

    def _sdp_answer(self) -> str:
        return (
            super()
            ._sdp_answer()
            .replace(
                f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_GATEWAY_ANSWER_SDES_KEY}",
                f"a=crypto:9 AES_CM_128_HMAC_SHA1_80 inline:{_GATEWAY_ANSWER_SDES_KEY}",
            )
        )


class _SrtpWrongSuiteGateway(_SrtpAnsweringGateway):
    """Answers RTP/SAVP with the offered tag but a DIFFERENT suite (32-bit).

    The plugin offered only AES_CM_128_HMAC_SHA1_80; a 32-bit-suite answer was not
    offered and must be rejected (RFC 4568 §6.1).
    """

    def _sdp_answer(self) -> str:
        return (
            super()
            ._sdp_answer()
            .replace(
                f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_GATEWAY_ANSWER_SDES_KEY}",
                f"a=crypto:1 AES_CM_128_HMAC_SHA1_32 inline:{_GATEWAY_ANSWER_SDES_KEY}",
            )
        )


class _SrtpMultipleCryptoGateway(_SrtpAnsweringGateway):
    """Answers RTP/SAVP with MULTIPLE a=crypto lines (RFC 4568 §5.1.2 violation).

    An SDP *answer* must carry exactly one a=crypto (the selected one). An ambiguous
    multi-line answer must be rejected, not silently keyed from the first line.
    """

    def _sdp_answer(self) -> str:
        base = super()._sdp_answer()
        # A SECOND a=crypto (tag 2, distinct key) inserted after the first — an answer
        # must select exactly one (RFC 4568 §5.1.2), so this ambiguous pair is invalid.
        extra = (
            "a=crypto:2 AES_CM_128_HMAC_SHA1_80 "
            f"inline:{_GATEWAY_ANSWER_SDES_KEY_2}\r\n"
        )
        first = (
            f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_GATEWAY_ANSWER_SDES_KEY}\r\n"
        )
        return base.replace(first, first + extra)


class _NoCommonCodecGateway(OutboundGateway):
    """Answers a 2xx whose only codec (G.729, PT 18) the plugin does not support.

    A codec-negotiation failure on a 2xx is a refused answer just like a downgraded
    SRTP answer: the 2xx established the dialog, so it MUST be ACKed then BYE'd (RFC
    3261 §13.2.2.4 + §15) — never raised un-ACKed (half-open). Plain RTP/AVP profile,
    so this is independent of the SDES flag.
    """

    def _sdp_answer(self) -> str:
        rtp_port = self.rtp.port
        return (
            "v=0\r\n"
            "o=- 8000 8000 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP 18\r\n"
            "a=rtpmap:18 G729/8000\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )


async def test_outbound_codec_mismatch_2xx_acks_then_byes_no_half_open() -> None:
    """A 2xx answering an unsupported codec is ACKed THEN BYE'd, not left half-open.

    The pre-existing codec-mismatch 488 paths (no-common-codec / no-voice-codec /
    not-carriable / unparseable-SDP) used to raise BEFORE ACKing the 2xx — the same
    half-open-dialog shape as the SRTP downgrade. ADR-0067 routes them through the
    same ACK+BYE teardown. RED on the prior cut (no BYE — await_bye_from_plugin
    times out). Flag OFF: this is a pure codec rejection, independent of SDES.
    """
    gateway = _NoCommonCodecGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)
            place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))

            await gateway.await_invite_from_plugin(timeout=5.0)  # challenge
            await gateway.await_invite_from_plugin(timeout=5.0)  # re-auth

            # The refused 2xx is still ACKed (TU owns the 2xx ACK), THEN BYE'd.
            ack = await gateway.await_ack_from_plugin(timeout=5.0)
            assert ack.method == "ACK"
            bye = await gateway.await_bye_from_plugin(timeout=5.0)
            assert bye.method == "BYE"

            with pytest.raises(OutboundCallFailed) as exc_info:
                await asyncio.wait_for(place_task, timeout=5.0)
            assert exc_info.value.status == 488

            await asyncio.sleep(0.05)
            assert not adapter._call_sessions
            assert not adapter._call_loops
    finally:
        await gateway.stop()


async def test_outbound_offer_is_plain_rtp_avp_by_default() -> None:
    """Default (flag unset): the outbound INVITE offers PLAIN RTP/AVP, no a=crypto.

    Regression guard for the safe default — turning SDES offering ON is opt-in
    (ADR-0067), so an unconfigured deployment keeps offering cleartext exactly as
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
            body = first_invite.body or ""
            assert "m=audio" in body
            # SDP appears in failure messages only via _redact_sdp (rule 34): a plain
            # offer has no key, but the redactor keeps every SDP message uniformly safe.
            assert "RTP/AVP" in body, (
                f"default offer is not plain RTP/AVP; body:\n{_redact_sdp(body)}"
            )
            assert "RTP/SAVP" not in body, (
                f"default offer wrongly advertises SAVP; body:\n{_redact_sdp(body)}"
            )
            assert _crypto_line_of(first_invite) is None, (
                f"default offer wrongly carries a=crypto; body:\n{_redact_sdp(body)}"
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
    no crypto=). After ADR-0067 the opt-in flag makes the INVITE offer SDES-SRTP with
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
            # SDP enters failure messages only via _redact_sdp (rule 34): this offer
            # carries our per-call inline key, which must never reach a CI log.
            assert "m=audio" in body, (
                f"offer has no audio m-line; body:\n{_redact_sdp(body)}"
            )
            assert "RTP/SAVP" in body, (
                f"opt-in outbound offer is not RTP/SAVP; body:\n{_redact_sdp(body)}"
            )
            assert body.count("a=crypto:") == 1, (
                f"expected one a=crypto in the offer; body:\n{_redact_sdp(body)}"
            )
            crypto = _crypto_line_of(first_invite)
            assert crypto is not None
            # Assert the suite token + that the line carries an inline key, WITHOUT
            # echoing the key (rule 34 — a failure message is a public log too).
            assert crypto.startswith("1 AES_CM_128_HMAC_SHA1_80 inline:"), (
                f"offer crypto is not the preferred suite; line: {_redact_sdp(crypto)}"
            )
            # Our offer key must be OUR OWN, never the gateway's answer key.
            assert _GATEWAY_ANSWER_SDES_KEY not in crypto, (
                "outbound offer leaked the gateway's answer key into our offer"
            )

            # The re-auth re-send MUST carry the SAME offer body (same a=crypto), so
            # the key we keyed the engine with matches what the gateway sees. Compare
            # by identity; never echo either line in the failure message.
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


async def _assert_refused_2xx_acks_then_byes_then_fails(
    gateway: OutboundGateway,
) -> None:
    """Assert the refused-2xx teardown for a place_call against ``gateway`` (flag ON).

    The UAC ACKs the 2xx, then BYEs the dialog (RFC 3261 §13.2.2.4 + §15), raises
    OutboundCallFailed, and leaves no running call/engine.

    Shared by every fail-closed case (plain answer, wrong tag, wrong suite, multiple
    a=crypto). A 2xx MUST be ACKed by the UAC even when we then reject it — leaving it
    un-ACKed produces a half-open remote-established dialog + retransmits (ADR-0065).
    """
    async with _real_adapter(
        gateway,
        providers=Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard()),
        extra_env={"HERMES_VOIP_SIP_SDES_OFFER": "true"},
    ) as adapter:
        from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

        assert isinstance(adapter, VoipAdapter)
        place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))

        await gateway.await_invite_from_plugin(timeout=5.0)  # challenge
        await gateway.await_invite_from_plugin(timeout=5.0)  # re-auth

        # The refused 2xx is still ACKed (TU owns the 2xx ACK), THEN BYE'd to tear
        # down the remote-established dialog — never left un-ACKed (half-open).
        ack = await gateway.await_ack_from_plugin(timeout=5.0)
        assert ack.method == "ACK"
        bye = await gateway.await_bye_from_plugin(timeout=5.0)
        assert bye.method == "BYE"

        with pytest.raises(OutboundCallFailed) as exc_info:
            await asyncio.wait_for(place_task, timeout=5.0)
        # 488 — we cannot accept this answer for a secured offer (mirrors the
        # codec-mismatch 488 in the same handler). Message is structural, never the key.
        assert exc_info.value.status == 488

        # No call may be running — neither secured nor (worse) cleartext — and the
        # media socket is released (no engine left behind).
        await asyncio.sleep(0.05)
        assert not adapter._call_sessions, (
            "a refused-2xx call was left running (possible silent downgrade)"
        )
        assert not adapter._call_loops


class _SrtpUnsupportedCodecGateway(_SrtpAnsweringGateway):
    """Answers RTP/SAVP with a matching a=crypto but ONLY an unsupported codec.

    The crypto is fine (tag 1 / 80-bit, as offered), but the m=audio lists only an
    unknown payload type, so codec negotiation fails (488). codex r2: that post-2xx
    failure must ACK + BYE the dialog (established once we ACK), not leave it
    half-open — the same teardown as the crypto-rejection paths.
    """

    def _sdp_answer(self) -> str:
        rtp_port = self.rtp.port
        return (
            "v=0\r\n"
            "o=- 8000 8000 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            f"m=audio {rtp_port} RTP/SAVP 99\r\n"
            "a=rtpmap:99 EXOTIC/8000\r\n"
            f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_GATEWAY_ANSWER_SDES_KEY}\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )


class _SrtpMatchingPlusMalformedCryptoGateway(_SrtpAnsweringGateway):
    """Answers RTP/SAVP with the matching crypto PLUS a malformed extra a=crypto.

    The extra line's key-params do not start with ``inline:``, so it is dropped from the
    parse-filtered ``crypto_attrs`` — leaving exactly one *parsed* crypto. codex r2:
    counting only the filtered list wrongly accepts this; the raw count is two, which an
    answer must never carry (RFC 4568 §5.1.2). The validator counts RAW ``a=crypto``
    lines and fails closed.
    """

    def _sdp_answer(self) -> str:
        first = (
            f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_GATEWAY_ANSWER_SDES_KEY}\r\n"
        )
        # A second, MALFORMED a=crypto (non-inline key-params → dropped by the parser).
        extra = "a=crypto:2 AES_CM_128_HMAC_SHA1_80 notinline:deadbeef\r\n"
        return super()._sdp_answer().replace(first, first + extra)


async def test_outbound_sdes_no_common_codec_acks_and_byes() -> None:
    """Flag ON + a 2xx with no common codec → ACK+BYE+fail (codec path, codex r2).

    The crypto matches, but codec negotiation fails (the answer offers only an unknown
    payload). This is a post-2xx rejection like the crypto cases, so it MUST ACK then
    BYE the dialog — never leave it half-open. RED before the codec-path teardown (the
    488 was raised before the ACK was sent).
    """
    gateway = _SrtpUnsupportedCodecGateway()
    gateway.set_register_responder()
    await gateway.start()
    try:
        await _assert_refused_2xx_acks_then_byes_then_fails(gateway)
    finally:
        await gateway.stop()


async def test_outbound_sdes_matching_plus_malformed_crypto_fails_closed() -> None:
    """Flag ON + matching crypto PLUS a malformed extra a=crypto → fail closed.

    RFC 4568 §5.1.2: an answer carries exactly one a=crypto. A matching line plus a
    malformed extra has raw count two but filters to one; counting only the filtered
    subset wrongly accepts it (codex r2). The validator counts RAW a=crypto lines and
    fails closed (ACK+BYE). RED before the raw-count fix.
    """
    gateway = _SrtpMatchingPlusMalformedCryptoGateway()
    gateway.set_register_responder()
    await gateway.start()
    try:
        await _assert_refused_2xx_acks_then_byes_then_fails(gateway)
    finally:
        await gateway.stop()


async def test_outbound_sdes_plain_answer_fails_closed() -> None:
    """Flag ON + the peer answers PLAIN RTP/AVP to our SAVP offer → ACK+BYE+fail.

    Fail-closed (ADR-0067): accepting a cleartext answer to an encrypted offer would
    silently stream plaintext for a call we asked to protect — a bid-down. The refused
    2xx is ACKed then BYE'd (no half-open dialog), the call raises, and NO call runs.
    RED on main (the answer's profile/crypto is ignored, so a plain answer is accepted).
    """
    # The base OutboundGateway answers plain RTP/AVP; with the flag on we OFFERED SAVP,
    # so this plain answer is the downgrade case.
    gateway = OutboundGateway()
    gateway.set_register_responder()
    await gateway.start()
    try:
        await _assert_refused_2xx_acks_then_byes_then_fails(gateway)
    finally:
        await gateway.stop()


async def test_outbound_sdes_answer_wrong_tag_fails_closed() -> None:
    """Flag ON + the SAVP answer uses a crypto tag we never offered → ACK+BYE+fail.

    RFC 4568 §6.1: the answerer selects by the offered tag. A tag the plugin never
    offered must not key inbound SRTP — fail closed (ACK+BYE), never key from it.
    RED on main and on the first ADR-0067 cut (which took crypto_attrs[0] blindly).
    """
    gateway = _SrtpWrongTagGateway()
    gateway.set_register_responder()
    await gateway.start()
    try:
        await _assert_refused_2xx_acks_then_byes_then_fails(gateway)
    finally:
        await gateway.stop()


async def test_outbound_sdes_answer_wrong_suite_fails_closed() -> None:
    """Flag ON + the SAVP answer uses a suite we never offered → ACK+BYE+fail.

    We offered only AES_CM_128_HMAC_SHA1_80; an answer selecting the 32-bit suite was
    never offered (RFC 4568 §6.1) and must be rejected, not keyed.
    """
    gateway = _SrtpWrongSuiteGateway()
    gateway.set_register_responder()
    await gateway.start()
    try:
        await _assert_refused_2xx_acks_then_byes_then_fails(gateway)
    finally:
        await gateway.stop()


async def test_outbound_sdes_answer_multiple_crypto_fails_closed() -> None:
    """Flag ON + the SAVP answer carries MULTIPLE a=crypto lines → ACK+BYE+fail.

    An SDP answer must select exactly one crypto (RFC 4568 §5.1.2). An ambiguous
    multi-line answer must be rejected — never silently keyed from the first line.
    """
    gateway = _SrtpMultipleCryptoGateway()
    gateway.set_register_responder()
    await gateway.start()
    try:
        await _assert_refused_2xx_acks_then_byes_then_fails(gateway)
    finally:
        await gateway.stop()


async def test_outbound_sdes_offer_key_is_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 34 (repo is PUBLIC): the outbound SDES master key never reaches a log.

    Patches the offer-crypto generator to a KNOWN runtime-computed key, drives a full
    secured outbound call, captures EVERY log record at DEBUG across the whole logger
    tree, and asserts the inline base64 key||salt appears in no message (and no record
    repr — the CryptoAttribute key_params is field(repr=False), so a logged object must
    not expose it either). The key is built at runtime (base64 of bytes 1..30) so no
    high-entropy literal sits in this file for gitleaks.
    """
    # A KNOWN per-call offer key (30 octets, distinct from the gateway's 0..29 answer
    # key) computed at runtime — never a source literal (gitleaks path-scoping).
    known_key_b64 = base64.b64encode(bytes(range(1, 31))).decode("ascii")
    known_offer_crypto = CryptoAttribute(
        tag=1,
        suite="AES_CM_128_HMAC_SHA1_80",
        key_params=f"inline:{known_key_b64}",
    )

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

            # Capture the WHOLE logger tree at DEBUG (the key must not leak from ANY
            # logger — adapter, sdp, media, transport), and pin the offer key to the
            # known value so we know exactly what must be absent.
            with (
                caplog.at_level(logging.DEBUG),
                patch(
                    "hermes_voip.adapter._outbound_offer_crypto",
                    return_value=known_offer_crypto,
                ),
            ):
                place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))
                await gateway.await_invite_from_plugin(timeout=5.0)  # challenge
                await gateway.await_invite_from_plugin(timeout=5.0)  # re-auth
                await gateway.await_ack_from_plugin(timeout=5.0)
                call_id = await asyncio.wait_for(place_task, timeout=5.0)
                await _until(lambda: call_id in adapter._call_sessions, timeout=5.0)

            # Sanity: the engine really came up secured (so the key WAS in play and a
            # leak was possible) — otherwise this test would pass vacuously.
            session = adapter._call_sessions[call_id]
            engine = session._media
            assert isinstance(engine, RtpMediaTransport)
            assert engine._srtp_out is not None
            assert engine._srtp_in is not None

            # The inline key||salt must appear in NO log record — neither in the
            # formatted message nor in any record's repr/args.
            for record in caplog.records:
                assert known_key_b64 not in record.getMessage(), (
                    f"SDES offer key leaked into a log message ({record.name} "
                    f"{record.levelname}): {record.getMessage()!r}"
                )
                assert known_key_b64 not in repr(record.args), (
                    f"SDES offer key leaked via log args ({record.name})"
                )
    finally:
        await gateway.stop()
