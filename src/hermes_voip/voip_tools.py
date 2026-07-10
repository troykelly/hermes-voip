"""Agent-facing VoIP tool registration + the pre-tool-call gate (ADR-0026/0011).

The plugin previously registered ONLY the platform, so a live agent had no way to
end a call — a usability gap the operator hit on a real call. This module wires
the agent call-control tools into the Hermes runtime and gates them through the
ADR-0009/0011/0020 tool policy.

Tools exposed (each registered via ``ctx.register_tool`` and gated by the shared
``pre_tool_call`` hook below):

* ``hang_up`` — SAFE: end the current call (ADR-0026, SOFT agent hangup).
* ``hold_call`` / ``resume_call`` — ELEVATED (ADR-0011): place the caller on hold
  / resume; reversible, so they need privilege but no confirmation.
* ``list_registrations`` — ELEVATED (ADR-0020): list the gateway registrations;
  read-only, but discloses internal extension metadata an untrusted caller must
  not enumerate, so it is clamped to a privileged session.

* ``transfer_blind`` — IRREVERSIBLE (ADR-0010/0011/0031): hand the current caller
  to another extension / SIP URI via a blind REFER. Operator-only; the REFER fires
  ONLY after the person on the call presses the armed ADR-0010 DTMF confirm digit
  (the spoof-resistant safeguard — see ``transfer_blind_on_call``), so a missed
  prompt injection cannot transfer the caller on a "yes" alone.

* ``transfer_attended`` — IRREVERSIBLE (ADR-0048): a CONSULTATIVE transfer. A single
  ``action``-discriminated tool drives the state machine: ``consult`` dials the target
  (a consultation leg, via the outbound origination path gated by the SAME outbound
  allowlist as ``place_call``), ``complete`` sends the REFER+Replaces (RFC 3891) on the
  original call so the caller is bridged to the target, and ``cancel`` abandons the
  consultation. The agent-driven consult-leg origination (ADR-0019/0029) makes this a
  real tool, not a stub — closing the deferral ADR-0031 §4 recorded.

Design constraints:

* **Light imports.** This module imports no hermes-agent runtime at module top
  (so ``import hermes_voip`` stays cheap). The handlers read the Hermes session
  context (``gateway.session_context``) lazily, only when invoked at runtime.
* **Finding the call.** A tool handler runs inside the agent's turn and does not
  receive the call id directly, but the Hermes session's ``chat_id`` IS the SIP
  ``Call-ID`` (ADR-0002: one call = one DM session keyed by Call-ID). The handler
  reads it from the task-local session context, so it acts on exactly the call
  whose turn is being processed — concurrency-safe across simultaneous calls.
  (``list_registrations`` is the one exception: a process-wide read that needs no
  Call-ID — but the gate still clamps it to the *calling* session's privilege.)
* **The adapter.** The live :class:`~hermes_voip.adapter.VoipAdapter` registers
  itself here (``set_active_adapter``) when it connects, so the handlers can reach
  the per-call session map. There is one voip adapter per gateway process.

The hangup is SOFT (ADR-0026): the tool sends BYE and ends the call loop, which
routes through the adapter teardown chokepoint as AGENT_HANGUP — a NORMAL end
that keeps the Hermes session open for follow-up, never a hard ``/stop``.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Protocol, runtime_checkable

from hermes_voip.call import CallError
from hermes_voip.config import ConfigError
from hermes_voip.originate import (
    OutboundCallCancelled,
    OutboundCallFailed,
    OutboundCallNotAllowed,
)
from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.refer import (
    NotifyProgress,
    TransferOutcomeClass,
    TransferUnknownReason,
    classify_transfer_progress,
)
from hermes_voip.tools import gate_voip_tool

__all__ = [
    "HANG_UP_TOOL_NAME",
    "HANG_UP_TOOL_SCHEMA",
    "HOLD_TOOL_NAME",
    "HOLD_TOOL_SCHEMA",
    "LIST_REGISTRATIONS_TOOL_NAME",
    "LIST_REGISTRATIONS_TOOL_SCHEMA",
    "OPEN_ENTRY_TOOL_NAME",
    "OPEN_ENTRY_TOOL_SCHEMA",
    "PLACE_CALL_TOOL_NAME",
    "PLACE_CALL_TOOL_SCHEMA",
    "REPORT_RESULT_TOOL_NAME",
    "REPORT_RESULT_TOOL_SCHEMA",
    "RESUME_TOOL_NAME",
    "RESUME_TOOL_SCHEMA",
    "SEND_DTMF_TOOL_NAME",
    "SEND_DTMF_TOOL_SCHEMA",
    "TRANSFER_ATTENDED_TOOL_NAME",
    "TRANSFER_ATTENDED_TOOL_SCHEMA",
    "TRANSFER_BLIND_TOOL_NAME",
    "TRANSFER_BLIND_TOOL_SCHEMA",
    "VOIP_TOOLSET",
    "_RING_TIMEOUT_ENV",  # ADR-0086: stable public name of the ring-timeout env var
    "AttendedTransferOutcome",
    "AttendedTransferResult",
    "PlaceCallOutcome",
    "ProactiveDecision",
    "ProactiveDenyReason",
    "TransferOutcome",
    "TransferResult",
    "VoipToolHost",
    "active_voip_adapter",
    "build_attended_transfer_result",
    "build_transfer_result",
    "hang_up_handler",
    "hold_call_handler",
    "list_registrations_handler",
    "open_entry_handler",
    "outbound_failure_category",
    "place_call_handler",
    "register_voip_tools",
    "report_call_result_handler",
    "resume_call_handler",
    "send_dtmf_handler",
    "set_active_adapter",
    "transfer_attended_handler",
    "transfer_blind_handler",
    "voip_pre_tool_call",
]

_log = logging.getLogger(__name__)


# The Hermes tool-handler contract is "a handler never raises; it returns a JSON
# tool result, an ``{"error": ...}`` object on failure" — at the handler boundary
# the model-facing JSON error IS the surfaced error. So every handler below ends
# with an OUTERMOST ``except Exception`` that LOGS the unanticipated exception (with
# full traceback context via ``_log.exception``) and returns an error-JSON string.
# This reconciles with rule 37 (errors propagate, never silently swallowed): the
# failure is surfaced twice — to the model as the contract's error result, and to
# the operator's logs with full context — it is *translated* into the error channel,
# not dropped. The specific-error returns inside each handler stay unchanged; the
# guard only catches what they did not anticipate. ``noqa: BLE001`` carries this
# justification at each site. The two SECRET-bearing tools (``send_dtmf`` /
# ``open_entry``) use ``_tool_failure_redacted`` instead — same contract, but the
# exception detail is never echoed (it could embed the secret digits / opening
# secret).
def _tool_failure(tool_name: str, exc: BaseException) -> str:
    """Log ``exc`` and render the handler-boundary error-JSON for ``tool_name``."""
    _log.exception("VoIP tool %r failed with an unanticipated error", tool_name)
    return json.dumps({"error": f"{tool_name} failed: {exc}"})


def _tool_failure_redacted(tool_name: str, exc: BaseException) -> str:
    """Handler-boundary failure for the SECRET-bearing tools (send_dtmf/open_entry).

    Identical contract to :func:`_tool_failure` EXCEPT the exception detail is never
    surfaced — neither in the model-facing result nor in the log line. An
    unanticipated ``exc`` from deep in the send path can embed the very DTMF digits
    the caller asked to send (or the opening secret), which must never be echoed (the
    handlers document "digits are NOT echoed in the result/log"). So the result is a
    FIXED generic message and the log line carries ONLY the tool name and the
    exception's TYPE name — never ``str(exc)`` and never ``exc_info`` (a traceback
    renders the exception's repr, which would re-embed the digits). The failure is
    still surfaced (rule 37): to the model as an error result, and to the operator's
    logs as a typed failure line — just without the secret-bearing message text.
    """
    # Deliberately NOT ``_log.exception`` / ``exc_info=exc``: a rendered traceback or
    # exception repr would re-embed the secret digits. The type name is safe and
    # carries enough to triage without leaking the message.
    _log.error(
        "VoIP tool %r failed with an unanticipated %s (detail redacted)",
        tool_name,
        type(exc).__name__,
    )
    return json.dumps({"error": f"{tool_name} failed (internal error)"})


class TransferOutcome(Enum):
    """The result of a DTMF-confirmed blind transfer (ADR-0010/0031/0109).

    :meth:`VoipToolHost.transfer_blind_on_call` returns this (wrapped in a
    :class:`TransferResult`) so the handler maps each outcome to a distinct tool
    message.

    Pre-REFER outcomes (the REFER never fired):

    * ``UNCONFIRMED`` — a wrong digit or the confirmation timed out; **no REFER**.
    * ``NO_CALL`` — the call was unknown or had already ended; **no REFER**.
    * ``BLOCKED`` — the call's OWN privilege gate refused the transfer (it is not an
      operator-level, non-degraded call). The sync ``pre_tool_call`` gate runs before
      this method, but the REFER chokepoint re-checks the guard ITSELF (defense in
      depth), so a session that lost privilege or went ``degraded`` *during* the
      confirmation window — or any direct/bypass invocation — cannot fire the REFER.
      **No REFER**.

    Post-REFER terminal outcomes (ADR-0109 — the REFER was accepted and its
    transfer-progress NOTIFY, if any, observed):

    * ``COMPLETED`` — the terminal NOTIFY reported a 2xx; the caller reached the target.
    * ``FAILED`` — the terminal NOTIFY reported a 3xx-6xx (busy / declined /
      unreachable); the transfer definitively did not complete.
    * ``OUTCOME_UNKNOWN`` — no terminal NOTIFY arrived within the bounded wait (timeout
      / call-end / a declined RFC 4488 subscription), so the real outcome is unknown.

    ``TRANSFERRED`` is the internal REFER-accepted intermediate (the confirm digit was
    pressed and the REFER fired); the adapter resolves it into COMPLETED / FAILED /
    OUTCOME_UNKNOWN via the terminal NOTIFY before returning, so it is no longer a
    terminal tool result.

    A *failure* to even obtain a confirmation (no telephone-event negotiated) or a
    REFER the gateway rejects is signalled by an exception, not a member — those are
    loud errors, never a silent no-op (rule 37).
    """

    TRANSFERRED = "transferred"
    UNCONFIRMED = "unconfirmed"
    NO_CALL = "no_call"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class AttendedTransferOutcome(Enum):
    """The result of COMPLETING an attended (consultative) transfer (ADR-0048/0109).

    Returned by :meth:`VoipToolHost.complete_attended_transfer` (wrapped in an
    :class:`AttendedTransferResult`) so the handler maps each outcome to a distinct
    tool-result message.

    Pre-REFER outcomes (the REFER+Replaces never fired):

    * ``NO_CONSULT`` — no consultation leg is in flight for this call (the agent must
      ``consult`` first); **no REFER**.
    * ``NO_CALL`` — the original call (or the consult leg) is unknown / already ended;
      **no REFER**.
    * ``BLOCKED`` — the original call's privilege clamp refused it (not operator-level,
      or degraded — possibly a state change during the consultation); **no REFER**.

    Post-REFER terminal outcomes (ADR-0109 — the REFER+Replaces fired and its
    transfer-progress NOTIFY, if any, observed):

    * ``COMPLETED`` — the terminal NOTIFY reported a 2xx; the caller reached the target.
    * ``FAILED`` — the terminal NOTIFY reported a 3xx-6xx; the transfer failed.
    * ``OUTCOME_UNKNOWN`` — no terminal NOTIFY within the bounded wait (timeout /
      call-end / declined RFC 4488 subscription), so the real outcome is unknown.

    ``TRANSFERRED`` is the internal REFER-fired intermediate; the adapter resolves it
    into COMPLETED / FAILED / OUTCOME_UNKNOWN via the terminal NOTIFY before returning,
    so it is no longer a terminal tool result.

    A gateway REFER rejection is signalled by an exception (``CallError``), not a
    member — a loud error, never a silent no-op (rule 37).
    """

    TRANSFERRED = "transferred"
    NO_CONSULT = "no_consult"
    NO_CALL = "no_call"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class TransferResult:
    """The terminal result of a blind transfer (ADR-0109): outcome + NOTIFY detail.

    :meth:`VoipToolHost.transfer_blind_on_call` returns this so the handler renders the
    real transfer outcome. ``outcome`` is the tool-facing :class:`TransferOutcome`; for
    a NOTIFY-reported ``FAILED``, ``notify_status`` + ``notify_reason`` carry the
    sipfrag SIP status line the message shows. ``timeout_secs`` is the bounded wait the
    adapter applied, so the ``OUTCOME_UNKNOWN`` message can name it. ``unknown_reason``
    (ADR-0109 P2) discriminates WHY an ``OUTCOME_UNKNOWN`` outcome is unknown (timeout
    / declined subscription / call-ended / wait-disabled) so the message states the real
    reason. The notify/timeout/reason fields are ``None`` for the pre-REFER outcomes
    (``NO_CALL`` / ``UNCONFIRMED`` / ``BLOCKED``).
    """

    outcome: TransferOutcome
    notify_status: int | None = None
    notify_reason: str | None = None
    timeout_secs: float | None = None
    unknown_reason: TransferUnknownReason | None = None


@dataclass(frozen=True, slots=True)
class AttendedTransferResult:
    """The terminal result of an attended transfer (ADR-0109): outcome + NOTIFY detail.

    The attended analogue of :class:`TransferResult`, returned by
    :meth:`VoipToolHost.complete_attended_transfer`; the field semantics are identical
    (including the ADR-0109 P2 ``unknown_reason``).
    """

    outcome: AttendedTransferOutcome
    notify_status: int | None = None
    notify_reason: str | None = None
    timeout_secs: float | None = None
    unknown_reason: TransferUnknownReason | None = None


# ADR-0109: map the pure terminal classification to the tool-facing outcome enums.
_BLIND_OUTCOME_BY_CLASS: dict[TransferOutcomeClass, TransferOutcome] = {
    TransferOutcomeClass.COMPLETED: TransferOutcome.COMPLETED,
    TransferOutcomeClass.FAILED: TransferOutcome.FAILED,
    TransferOutcomeClass.OUTCOME_UNKNOWN: TransferOutcome.OUTCOME_UNKNOWN,
}
_ATTENDED_OUTCOME_BY_CLASS: dict[TransferOutcomeClass, AttendedTransferOutcome] = {
    TransferOutcomeClass.COMPLETED: AttendedTransferOutcome.COMPLETED,
    TransferOutcomeClass.FAILED: AttendedTransferOutcome.FAILED,
    TransferOutcomeClass.OUTCOME_UNKNOWN: AttendedTransferOutcome.OUTCOME_UNKNOWN,
}


def _terminal_notify(progress: NotifyProgress | None) -> NotifyProgress | None:
    """Return ``progress`` only when it is a TERMINAL NOTIFY, else ``None`` (ADR-0109).

    A non-terminal update (a ``100 Trying`` that leaked through) carries no final
    status, so its status/reason must not be shown as an outcome.
    """
    return progress if progress is not None and progress.terminated else None


def build_transfer_result(
    progress: NotifyProgress | None,
    timeout_secs: float,
    unknown_reason: TransferUnknownReason | None = None,
) -> TransferResult:
    """Classify a blind transfer's terminal NOTIFY into a :class:`TransferResult`.

    The adapter's glue (ADR-0109): maps the pure classification to the tool-facing
    :class:`TransferOutcome` and attaches the terminal SIP status/reason (for
    ``FAILED``) plus the bounded wait applied and the discriminated ``unknown_reason``
    (ADR-0109 P2) the ``OUTCOME_UNKNOWN`` message names.
    """
    terminal = _terminal_notify(progress)
    return TransferResult(
        outcome=_BLIND_OUTCOME_BY_CLASS[classify_transfer_progress(progress)],
        notify_status=terminal.status_code if terminal is not None else None,
        notify_reason=terminal.reason if terminal is not None else None,
        timeout_secs=timeout_secs,
        unknown_reason=unknown_reason,
    )


def build_attended_transfer_result(
    progress: NotifyProgress | None,
    timeout_secs: float,
    unknown_reason: TransferUnknownReason | None = None,
) -> AttendedTransferResult:
    """Attended analogue of :func:`build_transfer_result` (ADR-0109)."""
    terminal = _terminal_notify(progress)
    return AttendedTransferResult(
        outcome=_ATTENDED_OUTCOME_BY_CLASS[classify_transfer_progress(progress)],
        notify_status=terminal.status_code if terminal is not None else None,
        notify_reason=terminal.reason if terminal is not None else None,
        timeout_secs=timeout_secs,
        unknown_reason=unknown_reason,
    )


class PlaceCallOutcome(Enum):
    """The structured outcome of a failed ``place_call`` (ADR-0086).

    Maps distinct SIP failure classes to a typed outcome the agent can branch
    on, WITHOUT leaking the gateway host, extension, or any other PII:

    * ``BUSY`` — the callee is busy (SIP 486 Busy Here / 600 Busy Everywhere).
    * ``NO_ANSWER`` — the call was not answered: either the gateway timed out on
      its own (SIP 408 / 487 before we sent a CANCEL), or the outbound INVITE
      was actively CANCELled by our own ring-timeout (the ADR-0069 CANCEL path,
      which raises :class:`~hermes_voip.originate.OutboundCallCancelled`).
    * ``DECLINED`` — the callee explicitly rejected the call (SIP 603 Decline).
    * ``FAILED`` — any other final non-2xx response (4xx / 5xx / 6xx other than
      the above), or a transport/media initialisation failure (``RuntimeError``
      from :meth:`VoipToolHost.place_call_with_objective`).

    These values are the **stable JSON API surface** embedded in the agent-facing
    tool result as ``{"failure_outcome": <value>, "error": <message>}``; the
    model branches on the value string, not the enum member name.
    """

    BUSY = "busy"
    NO_ANSWER = "no_answer"
    DECLINED = "declined"
    FAILED = "failed"


#: SIP 4xx/5xx/6xx status codes that map to ``PlaceCallOutcome.BUSY``.
_BUSY_STATUSES: frozenset[int] = frozenset({486, 600})
#: SIP status codes that map to ``PlaceCallOutcome.NO_ANSWER`` (peer-initiated).
_NO_ANSWER_STATUSES: frozenset[int] = frozenset({408, 487})
#: SIP status codes that map to ``PlaceCallOutcome.DECLINED``.
_DECLINED_STATUSES: frozenset[int] = frozenset({603})

#: Environment variable for the bounded ring timeout (seconds). When set to a
#: finite positive float no greater than ``_MAX_RING_TIMEOUT_SECS``,
#: ``place_call_handler`` forwards it as ``ring_timeout_secs`` to
#: :meth:`VoipToolHost.place_call_with_objective`, which arms the ADR-0069
#: outbound CANCEL timer. Unset / blank => ``None`` (the adapter's hard sink
#: timeout governs instead). Invalid values raise :class:`ConfigError`.
#: Default: unset.
_RING_TIMEOUT_ENV = "HERMES_VOIP_RING_TIMEOUT_SECS"
_DEFAULT_RING_TIMEOUT_SECS: float | None = None
_MAX_RING_TIMEOUT_SECS = 3600.0


def _parse_ring_timeout() -> float | None:
    """Read ``HERMES_VOIP_RING_TIMEOUT_SECS`` and return a validated timeout.

    Returns ``None`` when the variable is unset or blank. Raises
    :class:`ConfigError` when the value is non-numeric, non-finite, non-positive,
    or greater than ``_MAX_RING_TIMEOUT_SECS``.
    """
    raw = os.environ.get(_RING_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_RING_TIMEOUT_SECS
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{_RING_TIMEOUT_ENV} must be a number of seconds, got {raw!r}"
        raise ConfigError(msg) from exc
    if not math.isfinite(value):
        msg = f"{_RING_TIMEOUT_ENV} must be finite, got {raw!r}"
        raise ConfigError(msg)
    if value <= 0:
        msg = f"{_RING_TIMEOUT_ENV} must be > 0 seconds; got {value}"
        raise ConfigError(msg)
    if value > _MAX_RING_TIMEOUT_SECS:
        msg = (
            f"{_RING_TIMEOUT_ENV} must be <= {_MAX_RING_TIMEOUT_SECS:.0f} "
            f"seconds; got {value}"
        )
        raise ConfigError(msg)
    return value


def _classify_outbound_failure(exc: OutboundCallFailed) -> PlaceCallOutcome:
    """Map an ``OutboundCallFailed`` SIP status code to a ``PlaceCallOutcome``."""
    if exc.status in _BUSY_STATUSES:
        return PlaceCallOutcome.BUSY
    if exc.status in _NO_ANSWER_STATUSES:
        return PlaceCallOutcome.NO_ANSWER
    if exc.status in _DECLINED_STATUSES:
        return PlaceCallOutcome.DECLINED
    return PlaceCallOutcome.FAILED


def outbound_failure_category(exc: Exception) -> str:
    """Classify an outbound-dial failure into its stable ADR-0086 category string.

    The single source of truth for the failure CATEGORY surfaced BOTH in the
    agent-facing ``place_call`` tool result (via :func:`_classify_outbound_failure`
    and the ``place_call_handler`` branches) AND in the adapter's
    ``outbound_call_failed`` SLO log event (ADR-0075) — so the two never diverge.

    Returns ONLY the fixed category value string
    (``"busy"`` / ``"no_answer"`` / ``"declined"`` / ``"failed"``) — NEVER the SIP
    reason phrase, the gateway host, the dialled target, or the exception message.
    Gateway connection detail on the SIP/media path is public-repo-sensitive even
    though it is not a secret (rule 34 / ADR-0084 lesson), so a caller may log the
    returned value freely.

    * :class:`~hermes_voip.originate.OutboundCallFailed` — mapped by SIP status via
      :func:`_classify_outbound_failure` (486/600 busy, 408/487 no-answer, 603
      declined, else failed).
    * :class:`~hermes_voip.originate.OutboundCallCancelled` — our own ring-timeout /
      abort CANCEL (ADR-0069): the caller stopped waiting on an unanswered call, so
      the outcome is ``NO_ANSWER`` (the SAME mapping ``place_call_handler`` applies).
    * Any other exception (a transport/media-init ``RuntimeError``, including the WSS
      ``NotImplementedError``, or an unexpected error on the dial path) — the
      catch-all ``FAILED`` outcome, mirroring the handler's ``RuntimeError`` branch.
    """
    if isinstance(exc, OutboundCallFailed):
        return _classify_outbound_failure(exc).value
    if isinstance(exc, OutboundCallCancelled):
        return PlaceCallOutcome.NO_ANSWER.value
    return PlaceCallOutcome.FAILED.value


#: The Hermes ``chat_id`` (== SIP Call-ID) session-context variable name.
_SESSION_CHAT_ID_ENV = "HERMES_SESSION_CHAT_ID"

#: The originating session's PLATFORM session-context variable name (e.g.
#: ``telegram``). Paired with ``_SESSION_CHAT_ID_ENV`` (the originating chat id is
#: the SAME key the inbound path reads as the Call-ID — for a non-VoIP origin it is
#: the platform's chat id, not a SIP Call-ID) to identify the operator origin a
#: proactive ``place_call`` came from. Mirrors ``adapter._SESSION_PLATFORM_KEY``.
_SESSION_PLATFORM_ENV = "HERMES_SESSION_PLATFORM"

#: Opt-in env gate (issue #202): a comma-separated list of ``platform:chat_id``
#: origins permitted to trigger a PROACTIVE ``place_call`` (one originating from a
#: NON-VoIP session — e.g. a Telegram chat turn — where there is no live SIP call in
#: scope). EMPTY / unset (the default) => the privilege gate stays fully fail-safe
#: (level 0) in the no-live-call branch, i.e. byte-identical to the pre-#202
#: behaviour. See :func:`_proactive_place_call_allowed`.
_PROACTIVE_CALL_FROM_ENV = "HERMES_VOIP_PROACTIVE_CALL_FROM"

#: The agent-facing tool name. ``hang_up`` (not ``end_call``) matches the verb the
#: persona preambles name, so the model invokes the tool it is told about.
HANG_UP_TOOL_NAME = "hang_up"

#: The toolset the VoIP tools register under (groups them in the registry).
VOIP_TOOLSET = "voip"

#: The JSON schema the model reads to call ``hang_up``. No parameters: the call to
#: end is the current session's call, resolved from the session context — the
#: model cannot (and must not) target an arbitrary other call.
HANG_UP_TOOL_SCHEMA: dict[str, object] = {
    "name": HANG_UP_TOOL_NAME,
    "description": (
        "End the current phone call. Use this when the conversation has naturally "
        "concluded or the caller says goodbye. The line is hung up immediately; "
        "no further audio reaches the caller afterwards."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

#: ``hold_call`` — ELEVATED (ADR-0011). Reversible; needs privilege, no confirmation.
HOLD_TOOL_NAME = "hold_call"

#: ``resume_call`` — ELEVATED (ADR-0011). The inverse of ``hold_call``.
RESUME_TOOL_NAME = "resume_call"

#: ``list_registrations`` — ELEVATED (ADR-0020). Read-only but discloses internal
#: extension metadata, so it is clamped to a privileged session.
LIST_REGISTRATIONS_TOOL_NAME = "list_registrations"

#: ``hold_call`` schema. No parameters: the call to hold is the current session's
#: call, resolved from the session context — the model cannot target another call.
HOLD_TOOL_SCHEMA: dict[str, object] = {
    "name": HOLD_TOOL_NAME,
    "description": (
        "Place the current caller on hold (they stop hearing you and you stop "
        "hearing them) — for example while you check something. Use resume_call to "
        "bring them back. Only available on a privileged call."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

#: ``resume_call`` schema. No parameters: it resumes the current session's call.
RESUME_TOOL_SCHEMA: dict[str, object] = {
    "name": RESUME_TOOL_NAME,
    "description": (
        "Resume the current caller after a hold (re-establish two-way audio). Use "
        "this to return to a caller you placed on hold with hold_call. Only "
        "available on a privileged call."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

#: ``list_registrations`` schema. No parameters: it reports the gateway's own
#: registration status (a process-wide read, not a per-call action).
LIST_REGISTRATIONS_TOOL_SCHEMA: dict[str, object] = {
    "name": LIST_REGISTRATIONS_TOOL_NAME,
    "description": (
        "List the phone extensions this system is registered as and whether each is "
        "currently online. Only available on a privileged call (it discloses "
        "internal extension details). Takes no input."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

#: ``place_call`` — IRREVERSIBLE (ADR-0029): the agent places an OUTBOUND call to a
#: target on the operator's allowlist, with a per-call objective. Operator-only
#: (privilege level 3, non-degraded); the dial target must be on
#: ``HERMES_VOIP_OUTBOUND_ALLOW`` (the hard gate).
PLACE_CALL_TOOL_NAME = "place_call"

#: ``report_call_result`` — SAFE (ADR-0029): the call agent records the outcome of
#: ITS OWN call before hanging up, so the originating conversation can be told how
#: the call went. Resolved from the session context (chat_id == Call-ID), so the
#: agent can only report on the call it is currently handling.
REPORT_RESULT_TOOL_NAME = "report_call_result"

#: ``place_call`` schema. ``number`` is the dial target (an extension or SIP URI on
#: the operator's allowlist); ``objective`` is the goal of the call (e.g. "book a
#: table for two at 7pm"). Both are required: an objectiveless call would open mutely
#: with no reason to give the callee. The tool returns the new call's id immediately
#: — the call then runs as its own concurrent conversation (ADR-0029).
PLACE_CALL_TOOL_SCHEMA: dict[str, object] = {
    "name": PLACE_CALL_TOOL_NAME,
    "description": (
        "Place an outbound phone call to a permitted number to accomplish a specific "
        "objective on the operator's behalf (for example, calling a restaurant to "
        "book a table). The number MUST be one the operator has pre-approved; an "
        "un-approved number is refused. Provide a clear, self-contained objective — "
        "the call runs as its own conversation that opens with that goal, and you "
        "will be told the outcome when it finishes. Returns the call id; the call "
        "proceeds in the background (this does not wait for the call to end). Only "
        "available to the operator on a trusted, healthy session. NEVER include the "
        "operator's private credentials or secrets in the objective — the person you "
        "call is untrusted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "number": {
                "type": "string",
                "description": (
                    "The dial target: an extension or SIP URI the operator has "
                    "approved for outbound calling."
                ),
            },
            "objective": {
                "type": "string",
                "description": (
                    "The goal of the call, stated as a self-contained task (e.g. "
                    "'book a table for two at 7pm tonight under the name Smith'). "
                    "Must not contain operator secrets."
                ),
            },
        },
        "required": ["number", "objective"],
        "additionalProperties": False,
    },
}

#: ``report_call_result`` schema. ``summary`` is a short outcome the originating
#: conversation is told (e.g. "Booked for 7pm, 2 people"). The call to report on is
#: the current session's own call (resolved from the session context) — the model
#: cannot target another call.
REPORT_RESULT_TOOL_SCHEMA: dict[str, object] = {
    "name": REPORT_RESULT_TOOL_NAME,
    "description": (
        "Record the outcome of THIS call so the conversation that requested it can "
        "be told how it went. Call this once the objective is resolved (succeeded or "
        "not), just before ending the call. Provide a short, factual summary of the "
        "result (for example, 'Booked a table for two at 7pm under Smith' or 'They "
        "are fully booked tonight')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "A short, factual summary of the call's outcome.",
            },
        },
        "required": ["summary"],
        "additionalProperties": False,
    },
}

#: ``send_dtmf`` — ELEVATED (ADR-0031): transmit in-call DTMF (RFC 4733
#: telephone-event) on the CURRENT call's media stream. Reversible (a tone), but a
#: mutating action gated to a privileged (level >= 2, non-degraded) session.
SEND_DTMF_TOOL_NAME = "send_dtmf"

#: ``open_entry`` — ELEVATED (ADR-0031): actuate the intercom entry (open the door)
#: for a legitimate expected visitor. Exposed to the intercom caller group only (its
#: ``allowed_tools`` sub-ceiling), so it never reaches a general caller; gated so a
#: level-0 caller cannot open the door.
OPEN_ENTRY_TOOL_NAME = "open_entry"

#: Maximum ``send_dtmf`` digit-string length. The engine sends one digit at a time
#: under the call's tx lock (~170ms/digit), so an unbounded string would wedge the
#: live call's outbound audio for the whole send (a DoS at the ELEVATED tier). 64 is
#: generous for every legitimate use — a card number + expiry + CVV + ZIP with pause
#: digits is ~30 — while capping the wedge at a few seconds. Enforced both as the
#: schema ``maxLength`` (advisory) and as a runtime check in ``send_dtmf_handler``.
_MAX_DTMF_DIGITS = 64

#: ``send_dtmf`` schema. ``digits`` is the DTMF string to send (``0-9``, ``*``,
#: ``#``, ``A``-``D``), capped at ``_MAX_DTMF_DIGITS`` characters. The call to send on
#: is the current session's own call (resolved from the session context) — the model
#: cannot target another call.
SEND_DTMF_TOOL_SCHEMA: dict[str, object] = {
    "name": SEND_DTMF_TOOL_NAME,
    "description": (
        "Send DTMF tones (touch-tone key presses) on the current phone call — for "
        "navigating an automated menu ('press 1 for…') or entering a code you have "
        "been asked to. Provide the digits as a string (allowed characters: 0-9, "
        "* , #, A-D). Only available on a privileged call. NEVER enter the "
        "operator's private codes or card numbers unless the operator explicitly "
        "authorised this specific entry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "digits": {
                "type": "string",
                "maxLength": _MAX_DTMF_DIGITS,
                "description": (
                    "The DTMF digits to send, e.g. '1' or '1234#'. Allowed "
                    "characters: 0-9, *, #, A-D."
                ),
            },
        },
        "required": ["digits"],
        "additionalProperties": False,
    },
}

#: ``open_entry`` schema. An OPTIONAL ``name`` selects WHICH named opening to actuate
#: (ADR-0045): an intercom may have several (door / gate / garage). The call's context
#: tells the agent which names are available; ``name`` must be one of them (the adapter
#: rejects any other), and may be omitted for a legacy single-opening intercom. The
#: call to open on is the session's own call — never model-chosen.
OPEN_ENTRY_TOOL_SCHEMA: dict[str, object] = {
    "name": OPEN_ENTRY_TOOL_NAME,
    "description": (
        "Open an entry (unlock the door / gate / garage) for the visitor on this "
        "intercom call. Use this ONLY for a legitimate, expected visitor after you "
        "have confirmed who they are and that they are expected — opening an entry is "
        "a physical-access action. If this intercom has more than one named entry "
        "(shown in the call context), pass 'name' to choose which one to open; with a "
        "single entry you may omit it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Which named entry to open (e.g. 'door', 'gate', 'garage') — must "
                    "be one of the openings shown in this call's context. Omit for an "
                    "intercom with a single entry."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

#: ``transfer_blind`` — IRREVERSIBLE (ADR-0010/0011/0031): hand the CURRENT caller to
#: another extension / SIP URI via a blind REFER (RFC 3515). Operator-only (privilege
#: level 3, non-degraded). Its spoof-resistant safeguard is the ADR-0010 DTMF
#: confirmation: the REFER fires ONLY after the person on the call presses the armed
#: confirm digit (``transfer_blind_on_call`` awaits the per-call ArmedConfirmation),
#: so a missed prompt injection cannot transfer the caller on a "yes" alone. The
#: ``target`` is model-chosen and deliberately NOT allow-listed (unlike ``place_call``,
#: whose new untrusted outbound leg has no human in the loop): the operator-level party
#: on the live call confirming the keypad press IS the per-call, per-target
#: authorization (ADR-0031 alternatives — a destination allowlist is a recorded future
#: hardening, not shipped). ``transfer_attended`` is fully exposed and wired
#: (plugin.yaml, adapter.py); ADR-0048 (closed 2026-06-18) delivered the
#: consultative-transfer implementation end-to-end.
TRANSFER_BLIND_TOOL_NAME = "transfer_blind"

#: ``transfer_blind`` schema. ``target`` is the destination (an extension or SIP URI)
#: the current caller is handed to. The call being transferred is the session's own
#: call (resolved from the session context) — the model picks the destination, never
#: which call to transfer. The transfer is confirmed by a keypad press on the call
#: before it fires (see the description); an un-confirmed request transfers nobody.
TRANSFER_BLIND_TOOL_SCHEMA: dict[str, object] = {
    "name": TRANSFER_BLIND_TOOL_NAME,
    "description": (
        "Transfer the current caller to another extension or SIP address (a blind "
        "transfer — you hand them off and leave the call). Provide the destination "
        "as 'target'. Only available to the operator on a trusted, healthy call. "
        "IMPORTANT: the transfer does NOT happen on your say-so alone — the person on "
        "the call is asked to press a key on their phone to confirm, and the transfer "
        "only goes through if they do. If they do not confirm, nobody is transferred."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "The transfer destination: an extension (e.g. '1001') or a SIP "
                    "URI (e.g. 'sip:1001@pbx.example.test')."
                ),
            },
        },
        "required": ["target"],
        "additionalProperties": False,
    },
}

#: ``transfer_attended`` — IRREVERSIBLE (ADR-0048): a CONSULTATIVE transfer. Unlike a
#: blind transfer, the agent first CALLS the target (a consultation leg), converses,
#: and only then COMPLETES the transfer with a REFER carrying ``Replaces`` (RFC 3891)
#: so the original caller is bridged to the target. Operator-only (privilege level 3,
#: non-degraded). Its spoof-resistant safeguard is the SAME static
#: ``HERMES_VOIP_OUTBOUND_ALLOW`` allowlist as ``place_call``: the consultation dials a
#: NEW untrusted outbound leg, so the dial chokepoint refuses any unlisted target
#: (the completing REFER only bridges the already-allowlisted leg). A single tool
#: drives the consult -> complete/cancel state machine via its ``action`` argument.
TRANSFER_ATTENDED_TOOL_NAME = "transfer_attended"

#: ``transfer_attended`` schema. ``action`` selects the step: ``consult`` (call the
#: target — ``target`` required), ``complete`` (bridge the caller to the target via
#: REFER+Replaces), or ``cancel`` (abandon the consultation, keep the caller). The
#: call being transferred is the session's own call (resolved from the session
#: context) — the model picks only the destination + step, never which call.
TRANSFER_ATTENDED_TOOL_SCHEMA: dict[str, object] = {
    "name": TRANSFER_ATTENDED_TOOL_NAME,
    "description": (
        "Transfer the current caller to another extension or SIP address with a "
        "CONSULTATION first (an attended transfer): you call the destination and "
        "speak to them, then connect the caller through. Use 'action' to drive it: "
        "'consult' (with 'target') calls the destination so you can talk to them; "
        "'complete' connects the caller to that destination and drops you out; "
        "'cancel' hangs up the consultation and returns you to the caller. Only "
        "available to the operator on a trusted, healthy call, and only to a "
        "pre-approved destination — an un-approved number is refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["consult", "complete", "cancel"],
                "description": (
                    "The step: 'consult' to call the destination first, 'complete' "
                    "to connect the caller through, or 'cancel' to abandon it."
                ),
            },
            "target": {
                "type": "string",
                "description": (
                    "The transfer destination for 'consult': an extension (e.g. "
                    "'1001') or a SIP URI (e.g. 'sip:1001@pbx.example.test'). It must "
                    "be a destination the operator has pre-approved."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


@runtime_checkable
class _VoipOwnedPlatformSource(Protocol):
    """Optional adapter surface exposing VoIP-owned Hermes platform names."""

    def voip_owned_platform_names(self) -> frozenset[str]:
        """Return the plugin-owned platform names that identify VoIP call sessions."""
        ...


@runtime_checkable
class VoipToolHost(Protocol):
    """The adapter surface the VoIP tools drive (``VoipAdapter`` satisfies it).

    A narrow Protocol so this module needs no concrete import of (or dependency
    on) the hermes-importing adapter: the handlers reach the live call through
    just these members. Each ``*_call`` method resolves the live
    :class:`~hermes_voip.call.CallSession` for the Call-ID itself and returns
    whether it acted (``False`` for an unknown/ended call) so a handler never
    raises on a stale call.
    """

    def guard_state_for(self, call_id: str) -> GuardSessionState | None:
        """Return the per-call guard state, or ``None`` if the call is unknown."""
        ...

    async def hang_up_call(self, call_id: str) -> bool:
        """End the call (SOFT agent hangup); return whether a call was ended."""
        ...

    async def hold_call(self, call_id: str) -> bool:
        """Place the call on hold (re-INVITE); return whether a call was held."""
        ...

    async def resume_call(self, call_id: str) -> bool:
        """Resume the held call (re-INVITE); return whether a call was resumed."""
        ...

    def list_registrations_text(self) -> str:
        """Return a human-readable snapshot of the gateway registrations."""
        ...

    async def place_call_with_objective(
        self,
        number: str,
        objective: str,
        *,
        ring_timeout_secs: float | None = None,
    ) -> str:
        """Place an outbound call pursuing ``objective``; return the new Call-ID.

        Captures the originating session (for result reporting) and enforces the
        outbound allowlist BEFORE dialling. Raises
        :class:`~hermes_voip.originate.OutboundCallNotAllowed` when ``number`` is not
        permitted (so the handler reports a clear refusal and nothing is dialled).
        Returns immediately once the call loop is running (the call proceeds in the
        background) — it does NOT await the whole call.

        Args:
            number: The dial target (extension or SIP URI) — must be allowlisted.
            objective: The goal of the call, framed to the call agent.
            ring_timeout_secs: When set, the maximum time to ring an unanswered
                call before sending a CANCEL (ADR-0069); raises
                :class:`~hermes_voip.originate.OutboundCallCancelled` on expiry.
                ``None`` (the default) leaves only the adapter's hard sink bound.
        """
        ...

    def record_call_result(self, call_id: str, summary: str) -> bool:
        """Record the agent's outcome summary for ``call_id``; return whether stored.

        ``False`` for an unknown/ended call so the tool reports a clear, non-fatal
        outcome rather than raising.
        """
        ...

    async def send_dtmf_on_call(self, call_id: str, digits: str) -> bool:
        """Send ``digits`` as in-call DTMF on ``call_id`` (ADR-0031).

        Returns whether it acted (``False`` for an unknown/ended call). Raises if
        the call negotiated no telephone-event payload type (the handler renders that
        as a clear tool error) — DTMF is never silently dropped.
        """
        ...

    async def open_entry(self, call_id: str, name: str | None = None) -> bool:
        """Actuate the intercom entry for ``call_id`` (open the door — ADR-0031/0045).

        ``name`` selects WHICH named opening to actuate on a multi-intercom call
        (door / gate / garage), scoped to the calling intercom's set; ``None`` defaults
        to the sole opening / the legacy single-intercom path. Returns whether it acted
        (``False`` for an unknown/ended call). Raises :class:`ValueError` for a name
        outside the calling intercom's set (the handler renders that as a clear error).
        The actuation path (in-call DTMF or an external webhook/relay) is the adapter's
        concern; the tool only requests the entry on the current call.
        """
        ...

    async def transfer_blind_on_call(self, call_id: str, target: str) -> TransferResult:
        """Blind-transfer ``call_id`` to ``target`` (DTMF-confirmed; ADR-0031/0109).

        The spoof-resistant chokepoint: awaits the call's per-call
        :class:`~hermes_voip.dtmf_confirm.ArmedConfirmation` (prompts the person on
        the call to press the confirm digit) and sends the RFC 3515 REFER via
        :meth:`hermes_voip.call.CallSession.transfer_blind` **only** when the caller
        confirms; then (ADR-0109) waits bounded for the terminal transfer-progress
        NOTIFY and classifies the real outcome. Returns a :class:`TransferResult`
        whose ``outcome`` is:

        * ``COMPLETED`` — confirmed + a terminal 2xx NOTIFY; the caller reached target.
        * ``FAILED`` — confirmed + a terminal 3xx-6xx NOTIFY (``notify_status`` +
          ``notify_reason`` carry the SIP status); the transfer did not complete.
        * ``OUTCOME_UNKNOWN`` — confirmed but no terminal NOTIFY within the wait.
        * ``UNCONFIRMED`` — a wrong digit / timeout; no REFER.
        * ``NO_CALL`` — the call was unknown or had already ended; no REFER.
        * ``BLOCKED`` — the chokepoint's own privilege re-check refused it; no REFER.

        Raises (never a silent no-op — rule 37):

        * ``RuntimeError`` — the call has no bound confirmation channel (it negotiated
          no telephone-event, so a spoof-resistant keypad confirmation is impossible).
        * :class:`~hermes_voip.call.CallError` — the gateway rejected the REFER.
        """
        ...

    async def start_attended_consult(self, call_id: str, target: str) -> str:
        """Originate the CONSULTATION leg of an attended transfer (ADR-0048).

        Re-enforces the operator-level + non-degraded clamp itself (defense in depth),
        then dials ``target`` via the outbound origination path — gated by the SAME
        ``HERMES_VOIP_OUTBOUND_ALLOW`` allowlist as ``place_call`` — and records the
        ``call_id -> consult_call_id`` pairing. Returns the consult leg's Call-ID.

        Raises (never a silent no-op — rule 37):

        * :class:`~hermes_voip.originate.OutboundCallNotAllowed` — ``target`` is not on
          the outbound allowlist (no leg is dialled).
        * ``PermissionError`` — the original call is not operator-level / is degraded.
        * ``KeyError`` — the original call is unknown.
        * :class:`~hermes_voip.originate.OutboundCallFailed` — the consult dial reached
          the gateway but did not establish (busy slot / no registered ext / WSS).
        * ``RuntimeError`` — the transport/manager is not initialised, OR a second
          consultation was requested while one is already in flight for this call.
        """
        ...

    async def complete_attended_transfer(self, call_id: str) -> AttendedTransferResult:
        """COMPLETE the attended transfer for ``call_id`` (ADR-0048/0109).

        Sends the REFER on the ORIGINAL call naming the paired consultation dialog
        (RFC 3891 ``Replaces``), so the gateway bridges the caller to the target and
        releases our legs; clears the pairing. Re-enforces the privilege clamp itself,
        then (ADR-0109) waits bounded for the terminal NOTIFY and classifies the real
        outcome. Returns an :class:`AttendedTransferResult` (``COMPLETED`` / ``FAILED``
        with the SIP status / ``OUTCOME_UNKNOWN`` after the REFER, or the pre-REFER
        ``NO_CONSULT`` / ``NO_CALL`` / ``BLOCKED``). Raises
        :class:`~hermes_voip.call.CallError` if the gateway rejects the REFER.
        """
        ...

    async def cancel_attended_transfer(self, call_id: str) -> bool:
        """Abandon the consultation for ``call_id`` (ADR-0048); whether it acted.

        Hangs up the consultation leg (BYE) and keeps the original caller, clearing
        the pairing. Returns ``False`` (and does nothing) when no consultation is in
        flight, so the tool reports a clear, non-fatal outcome.
        """
        ...


class _RegisterTool(Protocol):
    """The ``ctx.register_tool`` surface this module calls (narrow, only our args).

    ``handler`` is typed ``object`` (not a precise ``Callable``): the runtime only
    stores it for later dispatch, so a precise call signature here would add no
    safety and would force a ``...``-Callable (an explicit ``Any`` under our strict
    config). The concrete handler we pass — :func:`hang_up_handler` — is a real
    async callable; the registry calls it, not this Protocol.
    """

    def __call__(  # noqa: PLR0913 — mirrors hermes-agent's register_tool arity (the args we pass)
        self,
        name: str,
        toolset: str,
        schema: dict[str, object],
        handler: object,
        *,
        is_async: bool,
        description: str,
        emoji: str,
    ) -> None:
        """Register a tool in the runtime's registry."""
        ...


