"""Adapter wiring for caller modes (ADR-0020 Phase 1), against the REAL base.

These tests drive ``VoipAdapter`` (a real ``BasePlatformAdapter`` subclass) and
assert the caller-mode integration at its three seams:

* ``_handle_inbound_invite``: classify the caller; DENY => ``603 Decline`` in the
  pre-200-OK reject window with no engine/agent; ALLOW/GREY => proceed and set
  ``guard_state.privileged`` from the mode.
* ``place_call`` (outbound): OUTBOUND-TASK mode => ``privileged=False`` (untrusted
  callee) and the callee identity recorded in ``_call_info`` (fixes "I don't
  know you").
* ``_deliver_turn``: a spotlighted per-mode persona preamble is prepended and the
  caller transcript is delimited as untrusted DATA.

Like ``test_adapter.py`` these require the optional ``hermes`` extra and run in
the hermes-contract CI job; fakes only (``pbx.example.test`` / ext ``1000``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig

from hermes_voip.caller_modes import CallerMode, CallerModeConfig, Normalization
from hermes_voip.config import ExtensionConfig
from hermes_voip.manager import NewCall
from hermes_voip.message import SipRequest, new_call_id, new_tag
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import GuardSessionState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from hermes_voip.adapter import VoipAdapter
    from hermes_voip.providers.audio import PcmFrame
    from hermes_voip.providers.tts import TtsStream


# --- fakes (mirrors test_adapter.py, kept local to this module) -------------


class _FakeTransport:
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

    async def connect(self) -> None: ...

    async def aclose(self) -> None: ...

    def bind_manager(self, manager: object) -> None: ...

    def add_call(self, call_id: str, sink: object) -> None:
        self._calls[call_id] = sink

    def remove_call(self, call_id: str, sink: object | None = None) -> None:
        if sink is not None and self._calls.get(call_id) is not sink:
            return
        self._calls.pop(call_id, None)


class _FakeManager:
    def __init__(self, *, is_up: bool = True) -> None:
        self._is_up = is_up
        self._calls: dict[tuple[str, str, str], object] = {}
        self.connected = False
        self.closed = False

    @property
    def is_up(self) -> bool:
        return self._is_up

    async def connect(self, *, timeout: float = 10.0) -> bool:
        self.connected = True
        return self._is_up

    async def aclose(self) -> None:
        self.closed = True

    def add_call(self, dialog_id: tuple[str, str, str], consumer: object) -> None:
        self._calls[dialog_id] = consumer

    def remove_call(self, dialog_id: tuple[str, str, str]) -> None:
        self._calls.pop(dialog_id, None)


class _FakeTtsStream:
    def __init__(self) -> None:
        self._frames: list[PcmFrame] = []

    def __aiter__(self) -> AsyncIterator[PcmFrame]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[PcmFrame]:
        for frame in self._frames:  # always empty — fixes the async-gen shape
            yield frame

    async def cancel(self) -> None: ...


class _FakeTTS:
    def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
        return _FakeTtsStream()  # type: ignore[return-value]


class _FakeASR:
    async def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[object]:
        chunks: list[object] = []
        async for _ in audio:
            pass  # consume so the pump does not stall
        for chunk in chunks:  # always empty — forces the async-gen shape
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


_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": "1000",
    "HERMES_SIP_PASSWORD": "fake-password",
}


def _ext_config() -> ExtensionConfig:
    return ExtensionConfig(index=0, extension="1000", username="1000", password="fake")


_FAKE_SDP_OFFER = (
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


def _make_invite(*, caller: str, call_id: str | None = None) -> str:
    cid = call_id or new_call_id()
    ftag = new_tag()
    content_length = len(_FAKE_SDP_OFFER.encode("utf-8"))
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKfake\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:{caller}@pbx.example.test>;tag={ftag}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {cid}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:{caller}@127.0.0.1:60000;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{_FAKE_SDP_OFFER}"
    )


async def _build_adapter(
    transport: _FakeTransport,
    manager: _FakeManager,
    *,
    caller_modes: CallerModeConfig,
) -> VoipAdapter:
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))
    with (
        patch(
            "hermes_voip.adapter.load_gateway_config",
            return_value=MagicMock(
                host="pbx.example.test",
                transport="tls",
                port=5061,
                extensions=(
                    MagicMock(
                        index=0, extension="1000", username="1000", password="fake"
                    ),
                ),
                default_extension=MagicMock(extension="1000"),
                via_transport="TLS",
            ),
        ),
        patch("hermes_voip.adapter.load_media_config", return_value=MagicMock()),
        patch(
            "hermes_voip.adapter.load_caller_modes",
            return_value=caller_modes,
        ),
        patch("hermes_voip.adapter.build_providers", return_value=_fake_providers()),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()
        return adapter


def _grey_only() -> CallerModeConfig:
    return CallerModeConfig(
        allow=(),
        deny=(),
        grey=(),
        default_mode=CallerMode.GREY,
        normalization=Normalization.E164,
    )


def _deny_caller(caller: str) -> CallerModeConfig:
    return CallerModeConfig(
        allow=(),
        deny=(caller,),
        grey=(),
        default_mode=CallerMode.GREY,
        normalization=Normalization.E164,
    )


def _allow_caller(caller: str) -> CallerModeConfig:
    return CallerModeConfig(
        allow=(caller,),
        deny=(),
        grey=(),
        default_mode=CallerMode.GREY,
        normalization=Normalization.E164,
    )


# --- inbound DENY => 603, no engine, no agent --------------------------------


@pytest.mark.asyncio
async def test_inbound_deny_sends_603_and_starts_no_call() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    # The caller "9999" (the From user-part of _make_invite) is on the deny list.
    adapter = await _build_adapter(
        transport, manager, caller_modes=_deny_caller("9999")
    )

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(caller="9999", call_id=call_id))

    new_call = NewCall(registration=_ext_config(), invite=invite)
    # No RtpMediaTransport/CallSession/CallLoop patches: if the deny path tried to
    # build them it would blow up — proving the early return.
    await adapter._handle_inbound_invite(new_call)

    assert len(transport.sent) == 1
    assert "603 Decline" in transport.sent[0]
    assert call_id not in adapter._call_loops
    assert call_id not in adapter._call_sessions


@pytest.mark.asyncio
async def test_inbound_grey_sets_privileged_false() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager, caller_modes=_grey_only())

    captured: dict[str, GuardSessionState] = {}

    def _real_guard(call_id: str) -> GuardSessionState:
        state = GuardSessionState(call_id=call_id)
        captured[call_id] = state
        return state

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(caller="9999", call_id=call_id))

    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                local_port=20002,
                inbound_sample_rate=8000,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallSession",
            return_value=MagicMock(dialog_id=("c", "l", "r"), ended=False),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", side_effect=_real_guard),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        new_call = NewCall(registration=_ext_config(), invite=invite)
        adapter._on_inbound_invite(new_call)
        for _ in range(20):
            await asyncio.sleep(0)

    assert call_id in captured
    assert captured[call_id].privileged is False  # GREY => receptionist, no privilege


@pytest.mark.asyncio
async def test_inbound_allow_sets_privileged_true() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(
        transport, manager, caller_modes=_allow_caller("9999")
    )

    captured: dict[str, GuardSessionState] = {}

    def _real_guard(call_id: str) -> GuardSessionState:
        state = GuardSessionState(call_id=call_id)
        captured[call_id] = state
        return state

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(caller="9999", call_id=call_id))

    with (
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            return_value=MagicMock(
                connect=AsyncMock(return_value=True),
                stop=AsyncMock(return_value=None),
                local_port=20002,
                inbound_sample_rate=8000,
            ),
        ),
        patch(
            "hermes_voip.adapter.CallSession",
            return_value=MagicMock(dialog_id=("c", "l", "r"), ended=False),
        ),
        patch(
            "hermes_voip.adapter.CallLoop",
            return_value=MagicMock(run=AsyncMock(return_value=None)),
        ),
        patch("hermes_voip.adapter.GuardSessionState", side_effect=_real_guard),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        new_call = NewCall(registration=_ext_config(), invite=invite)
        adapter._on_inbound_invite(new_call)
        for _ in range(20):
            await asyncio.sleep(0)

    assert call_id in captured
    assert captured[call_id].privileged is True  # ALLOW => assistant, privileged


# --- persona preamble + untrusted-data delimiting in _deliver_turn -----------


@pytest.mark.asyncio
async def test_deliver_turn_prepends_receptionist_preamble_for_grey() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager, caller_modes=_grey_only())

    captured: list[str] = []

    async def _handler(event: object) -> None:
        captured.append(getattr(event, "text", ""))

    adapter.set_message_handler(_handler)

    call_id = new_call_id()
    adapter._call_info[call_id] = {
        "name": "9999",
        "remote_uri": "sip:9999@pbx.example.test",
        "type": "dm",
        "ended": False,
        "mode": CallerMode.GREY,
    }

    await adapter._deliver_turn(call_id, "is bob there?")
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.02)

    assert captured
    text = captured[0]
    lowered = text.lower()
    # The spotlighted receptionist persona preamble is present...
    assert "receptionist" in lowered
    # ...the caller's actual words are present...
    assert "is bob there?" in text
    # ...and they are delimited as untrusted DATA (not instructions).
    assert "untrusted" in lowered


@pytest.mark.asyncio
async def test_deliver_turn_uses_assistant_preamble_for_allow() -> None:
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    adapter = await _build_adapter(transport, manager, caller_modes=_grey_only())

    captured: list[str] = []

    async def _handler(event: object) -> None:
        captured.append(getattr(event, "text", ""))

    adapter.set_message_handler(_handler)

    call_id = new_call_id()
    adapter._call_info[call_id] = {
        "name": "9999",
        "remote_uri": "sip:9999@pbx.example.test",
        "type": "dm",
        "ended": False,
        "mode": CallerMode.ALLOW,
    }

    await adapter._deliver_turn(call_id, "hold my next call please")
    for _ in range(50):
        if captured:
            break
        await asyncio.sleep(0.02)

    assert captured
    assert "assistant" in captured[0].lower()
    assert "hold my next call please" in captured[0]


# --- outbound: OUTBOUND-TASK mode => privileged=False + callee identity ------
#
# The full outbound UAC handshake (INVITE/407/200/ACK/RTP) is exercised against a
# loopback gateway in tests/e2e/test_outbound_call.py, where the caller-mode
# assertions (privileged=False + callee identity + OUTBOUND mode) ride on the real
# place_call path — see test_outbound_call_runs_unprivileged_with_callee_identity.
