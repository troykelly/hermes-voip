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


async def _build_adapter(transport: _FakeTransport, media_cfg: object) -> object:
    """A real VoipAdapter wired to fakes + a real RegistrationManager + media_cfg."""
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
        if _FakeSipDtlsSession.fail_handshake:
            msg = "DTLS handshake failed (fingerprint mismatch)"
            raise ValueError(msg)
        return (MagicMock(name="srtp_in"), MagicMock(name="srtp_out"))

    async def close(self) -> None:
        self.closed = True


def _fake_engine() -> MagicMock:
    """A fake RtpMediaTransport sufficient for the SIP-DTLS branch + call tail."""
    return MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        # The secured DTLS path does NOT activate RTCP (no SRTCP transform) — the
        # flag stays inert and teardown logs no quality, like the WebRTC path.
        _rtcp_active=False,
        local_port=_SESSION_PORT,
        inbound_sample_rate=8_000,
    )


@pytest.fixture(autouse=True)
def _reset_fake_session() -> None:
    """Reset the fake session's class-level state between tests."""
    _FakeSipDtlsSession.last = None
    _FakeSipDtlsSession.fail_handshake = False


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
            adapter._on_inbound_invite(  # type: ignore[attr-defined]
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)  # type: ignore[attr-defined]

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
            adapter._on_inbound_invite(  # type: ignore[attr-defined]
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)  # type: ignore[attr-defined]
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

    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> _FakeSipDtlsSession:
        captured.update(kwargs)
        return _FakeSipDtlsSession(
            offer_setup=kwargs["offer_setup"],  # type: ignore[arg-type]
            answer_setup=str(kwargs.get("answer_setup", "auto")),
        )

    engine = _fake_engine()
    try:
        with (
            patch("hermes_voip.adapter.SipDtlsMediaSession", _capture),
            patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
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
            # The offered a=setup (actpass) and the knob both reached the session.
            assert captured["answer_setup"] == "passive"
            offered = captured["offer_setup"]
            assert isinstance(offered, SetupRole)
            assert offered.value == "actpass"
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
        adapter._on_inbound_invite(  # type: ignore[attr-defined]
            NewCall(registration=_ext_config(), invite=invite)
        )
        # The fall-through SDES/plain path rejects an unkeyable SAVP offer with 488;
        # await that terminal response deterministically.
        await _until(lambda: bool(transport.sent))
        await asyncio.sleep(0)
        # The DTLS session was never constructed — the knob disabled the branch.
        dtls_ctor.assert_not_called()
        # And it was NOT answered as DTLS-SRTP.
        assert not any("UDP/TLS/RTP/SAVP" in m for m in transport.sent)


@pytest.mark.asyncio
async def test_sip_dtls_handshake_failure_ends_call_no_plaintext() -> None:
    """A DTLS handshake failure ends the call and NEVER answers plaintext (§4).

    The 200 OK is the DTLS answer (it precedes the handshake), so the call IS
    answered SAVP; when the handshake then fails the call is torn down (no CallLoop
    on dead media) — and crucially no SECOND, plaintext answer is ever sent (no
    plaintext fallback, RFC 5763 §5 / rule 37). The session is closed.
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
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(  # type: ignore[attr-defined]
            NewCall(registration=_ext_config(), invite=invite)
        )
        # The handshake failure tears the call down: no live CallLoop is registered.
        await _until(lambda: _FakeSipDtlsSession.last is not None)
        session = _FakeSipDtlsSession.last
        assert session is not None
        await _until(lambda: session.closed)
        await asyncio.sleep(0.01)

    # No CallLoop was built on the dead (unkeyed) media.
    assert call_id not in adapter._call_loops  # type: ignore[attr-defined]
    # The ONLY answer sent was the single DTLS-SRTP 200 OK; there is no second,
    # plaintext answer. Count the answers (RTP/AVP body) — there must be none.
    answers = [m for m in transport.sent if m.startswith("SIP/2.0 200")]
    assert len(answers) == 1
    plaintext_answers = [
        m
        for m in transport.sent
        if m.startswith("SIP/2.0 200") and "RTP/AVP" in m and "SAVP" not in m
    ]
    assert plaintext_answers == [], "a handshake failure must NOT answer plaintext"


@pytest.mark.asyncio
async def test_plain_offer_never_touches_sip_dtls_branch() -> None:
    """A plain RTP/AVP offer never builds a SIP-DTLS session (no regression)."""
    transport = _FakeTransport()
    adapter = await _build_adapter(transport, load_media_config({}))
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
            adapter._on_inbound_invite(  # type: ignore[attr-defined]
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)  # type: ignore[attr-defined]

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
        adapter._on_inbound_invite(  # type: ignore[attr-defined]
            NewCall(registration=_ext_config(), invite=invite)
        )
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
        # SDES (secured) path does not activate RTCP.
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
            adapter._on_inbound_invite(  # type: ignore[attr-defined]
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)  # type: ignore[attr-defined]

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
