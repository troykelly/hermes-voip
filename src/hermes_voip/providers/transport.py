"""The SIP/WebRTC signalling + RTP/SRTP media seam (ADR-0004; impl in ADR-0005)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from hermes_voip.providers.audio import PcmFrame

__all__ = ["MediaTransport"]


@runtime_checkable
class MediaTransport(Protocol):
    """The SIP/WebRTC signalling + RTP/SRTP media boundary.

    Hides G.711 codec, RTP packetisation, jitter buffering, and DTMF (RFC 4733,
    ADR-0010). Above this line everything is PCM16 frames. This is the single
    canonical media seam: exactly one media interface name (``MediaTransport``)
    with ``inbound_audio()`` / ``send_audio()``, an ``inbound_sample_rate`` property,
    and an ``on_hold`` hold-state flag. ADR-0005 implements this exact Protocol for its
    in-process engine.
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

    async def flush_outbound(self, *, fade_ms: int) -> None:
        """Drop pending outbound audio with a short fade-out (barge-in clean stop).

        Called the instant a barge-in is authorised: stop the agent's already-queued
        near-end audio within ~1 packet (not after the buffer drains), emitting a
        short linear fade-out (``fade_ms``) on the final frames so the cut does not
        click. ``fade_ms`` of 0 is an instant hard cut (no audio emitted).
        """
        ...

    @property
    def inbound_sample_rate(self) -> int:
        """Declared rate of frames yielded by ``inbound_audio()`` (e.g. 8000)."""
        ...

    @property
    def on_hold(self) -> bool:
        """Whether the call is on hold (media suspended in BOTH directions).

        While held, inbound datagrams are discarded and outbound send/flush is muted, so
        a silence window is not caller silence. The no-input watchdog reads this to skip
        a held window instead of reprompting into or hanging up a held call (agent- or
        peer-initiated hold).
        """
        ...
