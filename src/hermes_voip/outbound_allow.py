"""Outbound dial allowlist -- the hard gate for agent-triggered calls (ADR-0029).

``HERMES_VOIP_OUTBOUND_ALLOW`` is a comma-separated list of permitted dial targets
(extensions and/or SIP URIs). It is the **hard gate** on the agent-facing
``place_call`` tool: the handler refuses to dial any target not on the allowlist.

**The default is EMPTY = no target is permitted** -- the agent-initiated outbound
feature ships INERT until the operator opts numbers in (the safe ship). An empty or
absent value parses to an empty :class:`OutboundAllowlist`, which permits nothing.

PII posture: a dial target (an extension, or potentially a real PSTN number) lives
ONLY in the gitignored ``.env`` -- never a tracked file. This module reads the value
from a provided env mapping; tests pass fakes (ext ``1000``/``1001``). Extensions
are not PII, but the value is treated uniformly as sensitive config (kept out of
git and out of logs).

Matching is **exact** for entries that contain no wildcard characters, and
**pattern-based** (opt-in per entry) for entries that contain ``*`` or ``x``:

* In simple dial masks (digits plus ``+`` / ``#`` / ``*`` / ``x``), ``*`` and
  ``x`` each match exactly ONE decimal digit. Example: ``10**`` and ``10xx`` both
  permit only four-digit numbers beginning with ``10`` (1000-1099), not arbitrary
  ``10...`` prefixes.
* In SIP URI / non-mask patterns, ``*`` remains glob-like (any character
  sequence), while ``x`` is literal so values like ``sip:ext@pbx.example.test``
  stay exact unless the operator explicitly adds ``*``.

Wildcard matching is **opt-in**: an entry without ``*`` or ``x`` stays
exact-match, so a listed ``"1000"`` never accidentally also permits ``"10000"``
(the caller-groups escalation lesson from ADR-0021). An allowlist that *grants the
right to dial* must therefore be an exact-match set for exact entries; the pattern
opt-in is explicit per entry.

The security model is unchanged: empty/absent = deny-all (fail-closed), wildcard
entries are still enumerated explicitly by the operator (not implicit), and the
allowlist is the hard gate consulted at the chokepoint before any INVITE is sent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "OUTBOUND_ALLOW_ENV",
    "OUTBOUND_RESULT_CHANNEL_ENV",
    "OutboundAllowlist",
    "is_outbound_allowed",
    "load_outbound_allowlist",
    "resolve_result_channel",
]

#: The environment variable naming the comma-separated outbound dial allowlist.
OUTBOUND_ALLOW_ENV = "HERMES_VOIP_OUTBOUND_ALLOW"


def _is_sip_uri(entry: str) -> bool:
    """Return True iff ``entry`` looks like a SIP URI (has ``@`` or ``sip:`` prefix).

    SIP URIs legitimately contain alphabetic characters (``ext``, ``pbx``,
    ``example``) that would collide with the ``x`` digit-wildcard convention.
    In a SIP URI, ``x`` is ALWAYS a literal character -- only ``*`` triggers
    glob matching. A simple extension (digits + wildcard chars only, no URI
    structure) is the context where ``x`` means "any digit".
    """
    return "@" in entry or entry.lower().startswith("sip:")


def _is_extension_mask(entry: str) -> bool:
    """Return True iff ``entry`` is a simple dial-mask pattern.

    Dial masks are extension/feature-code-shaped entries made only from digits,
    ``+``, ``#``, ``*``, and ``x``/``X``. In this context both ``*`` and ``x`` mean
    exactly ONE decimal digit, so issue examples ``10**`` and ``10xx`` are the same
    fixed-length 10xx mask (1000-1099) rather than a broad ``10...`` prefix glob.
    SIP URIs and other non-mask strings keep glob-style ``*`` semantics instead.
    """
    return all(ch.isdigit() or ch in "+#*xX" for ch in entry) and (
        "*" in entry or "x" in entry or "X" in entry
    )


def _entry_to_regex(entry: str) -> re.Pattern[str] | None:
    """Compile ``entry`` to an anchored regex if it contains wildcards, else None.

    An entry without wildcards is exact (returns ``None``); the caller uses set
    membership for those. When wildcards ARE present, the character set depends
    on whether the entry is a simple extension/dial mask or a SIP URI/string pattern:

    **Simple extension mask** (only digits, ``+``, ``#``, ``*``, and ``x``/``X``):

    * ``x`` / ``X`` -> ``[0-9]`` (exactly one decimal digit).
    * ``*`` -> ``[0-9]`` (also exactly one decimal digit for dial-mask syntax).

    **SIP URI / other pattern**:

    * ``x`` is literal (SIP URIs legitimately contain ``x`` in words like
      ``ext`` or ``example`` -- treating it as a wildcard would match nothing).
    * ``*`` -> ``.*`` (glob any sequence, useful for e.g. ``sip:10*@pbx.example.test``).

    All other characters are regex-escaped so they match literally. The pattern
    is anchored (``^...$``) so ``10xx`` and ``10**`` both match exactly four digits
    beginning with ``10``; neither matches ``10``, ``100``, ``10000``, or ``10ab``.

    Args:
        entry: A single allowlist entry (already trimmed).

    Returns:
        A compiled :class:`re.Pattern` when the entry contains wildcards, else
        ``None`` (exact-match entry).
    """
    is_mask = _is_extension_mask(entry)
    is_uri = _is_sip_uri(entry)
    has_wildcard = "*" in entry or (("x" in entry or "X" in entry) and not is_uri)
    if not has_wildcard:
        return None
    # Build an anchored regex from the pattern.  Walk char-by-char so each
    # literal character is properly escaped while wildcard chars are expanded.
    parts: list[str] = []
    for ch in entry:
        if ch == "*" and is_mask:
            parts.append("[0-9]")
        elif ch == "*":
            parts.append(".*")
        elif ch in {"x", "X"} and not is_uri:
            # Digit wildcard: valid only outside SIP URIs.
            parts.append("[0-9]")
        else:
            parts.append(re.escape(ch))
    return re.compile("^" + "".join(parts) + "$")


@dataclass(frozen=True)
class OutboundAllowlist:
    """The parsed outbound dial allowlist (exact entries + compiled pattern entries).

    Constructed by :func:`load_outbound_allowlist`; consumed by
    :func:`is_outbound_allowed`. The split between exact and pattern entries is an
    internal optimisation (exact entries use O(1) set lookup; patterns use regex
    matching) and an explicit design guard: an entry that contains no wildcard
    characters ALWAYS uses exact membership, so ``"1000"`` never accidentally
    also permits ``"10000"``.

    The empty default (no exact entries, no patterns) permits nothing --
    the feature is inert until the operator opts numbers in.
    """

    _exact: frozenset[str]
    _patterns: tuple[re.Pattern[str], ...]

    def __bool__(self) -> bool:
        """True iff the allowlist contains at least one entry (exact or pattern)."""
        return bool(self._exact) or bool(self._patterns)

    def __eq__(self, other: object) -> bool:
        """Backwards-compatible equality for tests and simple callers.

        Existing tests (and any external caller using these pure helpers) compared
        the old ``frozenset[str]`` return value of :func:`load_outbound_allowlist`
        directly to a ``frozenset``. Preserve that ergonomics for the exact-only
        case: when ``other`` is a ``frozenset[str]``, equality means this allowlist
        has NO pattern entries and its exact entries equal ``other``. Normal
        dataclass-to-dataclass equality still works via the explicit field compare.
        """
        if isinstance(other, OutboundAllowlist):
            return self._exact == other._exact and self._patterns == other._patterns
        if isinstance(other, frozenset):
            return not self._patterns and self._exact == other
        return NotImplemented

    def __hash__(self) -> int:
        """Hash on the exact entries + compiled patterns.

        The dataclass is frozen and semantically immutable. Defining ``__eq__`` for
        backwards compatibility with ``frozenset`` comparisons suppresses the
        dataclass-generated hash, so provide the obvious field-based hash to retain
        hashability (required by lint, and valid because both fields are hashable).
        """
        return hash((self._exact, self._patterns))


def load_outbound_allowlist(extra: Mapping[str, str]) -> OutboundAllowlist:
    """Parse ``HERMES_VOIP_OUTBOUND_ALLOW`` into an :class:`OutboundAllowlist`.

    A comma-separated list; each entry is trimmed and empty entries are dropped,
    so ``" 1000 , 1001 ,, "`` yields entries ``{"1000", "1001"}``. An absent or
    blank value yields the **empty** allowlist -- the feature is inert (no target
    may be dialled) until the operator opts numbers in.

    Entries containing ``*`` or ``x`` are compiled to anchored regex patterns (opt-in
    per entry); all other entries are exact-match members.

    Args:
        extra: The plugin's env-config mapping (``config.extra``).

    Returns:
        An :class:`OutboundAllowlist` (empty when unset/blank).
    """
    raw = extra.get(OUTBOUND_ALLOW_ENV, "")
    exact: list[str] = []
    patterns: list[re.Pattern[str]] = []
    for raw_entry in raw.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        pat = _entry_to_regex(entry)
        if pat is not None:
            patterns.append(pat)
        else:
            exact.append(entry)
    return OutboundAllowlist(
        _exact=frozenset(exact),
        _patterns=tuple(patterns),
    )


def is_outbound_allowed(
    number: str, allowlist: OutboundAllowlist | frozenset[str]
) -> bool:
    """Return whether ``number`` is a permitted dial target.

    Exact entries are checked via O(1) set membership; pattern entries are
    checked via their compiled anchored regex. The candidate is trimmed before
    matching so a stray surrounding space neither bypasses nor falsely blocks the
    gate. An empty candidate is never allowed. With an empty allowlist (the
    default) nothing is permitted (fail-closed).

    Args:
        number: The dial target requested by the agent (extension or SIP URI).
        allowlist: The permitted allowlist from :func:`load_outbound_allowlist`;
            a legacy exact-only ``frozenset[str]`` is also accepted for compatibility.

    Returns:
        ``True`` iff the trimmed ``number`` is permitted by an exact entry or a
        compiled pattern in ``allowlist``.
    """
    candidate = number.strip()
    if not candidate:
        return False
    if isinstance(allowlist, frozenset):
        # Backwards compatibility for exact-only callers/tests that still hold the
        # pre-issue-355 representation. Pattern matching requires the typed
        # OutboundAllowlist returned by load_outbound_allowlist.
        return candidate in allowlist
    if candidate in allowlist._exact:
        return True
    return any(pat.fullmatch(candidate) is not None for pat in allowlist._patterns)


# ---------------------------------------------------------------------------
# Result-channel helpers -- channel target resolution for OUTBOUND_RESULT_CHANNEL
# ---------------------------------------------------------------------------

#: The environment variable naming the no-origin fallback result channel.
OUTBOUND_RESULT_CHANNEL_ENV = "HERMES_VOIP_OUTBOUND_RESULT_CHANNEL"


def _parse_channel_target(channel: str | None) -> tuple[str, str] | None:
    """Parse a ``platform:chat_id`` channel string into a pair, or None.

    The no-origin fallback target ``HERMES_VOIP_OUTBOUND_RESULT_CHANNEL`` is a
    ``platform:chat_id`` string (split on the FIRST ``:`` so a chat_id may itself
    contain colons). An absent, blank, or shapeless value yields ``None`` so the
    fallback logs only rather than mis-routing.
    """
    if not channel or ":" not in channel:
        return None
    platform, chat_id = channel.split(":", 1)
    platform = platform.strip()
    chat_id = chat_id.strip()
    if not platform or not chat_id:
        return None
    return (platform, chat_id)


def resolve_result_channel(
    channel: str | None,
    origin: tuple[str, str] | None,
) -> tuple[str, str] | None:
    """Resolve ``HERMES_VOIP_OUTBOUND_RESULT_CHANNEL`` to a delivery destination.

    Extends :func:`_parse_channel_target` with optional wildcard/pattern support
    (issue #355): when the configured channel contains ``*``, it is a pattern
    matched against the originating ``platform:chat_id`` rather than a fixed
    destination -- so operators can write ``telegram:*`` to mean "the originating
    telegram chat, whatever its id" without hardcoding every individual chat id.

    Resolution rules:

    1. Absent / blank channel => ``None`` (log-only; unchanged).
    2. No wildcard (exact ``platform:chat_id``) => the fixed destination pair,
       regardless of origin (current behaviour, unchanged).
    3. Wildcard channel (contains ``*``) => match the pattern against the origin's
       ``platform:chat_id`` string; when it matches, return the ORIGIN as the
       destination (so the result lands in the chat that triggered the call); when
       it does NOT match (different platform, or no origin), return ``None``.

    The wildcard is **opt-in per entry** -- an entry without ``*`` stays exact
    (rule from the issue: "exact entries (no wildcard) must preserve current
    behavior as a fixed destination").

    Args:
        channel: The raw ``HERMES_VOIP_OUTBOUND_RESULT_CHANNEL`` value (may be
            ``None`` when unset).
        origin: The captured originating session ``(platform, chat_id)``, or
            ``None`` when no session triggered the call (the cron/CALL_ON_CONNECT
            path).

    Returns:
        The resolved ``(platform, chat_id)`` delivery destination, or ``None``
        when the channel is absent/blank, shapeless, or a wildcard pattern that
        does not match the origin.
    """
    import fnmatch  # noqa: PLC0415 -- lazy import; stdlib, no cost concern

    if not channel or not channel.strip():
        return None
    channel = channel.strip()
    if "*" not in channel:
        # Exact entry: parse as a fixed destination (current behaviour).
        return _parse_channel_target(channel)
    # Wildcard entry: match against the origin.
    if origin is None:
        # No origin (cron/no-trigger path) -- cannot derive destination.
        return None
    origin_platform, origin_chat_id = origin
    origin_str = f"{origin_platform}:{origin_chat_id}"
    if fnmatch.fnmatchcase(origin_str, channel):
        return origin
    return None
