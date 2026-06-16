"""A reusable loopback *fake gateway* for end-to-end VoIP integration tests.

This is **test infrastructure**, not production code. It is the far-end SIP/RTP
peer the real plugin talks to over loopback sockets: a genuine TLS SIP server
(reusing :mod:`tests.transport._loopback`) plus a UDP RTP endpoint, wrapped in a
high-level driver that lets a test *script a whole inbound call* and assert what
the plugin does at each seam — REGISTER, OPTIONS qualify, INVITE/200/ACK, the
media plane (greeting out, caller speech in, agent reply out), and BYE teardown.

It is deliberately gateway-agnostic and uses only obvious fakes
(``pbx.example.test`` / ext ``1000`` / ``127.0.0.1`` for the loopback media, and
RFC-5737 ``198.51.100.x`` in the Via ``sent-by``) — no real host, extension,
credential, or PII.

Why this exists
---------------
The unit tests for the adapter fake the transport and patch ``CallSession`` to a
mock, so they never exercised the real SDP/dialog/RTP/rate path — which is how the
live no-audio bugs (resample 24 kHz→8 kHz, VAD 8 kHz vs 16 kHz, missing dialog
To-tag, un-answered OPTIONS qualify, teardown generator race) reached a real call
instead of CI. This harness drives the real stack end-to-end so those seams are
covered. Future transport/media work reuses it as a ready-made E2E peer.

Determinism
-----------
The RTP endpoint runs on a loopback UDP socket; inbound media is delivered as
explicit, test-paced datagrams (no real network jitter, no models). The plugin's
outbound RTP pacing is made instant by the test injecting a no-op ``sleep`` into
the engine (see :func:`tests.e2e.test_inbound_call` and the engine's ``sleep``
seam) — this harness only sends/receives datagrams and parses SIP.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from hermes_voip.media.audio import frame_to_ulaw, ulaw_to_frame
from hermes_voip.message import (
    SipRequest,
    SipResponse,
    build_request,
    new_branch,
    new_call_id,
    new_tag,
)
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.rtp import RtpPacket
from tests.transport._loopback import (
    LoopbackSipServer,
    Responder,
    client_ssl_context,
)

__all__ = [
    "FakeGateway",
    "FakeRtpEndpoint",
    "GatewayCall",
    "client_ssl_context",
    "g711_silence_frames",
    "g711_speech_frames",
]

# Fakes only (the repo is public): an RFC-5737 documentation address stands in for
# the "public" SDP connection address the gateway advertises, and an obvious fake
# extension. No real host, IP, extension, or credential appears here.
_FAKE_PUBLIC_ADDRESS = "198.51.100.10"
_G711_SAMPLE_RATE = 8000
_PTIME_MS = 20
# 8000 Hz * 20 ms = 160 samples = 320 PCM16 bytes = 160 G.711 octets per frame.
_SAMPLES_PER_FRAME = (_G711_SAMPLE_RATE * _PTIME_MS) // 1000

_TO_TAG_RE = re.compile(r";\s*tag=([^;,\s]+)", re.IGNORECASE)


def _to_tag_of(to_header: str | None) -> str | None:
    """Extract the dialog tag from a ``To`` header value (after the name-addr)."""
    if to_header is None:
        return None
    search_space = to_header.split(">", 1)[1] if ">" in to_header else to_header
    match = _TO_TAG_RE.search(search_space)
    return match.group(1) if match is not None else None


def _sdp_media_port(sdp: str) -> int:
    """Read the ``m=audio <port> ...`` port from an SDP body."""
    for line in sdp.splitlines():
        if line.startswith("m=audio "):
            return int(line.split()[1])
    msg = "SDP answer has no m=audio line"
    raise ValueError(msg)


def _pcm16_tone(num_frames: int, amplitude: int) -> list[bytes]:
    """``num_frames`` frames of a simple PCM16 square wave at ``amplitude``.

    A constant non-zero level is enough for a fake VAD model to score as speech;
    ``amplitude == 0`` yields digital silence. Each frame is one 20 ms G.711 frame
    (``_SAMPLES_PER_FRAME`` samples).
    """
    sample = amplitude.to_bytes(2, "little", signed=True)
    frame = sample * _SAMPLES_PER_FRAME
    return [frame] * num_frames


def g711_speech_frames(num_frames: int) -> list[bytes]:
    """``num_frames`` of 8 kHz PCM16 "speech" (constant tone) for inbound RTP."""
    return _pcm16_tone(num_frames, amplitude=8000)


def g711_silence_frames(num_frames: int) -> list[bytes]:
    """``num_frames`` of 8 kHz PCM16 silence for inbound RTP."""
    return _pcm16_tone(num_frames, amplitude=0)


class FakeRtpEndpoint(asyncio.DatagramProtocol):
    """A loopback UDP endpoint: sends inbound RTP, captures the plugin's outbound.

    Binds an ephemeral ``127.0.0.1`` UDP port (its address is what the fake SDP
    offer advertises so the plugin sends its RTP back here). Every received
    datagram is parsed as an :class:`~hermes_voip.rtp.RtpPacket` and recorded; the
    payload is decoded from G.711 to a :class:`PcmFrame` so a test can assert the
    plugin's outbound audio rate (8 kHz, the G.711 wire rate).
    """

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None
        self._dest: tuple[str, int] | None = None
        self._seq = 0
        self._ts = 0
        self._ssrc = 0x1234ABCD  # an obvious fake far-end SSRC
        self.received_packets: list[RtpPacket] = []
        self.received_frames: list[PcmFrame] = []
        self._received_event = asyncio.Event()

    async def start(self) -> int:
        """Bind a loopback UDP socket; return the OS-assigned port."""
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self,
            local_addr=("127.0.0.1", 0),
        )
        self._transport = transport
        return int(transport.get_extra_info("sockname")[1])

    @property
    def port(self) -> int:
        """The bound UDP port (valid after :meth:`start`)."""
        if self._transport is None:
            msg = "FakeRtpEndpoint not started"
            raise RuntimeError(msg)
        return int(self._transport.get_extra_info("sockname")[1])

    def aim_at(self, host: str, port: int) -> None:
        """Point inbound RTP at the plugin's advertised RTP ``host:port``."""
        self._dest = (host, port)

    # asyncio.DatagramProtocol -------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Parse + record one outbound RTP datagram from the plugin."""
        packet = RtpPacket.parse(data)
        self.received_packets.append(packet)
        # The plugin only ever sends G.711 mu-law (PCMU, payload type 0) on this
        # test's negotiated codec; decode to a PcmFrame so a test can assert the
        # wire rate (8 kHz) directly.
        self.received_frames.append(ulaw_to_frame(packet.payload, monotonic_ts_ns=0))
        self._received_event.set()

    def error_received(self, exc: Exception) -> None:  # pragma: no cover - env event
        """Ignore transient ICMP errors (a loopback peer may not be reading yet)."""

    # inbound media ------------------------------------------------------------

    def send_frame(self, pcm16_8k: bytes) -> None:
        """Encode one 8 kHz PCM16 frame to G.711 and send it as an RTP datagram."""
        if self._transport is None or self._dest is None:
            msg = "FakeRtpEndpoint must be started and aimed before sending"
            raise RuntimeError(msg)
        payload = frame_to_ulaw(
            PcmFrame(samples=pcm16_8k, sample_rate=_G711_SAMPLE_RATE, monotonic_ts_ns=0)
        )
        packet = RtpPacket(
            payload_type=0,  # PCMU
            sequence_number=self._seq,
            timestamp=self._ts,
            ssrc=self._ssrc,
            payload=payload,
        )
        self._transport.sendto(packet.pack(), self._dest)
        self._seq = (self._seq + 1) % (1 << 16)
        self._ts = (self._ts + _SAMPLES_PER_FRAME) % (1 << 32)

    def send_frames(self, frames: list[bytes]) -> None:
        """Send a sequence of 8 kHz PCM16 frames as consecutive RTP datagrams."""
        for frame in frames:
            self.send_frame(frame)

    async def wait_for_frames(self, count: int, *, timeout: float = 3.0) -> None:
        """Wait until at least ``count`` outbound RTP frames have been received."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.received_packets) < count:
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = (
                    f"only {len(self.received_packets)} outbound RTP frames "
                    f"received, wanted {count}"
                )
                raise TimeoutError(msg)
            self._received_event.clear()
            try:
                await asyncio.wait_for(self._received_event.wait(), remaining)
            except TimeoutError:
                continue

    def stop(self) -> None:
        """Close the UDP socket."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None


