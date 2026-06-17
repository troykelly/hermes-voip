"""Tests for the gateway-internal *system notice* guard (home-channel leak).

The Hermes runtime delivers operational/proactive *notices* (home-channel
onboarding, cron/kanban delivery errors) to a platform adapter's ``send()``
exactly as it delivers a genuine agent reply — same ``chat_id``, no
distinguishing metadata (verified against ``hermes-agent==0.16.0``:
``gateway.run._deliver_platform_notice`` calls ``adapter.send`` and
``_thread_metadata_for_source`` returns ``None`` for voip). For a live phone
call there is no text surface, so a notice that reaches ``send()`` would be
*spoken* to the caller. ``is_internal_system_notice`` recognises that
proactive/home-channel control family so the adapter can drop it instead of
synthesising it as audio.

The guard must be structural about the *home-channel / proactive-delivery*
concept — NOT a brittle exact-string match on one runtime message — and must
NEVER classify a genuine conversational reply as a notice.
"""

from __future__ import annotations

import pytest

from hermes_voip.notice_filter import is_internal_system_notice, is_interruption_ack

# The exact home-channel onboarding notice the gateway emits on the first turn
# of a session with no ``VOIP_HOME_CHANNEL`` (verified text of
# ``gateway.run._handle_message`` line ~9414 in hermes-agent 0.16.0). This is
# the operator-reported leak: "No home channel is set for voip".
_HOME_CHANNEL_NOTICE = (
    "\U0001f4ec No home channel is set for Voip. "
    "A home channel is where Hermes delivers cron job results "
    "and cross-platform messages.\n\n"
    "Type /sethome to make this chat your home channel, "
    "or ignore to skip."
)

# The Slack variant uses the parent slash command (same family, same concept).
_HOME_CHANNEL_NOTICE_SLACK = (
    "\U0001f4ec No home channel is set for Slack. "
    "A home channel is where Hermes delivers cron job results "
    "and cross-platform messages.\n\n"
    "Type /hermes sethome to make this chat your home channel, "
    "or ignore to skip."
)

# The kanban dashboard / cron delivery error for a platform with no home
# channel (verified: ``plugins.kanban.dashboard.plugin_api`` + ``cli`` +
# ``tools.send_message_tool`` all emit "No home channel ... for ... platform").
_KANBAN_NO_HOME = "No home channel configured for platform 'voip'. "
_CRON_NO_HOME = "No home channel set for voip to determine where to send the message."


@pytest.mark.parametrize(
    "notice",
    [
        _HOME_CHANNEL_NOTICE,
        _HOME_CHANNEL_NOTICE_SLACK,
        _KANBAN_NO_HOME,
        _CRON_NO_HOME,
    ],
)
def test_home_channel_and_proactive_notices_are_internal(notice: str) -> None:
    """The home-channel / cron 'no home channel' family is internal."""
    assert is_internal_system_notice(notice) is True


@pytest.mark.parametrize(
    "reply",
    [
        "Hello, you're through to the Hermes voice assistant. How can I help?",
        "Sure — your appointment is booked for Tuesday at 3pm.",
        "I can set that home channel up for you in the lounge if you like.",
        "The answer is eleven.",
        "Let me check the channel guide and the cron schedule for you.",
        # A genuine reply that merely *mentions* the /sethome command (without
        # the home-channel onboarding announcement) must still be spoken — the
        # slash-command cue alone is not a notice.
        "To pick a default chat later, run the /sethome command from that app.",
        # A genuine SUPPORT reply that *explains* how to set a home channel —
        # mentioning both "/sethome" AND "home channel" — is the agent helping
        # the caller, not a runtime notice; it must still be spoken (the
        # onboarding notice is caught by its "No home channel … for" wording).
        "To set your home channel, type /sethome from this chat.",
        "Your home channel can be set with /sethome whenever you like.",
        "",
        "   ",
    ],
)
def test_genuine_agent_replies_are_not_internal(reply: str) -> None:
    """A genuine reply is never an internal notice.

    Even replies that mention 'channel', 'cron', or a slash-command as ordinary
    words must pass the guard so the caller still hears them.
    """
    assert is_internal_system_notice(reply) is False


