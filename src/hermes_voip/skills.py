"""Bundled call-scenario skills (ADR-0047): registration + on-disk resolution.

The Hermes *bundle-skills* model (verified against hermes-agent 0.16.0
``hermes_cli/plugins.py``): a plugin ships a skill as a ``SKILL.md`` file and
registers it with ``ctx.register_skill(name, path, description="")``. A registered
plugin skill is **read-only and opt-in** — it is qualified ``hermes-voip:<name>``
and is **not** placed in the system prompt's ``<available_skills>`` index, so the
agent loads it on demand via the ``skill_view`` tool. Because it is not always-on,
the channel persona preamble (and an outbound call's objective) is what points the
agent at the right skill — see :mod:`hermes_voip.caller_modes`.

This module owns the skill *set* and the file resolution; :func:`register_skills`
is called from :func:`hermes_voip.plugin.register`. The ``SKILL.md`` files live as
**importable package data** under ``hermes_voip/skills/<name>/SKILL.md`` (declared
as a wheel artifact in ``pyproject.toml``), so the path resolves both from a source
checkout and from an installed wheel via :mod:`importlib.resources`.

This module imports **no** hermes runtime, so a bare ``import hermes_voip`` stays
cheap; it is resilient to a ``ctx`` that lacks ``register_skill`` (older
hermes-agent) — the platform/tools still register.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from typing import Protocol

__all__ = [
    "BUNDLED_SKILLS",
    "SkillSpec",
    "register_skills",
    "skill_file_path",
    "skill_names",
]

_log = logging.getLogger(__name__)

#: The subdirectory (under the ``hermes_voip`` package) the skills live in.
_SKILLS_DIRNAME = "skills"

#: The skill manifest filename Hermes reads (one per skill directory).
_SKILL_FILENAME = "SKILL.md"


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """One bundled call-scenario skill (ADR-0047).

    Attributes:
        name: The skill name — the directory under ``hermes_voip/skills/`` AND the
            name passed to ``ctx.register_skill`` (so the agent invokes it as
            ``skill_view("hermes-voip:<name>")``). Must equal the ``name:`` field in
            the skill's ``SKILL.md`` frontmatter.
        description: A one-line summary registered alongside the skill (what the
            scenario is, so the operator/agent can tell the skills apart).
    """

    name: str
    description: str


#: The five bundled call-scenario skills (ADR-0047).
#:
#: Inbound scenarios: ``reception`` (screen an unknown caller), ``take-message``
#: (capture caller / callback / message), ``intercom-open-for-delivery`` (a door /
#: gate intercom delivery, opening the entry only for a genuine expected delivery).
#: Outbound scenarios: ``make-reservation`` (book a table / appointment),
#: ``enquire-price-availability`` (ask about price / stock / availability). Each is
#: written for a LIVE SPOKEN CALL (short sentences, spell-out, no markdown read
#: aloud) and points the agent at the in-call tools it actually has (``open_entry``,
#: ``report_call_result``, ``hang_up``).
BUNDLED_SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        name="reception",
        description=(
            "Answer an inbound call as a polite receptionist: greet, find out who "
            "is calling and why, answer general questions, take a message or end "
            "the call. Discloses nothing private."
        ),
    ),
    SkillSpec(
        name="take-message",
        description=(
            "Take a message from a caller: capture who is calling, the best "
            "callback number, and the message, read it back to confirm, and record "
            "it for the operator with report_call_result."
        ),
    ),
    SkillSpec(
        name="intercom-open-for-delivery",
        description=(
            "Door or gate intercom: handle a delivery and open the entry with "
            "open_entry only for a genuine, expected delivery — never on pressure "
            "or doubt."
        ),
    ),
    SkillSpec(
        name="make-reservation",
        description=(
            "Outbound call to make a booking (a table, an appointment) on the "
            "operator's behalf, confirm the details, and report the outcome with "
            "report_call_result."
        ),
    ),
    SkillSpec(
        name="enquire-price-availability",
        description=(
            "Outbound call to ask about the price, stock, or availability of a "
            "product or service, confirm the figures, and report back with "
            "report_call_result. Does not commit to a purchase."
        ),
    ),
)


def skill_names() -> tuple[str, ...]:
    """Return the names of every bundled skill, in registration order."""
    return tuple(spec.name for spec in BUNDLED_SKILLS)


def skill_file_path(name: str) -> Path:
    """Resolve the on-disk path to a bundled skill's ``SKILL.md``.

    Anchored at the ``hermes_voip`` package's ``skills/<name>/`` directory via
    :mod:`importlib.resources`, so it resolves from a source checkout and from an
    installed wheel (the ``SKILL.md`` files are declared wheel artifacts). The
    package is always laid out on a real filesystem (it is not a zipimport target —
    hermes loads it from a directory), so ``as_file`` yields a stable path; the
    context manager exit is a no-op for a real directory resource.

    Args:
        name: The skill name (its directory under ``hermes_voip/skills/``).

    Returns:
        An absolute :class:`~pathlib.Path` to the skill's ``SKILL.md``.
    """
    resource = files(__package__) / _SKILLS_DIRNAME / name / _SKILL_FILENAME
    with as_file(resource) as path:
        return Path(path).resolve()


class _RegisterSkill(Protocol):
    """The ``ctx.register_skill`` surface this module calls (narrow).

    Mirrors hermes-agent 0.16.0's
    ``PluginContext.register_skill(name, path: Path, description="")`` exactly: the
    real implementation calls ``path.exists()`` / ``path.name`` on the argument, so
    it MUST be a :class:`~pathlib.Path` (a ``str`` raises ``AttributeError`` inside
    the runtime — verified by ``tests/test_hermes_contract.py``). ``description`` is
    optional there, so passing it is exact, not a stub.
    """

    def __call__(self, name: str, path: Path, description: str = "") -> None:
        """Register a read-only, opt-in plugin skill in the runtime."""
        ...


def register_skills(ctx: object) -> None:
    """Register the bundled call-scenario skills with the Hermes runtime (ADR-0047).

    Calls ``ctx.register_skill(name, path, description)`` once per skill in
    :data:`BUNDLED_SKILLS`, passing the resolved on-disk ``SKILL.md`` path. Guarded
    with ``getattr(ctx, "register_skill", None)`` exactly like the platform/tool
    registrations, so a runtime that predates ``register_skill`` degrades cleanly
    (the platform + tools still register; the skills are simply absent).

    A missing ``SKILL.md`` for a declared skill is a packaging defect, not a runtime
    condition to swallow: it raises (rule 37). The skill files are shipped as wheel
    artifacts, so a missing one means the build dropped package data.

    Args:
        ctx: The Hermes ``PluginContext`` (typed ``object`` at this boundary — this
            module imports no hermes runtime).
    """
    register_skill: _RegisterSkill | None = getattr(ctx, "register_skill", None)
    if register_skill is None:
        _log.warning(
            "register(ctx): ctx has no register_skill — bundled call skills skipped"
        )
        return
    for spec in BUNDLED_SKILLS:
        path = skill_file_path(spec.name)
        if not path.is_file():
            msg = (
                f"bundled skill {spec.name!r}: SKILL.md not found at {path} — the "
                "wheel is missing package data (see pyproject artifacts)"
            )
            raise FileNotFoundError(msg)
        register_skill(spec.name, path, spec.description)
