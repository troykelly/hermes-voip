"""End-to-end inbound-call integration test against the REAL plugin stack.

This is the test that should have caught the live no-audio / voicemail bugs in CI
instead of on a real phone call. It drives a COMPLETE inbound call — REGISTER →
OPTIONS qualify → INVITE/200/ACK → media (greeting out, caller speech in, one
agent turn, reply out) → BYE/teardown — against the real ``VoipAdapter``, the real
``SipOverTlsTransport``, real ``RegistrationManager`` / ``Dialog`` / ``CallSession``
/ ``CallLoop`` / ``RtpMediaTransport``, and real SDP — with only the far-end
gateway (:mod:`tests.e2e._fake_gateway`) and the LLM agent faked, **at real sample
rates at every seam**.

Each of the five live bugs maps to an assertion here:

1. OPTIONS qualify un-answered → voicemail. Caught by
   :func:`test_options_qualify_is_answered` (asserts the 200 OK).
2. 24 kHz TTS not resampled to 8 kHz G.711 → crash. Caught by
   :func:`test_full_inbound_call_end_to_end` (greeting + reply RTP arrive at
   8 kHz; the fake TTS emits at the real 24 kHz Kokoro rate).
3. VAD fed 8 kHz into a 16 kHz detector → ValueError. Caught by
   :func:`test_full_inbound_call_end_to_end` (one turn lands, no rate error) and
   the isolated :func:`test_inbound_8k_frames_drive_real_vad_without_rate_error`.
4. Missing dialog To-tag → in-dialog ACK/BYE routed out-of-dialog. Caught by
   :func:`test_full_inbound_call_end_to_end` (200 OK has a To-tag; ACK/BYE route
   in-dialog — no "unroutable" log).
5. Teardown async-generator ``aclose`` race / unretrieved task exception. Caught
   by running the whole module under ``-W error::RuntimeWarning`` (asyncio emits
   both as ``RuntimeWarning``) plus the clean-teardown assertions after BYE.

The adapter imports the real Hermes ``BasePlatformAdapter`` at module top, so the
module ``importorskip``s the ``hermes`` extra and runs in the ``hermes-contract``
CI job (rule 26: validate against the real deployment target). Fakes only —
``pbx.example.test`` / ext ``1000`` / ``127.0.0.1``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

# The adapter imports the real Hermes base at module top; skip the whole module
# when the optional runtime is absent (it runs in the hermes-contract CI job).
pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import MessageEvent

from hermes_voip.media.audio import frame_to_ulaw
from hermes_voip.media.call_loop import CallLoop
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.media.vad import SpeechEdge, VadEvent, VoiceActivityDetector
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.providers.tts import TtsStream
from hermes_voip.rtp import RtpPacket
from hermes_voip.transport.connection import SipOverTlsTransport
from tests.e2e._fake_gateway import (
    FakeGateway,
    g711_silence_frames,
    g711_speech_frames,
)
from tests.transport._loopback import client_ssl_context

if TYPE_CHECKING:
    from hermes_voip.adapter import VoipAdapter

# Promote RuntimeWarning to an error for THIS module so the teardown async-generator
# ``aclose`` race ("asynchronous generator is already running") and an unretrieved
# task exception ("Task exception was never retrieved") fail the test instead of
# printing to stderr (bug 5). asyncio emits both as RuntimeWarning.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.filterwarnings("error::RuntimeWarning"),
]

# The TTS provider's REAL output rate (sherpa-Kokoro emits 24 kHz); driving the
# fake at this rate exercises the outbound 24 kHz → 8 kHz resample seam (bug 2).
_KOKORO_RATE = 24_000
_G711_RATE = 8_000
_PTIME_MS = 20
_SAMPLES_PER_FRAME_24K = (_KOKORO_RATE * _PTIME_MS) // 1000  # 480 samples / frame
_SAMPLES_PER_FRAME_8K = (_G711_RATE * _PTIME_MS) // 1000  # 160 samples / frame
_TO_USER = "1000"


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
# Fake providers at REAL sample rates (only the providers + agent are faked).
# ---------------------------------------------------------------------------


class _RecordingVadModel:
    """A fake silero ``VadModel``: scores a window high iff it carries energy.

    Records the ``sample_rate`` it is called with so a test can assert the real
    :class:`~hermes_voip.media.vad.VoiceActivityDetector` ran at the rate the
    adapter configured — and, because the real detector validates the frame rate
    in ``feed()``, the real 8 kHz inbound frames must reach it without a rate
    ValueError (bug 3). Returns 0.9 for a non-silent window, 0.0 for silence, so
    speech→silence inbound audio yields a real ONSET then OFFSET edge.
    """

    def __init__(self) -> None:
        self.sample_rates: list[int] = []

    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        self.sample_rates.append(sample_rate)
        return 0.9 if any(window_pcm16) else 0.0


class _FakeTtsStream:
    """A ``TtsStream`` emitting fixed 24 kHz PCM16 frames (the real Kokoro rate).

    A genuine async iterator (``__aiter__`` + ``__anext__``) plus ``flush`` /
    ``cancel`` — structurally a ``TtsStream`` so it needs no type-ignore. It drains
    the agent's ``text`` iterator on the first frame and appends the joined text to
    ``recorded`` (no fire-and-forget task — the stream owns ``text`` the way a real
    synthesiser does). After ``cancel`` it stops yielding (barge-in semantics).
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
    """Synthesises any text to two 24 kHz frames; records every synth request.

    Emitting at 24 kHz forces the engine's ``send_audio`` to resample to the 8 kHz
    G.711 wire before encoding (ADR-0017) — the seam that crashed the live call
    when the greeting/reply was 24 kHz (bug 2). Each synthesised stream records the
    full joined text it was given in :attr:`synth_texts`.
    """

    output_sample_rate = _KOKORO_RATE

    def __init__(self) -> None:
        self.synth_texts: list[str] = []

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        sample = (4096).to_bytes(2, "little", signed=True)
        frame = PcmFrame(
            samples=sample * _SAMPLES_PER_FRAME_24K,
            sample_rate=_KOKORO_RATE,
            monotonic_ts_ns=0,
        )
        return _FakeTtsStream([frame, frame], text, self.synth_texts)


