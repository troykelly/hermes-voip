"""RFC 4028 session timers on the real VoipAdapter (#74, ADR-0071).

These drive the REAL ``VoipAdapter`` (real ``Dialog`` / ``RegistrationManager`` /
``CallSession`` / ``build_response`` / ``build_outbound_invite``; only the media
engine, the conversational pipeline and the providers are fakes), so the
on-the-wire 422 / 200-OK / refresh re-INVITE / BYE are what production emits
(rule 26 — exercised in the hermes-contract CI job).

Coverage:
  (a) inbound INVITE with Session-Expires < Min-SE → 422 + Min-SE header, no dialog;
  (b) inbound INVITE with a valid Session-Expires → 2xx carries Session-Expires +
      ``refresher`` + ``Supported: timer`` (+ ``Require: timer``);
  (c) we are the refresher → a refresh re-INVITE (sendrecv, carrying Session-Expires)
      is emitted around SE/2 (driven by a controllable sleep seam — no real sleep);
  (d) the refresh re-INVITE gets a failure response → the dialog is BYE'd;
  (e) outbound place_call INVITE carries Session-Expires + ``Supported: timer``, and a
      ``422`` answer triggers a retry with SE raised to the peer's Min-SE.

All credentials/hosts are obvious fakes (``pbx.example.test`` / ext ``1000`` /
``127.0.0.1``).
"""

from __future__ import annotations

import asyncio
import contextlib
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
from hermes_voip.originate import OutboundCallFailed
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.session_timer import Refresher, SessionExpires

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
# Fakes (no real network / ML). Mirror tests/test_adapter_secure_media.py.
# ---------------------------------------------------------------------------
class _FakeTransport:
    def __init__(self, *, local_sent_by: str = "127.0.0.1:5061") -> None:
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
        set_hold=AsyncMock(return_value=None),
        rekey_srtp=AsyncMock(return_value=None),
        send_dtmf=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=20002,
        inbound_sample_rate=8_000,
        dtmf_send_mode=_DEFAULT_DTMF_SEND_MODE,
    )


# Imported lazily so the module top stays free of the optional engine import.
from hermes_voip.dtmf import DtmfSendMode as _DtmfSendMode  # noqa: E402

_DEFAULT_DTMF_SEND_MODE = _DtmfSendMode.RFC4733


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
    # Keep the secure-media mandate OFF for these tests so the plain RTP/AVP offers
    # below exercise the session-timer path (not the ADR-0070 488), and pin SE/Min-SE.
    "HERMES_VOIP_REQUIRE_SECURE_MEDIA": "false",
    "HERMES_VOIP_SESSION_EXPIRES": "600",
    "HERMES_VOIP_MIN_SE": "90",
}


def _platform_config(extra: dict[str, str] | None = None) -> PlatformConfig:
    env = dict(_FAKE_ENV)
    if extra is not None:
        env.update(extra)
    return PlatformConfig(enabled=True, extra=env)


def _media_cfg(extra: dict[str, str] | None = None) -> MediaConfig:
    env = dict(_FAKE_ENV)
    if extra is not None:
        env.update(extra)
    return load_media_config(env)


async def _build_adapter_with_real_manager(
    transport: _FakeTransport,
    media_cfg: MediaConfig,
    *,
    platform: PlatformConfig | None = None,
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
        adapter = VoipAdapter(platform if platform is not None else _platform_config())
        await adapter.connect()
    return adapter, manager


# ---------------------------------------------------------------------------
# SDP (plain RTP/AVP; the mandate is off for these tests).
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


def _make_invite(
    sdp: str, call_id: str, *, session_expires: str | None, supported_timer: bool
) -> str:
    headers = [
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bK{new_tag()}",
        "Max-Forwards: 70",
        f"From: <sip:caller@pbx.example.test>;tag={new_tag()}",
        "To: <sip:1000@pbx.example.test>",
        f"Call-ID: {call_id}",
        "CSeq: 1 INVITE",
        "Contact: <sip:caller@127.0.0.1:60000;transport=tls>",
    ]
    if supported_timer:
        headers.append("Supported: timer")
    if session_expires is not None:
        headers.append(f"Session-Expires: {session_expires}")
    headers.append("Content-Type: application/sdp")
    headers.append(f"Content-Length: {len(sdp.encode('utf-8'))}")
    return (
        "INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        + "\r\n".join(headers)
        + "\r\n\r\n"
        + sdp
    )


def _sent_responses(transport: _FakeTransport) -> list[SipResponse]:
    return [SipResponse.parse(m) for m in transport.sent if m.startswith("SIP/2.0 ")]


def _sent_requests(transport: _FakeTransport, method: str) -> list[SipRequest]:
    out: list[SipRequest] = []
    for m in transport.sent:
        if m.startswith("SIP/2.0 "):
            continue
        req = SipRequest.parse(m)
        if req.method == method:
            out.append(req)
    return out


async def _block_forever() -> None:
    """A CallLoop.run stand-in that never returns (until the task is cancelled).

    The watchdog tests need the call to stay UP so the SE/2 refresh can fire; a
    ``run`` that returns immediately would tear the call down (MEDIA_TIMEOUT) and cancel
    the watchdog before it ever sleeps. Disconnect cancels the loop task at the end.
    """
    await asyncio.Event().wait()


def _patched_invite_env(*, block_loop: bool = False) -> contextlib.ExitStack:
    """Patch the conversational-pipeline collaborators to inert fakes.

    ``block_loop`` makes the fake ``CallLoop.run`` block forever instead of returning
    immediately, so the call stays established long enough to observe the long-lived
    session-timer watchdog (cancelled when the test disconnects).
    """
    run: AsyncMock = (
        AsyncMock(side_effect=_block_forever)
        if block_loop
        else AsyncMock(return_value=None)
    )
    stack = contextlib.ExitStack()
    stack.enter_context(
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=_fake_engine())
    )
    stack.enter_context(
        patch("hermes_voip.adapter.CallLoop", return_value=MagicMock(run=run))
    )
    stack.enter_context(
        patch("hermes_voip.adapter.GuardSessionState", return_value=MagicMock())
    )
    stack.enter_context(
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock())
    )
    stack.enter_context(
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock())
    )
    return stack


