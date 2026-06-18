"""Directory-install entry point for the hermes-voip plugin (ADR-0036).

This is the ``register(ctx)`` the Hermes runtime calls when ``hermes-voip`` is
installed as a **directory plugin** under ``~/.hermes/plugins/hermes-voip/`` (this
directory: ``plugin.yaml`` + this ``__init__.py``). It does NOT reimplement anything —
it re-exports the one real :func:`hermes_voip.plugin.register` from the installed
``hermes_voip`` package, so there is a single implementation and a single
registration regardless of which install model is used.

Why both models exist: the pip / entry-point install (``hermes_agent.plugins`` group
in ``pyproject.toml``) is the package source, but Hermes' ``hermes plugins list`` /
``enable`` / ``/plugins`` commands are filesystem-only — they never read entry-point
metadata (verified against hermes-agent 0.16.0). So this directory manifest is what
surfaces the version + tool-count and lets ``hermes plugins enable hermes-voip``
succeed. See ``docs/runbooks/0011-voip-enable-plugin.md``.

The ``hermes_voip`` package must be importable (it is, once ``pip install hermes-voip``
/ ``uv sync`` has run) — this file deliberately carries no fallback copy of the
registration logic, so it can never drift from the package.
"""

from __future__ import annotations

from hermes_voip.plugin import register

__all__ = ["register"]
