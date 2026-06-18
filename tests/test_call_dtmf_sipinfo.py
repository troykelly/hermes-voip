"""SIP INFO DTMF on the CallSession: in-dialog receive + send (ADR-0010/0034).

An inbound ``INFO`` carrying ``application/dtmf-relay`` / ``application/dtmf`` is
answered 200 OK and surfaced through the session's settable ``on_dtmf`` callback
(the adapter wires it to ``CallLoop.feed_dtmf``). On send, ``send_dtmf_info``
emits one in-dialog INFO per digit; ``send_dtmf`` in SIP-INFO send mode routes
there instead of to the media engine.

Fakes only (``pbx.example.test``, ext ``1000``/``2000``, ``198.51.100.x``).
"""

from __future__ import annotations

import pytest

from hermes_voip.call import CallSession
from hermes_voip.dialog import Dialog
from hermes_voip.digest import DigestCredentials
from hermes_voip.dtmf import DtmfSendMode
from hermes_voip.dtmf_sipinfo import parse_dtmf_info
from hermes_voip.incall import LocalMediaSession
from hermes_voip.message import SipRequest, SipResponse
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.sdp import Codec

pytestmark = pytest.mark.asyncio

_PCMU = Codec(payload_type=0, encoding="PCMU", clock_rate=8000)
_MEDIA = LocalMediaSession(
    local_address="198.51.100.7", port=40000, codecs=(_PCMU,), session_id=55555
)
_CREDENTIALS = DigestCredentials("1000", "s3cr3t")


class _FakeSignaling:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


class _FakeMedia:
    def __init__(self) -> None:
        self.dtmf: list[str] = []

    async def set_hold(self, on_hold: bool) -> None:
        """No-op."""

    async def send_dtmf(self, digits: str) -> None:
        self.dtmf.append(digits)

    async def stop(self) -> None:
        """No-op."""


def _dialog() -> Dialog:
    return Dialog(
        call_id="call-1",
        local_uri="sip:1000@pbx.example.test",
        local_tag="ours",
        remote_uri="sip:2000@pbx.example.test",
        remote_tag="theirs",
        remote_target="sip:2000@198.51.100.99:5061;transport=tls",
        route_set=(),
        local_contact="<sip:1000@198.51.100.7:5061;transport=tls>",
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
        local_cseq=2,
        sdp_version=0,
    )


def _session(signaling: _FakeSignaling, media: _FakeMedia, **kw: object) -> CallSession:
    return CallSession(
        dialog=_dialog(),
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id="call-1"),
        local_media=_MEDIA,
        credentials=_CREDENTIALS,
        response_timeout=2.0,
        **kw,  # type: ignore[arg-type]  # test-only extra kwargs
    )


def _info(content_type: str, body: str, *, cseq: int = 9) -> SipRequest:
    return SipRequest(
        method="INFO",
        request_uri="sip:1000@198.51.100.7:5061",
        headers=(
            ("Via", "SIP/2.0/TLS 198.51.100.99:5061;branch=z9hG4bK-in"),
            ("From", "<sip:2000@pbx.example.test>;tag=theirs"),
            ("To", "<sip:1000@pbx.example.test>;tag=ours"),
            ("Call-ID", "call-1"),
            ("CSeq", f"{cseq} INFO"),
            ("Content-Type", content_type),
        ),
        body=body,
    )


def _last_request(signaling: _FakeSignaling, method: str) -> SipRequest:
    for text in reversed(signaling.sent):
        if text.startswith("SIP/2.0 "):
            continue
        request = SipRequest.parse(text)
        if request.method == method:
            return request
    msg = f"no {method} was sent"
    raise AssertionError(msg)


# --- receive ----------------------------------------------------------------


async def test_inbound_dtmf_relay_info_answers_200_and_surfaces_digit() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    digits: list[str] = []
    session = _session(signaling, media)
    session.on_dtmf = digits.append
    await session.handle_request(
        _info("application/dtmf-relay", "Signal=5\r\nDuration=160\r\n")
    )
    response = SipResponse.parse(signaling.sent[-1])
    assert response.status_code == 200
    assert digits == ["5"]


async def test_inbound_bare_dtmf_info_surfaces_digit() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    digits: list[str] = []
    session = _session(signaling, media)
    session.on_dtmf = digits.append
    await session.handle_request(_info("application/dtmf", "7"))
    assert SipResponse.parse(signaling.sent[-1]).status_code == 200
    assert digits == ["7"]


async def test_inbound_non_dtmf_info_still_answers_200_no_digit() -> None:
    """A non-DTMF INFO body is acknowledged (200) but surfaces no digit."""
    signaling, media = _FakeSignaling(), _FakeMedia()
    digits: list[str] = []
    session = _session(signaling, media)
    session.on_dtmf = digits.append
    await session.handle_request(_info("application/media_control+xml", "<x/>"))
    assert SipResponse.parse(signaling.sent[-1]).status_code == 200
    assert digits == []


async def test_inbound_dtmf_info_without_callback_does_not_crash() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)  # on_dtmf left None
    await session.handle_request(_info("application/dtmf-relay", "Signal=1\r\n"))
    assert SipResponse.parse(signaling.sent[-1]).status_code == 200


# --- send -------------------------------------------------------------------


async def test_send_dtmf_info_emits_in_dialog_info_per_digit() -> None:
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media)
    await session.send_dtmf_info("12", duration_ms=160)
    infos = [
        SipRequest.parse(t)
        for t in signaling.sent
        if not t.startswith("SIP/2.0 ") and SipRequest.parse(t).method == "INFO"
    ]
    assert len(infos) == 2
    assert parse_dtmf_info(infos[0].header("Content-Type") or "", infos[0].body) == "1"
    assert parse_dtmf_info(infos[1].header("Content-Type") or "", infos[1].body) == "2"
    # Each INFO advanced the dialog CSeq (no two share a number).
    cseqs = [info.header("CSeq") for info in infos]
    assert cseqs[0] != cseqs[1]
    assert media.dtmf == []  # SIP-INFO send never touched the media engine


async def test_send_dtmf_routes_to_sip_info_when_mode_is_sip_info() -> None:
    """send_dtmf in SIP-INFO send mode emits INFO, not RFC 4733 media tones."""
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media, dtmf_send_mode=DtmfSendMode.SIP_INFO)
    await session.send_dtmf("9")
    info = _last_request(signaling, "INFO")
    assert parse_dtmf_info(info.header("Content-Type") or "", info.body) == "9"
    assert media.dtmf == []


async def test_send_dtmf_routes_to_media_when_mode_is_rfc4733() -> None:
    """The default (RFC 4733 / in-band) send still delegates to the media engine."""
    signaling, media = _FakeSignaling(), _FakeMedia()
    session = _session(signaling, media, dtmf_send_mode=DtmfSendMode.RFC4733)
    await session.send_dtmf("8")
    assert media.dtmf == ["8"]
