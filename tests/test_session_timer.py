"""Unit tests for the sans-IO RFC 4028 session-timer logic (#74, ADR-0071).

:mod:`hermes_voip.session_timer` is pure, fully-typed, off-the-event-loop logic:
it parses the ``Session-Expires`` (delta + optional ``;refresher=uac|uas``) and
``Min-SE`` header strings, elects the refresher, computes the refresher's refresh
interval (SE/2) and the non-refresher's teardown deadline (SE - min(32, SE/3)),
decides whether an inbound ``Session-Expires`` is too small (< our ``Min-SE`` →
``422 Session Interval Too Small``), and renders the outbound header values. The
RFC math lives here so it can be exhaustively unit-tested without a live dialog —
this module imports no asyncio, socket, or adapter symbol.

All values are obvious fakes; no host/extension/secret appears (the repo is
PUBLIC). These run in the DEFAULT gate (no optional extra needed).
"""

from __future__ import annotations

import pytest

from hermes_voip.session_timer import (
    MIN_SE_FLOOR,
    AcceptTimers,
    Refresher,
    Reject422,
    SessionExpires,
    build_session_expires_value,
    elect_refresher,
    negotiate_uas_timers,
    parse_min_se,
    refresh_interval_secs,
    teardown_deadline_secs,
)

# ---------------------------------------------------------------------------
# The RFC 4028 floor.
# ---------------------------------------------------------------------------


def test_min_se_floor_is_ninety_seconds() -> None:
    """RFC 4028 §4/§5: the absolute minimum Session-Expires/Min-SE is 90 s."""
    assert MIN_SE_FLOOR == 90


# ---------------------------------------------------------------------------
# Session-Expires parsing (delta + optional refresher param).
# ---------------------------------------------------------------------------


def test_parse_session_expires_bare_delta_has_no_refresher() -> None:
    """A bare ``Session-Expires: 1800`` parses to delta 1800, no refresher."""
    se = SessionExpires.parse("1800")
    assert se.delta == 1800
    assert se.refresher is None


def test_parse_session_expires_with_refresher_uac() -> None:
    """``1800;refresher=uac`` parses to delta 1800 and refresher UAC."""
    se = SessionExpires.parse("1800;refresher=uac")
    assert se.delta == 1800
    assert se.refresher is Refresher.UAC


def test_parse_session_expires_with_refresher_uas() -> None:
    """``600;refresher=uas`` parses to delta 600 and refresher UAS."""
    se = SessionExpires.parse("600;refresher=uas")
    assert se.delta == 600
    assert se.refresher is Refresher.UAS


def test_parse_session_expires_tolerates_whitespace_and_case() -> None:
    """Surrounding whitespace and a mixed-case refresher token both parse."""
    se = SessionExpires.parse("  1200 ; Refresher = UAS ")
    assert se.delta == 1200
    assert se.refresher is Refresher.UAS


def test_parse_session_expires_ignores_unknown_params() -> None:
    """An unrelated parameter (e.g. a future extension) is ignored, not fatal."""
    se = SessionExpires.parse("900;refresher=uac;foo=bar")
    assert se.delta == 900
    assert se.refresher is Refresher.UAC


def test_parse_session_expires_compact_uses_the_same_parser() -> None:
    """The compact-form ``x`` header carries the same value grammar as the long form."""
    se = SessionExpires.parse("450;refresher=uas")
    assert se.delta == 450
    assert se.refresher is Refresher.UAS


@pytest.mark.parametrize("bad", ["", "   ", "abc", "-5", "1800;refresher=bogus", ";"])
def test_parse_session_expires_rejects_malformed(bad: str) -> None:
    """A non-numeric delta, a negative delta, or a bad refresher token raises."""
    with pytest.raises(ValueError):  # noqa: PT011 — module raises bare ValueError
        SessionExpires.parse(bad)


# ---------------------------------------------------------------------------
# Min-SE parsing.
# ---------------------------------------------------------------------------