class _RegisterHook(Protocol):
    """The ``ctx.register_hook`` surface this module calls (narrow).

    ``callback`` is typed ``object`` for the same reason as ``_RegisterTool``'s
    ``handler``: the runtime stores it for later invocation.
    """

    def __call__(self, hook_name: str, callback: object) -> None:
        """Register a lifecycle-hook callback."""
        ...


# The single live adapter for this gateway process. Set by ``VoipAdapter.connect``
# (and cleared by ``disconnect``) so the tool handler can reach the per-call
# session map. There is exactly one voip adapter per process; a module global is
# the simplest correct seam (the alternative — threading the adapter through the
# plugin-load-time ``register(ctx)`` — is impossible because the adapter is built
# later, by the factory, which never sees ``ctx``).
_ACTIVE_ADAPTER: VoipToolHost | None = None


def set_active_adapter(adapter: VoipToolHost | None) -> None:
    """Register (or clear) the live adapter the VoIP tools operate on (ADR-0026)."""
    global _ACTIVE_ADAPTER  # noqa: PLW0603 — single process-wide adapter seam
    _ACTIVE_ADAPTER = adapter


def active_voip_adapter() -> VoipToolHost | None:
    """Return the live adapter the VoIP tools operate on, or ``None`` if unset.

    Lets the adapter clear the seam on disconnect only when it still points at
    itself (a later adapter may have superseded it).
    """
    return _ACTIVE_ADAPTER


