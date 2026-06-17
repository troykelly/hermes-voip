"""TDD tests for the echo-robust barge-in gate (ADR-0023).

The gate decides whether an inbound speech onset should interrupt the agent's
TTS. The live self-interruption bug (call ``20260617_033116``): the gateway
reflects the agent's own TTS back on the inbound path, the VAD transcribes it as
the caller speaking, and a single ONSET barged the agent in — repeatedly ending
its own turn. The gate's ``gated`` mode (default) requires a SUSTAINED voiced run
while TTS plays (and for a short tail after), so a short echo blip never barges in
but a genuine sustained interruption still does.

These tests drive the pure :class:`~hermes_voip.media.call_loop.BargeInGate`
state machine with synthetic window ordinals/edges — no audio, threads, or
network. ``frame_index`` is the VAD window ordinal (32 ms per window at 8 kHz).
"""

from __future__ import annotations

from hermes_voip.media.call_loop import BargeInGate, BargeInMode
from hermes_voip.media.vad import SpeechEdge, VadEvent, windows_for_ms

# 8 kHz silero windows are 32 ms; 400 ms → ceil(400/32) = 13 windows.
_RATE = 8_000
_MIN_WINDOWS = windows_for_ms(400, _RATE)  # 13
_TAIL_WINDOWS = windows_for_ms(250, _RATE)  # 8


def _onset(index: int) -> VadEvent:
    return VadEvent(SpeechEdge.ONSET, index, 1.0)


def _offset(index: int) -> VadEvent:
    return VadEvent(SpeechEdge.OFFSET, index, 0.0)


def _make_gate(mode: BargeInMode = BargeInMode.GATED) -> BargeInGate:
    return BargeInGate(
        mode=mode,
        min_voiced_windows=_MIN_WINDOWS,
        tail_windows=_TAIL_WINDOWS,
    )


# ---------------------------------------------------------------------------
# windows_for_ms helper (used by the adapter to convert config ms → windows)
# ---------------------------------------------------------------------------


def test_windows_for_ms_rounds_up_at_8khz() -> None:
    """400 ms / 32 ms-per-window rounds UP to 13 windows; 250 ms → 8."""
    assert windows_for_ms(400, 8_000) == 13
    assert windows_for_ms(250, 8_000) == 8
    # An exact multiple is not rounded past itself (32 ms → exactly 1 window).
    assert windows_for_ms(32, 8_000) == 1
    # Sub-window ms still yields at least one window (never zero → never
    # "instant" barge-in when a positive minimum was configured).
    assert windows_for_ms(1, 8_000) == 1


# ---------------------------------------------------------------------------
# gated mode — the echo-robust core
# ---------------------------------------------------------------------------


def test_gated_short_echo_blip_during_tts_does_not_barge_in() -> None:
    """A short voiced run while TTS plays (echo) must NOT trigger a barge-in.

    The echo blip is ~9 windows of voicing then an OFFSET — below the 13-window
    sustained threshold. The gate must never say "barge in".
    """
    gate = _make_gate()
    gate.tts_active(True)
    fired = False
    # ONSET at window 100, then 9 voiced windows (101..108 ticked), then OFFSET.
    gate.on_event(_onset(100))
    for w in range(100, 109):  # 9 windows of voicing (100..108 inclusive = 9)
        fired = fired or gate.should_barge_in(w)
    gate.on_event(_offset(109))  # echo dips → OFFSET before the threshold
    fired = fired or gate.should_barge_in(109)
    assert fired is False


def test_gated_sustained_speech_during_tts_barges_in() -> None:
    """A SUSTAINED voiced run while TTS plays MUST barge in (real interruption).

    Continuous voicing reaches the 13-window threshold with no OFFSET, so a
    genuine interruption still stops the agent — the gate fires exactly at the
    window where the run length reaches ``min_voiced_windows``.
    """
    gate = _make_gate()
    gate.tts_active(True)
    gate.on_event(_onset(200))
    fired_at: int | None = None
    # Tick continuous voiced windows from the onset; never an OFFSET.
    for w in range(200, 200 + _MIN_WINDOWS + 5):
        if gate.should_barge_in(w):
            fired_at = w
            break
    # Fires once the inclusive run reaches min windows: 200 + (13 - 1) = 212.
    assert fired_at == 200 + _MIN_WINDOWS - 1


def test_gated_repeated_echo_blips_never_accumulate_to_a_barge_in() -> None:
    """Several short echo bursts (each OFFSET before threshold) never barge in.

    Models the live log: rapid SHORT ONSET/OFFSET bursts of 2-9 windows during
    the agent's reply. Each OFFSET disarms the pending onset, so the voiced-run
    counter restarts - runs never accumulate across blips.
    """
    gate = _make_gate()
    gate.tts_active(True)
    fired = False
    cursor = 50
    for burst_len in (2, 9, 5, 3, 8, 4):  # all < 13
        gate.on_event(_onset(cursor))
        for w in range(cursor, cursor + burst_len):
            fired = fired or gate.should_barge_in(w)
        gate.on_event(_offset(cursor + burst_len))
        fired = fired or gate.should_barge_in(cursor + burst_len)
        cursor += burst_len + 7  # a gap of silent windows between bursts
    assert fired is False


