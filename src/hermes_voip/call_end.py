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


@enum.unique
class CallEndReason(enum.Enum):
    """Why a call ended, and what the plugin signals to the Hermes session.

    Each member carries two booleans — ``was_failure`` and ``can_followup`` — read
    back by the two properties. Several reasons share the same
    ``(was_failure, can_followup)`` behaviour, so the booleans are stored as
    attributes (see :meth:`__new__`) while the enum ``value`` is the member's own
    unique key string. A plain bool-pair value would make equal-valued members
    silent ALIASES of one another — collapsing ``reason.name`` in logs and the
    by-member ``_OUTBOUND_REASON_PHRASE`` outcome map (ADR-0029) — which
    ``@enum.unique`` now forbids. The taxonomy is split into two families:

    * **Normal** ends — the call completed without an error condition:
      :attr:`REMOTE_BYE` (the caller hung up), :attr:`AGENT_HANGUP` (the agent
      ended the call via the hang-up tool — a SOFT hangup that still keeps the
      session open for follow-up, ADR-0026), and :attr:`EOS` (the inbound media
      stream ended cleanly). All inject :data:`NORMAL_END_NOTE` (a replayed note).

    * **Failure** ends — the call died abnormally: :attr:`MEDIA_TIMEOUT` (RTP
      inactivity / silent media drop), :attr:`PIPELINE_FAILURE` (an ASR/TTS/guard
      task raised), :attr:`SIP_ERROR`, :attr:`CONNECTION_LOST` (the TLS transport
      dropped), :attr:`REGISTRATION_LOST`, and :attr:`MAX_CALL_DURATION` (a policy
      teardown: the active call exceeded the configured max-duration cap, ADR-0113).
      All inject :data:`STOP_COMMAND` (a hard ``/stop``).

    A failure forbids follow-up: the session is hard-stopped, so there is nothing
    to follow up on. A normal end allows it (background work only — see the module
    docstring; there is no live media path post-BYE).
    """

    _was_failure: bool
    _can_followup: bool

    def __new__(cls, key: str, was_failure: bool, can_followup: bool) -> CallEndReason:
        """Create a member whose enum ``value`` is its unique ``key`` string.

        The two booleans are stored as attributes rather than BEING the value, so
        two reasons with identical ``(was_failure, can_followup)`` behaviour stay
        DISTINCT members — a bool-pair value would alias them (see class docstring).
        """
        member = object.__new__(cls)
        member._value_ = key
        member._was_failure = was_failure
        member._can_followup = can_followup
        return member

    REMOTE_BYE = ("remote_bye", False, True)
    AGENT_HANGUP = ("agent_hangup", False, True)
    EOS = ("eos", False, True)
    MEDIA_TIMEOUT = ("media_timeout", True, False)
    PIPELINE_FAILURE = ("pipeline_failure", True, False)
    SIP_ERROR = ("sip_error", True, False)
    CONNECTION_LOST = ("connection_lost", True, False)
    REGISTRATION_LOST = ("registration_lost", True, False)
    # A POLICY teardown: the per-call max-duration watchdog force-ended this call
    # because it exceeded the configured active-call ceiling (ADR-0113). A FAILURE
    # end (hard ``/stop``, no follow-up) like MEDIA_TIMEOUT — the agent did not
    # choose to end it, so the session is stopped, not handed an ambiguous turn.
    MAX_CALL_DURATION = ("max_call_duration", True, False)

    @property
    def was_failure(self) -> bool:
        """Whether this end is a failure (inject ``/stop``) vs a normal end."""
        return self._was_failure

    @property
    def can_followup(self) -> bool:
        """Whether the session is left open for (background) follow-up work.

        ``True`` only for normal ends. This is NOT a promise the caller can still
        be reached — the media path is gone after the call ends; follow-up is
        background agent work / a new outbound callback / another channel.
        """
        return self._can_followup

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
