"""VoipAdapter WebRTC (UDP/TLS/RTP/SAVPF) inbound branch (ADR-0032).

``VoipAdapter`` subclasses the real ``gateway.platforms.base.BasePlatformAdapter``
at runtime, so these tests require the optional ``hermes`` extra and skip cleanly
without it (they run in the hermes-contract CI job). The SIP/RTP/WebRTC/provider
collaborators are fakes — no real network, no real ML, no real ICE/DTLS — but the
on-the-wire SDP answer and the branch logic are exactly what production produces.

Verifies: a WebRTC offer (SAVPF profile + a=fingerprint + a=ice-ufrag/pwd) is
answered with a SAVPF 200 OK advertising Opus first, carrying our fingerprint /
setup / ICE creds / rtcp-mux (no a=crypto, no c= per RFC 5763); the media engine is
built over the ICE pipe with the DTLS-derived SRTP; and a plain RTP/AVP offer still
uses the SDES/G.711 path unchanged (no regression).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")
pytest.importorskip("OpenSSL", reason="webrtc extra (pyOpenSSL) not installed")

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


class _FakeTtsStream:
    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[PcmFrame]:
        # An empty async generator: yields nothing (the agent's TTS is stubbed
        # silent). Iterating an explicitly-typed empty list keeps the ``yield``
        # reachable so this stays an async generator without tripping warn_unreachable.
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
        return _FakeTtsStream()  # type: ignore[return-value]


class _FakeASR:
    async def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[object]:
        async for _ in audio:
            pass
        # Empty async generator (no transcripts): see _FakeTtsStream._gen.
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
    return Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())  # type: ignore[arg-type]


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

# A WebRTC BUNDLE offer with audio + an H.264 m=video (ADR-0044). Fakes only.
_WEBRTC_AV_OFFER = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0 1\r\n"
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
    "a=mid:0\r\n"
    "m=video 50000 UDP/TLS/RTP/SAVPF 99\r\n"
    "a=rtpmap:99 H264/90000\r\n"
    "a=fmtp:99 profile-level-id=42e01f;packetization-mode=1\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
    "a=mid:1\r\n"
)

# A minimal H.264 Annex-B source: SPS + PPS + a small IDR (synthetic bytes).
_FAKE_H264_ANNEX_B = (
    b"\x00\x00\x00\x01\x67\x01\x02"  # SPS (type 7)
    b"\x00\x00\x00\x01\x68\x03"  # PPS (type 8)
    b"\x00\x00\x00\x01\x65" + b"\x00" * 40  # IDR (type 5)
)


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


async def _build_adapter(transport: _FakeTransport) -> object:
    """A real VoipAdapter wired to fakes + a real RegistrationManager."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))
    manager = RegistrationManager(_gateway_config(), transport)
    with (
        patch(
            "hermes_voip.adapter.load_gateway_config", return_value=_gateway_config()
        ),
        patch(
            "hermes_voip.adapter.load_media_config", return_value=load_media_config({})
        ),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()
    return adapter


def _sent_200_ok(transport: _FakeTransport) -> SipResponse:
    oks = [SipResponse.parse(m) for m in transport.sent if m.startswith("SIP/2.0 200")]
    assert oks, "the adapter did not send a 200 OK answer"
    return oks[-1]


class _FakeWebRtcSession:
    """A fake WebRtcMediaSession: skips real ICE/DTLS, returns canned SRTP sessions.

    Records construction + handshake so the test can assert the branch ran, and
    exposes a fixed fingerprint / setup / ICE creds + candidate for the answer.
    """

    last: _FakeWebRtcSession | None = None

    def __init__(
        self,
        *,
        offer_setup: SetupRole | None,
        stun_urls: tuple[str, ...] = (),
        **_kw: object,
    ) -> None:
        self.offer_setup = offer_setup
        self.stun_urls = stun_urls
        self.prepared = False
        self.handshake_args: dict[str, object] | None = None
        self.closed = False
        self.ice = MagicMock(name="ice_pipe")
        # The video sender (ADR-0044) awaits ice.send(bytes); make it awaitable.
        self.ice.send = AsyncMock(return_value=None)
        self.video_ssrcs: list[int] = []
        _FakeWebRtcSession.last = self

    async def prepare(self) -> None:
        self.prepared = True

    @property
    def setup(self) -> SetupRole:
        return SetupRole("passive")

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
        # sdp.IceCandidate shape (address/typ/raddr/rport + render()).
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

    def derive_outbound_srtp_session(self, *, ssrc: int) -> object:
        # Records the video SSRC and returns a fake SRTP whose protect() yields
        # bytes the (fake) ICE pipe can send (ADR-0044).
        self.video_ssrcs.append(ssrc)
        srtp = MagicMock(name="video_srtp")
        srtp.protect = MagicMock(return_value=b"SRTP-video-packet")
        return srtp

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_webrtc_offer_yields_savpf_answer_with_opus_dtls_ice() -> None:
    """A SAVPF/Opus/DTLS offer yields a SAVPF answer (Opus first, DTLS + ICE)."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_WEBRTC_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    # The WebRTC engine reports the 16 kHz analysis rate (Opus decoded 48 kHz is
    # downsampled to 16 kHz for the VAD/STT pipeline — ADR-0032), so the VAD /
    # endpointer / barge-in window math (which only accepts 8/16 kHz) is valid.
    fake_engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        local_port=0,
        inbound_sample_rate=16_000,
    )

    try:
        with (
            patch("hermes_voip.adapter.WebRtcMediaSession", _FakeWebRtcSession),
            patch(
                "hermes_voip.adapter.RtpMediaTransport", return_value=fake_engine
            ) as engine_ctor,
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            adapter._on_inbound_invite(  # type: ignore[attr-defined]
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)  # type: ignore[attr-defined]

            ok = _sent_200_ok(transport)
            answer = SessionDescription.parse(ok.body)
            assert answer.audio is not None
            # SAVPF answer (DTLS-SRTP), Opus advertised FIRST.
            assert answer.audio.is_webrtc
            assert answer.audio.codecs[0].encoding.lower() == "opus"
            assert answer.audio.codecs[0].clock_rate == 48_000
            # DTLS-SRTP + ICE attributes present; SDES crypto absent (RFC 5763 §5).
            assert answer.audio.fingerprint is not None
            assert answer.audio.setup is not None
            assert answer.audio.setup.value in ("active", "passive")
            assert answer.audio.ice_ufrag == "ourUFRAGxx"
            assert answer.audio.rtcp_mux is True
            assert "a=crypto" not in ok.body
            # The handshake ran with the peer's fingerprint + ICE creds.
            session = _FakeWebRtcSession.last
            assert session is not None
            assert session.prepared
            assert session.handshake_args is not None
            peer_fp = session.handshake_args["peer_fingerprint"]
            assert isinstance(peer_fp, Fingerprint)
            assert session.handshake_args["peer_ice_ufrag"] == "peerUFRG"
            # The engine was constructed over the ICE pipe with the DTLS SRTP.
            kwargs = engine_ctor.call_args.kwargs
            assert kwargs["ice_transport"] is session.ice
            assert kwargs["srtp_inbound"] is not None
            assert kwargs["srtp_outbound"] is not None
    finally:
        in_call.set()
        await asyncio.sleep(0)


async def _build_adapter_with_media(
    transport: _FakeTransport, media_cfg: object
) -> object:
    """A real VoipAdapter whose media config is ``media_cfg`` (ADR-0044 video)."""
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
    return adapter


@pytest.mark.asyncio
async def test_webrtc_av_offer_answers_sendonly_video_and_starts_sender(
    tmp_path: object,
) -> None:
    """A configured video source yields a sendonly video answer + a live sender.

    A WebRTC audio+video offer with a configured source is answered with a BUNDLE'd
    sendonly ``m=video`` and starts the outbound H.264 sender over the ICE pipe.
    We answer sendonly (never sendrecv): the plugin discards inbound video, and
    sendrecv would solicit inbound video onto the BUNDLE 5-tuple and risk binding
    the inbound audio SRTP session to the video SSRC (ADR-0044).
    """
    import pathlib  # noqa: PLC0415

    assert isinstance(tmp_path, pathlib.Path)
    source = tmp_path / "clip.h264"
    source.write_bytes(_FAKE_H264_ANNEX_B)
    media_cfg = load_media_config({"HERMES_VOIP_VIDEO_SOURCE_PATH": str(source)})

    transport = _FakeTransport()
    adapter = await _build_adapter_with_media(transport, media_cfg)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_WEBRTC_AV_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    fake_engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        local_port=0,
        inbound_sample_rate=16_000,
    )
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
        ):
            adapter._on_inbound_invite(  # type: ignore[attr-defined]
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)  # type: ignore[attr-defined]

            ok = _sent_200_ok(transport)
            # The answer carries a BUNDLE'd, sendonly m=video sharing audio's DTLS.
            assert "m=video 9 UDP/TLS/RTP/SAVPF 99\r\n" in ok.body
            assert "a=group:BUNDLE 0 1\r\n" in ok.body
            video_block = ok.body.split("m=video", 1)[1]
            assert "a=sendonly" in video_block
            assert "a=sendrecv" not in video_block
            assert "a=inactive" not in video_block
            # The outbound video sender started: a distinct video SSRC was keyed
            # and the sender is registered for this call.
            session = _FakeWebRtcSession.last
            assert session is not None
            assert session.video_ssrcs, "no video SRTP session derived"
            await _until(lambda: call_id in adapter._video_senders)  # type: ignore[attr-defined]
            # The sender pushes SRTP-protected video bytes onto the ICE pipe.
            await _until(lambda: session.ice.send.await_count > 0)
    finally:
        in_call.set()
        # Tear the call down so the video sender task is stopped + cancelled.
        await adapter.disconnect()  # type: ignore[attr-defined]
        await asyncio.sleep(0)
    # After teardown the per-call video sender registration is gone.
    assert call_id not in adapter._video_senders  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_webrtc_video_ssrc_never_collides_with_audio(
    tmp_path: object,
) -> None:
    """The random video SSRC never equals the fixed audio SSRC 0xCAFEBABE (ADR-0044).

    A collision would let the inbound demux confuse audio and video streams on the
    shared BUNDLE 5-tuple. The generator must skip the audio SSRC; here randint is
    forced to return 0xCAFEBABE first, then a safe value — the keyed SSRC must be
    the safe value, proving the collision is excluded.
    """
    import pathlib  # noqa: PLC0415

    from hermes_voip.media.engine import _OUTBOUND_SSRC  # noqa: PLC0415

    assert isinstance(tmp_path, pathlib.Path)
    source = tmp_path / "clip.h264"
    source.write_bytes(_FAKE_H264_ANNEX_B)
    media_cfg = load_media_config({"HERMES_VOIP_VIDEO_SOURCE_PATH": str(source)})

    transport = _FakeTransport()
    adapter = await _build_adapter_with_media(transport, media_cfg)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_WEBRTC_AV_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    fake_engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        local_port=0,
        inbound_sample_rate=16_000,
    )
    _safe_ssrc = 0x0BADF00D
    # First draw collides with the audio SSRC; the generator must reject it and
    # redraw, landing on the safe value.
    randint_returns = iter([_OUTBOUND_SSRC, _safe_ssrc])
    try:
        with (
            patch("hermes_voip.adapter.WebRtcMediaSession", _FakeWebRtcSession),
            patch("hermes_voip.adapter.RtpMediaTransport", return_value=fake_engine),
            patch(
                "hermes_voip.adapter.random.randint",
                side_effect=lambda *_a, **_k: next(randint_returns),
            ),
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            adapter._on_inbound_invite(  # type: ignore[attr-defined]
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)  # type: ignore[attr-defined]
            session = _FakeWebRtcSession.last
            assert session is not None
            await _until(lambda: bool(session.video_ssrcs))
            assert _OUTBOUND_SSRC not in session.video_ssrcs
            assert session.video_ssrcs == [_safe_ssrc]
    finally:
        in_call.set()
        await adapter.disconnect()  # type: ignore[attr-defined]
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_webrtc_av_offer_no_source_answers_inactive_video(
    tmp_path: object,
) -> None:
    """With no video source, a video offer is answered a=inactive (no sender).

    RFC-correct BUNDLE decline (ADR-0044): the m=video line stays so the group is
    intact, but a=inactive and no outbound video sender is started.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport)  # load_media_config({}) → no source
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_WEBRTC_AV_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    fake_engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        local_port=0,
        inbound_sample_rate=16_000,
    )
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
        ):
            adapter._on_inbound_invite(  # type: ignore[attr-defined]
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)  # type: ignore[attr-defined]

            ok = _sent_200_ok(transport)
            assert "m=video 9 UDP/TLS/RTP/SAVPF 99\r\n" in ok.body
            video_block = ok.body.split("m=video", 1)[1]
            assert "a=inactive" in video_block
            assert "a=sendrecv" not in video_block
            # No outbound sender: no video SSRC keyed, none registered.
            session = _FakeWebRtcSession.last
            assert session is not None
            assert session.video_ssrcs == []
            assert call_id not in adapter._video_senders  # type: ignore[attr-defined]
    finally:
        in_call.set()
        await adapter.disconnect()  # type: ignore[attr-defined]
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_plain_offer_still_uses_sdes_path_no_webrtc() -> None:
    """A plain RTP/AVP offer never touches the WebRTC branch (no regression)."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_PLAIN_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    webrtc_ctor = MagicMock()
    try:
        with (
            patch("hermes_voip.adapter.WebRtcMediaSession", webrtc_ctor),
            patch(
                "hermes_voip.adapter.RtpMediaTransport",
                return_value=MagicMock(
                    connect=AsyncMock(return_value=True),
                    stop=AsyncMock(return_value=None),
                    local_port=20002,
                    inbound_sample_rate=8_000,
                ),
            ),
            patch(
                "hermes_voip.adapter.CallLoop",
                return_value=MagicMock(run=_blocking_run),
            ),
            patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
            patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
        ):
            adapter._on_inbound_invite(  # type: ignore[attr-defined]
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)  # type: ignore[attr-defined]

            ok = _sent_200_ok(transport)
            answer = SessionDescription.parse(ok.body)
            assert answer.audio is not None
            # Plain RTP/AVP answer — NOT WebRTC; the WebRTC session was never built.
            assert not answer.audio.is_webrtc
            assert answer.audio.fingerprint is None
            webrtc_ctor.assert_not_called()
    finally:
        in_call.set()
        await asyncio.sleep(0)


def test_webrtc_supported_encodings_never_drift_ahead_of_engine() -> None:
    """DRIFT GUARD (#84/ADR-0032): every WebRTC voice encoding is engine-carriable.

    Opus appears in the WebRTC advertised menu ONLY because the engine can carry it
    (Codec.OPUS at 48 kHz). This guard fails the moment the menu advertises a codec
    the engine cannot run — turning advertise-without-carry into a loud test failure,
    not silent dead audio. Mirrors the SDES drift guard in test_adapter.py.
    """
    from hermes_voip.adapter import _WEBRTC_SUPPORTED_ENCODINGS  # noqa: PLC0415
    from hermes_voip.media.engine import (  # noqa: PLC0415
        UnsupportedCodecError,
        codec_for_encoding,
    )

    # The rtpmap clock each WebRTC voice encoding is advertised at.
    rates = {"opus": 48_000, "pcmu": 8_000, "pcma": 8_000}
    voice = [e for e in _WEBRTC_SUPPORTED_ENCODINGS if e.lower() != "telephone-event"]
    assert voice, "the WebRTC menu must advertise at least one voice codec"
    for enc in voice:
        rate = rates[enc.lower()]
        try:
            codec_for_encoding(enc, rate)
        except UnsupportedCodecError as exc:  # pragma: no cover — guard failure mode
            pytest.fail(
                f"WebRTC menu advertises {enc}/{rate} the engine cannot carry: {exc}"
            )


# A WebRTC profile offer MISSING the mandatory DTLS fingerprint + ICE credentials
# (RFC 5763/8839). It must be REJECTED 488 BEFORE any 200 OK (ADR-0032 pre-answer
# validation; codex review BLOCKING-1).
_WEBRTC_OFFER_NO_FP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=audio 50000 UDP/TLS/RTP/SAVPF 111\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
)


def _sent_status(transport: _FakeTransport, code: int) -> bool:
    return any(m.startswith(f"SIP/2.0 {code}") for m in transport.sent)


@pytest.mark.asyncio
async def test_webrtc_offer_missing_fingerprint_is_rejected_before_answer() -> None:
    """A SAVPF offer with no a=fingerprint/ICE creds is 488'd BEFORE any 200 OK."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_WEBRTC_OFFER_NO_FP, call_id))

    webrtc_ctor = MagicMock()
    with patch("hermes_voip.adapter.WebRtcMediaSession", webrtc_ctor):
        adapter._on_inbound_invite(  # type: ignore[attr-defined]
            NewCall(registration=_ext_config(), invite=invite)
        )
        await _until(lambda: _sent_status(transport, 488))

    # A clean 488 reject: no 200 OK, no WebRTC session ever constructed, no call loop.
    assert _sent_status(transport, 488)
    assert not _sent_status(transport, 200)
    webrtc_ctor.assert_not_called()
    assert call_id not in adapter._call_loops  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_webrtc_missing_opus_dependency_is_rejected_before_answer() -> None:
    """A WebRTC/Opus call with no libopus/opuslib is 488'd BEFORE any 200 OK.

    Preflighting the Opus dependency before the answer prevents an answered-but-dead
    call (codex review BLOCKING-2). We simulate the missing dependency by patching the
    adapter's preflight import target to raise ImportError.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_WEBRTC_OFFER, call_id))

    webrtc_ctor = MagicMock()
    with (
        patch("hermes_voip.adapter.WebRtcMediaSession", webrtc_ctor),
        patch(
            "hermes_voip.media.opus.ensure_opus_available",
            side_effect=ImportError("libopus not found"),
        ),
    ):
        adapter._on_inbound_invite(  # type: ignore[attr-defined]
            NewCall(registration=_ext_config(), invite=invite)
        )
        await _until(lambda: _sent_status(transport, 488))

    assert _sent_status(transport, 488)
    assert not _sent_status(transport, 200)
    # The codec dependency is preflighted BEFORE the WebRtcMediaSession is built.
    webrtc_ctor.assert_not_called()
    assert call_id not in adapter._call_loops  # type: ignore[attr-defined]
