"""VoipAdapter SIP-over-WSS signalling wiring (ADR-0038).

``adapter._establish()`` selects the signalling transport by
``gateway_cfg.transport``: ``tls`` builds ``SipOverTlsTransport`` (unchanged),
``wss`` builds ``WssSipTransport`` (RFC 7118). Both are wired with the SAME
``on_new_call`` / ``on_unroutable`` / ``on_connection_lost`` observers +
``bind_manager``, so an INVITE arriving over WSS flows through the identical
inbound handling. On a wss gateway the inbound UAS dialog + the agent-facing
call-context advertise the ``WSS`` Via transport (not the hardcoded ``TLS``),
and a SAVPF/Opus/DTLS offer is answered on the existing WebRTC media path.

``VoipAdapter`` subclasses the real ``gateway.platforms.base.BasePlatformAdapter``
at runtime, so this module requires the optional ``hermes`` extra (and the SAVPF
routing test needs the ``webrtc`` extra) and skips cleanly without them — it runs
in the hermes-contract CI job. The SIP/RTP/WebRTC/provider collaborators are
fakes: no live gateway, no real ICE/DTLS. Fakes only (``pbx.example.test`` / ext
``1000`` / ``127.0.0.1``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_voip.config import ExtensionConfig, GatewayConfig, load_media_config
from hermes_voip.manager import NewCall, RegistrationManager
from hermes_voip.message import SipRequest, SipResponse, new_call_id, new_tag
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.tts import TtsStream
from hermes_voip.sdp import Fingerprint, IceCandidate, SessionDescription, SetupRole

if TYPE_CHECKING:
    from hermes_voip.adapter import VoipAdapter


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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Fake SipTransport + CallSignaling.

    Mirrors the WSS transport's seams: a ``.invalid`` sent-by and a
    ``transport=ws`` Contact (so the manager builds an RFC-7118 Contact), and it
    captures both the sent messages and the observer callbacks it was wired with.
    """

    def __init__(self, *, local_sent_by: str = "abc123.invalid") -> None:
        self._local_sent_by = local_sent_by
        self.sent: list[str] = []
        self._calls: dict[str, object] = {}
        # The observer seams the adapter must wire identically on both transports.
        self.on_new_call: object = None
        self.on_unroutable: object = None
        self.on_connection_lost: object = None
        self.bound_manager: object = None

    @property
    def local_sent_by(self) -> str:
        return self._local_sent_by

    def contact_uri(self, extension: str) -> str:
        return (
            f"<sip:{extension}@{self._local_sent_by};transport=ws>"
            f';reg-id=1;+sip.instance="<urn:uuid:00000000-0000-4000-8000-000000000000>"'
        )

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def connect(self) -> None:
        """No-op."""

    async def aclose(self) -> None:
        """No-op."""

    def bind_manager(self, manager: object) -> None:
        self.bound_manager = manager

    def add_call(self, call_id: str, sink: object) -> None:
        self._calls[call_id] = sink

    def remove_call(self, call_id: str, sink: object | None = None) -> None:
        self._calls.pop(call_id, None)


class _FakeTtsStream:
    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[PcmFrame]:
        empty: list[PcmFrame] = []
        for frame in empty:
            yield frame

    async def cancel(self) -> None:
        """No-op."""


class _FakeTTS:
    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        # The fake yields no frames; it is structurally an async-iterable but not a
        # nominal TtsStream — the test never touches TTS output (rule 20: justified).
        return _FakeTtsStream()  # type: ignore[return-value]


class _FakeASR:
    async def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[object]:
        async for _ in audio:
            pass
        empty: list[object] = []
        for chunk in empty:
            yield chunk


class _FakeGuard:
    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            score=0.0,
            degraded=False,
            normalized_text=text,
            reasons=(),
        )


def _fake_providers() -> Providers:
    # Duck-typed provider fakes (the runtime Protocols are not nominally satisfied);
    # the WSS-signalling path under test never exercises real ASR/TTS/guard (rule 20).
    return Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())  # type: ignore[arg-type]


