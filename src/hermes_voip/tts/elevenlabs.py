"""ElevenLabs Flash v2.5 cloud-fallback streaming TTS (opt-in, ADR-0007).

An opt-in cloud fallback behind the same ``StreamingTTS`` seam (ADR-0004),
selected at runtime via ``ELEVENLABS_API_KEY`` (the value lives only in the
gitignored ``.env`` / 1Password; never committed — rules 34/41). It streams the
Flash v2.5 model's **raw PCM16 @ 8 kHz** (``output_format=pcm_8000``) — the
telephony-native rate — so the provider emits the canonical ``PcmFrame`` currency
already at the G.711 wire rate; the media layer then only G.711-encodes it, with
**no resample at all** (ADR-0005/0017).

Why 8 kHz, not 24 kHz (ADR-0007 amendment): ElevenLabs streams audio as many
small chunked-HTTP reads. Requesting ``pcm_24000`` forced a per-stream 24->8 kHz
downsample of 3x the bytes and amplified the outbound re-framing path's
per-chunk-boundary artefacts — a contributor to the live "very choppy" defect.
ElevenLabs can emit ``pcm_8000`` directly, so we ask for the wire rate: zero
lossy 3:1 resample, 3x less ElevenLabs->box bandwidth, and lower first-audio
latency. The codec stays the media layer's job (ADR-0004), so we request PCM16
(not ``ulaw_8000``) and let :class:`~hermes_voip.media.engine.RtpMediaTransport`
do the G.711 encode.

The requested rate is **not** an unconditional 8 kHz pin: it is the G.711 *case*
of a codec→rate mapping. ``output_sample_rate`` (default
:data:`G711_NARROWBAND_RATE`) is 8 kHz because the SDP codec menu the plugin
advertises today (``adapter._SUPPORTED_ENCODINGS``) is G.711-only, so the
negotiated wire is always 8 kHz. When the wideband lane (ADR-0005: prefer
Opus/G.722, negotiate by capability) lands, the negotiated codec's rate is passed
in instead (G.722→16000, Opus→48000 — see :func:`elevenlabs_pcm_format`), so the
TTS rate FOLLOWS the codec rather than throwing wideband away by downsampling.

The HTTP transport is behind the :class:`HttpByteStream` seam and
**dependency-injected**, so tests drive a recorded PCM response with no network.
The real transport (:class:`_UrllibHttp`) streams the chunked HTTP body on a
worker thread bridged to the loop; ``cancel()`` (barge-in) tears the byte stream
down, which closes the connection.
"""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from hermes_voip.aio import stream_from_thread
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.tts._stream import PcmFrameStream, SegmentSource

__all__ = [
    "DEFAULT_VOICE_SETTINGS",
    "ELEVENLABS_PCM_FORMATS",
    "ELEVENLABS_SAMPLE_RATE",
    "FLASH_V2_5_MODEL_ID",
    "MAX_STREAMING_LATENCY",
    "ElevenLabsRequest",
    "ElevenLabsTTS",
    "ElevenLabsVoiceSettings",
    "HttpByteStream",
    "HttpCancellation",
    "elevenlabs_pcm_format",
]

#: The G.711 narrowband wire rate (PCMU/PCMA are 8000 Hz, RFC 3551). The default
#: requested rate, because the SDP codec menu this plugin advertises today is
#: G.711-only (``adapter._SUPPORTED_ENCODINGS``), so the negotiated wire is always
#: 8 kHz. When the wideband lane (ADR-0005: prefer Opus/G.722, negotiate by
#: capability) lands, the requested rate FOLLOWS the negotiated codec (G.722→16k,
#: Opus→48k) — pass that rate into :class:`ElevenLabsTTS` rather than re-pinning
#: 8 kHz here. This is NOT a second hardcoded narrowband pin: it is the G.711
#: *case* of a codec→rate mapping, defaulted because G.711 is the only lane today.
G711_NARROWBAND_RATE = 8_000

