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

The IRREVERSIBLE transfer tools (``transfer_blind`` / ``transfer_attended``) are
**deliberately NOT exposed here**. The REFER itself is implemented
(:meth:`hermes_voip.call.CallSession.transfer_blind`), but the
``IRREVERSIBLE`` gate requires a spoof-resistant ADR-0010 DTMF confirmation, and
that confirmation channel is **not wired into the live adapter** (there is no
armed-DTMF resolver feeding ``confirmed=True``). Exposing a transfer tool would
therefore create an always-blocked no-op, which rule 6 forbids — so transfer is
deferred-not-registered until the DTMF confirmation channel lands (and, for
attended transfer, until an agent-driven consultation-leg origination exists).

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
from typing import Protocol, runtime_checkable

from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.tools import gate_voip_tool

__all__ = [
    "HANG_UP_TOOL_NAME",
    "HANG_UP_TOOL_SCHEMA",
    "HOLD_TOOL_NAME",
    "HOLD_TOOL_SCHEMA",
    "LIST_REGISTRATIONS_TOOL_NAME",
    "LIST_REGISTRATIONS_TOOL_SCHEMA",
    "RESUME_TOOL_NAME",
    "RESUME_TOOL_SCHEMA",
    "VOIP_TOOLSET",
    "VoipToolHost",
    "active_voip_adapter",
    "hang_up_handler",
    "hold_call_handler",
    "list_registrations_handler",
    "register_voip_tools",
    "resume_call_handler",
    "set_active_adapter",
    "voip_pre_tool_call",
]

_log = logging.getLogger(__name__)

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
    adapter = _ACTIVE_ADAPTER
    if adapter is None:
        return json.dumps({"error": "no active VoIP adapter; cannot resume the call"})
    call_id = _current_call_id()
    if call_id is None:
        return json.dumps({"error": "no active call in this session to resume"})
    resumed = await adapter.resume_call(call_id)
    if not resumed:
        return json.dumps({"error": "the call is not active (unknown or ended)"})
    return json.dumps({"result": "Caller resumed."})


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
    adapter = _ACTIVE_ADAPTER
    if adapter is None:
        return json.dumps(
            {"error": "no active VoIP adapter; cannot list registrations"}
        )
    return json.dumps({"result": adapter.list_registrations_text()})


