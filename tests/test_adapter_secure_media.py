"""Secure-media mandate on inbound INVITE (#73, ADR-0070).

The plugin only ever REGISTERS over TLS/WSS (``_VIA_TRANSPORT`` in ``config.py``
restricts the transport to ``{tls, wss}``), so every SIP *signalling* leg is
already encrypted. ADR-0070 closes the remaining MEDIA-plane cleartext gap: an
inbound INVITE that offers plain ``RTP/AVP`` audio (no SRTP) is REJECTED with
``488 Not Acceptable Here`` instead of being answered as a cleartext RTP call —
unless the new ``HERMES_VOIP_REQUIRE_SECURE_MEDIA`` flag (default ``True``)
disables the policy.

Any SECURED profile is still accepted: SDES (``RTP/SAVP``), DTLS-SRTP
(``UDP/TLS/RTP/SAVP``) and WebRTC (``UDP/TLS/RTP/SAVPF``) — i.e. every profile for
which :attr:`hermes_voip.sdp.AudioMedia.is_srtp` is true. This composes with the
opportunistic SDES answer (ADR-0053 Stage 1) and the DTLS / WebRTC tiers rather
than duplicating them: the prior work made secured media WORK; this mandate makes
cleartext media REFUSED.

These drive the REAL ``VoipAdapter._handle_inbound_invite`` (via the real Dialog,
RegistrationManager, build_response — only the media engine, the conversational
pipeline and the providers are fakes), so the on-the-wire 488 / 200 OK is what
production produces (rule 26 — exercised in the hermes-contract CI job). All
credentials and hosts are obvious fakes (``pbx.example.test`` / ext ``1000`` /
``127.0.0.1``).
"""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The adapter imports the real Hermes base at module top; skip the whole module
# when the optional runtime is absent (it runs in the hermes-contract CI job).
pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_voip.config import (
    ExtensionConfig,
    GatewayConfig,
    MediaConfig,
    load_media_config,
)
from hermes_voip.manager import NewCall, RegistrationManager
from hermes_voip.message import SipRequest, SipResponse, new_call_id, new_tag
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.sdp import AudioMedia, SessionDescription

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from hermes_voip.adapter import VoipAdapter
    from hermes_voip.providers.audio import PcmFrame
    from hermes_voip.providers.tts import TtsStream


# ---------------------------------------------------------------------------
# Poll helper (no fixed sleeps).
# ---------------------------------------------------------------------------
async def _until(
    predicate: Callable[[], bool], *, timeout: float = 3.0, step: float = 0.001
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            msg = "condition not met before timeout"
            raise AssertionError(msg)
        await asyncio.sleep(step)


# ---------------------------------------------------------------------------
# Platform registration so VoipAdapter construction succeeds.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _register_voip_platform() -> None:
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


# ---------------------------------------------------------------------------
# Fakes (no real network / ML). Mirror tests/test_adapter.py.
# ---------------------------------------------------------------------------
class _FakeTransport:
    def __init__(self, *, local_sent_by: str = "172.23.0.2:5061") -> None:
        self.sent: list[str] = []
        self._local_sent_by = local_sent_by
        self._sinks: dict[str, object] = {}

    @property
    def local_sent_by(self) -> str:
        return self._local_sent_by

    def contact_uri(self, extension: str) -> str:
        return f"<sip:{extension}@{self._local_sent_by};transport=tls>"

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def connect(self) -> None: ...

    async def aclose(self) -> None: ...

    def bind_manager(self, manager: object) -> None: ...

    def add_call(self, call_id: str, sink: object) -> None:
        self._sinks[call_id] = sink

    def remove_call(self, call_id: str, sink: object | None = None) -> None:
        self._sinks.pop(call_id, None)


class _FakeTtsStream:
    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        async def _gen() -> AsyncIterator[PcmFrame]:
            return
            yield  # pragma: no cover

        return _gen()

    async def cancel(self) -> None: ...


class _FakeTTS:
    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        return _FakeTtsStream()  # type: ignore[return-value]  # test double


class _FakeASR:
    async def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[object]:
        finals: list[object] = []
        async for _ in audio:
            pass  # consume so the pump does not stall
        for transcript in finals:  # always empty — forces the async-gen shape
            yield transcript


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
    return Providers(asr=_FakeASR(), tts=_FakeTTS(), guard=_FakeGuard())  # type: ignore[arg-type]  # test doubles


def _fake_engine() -> MagicMock:
    """A fake RtpMediaTransport whose connect()/stop()/start_rtcp() are inert."""
    return MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        start_rtcp=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=20002,
        inbound_sample_rate=8_000,
    )


