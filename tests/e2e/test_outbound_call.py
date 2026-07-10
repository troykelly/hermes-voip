"""End-to-end outbound call integration test (ADR-0019, RED phase).

Tests the full outbound UAC flow:
- Plugin sends INVITE
- Gateway challenges with 407
- Plugin re-sends with Proxy-Authorization
- Gateway sends 180 Ringing then 200 OK with SDP answer
- Plugin sends ACK
- No inbound greeting RTP flows (ADR-0019: the agent's first turn opens the call)
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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
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
from hermes_voip.originate import OutboundCallCancelled, OutboundCallFailed
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.tts import TtsStream
from hermes_voip.sdp import AudioMedia, Codec, CryptoAttribute, negotiate_audio
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
# A DISTINCTIVE fake dialled number for the redaction assertions (issue #1294): long
# and unlike any port / hex Call-ID, so "this string is absent from the SLO event
# records" cannot pass by coincidence. It embeds nothing real (555-01xx is the
# reserved fictional range); the fake gateway does not validate the dialled target.
_LEAK_PROBE_TARGET = "15550123456"
_ADAPTER_LOGGER = "hermes_voip.adapter"

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
# Structured outbound-lifecycle log-event helpers (issue #1294 / ADR-0075).
# The adapter emits ``outbound_invite_sent`` / ``outbound_call_connected`` /
# ``outbound_call_failed`` records carrying only ``call_id`` + fixed non-sensitive
# context; a log pipeline keys on the ``event`` discriminator to compute the
# outbound-call SLO. These helpers read the captured records without tripping mypy
# on the dynamic ``extra`` attributes (no ``# type: ignore`` needed — getattr).
# ---------------------------------------------------------------------------


def _records_with_event(
    caplog: pytest.LogCaptureFixture, event: str
) -> list[logging.LogRecord]:
    """All captured records whose structured ``event`` field equals ``event``."""
    return [rec for rec in caplog.records if getattr(rec, "event", None) == event]


def _one_event_record(
    caplog: pytest.LogCaptureFixture, event: str
) -> logging.LogRecord:
    """The single captured record with ``event``; fails loudly if 0 or >1 seen."""
    matches = _records_with_event(caplog, event)
    seen = [getattr(rec, "event", None) for rec in caplog.records]
    assert len(matches) == 1, (
        f"expected exactly one record with event={event!r}; got {len(matches)}. "
        f"events seen: {seen!r}"
    )
    return matches[0]


def _event_field(record: logging.LogRecord, field: str) -> object:
    """Read a structured ``extra`` field off a LogRecord (a dynamic attribute)."""
    return getattr(record, field, None)


def _assert_no_pii(record: logging.LogRecord, *needles: str) -> None:
    """Assert none of the sensitive ``needles`` appear on a SLO event record.

    Scoped to the record's rendered message + the fixed set of ``extra`` fields these
    events carry — the exact surface a log pipeline persists — so a regression that
    put the dialled number or gateway host into an outbound SLO event is caught
    (rule 34 / ADR-0084: gateway connection detail on the SIP path is public-repo
    sensitive even though it is not a secret).
    """
    surface = [record.getMessage()]
    for field in ("event", "call_id", "transport", "codec", "category"):
        value = getattr(record, field, None)
        if value is not None:
            surface.append(str(value))
    blob = " | ".join(surface)
    for needle in needles:
        assert needle not in blob, (
            f"event {getattr(record, 'event', None)!r} leaked {needle!r}: {blob!r}"
        )


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
    7. Plugin sends NO greeting RTP: an outbound call plays no inbound greeting
       (ADR-0019); the agent's first turn opens the call, and this no-objective
       dial injects no first turn, so the callee hears nothing.
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

            # ADR-0019: an OUTBOUND call plays NO inbound greeting — the agent's
            # first turn opens the call. This dial carries no objective, so
            # _inject_objective_first_turn (ADR-0029) seeds nothing, and the
            # greeting was the harness's only RTP source. So NO unsolicited RTP
            # must reach the gateway. Before the fix the canned inbound greeting
            # ("Hello, how can I help you?") flowed here; assert its absence over a
            # bounded window so a regression that re-plays it fails this test.
            await asyncio.sleep(0.5)
            assert gateway.rtp.received_packets == []

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


async def test_outbound_answer_order_selects_peer_preferred_pcmu_over_opus() -> None:
    """A received 2xx answer keeps the answerer's codec order.

    The plugin is the offerer on outbound calls. If it offered Opus before PCMU but
    the 2xx answer lists PCMU before Opus, the answerer selected PCMU and the engine
    must send PCMU. Reordering the received answer back to our offer menu would make
    us send Opus against a PCMU-preferred answer.
    """

    class _PcmuBeforeOpusGateway(OutboundGateway):
        """Gateway whose 2xx answer prefers PCMU even though Opus is also accepted."""

        def _sdp_answer(self) -> str:
            rtp_port = self.rtp.port
            return (
                "v=0\r\n"
                "o=- 8000 8000 IN IP4 127.0.0.1\r\n"
                "s=-\r\n"
                "c=IN IP4 127.0.0.1\r\n"
                "t=0 0\r\n"
                f"m=audio {rtp_port} RTP/AVP 0 111 101\r\n"
                "a=rtpmap:0 PCMU/8000\r\n"
                "a=rtpmap:111 opus/48000/2\r\n"
                "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
                "a=rtpmap:101 telephone-event/8000\r\n"
                "a=fmtp:101 0-16\r\n"
                "a=ptime:20\r\n"
                "a=sendrecv\r\n"
            )

    from hermes_voip.media.engine import Codec  # noqa: PLC0415

    gateway = _PcmuBeforeOpusGateway()
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

            with patch("hermes_voip.adapter._opus_sip_available", return_value=True):
                place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))

                await gateway.await_invite_from_plugin(timeout=5.0)
                await gateway.await_invite_from_plugin(timeout=5.0)
                await gateway.await_ack_from_plugin(timeout=5.0)

                returned_call_id = await asyncio.wait_for(place_task, timeout=5.0)

            await _until(
                lambda: returned_call_id in adapter._call_sessions, timeout=5.0
            )
            session = adapter._call_sessions[returned_call_id]
            engine = session._media
            assert isinstance(engine, RtpMediaTransport), (
                f"expected RtpMediaTransport, got {type(engine)}"
            )
            assert engine._codec is Codec.PCMU, (
                f"engine codec is {engine._codec!r} "
                f"(payload type {engine._codec.value}); expected Codec.PCMU because "
                "the received 2xx answer listed PCMU first."
            )

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


async def test_outbound_lifecycle_invite_sent_and_connected_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """place_call emits outbound_invite_sent + outbound_call_connected (issue #1294).

    ADR-0075-style structured records let a log pipeline compute the outbound-call
    SLO (attempts vs answers), correlating the pair by the shared Call-ID. Both
    carry only the Call-ID + fixed non-sensitive context (transport, and — on
    connect — the negotiated codec); NEVER the dialled number or gateway host
    (rule 34 / ADR-0084). Driven end-to-end against the real loopback UAC flow.
    """
    gateway = OutboundGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)

            with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
                place_task = asyncio.create_task(adapter.place_call(_LEAK_PROBE_TARGET))
                await gateway.await_invite_from_plugin(timeout=5.0)  # challenge
                await gateway.await_invite_from_plugin(timeout=5.0)  # re-auth
                await gateway.await_ack_from_plugin(timeout=5.0)
                call_id = await asyncio.wait_for(place_task, timeout=5.0)
                await _until(lambda: call_id in adapter._call_sessions, timeout=5.0)

            sent = _one_event_record(caplog, "outbound_invite_sent")
            assert _event_field(sent, "call_id") == call_id
            assert _event_field(sent, "transport") == "tls"

            connected = _one_event_record(caplog, "outbound_call_connected")
            assert _event_field(connected, "call_id") == call_id
            assert _event_field(connected, "transport") == "tls"
            # A real G.711 answer was negotiated → a concrete codec name (never PII).
            codec = _event_field(connected, "codec")
            assert isinstance(codec, str)
            assert codec, "connected event carries an empty codec"

            # Redaction (rule 34 / ADR-0084): neither event carries the dialled target
            # or the gateway host in its message or any structured field.
            for record in (sent, connected):
                _assert_no_pii(record, _LEAK_PROBE_TARGET, gateway.sip_host)
    finally:
        await gateway.stop()


async def test_outbound_lifecycle_failed_event_carries_category(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 486 outbound failure emits outbound_call_failed with category 'busy' (#1294).

    The event carries the ADR-0086 failure CATEGORY (the SAME the agent-facing tool
    result surfaces), NEVER the SIP reason phrase, the dialled number, or the gateway
    host (rule 34 / ADR-0084). The prior invite_sent event still fired (the INVITE
    left before the 486), and NO connected event is emitted for a failed attempt.
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

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)

            with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
                with pytest.raises(OutboundCallFailed) as exc_info:
                    await adapter.place_call(_LEAK_PROBE_TARGET)
                assert exc_info.value.status == 486

            failed = _one_event_record(caplog, "outbound_call_failed")
            assert _event_field(failed, "transport") == "tls"
            # The failure CATEGORY is the ADR-0086 outcome — 486 → BUSY — never the
            # SIP reason phrase.
            assert _event_field(failed, "category") == "busy"
            failed_call_id = _event_field(failed, "call_id")
            assert isinstance(failed_call_id, str)
            assert failed_call_id, "failed event carries an empty call_id"

            # invite_sent fired for the same attempt; no connected event on a failure.
            sent = _one_event_record(caplog, "outbound_invite_sent")
            assert _event_field(sent, "call_id") == failed_call_id
            assert not _records_with_event(caplog, "outbound_call_connected")

            # Redaction (rule 34 / ADR-0084): the failure category and the events carry
            # no dialled number and no gateway host — AND the failed event must carry
            # only the ADR-0086 CATEGORY, never the SIP reason phrase the 486 returned
            # ("Busy Here"), so a regression that logged str(exc)/the reason is caught.
            for record in (failed, sent):
                _assert_no_pii(record, _LEAK_PROBE_TARGET, gateway.sip_host)
            _assert_no_pii(failed, "Busy Here")
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


class _SrtpMatchingPlusUnsupportedCryptoGateway(_SrtpAnsweringGateway):
    """Answers RTP/SAVP with the matching crypto PLUS an UNSUPPORTED-suite extra.

    Distinct from the malformed case: the extra line is a *well-formed* ``a=crypto``
    (valid ``inline:`` key) but names a suite we do not support, so the parser drops it
    from ``crypto_attrs`` — again raw count two, filtered to one. Same RFC 4568 §5.1.2
    violation; the RAW-line count fails it closed.
    """

    def _sdp_answer(self) -> str:
        first = (
            f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_GATEWAY_ANSWER_SDES_KEY}\r\n"
        )
        # A second, well-formed-but-UNSUPPORTED-suite a=crypto (parser drops it; the
        # raw line is still present, so the raw count is two — ambiguous).
        extra = (
            "a=crypto:2 AES_256_CM_HMAC_SHA1_80 "
            f"inline:{_GATEWAY_ANSWER_SDES_KEY_2}\r\n"
        )
        return super()._sdp_answer().replace(first, first + extra)


class _SrtpDtlsProfileMatchingCryptoGateway(_SrtpAnsweringGateway):
    """Answers a SECURE-but-NON-RTP/SAVP profile (UDP/TLS/RTP/SAVP) + matching a=crypto.

    codex r3 BLOCKING 2: ``is_srtp`` is true for ANY ``*SAVP`` profile, so a DTLS-SRTP
    profile (``UDP/TLS/RTP/SAVP``) — or WebRTC ``RTP/SAVPF`` — carrying one matching
    a=crypto would be wrongly accepted and keyed as bare SDES (spec-invalid: SDES keying
    on a DTLS/AVPF media line is a dead call). The validator must require the answer
    profile is EXACTLY ``RTP/SAVP`` and fail closed otherwise.
    """

    def _sdp_answer(self) -> str:
        # Same SDP as the SDES gateway but with the DTLS-SRTP profile token instead of
        # plain SDES RTP/SAVP. The single a=crypto still matches our offer (tag 1 /
        # 80-bit) — proving the rejection is on the PROFILE, not the crypto.
        return super()._sdp_answer().replace("RTP/SAVP", "UDP/TLS/RTP/SAVP")


async def test_outbound_sdes_non_savp_secure_answer_fails_closed() -> None:
    """Flag ON + a SECURE non-RTP/SAVP answer (UDP/TLS/RTP/SAVP) + matching crypto.

    Codex r3 BLOCKING 2: ``answer_audio.is_srtp`` is true for any ``*SAVP`` profile, so
    a DTLS-SRTP (or WebRTC SAVPF) 2xx with one matching a=crypto would be wrongly keyed
    as bare SDES. The validator must require the answer profile is EXACTLY ``RTP/SAVP``;
    anything else fails closed (ACK+BYE), even with an otherwise-matching crypto. RED
    before the exact-profile check.
    """
    gateway = _SrtpDtlsProfileMatchingCryptoGateway()
    gateway.set_register_responder()
    await gateway.start()
    try:
        await _assert_refused_2xx_acks_then_byes_then_fails(gateway)
    finally:
        await gateway.stop()


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


async def test_outbound_sdes_matching_plus_unsupported_crypto_fails_closed() -> None:
    """Flag ON + matching crypto PLUS a well-formed UNSUPPORTED-suite extra → fail.

    The codex-r2 companion to the malformed case: the extra a=crypto is syntactically
    valid (good inline: key) but an unsupported suite, so it parses out of crypto_attrs
    yet still counts as a second RAW a=crypto line (RFC 4568 §5.1.2 — an answer selects
    exactly one). The RAW-line count fails it closed (ACK+BYE), proving the guard
    rejects an ambiguous answer regardless of WHY the extra line is unusable.
    """
    gateway = _SrtpMatchingPlusUnsupportedCryptoGateway()
    gateway.set_register_responder()
    await gateway.start()
    try:
        await _assert_refused_2xx_acks_then_byes_then_fails(gateway)
    finally:
        await gateway.stop()


async def test_outbound_unexpected_exception_after_ack_byes_once_no_half_open() -> None:
    """Codex r3 BLOCKING 3: a NON-OutboundCallFailed error after the ACK BYEs once.

    The 2xx is ACKed (dialog established), then a later step raises something OTHER than
    OutboundCallFailed (here CallSession wiring is patched to raise ValueError). That
    post-ACK exception MUST tear the established dialog down with EXACTLY ONE BYE before
    propagating — never leave it half-open, never double-BYE. RED before the ack_sent
    teardown guard (the old except only caught OutboundCallFailed, so the session-wiring
    failure left the dialog un-BYE'd). The error still propagates (rule 37) and no call
    runs.
    """
    gateway = OutboundGateway()  # plain RTP/AVP answer — flag OFF, so no crypto path
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)

            # Patch a POST-ACK step (CallSession construction, which runs after the ACK
            # and outside the codec/crypto try) to raise a non-OutboundCallFailed error.
            with patch(
                "hermes_voip.adapter.CallSession",
                side_effect=ValueError("injected post-ACK failure"),
            ):
                place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))

                await gateway.await_invite_from_plugin(timeout=5.0)  # challenge
                await gateway.await_invite_from_plugin(timeout=5.0)  # re-auth
                # The 2xx is ACKed (TU owns the 2xx ACK) before the patched step raises.
                ack = await gateway.await_ack_from_plugin(timeout=5.0)
                assert ack.method == "ACK"
                # The injected post-ACK failure must BYE the established dialog.
                bye = await gateway.await_bye_from_plugin(timeout=5.0)
                assert bye.method == "BYE"

                # The error propagates (not swallowed) — place_call raises ValueError.
                with pytest.raises(ValueError, match="injected post-ACK failure"):
                    await asyncio.wait_for(place_task, timeout=5.0)

            # EXACTLY ONE BYE — the teardown must not double-BYE. Give any stray
            # second BYE a moment to (not) arrive, then assert the queue is empty.
            await asyncio.sleep(0.1)
            assert gateway._received_byes.empty(), (
                "a second BYE was sent — the post-ACK teardown double-BYE'd the dialog"
            )
            # No call/engine left running.
            assert not adapter._call_sessions
            assert not adapter._call_loops
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
            # formatted message nor in any record's repr/args. CRITICAL (rule 34): the
            # failure message itself must NEVER interpolate the captured record payload
            # — if the key DID leak, printing record.getMessage() here would re-leak it
            # to the PUBLIC CI log. Report only the logger name + level + which check
            # failed, never the raw message/args.
            for record in caplog.records:
                assert known_key_b64 not in record.getMessage(), (
                    f"SDES offer key leaked into a log message "
                    f"({record.name} {record.levelname}) — payload withheld (rule 34)"
                )
                assert known_key_b64 not in repr(record.args), (
                    f"SDES offer key leaked via log args "
                    f"({record.name} {record.levelname}) — args withheld (rule 34)"
                )
    finally:
        await gateway.stop()


# ---------------------------------------------------------------------------
# ADR-0069: outbound SIP CANCEL (RFC 3261 §9.1) — abort_call + ring_timeout_secs.
# ---------------------------------------------------------------------------


class _RingingGateway(OutboundGateway):
    """A gateway that auth-challenges, then RINGS forever (no final response).

    On the re-auth INVITE it sends 100 Trying + 180 Ringing but never a 200/4xx, so
    the plugin's awaiter is parked exactly as during a real no-answer ring. When the
    plugin CANCELs (RFC 3261 §9.1) the gateway answers the CANCEL 200 OK and the
    pending INVITE 487 Request Terminated, recording the CANCEL it received.
    """

    def __init__(self) -> None:
        super().__init__()
        self._received_cancels: asyncio.Queue[SipRequest] = asyncio.Queue()
        # The most recent ringing INVITE per Call-ID, so the CANCEL handler can 487
        # the exact transaction (same Via/From/To/CSeq).
        self._ringing_invite: dict[str, SipRequest] = {}

    async def _respond(self, request: SipRequest) -> list[str]:
        if request.method == "CANCEL":
            return await self._handle_cancel(request)
        return await super()._respond(request)

    async def _handle_invite(self, request: SipRequest) -> list[str]:
        await self._received_invites.put(request)
        call_id = request.header("Call-ID") or ""
        seen = self._invite_challenges.get(call_id, 0)
        self._invite_challenges[call_id] = seen + 1
        if seen == 0:
            return [self._build_407(request)]
        # Re-INVITE with auth: ring, but never answer with a final response.
        self._ringing_invite[call_id] = request
        return [self._build_100(request), self._build_180(request)]

    async def _handle_cancel(self, request: SipRequest) -> list[str]:
        await self._received_cancels.put(request)
        call_id = request.header("Call-ID") or ""
        replies = [self._build_200_ok_simple(request)]
        invite = self._ringing_invite.get(call_id)
        if invite is not None:
            replies.append(self._build_487(invite))
        return replies

    def _build_487(self, invite: SipRequest) -> str:
        """487 Request Terminated for the CANCELled INVITE (its own transaction)."""
        via = invite.header("Via") or ""
        from_ = invite.header("From") or ""
        to = invite.header("To") or ""
        call_id = invite.header("Call-ID") or ""
        cseq = invite.header("CSeq") or ""
        return (
            "SIP/2.0 487 Request Terminated\r\n"
            f"Via: {via}\r\n"
            f"From: {from_}\r\n"
            f"To: {to};tag=cancelled-{new_tag()}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq}\r\n"
            "Content-Length: 0\r\n\r\n"
        )

    async def await_cancel_from_plugin(self, *, timeout: float = 5.0) -> SipRequest:
        """Wait for a CANCEL from the plugin (RFC 3261 §9.1)."""
        return await asyncio.wait_for(self._received_cancels.get(), timeout)


async def _call_id_of_latest_invite(gateway: OutboundGateway) -> str:
    """The Call-ID of the latest INVITE the gateway received (drains the queue)."""
    invite = await gateway.await_invite_from_plugin(timeout=5.0)
    return invite.header("Call-ID") or ""


async def test_abort_call_sends_cancel_and_leaves_no_running_call() -> None:
    """abort_call on a ringing outbound INVITE sends a §9.1 CANCEL and tears down.

    RED on main: there is no abort_call / send_cancel, so place_call has no abort
    lever and the call hangs until the 35 s sink timeout. After ADR-0069, abort_call
    sends a CANCEL, the gateway 487s, place_call raises OutboundCallCancelled, and no
    call session/loop/outbound-slot remains.
    """
    gateway = _RingingGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)
            place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))

            await gateway.await_invite_from_plugin(timeout=5.0)  # challenge
            call_id = await _call_id_of_latest_invite(gateway)  # re-auth (ringing)

            # Abort the ringing call.
            aborted = await adapter.abort_call(call_id, "operator abort")
            assert aborted is True, "abort_call must report it issued a CANCEL"

            cancel = await gateway.await_cancel_from_plugin(timeout=5.0)
            assert cancel.method == "CANCEL"
            assert cancel.header("Call-ID") == call_id

            with pytest.raises(OutboundCallCancelled):
                await asyncio.wait_for(place_task, timeout=5.0)

            await asyncio.sleep(0.05)
            assert not adapter._call_sessions
            assert not adapter._call_loops
            assert _TARGET_EXT not in adapter._outbound_extensions
    finally:
        await gateway.stop()


async def test_ring_timeout_fires_cancel_and_raises_cancelled() -> None:
    """place_call(ring_timeout_secs=...) cancels an unanswered ring automatically.

    RED on main: place_call has no ring_timeout_secs kwarg. After ADR-0069 the timer
    fires a CANCEL after the bound, the gateway 487s, and place_call raises
    OutboundCallCancelled — never hanging for the full sink timeout.
    """
    gateway = _RingingGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)
            place_task = asyncio.create_task(
                adapter.place_call(_TARGET_EXT, ring_timeout_secs=0.3)
            )

            await gateway.await_invite_from_plugin(timeout=5.0)  # challenge
            await gateway.await_invite_from_plugin(timeout=5.0)  # re-auth (ringing)

            # The ring-timeout fires the CANCEL with no further action from the test.
            cancel = await gateway.await_cancel_from_plugin(timeout=5.0)
            assert cancel.method == "CANCEL"

            with pytest.raises(OutboundCallCancelled):
                await asyncio.wait_for(place_task, timeout=5.0)

            await asyncio.sleep(0.05)
            assert not adapter._call_sessions
            assert not adapter._call_loops
    finally:
        await gateway.stop()


async def test_abort_call_after_200_is_a_noop_no_cancel() -> None:
    """abort_call on an ALREADY-answered call is a no-op (returns False, no CANCEL).

    RFC 3261 §9.1 applies only before the final response. Once the 2xx has arrived the
    dialog is established; CANCEL is too late (the right teardown is an in-dialog BYE,
    not CANCEL). abort_call must return False and put no CANCEL on the wire.
    """
    gateway = OutboundGateway()  # answers normally (200 OK with SDP)
    gateway.set_register_responder()
    await gateway.start()

    # Record every SIP byte the gateway saw, so we can assert no CANCEL was sent.
    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)
            place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))
            await gateway.await_invite_from_plugin(timeout=5.0)  # challenge
            await gateway.await_invite_from_plugin(timeout=5.0)  # re-auth
            await gateway.await_ack_from_plugin(timeout=5.0)
            call_id = await asyncio.wait_for(place_task, timeout=5.0)
            await _until(lambda: call_id in adapter._call_sessions, timeout=5.0)

            aborted = await adapter.abort_call(call_id, "too late")
            assert aborted is False, "abort_call after the 2xx must be a no-op"
            await asyncio.sleep(0.1)
            assert not any(raw.startswith("CANCEL ") for raw in gateway.received_sip), (
                "no CANCEL may be sent for an already-answered call"
            )
            # The established call is untouched by the failed abort.
            assert call_id in adapter._call_sessions
    finally:
        await gateway.stop()


async def test_double_abort_is_idempotent_one_cancel() -> None:
    """Two abort_call invocations for the same ringing call send exactly one CANCEL.

    The second abort_call is a no-op (returns False); the call is already cancelling.
    """
    gateway = _RingingGateway()
    gateway.set_register_responder()
    await gateway.start()

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)
            place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))
            await gateway.await_invite_from_plugin(timeout=5.0)  # challenge
            call_id = await _call_id_of_latest_invite(gateway)  # re-auth (ringing)

            first = await adapter.abort_call(call_id, "abort 1")
            second = await adapter.abort_call(call_id, "abort 2")
            assert first is True
            assert second is False, "a second abort_call must be a no-op"

            await gateway.await_cancel_from_plugin(timeout=5.0)
            with pytest.raises(OutboundCallCancelled):
                await asyncio.wait_for(place_task, timeout=5.0)

            await asyncio.sleep(0.1)
            cancels = [raw for raw in gateway.received_sip if raw.startswith("CANCEL ")]
            assert len(cancels) == 1, "exactly one CANCEL must reach the gateway"
    finally:
        await gateway.stop()


async def test_abort_racing_the_2xx_same_tick_aborts_instead_of_proceeding() -> None:
    """Fix (d): an abort that lands as the 2xx is accepted must abort, not proceed.

    The acceptance flow ACKs the 2xx, negotiates the codec, then wires the
    ``CallSession``. If an abort (a ``ring_timeout`` expiry or an explicit
    ``abort_call``) sets ``cancel_requested`` in that same loop tick — after the 2xx
    is dequeued but before the session is wired — the call must NOT proceed to a live
    session; it must abort and raise ``OutboundCallCancelled``. Without the post-2xx
    re-check the racing abort is ignored and a live call is established anyway.

    Deterministic: a sync seam in the acceptance window (``negotiate_audio``) flips
    ``cancel_requested`` on the in-flight pending entry exactly once, reproducing the
    same-tick race, then delegates to the real negotiation so the rest of acceptance
    runs unchanged.
    """
    gateway = OutboundGateway()  # answers normally (200 OK with SDP)
    gateway.set_register_responder()
    await gateway.start()

    fired = {"done": False}

    providers = Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())
    try:
        async with _real_adapter(gateway, providers=providers) as adapter:
            from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

            assert isinstance(adapter, VoipAdapter)

            def _negotiate_then_race(
                offer: AudioMedia,
                supported: Sequence[str],
                *,
                prefer_local: bool = True,
            ) -> tuple[Codec, ...]:
                # On the first (and only) acceptance, simulate an abort that landed
                # in this same tick: the 2xx is already dequeued and ACKed, but the
                # session is not wired yet. Flip the in-flight call's cancel flag.
                if not fired["done"] and adapter._outbound_pending:
                    fired["done"] = True
                    pending = next(iter(adapter._outbound_pending.values()))
                    pending.cancel_requested = True
                    pending.reason = "raced abort"
                return negotiate_audio(offer, supported, prefer_local=prefer_local)

            with patch(
                "hermes_voip.adapter.negotiate_audio",
                side_effect=_negotiate_then_race,
            ):
                place_task = asyncio.create_task(adapter.place_call(_TARGET_EXT))
                with pytest.raises(OutboundCallCancelled):
                    await asyncio.wait_for(place_task, timeout=5.0)

            assert fired["done"], "the acceptance-window race seam never fired"
            # No live call may survive an abort that raced the 2xx.
            await asyncio.sleep(0.05)
            assert not adapter._call_sessions, (
                "a call aborted as the 2xx arrived must not leave a live session"
            )
            assert not adapter._call_loops
            assert _TARGET_EXT not in adapter._outbound_extensions
            # The answered dialog the UAC ACKed is torn down with a BYE (RFC 3261 §15).
            bye = await asyncio.wait_for(gateway._received_byes.get(), timeout=5.0)
            assert bye.method == "BYE"
    finally:
        await gateway.stop()
