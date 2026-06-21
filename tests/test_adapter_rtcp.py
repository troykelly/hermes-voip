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
import base64
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from hermes_voip.adapter import VoipAdapter
    from hermes_voip.media.engine import RtpMediaTransport

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
from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict, InjectionGuard
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.sdp import (
    AudioMedia,
    Fingerprint,
    IceCandidate,
    SessionDescription,
    SetupRole,
)

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
        answer_profile="RTP/AVP",
        payload_types=(0, 8),
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
        answer_profile="RTP/AVP",
        payload_types=(0, 8),
        rtcp_enabled=True,
    )
    assert plan is not None
    assert plan.mux is True
    assert plan.remote_rtcp_addr == ("203.0.113.7", 20000)


def test_plan_rtcp_activation_kill_switch_suppresses() -> None:
    """The operator kill-switch (rtcp_enabled=False) suppresses all activation."""
    plan = _plan_rtcp_activation(
        _audio_of(_PLAIN_OFFER_NO_MUX),
        remote_address="203.0.113.7",
        answer_profile="RTP/AVP",
        payload_types=(0, 8),
        rtcp_enabled=False,
    )
    assert plan is None


# ---------------------------------------------------------------------------
# Codex review #4: activation gated on the ANSWERED PROFILE, fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer_profile",
    [
        "RTP/SAVP",  # SDES-SRTP (RFC 4568)
        "RTP/SAVPF",  # SDES-SRTP with AVPF feedback
        "UDP/TLS/RTP/SAVP",  # SIP DTLS-SRTP (ADR-0053 Stage 2)
        "UDP/TLS/RTP/SAVPF",  # WebRTC DTLS-SRTP (ADR-0016)
    ],
)
def test_plan_rtcp_activation_non_avp_profile_is_suppressed(
    answer_profile: str,
) -> None:
    """MAJOR #4: only an exact ``RTP/AVP`` answer activates RTCP (fail-closed).

    The engine has no SRTCP (RFC 3711 §3.4) transform, so RTCP is activated ONLY on
    the cleartext plain-RTP profile. Gating on the ANSWERED profile (not "no crypto
    present") is fail-closed: any secured profile — SDES SAVP/SAVPF, SIP DTLS, WebRTC
    SAVPF — leaves RTCP dormant. Cleartext RTCP on a secured 5-tuple would violate
    the profile and leak SSRC/CNAME/timing.
    """
    plan = _plan_rtcp_activation(
        _audio_of(_PLAIN_OFFER_WITH_MUX),
        remote_address="203.0.113.7",
        answer_profile=answer_profile,
        payload_types=(0, 8),
        rtcp_enabled=True,
    )
    assert plan is None


def test_plan_rtcp_activation_plain_avp_profile_activates() -> None:
    """The exact plain ``RTP/AVP`` answer profile DOES activate RTCP (the positive)."""
    plan = _plan_rtcp_activation(
        _audio_of(_PLAIN_OFFER_NO_MUX),
        remote_address="203.0.113.7",
        answer_profile="RTP/AVP",
        payload_types=(0, 8),
        rtcp_enabled=True,
    )
    assert plan is not None


# ---------------------------------------------------------------------------
# Codex review #2: rtcp-mux forbidden when an RTP payload type is in 64-95
# (RFC 5761 §4 — those PTs alias the RTCP packet-type range on a muxed stream)
# ---------------------------------------------------------------------------


def test_plan_rtcp_activation_mux_refused_for_conflict_range_pt() -> None:
    """MAJOR #2: an agreed RTP PT in 64-95 forbids rtcp-mux (RFC 5761 §4).

    Byte 2 of an RTP packet is ``(marker<<7 | PT)``; an RTP PT in 64-95 with the
    marker bit set becomes 192-223, which overlaps the RTCP packet-type range
    (200-204) on a muxed stream — an unresolvable RTP-vs-RTCP ambiguity. RFC 5761 §4
    forbids those PTs under rtcp-mux. The planner must NOT mux such a call: it falls
    back to a non-muxed plan (RTCP on RTP-port+1) so the demux ambiguity never arises.
    """
    plan = _plan_rtcp_activation(
        _audio_of(_PLAIN_OFFER_WITH_MUX),  # the offer requested a=rtcp-mux
        remote_address="203.0.113.7",
        answer_profile="RTP/AVP",
        payload_types=(0, 72),  # 72 ∈ [64, 95] — forbidden under mux
        rtcp_enabled=True,
    )
    assert plan is not None
    # The mux request is REFUSED — RTCP falls back to the sibling port (RFC 3550 §11).
    assert plan.mux is False
    assert plan.remote_rtcp_addr == ("203.0.113.7", 20001)