def _current_call_id() -> str | None:
    """Read the current Hermes session's chat_id (the SIP Call-ID), or ``None``.

    Reads the task-local ``HERMES_SESSION_CHAT_ID`` from ``gateway.session_context``
    (imported lazily so this module stays hermes-free at import time). ``None``
    when the runtime is absent or no session is in scope.
    """
    # FAIL CLOSED: this feeds voip_pre_tool_call, the privilege gate the Hermes
    # runtime calls before EVERY VoIP tool invocation — the same deliberate
    # fail-closed security boundary as _proactive_place_call_allowed below. ANY
    # failure to resolve the session (runtime absent, or get_session_env raising
    # for a reason other than the module being missing) must return None here,
    # never raise out of the gate.
    try:
        from gateway.session_context import get_session_env  # noqa: PLC0415

        value = get_session_env(_SESSION_CHAT_ID_ENV)
    except Exception:  # noqa: BLE001 — deliberate fail-closed security boundary
        return None
    return value or None


def _static_voip_owned_platforms() -> frozenset[str]:
    """Return the VoIP platform names registered by ``plugin.register`` (ADR-0035)."""
    from hermes_voip.plugin import (  # noqa: PLC0415 -- light module; avoids cycles
        _PLATFORM_NAME,
        channel_platform_names,
    )

    return frozenset((_PLATFORM_NAME, *channel_platform_names()))


