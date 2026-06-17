"""Tests for the armed-confirmation resolver (ADR-0010 spoof-resistant channel).

``ArmedConfirmation`` is the reusable primitive that turns inbound DTMF digits into
a spoof-resistant yes/no for an IRREVERSIBLE tool (transfer, ADR-0010/0031 §4): an
armed window prompts "press 1 to confirm" and resolves ``True`` only when the
matching digit arrives within the timeout, ``False`` on a wrong digit or a timeout.
It satisfies the existing :class:`hermes_voip.tools.ConfirmationSource` protocol so it
plugs straight into ``CallControlTools._irreversible`` — the load-bearing fact that
unblocks ``transfer_blind``.

The resolver's only time dependency is an injected ``sleep``-style timeout seam, so
every test is deterministic with no wall-clock wait.
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_voip.dtmf_confirm import ArmedConfirmation
from hermes_voip.tools import ConfirmationSource


def test_satisfies_confirmation_source_protocol() -> None:
    """ArmedConfirmation is structurally a ConfirmationSource (drop-in for transfer)."""
    resolver = ArmedConfirmation(prompt=_noop_prompt)
    assert isinstance(resolver, ConfirmationSource)


async def _noop_prompt(_text: str) -> None:
    """A prompt sink that does nothing (the test does not need spoken audio)."""


@pytest.mark.asyncio
async def test_resolves_true_on_matching_digit() -> None:
    """An armed confirmation resolves True when the confirm digit arrives in time."""
    resolver = ArmedConfirmation(prompt=_noop_prompt)

    async def _press() -> None:
        await asyncio.sleep(0)  # let arm() begin awaiting first
        resolver.feed("1")

    armer = asyncio.create_task(
        resolver.arm("1", timeout_s=5.0, prompt_text="press 1 to confirm")
    )
    presser = asyncio.create_task(_press())
    confirmed = await asyncio.wait_for(armer, timeout=2.0)
    await presser

    assert confirmed is True


@pytest.mark.asyncio
async def test_resolves_false_on_wrong_digit() -> None:
    """A non-matching digit during the armed window resolves False immediately."""
    resolver = ArmedConfirmation(prompt=_noop_prompt)

    async def _press() -> None:
        await asyncio.sleep(0)
        resolver.feed("2")  # not the confirm digit

    armer = asyncio.create_task(resolver.arm("1", timeout_s=5.0, prompt_text="press 1"))
    presser = asyncio.create_task(_press())
    confirmed = await asyncio.wait_for(armer, timeout=2.0)
    await presser

    assert confirmed is False


@pytest.mark.asyncio
async def test_resolves_false_on_timeout() -> None:
    """No digit before the timeout resolves False (deterministic injected sleep)."""
    fired = asyncio.Event()

    async def _fake_sleep(_seconds: float) -> None:
        # Resolve the timeout the moment arm() awaits it — no wall-clock wait.
        fired.set()

    resolver = ArmedConfirmation(prompt=_noop_prompt, sleep=_fake_sleep)
    confirmed = await asyncio.wait_for(
        resolver.arm("1", timeout_s=5.0, prompt_text="press 1"), timeout=2.0
    )
    assert confirmed is False
    assert fired.is_set()


@pytest.mark.asyncio
async def test_digit_outside_armed_window_is_ignored() -> None:
    """A digit fed while NOT armed must not affect the next arm() (no leak).

    Only the armed window accepts a digit. A press before arming is dropped, so a
    later arm() still waits for a fresh press (here: a timeout → False), proving the
    stale digit did not pre-satisfy it.
    """
    fired = asyncio.Event()

    async def _fake_sleep(_seconds: float) -> None:
        fired.set()

    resolver = ArmedConfirmation(prompt=_noop_prompt, sleep=_fake_sleep)
    # Feed a confirm digit while NOT armed — must be ignored.
    resolver.feed("1")
    confirmed = await asyncio.wait_for(
        resolver.arm("1", timeout_s=5.0, prompt_text="press 1"), timeout=2.0
    )
    assert confirmed is False  # the stale "1" did not satisfy this arming
    assert fired.is_set()


@pytest.mark.asyncio
async def test_first_digit_decides_then_window_closes() -> None:
    """The FIRST digit in the window decides; a later matching digit cannot flip it.

    A wrong digit resolves False; a confirm digit arriving after must NOT re-open the
    resolved confirmation (the window is closed once resolved).
    """
    resolver = ArmedConfirmation(prompt=_noop_prompt)

    armer = asyncio.create_task(resolver.arm("1", timeout_s=5.0, prompt_text="press 1"))
    await asyncio.sleep(0)
    resolver.feed("2")  # decides False
    confirmed = await asyncio.wait_for(armer, timeout=2.0)
    assert confirmed is False
    # A late confirm digit after resolution is ignored (window closed) — feeding it
    # must not raise and must not change anything observable.
    resolver.feed("1")


@pytest.mark.asyncio
async def test_prompt_is_spoken_when_arming() -> None:
    """arm() speaks the prompt text through the injected prompt sink exactly once."""
    spoken: list[str] = []

    async def _record_prompt(text: str) -> None:
        spoken.append(text)

    fired = asyncio.Event()

    async def _fake_sleep(_seconds: float) -> None:
        fired.set()

    resolver = ArmedConfirmation(prompt=_record_prompt, sleep=_fake_sleep)
    await asyncio.wait_for(
        resolver.arm("1", timeout_s=5.0, prompt_text="press 1 to confirm the transfer"),
        timeout=2.0,
    )
    assert spoken == ["press 1 to confirm the transfer"]


@pytest.mark.asyncio
async def test_confirm_uses_default_digit_and_prompt() -> None:
    """The ConfirmationSource ``confirm()`` arms with the resolver's default digit.

    ``confirm()`` is the transfer-tool entry point: it must arm a confirmation with
    a sensible default confirm digit + prompt and resolve like ``arm``.
    """
    resolver = ArmedConfirmation(prompt=_noop_prompt)

    async def _press() -> None:
        await asyncio.sleep(0)
        resolver.feed("1")  # the default confirm digit

    presser = asyncio.create_task(_press())
    confirmed = await asyncio.wait_for(resolver.confirm(), timeout=2.0)
    await presser
    assert confirmed is True


@pytest.mark.asyncio
async def test_arm_rejects_non_digit() -> None:
    """Arming on a non-DTMF expected digit raises (programming error, fail loud)."""
    resolver = ArmedConfirmation(prompt=_noop_prompt)
    with pytest.raises(ValueError, match="DTMF digit"):
        await resolver.arm("X", timeout_s=5.0, prompt_text="bad")


@pytest.mark.asyncio
async def test_concurrent_arm_rejected() -> None:
    """A second arm() while one is pending raises (one confirmation at a time)."""
    resolver = ArmedConfirmation(prompt=_noop_prompt)
    armer = asyncio.create_task(resolver.arm("1", timeout_s=5.0, prompt_text="press 1"))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="already armed"):
        await resolver.arm("1", timeout_s=5.0, prompt_text="press 1")
    # Clean up the first arming.
    resolver.feed("1")
    await asyncio.wait_for(armer, timeout=2.0)