def test_parse_min_se_plain() -> None:
    """``Min-SE: 120`` parses to 120."""
    assert parse_min_se("120") == 120


def test_parse_min_se_strips_params_and_whitespace() -> None:
    """A Min-SE with a trailing generic param and whitespace still yields the delta."""
    assert parse_min_se("  300 ;foo=bar ") == 300


@pytest.mark.parametrize("bad", ["", "abc", "-1", ";"])
def test_parse_min_se_rejects_malformed(bad: str) -> None:
    """A non-numeric or negative Min-SE raises."""
    with pytest.raises(ValueError):  # noqa: PT011 — module raises bare ValueError
        parse_min_se(bad)


# ---------------------------------------------------------------------------
# Refresher election.
# ---------------------------------------------------------------------------


def test_elect_refresher_honours_an_explicit_peer_choice_uac() -> None:
    """When the peer pinned ``refresher=uac`` we MUST NOT override it (RFC 4028 §9)."""
    assert elect_refresher(peer=Refresher.UAC, default=Refresher.UAS) is Refresher.UAC


def test_elect_refresher_honours_an_explicit_peer_choice_uas() -> None:
    """A peer that pinned ``refresher=uas`` keeps that choice."""
    assert elect_refresher(peer=Refresher.UAS, default=Refresher.UAC) is Refresher.UAS


def test_elect_refresher_falls_back_to_default_when_peer_absent() -> None:
    """No peer refresher param → the UAS picks the configured default (RFC 4028 §9)."""
    assert elect_refresher(peer=None, default=Refresher.UAS) is Refresher.UAS
    assert elect_refresher(peer=None, default=Refresher.UAC) is Refresher.UAC


# ---------------------------------------------------------------------------
# Refresh interval (SE/2 for the refresher) — RFC 4028 §7.2/§9.
# ---------------------------------------------------------------------------


def test_refresh_interval_is_half_the_session_interval() -> None:
    """The refresher refreshes at SE/2."""
    assert refresh_interval_secs(1800) == 900.0
    assert refresh_interval_secs(600) == 300.0


def test_refresh_interval_at_the_floor() -> None:
    """At the RFC floor SE=90 the refresh fires at 45 s."""
    assert refresh_interval_secs(90) == 45.0


# ---------------------------------------------------------------------------
# Teardown deadline (SE - min(32, SE/3)) for the non-refresher — RFC 4028 §10.
# ---------------------------------------------------------------------------


def test_teardown_deadline_large_interval_uses_the_32_second_cap() -> None:
    """For a large SE the guard band is capped at 32 s (SE/3 > 32)."""
    # SE/3 = 600 > 32, so the band is 32: deadline = 1800 - 32.
    assert teardown_deadline_secs(1800) == 1800 - 32


def test_teardown_deadline_small_interval_uses_one_third() -> None:
    """For a small SE the guard band is SE/3 (which is < 32)."""
    # SE=90: SE/3 = 30 < 32, so the band is 30: deadline = 90 - 30 = 60.
    assert teardown_deadline_secs(90) == 90 - 30.0


def test_teardown_deadline_boundary_where_third_equals_32() -> None:
    """At SE=96, SE/3 == 32 exactly; min(32, 32) = 32, deadline = 64."""
    assert teardown_deadline_secs(96) == 96 - 32


def test_teardown_deadline_is_always_before_expiry() -> None:
    """The non-refresher's BYE deadline is strictly earlier than the full interval."""
    for delta in (90, 96, 120, 600, 1800, 3600):
        assert teardown_deadline_secs(delta) < float(delta)


# ---------------------------------------------------------------------------
# The 422 decision boundary (inbound SE vs our Min-SE).
# ---------------------------------------------------------------------------


def test_negotiate_uas_rejects_se_below_min_se() -> None:
    """An inbound SE strictly below our Min-SE → 422 carrying our Min-SE."""
    result = negotiate_uas_timers(
        offered=SessionExpires(delta=89, refresher=None),
        min_se=90,
        local_se=600,
        default_refresher=Refresher.UAS,
    )
    assert isinstance(result, Reject422)
    assert result.min_se == 90