def _voip_owned_platforms() -> frozenset[str]:
    """Return platform names that identify this plugin's own VoIP call sessions.

    The static set is the primary platform plus every caller-group channel registered
    by ``plugin.register``. A real adapter may add operator-defined caller-group
    channels loaded from config; include them when the active adapter exposes them so
    the proactive gate cannot drift behind runtime routing.
    """
    owned: set[str] = set(_static_voip_owned_platforms())
    # Widen to ``object`` before the structural check: with the real hermes types
    # (the hermes-contract mypy step, ``--extra hermes``) mypy proves ``VoipToolHost``
    # can never also satisfy the disjoint ``_VoipOwnedPlatformSource`` protocol and
    # marks the body unreachable under ``warn_unreachable``. The isinstance is a
    # genuine RUNTIME check (an adapter MAY expose the optional surface), so widen the
    # static type to ``object`` — a plain widening, not a cast/Any/type-ignore — and
    # let isinstance narrow ``object`` → ``_VoipOwnedPlatformSource`` cleanly.
    adapter: object = _ACTIVE_ADAPTER
    if isinstance(adapter, _VoipOwnedPlatformSource):
        owned.update(adapter.voip_owned_platform_names())
    return frozenset(owned)


class ProactiveDenyReason(StrEnum):
    """Why a proactive (no-live-call) ``place_call`` grant was refused (issue #414).

    A NON-SENSITIVE diagnostic category for an operator inspecting why a proactive
    outbound call was blocked. Derived from the existing fail-closed branches (not
    invented) so a log line tells the operator the actionable cause WITHOUT leaking
    any origin value (platform / chat_id / ``HERMES_VOIP_PROACTIVE_CALL_FROM``
    contents are secrets and never appear here). ``ALLOWED`` is the single member
    meaning the relaxation granted; every other member is a distinct denial cause:

    * ``ALLOWED`` — the origin matched; the proactive relaxation grants level 3.
    * ``PROACTIVE_ALLOW_UNSET`` — ``HERMES_VOIP_PROACTIVE_CALL_FROM`` is empty/unset
      (the fail-safe default; proactive calling is not opted in).
    * ``ORIGIN_UNAVAILABLE`` — the originating ``(platform, chat_id)`` could not be
      read from ``gateway.session_context`` (runtime absent, context unavailable, or
      the reader raised — all fail closed).
    * ``ORIGIN_NOT_ALLOWLISTED`` — the origin was read but its ``platform:chat_id``
      matches no configured entry (exact or wildcard).
    * ``VOIP_ORIGIN_NOT_PROACTIVE`` — the origin is a VoIP-call session, which is
      never a proactive operator origin; deny regardless of allowlist contents (the
      code-enforced inbound fail-safe).
    * ``UNSUPPORTED_TOOL_FOR_PROACTIVE_ORIGIN`` — the tool is not ``place_call`` (the
      relaxation is place_call-scoped; transfer/dtmf/open_entry stay blocked).
    """

    ALLOWED = "allowed"
    PROACTIVE_ALLOW_UNSET = "proactive_allow_unset"
    ORIGIN_UNAVAILABLE = "origin_unavailable"
    ORIGIN_NOT_ALLOWLISTED = "origin_not_allowlisted"
    VOIP_ORIGIN_NOT_PROACTIVE = "voip_origin_not_proactive"
    UNSUPPORTED_TOOL_FOR_PROACTIVE_ORIGIN = "unsupported_tool_for_proactive_origin"


