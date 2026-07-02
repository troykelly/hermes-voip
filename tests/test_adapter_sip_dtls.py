"""VoipAdapter SIP DTLS-SRTP (UDP/TLS/RTP/SAVP, no ICE) inbound branch (ADR-0053 §6).

``VoipAdapter`` subclasses the real ``gateway.platforms.base.BasePlatformAdapter`` at
runtime, so these tests require the optional ``hermes`` extra and skip cleanly without
it (they run in the hermes-contract CI job). The SIP/RTP/DTLS/provider collaborators
are fakes — no real network, no real ML, no real DTLS handshake — but the on-the-wire
SDP answer and the branch logic are exactly what production produces.

Verifies the adapter-activation wave (ADR-0053 §6) end-to-end:

* a SIP DTLS-SRTP offer (``UDP/TLS/RTP/SAVP`` + ``a=fingerprint``, no ICE) routes to
  the DTLS activation path and is answered with a ``UDP/TLS/RTP/SAVP`` 200 OK carrying
  our ``a=fingerprint`` / ``a=setup`` and the **bound session port** in ``c=``/``m=``
  (RFC 5763 — no ``a=crypto``, no ICE);
* the media engine is built over the **session's datagram pipe** with the
  DTLS-derived SRTP, and the handshake runs with the peer's fingerprint + RTP address;
* the ``HERMES_VOIP_SIP_DTLS_SETUP`` role knob flows to the session;
* the rollback switch ``HERMES_VOIP_SIP_DTLS_SRTP=false`` makes a SIP-DTLS offer fall
  through to the SDES/plain path (no DTLS session built, no behaviour change);
* a handshake failure ends the call cleanly and **never** answers plaintext;
* a plain RTP/AVP and a WebRTC SAVPF offer never touch the SIP-DTLS branch (no
  regression).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from hermes_voip.adapter import VoipAdapter

pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")
pytest.importorskip("OpenSSL", reason="webrtc extra (pyOpenSSL) not installed")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_voip.config import ExtensionConfig, GatewayConfig, load_media_config
from hermes_voip.manager import NewCall, RegistrationManager
from hermes_voip.message import SipRequest, SipResponse, new_call_id, new_tag
from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict, InjectionGuard
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.sdp import Fingerprint, SessionDescription, SetupRole


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


async def _until(
    predicate: Callable[[], bool], *, timeout: float = 3.0, step: float = 0.001
) -> None:
    """Poll ``predicate`` until true or the timeout elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            msg = "condition not met within the timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(step)


class _FakeTransport:
    """Fake SipTransport + CallSignaling capturing sent messages."""

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
        """No-op."""

    async def aclose(self) -> None:
        """No-op."""

    def bind_manager(self, manager: object) -> None:
        """No-op."""

    def add_call(self, call_id: str, sink: object) -> None:
        self._calls[call_id] = sink

    def remove_call(self, call_id: str, sink: object | None = None) -> None:
        self._calls.pop(call_id, None)


class _FakeTtsStream(TtsStream):
    """An empty, lifecycle-complete TtsStream (yields nothing). Typed, no ignores."""

    def __aiter__(self) -> _FakeTtsStream:
        return self

    async def __anext__(self) -> PcmFrame:
        raise StopAsyncIteration

    async def flush(self) -> None:
        """No-op: nothing buffered."""

    async def cancel(self) -> None:
        """No-op."""

    async def aclose(self) -> None:
        """No-op: idempotent teardown."""


class _FakeTTS(StreamingTTS):
    """A StreamingTTS that returns the empty stream (typed, Protocol-conformant)."""

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        return _FakeTtsStream()

    @property
    def output_sample_rate(self) -> int:
        return 16_000


class _FakeASR(StreamingASR):
    """A StreamingASR that drains audio and yields no transcripts (typed)."""

    async def _drain(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        async for _ in audio:
            pass
        # An async generator that yields nothing: the for-loop never iterates.
        empty: tuple[Transcript, ...] = ()
        for transcript in empty:
            yield transcript

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        return self._drain(audio)

    @property
    def input_sample_rate(self) -> int:
        return 16_000


class _FakeGuard(InjectionGuard):
    """An InjectionGuard that always allows (typed, Protocol-conformant)."""

    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            score=0.0,
            degraded=False,
            normalized_text=text,
            reasons=(),
        )


def _fake_providers() -> Providers:
    return Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())


_FAKE_ENV = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
}


def _gateway_config() -> GatewayConfig:
    return GatewayConfig(
        host="pbx.example.test",
        port=5061,
        transport="tls",
        expires=3600,
        user_agent="hermes-voip-test",
        extensions=(
            ExtensionConfig(
                index=0, extension="1000", username="1000", password="fake"
            ),
        ),
        default_index=0,
    )


def _ext_config() -> ExtensionConfig:
    return ExtensionConfig(index=0, extension="1000", username="1000", password="fake")


# A SIP DTLS-SRTP offer: UDP/TLS/RTP/SAVP (NO 'F' feedback), a=fingerprint + a=setup,
# a real c=/port (no ICE) — exactly what build_sip_dtls_answer answers (ADR-0053 §1).
_SIP_DTLS_OFFER = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 40000 UDP/TLS/RTP/SAVP 0 8 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=fmtp:101 0-16\r\n"
    "a=fingerprint:sha-256 "
    "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:"
    "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99\r\n"
    "a=setup:actpass\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
)

_PLAIN_OFFER = (
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

_SDES_OFFER = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 20000 RTP/SAVP 0 8\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=crypto:1 AES_CM_128_HMAC_SHA1_80 "
    "inline:{sdes_key}\r\n"
    "a=sendrecv\r\n"
)

# A WebRTC SAVPF offer — must keep using the ICE/WebRTC path, never SIP-DTLS.
_WEBRTC_OFFER = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=audio 50000 UDP/TLS/RTP/SAVPF 111 0\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=fingerprint:sha-256 "
    "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:"
    "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00\r\n"
    "a=setup:actpass\r\n"
    "a=ice-ufrag:peerUFRG\r\n"
    "a=ice-pwd:peerPWDpeerPWDpeerPWDpeer\r\n"
    "a=candidate:1 1 UDP 2130706431 127.0.0.1 50000 typ host\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
)