@dataclass(slots=True)
class GatewayCall:
    """The dialog state the fake gateway holds for one inbound call it placed.

    Mirrors the far-end (UAC) half of the dialog so the gateway can send the
    in-dialog ACK / BYE the plugin's 200 OK requires — crucially echoing the
    plugin's To-tag (the dialog-forming tag), so the plugin's
    :class:`~hermes_voip.manager.RegistrationManager` routes them *in-dialog*.
    """

    call_id: str
    from_tag: str
    to_user: str
    request_uri: str
    via_branch: str
    remote_to_tag: str | None = None  # the plugin's local tag, from its 200 OK
    answer_sdp: str = ""
    plugin_rtp_port: int = 0
    cseq: int = 1


class FakeGateway:
    """A loopback SIP+RTP peer that drives a whole inbound call against the plugin.

    Combines the TLS :class:`~tests.transport._loopback.LoopbackSipServer` (the
    plugin dials it during ``connect()``) with a :class:`FakeRtpEndpoint` (the
    far-end media). The high-level methods script the call:

    * :meth:`set_register_responder` — auto-answer REGISTER (401 challenge → 200).
    * :meth:`send_options` / :meth:`await_response` — the qualify ping → 200 OK.
    * :meth:`send_invite` then :meth:`await_invite_ok` — INVITE with a realistic
      G.711 RTP/AVP SDP offer → the plugin's 200 OK (To-tag + SDP answer); the
      answer's RTP port is recorded and the RTP endpoint aimed at it.
    * :meth:`send_ack` — the in-dialog ACK (echoes the plugin's To-tag).
    * :meth:`send_bye` — the in-dialog BYE → clean teardown.

    The ``host``/``port`` to point the plugin at are :attr:`sip_host` (the SNI
    name) and :attr:`sip_port` (the loopback server's ephemeral port); the TLS
    client context is :func:`tests.transport._loopback.client_ssl_context`.
    """

    sip_host = "pbx.example.test"

    def __init__(self) -> None:
        self._server = LoopbackSipServer(self._respond)
        self.rtp = FakeRtpEndpoint()
        self._register_responder: Responder | None = None
        self._sip_port = 0

    # lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        """Start the TLS SIP server and the UDP RTP endpoint."""
        await self._server.start()
        self._sip_port = self._server.port
        await self.rtp.start()

    async def stop(self) -> None:
        """Tear down the RTP endpoint and the SIP server."""
        self.rtp.stop()
        await self._server.stop()

    @property
    def sip_port(self) -> int:
        """The loopback SIP server's ephemeral TLS port."""
        return self._sip_port

    @property
    def received_sip(self) -> list[str]:
        """Every raw SIP message the gateway has received from the plugin."""
        return self._server.received

    # REGISTER -----------------------------------------------------------------

    def set_register_responder(
        self,
        *,
        realm: str = "pbx.example.test",
        expires: int = 120,
    ) -> None:
        """Install a REGISTER responder: 401 challenge first, then 200 OK."""
        state = {"seen": 0}

        async def respond(request: SipRequest) -> list[str]:
            if request.method != "REGISTER":
                return []
            state["seen"] += 1
            if state["seen"] == 1:
                return [_register_challenge(request, realm=realm)]
            return [_register_ok(request, expires=expires)]

        self._register_responder = respond

    async def _respond(self, request: SipRequest) -> list[str]:
        """Top-level request handler: REGISTER goes to the installed responder.

        Any other request the plugin sends to the gateway (it should not, in this
        flow) is left unanswered, which the test will surface as a missing reply.
        """
        if request.method == "REGISTER" and self._register_responder is not None:
            return await self._register_responder(request)
        return []

    # out-of-dialog OPTIONS (qualify) -----------------------------------------

    async def send_options(self, *, to_user: str) -> None:
        """Send an out-of-dialog OPTIONS qualify ping (no To-tag)."""
        await self._server.push(
            build_request(
                "OPTIONS",
                f"sip:{to_user}@127.0.0.1:5061;transport=tls",
                [
                    (
                        "Via",
                        f"SIP/2.0/TLS {_FAKE_PUBLIC_ADDRESS}:5061"
                        f";branch={new_branch()};rport",
                    ),
                    ("Max-Forwards", "70"),
                    ("From", f"<sip:qualify@{self.sip_host}>;tag={new_tag()}"),
                    ("To", f"<sip:{to_user}@{self.sip_host}>"),
                    ("Call-ID", new_call_id()),
                    ("CSeq", "1 OPTIONS"),
                ],
            )
        )

    # INVITE / 200 OK / ACK ----------------------------------------------------

    async def send_invite(self, *, to_user: str) -> GatewayCall:
        """Send an INVITE with a realistic G.711 RTP/AVP SDP offer.

        The offer advertises this gateway's real loopback RTP endpoint (so the
        plugin's media flows back to :attr:`rtp`) under a "public" RFC-5737
        connection address, mirroring a NAT'd gateway. Returns the
        :class:`GatewayCall` dialog state for the follow-up ACK/BYE.
        """
        call = GatewayCall(
            call_id=new_call_id(),
            from_tag=new_tag(),
            to_user=to_user,
            request_uri=f"sip:{to_user}@127.0.0.1:5061;transport=tls",
            via_branch=new_branch(),
        )
        offer = _g711_avp_offer(rtp_port=self.rtp.port)
        await self._server.push(
            build_request(
                "INVITE",
                call.request_uri,
                [
                    (
                        "Via",
                        f"SIP/2.0/TLS {_FAKE_PUBLIC_ADDRESS}:5061"
                        f";branch={call.via_branch};rport",
                    ),
                    ("Max-Forwards", "70"),
                    ("From", f"<sip:caller@{self.sip_host}>;tag={call.from_tag}"),
                    ("To", f"<sip:{to_user}@{self.sip_host}>"),
                    ("Call-ID", call.call_id),
                    ("CSeq", "1 INVITE"),
                    (
                        "Contact",
                        f"<sip:caller@{_FAKE_PUBLIC_ADDRESS}:5061;transport=tls>",
                    ),
                    ("Content-Type", "application/sdp"),
                ],
                body=offer,
            )
        )
        return call

    async def await_response(
        self,
        *,
        method: str,
        status: int,
        timeout: float = 3.0,
    ) -> SipResponse:
        """Wait for the plugin's response with ``status`` to a ``method`` request."""

        def is_match(raw: str) -> bool:
            if not raw.startswith("SIP/2.0 "):
                return False
            response = SipResponse.parse(raw)
            cseq = response.header("CSeq") or ""
            return response.status_code == status and cseq.endswith(method)

        raw = await self._server.wait_for_received(is_match, timeout=timeout)
        return SipResponse.parse(raw)

    async def await_invite_ok(
        self, call: GatewayCall, *, timeout: float = 3.0
    ) -> SipResponse:
        """Wait for the plugin's 200 OK to the INVITE; record To-tag + RTP port.

        The 200 OK's To-tag is the plugin's dialog local tag — the gateway must
        echo it on the in-dialog ACK/BYE or the plugin routes them out-of-dialog
        (the live no-audio failure). The SDP answer's ``m=audio`` port is where
        the plugin receives RTP, so the RTP endpoint is aimed at it.
        """
        response = await self.await_response(
            method="INVITE", status=200, timeout=timeout
        )
        call.remote_to_tag = _to_tag_of(response.header("To"))
        call.answer_sdp = response.body
        call.plugin_rtp_port = _sdp_media_port(response.body)
        self.rtp.aim_at("127.0.0.1", call.plugin_rtp_port)
        return response

    async def send_ack(self, call: GatewayCall) -> None:
        """Send the in-dialog ACK echoing the plugin's To-tag (CSeq 1 ACK)."""
        await self._server.push(self._in_dialog(call, "ACK", cseq=1))

    async def send_bye(self, call: GatewayCall) -> None:
        """Send the in-dialog BYE echoing the plugin's To-tag (next CSeq)."""
        call.cseq += 1
        await self._server.push(self._in_dialog(call, "BYE", cseq=call.cseq))

    def _in_dialog(self, call: GatewayCall, method: str, *, cseq: int) -> str:
        """Build an in-dialog request (ACK/BYE) for ``call``.

        From-tag is the gateway's original tag; To-tag is the plugin's local tag
        (from its 200 OK). Same Call-ID. This is exactly the key the plugin's
        manager routes in-dialog requests by: ``(Call-ID, to_tag, from_tag)`` with
        ``to_tag`` = the plugin's local tag.
        """
        if call.remote_to_tag is None:
            msg = "cannot build in-dialog request before the plugin's 200 OK To-tag"
            raise RuntimeError(msg)
        return build_request(
            method,
            call.request_uri,
            [
                (
                    "Via",
                    f"SIP/2.0/TLS {_FAKE_PUBLIC_ADDRESS}:5061"
                    f";branch={new_branch()};rport",
                ),
                ("Max-Forwards", "70"),
                ("From", f"<sip:caller@{self.sip_host}>;tag={call.from_tag}"),
                (
                    "To",
                    f"<sip:{call.to_user}@{self.sip_host}>;tag={call.remote_to_tag}",
                ),
                ("Call-ID", call.call_id),
                ("CSeq", f"{cseq} {method}"),
            ],
        )


