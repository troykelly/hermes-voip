"""Call-termination reason taxonomy + Hermes-session signal text (ADR-0026).

When a call ends — any path — the plugin must tell the Hermes session, because
Hermes 0.16 has **no typed session-end / reason API**: lifecycle is driven only by
INBOUND text the gateway parses from a ``MessageEvent``. The single mechanism is
to inject a synthetic ``internal=True`` ``MessageEvent`` (the ``internal`` flag
bypasses user auth) from the adapter's teardown chokepoint, carrying either:

* the gateway-recognised control command ``/stop`` — a hard stop with no replay
  (used for a **failure** end); or
* a plain-text content note — which the gateway REPLAYS to the agent as its next
  turn (used for a **normal** end), so Hermes itself decides stop-vs-followup.

This module is the pure, hermes-free half: the :class:`CallEndReason` taxonomy
(each member carrying ``was_failure`` / ``can_followup``) and the total mapping
from a reason to its injection text. The adapter owns the actual injection.

Fail-safe: an unknown / ambiguous end is a **failure** (``/stop``) — never a
silent no-op and never a replayed note. An end we cannot explain must hard-stop
the session, not leave it dangling or hand the model an ambiguous turn.

Follow-up reality: a NORMAL end keeps the session open, but there is no media
path once the call is gone — "follow-up" can only be background agent work, a new
outbound callback (ADR-0019), or a notification on another Hermes channel, never
speaking to the now-disconnected caller. ``can_followup`` records whether the
session is left open for that, NOT a promise that the caller can still be heard.
"""

from __future__ import annotations

import enum

__all__ = [
    "NORMAL_END_NOTE",
    "STOP_COMMAND",
    "CallEndReason",
    "injection_text_for_reason",
]

#: The gateway-recognised hard-stop command. Parsed from ``MessageEvent.text`` by
#: the gateway's ``get_command()`` + ``should_bypass_active_session`` path; with
#: ``internal=True`` it bypasses user auth. A FAILURE end injects this verbatim.
STOP_COMMAND = "/stop"

#: The plain-text note a NORMAL end injects. It is deliberately NOT a slash
#: command and NOT one of the gateway's control-interrupt strings, so the gateway
#: REPLAYS it to the agent as the next user turn (Hermes then decides whether to
#: stop or do follow-up work). It states the line is disconnected so the model
#: does not attempt to keep speaking to a dead media path (there is none after
#: BYE). The bracketed framing marks it as a system observation, not caller speech.
NORMAL_END_NOTE = "[The caller has hung up; the line is now disconnected.]"


class CallEndReason(enum.Enum):
    """Why a call ended, and what the plugin signals to the Hermes session.

    The value of each member is its ``(was_failure, can_followup)`` pair; the two
    properties read it back. The taxonomy is split into two families:

    * **Normal** ends — the call completed without an error condition:
      :attr:`REMOTE_BYE` (the caller hung up), :attr:`AGENT_HANGUP` (the agent
      ended the call via the hang-up tool — a SOFT hangup that still keeps the
      session open for follow-up, ADR-0026), and :attr:`EOS` (the inbound media
      stream ended cleanly). All inject :data:`NORMAL_END_NOTE` (a replayed note).

    * **Failure** ends — the call died abnormally: :attr:`MEDIA_TIMEOUT` (RTP
      inactivity / silent media drop), :attr:`PIPELINE_FAILURE` (an ASR/TTS/guard
      task raised), :attr:`SIP_ERROR`, :attr:`CONNECTION_LOST` (the TLS transport
      dropped), and :attr:`REGISTRATION_LOST`. All inject :data:`STOP_COMMAND`
      (a hard ``/stop``).

    A failure forbids follow-up: the session is hard-stopped, so there is nothing
    to follow up on. A normal end allows it (background work only — see the module
    docstring; there is no live media path post-BYE).
    """

    REMOTE_BYE = (False, True)
    AGENT_HANGUP = (False, True)
    EOS = (False, True)
    MEDIA_TIMEOUT = (True, False)
    PIPELINE_FAILURE = (True, False)
    SIP_ERROR = (True, False)
    CONNECTION_LOST = (True, False)
    REGISTRATION_LOST = (True, False)

    @property
    def was_failure(self) -> bool:
        """Whether this end is a failure (inject ``/stop``) vs a normal end."""
        failure, _ = self.value
        return failure

    @property
    def can_followup(self) -> bool:
        """Whether the session is left open for (background) follow-up work.

        ``True`` only for normal ends. This is NOT a promise the caller can still
        be reached — the media path is gone after the call ends; follow-up is
        background agent work / a new outbound callback / another channel.
        """
        _, followup = self.value
        return followup

    @staticmethod
    def classify_clean_return(*, agent_hangup: bool) -> CallEndReason:
        """Classify a clean (non-exception) call-loop return.

        The conversational loop returning without raising means the inbound media
        stream ended — normally because the caller sent BYE (which stops the media
        engine). When the agent's own hang-up tool drove the BYE, ``agent_hangup``
        is set and the reason is :attr:`AGENT_HANGUP` instead; both are normal
        ends with the same signal, but the distinction is recorded for audit.

        Args:
            agent_hangup: Whether the agent's hang-up tool initiated this end.

        Returns:
            :attr:`AGENT_HANGUP` when ``agent_hangup`` is set, else
            :attr:`REMOTE_BYE`.
        """
        return CallEndReason.AGENT_HANGUP if agent_hangup else CallEndReason.REMOTE_BYE

    @staticmethod
    def fail_safe() -> CallEndReason:
        """The reason for an unknown / ambiguous end (the fail-safe default).

        Always a **failure** so an end the chokepoint cannot otherwise explain
        hard-stops the session (``/stop``) rather than leaving it dangling or
        replaying a content note. :attr:`PIPELINE_FAILURE` is used: an unexplained
        end is, by definition, an abnormal pipeline outcome.
        """
        return CallEndReason.PIPELINE_FAILURE


def injection_text_for_reason(reason: CallEndReason) -> str:
    """Return the ``MessageEvent.text`` to inject into the Hermes session.

    Total over :class:`CallEndReason` by construction (the single branch is on the
    member's own ``was_failure``, which every member defines): a failure end maps
    to :data:`STOP_COMMAND` (``/stop``), every normal end to :data:`NORMAL_END_NOTE`.
    There is no third outcome and no silent empty string (rule 37).

    Args:
        reason: The classified call-end reason.

    Returns:
        ``/stop`` for a failure end; the disconnected content note otherwise.
    """
    if reason.was_failure:
        return STOP_COMMAND
    return NORMAL_END_NOTE
