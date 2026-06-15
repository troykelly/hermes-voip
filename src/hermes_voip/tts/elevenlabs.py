"""ElevenLabs Flash v2.5 cloud-fallback streaming TTS (opt-in, ADR-0007).

An opt-in cloud fallback behind the same ``StreamingTTS`` seam (ADR-0004),
selected at runtime via ``ELEVENLABS_API_KEY`` (the value lives only in the
gitignored ``.env`` / 1Password; never committed — rules 34/41). It streams the
Flash v2.5 model's **raw PCM16 @ 24 kHz** (``output_format=pcm_24000``) so the
provider emits the canonical ``PcmFrame`` currency; the 24->8 kHz downsample and
G.711 encode stay the media layer's job (ADR-0005), exactly as for the self-host
default.

The HTTP transport is behind the :class:`HttpByteStream` seam and
**dependency-injected**, so tests drive a recorded PCM response with no network.
The real transport (:class:`_UrllibHttp`) streams the chunked HTTP body on a
worker thread bridged to the loop; ``cancel()`` (barge-in) tears the byte stream
down, which closes the connection.
"""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from hermes_voip.aio import stream_from_thread
from hermes_voip.providers.tts import StreamingTTS, TtsStream
from hermes_voip.tts._stream import PcmFrameStream
from hermes_voip.tts.segment import segment_stream

__all__ = [
    "ELEVENLABS_SAMPLE_RATE",
    "FLASH_V2_5_MODEL_ID",
    "ElevenLabsRequest",
    "ElevenLabsTTS",
    "HttpByteStream",
]

#: We request raw PCM16 @ 24 kHz so the provider emits ``PcmFrame``s in the same
#: currency as the self-host default; the media layer downsamples to 8 kHz for
#: G.711 (ADR-0005). (ElevenLabs can emit native ``ulaw_8000``, but the canonical
#: provider seam is PCM16 — codec is the media layer, ADR-0004.)
ELEVENLABS_SAMPLE_RATE = 24_000

#: The Flash v2.5 model id — the low-latency tier ADR-0007 names as the fallback.
FLASH_V2_5_MODEL_ID = "eleven_flash_v2_5"

_DEFAULT_BASE_URL = "https://api.elevenlabs.io"
_OUTPUT_FORMAT = "pcm_24000"


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
        output_format: The requested audio format (``pcm_24000``).
        url: The fully-formed stream endpoint URL.
        headers: The HTTP request headers (auth + content type).
        text: The segment text to synthesise (the JSON body's ``text``).
    """

    voice_id: str
    model_id: str
    output_format: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    text: str

    def body(self) -> bytes:
        """The JSON request body bytes (``text`` + ``model_id``)."""
        payload = {"text": self.text, "model_id": self.model_id}
        return json.dumps(payload).encode("utf-8")


@runtime_checkable
class HttpByteStream(Protocol):
    """The streaming-HTTP transport behind :class:`ElevenLabsTTS`.

    ``open`` performs the request and returns an iterator of raw response-body
    byte chunks (the PCM audio). Closing the iterator (on ``cancel()``) must tear
    the underlying connection down. Injected so tests replay a recorded body.
    """

    def open(self, request: ElevenLabsRequest) -> Iterator[bytes]:
        """POST ``request`` and yield the streamed response-body byte chunks."""
        ...


class ElevenLabsTTS:
    """ElevenLabs Flash v2.5 streaming TTS (StreamingTTS, ADR-0004).

    Emits 24 kHz ``PcmFrame``s built from the ``pcm_24000`` response stream.
    The API key is held privately and never appears in ``repr`` (secrets are
    never logged). Inject ``http`` in tests; production uses the urllib transport.
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice: str,
        http: HttpByteStream | None = None,
        model_id: str = FLASH_V2_5_MODEL_ID,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        """Create the provider.

        Args:
            api_key: The ElevenLabs credential (``ELEVENLABS_API_KEY``). Required;
                a blank value fails fast. Held privately, never logged.
            voice: The default voice id, used when ``synthesize`` is called with
                an empty ``voice``.
            http: The HTTP byte-stream transport (dependency injection); defaults
                to the real urllib-backed transport.
            model_id: The synthesis model id (defaults to Flash v2.5).
            base_url: The API base URL (override for testing/self-host proxies).

        Raises:
            ValueError: If ``api_key`` is empty/blank.
        """
        if not api_key.strip():
            msg = "api_key must be a non-empty ElevenLabs credential"
            raise ValueError(msg)
        self._api_key = api_key
        self._default_voice = voice
        self._model_id = model_id
        self._base_url = base_url.rstrip("/")
        self._http: HttpByteStream = http if http is not None else _UrllibHttp()

    def __repr__(self) -> str:
        """Repr without the credential (secrets never logged)."""
        return (
            f"{type(self).__name__}(voice={self._default_voice!r}, "
            f"model_id={self._model_id!r})"
        )

    @property
    def output_sample_rate(self) -> int:
        """24 kHz: the media layer downsamples to 8 kHz for G.711 (ADR-0005)."""
        return ELEVENLABS_SAMPLE_RATE

    def synthesize(self, text: AsyncIterator[str], voice: str) -> TtsStream:
        """Stream agent text in, stream 24 kHz ``PcmFrame``s out (ADR-0004).

        Returns a ``TtsStream`` that segments ``text`` and synthesises each
        sentence via a streaming HTTP request; ``voice`` overrides the
        construction default when non-empty.
        """
        voice_id = voice or self._default_voice
        stop = threading.Event()
        return PcmFrameStream(
            segments=segment_stream(text),
            open_segment=lambda sentence: stream_from_thread(
                lambda: self._http.open(self._request(sentence, voice_id))
            ),
            sample_rate=ELEVENLABS_SAMPLE_RATE,
            stop=stop,
        )

    def _request(self, text: str, voice_id: str) -> ElevenLabsRequest:
        """Form the streaming-synthesis request for one segment of ``text``."""
        url = f"{self._base_url}/v1/text-to-speech/{voice_id}/stream"
        url = f"{url}?output_format={_OUTPUT_FORMAT}"
        return ElevenLabsRequest(
            voice_id=voice_id,
            model_id=self._model_id,
            output_format=_OUTPUT_FORMAT,
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

    def open(self, request: ElevenLabsRequest) -> Iterator[bytes]:
        """POST ``request`` and stream the response body in chunks."""
        import urllib.request  # noqa: PLC0415 - lazy: real transport path only

        req = urllib.request.Request(  # noqa: S310 - fixed https ElevenLabs API URL
            request.url,
            data=request.body(),
            headers=dict(request.headers),
            method="POST",
        )
        response = urllib.request.urlopen(req)  # noqa: S310 - fixed https API URL
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
