"""Config-keyed provider selection (ADR-0004).

Provider choice is config, never code: a registry maps a name to a zero-arg
factory and resolves the active provider at startup. Each provider family (ASR,
TTS, guard, transport) owns one ``ProviderRegistry``; concrete implementations
(ADR-0006/0007/0009/0005) register themselves into it. Unknown names raise —
never swallowed (rule 37).
"""

from __future__ import annotations

from collections.abc import Callable


class ProviderRegistry[T]:
    """A name -> factory map for one provider family, resolved at startup."""

    def __init__(self, kind: str) -> None:
        """Create an empty registry labelled ``kind`` (used in error messages)."""
        self._kind = kind
        self._factories: dict[str, Callable[[], T]] = {}

    def register(self, name: str, factory: Callable[[], T]) -> None:
        """Register ``factory`` under ``name``.

        Raises:
            ValueError: If ``name`` is already registered (no silent shadowing).
        """
        if name in self._factories:
            msg = f"{self._kind} provider already registered: {name!r}"
            raise ValueError(msg)
        self._factories[name] = factory

    def make(self, name: str) -> T:
        """Instantiate the provider registered under ``name``.

        Raises:
            ValueError: If ``name`` is not registered.
        """
        try:
            factory = self._factories[name]
        except KeyError as exc:
            msg = f"unknown {self._kind} provider: {name!r}"
            raise ValueError(msg) from exc
        return factory()

    def names(self) -> tuple[str, ...]:
        """Return the registered provider names, sorted."""
        return tuple(sorted(self._factories))
