"""Tests for wildcard/pattern matching in outbound config env vars (issue #355).

All three outbound-config env vars gain optional wildcard/glob pattern matching
so operators can express intent concisely without enumerating every individual
origin, target, or result channel:

- ``HERMES_VOIP_PROACTIVE_CALL_FROM=telegram:*`` -- allow all Telegram origins
- ``HERMES_VOIP_OUTBOUND_ALLOW=10xx`` -- allow all 10xx extensions (1000..1099)
- ``HERMES_VOIP_OUTBOUND_RESULT_CHANNEL=telegram:*`` -- route results to the
  originating telegram chat

Semantics (per ADR-0029; ``*`` is LITERAL in the dial allowlist):

* ``OUTBOUND_ALLOW`` opt-in wildcard is ``x``/``X`` ONLY, and ONLY inside a simple
  extension mask -- each ``x`` matches exactly one digit (``10xx`` == 1000..1099).
  ``*`` is a LITERAL dial character here, so ``*67`` is an exact star-code (never a
  wildcard) and the ``10**`` spelling is a literal string, not a mask alias. SIP URIs
  and every other entry are exact-only -- no entry ever compiles to a ``.*`` glob.
* ``*`` remains glob-like ONLY for the origin/result-channel ``platform:chat_id``
  patterns (``PROACTIVE_CALL_FROM`` / ``OUTBOUND_RESULT_CHANNEL``, via ``fnmatch``) --
  an intentional divergence from the dial gate, which must never over-match a target.
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

    # --- `10**` is a LITERAL string, NOT a digit mask (`*` literal; use `10xx`) ---

    def test_double_star_is_literal_not_a_digit_mask(self) -> None:
        """``10**`` matches only the literal ``10**`` -- it is NOT the 1000..1099 mask.

        With ``*`` literal, the ``10**`` spelling from issue #355 no longer expands;
        operators use ``10xx`` for the range. Pin that ``10**`` neither denies itself
        nor over-matches any four-digit number.
        """
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "10**"})
        assert is_outbound_allowed("10**", allow) is True
        assert is_outbound_allowed("1000", allow) is False
        assert is_outbound_allowed("1099", allow) is False

    @pytest.mark.parametrize("target", ["10", "100", "10000", "10ab", "1100", "1042"])
    def test_double_star_matches_no_bare_digit_sequence(self, target: str) -> None:
        """The literal ``10**`` matches no bare digit sequence (no ``*`` expansion)."""
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "10**"})
        assert is_outbound_allowed(target, allow) is False

    def test_double_star_literal_denies_wrong_and_empty_targets(self) -> None:
        """``10**`` (literal) denies 1100/2000/empty like any exact entry."""
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

    def test_10xx_mask_regression_guard(self) -> None:
        """Regression guard: the ``10xx`` digit mask is UNCHANGED by the fix.

        Characterisation (passes before AND after the ``*``-literal fix): the primary
        issue-#355 example must keep working -- ``10xx`` matches 1000..1099 (one digit
        per ``x``) and nothing shorter.
        """
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "10xx"})
        assert is_outbound_allowed("1042", allow) is True
        assert is_outbound_allowed("1000", allow) is True
        assert is_outbound_allowed("10", allow) is False

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

    def test_non_mask_entry_is_exact_star_and_x_literal(self) -> None:
        """A non-mask entry is EXACT -- both ``*`` and ``x`` are literal characters."""
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "fax*"})
        assert is_outbound_allowed("fax*", allow) is True
        assert is_outbound_allowed("fax123", allow) is False
        assert is_outbound_allowed("fa9", allow) is False

    def test_literal_x_entry_stays_exact_no_overmatch(self) -> None:
        """A literal entry with ``x`` and no ``*`` stays EXACT -- no digit over-match.

        Security (fail-closed / no over-match): outside a simple dial mask ``x`` is
        never a digit wildcard, so a listed ``fax`` permits exactly ``fax`` and MUST
        NOT authorize ``fa0``..``fa9`` -- targets the operator never enumerated.
        """
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "fax"})
        assert is_outbound_allowed("fax", allow) is True
        assert is_outbound_allowed("fa5", allow) is False

    # --- `*` is a LITERAL dial char, never a wildcard (ADR-0029 over-match fix) ---

    def test_star_feature_code_is_exact_no_overmatch(self) -> None:
        """A star/service feature code (``*67``) is EXACT -- no digit over-match.

        Security (dial gate, no over-match): ``*`` in ``OUTBOUND_ALLOW`` is a LITERAL
        dial character, never a wildcard. A listed ``*67`` permits exactly ``*67`` and
        MUST NOT authorise ``067``..``967`` -- ten targets the operator never listed.
        (Regression: ``*`` previously compiled to a one-digit class, so ``*67`` became
        ``^[0-9]67$`` -- it DENIED the listed ``*67`` and ALLOWED ``167``.)
        """
        allow = load_outbound_allowlist({"HERMES_VOIP_OUTBOUND_ALLOW": "*67"})
        assert is_outbound_allowed("*67", allow) is True
        assert is_outbound_allowed("167", allow) is False
        assert is_outbound_allowed("067", allow) is False
        assert is_outbound_allowed("967", allow) is False

    def test_star_service_codes_stay_exact(self) -> None:
        """Star-codes (``*82``, ``*98``, long ``*67...``) match only themselves."""
        allow = load_outbound_allowlist(
            {"HERMES_VOIP_OUTBOUND_ALLOW": "*82,*98,*6712345678"}
        )
        assert is_outbound_allowed("*82", allow) is True
        assert is_outbound_allowed("*98", allow) is True
        assert is_outbound_allowed("*6712345678", allow) is True
        assert is_outbound_allowed("282", allow) is False
        assert is_outbound_allowed("098", allow) is False

    def test_sip_uri_entry_is_exact_only(self) -> None:
        """SIP URI entries are EXACT-only: ``*`` is literal, so no ``.*`` host swallow.

        Security: a URI entry never compiles to a ``.*`` glob, so a listed
        ``sip:fax*@pbx.example.test`` cannot match a different user/host and there is
        no ReDoS surface. The entry matches only itself, verbatim.
        """
        allow = load_outbound_allowlist(
            {"HERMES_VOIP_OUTBOUND_ALLOW": "sip:fax*@pbx.example.test"}
        )
        assert is_outbound_allowed("sip:fax*@pbx.example.test", allow) is True
        assert is_outbound_allowed("sip:fax123@pbx.example.test", allow) is False

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


def _set_chat(monkeypatch: pytest.MonkeyPatch, call_id: str | None) -> None:
    """Monkeypatch ``_current_call_id`` so proactive tests can model no live call.

    In a NON-VoIP proactive session, the session-context ``HERMES_SESSION_CHAT_ID``
    is the ORIGIN chat id (e.g. ``telegram:chat-1000``), not a SIP Call-ID. The
    gate's no-live-call branch is keyed off :func:`_current_call_id`, so tests must
    override that helper directly to ``None`` while still exposing the origin via the
    fake ``gateway.session_context`` module above.
    """
    import hermes_voip.voip_tools as vt  # noqa: PLC0415

    monkeypatch.setattr(vt, "_current_call_id", lambda: call_id)


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
        _set_chat(monkeypatch, None)
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
        _set_chat(monkeypatch, None)
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
        _set_chat(monkeypatch, None)
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
        _set_chat(monkeypatch, None)
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
        _set_chat(monkeypatch, None)
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
        _set_chat(monkeypatch, None)
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
        from hermes_voip.outbound_allow import resolve_result_channel  # noqa: PLC0415

        dest = resolve_result_channel("telegram:chat-1000", None)
        assert dest == ("telegram", "chat-1000")

    def test_exact_channel_with_origin_uses_fixed_destination(self) -> None:
        """Exact entry is always the fixed destination regardless of origin."""
        from hermes_voip.outbound_allow import resolve_result_channel  # noqa: PLC0415

        dest = resolve_result_channel("telegram:chat-1000", ("telegram", "chat-9999"))
        assert dest == ("telegram", "chat-1000")

    def test_wildcard_channel_with_matching_origin_derives_destination(self) -> None:
        """Wildcard ``telegram:*`` + matching origin => origin is the destination."""
        from hermes_voip.outbound_allow import resolve_result_channel  # noqa: PLC0415

        dest = resolve_result_channel("telegram:*", ("telegram", "chat-1000"))
        assert dest == ("telegram", "chat-1000")

    def test_wildcard_channel_with_non_matching_origin_returns_none(self) -> None:
        """Wildcard ``telegram:*`` with a ``slack`` origin does NOT match => None."""
        from hermes_voip.outbound_allow import resolve_result_channel  # noqa: PLC0415

        dest = resolve_result_channel("telegram:*", ("slack", "chat-1000"))
        assert dest is None

    def test_wildcard_channel_with_no_origin_returns_none(self) -> None:
        """Wildcard ``telegram:*`` with no origin (cron path) => None (log-only)."""
        from hermes_voip.outbound_allow import resolve_result_channel  # noqa: PLC0415

        dest = resolve_result_channel("telegram:*", None)
        assert dest is None

    def test_none_channel_returns_none(self) -> None:
        """None channel string => None (unset => log-only, unchanged)."""
        from hermes_voip.outbound_allow import resolve_result_channel  # noqa: PLC0415

        assert resolve_result_channel(None, ("telegram", "chat-1000")) is None

    def test_blank_channel_returns_none(self) -> None:
        """Blank/empty channel string => None."""
        from hermes_voip.outbound_allow import resolve_result_channel  # noqa: PLC0415

        assert resolve_result_channel("", None) is None
        assert resolve_result_channel("  ", None) is None

    def test_wildcard_channel_matches_any_telegram_chat(self) -> None:
        """``telegram:*`` matches any telegram chat_id -- pattern is per-origin."""
        from hermes_voip.outbound_allow import resolve_result_channel  # noqa: PLC0415

        assert resolve_result_channel("telegram:*", ("telegram", "chat-9999")) == (
            "telegram",
            "chat-9999",
        )
        assert resolve_result_channel(
            "telegram:*", ("telegram", "chat-1234567890")
        ) == ("telegram", "chat-1234567890")