# ===========================================================================
# (a) inbound Session-Expires < Min-SE -> 422 + Min-SE, no dialog.
# ===========================================================================
@pytest.mark.asyncio
async def test_inbound_se_below_min_se_rejected_422() -> None:
    """An inbound SE below our Min-SE is rejected 422 with our Min-SE; no dialog."""
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())
    call_id = new_call_id()
    invite = SipRequest.parse(
        _make_invite(_SDP_PLAIN, call_id, session_expires="50", supported_timer=True)
    )

    with _patched_invite_env():
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: bool(transport.sent))
        await asyncio.sleep(0)

    responses = _sent_responses(transport)
    statuses = [r.status_code for r in responses]
    assert 422 in statuses, f"expected a 422 reject; got statuses {statuses}"
    assert 200 not in statuses, (
        f"a too-small-SE call must NOT be answered; got statuses {statuses}"
    )
    rejected = next(r for r in responses if r.status_code == 422)
    assert rejected.reason == "Session Interval Too Small"
    min_se = rejected.header("Min-SE")
    assert min_se is not None, "422 must carry a Min-SE header"
    assert int(min_se) == 90, f"422 Min-SE must be 90; got {min_se!r}"
    # The dialog is not established behind a rejected call.
    assert call_id not in adapter._call_sessions


# ===========================================================================
# (b) inbound valid Session-Expires -> 2xx carries SE + refresher + Supported.
# ===========================================================================
@pytest.mark.asyncio
async def test_inbound_valid_se_answer_carries_session_expires() -> None:
    """A valid inbound SE is honoured: the 200 OK advertises the session timer."""
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())
    call_id = new_call_id()
    invite = SipRequest.parse(
        _make_invite(_SDP_PLAIN, call_id, session_expires="600", supported_timer=True)
    )

    with _patched_invite_env():
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: any(m.startswith("SIP/2.0 200") for m in transport.sent))
        await asyncio.sleep(0)

    ok = next(r for r in _sent_responses(transport) if r.status_code == 200)
    se_hdr = ok.header("Session-Expires")
    assert se_hdr is not None, "200 OK must carry Session-Expires"
    se = SessionExpires.parse(se_hdr)
    assert se.delta == 600, f"echoed SE delta wrong: {se.delta}"
    assert se.refresher is not None, "the 2xx Session-Expires must name a refresher"
    supported = ",".join(ok.headers_all("Supported")).lower()
    assert "timer" in supported, (
        f"200 OK must advertise Supported: timer; got {supported!r}"
    )
    # When refresher=uas the UAS SHOULD add Require: timer; when uac it MUST. Either
    # way the 2xx carries Require: timer so the UAC engages session timers.
    require = ",".join(ok.headers_all("Require")).lower()
    assert "timer" in require, f"200 OK must carry Require: timer; got {require!r}"


# ===========================================================================
# (b2) inbound INVITE requiring an UNSUPPORTED option-tag -> 420 Bad Extension.
# ===========================================================================
@pytest.mark.asyncio
async def test_inbound_unsupported_require_rejected_420() -> None:
    """An inbound INVITE requiring an option-tag we do not support is 420'd.

    RFC 3261 §8.2.2.3: a UAS that does not understand an option-tag in a Require
    header MUST reject the request 420 Bad Extension and list the unsupported tags in
    an Unsupported header — never silently answer a call whose mandatory extension it
    cannot honour. The only extension we engage as a UAS is RFC 4028 session timers
    ("timer"); "100rel" (reliable provisionals) is refused before any dialog, media,
    or agent surface.
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())
    call_id = new_call_id()
    invite_text = _make_invite(
        _SDP_PLAIN, call_id, session_expires="600", supported_timer=True
    ).replace("CSeq: 1 INVITE\r\n", "CSeq: 1 INVITE\r\nRequire: 100rel\r\n")
    invite = SipRequest.parse(invite_text)

    with _patched_invite_env():
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: bool(transport.sent))
        await asyncio.sleep(0)

    responses = _sent_responses(transport)
    statuses = [r.status_code for r in responses]
    assert 420 in statuses, f"expected a 420 Bad Extension reject; got {statuses}"
    assert 200 not in statuses, (
        f"an unsupported-Require call must NOT be answered; got {statuses}"
    )
    rejected = next(r for r in responses if r.status_code == 420)
    assert rejected.reason == "Bad Extension"
    unsupported = rejected.header("Unsupported")
    assert unsupported is not None, "420 must carry an Unsupported header"
    tags = {t.strip().lower() for t in unsupported.split(",")}
    assert "100rel" in tags, (
        f"Unsupported must list the rejected tag 100rel; got {unsupported!r}"
    )
    assert "timer" not in tags, (
        f"a supported tag must not be listed as Unsupported; got {unsupported!r}"
    )
    # No dialog is established behind a rejected call.
    assert call_id not in adapter._call_sessions


# ===========================================================================
# (b3) inbound INVITE requiring "timer" (SUPPORTED) is NOT 420'd, it is answered.
# ===========================================================================
@pytest.mark.asyncio
async def test_inbound_require_timer_is_supported_not_420() -> None:
    """Require: timer is the one tag we support — it must be honoured, not 420'd."""
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())
    call_id = new_call_id()
    invite_text = _make_invite(
        _SDP_PLAIN, call_id, session_expires="600", supported_timer=True
    ).replace("CSeq: 1 INVITE\r\n", "CSeq: 1 INVITE\r\nRequire: timer\r\n")
    invite = SipRequest.parse(invite_text)

    with _patched_invite_env():
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: any(m.startswith("SIP/2.0 200") for m in transport.sent))
        await asyncio.sleep(0)

    statuses = [r.status_code for r in _sent_responses(transport)]
    assert 420 not in statuses, (
        f"Require: timer is supported and must not be 420'd; got {statuses}"
    )
    assert 200 in statuses, f"a Require: timer call must be answered; got {statuses}"


