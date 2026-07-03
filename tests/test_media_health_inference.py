"""Tests for media-health inference from a CallQuality snapshot (bk1295, ADR-0102).

`infer_media_anomalies` turns the RTCP-derived `CallQuality` (local = what WE
received; remote = what the PEER reported receiving from US) into structured
`one_way_audio` / `media_degraded` anomalies, emitted at teardown so a log pipeline
can distinguish a NAT-style one-directional media failure and a degraded-quality call
from a clean one — WITHOUT any packet-count engine-API expansion.

Pure function, no ml / no network: runs in the default gate.
"""

from __future__ import annotations

from hermes_voip.media.engine import (
    CallQuality,
    MediaAnomaly,
    infer_media_anomalies,
)


def _q(  # noqa: PLR0913 — keyword-only builder mirroring CallQuality's 7 fields
    *,
    local_fraction_lost: float | None = 0.0,
    local_cumulative_lost: int | None = 0,
    local_jitter_ms: float | None = 5.0,
    remote_fraction_lost: float | None = 0.0,
    remote_cumulative_lost: int | None = 0,
    remote_jitter_ms: float | None = 5.0,
    rtt_seconds: float | None = 0.1,
) -> CallQuality:
    """A CallQuality with healthy defaults; override the field(s) under test."""
    return CallQuality(
        local_fraction_lost=local_fraction_lost,
        local_cumulative_lost=local_cumulative_lost,
        local_jitter_ms=local_jitter_ms,
        remote_fraction_lost=remote_fraction_lost,
        remote_cumulative_lost=remote_cumulative_lost,
        remote_jitter_ms=remote_jitter_ms,
        rtt_seconds=rtt_seconds,
    )


def _events(anomalies: tuple[MediaAnomaly, ...]) -> set[str]:
    return {a.event for a in anomalies}


def _reasons(anomalies: tuple[MediaAnomaly, ...]) -> set[str]:
    return {a.reason for a in anomalies}


def test_healthy_call_infers_no_anomaly() -> None:
    """A clean bidirectional call (low loss + low jitter, both views) is silent."""
    assert infer_media_anomalies(_q()) == ()


def test_inbound_dead_is_one_way_no_inbound_rtp() -> None:
    """local=None while the PEER reported on our stream => inbound audio is dead.

    We received no RTP at all, but the peer's report proves it received ours — so
    this is a genuine one-way failure (peer->us dead), disambiguated from a too-short
    call by the presence of the remote report.
    """
    anomalies = infer_media_anomalies(
        _q(local_fraction_lost=None, local_jitter_ms=None)
    )
    assert _events(anomalies) == {"one_way_audio"}
    assert _reasons(anomalies) == {"no_inbound_rtp"}


def test_peer_near_total_loss_is_one_way_outbound() -> None:
    """We received the peer fine, but it reports near-total loss of OUR stream."""
    anomalies = infer_media_anomalies(_q(remote_fraction_lost=0.95))
    assert "one_way_audio" in _events(anomalies)
    assert "peer_no_inbound" in _reasons(anomalies)


def test_high_loss_is_media_degraded() -> None:
    """Loss above the degraded threshold (but not one-way) => media_degraded."""
    anomalies = infer_media_anomalies(_q(local_fraction_lost=0.12))
    assert _events(anomalies) == {"media_degraded"}
    assert _reasons(anomalies) == {"high_loss"}


def test_high_jitter_is_media_degraded() -> None:
    """Jitter above the degraded threshold => media_degraded high_jitter."""
    anomalies = infer_media_anomalies(_q(remote_jitter_ms=45.0))
    assert _events(anomalies) == {"media_degraded"}
    assert _reasons(anomalies) == {"high_jitter"}


def test_no_data_both_views_absent_is_silent() -> None:
    """Both views absent (a too-short call / no RTCP report) is NOT one-way.

    With neither a local nor a remote report there is no evidence either direction
    carried audio, so no anomaly is inferred.
    """
    anomalies = infer_media_anomalies(
        _q(
            local_fraction_lost=None,
            local_jitter_ms=None,
            remote_fraction_lost=None,
            remote_jitter_ms=None,
        )
    )
    assert anomalies == ()


def test_anomaly_is_frozen_with_event_and_reason() -> None:
    """MediaAnomaly exposes the event name + a non-sensitive reason category only."""
    anomalies = infer_media_anomalies(_q(local_fraction_lost=None))
    assert anomalies
    a = anomalies[0]
    assert isinstance(a.event, str)
    assert isinstance(a.reason, str)