@dataclass(frozen=True, slots=True)
class ProactiveDecision:
    """The proactive-relaxation decision: whether to grant + the structured reason.

    Immutable audit record (an operator-facing decision must not be mutated after it
    is produced), mirroring ADR-0085's ``GateDecision``. Invariant: ``allowed`` is
    ``True`` iff ``reason is ProactiveDenyReason.ALLOWED``.
    """

    allowed: bool
    reason: ProactiveDenyReason


def _proactive_place_call_allowed(tool_name: str) -> ProactiveDecision:
    """The proactive (no-live-call) ``place_call`` relaxation decision for #202/#414.

    The opt-in relaxation for issue #202: when the privilege gate has NO live SIP
    call in scope (so it would otherwise fall back to a least-privilege level-0
    state), permit ``place_call`` — and ONLY ``place_call`` — when the originating
    ``(platform, chat_id)`` matches a ``HERMES_VOIP_PROACTIVE_CALL_FROM`` entry. This
    lets the operator drive a proactive outbound call from a NON-VoIP session (e.g. a
    Telegram chat: "call me with a brief"), which the design already anticipated —
    ``adapter._capture_origin_session`` captures the same origin and
    ``HERMES_VOIP_OUTBOUND_RESULT_CHANNEL`` reports the outcome back to it.

    Returns ``ProactiveDecision(allowed=True, reason=ALLOWED)`` ONLY when ALL hold;
    otherwise ``allowed`` is ``False`` and ``reason`` is the specific fail-closed
    cause (issue #414 — a non-sensitive diagnostic category, never an origin value):

    * the tool is EXACTLY ``place_call`` — ``transfer_blind`` / ``send_dtmf`` /
      ``open_entry`` are meaningless without a live call and stay blocked in the
      no-call branch (``UNSUPPORTED_TOOL_FOR_PROACTIVE_ORIGIN``; place_call-scoped);
    * ``HERMES_VOIP_PROACTIVE_CALL_FROM`` is set (EMPTY / unset =>
      ``PROACTIVE_ALLOW_UNSET``, so the gate is byte-identical to the fail-safe
      default);
    * the originating ``(platform, chat_id)`` is readable from
      ``gateway.session_context`` (else ``ORIGIN_UNAVAILABLE``);
    * the platform is NOT one this VoIP plugin owns (else
      ``VOIP_ORIGIN_NOT_PROACTIVE`` — a VoIP call session is never a proactive
      operator origin, regardless of allowlist contents);
    * its ``platform:chat_id`` is one of the configured entries (else
      ``ORIGIN_NOT_ALLOWLISTED``).

    This NEVER weakens the inbound fail-safe: it is consulted only when guard
    ``state is None``, and then code-denies every VoIP-owned platform before matching
    the operator allowlist. So a misconfigured ``voip:*`` (or a channel alias) cannot
    authorize a guard-missing inbound caller. The static ``HERMES_VOIP_OUTBOUND_ALLOW``
    allowlist (ADR-0029) still enforces the dial target at the chokepoint regardless,
    so even a misconfigured operator origin can only reach pre-approved numbers.
    """
    if tool_name != PLACE_CALL_TOOL_NAME:
        return ProactiveDecision(
            allowed=False,
            reason=ProactiveDenyReason.UNSUPPORTED_TOOL_FOR_PROACTIVE_ORIGIN,
        )
    allowed = os.environ.get(_PROACTIVE_CALL_FROM_ENV, "")
    if not allowed:
        # opt-in unset => fully fail-safe (the default)
        return ProactiveDecision(
            allowed=False, reason=ProactiveDenyReason.PROACTIVE_ALLOW_UNSET
        )
    # FAIL CLOSED: this is a privilege-gate decision, so ANY failure to resolve the
    # originating session (runtime absent, context unavailable/misconfigured) denies
    # — it must never raise out of the gate (which could bypass denial handling or
    # break unrelated tool calls) nor grant on an unresolved origin.
    try:
        from gateway.session_context import get_session_env  # noqa: PLC0415

        platform = get_session_env(_SESSION_PLATFORM_ENV)
        chat_id = get_session_env(_SESSION_CHAT_ID_ENV)
    except Exception:  # noqa: BLE001 — deliberate fail-closed security boundary
        return ProactiveDecision(
            allowed=False, reason=ProactiveDenyReason.ORIGIN_UNAVAILABLE
        )
    if not platform or not chat_id:
        return ProactiveDecision(
            allowed=False, reason=ProactiveDenyReason.ORIGIN_UNAVAILABLE
        )
    if platform in _voip_owned_platforms():
        # Code-enforced inbound fail-safe (ADR-0105): a VoIP-call session is NEVER a
        # proactive operator origin — the relaxation is for NON-VoIP sessions only
        # (ADR-0074). Deny regardless of HERMES_VOIP_PROACTIVE_CALL_FROM contents so a
        # misconfigured ``voip:*`` (or exact ``voip:<Call-ID>``) cannot let a
        # guard-missing inbound SIP caller reach place_call. This is the exact,
        # platform-based form of the boundary the removed call_id-is-None check only
        # approximated; it cannot break the legitimate non-VoIP proactive turn.
        return ProactiveDecision(
            allowed=False, reason=ProactiveDenyReason.VOIP_ORIGIN_NOT_PROACTIVE
        )
    needle = f"{platform}:{chat_id}"
    entries = {entry.strip() for entry in allowed.split(",") if entry.strip()}
    # Wildcard opt-in (issue #355): entries containing ``*`` are matched with
    # fnmatch so operators can write ``telegram:*`` to allow any Telegram origin
    # without enumerating every chat id. Non-wildcard entries continue to use the
    # exact set above. The fail-closed contract is unchanged: any unresolved origin
    # still denies; only a positively-matching pattern grants.
    import fnmatch  # noqa: PLC0415 -- lazy; stdlib, negligible cost

    matched = needle in entries or any(
        fnmatch.fnmatchcase(needle, entry) for entry in entries if "*" in entry
    )
    return ProactiveDecision(
        allowed=matched,
        reason=(
            ProactiveDenyReason.ALLOWED
            if matched
            else ProactiveDenyReason.ORIGIN_NOT_ALLOWLISTED
        ),
    )


async def hang_up_handler(
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: end the current call (SOFT agent hangup, ADR-0026).

    Resolves the call from the Hermes session context (its ``chat_id`` is the SIP
    Call-ID) and ends it via the live adapter. Returns a JSON string (the tool
    result contract): ``{"result": ...}`` on success, ``{"error": ...}`` when no
    adapter/call is in scope (so the model sees a clear, non-fatal outcome rather
    than a crash). ``args`` is ignored — the tool takes no parameters; the call to
    end is fixed to the session's own call (the model cannot target another call).
    """
    _ = args  # the tool takes no parameters
    try:
        adapter = _ACTIVE_ADAPTER
        if adapter is None:
            return json.dumps({"error": "no active VoIP adapter; cannot end the call"})
        call_id = _current_call_id()
        if call_id is None:
            return json.dumps({"error": "no active call in this session to end"})
        ended = await adapter.hang_up_call(call_id)
        if not ended:
            return json.dumps({"error": "the call has already ended"})
        return json.dumps({"result": "Call ended."})
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure)
        return _tool_failure(HANG_UP_TOOL_NAME, exc)


async def hold_call_handler(
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: place the current caller on hold (ELEVATED, ADR-0011).

    Resolves the call from the Hermes session context and holds it via the live
    adapter (re-INVITE ``sendonly``). Returns the JSON tool-result contract:
    ``{"result": ...}`` on success, ``{"error": ...}`` when no adapter/call is in
    scope or the call already ended. ``args`` is ignored — the held call is fixed
    to the session's own call. The ``pre_tool_call`` gate has already enforced the
    ELEVATED privilege clamp before this runs.
    """
    _ = args  # the tool takes no parameters
    try:
        adapter = _ACTIVE_ADAPTER
        if adapter is None:
            return json.dumps({"error": "no active VoIP adapter; cannot hold the call"})
        call_id = _current_call_id()
        if call_id is None:
            return json.dumps({"error": "no active call in this session to hold"})
        held = await adapter.hold_call(call_id)
        if not held:
            return json.dumps({"error": "the call is not active (unknown or ended)"})
        return json.dumps({"result": "Caller placed on hold."})
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure)
        return _tool_failure(HOLD_TOOL_NAME, exc)


async def resume_call_handler(
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: resume the held caller (ELEVATED, ADR-0011).

    Resolves the call from the Hermes session context and resumes it via the live
    adapter (re-INVITE ``sendrecv``). Same JSON tool-result contract and fail-safe
    behaviour as :func:`hold_call_handler`; the ``pre_tool_call`` gate has already
    enforced the ELEVATED privilege clamp.
    """
    _ = args  # the tool takes no parameters
    try:
        adapter = _ACTIVE_ADAPTER
        if adapter is None:
            return json.dumps(
                {"error": "no active VoIP adapter; cannot resume the call"}
            )
        call_id = _current_call_id()
        if call_id is None:
            return json.dumps({"error": "no active call in this session to resume"})
        resumed = await adapter.resume_call(call_id)
        if not resumed:
            return json.dumps({"error": "the call is not active (unknown or ended)"})
        return json.dumps({"result": "Caller resumed."})
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure)
        return _tool_failure(RESUME_TOOL_NAME, exc)


async def list_registrations_handler(
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: list the gateway registrations (ELEVATED, ADR-0020).

    A **process-wide** read of the registration manager (not a per-call action),
    so it does not resolve a Call-ID — but the ``pre_tool_call`` gate has already
    clamped it to a privileged *calling* session (it discloses internal extension
    metadata). Returns ``{"result": <snapshot text>}`` on success, ``{"error":
    ...}`` when no adapter is in scope. ``args`` is ignored.
    """
    _ = args  # the tool takes no parameters
    try:
        adapter = _ACTIVE_ADAPTER
        if adapter is None:
            return json.dumps(
                {"error": "no active VoIP adapter; cannot list registrations"}
            )
        return json.dumps({"result": adapter.list_registrations_text()})
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure)
        return _tool_failure(LIST_REGISTRATIONS_TOOL_NAME, exc)


def _str_arg(args: Mapping[str, object] | None, key: str) -> str:
    """Return a trimmed string tool argument, or ``""`` when absent/non-string.

    Tool arguments arrive as an untyped mapping (the model fills them); a missing or
    wrong-typed value is treated as empty so the handler can return a clear error
    rather than dialling/recording garbage.
    """
    if args is None:
        return ""
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