# ===========================================================================
# (b4) 420 Bad Extension takes precedence over the at-capacity 486 Busy Here.
# ===========================================================================
@pytest.mark.asyncio
async def test_inbound_unsupported_require_420_precedes_486_at_capacity() -> None:
    """A required unsupported extension is 420'd even at capacity — before the 486.

    RFC 3261 §8.2.2.3 header inspection (Require) precedes §8.2.5 request processing,
    where the at-capacity 486 Busy Here arises. A peer requiring an extension we cannot
    honour must be told 420 Bad Extension (retry WITHOUT it), never 486 Busy Here (retry
    the SAME unsatisfiable request later).
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())
    # Saturate admission so the at-capacity 486 branch would fire for a new call.
    assert adapter._gateway_cfg is not None
    max_calls = adapter._gateway_cfg.max_calls
    adapter._admitted_calls.update(f"admitted-{i}" for i in range(max_calls))

    call_id = new_call_id()
    invite_text = _make_invite(
        _SDP_PLAIN, call_id, session_expires="600", supported_timer=True
    ).replace("CSeq: 1 INVITE\r\n", "CSeq: 1 INVITE\r\nRequire: 100rel\r\n")
    invite = SipRequest.parse(invite_text)

    with _patched_invite_env():
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: bool(transport.sent))
        await asyncio.sleep(0)

    statuses = [r.status_code for r in _sent_responses(transport)]
    assert 420 in statuses, (
        f"an unsupported Require must 420 even at capacity (before 486); got {statuses}"
    )
    assert 486 not in statuses, (
        f"420 Bad Extension must take precedence over 486 Busy Here; got {statuses}"
    )


# ===========================================================================
# (c) we are refresher -> a refresh re-INVITE is emitted around SE/2.
# ===========================================================================
@pytest.mark.asyncio
async def test_refresher_emits_refresh_reinvite_at_half_interval() -> None:
    """As the refresher, the watchdog sends a sendrecv refresh re-INVITE at ~SE/2.

    The refresh re-INVITE reuses the in-dialog re-INVITE machinery (a new INVITE on
    the established dialog) and carries Session-Expires so the timer resets on both
    sides. Time is driven by a controllable sleep seam — no real 300 s sleep.
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())

    # A controllable sleep seam: record each requested delay, release the FIRST sleep
    # immediately (so the first refresh fires at once), and block every later sleep so
    # the watchdog does not spin.
    requested: list[float] = []
    first_released = asyncio.Event()

    async def _fake_sleep(secs: float) -> None:
        requested.append(secs)
        if len(requested) == 1:
            first_released.set()
            return
        await asyncio.Event().wait()  # block forever (cancelled on teardown)

    adapter._session_timer_sleep = _fake_sleep

    call_id = new_call_id()
    invite = SipRequest.parse(
        _make_invite(
            _SDP_PLAIN,
            call_id,
            session_expires="600;refresher=uas",
            supported_timer=True,
        )
    )

    with _patched_invite_env(block_loop=True):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        # Wait for the dialog to be wired and the watchdog to request its first sleep.
        await _until(lambda: call_id in adapter._call_sessions)
        session = adapter._call_sessions[call_id]
        await _until(first_released.is_set)

        # Drive the refresh re-INVITE to completion by answering it 200 OK so the
        # watchdog's refresh await returns (proving the refresh round-trips).
        async def _answer_reinvites() -> None:
            seen: set[int] = set()
            while True:
                for req in _sent_requests(transport, "INVITE"):
                    cseq = req.header("CSeq") or ""
                    num = int(cseq.split()[0]) if cseq.split() else 0
                    if num in seen:
                        continue
                    seen.add(num)
                    ok = _build_2xx_for(req, session)
                    await session.on_response(SipResponse.parse(ok))
                await asyncio.sleep(0.001)

        answerer = asyncio.create_task(_answer_reinvites())
        try:
            await _until(lambda: len(_sent_requests(transport, "INVITE")) >= 1)
        finally:
            answerer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await answerer

    # The first requested sleep is ~SE/2 = 300 s.
    assert requested, "the watchdog never armed a refresh sleep"
    assert requested[0] == pytest.approx(300.0), (
        f"refresh interval should be SE/2 = 300 s; got {requested[0]}"
    )
    # The refresh re-INVITE went out, in-dialog, carrying Session-Expires + sendrecv.
    refreshes = _sent_requests(transport, "INVITE")
    assert refreshes, "no refresh re-INVITE was emitted"
    refresh = refreshes[0]
    assert refresh.header("Session-Expires") is not None, (
        "the refresh re-INVITE must carry Session-Expires"
    )
    assert "sendrecv" in refresh.body, (
        "a session refresh re-INVITE offers sendrecv (not a hold)"
    )
    # In-dialog: the To header carries the dialog (our local) tag.
    assert ";tag=" in (refresh.header("To") or ""), "refresh re-INVITE is not in-dialog"

    # Clean up the watchdog task.
    await adapter.disconnect()


# ===========================================================================
# (d) the refresh re-INVITE hits a DEAD-DIALOG status -> the dialog is BYE'd.
#
# NOTE: this test previously answered the refresh 488 and asserted a BYE — but
# RFC 4028 §10 BYEs ONLY on a timeout / 408 / 481; a 488 is a transient non-2xx
# the call must SURVIVE (now covered by test_refresh_transient_5xx_continues_…).
# The dead-dialog 481 here keeps the original "a refusal that means the dialog is
# gone tears it down" intent on a status the RFC actually classifies as fatal.
# ===========================================================================
@pytest.mark.asyncio
async def test_refresh_failure_byes_the_dialog() -> None:
    """A refresh that draws a dead-dialog 481 tears the dialog down with BYE."""
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())

    requested: list[float] = []
    first_released = asyncio.Event()

    async def _fake_sleep(secs: float) -> None:
        requested.append(secs)
        if len(requested) == 1:
            first_released.set()
            return
        await asyncio.Event().wait()

    adapter._session_timer_sleep = _fake_sleep

    call_id = new_call_id()
    invite = SipRequest.parse(
        _make_invite(
            _SDP_PLAIN,
            call_id,
            session_expires="600;refresher=uas",
            supported_timer=True,
        )
    )

    with _patched_invite_env(block_loop=True):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: call_id in adapter._call_sessions)
        session = adapter._call_sessions[call_id]
        await _until(first_released.is_set)

        # Fail the refresh with a dead-dialog 481 so the watchdog classifies it as a
        # gone dialog and BYEs it (RFC 4028 §10).
        async def _reject_reinvites() -> None:
            seen: set[int] = set()
            while True:
                for req in _sent_requests(transport, "INVITE"):
                    cseq = req.header("CSeq") or ""
                    num = int(cseq.split()[0]) if cseq.split() else 0
                    if num in seen:
                        continue
                    seen.add(num)
                    rej = _build_response_for(
                        req, session, 481, "Call/Transaction Does Not Exist"
                    )
                    await session.on_response(SipResponse.parse(rej))
                await asyncio.sleep(0.001)

        rejecter = asyncio.create_task(_reject_reinvites())
        try:
            await _until(lambda: bool(_sent_requests(transport, "BYE")))
        finally:
            rejecter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rejecter

    byes = _sent_requests(transport, "BYE")
    assert byes, "a dead-dialog (481) session refresh must BYE the dialog"
    assert session.ended, (
        "the session should be marked ended after the refresh-failure BYE"
    )

    await adapter.disconnect()