def test_gated_no_tts_active_barges_in_immediately() -> None:
    """With no TTS playing (outside the tail), any ONSET barges in at once.

    There is nothing to echo when the agent is silent, so the gate must not add
    latency: a single ONSET is an immediate barge-in (matches ``full``).
    """
    gate = _make_gate()
    # tts_active never set True → agent is not speaking.
    gate.on_event(_onset(10))
    assert gate.should_barge_in(10) is True


def test_gated_tail_keeps_gating_briefly_after_tts_ends() -> None:
    """For ``tail_windows`` after TTS ends, the gate still suppresses echo blips.

    Echo lags the TTS (jitter buffer + network), so a blip arriving just after
    the stream ends must still be gated. A short run inside the tail does not
    barge in.
    """
    gate = _make_gate()
    gate.tts_active(True)
    gate.tts_active(False)  # TTS ended at the current window…
    gate.tail_from(300)  # …whose ordinal is 300; tail covers 300..307.
    fired = False
    gate.on_event(_onset(301))
    for w in range(301, 306):  # 5 windows — within the tail, below threshold
        fired = fired or gate.should_barge_in(w)
    gate.on_event(_offset(306))
    assert fired is False


def test_gated_after_tail_expires_reverts_to_immediate_barge_in() -> None:
    """Once the tail has elapsed, a fresh ONSET barges in immediately again."""
    gate = _make_gate()
    gate.tts_active(True)
    gate.tts_active(False)
    gate.tail_from(300)  # tail = 300..307
    # Advance well past the tail with silence (no edges), then a real onset.
    assert gate.should_barge_in(320) is False  # silence, no pending onset
    gate.on_event(_onset(321))
    assert gate.should_barge_in(321) is True  # tail expired → immediate


# ---------------------------------------------------------------------------
# off mode — never barge in
# ---------------------------------------------------------------------------


def test_off_mode_never_barges_in_even_on_sustained_speech() -> None:
    """``off`` must never barge in, even for a long sustained voiced run."""
    gate = _make_gate(BargeInMode.OFF)
    gate.tts_active(True)
    gate.on_event(_onset(0))
    fired = False
    for w in range(0, 100):  # far beyond any threshold
        fired = fired or gate.should_barge_in(w)
    assert fired is False


# ---------------------------------------------------------------------------
# full mode — legacy immediate barge-in
# ---------------------------------------------------------------------------


def test_full_mode_barges_in_on_first_onset_during_tts() -> None:
    """``full`` reproduces the pre-ADR-0023 behaviour: ONSET → immediate barge-in.

    This is correct only on an echo-cancelled gateway; it is the explicit opt-in
    to maximum interactivity (and what the live bug exhibited before the fix).
    """
    gate = _make_gate(BargeInMode.FULL)
    gate.tts_active(True)
    gate.on_event(_onset(7))
    assert gate.should_barge_in(7) is True


def test_gate_fires_at_most_once_per_onset() -> None:
    """A fired barge-in is latched: the same sustained run does not re-fire.

    Once the gate says "barge in" for an onset, it must not keep returning True
    for every later window of the same run (which would spam ``barge_in()``).
    """
    gate = _make_gate()
    gate.tts_active(True)
    gate.on_event(_onset(0))
    first: int | None = None
    extra_true = 0
    for w in range(0, _MIN_WINDOWS + 10):
        if gate.should_barge_in(w):
            if first is None:
                first = w
            else:
                extra_true += 1
    assert first == _MIN_WINDOWS - 1
    assert extra_true == 0


# ---------------------------------------------------------------------------
# delivery_suppressed — the SECOND echo route (codex finding #1): even when a
# short echo blip does NOT call barge_in(), it is still transcribed and the
# endpointer fires an end-of-turn on its trailing silence — delivering the
# echoed fragment to the agent as a CALLER turn (another "agent interrupts
# itself" path). While armed and the speech run was not an authorised barge-in,
# the turn delivery must be suppressed; a sustained (authorised) run still
# delivers, and outside playout/tail nothing is suppressed.
# ---------------------------------------------------------------------------


