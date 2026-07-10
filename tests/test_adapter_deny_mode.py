"""HERMES_VOIP_DENY_MODE=decline polite-decline path (ADR-0020 §5/§6 Phase 2).

Phase 1 always rejects a declined-group caller with a hard ``603 Decline`` in the
pre-200-OK window. Phase 2 adds the ``decline`` variant: instead of the hard 603,
the call is ANSWERED (``200 OK``), one short TTS line is synthesised and played to
the caller over the (real, existing) media path, then the call is torn down with a
``BYE``. This trains a spammer less than a hard 603 (no "this number is blocked"
signal) while still keeping the agent off the call.

These drive the REAL ``VoipAdapter`` at its inbound INVITE seam, mirroring
``test_adapter_caller_modes.py`` (the ``hermes`` extra + the hermes-contract CI
job). Fakes only — ``pbx.example.test`` / ext ``1000`` (PUBLIC-repo invariant).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("gateway.platforms.base")
pytest.importorskip("gateway.config")

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_voip.call import CallMedia, CallSession
from hermes_voip.caller_modes import (
    CallerMode,
    CallerModeConfig,
    Normalization,
)
from hermes_voip.config import ExtensionConfig
from hermes_voip.dialog import Dialog
from hermes_voip.digest import DigestCredentials
from hermes_voip.incall import LocalMediaSession
from hermes_voip.manager import NewCall
from hermes_voip.message import SipRequest, new_call_id, new_tag
from hermes_voip.providers.asr import Transcript
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.providers.build import Providers
from hermes_voip.providers.guard import GuardResult, GuardVerdict
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.sdp import Codec
from hermes_voip.spoken_text import sanitize_for_speech

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from hermes_voip.adapter import VoipAdapter
    from hermes_voip.providers.tts import TtsStream


@pytest.fixture(autouse=True)
def _register_voip_platform() -> None:
    """Register a throwaway "voip" entry so ``Platform("voip")`` resolves."""
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


# --- fakes (mirror test_adapter_caller_modes.py, kept local) -----------------


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
        self._calls.pop(call_id, None)


class _FakeManager:
    def __init__(self, *, is_up: bool = True) -> None:
        self._is_up = is_up
        self._calls: dict[tuple[str, str, str], object] = {}

    @property
    def is_up(self) -> bool:
        return self._is_up

    async def connect(self, *, timeout: float = 10.0) -> bool:
        return self._is_up

    async def aclose(self) -> None: ...

    def add_call(self, dialog_id: tuple[str, str, str], consumer: object) -> None:
        self._calls[dialog_id] = consumer

    def remove_call(self, dialog_id: tuple[str, str, str]) -> None:
        self._calls.pop(dialog_id, None)


class _FakeTtsStream:
    """A genuine ``AsyncIterator[PcmFrame]`` that records its text on the awaited path.

    Implements the ``TtsStream`` protocol structurally — ``__aiter__`` returns ``self``
    and ``__anext__`` is defined — so no ``# type: ignore[return-value]`` is needed
    when ``_RecordingTTS.synthesize`` returns it (must-fix 1).

    The text iterator handed to ``synthesize`` is drained on the FIRST ``__anext__``
    (the awaited iteration the adapter performs via ``async for frame in stream``), and
    the joined phrase is appended to the shared ``phrases`` list. That replaces the
    previous fire-and-forget ``asyncio.ensure_future`` recorder, whose exceptions were
    swallowed (must-fix 2): the drain now runs on the consumer's own awaited task, so a
    failure propagates through the ``async for`` instead of vanishing into an orphan
    task. Exactly one PCM frame is then emitted so the phrase actually plays out to
    ``engine.send_audio`` before ``StopAsyncIteration``.
    """

    def __init__(self, text: AsyncIterator[str], phrases: list[str]) -> None:
        self._text = text
        self._phrases = phrases
        self._emitted = False

    def __aiter__(self) -> _FakeTtsStream:
        return self

    async def __anext__(self) -> PcmFrame:
        if self._emitted:
            raise StopAsyncIteration
        # Drain + record the text on this awaited pull (errors propagate to the caller).
        parts = [chunk async for chunk in self._text]
        self._phrases.append("".join(parts))
        self._emitted = True
        return PcmFrame(samples=b"\x00\x00", sample_rate=8000, monotonic_ts_ns=0)

    async def flush(self) -> None: ...

    async def cancel(self) -> None: ...

    async def aclose(self) -> None: ...


class _RecordingTTS:
    """Records the joined text AND the voice passed to ``synthesize`` for assertions.

    Typed to satisfy ``StreamingTTS`` structurally (``output_sample_rate`` +
    ``synthesize``) so ``Providers(...)`` needs no ``# type: ignore`` (must-fix 1).
    """

    def __init__(self) -> None:
        self.phrases: list[str] = []
        self.voices: list[str] = []

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
        # Record the per-call voice synchronously (must-fix 5: assert the configured
        # voice reaches synthesize). The text is recorded by the returned stream as it
        # is iterated — no orphan task, no swallowed errors (must-fix 2).
        self.voices.append(voice)
        return _FakeTtsStream(text, self.phrases)


class _FakeASR:
    """StreamingASR fake: never iterated here (a declined caller builds no CallLoop).

    Typed to satisfy ``StreamingASR`` structurally (``input_sample_rate`` +
    ``stream`` returning ``AsyncIterator[Transcript]``) so ``Providers(...)`` needs no
    ``# type: ignore`` escape hatch (must-fix 1).
    """

    @property
    def input_sample_rate(self) -> int:
        return 16_000

    def stream(self, audio: AsyncIterator[PcmFrame]) -> AsyncIterator[Transcript]:
        async def _gen() -> AsyncIterator[Transcript]:
            async for _ in audio:
                pass
            empty: tuple[Transcript, ...] = ()
            for transcript in empty:  # always empty — gives the async-gen its shape
                yield transcript

        return _gen()


class _FakeGuard:
    async def screen(self, text: str, *, call_id: str) -> GuardResult:
        return GuardResult(
            verdict=GuardVerdict.ALLOW,
            score=0.0,
            degraded=False,
            normalized_text=text,
            reasons=(),
        )


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


def _make_invite(*, caller: str, call_id: str) -> str:
    ftag = new_tag()
    content_length = len(_FAKE_SDP_OFFER.encode("utf-8"))
    return (
        f"INVITE sip:1000@pbx.example.test SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS 127.0.0.1:5061;branch=z9hG4bKfake\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:{caller}@pbx.example.test>;tag={ftag}\r\n"
        f"To: <sip:1000@pbx.example.test>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:{caller}@127.0.0.1:60000;transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n"
        f"{_FAKE_SDP_OFFER}"
    )


def _deny_caller(caller: str) -> CallerModeConfig:
    return CallerModeConfig(
        allow=(),
        deny=(caller,),
        grey=(),
        default_mode=CallerMode.GREY,
        normalization=Normalization.E164,
    )


async def _build_adapter(  # noqa: PLR0913 — keyword-only test wiring: transport + manager + caller_modes + tts + the deny-mode knobs + the configured voice are all real dependencies
    transport: _FakeTransport,
    manager: _FakeManager,
    *,
    caller_modes: CallerModeConfig,
    tts: _RecordingTTS,
    deny_mode: str,
    decline_phrase: str,
    tts_voice: str | None = None,
) -> VoipAdapter:
    from hermes_voip.adapter import VoipAdapter  # noqa: PLC0415

    config = PlatformConfig(enabled=True, extra=dict(_FAKE_ENV))
    # The fakes satisfy the ASR/TTS/Guard Protocols structurally; Providers accepts them
    # via its keyword constructor (no type: ignore needed — the params are typed to the
    # Protocols, which these duck-type).
    providers = Providers(asr=_FakeASR(), tts=tts, guard=_FakeGuard())
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
                max_calls=8,
                shutdown_drain_secs=5.0,
                deny_mode=deny_mode,
            ),
        ),
        patch(
            "hermes_voip.adapter.load_media_config",
            return_value=MagicMock(
                require_secure_media=False,
                session_expires=600,
                min_se=90,
                decline_phrase=decline_phrase,
                # The configured TTS voice the decline synthesis must use (must-fix 5);
                # an explicit value (not a MagicMock attr) so `tts_voice or ""` resolves
                # to a real str the recorder can assert.
                tts_voice=tts_voice,
            ),
        ),
        patch("hermes_voip.adapter.load_caller_modes", return_value=caller_modes),
        patch("hermes_voip.adapter.build_providers", return_value=providers),
        patch("hermes_voip.adapter._make_tls_context", return_value=MagicMock()),
        patch("hermes_voip.adapter.SipOverTlsTransport", return_value=transport),
        patch("hermes_voip.adapter.RegistrationManager", return_value=manager),
    ):
        adapter = VoipAdapter(config)
        await adapter.connect()
        return adapter


def _real_call_session(
    *,
    signaling: _FakeTransport,
    media: CallMedia,
    call_id: str,
    caller: str,
) -> CallSession:
    """A REAL :class:`CallSession` over the adapter's transport (must-fix 4).

    The decline test must exercise the ACTUAL teardown path, not a hand-rolled mock BYE.
    The adapter builds the session with ``signaling=transport`` and ``media=engine``
    (see ``adapter._handle_inbound_invite``); this mirrors that wiring with a REAL
    ``CallSession``, so ``CallSession.hang_up`` builds a genuine in-dialog ``BYE`` via
    ``build_in_dialog_request`` and sends it on the SAME transport the ``200 OK`` went
    out on. The test then asserts the real BYE wire text (a parseable ``BYE`` request
    with a proper ``CSeq: <n> BYE``), proving the real BYE-producing call ran — not
    that the adapter merely invoked a mock.

    ``media`` is the adapter's engine mock; ``hang_up`` only calls ``media.stop()``.
    Fakes only — ``pbx.example.test`` / ext ``1000`` (PUBLIC-repo invariant).
    """
    dialog = Dialog(
        call_id=call_id,
        local_uri="sip:1000@pbx.example.test",
        local_tag="ours",
        remote_uri=f"sip:{caller}@pbx.example.test",
        remote_tag="theirs",
        remote_target=f"sip:{caller}@198.51.100.99:5061;transport=tls",
        route_set=(),
        local_contact="<sip:1000@198.51.100.7:5061;transport=tls>",
        local_sent_by="198.51.100.7:5061",
        transport="TLS",
        local_cseq=1,
        sdp_version=0,
    )
    return CallSession(
        dialog=dialog,
        signaling=signaling,
        media=media,
        guard=GuardSessionState(call_id=call_id),
        local_media=LocalMediaSession(
            local_address="198.51.100.7",
            port=40000,
            codecs=(Codec(payload_type=0, encoding="PCMU", clock_rate=8000),),
            session_id=55555,
        ),
        credentials=DigestCredentials("1000", "fake"),
    )


@pytest.mark.asyncio
async def test_deny_mode_decline_answers_200_speaks_then_byes() -> None:
    """Decline mode: answer 200 (NOT 603), speak one phrase, then a REAL BYE — no agent.

    The caller "9999" is on the deny list, and ``deny_mode=decline``. Instead of the
    Phase-1 hard ``603 Decline``, the adapter answers the call (a ``200 OK`` reaches
    the transport), synthesises the configured decline phrase through the EXISTING TTS
    provider IN THE CONFIGURED VOICE, plays it, then ends the dialog with a ``BYE``. No
    CallLoop is built (the agent never sees the caller).

    A REAL :class:`CallSession` drives the teardown (must-fix 4): the BYE asserted on
    the wire is the genuine in-dialog request the real ``hang_up`` builds and sends —
    not a mock-synthesised line. The configured ``tts_voice`` is asserted to reach
    ``synthesize`` (must-fix 5).
    """
    transport = _FakeTransport()
    manager = _FakeManager(is_up=True)
    tts = _RecordingTTS()
    phrase = "Sorry, I cannot take this call"
    voice = "operator-voice-7"
    adapter = await _build_adapter(
        transport,
        manager,
        caller_modes=_deny_caller("9999"),
        tts=tts,
        deny_mode="decline",
        decline_phrase=phrase,
        tts_voice=voice,
    )

    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(caller="9999", call_id=call_id))

    engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        start_rtcp=AsyncMock(return_value=None),
        send_audio=AsyncMock(return_value=None),
        set_hold=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=20002,
        inbound_sample_rate=8000,
    )

    # A REAL CallSession over the adapter's transport: its real hang_up emits a real
    # in-dialog BYE on `transport`, the same seam the 200 OK uses (must-fix 4).
    session = _real_call_session(
        signaling=transport, media=engine, call_id=call_id, caller="9999"
    )

    with (
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
        patch("hermes_voip.adapter.CallSession", return_value=session),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        new_call = NewCall(registration=_ext_config(), invite=invite)
        adapter._on_inbound_invite(new_call)
        for _ in range(60):
            await asyncio.sleep(0)

    # A 200 OK was sent (the call was ANSWERED) and NO 603 Decline.
    assert any("200 OK" in m for m in transport.sent), transport.sent
    assert not any("603 Decline" in m for m in transport.sent), transport.sent
    # A REAL BYE followed the answer: parse the wire text and confirm it is a genuine
    # in-dialog BYE request (method + CSeq), not a hand-rolled mock line. The real
    # CallSession.hang_up built it via build_in_dialog_request, sent on `transport`.
    bye_texts = [
        m for m in transport.sent if m.startswith("BYE") and not m.startswith("SIP/2.0")
    ]
    assert bye_texts, transport.sent
    bye = SipRequest.parse(bye_texts[0])
    assert bye.method == "BYE"
    cseq = bye.header("CSeq") or ""
    assert cseq.endswith("BYE"), cseq
    # The BYE targets this dialog's Call-ID (it is the SAME dialog the 200 OK answered).
    assert bye.header("Call-ID") == call_id
    # Ordering: the answer (200 OK) preceded the BYE.
    answer_idx = next(i for i, m in enumerate(transport.sent) if "200 OK" in m)
    bye_idx = next(
        i
        for i, m in enumerate(transport.sent)
        if m.startswith("BYE") and not m.startswith("SIP/2.0")
    )
    assert answer_idx < bye_idx, transport.sent
    # The real hang_up marked the session ended and stopped media (the real teardown).
    assert session.ended
    engine.stop.assert_awaited()
    # The decline phrase was synthesised through the real TTS provider (non-empty).
    assert tts.phrases, "the decline phrase was never synthesised"
    assert tts.phrases[0].strip()
    assert phrase in tts.phrases[0]
    # The CONFIGURED TTS voice reached synthesize (must-fix 5): not an empty voice.
    assert tts.voices == [voice], tts.voices
    # No CallLoop / CallSession-driven agent path was started for the declined caller.
    assert call_id not in adapter._call_loops


async def _drive_deny_decline(*, decline_phrase: str) -> _RecordingTTS:
    """Drive one denied INVITE through answer -> speak-decline -> BYE (item 1325 tests).

    Builds a decline-mode adapter for caller "9999" and returns the recording TTS whose
    ``.phrases`` hold the synthesised decline line.
    """
    transport = _FakeTransport()
    tts = _RecordingTTS()
    adapter = await _build_adapter(
        transport,
        _FakeManager(is_up=True),
        caller_modes=_deny_caller("9999"),
        tts=tts,
        deny_mode="decline",
        decline_phrase=decline_phrase,
        tts_voice="operator-voice-7",
    )
    call_id = new_call_id()
    invite = SipRequest.parse(_make_invite(caller="9999", call_id=call_id))
    engine = MagicMock(
        connect=AsyncMock(return_value=True),
        stop=AsyncMock(return_value=None),
        start_rtcp=AsyncMock(return_value=None),
        send_audio=AsyncMock(return_value=None),
        set_hold=AsyncMock(return_value=None),
        _rtcp_active=False,
        local_port=20002,
        inbound_sample_rate=8000,
    )
    session = _real_call_session(
        signaling=transport, media=engine, call_id=call_id, caller="9999"
    )
    with (
        patch("hermes_voip.adapter.RtpMediaTransport", return_value=engine),
        patch("hermes_voip.adapter.CallSession", return_value=session),
        patch("hermes_voip.adapter._make_vad", return_value=MagicMock()),
        patch("hermes_voip.adapter._make_endpointer", return_value=MagicMock()),
    ):
        adapter._on_inbound_invite(NewCall(registration=_ext_config(), invite=invite))
        for _ in range(60):
            await asyncio.sleep(0)
    return tts


@pytest.mark.asyncio
async def test_deny_mode_decline_sanitizes_custom_phrase_before_tts() -> None:
    """A custom decline phrase (markdown / URL) is sanitised before TTS (item 1325).

    Deny-mode 'decline' speaks the operator-authored HERMES_VOIP_DECLINE_PHRASE. Like
    every other spoken line (agent text goes through _sanitize_iter), it must first pass
    through sanitize_for_speech, or raw markdown / URLs / emoji are voiced verbatim.
    """
    phrase = "Sorry, please visit https://example.test for **help**."
    tts = await _drive_deny_decline(decline_phrase=phrase)

    assert tts.phrases, "the decline phrase was never synthesised"
    # The SANITISED phrase reached TTS, not the raw markdown/URL (item 1325): the bare
    # URL is dropped and the ** markers stripped by sanitize_for_speech.
    assert tts.phrases[0] == sanitize_for_speech(phrase)
    assert "https://example.test" not in tts.phrases[0]
    assert "**" not in tts.phrases[0]


@pytest.mark.asyncio
async def test_deny_mode_decline_empty_sanitised_phrase_falls_back_to_raw() -> None:
    """A phrase that sanitises to empty falls back to the raw phrase, never dead air."""
    phrase = "https://example.test"  # a bare URL: sanitize_for_speech drops it -> ""
    assert sanitize_for_speech(phrase) == "", "precondition: phrase sanitises to empty"
    tts = await _drive_deny_decline(decline_phrase=phrase)

    # Not dead air: the raw phrase was synthesised (the pre-1325 fallback behaviour).
    assert tts.phrases
    assert tts.phrases[0] == phrase
