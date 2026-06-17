"""Recognise gateway-internal *system notices* so they are never spoken.

The Hermes runtime delivers two unrelated kinds of text to a platform
adapter's ``send()`` through the *same* call signature:

1. The genuine agent reply — natural language meant for the user.
2. Operational/proactive **notices** the runtime authors itself: the
   home-channel onboarding prompt ("No home channel is set for …"), the
   cron/kanban "no home channel configured" delivery errors, AND the
   **busy/interrupt acknowledgments** it sends when a new user message arrives
   while the agent is still running ("⚡ Interrupting current task. I'll respond
   to your message shortly." and its Queued/Steered/Subagent siblings).

On a text platform a notice renders harmlessly as a chat message. ``voip`` is a
per-call, live-audio platform with **no text surface and no persistent home
channel** (every call is an ephemeral session). A notice that reaches the voip
adapter's ``send()`` is therefore synthesised as TTS and *spoken to the caller*
— the operator-reported "No home channel is set for voip" leak, and the
"Interrupting… I'll respond…" artifact a caller hears the instant they barge in.

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
proactive-delivery control family* by the one structural hallmark every member
shares — an **announcement that no home channel is set/configured for** a
platform — which natural spoken language never produces. It deliberately does
NOT match on the word "home channel" alone, on the ``/sethome`` setup command,
or on the runtime's emoji glyphs: a genuine support reply may legitimately use
any of those (e.g. "To set your home channel, type /sethome from this chat."),
and dropping such a reply would silence the agent. Matching the announcement
wording is enough — the onboarding prompt that closes with ``/sethome`` also
opens with "No home channel is set for {platform}", so it is caught anyway.
Pure, dependency-free, and covered by the default ``mypy --strict`` + pytest
gate.
"""

from __future__ import annotations

import re

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

# The gateway's busy/interrupt-acknowledgment family (hermes-agent: the
# ``_handle_active_session_busy_message`` block in ``gateway.run``). When a new
# user message arrives while the agent is still running, the runtime delivers one
# of these via ``adapter.send()`` — so on a live call it is SPOKEN to the caller,
# the "Interrupting… I'll respond…" artifact the operator hears on barge-in. Each
# member opens with a distinctive runtime *announcement* of the busy mode and ends
# with a distinctive tail; the regex pairs the two (per mode) so a genuine reply
# that merely echoes one half is not silenced. The four (opening -> tail) pairs:
#   interrupt -> opens "Interrupting current task", tail "I'll respond"
#   queued    -> opens "Queued for the next turn",  tail "I'll respond"
#   steered   -> opens "Steered into current run",  tail "Your message arrives"
#   subagent  -> opens "Subagent working",          tail "your message is queued"
# The optional " (N min elapsed, iteration X/Y, running: <tool>)." status detail —
# which ends the opening sentence with a period — can sit between the two halves, so
# the gap (``_ACK_GAP``) allows up to ~one short clause (90 chars incl. that period)
# but not arbitrary prose. Emoji glyphs are TTS-dependent and NOT matched; only the
# wording is. (Natural conversational speech does not reproduce a full pair verbatim.)
_ACK_GAP = r"[^\n]{0,90}?"
_INTERRUPTION_ACK_RE = re.compile(
    rf"\binterrupting\s+current\s+task\b{_ACK_GAP}\bi['\u2019]?ll\s+respond\b"
    rf"|\bqueued\s+for\s+the\s+next\s+turn\b{_ACK_GAP}\bi['\u2019]?ll\s+respond\b"
    rf"|\bsteered\s+into\s+current\s+run\b{_ACK_GAP}\byour\s+message\s+arrives\b"
    rf"|\bsubagent\s+working\b{_ACK_GAP}\byour\s+message\s+is\s+queued\b",
    re.IGNORECASE,
)


def is_interruption_ack(content: str) -> bool:
    """Return ``True`` when ``content`` is a gateway busy/interrupt acknowledgment.

    Recognises the runtime's "busy ack" family — the message it sends when a new
    user message (a barge-in) arrives mid-turn: "Interrupting current task … I'll
    respond …" and its Queued / Steered / Subagent siblings. On a live call these
    reach ``send()`` and would be *spoken* to the caller (the "Interrupting… I'll
    respond…" artifact), so the voip adapter drops them — after a barge-in the
    agent goes silent and processes the caller's input, it does not announce the
    interruption.

    Conservative by design: it keys on the runtime's distinctive *announcement*
    openings, so a genuine reply that merely mentions interrupting, queues, or
    steering as ordinary words ("Sorry to interrupt, your taxi's here.") is not an
    ack and is still spoken.
    """
    return bool(_INTERRUPTION_ACK_RE.search(content))


def is_internal_system_notice(content: str) -> bool:
    """Return ``True`` when ``content`` is a gateway-internal system notice.

    Recognises two runtime-authored families the voip adapter must never speak to
    the caller:

    * the home-channel / proactive-delivery control family — the onboarding prompt
      and the cron/kanban "no home channel" delivery errors (a "no home channel …
      set|configured … for …" announcement), and
    * the busy/interrupt-acknowledgment family (see :func:`is_interruption_ack`) —
      the "Interrupting… I'll respond…" artifact and its siblings.

    Both checks are structural about their family's announcement wording, which
    natural speech does not produce. It is intentionally conservative — a genuine
    conversational reply that merely *mentions* a home channel, a channel, cron,
    the ``/sethome`` command, or interrupting/queuing as ordinary words is not a
    notice and passes through unchanged.
    """
    return bool(_NO_HOME_CHANNEL_RE.search(content)) or is_interruption_ack(content)