def test_delivery_suppressed_for_unauthorised_echo_during_tts() -> None:
    """An echo blip's end-of-turn (after its OFFSET) is suppressed during TTS.

    The blip never reaches the sustained threshold, so it is unauthorised echo.
    The endpointer fires on the trailing silence (OFFSET already cleared the run
    state), and at THAT point the gate must still report delivery suppressed —
    so the echoed fragment is never delivered as a caller turn.
    """
    gate = _make_gate()
    gate.tts_active(True)
    gate.on_event(_onset(100))
    for w in range(100, 106):  # 6 voiced windows — below threshold
        gate.should_barge_in(w)
    gate.on_event(_offset(106))  # echo dips; run state cleared
    # The endpointer would fire on a later silent window (e.g. 113); the echo
    # must be suppressed there because it was never an authorised barge-in.
    assert gate.delivery_suppressed(113) is True


def test_delivery_not_suppressed_for_authorised_sustained_run() -> None:
    """A sustained (authorised) interruption's turn is NOT suppressed.

    Once the run fires a barge-in it is authorised, so its transcript may be
    delivered — even though the gate was armed when the run began.
    """
    gate = _make_gate()
    gate.tts_active(True)
    gate.on_event(_onset(200))
    fired = False
    for w in range(200, 200 + _MIN_WINDOWS + 2):
        fired = fired or gate.should_barge_in(w)
    assert fired is True
    # The authorised run may deliver its turn (the agent is being interrupted).
    assert gate.delivery_suppressed(200 + _MIN_WINDOWS + 1) is False


def test_delivery_not_suppressed_when_not_armed() -> None:
    """With the agent silent (not armed), turn delivery is never suppressed.

    Normal caller turns during silence must always be delivered — the gate only
    suppresses echo while the agent's TTS plays (and the tail).
    """
    gate = _make_gate()
    # tts never active, no tail → not armed.
    gate.on_event(_onset(10))
    gate.should_barge_in(10)
    gate.on_event(_offset(12))
    assert gate.delivery_suppressed(20) is False


def test_delivery_suppression_resets_on_new_onset() -> None:
    """A fresh ONSET clears the prior run's authorisation.

    After an authorised barge-in, a SUBSEQUENT short echo blip (new onset, below
    threshold) while still armed must be suppressed again — authorisation does
    not leak across runs.
    """
    gate = _make_gate()
    gate.tts_active(True)
    # First: an authorised sustained run.
    gate.on_event(_onset(0))
    for w in range(0, _MIN_WINDOWS + 1):
        gate.should_barge_in(w)
    # Then a new short echo blip — must NOT inherit the prior authorisation.
    blip_onset = _MIN_WINDOWS + 20
    gate.on_event(_onset(blip_onset))
    for w in range(blip_onset, blip_onset + 4):
        gate.should_barge_in(w)
    gate.on_event(_offset(blip_onset + 4))
    assert gate.delivery_suppressed(blip_onset + 11) is True


def test_off_mode_suppresses_all_delivery_during_tts() -> None:
    """``off`` mode never authorises a barge-in, so echo delivery stays suppressed.

    With barge-in disabled, no inbound speech during TTS is an authorised
    interruption, so none of it is delivered as a caller turn while armed.
    """
    gate = _make_gate(BargeInMode.OFF)
    gate.tts_active(True)
    gate.on_event(_onset(0))
    for w in range(0, 100):
        gate.should_barge_in(w)
    gate.on_event(_offset(100))
    assert gate.delivery_suppressed(110) is True


def test_full_mode_does_not_suppress_delivery() -> None:
    """``full`` mode authorises any onset, so it never suppresses turn delivery.

    ``full`` is the legacy immediate-barge-in path for echo-cancelled gateways;
    it treats every onset as a real interruption, so the turn is delivered.
    """
    gate = _make_gate(BargeInMode.FULL)
    gate.tts_active(True)
    gate.on_event(_onset(0))
    gate.should_barge_in(0)
    gate.on_event(_offset(3))
    assert gate.delivery_suppressed(10) is False


def test_sustained_run_beginning_in_tail_authorises_and_is_not_suppressed() -> None:
    """A sustained run that begins in the post-TTS tail authorises itself (codex #B).

    The gate is armed during the tail, so a SUSTAINED run beginning there must be
    allowed to barge in (and so deliver its turn): it must not be suppressed as
    echo just because TTS recently stopped. The caller drives ``should_barge_in``
    while armed (including the tail).
    """
    gate = _make_gate()
    gate.tts_active(True)
    gate.tts_active(False)
    gate.tail_from(100)  # tail covers 100..107
    # A sustained run begins inside the tail at window 101.
    gate.on_event(_onset(101))
    fired = False
    for w in range(101, 101 + _MIN_WINDOWS + 2):
        fired = fired or gate.should_barge_in(w)
    assert fired is True, "a sustained run starting in the tail must barge in"
    # …and because it is authorised, its end-of-turn must not be suppressed.
    assert gate.delivery_suppressed(101 + _MIN_WINDOWS + 1) is False
