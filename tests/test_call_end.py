"""Tests for the call-termination reason taxonomy (ADR-0026).

``CallEndReason`` is the typed enum that classifies WHY a call ended and what the
plugin signals to the Hermes session: a FAILURE end (``was_failure=True``) injects
a ``/stop`` hard stop; a NORMAL end injects a plain-text content note the gateway
replays so Hermes decides stop-vs-followup. The fail-safe is: an unknown /
ambiguous end is treated as a failure (``/stop``), never as a silent no-op.

This module is in the DEFAULT mypy + pytest gate (it imports no hermes-agent
runtime): ``CallEndReason`` and its injection-text helper are pure.
"""

from __future__ import annotations

import pytest

from hermes_voip.call_end import (
    NORMAL_END_NOTE,
    STOP_COMMAND,
    CallEndReason,
    injection_text_for_reason,
)


def test_every_reason_has_was_failure_and_can_followup() -> None:
    """Each member exposes the two typed booleans the chokepoint branches on."""
    for reason in CallEndReason:
        # Both are plain ``bool`` (not truthy objects) so the chokepoint's
        # branch and the once-per-call guard are unambiguous.
        assert isinstance(reason.was_failure, bool)
        assert isinstance(reason.can_followup, bool)


def test_normal_reasons_are_not_failures_and_allow_followup() -> None:
    """REMOTE_BYE / AGENT_HANGUP / EOS are normal ends (no failure, follow-up OK)."""
    for reason in (
        CallEndReason.REMOTE_BYE,
        CallEndReason.AGENT_HANGUP,
        CallEndReason.EOS,
    ):
        assert reason.was_failure is False, reason
        assert reason.can_followup is True, reason


def test_failure_reasons_are_failures_and_forbid_followup() -> None:
    """Every failure reason flags a failure and forbids follow-up (hard stop)."""
    for reason in (
        CallEndReason.MEDIA_TIMEOUT,
        CallEndReason.PIPELINE_FAILURE,
        CallEndReason.SIP_ERROR,
        CallEndReason.CONNECTION_LOST,
        CallEndReason.REGISTRATION_LOST,
        CallEndReason.MAX_CALL_DURATION,
    ):
        assert reason.was_failure is True, reason
        assert reason.can_followup is False, reason


def test_failure_reason_injects_the_stop_command() -> None:
    """A failure end injects the gateway-recognised ``/stop`` hard stop verbatim."""
    assert injection_text_for_reason(CallEndReason.PIPELINE_FAILURE) == STOP_COMMAND
    assert injection_text_for_reason(CallEndReason.MEDIA_TIMEOUT) == STOP_COMMAND
    assert STOP_COMMAND == "/stop"


def test_normal_reason_injects_the_content_note_not_a_command() -> None:
    """A normal end injects the plain-text disconnected note (NOT a slash command)."""
    text = injection_text_for_reason(CallEndReason.REMOTE_BYE)
    assert text == NORMAL_END_NOTE
    # The note is NOT a slash command (so the gateway does not treat it as /stop
    # /new /reset) and it states the line is disconnected so the model does not
    # try to keep speaking to a dead line.
    assert not text.startswith("/")
    lowered = text.lower()
    assert "disconnect" in lowered or "hung up" in lowered


def test_classify_clean_return_normal_vs_agent_hangup() -> None:
    """A clean loop return classifies as REMOTE_BYE, or AGENT_HANGUP when flagged."""
    assert CallEndReason.classify_clean_return(agent_hangup=False) is (
        CallEndReason.REMOTE_BYE
    )
    assert CallEndReason.classify_clean_return(agent_hangup=True) is (
        CallEndReason.AGENT_HANGUP
    )


