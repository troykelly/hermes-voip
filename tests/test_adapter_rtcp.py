"""VoipAdapter RTCP activation (ADR-0061 §"Adapter activation").

The RTCP engine capability (rtcp.py + engine.py, ADR-0061) ships dormant; the
adapter turns it live for each ``RtpMediaTransport`` it constructs. These tests
cover the adapter's activation contract:

* the per-call CNAME is an OPAQUE token, never the SIP host/extension or any PII
  (rule 34 — the RTCP SDES CNAME goes out on the wire);
* the RTCP transport is chosen from the negotiated mux (RFC 5761): a muxed offer
  rides the RTP socket; a non-muxed offer uses RTCP on RTP-port+1 (RFC 3550 §11);
* RTCP is activated ONLY on the cleartext plain-RTP path — a secured (SDES/SAVP)
  session is NOT activated, because the engine has no SRTCP (RFC 3711 §3.4)
  transform and cleartext RTCP on a secured 5-tuple is a protocol violation +
  cleartext metadata leak (this is the named, bounded scope of the activation lane);
* an operator kill-switch (``rtcp_enabled``) suppresses activation entirely.

The pure-helper tests need no SIP machinery. The integration test drives a real
inbound INVITE (plain RTP/AVP offer) through ``_on_inbound_invite`` with the REAL
media engine and asserts the live engine has RTCP active with its sibling socket
bound — proving the wire wiring, not just the planner.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_voip.adapter import (
    _mint_rtcp_cname,
    _plan_rtcp_activation,
)
from hermes_voip.config import (
    ExtensionConfig,
    GatewayConfig,
    load_media_config,
)
from hermes_voip.manager import NewCall, RegistrationManager
from hermes_voip.message import SipRequest, SipResponse, new_call_id, new_tag
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.tts import TtsStream
from hermes_voip.sdp import AudioMedia, SessionDescription

# ---------------------------------------------------------------------------
# Pure-helper unit tests: the activation planner + the opaque CNAME
# ---------------------------------------------------------------------------


def _audio_of(offer_text: str) -> AudioMedia:
    """Parse an SDP offer and return its (first) audio media section."""
    offer = SessionDescription.parse(offer_text)
    assert offer.audio is not None
    return offer.audio


_PLAIN_OFFER_NO_MUX = (
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

_PLAIN_OFFER_WITH_MUX = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 20000 RTP/AVP 0 8\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=rtcp-mux\r\n"
    "a=sendrecv\r\n"
)


def test_mint_rtcp_cname_is_opaque_not_pii() -> None:
    """The per-call CNAME is an opaque token — never the SIP host/extension/PII.

    The RTCP SDES CNAME is sent on the wire (RFC 3550 §6.5.1); on a PUBLIC repo it
    must not leak identity (rule 34). Two calls get distinct tokens (unlinkable),
    and the token contains neither the test host nor the extension.
    """
    a = _mint_rtcp_cname()
    b = _mint_rtcp_cname()
    assert a != b  # per-call, unlinkable
    assert "pbx.example.test" not in a
    assert "1000" not in a
    # Opaque + non-empty + ASCII (a valid SDES CNAME string).
    assert a
    assert a.isascii()
    # No '@host' identity form that would carry a hostname.
    assert "@" not in a


def test_plan_rtcp_activation_plain_non_muxed_uses_rtp_port_plus_one() -> None:
    """A plain non-muxed offer plans RTCP on the peer's RTP-port+1 (RFC 3550 §11)."""
    plan = _plan_rtcp_activation(
        _audio_of(_PLAIN_OFFER_NO_MUX),
        remote_address="203.0.113.7",
        secured=False,
        rtcp_enabled=True,
    )
    assert plan is not None
    assert plan.mux is False
    # RTCP rides RTP-port+1 when not multiplexed.
    assert plan.remote_rtcp_addr == ("203.0.113.7", 20001)