def _frame_is_speech(frame: PcmFrame) -> bool:
    """True iff the frame carries audible energy (peak |sample| above a floor).

    The inbound RTP is G.711-encoded then decoded by the engine, so silence is not
    perfectly zero; a small peak floor distinguishes the caller's tone from the
    digital-silence run.
    """
    peak = 0
    for i in range(0, len(frame.samples) - 1, 2):
        value = int.from_bytes(frame.samples[i : i + 2], "little", signed=True)
        peak = max(peak, abs(value))
    return peak > 256


class _FakeASR:
    """A streaming ASR whose end-of-turn is driven by the caller's speech→silence.

    The real CallLoop pump feeds it the SAME 8 kHz frames it feeds the VAD. This
    fake overlays an endpoint signal exactly as a self-host (sherpa) ASR must: it
    accumulates while the caller has energy and yields ONE
    ``Transcript(is_final=True, end_of_turn=True)`` only after it has seen speech
    and then ``_silence_to_end`` consecutive silent frames — so the delivered turn
    genuinely depends on the speech→silence inbound sequence (not a blind frame
    count). If the inbound path were broken (e.g. the VAD rate crash starves the
    pump), no frames arrive and no turn is ever emitted.
    """

    input_sample_rate = 16_000

    def __init__(self, *, transcript: str, silence_to_end: int = 10) -> None:
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
            # A silent frame: only counts toward end-of-turn once speech has begun.
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
# Test wiring: build the real VoipAdapter pointed at the fake gateway.
# ---------------------------------------------------------------------------