#: We request raw PCM16 at the telephony wire rate so the provider emits
#: ``PcmFrame``s already at that rate and the media layer encodes with NO resample
#: (ADR-0005/0017) — the ADR-0007 amendment that fixes the live "very choppy"
#: audio (``pcm_24000`` forced a lossy 3:1 downsample of 3x the bytes per streamed
#: chunk). We keep PCM16 (not the native ``ulaw_8000``) because the codec is the
#: media layer's job (ADR-0004). Default 8 kHz = G.711 (:data:`G711_NARROWBAND_RATE`).
ELEVENLABS_SAMPLE_RATE = G711_NARROWBAND_RATE

#: ElevenLabs' supported raw-PCM ``output_format`` values, keyed by sample rate
#: (the API exposes ``pcm_8000``/``pcm_16000``/``pcm_22050``/``pcm_24000``/
#: ``pcm_44100``). 8/16/24 kHz cover the telephony codec lanes we will negotiate
#: (G.711 8k now; G.722 16k and Opus — resampled — under the wideband lane).
ELEVENLABS_PCM_FORMATS: Mapping[int, str] = {
    8_000: "pcm_8000",
    16_000: "pcm_16000",
    22_050: "pcm_22050",
    24_000: "pcm_24000",
    44_100: "pcm_44100",
}


def elevenlabs_pcm_format(sample_rate: int) -> str:
    """Return the ElevenLabs raw-PCM ``output_format`` for ``sample_rate``.

    Maps a telephony wire sample rate to the matching ``pcm_<rate>`` request
    value. Raises ``ValueError`` for a rate ElevenLabs cannot emit natively
    (rather than silently falling back to a resampled rate) — so a future
    wideband codec whose rate is unsupported fails loudly at construction instead
    of degrading audio at runtime.

    Raises:
        ValueError: If ``sample_rate`` is not an ElevenLabs-supported PCM rate.
    """
    fmt = ELEVENLABS_PCM_FORMATS.get(sample_rate)
    if fmt is None:
        supported = ", ".join(str(r) for r in sorted(ELEVENLABS_PCM_FORMATS))
        msg = (
            f"ElevenLabs has no native PCM output for {sample_rate} Hz "
            f"(supported: {supported})"
        )
        raise ValueError(msg)
    return fmt


#: The Flash v2.5 model id — the low-latency tier ADR-0007 names as the fallback,
#: and the default model. Flash v2.5 is the only ElevenLabs model that both streams
#: in real time AND is ElevenLabs-recommended for voice agents (~75 ms model
#: latency): the more *expressive* ``eleven_v3`` tier cannot stream in real time
#: (its multi-context websocket is unavailable; ElevenLabs states it "can't do
#: real-time"), and ``eleven_turbo_v2_5`` is superseded by Flash ("use the Flash
#: models over Turbo in all use cases"). So the dynamism win on the phone path
#: comes from :data:`DEFAULT_VOICE_SETTINGS` (below), not a model swap. The model is
#: still operator-selectable via ``HERMES_VOIP_TTS_MODEL`` for off-path A/B tests.
FLASH_V2_5_MODEL_ID = "eleven_flash_v2_5"

#: The inclusive upper bound of ElevenLabs' ``optimize_streaming_latency`` query
#: parameter (0 = no optimisation … 4 = max, with the text normaliser disabled).
MAX_STREAMING_LATENCY = 4