def test_plan_rtcp_activation_mux_kept_for_normal_payload_types() -> None:
    """The plugin's own codecs (0/8/9/96-127) are outside 64-95: mux still honoured."""
    plan = _plan_rtcp_activation(
        _audio_of(_PLAIN_OFFER_WITH_MUX),
        remote_address="203.0.113.7",
        answer_profile="RTP/AVP",
        payload_types=(0, 8, 9, 96, 101),  # all outside [64, 95]
        rtcp_enabled=True,
    )
    assert plan is not None
    assert plan.mux is True  # mux honoured — no conflict-range PT


def test_media_config_rtcp_enabled_defaults_true() -> None:
    """RTCP is on by default; an operator can disable it via the env knob."""
    assert load_media_config({}).rtcp_enabled is True
    assert (
        load_media_config({"HERMES_VOIP_RTCP_ENABLED": "false"}).rtcp_enabled is False
    )


def test_media_config_secured_rtcp_enabled_defaults_false() -> None:
    """Secured-path RTCP (SRTCP) is OFF by default — opt-in via the env knob.

    Emitting SRTCP on a real gateway that did not negotiate a=rtcp-mux muted the media
    on a live call, so the secured planner stays dormant unless explicitly enabled
    (ADR-0061 §live finding). The master HERMES_VOIP_RTCP_ENABLED kill-switch still
    applies on top; this flag specifically gates the secured (SDES/SRTCP) path.
    """
    assert load_media_config({}).secured_rtcp_enabled is False
    assert (
        load_media_config(
            {"HERMES_VOIP_SECURED_RTCP_ENABLED": "true"}
        ).secured_rtcp_enabled
        is True
    )
    assert (
        load_media_config(
            {"HERMES_VOIP_SECURED_RTCP_ENABLED": "false"}
        ).secured_rtcp_enabled
        is False
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


class _FakeTtsStream(TtsStream):
    """An empty, lifecycle-complete TtsStream (yields nothing). Typed, no ignores.

    Subclasses the ``TtsStream`` Protocol so mypy enforces the full surface
    (``__anext__`` + ``flush``/``cancel``/``aclose``) — the call loop wraps playout
    in ``aclosing`` and may barge-in, so all three lifecycle methods must exist.
    """

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


async def _build_adapter(
    transport: _FakeTransport, *, media_env: dict[str, str] | None = None
) -> VoipAdapter:
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))
    manager = RegistrationManager(_gateway_config(), transport)
    # require_secure_media disabled: the plain-RTP RTCP-activation tests offer plain
    # RTP/AVP to exercise the cleartext-path RTCP, which the secure-media mandate
    # (ADR-0070) would otherwise 488. The secured-path RTCP tests offer RTP/SAVP and
    # are unaffected by that flag. ``media_env`` lets a test layer extra media-config
    # knobs (e.g. HERMES_VOIP_SECURED_RTCP_ENABLED) onto this base.
    media_config_env = {"HERMES_VOIP_REQUIRE_SECURE_MEDIA": "false"}
    if media_env is not None:
        media_config_env.update(media_env)
    with (
        patch(
            "hermes_voip.adapter.load_gateway_config", return_value=_gateway_config()
        ),
        patch(
            "hermes_voip.adapter.load_media_config",
            return_value=load_media_config(media_config_env),
        ),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()
    return adapter


def _live_engine(adapter: VoipAdapter, call_id: str) -> RtpMediaTransport:
    """The concrete live media engine for ``call_id`` (white-box narrowing).

    ``CallSession._media`` is the ``CallMedia`` Protocol; these integration tests
    inspect the concrete ``RtpMediaTransport`` RTCP internals, so narrow it with an
    ``isinstance`` assert (no cast / no type-ignore — rule 17).
    """
    from hermes_voip.media.engine import RtpMediaTransport  # noqa: PLC0415

    engine = adapter._call_sessions[call_id]._media
    assert isinstance(engine, RtpMediaTransport)
    return engine


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
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)

            # The 200 OK is a plain RTP/AVP answer (no a=crypto) — the cleartext path.
            oks = [
                SipResponse.parse(m)
                for m in transport.sent
                if m.startswith("SIP/2.0 200")
            ]
            assert oks
            assert "a=crypto" not in oks[-1].body

            # Reach the LIVE engine and assert RTCP was activated on it.
            engine = _live_engine(adapter, call_id)
            assert engine._rtcp_task is not None  # the run_rtcp loop is live
            assert engine._rtcp_active is True  # inbound muxed-demux engaged
            assert engine._rtcp_send is not None  # non-muxed: separate sink installed
            assert engine._rtcp_local_port == engine.local_port + 1
    finally:
        in_call.set()
        await adapter.disconnect()
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Secured-path RTCP via SRTCP (ADR-0066) — FAIL-CLOSED by default (ADR-0061 §live).
#
# SRTCP (RFC 3711 §3.4) secures the RTCP control channel, so the SDES path CAN carry
# RTCP wrapped in SRTCP. But emitting unexpected SRTCP on a real gateway that did NOT
# negotiate a=rtcp-mux MUTED the media on a live call (the sibling SRTCP socket on
# RTP-port+1 broke the session) — so secured-path RTCP is now OPT-IN, gated on
# HERMES_VOIP_SECURED_RTCP_ENABLED (default false). By default a secured (RTP/SAVP)
# inbound call stays RTCP-DORMANT (no sibling socket, no SRTCP on the wire), matching
# the pre-#160 behaviour. The capability still activates when the flag is set true.
#
# Test change (rule 19): the pre-existing
# test_inbound_sdes_savp_call_activates_rtcp_via_srtcp_on_live_engine asserted the
# SDES path ALWAYS activates RTCP — that expectation broke a real call (unexpected
# SRTCP muted the media on a non-mux Grandstream). It is INVERTED here to the
# now-correct default (dormant), with an explicit opt-in test proving the capability
# still works when chosen. The WebRTC SAVPF path always rtcp-muxes onto its single
# ICE/DTLS 5-tuple (no sibling socket) and is unchanged by this gate.
# ---------------------------------------------------------------------------


