"""Recognise gateway-internal *system notices* so they are never spoken.

The Hermes runtime delivers two unrelated kinds of text to a platform
adapter's ``send()`` through the *same* call signature:

1. The genuine agent reply — natural language meant for the user.
2. Operational/proactive **notices** the runtime authors itself: the
   home-channel onboarding prompt ("No home channel is set for …"), and the
   cron/kanban "no home channel configured" delivery errors.

On a text platform a notice renders harmlessly as a chat message. ``voip`` is a
per-call, live-audio platform with **no text surface and no persistent home
channel** (every call is an ephemeral session). A notice that reaches the voip
adapter's ``send()`` is therefore synthesised as TTS and *spoken to the caller*
— the operator-reported "No home channel is set for voip" leak.

Verified against ``hermes-agent==0.16.0`` that the two kinds are
indistinguishable at the ``send()`` boundary by their arguments:

* ``gateway.run._handle_message`` builds the onboarding notice and delivers it
  via ``gateway.run._deliver_platform_notice`` → ``adapter.send(chat_id,
  content, metadata=…)`` — the *same* method the genuine reply uses.
* ``gateway.platforms.base._thread_metadata_for_source`` returns ``None`` for a
  voip source (no ``thread_id``), so the notice and the reply both arrive with
  ``metadata=None`` — no marker to branch on.
* The runtime exposes no "notice vs reply" flag, no reply-text interception
  hook, and ``handle_message`` returns ``None`` (the reply is sent internally),
  so the adapter cannot capture "only the agent's reply" structurally.

This module is the principled stand-in: it recognises the *home-channel /
proactive-delivery control family* by its structural hallmarks — text that
**announces the absence/configuration of a home channel** or **instructs a
Hermes setup slash-command** — neither of which occurs in natural spoken
language. It deliberately does NOT match on the word "home channel" alone (a
genuine reply may use it as ordinary words), nor on the runtime's emoji
glyphs (a genuine reply may contain emoji). Pure, dependency-free, and covered
by the default ``mypy --strict`` + pytest gate.
"""

from __future__ import annotations

import re

# The Hermes setup slash-command that the home-channel onboarding notice tells
# the user to run (``gateway.run._handle_message``: ``/sethome`` for most
# platforms, ``/hermes sethome`` for Slack's single-parent-command dispatch).
# A genuine spoken reply never instructs the user to type a Hermes command.
_SETHOME_COMMAND_RE = re.compile(r"/(?:hermes\s+)?sethome\b", re.IGNORECASE)

# The "no home channel …" announcement shared verbatim by the whole proactive
# family in hermes-agent 0.16.0:
#   * ``gateway.run._handle_message``      — "No home channel is set for {P}."
#   * ``tools.send_message_tool``          — "No home channel set for {P} …"
#   * ``hermes_cli.setup``                 — "No home channel set for: …"
#   * ``plugins.kanban.dashboard.plugin_api`` / ``cli`` — "No home channel
#                                              configured for platform {P} …"
# i.e. "no home channel" optionally followed by "is"/"are", then
# "set"/"configured", then "for". This is an announcement *about the absence
# of* a home channel, which natural conversational speech does not produce.
_NO_HOME_CHANNEL_RE = re.compile(
    r"\bno\s+home\s+channel\b[^.\n]*?\b(?:set|configured)\b[^.\n]*?\bfor\b",
    re.IGNORECASE,
)


def is_internal_system_notice(content: str) -> bool:
    """Return ``True`` when ``content`` is a gateway-internal system notice.

    Recognises the home-channel / proactive-delivery control family — the
    onboarding prompt and the cron/kanban "no home channel" delivery errors —
    so the voip adapter can drop it instead of speaking it to the caller.

    The check is structural about that family (slash-command instruction, or a
    "no home channel … for …" announcement) and is intentionally conservative:
    a genuine conversational reply that merely *mentions* a home channel, a
    channel, or cron in passing is not a notice and passes through unchanged.
    """
    return bool(
        _SETHOME_COMMAND_RE.search(content) or _NO_HOME_CHANNEL_RE.search(content)
    )