_FAKE_ENV_WSS = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
    "HERMES_SIP_TRANSPORT": "wss",
}

_FAKE_ENV_TLS = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
    "HERMES_SIP_TRANSPORT": "tls",
}


def _gateway_config(transport: str, *, ws_path: str = "/ws") -> GatewayConfig:
    return GatewayConfig(
        host="pbx.example.test",
        port=443 if transport == "wss" else 5061,
        transport=transport,
        expires=3600,
        user_agent="hermes-voip-test",
        ws_path=ws_path,
        extensions=(
            ExtensionConfig(
                index=0, extension="1000", username="1000", password="fake"
            ),
        ),
        default_index=0,
    )


def _ext_config() -> ExtensionConfig:
    return ExtensionConfig(index=0, extension="1000", username="1000", password="fake")


# ---------------------------------------------------------------------------
# 1 + 2: _establish() selects the transport class by gateway_cfg.transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_establish_selects_wss_transport_when_transport_wss() -> None:
    """transport=wss ⇒ _establish() builds WssSipTransport (NOT the TLS transport)."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    transport = _FakeTransport()
    wss_ctor = MagicMock(return_value=transport)
    tls_ctor = MagicMock(name="SipOverTlsTransport")
    manager = RegistrationManager(_gateway_config("wss"), transport)
    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV_WSS))

    with (
        patch(
            "hermes_voip.adapter.load_gateway_config",
            return_value=_gateway_config("wss"),
        ),
        patch(
            "hermes_voip.adapter.load_media_config", return_value=load_media_config({})
        ),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", tls_ctor),
        patch("hermes_voip.adapter.WssSipTransport", wss_ctor),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()

    # The WSS transport was constructed; the TLS transport was NOT.
    wss_ctor.assert_called_once()
    tls_ctor.assert_not_called()

    kwargs = wss_ctor.call_args.kwargs
    assert kwargs["host"] == "pbx.example.test"
    assert kwargs["port"] == 443
    # The WS upgrade path is threaded from config (ADR-0038 §3).
    assert kwargs["ws_path"] == "/ws"
    # wss:// is WebSocket-over-TLS — the same SSL context the TLS path verifies with.
    assert kwargs["ssl_context"] is not None
    # The SAME inbound observer seams the TLS path uses (ADR-0038 §1) — wired so a
    # WSS-arriving INVITE reaches the identical inbound handling.
    assert kwargs["on_new_call"] == adapter._on_inbound_invite
    assert kwargs["on_unroutable"] == adapter._on_unroutable
    assert kwargs["on_connection_lost"] == adapter._on_connection_lost
    # The manager was bound to the WSS transport (demux/routing).
    assert transport.bound_manager is manager


@pytest.mark.asyncio
async def test_establish_selects_tls_transport_when_transport_tls() -> None:
    """transport=tls ⇒ _establish() builds SipOverTlsTransport (unchanged, no WSS)."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    transport = _FakeTransport(local_sent_by="127.0.0.1:5061")
    tls_ctor = MagicMock(return_value=transport)
    wss_ctor = MagicMock(name="WssSipTransport")
    manager = RegistrationManager(_gateway_config("tls"), transport)
    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV_TLS))

    with (
        patch(
            "hermes_voip.adapter.load_gateway_config",
            return_value=_gateway_config("tls"),
        ),
        patch(
            "hermes_voip.adapter.load_media_config", return_value=load_media_config({})
        ),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", tls_ctor),
        patch("hermes_voip.adapter.WssSipTransport", wss_ctor),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()

    tls_ctor.assert_called_once()
    wss_ctor.assert_not_called()
    # The TLS path does NOT pass a ws_path (it has no WebSocket upgrade path).
    assert "ws_path" not in tls_ctor.call_args.kwargs


# ---------------------------------------------------------------------------
# 3 + 4: a SAVPF INVITE over a WSS gateway routes to the is_webrtc path and the
# inbound dialog + answer advertise the WSS Via transport (not TLS).
# ---------------------------------------------------------------------------

pytest.importorskip("OpenSSL", reason="webrtc extra (pyOpenSSL) not installed")


_WEBRTC_OFFER = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=audio 50000 UDP/TLS/RTP/SAVPF 111 0\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
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


def _make_wss_invite(offer: str, call_id: str) -> str:
    """An inbound INVITE arriving over WSS (Via transport WSS, .invalid sent-by)."""
    content_length = len(offer.encode("utf-8"))
    ftag = new_tag()
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/WSS peer9x.invalid;branch=z9hG4bKfake;rport\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:9999@pbx.example.test>;tag={ftag}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:9999@peer9x.invalid;transport=ws>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{offer}"
    )


