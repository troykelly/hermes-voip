"""Call-progress (fax / AMD) config surface (ADR-0064 wiring, #43).

Three master switches gate the merged :class:`CallProgressDetector` wiring:

* ``HERMES_VOIP_CALL_PROGRESS`` (default OFF) — the overall switch: when off, no
  detector is constructed and the feature is fully inert.
* ``HERMES_VOIP_AMD`` (default OFF) — answering-machine detection, only meaningful
  on outbound calls; off keeps fax detection while AMD stays silent.
* ``HERMES_VOIP_AMD_HANGUP_ON_FAX`` (default ON) — when a fax tone is detected,
  auto hang up rather than leave the line open for a conversation that cannot
  happen.

All three default to a safe posture (feature off; fax-hangup on when the feature
is turned on), so existing deployments are unchanged until the operator opts in.
"""

from __future__ import annotations

from hermes_voip.config import load_media_config


def test_call_progress_defaults_off() -> None:
    """The call-progress feature and AMD are OFF by default; fax-hangup is ON."""
    cfg = load_media_config({})
    assert cfg.enable_call_progress is False
    assert cfg.enable_amd is False
    assert cfg.amd_hang_up_on_fax is True


def test_call_progress_enabled_from_env() -> None:
    """The overall switch turns on from env."""
    cfg = load_media_config({"HERMES_VOIP_CALL_PROGRESS": "true"})
    assert cfg.enable_call_progress is True
    # AMD still defaults off even when call-progress is on.
    assert cfg.enable_amd is False


def test_amd_enabled_from_env() -> None:
    """AMD turns on independently from env."""
    cfg = load_media_config(
        {"HERMES_VOIP_CALL_PROGRESS": "true", "HERMES_VOIP_AMD": "true"}
    )
    assert cfg.enable_call_progress is True
    assert cfg.enable_amd is True


def test_amd_hang_up_on_fax_overridable_off() -> None:
    """The fax-hangup behaviour can be turned off explicitly."""
    cfg = load_media_config({"HERMES_VOIP_AMD_HANGUP_ON_FAX": "false"})
    assert cfg.amd_hang_up_on_fax is False
