"""CallLoop DTMF surfacing + armed-confirmation routing (ADR-0010).

The engine demuxes inbound RFC 4733 digits and fires ``on_dtmf``; the adapter routes
that to :meth:`CallLoop.feed_dtmf`. This suite locks the controller-owned routing the
ADR mandates:

* while a confirmation is **armed**, a digit resolves it directly (a control action,
  never laundered into a speech turn — preserving the spoof-resistant property);
* otherwise a buffered digit GROUP (terminated by ``#`` or an inter-digit timeout) is
  delivered to the agent as a clearly-tagged synthetic ``[DTMF] 1234`` turn, never a
  fake transcript through STT/LLM.

These exercise the routing directly on a constructed :class:`CallLoop` (no live engine
needed); the engine→loop wiring is covered in ``test_dtmf_receive.py`` and the adapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from hermes_voip.dtmf_confirm import ArmedConfirmation
from hermes_voip.media.call_loop import CallLoop
from hermes_voip.media.endpoint import Endpointer
from hermes_voip.media.vad import VoiceActivityDetector
from hermes_voip.providers.policy import GuardSessionState

# Reuse the fakes from the main call-loop suite (same package, importable by path).
from tests.test_call_loop import (
    _FakeASR,
    _FakeGuard,
    _FakeTransport,
    _FakeTTS,
)


async def _noop(_text: str) -> None:
    pass


def _make_loop(
    *,
    deliver_turn: Callable[[str], Awaitable[None]],
    dtmf_interdigit_ms: int = 2000,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CallLoop:
    return CallLoop(
        transport=_FakeTransport([]),
        asr=_FakeASR([]),
        tts=_FakeTTS([]),
        guard=_FakeGuard(),
        vad=VoiceActivityDetector(threshold=0.5, sample_rate_hz=16_000),
        endpointer=Endpointer(silence_ms=600, sample_rate_hz=16_000),
        guard_state=GuardSessionState(call_id="c1"),
        deliver_turn=deliver_turn,
        voice="",
        call_id="c1",
        dtmf_interdigit_ms=dtmf_interdigit_ms,
        sleep=sleep,
    )


@pytest.mark.asyncio
async def test_digit_group_delivered_as_tagged_turn_on_hash() -> None:
    """A digit group terminated by ``#`` is delivered as a tagged synthetic turn."""
    delivered: list[str] = []

    async def _capture(text: str) -> None:
        delivered.append(text)

    loop = _make_loop(deliver_turn=_capture)
    loop.feed_dtmf("1")
    loop.feed_dtmf("2")
    loop.feed_dtmf("3")
    loop.feed_dtmf("4")
    await loop.feed_dtmf_async("#")  # terminator flushes the group
    # let any scheduled delivery run
    await asyncio.sleep(0)

    assert delivered == ["[DTMF] 1234"]


@pytest.mark.asyncio
async def test_digit_group_flushed_on_interdigit_timeout() -> None:
    """A digit group with no terminator flushes after the inter-digit timeout."""
    delivered: list[str] = []
    released = asyncio.Event()

    async def _capture(text: str) -> None:
        delivered.append(text)

    async def _fake_sleep(_seconds: float) -> None:
        # The inter-digit timer awaits this; release it to fire the flush.
        await released.wait()

    loop = _make_loop(deliver_turn=_capture, dtmf_interdigit_ms=2000, sleep=_fake_sleep)
    loop.feed_dtmf("9")
    loop.feed_dtmf("8")
    await asyncio.sleep(0)
    assert delivered == []  # not yet — timer still pending
    released.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert delivered == ["[DTMF] 98"]


@pytest.mark.asyncio
async def test_armed_confirmation_consumes_digit_not_delivered_as_turn() -> None:
    """While a confirmation is armed, a digit resolves it and is NOT delivered.

    The spoof-resistant property: the digit must never reach ``deliver_turn`` (the
    STT/LLM path) while a confirmation is armed.
    """
    delivered: list[str] = []

    async def _capture(text: str) -> None:
        delivered.append(text)

    loop = _make_loop(deliver_turn=_capture)
    resolver = ArmedConfirmation(prompt=_noop)
    loop.bind_confirmation(resolver)

    armer = asyncio.create_task(resolver.arm("1", timeout_s=5.0, prompt_text="press 1"))
    await asyncio.sleep(0)
    loop.feed_dtmf("1")  # should resolve the armed confirmation, not buffer
    confirmed = await asyncio.wait_for(armer, timeout=2.0)

    assert confirmed is True
    assert delivered == []  # digit consumed by the confirmation, never a turn


@pytest.mark.asyncio
async def test_digit_buffers_again_after_confirmation_resolves() -> None:
    """Once a confirmation resolves, subsequent digits buffer + deliver normally."""
    delivered: list[str] = []

    async def _capture(text: str) -> None:
        delivered.append(text)

    loop = _make_loop(deliver_turn=_capture)
    resolver = ArmedConfirmation(prompt=_noop)
    loop.bind_confirmation(resolver)

    armer = asyncio.create_task(resolver.arm("1", timeout_s=5.0, prompt_text="press 1"))
    await asyncio.sleep(0)
    loop.feed_dtmf("1")
    await asyncio.wait_for(armer, timeout=2.0)

    # Now a normal digit group should buffer + deliver (confirmation no longer armed).
    loop.feed_dtmf("5")
    loop.feed_dtmf("6")
    await loop.feed_dtmf_async("#")
    await asyncio.sleep(0)

    assert delivered == ["[DTMF] 56"]
