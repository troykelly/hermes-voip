"""Tests for the bundled call-scenario skills (ADR-0047).

TDD rule 18: these tests are written BEFORE the implementation. They drive
``register(ctx)`` with a recording ``ctx`` and assert that each of the five
bundled skills is registered via ``ctx.register_skill(name, path)`` with a path
that exists on disk and carries a valid ``SKILL.md`` YAML frontmatter block.

The Hermes bundle-skills model (verified against hermes-agent 0.16.0
``hermes_cli/plugins.py`` ``PluginContext.register_skill(name, path, description)``):
a skill lives at ``skills/<name>/SKILL.md`` and is registered read-only + opt-in
(it is NOT in the system-prompt ``<available_skills>`` index), so the agent loads
it on demand via the ``skill_view`` tool. Registration must degrade cleanly when a
runtime predates ``register_skill`` (the platform must still register).
"""

from __future__ import annotations

import re
from pathlib import Path

# The five bundled call-scenario skills (ADR-0047). Inbound: reception, take-message,
# intercom-open-for-delivery. Outbound: make-reservation, enquire-price-availability.
_EXPECTED_SKILLS: frozenset[str] = frozenset(
    {
        "intercom-open-for-delivery",
        "make-reservation",
        "enquire-price-availability",
        "reception",
        "take-message",
    }
)

# A minimal YAML-frontmatter matcher: a leading '---' line, a body, then a closing
# '---' line. We only need to prove the file opens with a frontmatter block whose
# body declares 'name:' and 'description:' (the Hermes skill manifest fields).
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<body>.*?)\r?\n---\r?\n", re.DOTALL)


class _SkillRecordingCtx:
    """A PluginContext stand-in recording register_skill (+ the other hooks).

    Mirrors the real ``PluginContext.register_skill(name, path: Path,
    description="")`` arity so :func:`hermes_voip.plugin.register` can call it
    exactly as it would the real runtime. The real implementation calls
    ``path.exists()`` / ``path.name`` on the argument, so ``path`` MUST be a
    :class:`~pathlib.Path` (a ``str`` would raise ``AttributeError`` inside the
    runtime). ``test_each_registered_skill_path_exists_with_valid_frontmatter`` below
    asserts every registered skill is passed a :class:`~pathlib.Path`. The
    platform/tool/hook registrations are accepted and ignored — this fake exists to
    capture the SKILL registrations.
    """

    def __init__(self) -> None:
        self.skill_calls: list[dict[str, object]] = []

    def register_platform(self, name: str, *args: object, **kwargs: object) -> None:
        # Accepted + ignored: this test only inspects skill registration.
        return None

    def register_tool(self, name: str, *args: object, **kwargs: object) -> None:
        return None

    def register_hook(self, hook_name: str, callback: object) -> None:
        return None

    def register_skill(self, name: str, path: Path, description: str = "") -> None:
        self.skill_calls.append(
            {"name": name, "path": path, "description": description}
        )


def test_register_registers_all_five_bundled_skills() -> None:
    """register(ctx) registers each of the five bundled call-scenario skills."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _SkillRecordingCtx()
    register(ctx)
    registered = {c["name"] for c in ctx.skill_calls}
    for name in _EXPECTED_SKILLS:
        assert name in registered, f"bundled skill {name!r} was not registered"


def test_each_registered_skill_path_exists_with_valid_frontmatter() -> None:
    """Each registered skill path exists on disk and is a SKILL.md with frontmatter."""
    from hermes_voip.plugin import register  # noqa: PLC0415

    ctx = _SkillRecordingCtx()
    register(ctx)
    by_name = {c["name"]: c for c in ctx.skill_calls}

    for name in _EXPECTED_SKILLS:
        assert name in by_name, f"bundled skill {name!r} was not registered"
        path_obj = by_name[name]["path"]
        # The real runtime requires a Path (it calls path.exists()/path.name); a str
        # would raise inside hermes-agent. This assertion is the contract guard.
        assert isinstance(path_obj, Path), f"{name}: path must be a pathlib.Path"
        skill_path = path_obj
        assert skill_path.exists(), f"{name}: registered path {path_obj!r} missing"
        assert skill_path.is_file(), f"{name}: registered path is not a file"
        assert skill_path.name == "SKILL.md", (
            f"{name}: registered path must point at a SKILL.md file"
        )

        text = skill_path.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        assert match is not None, f"{name}: SKILL.md lacks a YAML frontmatter block"
        body = match.group("body")
        # The frontmatter must declare the skill name + a description (the Hermes
        # skill manifest fields). The declared name must equal the registered name.
        assert re.search(r"^name:\s*\S", body, re.MULTILINE), (
            f"{name}: frontmatter has no 'name:' field"
        )
        assert re.search(r"^description:\s*\S", body, re.MULTILINE), (
            f"{name}: frontmatter has no 'description:' field"
        )
        declared = re.search(r"^name:\s*(?P<v>.+?)\s*$", body, re.MULTILINE)
        assert declared is not None
        declared_name = declared.group("v").strip().strip("\"'")
        assert declared_name == name, (
            f"{name}: frontmatter name {declared_name!r} != registered name {name!r}"
        )


def test_skills_skipped_cleanly_when_ctx_lacks_register_skill() -> None:
    """A ctx without register_skill must not break plugin load (graceful degrade).

    The bundle-skills wiring is guarded with ``getattr(ctx, "register_skill", None)``
    exactly like the other optional registrations, so a runtime predating
    ``register_skill`` still registers the platform + tools without raising.
    """
    from hermes_voip.plugin import register  # noqa: PLC0415

    class _NoSkillCtx:
        """A ctx that can register platforms/tools/hooks but has NO register_skill."""

        def __init__(self) -> None:
            self.platform_names: list[str] = []

        def register_platform(self, name: str, *args: object, **kwargs: object) -> None:
            self.platform_names.append(name)

        def register_tool(self, name: str, *args: object, **kwargs: object) -> None:
            return None

        def register_hook(self, hook_name: str, callback: object) -> None:
            return None

        # NOTE: deliberately NO register_skill attribute.

    ctx = _NoSkillCtx()
    register(ctx)  # must not raise even though register_skill is absent
    assert "voip" in ctx.platform_names
