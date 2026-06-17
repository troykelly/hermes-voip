"""End-to-end inbound **WebRTC** call against the REAL plugin stack (ADR-0032).

The safety net the SDES loopback harness (:mod:`tests.e2e.test_inbound_call`) gives
the SIP-over-TLS path, for the DTLS-SRTP / ICE / Opus WebRTC path. The component
pieces are unit-tested (DTLS in ``test_media_webrtc_session.py``, ICE, Opus, the
adapter ``is_webrtc`` branch in ``test_adapter_webrtc.py``) — but
``test_adapter_webrtc`` fakes the ``WebRtcMediaSession`` (canned MagicMock SRTP, no real
handshake), so the ASSEMBLED inbound WebRTC call — a real DTLS-SRTP handshake, real SRTP
keying, and real Opus media over the ICE pipe — was never driven end-to-end before its
first live call. This module drives it.

What it exercises (the whole assembled inbound WebRTC path, real at every seam):

1. REGISTER → registered (``adapter.connect()`` over the real TLS loopback).
2. INVITE with a genuine ``UDP/TLS/RTP/SAVPF`` + Opus + DTLS-``a=fingerprint`` + ICE
   offer (built from a *real peer*
   :class:`~hermes_voip.media.webrtc_session.WebRtcMediaSession`) → the real adapter
   routes to ``_setup_webrtc_call``, validates the offer, gathers ICE, builds the SAVPF
   answer via :func:`~hermes_voip.sdp.build_webrtc_answer`, and sends a 200 OK (To-tag +
   the answer carrying our fingerprint / setup / ICE creds).
3. The **real DTLS-SRTP handshake** runs over an in-memory ICE pipe linking the
   adapter's session to the peer's session (the same ``aioice``-free linked-pipe harness
   as ``test_media_webrtc_session.py``): the peer (DTLS client) and the adapter (DTLS
   server) exchange real DTLS records, verify each other's certificate fingerprint
   (RFC 5763 §5), and derive role-mirrored SRTP (RFC 5764). No real network, no real
   STUN — host/loopback only.
4. ACK (in-dialog) -> routes to the CallSession (the 200 OK's To-tag is honoured).
5. The greeting flows out as **SRTP-protected Opus** over the ICE pipe: the peer
   RFC-7983-demuxes it, ``unprotect``s it with its DTLS-derived SRTP, and
   Opus-**decodes** it — proving real keyed media actually reaches the far end
   (decryptable + decodable).
6. The peer sends **SRTP-Opus caller speech** the other way; the real CallLoop pump
   decrypts + Opus-decodes + downsamples it (48->16 kHz, ADR-0032) and the real VAD/ASR
   deliver exactly ONE turn carrying the caller's transcript; the echo reply is
   Opus-encoded + SRTP-protected back to the peer.
7. BYE -> clean teardown: the CallSession is removed, no aclose() generator race and no
   unretrieved task exception (the module runs under ``-W error::RuntimeWarning``).

**Regression coverage (rule 18/25 — it must FAIL if the path regresses).** The
assertions are not structural: the greeting bytes the peer decodes must be SRTP that
decrypts under the *DTLS-derived* key AND Opus-decode to the expected sample count, and
the caller turn must carry the transcript that only arrives if inbound SRTP-Opus
decrypts + decodes. A broken ``is_webrtc`` route (-> SDES/G.711, no DTLS), wrong DTLS
role/keying (SRTP that does not decrypt), or a dead media path (no Opus) each breaks a
concrete assertion — verified by the negative-control test
:func:`test_regression_wrong_srtp_key_fails_to_decrypt`.

**Hermetic + deterministic.** No real network (ICE is the in-memory linked pipe), no
real
STUN, no real models (fake providers), instant RTP pacing (the engine's ``sleep`` seam).
The DTLS handshake + SRTP transform + Opus codec are REAL (the ``webrtc`` + ``media``
extras + system ``libopus``). **Live-gateway validation against a real browser/WebRTC
client stays a manual operator step** (a real DTLS peer + real ICE over the network; no
secrets in CI).

The adapter imports the real Hermes ``BasePlatformAdapter`` at module top, so the module
``importorskip``s the ``hermes`` extra (it runs in the ``hermes-contract`` CI job, which
installs ``hermes`` + ``webrtc`` + ``media`` + ``libopus``) and the ``OpenSSL`` extra.
Fakes only — ``pbx.example.test`` / ext ``1000`` / ``127.0.0.1``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

# The adapter imports the real Hermes base at module top; the WebRTC session needs
# pyOpenSSL (DTLS). Skip the whole module when either optional dependency is absent
# (it runs in the hermes-contract CI job, which installs both + libopus).
pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")
pytest.importorskip("OpenSSL", reason="webrtc extra (pyOpenSSL) not installed")
pytest.importorskip("opuslib", reason="webrtc extra (opuslib) not installed")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import MessageEvent

from hermes_voip.media.engine import RtpMediaTransport
from hermes_voip.media.opus import (
    OPUS_FRAME_SAMPLES,
    OPUS_SAMPLE_RATE,
    OpusDecoder,
    OpusEncoder,
)
from hermes_voip.media.srtp import SrtpSession
from hermes_voip.media.webrtc_session import WebRtcMediaSession
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PCM16_BYTES_PER_SAMPLE, PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.tts import TtsStream
from hermes_voip.rtp import RtpPacket
from hermes_voip.sdp import SetupRole
from hermes_voip.transport.connection import SipOverTlsTransport
from tests.e2e._fake_webrtc_gateway import (
    FakeWebRtcGateway,
    opus_silence_frames,
    opus_speech_frames,
)
from tests.transport._loopback import client_ssl_context

if TYPE_CHECKING:
    from hermes_voip.adapter import VoipAdapter

# Promote RuntimeWarning to an error for THIS module so the teardown async-generator
# ``aclose`` race and an unretrieved task exception fail the test (as in the SDES e2e).
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.filterwarnings("error::RuntimeWarning"),
]

_TO_USER = "1000"
# Opus encodes/decodes at 48 kHz, 20 ms frames (960 samples). The engine downsamples
# inbound Opus 48 kHz → its 16 kHz analysis rate for the VAD/STT (ADR-0032).
_OPUS_ANALYSIS_RATE = 16_000


# ---------------------------------------------------------------------------
# Ensure the "voip" platform name resolves (Platform("voip") needs a registry
# entry; the plugin would register it, but the adapter is built directly here).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _register_voip_platform() -> None:
    """Register a throwaway "voip" entry so ``Platform("voip")`` resolves."""
    if not platform_registry.is_registered("voip"):
        platform_registry.register(
            PlatformEntry(
                name="voip",
                label="VoIP",
                adapter_factory=lambda cfg: None,  # never invoked here
                check_fn=lambda: True,
                validate_config=lambda cfg: True,
                required_env=[],
                install_hint="",
                source="plugin",
            )
        )


# ---------------------------------------------------------------------------
# Fake providers (only the providers + agent are faked; everything else is real).
# ---------------------------------------------------------------------------


class _RecordingVadModel:
    """A fake silero ``VadModel``: scores a window high iff it carries energy.

    Records the ``sample_rate`` it is called with so a test can assert the real
    :class:`~hermes_voip.media.vad.VoiceActivityDetector` ran at the rate the
    adapter configured for the WebRTC engine (16 kHz, the Opus analysis rate).
    """

    def __init__(self) -> None:
        self.sample_rates: list[int] = []

    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        self.sample_rates.append(sample_rate)
        return 0.9 if any(window_pcm16) else 0.0


class _FakeTtsStream:
    """A ``TtsStream`` emitting fixed 48 kHz PCM16 frames (the Opus wire rate).

    Emitting at 48 kHz means the engine encodes the greeting/reply straight to Opus
    with no resample. Drains the agent's ``text`` iterator on the first frame and
    records the joined text.
    """

    def __init__(
        self,
        frames: list[PcmFrame],
        text: AsyncIterator[str],
        recorded: list[str],
    ) -> None:
        self._frames = list(frames)
        self._index = 0
        self._cancelled = False
        self._text = text
        self._recorded = recorded
        self._text_drained = False

    def __aiter__(self) -> _FakeTtsStream:
        return self

    async def __anext__(self) -> PcmFrame:
        if not self._text_drained:
            self._text_drained = True
            chunks = [chunk async for chunk in self._text]
            self._recorded.append("".join(chunks))
        if self._cancelled or self._index >= len(self._frames):
            raise StopAsyncIteration
        frame = self._frames[self._index]
        self._index += 1
        return frame

    async def flush(self) -> None:
        """No buffered text to drain (fixed-frame fake)."""

    async def cancel(self) -> None:
        self._cancelled = True

    async def aclose(self) -> None:
        """Close the stream (the call loop closes it on every playout exit)."""
        self._cancelled = True


class _FakeTTS:
    """Synthesises any text to two 48 kHz Opus-frame-sized PcmFrames; records requests.

    48 kHz is the Opus wire rate, so the engine encodes each frame straight to an
    Opus packet (one 20 ms frame = ``OPUS_FRAME_SAMPLES`` samples). Each synthesised
    stream records the full joined text it was given in :attr:`synth_texts`.
    """

    output_sample_rate = OPUS_SAMPLE_RATE

    def __init__(self) -> None:
        self.synth_texts: list[str] = []

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        # A constant non-zero tone so the peer can confirm the decoded greeting
        # carries energy (a dead/garbled SRTP-Opus path would decode to silence).
        sample = (4096).to_bytes(2, "little", signed=True)
        frame = PcmFrame(
            samples=sample * OPUS_FRAME_SAMPLES,
            sample_rate=OPUS_SAMPLE_RATE,
            monotonic_ts_ns=0,
        )
        return _FakeTtsStream([frame, frame], text, self.synth_texts)


def _frame_is_speech(frame: PcmFrame) -> bool:
    """True iff the frame carries audible energy (peak |sample| above a floor).

    The inbound caller audio is Opus-encoded then decoded + downsampled by the
    engine, so silence is not perfectly zero; a peak floor distinguishes the
    caller's tone from the silence run.
    """
    peak = 0
    for i in range(0, len(frame.samples) - 1, 2):
        value = int.from_bytes(frame.samples[i : i + 2], "little", signed=True)
        peak = max(peak, abs(value))
    return peak > 256


class _FakeASR:
    """A streaming ASR whose end-of-turn is driven by the caller's speech→silence.

    The real CallLoop pump feeds it the SAME frames it feeds the VAD (the engine's
    decoded + downsampled inbound Opus). Yields ONE final/end-of-turn Transcript only
    after seeing speech and then ``_silence_to_end`` consecutive silent frames — so a
    broken inbound SRTP-Opus path (no frames, or garbage that never scores as speech)
    delivers no turn.
    """

    input_sample_rate = 16_000

    def __init__(self, *, transcript: str, silence_to_end: int = 6) -> None:
        self._transcript = transcript
        self._silence_to_end = silence_to_end
        self.frames_seen = 0
        self.saw_speech = False
        self.turns_emitted = 0

    async def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        trailing_silence = 0
        async for frame in audio:
            self.frames_seen += 1
            if _frame_is_speech(frame):
                self.saw_speech = True
                trailing_silence = 0
                continue
            if not self.saw_speech or self.turns_emitted:
                continue
            trailing_silence += 1
            if trailing_silence >= self._silence_to_end:
                self.turns_emitted += 1
                yield Transcript(
                    text=self._transcript,
                    is_final=True,
                    end_of_turn=True,
                    confidence=1.0,
                )


class _FakeGuard:
    """A guard that ALLOWs every turn (injection screening is not under test)."""

    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            normalized_text=text,
            reasons=(),
            degraded=False,
            score=0.0,
        )


# ---------------------------------------------------------------------------
# Test wiring: the real VoipAdapter pointed at the fake WebRTC gateway, with the
# WebRtcMediaSession's ICE pipe injected so the handshake runs in-memory.
# ---------------------------------------------------------------------------

_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": _TO_USER,
    "HERMES_SIP_PASSWORD": "fake-password",
    "HERMES_SIP_EXPIRES": "120",
}


def _platform_config() -> PlatformConfig:
    return PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))


async def _no_sleep(_seconds: float) -> None:
    """An instant ``sleep`` so the engine flushes outbound RTP without delay."""


@asynccontextmanager
async def _real_adapter(
    gateway: FakeWebRtcGateway,
    *,
    vad_model: _RecordingVadModel,
    providers: Providers,
) -> AsyncIterator[VoipAdapter]:
    """Yield the real ``VoipAdapter`` wired to the loopback WebRTC gateway + fakes.

    Patches ONLY the leaf seams that must be faked for a hermetic test:

    * ``build_providers`` → fake ASR/TTS/guard;
    * ``load_silero_model`` → a fake per-window model, so the real ``_make_vad`` builds
      a real ``VoiceActivityDetector`` at the rate the adapter chooses (the WebRTC
      engine's 16 kHz Opus analysis rate);
    * ``_make_tls_context`` → unused (the transport factory supplies the client
      context);
    * ``SipOverTlsTransport`` → the real class, redirected to the loopback port;
    * ``RtpMediaTransport`` → the real engine with instant pacing (real SRTP + Opus +
      ICE);
    * ``WebRtcMediaSession`` → the real class, but with an ``ice_factory`` injected that
      returns the **adapter's end of the in-memory ICE pipe** the gateway peer shares —
      so the real DTLS handshake + SRTP keying + Opus media run over loopback with NO
      real ICE/STUN sockets (the same linked-pipe harness as
      ``test_media_webrtc_session.py``).

    The transport, manager, dialog, CallSession, CallLoop, SDP, DTLS, SRTP, Opus, and
    VAD are all REAL. The adapter is disconnected on exit.
    """
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    def _transport_factory(**kwargs: object) -> SipOverTlsTransport:
        kwargs["ssl_context"] = client_ssl_context()
        kwargs["server_hostname"] = "pbx.example.test"
        kwargs["connect_address"] = "127.0.0.1"
        kwargs["port"] = gateway.sip_port
        return SipOverTlsTransport(**kwargs)  # type: ignore[arg-type]  # forwards the adapter's own kwargs; only test overrides are injected

    def _engine_factory(**kwargs: object) -> RtpMediaTransport:
        # The REAL engine with instant pacing. On the WebRTC path it carries SRTP-Opus
        # over the injected ICE pipe; the SRTP transform, Opus codec, and RFC 7983 demux
        # are all the real engine.
        kwargs["sleep"] = _no_sleep
        return RtpMediaTransport(**kwargs)  # type: ignore[arg-type]  # forwards the adapter's own kwargs; only the sleep seam is injected

    def _session_factory(
        *,
        offer_setup: SetupRole | None,
        stun_urls: tuple[str, ...] = (),
        **_kw: object,
    ) -> WebRtcMediaSession:
        # The REAL WebRtcMediaSession (real DtlsEndpoint, real SRTP derivation) but
        # with the adapter's end of the gateway peer's in-memory ICE pipe injected, so
        # no aioice agent / real STUN socket is needed. The peer (gateway) holds the
        # other end and runs the client side of the handshake concurrently.
        return WebRtcMediaSession(
            offer_setup=offer_setup,
            stun_urls=stun_urls,
            ice_factory=gateway.adapter_ice_factory(),
        )

    with (
        patch("hermes_voip.adapter.build_providers", return_value=providers),
        patch(
            "hermes_voip.adapter.load_silero_model",
            return_value=vad_model,
        ),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch(
            "hermes_voip.adapter.SipOverTlsTransport",
            side_effect=_transport_factory,
        ),
        patch(
            "hermes_voip.adapter.RtpMediaTransport",
            side_effect=_engine_factory,
        ),
        patch(
            "hermes_voip.adapter.WebRtcMediaSession",
            side_effect=_session_factory,
        ),
    ):
        adapter = VoipAdapter(_platform_config())
        up = await adapter.connect()
        assert up is True, "adapter did not register at least one extension"
        try:
            yield adapter
        finally:
            await adapter.disconnect()


async def _until(
    predicate: Callable[[], bool], *, timeout: float = 5.0, step: float = 0.005
) -> None:
    """Poll ``predicate`` until true or the timeout elapses (no fixed sleeps)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            msg = "condition not met within the timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(step)


# ===========================================================================
# The full inbound WebRTC call, end-to-end, against the real stack.
# ===========================================================================


async def test_full_inbound_webrtc_call_end_to_end() -> None:  # noqa: PLR0915 — one end-to-end WebRTC call is one logical scenario; splitting REGISTER→INVITE→DTLS→ACK→media→BYE across fixtures would hide the ordering each assertion depends on
    """A complete inbound WebRTC call exercises every seam with real DTLS-SRTP + Opus.

    Drives the assembled ``is_webrtc`` path: REGISTER → SAVPF/Opus/DTLS INVITE → SAVPF
    200 OK → real DTLS-SRTP handshake over the in-memory ICE pipe → ACK → SRTP-Opus
    greeting out (peer decrypts + Opus-decodes it) → SRTP-Opus caller speech in (one
    turn delivered, echo reply SRTP-Opus out) → BYE → clean teardown.
    """
    caplog_handler = _CapturingHandler()
    logging.getLogger("hermes_voip").addHandler(caplog_handler)
    logging.getLogger("hermes_voip").setLevel(logging.DEBUG)

    loop = asyncio.get_running_loop()
    loop_exceptions: list[str] = []
    previous_handler = loop.get_exception_handler()

    def _record_loop_exception(
        _loop: asyncio.AbstractEventLoop, context: dict[str, object]
    ) -> None:
        loop_exceptions.append(str(context.get("message", context)))

    loop.set_exception_handler(_record_loop_exception)

    gateway = FakeWebRtcGateway()
    gateway.set_register_responder()
    await gateway.start()

    fake_asr = _FakeASR(transcript="hello over webrtc")
    fake_tts = _FakeTTS()
    providers = Providers(
        asr=fake_asr,
        tts=fake_tts,
        guard=_FakeGuard(),
    )
    vad_model = _RecordingVadModel()

    delivered_turns: list[str] = []

    async def _echo_agent(event: MessageEvent) -> str:
        # Record only real CALLER turns, not the internal call-end signal event
        # (the BYE teardown injects an ``internal=True`` MessageEvent through this
        # same handler). The test asserts EXACTLY one caller turn.
        if not getattr(event, "internal", False):
            delivered_turns.append(event.text)
        return f"echo: {event.text}"

    try:
        async with _real_adapter(
            gateway, vad_model=vad_model, providers=providers
        ) as adapter:
            adapter.set_message_handler(_echo_agent)

            # (1) INVITE (WebRTC SAVPF/Opus/DTLS) → 200 OK + the real DTLS handshake.
            # send_invite_and_handshake builds the offer from the peer's real
            # WebRtcMediaSession, sends the INVITE, awaits the 200 OK (recording the
            # To-tag + the answer), and runs the peer's side of the DTLS handshake
            # against the adapter's (the adapter's run_handshake is awaited inside
            # _setup_webrtc_call after the 200 OK, over the SAME in-memory ICE pipe).
            # The ACK is sent below, once the dialog is established (the adapter
            # registers the in-dialog route only after the handshake — see the helper's
            # docstring).
            call = await gateway.send_invite_and_handshake(to_user=_TO_USER)

            # The dialog-forming 200 OK must carry a non-empty To-tag, a SAVPF answer
            # with our DTLS fingerprint + ICE creds, and NO a=crypto (RFC 5763 §5).
            assert call.remote_to_tag, "200 OK to the WebRTC INVITE carries no To-tag"
            assert call.answer_is_webrtc, (
                "200 OK answer is not a UDP/TLS/RTP/SAVPF (WebRTC) answer — the "
                "is_webrtc route regressed to the SDES/plain-RTP path"
            )
            assert call.answer_fingerprint is not None, (
                "the SAVPF answer carries no a=fingerprint (DTLS-SRTP keying)"
            )
            assert "a=crypto" not in call.answer_sdp, (
                "the WebRTC answer carries an SDES a=crypto line (RFC 5763 §5 forbids)"
            )
            # The real DTLS handshake completed on BOTH ends and keyed SRTP.
            assert gateway.handshake_complete, (
                "the peer's DTLS-SRTP handshake did not complete"
            )
            assert isinstance(gateway.srtp_inbound, SrtpSession)
            assert isinstance(gateway.srtp_outbound, SrtpSession)

            # The adapter registers the in-dialog route + starts the CallLoop only AFTER
            # its run_handshake returns (the WebRTC handshake delays it, unlike SDES).
            # Wait on that observable state — deterministic, no scheduler-settle guess —
            # THEN send the ACK so it routes in-dialog (a real UAC's earlier ACK would
            # be briefly unroutable during the handshake, which is benign for a 2xx
            # ACK).
            call_id = call.call_id
            await _until(lambda: call_id in adapter._call_loops, timeout=5.0)
            await gateway.send_ack(call)

            # (2) The greeting flows out as SRTP-protected Opus over the ICE pipe. The
            # peer decrypts it with its DTLS-derived SRTP and Opus-decodes it — proving
            # real KEYED media reaches the far end (not just "a 200 OK was sent").
            await gateway.wait_for_decoded_audio(frames=2, timeout=5.0)
            greeting = gateway.decoded_inbound_frames[:2]
            for pcm in greeting:
                assert len(pcm) == OPUS_FRAME_SAMPLES * PCM16_BYTES_PER_SAMPLE, (
                    "the decrypted+decoded greeting is not one 20 ms 48 kHz Opus frame "
                    f"({OPUS_FRAME_SAMPLES} samples); got {len(pcm)} bytes — the "
                    "SRTP-Opus media path is broken"
                )
            assert any(_pcm_has_energy(pcm) for pcm in greeting), (
                "the decrypted+decoded greeting is silent — SRTP decrypt or Opus "
                "decode produced garbage (a keying/codec regression)"
            )
            greeting_count = gateway.inbound_srtp_packet_count

            # (3) Inbound caller audio: SRTP-Opus speech then silence the OTHER way. The
            # real CallLoop pump decrypts + Opus-decodes + downsamples (48→16 kHz) it
            # and the real
            # VAD/ASR deliver exactly ONE turn carrying the transcript — which only
            # arrives if inbound
            # SRTP-Opus decrypts + decodes correctly.
            gateway.send_caller_audio(opus_speech_frames(20))
            gateway.send_caller_audio(opus_silence_frames(25))

            await _until(lambda: len(delivered_turns) >= 1, timeout=5.0)
            assert len(delivered_turns) == 1, (
                "the caller's speech→silence turn was not delivered exactly once over "
                "the SRTP-Opus inbound path"
            )
            assert "hello over webrtc" in delivered_turns[0], (
                "the caller's transcript is missing from the delivered turn — inbound "
                "SRTP-Opus did not decrypt/decode to drive the ASR"
            )
            assert "UNTRUSTED_CALLER_TRANSCRIPT" in delivered_turns[0], (
                "the caller transcript is not spotlighted as untrusted data (ADR-0020)"
            )
            assert fake_asr.saw_speech, "the ASR never saw the caller's speech frames"

            # The real VAD ran over the engine's 16 kHz Opus analysis frames.
            assert vad_model.sample_rates, "the VAD was never fed any inbound audio"
            assert all(sr == _OPUS_ANALYSIS_RATE for sr in vad_model.sample_rates), (
                f"the VAD was fed a non-16 kHz rate: {set(vad_model.sample_rates)} "
                "(the WebRTC engine downsamples Opus 48→16 kHz for the VAD/STT)"
            )

            # (4) The echo reply is Opus-encoded + SRTP-protected and sent back to the
            # peer — strictly MORE inbound SRTP packets than the greeting, the reply
            # text handed to
            # the TTS.
            await gateway.wait_for_srtp_packets(greeting_count + 1, timeout=5.0)
            assert gateway.inbound_srtp_packet_count > greeting_count, (
                "no agent-reply SRTP-Opus was sent to the peer after the greeting"
            )
            assert any("hello over webrtc" in t for t in fake_tts.synth_texts), (
                "the agent reply text was not handed to the TTS for synthesis"
            )

            # (5) BYE → clean teardown. The CallSession must be removed.
            assert call_id in adapter._call_loops  # call is live before BYE
            await gateway.send_bye(call)
            await gateway.await_response(method="BYE", status=200)
            await _until(lambda: call_id not in adapter._call_loops, timeout=5.0)
            assert call_id not in adapter._call_loops, (
                "the CallSession/CallLoop was not torn down after BYE"
            )

            # The in-dialog ACK (sent after establishment) and BYE both routed in-dialog
            # — no SIP message was reported unroutable. (A real caller's ACK during the
            # handshake could be briefly unroutable before the dialog registers; this
            # harness sends the ACK post-establishment, so the in-dialog ACK + BYE are
            # what is asserted here — the To-tag dialog-routing path the SDES e2e
            # covers.)
            unroutable = [
                r.getMessage()
                for r in caplog_handler.records
                if "unroutable" in r.getMessage().lower()
            ]
            assert not unroutable, f"a SIP message was unroutable: {unroutable}"

        await asyncio.sleep(0)
        assert not loop_exceptions, (
            f"unhandled task/loop exceptions during the call: {loop_exceptions}"
        )
    finally:
        loop.set_exception_handler(previous_handler)
        await gateway.stop()
        logging.getLogger("hermes_voip").removeHandler(caplog_handler)


# ===========================================================================
# Negative control: prove the harness's "media flows" assertion is REAL — SRTP
# protected under a DIFFERENT key does NOT decrypt, so a keying regression cannot
# pass the round-trip assertions above as a false-green.
# ===========================================================================


async def test_regression_wrong_srtp_key_fails_to_decrypt() -> None:
    """A packet protected under the wrong SRTP key fails to ``unprotect`` (sanity).

    The full-call test asserts the peer DECRYPTS the greeting with the DTLS-derived
    SRTP. This control proves that assertion has teeth: SRTP protected under a key the
    receiver does not share raises on ``unprotect`` — so if the adapter's DTLS keying
    regressed (wrong role, wrong export, the is_webrtc route falling back to plain RTP),
    the peer's decrypt of the greeting would fail and the full-call test would FAIL, not
    silently pass. Uses the same real :class:`SrtpSession` transform the engine uses.
    """
    # Two independent DTLS endpoints would key differently; here we model "the wrong
    # key" directly: protect with one session, unprotect with another keyed from
    # different raw material. Real RFC-3711 AES-CM/HMAC, the engine's transform.
    suite = "AES_CM_128_HMAC_SHA1_80"
    good = SrtpSession.from_raw_keys(b"\x01" * 16, b"\x02" * 14, suite=suite)
    wrong = SrtpSession.from_raw_keys(b"\x09" * 16, b"\x0a" * 14, suite=suite)

    encoder = OpusEncoder()
    pcm = (4096).to_bytes(2, "little", signed=True) * OPUS_FRAME_SAMPLES
    opus_payload = encoder.encode(pcm)
    pkt = RtpPacket(
        payload_type=111,
        sequence_number=1,
        timestamp=0,
        ssrc=0x1234ABCD,
        payload=opus_payload,
    )
    wire = good.protect(pkt)

    # The matching key recovers the exact Opus payload and it Opus-decodes to one frame.
    recovered = good.unprotect(wire)
    assert recovered.payload == opus_payload
    decoded = OpusDecoder().decode(recovered.payload)
    assert len(decoded) == OPUS_FRAME_SAMPLES * PCM16_BYTES_PER_SAMPLE

    # The WRONG key cannot recover it — auth fails (or the payload is garbage). This is
    # the regression the full-call round-trip catches: a keying break => no decrypt.
    with pytest.raises(Exception):  # noqa: B017, PT011 — SrtpError subclasses Exception; any decrypt failure proves the point
        wrong.unprotect(wire)


# ---------------------------------------------------------------------------
# Small test-local helpers.
# ---------------------------------------------------------------------------


def _pcm_has_energy(pcm16: bytes) -> bool:
    """True iff any PCM16 sample in ``pcm16`` is non-zero (carries energy)."""
    return any(pcm16)


class _CapturingHandler(logging.Handler):
    """A logging handler that records emitted records for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
