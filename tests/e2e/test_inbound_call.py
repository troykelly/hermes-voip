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
from unittest.mock import patch

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
    ``cancel`` — structurally a ``TtsStream`` so it needs no type-ignore. After
    ``cancel`` it stops yielding (barge-in semantics).
    """

    def __init__(self, frames: list[PcmFrame]) -> None:
        self._frames = list(frames)
        self._index = 0
        self._cancelled = False

    def __aiter__(self) -> _FakeTtsStream:
        return self

    async def __anext__(self) -> PcmFrame:
        if self._cancelled or self._index >= len(self._frames):
            raise StopAsyncIteration
        frame = self._frames[self._index]
        self._index += 1
        return frame

    async def flush(self) -> None:
        """No buffered text to drain (fixed-frame fake)."""

    async def cancel(self) -> None:
        self._cancelled = True


class _FakeTTS:
    """Synthesises any text to two 24 kHz frames; records every synth request.

    Emitting at 24 kHz forces the engine's ``send_audio`` to resample to the 8 kHz
    G.711 wire before encoding (ADR-0017) — the seam that crashed the live call
    when the greeting/reply was 24 kHz (bug 2).
    """

    output_sample_rate = _KOKORO_RATE

    def __init__(self) -> None:
        self.synth_texts: list[str] = []

    def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
        async def _collect_then_register() -> None:
            async for chunk in text:
                self.synth_texts.append(chunk)

        # The CallLoop drives ``text`` as an async iterator; collect it eagerly in
        # the background so the recorded text is available for assertions. The
        # frames are fixed so synthesis does not depend on the text content.
        asyncio.ensure_future(_collect_then_register())  # noqa: RUF006 - fire-and-forget collector; the loop awaits the stream
        sample = (4096).to_bytes(2, "little", signed=True)
        frame = PcmFrame(
            samples=sample * _SAMPLES_PER_FRAME_24K,
            sample_rate=_KOKORO_RATE,
            monotonic_ts_ns=0,
        )
        return _FakeTtsStream([frame, frame])


class _FakeASR:
    """A streaming ASR that yields exactly ONE final end-of-turn transcript.

    The real CallLoop pump feeds VAD-windowed 8 kHz frames into ``stream``. This
    fake counts frames and, once it has seen the caller's speech-then-silence run
    (``_turn_after`` frames), yields a single
    ``Transcript(is_final=True, end_of_turn=True)`` and then stays quiet — so the
    delivery stage hands the agent exactly one turn (bug-free turn delivery), no
    matter how many further frames arrive before BYE.
    """

    input_sample_rate = 16_000

    def __init__(self, *, transcript: str, turn_after: int) -> None:
        self._transcript = transcript
        self._turn_after = turn_after
        self.frames_seen = 0

    async def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        emitted = False
        async for _frame in audio:
            self.frames_seen += 1
            if not emitted and self.frames_seen >= self._turn_after:
                emitted = True
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
        patch("hermes_voip.adapter._make_tls_context", return_value=None),
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
        asr=_FakeASR(transcript="unused", turn_after=1),
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
# (Bug 3, isolated) The real VAD/endpointer must accept the engine's 8 kHz
# inbound frames without the "frame rate 8000 != detector rate 16000" ValueError.
# This drives a real RtpMediaTransport pair through the real CallLoop pump so the
# rate seam is exercised exactly as in production.
# ===========================================================================


async def test_inbound_8k_frames_drive_real_vad_without_rate_error() -> None:
    """Real CallLoop pump feeds real 8 kHz inbound RTP to the real VAD, no raise.

    Isolated companion to the full-call test: it pins the exact rate seam. On a
    stack where the adapter still builds the VAD at 16 kHz this fails with the
    live ValueError; with the VAD built at the engine's 8 kHz inbound rate it
    passes and the VAD genuinely scores the audio (ONSET + OFFSET edges seen).
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
        asr=_FakeASR(transcript="hi", turn_after=20),
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

    gateway = FakeGateway()
    gateway.set_register_responder()
    await gateway.start()

    fake_asr = _FakeASR(transcript="hello there", turn_after=20)
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

            # (5) The greeting flows out as RTP at the 8 kHz wire rate (TTS=24 kHz).
            await gateway.rtp.wait_for_frames(1, timeout=5.0)
            greeting_frame = gateway.rtp.received_frames[0]
            assert greeting_frame.sample_rate == _G711_RATE, (
                "outbound RTP is not 8 kHz G.711 — the 24 kHz TTS was not "
                "resampled (bug 2)"
            )
            assert greeting_frame.sample_count > 0
            greeting_count = len(gateway.rtp.received_packets)

            # (6) Inbound caller audio: 8 kHz speech then silence. The real
            # CallLoop pump feeds it through the real VAD/endpoint/ASR. A rate
            # mismatch (bug 3) raises inside the call task and no turn lands.
            gateway.rtp.send_frames(g711_speech_frames(30))
            gateway.rtp.send_frames(g711_silence_frames(30))

            # Exactly one turn reaches the echo agent.
            await _until(lambda: len(delivered_turns) >= 1, timeout=5.0)
            assert delivered_turns == ["hello there"], (
                "the caller's speech→silence turn was not delivered exactly once "
                "(bug 3 would raise a VAD rate ValueError before any turn lands)"
            )

            # The real VAD ran over the real 8 kHz inbound frames (bug 3 seam).
            assert vad_model.sample_rates, "the VAD was never fed any inbound audio"
            assert all(sr == _G711_RATE for sr in vad_model.sample_rates), (
                f"the VAD was fed a non-8 kHz rate: {set(vad_model.sample_rates)} "
                "(bug 3: the detector must run at the engine's 8 kHz inbound rate)"
            )

            # The echo reply is synthesised (24 kHz → 8 kHz) and sent as RTP out.
            await gateway.rtp.wait_for_frames(greeting_count + 1, timeout=5.0)
            reply_frame = gateway.rtp.received_frames[-1]
            assert reply_frame.sample_rate == _G711_RATE, (
                "the agent reply RTP is not 8 kHz G.711 (bug 2)"
            )
            assert "echo: hello there" in fake_tts.synth_texts, (
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
    finally:
        await gateway.stop()
        logging.getLogger("hermes_voip").removeHandler(caplog_handler)

    # Settle any just-completed callbacks so an unretrieved task exception (bug 5)
    # is surfaced as a RuntimeWarning -> error within this test, not at teardown.
    await asyncio.sleep(0)


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