_FAKE_ENV: dict[str, str] = {
    "HERMES_SIP_HOST": "pbx.example.test",
    "HERMES_SIP_EXTENSION": _TO_USER,
    "HERMES_SIP_PASSWORD": "fake-password",
    "HERMES_SIP_EXPIRES": "120",
    # This e2e exercises the cleartext G.711 RTP/AVP answer path end-to-end, so the
    # secure-media mandate (ADR-0070) is disabled here; with it on (the production
    # default) the plain offer would be 488'd. The mandate is covered in
    # tests/test_adapter_secure_media.py.
    "HERMES_VOIP_REQUIRE_SECURE_MEDIA": "false",
    # Provider/model env is irrelevant — build_providers is replaced with fakes —
    # but a valid media config still parses with defaults from just the SIP keys.
}


def _platform_config() -> PlatformConfig:
    return PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))


async def _no_sleep(_seconds: float) -> None:
    """An instant ``sleep`` so the engine flushes outbound RTP without delay."""


@asynccontextmanager
async def _real_adapter(
    gateway: FakeGateway,
    *,
    vad_model: _RecordingVadModel,
    providers: Providers,
) -> AsyncIterator[VoipAdapter]:
    """Yield the real ``VoipAdapter`` wired to the loopback gateway + fakes.

    The patches stay active for the WHOLE ``async with`` body — crucially through
    inbound-call handling, not just ``connect()`` — because ``_make_vad`` and the
    engine/transport construction happen inside the fire-and-forget INVITE handler
    long after ``connect()`` returns. Patches ONLY the leaf seams that must be
    faked for a hermetic test:

    * ``build_providers`` → fake ASR/TTS/guard at real rates;
    * ``load_silero_model`` → a fake per-window model, so the real ``_make_vad``
      builds a real ``VoiceActivityDetector`` AT THE RATE THE ADAPTER CHOOSES
      (this is what surfaces the 8 kHz-vs-16 kHz rate bug, bug 3);
    * ``_make_tls_context`` → unused (the transport factory supplies the client
      context), kept patched so no real system-trust context is built;
    * ``SipOverTlsTransport`` → the real class, redirected to the loopback port;
    * ``RtpMediaTransport`` → the real engine with instant pacing.

    The transport, manager, dialog, CallSession, CallLoop, SDP, RTP, and VAD are
    all REAL. The adapter is disconnected on exit.
    """
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    def _transport_factory(**kwargs: object) -> SipOverTlsTransport:
        # Build the REAL transport but dial the loopback gateway: the adapter
        # passes host/port + the three observer callbacks; we override the TLS
        # context (trust the throwaway cert), the SNI name, the dial address, and
        # the port. Framer, dispatch, and transaction logic are all real.
        kwargs["ssl_context"] = client_ssl_context()
        kwargs["server_hostname"] = "pbx.example.test"
        kwargs["connect_address"] = "127.0.0.1"
        kwargs["port"] = gateway.sip_port
        return SipOverTlsTransport(**kwargs)  # type: ignore[arg-type]  # forwards the adapter's own kwargs; only test overrides are injected

    def _engine_factory(**kwargs: object) -> RtpMediaTransport:
        # Build the REAL RTP engine with instant outbound pacing (no wall-clock
        # ptime delay); socket, RTP packing, G.711 codec, the 24→8 kHz resample,
        # the SRTP seam, and comedia latching are all the real engine.
        kwargs["sleep"] = _no_sleep
        return RtpMediaTransport(**kwargs)  # type: ignore[arg-type]  # forwards the adapter's own kwargs; only the sleep seam is injected

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
# (Bug 1) OPTIONS qualify is answered 200 OK — without this the registrar marks
# the contact UNREACHABLE and diverts inbound calls to voicemail.
# ===========================================================================


async def test_options_qualify_is_answered() -> None:
    """An out-of-dialog OPTIONS qualify ping gets a 200 OK from the real stack.

    Would have caught the voicemail bug: the gateway qualifies the registered
    contact with OPTIONS and, getting no 200, stops ringing the extension.
    """
    gateway = FakeGateway()
    gateway.set_register_responder()
    await gateway.start()
    vad_model = _RecordingVadModel()
    providers = Providers(
        asr=_FakeASR(transcript="unused"),
        tts=_FakeTTS(),
        guard=_FakeGuard(),
    )
    try:
        async with _real_adapter(gateway, vad_model=vad_model, providers=providers):
            await gateway.send_options(to_user=_TO_USER)
            response = await gateway.await_response(method="OPTIONS", status=200)
            assert response.status_code == 200
            allow = response.header("Allow") or ""
            assert "INVITE" in allow  # the qualify reply advertises the UA methods
    finally:
        await gateway.stop()