# ===========================================================================
# (f) refresh-failure CLASSIFICATION (RFC 4028 §10 + RFC 3261 §14.1).
#
# The refresher watchdog must NOT BYE on every non-2xx refresh response:
#   * 491 (glare)            -> RETRY after a randomized backoff, no BYE;
#   * 408 / 481 / timeout    -> BYE (the dialog is dead);
#   * any other non-2xx (5xx)-> log + CONTINUE, no BYE.
# These drive the real watchdog through the controllable sleep seam + a fake
# transport that scripts each refresh attempt's response status.
# ===========================================================================


def _drive_inbound_call_to_established(
    adapter: VoipAdapter,
    transport: _FakeTransport,
    call_id: str,
    *,
    session_expires: str,
) -> SipRequest:
    """Build + dispatch an inbound INVITE that engages a UAS refresher watchdog."""
    invite = SipRequest.parse(
        _make_invite(
            _SDP_PLAIN,
            call_id,
            session_expires=session_expires,
            supported_timer=True,
        )
    )
    adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
    return invite


class _ScriptedRefreshResponder:
    """Answers each refresh re-INVITE (by CSeq order) with a scripted status.

    ``responses`` is the per-attempt list of ``(status, reason)`` the test gateway
    returns to the Nth refresh re-INVITE. A ``status`` of ``None`` means *do not
    answer* — drop the refresh so it times out (drives the timeout → BYE path).
    """

    def __init__(
        self,
        transport: _FakeTransport,
        session: object,
        responses: list[tuple[int, str] | None],
    ) -> None:
        self._transport = transport
        self._session = session
        self._responses = responses
        self._seen: set[int] = set()
        self.attempts: list[int] = []

    async def run(self) -> None:
        while True:
            for req in _sent_requests(self._transport, "INVITE"):
                cseq = req.header("CSeq") or ""
                num = int(cseq.split()[0]) if cseq.split() else 0
                if num in self._seen:
                    continue
                self._seen.add(num)
                self.attempts.append(num)
                idx = len(self.attempts) - 1
                scripted = (
                    self._responses[idx] if idx < len(self._responses) else (200, "OK")
                )
                if scripted is None:
                    continue  # drop -> the refresh times out
                status, reason = scripted
                with_sdp = 200 <= status < 300
                resp = _build_response_for(
                    req, self._session, status, reason, with_sdp=with_sdp
                )
                await self._session.on_response(SipResponse.parse(resp))  # type: ignore[attr-defined]
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_refresh_491_glare_is_retried_not_byed() -> None:
    """A 491 to our refresh re-INVITE is retried after backoff — the call is NOT BYE'd.

    RFC 4028 §10 / RFC 3261 §14.1: 491 is glare, not a dead dialog. The watchdog
    must back off (a randomized interval) and re-send the refresh, never tear the
    live call down.
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())

    # Release the first TWO SE/2 refresh sleeps (so a retry's next cadence sleep also
    # runs), block the rest. The backoff sleep returns immediately (recorded).
    se_sleeps: list[float] = []
    backoff_sleeps: list[float] = []
    release_budget = 2

    async def _fake_sleep(secs: float) -> None:
        se_sleeps.append(secs)
        if len(se_sleeps) <= release_budget:
            return
        await asyncio.Event().wait()

    async def _fake_backoff(secs: float) -> None:
        backoff_sleeps.append(secs)

    adapter._session_timer_sleep = _fake_sleep
    adapter._session_timer_backoff_sleep = _fake_backoff

    call_id = new_call_id()
    with _patched_invite_env(block_loop=True):
        _drive_inbound_call_to_established(
            adapter, transport, call_id, session_expires="600;refresher=uas"
        )
        await _until(lambda: call_id in adapter._call_sessions)
        session = adapter._call_sessions[call_id]

        # First refresh -> 491 glare; second refresh -> 200 OK (proves the retry).
        responder = _ScriptedRefreshResponder(
            transport, session, [(491, "Request Pending"), (200, "OK")]
        )
        task = asyncio.create_task(responder.run())
        try:
            await _until(lambda: len(responder.attempts) >= 2, timeout=3.0)
            # Give any erroneous BYE a chance to be emitted (it must NOT be).
            await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert len(responder.attempts) >= 2, (
        "a 491 glare must trigger a retried refresh re-INVITE"
    )
    assert backoff_sleeps, "a 491 retry must wait a randomized backoff before retrying"
    assert not _sent_requests(transport, "BYE"), (
        "a 491 glare must NOT tear the call down (no BYE)"
    )
    assert not session.ended, "the call must stay up across a 491 glare retry"

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_refresh_408_byes_the_dialog() -> None:
    """A 408 Request Timeout to our refresh tears the dialog down (BYE).

    RFC 4028 §10: a timeout / 408 means the dialog is dead — BYE it. (The literal
    no-response timeout maps to the same :class:`RefreshTeardown` outcome; that
    mapping is covered by ``test_classify_refresh_timeout_is_teardown`` in
    ``tests/test_session_timer.py``. Here we drive the 408 final deterministically.)
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())

    first_released = asyncio.Event()
    se_sleeps: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        se_sleeps.append(secs)
        if len(se_sleeps) == 1:
            first_released.set()
            return
        await asyncio.Event().wait()

    adapter._session_timer_sleep = _fake_sleep

    call_id = new_call_id()
    with _patched_invite_env(block_loop=True):
        _drive_inbound_call_to_established(
            adapter, transport, call_id, session_expires="600;refresher=uas"
        )
        await _until(lambda: call_id in adapter._call_sessions)
        session = adapter._call_sessions[call_id]
        await _until(first_released.is_set)

        responder = _ScriptedRefreshResponder(
            transport, session, [(408, "Request Timeout")]
        )
        task = asyncio.create_task(responder.run())
        try:
            await _until(lambda: bool(_sent_requests(transport, "BYE")), timeout=3.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert _sent_requests(transport, "BYE"), "a 408 refresh must BYE the dialog"
    assert session.ended, "the session must be ended after the 408 BYE"

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_refresh_481_byes_the_dialog() -> None:
    """A 481 Call Leg/Transaction Does Not Exist to our refresh BYEs the dialog.

    RFC 4028 §10: 481 (like 408) means the dialog is gone — BYE it.
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())

    first_released = asyncio.Event()
    se_sleeps: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        se_sleeps.append(secs)
        if len(se_sleeps) == 1:
            first_released.set()
            return
        await asyncio.Event().wait()

    adapter._session_timer_sleep = _fake_sleep

    call_id = new_call_id()
    with _patched_invite_env(block_loop=True):
        _drive_inbound_call_to_established(
            adapter, transport, call_id, session_expires="600;refresher=uas"
        )
        await _until(lambda: call_id in adapter._call_sessions)
        session = adapter._call_sessions[call_id]
        await _until(first_released.is_set)

        responder = _ScriptedRefreshResponder(
            transport, session, [(481, "Call/Transaction Does Not Exist")]
        )
        task = asyncio.create_task(responder.run())
        try:
            await _until(lambda: bool(_sent_requests(transport, "BYE")), timeout=3.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert _sent_requests(transport, "BYE"), "a 481 refresh must BYE the dialog"
    assert session.ended, "the session must be ended after the 481 BYE"

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_refresh_transient_5xx_continues_without_bye() -> None:
    """A transient 5xx to our refresh must NOT BYE — log a warning and continue.

    RFC 4028 §10: only timeout/408/481 mean a dead dialog. A 503 is transient; the
    next SE/2 tick (or the peer's own deadline) still protects liveness, so the call
    stays up.
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())

    se_sleeps: list[float] = []
    release_budget = 2

    async def _fake_sleep(secs: float) -> None:
        se_sleeps.append(secs)
        if len(se_sleeps) <= release_budget:
            return
        await asyncio.Event().wait()

    adapter._session_timer_sleep = _fake_sleep

    call_id = new_call_id()
    with _patched_invite_env(block_loop=True):
        _drive_inbound_call_to_established(
            adapter, transport, call_id, session_expires="600;refresher=uas"
        )
        await _until(lambda: call_id in adapter._call_sessions)
        session = adapter._call_sessions[call_id]

        # First refresh -> 503 (transient); the watchdog must continue to a 2nd refresh.
        responder = _ScriptedRefreshResponder(
            transport, session, [(503, "Service Unavailable"), (200, "OK")]
        )
        task = asyncio.create_task(responder.run())
        try:
            await _until(lambda: len(responder.attempts) >= 2, timeout=3.0)
            await asyncio.sleep(0.02)  # let any erroneous BYE surface
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert len(responder.attempts) >= 2, (
        "after a transient 5xx the watchdog must continue and refresh again"
    )
    assert not _sent_requests(transport, "BYE"), (
        "a transient 5xx must NOT tear the call down (no BYE)"
    )
    assert not session.ended, "the call must stay up across a transient 5xx refresh"

    await adapter.disconnect()


# ===========================================================================
# (g) FINDING 2 — Require: timer is gated on the peer's Supported: timer.
# ===========================================================================
@pytest.mark.asyncio
async def test_inbound_timer_ignorant_peer_omits_require_timer() -> None:
    """A peer that did NOT advertise Supported: timer gets NO Require: timer.

    RFC 4028 §9: ``Require: timer`` is only valid when the refresher is the UAC,
    which a timer-ignorant UAC can never be. We still insert our Session-Expires +
    Supported: timer and become the (UAS) refresher, but MUST omit Require: timer.
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())
    call_id = new_call_id()
    # No Session-Expires AND no Supported: timer -> the peer is timer-ignorant.
    invite = SipRequest.parse(
        _make_invite(_SDP_PLAIN, call_id, session_expires=None, supported_timer=False)
    )

    with _patched_invite_env():
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: any(m.startswith("SIP/2.0 200") for m in transport.sent))
        await asyncio.sleep(0)

    ok = next(r for r in _sent_responses(transport) if r.status_code == 200)
    # We still engage timers as the UAS: Session-Expires + Supported: timer present.
    se_hdr = ok.header("Session-Expires")
    assert se_hdr is not None, "we still insert our own Session-Expires (UAS refresher)"
    assert SessionExpires.parse(se_hdr).delta == 600
    supported = ",".join(ok.headers_all("Supported")).lower()
    assert "timer" in supported, "the 200 OK still advertises Supported: timer"
    # But Require: timer MUST be absent for a timer-ignorant peer (RFC 4028 §9).
    require = ",".join(ok.headers_all("Require")).lower()
    assert "timer" not in require, (
        f"Require: timer must be OMITTED to a timer-ignorant UAC; got {require!r}"
    )

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_inbound_uac_refresher_still_requires_timer() -> None:
    """An inbound INVITE pinning refresher=uac still gets Require: timer in the 2xx.

    RFC 4028 §9: when the refresher is the UAC the UAS MUST add Require: timer. This
    locks the positive branch so the §9 gating did not over-correct.
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())
    call_id = new_call_id()
    invite = SipRequest.parse(
        _make_invite(
            _SDP_PLAIN,
            call_id,
            session_expires="600;refresher=uac",
            supported_timer=True,
        )
    )

    with _patched_invite_env():
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        await _until(lambda: any(m.startswith("SIP/2.0 200") for m in transport.sent))
        await asyncio.sleep(0)

    ok = next(r for r in _sent_responses(transport) if r.status_code == 200)
    se = SessionExpires.parse(ok.header("Session-Expires") or "")
    assert se.refresher is Refresher.UAC, "the peer pinned refresher=uac; honour it"
    require = ",".join(ok.headers_all("Require")).lower()
    assert "timer" in require, (
        f"refresher=uac MUST carry Require: timer (RFC 4028 §9); got {require!r}"
    )

    await adapter.disconnect()