def _sdes_offer() -> str:
    """An SDES offer with a runtime-computed inline key (gitleaks-safe — rule 34)."""
    import base64  # noqa: PLC0415

    # A 30-octet AES_CM_128_HMAC_SHA1_80 master key||salt, computed at runtime so no
    # secret-shaped literal is committed (the path-scoped gitleaks allowlist only
    # covers test_sdp.py + the KAT vectors).
    key = base64.b64encode(bytes(range(30))).decode("ascii")
    return _SDES_OFFER.format(sdes_key=key)


def _make_invite(offer: str, call_id: str) -> str:
    content_length = len(offer.encode("utf-8"))
    ftag = new_tag()
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKfake\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:9999@pbx.example.test>;tag={ftag}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:9999@127.0.0.1:60000;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{offer}"
    )


async def _build_adapter_with_manager(
    transport: _FakeTransport, media_cfg: object
) -> tuple[VoipAdapter, RegistrationManager]:
    """A real VoipAdapter + the real RegistrationManager it routes through.

    The manager is returned so a test can route an in-dialog ACK/BYE through it (the
    same path ``SipOverTlsTransport._dispatch_request`` uses) to the answer-time dialog
    guard registered during the handshake window.
    """
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))
    manager = RegistrationManager(_gateway_config(), transport)
    with (
        patch(
            "hermes_voip.adapter.load_gateway_config", return_value=_gateway_config()
        ),
        patch("hermes_voip.adapter.load_media_config", return_value=media_cfg),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()
    return adapter, manager


async def _build_adapter(transport: _FakeTransport, media_cfg: object) -> VoipAdapter:
    """A real VoipAdapter wired to fakes + a real RegistrationManager + media_cfg."""
    adapter, _manager = await _build_adapter_with_manager(transport, media_cfg)
    return adapter


def _sent_200_ok(transport: _FakeTransport) -> SipResponse:
    oks = [SipResponse.parse(m) for m in transport.sent if m.startswith("SIP/2.0 200")]
    assert oks, "the adapter did not send a 200 OK answer"
    return oks[-1]


def _last_status(transport: _FakeTransport) -> int:
    assert transport.sent, "the adapter sent no response at all"
    return int(transport.sent[-1].split(" ", 2)[1])


# The bound UDP port the fake session reports — what the SDP answer must advertise.
_SESSION_PORT = 41234


class _FakeSipDtlsSession:
    """A fake SipDtlsMediaSession: skips real DTLS, returns canned SRTP sessions.

    Records construction + prepare + handshake so the test can assert the branch
    ran with the right inputs, and exposes a fixed fingerprint / setup / local_port
    for the answer. ``run_handshake`` succeeds by default; set ``fail_handshake`` to
    raise (the handshake-failure path).
    """

    last: _FakeSipDtlsSession | None = None
    fail_handshake: bool = False
    # When set, run_handshake blocks on this event before completing/failing — so a
    # test can deterministically inject an in-dialog ACK/BYE WHILE the handshake is in
    # progress (the answer-time dialog guard is already registered), then release it.
    gate: asyncio.Event | None = None
    # Set once run_handshake has been entered (so a test can wait for the window).
    in_handshake: asyncio.Event | None = None

    def __init__(
        self,
        *,
        offer_setup: SetupRole | None,
        answer_setup: str = "auto",
        **_kw: object,
    ) -> None:
        self.offer_setup = offer_setup
        self.answer_setup = answer_setup
        self.prepared = False
        self.prepare_kwargs: dict[str, object] | None = None
        self.handshake_args: dict[str, object] | None = None
        self.closed = False
        # The pipe handed to the engine as ice_transport (its identity is asserted).
        self.pipe = MagicMock(name="udp_pipe")
        _FakeSipDtlsSession.last = self

    async def prepare(self, *, local_address: str, local_port: int = 0) -> None:
        self.prepared = True
        self.prepare_kwargs = {
            "local_address": local_address,
            "local_port": local_port,
        }

    @property
    def setup(self) -> SetupRole:
        # The fake always answers 'active' (the auto default for an actpass offer).
        return SetupRole("active")

    @property
    def fingerprint(self) -> Fingerprint:
        return Fingerprint(algorithm="sha-256", value=":".join(["AB"] * 32))

    @property
    def local_port(self) -> int:
        return _SESSION_PORT

    async def run_handshake(self, **kwargs: object) -> tuple[object, object]:
        self.handshake_args = kwargs
        if _FakeSipDtlsSession.in_handshake is not None:
            _FakeSipDtlsSession.in_handshake.set()
        # Hold inside the handshake until released, so a test can deliver an in-dialog
        # ACK/BYE to the answer-time guard mid-handshake (the realistic race).
        if _FakeSipDtlsSession.gate is not None:
            await _FakeSipDtlsSession.gate.wait()
        if _FakeSipDtlsSession.fail_handshake:
            msg = "DTLS handshake failed (fingerprint mismatch)"
            raise ValueError(msg)
        return (MagicMock(name="srtp_in"), MagicMock(name="srtp_out"))

    def derive_srtcp_sessions(self) -> tuple[object, object]:
        """The SRTCP (inbound, outbound) pair from the same DTLS export (ADR-0066)."""
        return (MagicMock(name="srtcp_in"), MagicMock(name="srtcp_out"))

    async def close(self) -> None:
        self.closed = True


def _fake_engine() -> MagicMock:
    """A fake RtpMediaTransport sufficient for the SIP-DTLS branch + call tail.

    ``start_rtcp`` is an AsyncMock: the secured DTLS path now ACTIVATES RTCP wrapped in
    SRTCP (ADR-0066), so the engine awaits it; ``_rtcp_active`` stays a plain attribute
    (the fake never runs the real loop, so teardown's quality-log guard reads False).
    """
    return MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        start_rtcp=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=_SESSION_PORT,
        inbound_sample_rate=8_000,
    )


@pytest.fixture(autouse=True)
def _reset_fake_session() -> None:
    """Reset the fake session's class-level state between tests."""
    _FakeSipDtlsSession.last = None
    _FakeSipDtlsSession.fail_handshake = False
    _FakeSipDtlsSession.gate = None
    _FakeSipDtlsSession.in_handshake = None


