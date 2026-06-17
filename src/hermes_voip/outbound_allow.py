"""Outbound dial allowlist — the hard gate for agent-triggered calls (ADR-0029).

``HERMES_VOIP_OUTBOUND_ALLOW`` is a comma-separated list of permitted dial targets
(extensions and/or SIP URIs). It is the **hard gate** on the agent-facing
``place_call`` tool: the handler refuses to dial any target not on the allowlist.

**The default is EMPTY = no target is permitted** — the agent-initiated outbound
feature ships INERT until the operator opts numbers in (the safe ship). An empty or
absent value parses to an empty frozenset, which permits nothing.

PII posture: a dial target (an extension, or potentially a real PSTN number) lives
ONLY in the gitignored ``.env`` — never a tracked file. This module reads the value
from a provided env mapping; tests pass fakes (ext ``1000``/``1001``). Extensions
are not PII, but the value is treated uniformly as sensitive config (kept out of
git and out of logs).

Matching is **exact** (after trimming whitespace on both the configured entries and
the candidate), deliberately NOT a prefix/wildcard match: the caller-groups work
(ADR-0021) showed that prefix wildcards on a trust-granting list are an escalation
surface (``"*"`` matches everything, a broad prefix matches more than intended). An
allowlist that *grants the right to dial* must therefore be an exact-match set, so a
listed ``"1000"`` never accidentally also permits ``"10000"``.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = [
    "OUTBOUND_ALLOW_ENV",
    "is_outbound_allowed",
    "load_outbound_allowlist",
]

#: The environment variable naming the comma-separated outbound dial allowlist.
OUTBOUND_ALLOW_ENV = "HERMES_VOIP_OUTBOUND_ALLOW"


def load_outbound_allowlist(extra: Mapping[str, str]) -> frozenset[str]:
    """Parse ``HERMES_VOIP_OUTBOUND_ALLOW`` into a set of permitted dial targets.

    A comma-separated list; each entry is trimmed and empty entries are dropped, so
    ``" 1000 , 1001 ,, "`` yields ``{"1000", "1001"}``. An absent or blank value
    yields the **empty** set — the feature is inert (no target may be dialled) until
    the operator opts numbers in.

    Args:
        extra: The plugin's env-config mapping (``config.extra``).

    Returns:
        The frozenset of permitted dial targets (empty when unset/blank).
    """
    raw = extra.get(OUTBOUND_ALLOW_ENV, "")
    return frozenset(entry.strip() for entry in raw.split(",") if entry.strip())


def is_outbound_allowed(number: str, allowlist: frozenset[str]) -> bool:
    """Return whether ``number`` is a permitted dial target (exact, trimmed match).

    The candidate is trimmed before matching so a stray surrounding space neither
    bypasses nor falsely blocks the gate. An empty candidate is never allowed. With
    an empty allowlist (the default) nothing is permitted.

    Args:
        number: The dial target requested by the agent (extension or SIP URI).
        allowlist: The permitted set from :func:`load_outbound_allowlist`.

    Returns:
        ``True`` iff the trimmed ``number`` is an exact member of ``allowlist``.
    """
    candidate = number.strip()
    if not candidate:
        return False
    return candidate in allowlist