# ---------------------------------------------------------------------------
# Interruption / busy-ack family (ADR-0028) — the "Interrupting…" leak
# ---------------------------------------------------------------------------

# The verbatim busy-acknowledgment strings the gateway delivers through
# adapter.send() when a new user message (a barge-in) arrives while the agent is
# still running (verified against the vendored gateway: run.py
# `_handle_active_session_busy_message`, lines ~3544-3564, delivered via
# `_send_with_retry` -> `send`). On a live call these are SPOKEN to the caller —
# the operator-reported "Interrupting… I will respond…" artifact. `status_detail`
# (the optional " (N min elapsed, iteration X/Y, running: <tool>)") is included
# below to prove the matcher tolerates it.
_INTERRUPT_ACK = (
    "⚡ Interrupting current task (2 min elapsed, iteration 3/10, "
    "running: web_search). I'll respond to your message shortly."
)
_INTERRUPT_ACK_NO_DETAIL = (
    "⚡ Interrupting current task. I'll respond to your message shortly."
)
_QUEUED_ACK = (
    "⏳ Queued for the next turn. I'll respond once the current task finishes."
)
_STEERED_ACK = (
    "⏩ Steered into current run. Your message arrives after the next tool call."
)
_SUBAGENT_ACK = (
    "⏳ Subagent working — your message is queued for when it finishes "
    "(use /stop to cancel everything)."
)


@pytest.mark.parametrize(
    "ack",
    [
        _INTERRUPT_ACK,
        _INTERRUPT_ACK_NO_DETAIL,
        _QUEUED_ACK,
        _STEERED_ACK,
        _SUBAGENT_ACK,
    ],
)
def test_interruption_acks_are_recognised(ack: str) -> None:
    """Every gateway busy/interrupt acknowledgment is recognised as an ack."""
    assert is_interruption_ack(ack) is True


@pytest.mark.parametrize(
    "ack",
    [
        _INTERRUPT_ACK,
        _INTERRUPT_ACK_NO_DETAIL,
        _QUEUED_ACK,
        _STEERED_ACK,
        _SUBAGENT_ACK,
    ],
)
def test_interruption_acks_are_internal_notices(ack: str) -> None:
    """The send() boundary drops the busy-ack family (never spoken to the caller).

    ``is_internal_system_notice`` is the single guard the voip adapter's
    ``send()`` consults; it must now cover the interruption-ack family too so the
    "Interrupting… I'll respond…" phrase is never synthesised as TTS.
    """
    assert is_internal_system_notice(ack) is True


@pytest.mark.parametrize(
    "reply",
    [
        # Genuine replies that mention interruption/queues/steering as ordinary
        # words must still be spoken — only the gateway's own ack family is dropped.
        "Sorry to interrupt, but your taxi has arrived.",
        "I'll respond to your email as soon as I can — what was the address?",
        "Your call is queued behind two others; you're next in line.",
        "Let me steer the conversation back to the booking.",
        "The current task is to confirm your appointment for Tuesday.",
        "I have to interrupt the music to tell you the meeting moved.",
        # An opening phrase WITHOUT the gateway's paired tail is not the ack: the
        # matcher requires both halves, so a reply that merely echoes one opening
        # (e.g. a board-game agent) is still spoken (codex review hardening).
        "You're queued for the next turn after Dana finishes her move.",
        "I steered into current run mode by mistake — let me redo that.",
        "",
        "   ",
    ],
)
def test_genuine_replies_are_not_interruption_acks(reply: str) -> None:
    """A conversational reply that mentions interruption is not an ack.

    The matcher keys on the gateway ack's distinctive *announcement* shape
    ("Interrupting current task … I'll respond", "Queued for the next turn …",
    "Steered into current run …", "Subagent working … your message is queued"),
    which natural speech does not reproduce verbatim.
    """
    assert is_interruption_ack(reply) is False
    assert is_internal_system_notice(reply) is False
