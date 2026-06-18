"""A reusable loopback *fake WebRTC gateway* for end-to-end DTLS-SRTP call tests.

Test infrastructure, not production code (ADR-0032). The far-end WebRTC peer the real
plugin talks to over loopback: a genuine TLS SIP server (reusing
:mod:`tests.transport._loopback`) PLUS a real far-end
:class:`~hermes_voip.media.webrtc_session.WebRtcMediaSession` (the DTLS *server* side —
the adapter is the active client per RFC 8842, ADR-0050)
and an in-memory ICE pipe linking it to the adapter's session — so an entire inbound
WebRTC call can be scripted and asserted at each seam: REGISTER, the SAVPF/Opus/DTLS
INVITE, the real DTLS-SRTP handshake, ACK, SRTP-protected Opus media both ways, and BYE.

It mirrors :mod:`tests.e2e._fake_gateway` (the SDES harness), but for the WebRTC path:
the offer is ``UDP/TLS/RTP/SAVPF`` + Opus + a real DTLS ``a=fingerprint`` + ICE creds
(built from the peer's real ``WebRtcMediaSession``), the media is real SRTP-protected
Opus carried over the ICE pipe, and the peer completes a real DTLS handshake against the
adapter (RFC 5763/5764). It is deliberately gateway-agnostic and uses only obvious fakes
(``pbx.example.test`` / ext ``1000`` / ``127.0.0.1``) — no real host, extension,
credential, or PII.

Hermeticity / determinism
-------------------------
The ICE layer is the **in-memory linked-pipe** harness from
``tests.test_media_webrtc_session`` (no ``aioice`` agent, no real STUN, no UDP
socket): two :class:`_LinkedIce` halves share linked ``asyncio.Queue`` objects. The
adapter's ``WebRtcMediaSession`` takes one half (injected via
:meth:`FakeWebRtcGateway.adapter_ice_factory`); the peer's session takes the other.
Both sides run the **real** ``DtlsEndpoint``
handshake over the pipe concurrently, so the keying + RFC 7983 demux + SRTP transform +
Opus codec are all genuine while nothing touches the network. (Live validation against a
real browser/WebRTC client — real ICE + real DTLS over the network — stays a manual
operator step; no secrets in CI.)
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import TYPE_CHECKING, Protocol

from hermes_voip.media.ice import IceCandidate as MediaIceCandidate
from hermes_voip.media.opus import (
    OPUS_DEFAULT_PAYLOAD_TYPE,
    OPUS_FRAME_SAMPLES,
    OPUS_RTP_CLOCK_RATE,
    OpusDecoder,
    OpusEncoder,
)
from hermes_voip.media.srtp import SrtpSession
from hermes_voip.media.webrtc_session import WebRtcMediaSession
from hermes_voip.message import (
    SipRequest,
    SipResponse,
    build_request,
    new_branch,
    new_call_id,
    new_tag,
)
from hermes_voip.rtp import RtpPacket
from hermes_voip.sdp import (
    Fingerprint,
    SessionDescription,
    SetupRole,
)
from hermes_voip.sdp import (
    IceCandidate as SdpIceCandidate,
)
from tests.transport._loopback import (
    LoopbackSipServer,
    Responder,
    client_ssl_context,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "FakeWebRtcGateway",
    "WebRtcCall",
    "client_ssl_context",
    "opus_silence_frames",
    "opus_speech_frames",
]

# Fakes only (the repo is public): an RFC-5737 documentation address stands in for
# the "public" SDP origin address the gateway advertises, and an obvious fake
# extension. No real host, IP, extension, or credential appears here.
_FAKE_PUBLIC_ADDRESS = "198.51.100.10"

# RFC 7983 first-byte demux ranges (the engine and session agree on these). The peer
# applies the SAME demux on its inbound ICE datagrams to separate residual DTLS/STUN
# from the SRTP media.
_RFC7983_SRTP_MIN = 128
_RFC7983_SRTP_MAX = 191

# The Opus RTP payload type the offer advertises (the adapter echoes it in the answer
# and the engine sends with it).
_OPUS_PT = OPUS_DEFAULT_PAYLOAD_TYPE

_TO_TAG_RE = re.compile(r";\s*tag=([^;,\s]+)", re.IGNORECASE)


def _to_tag_of(to_header: str | None) -> str | None:
    """Extract the dialog tag from a ``To`` header value (after the name-addr)."""
    if to_header is None:
        return None
    search_space = to_header.split(">", 1)[1] if ">" in to_header else to_header
    match = _TO_TAG_RE.search(search_space)
    return match.group(1) if match is not None else None


def _opus_pcm_tone(num_frames: int, amplitude: int) -> list[bytes]:
    """``num_frames`` frames of a constant PCM16 level at 48 kHz (one Opus frame each).

    A constant non-zero level Opus-encodes to a packet that decodes back to audible
    energy (enough for the fake VAD to score as speech); ``amplitude == 0`` is silence.
    Each frame is exactly one 20 ms 48 kHz Opus frame (``OPUS_FRAME_SAMPLES`` samples).
    """
    sample = amplitude.to_bytes(2, "little", signed=True)
    return [sample * OPUS_FRAME_SAMPLES] * num_frames


def opus_speech_frames(num_frames: int) -> list[bytes]:
    """``num_frames`` of 48 kHz PCM16 "speech" (constant tone) for inbound Opus."""
    return _opus_pcm_tone(num_frames, amplitude=8000)


def opus_silence_frames(num_frames: int) -> list[bytes]:
    """``num_frames`` of 48 kHz PCM16 silence for inbound Opus."""
    return _opus_pcm_tone(num_frames, amplitude=0)


class _LinkedIce:
    """One half of an in-memory ICE pipe pair (the test_media_webrtc_session harness).

    Implements the full :class:`~hermes_voip.media.webrtc_session._IcePipe` surface the
    :class:`WebRtcMediaSession` drives AND the
    :class:`~hermes_voip.media.engine._IceDatagramPipe` surface the engine drives
    (``send`` / ``recv`` / ``close``). The two halves are linked by
    :func:`_link_ice`: ``send`` enqueues onto the peer's inbound queue; ``recv`` awaits
    this half's inbound queue. No connectivity checks run — the pipe is already
    "connected" — so a real DTLS handshake completes in-process with no aioice /
    sockets.

    The credentials/candidate the peer exposes match the canonical SDP shape the answer
    renders; the host candidate's address is loopback (host-only, no STUN).
    """

    def __init__(self, ufrag: str, pwd: str, *, candidate_port: int) -> None:
        self._ufrag = ufrag
        self._pwd = pwd
        self._candidate_port = candidate_port
        self.inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self.peer: _LinkedIce | None = None
        self.closed = False
        self.remote_ufrag: str | None = None
        self.remote_pwd: str | None = None
        self.remote_candidates: list[MediaIceCandidate] = []

    @property
    def local_ufrag(self) -> str:
        return self._ufrag

    @property
    def local_pwd(self) -> str:
        return self._pwd

    @property
    def local_candidates(self) -> list[MediaIceCandidate]:
        # One loopback host candidate (the shape the SDP answer/offer renders). The
        # IceCandidate dataclass itself imports no aioice (only IceConnection /
        # from_sdp do), so it is imported at module top like webrtc_session.py does.
        return [
            MediaIceCandidate(
                foundation="candidate:1",
                component=1,
                transport="UDP",
                priority=2130706431,
                host="127.0.0.1",
                port=self._candidate_port,
                type="host",
                related_address=None,
                related_port=None,
            )
        ]

    async def gather_candidates(self) -> None:
        return None

    def set_remote_credentials(self, ufrag: str, pwd: str) -> None:
        self.remote_ufrag = ufrag
        self.remote_pwd = pwd

    async def add_remote_candidate(self, candidate: MediaIceCandidate | None) -> None:
        if candidate is not None:
            self.remote_candidates.append(candidate)

    async def connect(self) -> None:
        return None

    async def send(self, data: bytes) -> None:
        self.send_nowait(data)

    def send_nowait(self, data: bytes) -> None:
        """Enqueue ``data`` onto the peer's inbound queue synchronously.

        The linked pipe never blocks on send (the queue is unbounded), so the peer's
        media sender can push SRTP synchronously — preserving exact wire order with no
        fire-and-forget task. :meth:`send` (the async ``_IcePipe``/``_IceDatagramPipe``
        surface the session + engine drive) delegates here.
        """
        peer = self.peer
        assert peer is not None, "ICE pipe half is not linked to a peer"
        if peer.closed:
            return
        peer.inbound.put_nowait(bytes(data))

    async def recv(self) -> bytes:
        return await self.inbound.get()

    async def close(self) -> None:
        self.closed = True


def _link_ice(a: _LinkedIce, b: _LinkedIce) -> None:
    """Link two :class:`_LinkedIce` halves so a ``send`` reaches the other's queue."""
    a.peer = b
    b.peer = a


