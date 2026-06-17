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

from hermes_voip.notice_filter import is_internal_system_notice

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
        "",
        "   ",
    ],
)
def test_genuine_agent_replies_are_not_internal(reply: str) -> None:
    """A genuine reply is never an internal notice.

    Even replies that mention 'channel' or 'cron' as ordinary words must pass
    the guard so the caller still hears them.
    """
    assert is_internal_system_notice(reply) is False