# ===========================================================================
# (Media-layer rate contract) The real CallLoop pump must feed the engine's 8 kHz
# inbound frames to a VAD/endpointer built at that same rate without the
# "frame rate 8000 != detector rate 16000" ValueError. This pins the media-layer
# rate contract directly (the engine's inbound_sample_rate IS the rate the VAD must
# run at). The ADAPTER-side regression — the adapter must HONOUR that contract when
# it constructs the VAD — is covered by test_full_inbound_call_end_to_end, which
# drives the real _make_vad.
# ===========================================================================


async def test_inbound_8k_frames_drive_real_vad_without_rate_error() -> None:
    """Real CallLoop pump feeds real 8 kHz inbound RTP to a rate-matched VAD.

    Pins the media-layer rate contract: when the VAD/endpointer are built at the
    engine's ``inbound_sample_rate`` (8 kHz), the real pump feeds the real 8 kHz
    frames through them with no rate ValueError and the VAD genuinely scores the
    audio (real ONSET + OFFSET edges). The adapter-side guarantee that it picks
    that rate is asserted by the full-call test (it patches the real ``_make_vad``).
    """
    # The engine the plugin uses for inbound media decodes G.711 → 8 kHz PcmFrames.
    rx = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=0,
        codec=Codec.PCMU,
        symmetric=False,
        sleep=_no_sleep,
    )
    await rx.connect()
    sender = await _open_udp_sender("127.0.0.1", rx.local_port)

    vad_model = _RecordingVadModel()
    # Build the detector + endpointer at the engine's inbound rate — the fix the
    # full-call test asserts indirectly. ``inbound_sample_rate`` is the contract
    # the adapter must honour when constructing them.
    vad = VoiceActivityDetector(
        model=vad_model,
        sample_rate_hz=rx.inbound_sample_rate,
        threshold=0.5,
    )
    endpointer = Endpointer(silence_ms=200, sample_rate_hz=rx.inbound_sample_rate)
    edges: list[SpeechEdge] = []
    original_on_event = endpointer.on_event

    def _spy_on_event(event: VadEvent) -> None:
        edges.append(event.edge)
        original_on_event(event)

    # Wrap the endpointer's on_event to record edges; the real call still runs.
    endpointer.on_event = _spy_on_event  # type: ignore[method-assign]  # test spy wraps the bound method

    delivered: list[str] = []

    async def _deliver(text: str) -> None:
        delivered.append(text)

    loop = CallLoop(
        transport=rx,
        asr=_FakeASR(transcript="hi"),
        tts=_FakeTTS(),
        guard=_FakeGuard(),
        vad=vad,
        endpointer=endpointer,
        guard_state=GuardSessionState("c-iso"),
        deliver_turn=_deliver,
        voice="",
        call_id="c-iso",
    )
    run_task = asyncio.create_task(loop.run())
    sequencer = _UlawSequencer()
    try:
        # Enough 8 kHz speech windows to cross ONSET, then silence to cross OFFSET.
        for frame in g711_speech_frames(40):
            sender.send(sequencer.datagram(frame))
        for frame in g711_silence_frames(40):
            sender.send(sequencer.datagram(frame))
        await _until(lambda: SpeechEdge.ONSET in edges and SpeechEdge.OFFSET in edges)
        # The real VAD was fed 8 kHz frames and produced real edges — no rate
        # ValueError surfaced (which would have failed run_task).
        assert SpeechEdge.ONSET in edges
        assert SpeechEdge.OFFSET in edges
        assert all(sr == _G711_RATE for sr in vad_model.sample_rates)
        assert not run_task.done() or run_task.exception() is None
    finally:
        sender.close()
        await rx.stop()
        await asyncio.wait_for(run_task, timeout=3.0)


# ===========================================================================
# (All five bugs) The full inbound call, end-to-end, against the real stack.
# ===========================================================================