def test_all_members_are_distinct_no_silent_aliasing() -> None:
    """Every CallEndReason name is a DISTINCT member — no two collapse to an alias.

    A plain ``enum.Enum`` makes members with EQUAL values aliases of one another. If
    each member's value were only its ``(was_failure, can_followup)`` bool-pair, the
    five failure names would collapse onto a single member and the three normal names
    onto another — silently breaking ``reason.name`` in logs and the by-member
    outbound-outcome phrase map (ADR-0029, ``_OUTBOUND_REASON_PHRASE``). Each member
    therefore carries a UNIQUE value and the class is ``@enum.unique``; this locks it
    so a future edit that reintroduces a duplicate value fails loudly here rather than
    silently aliasing.
    """
    members = list(CallEndReason)
    # All names are live, distinct members (a naive bool-pair enum would yield 2).
    # These runtime-set cardinalities are the real re-aliasing lock: a revert to
    # bool-pair values would collapse them and fail here.
    assert len(members) == 9
    assert len({m.name for m in members}) == 9
    assert len({m.value for m in members}) == 9
    # Names resolve to themselves (an alias would report the canonical member's name).
    assert CallEndReason.SIP_ERROR.name == "SIP_ERROR"
    assert CallEndReason.AGENT_HANGUP.name == "AGENT_HANGUP"
    # The specific pairs that ALIAS under the naive (bool, bool)-value scheme are now
    # distinct. Typed as the widened enum (not member literals) so the identity check
    # is a genuine runtime assertion, not a statically-true no-op.
    aliased_before: list[tuple[CallEndReason, CallEndReason]] = [
        (CallEndReason.MEDIA_TIMEOUT, CallEndReason.PIPELINE_FAILURE),
        (CallEndReason.SIP_ERROR, CallEndReason.CONNECTION_LOST),
        (CallEndReason.REGISTRATION_LOST, CallEndReason.MEDIA_TIMEOUT),
        (CallEndReason.AGENT_HANGUP, CallEndReason.REMOTE_BYE),
        (CallEndReason.EOS, CallEndReason.REMOTE_BYE),
    ]
    for first, second in aliased_before:
        assert first is not second, (first, second)


def test_max_call_duration_is_a_distinct_failure_end() -> None:
    """MAX_CALL_DURATION (ADR-0113) is a DISTINCT failure member injecting ``/stop``.

    The per-call max-duration watchdog force-ends an over-long active call; the end
    must hard-stop the session (a policy teardown the agent did not choose), and it
    must be observably its OWN reason — not an alias of MEDIA_TIMEOUT — so logs and
    the outbound-outcome map can tell a duration cap apart from an RTP drop.
    """
    reason = CallEndReason.MAX_CALL_DURATION
    assert reason.was_failure is True
    assert reason.can_followup is False
    assert injection_text_for_reason(reason) == STOP_COMMAND
    # A distinct member, not an alias (an alias would report MEDIA_TIMEOUT's name).
    assert reason.name == "MAX_CALL_DURATION"
    assert reason.value == "max_call_duration"


def test_fail_safe_unknown_end_is_a_failure_stop() -> None:
    """The fail-safe default for an unknown/ambiguous end is a failure (``/stop``)."""
    # ``fail_safe`` is the reason the chokepoint uses when it cannot otherwise
    # classify the end — it MUST be a failure so an ambiguous end hard-stops the
    # session rather than leaving it dangling or replaying a content note.
    fail_safe = CallEndReason.fail_safe()
    assert fail_safe.was_failure is True
    assert injection_text_for_reason(fail_safe) == STOP_COMMAND


@pytest.mark.parametrize("reason", list(CallEndReason))
def test_injection_text_is_total_over_the_enum(reason: CallEndReason) -> None:
    """``injection_text_for_reason`` is total: every member maps to a non-empty text."""
    text = injection_text_for_reason(reason)
    assert text
    # Failure ⇔ /stop; normal ⇔ the content note. No third outcome.
    if reason.was_failure:
        assert text == STOP_COMMAND
    else:
        assert text == NORMAL_END_NOTE
