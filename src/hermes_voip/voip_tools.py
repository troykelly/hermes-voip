"""Agent-facing VoIP tool registration + the pre-tool-call gate (ADR-0026).

The plugin previously registered ONLY the platform, so a live agent had no way to
end a call — a usability gap the operator hit on a real call. This module wires
the agent ``hang_up`` tool into the Hermes runtime and gates it through the
ADR-0009/0011 tool policy.

Design constraints:

* **Light imports.** This module imports no hermes-agent runtime at module top
  (so ``import hermes_voip`` stays cheap). The handler reads the Hermes session
  context (``gateway.session_context``) lazily, only when invoked at runtime.
* **Finding the call.** A tool handler runs inside the agent's turn and does not
  receive the call id directly, but the Hermes session's ``chat_id`` IS the SIP
  ``Call-ID`` (ADR-0002: one call = one DM session keyed by Call-ID). The handler
  reads it from the task-local session context, so it ends exactly the call whose
  turn is being processed — concurrency-safe across simultaneous calls.
* **The adapter.** The live :class:`~hermes_voip.adapter.VoipAdapter` registers
  itself here (``set_active_adapter``) when it connects, so the handler can reach
  the per-call session map. There is one voip adapter per gateway process.

The hangup is SOFT (ADR-0026): the tool sends BYE and ends the call loop, which
routes through the adapter teardown chokepoint as AGENT_HANGUP — a NORMAL end
that keeps the Hermes session open for follow-up, never a hard ``/stop``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from hermes_voip.providers.policy import GuardSessionState
from hermes_voip.tools import gate_voip_tool

__all__ = [
    "HANG_UP_TOOL_NAME",
    "HANG_UP_TOOL_SCHEMA",
    "VOIP_TOOLSET",
    "VoipToolHost",
    "active_voip_adapter",
    "hang_up_handler",
    "register_voip_tools",
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


@runtime_checkable
class VoipToolHost(Protocol):
    """The adapter surface the VoIP tools drive (``VoipAdapter`` satisfies it).

    A narrow Protocol so this module needs no concrete import of (or dependency
    on) the hermes-importing adapter: the handler reaches the live call through
    just these two members.
    """

    def guard_state_for(self, call_id: str) -> GuardSessionState | None:
        """Return the per-call guard state, or ``None`` if the call is unknown."""
        ...

    async def hang_up_call(self, call_id: str) -> bool:
        """End the call (SOFT agent hangup); return whether a call was ended."""
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


def voip_pre_tool_call(
    tool_name: str = "",
    args: Mapping[str, object] | None = None,  # noqa: ARG001 — hook arity; args unused by the VoIP gate
    **_kwargs: object,
) -> dict[str, str] | None:
    """``pre_tool_call`` gate for the VoIP tools (ADR-0009/0011/0026).

    The Hermes runtime invokes every registered ``pre_tool_call`` hook before a
    tool runs and blocks the tool when a hook returns
    ``{"action": "block", "message": ...}`` (any other return allows it). This
    gate applies :func:`gate_voip_tool` to the VoIP tools using the current call's
    guard state (privilege level + degraded flag); a tool name we do not own is
    not ours to judge, so we return ``None`` (defer to other hooks / allow).

    ``hang_up`` is SAFE, so it is never blocked here — but routing it through the
    same gate means the moment a higher-risk VoIP tool is added (intercom lane #8)
    it is already gated, and a missed injection cannot reach a privileged VoIP
    action by bypassing this hook.
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
    # No DTMF confirmation is wired through this hook yet (the hangup is SAFE and
    # needs none); a future IRREVERSIBLE VoIP tool routes confirmation here.
    if not gate_voip_tool(tool_name, state, confirmed=False):
        return {
            "action": "block",
            "message": f"The {tool_name} tool is not permitted on this call.",
        }
    return None


def _voip_tool_names() -> frozenset[str]:
    """The set of tool names this plugin's gate is responsible for."""
    return frozenset({HANG_UP_TOOL_NAME})


def register_voip_tools(ctx: object) -> None:
    """Register the VoIP agent tools + the pre-tool-call gate on ``ctx`` (ADR-0026).

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
        register_tool(
            HANG_UP_TOOL_NAME,
            VOIP_TOOLSET,
            HANG_UP_TOOL_SCHEMA,
            hang_up_handler,
            is_async=True,
            description="End the current phone call when the conversation is done.",
            emoji="\U0001f4f4",  # mobile phone off
        )
    else:
        _log.warning("register(ctx): ctx has no register_tool — VoIP tools skipped")

    register_hook: _RegisterHook | None = getattr(ctx, "register_hook", None)
    if register_hook is not None:
        register_hook("pre_tool_call", voip_pre_tool_call)
    else:
        _log.warning("register(ctx): ctx has no register_hook — VoIP tool gate skipped")