# A 30-byte SDES master key||salt, generated at runtime (never a literal secret in a
# tracked file — rule 34). Valid base64 for an AES_CM_128_HMAC_SHA1_80 inline key.
_SDES_INLINE_KEY = base64.b64encode(bytes(range(30))).decode("ascii")

_SDES_OFFER = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 20000 RTP/SAVP 0 8\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_SDES_INLINE_KEY}\r\n"
    "a=sendrecv\r\n"
)

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


@pytest.mark.asyncio
async def test_inbound_sdes_savp_call_leaves_rtcp_dormant_by_default() -> None:
    """SDES: a real RTP/SAVP INVITE leaves RTCP DORMANT by default (fail-closed).

    Through ``_on_inbound_invite`` with the REAL ``RtpMediaTransport``: the answer is
    still SDES (a=crypto present, RTP/SAVP) — but because the offer has no a=rtcp-mux
    and HERMES_VOIP_SECURED_RTCP_ENABLED defaults FALSE, the secured planner returns
    ``None`` and RTCP is NOT activated: no run_rtcp loop, ``_rtcp_active`` stays False,
    no sibling SRTCP socket on RTP-port+1. This is the pre-#160 behaviour the live
    regression restored — emitting unexpected SRTCP on a non-mux gateway muted the
    media, so secured RTCP is opt-in. The SRTCP transform is still wired on the engine
    (zero datapath effect while RTCP is inactive), ready for the opt-in case.
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SDES_OFFER, call_id))

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
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)

            # The 200 OK is still an SDES answer (a=crypto present) — the secured path
            # is unchanged; only RTCP activation is gated off.
            oks = [
                SipResponse.parse(m)
                for m in transport.sent
                if m.startswith("SIP/2.0 200")
            ]
            assert oks
            assert "a=crypto" in oks[-1].body

            # RTCP is DORMANT: no loop task, not active, no sibling RTCP socket.
            engine = _live_engine(adapter, call_id)
            assert engine._rtcp_task is None  # no run_rtcp loop started
            assert engine._rtcp_active is False  # never activated
            assert engine._rtcp_local_port is None  # no sibling SRTCP socket bound
    finally:
        in_call.set()
        await adapter.disconnect()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_inbound_sdes_savp_call_activates_srtcp_when_opted_in() -> None:
    """SDES + HERMES_VOIP_SECURED_RTCP_ENABLED=true ACTIVATES RTCP over SRTCP.

    The capability is retained behind the flag (default-off restores audio on non-mux
    gateways; on a validated gateway the operator opts in). With the flag true, the
    same RTP/SAVP INVITE drives the live engine to RTCP ACTIVE with the SRTCP transform
    wired (``_srtcp_in``/``_srtcp_out`` set), and — the offer having no a=rtcp-mux — the
    sibling SRTCP socket bound on RTP-port+1 (RFC 3550 §11). The outbound RTCP is
    SRTCP-protected (never cleartext on the secured 5-tuple).
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(
        transport, media_env={"HERMES_VOIP_SECURED_RTCP_ENABLED": "true"}
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SDES_OFFER, call_id))

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
            adapter._on_inbound_invite(
                NewCall(registration=_ext_config(), invite=invite)
            )
            await _until(lambda: call_id in adapter._call_loops)

            # The 200 OK is an SDES answer (a=crypto present) — the secured path.
            oks = [
                SipResponse.parse(m)
                for m in transport.sent
                if m.startswith("SIP/2.0 200")
            ]
            assert oks
            assert "a=crypto" in oks[-1].body

            # The LIVE engine has RTCP ACTIVE, wrapped in SRTCP (no cleartext leak).
            engine = _live_engine(adapter, call_id)
            assert engine._rtcp_task is not None  # the run_rtcp loop is live
            assert engine._rtcp_active is True
            assert engine._has_srtcp is True  # SRTCP in+out wired
            assert engine._srtcp_in is not None
            assert engine._srtcp_out is not None
            # Non-muxed offer → sibling RTCP socket on RTP-port+1 (RFC 3550 §11).
            assert engine._rtcp_local_port == engine.local_port + 1
    finally:
        in_call.set()
        await adapter.disconnect()
        await asyncio.sleep(0)


