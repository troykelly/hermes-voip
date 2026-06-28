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


def test_delivery_suppressed_for_authorised_sustained_run_while_armed() -> None:
    """A sustained authorised run is STILL suppressed while the agent's TTS is on air.

    INVERTED from the pre-fix ``test_delivery_not_suppressed_for_authorised_
    sustained_run``, which asserted delivery was NOT suppressed for an authorised
    run during active TTS. That encoded the live self-echo bug (2026-06-21): a ~1 s
    reflected comfort filler is a sustained run that authorises a (self-)barge-in,
    and the pre-fix gate then delivered the agent's own filler to the STT. True
    half-duplex: while ARMED (here ``_tts_audio_active`` stays True) nothing is
    delivered REGARDLESS of authorisation; release happens only after the gate
    disarms (see ``test_authorised_run_delivers_after_gate_disarms_post_playout``).
    """
    gate = _make_gate()
    gate.tts_active(True)
    gate.on_event(_onset(200))
    fired = False
    for w in range(200, 200 + _MIN_WINDOWS + 2):
        fired = fired or gate.should_barge_in(w)
    assert fired is True
    # The agent's TTS is still on the wire: the run is its own echo, suppressed.
    assert gate.delivery_suppressed(200 + _MIN_WINDOWS + 1) is True


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


def test_full_mode_suppresses_delivery_while_tts_on_air() -> None:
    """``full`` mode still suppresses delivery while the agent's TTS is on the wire.

    INVERTED from the pre-fix ``test_full_mode_does_not_suppress_delivery``, which
    asserted ``full`` never suppresses (every onset authorised → delivered). On an
    echoing gateway that delivered the agent's own reflected audio. Half-duplex
    binds every mode: while ARMED nothing is delivered. ``full`` still barges in
    immediately (max interactivity) — it just cannot transcribe our own echo while
    we speak. (See also ``test_full_mode_suppresses_delivery_while_armed`` for the
    sustained-then-OFFSET variant.)
    """
    gate = _make_gate(BargeInMode.FULL)
    gate.tts_active(True)
    gate.on_event(_onset(0))
    gate.should_barge_in(0)
    gate.on_event(_offset(3))
    assert gate.delivery_suppressed(10) is True