def _in_dialog_request(method: str, ok: SipResponse, *, call_id: str) -> SipRequest:
    """A gateway in-dialog ACK/BYE echoing the dialog To/From from our 200 OK.

    Per RFC 3261 §12, the peer's in-dialog request carries OUR identity (the 200 OK's
    ``To``, including the local tag we set) in ``To`` and the caller's in ``From`` — so
    this is faithful to what a real gateway sends after our answer, and routes through
    the real ``RegistrationManager`` to the registered dialog consumer.
    """
    to_value = ok.header("To") or ""
    from_value = ok.header("From") or ""
    cseq = 1 if method == "ACK" else 2
    raw = (
        f"{method} sip:1000@127.0.0.1:5061 SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 203.0.113.7:5061;branch=z9hG4bKdlg\r\n"
        "Max-Forwards: 70\r\n"
        f"From: {from_value}\r\n"
        f"To: {to_value}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} {method}\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    return SipRequest.parse(raw)


async def _deliver_in_dialog(
    manager: RegistrationManager, request: SipRequest
) -> object:
    """Route an in-dialog request through the real manager to its consumer.

    Mirrors ``SipOverTlsTransport._dispatch_request``'s ``InDialog`` branch: route,
    then await the matched consumer's ``handle_request``. Returns the routing so the
    caller can assert it matched a dialog (``InDialog``) and not ``Unroutable``.
    """
    from hermes_voip.manager import InDialog  # noqa: PLC0415

    routing = manager.route_request(request)
    if isinstance(routing, InDialog):
        await routing.consumer.handle_request(routing.request)
    return routing


@pytest.mark.asyncio
async def test_sip_dtls_offer_yields_savp_answer_with_fingerprint_and_pipe() -> None:
    """A UDP/TLS/RTP/SAVP+fingerprint offer is answered DTLS-SRTP over the pipe."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, load_media_config({}))
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    engine = _fake_engine()
    try:
        with (
            patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
            patch(
                "hermes_voip.adapter.RtpMediaTransport", return_value=engine
            ) as engine_ctor,
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)

            ok = _sent_200_ok(transport)
            answer = SessionDescription.parse(ok.body)
            assert answer.audio is not None
            # A SIP DTLS-SRTP answer: UDP/TLS/RTP/SAVP + fingerprint + setup, the
            # bound session port in c=/m=, NO a=crypto, NO ICE (RFC 5763, ADR-0053).
            assert answer.audio.is_sip_dtls
            assert not answer.audio.is_webrtc
            assert answer.audio.fingerprint is not None
            assert answer.audio.setup is not None
            assert answer.audio.setup.value in ("active", "passive")
            assert answer.audio.port == _SESSION_PORT
            assert "a=crypto" not in ok.body
            assert "a=ice-ufrag" not in ok.body
            assert "a=candidate" not in ok.body

            session = _FakeSipDtlsSession.last
            assert session is not None
            assert session.prepared
            # The handshake ran with the peer's fingerprint + RTP address (the
            # offer's c=/m=audio).
            assert session.handshake_args is not None
            peer_fp = session.handshake_args["peer_fingerprint"]
            assert isinstance(peer_fp, Fingerprint)
            assert session.handshake_args["peer_address"] == "127.0.0.1"
            assert session.handshake_args["peer_port"] == 40000

            # The engine was built over the SESSION'S pipe with the DTLS SRTP.
            kwargs = engine_ctor.call_args.kwargs
            assert kwargs["ice_transport"] is session.pipe
            assert kwargs["srtp_inbound"] is not None
            assert kwargs["srtp_outbound"] is not None
    finally:
        in_call.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sip_dtls_call_activates_rtcp_via_srtcp() -> None:
    """The SIP DTLS-SRTP path ACTIVATES RTCP (muxed) wrapped in SRTCP (ADR-0066).

    DTLS-SRTP rides a single UDP pipe (the session owns the socket; the engine binds
    none), so RTCP must rtcp-mux. The adapter builds the engine with the SRTCP sessions
    derived from the SAME DTLS export and calls ``start_rtcp(mux=True)`` — so the
    secured SIP-DTLS call gets authenticated+encrypted RTCP, never cleartext on the
    secured 5-tuple. Supersedes the pre-SRTCP dormancy on this path (rule 19: stated
    limitation "until SRTCP lands", now landed).
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, load_media_config({}))
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    engine = _fake_engine()
    try:
        with (
            patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
            patch(
                "hermes_voip.adapter.RtpMediaTransport", return_value=engine
            ) as engine_ctor,
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)

            # The engine was built with SRTCP wired from the DTLS export.
            kwargs = engine_ctor.call_args.kwargs
            assert kwargs["srtcp_inbound"] is not None
            assert kwargs["srtcp_outbound"] is not None
            # RTCP was activated, MUXED (the single DTLS-SRTP UDP pipe).
            engine.start_rtcp.assert_awaited_once()
            assert engine.start_rtcp.call_args.kwargs["mux"] is True
    finally:
        in_call.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sip_dtls_200_ok_sent_before_handshake() -> None:
    """The 200 OK (our fingerprint) is sent BEFORE run_handshake (§4 ordering).

    The peer needs our fingerprint + setup to start its DTLS half, so the answer
    must precede the handshake. Proven by recording, inside run_handshake, whether a
    200 OK has already been transmitted.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, load_media_config({}))
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    ok_seen_at_handshake: list[bool] = []

    class _OrderingSession(_FakeSipDtlsSession):
        async def run_handshake(self, **kwargs: object) -> tuple[object, object]:
            ok_seen_at_handshake.append(
                any(m.startswith("SIP/2.0 200") for m in transport.sent)
            )
            return await super().run_handshake(**kwargs)

    engine = _fake_engine()
    try:
        with (
            patch("hermes_voip.adapter.SipDtlsMediaSession", _OrderingSession),
            patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)
            assert ok_seen_at_handshake == [True], (
                "200 OK must be sent BEFORE the DTLS handshake (RFC 5763 §4)"
            )
    finally:
        in_call.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sip_dtls_setup_knob_flows_to_session() -> None:
    """HERMES_VOIP_SIP_DTLS_SETUP is passed to the session as answer_setup."""
    transport = _FakeTransport()
    media_cfg = load_media_config({"HERMES_VOIP_SIP_DTLS_SETUP": "passive"})
    adapter = await _build_adapter(transport, media_cfg)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    engine = _fake_engine()
    try:
        with (
            patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
            patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)
            # The offered a=setup (actpass) and the knob both reached the session
            # (the fake records its construction kwargs on ``last``).
            session = _FakeSipDtlsSession.last
            assert session is not None
            assert session.answer_setup == "passive"
            assert session.offer_setup is not None
            assert session.offer_setup.value == "actpass"
    finally:
        in_call.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sip_dtls_disabled_falls_through_to_sdes_plain() -> None:
    """HERMES_VOIP_SIP_DTLS_SRTP=false ⇒ a SIP-DTLS offer never builds a DTLS session.

    The rollback switch routes an ``is_sip_dtls`` offer to the existing SDES/plain
    handler. ``build_sip_dtls_answer`` keeps c=/port, but with DTLS off the call
    falls through: no SipDtlsMediaSession is constructed. (The SDES/plain path then
    488s a UDP/TLS/RTP/SAVP offer it cannot key — that is the existing behaviour we
    must NOT change; the point of this test is that the DTLS branch is skipped.)
    """
    transport = _FakeTransport()
    media_cfg = load_media_config({"HERMES_VOIP_SIP_DTLS_SRTP": "false"})
    adapter = await _build_adapter(transport, media_cfg)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    dtls_ctor = MagicMock()
    with (
        patch("hermes_voip.adapter.SipDtlsMediaSession", dtls_ctor),
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=_fake_engine(),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        # The fall-through SDES/plain path rejects an unkeyable SAVP offer with 488;
        # await that terminal response deterministically.
        await _until(lambda: bool(transport.sent))
        await asyncio.sleep(0)
        # The DTLS session was never constructed — the knob disabled the branch.
        dtls_ctor.assert_not_called()
        # And it was NOT answered as DTLS-SRTP.
        assert not any("UDP/TLS/RTP/SAVP" in m for m in transport.sent)


@pytest.mark.asyncio
async def test_sip_dtls_handshake_failure_byes_after_ack_no_plaintext() -> None:
    """A handshake failure, once the ACK has confirmed the dialog, BYEs it.

    The 200 OK is the DTLS answer (it precedes the handshake), so the peer holds an
    answered call; the dialog is registered at answer-time (ADR-0065) so the ACK
    routes. When the ACK has arrived AND the handshake then fails, the now-CONFIRMED
    dialog (RFC 3261 §15.1.1) is definitively closed with an in-dialog BYE; no SECOND
    plaintext answer is ever sent, and no CallLoop is built on the dead media. The
    session is closed.
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_manager(
        transport, load_media_config({})
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    _FakeSipDtlsSession.fail_handshake = True
    _FakeSipDtlsSession.gate = asyncio.Event()
    _FakeSipDtlsSession.in_handshake = asyncio.Event()
    engine = _fake_engine()
    with (
        patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        # Wait until the 200 OK is sent and the handshake is in progress — the
        # answer-time dialog guard is registered by now.
        assert _FakeSipDtlsSession.in_handshake is not None
        await _FakeSipDtlsSession.in_handshake.wait()
        ok = _sent_200_ok(transport)
        # Deliver the peer's ACK mid-handshake: it MUST route to the dialog guard
        # (not Unroutable), confirming the dialog.
        from hermes_voip.manager import InDialog  # noqa: PLC0415

        routing = await _deliver_in_dialog(
            manager, _in_dialog_request("ACK", ok, call_id=call_id)
        )
        assert isinstance(routing, InDialog), (
            f"the ACK must route to the answer-time dialog guard, got "
            f"{type(routing).__name__}"
        )
        # Now release the handshake so it fails.
        _FakeSipDtlsSession.gate.set()
        session = _FakeSipDtlsSession.last
        assert session is not None
        await _until(lambda: session.closed)
        # The confirmed dialog is closed with a BYE.
        await _until(lambda: any(m.startswith("BYE ") for m in transport.sent))
        await asyncio.sleep(0.01)

    assert call_id not in adapter._call_loops
    answers = [m for m in transport.sent if m.startswith("SIP/2.0 200")]
    assert len(answers) == 1
    plaintext_answers = [
        m
        for m in transport.sent
        if m.startswith("SIP/2.0 200") and "RTP/AVP" in m and "SAVP" not in m
    ]
    assert plaintext_answers == [], "a handshake failure must NOT answer plaintext"
    byes = [m for m in transport.sent if m.startswith("BYE ")]
    assert len(byes) == 1, "a confirmed-dialog handshake failure sends exactly one BYE"


@pytest.mark.asyncio
async def test_sip_dtls_handshake_failure_pre_ack_waits_then_fallback_byes() -> None:
    """A handshake failure BEFORE any ACK does NOT send an illegal pre-ACK BYE.

    RFC 3261 §15.1.1: a UA MUST NOT BYE an unconfirmed dialog. With NO ACK delivered,
    a post-200 handshake failure must (a) tear down media IMMEDIATELY, (b) NOT send a
    BYE while the dialog is unconfirmed, and (c) send a fallback BYE only after the
    bounded ACK-wait (≈Timer H) elapses. We shrink the timeout so the fallback is
    fast and assert the ordering: media closed first, no BYE during the wait, BYE
    after.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, load_media_config({}))
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    _FakeSipDtlsSession.fail_handshake = True
    engine = _fake_engine()
    with (
        patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
        patch("hermes_voip.adapter._ANSWERED_ABORT_ACK_TIMEOUT_S", 0.2),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        # Media is torn down immediately on the failure.
        await _until(lambda: _FakeSipDtlsSession.last is not None)
        session = _FakeSipDtlsSession.last
        assert session is not None
        await _until(lambda: session.closed)
        # While the dialog is unconfirmed (no ACK) the abort must NOT have sent a BYE
        # yet — it is waiting for the ACK. Check right after media close.
        assert not any(m.startswith("BYE ") for m in transport.sent), (
            "must NOT send a pre-ACK BYE on an unconfirmed dialog (RFC 3261 §15.1.1)"
        )
        # After the bounded fallback (0.2s) elapses with no ACK, a fallback BYE is
        # sent so the dialog cannot linger forever.
        await _until(
            lambda: any(m.startswith("BYE ") for m in transport.sent), timeout=2.0
        )
        await asyncio.sleep(0.01)

    assert call_id not in adapter._call_loops
    byes = [m for m in transport.sent if m.startswith("BYE ")]
    assert len(byes) == 1, "exactly one fallback BYE after the bounded wait"


@pytest.mark.asyncio
async def test_sip_dtls_engine_failure_after_handshake_byes_and_cleans_up() -> None:
    """An engine construct/connect failure AFTER the handshake BYEs + cleans up.

    The handshake succeeded (SRTP keyed) but ``RtpMediaTransport.connect`` then
    raises. The 200 OK was already sent, so the dialog is HALF-OPEN: it must be
    closed with a BYE, the media engine stopped (no leaked SRTP state), and the DTLS
    session/pipe closed (no leaked UDP socket) — and no CallLoop built.
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_manager(
        transport, load_media_config({})
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    # The handshake succeeds (gated so we can deliver the ACK mid-flow); the engine's
    # connect() then raises (a post-200, post-handshake failure) — the engine was
    # constructed, so it must be stopped, and the now-confirmed dialog BYE'd.
    _FakeSipDtlsSession.gate = asyncio.Event()
    _FakeSipDtlsSession.in_handshake = asyncio.Event()
    engine = MagicMock(
        connect=AsyncMock(side_effect=RuntimeError("engine connect failed")),
        stop=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=_SESSION_PORT,
        inbound_sample_rate=8_000,
    )
    with (
        patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        assert _FakeSipDtlsSession.in_handshake is not None
        await _FakeSipDtlsSession.in_handshake.wait()
        ok = _sent_200_ok(transport)
        # Confirm the dialog with the ACK, then let the handshake succeed → engine fail.
        await _deliver_in_dialog(
            manager, _in_dialog_request("ACK", ok, call_id=call_id)
        )
        _FakeSipDtlsSession.gate.set()
        session = _FakeSipDtlsSession.last
        assert session is not None
        # The engine was stopped and the session closed (no leaked socket/SRTP).
        await _until(lambda: engine.stop.await_count > 0)
        await _until(lambda: session.closed)
        await _until(lambda: any(m.startswith("BYE ") for m in transport.sent))
        await asyncio.sleep(0.01)

    assert call_id not in adapter._call_loops
    assert engine.stop.await_count == 1, "the constructed engine must be stopped"
    assert session.closed, "the DTLS session/pipe must be closed (no leaked socket)"
    byes = [m for m in transport.sent if m.startswith("BYE ")]
    assert len(byes) == 1, "a post-200 engine failure must BYE the confirmed dialog"


@pytest.mark.asyncio
async def test_sip_dtls_bind_failure_closes_session_even_if_488_send_raises() -> None:
    """A pre-answer bind failure closes the session even if the 488 transmit RAISES.

    The codex finding: session.close() must not be stranded behind a failing
    transport.send(488). Here prepare() fails (a bind error) AND the 488 send itself
    raises — the session must still be closed (no leaked UDP socket), proving the
    close runs in a finally / before the send, not after it.
    """

    class _Failing488Transport(_FakeTransport):
        async def send(self, message: str) -> None:
            await super().send(message)
            if message.startswith("SIP/2.0 488"):
                msg = "network down sending 488"
                raise ConnectionError(msg)

    transport = _Failing488Transport()
    adapter = await _build_adapter(transport, load_media_config({}))
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    class _BindFailSession(_FakeSipDtlsSession):
        async def prepare(self, *, local_address: str, local_port: int = 0) -> None:
            self.prepared = False
            msg = "UDP bind failed"
            raise OSError(msg)

    with (
        patch("hermes_voip.adapter.SipDtlsMediaSession", _BindFailSession),
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=_fake_engine()),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: _FakeSipDtlsSession.last is not None)
        session = _FakeSipDtlsSession.last
        assert session is not None
        # The session is closed despite the 488 transmit raising (no leaked socket).
        await _until(lambda: session.closed)
        await asyncio.sleep(0.01)

    assert call_id not in adapter._call_loops
    assert session.closed, (
        "a pre-answer bind failure must close the session even if the 488 send raises"
    )


@pytest.mark.asyncio
async def test_sip_dtls_peer_bye_during_handshake_ends_call_no_own_bye() -> None:
    """A peer BYE during the handshake ends the call cleanly (we don't BYE back).

    The dialog is registered at answer-time, so a peer BYE mid-handshake routes to the
    guard, which answers 200 OK. When the handshake then fails, the abort sees the
    dialog already ended by the peer and does NOT send its own (redundant) BYE — it
    just releases media. (A BYE answered by 200 OK, then no second BYE from us.)
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_manager(
        transport, load_media_config({})
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    _FakeSipDtlsSession.fail_handshake = True
    _FakeSipDtlsSession.gate = asyncio.Event()
    _FakeSipDtlsSession.in_handshake = asyncio.Event()
    engine = _fake_engine()
    with (
        patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
        patch("hermes_voip.adapter._ANSWERED_ABORT_ACK_TIMEOUT_S", 0.2),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        assert _FakeSipDtlsSession.in_handshake is not None
        await _FakeSipDtlsSession.in_handshake.wait()
        ok = _sent_200_ok(transport)
        # The caller hangs up mid-handshake: the peer BYE routes to the guard.
        await _deliver_in_dialog(
            manager, _in_dialog_request("BYE", ok, call_id=call_id)
        )
        # The guard answered the peer BYE with 200 OK.
        assert any(
            m.startswith("SIP/2.0 200") and "BYE" in m for m in transport.sent
        ), "the peer BYE during the handshake must be answered 200 OK"
        # Release the handshake (it fails); the abort must NOT send its own BYE.
        _FakeSipDtlsSession.gate.set()
        session = _FakeSipDtlsSession.last
        assert session is not None
        await _until(lambda: session.closed)
        await asyncio.sleep(0.4)  # let the bounded fallback window pass

    assert call_id not in adapter._call_loops
    # We never send our OWN BYE — the peer already ended the dialog.
    our_byes = [m for m in transport.sent if m.startswith("BYE ")]
    assert our_byes == [], "must not BYE a dialog the peer already BYE'd"


@pytest.mark.asyncio
async def test_sip_dtls_no_media_engine_before_fingerprint_verified() -> None:
    """No media engine / RTP path is constructed before the handshake verifies (§5).

    The security invariant of registering the dialog early: it is SIGNALING only. The
    RtpMediaTransport (and thus any inbound_audio/RTP path) must NOT be constructed
    until run_handshake returns. We hold the handshake open and assert the engine ctor
    has not been called while it is in progress.
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_manager(
        transport, load_media_config({})
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    _FakeSipDtlsSession.gate = asyncio.Event()
    _FakeSipDtlsSession.in_handshake = asyncio.Event()
    engine = _fake_engine()

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    with (
        patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
        patch(
            "hermes_voip.adapter.RtpMediaTransport", return_value=engine
        ) as engine_ctor,
        patch(
            "hermes_voip.adapter.CallLoop", return_value=MagicMock(run=_blocking_run)
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        try:
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            assert _FakeSipDtlsSession.in_handshake is not None
            await _FakeSipDtlsSession.in_handshake.wait()
            # The dialog is registered (signaling) but the handshake has NOT returned —
            # the engine MUST NOT be built yet (no media before fingerprint verify).
            ok = _sent_200_ok(transport)
            routing = await _deliver_in_dialog(
                manager, _in_dialog_request("ACK", ok, call_id=call_id)
            )
            from hermes_voip.manager import InDialog  # noqa: PLC0415

            assert isinstance(routing, InDialog), "dialog registered (signaling) early"
            assert engine_ctor.call_count == 0, (
                "the media engine must NOT be built before the handshake verifies "
                "the peer fingerprint (RFC 5763 §5)"
            )
            # Release the handshake → success → NOW the engine is built.
            assert _FakeSipDtlsSession.gate is not None
            _FakeSipDtlsSession.gate.set()
            await _until(lambda: call_id in adapter._call_loops)
            assert engine_ctor.call_count == 1
        finally:
            in_call.set()
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sip_dtls_ack_during_handshake_then_success_normal_call() -> None:
    """An ACK during a SUCCEEDING handshake confirms the dialog; the call proceeds.

    The happy path through the answer-time-registration window: ACK arrives mid-
    handshake (routes to the guard), the handshake succeeds, the real CallSession is
    registered (upgrading the guard), and the call runs — no BYE, a live CallLoop.
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_manager(
        transport, load_media_config({})
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    _FakeSipDtlsSession.gate = asyncio.Event()
    _FakeSipDtlsSession.in_handshake = asyncio.Event()
    engine = _fake_engine()

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    try:
        with (
            patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
            patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            assert _FakeSipDtlsSession.in_handshake is not None
            await _FakeSipDtlsSession.in_handshake.wait()
            ok = _sent_200_ok(transport)
            await _deliver_in_dialog(
                manager, _in_dialog_request("ACK", ok, call_id=call_id)
            )
            assert _FakeSipDtlsSession.gate is not None
            _FakeSipDtlsSession.gate.set()
            # The call proceeds: a live CallLoop is registered, no BYE.
            await _until(lambda: call_id in adapter._call_loops)
            assert not any(m.startswith("BYE ") for m in transport.sent), (
                "a successful handshake must not BYE the call"
            )
    finally:
        in_call.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sip_dtls_peer_bye_during_successful_handshake_no_callloop() -> None:
    """A peer BYE during a SUCCESSFUL handshake ⇒ no CallLoop starts (codex r3).

    The peer hangs up mid-handshake (the guard answers 200 + records peer-ended), but
    the handshake then SUCCEEDS. The success path must NOT start a CallLoop on a dialog
    the peer already terminated — it stops media and ends cleanly. No BYE from us (the
    peer already BYE'd), no live CallLoop.
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_manager(
        transport, load_media_config({})
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    # Handshake SUCCEEDS, but gated so the peer BYE lands mid-handshake.
    _FakeSipDtlsSession.gate = asyncio.Event()
    _FakeSipDtlsSession.in_handshake = asyncio.Event()
    engine = _fake_engine()

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    try:
        with (
            patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
            patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            assert _FakeSipDtlsSession.in_handshake is not None
            await _FakeSipDtlsSession.in_handshake.wait()
            ok = _sent_200_ok(transport)
            # The caller hangs up mid-handshake (guard answers 200, marks peer-ended).
            await _deliver_in_dialog(
                manager, _in_dialog_request("BYE", ok, call_id=call_id)
            )
            # Let the handshake SUCCEED.
            assert _FakeSipDtlsSession.gate is not None
            _FakeSipDtlsSession.gate.set()
            session = _FakeSipDtlsSession.last
            assert session is not None
            # Media is torn down and the call ends — NO CallLoop on a peer-ended dialog.
            await _until(lambda: engine.stop.await_count > 0)
            await asyncio.sleep(0.05)
            assert call_id not in adapter._call_loops, (
                "must NOT start a CallLoop on a dialog the peer already BYE'd"
            )
            # We never send our OWN BYE (the peer's BYE was answered 200 by the guard).
            assert not any(m.startswith("BYE ") for m in transport.sent), (
                "no redundant BYE — the peer already terminated the dialog"
            )
    finally:
        in_call.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sip_dtls_peer_bye_during_abort_wait_no_double_bye() -> None:
    """A peer BYE arriving DURING the abort's ACK-wait ⇒ no double BYE (codex r3).

    The handshake fails with NO prior ACK, so the abort begins its bounded ACK-wait.
    The peer THEN sends a BYE (the guard answers 200, outcome = PEER_BYE). The abort
    must wake and send NO BYE of its own — exactly one BYE response (ours, the 200), no
    BYE request from us.
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_manager(
        transport, load_media_config({})
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    _FakeSipDtlsSession.fail_handshake = True
    engine = _fake_engine()
    with (
        patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
        # A long timeout so the wait is still in progress when we inject the BYE.
        patch("hermes_voip.adapter._ANSWERED_ABORT_ACK_TIMEOUT_S", 30.0),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        # Media is released synchronously; the abort is now waiting for the ACK.
        await _until(lambda: _FakeSipDtlsSession.last is not None)
        session = _FakeSipDtlsSession.last
        assert session is not None
        await _until(lambda: session.closed)
        ok = _sent_200_ok(transport)
        # No BYE yet — the dialog is unconfirmed and the abort is waiting.
        assert not any(m.startswith("BYE ") for m in transport.sent)
        # The peer now hangs up DURING the wait: the guard answers 200 + outcome
        # becomes PEER_BYE, so the abort must NOT send its own BYE.
        await _deliver_in_dialog(
            manager, _in_dialog_request("BYE", ok, call_id=call_id)
        )
        await asyncio.sleep(0.05)

    assert call_id not in adapter._call_loops
    # The guard answered the peer BYE with a 200; we sent NO BYE request of our own.
    assert any(m.startswith("SIP/2.0 200") and "BYE" in m for m in transport.sent)
    assert not any(m.startswith("BYE ") for m in transport.sent), (
        "a peer BYE during the abort wait must not produce a second (our) BYE"
    )


@pytest.mark.asyncio
async def test_answered_guard_trailing_bye_after_ack_wins_over_ack_confirmed() -> None:
    """A trailing BYE after an ACK ⇒ PEER_BYE, not ACK_CONFIRMED (codex r4).

    A peer can ACK (confirming the dialog) and THEN BYE within the same abort window —
    e.g. when BOTH sides' media handshake failed. The transport drives one sequential
    read loop, so the BYE is a separate, not-yet-read frame when the ACK wakes
    ``wait_outcome``; a snapshot taken at that wake returns ``ACK_CONFIRMED`` and the
    abort then sends OUR BYE — which collides with the peer's just-arriving BYE (glare).

    The settle-grace makes ``wait_outcome`` wait briefly, after an ACK confirms, for a
    trailing BYE so the BYE wins (no second BYE from us). This drives the guard's
    ``wait_outcome`` directly so the ordering is deterministic: ACK first, then assert
    the decision is still pending (it must NOT have committed to ACK_CONFIRMED), then
    deliver the BYE and assert ``PEER_BYE``.
    """
    from hermes_voip.adapter import (  # noqa: PLC0415 — adapter-internal guard
        _AnsweredDialogGuard,
        _DialogOutcome,
    )
    from hermes_voip.dialog import Dialog  # noqa: PLC0415

    transport = _FakeTransport()
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))
    dialog = Dialog.from_inbound_invite(
        invite,
        local_tag=new_tag(),
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
    )
    guard = _AnsweredDialogGuard(dialog=dialog, transport=transport, call_id=call_id)
    # A synthetic 200 OK so the in-dialog ACK/BYE echo a consistent To/From (RFC 3261
    # §12) — exactly what a gateway sends after our answer.
    ok = SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS 203.0.113.7:5061;branch=z9hG4bKdlg\r\n"
        f"From: {invite.header('From')}\r\n"
        f"To: {invite.header('To')};tag={new_tag()}\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    ack = _in_dialog_request("ACK", ok, call_id=call_id)
    bye = _in_dialog_request("BYE", ok, call_id=call_id)

    task: asyncio.Task[_DialogOutcome] = asyncio.create_task(
        guard.wait_outcome(timeout=30.0)
    )
    for _ in range(5):  # let wait_outcome reach its phase-1 wait
        await asyncio.sleep(0)
    await guard.handle_request(ack)
    for _ in range(5):  # the ACK wakes phase-1; the guard now grace-waits for a BYE
        await asyncio.sleep(0)
    assert not task.done(), (
        "wait_outcome committed to ACK_CONFIRMED on the ACK before a trailing BYE "
        "could win — this reopens the double-BYE glare (codex r4)"
    )
    await guard.handle_request(bye)
    assert await task is _DialogOutcome.PEER_BYE
    # The guard answered the peer BYE 200; the abort (caller) sends no BYE of its own.
    assert any(m.startswith("SIP/2.0 200") and "BYE" in m for m in transport.sent)


@pytest.mark.asyncio
async def test_answered_guard_ack_without_trailing_bye_confirms_after_grace() -> None:
    """ACK with no trailing BYE ⇒ ACK_CONFIRMED after the (bounded) settle grace.

    The control for the settle-grace fix: when the peer only ACKs, ``wait_outcome``
    still returns ``ACK_CONFIRMED`` (so the abort sends its necessary BYE) — the grace
    expires and does not change the outcome. A tiny grace keeps the test fast.
    """
    from hermes_voip.adapter import (  # noqa: PLC0415 — adapter-internal guard
        _AnsweredDialogGuard,
        _DialogOutcome,
    )
    from hermes_voip.dialog import Dialog  # noqa: PLC0415

    transport = _FakeTransport()
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))
    dialog = Dialog.from_inbound_invite(
        invite,
        local_tag=new_tag(),
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
    )
    guard = _AnsweredDialogGuard(dialog=dialog, transport=transport, call_id=call_id)
    ok = SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS 203.0.113.7:5061;branch=z9hG4bKdlg\r\n"
        f"From: {invite.header('From')}\r\n"
        f"To: {invite.header('To')};tag={new_tag()}\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    ack = _in_dialog_request("ACK", ok, call_id=call_id)
    with patch("hermes_voip.adapter._ACK_BYE_SETTLE_S", 0.01):
        await guard.handle_request(ack)
        assert await guard.wait_outcome(timeout=30.0) is _DialogOutcome.ACK_CONFIRMED


@pytest.mark.asyncio
async def test_answered_guard_drops_a_header_incomplete_in_dialog_bye(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A header-incomplete in-dialog BYE must not crash the answer-handshake guard.

    ADR-0081 class (build side): the guard's handle_request runs INLINE in the transport
    reader task (the InDialog branch awaits it directly). A peer BYE that PARSES but
    lacks a mandatory echo header (no Via) makes build_response(request, 200, ...)
    raise ValueError (RFC 3261 §8.2.6). Before the fix that escaped the reader and
    tore down the whole connection — a DoS against every other active call. The BYE
    must be dropped fail-closed: no raise, no (broken) 200 sent, the dialog
    still marked peer-ended, and a non-PII WARNING logged.
    """
    from hermes_voip.adapter import _AnsweredDialogGuard  # noqa: PLC0415
    from hermes_voip.dialog import Dialog  # noqa: PLC0415

    transport = _FakeTransport()
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))
    local_tag = new_tag()
    dialog = Dialog.from_inbound_invite(
        invite,
        local_tag=local_tag,
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
    )
    guard = _AnsweredDialogGuard(dialog=dialog, transport=transport, call_id=call_id)
    # A peer BYE that parses but carries NO Via — build_response(200) has none to echo.
    bye_no_via = SipRequest.parse(
        "BYE sip:1000@127.0.0.1:5061 SIP/2.0\r\n"
        f"From: {invite.header('From')}\r\n"
        f"To: {invite.header('To')};tag={local_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 2 BYE\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    with caplog.at_level(logging.WARNING, logger="hermes_voip.adapter"):
        # Must NOT raise (before the fix this propagated ValueError out of the reader).
        await guard.handle_request(bye_no_via)

    assert transport.sent == [], (
        "an un-answerable BYE must not send a (broken) 200 — it is dropped fail-closed"
    )
    assert guard.peer_ended, (
        "the peer BYE still marks the dialog peer-ended even when its 200 can't build"
    )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "dropping the un-answerable BYE must emit a WARNING (rule 37)"
    assert "ValueError" in warnings[0].getMessage()
    # Non-PII (rule 34): the request's identity must not leak into the log.
    assert call_id not in caplog.text, "Call-ID must not appear in the log (PII guard)"
    assert "sip:2000@" not in caplog.text, "caller URI must not appear in the log"


@pytest.mark.asyncio
async def test_answered_guard_answers_a_well_formed_bye() -> None:
    """Positive control: a well-formed in-dialog BYE is still answered 200 OK.

    The fail-closed guard must not change the happy path — a BYE carrying its Via (and
    the other echo headers) is answered 200 OK exactly as before.
    """
    from hermes_voip.adapter import _AnsweredDialogGuard  # noqa: PLC0415
    from hermes_voip.dialog import Dialog  # noqa: PLC0415

    transport = _FakeTransport()
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))
    dialog = Dialog.from_inbound_invite(
        invite,
        local_tag=new_tag(),
        local_contact="<sip:1000@127.0.0.1:5061;transport=tls>",
        local_sent_by="127.0.0.1:5061",
        transport="TLS",
    )
    guard = _AnsweredDialogGuard(dialog=dialog, transport=transport, call_id=call_id)
    ok = SipResponse.parse(
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS 203.0.113.7:5061;branch=z9hG4bKdlg\r\n"
        f"From: {invite.header('From')}\r\n"
        f"To: {invite.header('To')};tag={new_tag()}\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    bye = _in_dialog_request("BYE", ok, call_id=call_id)
    await guard.handle_request(bye)
    assert any(m.startswith("SIP/2.0 200") and "BYE" in m for m in transport.sent), (
        "a well-formed BYE must still be answered 200 OK"
    )
    assert guard.peer_ended


@pytest.mark.asyncio
async def test_sip_dtls_abort_is_non_blocking_frees_admission_slot() -> None:
    """The post-200 abort does NOT hold the handler/admission slot during the wait.

    The handshake fails with no ACK; the abort's bounded ACK-wait runs in a TRACKED
    BACKGROUND task. The inbound handler returns and the admission slot is freed
    promptly (well within the long ACK timeout), and the background task is registered
    so shutdown can cancel it. The fallback BYE still fires later from that task.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, load_media_config({}))
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SIP_DTLS_OFFER, call_id))

    _FakeSipDtlsSession.fail_handshake = True
    engine = _fake_engine()
    with (
        patch("hermes_voip.adapter.SipDtlsMediaSession", _FakeSipDtlsSession),
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
        # A long ACK timeout: if the abort blocked the handler, the admission slot
        # would still be held for ~30s. It must be freed long before that.
        patch("hermes_voip.adapter._ANSWERED_ABORT_ACK_TIMEOUT_S", 30.0),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: _FakeSipDtlsSession.last is not None)
        session = _FakeSipDtlsSession.last
        assert session is not None
        await _until(lambda: session.closed)
        # The admission slot is freed promptly — the abort wait does NOT block the
        # handler (it runs in the background). 1.0s ≪ the 30s ACK timeout.
        await _until(
            lambda: call_id not in adapter._admitted_calls,
            timeout=1.0,
        )
        # A tracked background teardown task exists (so shutdown can cancel/await it).
        assert call_id in adapter._call_tasks
        assert adapter._call_tasks[call_id]
        # Tear down via disconnect (cancels the bg task within the bounded shutdown).
        await adapter.disconnect()
        await asyncio.sleep(0)

    assert call_id not in adapter._call_loops


@pytest.mark.asyncio
async def test_plain_offer_never_touches_sip_dtls_branch() -> None:
    """A plain RTP/AVP offer never builds a SIP-DTLS session (no regression)."""
    transport = _FakeTransport()
    # require_secure_media off: this test offers plain RTP/AVP to prove it falls to
    # the SDES/plain path (never the SIP-DTLS branch); the secure-media mandate
    # (ADR-0070) would otherwise 488 it before any media-path selection.
    adapter = await _build_adapter(
        transport, load_media_config({"HERMES_VOIP_REQUIRE_SECURE_MEDIA": "false"})
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_PLAIN_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    dtls_ctor = MagicMock()
    engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        start_rtcp=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=20002,
        inbound_sample_rate=8_000,
    )
    try:
        with (
            patch("hermes_voip.adapter.SipDtlsMediaSession", dtls_ctor),
            patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)

            ok = _sent_200_ok(transport)
            answer = SessionDescription.parse(ok.body)
            assert answer.audio is not None
            assert not answer.audio.is_sip_dtls
            assert answer.audio.fingerprint is None
            dtls_ctor.assert_not_called()
    finally:
        in_call.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_webrtc_offer_never_touches_sip_dtls_branch() -> None:
    """A WebRTC SAVPF offer keeps using the ICE/WebRTC path, never SIP-DTLS."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, load_media_config({}))
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_WEBRTC_OFFER, call_id))

    dtls_ctor = MagicMock()
    webrtc_ctor = MagicMock(side_effect=RuntimeError("stop before answering"))
    with (
        patch("hermes_voip.adapter.SipDtlsMediaSession", dtls_ctor),
        patch("hermes_voip.adapter.WebRtcMediaSession", webrtc_ctor),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        # The WebRTC branch is taken (its ctor is reached); the SIP-DTLS one is not.
        await _until(lambda: webrtc_ctor.called)
        await asyncio.sleep(0)
        dtls_ctor.assert_not_called()


@pytest.mark.asyncio
async def test_sdes_offer_never_touches_sip_dtls_branch() -> None:
    """An RTP/SAVP (SDES) offer keeps the SDES path; the DTLS branch is skipped."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, load_media_config({}))
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_sdes_offer(), call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    dtls_ctor = MagicMock()
    engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        # SDES (secured) path now activates RTCP over SRTCP (ADR-0066); the real engine
        # awaits start_rtcp, so the fake must provide an AsyncMock.
        start_rtcp=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=20002,
        inbound_sample_rate=8_000,
    )
    try:
        with (
            patch("hermes_voip.adapter.SipDtlsMediaSession", dtls_ctor),
            patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)

            ok = _sent_200_ok(transport)
            answer = SessionDescription.parse(ok.body)
            assert answer.audio is not None
            # An SDES (RTP/SAVP + a=crypto) answer — never DTLS-SRTP.
            assert not answer.audio.is_sip_dtls
            assert answer.audio.is_srtp
            assert "a=crypto" in ok.body
            dtls_ctor.assert_not_called()
    finally:
        in_call.set()
        await asyncio.sleep(0)