def _validate_model_id(model_id: str) -> None:
    """Reject a ``model_id`` that is blank or a filesystem path (the 400 foot-gun).

    The live HTTP 400 root cause (ADR-0025): the shared ``HERMES_VOIP_TTS_MODEL``
    env knob is a model **directory** for the self-host ``sherpa-kokoro`` provider
    but the model **id** for ElevenLabs. A deployment that points it at a Kokoro
    directory (e.g. for a self-host A/B) while selecting ``provider=elevenlabs``
    sends that directory string verbatim as ``model_id``; ElevenLabs then returns
    ``400 invalid_uid`` mid-call — which (before this guard) rose out of the call's
    TaskGroup and dropped the call with NO audio. Reproduced live: a valid id
    (``eleven_flash_v2_5``) returns HTTP 200, while ``model_id="/opt/models/kokoro"``
    or ``"kokoro-multi-lang-v1_0"`` returns 400.

    A legitimate ElevenLabs model id (``eleven_flash_v2_5``, ``eleven_multilingual_v2``,
    …) contains no path separator and is non-blank, so this rejects only the
    misconfiguration — failing fast at construction (a startup ``ConfigError``)
    rather than as a per-call 400. The ``HERMES_VOIP_TTS_MODEL`` name is in the
    message so the operator sees exactly which knob to fix.

    Raises:
        ValueError: If ``model_id`` is empty/blank, or contains a forward slash or a
            backslash (i.e. looks like a filesystem path — the Kokoro-dir foot-gun).
    """
    if not model_id.strip():
        msg = (
            "model_id must be a non-empty ElevenLabs model id "
            "(e.g. 'eleven_flash_v2_5'); set HERMES_VOIP_TTS_MODEL to a model id, "
            "not a blank value"
        )
        raise ValueError(msg)
    if "/" in model_id or "\\" in model_id:
        msg = (
            f"model_id {model_id!r} looks like a filesystem path, not an ElevenLabs "
            "model id. HERMES_VOIP_TTS_MODEL is a model DIRECTORY for the "
            "sherpa-kokoro provider but the model ID for ElevenLabs — set it to a "
            "model id such as 'eleven_flash_v2_5' when HERMES_VOIP_TTS_PROVIDER="
            "elevenlabs (a path value causes a live HTTP 400 that drops the call)"
        )
        raise ValueError(msg)


# The inclusive bounds for every ``voice_settings`` float field (ElevenLabs treats
# these as 0.0-1.0; values outside that band are rejected, never clamped).
_MIN_SETTING = 0.0
_MAX_SETTING = 1.0


def _check_unit_interval(name: str, value: float) -> None:
    """Raise ``ValueError`` unless ``value`` is finite and within ``[0.0, 1.0]``.

    Used by :class:`ElevenLabsVoiceSettings` so a misconfigured tuning value fails
    fast at construction (NaN/inf slip past a naive ``lo <= x <= hi`` test, so they
    are rejected explicitly), never silently clamped into range.

    Raises:
        ValueError: If ``value`` is NaN/inf or outside ``[0.0, 1.0]``.
    """
    import math  # noqa: PLC0415 - tiny stdlib finite check, no import-time cost

    if not math.isfinite(value) or not _MIN_SETTING <= value <= _MAX_SETTING:
        msg = (
            f"{name} must be a finite value in "
            f"[{_MIN_SETTING}, {_MAX_SETTING}], got {value!r}"
        )
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ElevenLabsVoiceSettings:
    """The ElevenLabs ``voice_settings`` request-body object (one synthesis voice).

    These four floats/flags are the dynamism controls ElevenLabs applies to a
    voice; sending **no** ``voice_settings`` (the previous behaviour) makes the API
    fall back to its own defaults — notably ``stability=0.5``, which "can result in
    a monotonous voice". Supplying them is how a phone agent gets a livelier
    delivery without changing model. Every float is validated to ``[0.0, 1.0]`` at
    construction (fail-fast, never clamped).

    Attributes:
        stability: 0.0-1.0. The primary dynamism dial — *lower* values give a
            broader emotional range; too low becomes inconsistent between
            generations. ElevenLabs' own default is 0.5 (flat); the shipped default
            here is lower (see :data:`DEFAULT_VOICE_SETTINGS`). Cheapest lever and
            no latency cost.
        similarity_boost: 0.0-1.0. Clarity / similarity to the source voice;
            very high values can over-enunciate or reproduce source artefacts.
        style: 0.0-1.0. Style exaggeration. ``0.0`` is the default and the
            telephony-safe value: any value above 0 makes the model slightly less
            stable and *may add latency*, so raise it only deliberately.
        use_speaker_boost: Boosts similarity to the source speaker (subtle); a
            small latency cost. Default on.
    """

    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True

    def __post_init__(self) -> None:
        """Validate every float field is within ``[0.0, 1.0]`` (fail-fast)."""
        _check_unit_interval("stability", self.stability)
        _check_unit_interval("similarity_boost", self.similarity_boost)
        _check_unit_interval("style", self.style)

    def payload(self) -> dict[str, float | bool]:
        """The JSON object for the request body's ``voice_settings`` field.

        The keys are exactly ElevenLabs' documented field names so the dict drops
        straight into the synthesis request body.
        """
        return {
            "stability": self.stability,
            "similarity_boost": self.similarity_boost,
            "style": self.style,
            "use_speaker_boost": self.use_speaker_boost,
        }