# ---------------------------------------------------------------------------
# Real GatewayConfig / ExtensionConfig (ext 1000, matches the contact).
# ---------------------------------------------------------------------------
def _ext_config() -> ExtensionConfig:
    return ExtensionConfig(index=0, extension="1000", username="1000", password="fake")


def _real_gateway_config() -> GatewayConfig:
    return GatewayConfig(
        host="pbx.example.test",
        port=5061,
        transport="tls",
        expires=300,
        user_agent="hermes-voip/0",
        extensions=(_ext_config(),),
        default_index=0,
    )


_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
    "HERMES_VOIP_STT_PROVIDER": "sherpa-onnx",
    "HERMES_VOIP_STT_MODEL_DIR": "/fake/stt",
    "HERMES_VOIP_TTS_PROVIDER": "sherpa-kokoro",
    "HERMES_VOIP_TTS_MODEL": "/fake/tts",
    "HERMES_VOIP_INJECTION_GUARD": "onnx",
    "HERMES_VOIP_INJECTION_GUARD_MODEL_DIR": "/fake/guard",
}


def _platform_config() -> PlatformConfig:
    return PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))


async def _build_adapter_with_real_manager(
    transport: _FakeTransport,
    media_cfg: MediaConfig,
) -> tuple[VoipAdapter, RegistrationManager]:
    """A real VoipAdapter + real RegistrationManager over the fake transport."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    manager = RegistrationManager(_real_gateway_config(), transport)
    with (
        patch(
            "hermes_voip.adapter.load_gateway_config",
            return_value=_real_gateway_config(),
        ),
        patch("hermes_voip.adapter.load_media_config", return_value=media_cfg),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(_platform_config())
        await adapter.connect()
    return adapter, manager


# ---------------------------------------------------------------------------
# SDP offers. Plain RTP/AVP (cleartext) vs SDES RTP/SAVP (secured).
# The fake SDES master key||salt (30 octets for AES_CM_128_HMAC_SHA1_80) is built
# at runtime (sequential bytes 0..29) so no high-entropy key literal appears in
# this file (the gitleaks allowlist is path-scoped to test_sdp.py).
# ---------------------------------------------------------------------------
_SDP_PLAIN = (
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

_OFFER_SDES_KEY = base64.b64encode(bytes(range(30))).decode("ascii")
_SDP_SAVP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 127.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 20000 RTP/SAVP 0 8\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_OFFER_SDES_KEY}\r\n"
    "a=sendrecv\r\n"
)


def _make_invite(sdp: str, call_id: str) -> str:
    content_length = len(sdp.encode("utf-8"))
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bK{new_tag()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:caller@pbx.example.test>;tag={new_tag()}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:caller@127.0.0.1:60000;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{sdp}"
    )


def _media_cfg(*, require_secure_media: bool) -> MediaConfig:
    return load_media_config(
        {
            "HERMES_VOIP_REQUIRE_SECURE_MEDIA": "true"
            if require_secure_media
            else "false"
        }
    )


def _sent_responses(transport: _FakeTransport) -> list[SipResponse]:
    return [SipResponse.parse(m) for m in transport.sent if m.startswith("SIP/2.0 ")]


# ===========================================================================
# (a) plain RTP/AVP + require_secure_media=True -> 488, no media, dialog cleaned
# ===========================================================================
@pytest.mark.asyncio
async def test_plain_rtp_avp_rejected_488_when_mandate_on() -> None:
    """A cleartext RTP/AVP offer is REJECTED 488 (no 200 OK, no media engine)."""
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(
        transport, _media_cfg(require_secure_media=True)
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SDP_PLAIN, call_id))

    rtp_ctor = MagicMock(return_value=_fake_engine())
    with (
        patch("hermes_voip.adapter.RtpMediaTransport", rtp_ctor),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: bool(transport.sent))
        await asyncio.sleep(0)

    responses = _sent_responses(transport)
    statuses = [r.status_code for r in responses]
    assert 488 in statuses, f"expected a 488 reject; got statuses {statuses}"
    assert 200 not in statuses, (
        f"a cleartext RTP/AVP call must NOT be answered; got statuses {statuses}"
    )
    rejected = next(r for r in responses if r.status_code == 488)
    assert rejected.reason == "Not Acceptable Here", (
        f"unexpected reject reason: {rejected.reason!r}"
    )
    # No RTP media engine was ever constructed for the rejected cleartext call.
    rtp_ctor.assert_not_called()
    # The dialog is not left registered/tracked behind a rejected call.
    assert call_id not in adapter._call_sessions
    assert call_id not in adapter._call_loops


# ===========================================================================
# (b) SDES RTP/SAVP + require_secure_media=True -> ACCEPTED (200, secured)
# ===========================================================================
@pytest.mark.asyncio
async def test_sdes_savp_offer_accepted_when_mandate_on() -> None:
    """A secured RTP/SAVP (SDES) offer is still ANSWERED (200) under the mandate."""
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(
        transport, _media_cfg(require_secure_media=True)
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SDP_SAVP, call_id))

    with (
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=_fake_engine()),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: any(m.startswith("SIP/2.0 200") for m in transport.sent))
        await asyncio.sleep(0)

    responses = _sent_responses(transport)
    statuses = [r.status_code for r in responses]
    assert 200 in statuses, (
        f"a secured SDES RTP/SAVP offer must be answered; got statuses {statuses}"
    )
    ok = next(r for r in responses if r.status_code == 200)
    # The mandate must not have downgraded the answer: it stays RTP/SAVP + a=crypto.
    assert "RTP/SAVP" in ok.body, f"SDES answer is not RTP/SAVP; body:\n{ok.body}"
    assert ok.body.count("a=crypto:") == 1, (
        f"expected one a=crypto in the SDES answer; body:\n{ok.body}"
    )


# ===========================================================================
# (c) plain RTP/AVP + require_secure_media=False -> ACCEPTED (back-compat)
# ===========================================================================
@pytest.mark.asyncio
async def test_plain_rtp_avp_accepted_when_mandate_off() -> None:
    """With the mandate disabled, a cleartext RTP/AVP offer is answered (200)."""
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(
        transport, _media_cfg(require_secure_media=False)
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(_SDP_PLAIN, call_id))

    with (
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=_fake_engine()),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: any(m.startswith("SIP/2.0 200") for m in transport.sent))
        await asyncio.sleep(0)

    responses = _sent_responses(transport)
    statuses = [r.status_code for r in responses]
    assert 200 in statuses, (
        f"mandate-off must answer a cleartext call (back-compat); got {statuses}"
    )
    ok = next(r for r in responses if r.status_code == 200)
    # Answered as plain RTP/AVP (no secured profile / no a=crypto forced).
    assert "RTP/AVP" in ok.body, f"plain answer is not RTP/AVP; body:\n{ok.body}"
    assert "a=crypto:" not in ok.body, (
        f"a plain RTP/AVP answer must not carry a=crypto; body:\n{ok.body}"
    )


# ===========================================================================
# (d) The mandate ACCEPTS every secured profile and rejects only cleartext. Each
# secured profile (the SDES, DTLS-SRTP and WebRTC m-line transports) reports
# ``is_srtp``; plain audio over RTP slash AVP is the sole ``is_srtp`` False case.
# The guard rejects iff ``require_secure_media and not is_srtp``, so the DTLS and
# WebRTC tiers are never rejected by it. This pins the secured-profile set at the
# SDP layer (no OpenSSL extra needed) alongside the end-to-end SDES acceptance.
# ===========================================================================
def _audio_of(sdp: str) -> AudioMedia:
    audio = SessionDescription.parse(sdp).audio
    assert audio is not None
    return audio


@pytest.mark.parametrize(
    ("profile", "extra_attrs", "is_srtp_expected"),
    [
        ("RTP/AVP", "", False),
        (
            "RTP/SAVP",
            f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_OFFER_SDES_KEY}\r\n",
            True,
        ),
        ("UDP/TLS/RTP/SAVP", "a=fingerprint:sha-256 AA:BB\r\n", True),
        ("UDP/TLS/RTP/SAVPF", "a=fingerprint:sha-256 AA:BB\r\n", True),
    ],
)
def test_secured_profiles_pass_the_mandate_guard(
    profile: str, extra_attrs: str, is_srtp_expected: bool
) -> None:
    """Every secured profile reports is_srtp; only plain RTP/AVP does not.

    The mandate guard rejects iff ``require_secure_media and not audio.is_srtp``.
    """
    sdp = (
        "v=0\r\n"
        "o=- 0 0 IN IP4 127.0.0.1\r\n"
        "s=-\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "t=0 0\r\n"
        f"m=audio 20000 {profile} 0 8\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        f"{extra_attrs}"
        "a=sendrecv\r\n"
    )
    audio = _audio_of(sdp)
    assert audio.is_srtp is is_srtp_expected
    # The guard's reject predicate with the mandate ON: ``require and not is_srtp``.
    require_secure_media = True
    rejected = require_secure_media and not audio.is_srtp
    assert rejected is (not is_srtp_expected)