class _FakeWebRtcSession:
    """Fake WebRtcMediaSession: skips real ICE/DTLS, returns canned SRTP sessions."""

    last: _FakeWebRtcSession | None = None

    def __init__(
        self,
        *,
        offer_setup: SetupRole | None = None,
        stun_urls: tuple[str, ...] = (),
        built_for_outbound: bool = False,
        **_kw: object,
    ) -> None:
        self.offer_setup = offer_setup
        self.prepared = False
        self.handshake_args: dict[str, object] | None = None
        self.built_for_outbound = built_for_outbound
        self.ice = MagicMock(name="ice_pipe")
        _FakeWebRtcSession.last = self

    @classmethod
    def for_outbound_offer(cls, **_kw: object) -> _FakeWebRtcSession:
        """Mirror WebRtcMediaSession.for_outbound_offer (ADR-0049): the offerer side."""
        return cls(built_for_outbound=True)

    @property
    def setup_for_outbound(self) -> SetupRole:
        return SetupRole("active")

    async def prepare(self) -> None:
        self.prepared = True

    @property
    def setup(self) -> SetupRole:
        # The outbound offerer offers active (DTLS client); the inbound answerer
        # answers passive (DTLS server).
        return SetupRole("active" if self.built_for_outbound else "passive")

    @property
    def fingerprint(self) -> Fingerprint:
        return Fingerprint(algorithm="sha-256", value=":".join(["AB"] * 32))

    @property
    def ice_ufrag(self) -> str:
        return "ourUFRAGxx"

    @property
    def ice_pwd(self) -> str:
        return "ourPWDourPWDourPWDourPWD"

    @property
    def ice_candidates(self) -> list[IceCandidate]:
        return [
            IceCandidate(
                foundation="candidate:1",
                component=1,
                transport="UDP",
                priority=2130706431,
                address="127.0.0.1",
                port=51000,
                typ="host",
                raddr=None,
                rport=None,
            )
        ]

    async def run_handshake(self, **kwargs: object) -> tuple[object, object]:
        self.handshake_args = kwargs
        return (MagicMock(name="srtp_in"), MagicMock(name="srtp_out"))

    def derive_srtcp_sessions(self) -> tuple[object, object]:
        """The SRTCP (inbound, outbound) pair from the same DTLS export (ADR-0066)."""
        return (MagicMock(name="srtcp_in"), MagicMock(name="srtcp_out"))

    async def close(self) -> None:
        """No-op."""