#: The shipped default ``voice_settings`` — a **dynamic-but-stable** conversational
#: starting point, the fix for the operator's "flat voice" report. The single
#: change that matters is ``stability=0.35`` (below ElevenLabs' monotone 0.5
#: default), which broadens the voice's emotional range at no latency cost while
#: staying high enough to remain consistent. ``style`` is kept at 0.0 to protect
#: first-audio latency and stability on the phone path; ``similarity_boost=0.75``
#: and ``use_speaker_boost=True`` keep the API defaults. Every field is overridable
#: per deployment via the ``HERMES_VOIP_TTS_*`` env knobs so the operator can A/B
#: live without a code change.
DEFAULT_VOICE_SETTINGS = ElevenLabsVoiceSettings(
    stability=0.35,
    similarity_boost=0.75,
    style=0.0,
    use_speaker_boost=True,
)

_DEFAULT_BASE_URL = "https://api.elevenlabs.io"


@dataclass(frozen=True, slots=True)
class ElevenLabsRequest:
    """A single streaming-synthesis HTTP request for one segment of text.

    Built by :class:`ElevenLabsTTS` and handed to the :class:`HttpByteStream`
    transport, which performs the POST and yields the response body chunks. The
    ``xi-api-key`` credential rides in ``headers`` for the transport only; it is
    never logged by the provider (see :class:`ElevenLabsTTS`).

    Attributes:
        voice_id: The ElevenLabs voice id (path segment of the stream endpoint).
        model_id: The synthesis model id (``eleven_flash_v2_5``).
        voice_settings: The dynamism controls (stability/style/…) for this voice,
            serialised into the body's ``voice_settings`` object.
        output_format: The requested audio format (``pcm_8000``).
        url: The fully-formed stream endpoint URL (``optimize_streaming_latency``,
            when set, rides here as a query param — never in the body).
        headers: The HTTP request headers (auth + content type).
        text: The segment text to synthesise (the JSON body's ``text``).
    """

    voice_id: str
    model_id: str
    voice_settings: ElevenLabsVoiceSettings
    output_format: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    text: str

    def body(self) -> bytes:
        """The JSON request body bytes (``text`` + ``model_id`` + ``voice_settings``).

        The ``voice_settings`` object is what makes the voice dynamic rather than
        flat: omitting it (the old body) let ElevenLabs apply its monotone
        ``stability=0.5`` default.
        """
        payload: dict[str, object] = {
            "text": self.text,
            "model_id": self.model_id,
            "voice_settings": self.voice_settings.payload(),
        }
        return json.dumps(payload).encode("utf-8")