async def test_full_inbound_call_end_to_end() -> None:  # noqa: PLR0915 — one end-to-end call is one logical scenario; splitting the REGISTER→OPTIONS→INVITE→ACK→media→BYE sequence across fixtures would hide the ordering each assertion depends on
    """A complete inbound call exercises every seam at real sample rates.

    Sequence + the bug each step would have caught:

    1. REGISTER → registered (adapter.connect()).
    2. OPTIONS qualify → 200 OK (bug 1: voicemail).
    3. INVITE (G.711 RTP/AVP SDP) → 200 OK with a To-tag + SDP answer (bug 4).
    4. ACK (in-dialog) → routes to the CallSession, NOT unroutable (bug 4).
    5. Greeting RTP flows out at 8 kHz (the TTS is 24 kHz → resampled; bug 2).
    6. Inbound 8 kHz speech→silence RTP drives the real VAD/endpoint/ASR with NO
       rate ValueError (bug 3), delivering exactly ONE turn to the echo agent,
       whose reply is synthesised (24 kHz → 8 kHz) and sent as RTP (bug 2).
    7. BYE → clean teardown: CallSession removed, no aclose() generator race and
       no unretrieved task exception (bug 5; the module runs under
       ``-W error::RuntimeWarning``).
    """
    caplog_handler = _CapturingHandler()
    logging.getLogger("hermes_voip").addHandler(caplog_handler)
    logging.getLogger("hermes_voip").setLevel(logging.DEBUG)

    # Capture unhandled task exceptions via the loop exception handler (bug 5):
    # "Task exception was never retrieved" is reported HERE, not as a warning, so
    # the -W error::RuntimeWarning filter alone would miss a swallowed failure in
    # a fire-and-forget call task. Any entry here fails the test.
    loop = asyncio.get_running_loop()
    loop_exceptions: list[str] = []
    previous_handler = loop.get_exception_handler()

    def _record_loop_exception(
        _loop: asyncio.AbstractEventLoop, context: dict[str, object]
    ) -> None:
        loop_exceptions.append(str(context.get("message", context)))

    loop.set_exception_handler(_record_loop_exception)

    gateway = FakeGateway()
    gateway.set_register_responder()
    await gateway.start()

    fake_asr = _FakeASR(transcript="hello there")
    fake_tts = _FakeTTS()
    providers = Providers(
        asr=fake_asr,
        tts=fake_tts,
        guard=_FakeGuard(),
    )
    vad_model = _RecordingVadModel()

    # The fake echo agent: records each delivered turn and echoes it back. The
    # base BasePlatformAdapter.handle_message delivers a non-empty return value
    # via adapter.send() → CallLoop.speak() → TTS → RTP out (the real reply path).
    delivered_turns: list[str] = []

    async def _echo_agent(event: MessageEvent) -> str:
        # Record only real CALLER turns, not the internal call-end signal event
        # (ADR-0026): the BYE teardown injects an ``internal=True`` MessageEvent
        # (here the replayed disconnected note) through this same handler, and the
        # test asserts EXACTLY one turn was delivered — so the signal must not be
        # counted as a caller turn. The runtime gates lifecycle on ``internal``;
        # the test mirrors that.
        if not getattr(event, "internal", False):
            delivered_turns.append(event.text)
        return f"echo: {event.text}"

    try:
        async with _real_adapter(
            gateway, vad_model=vad_model, providers=providers
        ) as adapter:
            adapter.set_message_handler(_echo_agent)

            # (2) OPTIONS qualify.
            await gateway.send_options(to_user=_TO_USER)
            await gateway.await_response(method="OPTIONS", status=200)

            # (3) INVITE → 200 OK with To-tag + SDP answer.
            call = await gateway.send_invite(to_user=_TO_USER)
            ok = await gateway.await_invite_ok(call)
            assert ok.status_code == 200
            # The dialog-forming 200 OK must carry a non-empty To-tag (bug 4):
            # without it the gateway's in-dialog ACK/BYE route out-of-dialog.
            assert call.remote_to_tag is not None, (
                "200 OK to the INVITE carries no dialog To-tag (bug 4)"
            )
            assert call.remote_to_tag != "", "200 OK To-tag is empty (bug 4)"
            assert call.answer_sdp != "", "200 OK carries no SDP answer body"
            assert call.plugin_rtp_port > 0, "SDP answer advertises no RTP port"

            # (4) ACK (in-dialog). Must route to the CallSession, never unroutable.
            await gateway.send_ack(call)

            # (5) The greeting flows out as RTP at the 8 kHz wire rate. The fake
            # TTS emits TWO 24 kHz frames per synth, so wait for both before taking
            # a baseline (otherwise a late second greeting packet would satisfy the
            # reply check below — a false-green for reply playout, codex HIGH #4).
            await gateway.rtp.wait_for_frames(2, timeout=5.0)
            # Genuine 8 kHz G.711, proven on the WIRE (codec payload type + the
            # exact 20 ms payload length), not just the test decoder's metadata: a
            # mis-resampled or wrong-length frame would fail here (codec HIGH #3).
            for packet in gateway.rtp.received_packets[:2]:
                assert packet.payload_type == Codec.PCMU.value, (
                    "greeting RTP payload type is not PCMU (0)"
                )
                assert len(packet.payload) == _SAMPLES_PER_FRAME_8K, (
                    f"greeting RTP payload is {len(packet.payload)} octets, expected "
                    f"{_SAMPLES_PER_FRAME_8K} (20 ms of 8 kHz G.711) — the 24 kHz "
                    "TTS was not resampled to the wire rate (bug 2)"
                )
            assert gateway.rtp.received_frames[0].sample_rate == _G711_RATE
            greeting_count = len(gateway.rtp.received_packets)

            # (6) Inbound caller audio: 8 kHz speech then silence. The real
            # CallLoop pump feeds it through the real VAD/endpoint/ASR. A rate
            # mismatch (bug 3) raises inside the call task and no turn lands. The
            # fake ASR's end-of-turn is driven by the speech→silence transition
            # (not a frame count), so a turn lands only if the trailing silence is
            # actually delivered through the pipeline.
            gateway.rtp.send_frames(g711_speech_frames(30))
            gateway.rtp.send_frames(g711_silence_frames(40))

            # Exactly one turn reaches the echo agent. The delivered text is the
            # spotlighted per-mode persona preamble (ADR-0020) with the caller's
            # transcript fenced as untrusted DATA — so we assert one turn carrying
            # the caller's words inside the untrusted-data marker, not a bare equal.
            await _until(lambda: len(delivered_turns) >= 1, timeout=5.0)
            assert len(delivered_turns) == 1, (
                "the caller's speech→silence turn was not delivered exactly once "
                "(bug 3 would raise a VAD rate ValueError before any turn lands)"
            )
            assert "hello there" in delivered_turns[0], (
                "the caller's transcript is missing from the delivered turn"
            )
            assert "UNTRUSTED_CALLER_TRANSCRIPT" in delivered_turns[0], (
                "the caller transcript is not spotlighted as untrusted data (ADR-0020)"
            )
            assert fake_asr.saw_speech, "the ASR never saw the caller's speech frames"

            # The real VAD ran over the real 8 kHz inbound frames (bug 3 seam).
            assert vad_model.sample_rates, "the VAD was never fed any inbound audio"
            assert all(sr == _G711_RATE for sr in vad_model.sample_rates), (
                f"the VAD was fed a non-8 kHz rate: {set(vad_model.sample_rates)} "
                "(bug 3: the detector must run at the engine's 8 kHz inbound rate)"
            )

            # The echo reply is synthesised (24 kHz → 8 kHz) and sent as RTP out —
            # strictly MORE packets than the greeting, all genuine 8 kHz G.711.
            await gateway.rtp.wait_for_frames(greeting_count + 1, timeout=5.0)
            reply_packets = gateway.rtp.received_packets[greeting_count:]
            assert reply_packets, "no agent-reply RTP was sent after the greeting"
            for packet in reply_packets:
                assert packet.payload_type == Codec.PCMU.value
                assert len(packet.payload) == _SAMPLES_PER_FRAME_8K, (
                    "the agent reply RTP is not 20 ms of 8 kHz G.711 (bug 2)"
                )
            # The echo agent mirrors the (now spotlighted) turn, so the synthesised
            # reply contains the caller's transcript rather than equalling
            # "echo: hello there" verbatim.
            assert any("hello there" in t for t in fake_tts.synth_texts), (
                "the agent reply text was not handed to the TTS for synthesis"
            )

            # (7) BYE → clean teardown. The CallSession must be removed.
            call_id = call.call_id
            assert call_id in adapter._call_loops  # call is live before BYE
            await gateway.send_bye(call)
            await gateway.await_response(method="BYE", status=200)
            await _until(lambda: call_id not in adapter._call_loops, timeout=5.0)
            assert call_id not in adapter._call_loops, (
                "the CallSession/CallLoop was not torn down after BYE"
            )

            # No SIP message was reported unroutable: the ACK and BYE routed
            # in-dialog (bug 4). _on_unroutable logs unroutable at DEBUG.
            unroutable = [
                r.getMessage()
                for r in caplog_handler.records
                if "unroutable" in r.getMessage().lower()
            ]
            assert not unroutable, f"a SIP message was unroutable (bug 4): {unroutable}"

        # The adapter is now disconnected (the async with exited). Settle the loop
        # so any callback/finaliser from the torn-down call task runs, then assert
        # no unhandled task exception was reported and no aclose generator race
        # surfaced (bug 5). The module's -W error::RuntimeWarning catches the
        # generator race; the loop exception handler catches the unretrieved task.
        await asyncio.sleep(0)
        assert not loop_exceptions, (
            f"unhandled task/loop exceptions during the call (bug 5): {loop_exceptions}"
        )
    finally:
        loop.set_exception_handler(previous_handler)
        await gateway.stop()
        logging.getLogger("hermes_voip").removeHandler(caplog_handler)