class _IceFactory(Protocol):
    """The ICE-factory callable :class:`WebRtcMediaSession` accepts (``ice_factory``).

    Matches the session's private ``_IceFactory`` protocol structurally (keyword-only
    ``ice_controlling`` / ``stun_urls`` plus the ADR-0034 TURN keywords, defaulted),
    so a factory typed as this is accepted where the session wants one — without
    importing the session's underscore-prefixed Protocol. The returned
    :class:`_LinkedIce` satisfies the session's ``_IcePipe`` surface structurally.
    """

    def __call__(  # noqa: PLR0913 -- ICE factory config kwargs (role/STUN/TURN/IP-family)
        self,
        *,
        ice_controlling: bool,
        stun_urls: tuple[str, ...],
        turn_urls: tuple[str, ...] = (),
        turn_username: str | None = None,
        turn_password: str | None = None,
        use_ipv4: bool = True,
        use_ipv6: bool = True,
    ) -> _LinkedIce:
        """Build an ICE pipe for the given role, STUN, and (optional) TURN."""
        ...


class WebRtcCall:
    """Dialog + media state for one inbound WebRTC call the fake gateway placed.

    Holds the far-end (UAC) dialog half (so the gateway can send the in-dialog ACK / BYE
    the plugin's 200 OK requires, echoing the plugin's To-tag) and the peer's
    :class:`WebRtcMediaSession` (the DTLS server; ADR-0050) whose handshake keys the
    SRTP the media flows under.
    """

    def __init__(
        self,
        *,
        call_id: str,
        from_tag: str,
        to_user: str,
        request_uri: str,
        peer_session: WebRtcMediaSession,
    ) -> None:
        self.call_id = call_id
        self.from_tag = from_tag
        self.to_user = to_user
        self.request_uri = request_uri
        self.peer_session = peer_session
        self.remote_to_tag: str | None = None  # the plugin's local tag, from its 200 OK
        self.answer_sdp: str = ""
        self.answer: SessionDescription | None = None
        self.cseq = 1

    @property
    def answer_is_webrtc(self) -> bool:
        """True iff the plugin's 200 OK answer is a UDP/TLS/RTP/SAVPF (WebRTC) one."""
        return (
            self.answer is not None
            and self.answer.audio is not None
            and (self.answer.audio.is_webrtc)
        )

    @property
    def answer_fingerprint(self) -> Fingerprint | None:
        """The plugin's DTLS ``a=fingerprint`` from its SAVPF answer, or ``None``."""
        if self.answer is None or self.answer.audio is None:
            return None
        return self.answer.audio.fingerprint