class HttpCancellation:
    """A thread-safe barge-in handle linking the loop side to the HTTP read.

    The transport opens the connection *on a worker thread* (so the loop never
    blocks on connect/TLS), but a barge-in ``cancel()`` runs on the **loop**
    thread — and a worker parked in ``response.read()`` cannot see a Python flag.
    This handle bridges them: the worker :meth:`arm`s it with the live response's
    ``close`` right after opening, and the loop calls :meth:`close` on barge-in to
    shut the socket so the blocked read returns and the worker is freed. It is
    created on the loop side and passed into ``open`` so ``close`` is wireable as
    ``stream_from_thread``'s ``on_cancel`` before the worker has even connected;
    if ``close`` lands first, the next :meth:`arm` tears the response down at once.
    """

    def __init__(self) -> None:
        """Create an un-armed, not-yet-cancelled barge-in handle."""
        self._lock = threading.Lock()
        self._closer: Callable[[], None] | None = None
        self._cancelled = False

    def arm(self, closer: Callable[[], None]) -> None:
        """Register the live response's close (worker side, after it opens).

        If :meth:`close` already fired (a barge-in that raced ahead of connect),
        ``closer`` is invoked immediately so the just-opened response is torn down
        without waiting for a read that would otherwise block.
        """
        with self._lock:
            if not self._cancelled:
                self._closer = closer
                return
        closer()

    def close(self) -> None:
        """Abort the in-flight read (loop side, on barge-in). Idempotent.

        Closes the armed response/socket so a worker blocked in ``read()`` returns
        promptly; if nothing is armed yet, records the cancellation so the next
        :meth:`arm` closes immediately.
        """
        with self._lock:
            self._cancelled = True
            closer, self._closer = self._closer, None
        if closer is not None:
            closer()


@runtime_checkable
class HttpByteStream(Protocol):
    """The streaming-HTTP transport behind :class:`ElevenLabsTTS`.

    ``open`` performs the request and returns an iterator of raw response-body
    byte chunks (the PCM audio). It must :meth:`HttpCancellation.arm` ``cancel``
    with a handle that closes the live response, so a barge-in (which calls
    ``cancel.close()`` from the loop) tears the connection down and releases a
    worker parked in the body read. Injected so tests replay a recorded body.
    """

    def open(
        self, request: ElevenLabsRequest, cancel: HttpCancellation
    ) -> Iterator[bytes]:
        """POST ``request`` and yield the streamed response-body byte chunks."""
        ...