# ---------------------------------------------------------------------------
# SIP message bodies (REGISTER auth dance + the SDP offer). Fakes only.
# ---------------------------------------------------------------------------


def _register_challenge(reg: SipRequest, *, realm: str) -> str:
    return (
        "SIP/2.0 401 Unauthorized\r\n"
        f"Via: {reg.header('Via')}\r\n"
        f"From: {reg.header('From')}\r\n"
        f"To: {reg.header('To')};tag=reg-srv\r\n"
        f"Call-ID: {reg.header('Call-ID')}\r\n"
        f"CSeq: {reg.header('CSeq')}\r\n"
        f'WWW-Authenticate: Digest realm="{realm}", nonce="abc123", '
        'algorithm=MD5, qop="auth"\r\n'
        "Content-Length: 0\r\n\r\n"
    )


def _register_ok(reg: SipRequest, *, expires: int) -> str:
    return (
        "SIP/2.0 200 OK\r\n"
        f"Via: {reg.header('Via')}\r\n"
        f"From: {reg.header('From')}\r\n"
        f"To: {reg.header('To')};tag=reg-srv\r\n"
        f"Call-ID: {reg.header('Call-ID')}\r\n"
        f"CSeq: {reg.header('CSeq')}\r\n"
        f"Contact: {reg.header('Contact')};expires={expires}\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def _g711_avp_offer(*, rtp_port: int) -> str:
    """A realistic G.711 (PCMU/PCMA) + DTMF RTP/AVP SDP offer.

    The media connection address is the loopback RTP endpoint (``127.0.0.1`` :
    ``rtp_port``) so the plugin's outbound RTP reaches this gateway directly — as
    a non-NAT gateway that advertises a reachable media address would. (The
    symmetric-RTP comedia latch — the NAT-traversal path where the advertised
    address is unreachable and the plugin must latch onto the caller's real RTP
    source — is covered by the dedicated engine latch tests; here the goal is the
    end-to-end media+rate seam, so the advertised address is the real one.)
    """
    return (
        "v=0\r\n"
        f"o=- 8000 8000 IN IP4 127.0.0.1\r\n"
        "s=-\r\n"
        "c=IN IP4 127.0.0.1\r\n"
        "t=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP 0 8 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
        "a=fmtp:101 0-16\r\n"
        "a=ptime:20\r\n"
        "a=sendrecv\r\n"
    )
