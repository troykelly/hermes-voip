"""End-of-turn endpointing by trailing silence (ADR-0008 Phase 1).

VAD (:mod:`hermes_voip.media.vad`) says *when* the caller is speaking; the
endpointer decides when their **turn is over** so the agent may respond. The rule
(ADR-0008): a turn ends once speech has occurred and then been absent for
``HERMES_VOIP_ENDPOINT_SILENCE_MS`` (``MediaConfig.endpoint_silence_ms``, default
500 ms). Downstream this sets ``Transcript.end_of_turn`` for engines without
native turn detection (ADR-0006).

The timer is measured in **silero window ordinals**, not the wall clock: VAD emits
one window per fixed 32 ms slice and stamps each edge with its monotonic ordinal,
so counting windows gives an exact, deterministic, offline-testable timer with no
clock drift. ``silence_ms`` is converted to a window count once (rounding up so we
never end a turn *earlier* than configured). The VAD stamps its OFFSET edge on the
*first* silent window, so the ``silence_windows`` silent windows of the timer are
the ordinals ``[offset, offset + silence_windows - 1]`` and the turn ends on the
**last** of them — once that many windows have actually been silent.

``Smart-Turn-v2`` (a learned end-of-turn classifier) is the optional later swap
behind this same end-of-turn signal (ADR-0008) — not built here.
"""

from __future__ import annotations

import math
from typing import Final

from hermes_voip.media.vad import SILERO_WINDOW_SAMPLES, SpeechEdge, VadEvent

__all__ = ["Endpointer"]

_MS_PER_SECOND: Final[int] = 1000


class Endpointer:
    """Fires end-of-turn after a configured run of trailing silence.

    Drive it two ways, together:

    * :meth:`on_event` — feed every :class:`VadEvent` from the detector. ONSET
      marks speech in progress (cancelling any pending end-of-turn); OFFSET arms
      the silence timer from that window.
    * :meth:`advance` — call once per processed window with its ordinal; returns
      ``True`` on the single window at which the trailing-silence threshold is
      reached. It returns ``True`` exactly once per turn.

    The two are separate because a turn can end during *silence*, when no VAD edge
    is being produced — so the host loop must tick :meth:`advance` on every window
    it processes, not only when an edge fires.
    """

    def __init__(self, *, silence_ms: int, sample_rate_hz: int = 16_000) -> None:
        """Create an endpointer.

        Args:
            silence_ms: Trailing silence (ms) that declares end-of-turn
                (``MediaConfig.endpoint_silence_ms``). Must be positive.
            sample_rate_hz: ``8000`` or ``16000`` — sets the window duration the
                ms threshold is converted against (must match the detector's
                rate so ordinals line up).

        Raises:
            ValueError: If ``silence_ms`` is not positive or the rate is not a
                native silero rate.
        """
        if silence_ms <= 0:
            msg = f"silence_ms must be positive, got {silence_ms}"
            raise ValueError(msg)
        if sample_rate_hz not in SILERO_WINDOW_SAMPLES:
            msg = f"sample_rate_hz must be 8000 or 16000, got {sample_rate_hz}"
            raise ValueError(msg)
        window_samples = SILERO_WINDOW_SAMPLES[sample_rate_hz]
        window_ms = window_samples / sample_rate_hz * _MS_PER_SECOND
        # round up: never declare the turn over before the configured silence.
        self._silence_windows = math.ceil(silence_ms / window_ms)
        # window ordinal at which the current silence run began, or None when
        # speech is in progress / no speech has occurred yet.
        self._silence_since: int | None = None
        # True once the pending end-of-turn has fired, until speech resumes.
        self._fired = False
        # the highest ordinal seen by advance(), to enforce monotonicity.
        self._last_index: int | None = None

    @property
    def silence_windows(self) -> int:
        """Number of trailing-silence windows that declare end-of-turn."""
        return self._silence_windows

    def on_event(self, event: VadEvent) -> None:
        """Update turn state from a VAD edge.

        ONSET: speech is in progress — cancel any armed silence timer and clear
        the fired latch so the *next* offset can end a fresh turn. OFFSET: arm
        the silence timer at this window.
        """
        if event.edge is SpeechEdge.ONSET:
            self._silence_since = None
            self._fired = False
        else:  # SpeechEdge.OFFSET
            self._silence_since = event.frame_index
            self._fired = False

    def advance(self, current_index: int) -> bool:
        """Tick the timer at window ``current_index``; True iff a turn ends now.

        Returns ``True`` on the first window at which :attr:`silence_windows`
        windows (counting the OFFSET window itself, which is already silent) have
        been silent — i.e. ``offset + silence_windows - 1`` — and only once per
        turn (further ticks in the same silent run return ``False`` until speech
        resumes).

        Args:
            current_index: The ordinal of the window just processed. Must be
                non-decreasing across calls (the VAD window clock is monotonic).

        Raises:
            ValueError: If ``current_index`` goes backwards.
        """
        if self._last_index is not None and current_index < self._last_index:
            msg = (
                f"current_index must be monotonic, got {current_index} "
                f"after {self._last_index}"
            )
            raise ValueError(msg)
        self._last_index = current_index
        if self._silence_since is None or self._fired:
            return False
        # ``_silence_since`` is the FIRST silent window (the ordinal VAD stamped on
        # the OFFSET edge — see vad.py ``_score_window``, which fires OFFSET on the
        # first below-exit window). By ``current_index`` inclusive we have seen
        # ``current_index - _silence_since + 1`` silent windows; the turn ends once
        # that count reaches ``silence_windows``, i.e. on window
        # ``_silence_since + silence_windows - 1``.
        silent_windows_seen = current_index - self._silence_since + 1
        if silent_windows_seen >= self._silence_windows:
            self._fired = True
            return True
        return False

    def reset(self) -> None:
        """Clear all turn state: no speech in progress, no pending end-of-turn.

        Call when starting a new conversation or after the agent takes the floor,
        so a stale silence timer cannot fire into the next turn.
        """
        self._silence_since = None
        self._fired = False
        self._last_index = None