async def place_call_handler(  # noqa: PLR0911 — each return is a distinct, clear branch (no adapter / no number / no objective / not-allowed / busy / declined / no-answer / failed / success); collapsing them would hide which outcome the agent sees
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: place an outbound call pursuing an objective (IRREVERSIBLE).

    Reads ``number`` + ``objective`` from the tool args, then calls the live
    adapter's :meth:`VoipToolHost.place_call_with_objective`. Returns the JSON
    tool-result contract:

    * ``{"call_id": …}`` IMMEDIATELY on success (ASYNC — the call runs as its own
      background conversation; this does NOT await the whole call).
    * ``{"error": …}`` when no adapter is in scope, an argument is missing, or the
      target is not on the allowlist (dial never made — no ``failure_outcome``).
    * ``{"error": …, "failure_outcome": <outcome>}`` when the outbound call fails
      after dialling: the ``failure_outcome`` key carries a
      :class:`PlaceCallOutcome` value string (``"busy"`` / ``"no_answer"`` /
      ``"declined"`` / ``"failed"``) so the agent can branch on WHY the call
      failed. The SIP reason phrase is **never** echoed (it may embed gateway
      host/extension details — rule 34).

    The ``pre_tool_call`` gate has already enforced the operator-level (3) +
    non-degraded privilege clamp before this runs; the allowlist (enforced inside
    ``place_call_with_objective``) is the hard irreversibility gate.

    ``HERMES_VOIP_RING_TIMEOUT_SECS``, when set to a positive float, is forwarded
    as ``ring_timeout_secs`` to arm the ADR-0069 outbound CANCEL timer: if the
    call rings unanswered beyond that bound, the INVITE is CANCELled and the
    adapter raises :class:`~hermes_voip.originate.OutboundCallCancelled`, which
    the handler maps to ``NO_ANSWER``.
    """
    try:
        adapter = _ACTIVE_ADAPTER
        if adapter is None:
            return json.dumps({"error": "no active VoIP adapter; cannot place a call"})
        number = _str_arg(args, "number")
        objective = _str_arg(args, "objective")
        if not number:
            return json.dumps({"error": "place_call requires a 'number' to dial"})
        if not objective:
            return json.dumps(
                {"error": "place_call requires an 'objective' for the call"}
            )
        ring_timeout = _parse_ring_timeout()
        try:
            call_id = await adapter.place_call_with_objective(
                number, objective, ring_timeout_secs=ring_timeout
            )
        except OutboundCallNotAllowed as exc:
            # The hard gate refused the target — surface a clear, non-fatal error; the
            # number was NOT dialled. (The error message names the rejected target.)
            # No ``failure_outcome``: this is a policy refusal before any dial.
            return json.dumps({"error": str(exc)})
        except OutboundCallCancelled:
            # Our own ring-timeout fired (ADR-0069): we sent the CANCEL and the peer
            # returned 487 Request Terminated. This is "no one answered because we
            # stopped waiting", so the outcome is NO_ANSWER. The exception carries the
            # Call-ID and a reason string that is internal (never forwarded — rule 34).
            return json.dumps(
                {
                    "error": "outbound call was not answered (ring timeout)",
                    "failure_outcome": PlaceCallOutcome.NO_ANSWER.value,
                }
            )
        except OutboundCallFailed as exc:
            # A final non-2xx SIP response: classify by status code into a structured
            # outcome the agent can branch on WITHOUT the gateway reason phrase (PII).
            outcome = _classify_outbound_failure(exc)
            return json.dumps(
                {
                    "error": f"outbound call failed: {outcome.value}",
                    "failure_outcome": outcome.value,
                }
            )
        except RuntimeError as exc:
            # A transport/media-initialisation failure (e.g. the RTP transport could
            # not be opened, or the WSS/WebRTC path is unsupported): NOT a SIP final
            # response, but ADR-0086 classifies it as the FAILED outcome so the agent
            # still receives the structured ``failure_outcome`` contract instead of a
            # generic, unstructured error. The exception message can embed gateway
            # connection details (host:port) — so, exactly like the SIP path above
            # suppresses the reason phrase, ``str(exc)`` is NEVER echoed in the
            # agent-facing result (rule 34 / public-repo invariant). The failure is
            # still surfaced to the operator's logs (rule 37), but with only the
            # exception TYPE name (no message, no traceback) so a host/port embedded
            # in the message cannot leak into logs either.
            _log.error(
                "VoIP tool %r: outbound call failed at transport/media init "
                "with a %s (detail redacted)",
                PLACE_CALL_TOOL_NAME,
                type(exc).__name__,
            )
            return json.dumps(
                {
                    "error": f"outbound call failed: {PlaceCallOutcome.FAILED.value}",
                    "failure_outcome": PlaceCallOutcome.FAILED.value,
                }
            )
        return json.dumps({"call_id": call_id})
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure)
        return _tool_failure(PLACE_CALL_TOOL_NAME, exc)


async def report_call_result_handler(
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: record THIS call's outcome for cross-session reporting (SAFE).

    Resolves the call from the Hermes session context (its ``chat_id`` is the SIP
    Call-ID) and records the ``summary`` on the live adapter so the call-end bridge
    can report it to the originating conversation (ADR-0029). Returns the JSON
    tool-result contract: ``{"result": …}`` on success, ``{"error": …}`` when no
    adapter/call is in scope or the call already ended. The call to report on is
    fixed to the session's own call — the model cannot target another call.
    """
    try:
        adapter = _ACTIVE_ADAPTER
        if adapter is None:
            return json.dumps(
                {"error": "no active VoIP adapter; cannot record the result"}
            )
        call_id = _current_call_id()
        if call_id is None:
            return json.dumps({"error": "no active call in this session to report on"})
        summary = _str_arg(args, "summary")
        if not summary:
            return json.dumps({"error": "report_call_result requires a 'summary'"})
        recorded = adapter.record_call_result(call_id, summary)
        if not recorded:
            return json.dumps({"error": "the call is not active (unknown or ended)"})
        return json.dumps({"result": "Outcome recorded."})
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure)
        return _tool_failure(REPORT_RESULT_TOOL_NAME, exc)


async def send_dtmf_handler(  # noqa: PLR0911 — each return is a distinct fail-clear branch (no adapter / no call / no digits / too-long / invalid-DTMF / not-negotiated / inactive / success); collapsing them would hide which failure the model sees
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: send in-call DTMF on the current call (ELEVATED, ADR-0031).

    Resolves the call from the Hermes session context (its ``chat_id`` is the SIP
    Call-ID) and sends the requested ``digits`` via the live adapter. Returns the
    JSON tool-result contract: ``{"result": …}`` on success, ``{"error": …}`` when
    no adapter/call is in scope, ``digits`` is missing or exceeds the length cap, the
    call has ended, the call negotiated no telephone-event payload type, or ``digits``
    contains a non-DTMF character. The ``pre_tool_call`` gate has already enforced the
    ELEVATED privilege
    clamp (and any caller-group ``allowed_tools`` sub-ceiling) before this runs. The
    call to send on is fixed to the session's own call — the model cannot target
    another call. The digits are NOT echoed in the result/log (they may be secret).
    """
    try:
        adapter = _ACTIVE_ADAPTER
        if adapter is None:
            return json.dumps({"error": "no active VoIP adapter; cannot send DTMF"})
        call_id = _current_call_id()
        if call_id is None:
            return json.dumps(
                {"error": "no active call in this session to send DTMF on"}
            )
        digits = _str_arg(args, "digits")
        if not digits:
            return json.dumps({"error": "send_dtmf requires a 'digits' string to send"})
        if len(digits) > _MAX_DTMF_DIGITS:
            # Defensive length cap: the schema maxLength is advisory (the model can
            # ignore it), so enforce it at the boundary. An over-long string would wedge
            # the call's outbound audio (~170ms/digit under the tx lock) for the whole
            # send — a DoS on the live call. Reject before dispatch; NEVER echo the
            # digits (they may be secret — rule 34), only the cap.
            return json.dumps(
                {"error": f"digits string too long (max {_MAX_DTMF_DIGITS})"}
            )
        try:
            sent = await adapter.send_dtmf_on_call(call_id, digits)
        except ValueError as exc:
            # A non-DTMF character — surface a clear, non-fatal error (nothing sent).
            return json.dumps({"error": f"invalid DTMF digits: {exc}"})
        except RuntimeError as exc:
            # The call negotiated no telephone-event payload type, so DTMF cannot be
            # sent. Report it (never a silent success); the engine sent nothing.
            return json.dumps({"error": f"cannot send DTMF on this call: {exc}"})
        if not sent:
            return json.dumps({"error": "the call is not active (unknown or ended)"})
        return json.dumps({"result": "Tones sent."})
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure_redacted)
        # REDACTED guard: an unanticipated exc could embed the secret digits.
        return _tool_failure_redacted(SEND_DTMF_TOOL_NAME, exc)


async def open_entry_handler(
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: actuate the intercom entry on the current call (ELEVATED).

    Resolves the call from the Hermes session context and asks the live adapter to
    open the entry (ADR-0031/0045). The OPTIONAL ``name`` arg selects WHICH named
    opening to actuate on a multi-intercom call (door / gate / garage); it is scoped to
    the calling intercom's set (the adapter rejects a name the intercom does not own).
    Returns the JSON tool-result contract: ``{"result": …}`` on success, ``{"error":
    …}`` when no adapter/call is in scope, the call has ended, the requested name is
    not in this intercom's set, or the actuation could not be performed (e.g. DTMF not
    negotiated, the relay/webhook is not configured). The ``pre_tool_call`` gate has
    already enforced the ELEVATED clamp and the intercom group's ``allowed_tools``
    sub-ceiling before this runs.
    """
    try:
        adapter = _ACTIVE_ADAPTER
        if adapter is None:
            return json.dumps(
                {"error": "no active VoIP adapter; cannot open the entry"}
            )
        call_id = _current_call_id()
        if call_id is None:
            return json.dumps({"error": "no active call in this session"})
        # Absent/blank name => None (legacy single-opening / default-the-sole-opening).
        name = _str_arg(args, "name") or None
        try:
            opened = await adapter.open_entry(call_id, name)
        except (RuntimeError, ValueError) as exc:
            # The actuation failed (DTMF not negotiated, relay misconfigured, etc.).
            # Report it clearly — the door was NOT opened (rule 37: surfaced, not
            # hidden).
            return json.dumps({"error": f"could not open the entry: {exc}"})
        if not opened:
            return json.dumps({"error": "the call is not active (unknown or ended)"})
        return json.dumps({"result": "Entry opened."})
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure_redacted)
        # REDACTED guard: an unanticipated exc could embed the opening secret.
        return _tool_failure_redacted(OPEN_ENTRY_TOOL_NAME, exc)


def _sip_status_detail(status: int | None, reason: str | None) -> str:
    """Render a sipfrag ``<status> <reason>`` detail for a FAILED transfer (ADR-0109).

    Tolerates a missing part (a terminal NOTIFY always carries a status, but the reason
    phrase is optional); an empty detail collapses to a generic phrase so a message
    never reads ``failed: .``.
    """
    parts = [str(status) if status is not None else "", reason or ""]
    return " ".join(part for part in parts if part) or "no status reported"


def _within_timeout(timeout_secs: float | None) -> str:
    """Render the ``within Ns`` clause of the OUTCOME_UNKNOWN message (ADR-0109).

    ``timeout_secs`` is the bounded wait the adapter applied (``None`` only when it is
    unavailable); ``:g`` trims a whole-number float (``20.0`` -> ``20``).
    """
    if timeout_secs is None:
        return "within the timeout"
    return f"within {timeout_secs:g}s"


def _outcome_unknown_message(
    reason: TransferUnknownReason | None, timeout_secs: float | None, *, prefix: str
) -> str:
    """Render the OUTCOME_UNKNOWN tool message for the discriminated reason (P2).

    ``prefix`` opens the message with the transfer-identifying clause — ``"Transfer to
    {target}"`` (blind) or ``"Transfer"`` (attended) — so the per-reason wording is
    shared. Each reason states what actually happened instead of always claiming a
    bounded wait (ADR-0109 §4): a declined RFC 4488 subscription, the call ending first,
    or outcome confirmation being disabled. A ``TIMEOUT`` — or a defensive ``None``
    reason — keeps the honest "outcome not confirmed within Ns" wording naming the wait
    that actually elapsed.
    """
    if reason is TransferUnknownReason.WAIT_DISABLED:
        return f"{prefix} initiated."
    if reason is TransferUnknownReason.SUBSCRIPTION_DECLINED:
        return (
            f"{prefix} initiated; the peer declined the transfer-progress "
            "subscription, so the final outcome was not reported."
        )
    if reason is TransferUnknownReason.CALL_ENDED:
        return (
            f"{prefix} initiated; the call ended before the transfer outcome "
            "was reported."
        )
    return f"{prefix} initiated; outcome not confirmed {_within_timeout(timeout_secs)}."


