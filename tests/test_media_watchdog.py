"""RTP-inactivity watchdog + transport-loss wiring for the media engine (ADR-0026).

The reliability fix behind call-termination: a silent media / network drop used
to HANG a call forever — the inbound generator blocked indefinitely on
``queue.get()`` because nothing ever set the stop event (stop fired only from a
received BYE or from teardown after ``run()`` returned). These tests pin the two
detectors that end such a call:

* a no-media DEADLINE in the inbound generator — when no datagram arrives within
  the configured silence window, the generator ends and the engine records that
  it timed out (so the adapter classifies MEDIA_TIMEOUT → ``/stop``); and
* ``connection_lost`` / ``error_received`` on the UDP protocol — a transport drop
  ends the generator too (and records the timeout flag) instead of being a
  DEBUG-only no-op.

All deterministic: the watchdog's wait is an injected callable a test resolves on
demand (no wall-clock sleeps).
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_voip.media.engine import Codec, RtpMediaTransport
from hermes_voip.providers.audio import PcmFrame
from hermes_voip.rtp import RtpPacket

_FAKE_SRC: tuple[str, int] = ("198.51.100.7", 6000)
_SAMPLES_PER_FRAME = 160
_PCM_SILENCE = b"\x00" * (_SAMPLES_PER_FRAME * 2)


def _ulaw_silence_datagram(seq: int) -> bytes:
    from hermes_voip.media.audio import encode_ulaw  # noqa: PLC0415

    return RtpPacket(
        payload_type=0,
        sequence_number=seq,
        timestamp=seq * _SAMPLES_PER_FRAME,
        ssrc=0xDEADBEEF,
        payload=encode_ulaw(_PCM_SILENCE),
    ).pack()


def _dummy_clock() -> int:
    return 0


def test_media_timed_out_starts_false() -> None:
    """A fresh engine has not timed out (the flag is False until the watchdog fires)."""
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        media_timeout_secs=20.0,
    )
    assert engine.media_timed_out is False


@pytest.mark.asyncio
async def test_silent_inbound_fires_the_watchdog_and_ends_the_stream() -> None:
    """No datagram within the deadline ends the inbound generator (no infinite hang).

    The watchdog's wait is an injected coroutine the test resolves once: when it
    completes, the generator must end cleanly (return, not block) AND the engine
    must record that it timed out so the adapter can classify MEDIA_TIMEOUT.
    """
    fire = asyncio.Event()

    async def _watchdog_sleep(_secs: float) -> None:
        await fire.wait()

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        clock=_dummy_clock,
        media_timeout_secs=20.0,
        watchdog_sleep=_watchdog_sleep,
    )
    await engine.connect()

    frames: list[PcmFrame] = []

    async def _drain() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0)  # let the generator block on the racing get/watchdog
    assert not task.done(), "the generator must block while media could still arrive"

    fire.set()  # the inactivity deadline elapses
    await asyncio.wait_for(task, timeout=2.0)

    assert frames == [], "no media arrived, so no frame should be produced"
    assert engine.media_timed_out is True, (
        "the watchdog firing must record a media timeout so the adapter "
        "classifies MEDIA_TIMEOUT -> /stop"
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_arriving_datagrams_reset_the_deadline() -> None:
    """A datagram resets the inactivity deadline — a live call is never false-killed.

    With the watchdog wait gated on an event the test never sets, datagrams still
    flow normally and the generator yields frames; the watchdog does not fire, so
    ``media_timed_out`` stays False. This is the false-positive guard: continuous
    media keeps re-arming the deadline.
    """
    never = asyncio.Event()

    async def _watchdog_sleep(_secs: float) -> None:
        await never.wait()  # never fires: media is flowing

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        clock=_dummy_clock,
        media_timeout_secs=20.0,
        watchdog_sleep=_watchdog_sleep,
    )
    await engine.connect()

    frames: list[PcmFrame] = []
    done = asyncio.Event()

    async def _drain() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)
            if len(frames) == 3:
                done.set()
                break

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0)
    for seq in range(3):
        engine._recv_queue.put_nowait((_ulaw_silence_datagram(seq), _FAKE_SRC))

    await asyncio.wait_for(done.wait(), timeout=2.0)
    await asyncio.wait_for(task, timeout=2.0)

    assert len(frames) == 3
    assert engine.media_timed_out is False, (
        "media was flowing, so the watchdog must not have fired"
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_watchdog_disabled_when_timeout_is_zero() -> None:
    """``media_timeout_secs=0`` disables the watchdog (no deadline arms).

    With the watchdog off, the injected sleep is never even invoked; the only way
    the generator ends is a received datagram or stop(). This proves the knob's
    off-switch.
    """
    invoked = asyncio.Event()

    async def _watchdog_sleep(_secs: float) -> None:
        invoked.set()
        await asyncio.sleep(0)

    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        clock=_dummy_clock,
        media_timeout_secs=0.0,
        watchdog_sleep=_watchdog_sleep,
    )
    await engine.connect()

    frames: list[PcmFrame] = []

    async def _drain() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0.02)
    assert not invoked.is_set(), "a disabled watchdog must never arm its sleep"
    assert not task.done(), "with no media and no stop, the generator stays blocked"

    await engine.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert engine.media_timed_out is False


@pytest.mark.asyncio
async def test_connection_lost_ends_the_stream_as_a_media_timeout() -> None:
    """A UDP transport drop ends the generator and records the failure flag.

    ``connection_lost`` was a DEBUG-only no-op; it must now set the stop event so
    the inbound generator wakes and ends (instead of hanging), and record the
    media-loss flag so the adapter classifies the end as a failure → ``/stop``.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        clock=_dummy_clock,
        media_timeout_secs=20.0,
    )
    await engine.connect()

    frames: list[PcmFrame] = []

    async def _drain() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0)
    assert not task.done()

    # Simulate the OS reporting the socket closed/errored under the engine.
    engine._on_transport_lost(ConnectionResetError("peer reset"))
    await asyncio.wait_for(task, timeout=2.0)

    assert frames == []
    assert engine.media_timed_out is True
    await engine.stop()


@pytest.mark.asyncio
async def test_error_received_triggers_transport_loss() -> None:
    """A fatal ICMP/socket error (``error_received``) also ends the call as a failure.

    The UDP protocol's ``error_received`` previously only logged; an unreachable
    destination (ICMP port-unreachable surfaced here) must drive the same
    transport-loss path so the call does not hang on a dead socket.
    """
    engine = RtpMediaTransport(
        local_address="127.0.0.1",
        local_port=0,
        remote_address="127.0.0.1",
        remote_port=5004,
        codec=Codec.PCMU,
        clock=_dummy_clock,
        media_timeout_secs=20.0,
    )
    await engine.connect()

    frames: list[PcmFrame] = []

    async def _drain() -> None:
        async for frame in engine.inbound_audio():
            frames.append(frame)

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0)

    # The protocol object the engine handed to asyncio reports a fatal error.
    protocol = engine._protocol
    assert protocol is not None
    protocol.error_received(OSError("connection refused"))
    await asyncio.wait_for(task, timeout=2.0)

    assert engine.media_timed_out is True
    await engine.stop()