def voip_pre_tool_call(
    tool_name: str = "",
    args: Mapping[str, object] | None = None,  # noqa: ARG001 — hook arity; args unused by the VoIP gate
    **_kwargs: object,
) -> dict[str, str] | None:
    """``pre_tool_call`` gate for the VoIP tools (ADR-0009/0011/0020/0026).

    The Hermes runtime invokes every registered ``pre_tool_call`` hook before a
    tool runs and blocks the tool when a hook returns
    ``{"action": "block", "message": ...}`` (any other return allows it). This
    gate applies :func:`gate_voip_tool` to the VoIP tools using the current call's
    guard state (privilege level + degraded flag); a tool name we do not own is
    not ours to judge, so we return ``None`` (defer to other hooks / allow).

    The privilege clamp is the security spine: ``hang_up`` is SAFE (never blocked),
    but ``hold_call`` / ``resume_call`` / ``list_registrations`` are ELEVATED, so a
    level-0 (untrusted/receptionist) caller — or any ``degraded`` session — is
    BLOCKED here even if a prompt injection coaxes the model into calling the tool.
    An unknown call context falls back to a level-0 state, so it can never
    accidentally grant a privileged tool (fail safe).

    ``confirmed`` is hard-wired ``False`` here: a model-influenced confirmation
    would defeat the ADR-0010 spoof-resistant requirement, and the spoof-resistant
    DTMF confirmation channel is not wired into the live adapter. No IRREVERSIBLE
    VoIP tool is exposed, so no tool depends on ``confirmed`` today; the moment one
    is, its confirmation MUST be sourced from the DTMF control path, never from a
    tool argument.
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
    # ``confirmed=False`` always (see docstring): the ADR-0010 confirmation channel
    # is out-of-band DTMF, never a tool argument. Every exposed tool is SAFE or
    # ELEVATED — neither consults ``confirmed`` — so this is exact, not a stub.
    if not gate_voip_tool(tool_name, state, confirmed=False):
        return {
            "action": "block",
            "message": f"The {tool_name} tool is not permitted on this call.",
        }
    return None


def _voip_tool_names() -> frozenset[str]:
    """The tool names this plugin's gate is responsible for (every exposed tool).

    Must list EVERY tool :func:`register_voip_tools` registers: a tool absent here
    would have the gate ``return None`` (defer) for it, bypassing the privilege
    clamp. The IRREVERSIBLE transfer tools are intentionally absent because they
    are not registered (deferred — no spoof-resistant DTMF confirmation wired).
    """
    return frozenset(
        {
            HANG_UP_TOOL_NAME,
            HOLD_TOOL_NAME,
            RESUME_TOOL_NAME,
            LIST_REGISTRATIONS_TOOL_NAME,
        }
    )


@dataclass(frozen=True, slots=True)
class _ToolSpec:
    """One agent tool to register: its name, schema, handler, summary, and emoji."""

    name: str
    schema: dict[str, object]
    handler: object
    description: str
    emoji: str


# Every tool exposed to the agent (the gate's ``_voip_tool_names`` MUST cover the
# same set). The IRREVERSIBLE transfer tools are intentionally absent — deferred,
# not registered, until a spoof-resistant DTMF confirmation channel is wired
# (registering an always-blocked transfer would be a no-op, rule 6).
_VOIP_TOOLS: tuple[_ToolSpec, ...] = (
    _ToolSpec(
        name=HANG_UP_TOOL_NAME,
        schema=HANG_UP_TOOL_SCHEMA,
        handler=hang_up_handler,
        description="End the current phone call when the conversation is done.",
        emoji="\U0001f4f4",  # mobile phone off
    ),
    _ToolSpec(
        name=HOLD_TOOL_NAME,
        schema=HOLD_TOOL_SCHEMA,
        handler=hold_call_handler,
        description="Place the current caller on hold (privileged calls only).",
        emoji="⏸️",  # pause button
    ),
    _ToolSpec(
        name=RESUME_TOOL_NAME,
        schema=RESUME_TOOL_SCHEMA,
        handler=resume_call_handler,
        description="Resume a caller you placed on hold (privileged calls only).",
        emoji="▶️",  # play button
    ),
    _ToolSpec(
        name=LIST_REGISTRATIONS_TOOL_NAME,
        schema=LIST_REGISTRATIONS_TOOL_SCHEMA,
        handler=list_registrations_handler,
        description="List this system's phone registrations (privileged calls only).",
        emoji="\U0001f4cb",  # clipboard
    ),
)


def register_voip_tools(ctx: object) -> None:
    """Register the VoIP agent tools + the pre-tool-call gate (ADR-0026/0011).

    Registers ``hang_up`` (SAFE) and the in-call control tools ``hold_call`` /
    ``resume_call`` / ``list_registrations`` (ELEVATED), each through
    ``ctx.register_tool`` and all behind the single ``pre_tool_call`` gate so the
    privilege clamp governs every one. The IRREVERSIBLE transfer tools are NOT
    registered (deferred — see the module docstring).

    Best-effort and resilient: a runtime whose ``PluginContext`` predates
    ``register_tool`` / ``register_hook`` (older hermes-agent) simply does not get
    the tools — the platform still registers. Mirrors the ``getattr`` guard
    :func:`hermes_voip.plugin.register` already uses for ``register_platform``.

    Args:
        ctx: The Hermes ``PluginContext`` (typed ``object`` at this boundary —
            this module imports no hermes runtime).
    """
    register_tool: _RegisterTool | None = getattr(ctx, "register_tool", None)
    if register_tool is not None:
        for spec in _VOIP_TOOLS:
            register_tool(
                spec.name,
                VOIP_TOOLSET,
                spec.schema,
                spec.handler,
                is_async=True,
                description=spec.description,
                emoji=spec.emoji,
            )
    else:
        _log.warning("register(ctx): ctx has no register_tool — VoIP tools skipped")

    register_hook: _RegisterHook | None = getattr(ctx, "register_hook", None)
    if register_hook is not None:
        register_hook("pre_tool_call", voip_pre_tool_call)
    else:
        _log.warning("register(ctx): ctx has no register_hook — VoIP tool gate skipped")
