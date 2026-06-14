"""The SIP/WebRTC signalling + RTP/SRTP media seam (ADR-0004; impl in ADR-0005)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from hermes_voip.providers.audio import PcmFrame


@runtime_checkable
class MediaTransport(Protocol):
    """The SIP/WebRTC signalling + RTP/SRTP media boundary.

    Hides G.711 codec, RTP packetisation, jitter buffering, and DTMF (RFC 4733,
    ADR-0010). Above this line everything is PCM16 frames. This is the single
    canonical media seam: exactly one media interface name (``MediaTransport``)
    with ``inbound_audio()`` / ``send_audio()`` and an ``inbound_sample_rate``
    property. ADR-0005 implements this exact Protocol for its in-process engine.
    """

    async def connect(self) -> bool:
        """Register the extension and establish signalling. Returns success."""
        ...

    async def disconnect(self) -> None:
        """Tear down media and signalling; idempotent."""
        ...

    def inbound_audio(self) -> AsyncIterator[PcmFrame]:
        """Far-end (caller) audio decoded to PCM16 at ``inbound_sample_rate``."""
        ...

    async def send_audio(self, frame: PcmFrame) -> None:
        """Encode + packetise one near-end (agent) frame to the gateway."""
        ...

    @property
    def inbound_sample_rate(self) -> int:
        """Declared rate of frames yielded by ``inbound_audio()`` (e.g. 8000)."""
        ...