# ===========================================================================
# (h) FINDING 3 — a held call's refresh re-asserts hold (sendonly), not sendrecv.
# ===========================================================================
@pytest.mark.asyncio
async def test_held_call_refresh_offers_sendonly() -> None:
    """A call on hold whose watchdog fires offers a=sendonly in the refresh re-INVITE.

    A session refresh re-asserts state; it must not silently un-hold the call. The
    refresh re-INVITE of a held call must therefore offer ``sendonly`` (mirroring the
    re-INVITE direction logic), never ``sendrecv``.
    """
    transport = _FakeTransport()
    adapter, _manager = await _build_adapter_with_real_manager(transport, _media_cfg())

    # The first SE/2 sleep BLOCKS until the test opens ``hold_set`` — so the refresh
    # cannot fire before the call is put on hold (no on_hold race). Later sleeps block.
    se_sleeps: list[float] = []
    hold_set = asyncio.Event()
    first_armed = asyncio.Event()

    async def _fake_sleep(secs: float) -> None:
        se_sleeps.append(secs)
        if len(se_sleeps) == 1:
            first_armed.set()
            await hold_set.wait()  # release only once the test has set on_hold
            return
        await asyncio.Event().wait()

    adapter._session_timer_sleep = _fake_sleep

    call_id = new_call_id()
    with _patched_invite_env(block_loop=True):
        _drive_inbound_call_to_established(
            adapter, transport, call_id, session_expires="600;refresher=uas"
        )
        await _until(lambda: call_id in adapter._call_sessions)
        session = adapter._call_sessions[call_id]
        # Wait until the watchdog has armed its first SE/2 sleep, THEN put the call on
        # hold and release the sleep — so the refresh is offered with on_hold set.
        await _until(first_armed.is_set)
        session.on_hold = True
        hold_set.set()

        responder = _ScriptedRefreshResponder(transport, session, [(200, "OK")])
        task = asyncio.create_task(responder.run())
        try:
            await _until(lambda: bool(_sent_requests(transport, "INVITE")), timeout=3.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    refreshes = _sent_requests(transport, "INVITE")
    assert refreshes, "the held call must still emit a refresh re-INVITE"
    refresh = refreshes[0]
    assert "sendonly" in refresh.body, (
        "a held call's refresh must re-assert hold (a=sendonly), not un-hold it"
    )
    assert "sendrecv" not in refresh.body, (
        "a held call's refresh must NOT offer sendrecv (silent un-hold)"
    )

    await adapter.disconnect()


# ===========================================================================
# (e) outbound place_call carries SE + Supported: timer; a 422 retries with raised SE.
# ===========================================================================
@pytest.mark.asyncio
async def test_outbound_invite_carries_session_expires_and_retries_on_422() -> None:
    """place_call offers Session-Expires + Supported: timer; 422 raises SE, retry."""
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_real_manager(transport, _media_cfg())

    # Mark the registration as registered so place_call can source the call.
    for state in manager._by_extension.values():
        state.registered = True

    # A scripted responder: the FIRST INVITE (CSeq 1) is answered 422 with Min-SE 1200;
    # the retry (raised SE) is answered 2xx so the call establishes. Responses are fed
    # into the transport's per-call sink (the adapter registers a _QueueSink there).
    answered: dict[int, bool] = {}

    async def _drive_outbound() -> None:
        # Wait until the first INVITE is on the wire.
        await _until(lambda: bool(_sent_requests(transport, "INVITE")))
        while True:
            for req in _sent_requests(transport, "INVITE"):
                cseq = req.header("CSeq") or ""
                num = int(cseq.split()[0]) if cseq.split() else 0
                if answered.get(num):
                    continue
                answered[num] = True
                if req.header("Session-Expires") is None:
                    continue  # only react to the session-timer INVITEs
                offered = SessionExpires.parse(req.header("Session-Expires") or "")
                sink = transport._sinks.get(_call_id_of(req))
                if sink is None:
                    continue
                if offered.delta < 1200:
                    resp = _build_response_for_outbound(
                        req,
                        422,
                        "Session Interval Too Small",
                        extra=[("Min-SE", "1200")],
                    )
                else:
                    resp = _build_2xx_for_outbound(req)
                await sink.on_response(SipResponse.parse(resp))  # type: ignore[attr-defined]
            await asyncio.sleep(0.001)

    driver = asyncio.create_task(_drive_outbound())
    with _patched_invite_env():
        try:
            returned_call_id = await adapter.place_call("1001")
        finally:
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver

    invites = _sent_requests(transport, "INVITE")
    assert len(invites) >= 2, (
        f"expected an initial INVITE + a 422 retry; got {len(invites)} INVITEs"
    )
    first = invites[0]
    # The initial INVITE carries Session-Expires + Supported: timer.
    assert first.header("Session-Expires") is not None, (
        "the outbound INVITE must offer Session-Expires"
    )
    assert "timer" in ",".join(first.headers_all("Supported")).lower(), (
        "the outbound INVITE must advertise Supported: timer"
    )
    first_se = SessionExpires.parse(first.header("Session-Expires") or "")
    assert first_se.delta == 600, (
        f"initial SE should be the configured 600; got {first_se.delta}"
    )
    # The retry raised SE to the peer's Min-SE (1200).
    retry = invites[1]
    retry_se = SessionExpires.parse(retry.header("Session-Expires") or "")
    assert retry_se.delta == 1200, (
        f"the 422 retry must raise SE to the peer Min-SE 1200; got {retry_se.delta}"
    )
    assert returned_call_id

    await adapter.disconnect()


# A digest challenge the outbound auth path can answer (ext 1000 / password "fake").
_OUTBOUND_PROXY_CHALLENGE = (
    'Digest realm="pbx.example.test", nonce="abc123", algorithm=MD5, qop="auth"'
)


# ===========================================================================
# (e2) outbound: a 422 that arrives AFTER a 407 auth challenge is retried (SE
#      raised) and the call still connects — NOT mis-reported as a failure.
# ===========================================================================
@pytest.mark.asyncio
async def test_outbound_422_after_auth_challenge_retries_and_connects() -> None:
    """A 407-then-422 gateway: the post-auth 422 raises SE, re-sends, and connects.

    Regression for the ordering gap where the 422 (Session Interval Too Small)
    handler only inspected the FIRST awaited response: on a proxy-auth gateway the
    first response is the 407, the authed INVITE draws the 422, and the SE-raise
    retry never ran → the agent was told the call FAILED. The raise-SE retry must
    apply to whichever transaction yields the final response, carrying the auth
    header through so it stays authenticated.
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_real_manager(transport, _media_cfg())
    for state in manager._by_extension.values():
        state.registered = True

    # Scripted gateway: challenge the UNAUTHENTICATED INVITE (407); once authed,
    # 422 while our offered SE is below the peer Min-SE (1200); 200 once SE >= 1200.
    answered: dict[int, bool] = {}

    async def _drive_outbound() -> None:
        await _until(lambda: bool(_sent_requests(transport, "INVITE")))
        while True:
            for req in _sent_requests(transport, "INVITE"):
                cseq = req.header("CSeq") or ""
                num = int(cseq.split()[0]) if cseq.split() else 0
                if answered.get(num):
                    continue
                answered[num] = True
                sink = transport._sinks.get(_call_id_of(req))
                if sink is None:
                    continue
                if req.header("Proxy-Authorization") is None:
                    resp = _build_response_for_outbound(
                        req,
                        407,
                        "Proxy Authentication Required",
                        extra=[("Proxy-Authenticate", _OUTBOUND_PROXY_CHALLENGE)],
                    )
                else:
                    offered = SessionExpires.parse(req.header("Session-Expires") or "")
                    if offered.delta < 1200:
                        resp = _build_response_for_outbound(
                            req,
                            422,
                            "Session Interval Too Small",
                            extra=[("Min-SE", "1200")],
                        )
                    else:
                        resp = _build_2xx_for_outbound(req)
                await sink.on_response(SipResponse.parse(resp))  # type: ignore[attr-defined]
            await asyncio.sleep(0.001)

    driver = asyncio.create_task(_drive_outbound())
    with _patched_invite_env():
        try:
            returned_call_id = await adapter.place_call("1001")
        finally:
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver

    # The call SUCCEEDED — the agent is NOT told it failed.
    assert returned_call_id

    invites = _sent_requests(transport, "INVITE")
    assert len(invites) >= 3, (
        "expected INVITE#1 (unauth) + INVITE#2 (auth) + INVITE#3 (auth + raised SE); "
        f"got {len(invites)} INVITEs"
    )
    # The final INVITE is authenticated AND carries the raised Session-Expires — the
    # post-auth 422 retry both stayed authed and raised SE to the peer Min-SE.
    final = invites[-1]
    assert final.header("Proxy-Authorization") is not None, (
        "the post-auth 422 retry must remain authenticated"
    )
    final_se = SessionExpires.parse(final.header("Session-Expires") or "")
    assert final_se.delta >= 1200, (
        f"the post-auth 422 retry must raise SE to >= peer Min-SE 1200; "
        f"got {final_se.delta}"
    )

    await adapter.disconnect()


# ===========================================================================
# (e3) outbound: a SECOND 422 after the SE-raise (still after auth) is a genuine
#      failure — the retry is capped at one, so the loop is BOUNDED (no spin).
# ===========================================================================
@pytest.mark.asyncio
async def test_outbound_second_422_after_auth_se_raise_fails_cleanly() -> None:
    """A gateway that 422s even the raised-SE authed INVITE fails cleanly, bounded.

    Guards the retry cap: after ONE SE-raise (post auth) a repeat 422 is a real
    failure, so exactly three INVITEs go out (unauth, auth, auth+raised-SE) and
    ``place_call`` raises ``OutboundCallFailed`` — the bounded loop never spins.
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_real_manager(transport, _media_cfg())
    for state in manager._by_extension.values():
        state.registered = True

    answered: dict[int, bool] = {}

    async def _drive_outbound() -> None:
        await _until(lambda: bool(_sent_requests(transport, "INVITE")))
        while True:
            for req in _sent_requests(transport, "INVITE"):
                cseq = req.header("CSeq") or ""
                num = int(cseq.split()[0]) if cseq.split() else 0
                if answered.get(num):
                    continue
                answered[num] = True
                sink = transport._sinks.get(_call_id_of(req))
                if sink is None:
                    continue
                if req.header("Proxy-Authorization") is None:
                    resp = _build_response_for_outbound(
                        req,
                        407,
                        "Proxy Authentication Required",
                        extra=[("Proxy-Authenticate", _OUTBOUND_PROXY_CHALLENGE)],
                    )
                else:
                    # ALWAYS 422 once authed — even the raised-SE retry. An uncapped
                    # loop would spin forever escalating SE; the cap must stop it.
                    resp = _build_response_for_outbound(
                        req,
                        422,
                        "Session Interval Too Small",
                        extra=[("Min-SE", "1200")],
                    )
                await sink.on_response(SipResponse.parse(resp))  # type: ignore[attr-defined]
            await asyncio.sleep(0.001)

    driver = asyncio.create_task(_drive_outbound())
    with _patched_invite_env():
        try:
            with pytest.raises(OutboundCallFailed) as excinfo:
                # wait_for is a safety net: a regression into an infinite retry loop
                # fails as a timeout instead of hanging the suite.
                await asyncio.wait_for(adapter.place_call("1001"), timeout=5.0)
        finally:
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver

    assert excinfo.value.status == 422, (
        f"a repeat 422 must fail as 422; got {excinfo.value.status}"
    )
    invites = _sent_requests(transport, "INVITE")
    assert len(invites) == 3, (
        "the SE-raise retry is capped at one: expected exactly 3 INVITEs "
        f"(unauth, auth, auth+raised-SE); got {len(invites)} — the loop is not bounded"
    )

    await adapter.disconnect()


# ===========================================================================
# (e3b) outbound: a 422 whose Min-SE we cannot parse fails CLOSED (typed 422),
#       never a raw ValueError crash out of the UAC coroutine (ADR-0081).
# ===========================================================================
@pytest.mark.asyncio
async def test_outbound_422_with_malformed_min_se_fails_closed() -> None:
    """A 422 whose Min-SE is unparseable fails as a typed 422, not a raw ValueError.

    RFC 4028 §6: a 422 Session Interval Too Small carries a Min-SE naming the interval
    to raise the Session-Expires to. If that Min-SE is malformed (not delta-seconds) we
    have no interval to raise to — exactly like a 422 with NO Min-SE — so origination
    must fail CLOSED as ``OutboundCallFailed(422)``, the typed failure ``place_call``
    contracts to emit, never a raw ``ValueError`` escaping the UAC coroutine (ADR-0081
    fail-closed; RFC 3261 §8.1.3.2 robustness). Before the fix the unguarded
    ``parse_min_se`` raised ``ValueError`` straight out of ``place_call``.
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_real_manager(transport, _media_cfg())
    for state in manager._by_extension.values():
        state.registered = True

    answered: dict[int, bool] = {}

    async def _drive_outbound() -> None:
        await _until(lambda: bool(_sent_requests(transport, "INVITE")))
        while True:
            for req in _sent_requests(transport, "INVITE"):
                cseq = req.header("CSeq") or ""
                num = int(cseq.split()[0]) if cseq.split() else 0
                if answered.get(num):
                    continue
                answered[num] = True
                if req.header("Session-Expires") is None:
                    continue  # only react to the session-timer INVITEs
                sink = transport._sinks.get(_call_id_of(req))
                if sink is None:
                    continue
                # A 422 whose Min-SE is not a delta-seconds value at all.
                resp = _build_response_for_outbound(
                    req,
                    422,
                    "Session Interval Too Small",
                    extra=[("Min-SE", "not-a-number")],
                )
                await sink.on_response(SipResponse.parse(resp))  # type: ignore[attr-defined]
            await asyncio.sleep(0.001)

    driver = asyncio.create_task(_drive_outbound())
    with _patched_invite_env():
        try:
            with pytest.raises(OutboundCallFailed) as excinfo:
                # wait_for safety net: a regression that hangs fails as a timeout
                # instead of hanging the whole suite.
                await asyncio.wait_for(adapter.place_call("1001"), timeout=5.0)
        finally:
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver

    assert excinfo.value.status == 422, (
        f"a malformed-Min-SE 422 must fail closed as a typed 422; "
        f"got status {excinfo.value.status}"
    )
    # A malformed Min-SE gives no interval to raise to, so there is no SE-raise retry:
    # exactly the initial INVITE goes out (fail closed like a 422 with no Min-SE).
    invites = _sent_requests(transport, "INVITE")
    assert len(invites) == 1, (
        f"a malformed-Min-SE 422 must NOT trigger an SE-raise retry; "
        f"got {len(invites)} INVITEs"
    )

    await adapter.disconnect()


# ===========================================================================
# (e4) outbound: a flood of stale wrong-CSeq INVITE finals is BOUNDED — the
#      await-final loop fails cleanly instead of extending the call unboundedly.
# ===========================================================================
@pytest.mark.asyncio
async def test_outbound_stale_final_flood_is_bounded() -> None:
    """A peer flooding stale finals for an OLD CSeq fails cleanly, never hangs.

    ``_await_invite_final_for_cseq`` skips finals that do not match the CSeq of the
    transaction it just sent (the W1 retransmit filter). Each stale final resets the
    sink's ~35s idle ``get()`` timeout, so WITHOUT an absolute bound a peer
    retransmitting finals for a PRIOR CSeq (< 35s apart) would keep the await-final
    loop — and the whole outbound call — alive indefinitely when no ring-timeout is
    armed. The loop must cap the skipped-final count and fail the call cleanly
    (``OutboundCallFailed``) so ``place_call`` returns a clean failure instead of
    hanging (codex review of #433).
    """
    transport = _FakeTransport()
    adapter, manager = await _build_adapter_with_real_manager(transport, _media_cfg())
    for state in manager._by_extension.values():
        state.registered = True

    async def _flood_stale_finals() -> None:
        # Challenge INVITE#1 (CSeq 1) so the adapter sends the authed INVITE#2
        # (CSeq 2) and enters the await-final loop expecting CSeq 2.
        await _until(lambda: bool(_sent_requests(transport, "INVITE")))
        first = _sent_requests(transport, "INVITE")[0]
        sink = transport._sinks.get(_call_id_of(first))
        assert sink is not None
        challenge = _build_response_for_outbound(
            first,
            407,
            "Proxy Authentication Required",
            extra=[("Proxy-Authenticate", _OUTBOUND_PROXY_CHALLENGE)],
        )
        await sink.on_response(SipResponse.parse(challenge))  # type: ignore[attr-defined]
        await _until(lambda: len(_sent_requests(transport, "INVITE")) >= 2)
        # Flood stale finals for the OLD CSeq (CSeq 1) — NEVER the expected CSeq 2 —
        # so the await-final loop skips every one. An unbounded loop would spin here
        # forever; the cap must fail the call cleanly.
        while True:
            await sink.on_response(SipResponse.parse(challenge))  # type: ignore[attr-defined]
            await asyncio.sleep(0)

    driver = asyncio.create_task(_flood_stale_finals())
    with _patched_invite_env():
        try:
            with pytest.raises(OutboundCallFailed):
                # wait_for is the hang detector: an unbounded await-final loop fails
                # as a timeout (not OutboundCallFailed) and the test fails visibly.
                await asyncio.wait_for(adapter.place_call("1001"), timeout=5.0)
        finally:
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver

    await adapter.disconnect()


# ---------------------------------------------------------------------------
# Response builders for the inbound (UAS) refresh re-INVITE the SESSION sent.
# We are the UAC of the refresh; the gateway (test) answers it.
# ---------------------------------------------------------------------------
def _build_2xx_for(req: SipRequest, session: object) -> str:
    """A 200 OK answering a refresh re-INVITE the session sent, with an SDP body."""
    return _build_response_for(req, session, 200, "OK", with_sdp=True)


def _build_response_for(
    req: SipRequest,
    session: object,
    status: int,
    reason: str,
    *,
    with_sdp: bool = False,
) -> str:
    via = req.header("Via") or ""
    from_hdr = req.header("From") or ""
    to_hdr = req.header("To") or ""
    call_id = req.header("Call-ID") or ""
    cseq = req.header("CSeq") or ""
    body = ""
    extra = ""
    if with_sdp:
        body = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            "m=audio 20002 RTP/AVP 0 8\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=recvonly\r\n"
        )
        extra = "Content-Type: application/sdp\r\n"
    return (
        f"SIP/2.0 {status} {reason}\r\n"
        f"Via: {via}\r\n"
        f"From: {from_hdr}\r\n"
        f"To: {to_hdr}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq}\r\n"
        f"{extra}"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        f"\r\n"
        f"{body}"
    )


# ---------------------------------------------------------------------------
# Response builders for the OUTBOUND INVITE (we are the UAC; the test gateway answers).
# ---------------------------------------------------------------------------
def _call_id_of(req: SipRequest) -> str:
    return req.header("Call-ID") or ""


def _build_response_for_outbound(
    req: SipRequest,
    status: int,
    reason: str,
    *,
    extra: list[tuple[str, str]] | None = None,
) -> str:
    via = req.header("Via") or ""
    from_hdr = req.header("From") or ""
    to_hdr = req.header("To") or ""
    call_id = req.header("Call-ID") or ""
    cseq = req.header("CSeq") or ""
    extra_lines = "".join(f"{name}: {value}\r\n" for name, value in (extra or []))
    return (
        f"SIP/2.0 {status} {reason}\r\n"
        f"Via: {via}\r\n"
        f"From: {from_hdr}\r\n"
        f"To: {to_hdr};tag={new_tag()}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq}\r\n"
        f"{extra_lines}"
        f"Content-Length: 0\r\n"
        f"\r\n"
    )


def _build_2xx_for_outbound(req: SipRequest) -> str:
    body = (
        "v=0\r\n"
        "o=- 0 0 IN IP4 127.0.0.1\r\n"
        "s=-\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "t=0 0\r\n"
        "m=audio 30000 RTP/AVP 0 8\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=sendrecv\r\n"
    )
    via = req.header("Via") or ""
    from_hdr = req.header("From") or ""
    to_hdr = req.header("To") or ""
    call_id = req.header("Call-ID") or ""
    cseq = req.header("CSeq") or ""
    return (
        "SIP/2.0 200 OK\r\n"
        f"Via: {via}\r\n"
        f"From: {from_hdr}\r\n"
        f"To: {to_hdr};tag={new_tag()}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq}\r\n"
        "Contact: <sip:1001@127.0.0.1:5061;transport=tls>\r\n"
        "Content-Type: application/sdp\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "\r\n"
        f"{body}"
    )