class ElevenLabsTTS:
    """ElevenLabs streaming TTS (StreamingTTS, ADR-0004); Flash v2.5 by default.

    Emits ``PcmFrame``s at the telephony wire rate (default 8 kHz = G.711, via
    ``output_format=pcm_8000``) so the media layer encodes with no resample. The
    requested rate is :data:`G711_NARROWBAND_RATE` today because the SDP codec
    menu is G.711-only; the wideband lane (ADR-0005) passes the negotiated codec's
    rate via ``output_sample_rate`` instead.

    Dynamism is controlled by ``voice_settings`` (default
    :data:`DEFAULT_VOICE_SETTINGS` — a livelier-than-flat starting point), sent on
    every synthesis request so the voice is not the API's monotone default; the
    model and the optional ``optimize_streaming_latency`` query param are likewise
    configurable. The API key is held privately and never appears in ``repr``
    (secrets are never logged). Inject ``http`` in tests; production uses the
    urllib transport.
    """

    def __init__(  # noqa: PLR0913 — all keyword-only config; output_sample_rate + voice_settings are load-bearing
        self,
        *,
        api_key: str,
        voice: str,
        http: HttpByteStream | None = None,
        model_id: str = FLASH_V2_5_MODEL_ID,
        voice_settings: ElevenLabsVoiceSettings = DEFAULT_VOICE_SETTINGS,
        optimize_streaming_latency: int | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        output_sample_rate: int = G711_NARROWBAND_RATE,
    ) -> None:
        """Create the provider.

        Args:
            api_key: The ElevenLabs credential (``ELEVENLABS_API_KEY``). Required;
                a blank value fails fast. Held privately, never logged.
            voice: The default voice id, used when ``synthesize`` is called with
                an empty ``voice``.
            http: The HTTP byte-stream transport (dependency injection); defaults
                to the real urllib-backed transport.
            model_id: The synthesis model id (defaults to Flash v2.5 — the only
                real-time-streaming, ElevenLabs-recommended voice-agent model).
            voice_settings: The dynamism controls sent in every request body.
                Defaults to :data:`DEFAULT_VOICE_SETTINGS` (dynamic-but-stable), so
                a bare provider is livelier than ElevenLabs' flat default rather
                than emitting no ``voice_settings`` at all.
            optimize_streaming_latency: The ElevenLabs latency query param, an int
                in ``[0, 4]`` or ``None``. ``None`` (the default) sends nothing —
                the param is deprecated and Flash + ``pcm_8000`` already keep
                first-audio latency low; set ``1`` to opt in to mild optimisation.
                ``4`` disables the text normaliser (mispronounces numbers/dates) so
                it is a deliberate, rarely-correct choice for a phone agent.
            base_url: The API base URL (override for testing/self-host proxies).
            output_sample_rate: The wire rate to request from ElevenLabs, in Hz.
                Defaults to :data:`G711_NARROWBAND_RATE` (8 kHz), the rate of the
                only codec lane (G.711) the plugin negotiates today, so the media
                layer encodes with no resample. The wideband lane (ADR-0005) sets
                this to the negotiated codec's rate (e.g. 16000 for G.722). Must be
                a rate ElevenLabs can emit natively (see :func:`elevenlabs_pcm_format`).

        Raises:
            ValueError: If ``api_key`` is empty/blank, ``output_sample_rate`` is not
                an ElevenLabs-supported PCM rate, or ``optimize_streaming_latency``
                is outside ``[0, 4]``.
        """
        if not api_key.strip():
            msg = "api_key must be a non-empty ElevenLabs credential"
            raise ValueError(msg)
        # Reject a path-shaped / blank model id at construction — the live HTTP 400
        # foot-gun (a Kokoro model dir leaking through the shared HERMES_VOIP_TTS_MODEL
        # knob). Fail fast here, not as a per-call 400 that kills the call (ADR-0025).
        _validate_model_id(model_id)
        if optimize_streaming_latency is not None and not (
            0 <= optimize_streaming_latency <= MAX_STREAMING_LATENCY
        ):
            msg = (
                f"optimize_streaming_latency must be in [0, {MAX_STREAMING_LATENCY}] "
                f"or None, got {optimize_streaming_latency!r}"
            )
            raise ValueError(msg)
        # Resolve (and validate) the request format from the wire rate now, so an
        # unsupported rate fails fast at construction, not mid-call.
        self._output_format = elevenlabs_pcm_format(output_sample_rate)
        self._output_sample_rate = output_sample_rate
        self._api_key = api_key
        self._default_voice = voice
        self._model_id = model_id
        self._voice_settings = voice_settings
        self._optimize_streaming_latency = optimize_streaming_latency
        self._base_url = base_url.rstrip("/")
        self._http: HttpByteStream = http if http is not None else _UrllibHttp()

    def __repr__(self) -> str:
        """Repr without the credential (secrets never logged)."""
        return (
            f"{type(self).__name__}(voice={self._default_voice!r}, "
            f"model_id={self._model_id!r}, rate={self._output_sample_rate}, "
            f"stability={self._voice_settings.stability})"
        )

    @property
    def output_sample_rate(self) -> int:
        """The requested wire rate (default 8 kHz G.711 → media layer only encodes)."""
        return self._output_sample_rate

    @property
    def model_id(self) -> str:
        """The configured synthesis model id (default Flash v2.5)."""
        return self._model_id

    @property
    def voice_settings(self) -> ElevenLabsVoiceSettings:
        """The dynamism controls sent on every request (dynamic-but-stable default)."""
        return self._voice_settings

    @property
    def optimize_streaming_latency(self) -> int | None:
        """The opt-in latency query value, or ``None`` when not sent (the default)."""
        return self._optimize_streaming_latency

    def synthesize(
        self,
        text: AsyncIterator[str],
        voice: str,
        *,
        sample_rate: int | None = None,
    ) -> TtsStream:
        """Stream agent text in, stream wire-rate ``PcmFrame``s out (ADR-0004/0022).

        Returns a ``TtsStream`` that segments ``text`` and synthesises each
        sentence via a streaming HTTP request; ``voice`` overrides the
        construction default when non-empty.

        ``sample_rate`` (the negotiated wire rate the call loop passes per call)
        OVERRIDES the construction default for this call: ElevenLabs can emit
        ``pcm_8000``/``pcm_16000``/… natively, so a G.722 call (16 kHz) requests
        ``pcm_16000`` and emits 16 kHz frames — the rate follows the codec, with no
        upsample-from-8k and no downsample. ``None`` uses the construction default
        (the 8 kHz G.711 case, preserving the no-resample choppiness fix). An
        unsupported rate raises :class:`ValueError` here (no silent fallback).
        """
        voice_id = voice or self._default_voice
        # Resolve the per-call rate/format: a per-call override follows the
        # negotiated codec; None keeps the construction default. Validated now (an
        # unsupported rate raises at the synthesize call, not mid-stream).
        out_rate = self._output_sample_rate if sample_rate is None else sample_rate
        out_format = (
            self._output_format
            if sample_rate is None
            else elevenlabs_pcm_format(sample_rate)
        )
        stop = threading.Event()

        def _open(sentence: str) -> SegmentSource:
            # One cancellation handle per segment: the read happens on a worker
            # thread, so a barge-in must close the response from the loop side.
            # ``cancel.close`` is both the segment's barge-in ``abort`` (called by
            # PcmFrameStream.cancel() to unblock a parked read) and on_cancel for
            # stream_from_thread (so teardown joins the worker). Without it the
            # worker stays parked in read() until the join timeout (the bug fixed).
            cancel = HttpCancellation()
            request = self._request(sentence, voice_id, out_format)
            chunks = stream_from_thread(
                lambda: self._http.open(request, cancel),
                on_cancel=cancel.close,
            )
            return SegmentSource(chunks=chunks, abort=cancel.close)

        return PcmFrameStream(
            text=text,
            open_segment=_open,
            sample_rate=out_rate,
            stop=stop,
        )

    def _request(
        self, text: str, voice_id: str, output_format: str
    ) -> ElevenLabsRequest:
        """Form the streaming-synthesis request for one segment of ``text``.

        ``output_format`` and (when configured) ``optimize_streaming_latency`` are
        query params per the ElevenLabs API; ``model_id`` + ``voice_settings`` go in
        the body (set by :meth:`ElevenLabsRequest.body`).
        """
        query = f"output_format={output_format}"
        if self._optimize_streaming_latency is not None:
            query = (
                f"{query}&optimize_streaming_latency={self._optimize_streaming_latency}"
            )
        url = f"{self._base_url}/v1/text-to-speech/{voice_id}/stream?{query}"
        return ElevenLabsRequest(
            voice_id=voice_id,
            model_id=self._model_id,
            voice_settings=self._voice_settings,
            output_format=output_format,
            url=url,
            headers={
                "xi-api-key": self._api_key,
                "Content-Type": "application/json",
                "Accept": "audio/pcm",
            },
            text=text,
        )


