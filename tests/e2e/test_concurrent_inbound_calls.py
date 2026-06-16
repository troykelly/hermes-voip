"""End-to-end CONCURRENT inbound-call integration test against the real stack.

The live production bug (2026-06-16): a gateway places multiple overlapping
INVITEs (seen with distinct Call-IDs, overlapping in time). The first call's
greeting succeeded; the overlapping calls crashed mid-greeting with
``RuntimeError: send_audio called before connect()`` and the teardown cascade
emitted ``RuntimeError: aclose(): asynchronous generator is already running`` and
``Task exception was never retrieved`` — so the gateway announced "something is
wrong with the remote party's network".

This module drives 3 genuinely-overlapping inbound calls through the REAL plugin
stack (``VoipAdapter`` → ``SipOverTlsTransport`` → ``RegistrationManager`` /
``Dialog`` / ``CallSession`` / ``CallLoop`` / ``RtpMediaTransport`` + real SDP)
against the loopback :mod:`tests.e2e._fake_gateway`, each call with its OWN RTP
endpoint, at real sample rates. It is the integration-seam regression guard for
the whole concurrency fix:

* per-call media isolation: each call's greeting RTP arrives on ITS OWN endpoint
  (one call never sends another call's media), all at the 8 kHz wire rate;
* no engine is stopped while another call is live (the cross-engine-stop bug);
* one call's BYE tears down ONLY that call — the others keep flowing;
* clean teardown across all calls: NO aclose generator race, NO unretrieved task
  exception (the module runs under ``-W error::RuntimeWarning`` and installs a
  loop exception handler).

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

from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.tts import TtsStream
from hermes_voip.transport.connection import SipOverTlsTransport
from tests.e2e._fake_gateway import (
    FakeGateway,
    FakeRtpEndpoint,
    g711_silence_frames,
    g711_speech_frames,
)
from tests.transport._loopback import client_ssl_context

if TYPE_CHECKING:
    from hermes_voip.adapter import VoipAdapter

# Promote RuntimeWarning to an error for THIS module so the teardown aclose race
# ("asynchronous generator is already running") and an unretrieved task exception
# ("Task exception was never retrieved") fail the test instead of printing to
# stderr — the exact cascade the live concurrent calls produced.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.filterwarnings("error::RuntimeWarning"),
]

_KOKORO_RATE = 24_000  # the real sherpa-Kokoro output rate (exercises 24→8 resample)
_G711_RATE = 8_000
_PTIME_MS = 20
_SAMPLES_PER_FRAME_24K = (_KOKORO_RATE * _PTIME_MS) // 1000
_SAMPLES_PER_FRAME_8K = (_G711_RATE * _PTIME_MS) // 1000
_TO_USER = "1000"


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
# Fakes at REAL sample rates (only the providers + agent are faked).
# ---------------------------------------------------------------------------


class _RecordingVadModel:
    """Fake silero ``VadModel``: scores a window high iff it carries energy."""

    def __init__(self) -> None:
        self.sample_rates: list[int] = []

    def __call__(self, window_pcm16: bytes, sample_rate: int) -> float:
        self.sample_rates.append(sample_rate)
        return 0.9 if any(window_pcm16) else 0.0


class _FakeTtsStream:
    """A ``TtsStream`` emitting fixed 24 kHz PCM16 frames (the real Kokoro rate)."""

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
    """Synthesises any text to two 24 kHz frames; records every synth request."""

    def __init__(self) -> None:
        self.synth_texts: list[str] = []

    @property
    def output_sample_rate(self) -> int:
        return _KOKORO_RATE

    def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
        _ = voice
        sample = (4096).to_bytes(2, "little", signed=True)
        frame = PcmFrame(
            samples=sample * _SAMPLES_PER_FRAME_24K,
            sample_rate=_KOKORO_RATE,
            monotonic_ts_ns=0,
        )
        return _FakeTtsStream([frame, frame], text, self.synth_texts)


def _frame_is_speech(frame: PcmFrame) -> bool:
    """True iff the frame carries audible energy (peak |sample| above a floor)."""
    peak = 0
    for i in range(0, len(frame.samples) - 1, 2):
        value = int.from_bytes(frame.samples[i : i + 2], "little", signed=True)
        peak = max(peak, abs(value))
    return peak > 256


class _FakeASR:
    """A streaming ASR whose end-of-turn is driven by the caller's speech→silence.

    Critically, the per-utterance state (saw-speech / turn-emitted / trailing
    silence) is LOCAL to each ``stream()`` call, not on the instance — a real
    streaming ASR is one provider object whose ``stream()`` opens an independent
    session per call. Concurrent calls share this one provider, so instance-level
    turn state would let call 0's turn suppress call 1's (a test artefact, not a
    product bug). Cumulative counters (frames seen, turns emitted) are kept on the
    instance only for cross-call assertions.
    """

    input_sample_rate = 16_000

    def __init__(self, *, transcript: str, silence_to_end: int = 10) -> None:
        self._transcript = transcript
        self._silence_to_end = silence_to_end
        self.frames_seen = 0
        self.saw_speech = False
        self.turns_emitted = 0

    async def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        # Per-utterance (per-call) state — independent for each concurrent call.
        saw_speech = False
        turn_emitted = False
        trailing_silence = 0
        async for frame in audio:
            self.frames_seen += 1
            if _frame_is_speech(frame):
                saw_speech = True
                self.saw_speech = True
                trailing_silence = 0
                continue
            if not saw_speech or turn_emitted:
                continue
            trailing_silence += 1
            if trailing_silence >= self._silence_to_end:
                turn_emitted = True
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
        _ = call_id
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            normalized_text=text,
            reasons=(),
            degraded=False,
            score=0.0,
        )


# ---------------------------------------------------------------------------
# Adapter wiring (mirrors tests/e2e/test_inbound_call.py::_real_adapter).
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
    gateway: FakeGateway,
    *,
    vad_model: _RecordingVadModel,
    providers: Providers,
) -> AsyncIterator[VoipAdapter]:
    """Yield the real ``VoipAdapter`` wired to the loopback gateway + fakes."""
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    def _transport_factory(**kwargs: object) -> SipOverTlsTransport:
        kwargs["ssl_context"] = client_ssl_context()
        kwargs["server_hostname"] = "pbx.example.test"
        kwargs["connect_address"] = "127.0.0.1"
        kwargs["port"] = gateway.sip_port
        return SipOverTlsTransport(**kwargs)  # type: ignore[arg-type]  # forwards the adapter's own kwargs; only test overrides are injected

    def _engine_factory(**kwargs: object) -> RtpMediaTransport:
        kwargs["sleep"] = _no_sleep
        return RtpMediaTransport(**kwargs)  # type: ignore[arg-type]  # forwards the adapter's own kwargs; only the sleep seam is injected

    with (
        patch("hermes_voip.adapter.build_providers", return_value=providers),
        patch("hermes_voip.adapter.load_silero_model", return_value=vad_model),
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
# The concurrent inbound calls, end-to-end, against the real stack.
# ===========================================================================

_N_CONCURRENT = 3


async def test_concurrent_inbound_calls_isolated_media_and_teardown() -> None:  # noqa: PLR0915, PLR0912 — N overlapping calls through the full REGISTER→INVITE→media→BYE stack is one logical scenario; splitting hides the interleaving each assertion depends on
    """Three overlapping inbound calls each get isolated media and clean teardown.

    Reproduces the live concurrent-INVITE crash at the integration seam and pins
    the whole fix:

    1. REGISTER + OPTIONS qualify (adapter.connect()).
    2. Three overlapping INVITEs (distinct Call-IDs, each with its OWN RTP
       endpoint) → three 200 OKs, each with a DISTINCT To-tag and a DISTINCT RTP
       port (per-call dialogs and engines).
    3. ACK all three. Each call's greeting RTP arrives on ITS OWN endpoint at the
       8 kHz wire rate — no endpoint receives another call's media (per-call media
       isolation; the pre-fix crash sent zero greeting frames on the overlapping
       calls).
    4. Drive caller speech→silence on ONE call: exactly one turn is delivered for
       that call and its echo reply flows out — while the other two calls stay up.
    5. BYE one call: ONLY that call tears down; the others remain live and can
       still receive RTP (one call ending never stops another's engine).
    6. BYE the rest. Across the whole scenario there is NO aclose generator race
       and NO unretrieved task exception (module under -W error::RuntimeWarning +
       a loop exception handler).
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

    gateway = FakeGateway()
    gateway.set_register_responder()
    await gateway.start()

    # One RTP endpoint per concurrent call so each call's media is independent.
    endpoints: list[FakeRtpEndpoint] = []
    for _ in range(_N_CONCURRENT):
        endpoint = FakeRtpEndpoint()
        await endpoint.start()
        endpoints.append(endpoint)

    fake_asr = _FakeASR(transcript="hello there")
    fake_tts = _FakeTTS()
    providers = Providers(asr=fake_asr, tts=fake_tts, guard=_FakeGuard())
    vad_model = _RecordingVadModel()

    delivered_turns: list[str] = []

    async def _echo_agent(event: MessageEvent) -> str:
        delivered_turns.append(event.text)
        return f"echo: {event.text}"

    try:
        async with _real_adapter(
            gateway, vad_model=vad_model, providers=providers
        ) as adapter:
            adapter.set_message_handler(_echo_agent)

            await gateway.send_options(to_user=_TO_USER)
            await gateway.await_response(method="OPTIONS", status=200)

            # (2) Three overlapping INVITEs — send all three BEFORE answering any,
            # so they are genuinely concurrent (each handler task is in flight when
            # the next INVITE arrives), each advertising its own RTP endpoint.
            calls = [
                await gateway.send_invite(to_user=_TO_USER, rtp=endpoints[i])
                for i in range(_N_CONCURRENT)
            ]

            # Each INVITE gets its own 200 OK with a distinct To-tag + RTP port.
            for call, endpoint in zip(calls, endpoints, strict=True):
                ok = await gateway.await_invite_ok(call, rtp=endpoint, timeout=5.0)
                assert ok.status_code == 200
                assert call.remote_to_tag, "200 OK carries no dialog To-tag"
                assert call.plugin_rtp_port > 0, "SDP answer advertises no RTP port"

            # Distinct dialogs: every call's To-tag (the plugin's local tag) and
            # RTP port must be unique — proof the adapter built one dialog + one
            # engine PER call, not shared state keyed by a colliding identifier.
            to_tags = {call.remote_to_tag for call in calls}
            rtp_ports = {call.plugin_rtp_port for call in calls}
            assert len(to_tags) == _N_CONCURRENT, (
                f"expected {_N_CONCURRENT} distinct dialog To-tags, got {to_tags}"
            )
            assert len(rtp_ports) == _N_CONCURRENT, (
                f"expected {_N_CONCURRENT} distinct RTP ports, got {rtp_ports}"
            )
            # All three calls are live in the adapter, each under its own Call-ID.
            for call in calls:
                assert call.call_id in adapter._call_loops, (
                    f"call {call.call_id} not registered as a live CallLoop"
                )

            # (3) ACK all three; each greeting must flow out on ITS OWN endpoint.
            for call in calls:
                await gateway.send_ack(call)

            # The fake TTS emits two 24 kHz greeting frames; each must arrive,
            # resampled to 8 kHz G.711, on the call's OWN endpoint — and nowhere
            # else. The pre-fix crash sent ZERO frames on the overlapping calls.
            for endpoint in endpoints:
                await endpoint.wait_for_frames(2, timeout=5.0)
            for i, endpoint in enumerate(endpoints):
                assert len(endpoint.received_packets) >= 2, (
                    f"call {i} greeting sent < 2 RTP frames on its own endpoint "
                    "(concurrent-call media isolation broken)"
                )
                for packet in endpoint.received_packets[:2]:
                    assert packet.payload_type == Codec.PCMU.value, (
                        f"call {i} greeting RTP is not PCMU (0)"
                    )
                    assert len(packet.payload) == _SAMPLES_PER_FRAME_8K, (
                        f"call {i} greeting RTP is not 20 ms of 8 kHz G.711 "
                        "(24 kHz TTS not resampled to the wire rate)"
                    )
                assert endpoint.received_frames[0].sample_rate == _G711_RATE

            greeting_counts = [len(ep.received_packets) for ep in endpoints]

            # (4) Drive caller audio on call 0 only. Exactly one turn is delivered
            # and its echo reply flows out — while calls 1 and 2 stay live.
            endpoints[0].send_frames(g711_speech_frames(30))
            endpoints[0].send_frames(g711_silence_frames(40))

            await _until(lambda: len(delivered_turns) >= 1, timeout=5.0)
            assert delivered_turns == ["hello there"], (
                "call 0's speech→silence turn was not delivered exactly once"
            )

            # Call 0's echo reply is synthesised (24→8 kHz) and sent as RTP — its
            # endpoint receives MORE packets than its greeting baseline.
            await endpoints[0].wait_for_frames(greeting_counts[0] + 1, timeout=5.0)
            reply_packets = endpoints[0].received_packets[greeting_counts[0] :]
            assert reply_packets, "no agent-reply RTP was sent on call 0"
            for packet in reply_packets:
                assert packet.payload_type == Codec.PCMU.value
                assert len(packet.payload) == _SAMPLES_PER_FRAME_8K
            assert "echo: hello there" in fake_tts.synth_texts

            # Calls 1 and 2 are still live (call 0's activity didn't end them).
            for call in calls[1:]:
                assert call.call_id in adapter._call_loops, (
                    f"call {call.call_id} was torn down by call 0's activity "
                    "(cross-call interference)"
                )

            # (5) BYE call 0 only → ONLY call 0 tears down; 1 and 2 stay up.
            await gateway.send_bye(calls[0])
            await gateway.await_response(method="BYE", status=200)
            await _until(
                lambda: calls[0].call_id not in adapter._call_loops, timeout=5.0
            )
            assert calls[0].call_id not in adapter._call_loops, (
                "call 0 was not torn down after its BYE"
            )
            for call in calls[1:]:
                assert call.call_id in adapter._call_loops, (
                    f"call {call.call_id} was torn down by call 0's BYE "
                    "(one call ending stopped another — the live cross-teardown bug)"
                )

            # A still-live call must still pass inbound media (its engine wasn't
            # stopped by call 0's teardown): drive call 1 and assert a turn lands.
            endpoints[1].send_frames(g711_speech_frames(30))
            endpoints[1].send_frames(g711_silence_frames(40))
            await _until(lambda: len(delivered_turns) >= 2, timeout=5.0)
            assert delivered_turns[1] == "hello there", (
                "call 1 could not deliver a turn after call 0's teardown "
                "(its engine/loop was collaterally damaged)"
            )

            # (6) BYE the remaining calls → all tear down cleanly.
            for call in calls[1:]:
                await gateway.send_bye(call)
            await _until(
                lambda: all(c.call_id not in adapter._call_loops for c in calls),
                timeout=5.0,
            )

            # No SIP message was unroutable across any of the concurrent dialogs.
            unroutable = [
                r.getMessage()
                for r in caplog_handler.records
                if "unroutable" in r.getMessage().lower()
            ]
            assert not unroutable, f"a SIP message was unroutable: {unroutable}"

        # The adapter is now disconnected. Settle the loop so any callback/finaliser
        # from the torn-down call tasks runs, then assert NO unhandled task/loop
        # exception and NO aclose generator race surfaced across all the concurrent
        # calls (the live cascade). The -W error::RuntimeWarning filter catches the
        # generator race; the loop exception handler catches the unretrieved task.
        await asyncio.sleep(0)
        assert not loop_exceptions, (
            "unhandled task/loop exceptions during concurrent calls "
            f"(the live aclose / unretrieved-task cascade): {loop_exceptions}"
        )
    finally:
        loop.set_exception_handler(previous_handler)
        for endpoint in endpoints:
            endpoint.stop()
        await gateway.stop()
        logging.getLogger("hermes_voip").removeHandler(caplog_handler)


class _CapturingHandler(logging.Handler):
    """A logging handler that records every emitted record for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