# ---------------------------------------------------------------------------
# Small test-local async/UDP helpers (kept here, not in the harness, since they
# are only needed by the isolated VAD-rate test).
# ---------------------------------------------------------------------------


class _UlawSequencer:
    """Encodes consecutive 8 kHz PCM16 frames into G.711 RTP datagrams.

    Advances the RTP sequence number and timestamp per frame — a fixed seq would
    make the jitter buffer treat every packet as a duplicate of the first and drop
    all but one, starving the VAD of audio.
    """

    def __init__(self) -> None:
        self._seq = 0
        self._ts = 0

    def datagram(self, pcm16_8k: bytes) -> bytes:
        payload = frame_to_ulaw(
            PcmFrame(samples=pcm16_8k, sample_rate=_G711_RATE, monotonic_ts_ns=0)
        )
        packet = RtpPacket(
            payload_type=0,
            sequence_number=self._seq,
            timestamp=self._ts,
            ssrc=0x55AA55AA,
            payload=payload,
        )
        self._seq = (self._seq + 1) % (1 << 16)
        self._ts = (self._ts + _SAMPLES_PER_FRAME_8K) % (1 << 32)
        return packet.pack()


class _UdpSender(asyncio.DatagramProtocol):
    """A minimal loopback UDP sender for the isolated VAD-rate test."""

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None
        self._dest: tuple[str, int] | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self._transport = transport

    def send(self, data: bytes) -> None:
        if self._transport is None or self._dest is None:  # pragma: no cover
            msg = "sender not ready"
            raise RuntimeError(msg)
        self._transport.sendto(data, self._dest)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()


async def _open_udp_sender(host: str, port: int) -> _UdpSender:
    loop = asyncio.get_running_loop()
    sender = _UdpSender()
    sender._dest = (host, port)
    # Bind an UNCONNECTED local socket (local_addr, not remote_addr) so sendto()
    # may target an explicit destination — a connected socket rejects an address.
    await loop.create_datagram_endpoint(lambda: sender, local_addr=("127.0.0.1", 0))
    return sender


class _CapturingHandler(logging.Handler):
    """A logging handler that records emitted records for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