async def transfer_blind_handler(  # noqa: PLR0911 — each return is a distinct fail-clear branch the model must be able to tell apart (no adapter / no call / no target / transfer-failed / transferred / not-confirmed / call-ended); collapsing them would hide which outcome occurred
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: DTMF-confirmed blind transfer of the current call (IRREVERSIBLE).

    Resolves the call from the Hermes session context (its ``chat_id`` is the SIP
    Call-ID), reads the ``target`` destination from the tool args, and asks the live
    adapter to confirm-then-transfer via
    :meth:`VoipToolHost.transfer_blind_on_call`. The REFER fires **only** when the
    person on the call presses the armed confirm digit; a wrong digit or a timeout
    transfers nobody.

    Returns the JSON tool-result contract reporting the REAL transfer outcome
    (ADR-0109): ``{"result": …}`` when the transfer COMPLETED (terminal 2xx NOTIFY) or
    the outcome is genuinely unknown (no terminal NOTIFY within the wait — the honest
    "initiated" wording); ``{"error": …}`` when it FAILED (terminal 3xx-6xx, carrying
    the SIP status), no adapter/call is in scope, ``target`` is missing, the caller did
    not confirm (UNCONFIRMED), the call ended (NO_CALL), the call cannot obtain a
    spoof-resistant confirmation (no telephone-event), or the gateway rejected the
    REFER. The call to transfer is fixed to the session's own call — the model only
    chooses the target.

    The ``pre_tool_call`` gate has already enforced the operator-level (3) +
    non-degraded privilege clamp before this runs (a fail-fast: an unprivileged or
    degraded caller is blocked before the confirm prompt is ever spoken). The DTMF
    confirmation here is the irreversibility safeguard — the live, per-call analogue
    of ``place_call``'s static allowlist.
    """
    try:
        adapter = _ACTIVE_ADAPTER
        if adapter is None:
            return json.dumps(
                {"error": "no active VoIP adapter; cannot transfer the call"}
            )
        call_id = _current_call_id()
        if call_id is None:
            return json.dumps({"error": "no active call in this session to transfer"})
        target = _str_arg(args, "target")
        if not target:
            return json.dumps(
                {"error": "transfer_blind requires a 'target' to transfer to"}
            )
        try:
            result = await adapter.transfer_blind_on_call(call_id, target)
        except RuntimeError as exc:
            # No confirmation channel (no telephone-event) OR the gateway rejected the
            # REFER (CallError is a RuntimeError). Surface it clearly — nobody was
            # transferred, and the failure is reported, never hidden (rule 37).
            return json.dumps({"error": f"transfer failed: {exc}"})
        outcome = result.outcome
        if outcome is TransferOutcome.COMPLETED:
            # ADR-0109: the terminal NOTIFY reported a 2xx — the caller reached target.
            return json.dumps({"result": f"Transfer to {target} completed."})
        if outcome is TransferOutcome.FAILED:
            # The terminal NOTIFY reported a 3xx-6xx (busy / declined / unreachable):
            # the transfer definitively did not complete — the agent can recover.
            detail = _sip_status_detail(result.notify_status, result.notify_reason)
            return json.dumps({"error": f"Transfer to {target} failed: {detail}."})
        if outcome is TransferOutcome.OUTCOME_UNKNOWN:
            # The REFER was accepted but no terminal NOTIFY resolved the transfer. The
            # honest "initiated" wording stays, but ADR-0109 P2 names WHY it is unknown
            # (timeout / declined subscription / call-ended / wait-disabled) instead of
            # always claiming a bounded wait that may never have elapsed (rule 27).
            return json.dumps(
                {
                    "result": _outcome_unknown_message(
                        result.unknown_reason,
                        result.timeout_secs,
                        prefix=f"Transfer to {target}",
                    )
                }
            )
        if outcome is TransferOutcome.UNCONFIRMED:
            return json.dumps(
                {
                    "error": (
                        "the caller did not confirm the transfer; "
                        "nobody was transferred"
                    )
                }
            )
        if outcome is TransferOutcome.BLOCKED:
            # The REFER chokepoint's own guard re-check refused it (not operator-level /
            # degraded — possibly a state change during the confirmation window). The
            # privilege clamp is enforced at the chokepoint, not only at the gate.
            return json.dumps({"error": "the transfer is not permitted on this call"})
        # NO_CALL — the call is unknown or already ended. (TRANSFERRED is the internal
        # REFER-accepted intermediate the adapter resolves before returning, so it
        # never reaches the handler.)
        return json.dumps({"error": "the call is not active (unknown or ended)"})
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure)
        return _tool_failure(TRANSFER_BLIND_TOOL_NAME, exc)


async def transfer_attended_handler(  # noqa: PLR0911 — one return per fail-clear branch (no adapter / no call / consult / complete / cancel / unknown-action / handler-boundary failure); collapsing them would hide which outcome the model sees
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: attended (consultative) transfer of the current call (ADR-0048).

    Resolves the call from the Hermes session context (its ``chat_id`` is the SIP
    Call-ID), reads the ``action`` (``consult`` / ``complete`` / ``cancel``), and drives
    the matching live-adapter host method:

    * ``consult`` (needs ``target``) -> :meth:`VoipToolHost.start_attended_consult` —
      dials the consultation leg (gated by the outbound allowlist) and returns its
      Call-ID;
    * ``complete`` -> :meth:`VoipToolHost.complete_attended_transfer` — sends the
      REFER+Replaces on the original call (bridging the caller to the target);
    * ``cancel`` -> :meth:`VoipToolHost.cancel_attended_transfer` — abandons the
      consultation and keeps the original caller.

    Returns the JSON tool-result contract on success AND every error and never raises
    (the always-JSON contract): ``{"consult_call_id": …}`` / ``{"result": …}`` on
    success, ``{"error": …}`` for an unknown action, a missing target, an unlisted
    target, a blocked privilege, a missing consultation, an ended call, or a gateway
    REFER rejection. The call to transfer is fixed to the session's own call — the
    model picks only the destination + step.

    The ``pre_tool_call`` gate has already enforced the operator-level (3) +
    non-degraded clamp before this runs; the outbound allowlist (the consult gate) is
    the irreversibility safeguard — the static analogue of ``transfer_blind``'s live
    DTMF confirmation.
    """
    try:
        adapter = _ACTIVE_ADAPTER
        if adapter is None:
            return json.dumps(
                {"error": "no active VoIP adapter; cannot transfer the call"}
            )
        call_id = _current_call_id()
        if call_id is None:
            return json.dumps({"error": "no active call in this session to transfer"})
        action = _str_arg(args, "action")
        if action == "consult":
            return await _attended_consult(adapter, call_id, _str_arg(args, "target"))
        if action == "complete":
            return await _attended_complete(adapter, call_id)
        if action == "cancel":
            return await _attended_cancel(adapter, call_id)
        return json.dumps(
            {"error": "transfer_attended 'action' must be consult, complete, or cancel"}
        )
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure)
        return _tool_failure(TRANSFER_ATTENDED_TOOL_NAME, exc)


async def _attended_consult(adapter: VoipToolHost, call_id: str, target: str) -> str:
    """``transfer_attended action=consult``: dial the consultation leg (ADR-0048)."""
    if not target:
        return json.dumps({"error": "transfer_attended consult requires a 'target'"})
    try:
        consult_call_id = await adapter.start_attended_consult(call_id, target)
    except OutboundCallNotAllowed as exc:
        # The consult dial gate refused the target — surface a clear, non-fatal error;
        # the leg was NOT dialled. (The message names the rejected target.)
        return json.dumps({"error": str(exc)})
    except PermissionError:
        return json.dumps({"error": "the transfer is not permitted on this call"})
    except KeyError:
        return json.dumps({"error": "the call is not active (unknown or ended)"})
    except (OutboundCallFailed, RuntimeError) as exc:
        # The consult dial did not establish: OutboundCallFailed for a busy outbound
        # slot / no registered extension (503) or the WSS transport (501 — outbound
        # origination is deferred there); RuntimeError for an un-initialised
        # transport/manager OR a second consultation refused while one is already in
        # flight for this call. All render as a clear, non-fatal JSON error — never an
        # escaped exception (the always-JSON contract); no leg is left connected.
        return json.dumps({"error": f"consultation call failed: {exc}"})
    return json.dumps({"consult_call_id": consult_call_id})


async def _attended_complete(adapter: VoipToolHost, call_id: str) -> str:  # noqa: PLR0911 — one return per fail-clear outcome the model must tell apart (completed/failed/unknown/no-consult/blocked/no-call); collapsing them hides which occurred
    """action=complete: send the REFER+Replaces + await the outcome (ADR-0109)."""
    try:
        result = await adapter.complete_attended_transfer(call_id)
    except CallError as exc:
        # The gateway rejected the REFER (CallError, a RuntimeError subclass). Catch
        # it SPECIFICALLY — an unrelated RuntimeError is a real bug and must propagate
        # (rule 37), not be masked as a transfer failure. Nobody was transferred.
        return json.dumps({"error": f"transfer failed: {exc}"})
    outcome = result.outcome
    if outcome is AttendedTransferOutcome.COMPLETED:
        # ADR-0109: the terminal NOTIFY reported a 2xx — the caller reached the target.
        return json.dumps({"result": "Caller connected to the consultation target."})
    if outcome is AttendedTransferOutcome.FAILED:
        detail = _sip_status_detail(result.notify_status, result.notify_reason)
        return json.dumps({"error": f"Transfer failed: {detail}."})
    if outcome is AttendedTransferOutcome.OUTCOME_UNKNOWN:
        # ADR-0109 P2: name WHY the outcome is unknown (mirrors the blind path, with no
        # {target} in the attended phrasing) rather than always claiming a bounded wait.
        return json.dumps(
            {
                "result": _outcome_unknown_message(
                    result.unknown_reason, result.timeout_secs, prefix="Transfer"
                )
            }
        )
    if outcome is AttendedTransferOutcome.NO_CONSULT:
        return json.dumps(
            {"error": "no consultation is in progress; call the target first (consult)"}
        )
    if outcome is AttendedTransferOutcome.BLOCKED:
        return json.dumps({"error": "the transfer is not permitted on this call"})
    # NO_CALL — the original call or the consult leg is unknown / already ended.
    # (TRANSFERRED is the internal intermediate the adapter resolves before returning.)
    return json.dumps({"error": "the call is not active (unknown or ended)"})


async def _attended_cancel(adapter: VoipToolHost, call_id: str) -> str:
    """``transfer_attended action=cancel``: abandon the consultation (ADR-0048)."""
    cancelled = await adapter.cancel_attended_transfer(call_id)
    if not cancelled:
        return json.dumps({"error": "no consultation is in progress to cancel"})
    return json.dumps({"result": "Consultation ended; you are back with the caller."})


# The IRREVERSIBLE tools whose confirmation is enforced at a deeper chokepoint, not in
# the sync ``pre_tool_call`` hook (which cannot speak a prompt / await DTMF). The gate
# passes ``confirmed=True`` for these so ``gate_tool_call`` applies the level-3 +
# non-degraded clamp as a fail-fast; the real per-action safeguard runs later:
# ``place_call`` -> the static outbound allowlist (ADR-0029); ``transfer_attended`` ->
# the SAME outbound allowlist on its consult leg (ADR-0048); ``transfer_blind`` -> the
# live ADR-0010 DTMF ArmedConfirmation in ``transfer_blind_on_call`` (the REFER fires
# ONLY on the armed confirm digit). This is a fixed property of the tool, NOT a model
# input — see :func:`voip_pre_tool_call`.
_CONFIRMED_AT_CHOKEPOINT: frozenset[str] = frozenset(
    {PLACE_CALL_TOOL_NAME, TRANSFER_BLIND_TOOL_NAME, TRANSFER_ATTENDED_TOOL_NAME}
)


def voip_pre_tool_call(
    tool_name: str = "",
    args: Mapping[str, object] | None = None,  # noqa: ARG001 — hook arity; args unused by the VoIP gate
    **_kwargs: object,
) -> dict[str, str] | None:
    """``pre_tool_call`` gate for the VoIP tools (ADR-0009/0011/0020/0026/0029).

    The Hermes runtime invokes every registered ``pre_tool_call`` hook before a
    tool runs and blocks the tool when a hook returns
    ``{"action": "block", "message": ...}`` (any other return allows it). This
    gate applies :func:`gate_voip_tool` to the VoIP tools using the current call's
    guard state (privilege level + degraded flag); a tool name we do not own is
    not ours to judge, so we return ``None`` (defer to other hooks / allow).

    The privilege clamp is the security spine: ``hang_up`` and ``report_call_result``
    are SAFE (never blocked); ``hold_call`` / ``resume_call`` / ``list_registrations``
    / ``send_dtmf`` are ELEVATED; ``place_call`` and ``transfer_blind`` are
    IRREVERSIBLE. So a level-0 (untrusted/receptionist) caller — or any ``degraded``
    session — is BLOCKED from the ELEVATED/IRREVERSIBLE tools here even if a prompt
    injection coaxes the model into calling one, and ``place_call`` /
    ``transfer_blind`` additionally require the operator level (3). An unknown call
    context falls back to a level-0 state, so it can never accidentally grant a
    privileged tool (fail safe).

    **Proactive ``place_call`` (issue #202).** The one opt-in exception to the
    no-live-call fail-safe: when ``HERMES_VOIP_PROACTIVE_CALL_FROM`` lists the
    originating ``platform:chat_id`` (read from ``gateway.session_context``), a
    ``place_call`` from that NON-VoIP operator session resolves operator level 3
    instead of 0 — so the operator can drive a proactive outbound call from a chat
    (``adapter._capture_origin_session`` captures the same origin;
    ``HERMES_VOIP_OUTBOUND_RESULT_CHANNEL`` reports the outcome back). It is
    place_call-only (transfer/dtmf/open_entry stay blocked), opt-in (EMPTY / unset =>
    byte-identical to the fail-safe default), and never touches the INBOUND fail-safe
    (a live call still resolves its real caller-group privilege). The static
    ``HERMES_VOIP_OUTBOUND_ALLOW`` allowlist (ADR-0029) still gates the dial target at
    the chokepoint regardless. See :func:`_proactive_place_call_allowed`.

    **The ``confirmed`` argument (ADR-0010/0029).** A model-set confirmation is never
    trusted — ``confirmed`` here is a fixed per-tool property, not a model input. For
    every SAFE/ELEVATED tool the gate passes ``confirmed=False`` (they never consult
    it). The two IRREVERSIBLE tools, ``place_call`` and ``transfer_blind``, are gated
    WITH ``confirmed=True`` so ``gate_tool_call`` applies exactly the *privilege* part
    of the IRREVERSIBLE clamp (operator level 3 + non-degraded) here at the gate; the
    actual irreversibility safeguard then runs deeper, NOT in this sync hook:

    * ``place_call`` — its safeguard is the static, operator-curated
      ``HERMES_VOIP_OUTBOUND_ALLOW`` allowlist, enforced inside
      ``place_call_with_objective`` before any dial (ADR-0029).
    * ``transfer_blind`` — its safeguard is the **live ADR-0010 DTMF confirmation**:
      ``transfer_blind_on_call`` awaits the per-call ``ArmedConfirmation`` and fires
      the REFER only when the person on the call presses the armed confirm digit. The
      gate cannot run that here (it is async and speaks a prompt), so the gate's job
      is the *fail-fast* privilege clamp — an unprivileged or degraded caller is
      blocked BEFORE any confirm prompt is ever spoken — and the handler enforces the
      confirmation before any caller is transferred.

    Passing ``confirmed=True`` for these two is therefore exact, not a stub: it makes
    the gate apply the privilege clamp, while the real per-action safeguard lives at
    the dial / REFER chokepoint where it can be enforced for real.
    """
    if tool_name not in _voip_tool_names():
        return None  # not a VoIP tool — defer (this hook fires for ALL tools)
    adapter = _ACTIVE_ADAPTER
    call_id = _current_call_id()
    # Resolve the call's guard state; fall back to a least-privilege receptionist
    # state when the call/adapter is not in scope, so an unknown context cannot
    # accidentally grant a privileged tool (fail safe).
    state: GuardSessionState | None = None
    # The non-sensitive proactive-relaxation deny CATEGORY (issue #414), set only when
    # the fail-safe (no-live-guard-state) path did NOT grant. Logged once BELOW iff the
    # gate then actually blocks the tool — so an operator can tell the actionable cause
    # (unset opt-in / unreadable or unlisted origin / live-guard-missing) apart. It is a
    # category ONLY; the origin values themselves are secrets and are never logged.
    proactive_deny_reason: ProactiveDenyReason | None = None
    if adapter is not None and call_id is not None:
        state = adapter.guard_state_for(call_id)
    if state is None:
        # Fail-safe default: least-privilege (level 0) when there is no live SIP call
        # guard state in scope, so an unknown/spoofed context can never reach a
        # privileged tool. The ONLY relaxation (issue #202): a PROACTIVE place_call
        # from a configured operator origin (HERMES_VOIP_PROACTIVE_CALL_FROM) resolves
        # operator level 3 — opt-in and place_call-only. Per ADR-0074 the relaxation is
        # reached whenever the guard ``state is None``: on a non-VoIP operator turn
        # (e.g. a Telegram chat) the session chat_id is NOT a SIP Call-ID, so
        # ``guard_state_for`` misses even though ``call_id`` is non-None — the
        # legitimate proactive turn. The INBOUND fail-safe is preserved by
        # PLATFORM-SCOPED matching inside the helper, NOT by the presence of a Call-ID
        # (non-None on the proactive turn too): an inbound SIP call's
        # ``(platform="voip", chat_id=<Call-ID>)`` origin never matches an operator's
        # non-VoIP HERMES_VOIP_PROACTIVE_CALL_FROM entry, and an unreadable origin
        # denies via ORIGIN_UNAVAILABLE. The static HERMES_VOIP_OUTBOUND_ALLOW
        # allowlist (ADR-0029) still gates the dial target at the chokepoint
        # regardless.
        decision = _proactive_place_call_allowed(tool_name)
        if not decision.allowed:
            proactive_deny_reason = decision.reason
        state = GuardSessionState(
            call_id=call_id or "", privilege_level=3 if decision.allowed else 0
        )
    # ``confirmed`` is a fixed per-tool property, never a model input. The two
    # IRREVERSIBLE tools (``place_call``, ``transfer_blind``) are gated WITH
    # confirmation satisfied so ``gate_tool_call`` applies the level-3 + non-degraded
    # clamp here (a fail-fast); their REAL irreversibility safeguard — the outbound
    # allowlist / the live DTMF confirmation — runs at the dial / REFER chokepoint
    # (see docstring). Every other tool is SAFE/ELEVATED and ignores ``confirmed``, so
    # passing False for them is exact, not a stub.
    confirmed = tool_name in _CONFIRMED_AT_CHOKEPOINT
    if not gate_voip_tool(tool_name, state, confirmed=confirmed):
        if proactive_deny_reason is not None:
            # ADR-0075-style structured, machine-parseable diagnostic (issue #414):
            # emit the NON-SENSITIVE deny CATEGORY so an operator can diagnose a
            # refused proactive place_call. This fires exactly once — only when the
            # no-live-guard fail-safe path did not grant AND the tool was actually
            # blocked — alongside the WARNING gate_voip_tool already logs. rule 34:
            # NEVER log the origin platform/chat_id/allowlist — the category only.
            _log.warning(
                "proactive place_call gate denied (reason=%s)",
                proactive_deny_reason.value,
                extra={
                    "event": "proactive_place_call_gate",
                    "reason": proactive_deny_reason.value,
                    "tool": tool_name,
                },
            )
        return {
            "action": "block",
            "message": f"The {tool_name} tool is not permitted on this call.",
        }
    return None


