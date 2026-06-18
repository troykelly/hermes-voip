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

The one transfer still **deliberately NOT exposed here** is ``transfer_attended``.
Its REFER+Replaces is implemented, but an attended transfer needs a consultation
:class:`~hermes_voip.dialog.Dialog` the agent cannot originate (no consult-leg
origination path exists — ADR-0031 §4 / the ADR-0011 finding). Registering it would
be a lying stub, which rule 6 forbids — so it is deferred-not-registered until that
origination path lands.

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
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from hermes_voip.originate import OutboundCallNotAllowed
from hermes_voip.providers.policy import GuardSessionState
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
    "TRANSFER_BLIND_TOOL_NAME",
    "TRANSFER_BLIND_TOOL_SCHEMA",
    "VOIP_TOOLSET",
    "TransferOutcome",
    "VoipToolHost",
    "active_voip_adapter",
    "hang_up_handler",
    "hold_call_handler",
    "list_registrations_handler",
    "open_entry_handler",
    "place_call_handler",
    "register_voip_tools",
    "report_call_result_handler",
    "resume_call_handler",
    "send_dtmf_handler",
    "set_active_adapter",
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
# justification at each site.
def _tool_failure(tool_name: str, exc: BaseException) -> str:
    """Log ``exc`` and render the handler-boundary error-JSON for ``tool_name``."""
    _log.exception("VoIP tool %r failed with an unanticipated error", tool_name)
    return json.dumps({"error": f"{tool_name} failed: {exc}"})


class TransferOutcome(Enum):
    """The tri-state result of a DTMF-confirmed blind transfer (ADR-0010/0031).

    The host method :meth:`VoipToolHost.transfer_blind_on_call` returns this so the
    handler distinguishes a fired REFER from a *refused-by-the-caller* one from a
    stale call — each maps to a distinct tool-result message:

    * ``TRANSFERRED`` — the caller pressed the armed confirm digit; the REFER fired.
    * ``UNCONFIRMED`` — a wrong digit or the confirmation timed out; **no REFER**.
    * ``NO_CALL`` — the call was unknown or had already ended; **no REFER**.
    * ``BLOCKED`` — the call's OWN privilege gate refused the transfer (it is not an
      operator-level, non-degraded call). The sync ``pre_tool_call`` gate runs before
      this method, but the REFER chokepoint re-checks the guard ITSELF (defense in
      depth), so a session that lost privilege or went ``degraded`` *during* the
      confirmation window — or any direct/bypass invocation — cannot fire the REFER.
      **No REFER**.

    A *failure* to even obtain a confirmation (no telephone-event negotiated) or a
    REFER the gateway rejects is signalled by an exception, not a member — those are
    loud errors, never a silent no-op (rule 37).
    """

    TRANSFERRED = "transferred"
    UNCONFIRMED = "unconfirmed"
    NO_CALL = "no_call"
    BLOCKED = "blocked"


#: The Hermes ``chat_id`` (== SIP Call-ID) session-context variable name.
_SESSION_CHAT_ID_ENV = "HERMES_SESSION_CHAT_ID"

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

#: ``send_dtmf`` schema. ``digits`` is the DTMF string to send (``0-9``, ``*``,
#: ``#``, ``A``-``D``). The call to send on is the current session's own call
#: (resolved from the session context) — the model cannot target another call.
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