async def _build_wss_adapter(transport: _FakeTransport) -> VoipAdapter:
    """A real VoipAdapter on a wss gateway wired to fakes + a real manager."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV_WSS))
    manager = RegistrationManager(_gateway_config("wss"), transport)
    with (
        patch(
            "hermes_voip.adapter.load_gateway_config",
            return_value=_gateway_config("wss"),
        ),
        patch(
            "hermes_voip.adapter.load_media_config", return_value=load_media_config({})
        ),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.WssSipTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()
    return adapter


def _sent_200_ok(transport: _FakeTransport) -> SipResponse:
    oks = [SipResponse.parse(m) for m in transport.sent if m.startswith("SIP/2.0 200")]
    assert oks, "the adapter did not send a 200 OK answer"
    return oks[-1]


@pytest.mark.asyncio
async def test_savpf_invite_over_wss_routes_to_webrtc_and_advertises_wss_via() -> None:
    """A SAVPF/Opus/DTLS INVITE over WSS reaches the is_webrtc path; dialog Via=WSS."""
    transport = _FakeTransport()
    adapter = await _build_wss_adapter(transport)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_wss_invite(_WEBRTC_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    fake_engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        # RTCP activation (ADR-0061): the inbound plain-RTP path starts RTCP after
        # connect(); the fake models the awaitable method + inert _rtcp_active flag.
        start_rtcp=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=0,
        inbound_sample_rate=16_000,
    )

    # Wrap the real extract_call_context so the call context is still built AND
    # the kwargs (incl. the transport token) the adapter passed are recorded.
    from hermes_voip.call_context import extract_call_context  # noqa: PLC0415

    extract_spy = MagicMock(wraps=extract_call_context)

    try:
        with (
            patch("hermes_voip.adapter.WebRtcMediaSession", _FakeWebRtcSession),
            patch("hermes_voip.adapter.RtpMediaTransport", return_value=fake_engine),
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
            patch("hermes_voip.adapter.extract_call_context", extract_spy),
        ):
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)

            # The is_webrtc branch ran: the WebRTC session was built + handshook.
            session = _FakeWebRtcSession.last
            assert session is not None
            assert session.prepared
            assert session.handshake_args is not None

            # The 200 OK is a SAVPF/Opus answer (the WebRTC media path).
            ok = _sent_200_ok(transport)
            answer = SessionDescription.parse(ok.body)
            assert answer.audio is not None
            assert answer.audio.is_webrtc
            assert answer.audio.codecs[0].encoding.lower() == "opus"
            # The 200 OK's own Via reflects the request's Via (RFC 3261 §8.2.6.2) —
            # WSS, copied from the inbound INVITE.
            assert "SIP/2.0/WSS" in (ok.header("Via") or "")

            # ADR-0038 §2: the agent-facing call context reports the WSS transport
            # (derived from gateway via_transport, NOT the hardcoded "TLS").
            extract_spy.assert_called_once()
            assert extract_spy.call_args.kwargs["transport"] == "WSS"
    finally:
        in_call.set()
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 5: outbound WebRTC origination over WSS (ADR-0049 — lifts ADR-0032 §5 / 0038 §4).
# place_call on a wss gateway sends OUR DTLS/ICE/Opus offer, not a 501 reject.
# ---------------------------------------------------------------------------


def _mark_registered(manager: RegistrationManager) -> None:
    """Mark the gateway's first extension registered so place_call can source it."""
    for state in manager._by_extension.values():
        state.registered = True