class _FakeWebRtcSession:
    """A fake WebRtcMediaSession: skips real ICE/DTLS, returns canned SRTP sessions.

    Mirrors the fake in test_adapter_webrtc.py so a WebRTC INVITE reaches the engine
    construction without a real ICE/DTLS stack.
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
        self.ice.send = AsyncMock(return_value=None)
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
        self.closed = True


@pytest.mark.asyncio
async def test_inbound_webrtc_savpf_call_activates_rtcp_via_srtcp() -> None:
    """WebRTC: a real SAVPF INVITE activates RTCP (muxed) wired with SRTCP.

    Through ``_on_inbound_invite`` with the WebRTC (DTLS-SRTP) path: the engine is a
    fake whose ``start_rtcp`` is an AsyncMock. WebRTC always rtcp-muxes onto the single
    ICE/DTLS 5-tuple, so ``start_rtcp(mux=True)`` IS called, and the engine is built
    with the SRTCP sessions derived from the SAME DTLS export (so the RTCP is encrypted
    + authenticated, never cleartext over the encrypted pipe).
    """
    transport = _FakeTransport()
    adapter = await _build_adapter(transport)
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_WEBRTC_OFFER, call_id))

    in_call = asyncio.Event()

    async def _blocking_run() -> None:
        await in_call.wait()

    fake_engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        start_rtcp=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=0,
        inbound_sample_rate=16_000,
    )
    ctor_kwargs: dict[str, object] = {}

    def _capture_engine(**kwargs: object) -> MagicMock:
        ctor_kwargs.update(kwargs)
        return fake_engine

    try:
        with (
            patch("hermes_voip.adapter.WebRtcMediaSession", _FakeWebRtcSession),
            patch("hermes_voip.adapter.RtpMediaTransport", _capture_engine),
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

            # The answer is a WebRTC (DTLS) answer: fingerprint present, no a=crypto.
            oks = [
                SipResponse.parse(m)
                for m in transport.sent
                if m.startswith("SIP/2.0 200")
            ]
            assert oks
            assert "a=fingerprint" in oks[-1].body
            assert "a=crypto" not in oks[-1].body

            # RTCP activated, MUXED (single ICE/DTLS 5-tuple).
            fake_engine.start_rtcp.assert_called_once()
            assert fake_engine.start_rtcp.call_args.kwargs["mux"] is True
            # The engine was built with SRTCP wired from the DTLS export.
            assert ctor_kwargs.get("srtcp_inbound") is not None
            assert ctor_kwargs.get("srtcp_outbound") is not None
    finally:
        in_call.set()
        await asyncio.sleep(0)
