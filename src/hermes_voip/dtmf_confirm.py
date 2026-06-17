"""Armed-confirmation resolver — the spoof-resistant DTMF confirmation channel.

ADR-0009 requires an irreversible tool (transfer, ADR-0010/0031 §4) to obtain a
confirmation that a caller cannot forge by reading the agent's injected text back or
by audio replay. A keypad press carried out-of-band on its own RTP payload type is
materially harder to forge than recognised speech, so DTMF is the designated
confirmation input.

:class:`ArmedConfirmation` is that channel as a reusable primitive:

* an irreversible action **arms** a confirmation — :meth:`arm` speaks a prompt
  ("press 1 to confirm") and then awaits the matching digit;
* the call controller feeds every received digit into it (:meth:`feed`);
* the first digit in the armed window decides: a match resolves ``True``, any other
  digit resolves ``False``, and the absence of a digit before ``timeout_s`` resolves
  ``False``;
* outside an armed window a fed digit is **ignored** (it never pre-satisfies a future
  arming), so a stale or spoofed press cannot leak across the gate.

It satisfies the existing :class:`hermes_voip.tools.ConfirmationSource` protocol
(``async confirm() -> bool``), so it is a drop-in for ``CallControlTools._irreversible``
— the load-bearing fact that **unblocks ``transfer_blind``** (task #26). A future
``transfer``/destructive tool reaches the same primitive through ``confirm()``.

The digit math (``0-9 * # A-D``) is validated by :func:`hermes_voip.dtmf.digit_to_event`
so an out-of-range expected digit fails loud at :meth:`arm`. The only time dependency
is an injected ``sleep`` seam (default :func:`asyncio.sleep`), so the resolver is fully
deterministic under test.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from hermes_voip.dtmf import digit_to_event

__all__ = ["ArmedConfirmation"]

_log: Final = logging.getLogger(__name__)

#: Default confirm digit ``confirm()`` arms on (the conventional "press 1 to confirm").
_DEFAULT_CONFIRM_DIGIT: Final[str] = "1"

#: Default armed window (seconds) ``confirm()`` waits for the confirm digit.
_DEFAULT_CONFIRM_TIMEOUT_S: Final[float] = 10.0

#: Default spoken prompt ``confirm()`` speaks when it arms. Generic + gateway-agnostic
#: (no PII); the action-specific phrasing belongs to the tool that calls :meth:`arm`.
_DEFAULT_CONFIRM_PROMPT: Final[str] = (
    "To confirm, press 1 on your keypad now. To cancel, do nothing."
)


class ArmedConfirmation:
    """A one-shot, windowed DTMF confirmation resolver (ADR-0009/0010).

    Reusable across irreversible tools. One :class:`ArmedConfirmation` lives per
    active call; the call controller binds it (so received digits route here while it
    is armed) and a tool calls :meth:`confirm` (or :meth:`arm` for a custom digit /
    prompt / window). At most one confirmation is armed at a time — a concurrent
    :meth:`arm` raises rather than racing two windows on one keypad stream.

    The resolution is **edge-true**: the FIRST digit in the window decides and the
    window then closes, so a later digit (even a matching one) cannot flip an already
    -resolved confirmation. This keeps the gate single-decision and non-repeatable.
    """

    def __init__(
        self,
        *,
        prompt: Callable[[str], Awaitable[None]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Create a resolver.

        Args:
            prompt: Async sink that speaks (or otherwise delivers) the confirmation
                prompt text to the caller — typically a thin wrapper over the call
                loop's TTS path. Awaited once at the start of :meth:`arm`.
            sleep: The timeout seam the armed window awaits (default
                :func:`asyncio.sleep`). Tests inject a controllable coroutine to fire
                the timeout deterministically.
        """
        self._prompt = prompt
        self._sleep = sleep
        # The future the current armed window resolves; None when not armed. A digit
        # (feed) or the timeout sets it exactly once.
        self._pending: asyncio.Future[bool] | None = None
        # The digit that resolves the current window True (validated at arm()).
        self._expected: str | None = None

    @property
    def armed(self) -> bool:
        """Whether a confirmation is currently armed (awaiting a digit / timeout)."""
        return self._pending is not None

    async def confirm(self) -> bool:
        """Arm a default confirmation and await the result (``ConfirmationSource``).

        The transfer-tool entry point: arms on the default confirm digit (``1``), the
        default prompt, and the default window, then resolves like :meth:`arm`. This
        is the method ``CallControlTools._irreversible`` calls — satisfying the
        :class:`hermes_voip.tools.ConfirmationSource` protocol.

        Returns:
            ``True`` iff the caller pressed the confirm digit within the window.
        """
        return await self.arm(
            _DEFAULT_CONFIRM_DIGIT,
            timeout_s=_DEFAULT_CONFIRM_TIMEOUT_S,
            prompt_text=_DEFAULT_CONFIRM_PROMPT,
        )

    async def arm(
        self, expected_digit: str, *, timeout_s: float, prompt_text: str
    ) -> bool:
        """Speak ``prompt_text``, then await ``expected_digit`` for ``timeout_s``.

        Args:
            expected_digit: The single DTMF digit that confirms (``0-9``, ``*``,
                ``#``, ``A``-``D``; case-insensitive). Validated — a non-DTMF value
                raises before the window opens (a programming error, fail loud).
            timeout_s: How long (seconds) to await a digit before resolving ``False``.
            prompt_text: The prompt spoken once via the injected prompt sink.

        Returns:
            ``True`` if the caller pressed ``expected_digit`` first within the window;
            ``False`` on any other digit or on timeout.

        Raises:
            ValueError: If ``expected_digit`` is not a single DTMF keypad character.
            RuntimeError: If a confirmation is already armed (one window at a time).
        """
        # Validate the confirm digit up front (normalises case, rejects junk). This
        # also normalises ``expected_digit`` to the upper-case form ``feed`` compares.
        digit_to_event(expected_digit)
        normalised = expected_digit.upper()
        if self._pending is not None:
            msg = "a confirmation is already armed (one window at a time)"
            raise RuntimeError(msg)

        loop = asyncio.get_running_loop()
        pending: asyncio.Future[bool] = loop.create_future()
        self._pending = pending
        self._expected = normalised
        _log.info("dtmf confirm: armed (await digit, %.0fs window)", timeout_s)

        # Everything after arming runs under try/finally so the window is ALWAYS
        # disarmed on exit — including if the prompt raises or this coroutine is
        # CANCELLED while the prompt audio is in flight (cross-vendor review #2). Were
        # the prompt awaited before the try, a TTS failure / barge-in cancel would
        # leave ``_pending`` set: a later digit would resolve a stale window and the
        # next arm() would be wrongly rejected as 'already armed'.
        timeout_task: asyncio.Task[None] | None = None
        try:
            # Speak the prompt AFTER arming so a digit pressed during the prompt audio
            # is already routed to this window (feed() resolves it), never dropped.
            await self._prompt(prompt_text)
            # Arm the no-digit timeout only once the prompt has been delivered (the
            # window the caller is told about starts now).
            timeout_task = loop.create_task(self._expire(pending, timeout_s))
            return await pending
        finally:
            # Window closed (resolved, timed out, prompt-failed, or cancelled): cancel
            # the timer if it was created, and clear the armed state so the next arm()
            # starts clean — never stuck armed.
            if timeout_task is not None:
                timeout_task.cancel()
            self._pending = None
            self._expected = None

    async def _expire(self, pending: asyncio.Future[bool], timeout_s: float) -> None:
        """Resolve ``pending`` ``False`` after ``timeout_s`` (the no-digit path).

        Awaits the injected sleep seam; if the window is still unresolved when it
        returns, resolves ``False``. ``CancelledError`` (a digit resolved first, so
        :meth:`arm` cancelled this task) propagates cleanly and is never swallowed
        (rule 37).
        """
        await self._sleep(timeout_s)
        if not pending.done():
            _log.info("dtmf confirm: timed out with no digit — resolved False")
            pending.set_result(False)

    def feed(self, digit: str) -> bool:
        """Offer one received DTMF ``digit`` to the armed window.

        Called by the call controller for every digit the engine surfaces while a
        confirmation is armed. The FIRST digit in the window decides:

        * the expected digit (case-insensitive) resolves ``True``;
        * any other digit resolves ``False``.

        Outside an armed window — or after the window has already resolved — the digit
        is **ignored** (returns ``False``), so a stale/spoofed press cannot leak into
        a later arming. The boolean return reports whether this call CONSUMED the digit
        (i.e. a window was armed and unresolved) so the controller knows not to also
        surface it as a menu turn.

        Returns:
            ``True`` if the digit was consumed by an armed confirmation; ``False`` if
            there was nothing armed to consume it (the controller routes it elsewhere).
        """
        pending = self._pending
        if pending is None or pending.done():
            return False
        result = digit.upper() == self._expected
        pending.set_result(result)
        _log.info(
            "dtmf confirm: digit received — resolved %s", "True" if result else "False"
        )
        return True