@pytest.mark.asyncio
async def test_place_call_over_wss_sends_webrtc_offer_invite() -> None:
    """Outbound over WSS now carries OUR WebRTC offer (ADR-0049 lifts the deferral).

    place_call on a wss gateway no longer returns 501: it sends an RFC-7118 INVITE
    (WSS Via) carrying a UDP/TLS/RTP/SAVPF offer with our DTLS fingerprint, a=setup,
    ICE creds + candidates, and the Opus rtpmap — never a TLS-token SDES INVITE.
    """
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    transport = _FakeTransport()
    manager = RegistrationManager(_gateway_config("wss"), transport)
    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV_WSS))
    with (
        patch(
            "hermes_voip.adapter.load_gateway_config",
            return_value=_gateway_config("wss"),
        ),
        patch(
            "hermes_voip.adapter.load_media_config", return_value=load_media_config({})
        ),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.WssSipTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()
        _mark_registered(manager)
        transport.sent.clear()  # drop the REGISTER; we only assert about the INVITE

        with patch("hermes_voip.adapter.WebRtcMediaSession", _FakeWebRtcSession):
            call_task = asyncio.ensure_future(adapter.place_call("1001"))
            try:
                # place_call awaits the 2xx; assert only the INVITE on the wire.
                await _until(
                    lambda: any(m.startswith("INVITE") for m in transport.sent)
                )
            finally:
                call_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await call_task

    invites = [m for m in transport.sent if m.startswith("INVITE")]
    assert invites, "no INVITE was sent over the WSS transport"
    invite = SipRequest.parse(invites[0])
    # The Via reflects the WSS transport (RFC 7118), never TLS.
    assert "SIP/2.0/WSS" in (invite.header("Via") or "")
    assert "SIP/2.0/TLS" not in (invite.header("Via") or "")
    # The body is a WebRTC offer (our own DTLS/ICE/Opus offer).
    offer = SessionDescription.parse(invite.body or "")
    assert offer.audio is not None
    assert offer.audio.is_webrtc
    assert offer.audio.fingerprint is not None
    assert offer.audio.setup is not None
    assert offer.audio.setup.value == "active"
    assert offer.audio.ice_ufrag is not None
    assert any(c.encoding.lower() == "opus" for c in offer.audio.codecs)
    # The outbound offerer is the ICE-controlling / DTLS-active side.
    session = _FakeWebRtcSession.last
    assert session is not None
    assert session.built_for_outbound is True


async def _capture_outbound_webrtc_offer() -> SessionDescription:
    """Drive place_call over WSS and return the parsed outbound WebRTC SDP offer.

    Shared setup for the offer-content assertions below: mirrors
    ``test_place_call_over_wss_sends_webrtc_offer_invite`` but returns the parsed
    offer so a test can assert on its codec menu.
    """
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    transport = _FakeTransport()
    manager = RegistrationManager(_gateway_config("wss"), transport)
    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV_WSS))
    with (
        patch(
            "hermes_voip.adapter.load_gateway_config",
            return_value=_gateway_config("wss"),
        ),
        patch(
            "hermes_voip.adapter.load_media_config", return_value=load_media_config({})
        ),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.WssSipTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()
        _mark_registered(manager)
        transport.sent.clear()

        with patch("hermes_voip.adapter.WebRtcMediaSession", _FakeWebRtcSession):
            call_task = asyncio.ensure_future(adapter.place_call("1001"))
            try:
                await _until(
                    lambda: any(m.startswith("INVITE") for m in transport.sent)
                )
            finally:
                call_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await call_task

    invites = [m for m in transport.sent if m.startswith("INVITE")]
    assert invites, "no INVITE was sent over the WSS transport"
    offer = SessionDescription.parse(SipRequest.parse(invites[0]).body or "")
    assert offer.audio is not None
    return offer


@pytest.mark.asyncio
async def test_outbound_webrtc_offer_includes_dtmf_and_g711_fallback() -> None:
    """The outbound WebRTC offer carries telephone-event + G.711, not Opus alone.

    ADR-0049: the outbound offer must mirror the inbound answer menu
    (``_WEBRTC_SUPPORTED_ENCODINGS`` = opus, PCMU, PCMA, telephone-event) so that:
      * RFC 4733 DTMF (telephone-event) can negotiate — an Opus-only offer makes
        ``te_pt`` structurally always ``None`` and DTMF impossible on the call; and
      * a gateway that cannot do Opus can still answer G.711 (PCMU/PCMA).
    An Opus-only offer is the bug this test guards against.
    """
    offer = await _capture_outbound_webrtc_offer()
    assert offer.audio is not None
    encodings = {c.encoding.lower() for c in offer.audio.codecs}
    assert "opus" in encodings
    assert "telephone-event" in encodings, (
        "outbound WebRTC offer must offer telephone-event so RFC 4733 DTMF "
        "can negotiate"
    )
    assert "pcmu" in encodings, "outbound WebRTC offer must offer G.711 PCMU fallback"
    assert "pcma" in encodings, "outbound WebRTC offer must offer G.711 PCMA fallback"
    # Opus is offered first (preference order); DTMF is offered, not the only entry.
    assert offer.audio.codecs[0].encoding.lower() == "opus"