def test_sustained_run_beginning_in_tail_barges_in_but_is_suppressed_in_tail() -> None:
    """A sustained run in the post-TTS tail still barges in, but stays suppressed.

    INVERTED (delivery half only) from the pre-fix
    ``test_sustained_run_beginning_in_tail_authorises_and_is_not_suppressed``,
    which asserted such a run delivers its turn while still in the tail. The tail
    exists BECAUSE the echo of the just-stopped TTS keeps arriving, so audio there
    is ambiguous (late echo vs. real caller) and half-duplex withholds it. The
    barge-in itself is UNCHANGED — a sustained run in the tail still stops the
    agent — only its in-tail transcript is suppressed; the caller's continued
    speech delivers once the tail elapses (the pump withholds its frames from the
    endpointer meanwhile, so no in-tail end-of-turn fires).
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
    assert fired is True, "a sustained run starting in the tail must still barge in"
    # While still within the tail (armed) its end-of-turn is suppressed (echo-safe).
    assert gate.delivery_suppressed(105) is True
    # Once the tail has elapsed the gate disarms → the continued run is delivered.
    assert gate.delivery_suppressed(108) is False


# ---------------------------------------------------------------------------
# TRUE HALF-DUPLEX (live self-echo regression, call 2026-06-21 Yealink T48G
# HANDSET so NO acoustic echo): the operator test gateway reflects the agent's own
# outbound TTS back on the inbound leg under the gateway's SSRC. A ~1 s comfort
# filler ("One moment please.") is a SUSTAINED continuous voiced run that EXCEEDS
# the sustained barge-in threshold, so ``should_barge_in`` FIRES on the agent's
# OWN echo and ``_fire`` authorises the run — and the OLD ``delivery_suppressed``
# then returned False for that authorised run while still armed, delivering the
# agent's own filler to the STT as a caller turn ("ONE MOMENT PLEASE" @ 100%).
#
# The fix: while the gate is ARMED (the agent's TTS is on the wire, or within the
# echo tail) NOTHING is delivered as a transcript — authorisation may RELEASE
# suppression only AFTER the agent has actually stopped (gate disarmed). A real
# caller still interrupts (``should_barge_in`` stops the agent, which flips
# ``_tts_audio_active`` False and disarms the gate); their CONTINUED speech then
# flows to the STT normally once the gate is disarmed. The ONLY behaviour change
# is: a sustained self-echo can no longer "authorise" its own transcript.
# ---------------------------------------------------------------------------


def test_sustained_echo_during_active_tts_is_suppressed_unconditionally() -> None:
    """A sustained run while the agent's TTS is STILL on the wire is suppressed.

    This is the live comfort-filler self-echo: the gateway reflects the ~1 s
    filler while ``_tts_audio_active`` is True, the run exceeds the sustained
    threshold so ``should_barge_in`` fires and authorises it — but because the
    agent's audio is still playing it is the agent's OWN echo, and its end-of-turn
    must be suppressed REGARDLESS of authorisation (true half-duplex). The OLD
    gate returned ``False`` here, delivering the agent's filler as a caller turn.
    """
    gate = _make_gate()
    gate.tts_active(True)  # the agent's TTS stays on the wire throughout
    gate.on_event(_onset(200))
    fired = False
    # A sustained voiced run past the threshold — the reflected filler echo.
    for w in range(200, 200 + _MIN_WINDOWS + 5):
        fired = fired or gate.should_barge_in(w)
    assert fired is True, "the sustained echo run does reach the barge-in threshold"
    # While the agent's audio is still on the wire the run is its own echo: the
    # endpointer's end-of-turn on it must be suppressed despite authorisation.
    assert gate.delivery_suppressed(200 + _MIN_WINDOWS + 6) is True


def test_sustained_echo_in_tail_is_suppressed_unconditionally() -> None:
    """A sustained authorised run within the post-TTS tail is still suppressed.

    After the agent's own filler is barged in, ``_tts_audio_active`` flips False
    but the echo of the already-transmitted filler keeps arriving across the tail
    (jitter buffer + network). That residual echo is a sustained run that
    authorises itself — yet while ARMED (the tail) nothing may be delivered.
    """
    gate = _make_gate()
    gate.tts_active(True)
    gate.tts_active(False)  # the agent's audio stopped (e.g. barge-in cut it)…
    gate.tail_from(300)  # …and the echo tail covers windows 300..307.
    gate.on_event(_onset(301))
    fired = False
    for w in range(301, 301 + _MIN_WINDOWS + 2):  # sustained run inside the tail
        fired = fired or gate.should_barge_in(w)
    assert fired is True, "a sustained run in the tail still reaches the threshold"
    # Within the tail the gate is armed → suppressed regardless of authorisation.
    assert gate.delivery_suppressed(305) is True


def test_full_mode_suppresses_delivery_while_armed() -> None:
    """Even ``full`` mode suppresses STT delivery while the agent's TTS is on air.

    ``full`` authorises every onset (legacy immediate barge-in), but half-duplex
    binds it too: nothing the gate hears WHILE our audio is on the wire may be
    delivered as a transcript. The OLD gate delivered an authorised ``full``-mode
    run during playout — the same self-echo route on an echoing gateway.
    """
    gate = _make_gate(BargeInMode.FULL)
    gate.tts_active(True)
    gate.on_event(_onset(0))
    assert gate.should_barge_in(0) is True  # full mode authorises immediately
    gate.on_event(_offset(3))
    assert gate.delivery_suppressed(10) is True


def test_authorised_run_delivers_after_gate_disarms_post_playout() -> None:
    """No-regression: once the gate disarms (TTS off + tail elapsed) delivery resumes.

    A genuine interruption stops the agent (``_tts_audio_active`` → False); after
    the tail elapses the gate is no longer armed, so the caller's CONTINUED speech
    flows to the STT normally. Authorisation RELEASES suppression only here — after
    the agent has actually stopped — never while our audio is on the wire.
    """
    gate = _make_gate()
    gate.tts_active(True)
    # A sustained run authorises a real barge-in and the agent stops.
    gate.on_event(_onset(0))
    fired = False
    for w in range(0, _MIN_WINDOWS + 2):
        fired = fired or gate.should_barge_in(w)
    assert fired is True
    gate.tts_active(False)
    gate.tail_from(_MIN_WINDOWS + 2)  # tail = (m+2)..(m+2+_TAIL_WINDOWS-1)
    tail_last = _MIN_WINDOWS + 2 + _TAIL_WINDOWS - 1
    # Still armed across the tail → suppressed (it is residual echo of the cut audio).
    assert gate.delivery_suppressed(tail_last) is True
    # One window past the tail the gate has disarmed → the caller's continued
    # speech (still the authorised run) is delivered.
    assert gate.delivery_suppressed(tail_last + 1) is False