def test_plan_rtcp_activation_plain_muxed_uses_rtp_port() -> None:
    """A plain offer that requested rtcp-mux plans RTCP on the RTP port itself."""
    plan = _plan_rtcp_activation(
        _audio_of(_PLAIN_OFFER_WITH_MUX),
        remote_address="203.0.113.7",
        secured=False,
        rtcp_enabled=True,
    )
    assert plan is not None
    assert plan.mux is True
    assert plan.remote_rtcp_addr == ("203.0.113.7", 20000)


def test_plan_rtcp_activation_secured_is_suppressed() -> None:
    """A secured (SDES/SAVP) session does NOT activate RTCP (no SRTCP transform).

    The engine emits/parses cleartext RTCP only; activating on a secured 5-tuple
    would violate RFC 3711 §3.4 and leak the CNAME/timing in cleartext, so the
    planner returns None — the named, bounded scope of this activation lane.
    """
    plan = _plan_rtcp_activation(
        _audio_of(_PLAIN_OFFER_WITH_MUX),
        remote_address="203.0.113.7",
        secured=True,
        rtcp_enabled=True,
    )
    assert plan is None


def test_plan_rtcp_activation_kill_switch_suppresses() -> None:
    """The operator kill-switch (rtcp_enabled=False) suppresses all activation."""
    plan = _plan_rtcp_activation(
        _audio_of(_PLAIN_OFFER_NO_MUX),
        remote_address="203.0.113.7",
        secured=False,
        rtcp_enabled=False,
    )
    assert plan is None


def test_media_config_rtcp_enabled_defaults_true() -> None:
    """RTCP is on by default; an operator can disable it via the env knob."""
    assert load_media_config({}).rtcp_enabled is True
    assert (
        load_media_config({"HERMES_VOIP_RTCP_ENABLED": "false"}).rtcp_enabled is False
    )


# ---------------------------------------------------------------------------
# Integration: a real inbound plain-RTP INVITE activates RTCP on the live engine
# ---------------------------------------------------------------------------


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
    predicate: Callable[[], bool], *, timeout: float = 3.0, step: float = 0.005
) -> None:
    waited = 0.0
    while not predicate():
        await asyncio.sleep(step)
        waited += step
        if waited >= timeout:
            raise AssertionError("condition not met in time")


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


_PLAIN_INBOUND_OFFER = (
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


@pytest.mark.asyncio
async def test_inbound_plain_rtp_call_activates_rtcp_on_live_engine() -> None:
    """A real inbound plain-RTP INVITE turns RTCP live on the engine it builds.

    End-to-end through ``_on_inbound_invite`` with the REAL ``RtpMediaTransport``:
    the engine is connected, RTCP is started (loop task registered) on the
    non-muxed path (the offer has no a=rtcp-mux), and the sibling RTCP socket is
    bound on RTP-port+1 — i.e. the live wiring, not just the planner. The
    conversational loop is stubbed so the call parks while we inspect the engine.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_PLAIN_INBOUND_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    try:
        with (
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

            # The 200 OK is a plain RTP/AVP answer (no a=crypto) — the cleartext path.
            oks = [
                SipResponse.parse(m)
                for m in transport.sent
                if m.startswith("SIP/2.0 200")
            ]
            assert oks
            assert "a=crypto" not in oks[-1].body

            # Reach the LIVE engine and assert RTCP was activated on it.
            session = adapter._call_sessions[call_id]  # type: ignore[attr-defined]
            engine = session._media  # white-box: the concrete RtpMediaTransport
            assert engine._rtcp_task is not None  # the run_rtcp loop is live
            assert engine._rtcp_active is True  # inbound muxed-demux engaged
            assert engine._rtcp_send is not None  # non-muxed: separate sink installed
            assert engine._rtcp_local_port == engine.local_port + 1
    finally:
        in_call.set()
        await adapter.disconnect()  # type: ignore[attr-defined]
        await asyncio.sleep(0)