class FakeWebRtcGateway:
    """A loopback SIP+WebRTC peer driving a whole inbound DTLS-SRTP call (ADR-0032).

    Combines the TLS :class:`~tests.transport._loopback.LoopbackSipServer` (the plugin
    dials it during ``connect()``) with a real far-end
    :class:`~hermes_voip.media.webrtc_session.WebRtcMediaSession` and the in-memory ICE
    pipe linking it to the adapter's session. The high-level methods script the call:

    * :meth:`set_register_responder` — auto-answer REGISTER (401 challenge → 200).
    * :meth:`send_invite_and_handshake` — build the SAVPF/Opus/DTLS offer from the
      peer's
      session, send the INVITE, await the plugin's 200 OK (To-tag + SAVPF answer), send
      the ACK, and run the peer's side of the **real DTLS handshake** concurrently with
      the adapter's (keying the SRTP pair).
    * :meth:`send_caller_audio` — Opus-encode + SRTP-protect 48 kHz caller frames and
      send
      them over the ICE pipe as inbound RTP.
    * the engine's outbound SRTP-Opus is decrypted + Opus-decoded into
      :attr:`decoded_inbound_frames` by the peer's reader task.
    * :meth:`send_bye` — the in-dialog BYE → clean teardown.

    The ``host``/``port`` to point the plugin at are :attr:`sip_host` and
    :attr:`sip_port`; the TLS client context is
    :func:`tests.transport._loopback.client_ssl_context`.
    """

    sip_host = "pbx.example.test"

    def __init__(self) -> None:
        self._server = LoopbackSipServer(self._respond)
        self._register_responder: Responder | None = None
        self._sip_port = 0

        # The in-memory ICE pipe pair linking the adapter's session (one half) to the
        # peer's session (the other). Built once; the adapter's half is handed out by
        # adapter_ice_factory(), the peer's half is consumed by the peer session.
        self._adapter_ice = _LinkedIce(
            "adptUFRAGxx", "adptPWDadptPWDadptPWDadpt", candidate_port=51001
        )
        self._peer_ice = _LinkedIce(
            "peerUFRAGxx", "peerPWDpeerPWDpeerPWDpeer", candidate_port=51002
        )
        _link_ice(self._adapter_ice, self._peer_ice)
        self._adapter_ice_handed_out = False

        # The peer's DTLS-client media session (built in send_invite_and_handshake).
        self._peer_session: WebRtcMediaSession | None = None
        # Keying outcome (after the handshake).
        self.srtp_inbound: SrtpSession | None = None
        self.srtp_outbound: SrtpSession | None = None
        self.handshake_complete = False

        # Opus codec for the peer's media (one per direction; stateful).
        self._opus_encoder = OpusEncoder()
        self._opus_decoder = OpusDecoder()
        # Outbound (caller→plugin) RTP bookkeeping for SRTP-Opus we send.
        self._tx_seq = 0
        self._tx_ts = 0
        self._tx_ssrc = 0x0CA11E55  # an obvious fake far-end SSRC

        # Inbound (plugin→peer) media the peer's reader decrypts + Opus-decodes.
        self.inbound_srtp_packet_count = 0
        self.decoded_inbound_frames: list[bytes] = []
        self._decoded_event = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None

    # lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        """Start the TLS SIP server (the RTP media rides the in-memory ICE pipe)."""
        await self._server.start()
        self._sip_port = self._server.port

    async def stop(self) -> None:
        """Tear down the reader task, the ICE pipe, the peer session, and the server."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        await self._adapter_ice.close()
        await self._peer_ice.close()
        if self._peer_session is not None:
            await self._peer_session.close()
            self._peer_session = None
        await self._server.stop()

    @property
    def sip_port(self) -> int:
        """The loopback SIP server's ephemeral TLS port."""
        return self._sip_port

    @property
    def received_sip(self) -> list[str]:
        """Every raw SIP message the gateway has received from the plugin."""
        return self._server.received

    def adapter_ice_factory(self) -> _IceFactory:
        """Return an ICE factory the adapter's ``WebRtcMediaSession`` uses (one-shot).

        The factory ignores its ``ice_controlling`` / ``stun_urls`` / TURN arguments
        and returns the adapter's pre-linked half of the in-memory ICE pipe. It is
        one-shot: the adapter builds exactly one WebRtcMediaSession per inbound call,
        and this harness drives exactly one call, so a second call would be a
        test-wiring bug.
        """

        def _factory(  # noqa: PLR0913 -- ICE factory config kwargs (role/STUN/TURN/IP-family)
            *,
            ice_controlling: bool,
            stun_urls: tuple[str, ...],
            turn_urls: tuple[str, ...] = (),
            turn_username: str | None = None,
            turn_password: str | None = None,
            use_ipv4: bool = True,
            use_ipv6: bool = True,
        ) -> _LinkedIce:
            if self._adapter_ice_handed_out:
                msg = "adapter ICE pipe already handed out (one call per gateway)"
                raise RuntimeError(msg)
            self._adapter_ice_handed_out = True
            return self._adapter_ice

        return _factory

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
        """Top-level request handler: REGISTER goes to the installed responder."""
        if request.method == "REGISTER" and self._register_responder is not None:
            return await self._register_responder(request)
        return []

    # INVITE / 200 OK / ACK + the DTLS handshake -------------------------------

    async def send_invite_and_handshake(
        self,
        *,
        to_user: str,
        timeout: float = 5.0,
    ) -> WebRtcCall:
        """Send a WebRTC INVITE, await the 200 OK, ACK, and run the DTLS handshake.

        Builds the peer's far-end :class:`WebRtcMediaSession` (the DTLS *server*: the
        offer carries ``a=setup:actpass`` and RFC 8842 §5.3 makes the adapter the
        ``active`` client, ADR-0050, so this peer is the server by constructing its
        session with ``offer_setup=active``), gathers its ICE, and emits a
        ``UDP/TLS/RTP/SAVPF`` + Opus + ``a=fingerprint`` + ICE offer.

        After the plugin's 200 OK arrives (recording the To-tag + SAVPF answer), it runs
        the peer's :meth:`WebRtcMediaSession.run_handshake` against the adapter's: the
        adapter's own ``run_handshake`` is awaited inside ``_setup_webrtc_call`` (right
        after the 200 OK), and both ride the SAME linked ICE pipe — so the real DTLS
        records flow both ways and both sides key SRTP. On return the
        peer's SRTP pair is stored and the inbound reader (decrypt + Opus-decode of the
        plugin's media) is running.

        The ACK is NOT sent here. The adapter registers the in-dialog route only AFTER
        its ``run_handshake`` returns (the WebRTC handshake rides the media that only
        flows once the peer has the answer; the SDES path has no such delay), so the
        caller must wait until the dialog is established before the ACK can route
        in-dialog. The test owns the adapter and waits on that observable state
        (``call_id in adapter._call_loops``) before calling :meth:`send_ack` — a real
        UAC's earlier ACK would be briefly unroutable during the handshake, which is
        benign for a 2xx ACK (the dialog still forms and the BYE later routes
        in-dialog). Keeping the ACK out of this method avoids a nondeterministic
        scheduler-settle and asserts only what is genuinely guaranteed.
        """
        # The peer is the DTLS SERVER (ADR-0050). The offer carries `a=setup:actpass`,
        # and under RFC 8842 §5.3 the adapter answers `active` (the DTLS client). So
        # this peer — modelling a real Asterisk/UCM gateway that offers actpass yet
        # behaves as the DTLS server — must be passive/server. WebRtcMediaSession
        # derives its own role from `offer_setup`: an `active` offer makes this side
        # passive/server (the complement of the adapter's active answer).
        peer = WebRtcMediaSession(
            offer_setup=SetupRole("active"),
            ice_factory=self._peer_ice_factory(),
        )
        self._peer_session = peer
        await peer.prepare()

        call = WebRtcCall(
            call_id=new_call_id(),
            from_tag=new_tag(),
            to_user=to_user,
            request_uri=f"sip:{to_user}@127.0.0.1:5061;transport=tls",
            peer_session=peer,
        )
        offer = _webrtc_offer(
            fingerprint=str(peer.fingerprint.render()),
            ice_ufrag=peer.ice_ufrag,
            ice_pwd=peer.ice_pwd,
            ice_candidates=peer.ice_candidates,
        )
        await self._server.push(
            build_request(
                "INVITE",
                call.request_uri,
                [
                    (
                        "Via",
                        f"SIP/2.0/TLS {_FAKE_PUBLIC_ADDRESS}:5061"
                        f";branch={new_branch()};rport",
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

        # Await the plugin's 200 OK first (it is sent BEFORE the adapter awaits its own
        # run_handshake inside _setup_webrtc_call), recording the To-tag + SAVPF answer.
        response = await self._await_invite_ok(call, timeout=timeout)
        call.remote_to_tag = _to_tag_of(response.header("To"))
        call.answer_sdp = response.body
        call.answer = SessionDescription.parse(response.body)
        answer_audio = call.answer.audio
        if answer_audio is None:
            msg = "plugin 200 OK has no audio media in the SAVPF answer"
            raise AssertionError(msg)
        # A WebRTC answer MUST carry the DTLS fingerprint + ICE creds the peer needs to
        # run the handshake (RFC 5763/8839). Narrow them explicitly (no escape hatch): a
        # missing attribute is a real adapter defect, surfaced here as a clear error
        # rather than a typing override — and exactly the SAVPF-answer regression the
        # full-call test also asserts.
        fp = answer_audio.fingerprint
        ice_ufrag = answer_audio.ice_ufrag
        ice_pwd = answer_audio.ice_pwd
        if fp is None or ice_ufrag is None or ice_pwd is None:
            msg = (
                "plugin SAVPF answer is missing a=fingerprint / a=ice-ufrag / a=ice-pwd"
            )
            raise AssertionError(msg)

        # Run the peer's side of the DTLS handshake. The adapter's own run_handshake is
        # already in flight (awaited right after it sent the 200 OK); both ride the SAME
        # linked ICE pipe, so the real DTLS records interleave over the event loop and
        # BOTH sides key SRTP. We do NOT send the ACK first: the adapter only registers
        # the in-dialog route AFTER its run_handshake returns (no await between the
        # handshake and manager.add_call), so an ACK sent before the handshake completes
        # would arrive at an unregistered dialog and route out-of-dialog. The ACK goes
        # out below, once the dialog is established.
        srtp_in, srtp_out = await peer.run_handshake(
            peer_fingerprint=fp,
            peer_ice_ufrag=ice_ufrag,
            peer_ice_pwd=ice_pwd,
            peer_candidates=answer_audio.ice_candidates,
        )
        self.srtp_inbound = srtp_in
        self.srtp_outbound = srtp_out
        self.handshake_complete = True

        # Start the inbound reader: decrypt + Opus-decode the plugin's outbound media.
        self._reader_task = asyncio.ensure_future(self._read_inbound_media())
        # The ACK is sent by the caller via send_ack() once it has observed the dialog
        # established (see the method docstring) — not here.
        return call

    def _peer_ice_factory(self) -> _IceFactory:
        """An ICE factory returning the peer's half of the linked pipe."""

        def _factory(  # noqa: PLR0913 -- ICE factory config kwargs (role/STUN/TURN/IP-family)
            *,
            ice_controlling: bool,
            stun_urls: tuple[str, ...],
            turn_urls: tuple[str, ...] = (),
            turn_username: str | None = None,
            turn_password: str | None = None,
            use_ipv4: bool = True,
            use_ipv6: bool = True,
        ) -> _LinkedIce:
            return self._peer_ice

        return _factory

    async def _await_invite_ok(
        self, call: WebRtcCall, *, timeout: float
    ) -> SipResponse:
        """Wait for the plugin's 200 OK to ``call``'s INVITE (by Call-ID + CSeq).

        Scoped to this call's Call-ID (not just status + CSeq method) so the match is
        unambiguous even if the helper is reused for a second/retried INVITE on the same
        connection.
        """

        def is_match(raw: str) -> bool:
            if not raw.startswith("SIP/2.0 "):
                return False
            resp = SipResponse.parse(raw)
            if resp.status_code != 200:
                return False
            if resp.header("Call-ID") != call.call_id:
                return False
            cseq = resp.header("CSeq") or ""
            return cseq.endswith("INVITE")

        raw = await self._server.wait_for_received(is_match, timeout=timeout)
        return SipResponse.parse(raw)

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

    async def send_ack(self, call: WebRtcCall) -> None:
        """Send the in-dialog ACK echoing the plugin's To-tag (CSeq 1 ACK)."""
        await self._server.push(self._in_dialog(call, "ACK", cseq=1))

    async def send_bye(self, call: WebRtcCall) -> None:
        """Send the in-dialog BYE echoing the plugin's To-tag (next CSeq)."""
        call.cseq += 1
        await self._server.push(self._in_dialog(call, "BYE", cseq=call.cseq))

    def _in_dialog(self, call: WebRtcCall, method: str, *, cseq: int) -> str:
        """Build an in-dialog request (ACK/BYE) for ``call``, echoing the To-tag."""
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

    # Media (SRTP-Opus over the ICE pipe) --------------------------------------

    def send_caller_audio(self, pcm48k_frames: list[bytes]) -> None:
        """Opus-encode + SRTP-protect 48 kHz caller frames; send as inbound RTP.

        Each ``pcm48k_frames`` entry is one 20 ms 48 kHz PCM16 frame
        (``OPUS_FRAME_SAMPLES`` samples). The frame is Opus-encoded, wrapped in an RTP
        packet (Opus PT, advancing seq/ts), SRTP-protected with the peer's outbound key
        (which the adapter's inbound SRTP decrypts), and sent over the ICE pipe to the
        engine — exactly the inbound media path of a real WebRTC caller.
        """
        srtp = self.srtp_outbound
        if srtp is None:
            msg = "send_caller_audio before the DTLS handshake keyed SRTP"
            raise RuntimeError(msg)
        for pcm in pcm48k_frames:
            payload = self._opus_encoder.encode(pcm)
            packet = RtpPacket(
                payload_type=_OPUS_PT,
                sequence_number=self._tx_seq,
                timestamp=self._tx_ts,
                ssrc=self._tx_ssrc,
                payload=payload,
            )
            wire = srtp.protect(packet)
            # Synchronous, ordered enqueue onto the engine's inbound ICE queue (the
            # linked pipe never blocks on send) — exact wire order, no fire-and-forget.
            self._peer_ice.send_nowait(wire)
            self._tx_seq = (self._tx_seq + 1) % (1 << 16)
            self._tx_ts = (self._tx_ts + OPUS_FRAME_SAMPLES) % (1 << 32)

    async def _read_inbound_media(self) -> None:
        """Decrypt + Opus-decode the plugin's outbound SRTP-Opus from the ICE pipe.

        Pumps the peer's inbound ICE queue, applies the RFC 7983 first-byte demux (only
        SRTP 128-191), ``unprotect``s each packet with the peer's inbound SRTP (keyed
        from the same DTLS handshake as the adapter's outbound), and Opus-decodes the
        payload into :attr:`decoded_inbound_frames`. A decrypt/decode failure re-raises
        (rule 37) so a keying/codec regression surfaces as a failing test, never a
        silent drop.
        """
        srtp = self.srtp_inbound
        assert srtp is not None, "inbound reader started before SRTP keying"
        while True:
            data = await self._peer_ice.recv()
            if not data:
                continue
            first = data[0]
            if not (_RFC7983_SRTP_MIN <= first <= _RFC7983_SRTP_MAX):
                # Residual DTLS/STUN after the handshake — not SRTP media; skip it.
                continue
            self.inbound_srtp_packet_count += 1
            # ``unprotect`` raises SrtpError on a decrypt/auth failure (a keying bug);
            # we let it propagate (rule 37) so a keying/codec regression surfaces as a
            # failing test, never a silent drop — the whole point of the harness.
            packet = srtp.unprotect(data)
            decoded = self._opus_decoder.decode(packet.payload)
            self.decoded_inbound_frames.append(decoded)
            self._decoded_event.set()

    async def wait_for_decoded_audio(
        self, *, frames: int, timeout: float = 5.0
    ) -> None:
        """Wait until at least ``frames`` decrypted + Opus-decoded frames arrive."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.decoded_inbound_frames) < frames:
            remaining = deadline - loop.time()
            if remaining <= 0:
                got = len(self.decoded_inbound_frames)
                msg = (
                    f"only {got} decrypted+decoded inbound frames arrived, "
                    f"wanted {frames}"
                )
                raise TimeoutError(msg)
            self._decoded_event.clear()
            try:
                await asyncio.wait_for(self._decoded_event.wait(), remaining)
            except TimeoutError:
                continue

    async def wait_for_srtp_packets(self, count: int, *, timeout: float = 5.0) -> None:
        """Wait until at least ``count`` inbound SRTP packets have been decrypted."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.inbound_srtp_packet_count < count:
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = (
                    f"only {self.inbound_srtp_packet_count} inbound SRTP packets "
                    f"arrived, wanted {count}"
                )
                raise TimeoutError(msg)
            self._decoded_event.clear()
            try:
                await asyncio.wait_for(self._decoded_event.wait(), remaining)
            except TimeoutError:
                continue


# ---------------------------------------------------------------------------
# SIP message bodies (REGISTER auth dance + the WebRTC SDP offer). Fakes only.
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


def _webrtc_offer(
    *,
    fingerprint: str,
    ice_ufrag: str,
    ice_pwd: str,
    ice_candidates: Sequence[SdpIceCandidate],
) -> str:
    """A realistic ``UDP/TLS/RTP/SAVPF`` + Opus + DTLS + ICE offer (RFC 5763/8839).

    Mirrors a browser/WebRTC offer: Opus first (the WebRTC codec) then PCMU fallback,
    the peer's real DTLS ``a=fingerprint``, ``a=setup:actpass`` (so the adapter answers
    ``active``/client per RFC 8842 §5.3 and the peer is the passive/server, ADR-0050),
    the peer's ICE ufrag/pwd +
    loopback host candidate, and ``a=rtcp-mux``. Per RFC 5763 §5 there is no ``c=`` line
    and no ``a=crypto`` (the media address is conveyed by ICE; SDES is forbidden here).
    """
    lines = [
        "v=0",
        "o=- 0 0 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        f"m=audio 9 UDP/TLS/RTP/SAVPF {_OPUS_PT} 0",
        f"a=rtpmap:{_OPUS_PT} opus/{OPUS_RTP_CLOCK_RATE}/2",
        f"a=fmtp:{_OPUS_PT} minptime=10;useinbandfec=1",
        "a=rtpmap:0 PCMU/8000",
        f"a=fingerprint:{fingerprint}",
        "a=setup:actpass",
        f"a=ice-ufrag:{ice_ufrag}",
        f"a=ice-pwd:{ice_pwd}",
    ]
    lines.extend(f"a=candidate:{cand.render()}" for cand in ice_candidates)
    lines.append("a=rtcp-mux")
    lines.append("a=ptime:20")
    lines.append("a=sendrecv")
    return "\r\n".join(lines) + "\r\n"
