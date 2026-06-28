"""Tests for wildcard/pattern matching in outbound config env vars (issue #355).

All three outbound-config env vars gain optional wildcard/glob pattern matching
so operators can express intent concisely without enumerating every individual
origin, target, or result channel:

- ``HERMES_VOIP_PROACTIVE_CALL_FROM=telegram:*`` -- allow all Telegram origins
- ``HERMES_VOIP_OUTBOUND_ALLOW=10**`` or ``10xx`` -- allow all 10xx extensions
- ``HERMES_VOIP_OUTBOUND_RESULT_CHANNEL=telegram:*`` -- route results to the
  originating telegram chat

Semantics (per the corrected orchestrator brief):

* Wildcard is **opt-in per entry**: an entry WITHOUT ``*`` or ``x`` stays
  exact-match -- backwards compatible.
* ``x`` in a pattern for ``OUTBOUND_ALLOW`` is a digit wildcard (one digit 0-9).
* ``*`` in any pattern matches any character sequence (fnmatch convention).
* **Fail-closed**: empty/unset still means deny-all for ``PROACTIVE_CALL_FROM``
  and ``OUTBOUND_ALLOW``.
* For ``OUTBOUND_RESULT_CHANNEL``: an exact entry is a fixed destination (current
  behaviour); a wildcard entry resolves the destination from the originating
  ``platform:chat_id`` when it matches the pattern.

Public-repo invariant: fake origins/targets only (``telegram:chat-1000``,
``1000``..``1099``, ``pbx.example.test``).
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from hermes_voip.outbound_allow import is_outbound_allowed, load_outbound_allowlist

# ---------------------------------------------------------------------------
# HERMES_VOIP_OUTBOUND_ALLOW -- wildcard matching
# ---------------------------------------------------------------------------


class TestOutboundAllowWildcard:
    """Wildcard pattern entries in ``HERMES_VOIP_OUTBOUND_ALLOW``."""

    # --- 10** pattern (glob-style: * = any sequence) ---

    def test_double_star_matches_4digit_10xx_extension(self) -> None:
        """``10**`` matches 1000..1099 (10 + any two chars)."""
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "10**"})
        assert is_outbound_allowed("1000", allow) is True
        assert is_outbound_allowed("1099", allow) is True

    def test_double_star_does_not_match_outside_prefix(self) -> None:
        """``10**`` does not match 1100, 2000, or empty."""
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "10**"})
        assert is_outbound_allowed("1100", allow) is False
        assert is_outbound_allowed("2000", allow) is False
        assert is_outbound_allowed("", allow) is False

    # --- 10xx pattern (x = digit wildcard, 0-9) ---

    def test_10xx_matches_4digit_extensions(self) -> None:
        """``10xx`` matches 1000..1099 (10 + exactly two digits)."""
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "10xx"})
        assert is_outbound_allowed("1000", allow) is True
        assert is_outbound_allowed("1099", allow) is True
        assert is_outbound_allowed("1050", allow) is True

    def test_10xx_does_not_match_non_digit_suffix(self) -> None:
        """``10xx`` rejects any suffix that contains non-digit characters."""
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "10xx"})
        assert is_outbound_allowed("10ab", allow) is False
        assert is_outbound_allowed("10a0", allow) is False

    def test_10xx_does_not_match_wrong_prefix(self) -> None:
        """``10xx`` does not match 1100, 2000, or different prefix."""
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "10xx"})
        assert is_outbound_allowed("1100", allow) is False
        assert is_outbound_allowed("2000", allow) is False

    def test_10xx_does_not_match_too_short_or_too_long(self) -> None:
        """``10xx`` requires exactly two digit positions after ``10``."""
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "10xx"})
        assert is_outbound_allowed("100", allow) is False
        assert is_outbound_allowed("10000", allow) is False

    # --- exact entries remain exact ---

    def test_exact_entry_not_affected_by_wildcard_support(self) -> None:
        """Exact entries (no wildcard chars) still use exact membership."""
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "1000,1001"})
        assert is_outbound_allowed("1000", allow) is True
        assert is_outbound_allowed("1001", allow) is True
        # Unlisted ext shares a prefix but is still rejected.
        assert is_outbound_allowed("10000", allow) is False

    def test_mixed_exact_and_wildcard(self) -> None:
        """A list may mix exact entries and wildcard entries."""
        allow = load_outbound_allowlist(
            {"HERMES_VOIP_OUTBOUND_ALLOW": "1000,20xx,sip:ext@pbx.example.test"}
        )
        assert is_outbound_allowed("1000", allow) is True
        assert is_outbound_allowed("2055", allow) is True
        assert is_outbound_allowed("sip:ext@pbx.example.test", allow) is True
        assert is_outbound_allowed("1001", allow) is False

    # --- fail-closed / empty ---

    def test_empty_allow_still_denies_all(self) -> None:
        """An empty allowlist denies everything (unchanged)."""
        allow = load_outbound_allowlist({})
        assert is_outbound_allowed("1000", allow) is False
        assert is_outbound_allowed("", allow) is False

    def test_unset_deny_all(self) -> None:
        """Absent env still denies all targets (default inert behaviour unchanged)."""
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": ""})
        assert is_outbound_allowed("1000", allow) is False


# ---------------------------------------------------------------------------
# HERMES_VOIP_PROACTIVE_CALL_FROM -- wildcard matching
# ---------------------------------------------------------------------------


def _inject_session(
    monkeypatch: pytest.MonkeyPatch, platform: str, chat_id: str
) -> None:
    """Inject a fake ``gateway.session_context`` returning (platform, chat_id)."""
    module = ModuleType("gateway.session_context")
    values = {
        "HERMES_SESSION_PLATFORM": platform,
        "HERMES_SESSION_CHAT_ID": chat_id,
    }

    def _get_session_env(key: str) -> str:
        return values.get(key, "")

    setattr(module, "get_session_env", _get_session_env)  # noqa: B010
    monkeypatch.setitem(sys.modules, "gateway", ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)


class TestProactiveCallFromWildcard:
    """Wildcard pattern entries in ``HERMES_VOIP_PROACTIVE_CALL_FROM``."""

    def test_wildcard_allows_any_telegram_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PROACTIVE_CALL_FROM=telegram:*`` allows any telegram chat_id."""
        from hermes_voip.voip_tools import (  # noqa: PLC0415
            PLACE_CALL_TOOL_NAME,
            set_active_adapter,
            voip_pre_tool_call,
        )

        monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:*")
        set_active_adapter(None)
        _inject_session(monkeypatch, "telegram", "chat-1000")

        result = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})
        assert result is None, f"Expected allowed (None) but got {result!r}"

    def test_wildcard_allows_different_telegram_chat_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``telegram:*`` allows any telegram chat, not just a specific one."""
        from hermes_voip.voip_tools import (  # noqa: PLC0415
            PLACE_CALL_TOOL_NAME,
            set_active_adapter,
            voip_pre_tool_call,
        )

        monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:*")
        set_active_adapter(None)
        _inject_session(monkeypatch, "telegram", "chat-9999")

        result = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})
        assert result is None, f"Expected allowed for chat-9999 but got {result!r}"

    def test_wildcard_blocks_non_matching_platform(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PROACTIVE_CALL_FROM=telegram:*`` does NOT allow other platforms."""
        from hermes_voip.voip_tools import (  # noqa: PLC0415
            PLACE_CALL_TOOL_NAME,
            set_active_adapter,
            voip_pre_tool_call,
        )

        monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:*")
        set_active_adapter(None)
        _inject_session(monkeypatch, "slack", "chat-1000")

        verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})
        assert verdict is not None
        assert verdict["action"] == "block"

    def test_exact_entry_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exact ``telegram:chat-1000`` still works as before (backwards compat)."""
        from hermes_voip.voip_tools import (  # noqa: PLC0415
            PLACE_CALL_TOOL_NAME,
            set_active_adapter,
            voip_pre_tool_call,
        )

        monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:chat-1000")
        set_active_adapter(None)
        _inject_session(monkeypatch, "telegram", "chat-1000")

        result = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})
        assert result is None

    def test_exact_entry_does_not_match_different_chat_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exact entry ``telegram:chat-1000`` blocks ``telegram:chat-9999``."""
        from hermes_voip.voip_tools import (  # noqa: PLC0415
            PLACE_CALL_TOOL_NAME,
            set_active_adapter,
            voip_pre_tool_call,
        )

        monkeypatch.setenv("HERMES_VOIP_PROACTIVE_CALL_FROM", "telegram:chat-1000")
        set_active_adapter(None)
        _inject_session(monkeypatch, "telegram", "chat-9999")

        verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})
        assert verdict is not None
        assert verdict["action"] == "block"

    def test_unset_still_denies_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset ``PROACTIVE_CALL_FROM`` => all proactive calls blocked (unchanged)."""
        from hermes_voip.voip_tools import (  # noqa: PLC0415
            PLACE_CALL_TOOL_NAME,
            set_active_adapter,
            voip_pre_tool_call,
        )

        monkeypatch.delenv("HERMES_VOIP_PROACTIVE_CALL_FROM", raising=False)
        set_active_adapter(None)
        _inject_session(monkeypatch, "telegram", "chat-1000")

        verdict = voip_pre_tool_call(tool_name=PLACE_CALL_TOOL_NAME, args={})
        assert verdict is not None
        assert verdict["action"] == "block"


# ---------------------------------------------------------------------------
# HERMES_VOIP_OUTBOUND_RESULT_CHANNEL -- wildcard / pattern matching
# ---------------------------------------------------------------------------


class TestResultChannelWildcard:
    """Wildcard pattern in ``HERMES_VOIP_OUTBOUND_RESULT_CHANNEL``."""

    def test_exact_channel_resolves_as_fixed_destination(self) -> None:
        """Exact ``telegram:chat-1000`` resolves to fixed ``(telegram, chat-1000)``."""
        from hermes_voip.adapter import _resolve_result_channel  # noqa: PLC0415

        dest = _resolve_result_channel("telegram:chat-1000", None)
        assert dest == ("telegram", "chat-1000")

    def test_exact_channel_with_origin_uses_fixed_destination(self) -> None:
        """Exact entry is always the fixed destination regardless of origin."""
        from hermes_voip.adapter import _resolve_result_channel  # noqa: PLC0415

        dest = _resolve_result_channel("telegram:chat-1000", ("telegram", "chat-9999"))
        assert dest == ("telegram", "chat-1000")

    def test_wildcard_channel_with_matching_origin_derives_destination(self) -> None:
        """Wildcard ``telegram:*`` + matching origin => origin is the destination."""
        from hermes_voip.adapter import _resolve_result_channel  # noqa: PLC0415

        dest = _resolve_result_channel("telegram:*", ("telegram", "chat-1000"))
        assert dest == ("telegram", "chat-1000")

    def test_wildcard_channel_with_non_matching_origin_returns_none(self) -> None:
        """Wildcard ``telegram:*`` with a ``slack`` origin does NOT match => None."""
        from hermes_voip.adapter import _resolve_result_channel  # noqa: PLC0415

        dest = _resolve_result_channel("telegram:*", ("slack", "chat-1000"))
        assert dest is None

    def test_wildcard_channel_with_no_origin_returns_none(self) -> None:
        """Wildcard ``telegram:*`` with no origin (cron path) => None (log-only)."""
        from hermes_voip.adapter import _resolve_result_channel  # noqa: PLC0415

        dest = _resolve_result_channel("telegram:*", None)
        assert dest is None

    def test_none_channel_returns_none(self) -> None:
        """None channel string => None (unset => log-only, unchanged)."""
        from hermes_voip.adapter import _resolve_result_channel  # noqa: PLC0415

        assert _resolve_result_channel(None, ("telegram", "chat-1000")) is None

    def test_blank_channel_returns_none(self) -> None:
        """Blank/empty channel string => None."""
        from hermes_voip.adapter import _resolve_result_channel  # noqa: PLC0415

        assert _resolve_result_channel("", None) is None
        assert _resolve_result_channel("  ", None) is None

    def test_wildcard_channel_matches_any_telegram_chat(self) -> None:
        """``telegram:*`` matches any telegram chat_id -- pattern is per-origin."""
        from hermes_voip.adapter import _resolve_result_channel  # noqa: PLC0415

        assert _resolve_result_channel("telegram:*", ("telegram", "chat-9999")) == (
            "telegram",
            "chat-9999",
        )
        assert _resolve_result_channel(
            "telegram:*", ("telegram", "chat-1234567890")
        ) == ("telegram", "chat-1234567890")