#: ``open_entry`` schema. No parameters: opening the entry is a single fixed action
#: on the current call, never a model-chosen target.
OPEN_ENTRY_TOOL_SCHEMA: dict[str, object] = {
    "name": OPEN_ENTRY_TOOL_NAME,
    "description": (
        "Open the entry (unlock the door / gate) for the visitor on this intercom "
        "call. Use this ONLY for a legitimate, expected visitor after you have "
        "confirmed who they are and that they are expected — opening the door is a "
        "physical-access action. Takes no input."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
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
#: hardening, not shipped). ``transfer_attended`` is deliberately NOT exposed — it needs
#: a consult-leg Dialog the agent cannot originate (deferred, ADR-0031 §4).
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

    async def place_call_with_objective(self, number: str, objective: str) -> str:
        """Place an outbound call pursuing ``objective``; return the new Call-ID.

        Captures the originating session (for result reporting) and enforces the
        outbound allowlist BEFORE dialling. Raises
        :class:`~hermes_voip.originate.OutboundCallNotAllowed` when ``number`` is not
        permitted (so the handler reports a clear refusal and nothing is dialled).
        Returns immediately once the call loop is running (the call proceeds in the
        background) — it does NOT await the whole call.
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

    async def open_entry(self, call_id: str) -> bool:
        """Actuate the intercom entry for ``call_id`` (open the door — ADR-0031).

        Returns whether it acted (``False`` for an unknown/ended call). The
        actuation path (in-call DTMF or an external relay) is the adapter's concern;
        the tool only requests the entry on the current call.
        """
        ...

    async def transfer_blind_on_call(
        self, call_id: str, target: str
    ) -> TransferOutcome:
        """DTMF-confirmed blind transfer of ``call_id`` to ``target`` (ADR-0010/0031).

        The spoof-resistant chokepoint: awaits the call's per-call
        :class:`~hermes_voip.dtmf_confirm.ArmedConfirmation` (prompts the person on
        the call to press the confirm digit) and sends the RFC 3515 REFER via
        :meth:`hermes_voip.call.CallSession.transfer_blind` **only** when the caller
        confirms. Returns a :class:`TransferOutcome`:

        * ``TRANSFERRED`` — confirmed; the REFER fired.
        * ``UNCONFIRMED`` — a wrong digit / timeout; no REFER.
        * ``NO_CALL`` — the call was unknown or had already ended; no REFER.

        Raises (never a silent no-op — rule 37):

        * ``RuntimeError`` — the call has no bound confirmation channel (it negotiated
          no telephone-event, so a spoof-resistant keypad confirmation is impossible).
        * :class:`~hermes_voip.call.CallError` — the gateway rejected the REFER.
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
    try:
        from gateway.session_context import get_session_env  # noqa: PLC0415
    except ImportError:
        return None
    value = get_session_env(_SESSION_CHAT_ID_ENV)
    return value or None


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


async def place_call_handler(
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: place an outbound call pursuing an objective (IRREVERSIBLE).

    Reads ``number`` + ``objective`` from the tool args, then calls the live
    adapter's :meth:`VoipToolHost.place_call_with_objective`. Returns the JSON
    tool-result contract: ``{"call_id": …}`` IMMEDIATELY on success (ASYNC — it does
    NOT await the whole call; the call runs as its own background conversation), or
    ``{"error": …}`` when no adapter is in scope, an argument is missing, or the
    target is not on the allowlist (the dial is then never made).

    The ``pre_tool_call`` gate has already enforced the operator-level (3) +
    non-degraded privilege clamp before this runs; the allowlist (enforced inside
    ``place_call_with_objective``) is the hard irreversibility gate.
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
        try:
            call_id = await adapter.place_call_with_objective(number, objective)
        except OutboundCallNotAllowed as exc:
            # The hard gate refused the target — surface a clear, non-fatal error; the
            # number was NOT dialled. (The error message names the rejected target.)
            return json.dumps({"error": str(exc)})
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


async def send_dtmf_handler(  # noqa: PLR0911 — each return is a distinct fail-clear branch (no adapter / no call / no digits / invalid-DTMF / not-negotiated / inactive / success); collapsing them would hide which failure the model sees
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: send in-call DTMF on the current call (ELEVATED, ADR-0031).

    Resolves the call from the Hermes session context (its ``chat_id`` is the SIP
    Call-ID) and sends the requested ``digits`` via the live adapter. Returns the
    JSON tool-result contract: ``{"result": …}`` on success, ``{"error": …}`` when
    no adapter/call is in scope, ``digits`` is missing, the call has ended, the call
    negotiated no telephone-event payload type, or ``digits`` contains a non-DTMF
    character. The ``pre_tool_call`` gate has already enforced the ELEVATED privilege
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
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure)
        return _tool_failure(SEND_DTMF_TOOL_NAME, exc)


async def open_entry_handler(
    args: Mapping[str, object] | None = None,
    **_kwargs: object,
) -> str:
    """Tool handler: actuate the intercom entry on the current call (ELEVATED).

    Resolves the call from the Hermes session context and asks the live adapter to
    open the entry (ADR-0031). Returns the JSON tool-result contract: ``{"result":
    …}`` on success, ``{"error": …}`` when no adapter/call is in scope, the call has
    ended, or the actuation could not be performed (e.g. DTMF not negotiated, or the
    relay is not configured). The ``pre_tool_call`` gate has already enforced the
    ELEVATED clamp and the intercom group's ``allowed_tools`` sub-ceiling before this
    runs. ``args`` is ignored — opening the entry is a fixed action on this call.
    """
    _ = args  # the tool takes no parameters
    try:
        adapter = _ACTIVE_ADAPTER
        if adapter is None:
            return json.dumps(
                {"error": "no active VoIP adapter; cannot open the entry"}
            )
        call_id = _current_call_id()
        if call_id is None:
            return json.dumps({"error": "no active call in this session"})
        try:
            opened = await adapter.open_entry(call_id)
        except (RuntimeError, ValueError) as exc:
            # The actuation failed (DTMF not negotiated, relay misconfigured, etc.).
            # Report it clearly — the door was NOT opened (rule 37: surfaced, not
            # hidden).
            return json.dumps({"error": f"could not open the entry: {exc}"})
        if not opened:
            return json.dumps({"error": "the call is not active (unknown or ended)"})
        return json.dumps({"result": "Entry opened."})
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure)
        return _tool_failure(OPEN_ENTRY_TOOL_NAME, exc)


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

    Returns the JSON tool-result contract: ``{"result": …}`` once the transfer is
    initiated (TRANSFERRED), or ``{"error": …}`` when no adapter/call is in scope,
    ``target`` is missing, the caller did not confirm (UNCONFIRMED), the call ended
    (NO_CALL), the call cannot obtain a spoof-resistant confirmation (no
    telephone-event negotiated), or the gateway rejected the REFER. The call to
    transfer is fixed to the session's own call — the model only chooses the target.

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
            outcome = await adapter.transfer_blind_on_call(call_id, target)
        except RuntimeError as exc:
            # No confirmation channel (no telephone-event) OR the gateway rejected the
            # REFER (CallError is a RuntimeError). Surface it clearly — nobody was
            # transferred, and the failure is reported, never hidden (rule 37).
            return json.dumps({"error": f"transfer failed: {exc}"})
        if outcome is TransferOutcome.TRANSFERRED:
            return json.dumps({"result": f"Transfer to {target} initiated."})
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
        # NO_CALL — the call is unknown or already ended.
        return json.dumps({"error": "the call is not active (unknown or ended)"})
    except Exception as exc:  # noqa: BLE001 — handler-boundary log-and-return (see _tool_failure)
        return _tool_failure(TRANSFER_BLIND_TOOL_NAME, exc)


# The IRREVERSIBLE tools whose confirmation is enforced at a deeper chokepoint, not in
# the sync ``pre_tool_call`` hook (which cannot speak a prompt / await DTMF). The gate
# passes ``confirmed=True`` for these so ``gate_tool_call`` applies the level-3 +
# non-degraded clamp as a fail-fast; the real per-action safeguard runs later:
# ``place_call`` -> the static outbound allowlist (ADR-0029); ``transfer_blind`` -> the
# live ADR-0010 DTMF ArmedConfirmation in ``transfer_blind_on_call`` (the REFER fires
# ONLY on the armed confirm digit). This is a fixed property of the tool, NOT a model
# input — see :func:`voip_pre_tool_call`.
_CONFIRMED_AT_CHOKEPOINT: frozenset[str] = frozenset(
    {PLACE_CALL_TOOL_NAME, TRANSFER_BLIND_TOOL_NAME}
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
    if adapter is not None and call_id is not None:
        state = adapter.guard_state_for(call_id)
    if state is None:
        state = GuardSessionState(call_id=call_id or "", privilege_level=0)
    # ``confirmed`` is a fixed per-tool property, never a model input. The two
    # IRREVERSIBLE tools (``place_call``, ``transfer_blind``) are gated WITH
    # confirmation satisfied so ``gate_tool_call`` applies the level-3 + non-degraded
    # clamp here (a fail-fast); their REAL irreversibility safeguard — the outbound
    # allowlist / the live DTMF confirmation — runs at the dial / REFER chokepoint
    # (see docstring). Every other tool is SAFE/ELEVATED and ignores ``confirmed``, so
    # passing False for them is exact, not a stub.
    confirmed = tool_name in _CONFIRMED_AT_CHOKEPOINT
    if not gate_voip_tool(tool_name, state, confirmed=confirmed):
        return {
            "action": "block",
            "message": f"The {tool_name} tool is not permitted on this call.",
        }
    return None


def _voip_tool_names() -> frozenset[str]:
    """The tool names this plugin's gate is responsible for (every exposed tool).

    Must list EVERY tool :func:`register_voip_tools` registers: a tool absent here
    would have the gate ``return None`` (defer) for it, bypassing the privilege
    clamp. ``transfer_blind`` IS registered (its spoof-resistant DTMF confirmation
    channel landed) and so is owned here; ``transfer_attended`` is intentionally
    absent because it is NOT registered (deferred — no agent-driven consult-leg
    Dialog origination exists; ADR-0031 §4).
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
# same set). ``transfer_blind`` (IRREVERSIBLE) IS exposed — its spoof-resistant DTMF
# confirmation channel landed, so the REFER fires only on a real keypad confirm.
# ``transfer_attended`` is the one transfer still ABSENT — deferred, not registered,
# because no agent-driven consult-leg Dialog origination exists (ADR-0031 §4); a
# lying stub would violate rule 6.
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
)


def register_voip_tools(ctx: object) -> None:
    """Register the VoIP agent tools + the pre-tool-call gate (ADR-0026/0011).

    Registers ``hang_up`` (SAFE) and the in-call control tools ``hold_call`` /
    ``resume_call`` / ``list_registrations`` (ELEVATED), each through
    ``ctx.register_tool`` and all behind the single ``pre_tool_call`` gate so the
    privilege clamp governs every one. The IRREVERSIBLE transfer tools are NOT
    registered (deferred — see the module docstring).

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