def _voip_tool_names() -> frozenset[str]:
    """The tool names this plugin's gate is responsible for (every exposed tool).

    Must list EVERY tool :func:`register_voip_tools` registers: a tool absent here
    would have the gate ``return None`` (defer) for it, bypassing the privilege
    clamp. Both transfer tools are registered and owned here: ``transfer_blind``
    (its spoof-resistant DTMF confirmation channel landed) and ``transfer_attended``
    (its agent-driven consult-leg origination landed — ADR-0048).
    """
    return frozenset(
        {
            HANG_UP_TOOL_NAME,
            HOLD_TOOL_NAME,
            RESUME_TOOL_NAME,
            LIST_REGISTRATIONS_TOOL_NAME,
            PLACE_CALL_TOOL_NAME,
            REPORT_RESULT_TOOL_NAME,
            SEND_DTMF_TOOL_NAME,
            OPEN_ENTRY_TOOL_NAME,
            TRANSFER_BLIND_TOOL_NAME,
            TRANSFER_ATTENDED_TOOL_NAME,
        }
    )


@dataclass(frozen=True, slots=True)
class _ToolSpec:
    """One agent tool to register: name, schema, handler, summary, emoji, gating.

    ``requires_gate`` is True for a tool whose privilege clamp lives in the
    ``pre_tool_call`` hook (every non-SAFE tool). Such a tool MUST NOT be
    registered unless the hook is installed — otherwise it would be reachable
    ungated (a level-0 caller could invoke it). ``hang_up`` is SAFE and needs no
    clamp, so it registers regardless of the hook.
    """

    name: str
    schema: dict[str, object]
    handler: object
    description: str
    emoji: str
    requires_gate: bool


# Every tool exposed to the agent (the gate's ``_voip_tool_names`` MUST cover the
# same set). Both transfers (IRREVERSIBLE) are exposed: ``transfer_blind`` (its
# spoof-resistant DTMF confirmation channel landed, so the REFER fires only on a real
# keypad confirm) and ``transfer_attended`` (its agent-driven consult-leg origination
# landed — ADR-0048; the consult dial is gated by the outbound allowlist). Neither is a
# lying stub (rule 6); both are wired end-to-end.
_VOIP_TOOLS: tuple[_ToolSpec, ...] = (
    _ToolSpec(
        name=HANG_UP_TOOL_NAME,
        schema=HANG_UP_TOOL_SCHEMA,
        handler=hang_up_handler,
        description="End the current phone call when the conversation is done.",
        emoji="\U0001f4f4",  # mobile phone off
        requires_gate=False,  # SAFE — runs for any caller, needs no privilege clamp
    ),
    _ToolSpec(
        name=HOLD_TOOL_NAME,
        schema=HOLD_TOOL_SCHEMA,
        handler=hold_call_handler,
        description="Place the current caller on hold (privileged calls only).",
        emoji="⏸️",  # pause button
        requires_gate=True,  # ELEVATED — only register WITH the privilege gate
    ),
    _ToolSpec(
        name=RESUME_TOOL_NAME,
        schema=RESUME_TOOL_SCHEMA,
        handler=resume_call_handler,
        description="Resume a caller you placed on hold (privileged calls only).",
        emoji="▶️",  # play button
        requires_gate=True,  # ELEVATED
    ),
    _ToolSpec(
        name=LIST_REGISTRATIONS_TOOL_NAME,
        schema=LIST_REGISTRATIONS_TOOL_SCHEMA,
        handler=list_registrations_handler,
        description="List this system's phone registrations (privileged calls only).",
        emoji="\U0001f4cb",  # clipboard
        requires_gate=True,  # ELEVATED — discloses internal extension metadata
    ),
    _ToolSpec(
        name=PLACE_CALL_TOOL_NAME,
        schema=PLACE_CALL_TOOL_SCHEMA,
        handler=place_call_handler,
        description="Place an outbound call to an approved number (operator only).",
        emoji="\U0001f4de",  # telephone receiver
        requires_gate=True,  # IRREVERSIBLE — never register without the privilege gate
    ),
    _ToolSpec(
        name=REPORT_RESULT_TOOL_NAME,
        schema=REPORT_RESULT_TOOL_SCHEMA,
        handler=report_call_result_handler,
        description="Record this call's outcome for the requesting conversation.",
        emoji="\U0001f4dd",  # memo
        requires_gate=False,  # SAFE — the agent records its own call's outcome
    ),
    _ToolSpec(
        name=SEND_DTMF_TOOL_NAME,
        schema=SEND_DTMF_TOOL_SCHEMA,
        handler=send_dtmf_handler,
        description="Send DTMF tones on the current call (privileged calls only).",
        emoji="\U0001f522",  # input numbers
        requires_gate=True,  # ELEVATED — only register WITH the privilege gate
    ),
    _ToolSpec(
        name=OPEN_ENTRY_TOOL_NAME,
        schema=OPEN_ENTRY_TOOL_SCHEMA,
        handler=open_entry_handler,
        description="Open the intercom entry for an expected visitor (gated).",
        emoji="\U0001f6aa",  # door
        requires_gate=True,  # ELEVATED — physical access; never register ungated
    ),
    _ToolSpec(
        name=TRANSFER_BLIND_TOOL_NAME,
        schema=TRANSFER_BLIND_TOOL_SCHEMA,
        handler=transfer_blind_handler,
        description="Transfer the current caller to another number (operator only).",
        emoji="\U0001f504",  # counterclockwise arrows (transfer)
        requires_gate=True,  # IRREVERSIBLE — never register without the privilege gate
    ),
    _ToolSpec(
        name=TRANSFER_ATTENDED_TOOL_NAME,
        schema=TRANSFER_ATTENDED_TOOL_SCHEMA,
        handler=transfer_attended_handler,
        description="Consult a destination then transfer the caller (operator only).",
        emoji="\U0001f500",  # twisted rightwards arrows (attended transfer)
        requires_gate=True,  # IRREVERSIBLE — never register without the privilege gate
    ),
)


def register_voip_tools(ctx: object) -> None:
    """Register the VoIP agent tools + the pre-tool-call gate (ADR-0026/0011).

    Registers ``hang_up`` (SAFE) and the in-call control tools ``hold_call`` /
    ``resume_call`` / ``list_registrations`` (ELEVATED), each through
    ``ctx.register_tool`` and all behind the single ``pre_tool_call`` gate so the
    privilege clamp governs every one. The IRREVERSIBLE transfer tools —
    ``transfer_blind`` (its spoof-resistant DTMF confirmation channel landed) and
    ``transfer_attended`` (its agent-driven consult-leg origination landed, ADR-0048)
    — ARE registered, both ELEVATED behind the same gate.

    **Fail-closed gating.** The ELEVATED tools' privilege clamp lives in the
    ``pre_tool_call`` hook, so they are registered ONLY when the hook is also
    installed. If a (hypothetical/older) ``PluginContext`` had ``register_tool``
    but no ``register_hook``, registering an ELEVATED tool would leave it reachable
    ungated — a level-0 caller could then hold/resume the call or enumerate the
    operator's registrations. So the hook is installed FIRST and a tool whose
    ``requires_gate`` is True is skipped (with a warning) when the hook is absent.
    ``hang_up`` is SAFE and needs no clamp, so it registers regardless.

    Best-effort and resilient otherwise: a runtime whose ``PluginContext`` predates
    ``register_tool`` simply does not get the tools — the platform still registers.
    Mirrors the ``getattr`` guard :func:`hermes_voip.plugin.register` already uses
    for ``register_platform``.

    Args:
        ctx: The Hermes ``PluginContext`` (typed ``object`` at this boundary —
            this module imports no hermes runtime).
    """
    # Install the privilege-clamp hook FIRST so the gate is in place before any
    # ELEVATED tool is registered (fail closed — see the docstring).
    register_hook: _RegisterHook | None = getattr(ctx, "register_hook", None)
    gate_installed = register_hook is not None
    if register_hook is not None:
        register_hook("pre_tool_call", voip_pre_tool_call)
    else:
        _log.warning("register(ctx): ctx has no register_hook — VoIP tool gate skipped")

    register_tool: _RegisterTool | None = getattr(ctx, "register_tool", None)
    if register_tool is None:
        _log.warning("register(ctx): ctx has no register_tool — VoIP tools skipped")
        return
    for spec in _VOIP_TOOLS:
        if spec.requires_gate and not gate_installed:
            # The clamp is missing — refuse to expose a privileged tool ungated.
            _log.warning(
                "register(ctx): no pre_tool_call gate — skipping ELEVATED VoIP "
                "tool %r (would be reachable without its privilege clamp)",
                spec.name,
            )
            continue
        # Fail-soft per tool: a runtime that rejects ONE tool (e.g. a name collision
        # with another plugin) must not abort the whole loop and silently drop the
        # remaining tools. Log a warning naming the offending tool and continue —
        # mirrors the getattr presence guards above (best-effort, resilient).
        try:
            register_tool(
                spec.name,
                VOIP_TOOLSET,
                spec.schema,
                spec.handler,
                is_async=True,
                description=spec.description,
                emoji=spec.emoji,
            )
        except Exception:  # noqa: BLE001 — one bad tool must not abort the rest (fail-soft)
            _log.warning(
                "register(ctx): register_tool failed for VoIP tool %r — skipping it "
                "(the other tools still register)",
                spec.name,
                exc_info=True,
            )