class _UrllibHttp:
    """The real streaming-HTTP transport (stdlib ``urllib`` — no extra deps).

    Opens the POST and yields the response body in chunks, closing the response
    when the iterator is closed (on ``cancel()``) or exhausted. Using stdlib
    keeps the default install lean (no httpx/websockets), matching the project's
    minimal-dependency posture.
    """

    _CHUNK_BYTES = 4096

    def open(
        self, request: ElevenLabsRequest, cancel: HttpCancellation
    ) -> Iterator[bytes]:
        """POST ``request`` and stream the response body in chunks.

        Arms ``cancel`` with the live response's ``close`` immediately after
        opening (before the first, blocking ``read``), so a barge-in closes the
        socket from the loop side and the parked read returns promptly.
        """
        import urllib.request  # noqa: PLC0415 - lazy: real transport path only

        req = urllib.request.Request(  # noqa: S310 - fixed https ElevenLabs API URL
            request.url,
            data=request.body(),
            headers=dict(request.headers),
            method="POST",
        )
        response = urllib.request.urlopen(req)  # noqa: S310 - fixed https API URL
        # If a barge-in already fired while connecting, arm() closes it right now.
        cancel.arm(response.close)
        try:
            while True:
                chunk = response.read(self._CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
        finally:
            response.close()


# Structural conformance to the ADR-0004 seam (mypy + runtime_checkable Protocol).
_: type[StreamingTTS] = ElevenLabsTTS
_transport: type[HttpByteStream] = _UrllibHttp
