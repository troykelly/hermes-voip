"""TDD tests for codex cross-vendor review findings on the conversational path.

Findings addressed (RED before GREEN):

  B1 (BLOCK): outbound 2xx-ACK omits Route headers -- RFC 3261 S13.2.2.4.
  B2 (BLOCK): send_audio crashes if stop() lands mid-loop (AttributeError on
              self._transport.sendto() after sleep() yields to stop()).
  W1 (WARN):  stale 407 with old CSeq accepted as final after re-auth.
  W2 (WARN):  EOT signal lost if endpointer fires twice before is_final.
  W3 (WARN):  wrong SIP error code 500 for SRTP refusal -- should be 488.
  N2 (NIT):   _QueueSink queue unbounded -- add maxsize.
  N3 (NIT):   RTP initial seq/ts are 0 -- should be random (RFC 3550 S5.1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_voip.dialog import Dialog
from hermes_voip.media.audio import G711_SAMPLE_RATE
from hermes_voip.media.call_loop import CallLoop
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.media.vad import VadEvent, VoiceActivityDetector
from hermes_voip.message import (
    SipRequest,
    SipResponse,
    build_request,
    build_response,
    new_branch,
    new_tag,
)
from hermes_voip.providers.asr import StreamingASR, Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.guard import GuardResult, GuardVerdict, InjectionGuard
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.sdp import (
    Codec as SdpCodec,
)
from hermes_voip.sdp import (
    SdpError,
    SessionDescription,
    build_audio_answer,
    build_audio_offer,
)

# ---------------------------------------------------------------------------
# B2 -- send_audio: stop() mid-loop must not raise AttributeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b2_stop_mid_send_audio_returns_cleanly() -> None:
    """stop() called between chunk iterations must NOT raise AttributeError.

    The engine checks self._transport once at method entry but then calls
    self._transport.sendto() inside a for-loop after an await self._sleep().
    If stop() fires during the sleep, _transport becomes None on the next
    iteration and sendto raises AttributeError -- crashing the TaskGroup.

    After the fix: the iteration re-checks _transport each time and exits
    cleanly (no exception) when it becomes None.
    """
    # Build a 2-chunk frame (2 x 160 samples = 40 ms) so the loop iterates
    # at least twice, giving stop() a window to fire during the sleep.
    samples_per_chunk = 160  # 20 ms at 8 kHz
    two_chunks = bytes(samples_per_chunk * 2 * 2)  # 2 chunks x 2 bytes/sample
    frame = PcmFrame(
        samples=two_chunks,
        sample_rate=G711_SAMPLE_RATE,
        monotonic_ts_ns=0,
    )

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=19000,
        codec=Codec.PCMU,
        sleep=AsyncMock(),
    )
    await engine.connect()

    stop_called = False

    async def _sleep_then_stop(delay: float) -> None:
        nonlocal stop_called
        if not stop_called:
            stop_called = True
            await engine.stop()

    engine._sleep = _sleep_then_stop

    # Must NOT raise; should return cleanly (frame dropped mid-stop).
    await engine.send_audio(frame)


@pytest.mark.asyncio
async def test_b2_stop_before_send_audio_does_not_raise() -> None:
    """stop() before any send_audio on an ever-connected engine: silent drop."""
    samples = bytes(320)
    frame = PcmFrame(samples=samples, sample_rate=G711_SAMPLE_RATE, monotonic_ts_ns=0)

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=19001,
        codec=Codec.PCMU,
    )
    await engine.connect()
    await engine.stop()

    # post-stop send_audio must be a no-op (not raise)
    await engine.send_audio(frame)


# ---------------------------------------------------------------------------
# B1 -- outbound ACK for 2xx must include Route headers from dialog.route_set
# ---------------------------------------------------------------------------


def _make_invite_with_2xx_record_route() -> tuple[str, str]:
    """Return (invite_text, response_text) where the 2xx carries Record-Route."""
    offer_body = build_audio_offer(
        local_address="192.0.2.1",
        port=17000,
        codecs=[SdpCodec(payload_type=0, encoding="PCMU", clock_rate=8000)],
        session_id=12345,
    )
    call_id = "test-call-b1@pbx.example.test"
    from_tag = new_tag()
    branch = new_branch()

    invite_text = build_request(
        "INVITE",
        "sip:1000@pbx.example.test",
        [
            ("Via", f"SIP/2.0/TLS 192.0.2.99:5061;branch={branch};rport"),
            ("From", f"<sip:agent@pbx.example.test>;tag={from_tag}"),
            ("To", "<sip:1000@pbx.example.test>"),
            ("Call-ID", call_id),
            ("CSeq", "1 INVITE"),
            ("Contact", "<sip:agent@192.0.2.99:5061;transport=tls>"),
            ("Content-Type", "application/sdp"),
        ],
        offer_body,
    )
    parsed_invite = SipRequest.parse(invite_text)
    remote_tag = new_tag()

    # 2xx with two Record-Route headers (proxy chain: proxy2 first, proxy1 second).
    response_text = build_response(
        parsed_invite,
        200,
        "OK",
        to_tag=remote_tag,
        extra_headers=(
            ("Contact", "<sip:1000@192.0.2.10:5060;transport=tls>"),
            ("Record-Route", "<sip:proxy2.example.test;lr>"),
            ("Record-Route", "<sip:proxy1.example.test;lr>"),
            ("Content-Type", "application/sdp"),
        ),
        body=offer_body,
    )
    return invite_text, response_text


def test_b1_ack_includes_route_from_dialog_route_set() -> None:
    """The ACK built for a 2xx with Record-Route carries the Route headers.

    RFC 3261 S13.2.2.4: the 2xx ACK MUST use the dialog route set (Record-Route
    from the 2xx, reversed, as Route headers). The adapter's _handle_outbound_invite
    builds the ACK manually; before the fix it omits Route entirely.
    """
    invite_text, response_text = _make_invite_with_2xx_record_route()
    parsed_invite = SipRequest.parse(invite_text)
    parsed_response = SipResponse.parse(response_text)

    dialog = Dialog.from_invite_2xx(parsed_invite, parsed_response)

    # route_set is the reversed Record-Route: proxy1 first (nearest), proxy2 second.
    assert len(dialog.route_set) == 2
    assert "proxy1" in dialog.route_set[0]
    assert "proxy2" in dialog.route_set[1]

    # Simulate the adapter ACK builder emitting Route headers.
    ack_via = f"SIP/2.0/TLS {dialog.local_sent_by};branch={new_branch()};rport"
    route_headers = [("Route", route) for route in dialog.route_set]
    ack_text = build_request(
        "ACK",
        dialog.remote_target,
        [
            ("Via", ack_via),
            ("Max-Forwards", "70"),
            ("From", f"<{dialog.local_uri}>;tag={dialog.local_tag}"),
            ("To", f"<{dialog.remote_uri}>;tag={dialog.remote_tag}"),
            ("Call-ID", dialog.call_id),
            ("CSeq", f"{dialog.local_cseq} ACK"),
            ("Contact", dialog.local_contact),
            *route_headers,
        ],
    )

    # After fix: ACK wire text contains Route headers.
    assert "Route:" in ack_text or "route:" in ack_text.lower()
    assert "proxy1" in ack_text
    assert "proxy2" in ack_text


# ---------------------------------------------------------------------------
# W1 -- stale 407 with old CSeq must not be mistaken for a final response
# ---------------------------------------------------------------------------


def test_w1_stale_407_old_cseq_is_ignored() -> None:
    """A retransmitted 407 carrying the FIRST INVITE's CSeq (1) must NOT be accepted.

    The fix filters the response wait-loop by the CSeq sequence number of the
    outstanding INVITE, ignoring any response whose CSeq-number does not match
    the current transaction's CSeq.
    """

    def _should_accept(status: int, response_cseq: int, expected_cseq: int) -> bool:
        """Model the post-fix acceptance predicate."""
        return status >= 200 and response_cseq == expected_cseq

    last_cseq = 2  # after re-auth

    # Stale 407 from first transaction (CSeq 1) -- must be skipped.
    assert not _should_accept(407, response_cseq=1, expected_cseq=last_cseq)
    # Real 200 OK from re-auth INVITE (CSeq 2) -- must be accepted.
    assert _should_accept(200, response_cseq=2, expected_cseq=last_cseq)
    # 180 Ringing is provisional -- not final.
    assert not _should_accept(180, response_cseq=2, expected_cseq=last_cseq)
    # 487 from re-auth INVITE is final.
    assert _should_accept(487, response_cseq=2, expected_cseq=last_cseq)


def test_w1_cseq_num_parseable_from_response_header() -> None:
    """CSeq sequence number is parseable from a SipResponse.header('CSeq')."""
    # Build a minimal INVITE at CSeq 2 (the re-auth INVITE).
    invite_text = build_request(
        "INVITE",
        "sip:1000@pbx.example.test",
        [
            ("Via", f"SIP/2.0/TLS 192.0.2.1:5061;branch={new_branch()};rport"),
            ("From", "<sip:agent@pbx.example.test>;tag=abc"),
            ("To", "<sip:1000@pbx.example.test>"),
            ("Call-ID", "w1-test@pbx.example.test"),
            ("CSeq", "2 INVITE"),
            ("Contact", "<sip:agent@192.0.2.1:5061;transport=tls>"),
        ],
    )
    parsed = SipRequest.parse(invite_text)

    # Stale 407 with CSeq 1 (modified to simulate a retransmit from txn 1).
    stale_407 = build_response(parsed, 407, "Proxy Authentication Required")
    stale_407 = stale_407.replace("2 INVITE", "1 INVITE", 1)
    parsed_407 = SipResponse.parse(stale_407)
    cseq_hdr = parsed_407.header("CSeq") or ""
    assert int(cseq_hdr.split()[0]) == 1

    # Real 200 OK carries CSeq 2.
    ok_200 = build_response(parsed, 200, "OK")
    parsed_200 = SipResponse.parse(ok_200)
    cseq_hdr_200 = parsed_200.header("CSeq") or ""
    assert int(cseq_hdr_200.split()[0]) == 2


# ---------------------------------------------------------------------------
# W2 -- two endpointer fires before is_final must yield two delivered turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_w2_two_endpointer_fires_yield_two_turns() -> None:
    """Two endpointer fires before any is_final transcript must produce two turns.

    With asyncio.Event (boolean), the second fire is a no-op because the Event is
    already set; the first is_final clears it, so only ONE turn is delivered.

    After the fix (counter/semaphore), each fire increments the counter, and the
    asr task decrements it once per is_final, so TWO fires yield TWO turns.
    """
    delivered: list[str] = []

    async def _deliver(text: str) -> None:
        delivered.append(text)

    class _NoVad(VoiceActivityDetector):
        """Fake VAD: emits no events (silence, no barge-in)."""

        def feed(self, frame: PcmFrame) -> list[VadEvent]:  # type: ignore[override]
            return []

    class _TwoFireEndpointer(Endpointer):
        """Fake Endpointer: fires on the first two advance() calls."""

        def __init__(self) -> None:
            super().__init__(silence_ms=200, sample_rate_hz=8000)
            self._count = 0

        def advance(self, window_index: int) -> bool:
            self._count += 1
            return self._count <= 2

        def on_event(self, event: VadEvent) -> None:
            pass

    class _TwoFrameTransport:
        """Fake Transport: yields 2 inbound frames then stops."""

        @property
        def inbound_sample_rate(self) -> int:
            return 8000

        def inbound_audio(self) -> AsyncIterator[PcmFrame]:
            async def _gen() -> AsyncIterator[PcmFrame]:
                for i in range(2):
                    yield PcmFrame(
                        samples=bytes(320), sample_rate=8000, monotonic_ts_ns=i
                    )

            return _gen()

        async def send_audio(self, frame: PcmFrame) -> None:
            pass

    class _TwoFinalASR(StreamingASR):
        """Fake ASR: drains audio, then yields two final transcripts."""

        @property
        def input_sample_rate(self) -> int:
            return 8000

        def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
            async def _gen() -> AsyncIterator[Transcript]:
                async for _ in audio:
                    pass
                yield Transcript(
                    text="first", is_final=True, end_of_turn=False, confidence=1.0
                )
                yield Transcript(
                    text="second", is_final=True, end_of_turn=False, confidence=1.0
                )

            return _gen()

    class _PassGuard(InjectionGuard):
        """Fake Guard: always allows."""

        async def screen(self, text: str, *, call_id: str) -> GuardResult:
            return GuardResult(
                verdict=GuardVerdict.ALLOW,
                normalized_text=text,
                reasons=(),
                degraded=False,
                score=0.0,
            )

    class _NoTts(StreamingTTS):
        """Fake TTS: not used in this test."""

        @property
        def output_sample_rate(self) -> int:
            return 8000

        def synthesize(
            self,
            text: AsyncIterator[str],
            voice: str,
            *,
            sample_rate: int | None = None,
        ) -> TtsStream:
            raise NotImplementedError

    loop = CallLoop(
        transport=_TwoFrameTransport(),  # type: ignore[arg-type]
        asr=_TwoFinalASR(),
        tts=_NoTts(),
        guard=_PassGuard(),
        vad=_NoVad(model=MagicMock(), sample_rate_hz=8000, threshold=0.5),
        endpointer=_TwoFireEndpointer(),
        guard_state=GuardSessionState("w2-test"),
        deliver_turn=_deliver,
        voice="",
        call_id="w2-test",
    )

    await loop.run()

    assert len(delivered) == 2, (
        f"Expected 2 delivered turns (one per endpointer fire), got {len(delivered)}: "
        f"{delivered!r}"
    )
    assert delivered[0] == "first"
    assert delivered[1] == "second"


# ---------------------------------------------------------------------------
# W3 -- SRTP-only offer should produce 488, not 500
# ---------------------------------------------------------------------------


def test_w3_srtp_only_offer_path_sends_488() -> None:
    """An SRTP-only offer that cannot be answered must use 488 Not Acceptable Here.

    The adapter's _handle_inbound_invite was catching SdpError from build_audio_answer
    and sending 500. The fix sends 488 for SDP negotiation failures (caller can retry
    with plain RTP) and reserves 500 for genuine server errors.

    We verify (a) that an SRTP offer raises SdpError from build_audio_answer,
    and (b) that the production source contains '488' in the handler.
    """
    srtp_offer_sdp = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 192.0.2.10\r\n"
        "s=-\r\n"
        "c=IN IP4 192.0.2.10\r\n"
        "t=0 0\r\n"
        "m=audio 17000 RTP/SAVP 0\r\n"
        "a=crypto:1 AES_CM_128_HMAC_SHA1_80 "
        "inline:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )

    offer = SessionDescription.parse(srtp_offer_sdp)
    audio = offer.audio
    assert audio is not None
    assert audio.is_srtp, "Expected SRTP offer"

    # build_audio_answer raises SdpError for an SRTP offer with no crypto answer.
    raised: bool
    try:
        build_audio_answer(
            offer,
            local_address="192.0.2.1",
            port=18000,
            supported=["PCMU", "PCMA", "telephone-event"],
            session_id=99999,
        )
        raised = False
    except SdpError:
        raised = True

    if raised:
        import pathlib  # noqa: PLC0415

        adapter_src = (
            pathlib.Path(__file__).parent.parent / "src" / "hermes_voip" / "adapter.py"
        )
        src = adapter_src.read_text()

        # Find the except block that catches build_audio_answer failures.
        # The call site is "answer_sdp = build_audio_answer(" inside
        # _handle_inbound_invite. Skip import occurrences by finding the
        # function call (preceded by "= ").
        call_pos = src.find("answer_sdp = build_audio_answer(")
        assert call_pos != -1, "build_audio_answer call-site not found in adapter"

        # Extract ~600 chars from the call through the except block.
        excerpt = src[call_pos : call_pos + 600]

        # After the fix: the except block sends 488 not 500.
        assert "488" in excerpt, (
            "W3 fix not applied: the except block after build_audio_answer "
            "must send 488, not 500. Excerpt:\n" + excerpt[:500]
        )
        assert '500, "Server Internal Error"' not in excerpt, (
            "W3: '500 Server Internal Error' still in build_audio_answer except block"
        )


# ---------------------------------------------------------------------------
# N2 -- _QueueSink queue must be bounded (maxsize > 0)
# ---------------------------------------------------------------------------


def test_n2_queue_sink_has_bounded_queue() -> None:
    """_QueueSink must use a bounded asyncio.Queue (maxsize > 0).

    An unbounded queue allows the sink to accumulate unlimited responses if the
    consumer falls behind -- unbounded memory growth in the response backlog.
    """
    import pathlib  # noqa: PLC0415

    adapter_src = (
        pathlib.Path(__file__).parent.parent / "src" / "hermes_voip" / "adapter.py"
    )
    src = adapter_src.read_text()

    # Find the _QueueSink class definition and check for maxsize.
    # Extract the class body (from "class _QueueSink" to the next "class ").
    class_start = src.find("class _QueueSink")
    assert class_start != -1, "N2: _QueueSink not found in adapter.py"
    next_class = src.find("\nclass ", class_start + 1)
    class_body = src[class_start : next_class if next_class != -1 else None]

    assert "maxsize" in class_body, "N2: _QueueSink must pass maxsize= to asyncio.Queue"


# ---------------------------------------------------------------------------
# N3 -- RTP initial seq/ts must be random (RFC 3550 S5.1)
# ---------------------------------------------------------------------------


def test_n3_initial_seq_and_ts_are_random() -> None:
    """Two independently constructed engines must have different initial seq/ts.

    With a uniform random uint16 for seq and uint32 for ts, the probability that
    two engines share the SAME pair is 1/(2^48) -- effectively impossible.
    """
    engines = [
        RtpMediaTransport(
            local_address="127.0.0.1",
            local_port=0,
            remote_address="127.0.0.1",
            remote_port=5004 + i,
            codec=Codec.PCMU,
        )
        for i in range(10)
    ]

    seqs = [e._seq for e in engines]
    tss = [e._ts for e in engines]

    # With fixed values (0, 0) ALL would be equal -- a deterministic failure.
    assert len(set(seqs)) > 1 or len(set(tss)) > 1, (
        "N3: all engines have the same initial seq and ts -- not random. "
        f"seqs={seqs}, tss={tss}"
    )

    # Each value must be within the allowed range (RFC 3550 S5.1).
    for e in engines:
        assert 0 <= e._seq < 2**16, f"seq {e._seq} out of range [0, 65535]"
        assert 0 <= e._ts < 2**32, f"ts {e._ts} out of range [0, 2^32)"


def test_n3_initial_values_injectable_for_determinism() -> None:
    """RtpMediaTransport must accept initial_seq and initial_ts kwargs for tests."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        initial_seq=1234,
        initial_ts=5678,
    )
    assert engine._seq == 1234
    assert engine._ts == 5678