def test_negotiate_uas_accepts_se_equal_to_min_se() -> None:
    """An inbound SE EXACTLY at our Min-SE is accepted (the boundary is inclusive)."""
    result = negotiate_uas_timers(
        offered=SessionExpires(delta=90, refresher=None),
        min_se=90,
        local_se=600,
        default_refresher=Refresher.UAS,
    )
    assert isinstance(result, AcceptTimers)
    # RFC 4028 §9: the UAS MUST NOT increase the offered value; 90 is honoured.
    assert result.delta == 90


def test_negotiate_uas_accepts_se_above_min_se() -> None:
    """An inbound SE above our Min-SE is accepted unchanged (we never raise it)."""
    result = negotiate_uas_timers(
        offered=SessionExpires(delta=1800, refresher=None),
        min_se=90,
        local_se=600,
        default_refresher=Refresher.UAS,
    )
    assert isinstance(result, AcceptTimers)
    # RFC 4028 §9: the UAS MUST NOT increase the offered value. 1800 > our 600 but
    # we must not bump it down to 600 either — we accept the peer's value as-is when
    # it is above Min-SE (a UAS MAY reduce, but our policy honours the peer's choice).
    assert result.delta == 1800


def test_negotiate_uas_picks_default_refresher_when_peer_absent() -> None:
    """No peer refresher param → the accepted result carries our default (UAS)."""
    result = negotiate_uas_timers(
        offered=SessionExpires(delta=600, refresher=None),
        min_se=90,
        local_se=600,
        default_refresher=Refresher.UAS,
    )
    assert isinstance(result, AcceptTimers)
    assert result.refresher is Refresher.UAS


def test_negotiate_uas_keeps_peer_refresher_choice() -> None:
    """A peer that pinned refresher=uac keeps it in the result (we never override)."""
    result = negotiate_uas_timers(
        offered=SessionExpires(delta=600, refresher=Refresher.UAC),
        min_se=90,
        local_se=600,
        default_refresher=Refresher.UAS,
    )
    assert isinstance(result, AcceptTimers)
    assert result.refresher is Refresher.UAC


def test_negotiate_uas_no_offer_adds_our_own_se() -> None:
    """No inbound Session-Expires but we want timers → AcceptTimers with our local SE.

    RFC 4028 §9: a UAS that supports timers MAY insert a Session-Expires into its
    2xx even when the request carried none — using our configured interval and our
    default refresher.
    """
    result = negotiate_uas_timers(
        offered=None,
        min_se=90,
        local_se=600,
        default_refresher=Refresher.UAS,
    )
    assert isinstance(result, AcceptTimers)
    assert result.delta == 600
    assert result.refresher is Refresher.UAS


def test_negotiate_uas_no_offer_below_floor_local_se_is_invalid() -> None:
    """A misconfigured local SE below the floor is a construction error, not silent."""
    with pytest.raises(ValueError):  # noqa: PT011 — module raises bare ValueError
        negotiate_uas_timers(
            offered=None,
            min_se=90,
            local_se=10,
            default_refresher=Refresher.UAS,
        )


# ---------------------------------------------------------------------------
# Outbound header rendering.
# ---------------------------------------------------------------------------


def test_build_session_expires_value_with_refresher() -> None:
    """The rendered header value is ``<delta>;refresher=<role>`` (lowercase role)."""
    assert build_session_expires_value(600, Refresher.UAS) == "600;refresher=uas"
    assert build_session_expires_value(1800, Refresher.UAC) == "1800;refresher=uac"


def test_build_session_expires_value_round_trips_through_parse() -> None:
    """A rendered value parses back to the same delta + refresher (no drift)."""
    rendered = build_session_expires_value(1234, Refresher.UAC)
    parsed = SessionExpires.parse(rendered)
    assert parsed.delta == 1234
    assert parsed.refresher is Refresher.UAC
